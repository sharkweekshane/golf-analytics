"""End to end through the CLI, offline: synthetic 18Birdies archive -> import -> courses sync -> recompute ->
Apple Notes sync (fake osascript runner + fake_llm) -> review -> build -> the dashboard HTML shows the
rounds and the lesson, and the read-only MCP tools see the same data."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import golf.cli as cli
import golf.notes.applenotes as applenotes
from golf import mcp_server
from golf.config import PROJECT_ROOT

LESSON_LINE = "2025-07-20 lesson w/ Mike at Pine Hill: ball position back an inch for irons"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def no_osascript(args):
        raise AssertionError("unexpected osascript call")

    monkeypatch.setattr(applenotes, "_default_runner", no_osascript)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def run(*args, input: str | None = None) -> str:
    result = runner.invoke(cli.app, [str(a) for a in args], input=input)
    assert result.exit_code == 0, f"{args}: exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result.output


class FakeNotes:
    """notes_dump.js stand-in: one iCloud 'Golf' folder with one lesson note."""

    note = {"id": "x-coredata://E2E/ICNote/1", "name": "Lesson w/ Mike", "created": "2025-07-20T22:00:00.000Z",
            "modified": "2025-07-20T22:05:00.000Z", "locked": False, "folder": "Golf"}
    text = f"Lesson w/ Mike\n{LESSON_LINE}\nFeel: trail elbow in front of the hip."

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[4] == "meta":
            payload = {"ok": True, "accounts": ["iCloud"], "folders": [
                {"id": "f1", "name": "Golf", "account": "iCloud", "subfolders": [], "notes": [self.note]}]}
        else:
            payload = {"ok": True, "bodies": [{"id": i, "plaintext": self.text} for i in args[6:]]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def lesson_extraction() -> str:
    date = {"expression": "2025-07-20", "kind": "absolute", "year": 2025, "month": 7, "day": 20, "time_hhmm": "",
            "anchor": "na", "header_line": -1, "rel_kind": "none", "rel_value": 0, "weekday": "none",
            "weekday_rel": "none", "period": "none", "approximate": False}
    event = {"event_type": "lesson", "date": date, "is_planned": False, "coach": "Mike",
             "lesson_format": "in_person", "location": "Pine Hill", "location_kind": "range", "duration_minutes": -1,
             "focus_areas": [{"game_area": "full_swing", "detail": "ball position"}], "drills": [],
             "swing_thoughts": ["trail elbow in front of the hip"], "equipment": [], "measurements": [], "injury": [],
             "goal_text": "", "summary": "Lesson with Mike on ball position for irons.",
             "source": {"line_start": 2, "line_end": 2, "excerpt": LESSON_LINE}, "confidence": "high",
             "review_reasons": []}
    return json.dumps({"note_kind": "single_entry", "log_order": "na", "entry_headers": [], "events": [event]})


def dashboard_json(html: str) -> dict:
    blob = re.search(r'<script type="application/json" id="golf-data">(.*?)</script>', html, re.DOTALL).group(1)
    return json.loads(blob)


def test_archive_to_dashboard_end_to_end(cfg, fake_llm, monkeypatch, tmp_path):
    from fixtures.synthetic_archive import CASES, make_archive, write_archive

    root = Path(cfg.root)
    shutil.copyfile(PROJECT_ROOT / "courses.example.yaml", root / "courses.yaml")
    archive = write_archive(tmp_path / "Downloads" / "18Birdies_archive_20251201.json", make_archive())

    # 1. export -> rounds (+ courses sync + WHS inside the pipeline). The numbers below are the 18-hole-only
    #    index; 9-hole rounds count by default, and the choice sticks for every later step.
    assert "9-hole rounds left out" in run("recompute", "--no-include-nine")
    out = run("import-18b", archive, "--no-sync")
    assert "Courses:" not in out
    out = run("courses", "sync")
    assert "Rounds mapped" in out and "index 9.2" in out
    assert "index 9.2" in run("recompute")

    # 2. Apple Notes -> one pending lesson event (auto-accept is off by default) -> accepted in review
    fake = FakeNotes()
    monkeypatch.setattr(cli, "NOTES_RUNNER", fake)
    fake_llm.push(lesson_extraction())
    out = run("notes", "sync")
    assert "1 new" in out and "1 need review" in out and len(fake_llm.requests) == 1
    assert "0 call(s)" in run("notes", "sync")                                 # unchanged note: no API call
    review = run("review", "events", input="a\n")
    assert LESSON_LINE in review and "saved." in review

    # 3. build -> the dashboard carries the rounds and the lesson
    out = run("build")
    html = (cfg.site_dir / "index.html").read_text()
    assert "http://" not in html and "https://" not in html                    # self-contained, no CDN
    data = dashboard_json(html)
    ids = {r["id"] for r in data["rounds"]}
    assert "syn-001" in ids and CASES["abandoned"] not in ids
    assert not data["is_demo"] and data["metric"]["key"] == "differential"
    lessons = [e for e in data["events"] if e["type"] == "lesson"]
    assert [e["date"] for e in lessons] == ["2025-07-20"] and lessons[0]["coach"] == "Mike"
    effect = data["lessons"]["lessons"][0]
    assert effect["date"] == "2025-07-20" and effect["n_before"] == 5 and effect["n_after"] == 5
    assert data["lessons"]["mde_sentence"]
    assert data["kpis"] and data["hero"]["points"]
    assert "fake.golfer" not in html and "Imaginary Friend" not in html          # archive PII never reaches the page

    # 3b. the public build (GitHub Pages) passes the privacy gate and still shows the golf and the lesson
    out = run("publish", "--dry-run")
    assert "passed the privacy check" in out
    public = (cfg.site_dir / "public" / "index.html").read_text()
    pdata = dashboard_json(public)
    assert pdata["public"] is True and len(pdata["rounds"]) == len(ids)
    assert {r["id"] for r in pdata["rounds"]} == {f"r{i}" for i in range(1, len(ids) + 1)}   # no 18Birdies ids
    assert not any(i in public for i in ids)
    assert [e["date"] for e in pdata["events"] if e["type"] == "lesson"] == ["2025-07-20"]
    assert str(root) not in public and "cost_usd" not in public and "/Users/" not in public

    # 4. status and the read-only MCP view agree
    status = run("status")
    assert "Handicap index (unofficial): 9.2" in status and "0 events" in status
    ro = mcp_server.open_readonly(cfg.db_path)
    try:
        assert mcp_server.timeline_events(ro, event_type="lesson")["events"][0]["date"] == "2025-07-20"
        assert mcp_server.lesson_effects(ro)["lessons"][0]["n_before"] == 5
        assert mcp_server.query_sql(ro, "SELECT COUNT(*) FROM handicap_history")["rows"][0][0] == 18
    finally:
        ro.close()
