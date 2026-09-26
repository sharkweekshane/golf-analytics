"""Quick-add (`golf add`) and remembered past events (manual_events.yaml).

Both are Shane's own words with a date he chose, so their events are auto-accepted. `golf add` text
goes through the same tl-1 extractor when the LLM is available (anchored at the given date); without it
the entry is still stored, as event_type 'other', so nothing typed is ever lost.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, get_args
from zoneinfo import ZoneInfo

import yaml

import golf.llm as llm
from golf.config import Config
from golf.db import dumps, loads, now_iso, upsert
from golf.schemas.events import EventType, GameArea

from .dates import month_bounds
from .entities import Entities, load_entities
from .extract import content_hash, extract_doc, normalize_text
from .reconcile import event_dict, event_id, flag_duplicates, make_flag, reconcile_doc

EVENT_TYPES = get_args(EventType)
GAME_AREAS = get_args(GameArea)


def _next_manual_id(conn: sqlite3.Connection) -> str:
    nums = [int(r[0][4:]) for r in conn.execute("SELECT doc_id FROM source_docs WHERE doc_id GLOB 'man-[0-9]*'")
            if r[0][4:].isdigit()]
    return f"man-{max(nums, default=0) + 1}"


def _as_date(value: date | datetime | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def parse_focus(focus: str | Iterable[str] | None) -> list[dict[str, str]]:
    """'putting, short game' -> focus_areas entries; unknown areas are an error with the valid list."""
    if not focus:
        return []
    items = focus.split(",") if isinstance(focus, str) else list(focus)
    out = []
    for item in items:
        key = re.sub(r"[\s\-]+", "_", str(item).strip().lower())
        if not key:
            continue
        if key not in GAME_AREAS:
            raise ValueError(f"unknown focus {item!r}; use one of: {', '.join(GAME_AREAS)}")
        out.append({"game_area": key, "detail": ""})
    return out


def _check_type(event_type: str) -> str:
    t = event_type.strip().lower().replace(" ", "_").replace("-", "_")
    if t not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}; use one of: {', '.join(EVENT_TYPES)}")
    return t


def manual_row(doc_id: str, *, event_type: str, excerpt: str, start: date, end: date, precision: str,
               reasoning: str, ordinal: int = 0, summary: str | None = None, coach: str | None = None,
               location: str | None = None, focus_areas: list[dict] | None = None,
               equipment: list[dict] | None = None, is_planned: bool = False, flags: list[dict] | None = None,
               ents: Entities | None = None, line_end: int = 1) -> dict[str, Any]:
    """A DB-ready event row for something Shane typed (no model involved)."""
    ents = ents or Entities()
    coach = ents.canonical_coach(coach) or coach or None
    canonical_loc = ents.canonical_location(location)
    focus_areas = focus_areas or []
    return {
        "event_id": event_id(doc_id, event_type, excerpt, ordinal), "call_id": None,
        "event_type": event_type, "is_planned": int(is_planned),
        "date": (start + (end - start) // 2).isoformat(), "date_start": start.isoformat(), "date_end": end.isoformat(),
        "date_precision": precision, "date_source": "manual", "date_expression": None, "date_reasoning": reasoning,
        "coach": coach, "lesson_format": None, "location": canonical_loc or location or None,
        "location_kind": ents.location_kind(canonical_loc), "duration_minutes": None,
        "focus_areas": dumps(focus_areas),
        "game_areas": ",".join(sorted({f["game_area"] for f in focus_areas if f.get("game_area")})) or None,
        "drills": "[]", "swing_thoughts": "[]", "equipment": dumps(equipment or []), "measurements": "[]",
        "injury": None, "goal_text": None, "summary": summary or excerpt[:200], "source_excerpt": excerpt,
        "line_start": 1, "line_end": line_end, "excerpt_verified": 1, "confidence": "high",
        "flags": dumps(flags or []),
    }


def add_manual_event(conn: sqlite3.Connection, cfg: Config, text: str, *, date: date | str | None = None,
                     event_type: str | None = None, coach: str | None = None, focus: str | Iterable[str] | None = None,
                     use_llm: bool = True) -> dict[str, Any]:
    """`golf add "<text>" [--date] [--type --coach --focus]`: a manual source doc plus accepted event(s).

    With --type the LLM is skipped. Otherwise the text is extracted with tl-1 anchored at the date
    (default today); coach/focus given alongside override what the model found. If the LLM is
    unavailable or fails, one 'other' event is stored with the given date."""
    text = normalize_text(text)
    if not text:
        raise ValueError("nothing to add: the text is empty")
    focus_areas = parse_focus(focus)
    event_type = _check_type(event_type) if event_type else None
    zone = ZoneInfo(cfg.timezone)
    now = datetime.now(zone).replace(microsecond=0)
    day = _as_date(date) or now.date()
    doc_id = _next_manual_id(conn)
    doc = {"doc_id": doc_id, "source": "manual", "external_id": None, "title": text.split("\n")[0][:80],
           "folder": None, "created_at": datetime.combine(day, time(12), zone).isoformat(),
           "modified_at": max(now, datetime.combine(day, time(12), zone)).isoformat(), "text": text,
           "content_hash": content_hash(text), "status": "active", "flags": "[]"}
    upsert(conn, "source_docs", doc, ["doc_id"])
    conn.commit()
    ents = load_entities(cfg)
    used_llm, error = False, None
    if event_type is None and use_llm:
        try:
            res = extract_doc(conn, cfg, doc_id, entities=ents)
            used_llm = res["events"]["new"] > 0
        except (llm.LLMUnavailable, llm.LLMOutputError) as e:
            error = str(e)
    if used_llm:
        _apply_overrides(conn, doc_id, ents.canonical_coach(coach) or coach, focus_areas)
    else:
        flags = [make_flag("extraction_failed", f"stored without extraction: {error}")] if error else []
        row = manual_row(doc_id, event_type=event_type or "other", excerpt=text, start=day, end=day, precision="day",
                         reasoning=f"date given with golf add: {day} ({day:%a})", coach=coach,
                         focus_areas=focus_areas, is_planned=day > now.date(), flags=flags, ents=ents,
                         line_end=len(text.split("\n")))
        reconcile_doc(conn, doc_id, [row], source="manual")
    flag_duplicates(conn)
    events = [event_dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE doc_id = ? AND status IN ('pending','auto_accepted') ORDER BY line_start, event_id",
        (doc_id,))]
    return {"doc_id": doc_id, "used_llm": used_llm, "llm_error": error, "events": events}


def _apply_overrides(conn: sqlite3.Connection, doc_id: str, coach: str | None, focus_areas: list[dict]) -> None:
    """--coach goes on the lesson/fitting events (on every event if there are none); --focus on all."""
    if not coach and not focus_areas:
        return
    rows = conn.execute("SELECT event_id, event_type, focus_areas FROM events WHERE doc_id = ?", (doc_id,)).fetchall()
    coached = {r["event_id"] for r in rows if r["event_type"] in ("lesson", "fitting")} or {r["event_id"] for r in rows}
    for r in rows:
        sets: dict[str, Any] = {}
        if coach and r["event_id"] in coached:
            sets["coach"] = coach
        if focus_areas:
            merged = {f["game_area"]: f for f in focus_areas}
            merged.update({f["game_area"]: f for f in loads(r["focus_areas"], [])})
            sets["focus_areas"] = dumps(list(merged.values()))
            sets["game_areas"] = ",".join(sorted(merged))
        if not sets:
            continue
        sets["updated_at"] = now_iso()
        conn.execute(f"UPDATE events SET {', '.join(f'{k} = ?' for k in sets)} WHERE event_id = ?",
                     [*sets.values(), r["event_id"]])
    conn.commit()


def _yaml_dates(value: Any) -> tuple[date, date, str]:
    """2024-05-18 -> day; '2024-05' -> month; 2024 / '2024' -> the whole year (precision inferred)."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value, value, "day"
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        d = date.fromisoformat(s)
        return d, d, "day"
    if re.fullmatch(r"\d{4}-\d{1,2}", s):
        y, m = map(int, s.split("-"))
        return (*month_bounds(y, m), "month")
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 1, 1), date(int(s), 12, 31), "inferred"
    raise ValueError(f"unrecognised date {value!r}: use YYYY-MM-DD, YYYY-MM or YYYY")


def load_manual_yaml(conn: sqlite3.Connection, path: Path | str) -> dict[str, Any]:
    """Load manual_events.yaml (remembered events, usually month precision). Idempotent: an unchanged
    entry keeps its id; an entry removed from the file is superseded."""
    path = Path(path)
    raw = path.read_text()
    data = yaml.safe_load(raw) or []
    if isinstance(data, dict):
        data = data.get("events") or []
    doc_id = "man-yaml-" + (re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-") or "events")
    starts = []
    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict) or "date" not in item or "type" not in item:
            raise ValueError(f"{path.name} entry {i}: needs at least 'date' and 'type'")
        etype = _check_type(str(item["type"]))
        start, end, precision = _yaml_dates(item["date"])
        if item.get("precision") in ("day", "week", "month", "inferred"):
            precision = item["precision"]
        summary = str(item.get("summary") or item.get("text") or f"{etype} ({item['date']})").strip()
        key = (etype, " ".join(summary.split()).casefold())
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        starts.append(start)
        rows.append(manual_row(
            doc_id, event_type=etype, excerpt=summary, start=start, end=end, precision=precision,
            reasoning=f"{path.name}: date {item['date']}", ordinal=ordinal, summary=summary,
            coach=item.get("coach"), location=item.get("location"), focus_areas=parse_focus(item.get("focus")),
            equipment=list(item.get("equipment") or []), line_end=1))
    mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().replace(microsecond=0)
    upsert(conn, "source_docs", {
        "doc_id": doc_id, "source": "manual", "external_id": str(path), "title": path.name, "folder": None,
        "created_at": (datetime.combine(min(starts), time(12)).astimezone() if starts else mtime).isoformat(),
        "modified_at": mtime.isoformat(), "text": raw, "content_hash": content_hash(raw), "status": "active",
        "flags": "[]", "deleted_at": None}, ["doc_id"])
    counts = reconcile_doc(conn, doc_id, rows, source="manual")
    counts.pop("new_ids", None)
    flag_duplicates(conn)
    return {"doc_id": doc_id, "entries": len(rows), **counts}
