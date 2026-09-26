"""Screenshot inbox: data/inbox/screenshots -> one extraction per round -> data/raw/screenshots/<date>/.

A subfolder is one round (the reliable way to say which screens belong together); a folder name that
starts with YYYY-MM-DD also gives the played date. Loose files are grouped by capture time, because a
round's screens are captured together while different rounds are hours or days apart. Capture time is
only used for grouping, never as the date of play.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import llm
from ..config import Config
from ..db import loads
from . import rounds

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".gif"}
GROUP_GAP = timedelta(minutes=20)
_EXIF_IFD, _DATETIME_ORIGINAL, _DATETIME = 0x8769, 0x9003, 0x0132
_XMP_DATE = re.compile(r"(?:DateTimeOriginal|DateCreated|CreateDate)(?:>|=\")\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def inbox_dir(cfg: Config) -> Path:
    return cfg.inbox_dir / "screenshots"


def archive_dir(cfg: Config) -> Path:
    return cfg.raw_dir / "screenshots"


def rejected_dir(cfg: Config) -> Path:
    """Where unreadable files go, so one bad file never blocks the rest of the inbox."""
    return cfg.inbox_dir / "rejected"


def _is_image(p: Path) -> bool:
    return p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTS


def capture_time(path: Path, cfg: Config) -> datetime:
    """Local, naive capture time: EXIF DateTimeOriginal, then XMP (iOS PNG screenshots), then mtime."""
    try:
        from PIL import Image
        import pillow_heif

        pillow_heif.register_heif_opener()
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
            if raw:
                return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
            xmp = im.info.get("XML:com.adobe.xmp") or im.info.get("xmp") or ""
            if isinstance(xmp, bytes):
                xmp = xmp.decode("utf-8", "ignore")
            m = _XMP_DATE.search(xmp)
            if m:
                return datetime.fromisoformat(m.group(1))
    except Exception:  # unreadable metadata is normal; fall back to the file clock
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo(cfg.timezone)).replace(tzinfo=None)


def _folder_date(name: str) -> str | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    if not m:
        return None
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
        return m.group(1)
    except ValueError:
        return None


def group_inbox(cfg: Config) -> list[dict[str, Any]]:
    """Groups of screenshots, one per round: [{key, kind: 'folder'|'loose', paths, captured_at, played_on}].
    Paths within a group are in capture order (that is the "Image N" order the model sees)."""
    root = inbox_dir(cfg)
    if not root.is_dir():
        return []
    groups: list[dict[str, Any]] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        timed = sorted((capture_time(p, cfg), p.name, p) for p in folder.rglob("*") if _is_image(p))
        if timed:
            groups.append({"key": folder.name, "kind": "folder", "paths": [p for _, _, p in timed],
                           "captured_at": timed[0][0].isoformat(timespec="seconds"),
                           "played_on": _folder_date(folder.name)})
    loose = sorted((capture_time(p, cfg), p.name, p) for p in root.iterdir() if _is_image(p))
    run: list[tuple[datetime, str, Path]] = []
    for item in loose:
        if run and item[0] - run[-1][0] > GROUP_GAP:
            groups.append(_loose_group(run))
            run = []
        run.append(item)
    if run:
        groups.append(_loose_group(run))
    return groups


def _loose_group(run: list[tuple[datetime, str, Path]]) -> dict[str, Any]:
    first = run[0][0]
    return {"key": f"loose-{first:%Y%m%d-%H%M}", "kind": "loose", "paths": [p for _, _, p in run],
            "captured_at": first.isoformat(timespec="seconds"), "played_on": None}


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in range(2, 10_000):
        cand = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(dest)


def _move(paths: list[Path], dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in paths:
        target = _unique(dest_dir / p.name)
        shutil.move(str(p), str(target))
        moved.append(target)
    return moved


def _dest_name(conn: sqlite3.Connection, extraction_id: int, group: dict[str, Any]) -> str:
    """The played date when it is known (the round's, else the date read off the screens), else the group key."""
    row = conn.execute("SELECT round_id, result_json FROM extractions WHERE extraction_id = ?",
                       (extraction_id,)).fetchone()
    played = conn.execute("SELECT played_on_local FROM rounds WHERE round_id = ?", (row["round_id"],)).fetchone()
    if played:
        return played[0]
    date_iso = loads(row["result_json"], {}).get("date_iso", "")
    return group.get("played_on") or (date_iso if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso or "") else group["key"])


def _remove_empty_dirs(folder: Path, stop: Path) -> None:
    for d in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True) + [folder]:
        if d != stop and d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def process_inbox(conn: sqlite3.Connection, cfg: Config, *, dry_run: bool = False,
                  reread: bool = True) -> list[dict[str, Any]]:
    """Extract each inbox group once and archive its files under data/raw/screenshots/<date or group>/.

    A group whose exact image set already has an extraction is not sent again; its files are archived
    next to the round. A failed extraction leaves its files in the inbox. With `dry_run`, nothing is
    called or moved: the result says what would happen. LLMUnavailable (no API key, or the API is
    unreachable) stops the run; any other failure is reported for its group and the run carries on."""
    results: list[dict[str, Any]] = []
    for group in group_inbox(cfg):
        if dry_run:
            existing = rounds.find_extraction(conn, [rounds.file_sha256(p) for p in group["paths"]])
            results.append({**_entry(group), "action": "skip" if existing is not None else "extract",
                            "extraction_id": existing})
            continue
        results.append(process_group(conn, cfg, group, reread=reread))
    return results


def _entry(group: dict[str, Any]) -> dict[str, Any]:
    return {"key": group["key"], "kind": group["kind"], "n_images": len(group["paths"]),
            "paths": [str(p) for p in group["paths"]], "played_on": group["played_on"]}


def process_group(conn: sqlite3.Connection, cfg: Config, group: dict[str, Any], *, reread: bool = True,
                  club_id: str | None = None, expected_holes: int | None = None) -> dict[str, Any]:
    """One inbox group (a group_inbox() item): extract it unless this exact image set was extracted
    before, then archive its files. Split out of process_inbox so the web upload can process just
    its own round and pass the course hint Shane picked."""
    entry = _entry(group)
    bad = rounds.unreadable_images(group["paths"])
    if bad:
        moved = _move([p for p, _ in bad], rejected_dir(cfg) / group["key"])
        if group["kind"] == "folder":
            _remove_empty_dirs(inbox_dir(cfg) / group["key"], inbox_dir(cfg))
        names = ", ".join(p.name for p, _ in bad)
        return {**entry, "action": "error", "rejected": [str(p) for p in moved],
                "error": f"{names}: not a readable image ({bad[0][1]}); moved to "
                         f"{rejected_dir(cfg).relative_to(cfg.data_dir)}/{group['key']}/. The other screenshots "
                         "of this round stay in the inbox: add a good copy and process again."}
    existing = rounds.find_extraction(conn, [rounds.file_sha256(p) for p in group["paths"]])
    if existing is not None:
        outcome, action = None, "skipped"
        xid = existing
    else:
        try:
            outcome = rounds.extract_round(conn, cfg, group["paths"], played_on=group["played_on"], reread=reread,
                                           club_id=club_id, expected_holes=expected_holes)
        except llm.LLMUnavailable:
            raise                     # no key / API down: every other group would fail the same way
        except llm.LLMOutputError as e:
            return {**entry, "action": "error", "error": str(e)}
        except Exception as e:        # one broken group must not stop the others
            conn.rollback()
            return {**entry, "action": "error", "error": f"{type(e).__name__}: {e}"}
        action, xid = "extracted", outcome["extraction_id"]
    old_rel = [rounds.rel_path(cfg, p) for p in group["paths"]]
    moved = _move(group["paths"], archive_dir(cfg) / _dest_name(conn, xid, group))
    stored = loads(conn.execute("SELECT image_paths FROM extractions WHERE extraction_id = ?",
                                (xid,)).fetchone()[0], [])
    if stored == old_rel:  # the extraction points at these very files: follow them
        rounds.relocate_images(conn, cfg, xid, moved)
    if group["kind"] == "folder":
        _remove_empty_dirs(inbox_dir(cfg) / group["key"], inbox_dir(cfg))
    return {**entry, "action": action, "extraction_id": xid, "outcome": outcome, "moved_to": [str(p) for p in moved]}
