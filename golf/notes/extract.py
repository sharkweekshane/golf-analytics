"""Note -> timeline events: the tl-1 extraction call and the step from model output to event rows.

Shared by the Apple Notes sync, notebook pages and `golf add`. The model DESCRIBES (NoteExtraction /
NotebookPage); Python resolves dates (golf.notes.dates), verifies excerpts and reconciles
(golf.notes.reconcile). Nothing here ever sends note text anywhere but the Anthropic API.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import golf.llm as llm
from golf.config import Config
from golf.db import dumps
from golf.schemas.events import SCHEMA_VERSION, NoteExtraction, NotebookPage

from .dates import Resolved, resolve, resolve_headers
from .entities import Entities, load_entities
from .reconcile import clean_excerpt, event_id, flag_list, make_flag, norm_excerpt, reconcile_doc, verify_excerpt

PROMPT_VERSION = "tl-1"
PURPOSE = "note"
PROMPTS_DIR = Path(__file__).with_name("prompts")


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


# ------------------------------------------------------------------ text
def normalize_text(text: str, *, keep_lines: bool = False) -> str:
    """NFC, unified newlines, attachment placeholders removed, trailing spaces stripped. keep_lines
    preserves the line count (notebook transcriptions: the model's line numbers must stay valid)."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    t = t.replace("￼", "")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    if keep_lines:
        return t.rstrip()
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def numbered(text: str) -> str:
    return "\n".join(f"L{i:03d}: {line}" for i, line in enumerate((text or "").split("\n"), start=1))


def parse_local(iso: str | None, tz: str) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def has_flag(flags_json: str | None, code: str) -> bool:
    return any(f.get("code") == code for f in flag_list(flags_json))


# ------------------------------------------------------------------ request
def _when(dt: datetime, tz: str) -> str:
    return f"{dt:%Y-%m-%d (%A) %H:%M} {tz}"


def note_message(doc: Mapping[str, Any], ents: Entities, cfg: Config) -> str:
    created = parse_local(doc["created_at"], cfg.timezone)
    modified = parse_local(doc["modified_at"], cfg.timezone) or created
    if doc["source"] == "manual":
        meta = (f"source: quick entry typed by Shane (golf add)\n"
                f"entry date: {created:%Y-%m-%d (%A)} -- relative words are relative to this date")
    else:
        meta = (f"doc_id: {doc['doc_id']}\ntitle: {doc['title'] or ''}\nfolder: {doc['folder'] or ''}\n"
                f"created: {_when(created, cfg.timezone)}\nmodified: {_when(modified, cfg.timezone)}")
    return (f"<note_metadata>\n{meta}\n</note_metadata>\n{ents.prompt_context()}\n"
            f"<note>\n{numbered(doc['text'] or '')}\n</note>")


def extraction_signature(cfg: Config, text_hash: str, ents: Entities, system: str,
                         prompt_version: str = PROMPT_VERSION) -> str:
    """What the events of a doc were extracted from. Stored in source_docs.extracted_key; a sync
    re-extracts only when it differs (text, prompt, schema, model or entities changed). Deliberately
    excludes modified_at, so pinning or moving a note never re-bills."""
    blob = dumps({"text": text_hash, "prompt_version": prompt_version, "schema_version": SCHEMA_VERSION,
                  "model": cfg.model, "effort": cfg.effort, "entities": ents.hash,
                  "system": hashlib.sha256(system.encode()).hexdigest()})
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# ------------------------------------------------------------------ model output -> rows
def _none_if(value: Any, *sentinels: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
    return None if value in sentinels or value in ("", None) else value


def build_events(doc: Mapping[str, Any], extr: NoteExtraction | NotebookPage, ents: Entities, *, call_id: int | None,
                 created: datetime, modified: datetime, created_unreliable: bool = False,
                 doc_date: Resolved | None = None, extra_flags: Iterable[dict] = ()) -> list[dict[str, Any]]:
    """Event rows (DB-ready values) for one extraction: dates resolved, excerpts verified, ids assigned."""
    text = doc["text"] or ""
    n_lines = len(text.split("\n"))
    extra = list(extra_flags)
    headers = resolve_headers(extr.entry_headers, extr.log_order, created, modified,
                              created_unreliable=created_unreliable, doc_date=doc_date)
    header_expr = {h.line: h.date.expression.strip() for h in extr.entry_headers}
    ordinals: Counter = Counter()
    rows = []
    for ev in extr.events:
        excerpt = clean_excerpt(ev.source.excerpt)
        v = verify_excerpt(text, excerpt)
        if v.verified:
            line_start, line_end = v.line_start, v.line_end
        else:
            ok = 1 <= ev.source.line_start <= n_lines
            line_start = ev.source.line_start if ok else None
            line_end = max(ev.source.line_end, ev.source.line_start) if ok and ev.source.line_end <= n_lines else line_start
        r = resolve(ev.date, created, modified, headers=headers, is_planned=ev.is_planned, note_kind=extr.note_kind,
                    log_order=extr.log_order, event_line=line_start or -1, created_unreliable=created_unreliable,
                    doc_date=doc_date)
        flags = [make_flag(code) for code in r.flags]
        if not v.verified:
            flags.append(make_flag("excerpt_unverified"))
        if "[illegible]" in excerpt.lower():
            flags.append(make_flag("illegible_text"))
        raw_coach = _none_if(ev.coach, "none", "null")
        coach = ents.canonical_coach(raw_coach) or raw_coach
        if raw_coach and ents.coaches and not ents.canonical_coach(raw_coach):
            flags.append(make_flag("unknown_coach", f"coach '{raw_coach}' is not in entities.yaml"))
        raw_loc = _none_if(ev.location, "none", "null")
        location = ents.canonical_location(raw_loc) or raw_loc
        location_kind = _none_if(ev.location_kind, "none") or ents.location_kind(location)
        if len(ev.injury) > 1:
            flags.append(make_flag("multiple_injuries"))
        for reason in ev.review_reasons:
            if reason.strip():
                flags.append(make_flag("model_review", reason.strip()))
        flags.extend(extra)
        key = (ev.event_type, norm_excerpt(excerpt))
        ordinal = ordinals[key]
        ordinals[key] += 1
        expression = ev.date.expression.strip() or (header_expr.get(ev.date.header_line, "") if ev.date.kind == "header" else "")
        areas = sorted({f.game_area for f in ev.focus_areas})
        rows.append({
            "event_id": event_id(doc["doc_id"], ev.event_type, excerpt, ordinal),
            "call_id": call_id,
            "event_type": ev.event_type,
            "is_planned": int(ev.is_planned),
            "date": r.date.isoformat() if r.date else None,
            "date_start": r.date_start.isoformat() if r.date_start else None,
            "date_end": r.date_end.isoformat() if r.date_end else None,
            "date_precision": r.precision,
            "date_source": r.source,
            "date_expression": expression or None,
            "date_reasoning": r.reasoning,
            "coach": coach,
            "lesson_format": _none_if(ev.lesson_format, "none"),
            "location": location,
            "location_kind": location_kind,
            "duration_minutes": ev.duration_minutes if ev.duration_minutes > 0 else None,
            "focus_areas": dumps([f.model_dump() for f in ev.focus_areas]),
            "game_areas": ",".join(areas) or None,
            "drills": dumps([d.model_dump() for d in ev.drills]),
            "swing_thoughts": dumps([s.strip() for s in ev.swing_thoughts if s.strip()]),
            "equipment": dumps([e.model_dump() for e in ev.equipment]),
            "measurements": dumps([m.model_dump() for m in ev.measurements]),
            "injury": dumps(ev.injury[0].model_dump()) if ev.injury else None,
            "goal_text": _none_if(ev.goal_text),
            "summary": _none_if(ev.summary),
            "source_excerpt": excerpt,
            "line_start": line_start,
            "line_end": line_end,
            "excerpt_verified": int(v.verified),
            "confidence": ev.confidence,
            "flags": dumps(flags),
        })
    return rows


def manual_doc_date(doc: Mapping[str, Any], tz: str) -> Resolved:
    d = parse_local(doc["created_at"], tz).date()
    return Resolved(d, d, d, "day", "manual", f"entry date {d} ({d:%a}, golf add)")


def apply_extraction(conn: sqlite3.Connection, cfg: Config, doc: Mapping[str, Any], extr: NoteExtraction, *,
                     call_id: int | None, ents: Entities, signature: str) -> dict[str, Any]:
    created = parse_local(doc["created_at"], cfg.timezone)
    modified = parse_local(doc["modified_at"], cfg.timezone) or created
    doc_date = manual_doc_date(doc, cfg.timezone) if doc["source"] == "manual" else None
    rows = build_events(doc, extr, ents, call_id=call_id, created=created, modified=modified,
                        created_unreliable=has_flag(doc["flags"], "created_unreliable"), doc_date=doc_date)
    counts = reconcile_doc(conn, doc["doc_id"], rows, source=doc["source"])
    flags = [f for f in flag_list(doc["flags"]) if f.get("code") != "extraction_failed"]
    conn.execute("UPDATE source_docs SET extracted_key = ?, flags = ? WHERE doc_id = ?",
                 (signature, dumps(flags), doc["doc_id"]))
    conn.commit()
    return counts


def extract_doc(conn: sqlite3.Connection, cfg: Config, doc_id: str, *, force: bool = False,
                entities: Entities | None = None) -> dict[str, Any]:
    """Extract (or replay from the llm_calls cache) one Apple Notes / manual doc and reconcile its events.

    Returns {doc_id, api_calls (0 when served from cache), cost_usd, note_kind, events: reconcile counts}.
    Raises LLMUnavailable (no key) and LLMOutputError (refusal/truncation; recorded on the doc so the
    next sync does not re-bill the same failing request)."""
    doc = conn.execute("SELECT * FROM source_docs WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise KeyError(f"no source doc {doc_id}")
    if doc["source"] == "notebook":
        raise ValueError("notebook pages are re-read with import_notebook_pages(..., force=True)")
    ents = entities or load_entities(cfg)
    system = load_prompt("notes_tl1.md")
    signature = extraction_signature(cfg, doc["content_hash"] or content_hash(doc["text"] or ""), ents, system)
    out: dict[str, Any] = {"doc_id": doc_id, "api_calls": 0, "cost_usd": 0.0, "note_kind": None}
    if not (doc["text"] or "").strip():
        out["events"] = reconcile_doc(conn, doc_id, [], source=doc["source"])
        conn.execute("UPDATE source_docs SET extracted_key = ? WHERE doc_id = ?", (signature, doc_id))
        conn.commit()
        return out
    try:
        res = llm.structured_call(
            conn, purpose=PURPOSE, system=system,
            content=[{"type": "text", "text": note_message(doc, ents, cfg)}],
            output_model=NoteExtraction, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            request_meta={"doc_id": doc_id, "content_hash": doc["content_hash"], "entities_hash": ents.hash},
            model=cfg.model, effort=cfg.effort, cache_extra=ents.hash, force=force)
    except llm.LLMOutputError as e:
        flags = [f for f in flag_list(doc["flags"]) if f.get("code") != "extraction_failed"]
        flags.append(make_flag("extraction_failed", str(e)[:300], signature=signature, call_id=e.call_id))
        conn.execute("UPDATE source_docs SET flags = ? WHERE doc_id = ?", (dumps(flags), doc_id))
        conn.commit()
        raise
    out.update(api_calls=0 if res.cached else 1, cost_usd=res.cost_usd, note_kind=res.parsed.note_kind)
    out["events"] = apply_extraction(conn, cfg, doc, res.parsed, call_id=res.call_id, ents=ents, signature=signature)
    return out


def needs_extraction(doc: Mapping[str, Any], signature: str) -> bool:
    """Stale events, and not a request that already failed with exactly these inputs."""
    if doc["extracted_key"] == signature:
        return False
    return not any(f.get("code") == "extraction_failed" and f.get("signature") == signature
                   for f in flag_list(doc["flags"]))
