"""Events: stable ids, excerpt verification, reconcile against earlier extractions, and the review overlay.

- event_id = sha256(doc_id|event_type|norm(excerpt)|ordinal)[:16]: an entry keeps its id (and its review)
  when other parts of the note are edited, and a re-extraction of unchanged text changes nothing.
- The extractor owns `events`; humans own `event_reviews`, whose edits are merge-patches applied on read
  by timeline(). Nothing here ever deletes an event.
- Auto-accept is OFF until meta 'notes_auto_accept' is set, so the first backfill is reviewed in full.
  Notebook events are never auto-accepted; manual (`golf add`) events always are.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, NamedTuple, get_args

from golf.db import dumps, loads, now_iso
from golf.schemas.events import EventType

from .dates import INFO_FLAGS

AUTO_ACCEPT_KEY = "notes_auto_accept"
LIVE = ("pending", "auto_accepted")
JSON_COLS = ("focus_areas", "drills", "swing_thoughts", "equipment", "measurements", "injury", "flags")
MACHINE_COLS = (
    "call_id", "event_type", "is_planned", "date", "date_start", "date_end", "date_precision", "date_source",
    "date_expression", "date_reasoning", "coach", "lesson_format", "location", "location_kind",
    "duration_minutes", "focus_areas", "game_areas", "drills", "swing_thoughts", "equipment", "measurements",
    "injury", "goal_text", "summary", "source_excerpt", "line_start", "line_end", "excerpt_verified",
    "confidence", "flags", "linked_round_id",
)
# Not counted as a change: line positions (and the reasoning that cites them) shift when an unrelated
# entry above is edited. call_id alone never triggers a write: it keeps pointing at the call that first
# produced this exact event, so a re-extraction of an unchanged entry leaves its row untouched.
QUIET_COLS = ("line_start", "line_end", "date_reasoning")
CROSS_FLAGS = ("possible_duplicate",)          # owned by flag_duplicates(); reconcile preserves them
AUTO_DATE_SOURCES = ("explicit_text", "relative_reference", "entry_header")
CARRYOVER_MIN_SIMILARITY = 0.8
EXCERPT_MIN_RATIO = 0.9
EDITABLE = (
    "event_type", "is_planned", "date", "date_start", "date_end", "date_precision", "date_source", "coach",
    "lesson_format", "location", "location_kind", "duration_minutes", "focus_areas", "game_areas", "drills",
    "swing_thoughts", "equipment", "measurements", "injury", "goal_text", "summary",
)
DECISIONS = {"accept": "accepted", "accepted": "accepted", "reject": "rejected", "rejected": "rejected",
             "edit": "edited", "edited": "edited", "merge": "merged", "merged": "merged"}

FLAG_MESSAGES = {
    "year_inferred": "no year written; picked from the note's dates",
    "year_ambiguous": "two different years fit the note's dates",
    "out_of_window": "the date falls outside the note's lifetime",
    "non_monotonic": "this entry is out of order with its neighbours",
    "ambiguous_last_weekday": "'last <day>' could mean this week or the week before",
    "ambiguous_next_weekday": "'next <day>' could mean this week or the week after",
    "ambiguous_weekend": "'this weekend' was read as the weekend just past",
    "approximate": "the note gives only an approximate date",
    "coarse_period": "only the year is known",
    "month_inferred": "only the day of the month is written",
    "invalid_date": "the written date does not exist",
    "future_date": "the date is after the note was last edited",
    "far_past": "the date is more than a year before the note was created",
    "created_unreliable": "the creation time is unreliable (bulk import or photo time)",
    "date_disagreement": "an independent date parser read the date differently",
    "parsed_by_dateparser": "the date words were read by the fallback parser",
    "unparsed_date": "the date words could not be resolved; the note date was used",
    "bad_header_line": "the event pointed at an entry header that does not exist",
    "excerpt_unverified": "the quoted text was not found in the note",
    "unknown_coach": "the coach is not in entities.yaml",
    "multiple_injuries": "the model reported more than one injury; only the first is kept",
    "illegible_text": "the excerpt contains [illegible] words",
    "low_legibility": "the page is hard to read",
    "undated_page": "no date anywhere on the page",
    "possible_duplicate": "looks like the same occasion as another event",
    "possible_carryover": "replaces a reviewed event whose text was edited",
    "orphaned_review": "the reviewed text changed; carry the review over or dismiss it",
    "multiple_rounds_same_day": "more than one round was played that day",
    "extraction_failed": "the extraction call failed",
}


def make_flag(code: str, message: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message or FLAG_MESSAGES.get(code, code.replace("_", " ")), **extra}


def flag_list(flags: Iterable[dict | str] | str | None) -> list[dict[str, Any]]:
    """Flags as dicts; tolerates plain-string flags written by other tools (golf.db.add_flag)."""
    items = loads(flags, []) if isinstance(flags, str) or flags is None else list(flags)
    return [f if isinstance(f, dict) else make_flag(str(f)) for f in items or []]


def flag_codes(flags: Iterable[dict | str] | str | None) -> list[str]:
    return [f.get("code", "") for f in flag_list(flags)]


# ------------------------------------------------------------------ ids and excerpts
_LPREFIX = re.compile(r"(?m)^\s*L\d{1,4}:\s?")


def clean_excerpt(excerpt: str) -> str:
    """Drop "L004: " prefixes the model sometimes copies from the numbered text."""
    return _LPREFIX.sub("", excerpt or "").strip()


def norm_excerpt(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def event_id(doc_id: str, event_type: str, excerpt: str, ordinal: int = 0) -> str:
    blob = f"{doc_id}|{event_type}|{norm_excerpt(clean_excerpt(excerpt))}|{ordinal}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Verification(NamedTuple):
    verified: bool
    method: str           # exact | fuzzy | none
    line_start: int       # 1-based lines of the match in the stored text; -1 when not found
    line_end: int
    ratio: float


def _lines(text: str, start: int, end: int) -> tuple[int, int]:
    return text.count("\n", 0, start) + 1, text.count("\n", 0, max(start, end - 1)) + 1


def verify_excerpt(text: str, excerpt: str, *, min_ratio: float = EXCERPT_MIN_RATIO) -> Verification:
    """Substring match after whitespace collapse; else the best difflib window at ratio >= 0.9
    (rapidfuzz finds the window, difflib scores it)."""
    tokens = clean_excerpt(excerpt).split()
    if not tokens or not text:
        return Verification(False, "none", -1, -1, 0.0)
    m = re.search(r"\s+".join(map(re.escape, tokens)), text)
    if m:
        return Verification(True, "exact", *_lines(text, m.start(), m.end()), 1.0)
    from rapidfuzz import fuzz

    collapsed = " ".join(tokens)
    al = fuzz.partial_ratio_alignment(collapsed, text)
    if al is None:
        return Verification(False, "none", -1, -1, 0.0)
    window = text[al.dest_start:al.dest_end]
    ratio = difflib.SequenceMatcher(None, collapsed, " ".join(window.split()), autojunk=False).ratio()
    if ratio >= min_ratio:
        return Verification(True, "fuzzy", *_lines(text, al.dest_start, al.dest_end), round(ratio, 3))
    return Verification(False, "none", -1, -1, round(ratio, 3))


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_excerpt(a), norm_excerpt(b), autojunk=False).ratio()


# ------------------------------------------------------------------ policy
def auto_accept_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (AUTO_ACCEPT_KEY,)).fetchone()
    return bool(row) and str(row[0]).strip().lower() in ("1", "true", "yes", "on")


def set_auto_accept(conn: sqlite3.Connection, enabled: bool) -> None:
    """Turn on only after the gold-set targets are met (RESEARCH_PLAN §4 Review)."""
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (AUTO_ACCEPT_KEY, "1" if enabled else "0"))
    conn.commit()


def passes_auto_accept(ev: dict[str, Any]) -> bool:
    """confidence high, excerpt verified, no blocking flags (approximate dates and model review reasons
    are flags), and a date that came from written text."""
    blocking = [c for c in flag_codes(ev.get("flags")) if c not in INFO_FLAGS]
    return (ev.get("confidence") == "high" and bool(ev.get("excerpt_verified")) and not blocking
            and ev.get("date_source") in AUTO_DATE_SOURCES and ev.get("date_precision") != "inferred")


def _policy_status(source: str, ev: dict[str, Any], auto: bool) -> str:
    if source == "manual":
        return "auto_accepted"
    if source == "notebook" or not auto:
        return "pending"
    return "auto_accepted" if passes_auto_accept(ev) else "pending"


# ------------------------------------------------------------------ rounds
def _round_for(conn: sqlite3.Connection, ev: dict[str, Any]) -> tuple[str | None, bool]:
    if ev.get("event_type") != "on_course" or ev.get("date_precision") not in ("exact", "day") or not ev.get("date"):
        return None, False
    ids = [r[0] for r in conn.execute(
        "SELECT round_id FROM v_rounds WHERE played_on_local = ? ORDER BY played_at_utc, round_id", (ev["date"],))]
    return (ids[0] if ids else None), len(ids) > 1


def _with_round(conn: sqlite3.Connection, ev: dict[str, Any]) -> dict[str, Any]:
    rid, several = _round_for(conn, ev)
    flags = [f for f in flag_list(ev.get("flags")) if f.get("code") != "multiple_rounds_same_day"]
    if several:
        flags.append(make_flag("multiple_rounds_same_day"))
    return {**ev, "linked_round_id": rid, "flags": dumps(flags)}


def link_rounds(conn: sqlite3.Connection) -> int:
    """Re-link on_course events to rounds (run after a round import). Returns the number of events changed."""
    changed = 0
    for row in conn.execute("SELECT * FROM events WHERE status IN ('pending','auto_accepted')").fetchall():
        ev = dict(row)
        new = _with_round(conn, ev)
        if (new["linked_round_id"], new["flags"]) != (ev["linked_round_id"], ev["flags"]):
            conn.execute("UPDATE events SET linked_round_id = ?, flags = ?, updated_at = ? WHERE event_id = ?",
                         (new["linked_round_id"], new["flags"], now_iso(), ev["event_id"]))
            changed += 1
    conn.commit()
    return changed


# ------------------------------------------------------------------ reconcile
def _keep_cross_flags(new_flags: str, old_flags: str) -> str:
    kept = [f for f in flag_list(old_flags) if f.get("code") in CROSS_FLAGS]
    fresh = [f for f in flag_list(new_flags) if f.get("code") not in CROSS_FLAGS]
    return dumps(fresh + kept)


def reconcile_doc(conn: sqlite3.Connection, doc_id: str, candidates: list[dict[str, Any]], *,
                  source: str) -> dict[str, Any]:
    """Apply one extraction's events to a doc.

    Same id -> machine fields updated, status kept; new id -> pending / auto_accepted by policy;
    an id that vanished -> 'orphaned' if a human accepted/edited it (with a carry-over proposal when a
    new event of the same type has excerpt similarity >= 0.8), otherwise 'superseded'."""
    now = now_iso()
    existing = {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM events WHERE doc_id = ?", (doc_id,))}
    decisions = {r[0]: r[1] for r in conn.execute(
        "SELECT r.event_id, r.decision FROM event_reviews r JOIN events e ON e.event_id = r.event_id "
        "WHERE e.doc_id = ?", (doc_id,))}
    auto = auto_accept_enabled(conn)
    counts = dict.fromkeys(("new", "updated", "unchanged", "restored", "orphaned", "superseded",
                            "auto_accepted", "pending"), 0)
    cands: dict[str, dict[str, Any]] = {}
    for c in candidates:
        cands.setdefault(c["event_id"], _with_round(conn, c))

    missing = [eid for eid, old in existing.items() if eid not in cands and old["status"] in LIVE]
    carry: dict[str, tuple[str, float]] = {}
    fresh_ids = [eid for eid in cands if eid not in existing]
    for eid in missing:
        if decisions.get(eid) not in ("accepted", "edited"):
            continue
        old = existing[eid]
        scored = [(_similarity(old["source_excerpt"] or "", cands[n]["source_excerpt"] or ""), n)
                  for n in fresh_ids if cands[n]["event_type"] == old["event_type"]]
        best = max(scored, default=(0.0, None))
        if best[1] and best[0] >= CARRYOVER_MIN_SIMILARITY:
            carry[eid] = (best[1], round(best[0], 3))
            target = cands[best[1]]
            target["flags"] = dumps(flag_list(target["flags"]) + [
                make_flag("possible_carryover", from_event_id=eid, similarity=round(best[0], 3))])

    new_ids: list[str] = []
    for eid, cand in cands.items():
        old = existing.get(eid)
        if old is None:
            status = _policy_status(source, cand, auto)
            row = {**cand, "doc_id": doc_id, "status": status, "created_at": now, "updated_at": now}
            cols = list(row)
            conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                         [row[c] for c in cols])
            counts["new"] += 1
            counts[status] += 1
            new_ids.append(eid)
            continue
        cand = {**cand, "flags": _keep_cross_flags(cand["flags"], old["flags"])}
        loud = any(old[c] != cand.get(c) for c in MACHINE_COLS if c not in QUIET_COLS and c != "call_id")
        quiet = any(old[c] != cand.get(c) for c in QUIET_COLS)
        sets = {c: cand.get(c) for c in MACHINE_COLS}
        if old["status"] not in LIVE:              # the text came back (e.g. an edit was undone)
            sets["status"] = _policy_status(source, cand, auto)
            counts["restored"] += 1
        elif loud:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        if loud or quiet or "status" in sets:
            if loud or "status" in sets:
                sets["updated_at"] = now
            conn.execute(f"UPDATE events SET {', '.join(f'{c} = ?' for c in sets)} WHERE event_id = ?",
                         [*sets.values(), eid])

    for eid in missing:
        old = existing[eid]
        if eid in carry or decisions.get(eid) in ("accepted", "edited"):
            to = carry.get(eid)
            extra = {"carryover_to": to[0], "similarity": to[1]} if to else {}
            flags = [f for f in flag_list(old["flags"]) if f.get("code") != "orphaned_review"]
            flags.append(make_flag("orphaned_review", **extra))
            conn.execute("UPDATE events SET status = 'orphaned', flags = ?, updated_at = ? WHERE event_id = ?",
                         (dumps(flags), now, eid))
            counts["orphaned"] += 1
        else:
            conn.execute("UPDATE events SET status = 'superseded', updated_at = ? WHERE event_id = ?", (now, eid))
            counts["superseded"] += 1
    conn.commit()
    counts["new_ids"] = new_ids
    return counts


def flag_duplicates(conn: sqlite3.Connection, *, new_ids: Iterable[str] = ()) -> int:
    """Cross-note duplicates: same type, date ranges overlapping (1-day slack), same coach or same
    equipment model. Both events get a possible_duplicate flag pointing at the other; an event created in
    this run that was auto-accepted goes back to pending. Idempotent; returns the number of rows changed."""
    new_ids = set(new_ids)
    rows = [dict(r) for r in conn.execute(
        "SELECT e.event_id, e.doc_id, e.event_type, e.date_start, e.date_end, e.coach, e.equipment, e.flags,"
        " e.status, d.source AS doc_source, r.decision FROM events e JOIN source_docs d ON d.doc_id = e.doc_id"
        " LEFT JOIN event_reviews r ON r.event_id = e.event_id"
        " WHERE e.status IN ('pending','auto_accepted') AND d.status != 'deleted'")]
    active = [r for r in rows if r["decision"] not in ("rejected", "merged") and r["date_start"]]
    for r in active:
        r["_coach"] = (r["coach"] or "").casefold().strip()
        r["_models"] = {(e.get("model") or "").casefold().strip() for e in loads(r["equipment"], [])} - {""}
        r["_s"] = date.fromisoformat(r["date_start"]) - timedelta(days=1)
        r["_e"] = date.fromisoformat(r["date_end"] or r["date_start"]) + timedelta(days=1)
    dupes: dict[str, set[str]] = {}
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if a["event_type"] != b["event_type"] or a["doc_id"] == b["doc_id"]:
                continue
            if a["_s"] > b["_e"] or b["_s"] > a["_e"]:
                continue
            if (a["_coach"] and a["_coach"] == b["_coach"]) or (a["_models"] & b["_models"]):
                dupes.setdefault(a["event_id"], set()).add(b["event_id"])
                dupes.setdefault(b["event_id"], set()).add(a["event_id"])
    changed = 0
    for r in rows:
        old = flag_list(r["flags"])
        new = [f for f in old if f.get("code") not in CROSS_FLAGS]
        new += [make_flag("possible_duplicate", other_event_id=o) for o in sorted(dupes.get(r["event_id"], ()))]
        if new == old:
            continue
        status = r["status"]
        if (r["event_id"] in new_ids and status == "auto_accepted" and r["doc_source"] != "manual"
                and r["event_id"] in dupes and r["decision"] is None):
            status = "pending"
        conn.execute("UPDATE events SET flags = ?, status = ?, updated_at = ? WHERE event_id = ?",
                     (dumps(new), status, now_iso(), r["event_id"]))
        changed += 1
    conn.commit()
    return changed


# ------------------------------------------------------------------ reading
def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 merge-patch (nested objects merge, null deletes, anything else replaces)."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def apply_edits(ev: dict[str, Any], edits: dict[str, Any] | None) -> dict[str, Any]:
    """Merge-patch on an event dict. At the top level a null sets the field to None (the key stays, so
    consumers can rely on every column being present)."""
    if not edits:
        return ev
    out = dict(ev)
    for k, v in edits.items():
        out[k] = None if v is None else merge_patch(out.get(k), v)
    return out


def event_dict(row: sqlite3.Row | dict) -> dict[str, Any]:
    ev = dict(row)
    for c in JSON_COLS:
        if c in ev:
            ev[c] = loads(ev[c], [] if c != "injury" else None)
    return ev


def timeline(conn: sqlite3.Connection, *, start: str | None = None, end: str | None = None,
             event_types: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Events that count (auto-accepted and unreviewed, or reviewed accepted/edited) with review edits
    applied, oldest first. Events of deleted notes are hidden but kept."""
    types = set(event_types or ())
    out = []
    for row in conn.execute(
            "SELECT t.*, d.source AS doc_source, d.title AS doc_title FROM timeline_raw t"
            " JOIN source_docs d ON d.doc_id = t.doc_id WHERE d.status != 'deleted'"):
        ev = event_dict(row)
        ev = apply_edits(ev, loads(ev.pop("review_edits"), None))
        if types and ev["event_type"] not in types:
            continue
        if start and (ev["date_end"] or ev["date"] or "") < start:
            continue
        if end and (ev["date_start"] or ev["date"] or "9999") > end:
            continue
        out.append(ev)
    out.sort(key=lambda e: (e["date"] or "9999-12-31", e["event_id"]))
    return out


def pending_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The review queue: unreviewed pending events, plus reviewed events orphaned by an edit to their text
    (queue_reason 'orphaned'; resolve those with carry_over_review() or review_event(..., 'reject'/'merge'):
    review_event refuses to accept or edit them)."""
    rows = conn.execute(
        "SELECT e.*, d.source AS doc_source, d.title AS doc_title, d.external_id AS doc_external_id,"
        " r.decision AS review_decision FROM events e JOIN source_docs d ON d.doc_id = e.doc_id"
        " LEFT JOIN event_reviews r ON r.event_id = e.event_id"
        " WHERE d.status != 'deleted' AND ((e.status = 'pending' AND r.decision IS NULL)"
        "   OR (e.status = 'orphaned' AND r.decision IN ('accepted','edited')))"
        " ORDER BY e.date IS NULL, e.date, e.doc_id, e.line_start, e.event_id").fetchall()
    out = []
    for row in rows:
        ev = event_dict(row)
        ev["queue_reason"] = "orphaned" if ev["status"] == "orphaned" else "new"
        out.append(ev)
    return out


# ------------------------------------------------------------------ reviewing
def _as_text(v: Any) -> str | None:
    return None if v is None else v if isinstance(v, str) else dumps(v)


def _complete_edits(ev: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(edits) - set(EDITABLE))
    if unknown:
        raise ValueError(f"cannot edit {unknown}; editable fields: {', '.join(EDITABLE)}")
    edits = dict(edits)
    if "event_type" in edits and edits["event_type"] not in get_args(EventType):
        raise ValueError(f"unknown event_type {edits['event_type']!r}")
    if edits.get("date_precision") not in (None, "exact", "day", "week", "month", "inferred"):
        raise ValueError("date_precision must be exact, day, week, month or inferred")
    for k in ("date", "date_start", "date_end"):
        if edits.get(k) is not None:
            edits[k] = date.fromisoformat(str(edits[k])).isoformat()
    if edits.get("date"):
        edits.setdefault("date_start", edits["date"])
        edits.setdefault("date_end", edits["date"])
        edits.setdefault("date_precision", "day")
        edits.setdefault("date_source", "manual")
    if "focus_areas" in edits and "game_areas" not in edits:
        areas = sorted({f.get("game_area") for f in edits["focus_areas"] or [] if f.get("game_area")})
        edits["game_areas"] = ",".join(areas) or None
    return edits


def review_event(conn: sqlite3.Connection, event_id: str, decision: str, edits: dict[str, Any] | None = None,
                 comment: str | None = None, merged_into: str | None = None) -> dict[str, Any]:
    """Record a human decision (accept / reject / edit / merge) and one corrections row per changed field
    (the corrected set becomes the golden eval set). Returns the event as the timeline would show it."""
    decision = DECISIONS.get((decision or "").strip().lower(), "")
    if not decision:
        raise ValueError("decision must be one of accept, reject, edit, merge")
    row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(f"no event {event_id}")
    ev = dict(row)
    if edits and decision == "accepted":
        decision = "edited"
    if ev["status"] == "orphaned" and decision in ("accepted", "edited"):
        # Its text is gone from the note: accepting it again would change nothing and leave it queued.
        raise ValueError("This event's text was edited in the note after you reviewed it, so it cannot be accepted "
                         "again. Carry your earlier review over to the event that replaced it, or dismiss it.")
    if decision == "edited" and not edits:
        raise ValueError("an 'edit' decision needs edits")
    if decision == "merged":
        if not merged_into or merged_into == event_id or not conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (merged_into,)).fetchone():
            raise ValueError("merge needs merged_into = another existing event id")
    else:
        merged_into = None
    edits = _complete_edits(ev, edits) if edits else None
    now = now_iso()
    conn.execute(
        "INSERT INTO event_reviews(event_id, decision, merged_into, edits_json, comment, reviewed_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET decision = excluded.decision,"
        " merged_into = excluded.merged_into, edits_json = excluded.edits_json, comment = excluded.comment,"
        " reviewed_at = excluded.reviewed_at",
        (event_id, decision, merged_into, dumps(edits) if edits else None, comment, now))
    rows = [("decision", ev["status"], decision if not merged_into else f"merged:{merged_into}")]
    rows += [(k, _as_text(ev.get(k)), _as_text(v)) for k, v in (edits or {}).items() if _as_text(ev.get(k)) != _as_text(v)]
    conn.executemany(
        "INSERT INTO corrections(entity, entity_id, field, model_value, corrected_value, call_id, corrected_at)"
        " VALUES ('event', ?, ?, ?, ?, ?, ?)", [(event_id, f, mv, cv, ev["call_id"], now) for f, mv, cv in rows])
    conn.commit()
    return apply_edits(event_dict(ev), edits)


def carry_over_review(conn: sqlite3.Connection, from_event_id: str, to_event_id: str) -> None:
    """Move a review from an orphaned event onto the event that replaced it (one keystroke in review)."""
    old = conn.execute("SELECT * FROM event_reviews WHERE event_id = ?", (from_event_id,)).fetchone()
    if old is None:
        raise ValueError(f"{from_event_id} has no review to carry over")
    if not conn.execute("SELECT 1 FROM events WHERE event_id = ?", (to_event_id,)).fetchone():
        raise KeyError(f"no event {to_event_id}")
    if conn.execute("SELECT 1 FROM event_reviews WHERE event_id = ?", (to_event_id,)).fetchone():
        raise ValueError(f"{to_event_id} is already reviewed")
    now = now_iso()
    comment = f"carried over from {from_event_id}" + (f"; {old['comment']}" if old["comment"] else "")
    conn.execute("INSERT INTO event_reviews(event_id, decision, merged_into, edits_json, comment, reviewed_at)"
                 " VALUES (?,?,?,?,?,?)", (to_event_id, old["decision"], None, old["edits_json"], comment, now))
    conn.execute("UPDATE event_reviews SET decision = 'merged', merged_into = ?, reviewed_at = ? WHERE event_id = ?",
                 (to_event_id, now, from_event_id))
    conn.commit()
