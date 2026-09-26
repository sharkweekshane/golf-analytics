"""Round screenshots -> per-hole data (RESEARCH_PLAN §3, DECISIONS §1.2).

extract_round() sends one round's screenshots to Claude and stores an extractions row. The round is
matched to an existing round (an export round first) or reserved as a screenshot round 'ss-<hash>',
validated (golf.extract.validate), up to five flagged cells are re-read, and the extraction is either
auto-accepted or queued for review. Only accepted extractions write round_holes, and they never
overwrite strokes from the export or a manual entry: those are the independent checksum.

The review model (pending_round_reviews, get_review, apply_corrections, accept/reject) has no UI in it;
the web page and the CLI are built on top.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rapidfuzz import fuzz

from .. import llm
from ..config import Config
from ..db import dumps, loads, now_iso, upsert
from ..schemas.rounds import SCHEMA_VERSION, CellReread, ExtractedRound
from . import validate as V

PROMPT_VERSION = "round-v1"
REREAD_PROMPT_VERSION = "reread-v1"
PURPOSE = "round_screenshots"
REREAD_PURPOSE = "reread"
PROMPTS_DIR = Path(__file__).with_name("prompts")

APPLIED = ("auto_accepted", "accepted")
OPEN = ("extracted", "needs_review")
REFERENCE_SOURCES = ("18b_export", "manual")
SOURCE_RANK = {"18b_export": 0, "manual": 1, "screenshot": 2}
ROUND_FIELDS = ("date_iso", "course_text", "tee_text", "course_par")
CLUB_MATCH_SCORE = 85
REREAD_MAX_TOKENS = 16000          # one cell, but adaptive thinking counts toward it
DB_FAIRWAY = V.FAIRWAY_RESULTS + ("not_applicable",)
DB_GIR_MISS = V.MISS_DIRECTIONS + ("no_chance",)
EMPTY_VALUE = {"symbol": "not_visible", "fairway": "not_recorded", "gir": "not_recorded", "gir_miss": "none"}
FIELD_HELP = {
    "par": "the Par row",
    "si": "the Handicap row (stroke index)",
    "strokes": "the player's gross score",
    "symbol": "the shape drawn around the player's score",
    "putts": "the Putts row",
    "fairway": "the Fairway Hit row",
    "gir": "the Green in Regulation row",
    "gir_miss": "the miss direction in the Green in Regulation row",
    "penalties": "the Penalties row",
    "chips": "the Chip Shots row",
    "sand": "the Greenside Sand Shots row",
}
_STATS_SCREENS = ("scorecard_stats",)
PREFERRED_SCREENS = {
    **{f: _STATS_SCREENS for f in V.STATS_FIELDS},
    "par": ("scorecard_scores",),
    "si": ("scorecard_scores",),
    "strokes": ("scorecard_scores", "round_summary", "share_scorecard", "scorecard_stats"),
    "symbol": ("round_summary", "share_scorecard", "scorecard_scores"),
}


# ----------------------------------------------------------------- prompts
def _prompt(name: str) -> str:
    """Prompt file text without its leading maintainer comment."""
    text = (PROMPTS_DIR / name).read_text()
    if text.lstrip().startswith("<!--"):
        text = text.split("-->", 1)[1]
    return text.strip()


def system_prompt() -> str:
    """round_v1.md with the screen guide spliced in. The guide is a separate file because it will be
    rewritten from Shane's real screenshots; the rules around it stay frozen."""
    return _prompt("round_v1.md").replace("{{SCREEN_GUIDE}}", _prompt("screen_guide_18birdies.md"))


def reread_system_prompt() -> str:
    return _prompt("reread_v1.md")


# -------------------------------------------------------------- identities
def _group_hash(image_sha256s: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(image_sha256s)).encode()).hexdigest()


def screenshot_round_id(image_sha256s: Iterable[str]) -> str:
    """'ss-' + sha256 of the sorted image hashes: the same screenshots always name the same round."""
    return "ss-" + _group_hash(image_sha256s)[:16]


def file_sha256(path: Path | str) -> str:
    """Same key golf.llm.load_image uses (the original bytes), without decoding the image."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rel_path(cfg: Config, path: Path | str) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(Path(cfg.data_dir).resolve()).as_posix()
    except ValueError:
        return str(p)


def image_path(cfg: Config, stored: str) -> Path:
    """Absolute path of an image as stored in extractions.image_paths (relative to data_dir)."""
    p = Path(stored)
    return p if p.is_absolute() else Path(cfg.data_dir) / p


def unreadable_images(paths: Iterable[Path | str]) -> list[tuple[Path, str]]:
    """(path, reason) for every file that is not an image Pillow can open (empty, truncated, text named
    .png). Cheap: headers are verified, pixels are not decoded. Callers reject these before copying or
    sending anything, so one bad file can never block a whole inbox."""
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    bad: list[tuple[Path, str]] = []
    for p in map(Path, paths):
        try:
            if p.stat().st_size == 0:
                raise ValueError("empty file")
            with Image.open(p) as im:
                im.verify()
        except Exception as e:  # PIL raises several unrelated types for junk input
            bad.append((p, str(e) or type(e).__name__))
    return bad


def check_readable(paths: Iterable[Path | str]) -> None:
    """ValueError naming the first unreadable image, if any."""
    bad = unreadable_images(paths)
    if bad:
        names = ", ".join(p.name for p, _ in bad)
        raise ValueError(f"{names}: not a readable image ({bad[0][1]}). Nothing was sent or copied; "
                         "re-export the screenshot from Photos and try again.")


def find_extraction(conn: sqlite3.Connection, image_sha256s: Iterable[str]) -> int | None:
    """The extraction of exactly this set of images, in any order."""
    want = sorted(image_sha256s)
    for row in conn.execute("SELECT extraction_id, image_sha256s FROM extractions ORDER BY extraction_id DESC"):
        if sorted(loads(row["image_sha256s"], [])) == want:
            return row["extraction_id"]
    return None


def relocate_images(conn: sqlite3.Connection, cfg: Config, extraction_id: int, new_paths: Sequence[Path | str]) -> None:
    """Point an extraction at its images' new home (the inbox moves files after extracting)."""
    row = _xrow(conn, extraction_id)
    if len(new_paths) != len(loads(row["image_paths"], [])):
        raise ValueError("relocate_images needs one new path per stored image")
    conn.execute("UPDATE extractions SET image_paths = ? WHERE extraction_id = ?",
                 (dumps([rel_path(cfg, p) for p in new_paths]), extraction_id))
    conn.execute("UPDATE imports SET path = ? WHERE kind = 'screenshot' AND sha256 = ?",
                 (rel_path(cfg, Path(new_paths[0]).parent), _group_hash(loads(row["image_sha256s"]))))
    conn.commit()


# -------------------------------------------------------------- row access
def _xrow(conn: sqlite3.Connection, extraction_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM extractions WHERE extraction_id = ?", (extraction_id,)).fetchone()
    if row is None:
        raise KeyError(f"No extraction {extraction_id}")
    return row


def _parsed(row: sqlite3.Row) -> ExtractedRound:
    return ExtractedRound.model_validate_json(row["result_json"])


def _round(conn: sqlite3.Connection, round_id: str | None) -> sqlite3.Row | None:
    if not round_id:
        return None
    return conn.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()


def _context(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """What extract_round was told (player, played_on, club_id, expected_holes). It lives in the call's
    request_meta, which is part of the same cache key as the request text that carried it."""
    meta = conn.execute("SELECT request_meta FROM llm_calls WHERE call_id = ?", (row["call_id"],)).fetchone()
    return loads(meta[0], {}) if meta else {}


def _model_output(conn: sqlite3.Connection, row: sqlite3.Row) -> ExtractedRound:
    """The model's own answer, before any human correction (the 'model_value' of a correction)."""
    raw = conn.execute("SELECT response_json FROM llm_calls WHERE call_id = ?", (row["call_id"],)).fetchone()
    try:
        return ExtractedRound.model_validate_json(raw[0])
    except Exception:
        return _parsed(row)


def _outcome(conn: sqlite3.Connection, extraction_id: int, *, cost: float, cached: bool) -> dict[str, Any]:
    row = _xrow(conn, extraction_id)
    return {
        "extraction_id": extraction_id,
        "status": row["status"],
        "round_id": row["round_id"],
        "flags": V.active_flags(loads(row["flags"], [])),
        "cost_usd": round(cost, 6),
        "cached": cached,
    }


# -------------------------------------------------------------- extraction
def extract_round(
    conn: sqlite3.Connection,
    cfg: Config,
    image_paths: Sequence[Path | str],
    *,
    expected_holes: int | None = None,
    played_on: str | None = None,
    club_id: str | None = None,
    force: bool = False,
    reread: bool = True,
) -> dict[str, Any]:
    """Extract one round from all of its screenshots (in capture order) and settle its status.

    Returns {extraction_id, status, round_id, flags, cost_usd, cached}. An image set that was already
    extracted comes back from the database with no API call unless `force`. The export's strokes and
    totals are never put in the request: they are the independent checksum."""
    paths = [Path(p) for p in image_paths]
    if not paths:
        raise ValueError("extract_round needs at least one image")
    images = []
    for p in paths:
        try:
            images.append(llm.load_image(p))
        except FileNotFoundError:
            raise
        except Exception as e:        # PIL.UnidentifiedImageError, truncated data, HEIF decoder errors
            raise ValueError(f"{p.name} is not a readable image ({e})") from None
    shas = [im.sha256 for im in images]
    existing = find_extraction(conn, shas)
    if existing is not None and not force:
        out = _outcome(conn, existing, cost=0.0, cached=True)
        known = _effective_date(conn, existing)
        if played_on and known and played_on != known:
            out["warning"] = (f"These screenshots were extracted before (extraction {existing}, played {known}); "
                              f"the date {played_on} was not applied. To change it: "
                              f"golf review fix {existing} - date_iso {played_on}")
        return out

    context = {"player": cfg.player_name, "played_on": played_on, "club_id": club_id, "expected_holes": expected_holes}
    content = llm.image_blocks(images) + [{"type": "text", "text": _request_text(conn, context)}]
    res = llm.structured_call(
        conn, purpose=PURPOSE, system=system_prompt(), content=content, output_model=ExtractedRound,
        prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
        request_meta={"image_sha256s": shas, **context},
        model=cfg.model, effort=cfg.effort, max_tokens=cfg.max_tokens, force=force,
    )
    _record_import(conn, cfg, paths, shas)
    values = {
        "round_id": None, "image_paths": dumps([rel_path(cfg, p) for p in paths]), "image_sha256s": dumps(shas),
        "call_id": res.call_id, "status": "extracted", "flags": "[]",
        "result_json": dumps(res.parsed.model_dump()), "created_at": now_iso(), "reviewed_at": None,
    }
    if existing is None:
        cols = list(values)
        cur = conn.execute(f"INSERT INTO extractions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                           [values[c] for c in cols])
        xid = int(cur.lastrowid)
    else:
        xid = existing
        _set_status(conn, xid, "extracted")
        conn.execute(f"UPDATE extractions SET {', '.join(f'{c} = ?' for c in values)} WHERE extraction_id = ?",
                     [*values.values(), xid])
    _assign_round(conn, xid)
    cost = res.cost_usd + _settle(conn, cfg, xid, reread=reread)
    conn.commit()
    return _outcome(conn, xid, cost=cost, cached=res.cached)


def _effective_date(conn: sqlite3.Connection, extraction_id: int) -> str | None:
    """The played date an extraction currently stands for: its round's, else the one it would use."""
    row = _xrow(conn, extraction_id)
    rr = _round(conn, row["round_id"])
    if rr is not None:
        return rr["played_on_local"]
    return _played_on(conn, row, _parsed(row), _context(conn, row))


def _request_text(conn: sqlite3.Connection, context: Mapping[str, Any]) -> str:
    parts = [f"Extract the round for player: {context['player']}."]
    if context.get("expected_holes"):
        parts.append(f"Expected holes: {context['expected_holes']}.")
    if context.get("played_on"):
        parts.append(f"Played on: {context['played_on']}.")
    club = _club_name(conn, context.get("club_id"))
    if club:
        parts.append(f"Club: {club}.")
    parts.append("The images are this round's screens in capture order.")
    return " ".join(parts)


def _record_import(conn: sqlite3.Connection, cfg: Config, paths: list[Path], shas: list[str]) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO imports(kind, path, sha256, imported_at, summary) VALUES ('screenshot', ?, ?, ?, ?)",
        (rel_path(cfg, Path(os.path.commonpath([str(p.resolve().parent) for p in paths]))), _group_hash(shas),
         now_iso(), dumps({"images": len(shas)})),
    )


def _import_id(conn: sqlite3.Connection, shas: Iterable[str]) -> int | None:
    row = conn.execute("SELECT import_id FROM imports WHERE kind = 'screenshot' AND sha256 = ?",
                       (_group_hash(shas),)).fetchone()
    return row[0] if row else None


def _settle(conn: sqlite3.Connection, cfg: Config, extraction_id: int, *, reread: bool) -> float:
    """Validate, decide, and give flagged cells one independent re-read before a human sees them."""
    flags = _revalidate(conn, extraction_id)
    if _decide(conn, extraction_id) != "needs_review" or not reread:
        return 0.0
    cells = V.reread_targets(flags)
    if not cells:
        return 0.0
    return sum(r["cost_usd"] for r in reread_cells(conn, cfg, extraction_id, cells))


# ---------------------------------------------------------------- matching
def _valid_date(s: str | None) -> bool:
    if not s or len(s) != 10:
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _club_name(conn: sqlite3.Connection, club_id: str | None) -> str | None:
    if not club_id:
        return None
    row = conn.execute("SELECT name FROM clubs WHERE club_id = ?", (club_id,)).fetchone()
    return row[0] if row else None


def _club_id(conn: sqlite3.Connection, extracted: ExtractedRound, context: Mapping[str, Any]) -> str | None:
    """The hint wins; otherwise a confident, unambiguous fuzzy match of the course name to a known club."""
    if context.get("club_id"):
        return context["club_id"]
    text = _norm_text(extracted.course_text)
    if not text:
        return None
    scored = sorted(((fuzz.token_set_ratio(text, _norm_text(r["name"])), r["club_id"])
                     for r in conn.execute("SELECT club_id, name FROM clubs WHERE name IS NOT NULL")), reverse=True)
    if scored and scored[0][0] >= CLUB_MATCH_SCORE and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


def _played_on(conn: sqlite3.Connection, row: sqlite3.Row, extracted: ExtractedRound,
               context: Mapping[str, Any]) -> str | None:
    """A reviewer's date correction beats the caller's hint, which beats the date read off the screen."""
    corrected = conn.execute(
        "SELECT 1 FROM corrections WHERE extraction_id = ? AND entity = 'round' AND field = 'date_iso'",
        (row["extraction_id"],)).fetchone()
    hinted = context.get("played_on")
    order = (extracted.date_iso, hinted) if corrected else (hinted, extracted.date_iso)
    return next((d for d in order if _valid_date(d)), None)


def _hole_strokes(extracted: ExtractedRound) -> dict[int, int]:
    return {h.hole: h.strokes for h in extracted.holes if h.strokes >= 1}


def _gross(extracted: ExtractedRound) -> int | None:
    if extracted.displayed.gross >= 1:
        return extracted.displayed.gross
    strokes = _hole_strokes(extracted)
    return sum(strokes.values()) if frozenset(strokes) in (V.ONE_NINE, V.BACK_NINE, V.EIGHTEEN) else None


def _club_relation(conn: sqlite3.Connection, r: sqlite3.Row, club_id: str | None, course_text: str) -> str:
    if club_id and r["club_id"]:
        return "same" if club_id == r["club_id"] else "different"
    other = _club_name(conn, r["club_id"])
    if not other and r["source"] == "screenshot":
        other = loads(r["raw"], {}).get("course_text", "")
    if course_text and other and fuzz.token_set_ratio(_norm_text(course_text), _norm_text(other)) >= CLUB_MATCH_SCORE:
        return "same"
    return "unknown"


def _strokes_agree(conn: sqlite3.Connection, r: sqlite3.Row, strokes: Mapping[int, int],
                   gross: int | None) -> bool | None:
    """Same round if hole scores agree but for a misread or two (1 on a nine, 2 on eighteen); a total-only
    record can only be compared on gross. None when there is nothing to compare."""
    ref = V.reference_strokes(conn, r["round_id"])
    common = set(ref) & set(strokes)
    if common:
        mismatches = sum(ref[h] != strokes[h] for h in common)
        return mismatches <= (1 if len(common) <= 9 else 2)
    if gross is not None and r["gross"]:
        return gross == r["gross"]
    return None


def _same_day(conn: sqlite3.Connection, played_on: str, sources: Sequence[str]) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM rounds WHERE played_on_local = ? AND deleted_in_source = 0 AND entry_mode <> 'abandoned'"
        f" AND source IN ({', '.join('?' for _ in sources)}) ORDER BY round_id", (played_on, *sources)).fetchall()


def _match(conn: sqlite3.Connection, played_on: str | None, club_id: str | None, course_text: str,
           strokes: Mapping[int, int], gross: int | None, *, sources: Sequence[str] = tuple(SOURCE_RANK)) -> str | None:
    if not played_on:
        return None
    best: tuple | None = None
    for r in _same_day(conn, played_on, sources):
        club = _club_relation(conn, r, club_id, course_text)
        if club == "different":
            continue
        agree = _strokes_agree(conn, r, strokes, gross)
        if agree is False or (club == "unknown" and agree is not True):
            continue
        key = (SOURCE_RANK[r["source"]], club != "same", agree is not True, r["round_id"])
        best = min(best, key) if best else key
    return best[-1] if best else None


def match_round(conn: sqlite3.Connection, cfg: Config | None, extracted: ExtractedRound,
                hint: Mapping[str, Any] | None = None) -> str | None:
    """The existing round these screenshots belong to, or None.

    Same local date and club with agreeing strokes, preferring an export round, then a manual one,
    then an earlier screenshot round; with no club information, same date and agreeing strokes.
    `hint` = {played_on, club_id} from the caller (either may be missing). `cfg` is not needed today."""
    hint = dict(hint or {})
    played_on = hint.get("played_on") if _valid_date(hint.get("played_on")) else None
    played_on = played_on or (extracted.date_iso if _valid_date(extracted.date_iso) else None)
    return _match(conn, played_on, _club_id(conn, extracted, hint), extracted.course_text,
                  _hole_strokes(extracted), _gross(extracted))


def _assign_round(conn: sqlite3.Connection, extraction_id: int, *,
                  sources: Sequence[str] = tuple(SOURCE_RANK)) -> str | None:
    """Attach the extraction to a matching round, else reserve its 'ss-' id (the round row itself is only
    written on acceptance). None when there is no date to place the round."""
    row = _xrow(conn, extraction_id)
    if row["round_id"] and _round(conn, row["round_id"]) is not None:
        return row["round_id"]
    ex, ctx = _parsed(row), _context(conn, row)
    played_on = _played_on(conn, row, ex, ctx)
    rid = _match(conn, played_on, _club_id(conn, ex, ctx), ex.course_text, _hole_strokes(ex), _gross(ex), sources=sources)
    if rid is None and played_on:
        rid = screenshot_round_id(loads(row["image_sha256s"]))
    conn.execute("UPDATE extractions SET round_id = ? WHERE extraction_id = ?", (rid, extraction_id))
    return rid


def _near_misses(conn: sqlite3.Connection, row: sqlite3.Row, ex: ExtractedRound,
                 ctx: Mapping[str, Any]) -> list[sqlite3.Row]:
    """Independent records on the same day at the same club whose hole scores disagree."""
    played_on = _played_on(conn, row, ex, ctx)
    if not played_on:
        return []
    club_id, strokes, gross = _club_id(conn, ex, ctx), _hole_strokes(ex), _gross(ex)
    return [r for r in _same_day(conn, played_on, REFERENCE_SOURCES)
            if _club_relation(conn, r, club_id, ex.course_text) == "same"
            and _strokes_agree(conn, r, strokes, gross) is False]


# -------------------------------------------------------------- validation
def _tee_holes(conn: sqlite3.Connection, rr: sqlite3.Row | None, club_id: str | None) -> dict[int, dict] | None:
    """Per-hole par/SI for the tee played: the round's (override-aware) tee, else the course default."""
    tee_id = course_key = None
    if rr is not None:
        v = conn.execute("SELECT tee_id_eff, course_key_eff FROM v_rounds WHERE round_id = ?",
                         (rr["round_id"],)).fetchone()
        tee_id, course_key = (v["tee_id_eff"], v["course_key_eff"]) if v else (rr["tee_id"], rr["course_key"])
        club_id = club_id or rr["club_id"]
    if not tee_id and not course_key and club_id:
        keys = [r[0] for r in conn.execute("SELECT course_key FROM courses WHERE club_id = ?", (club_id,))]
        course_key = keys[0] if len(keys) == 1 else None
    if not tee_id and course_key:
        tees = [r[0] for r in conn.execute("SELECT tee_id FROM tees WHERE course_key = ? AND is_default = 1",
                                           (course_key,))]
        tee_id = tees[0] if len(tees) == 1 else None
    if not tee_id:
        return None
    rows = conn.execute("SELECT hole, par, si, yards FROM tee_holes WHERE tee_id = ?", (tee_id,)).fetchall()
    return {r["hole"]: dict(r) for r in rows} or None


def _revalidate(conn: sqlite3.Connection, extraction_id: int) -> list[dict]:
    row = _xrow(conn, extraction_id)
    ex, ctx = _parsed(row), _context(conn, row)
    rr = _round(conn, row["round_id"])
    flags = V.validate_extraction(conn, ex, rr, _tee_holes(conn, rr, _club_id(conn, ex, ctx)),
                                  expected_holes=ctx.get("expected_holes"), player_name=ctx.get("player"))
    if row["round_id"] is None:
        flags.append(V.flag("D1", "E", None, "date_iso",
                            "No played date: none was given and none is readable on the screens. "
                            "Correct date_iso to place the round."))
    if rr is None or rr["source"] == "screenshot":
        for r in _near_misses(conn, row, ex, ctx):
            flags.append(V.flag("M1", "E", None, "strokes",
                                f"Round {r['round_id']} ({r['source']}) on the same day at the same club has different "
                                "hole scores. If it is this round, scores were misread or that record is stale; if it "
                                "is a different round, accept with force to keep both."))
    flags = V.carry_over(loads(row["flags"], []), flags, ex)
    conn.execute("UPDATE extractions SET flags = ? WHERE extraction_id = ?", (dumps(flags), extraction_id))
    return flags


def _decide(conn: sqlite3.Connection, extraction_id: int, *, human: bool = False) -> str:
    """Machine decisions never override a person's accept/reject; a person's corrections use the
    reviewed rule (no E, at most one W) instead of the auto-accept rule."""
    row = _xrow(conn, extraction_id)
    if not human and row["status"] not in OPEN + ("auto_accepted",):
        return row["status"]
    flags = loads(row["flags"], [])
    if human:
        new = "accepted" if V.is_clean(flags) else "needs_review"
    else:
        new = "auto_accepted" if V.auto_accept(flags, _parsed(row)) else "needs_review"
    if new in APPLIED and row["round_id"] is None:
        new = "needs_review"
    _set_status(conn, extraction_id, new)
    return new


def _set_status(conn: sqlite3.Connection, extraction_id: int, new: str) -> None:
    old = _xrow(conn, extraction_id)["status"]
    if old in APPLIED and new not in APPLIED:
        _unapply(conn, extraction_id)
    conn.execute("UPDATE extractions SET status = ?, reviewed_at = ? WHERE extraction_id = ?",
                 (new, now_iso() if new in ("accepted", "rejected") else None, extraction_id))
    if new in APPLIED:
        apply_extraction(conn, extraction_id)
    conn.commit()


# ---------------------------------------------------------------- applying
def _db_int(v: int) -> int | None:
    return v if v is not None and v >= 0 else None


def _stats(h) -> dict[str, Any]:
    return {
        "putts": _db_int(h.putts),
        "fairway": h.fairway if h.fairway in DB_FAIRWAY else None,
        "gir": {"hit": 1, "miss": 0}.get(h.gir),
        "gir_miss": h.gir_miss if h.gir == "miss" and h.gir_miss in DB_GIR_MISS else None,
        "penalties": _db_int(h.penalties),
        "chips": _db_int(h.chips),
        "sand": _db_int(h.sand),
    }


def _has_stats(h) -> bool:
    return any(v is not None for k, v in _stats(h).items() if not (k == "fairway" and v == "not_applicable"))


def apply_extraction(conn: sqlite3.Connection, extraction_id: int) -> dict[str, Any]:
    """Write an accepted extraction into round_holes.

    Export/manual rounds: stats (stats_src='screenshot') and missing par/SI on the holes they already
    have; their strokes are never touched. Screenshot rounds: strokes too (strokes_src='screenshot'),
    and the round-level totals are recomputed from the holes."""
    row = _xrow(conn, extraction_id)
    if row["status"] not in APPLIED:
        raise ValueError(f"Extraction {extraction_id} is '{row['status']}'; only accepted extractions are applied.")
    ex = _parsed(row)
    rid = row["round_id"]
    if rid is None or _round(conn, rid) is None:
        # A sibling extraction of the same round (e.g. Scores view captured separately) may have created it.
        conn.execute("UPDATE extractions SET round_id = NULL WHERE extraction_id = ?", (extraction_id,))
        rid = _assign_round(conn, extraction_id, sources=("screenshot",))
        if rid is None:
            raise ValueError(f"Extraction {extraction_id} has no played date; correct date_iso first.")
        if _round(conn, rid) is None:
            create_screenshot_round(conn, extraction_id)
    rr = _round(conn, rid)
    if rr["source"] == "screenshot":
        written, skipped = _write_screenshot_holes(conn, rid, ex), []
        _recompute_screenshot_round(conn, rid, ex)
    else:
        written, skipped = _write_reference_holes(conn, rid, ex)
    conn.commit()
    return {"extraction_id": extraction_id, "round_id": rid, "holes_written": written, "holes_skipped": skipped}


def _write_reference_holes(conn: sqlite3.Connection, rid: str, ex: ExtractedRound) -> tuple[list[int], list[int]]:
    existing = {r["hole"]: r for r in conn.execute("SELECT * FROM round_holes WHERE round_id = ?", (rid,))}
    written, skipped = [], []
    for h in ex.holes:
        e = existing.get(h.hole)
        if e is None or not e["strokes"]:
            if h.strokes >= 1 or (h.stats_visible and _has_stats(h)):
                skipped.append(h.hole)
            continue
        sets: dict[str, Any] = {
            "par": e["par"] if e["par"] is not None else _db_int(h.par),
            "si": e["si"] if e["si"] is not None else _db_int(h.si),
        }
        if h.stats_visible and e["stats_src"] != "manual":
            sets.update(_stats(h), stats_src="screenshot")
        conn.execute(f"UPDATE round_holes SET {', '.join(f'{c} = ?' for c in sets)} WHERE round_id = ? AND hole = ?",
                     [*sets.values(), rid, h.hole])
        written.append(h.hole)
    return written, skipped


def _write_screenshot_holes(conn: sqlite3.Connection, rid: str, ex: ExtractedRound) -> list[int]:
    """Later readings fill and update earlier ones; a value this extraction could not see is kept."""
    existing = {r["hole"]: dict(r) for r in conn.execute("SELECT * FROM round_holes WHERE round_id = ?", (rid,))}
    written = []
    for h in ex.holes:
        if h.strokes < 1 and not (h.stats_visible and _has_stats(h)):
            continue
        e = existing.get(h.hole, {})
        strokes = _db_int(h.strokes) if h.strokes >= 1 else e.get("strokes")
        row = {
            "round_id": rid, "hole": h.hole, "strokes": strokes,
            "par": _db_int(h.par) if h.par >= 0 else e.get("par"),
            "si": _db_int(h.si) if h.si >= 0 else e.get("si"),
            "strokes_src": "screenshot" if strokes else None,
        }
        if h.stats_visible:
            row.update(_stats(h), stats_src="screenshot")
        else:
            row.update({k: e.get(k) for k in (*V.STATS_FIELDS, "stats_src")})
        upsert(conn, "round_holes", row, ["round_id", "hole"])
        written.append(h.hole)
    return written


def create_screenshot_round(conn: sqlite3.Connection, extraction_id: int) -> str:
    """The rounds row for a round seen only in screenshots (source='screenshot', round_id 'ss-<hash>').
    entry_mode and totals are filled by apply_extraction from the holes."""
    row = _xrow(conn, extraction_id)
    ex, ctx = _parsed(row), _context(conn, row)
    shas = loads(row["image_sha256s"])
    rid = row["round_id"] or screenshot_round_id(shas)
    played_on = _played_on(conn, row, ex, ctx)
    if not played_on:
        raise ValueError(f"Extraction {extraction_id} has no played date; correct date_iso first.")
    raw = {"extraction_id": extraction_id, "course_text": ex.course_text, "tee_text": ex.tee_text,
           "date_text": ex.date_text, "course_par": ex.course_par, "rating_text": ex.rating_text,
           "displayed": ex.displayed.model_dump()}
    import_id = _import_id(conn, shas)
    conn.execute(
        "INSERT INTO rounds(round_id, source, played_on_local, club_id, entry_mode, raw, first_import_id, last_import_id)"
        " VALUES (?, 'screenshot', ?, ?, 'partial', ?, ?, ?)"
        " ON CONFLICT(round_id) DO UPDATE SET played_on_local = excluded.played_on_local,"
        " club_id = COALESCE(excluded.club_id, rounds.club_id), raw = excluded.raw,"
        " last_import_id = excluded.last_import_id",
        (rid, played_on, _club_id(conn, ex, ctx), dumps(raw), import_id, import_id),
    )
    conn.execute("UPDATE extractions SET round_id = ? WHERE extraction_id = ?", (rid, extraction_id))
    return rid


def _recompute_screenshot_round(conn: sqlite3.Connection, rid: str, ex: ExtractedRound) -> None:
    holes = [dict(r) for r in conn.execute("SELECT * FROM round_holes WHERE round_id = ? ORDER BY hole", (rid,))]
    scored = [h for h in holes if h["strokes"]]
    played = frozenset(h["hole"] for h in scored)
    nine = {V.ONE_NINE: "front", V.BACK_NINE: "back"}.get(played)
    if played in (V.ONE_NINE, V.BACK_NINE, V.EIGHTEEN):
        entry_mode = "hole_by_hole"
    elif not scored and ex.displayed.gross >= 1:
        entry_mode = "total_only"
    else:
        entry_mode = "partial"
    gross = sum(h["strokes"] for h in scored) if scored else (ex.displayed.gross if ex.displayed.gross >= 1 else None)
    all_par = bool(scored) and all(h["par"] for h in scored)
    to_par = gross - sum(h["par"] for h in scored) if all_par else V.parse_to_par(ex.displayed.to_par_text)
    totals: dict[str, Any] = {
        "entry_mode": entry_mode, "holes_played": len(scored) or None, "nine": nine, "gross": gross, "to_par": to_par,
        "par_played": gross - to_par if gross is not None and to_par is not None else None,
    }
    fw = [h for h in scored if h["fairway"] in V.FAIRWAY_RESULTS]
    totals.update({f"fw_{k}": (sum(h["fairway"] == k for h in fw) if fw else None)
                   for k in ("hit", "left", "right", "short", "long")}, fw_chances=len(fw) or None)
    gir = [h for h in scored if h["gir"] is not None]
    totals.update({f"gir_{k}": (sum(h["gir_miss"] == k for h in gir) if gir else None) for k in V.MISS_DIRECTIONS},
                  gir=sum(h["gir"] for h in gir) if gir else None, gir_chances=len(gir) or None)
    putts = [h["putts"] for h in scored if h["putts"] is not None]
    totals.update(putts=sum(putts) if putts else None, putts_tracked=int(bool(putts)))
    diffs = [h["strokes"] - h["par"] for h in scored] if all_par else None
    for col, test in (("eagles_plus", lambda d: d <= -2), ("birdies", lambda d: d == -1), ("pars", lambda d: d == 0),
                      ("bogeys", lambda d: d == 1), ("dbl_plus", lambda d: d >= 2)):
        totals[col] = sum(map(test, diffs)) if diffs is not None else None
    conn.execute(f"UPDATE rounds SET {', '.join(f'{c} = ?' for c in totals)} WHERE round_id = ?", [*totals.values(), rid])


def _unapply(conn: sqlite3.Connection, extraction_id: int) -> None:
    """Take an extraction's data back out of its round, then replay the round's other accepted extractions."""
    rid = _xrow(conn, extraction_id)["round_id"]
    rr = _round(conn, rid)
    if rr is None:
        return
    others = [r[0] for r in conn.execute(
        "SELECT extraction_id FROM extractions WHERE round_id = ? AND extraction_id <> ?"
        " AND status IN ('auto_accepted', 'accepted') ORDER BY extraction_id", (rid, extraction_id))]
    if rr["source"] == "screenshot":
        conn.execute("DELETE FROM round_holes WHERE round_id = ?", (rid,))
        if not others:
            _drop_round(conn, rid)
    else:
        conn.execute(
            "UPDATE round_holes SET putts = NULL, fairway = NULL, gir = NULL, gir_miss = NULL, penalties = NULL,"
            " chips = NULL, sand = NULL, stats_src = NULL WHERE round_id = ? AND stats_src = 'screenshot'", (rid,))
    for other in others:
        apply_extraction(conn, other)


def _drop_round(conn: sqlite3.Connection, rid: str, *, moved_to: str | None = None) -> None:
    """Remove a screenshot round. On a merge, links a person made to it follow it to the export round."""
    conn.execute("DELETE FROM round_holes WHERE round_id = ?", (rid,))
    conn.execute("DELETE FROM handicap_history WHERE round_id = ?", (rid,))
    if moved_to:
        conn.execute("UPDATE round_overrides SET round_id = ? WHERE round_id = ?"
                     " AND NOT EXISTS (SELECT 1 FROM round_overrides WHERE round_id = ?)", (moved_to, rid, moved_to))
        conn.execute("UPDATE events SET linked_round_id = ? WHERE linked_round_id = ?", (moved_to, rid))
    conn.execute("DELETE FROM rounds WHERE round_id = ?", (rid,))


# ----------------------------------------------------------------- merging
def merge_screenshot_round_into_export(conn: sqlite3.Connection, ss_round_id: str,
                                       export_round_id: str) -> dict[str, Any]:
    """Fold a screenshot-only round into the export (or manual) round that later turned up for it.

    The export's strokes win. Each extraction is re-validated against the export (R1/R2 now apply): one
    that still passes keeps its status and its stats move over; one that now fails goes back to review."""
    ss, target = _round(conn, ss_round_id), _round(conn, export_round_id)
    if ss is None or ss["source"] != "screenshot":
        raise ValueError(f"{ss_round_id} is not a screenshot round")
    if target is None or target["source"] == "screenshot":
        raise ValueError(f"{export_round_id} is not an export or manual round")
    xids = [r[0] for r in conn.execute(
        "SELECT extraction_id FROM extractions WHERE round_id = ? ORDER BY extraction_id", (ss_round_id,))]
    conn.execute("UPDATE extractions SET round_id = ? WHERE round_id = ?", (export_round_id, ss_round_id))
    _drop_round(conn, ss_round_id, moved_to=export_round_id)
    results = []
    for xid in xids:
        flags = _revalidate(conn, xid)
        status = _xrow(conn, xid)["status"]
        if status in APPLIED:
            if any(f["severity"] == "E" for f in V.active_flags(flags)):
                _set_status(conn, xid, "needs_review")
            else:
                apply_extraction(conn, xid)
        elif status in OPEN:
            _decide(conn, xid)
        results.append({"extraction_id": xid, "status": _xrow(conn, xid)["status"]})
    conn.commit()
    return {"screenshot_round_id": ss_round_id, "export_round_id": export_round_id, "extractions": results}


def reconcile_screenshot_rounds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Run after every export import. Merges screenshot rounds that an export (or manual) round now
    covers (same date + club, strokes agree), and re-matches extractions still waiting for review."""
    actions: list[dict[str, Any]] = []
    for ss in conn.execute("SELECT * FROM rounds WHERE source = 'screenshot' ORDER BY round_id").fetchall():
        raw = loads(ss["raw"], {})
        target = _match(conn, ss["played_on_local"], ss["club_id"], raw.get("course_text", ""),
                        V.reference_strokes(conn, ss["round_id"]), ss["gross"], sources=REFERENCE_SOURCES)
        if target:
            actions.append({"action": "merged", **merge_screenshot_round_into_export(conn, ss["round_id"], target)})
    pending = conn.execute("SELECT extraction_id, round_id FROM extractions"
                           " WHERE status IN ('extracted', 'needs_review') ORDER BY extraction_id").fetchall()
    for row in pending:
        if row["round_id"] and _round(conn, row["round_id"]) is not None:
            continue
        conn.execute("UPDATE extractions SET round_id = NULL WHERE extraction_id = ?", (row["extraction_id"],))
        new = _assign_round(conn, row["extraction_id"])
        _revalidate(conn, row["extraction_id"])
        status = _decide(conn, row["extraction_id"])
        if new != row["round_id"]:
            actions.append({"action": "rematched", "extraction_id": row["extraction_id"], "round_id": new,
                            "status": status})
    conn.commit()
    return actions


# ----------------------------------------------------------------- re-read
def _pick_image(ex: ExtractedRound, hole_row, field: str, n_images: int) -> int:
    """The single image most likely to show this cell: the right screen type covering the hole."""
    valid = [im for im in ex.images if 1 <= im.image_index <= n_images]
    covering = [im for im in valid if im.first_hole != -1 and im.first_hole <= hole_row.hole <= im.last_hole]
    for screen in PREFERRED_SCREENS.get(field, ()):
        hits = [im.image_index for im in covering if im.screen_type == screen]
        if hits:
            return next((i for i in hits if i in hole_row.source_images), hits[0])
    for i in list(hole_row.source_images) + [im.image_index for im in covering]:
        if 1 <= i <= n_images:
            return i
    return 1


def _cells(cells: Iterable[Any]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for c in cells:
        cell = (int(c["hole"]), str(c["field"])) if isinstance(c, Mapping) else (int(c[0]), str(c[1]))
        if cell not in out:
            out.append(cell)
    return out


def reread_cells(conn: sqlite3.Connection, cfg: Config, extraction_id: int, cells: Iterable[Any],
                 max_cells: int = 5) -> list[dict[str, Any]]:
    """Ask again about single cells, one call each, showing only the most relevant image and never the
    first reading. Agreement clears that cell's warnings; errors and disagreements stay for Shane.
    `cells`: [{hole, field}] or [(hole, field)]. Returns one result per cell asked."""
    row = _xrow(conn, extraction_id)
    ex, ctx = _parsed(row), _context(conn, row)
    paths = loads(row["image_paths"], [])
    player = ctx.get("player") or cfg.player_name
    results: list[dict[str, Any]] = []
    for hole, field in _cells(cells)[:max_cells]:
        hole_row = next((h for h in ex.holes if h.hole == hole), None)
        if hole_row is None or field not in V.HOLE_FIELDS:
            continue
        idx = _pick_image(ex, hole_row, field, len(paths))
        img = llm.load_image(image_path(cfg, paths[idx - 1]))
        question = (f"Player: {player}. Read hole {hole}, field \"{field}\": {FIELD_HELP[field]}. "
                    f"Return hole={hole} and field=\"{field}\".")
        original = V.cell_value(ex, hole, field)
        base = {"hole": hole, "field": field, "original": original, "image_index": idx}
        try:
            res = llm.structured_call(
                conn, purpose=REREAD_PURPOSE, system=reread_system_prompt(),
                content=llm.image_blocks([img]) + [{"type": "text", "text": question}], output_model=CellReread,
                prompt_version=REREAD_PROMPT_VERSION, schema_version=SCHEMA_VERSION,
                request_meta={"extraction_id": extraction_id, "hole": hole, "field": field, "image_sha256": img.sha256},
                model=cfg.model, effort=cfg.effort, max_tokens=min(cfg.max_tokens, REREAD_MAX_TOKENS),
            )
        except llm.LLMOutputError as e:
            results.append({**base, "value": "", "agrees": False, "error": str(e), "call_id": e.call_id,
                            "cost_usd": 0.0, "cached": False})
            continue
        rr = res.parsed
        results.append({**base, "value": rr.value, "confidence": rr.confidence, "note": rr.note,
                        "agrees": V.reread_agrees(hole, field, original, rr), "call_id": res.call_id,
                        "cost_usd": res.cost_usd, "cached": res.cached})

    flags = loads(_xrow(conn, extraction_id)["flags"], [])
    keep = ("value", "confidence", "note", "agrees", "original", "image_index", "call_id")
    rereads = {**V.rereads_of(flags),
               **{(r["hole"], r["field"]): {k: r[k] for k in keep if k in r} for r in results if "error" not in r}}
    conn.execute("UPDATE extractions SET flags = ? WHERE extraction_id = ?",
                 (dumps(V.apply_rereads(flags, rereads, ex)), extraction_id))
    _decide(conn, extraction_id)
    conn.commit()
    return results


# ------------------------------------------------------------------ review
def pending_round_reviews(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extractions waiting for Shane, oldest first."""
    out = []
    for row in conn.execute("SELECT * FROM extractions WHERE status IN ('extracted', 'needs_review')"
                            " ORDER BY created_at, extraction_id").fetchall():
        ex, act = _parsed(row), V.active_flags(loads(row["flags"], []))
        rr = _round(conn, row["round_id"])
        out.append({
            "extraction_id": row["extraction_id"],
            "status": row["status"],
            "round_id": row["round_id"],
            "round_source": rr["source"] if rr else None,
            "played_on": rr["played_on_local"] if rr else _played_on(conn, row, ex, _context(conn, row)),
            "course_text": ex.course_text,
            "n_images": len(loads(row["image_paths"], [])),
            "n_errors": sum(f["severity"] == "E" for f in act),
            "n_warnings": sum(f["severity"] == "W" for f in act),
            "created_at": row["created_at"],
        })
    return out


def _display(field: str, value: Any) -> Any:
    return None if field in V.INT_FIELDS and value < 0 else value


def get_review(conn: sqlite3.Connection, extraction_id: int) -> dict[str, Any]:
    """Everything a review page needs for one extraction. Images are paths relative to data_dir
    (image_path(cfg, p) resolves them); integer sentinels (-1) are shown as None."""
    row = _xrow(conn, extraction_id)
    ex, ctx = _parsed(row), _context(conn, row)
    flags = loads(row["flags"], [])
    rr = _round(conn, row["round_id"])
    reference = dict(rr) if rr is not None and rr["source"] != "screenshot" else None
    ref_strokes = V.reference_strokes(conn, rr["round_id"]) if reference else {}
    holes = []
    for h in ex.holes:
        cell = {f: _display(f, getattr(h, f)) for f in V.HOLE_FIELDS}
        holes.append({"hole": h.hole, **cell, "par_alt": _display("par", h.par_alt), "si_alt": _display("si", h.si_alt),
                      "confidence": h.confidence, "uncertain_fields": list(h.uncertain_fields),
                      "stats_visible": h.stats_visible, "source_images": list(h.source_images),
                      "reference_strokes": ref_strokes.get(h.hole)})
    corrections = [dict(r) for r in conn.execute(
        "SELECT entity, entity_id, field, model_value, corrected_value, corrected_at FROM corrections"
        " WHERE extraction_id = ? ORDER BY id", (extraction_id,))]
    return {
        "extraction_id": extraction_id,
        "status": row["status"],
        "round_id": row["round_id"],
        "round_source": rr["source"] if rr else ("screenshot (created on accept)" if row["round_id"] else None),
        "played_on": rr["played_on_local"] if rr else _played_on(conn, row, ex, ctx),
        "image_paths": loads(row["image_paths"], []),
        "images": [im.model_dump() for im in ex.images],
        "header": {k: getattr(ex, k) for k in ("player_name", "course_text", "tee_text", "date_text", "date_iso",
                                               "course_par", "rating_text", "overall_confidence", "issues")},
        "displayed": ex.displayed.model_dump(),
        "fields": list(V.HOLE_FIELDS),
        "choices": {f: list(v) for f, v in V.FIELD_CHOICES.items()},
        "holes": holes,
        "flags": V.active_flags(flags),
        "cleared_flags": [f for f in flags if f.get("cleared")],
        "flags_by_cell": V.flags_by_cell(flags),
        "corrections": corrections,
        "reference": reference and {k: reference[k] for k in (
            "round_id", "source", "played_on_local", "gross", "to_par", "putts", "fw_hit", "gir", "entry_mode")},
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
    }


def _coerce(field: str, value: Any) -> Any:
    """A reviewer's value in ExtractedRound form (sentinels for empty)."""
    empty = value is None or (isinstance(value, str) and value.strip() in ("", "-"))
    if field in V.INT_FIELDS or field == "course_par":
        if empty:
            return -1
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
            raise ValueError(f"{field} must be a whole number, got {value!r}")
        return int(str(value).strip())
    if field in ("course_text", "tee_text"):
        return "" if value is None else str(value).strip()
    if field == "date_iso":
        if empty:
            return ""
        if not _valid_date(str(value).strip()):
            raise ValueError(f"date_iso must be YYYY-MM-DD, got {value!r}")
        return str(value).strip()
    if empty:
        return EMPTY_VALUE[field]
    if field == "gir" and isinstance(value, (bool, int)):
        return "hit" if value else "miss"
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s not in V.FIELD_CHOICES[field]:
        raise ValueError(f"{field} must be one of {', '.join(V.FIELD_CHOICES[field])}; got {value!r}")
    return s


def _blank_hole(hole: int) -> dict[str, Any]:
    return {"hole": hole, "par": -1, "par_alt": -1, "si": -1, "si_alt": -1, "yards": -1, "strokes": -1,
            "symbol": "not_visible", "putts": -1, "penalties": -1, "chips": -1, "sand": -1, "fairway": "not_visible",
            "gir": "not_visible", "gir_miss": "not_visible", "stats_visible": False, "confidence": "high",
            "uncertain_fields": [], "source_images": []}


def _record_correction(conn: sqlite3.Connection, row: sqlite3.Row, entity: str, entity_id: str, field: str,
                       model_value: Any, corrected: Any) -> bool:
    """One row per corrected cell (updated if corrected again); nothing if it matches the model."""
    old = conn.execute("SELECT id FROM corrections WHERE extraction_id = ? AND entity_id = ? AND field = ?",
                       (row["extraction_id"], entity_id, field)).fetchone()
    mv = None if model_value is None else str(model_value)
    if old:
        conn.execute("UPDATE corrections SET corrected_value = ?, corrected_at = ? WHERE id = ?",
                     (str(corrected), now_iso(), old[0]))
        return True
    if mv == str(corrected):
        return False
    conn.execute("INSERT INTO corrections(entity, entity_id, field, model_value, corrected_value, extraction_id,"
                 " call_id, corrected_at) VALUES (?,?,?,?,?,?,?,?)",
                 (entity, entity_id, field, mv, str(corrected), row["extraction_id"], row["call_id"], now_iso()))
    return True


def apply_corrections(conn: sqlite3.Connection, extraction_id: int,
                      corrections: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply a reviewer's cell edits: [{hole, field, value}] (hole None for round-level date_iso,
    course_text, tee_text, course_par). Every edit that differs from the model's own answer is kept in
    `corrections` (the golden set); edited cells count as verified. Then re-validate: clean (no E, at
    most one W) means accepted, otherwise it stays in review."""
    row = _xrow(conn, extraction_id)
    data = _parsed(row).model_dump()
    model = _model_output(conn, row)
    model_holes = {h.hole: h for h in model.holes}
    holes = {h["hole"]: h for h in data["holes"]}
    recorded, identity_changed = 0, False
    for c in corrections:
        hole, field = c.get("hole"), c["field"]
        if hole is None:
            if field not in ROUND_FIELDS:
                raise ValueError(f"Round-level corrections can change {', '.join(ROUND_FIELDS)}; not {field!r}")
            value = _coerce(field, c.get("value"))
            data[field] = value
            identity_changed |= field in ("date_iso", "course_text")
            recorded += _record_correction(conn, row, "round", str(extraction_id), field, getattr(model, field), value)
            continue
        if field not in V.HOLE_FIELDS:
            raise ValueError(f"Unknown hole field {field!r}; expected one of {', '.join(V.HOLE_FIELDS)}")
        try:
            hole = int(hole)
        except (TypeError, ValueError):
            raise ValueError(f"Hole must be a number from 1 to 18, got {hole!r}") from None
        if not 1 <= hole <= 18:
            raise ValueError(f"Hole must be 1-18, got {hole}.")
        value = _coerce(field, c.get("value"))
        h = holes.setdefault(hole, _blank_hole(hole))
        h[field] = value
        h["uncertain_fields"] = [f for f in h["uncertain_fields"] if f != field]
        if field in V.STATS_FIELDS:
            h["stats_visible"] = True
        mh = model_holes.get(hole)
        recorded += _record_correction(conn, row, "round_hole", f"{extraction_id}:{hole}", field,
                                       getattr(mh, field) if mh else None, value)
    data["holes"] = [holes[k] for k in sorted(holes)]
    conn.execute("UPDATE extractions SET result_json = ? WHERE extraction_id = ?",
                 (dumps(ExtractedRound.model_validate(data).model_dump()), extraction_id))
    if identity_changed:
        _rematch(conn, extraction_id)
    _revalidate(conn, extraction_id)
    _decide(conn, extraction_id, human=True)
    conn.commit()
    return {**_outcome(conn, extraction_id, cost=0.0, cached=False), "corrections_recorded": recorded}


def _rematch(conn: sqlite3.Connection, extraction_id: int) -> str | None:
    """A new date or course means a possibly different round: take the extraction's data back out of the
    round it was applied to, forget that match, and match again (the corrected date now wins)."""
    row = _xrow(conn, extraction_id)
    if row["status"] in APPLIED:
        _unapply(conn, extraction_id)
        conn.execute("UPDATE extractions SET status = 'needs_review', reviewed_at = NULL WHERE extraction_id = ?",
                     (extraction_id,))
    conn.execute("UPDATE extractions SET round_id = NULL WHERE extraction_id = ?", (extraction_id,))
    return _assign_round(conn, extraction_id)


def accept_extraction(conn: sqlite3.Connection, extraction_id: int, *, force: bool = False) -> dict[str, Any]:
    """A reviewer accepts the round as it stands (warnings allowed). Errors block unless `force`,
    e.g. to keep a genuinely different round that M1 thought was a duplicate."""
    act = V.active_flags(loads(_xrow(conn, extraction_id)["flags"], []))
    errors = [f for f in act if f["severity"] == "E"]
    if errors and not force:
        raise ValueError(f"{len(errors)} error flag(s) remain (e.g. {errors[0]['message']}); "
                         "correct them or pass force=True")
    if _xrow(conn, extraction_id)["round_id"] is None and _assign_round(conn, extraction_id) is None:
        raise ValueError("No played date: correct date_iso before accepting.")
    _set_status(conn, extraction_id, "accepted")
    return _outcome(conn, extraction_id, cost=0.0, cached=False)


def reject_extraction(conn: sqlite3.Connection, extraction_id: int) -> dict[str, Any]:
    """Discard the extraction; anything it had written to a round is taken back out."""
    _set_status(conn, extraction_id, "rejected")
    return _outcome(conn, extraction_id, cost=0.0, cached=False)
