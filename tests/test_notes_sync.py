"""Apple Notes sync, reconcile, review and quick-add. osascript is never run: FakeNotes stands in for
notes_dump.js, and golf.llm.SEND is replaced by a router that answers per note."""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import golf.llm as llm
from golf.notes import (
    NOTE_TEMPLATE_HELP, NotesError, NotesPermissionError, add_manual_event, carry_over_review, flag_duplicates,
    format_sync_summary, link_rounds, load_manual_yaml, pending_events, review_event, set_auto_accept,
    sync_apple_notes, timeline,
)
from golf.notes.applenotes import PERMISSION_HELP
from golf.notes.reconcile import event_id, verify_excerpt
from golf.schemas.events import NoteExtraction

FIX = Path(__file__).parent / "fixtures" / "notes"
TZ = ZoneInfo("America/New_York")
ENTITIES = """
coaches:
  - {name: Mike Rossi, aliases: [Mike, coach Mike], initials: [MR]}
  - {name: Dana Lee, aliases: [Dana]}
locations:
  - {name: Pine Hill, kind: range, aliases: [PH]}
  - {name: Stow North, kind: course, aliases: [Stow]}
bag: ["Driver: Stealth 2 10.5"]
"""


# ------------------------------------------------------------------ fakes
def utc(y, m, d, h=12, mi=0) -> str:
    return datetime(y, m, d, h, mi, tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeNotes:
    """Emulates notes_dump.js (meta / bodies) over an in-memory folder and records every osascript call."""

    def __init__(self, *, folder="Golf", account="iCloud", extra_accounts=()):
        self.folder, self.account, self.extra_accounts = folder, account, list(extra_accounts)
        self.notes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.fail: tuple[int, str, str] | None = None
        self.vanish_on_body: set[str] = set()

    def add(self, nid, name, text, created, modified=None, *, locked=False, folder=None):
        self.notes[nid] = {"name": name, "text": text, "created": created, "modified": modified or created,
                           "locked": locked, "folder": folder or self.folder}

    def __call__(self, args):
        self.calls.append(args)
        assert args[:3] == ["osascript", "-l", "JavaScript"] and args[3].endswith("notes_dump.js")
        if self.fail:
            code, out, err = self.fail
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        mode, folder = args[4], args[5]
        if mode == "meta":
            payload = {"ok": True, "accounts": [self.account, *self.extra_accounts], "folders": [], "available": []}
            if folder == self.folder:
                subs = sorted({n["folder"] for n in self.notes.values() if n["folder"] != self.folder})
                payload["folders"].append({
                    "id": "f1", "name": self.folder, "account": self.account,
                    "subfolders": [{"path": s, "notes": sum(n["folder"] == s for n in self.notes.values())} for s in subs],
                    "notes": [{"id": i, "name": n["name"], "created": n["created"], "modified": n["modified"],
                               "locked": n["locked"], "folder": n["folder"]} for i, n in self.notes.items()]})
                for k, acc in enumerate(self.extra_accounts):
                    payload["folders"].append({"id": f"x{k}", "name": folder, "account": acc, "subfolders": [], "notes": []})
            else:
                payload["available"] = [f"{self.account}/{self.folder}", f"{self.account}/Recipes"]
        else:
            bodies = []
            for i in args[6:]:
                n = self.notes.get(i)
                if n is None or i in self.vanish_on_body:
                    bodies.append({"id": i, "error": "Can't get note.", "errorNumber": -1728})
                elif n["locked"]:
                    bodies.append({"id": i, "locked": True})
                else:
                    bodies.append({"id": i, "plaintext": n["text"]})
            payload = {"ok": True, "bodies": bodies}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload, ensure_ascii=True) + "\n", stderr="")

    @property
    def body_calls(self):
        return [c for c in self.calls if c[4] == "bodies"]


class RoutedLLM:
    """Answers each extraction by the first marker found in the user message (later routes win)."""

    def __init__(self):
        self.routes: list[tuple[str, str, str]] = []
        self.requests: list[dict] = []

    def route(self, marker, output, stop="end_turn"):
        self.routes.insert(0, (marker, output, stop))

    def __call__(self, request, use_fallbacks=False):
        self.requests.append(request)
        blob = json.dumps(request["messages"], ensure_ascii=False)
        for marker, out, stop in self.routes:
            if marker in blob:
                return llm.SendResult(text=out, stop_reason=stop, model=request["model"],
                                      usage={"input_tokens": 3500, "output_tokens": 1200})
        raise AssertionError("unexpected API call")


# ------------------------------------------------------------------ canned model output
def dref(**kw):
    base = dict(expression="", kind="none", year=-1, month=-1, day=-1, time_hhmm="", anchor="na", header_line=-1,
                rel_kind="none", rel_value=0, weekday="none", weekday_rel="none", period="none", approximate=False)
    base.update(kw)
    return base


def hdr(line):
    return dref(kind="header", header_line=line)


def ev(event_type, excerpt, line, date, *, coach="", planned=False, confidence="high", reasons=(), equipment=(),
       measurements=(), focus=(), location=""):
    return {"event_type": event_type, "date": date, "is_planned": planned, "coach": coach,
            "lesson_format": "in_person" if event_type == "lesson" else "none", "location": location,
            "location_kind": "none", "duration_minutes": -1,
            "focus_areas": [{"game_area": g, "detail": d} for g, d in focus], "drills": [], "swing_thoughts": [],
            "equipment": list(equipment), "measurements": list(measurements), "injury": [], "goal_text": "",
            "summary": excerpt[:60], "source": {"line_start": line, "line_end": line, "excerpt": excerpt},
            "confidence": confidence, "review_reasons": list(reasons)}


def extraction(kind, order, headers, events):
    return json.dumps({"note_kind": kind, "log_order": order,
                       "entry_headers": [{"line": ln, "date": dref(kind="absolute", month=m, day=d, expression=f"{m}/{d}")}
                                         for ln, m, d in headers],
                       "events": events})


OLDEST_L3 = "12/30 range, driver only, fixing slice with closed face feel"


def oldest_first_output(l3=OLDEST_L3):
    return extraction("running_log", "chronological", [(2, 12, 27), (3, 12, 30), (4, 1, 2), (5, 1, 6)], [
        ev("practice", "12/27 putting mat 20 min, 3ft drill 18/20", 2, hdr(2), focus=[("putting", "3ft drill")]),
        ev("practice", l3, 3, hdr(3), focus=[("driving", "fixing slice")]),
        ev("lesson", "1/2 lesson w/ Mike at Pine Hill: ball position back an inch for irons", 4, hdr(4),
           coach="Mike Rossi", location="Pine Hill", focus=[("full_swing", "ball position")]),
        ev("practice", "1/6 range 1 hr, ball position drill", 5, hdr(5)),
    ])


OUTPUTS = {
    "Worked on shallowing": extraction("single_entry", "na", [], [
        ev("lesson", "Today at Pine Hill w/ MR. Worked on shallowing the club in transition.", 2,
           dref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=0, expression="Today"),
           coach="Mike Rossi", location="Pine Hill", focus=[("full_swing", "shallowing in transition")])]),
    "wedges crisp": extraction("running_log", "reverse_chronological", [(2, 1, 9), (3, 1, 3), (4, 12, 28), (5, 12, 20)], [
        ev("practice", "1/9 - range 45 min. wedges crisp, 50 balls w/ gate drill", 2, hdr(2)),
        ev("practice", "1/3 - sim session, driver speed 101", 3, hdr(3), confidence="medium",
           reasons=["simulator location not named"]),
        ev("practice", "12/28 - chipping at home net, 30 min", 4, hdr(4)),
        ev("lesson", "12/20 - lesson w/ Mike, grip check, weaker left hand", 5, hdr(5), coach="Mike Rossi"),
    ]),
    "3ft drill": oldest_first_output(),
    "hit a bucket": extraction("mixed", "na", [], [
        ev("practice", "Yesterday hit a bucket at Pine Hill, 7i and 9i, contact better.", 2,
           dref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="Yesterday")),
        ev("on_course", "Last Sat played 9 at Stow w/ Dan, shot 44, 2 three-putts", 3,
           dref(kind="relative", anchor="note_created", rel_kind="weekday", weekday="sat", weekday_rel="last",
                expression="Last Sat"), location="Stow",
           measurements=[{"metric": "score", "value": "44", "unit": "", "club": ""}]),
        ev("lesson", "Plan: lesson next Tue w/ Mike", 4,
           dref(kind="relative", anchor="note_created", rel_kind="weekday", weekday="tue", weekday_rel="next",
                expression="next Tue"), coach="Mike Rossi", planned=True),
    ]),
    "Ventus Blue": extraction("single_entry", "na", [], [
        ev("fitting", "Got fitted at the club fitter on 5/2.", 2, dref(kind="absolute", month=5, day=2, expression="5/2")),
        ev("equipment_change", "Switched to Ping G440 10.5 w/ Ventus Blue 6S, replaces the old Stealth 2.", 2,
           dref(kind="absolute", month=5, day=2, expression="5/2"),
           equipment=[{"action": "replaced", "category": "driver", "brand": "Ping", "model": "G440",
                       "specs": "10.5 Ventus Blue 6S", "replaces": "Stealth 2"}]),
    ]),
    "coffee beans": extraction("not_golf", "na", [], []),
}
N_EVENTS = 14


def standard_notes(fake: FakeNotes) -> FakeNotes:
    t = lambda name: (FIX / f"{name}.txt").read_text()
    fake.add("x-coredata://S/ICNote/p1", "Lesson w/ Mike", t("single_lesson"), utc(2026, 3, 14, 23, 30), utc(2026, 3, 14, 23, 35))
    fake.add("x-coredata://S/ICNote/p2", "Range log", t("log_newest_first"), utc(2025, 12, 20, 19), utc(2026, 1, 9, 20))
    fake.add("x-coredata://S/ICNote/p3", "Practice log", t("log_oldest_first"), utc(2025, 12, 27, 18), utc(2026, 1, 6, 21))
    fake.add("x-coredata://S/ICNote/p4", "Practice", t("practice_relative"), utc(2026, 4, 15, 20))
    fake.add("x-coredata://S/ICNote/p5", "New driver", t("equipment_change"), utc(2026, 5, 3, 10))
    fake.add("x-coredata://S/ICNote/p6", "Groceries", t("non_golf"), utc(2026, 2, 1, 9))
    fake.add("x-coredata://S/ICNote/p7", "Private swing notes", "", utc(2026, 2, 2, 9), locked=True)
    return fake


@pytest.fixture
def notes(cfg):
    (Path(cfg.root) / "entities.yaml").write_text(ENTITIES)
    return standard_notes(FakeNotes())


@pytest.fixture
def router(monkeypatch):
    r = RoutedLLM()
    for marker, out in OUTPUTS.items():
        r.route(marker, out)
    monkeypatch.setattr(llm, "SEND", r)
    return r


def eid(conn, needle, statuses=("pending", "auto_accepted")):
    rows = conn.execute(f"SELECT event_id FROM events WHERE source_excerpt LIKE ? AND status IN ({','.join('?' * len(statuses))})",
                        (f"%{needle}%", *statuses)).fetchall()
    assert len(rows) == 1, (needle, rows)
    return rows[0][0]


def ev_row(conn, needle):
    return dict(conn.execute("SELECT * FROM events WHERE event_id = ?", (eid(conn, needle),)).fetchone())


def snapshot(conn):
    return ([dict(r) for r in conn.execute("SELECT * FROM events ORDER BY event_id")],
            [dict(r) for r in conn.execute("SELECT * FROM source_docs ORDER BY doc_id")])


# ------------------------------------------------------------------ first sync
def test_first_sync_extracts_everything_pending(conn, cfg, notes, router):
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["new"] == 6 and s["docs"]["locked"] == 1
    assert s["api_calls"] == 6 and s["cost_usd"] > 0 and s["not_golf"] == 1
    assert s["events"]["new"] == N_EVENTS and s["events"]["pending"] == N_EVENTS and s["events"]["auto_accepted"] == 0
    assert timeline(conn) == [] and len(pending_events(conn)) == N_EVENTS
    locked_id = "x-coredata://S/ICNote/p7"
    assert all(locked_id not in c for c in notes.body_calls)
    assert conn.execute("SELECT status, text FROM source_docs WHERE external_id = ?", (locked_id,)).fetchone()[:] == ("locked", None)
    assert conn.execute("SELECT MIN(excerpt_verified) FROM events").fetchone()[0] == 1

    dates = {needle: ev_row(conn, needle)["date"] for needle in (
        "Worked on shallowing", "1/9 - range", "12/28 - chipping", "12/20 - lesson", "12/27 putting", "1/2 lesson",
        "Yesterday hit", "Last Sat", "next Tue", "Got fitted", "Switched to Ping")}
    assert dates == {
        "Worked on shallowing": "2026-03-14",          # created 23:30 local = next day in UTC
        "1/9 - range": "2026-01-09", "12/28 - chipping": "2025-12-28", "12/20 - lesson": "2025-12-20",
        "12/27 putting": "2025-12-27", "1/2 lesson": "2026-01-02",
        "Yesterday hit": "2026-04-14", "Last Sat": "2026-04-11", "next Tue": "2026-04-21",
        "Got fitted": "2026-05-02", "Switched to Ping": "2026-05-02"}
    lesson = ev_row(conn, "Worked on shallowing")
    assert (lesson["coach"], lesson["location"], lesson["location_kind"]) == ("Mike Rossi", "Pine Hill", "range")
    assert (lesson["date_precision"], lesson["date_source"]) == ("day", "relative_reference")
    assert ev_row(conn, "1/2 lesson")["date_source"] == "entry_header"
    assert ev_row(conn, "Last Sat")["location"] == "Stow North"
    assert "need review" in format_sync_summary(s)
    # privacy: no note text in the logged request metadata
    metas = " ".join(r[0] for r in conn.execute("SELECT request_meta FROM llm_calls"))
    assert "shallowing" not in metas and "coffee" not in metas


def test_resync_without_edits_is_free_and_changes_nothing(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    before, calls_before, bodies_before = snapshot(conn), len(router.requests), len(notes.body_calls)
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["api_calls"] == 0 and len(router.requests) == calls_before
    assert len(notes.body_calls) == bodies_before                      # no body was even fetched
    assert all(v == 0 for v in s["events"].values()) and s["docs"]["unchanged"] == 6
    assert snapshot(conn) == before


def test_editing_one_log_entry_changes_only_that_entry(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    review_event(conn, eid(conn, "1/2 lesson"), "accept")
    review_event(conn, eid(conn, "12/27 putting"), "accept")
    before = {r["event_id"]: r for r in snapshot(conn)[0]}

    new_l3 = "12/30 range, driver only, fixing slice with a stronger grip"
    note = notes.notes["x-coredata://S/ICNote/p3"]
    note["text"] = note["text"].replace(OLDEST_L3, new_l3)
    note["modified"] = utc(2026, 1, 7, 8)
    router.route("stronger grip", oldest_first_output(new_l3))
    s = sync_apple_notes(conn, cfg, runner=notes)

    assert s["docs"]["changed"] == 1 and s["api_calls"] == 1
    assert {k: s["events"][k] for k in ("new", "updated", "unchanged", "superseded", "orphaned")} == \
        {"new": 1, "updated": 0, "unchanged": 3, "superseded": 1, "orphaned": 0}
    after = {r["event_id"]: r for r in snapshot(conn)[0]}
    changed = {k for k in after if k not in before or after[k] != before[k]}
    old_l3 = next(k for k, r in before.items() if r["source_excerpt"] == OLDEST_L3)
    new_id = eid(conn, "stronger grip")
    assert changed == {old_l3, new_id}
    assert after[old_l3]["status"] == "superseded" and after[new_id]["status"] == "pending"
    assert {e["source_excerpt"][:10] for e in timeline(conn)} == {"1/2 lesson", "12/27 putt"}


def test_editing_a_reviewed_entry_orphans_it_and_offers_carry_over(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    old = eid(conn, "closed face feel")
    review_event(conn, old, "edit", edits={"summary": "Driver-only range session"})
    edited = OLDEST_L3.replace("closed face feel", "closed clubface feel")
    note = notes.notes["x-coredata://S/ICNote/p3"]
    note["text"], note["modified"] = note["text"].replace(OLDEST_L3, edited), utc(2026, 1, 7, 8)
    router.route("clubface", oldest_first_output(edited))
    s = sync_apple_notes(conn, cfg, runner=notes)

    assert s["events"]["orphaned"] == 1 and s["events"]["new"] == 1
    new = eid(conn, "clubface")
    queue = {e["event_id"]: e for e in pending_events(conn)}
    assert queue[old]["queue_reason"] == "orphaned" and queue[new]["queue_reason"] == "new"
    orphan_flag = next(f for f in queue[old]["flags"] if f["code"] == "orphaned_review")
    assert orphan_flag["carryover_to"] == new and orphan_flag["similarity"] >= 0.8
    assert any(f["code"] == "possible_carryover" and f["from_event_id"] == old for f in queue[new]["flags"])

    carry_over_review(conn, old, new)
    assert old not in {e["event_id"] for e in pending_events(conn)} and new not in {e["event_id"] for e in pending_events(conn)}
    shown = next(e for e in timeline(conn) if e["event_id"] == new)
    assert shown["summary"] == "Driver-only range session"


# ------------------------------------------------------------------ locked / deleted / re-keyed
def test_note_that_becomes_locked_keeps_its_events(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    review_event(conn, eid(conn, "Worked on shallowing"), "accept")
    bodies = len(notes.body_calls)
    notes.notes["x-coredata://S/ICNote/p1"].update(locked=True, modified=utc(2026, 3, 20))
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["locked"] == 2 and s["api_calls"] == 0 and len(notes.body_calls) == bodies
    doc = conn.execute("SELECT status, text FROM source_docs WHERE external_id = 'x-coredata://S/ICNote/p1'").fetchone()
    assert doc["status"] == "locked" and "shallowing" in doc["text"]
    assert [e["source_excerpt"][:5] for e in timeline(conn)] == ["Today"]

    notes.notes["x-coredata://S/ICNote/p1"].update(locked=False, modified=utc(2026, 3, 21))
    s = sync_apple_notes(conn, cfg, runner=notes)                       # same text: no re-extraction
    assert s["docs"]["restored"] == 1 and s["api_calls"] == 0 and s["events"]["new"] == 0


def test_deleted_note_hides_events_and_restores(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    review_event(conn, eid(conn, "Got fitted"), "accept")
    assert len(timeline(conn)) == 1
    gone = notes.notes.pop("x-coredata://S/ICNote/p5")
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["deleted"] == 1
    assert conn.execute("SELECT status FROM source_docs WHERE external_id = 'x-coredata://S/ICNote/p5'").fetchone()[0] == "deleted"
    assert timeline(conn) == [] and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == N_EVENTS
    assert not any(e["source_excerpt"].startswith("Switched") for e in pending_events(conn))

    notes.notes["x-coredata://S/ICNote/p5"] = gone                       # back from Recently Deleted
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["restored"] == 1 and s["api_calls"] == 0 and len(timeline(conn)) == 1


def test_rekeyed_note_keeps_doc_and_reviews(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    review_event(conn, eid(conn, "Got fitted"), "accept")
    doc_id = conn.execute("SELECT doc_id FROM source_docs WHERE external_id = 'x-coredata://S/ICNote/p5'").fetchone()[0]
    notes.notes["x-coredata://T/ICNote/p99"] = notes.notes.pop("x-coredata://S/ICNote/p5")
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["rekeyed"] == 1 and s["docs"]["deleted"] == 0 and s["api_calls"] == 0
    assert conn.execute("SELECT external_id FROM source_docs WHERE doc_id = ?", (doc_id,)).fetchone()[0] == "x-coredata://T/ICNote/p99"
    assert len(timeline(conn)) == 1


def test_body_read_failure_is_reported_and_retried(conn, cfg, notes, router):
    notes.vanish_on_body.add("x-coredata://S/ICNote/p4")
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["new"] == 5 and any("Can't get note" in e["error"] for e in s["errors"])
    notes.vanish_on_body.clear()
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["docs"]["new"] == 1 and s["events"]["new"] == 3


# ------------------------------------------------------------------ policy, entities, failures
def test_auto_accept_policy_once_enabled(conn, cfg, notes, router):
    set_auto_accept(conn, True)
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["events"]["auto_accepted"] == N_EVENTS - 1 and s["events"]["pending"] == 1
    assert [e["source_excerpt"] for e in pending_events(conn)] == ["1/3 - sim session, driver speed 101"]
    assert len(timeline(conn)) == N_EVENTS - 1
    flags = pending_events(conn)[0]["flags"]
    assert {"code": "model_review", "message": "simulator location not named"} in flags


def test_editing_entities_reextracts_from_cache_without_changing_events(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    before = snapshot(conn)[0]
    (Path(cfg.root) / "entities.yaml").write_text(ENTITIES.replace("[Dana]", "[Dana, DL]"))
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["api_calls"] == 6                               # entities hash is part of the cache key
    assert s["events"]["new"] == 0 and s["events"]["updated"] == 0
    assert [{k: v for k, v in r.items() if k != "call_id"} for r in snapshot(conn)[0]] == \
        [{k: v for k, v in r.items() if k != "call_id"} for r in before]
    (Path(cfg.root) / "entities.yaml").write_text(ENTITIES.replace("[Dana]", "[Dana, DL]") + "\n# a comment\n")
    assert sync_apple_notes(conn, cfg, runner=notes)["api_calls"] == 0      # comments don't change the hash


def test_extract_false_then_later(conn, cfg, notes, router):
    s = sync_apple_notes(conn, cfg, runner=notes, extract=False)
    assert s["extract_pending"] == 6 and s["api_calls"] == 0 and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    bodies = len(notes.body_calls)
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["extracted"] == 6 and s["events"]["new"] == N_EVENTS and len(notes.body_calls) == bodies


def test_llm_unavailable_keeps_docs_for_later(conn, cfg, notes, monkeypatch):
    def no_key(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")
    monkeypatch.setattr(llm, "SEND", no_key)
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["llm_unavailable"] and s["docs"]["new"] == 6 and s["extract_pending"] == 6
    router = RoutedLLM()
    for marker, out in OUTPUTS.items():
        router.route(marker, out)
    monkeypatch.setattr(llm, "SEND", router)
    assert sync_apple_notes(conn, cfg, runner=notes)["events"]["new"] == N_EVENTS


def test_refused_note_is_not_rebilled_every_sync(conn, cfg, notes, router):
    router.route("coffee beans", "", stop="refusal")
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["api_calls"] == 5 and any("refusal" in e["error"] for e in s["errors"])
    calls = len(router.requests)
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert len(router.requests) == calls and s["errors"] == []


def test_dry_run_writes_nothing(conn, cfg, notes, router):
    s = sync_apple_notes(conn, cfg, runner=notes, dry_run=True)
    assert s["dry_run"] and s["docs"]["new"] == 6 and s["extract_pending"] == 6 and s["api_calls"] == 0
    for table in ("source_docs", "events", "imports", "llm_calls"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_bulk_imported_notes_have_unreliable_creation_dates(conn, cfg, router):
    fake = FakeNotes()
    for i in range(3):
        fake.add(f"id-{i}", f"Imported {i}", f"Imported {i}\nYesterday hit a bucket at Pine Hill, 7i and 9i, contact better.",
                 utc(2024, 1, 5, 10), utc(2024, 1, 5, 10))
    sync_apple_notes(conn, cfg, runner=fake)
    flags = [json.loads(r[0]) for r in conn.execute("SELECT flags FROM source_docs")]
    assert all(f[0]["code"] == "created_unreliable" for f in flags)
    rows = conn.execute("SELECT date_precision, flags FROM events").fetchall()
    assert rows and all(r[0] == "inferred" and "created_unreliable" in r[1] for r in rows)


def test_non_ascii_text_round_trips(conn, cfg, router):
    fake = FakeNotes()
    text = "Lesson recap\nCafé chat w/ Dana — ⛳ tempo drill 👍 3x10"
    fake.add("u1", "Lesson recap", text, utc(2026, 6, 1, 18))
    router.route("tempo drill", extraction("single_entry", "na", [], [
        ev("lesson", "Café chat w/ Dana — ⛳ tempo drill 👍 3x10", 2, dref(kind="none"), coach="Dana Lee")]))
    sync_apple_notes(conn, cfg, runner=fake)
    assert conn.execute("SELECT text FROM source_docs").fetchone()[0] == text
    row = conn.execute("SELECT excerpt_verified, coach, date, date_precision FROM events").fetchone()
    assert tuple(row) == (1, "Dana Lee", "2026-06-01", "inferred")


# ------------------------------------------------------------------ osascript diagnostics
def test_permission_denied_gives_clear_instructions(conn, cfg):
    fake = FakeNotes()
    fake.fail = (1, "", "execution error: Not authorized to send Apple events to Notes. (-1743)")
    with pytest.raises(NotesPermissionError) as e:
        sync_apple_notes(conn, cfg, runner=fake)
    assert "Privacy & Security > Automation" in str(e.value) and str(e.value) == PERMISSION_HELP
    fake.fail = (0, json.dumps({"ok": False, "error": "Not authorized", "errorNumber": -1743}), "")
    with pytest.raises(NotesPermissionError):
        sync_apple_notes(conn, cfg, runner=fake)


@pytest.mark.parametrize("fail,match", [
    ((1, "", "execution error: Notes got an error: Application isn't running. (-600)"), "did not respond"),
    ((1, "", "syntax error"), "Reading Apple Notes failed"),
    ((0, "garbage", ""), "Unexpected output"),
])
def test_other_osascript_failures(conn, cfg, fail, match):
    fake = FakeNotes()
    fake.fail = fail
    with pytest.raises(NotesError, match=match):
        sync_apple_notes(conn, cfg, runner=fake)


def test_runner_exceptions_become_notes_errors(conn, cfg):
    def timeout(args):
        raise subprocess.TimeoutExpired(args, 300)

    def missing(args):
        raise FileNotFoundError("osascript")
    with pytest.raises(NotesError, match="did not answer"):
        sync_apple_notes(conn, cfg, runner=timeout)
    with pytest.raises(NotesError, match="only runs on macOS"):
        sync_apple_notes(conn, cfg, runner=missing)


def test_folder_not_found_lists_what_exists(conn, cfg):
    with pytest.raises(NotesError) as e:
        sync_apple_notes(conn, cfg, runner=FakeNotes(folder="Golfing"))
    assert "No Apple Notes folder named 'Golf'" in str(e.value) and "iCloud/Recipes" in str(e.value)


def test_duplicate_golf_folders_list_accounts_and_can_be_disambiguated(conn, cfg, router):
    fake = standard_notes(FakeNotes(extra_accounts=["On My Mac"]))
    with pytest.raises(NotesError) as e:
        sync_apple_notes(conn, cfg, runner=fake)
    assert "iCloud" in str(e.value) and "On My Mac" in str(e.value) and "iCloud/Golf" in str(e.value)
    s = sync_apple_notes(conn, dataclasses.replace(cfg, apple_notes_folder="iCloud/Golf"), runner=fake)
    assert s["docs"]["new"] == 6 and s["account"] == "iCloud"


def test_nested_subfolders_and_non_icloud_account_are_reported(conn, cfg, router):
    fake = FakeNotes(account="On My Mac")
    fake.add("n1", "Groceries", (FIX / "non_golf.txt").read_text(), utc(2026, 2, 1), folder="Golf/Archive")
    s = sync_apple_notes(conn, cfg, runner=fake)
    assert any("Golf/Archive (1 notes)" in w for w in s["warnings"])
    assert any("not iCloud" in w for w in s["warnings"])
    assert conn.execute("SELECT folder FROM source_docs").fetchone()[0] == "Golf/Archive"


# ------------------------------------------------------------------ review, duplicates, rounds
def test_review_edit_patches_timeline_and_records_corrections(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    lesson = eid(conn, "Worked on shallowing")
    out = review_event(conn, lesson, "edit", edits={"coach": "Dana Lee", "date": "2026-03-15"}, comment="wrong coach")
    assert out["coach"] == "Dana Lee"
    shown = timeline(conn)[0]
    assert (shown["coach"], shown["date"], shown["date_start"], shown["date_end"], shown["date_precision"],
            shown["date_source"], shown["review_decision"]) == \
        ("Dana Lee", "2026-03-15", "2026-03-15", "2026-03-15", "day", "manual", "edited")
    assert ev_row(conn, "Worked on shallowing")["coach"] == "Mike Rossi"      # machine row untouched
    fields = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT field, model_value, corrected_value FROM corrections WHERE entity_id = ?", (lesson,))}
    assert fields["coach"] == ("Mike Rossi", "Dana Lee") and fields["decision"] == ("pending", "edited")

    review_event(conn, lesson, "reject")
    assert timeline(conn) == []
    with pytest.raises(ValueError):
        review_event(conn, lesson, "edit", edits={"not_a_field": 1})
    with pytest.raises(ValueError):
        review_event(conn, lesson, "merge")
    with pytest.raises(KeyError):
        review_event(conn, "nope", "accept")


def test_cross_note_duplicates_are_flagged_and_merge_clears_them(conn, cfg, notes, router):
    notes.add("x-coredata://S/ICNote/p8", "Lesson recap", "Lesson recap\nMike lesson today: shallowing, pump drill",
              utc(2026, 3, 14, 22))
    router.route("Mike lesson today", extraction("single_entry", "na", [], [
        ev("lesson", "Mike lesson today: shallowing, pump drill", 2,
           dref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=0, expression="today"),
           coach="Mike Rossi")]))
    s = sync_apple_notes(conn, cfg, runner=notes)
    assert s["duplicates_flagged"] == 2
    a, b = eid(conn, "Worked on shallowing"), eid(conn, "Mike lesson today")
    flags_a = json.loads(ev_row(conn, "Worked on shallowing")["flags"])
    assert {"code": "possible_duplicate", "message": flags_a[-1]["message"], "other_event_id": b} in flags_a
    review_event(conn, b, "merge", merged_into=a)
    assert flag_duplicates(conn) == 2
    assert not any(f["code"] == "possible_duplicate" for f in json.loads(ev_row(conn, "Worked on shallowing")["flags"]))


def test_on_course_events_link_to_rounds(conn, cfg, notes, router):
    sync_apple_notes(conn, cfg, runner=notes)
    assert ev_row(conn, "Last Sat")["linked_round_id"] is None
    conn.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode) VALUES ('r1', '18b_export', '2026-04-11', 'total_only')")
    assert link_rounds(conn) == 1 and link_rounds(conn) == 0
    assert ev_row(conn, "Last Sat")["linked_round_id"] == "r1"


# ------------------------------------------------------------------ helpers, quick-add, manual yaml
def test_event_id_is_stable_and_excerpt_verification():
    a = event_id("an-1", "lesson", "L004: Pump  drill\n3x10", 0)
    assert a == event_id("an-1", "lesson", "pump drill 3x10", 0) != event_id("an-1", "lesson", "pump drill 3x10", 1)
    text = "Practice log\n12/30 range, driver only,\n  fixing slice"
    v = verify_excerpt(text, "12/30 range, driver only, fixing slice")
    assert (v.verified, v.method, v.line_start, v.line_end) == (True, "exact", 2, 3)
    fuzzy = verify_excerpt(text, "12/30 range driver only fixing slice")
    assert fuzzy.verified and fuzzy.method == "fuzzy"
    assert not verify_excerpt(text, "bought a new putter today").verified


def test_prompt_example_matches_schema():
    prompt = (Path(__file__).parents[1] / "golf" / "notes" / "prompts" / "notes_tl1.md").read_text()
    example = re.search(r"<example_output>\s*(.*?)\s*</example_output>", prompt, re.S).group(1)
    assert len(NoteExtraction.model_validate(json.loads(example)).events) == 4


def test_note_template_help_has_the_shortcut_recipe():
    assert "Append to Note" in NOTE_TEMPLATE_HELP and "yyyy-MM-dd" in NOTE_TEMPLATE_HELP
    assert "2026-09-25 — lesson w/ Mike" in NOTE_TEMPLATE_HELP


def test_manual_add_with_type_skips_llm(conn, cfg, fake_llm):
    (Path(cfg.root) / "entities.yaml").write_text(ENTITIES)
    out = add_manual_event(conn, cfg, "lesson w/ MR, pump drill", date="2026-09-20", event_type="lesson",
                           coach="MR", focus="full swing, putting")
    assert not fake_llm.requests and not out["used_llm"]
    ev_ = out["events"][0]
    assert (ev_["status"], ev_["date"], ev_["coach"], ev_["game_areas"], ev_["date_source"]) == \
        ("auto_accepted", "2026-09-20", "Mike Rossi", "full_swing,putting", "manual")
    assert [e["event_id"] for e in timeline(conn)] == [ev_["event_id"]]
    assert add_manual_event(conn, cfg, "range", event_type="practice")["doc_id"] == "man-2"
    with pytest.raises(ValueError):
        add_manual_event(conn, cfg, "x", event_type="lesson", focus="chipping yips")


def test_manual_add_runs_tl1_anchored_at_the_date(conn, cfg, router):
    router.route("pump drill again", extraction("single_entry", "na", [], [
        ev("lesson", "lesson w/ Mike yesterday, pump drill again", 1,
           dref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="yesterday"),
           coach="Mike Rossi")]))
    out = add_manual_event(conn, cfg, "lesson w/ Mike yesterday, pump drill again", date="2026-09-20")
    assert out["used_llm"] and len(out["events"]) == 1
    e = out["events"][0]
    assert (e["status"], e["date"], e["event_type"]) == ("auto_accepted", "2026-09-19", "lesson")
    assert "entry date" in router.requests[0]["messages"][0]["content"][0]["text"]


def test_manual_add_without_llm_stores_other(conn, cfg, monkeypatch):
    def no_key(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")
    monkeypatch.setattr(llm, "SEND", no_key)
    out = add_manual_event(conn, cfg, "new grips on all irons", date="2026-09-18")
    assert not out["used_llm"] and out["llm_error"]
    e = out["events"][0]
    assert (e["event_type"], e["date"], e["status"]) == ("other", "2026-09-18", "auto_accepted")
    assert timeline(conn)[0]["event_id"] == e["event_id"]


def test_load_manual_yaml_is_idempotent(conn, tmp_path):
    path = tmp_path / "manual_events.yaml"
    path.write_text(
        "- {date: 2024-05, type: fitting, summary: Iron fitting, equipment: [{action: fitted, category: iron_set, model: T200}]}\n"
        "- {date: 2024-06-02, type: lesson, summary: First lesson with Mike, coach: Mike Rossi}\n"
        "- {date: 2023, type: injury, summary: Sore wrist all season}\n")
    first = load_manual_yaml(conn, path)
    assert (first["entries"], first["new"]) == (3, 3)
    rows = {r["event_type"]: r for r in timeline(conn)}
    assert (rows["fitting"]["date_start"], rows["fitting"]["date_end"], rows["fitting"]["date_precision"]) == \
        ("2024-05-01", "2024-05-31", "month")
    assert rows["lesson"]["date"] == "2024-06-02" and rows["injury"]["date_precision"] == "inferred"
    again = load_manual_yaml(conn, path)
    assert (again["new"], again["unchanged"]) == (0, 3)
    path.write_text(path.read_text().splitlines()[0] + "\n")
    assert load_manual_yaml(conn, path)["superseded"] == 2 and len(timeline(conn)) == 1
