"""Paper-notebook pages -> timeline, via Claude vision (prompt nb-1, schema NotebookPage).

Each photographed page is transcribed faithfully first ([illegible] markers), then events are
extracted from that transcription, with line numbers referring to it. The photo's capture time is NOT
when the page was written, so the date written on the page drives resolution; a page with no date
anywhere gets 'inferred' dates and a review flag. Per DECISIONS.md every notebook event starts as
'pending' and is never auto-accepted.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import golf.llm as llm
from golf.config import Config
from golf.db import dumps, now_iso, upsert
from golf.schemas.events import SCHEMA_VERSION, NotebookPage

from .dates import Resolved, month_bounds
from .entities import Entities, load_entities
from .extract import build_events, content_hash, extraction_signature, load_prompt, normalize_text
from .reconcile import flag_duplicates, flag_list, make_flag, reconcile_doc

PROMPT_VERSION = "nb-1"
PURPOSE = "notebook_page"
MAX_LONG_EDGE = 2600          # enough for handwriting; keeps a phone photo well under the API image limits
PAGE_SPAN_DAYS = 31           # a page is assumed to be filled within about a month of its written date
_YEAR = re.compile(r"\b(19|20)\d{2}\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b|'\d{2}\b")


def capture_time(path: Path, tz: str) -> datetime:
    """EXIF DateTimeOriginal, else EXIF DateTime, else the file's mtime (local)."""
    from PIL import Image

    zone = ZoneInfo(tz)
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=zone)
    except Exception:
        pass
    return datetime.fromtimestamp(Path(path).stat().st_mtime, zone).replace(microsecond=0)


def parse_page_date(text: str, capture: datetime) -> Resolved | None:
    """The date written on the page, with its precision (day / month / year). A page date without a
    year borrows the photo's year and is flagged, because the photo may be years later."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from dateparser.date import DateDataParser
        from dateparser.search import search_dates

        settings = {"PREFER_DATES_FROM": "past", "RELATIVE_BASE": capture.replace(tzinfo=None),
                    "DATE_ORDER": "MDY", "RETURN_AS_TIMEZONE_AWARE": False, "PREFER_DAY_OF_MONTH": "first"}
        data = DateDataParser(languages=["en"], settings=settings).get_date_data(text)
        found, period = (data.date_obj, data.period) if data and data.date_obj else (None, "day")
        if found is None:
            hits = search_dates(text, languages=["en"], settings=settings) or []
            found = hits[0][1] if hits else None
    except Exception:
        return None
    if found is None:
        return None
    d = found.date()
    flags = () if _YEAR.search(text) else ("year_inferred", "created_unreliable")
    why = f"page date '{text}'"
    if period == "month":
        s, e = month_bounds(d.year, d.month)
        return Resolved(s + (e - s) // 2, s, e, "month", "entry_header", f"{why} -> {s:%Y-%m}", flags)
    if period == "year":
        s, e = d.replace(month=1, day=1), d.replace(month=12, day=31)
        return Resolved(s + (e - s) // 2, s, e, "inferred", "entry_header", f"{why} -> {d.year}", flags + ("coarse_period",))
    return Resolved(d, d, d, "day", "entry_header", f"{why} -> {d} ({d:%a})", flags)


def page_message(capture: datetime, ents: Entities, tz: str) -> str:
    return (f"<page_metadata>\nphoto taken: {capture:%Y-%m-%d (%A) %H:%M} {tz} "
            "(when the photo was taken, NOT when the page was written)\n</page_metadata>\n"
            f"{ents.prompt_context()}\n"
            "Transcribe the page in Image 1 exactly, then extract the golf events from your transcription.")


def _external_id(path: Path, cfg: Config) -> str:
    try:
        return str(path.resolve().relative_to(Path(cfg.data_dir).resolve()))
    except ValueError:
        return str(path.resolve())


def _record_import(conn: sqlite3.Connection, path: str, sha: str, summary: dict[str, Any]) -> None:
    row = conn.execute("SELECT import_id FROM imports WHERE kind = 'notebook' AND sha256 = ?", (sha,)).fetchone()
    if row:
        conn.execute("UPDATE imports SET summary = ?, imported_at = ? WHERE import_id = ?",
                     (dumps(summary), now_iso(), row[0]))
    else:
        conn.execute("INSERT INTO imports(kind, path, sha256, imported_at, summary) VALUES ('notebook', ?, ?, ?, ?)",
                     (path, sha, now_iso(), dumps(summary)))


def import_notebook_pages(conn: sqlite3.Connection, cfg: Config, image_paths: Iterable[Path | str], *,
                          force: bool = False) -> dict[str, Any]:
    """Transcribe + extract each page photo. Re-importing an unchanged page (same image bytes, prompt,
    model and entities) is free; force=True re-reads it (re-bills). Returns a summary with per-page
    doc ids; failures are listed in summary['errors'] and do not stop the other pages."""
    ents = load_entities(cfg)
    system = load_prompt("notebook_nb1.md")
    tz = cfg.timezone
    summary: dict[str, Any] = {
        "pages": 0, "new": 0, "reread": 0, "unchanged": 0, "doc_ids": [], "api_calls": 0, "cost_usd": 0.0,
        "events": dict.fromkeys(("new", "updated", "unchanged", "restored", "orphaned", "superseded", "pending"), 0),
        "errors": [], "llm_unavailable": None,
    }
    new_ids: list[str] = []
    for p in map(Path, image_paths):
        summary["pages"] += 1
        try:
            img = llm.load_image(p, max_long_edge=MAX_LONG_EDGE)
        except Exception as e:
            summary["errors"].append({"path": str(p), "error": f"could not read image: {e}"})
            continue
        doc_id = "nb-" + img.sha256[:16]
        summary["doc_ids"].append(doc_id)
        prev = conn.execute("SELECT * FROM source_docs WHERE doc_id = ?", (doc_id,)).fetchone()
        signature = extraction_signature(cfg, img.sha256, ents, system, PROMPT_VERSION)
        if prev is not None and prev["extracted_key"] == signature and not force:
            summary["unchanged"] += 1
            continue
        capture = capture_time(p, tz)
        try:
            res = llm.structured_call(
                conn, purpose=PURPOSE, system=system,
                content=llm.image_blocks([img]) + [{"type": "text", "text": page_message(capture, ents, tz)}],
                output_model=NotebookPage, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
                request_meta={"doc_id": doc_id, "image_sha256": img.sha256, "entities_hash": ents.hash},
                model=cfg.model, effort=cfg.effort, cache_extra=ents.hash, force=force)
        except llm.LLMUnavailable as e:
            summary["llm_unavailable"] = str(e)
            summary["errors"].append({"path": str(p), "error": str(e)})
            break
        except llm.LLMOutputError as e:
            summary["errors"].append({"path": str(p), "error": str(e)})
            continue
        summary["api_calls"] += 0 if res.cached else 1
        summary["cost_usd"] = round(summary["cost_usd"] + res.cost_usd, 6)
        page: NotebookPage = res.parsed
        text = normalize_text(page.transcription, keep_lines=True)
        doc_date = parse_page_date(page.page_date_text, capture)
        doc_flags = [make_flag("created_unreliable", "photo time is not when the page was written")]
        if page.legibility == "low":
            doc_flags.append(make_flag("low_legibility"))
        doc = {
            "doc_id": doc_id, "source": "notebook", "external_id": _external_id(p, cfg),
            "title": f"Notebook page {page.page_date_text.strip() or p.stem}", "folder": None,
            "created_at": capture.isoformat(), "modified_at": capture.isoformat(), "text": text,
            "content_hash": content_hash(text), "status": "active", "flags": dumps(doc_flags), "deleted_at": None,
        }
        upsert(conn, "source_docs", doc, ["doc_id"])
        if doc_date is not None:
            created = datetime.combine(doc_date.date_start, time(12), ZoneInfo(tz))
            modified = datetime.combine(doc_date.date_end + timedelta(days=PAGE_SPAN_DAYS), time(12), ZoneInfo(tz))
            unreliable = False
        else:
            created = modified = capture
            unreliable = True
        extra = [make_flag("low_legibility")] if page.legibility == "low" else []
        rows = build_events(doc, page, ents, call_id=res.call_id, created=created, modified=modified,
                            created_unreliable=unreliable, doc_date=doc_date, extra_flags=extra)
        for row in rows:
            if row["date_source"] == "note_created":
                row["flags"] = dumps(flag_list(row["flags"]) + [make_flag("undated_page")])
        counts = reconcile_doc(conn, doc_id, rows, source="notebook")
        conn.execute("UPDATE source_docs SET extracted_key = ? WHERE doc_id = ?", (signature, doc_id))
        new_ids += counts.pop("new_ids")
        for k in summary["events"]:
            summary["events"][k] += counts.get(k, 0)
        summary["new" if prev is None else "reread"] += 1
        _record_import(conn, doc["external_id"], img.sha256, {"doc_id": doc_id, "events": counts})
        conn.commit()
    if new_ids:
        flag_duplicates(conn, new_ids=new_ids)
    return summary
