"""`golf` command line: every data path in, review, build, serve, MCP, privacy.

The pipeline functions at the top (import_18b, extract_files, import_notebook_files, status_info, ...)
are shared with the web app, so the terminal and the browser do exactly the same thing; the Typer
commands below them only parse arguments and print.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Iterable, Iterator, List, Optional
from zoneinfo import ZoneInfo

import typer

from golf import config as config_mod
from golf import llm
from golf.config import Config
from golf.db import connect, loads

WEB_URL = "http://127.0.0.1:8765"
NOTES_RUNNER = None          # tests inject a fake osascript runner here; None = the real Notes app
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".gif"}
EXPORT_DIR = Path("raw") / "18birdies"
EXPORT_URL = "https://18birdies.com/download-account-data/"
DATA_DIRS = ("raw/18birdies", "raw/screenshots", "raw/notebook", "inbox/screenshots", "inbox/notebook", "site",
             "logs")
_DATE_IN_NAME = re.compile(r"(19|20)\d{6}")

EXPORT_HELP = """\
To get your 18Birdies export:
  1. Sign in at https://18birdies.com/download-account-data/ (email + password, or phone + SMS code).
     If you only ever signed in to the app with Apple or Google, first add an email or phone in the app's Settings.
  2. Click "Request My Data"; the browser saves 18Birdies_archive.json into Downloads.
  3. That's it when the watcher runs (`golf watch install`, or `golf serve`): it is imported automatically.
     By hand: golf import-18b ~/Downloads/18Birdies_archive.json (a dated copy is kept in {export_dir}/).
The file holds your email, phone and friends list; the copy stays under data/, which is never committed."""

KEY_HELP = """\
Claude API unavailable: {error}
Screenshot, note and notebook extraction call the Claude API. To enable it:
  1. Create a key at https://console.anthropic.com/settings/keys
  2. In {root}: cp .env.example .env, then set ANTHROPIC_API_KEY=... in .env (gitignored).
  3. Run the command again (restart `golf serve` if it is running). Re-runs never re-bill work already done."""

TRANSIENT_HEADLINE = "Claude API unreachable or busy; re-run later"
TRANSIENT_HELP = """\
{headline} (work already finished is kept, and re-runs never re-bill it).
  Details: {error}
  If it keeps happening: check the network, https://status.anthropic.com, and [llm].model in config.toml."""
_TRANSIENT_WORDS = ("unreachable", "busy", "overloaded", "rate limit", "rate-limit", "could not reach", "connection",
                    "timed out", "timeout", "right now", "try again", "re-run later", "re-run the same command later")


COURSES_STARTER = """\
# courses.yaml: your course reference data (ratings, slopes, par and stroke index), checked by hand.
# Format and a worked example: courses.example.yaml (synthetic; do not copy its club here).
#   golf courses check                 which clubs need an entry (club ids come from your 18Birdies export)
#   golf courses autofill <club_id>    proposes an entry from OpenGolfAPI; confirm it against the scorecard
#   golf courses sync                  loads this file and recomputes the handicap index
clubs: {}
"""

ENTITIES_STARTER = """\
# entities.yaml: names the note extractor should know. Format: entities.example.yaml (placeholders).
# Coaches (with nicknames and initials), practice places, and what is in the bag now, e.g.
#   coaches: [{name: Full Name, aliases: [First], initials: [FN]}]
#   locations: [{name: Range Name, kind: range, aliases: [short name]}]
#   bag: ["Driver: brand model loft"]
# Editing this file re-extracts notes on the next sync (served from the cache where nothing changed).
coaches: []
locations: []
bag: []
"""


# ================================================================ shared pipeline
def get_cfg() -> Config:
    return config_mod.get_config()


@contextmanager
def open_db(cfg: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def api_key_set(cfg: Config | None = None) -> bool:
    """Whether a key is available (re-reads .env so a key added while `golf serve` runs is picked up)."""
    if cfg is not None:
        config_mod.load_dotenv(Path(cfg.root) / ".env")
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def key_help(cfg: Config, error: Exception | str) -> str:
    return KEY_HELP.format(error=str(error), root=cfg.root)


def is_transient(error: Exception | str) -> bool:
    """A network / overload / unknown-model failure rather than a missing or rejected key. Summaries carry
    only the message text, so for strings the wording decides."""
    transient = getattr(llm, "LLMTransient", None)
    if transient is not None and isinstance(error, transient):
        return True
    if isinstance(error, BaseException) and not isinstance(error, llm.LLMUnavailable):
        return False
    text = str(error).lower()
    return "api key" not in text and any(w in text for w in _TRANSIENT_WORDS)


def llm_help(cfg: Config, error: Exception | str) -> str:
    """What to tell Shane when the Claude API could not be used: try later (busy, unreachable), fix the key
    (missing, rejected, not allowed), or fix what the message names (e.g. an unknown [llm].model)."""
    if is_transient(error):
        return TRANSIENT_HELP.format(headline=TRANSIENT_HEADLINE, error=str(error))
    text = str(error)
    if re.search(r"\bkey\b|auth|credential", text, re.IGNORECASE):
        return key_help(cfg, error)
    return f"Claude API unavailable: {text}"


def llm_headline(error: Exception | str) -> str:
    return TRANSIENT_HEADLINE + "." if is_transient(error) else "Claude API unavailable."


def export_help(cfg: Config) -> str:
    return EXPORT_HELP.format(export_dir=_display_path(cfg, cfg.data_dir / EXPORT_DIR))


def _display_path(cfg: Config, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(cfg.root).resolve()))
    except ValueError:
        return str(p)


def _today(cfg: Config) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


def _stamp(cfg: Config) -> str:
    return datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y%m%d-%H%M%S")


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in range(2, 10_000):
        cand = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(dest)


def expand_images(paths: Iterable[Path | str]) -> list[Path]:
    """Files as given plus the images inside any directory (recursively), skipping dotfiles."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith(".")
                          and f.suffix.lower() in IMAGE_EXTS)
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(f"No such file or folder: {p}")
    return out


def stage_into_data(cfg: Config, paths: list[Path], sub: str, label: str,
                    created: list[Path] | None = None) -> list[Path]:
    """Copy files that live outside data/ into data/<sub>/<label>/ (the review page only serves files
    under data/). Files already under data/ are used where they are; order is kept. New copies are
    appended to `created`, so a caller can take them back out if the run fails."""
    out = []
    for p in paths:
        if _inside(p, cfg.data_dir):
            out.append(p)
            continue
        dest_dir = cfg.data_dir / sub / label
        dest_dir.mkdir(parents=True, exist_ok=True)
        same = next((q for q in dest_dir.iterdir() if q.name == p.name and _sha(q) == _sha(p)), None)
        if same is None:
            same = _unique(dest_dir / p.name)
            shutil.copy2(p, same)
            if created is not None:
                created.append(same)
        out.append(same)
    return out


def unstage(cfg: Config, copies: Iterable[Path], sub: str) -> None:
    """Remove copies a failed run made under data/<sub>/ (and the label folders that leaves empty)."""
    base = (cfg.data_dir / sub).resolve()
    for p in copies:
        if not _inside(p, base):
            continue
        p.unlink(missing_ok=True)
        folder = p.parent.resolve()
        while folder != base and _inside(folder, base) and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent


# ---------------------------------------------------------------- 18Birdies snapshots
def export_files(cfg: Config) -> list[Path]:
    """Snapshots under data/raw/18birdies/, oldest first by snapshot date (the date in the name, else
    the file's mtime), then mtime. Never by name alone: '..._20260925-2.json' sorts before '.json'."""
    from golf.ingest.birdies_export import snapshot_date

    folder = cfg.data_dir / EXPORT_DIR
    if not folder.is_dir():
        return []
    tz = ZoneInfo(cfg.timezone)
    files = [p for p in folder.glob("*.json") if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: (snapshot_date(p, tz), p.stat().st_mtime, p.name))


def latest_export_file(cfg: Config) -> Path | None:
    files = export_files(cfg)
    return files[-1] if files else None


def export_name(cfg: Config, src: Path, *, when: date | None = None) -> str:
    """18Birdies_archive_YYYYMMDD.json: the date already in the name, else `when`, else the file's mtime
    (local): the download time, which is when 18Birdies made the snapshot."""
    m = _DATE_IN_NAME.search(src.name)
    if m:
        stamp = m.group(0)
    else:
        day = when or datetime.fromtimestamp(src.stat().st_mtime, ZoneInfo(cfg.timezone)).date()
        stamp = day.strftime("%Y%m%d")
    return f"18Birdies_archive_{stamp}.json"


def stored_export_with_sha(cfg: Config, sha: str) -> Path | None:
    return next((p for p in export_files(cfg) if _sha(p) == sha), None)


def store_export(cfg: Config, src: Path, *, name: str | None = None, move: bool = False) -> Path:
    """Keep every snapshot under data/raw/18birdies/ (re-parsable later; never committed), named
    18Birdies_archive_YYYYMMDD.json. The same bytes already stored are reused, whatever their name; a
    different file is never overwritten (a -2 suffix is added). The copy is written under a temporary
    name first, so a half-copied file never looks like a snapshot."""
    src = Path(src)
    if _inside(src, cfg.data_dir / EXPORT_DIR):
        return src
    same = stored_export_with_sha(cfg, _sha(src))
    if same is not None:
        if move:
            src.unlink(missing_ok=True)
        return same
    dest_dir = cfg.data_dir / EXPORT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir / (name or export_name(cfg, src)))
    tmp = dest_dir / f".incoming-{os.getpid()}-{dest.name}"
    if move:
        shutil.move(str(src), str(tmp))
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    return dest


def check_export(path: Path) -> None:
    """ExportFormatError unless the file is JSON shaped like an 18Birdies archive. Nothing is kept."""
    from golf.ingest.birdies_export import load_archive, parse_archive

    parse_archive(load_archive(path), "UTC")


def store_upload(cfg: Config, tmp: Path, filename: str | None) -> Path:
    """A browser upload saved at `tmp`: validated first (a failed upload leaves nothing behind), then
    stored like any snapshot (de-duplicated by content; dated by the name, else today)."""
    try:
        check_export(tmp)
        name = export_name(cfg, Path(filename or "18Birdies_archive.json"), when=_today(cfg))
        return store_export(cfg, tmp, name=name, move=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


# ---------------------------------------------------------------- settings kept in golf.db
def include_nine_setting(conn: sqlite3.Connection) -> bool:
    """Whether 9-hole rounds count toward the HI (meta 'whs_include_nine'; default: yes)."""
    from golf import whs

    return whs.include_nine_setting(conn)


def import_18b(conn: sqlite3.Connection, cfg: Config, path: Path, *, sync: bool = True,
               new_account: bool = False) -> dict[str, Any]:
    """The whole export pipeline: import the snapshot, map courses (if courses.yaml exists), fold
    screenshot rounds into export rounds, recompute the unofficial HI, and link notes to rounds.
    Synthetic demo rows are removed first: a real import would otherwise mark them deleted.
    Another 18Birdies account's export is refused (ExportAccountError) before a copy is kept, unless
    new_account=True."""
    from golf.courses import CoursesFileError, default_courses_path, sync_courses
    from golf.demo import clear_demo, has_demo
    from golf.extract.rounds import reconcile_screenshot_rounds
    from golf.ingest.birdies_export import foreign_account_error, import_export, preview_export
    from golf.notes import link_rounds

    out: dict[str, Any] = {"demo_cleared": False, "stored_as": None, "courses": None, "courses_error": None}
    if not new_account and preview_export(conn, Path(path), cfg)["account"] == "different":
        raise foreign_account_error(path)     # someone else's personal data: not even copied under data/
    if not _inside(Path(path), cfg.data_dir / EXPORT_DIR):
        check_export(Path(path))              # junk is refused before a copy is kept under data/
    stored = store_export(cfg, Path(path))
    out["stored_as"] = str(stored)
    if has_demo(conn):
        clear_demo(conn)
        out["demo_cleared"] = True
    out["import"] = import_export(conn, stored, cfg, new_account=new_account)
    yaml_path = default_courses_path(cfg)
    if sync and yaml_path.exists():
        try:
            out["courses"] = sync_courses(conn, yaml_path)
        except CoursesFileError as e:        # a typo in courses.yaml must not undo the import
            out["courses_error"] = str(e)
    out["courses_yaml"] = str(yaml_path) if yaml_path.exists() else None
    out["reconcile"] = reconcile_screenshot_rounds(conn)
    out["whs"] = recompute_hi(conn, cfg)
    out["rounds_linked"] = link_rounds(conn)
    conn.commit()
    return out


def recompute_hi(conn: sqlite3.Connection, cfg: Config, include_nine: bool | None = None) -> dict[str, Any]:
    """whs.recompute with the stored 9-hole choice (meta 'whs_include_nine', default on), so an import,
    a review or a courses sync never silently flips it back. An explicit value applies to this run only."""
    from golf import whs

    return whs.recompute(conn, cfg, include_nine=include_nine)


def refresh_derived(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """After a round is accepted, rejected or corrected: map it to courses.yaml, recompute the HI and
    link same-day notes, so the dashboard never shows a stale index. All idempotent and cheap."""
    from golf.courses import CoursesFileError, default_courses_path, sync_courses
    from golf.notes import link_rounds

    out: dict[str, Any] = {"courses": None, "courses_error": None}
    yaml_path = default_courses_path(cfg)
    if yaml_path.exists():
        try:
            out["courses"] = sync_courses(conn, yaml_path)
        except CoursesFileError as e:
            out["courses_error"] = str(e)
    out["whs"] = recompute_hi(conn, cfg)
    out["rounds_linked"] = link_rounds(conn)
    conn.commit()
    return out


def resolve_round(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """A counted round by id, id prefix (6+ characters) or date played (YYYY-MM-DD, when that day has
    one round). ValueError says what matched instead."""
    ref = (ref or "").strip()
    sql = ("SELECT v.round_id, v.played_on_local, v.played_at_utc, v.holes_played, v.gross, v.putts, "
           "COALESCE(c.name, cl.name, 'unmapped club') AS place FROM v_rounds v "
           "LEFT JOIN courses c ON c.course_key = v.course_key_eff LEFT JOIN clubs cl ON cl.club_id = v.club_id ")
    rows = conn.execute(sql + "WHERE v.round_id = ?", (ref,)).fetchall()
    if not rows and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref):
        rows = conn.execute(sql + "WHERE v.played_on_local = ? ORDER BY v.played_at_utc", (ref,)).fetchall()
    elif not rows and len(ref) >= 6:
        rows = conn.execute(sql + "WHERE v.round_id LIKE ? ORDER BY v.played_at_utc", (ref + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ValueError(f"No counted round matches {ref!r} (use the round id, or the date played as YYYY-MM-DD).")
    listed = "; ".join(f"{r['round_id']} ({r['place']}, {r['gross']} over {r['holes_played']})" for r in rows)
    raise ValueError(f"{len(rows)} rounds match {ref!r}; give the round id: {listed}")


def set_putts_override(conn: sqlite3.Connection, ref: str, value: str) -> dict[str, Any]:
    """Shane's call on a round's putt total: 'full' (entered on every hole), 'partial' (some holes only)
    or 'auto' (the importer's rule). Kept in round_overrides, which imports never touch."""
    if value not in ("full", "partial", "auto"):
        raise ValueError("Say full, partial or auto.")
    rnd = resolve_round(conn, ref)
    conn.execute("INSERT INTO round_overrides(round_id, putts) VALUES (?, ?) "
                 "ON CONFLICT(round_id) DO UPDATE SET putts = excluded.putts",
                 (rnd["round_id"], None if value == "auto" else value))
    conn.commit()
    return {"round_id": rnd["round_id"], "date": rnd["played_on_local"], "place": rnd["place"],
            "putts": rnd["putts"], "holes": rnd["holes_played"], "setting": value}


def format_import(out: dict[str, Any]) -> str:
    imp, lines = out["import"], []
    if out.get("demo_cleared"):
        lines.append("Removed the synthetic demo data first.")
    if imp.get("already_imported"):
        lines.append(f"{imp['file']}: already imported (same file); nothing changed.")
    else:
        modes = ", ".join(f"{k} {v}" for k, v in sorted((imp.get("by_entry_mode") or {}).items()))
        lines.append(f"{imp['file']} (snapshot {imp.get('snapshot_date')}): {imp['rounds_in_file']} rounds"
                     + (f" ({modes})" if modes else "") + ".")
        lines.append(f"  inserted {imp['inserted']}, updated {imp['updated']}, unchanged {imp['unchanged']}, "
                     f"restored {imp['restored']}, deleted in 18Birdies {imp['deleted_in_source']}, "
                     f"flagged {imp['flagged']}, clubs {imp['clubs']}.")
    for w in imp.get("warnings") or []:
        lines.append(f"  Warning: {w}")
    if imp.get("unknown_keys"):
        lines.append("  New keys in the export (logged, not imported): " + ", ".join(imp["unknown_keys"]))
    c = out.get("courses")
    if c:
        lines.append(f"Courses: {c['courses']} course(s), {c['tees']} tee(s); rounds mapped {c['rounds_mapped']}, "
                     f"unmapped {c['rounds_unmapped']}; par/SI filled on {c['holes_filled']} holes.")
    elif out.get("courses_error"):
        lines.append(f"Courses: courses.yaml was not loaded ({out['courses_error']}); fix it and run "
                     "`golf courses sync`.")
    elif out.get("courses_yaml") is None:
        lines.append("Courses: no courses.yaml yet, so no ratings: run `golf init` (writes an empty one) and "
                     "`golf courses check` to see which clubs need one.")
    merged = [a for a in out.get("reconcile") or [] if a.get("action") == "merged"]
    rematched = [a for a in out.get("reconcile") or [] if a.get("action") == "rematched"]
    if merged or rematched:
        lines.append(f"Screenshots: {len(merged)} screenshot round(s) merged into export rounds, "
                     f"{len(rematched)} pending extraction(s) re-matched.")
    lines.append(format_whs(out["whs"]))
    return "\n".join(lines)


def format_whs(w: dict[str, Any]) -> str:
    hi = w.get("handicap_index")
    skipped = ", ".join(f"{k} {v}" for k, v in sorted((w.get("skipped") or {}).items()))
    nine = w.get("include_nine")
    return (f"Handicap (unofficial, PCC 0): {w['scored']} of {w['rounds_considered']} rounds scored; "
            + (f"index {hi:.1f}" if hi is not None else
               "no index yet (" + (f"{w['holes_needed']} more holes needed: " if w.get("holes_needed") else "")
               + "the first index needs 54 holes of scores on rated courses)")
            + (f", low {w['low_hi']:.1f}" if w.get("low_hi") is not None else "")
            + (f". Skipped: {skipped}." if skipped else ".")
            + ("" if nine is None else " 9-hole rounds " + ("included." if nine else "left out.")))


def extract_files(conn: sqlite3.Connection, cfg: Config, paths: list[Path], *, played_on: str | None = None,
                  club_id: str | None = None, expected_holes: int | None = None, force: bool = False,
                  reread: bool = True) -> dict[str, Any]:
    """One round's screenshots, in capture order, copied under data/ if needed, then extracted."""
    from golf.extract import inbox, rounds

    images = expand_images(paths)
    if not images:
        raise ValueError("No images given (PNG, JPEG, HEIC, WebP or GIF).")
    rounds.check_readable(images)     # before anything is copied or sent
    images = [p for _, _, p in sorted((inbox.capture_time(p, cfg), p.name, p) for p in images)]
    if not force and rounds.find_extraction(conn, [_sha(p) for p in images]) is not None:
        staged = images               # seen before: the stored result comes back; nothing to copy or send
        return rounds.extract_round(conn, cfg, staged, expected_holes=expected_holes, played_on=played_on,
                                    club_id=club_id, force=force, reread=reread)
    created: list[Path] = []
    staged = stage_into_data(cfg, images, "raw/screenshots", played_on or f"cli-{_stamp(cfg)}", created)
    try:
        return rounds.extract_round(conn, cfg, staged, expected_holes=expected_holes, played_on=played_on,
                                    club_id=club_id, force=force, reread=reread)
    except BaseException:
        if created and rounds.find_extraction(conn, [_sha(p) for p in staged]) is None:
            unstage(cfg, created, "raw/screenshots")   # no copies pile up on retries
        raise


def import_notebook_files(conn: sqlite3.Connection, cfg: Config, paths: list[Path], *, force: bool = False,
                          from_inbox: bool = False) -> dict[str, Any]:
    """Notebook page photos -> transcription + events (all pending review).

    Photos are kept under data/raw/notebook/<stamp>/ so the review page can show them. Photos taken
    from the inbox are moved there first and moved back if their import did not complete (no API
    key, unusable output), so the inbox stays the retry queue."""
    from golf.notes import import_notebook_pages

    images = expand_images(paths)
    if not images:
        raise ValueError("No images given (PNG, JPEG, HEIC, WebP or GIF).")
    label = _stamp(cfg)
    if from_inbox:
        dest_dir = cfg.data_dir / "raw" / "notebook" / label
        dest_dir.mkdir(parents=True, exist_ok=True)
        origin = {}
        staged = []
        for p in images:
            target = _unique(dest_dir / p.name)
            shutil.move(str(p), str(target))
            origin[target] = p
            staged.append(target)
    else:
        created: list[Path] = []
        staged = stage_into_data(cfg, images, "raw/notebook", label, created)
    try:
        summary = import_notebook_pages(conn, cfg, staged, force=force)
    except BaseException:
        if from_inbox:
            for p in staged:
                if p.exists():
                    shutil.move(str(p), str(origin[p]))
        else:
            unstage(cfg, created, "raw/notebook")
        raise

    def done(p: Path) -> bool:
        row = conn.execute("SELECT extracted_key FROM source_docs WHERE doc_id = ?",
                           ("nb-" + _sha(p)[:16],)).fetchone()
        return row is not None and row[0] is not None

    if from_inbox:
        returned = []
        for p in staged:
            if not done(p):
                origin[p].parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(origin[p]))
                returned.append(str(origin[p]))
        summary["returned_to_inbox"] = returned
    else:                             # pages that did not complete leave no copy behind; re-run the command
        unstage(cfg, [p for p in created if not done(p)], "raw/notebook")
    return summary


def format_notebook(s: dict[str, Any]) -> str:
    e = s["events"]
    lines = [f"Notebook: {s['pages']} page(s): {s['new']} new, {s['reread']} re-read, {s['unchanged']} unchanged; "
             f"{e['new']} new event(s), all waiting for review. API: {s['api_calls']} call(s), ${s['cost_usd']:.2f}."]
    lines += [f"Error ({Path(x['path']).name}): {x['error']}" for x in s["errors"]
              if x["error"] != s.get("llm_unavailable")]
    if s.get("returned_to_inbox"):
        lines.append(f"{len(s['returned_to_inbox'])} photo(s) left in the inbox to retry.")
    return "\n".join(lines)


def notebook_inbox_files(cfg: Config) -> list[Path]:
    folder = cfg.inbox_dir / "notebook"
    return expand_images([folder]) if folder.is_dir() else []


def process_inboxes(conn: sqlite3.Connection, cfg: Config, *, dry_run: bool = False,
                    reread: bool = True) -> dict[str, Any]:
    """data/inbox/screenshots (one round per folder / capture burst) and data/inbox/notebook."""
    from golf.extract import inbox

    out: dict[str, Any] = {"screenshots": inbox.process_inbox(conn, cfg, dry_run=dry_run, reread=reread),
                           "notebook": None, "notebook_files": [str(p) for p in notebook_inbox_files(cfg)]}
    if out["notebook_files"] and not dry_run:
        out["notebook"] = import_notebook_files(conn, cfg, [Path(p) for p in out["notebook_files"]], from_inbox=True)
    return out


def format_screenshot_outcome(o: dict[str, Any]) -> str:
    flags = o.get("flags") or []
    n_e = sum(f.get("severity") == "E" for f in flags)
    n_w = sum(f.get("severity") == "W" for f in flags)
    return (f"extraction {o['extraction_id']}: {o['status']}, round {o['round_id'] or '(no date yet)'}, "
            f"{n_e} error(s), {n_w} warning(s), ${o['cost_usd']:.2f}{' (cached)' if o.get('cached') else ''}")


def format_inbox(out: dict[str, Any]) -> str:
    lines = []
    groups = out["screenshots"]
    if not groups:
        lines.append("Screenshot inbox: empty.")
    for g in groups:
        head = f"{g['key']} ({g['n_images']} image(s)): {g['action']}"
        if g.get("outcome"):
            head += " - " + format_screenshot_outcome(g["outcome"])
        elif g.get("extraction_id") is not None:
            head += f" (extraction {g['extraction_id']})"
        if g.get("error"):
            head += f" - {g['error']}"
        lines.append(head)
    if out["notebook"]:
        lines.append(format_notebook(out["notebook"]))
    elif out["notebook_files"]:
        lines.append(f"Notebook inbox: {len(out['notebook_files'])} photo(s) waiting.")
    return "\n".join(lines)


def status_info(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Counts and health for `golf status` and the web status page. Never includes the API key."""
    from golf import publish as publish_mod
    from golf import watch as watch_mod
    from golf.demo import has_demo
    from golf.notes import pending_events

    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    n_pending_events = len(pending_events(conn))
    by_source = {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM v_rounds GROUP BY source")}
    hi = conn.execute("SELECT hi_after, low_hi, as_of FROM handicap_history WHERE hi_after IS NOT NULL"
                      " ORDER BY as_of DESC, round_id DESC LIMIT 1").fetchone()
    last_export = last_export_info(conn, cfg)
    spend = llm.spend_summary(conn)
    pub = publish_mod.settings(cfg)
    scan = watch_mod._meta_json(conn, watch_mod.LAST_SCAN_KEY, None)
    entities = Path(cfg.root) / "entities.yaml"
    example = Path(cfg.root) / "entities.example.yaml"
    inbox_shots = cfg.inbox_dir / "screenshots"
    return {
        "db_path": str(cfg.db_path),
        "data_dir": str(cfg.data_dir),
        "timezone": cfg.timezone,
        "rounds": {"counted": sum(by_source.values()), "by_source": by_source,
                   "excluded": one("SELECT COUNT(*) FROM v_rounds WHERE excluded = 1"),
                   "deleted_in_source": one("SELECT COUNT(*) FROM rounds WHERE deleted_in_source = 1"),
                   "abandoned": one("SELECT COUNT(*) FROM rounds WHERE entry_mode = 'abandoned'")},
        "handicap": {"index": hi["hi_after"], "low": hi["low_hi"], "as_of": hi["as_of"]} if hi else None,
        "include_nine": include_nine_setting(conn),
        "events": {"timeline": one("SELECT COUNT(*) FROM timeline_raw"), "pending": n_pending_events},
        "reviews": {"rounds": one("SELECT COUNT(*) FROM extractions WHERE status IN ('extracted','needs_review')"),
                    "events": n_pending_events},
        "last_export": last_export,
        # calls = every call sent (retries and --force re-runs are billed too); unpriced = calls on a model
        # with no known price, which the spend total can't include.
        "api": {"key_set": api_key_set(cfg), "calls": spend["billed_calls"], "unpriced": spend["unpriced"],
                "spend_usd": round(llm.total_spend(conn), 4), "model": cfg.model},
        "demo": has_demo(conn),
        "files": {"courses_yaml": (Path(cfg.root) / "courses.yaml").exists(), "entities_yaml": entities.exists(),
                  "entities_is_example": entities.exists() and example.exists()
                  and entities.read_bytes() == example.read_bytes(),
                  "env": (Path(cfg.root) / ".env").exists()},
        "inbox": {"screenshots": len(expand_images([inbox_shots])) if inbox_shots.is_dir() else 0,
                  "notebook": len(notebook_inbox_files(cfg))},
        "watch": {"last_scan": scan,
                  "agent_installed": watch_mod.plist_path(AGENTS_DIR).exists()},
        "publish": {"enabled": pub.enabled, "auto": pub.auto, "site_url": pub.site_url,
                    "last": publish_mod.last_publish(conn)},
    }


def last_export_info(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any] | None:
    """The newest real snapshot imported (by snapshot date, not import order: an older file imported later
    does not make the data look older). Age in days against today's local date, never negative."""
    best = None
    for r in conn.execute("SELECT summary, imported_at FROM imports WHERE kind = '18b_export'"
                          " AND sha256 NOT LIKE 'demo-%'"):
        snap = (loads(r["summary"], {}) or {}).get("snapshot_date") or _local_day(cfg, r["imported_at"])
        if best is None or (snap, r["imported_at"]) > (best["snapshot_date"], best["imported_at"]):
            best = {"snapshot_date": snap[:10], "imported_at": r["imported_at"]}
    if best is None:
        return None
    try:
        age = (_today(cfg) - date.fromisoformat(best["snapshot_date"])).days
    except ValueError:
        age = 0
    return {**best, "age_days": max(0, age), "future": age < 0}


def _local_day(cfg: Config, iso_utc: str | None) -> str:
    """imported_at is UTC; after 8 pm in New York that is already tomorrow."""
    try:
        return datetime.fromisoformat(str(iso_utc)).astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    except (TypeError, ValueError):
        return str(iso_utc or "")[:10]


SOURCE_NAMES = {"18b_export": "18Birdies export", "screenshot": "screenshots", "manual": "manual entry"}


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _local_stamp(iso_utc: str | None, tz: str) -> str:
    """A stored UTC timestamp as local wall time, e.g. 'Sep 25 22:07' (02:07 UTC would read as tomorrow)."""
    try:
        dt = datetime.fromisoformat(str(iso_utc)).astimezone(ZoneInfo(tz))
    except (TypeError, ValueError):
        return str(iso_utc or "")[:16].replace("T", " ")
    return f"{dt:%b} {dt.day} {dt:%H:%M}"


def format_status(s: dict[str, Any]) -> str:
    r, h, e, a = s["rounds"], s["handicap"], s["events"], s["api"]
    tz = s.get("timezone") or "UTC"
    src = ", ".join(f"{SOURCE_NAMES.get(k, k)} {v}" for k, v in sorted(r["by_source"].items())) or "none"
    lines = [
        f"Database: {s['db_path']}" + ("  [DEMO DATA: synthetic; `golf demo clear` removes it]" if s["demo"] else ""),
        f"Rounds counted: {r['counted']} ({src}); excluded {r['excluded']}, deleted in 18Birdies "
        f"{r['deleted_in_source']}, abandoned {r['abandoned']}.",
        ("Handicap index (unofficial): " + (f"{h['index']:.1f} as of {h['as_of'][:10]}"
                                            + (f", low {h['low']:.1f}" if h["low"] is not None else "")
                                            if h else "none yet")
         + ("; 9-hole rounds included" if s["include_nine"] else "; 9-hole rounds left out")
         + " (`golf recompute --include-nine/--no-include-nine`)."),
        ("Last 18Birdies export: " + (f"snapshot {s['last_export']['snapshot_date']}, "
                                      + ("today" if s["last_export"]["age_days"] == 0 else
                                         f"{_n(s['last_export']['age_days'], 'day')} ago")
                                      + ("  (download a fresh one)" if s["last_export"]["age_days"]
                                         > config_mod.EXPORT_STALE_DAYS else "")
                                      if s["last_export"] else "never imported")),
        f"Timeline events: {e['timeline']} counted.",
        f"Waiting for review: {_n(s['reviews']['rounds'], 'screenshot round')}, {_n(s['reviews']['events'], 'event')}"
        + (f"  -> {WEB_URL}/review" if s["reviews"]["rounds"] or s["reviews"]["events"] else "") + ".",
        f"Inbox: {_n(s['inbox']['screenshots'], 'screenshot')}, {_n(s['inbox']['notebook'], 'notebook photo')} "
        "waiting.",
        f"Claude API: key {'set' if a['key_set'] else 'NOT set (see .env.example)'}; model {a['model']}; "
        f"{_n(a['calls'], 'call')} billed, ${a['spend_usd']:.2f} spent so far"
        + (f" ({a['unpriced']} on a model with no known price, not in that total)." if a.get("unpriced") else "."),
    ]
    w, pub = s["watch"], s["publish"]
    scan = w["last_scan"]
    lines.append("Auto-import: " + ("LaunchAgent installed" if w["agent_installed"] else
                                    "LaunchAgent not installed (`golf watch install`)")
                 + (f"; last scan {_local_stamp(scan['at'], tz)}" if scan else "; never scanned") + ".")
    for x in (scan or {}).get("held") or []:
        lines.append(f"  Not imported automatically: {x['file']}: {x['reason']}")
    last_pub = f", last published {_local_stamp(pub['last']['at'], tz)}" if pub["last"] else ", not published yet"
    lines.append("Public site: " + (f"on, {pub['site_url']}{last_pub}" if pub["enabled"]
                                    else "off ([publish] enabled = false)") + ".")
    f = s["files"]
    if not f["courses_yaml"]:
        lines.append("courses.yaml: missing (no course ratings, so no differentials). `golf init` writes an empty one.")
    if not f["entities_yaml"]:
        lines.append("entities.yaml: missing (coach names and places help the note extractor). `golf init` writes "
                     "an empty one.")
    elif f["entities_is_example"]:
        lines.append("entities.yaml: still the example's placeholder names; replace them with your coaches, "
                     "places and bag before syncing notes.")
    return "\n".join(lines)


# ================================================================ Typer app
app = typer.Typer(help="Personal golf analytics: 18Birdies export, screenshots, notes, lessons. Local only.",
                  no_args_is_help=True, add_completion=False)
courses_app = typer.Typer(help="Course reference data (courses.yaml): check, autofill, sync.", no_args_is_help=True)
notes_app = typer.Typer(help="Apple Notes 'Golf' folder -> timeline events.", no_args_is_help=True)
review_app = typer.Typer(help="Review extracted rounds and events in the terminal (the web UI is nicer).",
                         no_args_is_help=True)
demo_app = typer.Typer(help="Synthetic demo data for trying the dashboard.", no_args_is_help=True)
round_app = typer.Typer(help="Your corrections to one round, kept in round_overrides (imports never overwrite "
                             "them).", no_args_is_help=True)
analyze_app = typer.Typer(help="Descriptive analyses.", no_args_is_help=True)
watch_app = typer.Typer(help="Import new 18Birdies exports from Downloads automatically.",
                        invoke_without_command=True)
app.add_typer(courses_app, name="courses")
app.add_typer(watch_app, name="watch")
app.add_typer(notes_app, name="notes")
app.add_typer(review_app, name="review")
app.add_typer(demo_app, name="demo")
app.add_typer(round_app, name="round")
app.add_typer(analyze_app, name="analyze")


def _echo(text: str = "") -> None:
    typer.echo(text)


def _fail(message: str, code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _fail_llm(cfg: Config, error: Exception | str) -> None:
    """Exit 1 when the API is just busy or unreachable (try later), 2 when the key needs fixing."""
    _fail(llm_help(cfg, error), 1 if is_transient(error) else 2)


@contextmanager
def _llm_errors(cfg: Config) -> Iterator[None]:
    """Turn API problems into instructions instead of tracebacks."""
    try:
        yield
    except llm.LLMUnavailable as e:          # includes llm.LLMTransient (network, 429/529, unknown model)
        _fail_llm(cfg, e)
    except llm.LLMOutputError as e:
        _fail(f"The model's output could not be used: {e}\n(The raw response is kept in llm_calls, "
              f"call {e.call_id}.)", 1)


# ---------------------------------------------------------------- setup
@app.command()
def init(hook: Annotated[bool, typer.Option("--hook/--no-hook", help="Install the git pre-commit privacy hook.")] = True
         ) -> None:
    """Create data folders and golf.db; copy the example config files you still need to fill in."""
    from golf.privacy import install_hook

    cfg = get_cfg()
    root = Path(cfg.root)
    for sub in DATA_DIRS:
        (cfg.data_dir / sub).mkdir(parents=True, exist_ok=True)
    with open_db(cfg):
        pass
    _echo(f"Data folder: {cfg.data_dir} (gitignored)\nDatabase:    {cfg.db_path}")
    # courses.yaml and entities.yaml start EMPTY: copying the examples would sync a synthetic club into the
    # real database and let placeholder coach names resolve real initials. The examples stay as references.
    for target, text, note in (("courses.yaml", COURSES_STARTER, "empty; add your clubs (format: "
                                "courses.example.yaml)"),
                               ("entities.yaml", ENTITIES_STARTER, "empty; add your coaches, places and bag "
                                "(format: entities.example.yaml)")):
        dst = root / target
        if dst.exists():
            _echo(f"{target}: exists, left as is.")
        else:
            dst.write_text(text)
            _echo(f"{target}: created ({note}).")
    src, dst = root / ".env.example", root / ".env"
    if dst.exists():
        _echo(".env: exists, left as is.")
    elif src.exists():
        shutil.copyfile(src, dst)
        _echo(".env: created from .env.example (put your ANTHROPIC_API_KEY in it).")
    if hook:
        _echo("Pre-commit privacy hook: " + install_hook(root, sys.executable))
    _echo("\nNext:\n"
          "  1. Put your Claude API key in .env (ANTHROPIC_API_KEY=...).\n"
          "  2. `golf watch install`: new 18Birdies exports in Downloads are then imported automatically.\n"
          f"  3. Download your export at {EXPORT_URL} (Request My Data); or import one by hand with "
          "`golf import-18b <file>`.\n"
          "  4. `golf courses check` lists the clubs that need course ratings.\n"
          f"  5. `golf serve` opens the local app at {WEB_URL} (dashboard, inbox, review).\n"
          "  Try it first with synthetic data: `golf demo seed`, then `golf serve`.")


@app.command()
def status() -> None:
    """Counts, last export age, pending reviews, API spend, whether the API key is set (never shown)."""
    cfg = get_cfg()
    with open_db(cfg) as conn:
        _echo(format_status(status_info(conn, cfg)))


# ---------------------------------------------------------------- 18Birdies export
@app.command("inspect-export")
def inspect_export_cmd(file: Annotated[Path, typer.Argument(help="An 18Birdies_archive*.json file.")]) -> None:
    """Print the export's key tree (types and counts, never values) and a schema check."""
    from golf.ingest.birdies_export import ExportFormatError, inspect_export

    cfg = get_cfg()
    if not file.is_file():
        _fail(f"No file at {file}.\n\n{export_help(cfg)}")
    try:
        _echo(inspect_export(file))
    except (ExportFormatError, ValueError) as e:
        _fail(f"Could not read {file.name}: {e}")


@app.command("import-18b")
def import_18b_cmd(
    file: Annotated[Optional[Path], typer.Argument(help="Export file; default: newest in data/raw/18birdies/.")] = None,
    sync: Annotated[bool, typer.Option("--sync/--no-sync", help="Run courses sync after importing.")] = True,
    new_account: Annotated[bool, typer.Option(
        "--new-account", help="Accept an export of a different 18Birdies account than the one imported so far "
                              "(only if you switched accounts).")] = False,
) -> None:
    """Import an 18Birdies export, map courses, fold in screenshot rounds, recompute the HI."""
    from golf.ingest.birdies_export import ExportAccountError, ExportFormatError

    cfg = get_cfg()
    if file is None:
        file = latest_export_file(cfg)
        if file is None:
            _fail(f"No export found in {cfg.data_dir / EXPORT_DIR}.\n\n{export_help(cfg)}")
        _echo(f"Using {file}")
    if not file.is_file():
        _fail(f"No file at {file}.\n\n{export_help(cfg)}")
    with open_db(cfg) as conn:
        try:
            out = import_18b(conn, cfg, file, sync=sync, new_account=new_account)
        except ExportFormatError as e:
            _fail(f"{file.name} does not look like an 18Birdies export: {e}\nRun `golf inspect-export {file}` "
                  "to see its structure (values are never printed).")
        except ExportAccountError as e:
            _fail(str(e), 1)
    _echo(format_import(out))


# ---------------------------------------------------------------- courses
def _yaml_path(cfg: Config, yaml: Path | None) -> Path:
    from golf.courses import default_courses_path

    return yaml or default_courses_path(cfg)


@courses_app.command("check")
def courses_check() -> None:
    """List unmapped clubs, missing/unchecked ratings and par-checksum failures, most severe first."""
    from golf.courses import check_courses

    cfg = get_cfg()
    with open_db(cfg) as conn:
        issues = check_courses(conn)
    if not issues:
        _echo("No course problems found.")
        return
    for sev in ("error", "warn", "info"):
        group = [i for i in issues if i["severity"] == sev]
        if group:
            _echo(f"{sev.upper()} ({len(group)})")
            for i in group:
                _echo(f"  [{i['code']}] {i['message']}")
    if any(i["code"] == "unmapped_club" for i in issues):
        _echo("\nFor an unmapped club: `golf courses autofill <club_id>` proposes an entry from OpenGolfAPI; "
              "then confirm the tee you play against the scorecard and set checked_on / is_default.")


@courses_app.command("sync")
def courses_sync(yaml: Annotated[Optional[Path], typer.Option("--yaml", help="Default: ./courses.yaml")] = None
                 ) -> None:
    """Load courses.yaml into golf.db, map rounds, fill per-hole par/SI, then recompute the HI."""
    from golf.courses import CoursesFileError, sync_courses

    cfg = get_cfg()
    path = _yaml_path(cfg, yaml)
    if not path.exists():
        _fail(f"No {path}. Copy courses.example.yaml to courses.yaml (or run `golf init`) and fill in your clubs.")
    with open_db(cfg) as conn:
        try:
            s = sync_courses(conn, path)
        except CoursesFileError as e:
            _fail(f"{path.name} is not valid: {e}")
        w = recompute_hi(conn, cfg)
    _echo(f"Synced {s['clubs']} club(s), {s['courses']} course(s), {s['tees']} tee(s), {s['tee_holes']} tee holes. "
          f"Rounds mapped {s['rounds_mapped']}, unmapped {s['rounds_unmapped']}; nines resolved "
          f"{s['nine_resolved']}" + (f" ({s['nine_inferred']} placed by the course's par layout)"
                                     if s.get("nine_inferred") else "")
          + f"; par/SI filled on {s['holes_filled']} holes.")
    if s["unknown_round_ids"]:
        _echo("Round ids in courses.yaml that are not in golf.db: " + ", ".join(s["unknown_round_ids"]))
    _echo(format_whs(w))


@courses_app.command("autofill")
def courses_autofill(
    club_id: Annotated[str, typer.Argument(help="18Birdies club id (see `golf courses check`).")],
    course_id: Annotated[Optional[str], typer.Option("--course-id", help="Pick this OpenGolfAPI course.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace a course whose ratings you checked.")] = False,
    yaml: Annotated[Optional[Path], typer.Option("--yaml")] = None,
) -> None:
    """Propose a course from OpenGolfAPI (network call; ODbL data) and write it into courses.yaml."""
    import httpx

    from golf.courses import AutofillError, CoursesFileError, autofill_club

    cfg = get_cfg()
    path = _yaml_path(cfg, yaml)
    with open_db(cfg) as conn:
        try:
            r = autofill_club(conn, club_id, path, course_id=course_id, force=force)
        except (AutofillError, CoursesFileError) as e:
            _fail(str(e))
        except httpx.HTTPError as e:
            _fail(f"OpenGolfAPI request failed: {e}")
    _echo(f"Proposed {r['course_key']} for {club_id}: OpenGolfAPI {r['opengolfapi_id']} '{r['matched_name']}' "
          f"(match {r['match_score']}), {r['tees']} tee(s), {r['holes']} holes -> {r['yaml_path']}")
    if r.get("also_matched"):
        _echo("Warning: this club name matches other courses just as well (a multi-course facility; the export "
              "never says which course was played):\n"
              + "\n".join(f"  --course-id {c['id']}: {c['club_name']} {c['name']}".rstrip() for c in r["also_matched"])
              + "\nIf you played one of those, re-run with its --course-id; `golf courses check` reports a par "
                "layout that doesn't fit your rounds.")
    _echo("Before trusting it: confirm the tee you play against the scorecard or USGA NCRDB (OpenGolfAPI ratings "
          "can be off), set checked_on and is_default: true on that tee, then run `golf courses sync`.")


@app.command()
def recompute(include_nine: Annotated[Optional[bool], typer.Option(
        "--include-nine/--no-include-nine",
        help="Count 9-hole rounds (unofficial expected-score approximation). The choice is remembered for every "
             "later import, review and courses sync; default: included.")] = None) -> None:
    """Rebuild the unofficial Handicap Index history from scratch."""
    cfg = get_cfg()
    with open_db(cfg) as conn:
        if include_nine is not None:
            from golf import whs

            whs.set_include_nine(conn, include_nine)
        _echo(format_whs(recompute_hi(conn, cfg)))


# ---------------------------------------------------------------- screenshots
@app.command()
def extract(
    paths: Annotated[List[Path], typer.Argument(help="One round's screenshots: files and/or a folder.")],
    played_on: Annotated[Optional[str], typer.Option("--date", help="Date played, YYYY-MM-DD.")] = None,
    club: Annotated[Optional[str], typer.Option("--club", help="18Birdies club id hint.")] = None,
    holes: Annotated[Optional[int], typer.Option("--holes", help="9 or 18.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-extract even if seen before (re-bills).")] = False,
    no_reread: Annotated[bool, typer.Option("--no-reread", help="Skip the targeted re-read of flagged cells.")]
    = False,
) -> None:
    """Extract ONE round from its 18Birdies screenshots (Claude vision)."""
    cfg = get_cfg()
    if played_on:
        _check_date(played_on)
    with open_db(cfg) as conn, _llm_errors(cfg):
        try:
            out = extract_files(conn, cfg, paths, played_on=played_on, club_id=club, expected_holes=holes,
                                force=force, reread=not no_reread)
        except (FileNotFoundError, ValueError) as e:
            _fail(str(e))
        if out["status"] in ("accepted", "auto_accepted"):
            refresh_derived(conn, cfg)
    _echo(format_screenshot_outcome(out))
    if out.get("warning"):
        _echo(f"Note: {out['warning']}")
    for f in out["flags"]:
        _echo(f"  {f['severity']} {f['code']} {_cell(f)}: {f['message']}")
    if out["status"] in ("needs_review", "extracted"):
        _echo(f"Review it at {WEB_URL}/review/round/{out['extraction_id']} (`golf serve`) "
              f"or `golf review round {out['extraction_id']}`.")


@app.command("inbox")
def inbox_cmd(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen; no API calls, no moves.")]
    = False,
    no_reread: Annotated[bool, typer.Option("--no-reread")] = False,
) -> None:
    """Process data/inbox/screenshots (one folder or capture burst = one round) and data/inbox/notebook."""
    cfg = get_cfg()
    with open_db(cfg) as conn, _llm_errors(cfg):
        out = process_inboxes(conn, cfg, dry_run=dry_run, reread=not no_reread)
        if any(g["action"] == "extracted" for g in out["screenshots"]):
            refresh_derived(conn, cfg)
    _echo(format_inbox(out))
    if out["notebook"] and out["notebook"].get("llm_unavailable"):
        _fail_llm(cfg, out["notebook"]["llm_unavailable"])


# ---------------------------------------------------------------- notes, notebook, add
@app.command()
def notebook(
    paths: Annotated[List[Path], typer.Argument(help="Notebook page photos and/or folders.")],
    force: Annotated[bool, typer.Option("--force", help="Re-read pages already imported (re-bills).")] = False,
) -> None:
    """Transcribe paper-notebook page photos and extract events (every event needs review)."""
    cfg = get_cfg()
    with open_db(cfg) as conn, _llm_errors(cfg):
        try:
            s = import_notebook_files(conn, cfg, paths, force=force)
        except (FileNotFoundError, ValueError) as e:
            _fail(str(e))
    _echo(format_notebook(s))
    if s.get("llm_unavailable"):
        _fail_llm(cfg, s["llm_unavailable"])
    if s["events"]["new"]:
        _echo(f"Review them at {WEB_URL}/review/events or with `golf review events`.")


@notes_app.command("sync")
def notes_sync(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Read Notes and report; write nothing, no API.")]
    = False,
    no_extract: Annotated[bool, typer.Option("--no-extract", help="Sync note text only.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-extract every note (re-bills).")] = False,
) -> None:
    """Sync the Apple Notes 'Golf' folder (first run: macOS asks to let this app control Notes)."""
    from golf.notes import NotesError, format_sync_summary, sync_apple_notes

    cfg = get_cfg()
    with open_db(cfg) as conn, _llm_errors(cfg):
        try:
            s = sync_apple_notes(conn, cfg, runner=NOTES_RUNNER, dry_run=dry_run, extract=not no_extract,
                                 force=force)
        except NotesError as e:
            _fail(str(e))
    _echo(format_sync_summary(s))
    if s.get("llm_unavailable"):
        _fail_llm(cfg, s["llm_unavailable"])
    if s["events"]["pending"]:
        _echo(f"Review new events at {WEB_URL}/review/events or with `golf review events`.")


@notes_app.command("template")
def notes_template() -> None:
    """How to write notes the timeline can date exactly, plus the iPhone Shortcut recipe."""
    from golf.notes import NOTE_TEMPLATE_HELP

    _echo(NOTE_TEMPLATE_HELP)


@notes_app.command("load-manual")
def notes_load_manual(path: Annotated[Optional[Path], typer.Argument(help="Default: ./manual_events.yaml")] = None
                      ) -> None:
    """Load remembered past events from manual_events.yaml (idempotent)."""
    from golf.notes import load_manual_yaml

    cfg = get_cfg()
    path = path or Path(cfg.root) / "manual_events.yaml"
    if not path.exists():
        _fail(f"No {path}. It is a YAML list like:\n  - {{date: 2024-05, type: fitting, summary: \"Iron fitting\"}}")
    with open_db(cfg) as conn:
        try:
            s = load_manual_yaml(conn, path)
        except ValueError as e:
            _fail(str(e))
    _echo(f"{path.name}: {s['entries']} entries; {s['new']} new, {s['updated']} updated, {s['unchanged']} unchanged, "
          f"{s['superseded']} removed.")


@notes_app.command("auto-accept")
def notes_auto_accept(setting: Annotated[str, typer.Argument(help="on | off | show")] = "show") -> None:
    """Auto-accept high-confidence, clearly dated note events (default off until the gold set passes)."""
    from golf.notes import auto_accept_enabled, set_auto_accept

    cfg = get_cfg()
    with open_db(cfg) as conn:
        if setting in ("on", "off"):
            set_auto_accept(conn, setting == "on")
        elif setting != "show":
            _fail("Use on, off or show.")
        _echo(f"Note auto-accept is {'ON' if auto_accept_enabled(conn) else 'OFF'}.")


def _check_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        _fail(f"Dates are YYYY-MM-DD; got {value!r}.")
    return value


@app.command()
def add(
    text: Annotated[str, typer.Argument(help='What happened, e.g. "lesson w/ Mike, pump drill".')],
    on: Annotated[Optional[str], typer.Option("--date", help="YYYY-MM-DD (default today).")] = None,
    event_type: Annotated[Optional[str], typer.Option("--type", help="lesson, practice, on_course, "
                                                                     "equipment_change, fitting, injury, ...")] = None,
    coach: Annotated[Optional[str], typer.Option("--coach")] = None,
    focus: Annotated[Optional[str], typer.Option("--focus", help='e.g. "putting, short_game"')] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Store as typed; no API call.")] = False,
) -> None:
    """Quick-add a timeline event (auto-accepted). With --type no API call is made."""
    from golf.notes import add_manual_event

    cfg = get_cfg()
    if on:
        _check_date(on)
    with open_db(cfg) as conn:
        try:
            r = add_manual_event(conn, cfg, text, date=on, event_type=event_type, coach=coach, focus=focus,
                                 use_llm=not no_llm)
        except ValueError as e:
            _fail(str(e))
    for ev in r["events"]:
        _echo(f"Added {ev['event_type']} on {ev['date']}: {ev['summary']}  (id {ev['event_id']})")
    if r["llm_error"]:
        _echo(f"Stored without extraction ({r['llm_error'].splitlines()[0]}).")


@round_app.command("putts")
def round_putts(
    ref: Annotated[str, typer.Argument(help="Round id (or its first 6+ characters), or the date played "
                                            "YYYY-MM-DD when that day has one round.")],
    setting: Annotated[str, typer.Argument(help="full (putts entered on every hole) | partial | auto (the "
                                                "importer's rule)")],
) -> None:
    """Say whether a round's putt total covers every hole. Partial rounds are left out of putting stats."""
    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            r = set_putts_override(conn, ref, setting.strip().lower())
        except ValueError as e:
            _fail(str(e))
    what = {"full": "count as tracked on every hole", "partial": "are left out of putting stats (partial)",
            "auto": "follow the importer's rule again"}[r["setting"]]
    _echo(f"{r['date']} at {r['place']}: {r['putts'] if r['putts'] is not None else 'no'} putts over "
          f"{r['holes']} holes now {what}. Run `golf build` (or reload golf serve) to see it.")


# ---------------------------------------------------------------- review (terminal fallback)
def _cell(f: dict[str, Any]) -> str:
    hole, field = f.get("hole"), f.get("field")
    return f"hole {hole} {field or ''}".strip() if hole is not None else (field or "round")


@review_app.command("rounds")
def review_rounds() -> None:
    """List screenshot extractions waiting for review."""
    from golf.extract.rounds import pending_round_reviews

    cfg = get_cfg()
    with open_db(cfg) as conn:
        pending = pending_round_reviews(conn)
    if not pending:
        _echo("No screenshot rounds waiting for review.")
        return
    for p in pending:
        _echo(f"#{p['extraction_id']}  {p['played_on'] or '(no date)'}  {p['course_text'] or '?'}  "
              f"{p['n_images']} image(s)  {p['n_errors']} error(s), {p['n_warnings']} warning(s)  "
              f"[{p['round_source'] or 'unmatched'}]")
    _echo(f"\nEasiest: {WEB_URL}/review (`golf serve`): screenshots next to an editable grid.\n"
          "Terminal: `golf review round ID`, `golf review fix ID HOLE FIELD VALUE`, `golf review accept ID`.")


@review_app.command("round")
def review_round(extraction_id: int) -> None:
    """Show one extraction's grid and flags (E = error, W = warning)."""
    from golf.extract.rounds import get_review

    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            rv = get_review(conn, extraction_id)
        except KeyError as e:
            _fail(str(e.args[0]))
    hdr = rv["header"]
    _echo(f"Extraction {extraction_id}: {rv['status']}; round {rv['round_id'] or '(none yet)'} "
          f"({rv['round_source'] or 'unmatched'}); played {rv['played_on'] or '(no date)'}")
    _echo(f"Screens say: {hdr['course_text']} / {hdr['tee_text']} / {hdr['date_text']} / par {hdr['course_par']}")
    cells = rv["flags_by_cell"]
    cols = ["par", "si", "strokes", "symbol", "putts", "fairway", "gir", "gir_miss", "penalties", "chips", "sand"]
    width = {c: max(len(c), 8 if c in ("symbol", "fairway", "gir_miss") else 4) for c in cols}
    _echo("hole " + " ".join(c[:width[c]].rjust(width[c]) for c in cols) + "  export")
    for h in rv["holes"]:
        row = []
        for c in cols:
            v = h[c]
            mark = "".join(sorted({f["severity"] for f in cells.get(f"{h['hole']}:{c}", [])}))
            text = ("-" if v is None else str(v)).replace("not_", "n/")[:width[c] - len(mark)] + mark
            row.append(text.rjust(width[c]))
        ref = h["reference_strokes"]
        _echo(f"{h['hole']:>4} " + " ".join(row) + f"  {'' if ref is None else ref:>6}")
    if rv["flags"]:
        _echo("\nFlags:")
        for f in rv["flags"]:
            extra = f" (re-read: {f['reread'].get('value')!r})" if f.get("reread") else ""
            _echo(f"  {f['severity']} {f['code']} {_cell(f)}: {f['message']}{extra}")


@review_app.command("fix")
def review_fix(
    extraction_id: int,
    hole: Annotated[str, typer.Argument(help="Hole number, or - for round-level fields (date_iso, course_text, "
                                             "tee_text, course_par).")],
    field: str,
    value: str,
) -> None:
    """Correct one cell (recorded for the golden set); accepted automatically if the round is then clean."""
    from golf.extract.rounds import apply_corrections

    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            out = apply_corrections(conn, extraction_id, [{"hole": None if hole == "-" else int(hole),
                                                           "field": field, "value": value}])
        except (ValueError, KeyError) as e:
            _fail(str(e))
        refresh_derived(conn, cfg)
    _echo(format_screenshot_outcome(out))


@review_app.command("accept")
def review_accept(extraction_id: int,
                  force: Annotated[bool, typer.Option("--force", help="Accept despite error flags.")] = False) -> None:
    """Accept an extraction as it stands (warnings are fine; errors need --force)."""
    from golf.extract.rounds import accept_extraction

    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            out = accept_extraction(conn, extraction_id, force=force)
        except (ValueError, KeyError) as e:
            _fail(str(e))
        refresh_derived(conn, cfg)
    _echo(format_screenshot_outcome(out))


@review_app.command("reject")
def review_reject(extraction_id: int) -> None:
    """Reject an extraction; anything it wrote to a round is taken back out."""
    from golf.extract.rounds import reject_extraction

    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            out = reject_extraction(conn, extraction_id)
        except KeyError as e:
            _fail(str(e.args[0]))
        refresh_derived(conn, cfg)
    _echo(format_screenshot_outcome(out))


def format_event(ev: dict[str, Any]) -> str:
    focus = ", ".join(f"{f.get('game_area')}" + (f" ({f['detail']})" if f.get("detail") else "")
                      for f in ev.get("focus_areas") or [])
    lines = [
        f"{ev['event_type']}{' (planned)' if ev.get('is_planned') else ''} on {ev.get('date') or '?'} "
        f"[{ev.get('date_precision') or '?'}, {ev.get('date_source') or '?'}]"
        + (f", coach {ev['coach']}" if ev.get("coach") else "") + f"   id {ev['event_id']}",
        f"  \"{(ev.get('source_excerpt') or '').strip()}\"   ({ev.get('doc_title') or ev.get('doc_id')}, "
        f"{ev.get('doc_source')})",
        f"  summary: {ev.get('summary') or ''}",
    ]
    if ev.get("date_reasoning"):
        lines.append(f"  date: {ev['date_reasoning']}")
    if focus:
        lines.append(f"  focus: {focus}")
    for f in ev.get("flags") or []:
        other = f.get("other_event_id") or f.get("carryover_to")
        lines.append(f"  flag {f.get('code')}: {f.get('message')}" + (f" (-> {other})" if other else ""))
    return "\n".join(lines)


def event_edits(ev: dict[str, Any], *, date_: str | None = None, event_type: str | None = None,
                coach: str | None = None, summary: str | None = None, focus: str | None = None) -> dict[str, Any]:
    """Only the fields that actually changed, in review_event's merge-patch form."""
    from golf.notes.manual import parse_focus

    edits: dict[str, Any] = {}
    if date_ is not None and date_.strip() and date_.strip() != (ev.get("date") or ""):
        edits["date"] = date_.strip()
    if event_type is not None and event_type.strip() and event_type.strip() != ev.get("event_type"):
        edits["event_type"] = event_type.strip()
    if coach is not None and coach.strip() != (ev.get("coach") or ""):
        edits["coach"] = coach.strip() or None
    if summary is not None and summary.strip() and summary.strip() != (ev.get("summary") or ""):
        edits["summary"] = summary.strip()
    if focus is not None:
        current = ", ".join(f.get("game_area", "") for f in ev.get("focus_areas") or [])
        if focus.strip() != current:
            edits["focus_areas"] = parse_focus(focus)
    return edits


@review_app.command("events")
def review_events() -> None:
    """Walk the event review queue: [a]ccept [r]eject [e]dit [m]erge [c]arry over [s]kip [q]uit."""
    from golf.notes import carry_over_review, pending_events, review_event

    cfg = get_cfg()
    with open_db(cfg) as conn:
        queue = pending_events(conn)
        if not queue:
            _echo("No events waiting for review.")
            return
        _echo(f"{len(queue)} event(s) to review (the web page {WEB_URL}/review/events shows the same queue).\n")
        for i, ev in enumerate(queue, start=1):
            _echo(f"[{i}/{len(queue)}] " + format_event(ev))
            carry = next((f.get("carryover_to") for f in ev.get("flags") or [] if f.get("carryover_to")), None)
            orphaned = ev.get("queue_reason") == "orphaned"
            if orphaned:
                _echo("  Its text was edited in the note after you reviewed it: carry your review over to the "
                      "new event, or dismiss it.")
                menu = ("  [c]arry over " if carry else "  ") + "[d]ismiss [m]erge [s]kip [q]uit"
            else:
                menu = "  [a]ccept [r]eject [e]dit [m]erge" + (" [c]arry over" if carry else "") + " [s]kip [q]uit"
            choice = typer.prompt(menu, default="s").strip().lower()[:1]
            if orphaned and choice in ("a", "e"):
                _echo("  not saved: an orphaned event can only be carried over, dismissed or merged.")
                continue
            if orphaned and choice == "d":
                choice = "r"
            try:
                if choice == "q":
                    break
                if choice == "a":
                    review_event(conn, ev["event_id"], "accept")
                elif choice == "r":
                    review_event(conn, ev["event_id"], "reject")
                elif choice == "e":
                    edits = event_edits(
                        ev, date_=typer.prompt("  date", default=ev.get("date") or ""),
                        event_type=typer.prompt("  type", default=ev["event_type"]),
                        coach=typer.prompt("  coach", default=ev.get("coach") or "", show_default=False),
                        summary=typer.prompt("  summary", default=ev.get("summary") or ""),
                        focus=typer.prompt("  focus", default=", ".join(f.get("game_area", "") for f in
                                                                         ev.get("focus_areas") or []),
                                           show_default=False))
                    review_event(conn, ev["event_id"], "edit" if edits else "accept", edits=edits or None)
                elif choice == "m":
                    dup = next((f.get("other_event_id") for f in ev.get("flags") or [] if f.get("other_event_id")),
                               "")
                    target = typer.prompt("  merge into event id", default=dup)
                    review_event(conn, ev["event_id"], "merge", merged_into=target.strip())
                elif choice == "c" and carry:
                    carry_over_review(conn, ev["event_id"], carry)
                else:
                    continue
                _echo("  saved.")
            except (ValueError, KeyError) as e:
                _echo(f"  not saved: {e}")


# ---------------------------------------------------------------- outputs
@app.command()
def build(out: Annotated[Optional[Path], typer.Option(
              "--out", help="Default: data/site/index.html (data/site/public/index.html with --public)")] = None,
          public: Annotated[bool, typer.Option(
              "--public", help="The shareable version: no file paths, screenshots, GPS, API cost or machine "
                               "details (what `golf publish` pushes; it also runs the privacy check).")] = False,
          ) -> None:
    """Write the self-contained dashboard HTML (no network requests; open it in a browser)."""
    from golf.dashboard import build as build_dashboard

    cfg = get_cfg()
    with open_db(cfg) as conn:
        path = build_dashboard(conn, cfg, out, public=public)
    _echo(f"{'Public dashboard' if public else 'Dashboard'} written to {path}\nOpen it with: open '{path}'")


@app.command("chat-data")
def chat_data(out: Annotated[Optional[Path], typer.Option(
                  "--out", help="Folder for golf-data.json (default: data/site/chat/)")] = None) -> None:
    """Write golf-data.json for the Shane's Caddie chat page on claude.ai (public-safe, privacy-checked)."""
    from golf.chat import write_bundle

    cfg = get_cfg()
    with open_db(cfg) as conn:
        path = write_bundle(conn, cfg, out)
    _echo(f"Chat data written to {path} ({path.stat().st_size // 1024} KB).\n"
          "To update the chat page, ask Claude Code to \"refresh Shane's Caddie\" (it republishes golf-data.json).")


@app.command()
def serve(port: Annotated[int, typer.Option("--port")] = 8765,
          open_browser: Annotated[bool, typer.Option("--open", help="Open the browser.")] = False) -> None:
    """Run the local web app on 127.0.0.1 (dashboard, inbox, review, 18Birdies auto-import). Ctrl-C stops it."""
    import uvicorn

    from golf.web.app import create_app

    cfg = get_cfg()
    url = f"http://127.0.0.1:{port}"
    _echo(f"Golf app on {url}  (local only; Ctrl-C to stop). It also looks in Downloads for a new 18Birdies "
          "export every minute.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def mcp() -> None:
    """Run the read-only MCP server on stdio (for Claude Desktop / Claude Code)."""
    from golf.mcp_server import run

    run(get_cfg())


@analyze_app.command("lessons")
def analyze_lessons() -> None:
    """Descriptive before/after comparison for each lesson, with the minimum detectable effect."""
    from golf.analytics.lessons import lesson_report
    from golf.analytics.stats import lesson_metric, load_timeline, round_facts, set_outcome

    cfg = get_cfg()
    with open_db(cfg) as conn:
        facts = round_facts(conn)
        rep = lesson_report(set_outcome(facts, lesson_metric(facts)), load_timeline(conn))
    _echo(rep["mde_sentence"])
    if not rep["lessons"]:
        _echo("No lessons on the timeline yet.")
    for les in rep["lessons"]:
        _echo(f"\n{les['date']} {les['coach'] or ''} {les['summary_note']}".rstrip())
        _echo(f"  {les['summary']}")
        for text in les["flag_text"].values():
            _echo(f"  caveat: {text}")
    _echo("\nDescriptive only: a before/after difference is not proof that the lesson caused it.")


# ---------------------------------------------------------------- demo
@demo_app.command("seed")
def demo_seed(rounds: Annotated[int, typer.Option("--rounds")] = 24,
              seed: Annotated[int, typer.Option("--seed")] = 7,
              force: Annotated[bool, typer.Option("--force", help="Seed even if real rounds exist.")] = False) -> None:
    """Fill golf.db with labelled synthetic rounds, lessons and one review item."""
    from golf.demo import seed_demo

    cfg = get_cfg()
    with open_db(cfg) as conn:
        real = conn.execute("SELECT COUNT(*) FROM rounds WHERE round_id NOT LIKE 'demo-%'").fetchone()[0]
        if real and not force:
            _fail(f"golf.db already holds {real} real round(s); demo rows would mix into your dashboard. "
                  "Use --force if you really want that, or point GOLF_DATA_DIR at a scratch folder.")
        s = seed_demo(conn, n_rounds=rounds, seed=seed, end=_today(cfg))
    _echo(f"Demo data: {s['rounds']} synthetic rounds and {s['events']} events ({s['start']} to {s['end']}). "
          f"Run `golf serve` or `golf build`; `golf demo clear` removes it.")


@demo_app.command("clear")
def demo_clear() -> None:
    """Remove every synthetic demo row (ids starting with demo-)."""
    from golf.demo import clear_demo

    cfg = get_cfg()
    with open_db(cfg) as conn:
        removed = clear_demo(conn)
    _echo(f"Removed {sum(removed.values())} demo row(s).")


# ---------------------------------------------------------------- watch + publish
AGENTS_DIR: Path | None = None       # tests point these at fakes; None = ~/Library/LaunchAgents and launchctl
LAUNCHCTL = None
PUBLISH_RUNNER = None


@watch_app.callback()
def watch_main(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once", help="Scan once and exit (what the LaunchAgent runs).")] = False,
    interval: Annotated[Optional[int], typer.Option("--interval", help="Seconds between scans (default: "
                                                                        "[watch] interval_seconds, 300).")] = None,
) -> None:
    """Watch Downloads (and iCloud Drive's Downloads) for 18Birdies_archive*.json and import each new one:
    import, courses, screenshots, handicap, dashboard, and GitHub Pages when [publish] is on."""
    from golf import watch as watch_mod

    if ctx.invoked_subcommand is not None:
        return
    cfg = get_cfg()
    if once:
        under_launchd = os.environ.get("XPC_SERVICE_NAME") == watch_mod.LABEL
        with open_db(cfg) as conn:
            s = watch_mod.scan_once(conn, cfg, source="launchd" if under_launchd else "cli")
        # launchd appends stdout to data/logs/watch.out.log on every run (each Downloads change, every 30
        # minutes): print only news there, which watch.log (rolled over at 1 MB) also records.
        if not under_launchd or s.get("news"):
            _echo(watch_mod.format_scan(s))
        if s["errors"]:
            raise typer.Exit(1)
        return
    st = watch_mod.settings(cfg)
    _echo(f"Watching {', '.join(str(d) for d in st.dirs)} for {st.pattern} every {interval or st.interval}s "
          "(Ctrl-C stops). Your files are only read, never moved.")
    try:
        watch_mod.run_forever(lambda: connect(cfg.db_path), cfg, interval=interval, echo=_echo)
    except KeyboardInterrupt:
        _echo("Stopped.")


@watch_app.command("install")
def watch_install() -> None:
    """Install the LaunchAgent: scans when Downloads changes and every 30 minutes, even without golf serve."""
    from golf import watch as watch_mod

    cfg = get_cfg()
    try:
        r = watch_mod.install(cfg, agents_dir=AGENTS_DIR, launchctl=LAUNCHCTL)
    except watch_mod.WatchError as e:
        _fail(str(e), 1)
    _echo(f"LaunchAgent installed: {r['plist']}\n  runs {r['python']} -m golf.cli watch --once\n"
          f"  when these change: {', '.join(r['watch_paths']) or '(no watched folder exists yet)'}; and every "
          f"{r['interval'] // 60} minutes.\n  Log: {_display_path(cfg, cfg.data_dir / 'logs' / 'watch.log')}")
    _echo("\n" + watch_mod.permission_help(cfg, r["python"])
          + "\n`golf watch status` shows whether the last scan could read the folders.")


@watch_app.command("uninstall")
def watch_uninstall() -> None:
    """Unload and remove the LaunchAgent."""
    from golf import watch as watch_mod

    r = watch_mod.uninstall(get_cfg(), agents_dir=AGENTS_DIR, launchctl=LAUNCHCTL)
    _echo(f"LaunchAgent {'removed' if r['removed'] else 'was not installed'}: {r['plist']}")


@watch_app.command("status")
def watch_status() -> None:
    """Is the LaunchAgent installed and loaded, which folders are watched, what did the last scan see."""
    from golf import watch as watch_mod

    cfg = get_cfg()
    with open_db(cfg) as conn:
        w = watch_mod.status_info(conn, cfg, agents_dir=AGENTS_DIR, launchctl=LAUNCHCTL, check_loaded=True)
        last = status_info(conn, cfg)["last_export"]
    a = w["agent"]
    _echo("LaunchAgent: " + ("not installed (`golf watch install`)" if not a["installed"] else
                             f"installed ({a['plist']}), " + ("loaded" if a["loaded"] else "NOT loaded")
                             + (f", last exit code {a['last_exit']}" if a["last_exit"] not in (None, "") else "")))
    for d in w["dirs"]:
        _echo(f"  watches {d['path']}" + ("" if d["exists"] else "  (does not exist)"))
    scan = w["last_scan"]
    if scan:
        _echo(f"Last scan: {scan['at']} ({scan.get('source')}): imported {len(scan.get('imported') or [])}, "
              f"waiting {len(scan.get('pending') or [])}, errors {len(scan.get('errors') or [])}.")
        for d in scan.get("denied_dirs") or []:
            _echo(f"  NO PERMISSION to read {d}.")
    else:
        _echo("Last scan: never.")
    _echo("Last 18Birdies export: " + (f"snapshot {last['snapshot_date']}, {last['age_days']} day(s) ago"
                                       if last else "never imported"))
    _echo(f"Log: {w['log']}\nThe LaunchAgent runs {w['python_for_permissions']}"
          + (" (a Python used only by this app)." if w["python_dedicated"] else
             " (a Python other programs share: prefer Files and Folders over Full Disk Access; see below).")
          + "\n" + watch_mod.permission_help(cfg))


@app.command()
def publish(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Build and privacy-check only; nothing is pushed.")]
    = False,
    force: Annotated[bool, typer.Option("--force", help="Push even if nothing changed since the last publish.")]
    = False,
) -> None:
    """Publish the dashboard to GitHub Pages (gh-pages branch). No paths, photos, GPS or API cost go out."""
    from golf import publish as publish_mod

    cfg = get_cfg()
    with open_db(cfg) as conn:
        try:
            out = publish_mod.publish(conn, cfg, dry_run=dry_run, force=force, runner=PUBLISH_RUNNER)
        except publish_mod.PublishPrivacyError as e:
            _echo(f"Not published: the public build contains {len(e.findings)} thing(s) that must stay on this Mac:")
            for f in e.findings:
                _echo(f"  {f}")
            raise typer.Exit(1)
        except publish_mod.PublishError as e:
            _fail(str(e), 1)
    _echo(publish_mod.format_publish(out))


# ---------------------------------------------------------------- privacy
@app.command("privacy-check")
def privacy_check_cmd(
    paths: Annotated[Optional[List[Path]], typer.Argument(help="Files or folders to scan (a folder: what git would "
                                                               "commit); default: the staged files.")] = None,
    staged: Annotated[bool, typer.Option("--staged", help="Scan what is staged for commit.")] = False,
) -> None:
    """Scan files for personal data (the pre-commit hook runs this on staged files)."""
    from golf.privacy import privacy_check, staged_files

    from golf.privacy import expand_paths

    cfg = get_cfg()
    root = Path(cfg.root)
    if paths and not staged:
        try:
            targets: list[Any] = expand_paths(paths, root)
        except FileNotFoundError as e:
            _fail(str(e))
    else:
        try:
            targets = staged_files(root)
        except (RuntimeError, FileNotFoundError) as e:
            _fail(f"Could not list staged files: {e}")
        staged = True
    findings = privacy_check(targets, root=root, db_path=cfg.db_path, staged=staged)
    if not findings:
        _echo(f"privacy-check: {len(targets)} file(s) clean.")
        return
    _echo(f"privacy-check: {len(findings)} problem(s) - personal data must stay under data/ (gitignored):", )
    for f in findings:
        _echo(f"  {f}")
    _echo("Unstage them (git restore --staged <file>). A synthetic test fixture can opt out of content checks "
          "with the line 'privacy-check: synthetic' near its top.")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
