"""Notebook photos -> transcription -> events. Every notebook event starts (and stays) pending."""
from __future__ import annotations

import base64
import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from golf.notes import import_notebook_pages, pending_events, review_event, set_auto_accept, timeline
from golf.notes.notebook import MAX_LONG_EDGE, capture_time, parse_page_date

TZ = ZoneInfo("America/New_York")


def dref(**kw):
    base = dict(expression="", kind="none", year=-1, month=-1, day=-1, time_hhmm="", anchor="na", header_line=-1,
                rel_kind="none", rel_value=0, weekday="none", weekday_rel="none", period="none", approximate=False)
    base.update(kw)
    return base


def ev(event_type, excerpt, line_start, line_end, date, *, coach="", reasons=()):
    return {"event_type": event_type, "date": date, "is_planned": False, "coach": coach,
            "lesson_format": "in_person" if event_type == "lesson" else "none", "location": "", "location_kind": "none",
            "duration_minutes": -1, "focus_areas": [], "drills": [], "swing_thoughts": [], "equipment": [],
            "measurements": [], "injury": [], "goal_text": "", "summary": excerpt[:50],
            "source": {"line_start": line_start, "line_end": line_end, "excerpt": excerpt},
            "confidence": "high", "review_reasons": list(reasons)}


def page(transcription, page_date, events, legibility="high", kind="single_entry"):
    return json.dumps({"transcription": transcription, "legibility": legibility, "page_date_text": page_date,
                       "note_kind": kind, "log_order": "na", "entry_headers": [], "events": events})


DATED = page(
    "3/14/19\nLesson w/ Mike @ PH\nshallowing - pump drill 3x10\nfeel: trail elbow [illegible] hip\nnext day range 1 hr",
    "3/14/19",
    [ev("lesson", "Lesson w/ Mike @ PH\nshallowing - pump drill 3x10\nfeel: trail elbow [illegible] hip", 2, 4,
        dref(kind="none"), coach="Mike Rossi"),
     ev("practice", "next day range 1 hr", 5, 5,
        dref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=1, expression="next day"))],
    legibility="medium", kind="mixed")
UNDATED = page("putting - gate drill 20 min\nstart line better", "",
               [ev("practice", "putting - gate drill 20 min", 1, 1, dref(kind="none"))], legibility="low")


@pytest.fixture
def pages(tmp_path):
    """A rotated 3000x2000 phone photo with EXIF capture time, and a PNG without EXIF (file mtime)."""
    photo = tmp_path / "page1.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6                                                   # rotate 90 degrees on display
    exif.get_ifd(0x8769)[36867] = "2026:09:20 14:03:00"
    Image.new("RGB", (3000, 2000), (250, 248, 240)).save(photo, "JPEG", exif=exif.tobytes())
    scan = tmp_path / "page2.png"
    Image.new("RGB", (800, 1000), (255, 255, 255)).save(scan, "PNG")
    ts = datetime(2026, 9, 21, 9, 0, tzinfo=TZ).timestamp()
    os.utime(scan, (ts, ts))
    return photo, scan


def ev_by(conn, needle):
    return dict(conn.execute("SELECT * FROM events WHERE source_excerpt LIKE ?", (f"%{needle}%",)).fetchone())


def test_pages_are_transcribed_and_always_pending(conn, cfg, fake_llm, pages):
    (Path(cfg.root) / "entities.yaml").write_text("coaches:\n  - {name: Mike Rossi, initials: [MR]}\n")
    set_auto_accept(conn, True)                                       # even with auto-accept on
    fake_llm.push(DATED)
    fake_llm.push(UNDATED)
    s = import_notebook_pages(conn, cfg, pages)
    assert (s["new"], s["api_calls"], s["errors"]) == (2, 2, []) and s["cost_usd"] > 0
    assert s["events"]["new"] == 3 and s["events"]["pending"] == 3
    assert conn.execute("SELECT DISTINCT status FROM events").fetchall()[0][0] == "pending"
    assert timeline(conn) == [] and len(pending_events(conn)) == 3

    doc = conn.execute("SELECT * FROM source_docs WHERE doc_id = ?", (s["doc_ids"][0],)).fetchone()
    assert doc["source"] == "notebook" and doc["doc_id"].startswith("nb-") and len(doc["doc_id"]) == 19
    assert doc["text"].startswith("3/14/19\nLesson w/ Mike") and doc["external_id"].endswith("page1.jpg")
    assert doc["created_at"].startswith("2026-09-20T14:03:00")
    assert json.loads(doc["flags"])[0]["code"] == "created_unreliable"
    assert conn.execute("SELECT purpose, prompt_version FROM llm_calls").fetchall()[0][:] == ("notebook_page", "nb-1")


def test_page_date_drives_the_dates(conn, cfg, fake_llm, pages):
    fake_llm.push(DATED)
    import_notebook_pages(conn, cfg, pages[:1])
    lesson, practice = ev_by(conn, "Lesson w/ Mike"), ev_by(conn, "next day")
    assert (lesson["date"], lesson["date_precision"], lesson["date_source"]) == ("2019-03-14", "day", "entry_header")
    assert (lesson["line_start"], lesson["line_end"], lesson["excerpt_verified"]) == (2, 4, 1)
    assert "illegible_text" in lesson["flags"]
    assert (practice["date"], practice["date_precision"]) == ("2019-03-15", "day")
    assert "undated_page" not in practice["flags"] and "far_past" not in practice["flags"]


def test_undated_page_is_inferred_and_flagged(conn, cfg, fake_llm, pages):
    fake_llm.push(UNDATED)
    import_notebook_pages(conn, cfg, pages[1:])
    e = ev_by(conn, "gate drill")
    assert (e["date"], e["date_precision"], e["date_source"]) == ("2026-09-21", "inferred", "note_created")
    codes = [f["code"] for f in json.loads(e["flags"])]
    assert {"undated_page", "created_unreliable", "low_legibility"} <= set(codes)


def test_reimport_is_free_and_force_rereads(conn, cfg, fake_llm, pages):
    fake_llm.push(DATED)
    fake_llm.push(UNDATED)
    import_notebook_pages(conn, cfg, pages)
    s = import_notebook_pages(conn, cfg, pages)
    assert (s["unchanged"], s["api_calls"], len(fake_llm.requests)) == (2, 0, 2)
    fake_llm.push(DATED)
    s = import_notebook_pages(conn, cfg, pages[:1], force=True)
    assert (s["reread"], s["api_calls"], s["events"]["new"], s["events"]["unchanged"]) == (1, 1, 0, 2)
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'notebook'").fetchone()[0] == 2


def test_photo_is_rotated_and_downscaled_before_sending(conn, cfg, fake_llm, pages):
    fake_llm.push(DATED)
    import_notebook_pages(conn, cfg, pages[:1])
    blocks = fake_llm.requests[0]["messages"][0]["content"]
    img_block = next(b for b in blocks if b["type"] == "image")
    sent = Image.open(io.BytesIO(base64.b64decode(img_block["source"]["data"])))
    assert max(sent.size) <= MAX_LONG_EDGE and sent.height > sent.width
    assert "NOT when the page was written" in blocks[-1]["text"]


def test_refused_page_does_not_stop_the_batch(conn, cfg, fake_llm, pages):
    fake_llm.push("", stop_reason="refusal")
    fake_llm.push(UNDATED)
    s = import_notebook_pages(conn, cfg, pages)
    assert len(s["errors"]) == 1 and s["new"] == 1 and s["events"]["new"] == 1


def test_unreadable_file_is_reported(conn, cfg, fake_llm, tmp_path):
    bad = tmp_path / "not_an_image.jpg"
    bad.write_text("nope")
    s = import_notebook_pages(conn, cfg, [bad])
    assert s["errors"] and "could not read image" in s["errors"][0]["error"] and not fake_llm.requests


def test_accepted_notebook_event_reaches_the_timeline(conn, cfg, fake_llm, pages):
    fake_llm.push(DATED)
    import_notebook_pages(conn, cfg, pages[:1])
    review_event(conn, ev_by(conn, "Lesson w/ Mike")["event_id"], "accept")
    assert [e["date"] for e in timeline(conn)] == ["2019-03-14"]
    assert timeline(conn)[0]["doc_source"] == "notebook"


def test_capture_time_prefers_exif(pages):
    assert capture_time(pages[0], "America/New_York") == datetime(2026, 9, 20, 14, 3, tzinfo=TZ)
    assert capture_time(pages[1], "America/New_York") == datetime(2026, 9, 21, 9, 0, tzinfo=TZ)


@pytest.mark.parametrize("text,want", [
    ("3/14/19", ("2019-03-14", "2019-03-14", "day", ())),
    ("March 2019", ("2019-03-01", "2019-03-31", "month", ())),
    ("Tues 3/14", ("2026-03-14", "2026-03-14", "day", ("year_inferred", "created_unreliable"))),
])
def test_parse_page_date(text, want):
    r = parse_page_date(text, datetime(2026, 9, 20, 14, 3, tzinfo=TZ))
    assert (r.date_start.isoformat(), r.date_end.isoformat(), r.precision, r.flags) == want
    assert parse_page_date("", datetime(2026, 9, 20, tzinfo=TZ)) is None
