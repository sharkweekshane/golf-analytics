"""Screenshot extraction end to end, offline: fake_llm stands in for the API, PNGs are generated with
Pillow in tmp_path, and every round, course and number is synthetic (tests/fixtures/rounds)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

import golf.llm as llm
from golf.db import upsert
from golf.extract import inbox, rounds
from golf.extract import validate as V
from golf.schemas.rounds import ExtractedRound

FIX = Path(__file__).parent / "fixtures" / "rounds"
CLEAN = json.loads((FIX / "clean_18.json").read_text())
EXPORT = json.loads((FIX / "export_round.json").read_text())
FULL_COST = round((4000 * 5 + 1500 * 25) / 1e6, 6)


# ---------------------------------------------------------------- helpers
def data() -> dict:
    return copy.deepcopy(CLEAN)


def hole(d: dict, n: int) -> dict:
    return next(h for h in d["holes"] if h["hole"] == n)


def make_pngs(folder: Path, n: int = 3, seed: int = 0) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = folder / f"IMG_{seed:02d}{i:02d}.png"
        Image.new("RGB", (60, 120), (40 * i + seed, 90, 200 - 30 * i)).save(p)
        paths.append(p)
    return paths


@pytest.fixture
def shots(cfg) -> list[Path]:
    return make_pngs(cfg.data_dir / "raw" / "screenshots" / "batch")


def seed_export(conn, *, strokes=None, **overrides) -> None:
    upsert(conn, "clubs", EXPORT["club"], ["club_id"])
    upsert(conn, "rounds", {**EXPORT["round"], **overrides}, ["round_id"])
    for n, s in enumerate(strokes or EXPORT["hole_strokes"], start=1):
        upsert(conn, "round_holes", {"round_id": "r-1001", "hole": n, "strokes": s, "strokes_src": "18b_export"},
               ["round_id", "hole"])
    conn.commit()


def push_round(fake_llm, d: dict) -> None:
    fake_llm.push(json.dumps(d))


def push_reread(fake_llm, hole: int, field: str, value: str, confidence: str = "high") -> None:
    fake_llm.push(json.dumps({"hole": hole, "field": field, "value": value, "confidence": confidence, "note": ""}))


def request_text(req: dict) -> str:
    return " ".join(b["text"] for b in req["messages"][0]["content"] if b["type"] == "text")


def request_images(req: dict) -> list[str]:
    return [b["source"]["data"] for b in req["messages"][0]["content"] if b["type"] == "image"]


def holes_of(conn, round_id: str) -> dict[int, dict]:
    return {r["hole"]: dict(r) for r in conn.execute("SELECT * FROM round_holes WHERE round_id = ?", (round_id,))}


def xrow(conn, xid: int) -> dict:
    return dict(conn.execute("SELECT * FROM extractions WHERE extraction_id = ?", (xid,)).fetchone())


# ---------------------------------------------------------------- prompts
def test_prompts_splice_the_provisional_screen_guide():
    assert rounds.PROMPT_VERSION == "round-v1"
    system = rounds.system_prompt()
    assert "{{SCREEN_GUIDE}}" not in system and "# Screen guide" in system and "<!--" not in system
    assert "never change a value you read to make the totals match" in system.lower()
    guide = (rounds.PROMPTS_DIR / "screen_guide_18birdies.md").read_text()
    assert guide.startswith("<!-- PROVISIONAL")


# ------------------------------------------------------------- extraction
def test_export_round_is_matched_validated_and_auto_accepted(conn, cfg, fake_llm, shots):
    seed_export(conn)
    push_round(fake_llm, data())
    out = rounds.extract_round(conn, cfg, shots)

    assert out == {"extraction_id": out["extraction_id"], "status": "auto_accepted", "round_id": "r-1001",
                   "flags": [], "cost_usd": FULL_COST, "cached": False}
    hs = holes_of(conn, "r-1001")
    assert {h["strokes_src"] for h in hs.values()} == {"18b_export"}
    assert {h["stats_src"] for h in hs.values()} == {"screenshot"}
    assert sum(h["putts"] for h in hs.values()) == 36
    assert hs[3]["fairway"] == "not_applicable" and hs[17]["gir"] == 0 and hs[17]["gir_miss"] == "no_chance"
    assert hs[4]["par"] == 5 and hs[4]["si"] == 1

    row = xrow(conn, out["extraction_id"])
    assert json.loads(row["image_paths"]) == [f"raw/screenshots/batch/{p.name}" for p in shots]
    assert json.loads(row["image_sha256s"]) == [hashlib.sha256(p.read_bytes()).hexdigest() for p in shots]
    assert row["call_id"] and json.loads(row["result_json"])["player_name"] == "Shane"
    assert conn.execute("SELECT kind FROM imports").fetchone()[0] == "screenshot"


def test_request_carries_images_and_hints_but_never_export_totals(conn, cfg, fake_llm, shots):
    seed_export(conn)
    push_round(fake_llm, data())
    rounds.extract_round(conn, cfg, shots, expected_holes=18, played_on="2026-09-20", club_id="club-maple")
    req = fake_llm.requests[0]
    assert req["system"][0]["text"] == rounds.system_prompt()
    assert len(request_images(req)) == 3
    text = request_text(req)
    assert "Image 1:" in text and "Image 3:" in text
    assert "Shane" in text and "2026-09-20" in text and "Maple Ridge Golf Club" in text and "18" in text
    for secret in ("83", "36", "41", "42", "+11"):
        assert secret not in text.replace("2026-09-20", "")


def test_rerun_is_served_from_cache(conn, cfg, fake_llm, shots):
    seed_export(conn)
    push_round(fake_llm, data())
    first = rounds.extract_round(conn, cfg, shots)
    again = rounds.extract_round(conn, cfg, list(reversed(shots)))       # same image set, any order
    assert again["cached"] and again["cost_usd"] == 0 and again["extraction_id"] == first["extraction_id"]
    assert len(fake_llm.requests) == 1

    conn.execute("DELETE FROM extractions")                              # extraction lost, API response cached
    fresh = rounds.extract_round(conn, cfg, shots)
    assert fresh["cached"] and fresh["status"] == "auto_accepted" and len(fake_llm.requests) == 1

    push_round(fake_llm, data())
    forced = rounds.extract_round(conn, cfg, shots, force=True)
    assert not forced["cached"] and len(fake_llm.requests) == 2
    assert forced["extraction_id"] == fresh["extraction_id"]


def test_round_seen_only_in_screenshots_becomes_a_screenshot_round(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    out = rounds.extract_round(conn, cfg, shots)
    shas = [hashlib.sha256(p.read_bytes()).hexdigest() for p in shots]
    rid = "ss-" + hashlib.sha256("\n".join(sorted(shas)).encode()).hexdigest()[:16]
    assert out["status"] == "auto_accepted" and out["round_id"] == rid == rounds.screenshot_round_id(shas)

    r = dict(conn.execute("SELECT * FROM rounds WHERE round_id = ?", (rid,)).fetchone())
    assert (r["source"], r["played_on_local"], r["entry_mode"], r["nine"]) == ("screenshot", "2026-09-20", "hole_by_hole", None)
    assert (r["gross"], r["to_par"], r["par_played"], r["holes_played"]) == (83, 11, 72, 18)
    assert (r["putts"], r["putts_tracked"], r["fw_hit"], r["fw_left"], r["fw_right"], r["fw_chances"]) == (36, 1, 9, 3, 2, 14)
    assert (r["gir"], r["gir_short"], r["gir_chances"]) == (7, 4, 18)
    assert (r["eagles_plus"], r["birdies"], r["pars"], r["bogeys"], r["dbl_plus"]) == (0, 0, 8, 9, 1)
    assert r["first_import_id"] is not None
    raw = json.loads(r["raw"])
    assert "player_name" not in raw and "other_players" not in raw and raw["course_text"] == "Maple Ridge GC"
    hs = holes_of(conn, rid)
    assert len(hs) == 18 and {h["strokes_src"] for h in hs.values()} == {"screenshot"}


def test_front_nine_screenshot_round(conn, cfg, fake_llm, shots):
    d = data()
    d["holes"] = [h for h in d["holes"] if h["hole"] <= 9]
    d["displayed"].update(gross=41, back=-1, to_par_text="+5", total_putts=17, fairways_hit=5, gir=3, penalties=0)
    push_round(fake_llm, d)
    out = rounds.extract_round(conn, cfg, shots)
    r = conn.execute("SELECT entry_mode, nine, gross, holes_played FROM rounds WHERE round_id = ?", (out["round_id"],)).fetchone()
    assert out["status"] == "auto_accepted" and tuple(r) == ("hole_by_hole", "front", 41, 9)


def test_match_by_date_and_strokes_when_club_is_unknown(conn, cfg, fake_llm, shots):
    seed_export(conn)
    d = data()
    d["course_text"] = ""
    push_round(fake_llm, d)
    assert rounds.extract_round(conn, cfg, shots)["round_id"] == "r-1001"


def test_match_round_rules(conn):
    seed_export(conn)
    e = ExtractedRound.model_validate(data())
    assert rounds.match_round(conn, None, e, {}) == "r-1001"                                  # fuzzy club name
    assert rounds.match_round(conn, None, e, {"club_id": "club-maple"}) == "r-1001"
    assert rounds.match_round(conn, None, e, {"club_id": "club-elsewhere"}) is None           # different club
    assert rounds.match_round(conn, None, e, {"played_on": "2026-09-21"}) is None             # different day
    d = data()
    for n in (1, 2, 4):                                                                       # 3 holes differ
        hole(d, n)["strokes"] += 1
    assert rounds.match_round(conn, None, ExtractedRound.model_validate(d), {}) is None
    d = data()
    hole(d, 1)["strokes"] += 1                                                                # a single misread
    assert rounds.match_round(conn, None, ExtractedRound.model_validate(d), {}) == "r-1001"


def test_same_day_same_club_with_different_scores_is_flagged_not_matched(conn, cfg, fake_llm, shots):
    strokes = list(EXPORT["hole_strokes"])
    for i in (0, 1, 3, 4):
        strokes[i] += 1
    seed_export(conn, strokes=strokes)
    push_round(fake_llm, data())
    out = rounds.extract_round(conn, cfg, shots)
    assert out["round_id"].startswith("ss-") and out["status"] == "needs_review"
    assert [f["code"] for f in out["flags"]] == ["M1"]
    assert conn.execute("SELECT COUNT(*) FROM rounds WHERE source = 'screenshot'").fetchone()[0] == 0
    with pytest.raises(ValueError):
        rounds.accept_extraction(conn, out["extraction_id"])
    kept = rounds.accept_extraction(conn, out["extraction_id"], force=True)
    assert kept["status"] == "accepted"
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 2


# ----------------------------------------------------------------- re-read
def test_export_mismatch_is_reread_and_stays_an_error_when_confirmed(conn, cfg, fake_llm, shots):
    strokes = list(EXPORT["hole_strokes"])
    strokes[4] = 4                                                   # export says 4 on hole 5; screen shows 5
    seed_export(conn, strokes=strokes)
    push_round(fake_llm, data())
    push_reread(fake_llm, 5, "strokes", "5")
    out = rounds.extract_round(conn, cfg, shots)

    assert out["status"] == "needs_review" and out["round_id"] == "r-1001"
    r1 = next(f for f in out["flags"] if f["code"] == "R1")
    assert r1["reread"]["agrees"] and "stale" in r1["message"]
    assert out["cost_usd"] == pytest.approx(2 * FULL_COST)
    req = fake_llm.requests[1]
    assert len(request_images(req)) == 1 and "hole 5" in request_text(req)
    assert req["system"][0]["text"] == rounds.reread_system_prompt()
    assert "5 strokes" not in request_text(req)                     # the first reading is never shown
    assert holes_of(conn, "r-1001")[5]["stats_src"] is None          # nothing applied while in review


def test_agreeing_rereads_clear_warnings_and_auto_accept(conn, cfg, fake_llm, shots):
    seed_export(conn)
    d = data()
    hole(d, 4)["uncertain_fields"] = ["putts"]
    hole(d, 9)["uncertain_fields"] = ["par"]
    push_round(fake_llm, d)
    push_reread(fake_llm, 4, "putts", "2")
    push_reread(fake_llm, 9, "par", "5")
    out = rounds.extract_round(conn, cfg, shots)

    assert out["status"] == "auto_accepted" and out["flags"] == []
    stats_image = llm.load_image(shots[1]).data_b64                  # putts come from the Stats view image
    assert request_images(fake_llm.requests[1]) == [stats_image]
    cleared = [f for f in json.loads(xrow(conn, out["extraction_id"])["flags"]) if f.get("cleared")]
    assert {(f["hole"], f["field"]) for f in cleared} == {(4, "putts"), (9, "par")}


def test_disagreeing_reread_goes_to_review_with_both_readings(conn, cfg, fake_llm, shots):
    seed_export(conn)
    d = data()
    hole(d, 4)["uncertain_fields"] = ["putts"]
    hole(d, 9)["uncertain_fields"] = ["par"]
    push_round(fake_llm, d)
    push_reread(fake_llm, 4, "putts", "3")
    push_reread(fake_llm, 9, "par", "5")
    out = rounds.extract_round(conn, cfg, shots)
    assert out["status"] == "needs_review"
    assert {(f["code"], f["hole"]) for f in out["flags"]} == {("W3", 4), ("RR", 4)}
    rr = next(f for f in out["flags"] if f["code"] == "RR")
    assert "'3'" in rr["message"] and "'2'" in rr["message"]


def test_reread_cells_can_be_called_directly_and_is_cached(conn, cfg, fake_llm, shots):
    seed_export(conn)
    push_round(fake_llm, data())
    xid = rounds.extract_round(conn, cfg, shots)["extraction_id"]
    push_reread(fake_llm, 7, "putts", "2")
    res = rounds.reread_cells(conn, cfg, xid, [{"hole": 7, "field": "putts"}, (99, "putts")])
    assert [(r["hole"], r["value"], r["agrees"], r["cached"]) for r in res] == [(7, "2", True, False)]
    again = rounds.reread_cells(conn, cfg, xid, [(7, "putts")])
    assert again[0]["cached"] and len(fake_llm.requests) == 2


# ------------------------------------------------------------------ review
def test_review_corrections_are_recorded_and_accept_when_clean(conn, cfg, fake_llm, shots):
    seed_export(conn)
    d = data()
    hole(d, 7)["putts"] = 3                                          # misread: 4 strokes on a par 3, really 2 putts
    push_round(fake_llm, d)
    push_reread(fake_llm, 7, "gir", "miss")                          # W1 (GIR vs putts) is re-read first,
    push_reread(fake_llm, 7, "strokes", "4")                         # then W2 (stroke budget)
    out = rounds.extract_round(conn, cfg, shots)
    xid = out["extraction_id"]
    assert out["status"] == "needs_review"
    assert {f["code"] for f in out["flags"]} == {"R2", "W4"}

    pending = rounds.pending_round_reviews(conn)
    assert [(p["extraction_id"], p["n_errors"], p["played_on"], p["round_source"]) for p in pending] == \
        [(xid, 1, "2026-09-20", "18b_export")]

    review = rounds.get_review(conn, xid)
    assert review["image_paths"] == [f"raw/screenshots/batch/{p.name}" for p in shots]
    assert rounds.image_path(cfg, review["image_paths"][0]) == shots[0]
    assert ":putts" in review["flags_by_cell"] and V.cell_key(None, "putts") == ":putts"
    json.dumps(review)                                               # the payload is JSON-ready
    assert review["holes"][6]["putts"] == 3 and review["holes"][6]["reference_strokes"] == 4
    assert review["fields"] == list(V.HOLE_FIELDS) and "double_square" in review["choices"]["symbol"]
    assert [f["code"] for f in review["cleared_flags"]] == ["W1", "W2"]

    after = rounds.apply_corrections(conn, xid, [{"hole": 7, "field": "putts", "value": "2"}])
    assert after["status"] == "accepted" and after["flags"] == [] and after["corrections_recorded"] == 1
    c = dict(conn.execute("SELECT * FROM corrections").fetchone())
    assert (c["entity"], c["entity_id"], c["field"], c["model_value"], c["corrected_value"]) == \
        ("round_hole", f"{xid}:7", "putts", "3", "2")
    assert c["extraction_id"] == xid and c["call_id"] == xrow(conn, xid)["call_id"]
    assert holes_of(conn, "r-1001")[7]["putts"] == 2 and holes_of(conn, "r-1001")[7]["strokes_src"] == "18b_export"
    assert rounds.pending_round_reviews(conn) == []

    rounds.apply_corrections(conn, xid, [{"hole": 7, "field": "putts", "value": 1}])   # corrected again: one row
    assert conn.execute("SELECT COUNT(*), MAX(corrected_value) FROM corrections").fetchone()[:] == (1, "1")


def test_corrections_reject_bad_values(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    xid = rounds.extract_round(conn, cfg, shots)["extraction_id"]
    for bad in ({"hole": 1, "field": "putts", "value": "two"}, {"hole": 1, "field": "fairway", "value": "sideways"},
                {"hole": 1, "field": "yards", "value": 400}, {"hole": None, "field": "player_name", "value": "x"},
                {"hole": None, "field": "date_iso", "value": "9/20/26"}):
        with pytest.raises(ValueError):
            rounds.apply_corrections(conn, xid, [bad])


def test_missing_date_waits_for_a_correction(conn, cfg, fake_llm, shots):
    d = data()
    d["date_iso"], d["date_text"] = "", ""
    push_round(fake_llm, d)
    out = rounds.extract_round(conn, cfg, shots)
    assert out["round_id"] is None and out["status"] == "needs_review"
    assert [f["code"] for f in out["flags"]] == ["D1"]
    with pytest.raises(ValueError):
        rounds.accept_extraction(conn, out["extraction_id"], force=True)

    fixed = rounds.apply_corrections(conn, out["extraction_id"], [{"hole": None, "field": "date_iso", "value": "2026-09-20"}])
    assert fixed["status"] == "accepted" and fixed["round_id"].startswith("ss-")
    assert conn.execute("SELECT played_on_local FROM rounds WHERE round_id = ?", (fixed["round_id"],)).fetchone()[0] == "2026-09-20"
    c = conn.execute("SELECT entity, model_value, corrected_value FROM corrections").fetchone()
    assert tuple(c) == ("round", "", "2026-09-20")


def test_reject_takes_applied_data_back_out(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    ss = rounds.extract_round(conn, cfg, shots)
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1
    assert rounds.reject_extraction(conn, ss["extraction_id"])["status"] == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM round_holes").fetchone()[0] == 0


def test_reject_on_export_round_clears_only_screenshot_stats(conn, cfg, fake_llm, shots):
    seed_export(conn)
    push_round(fake_llm, data())
    xid = rounds.extract_round(conn, cfg, shots)["extraction_id"]
    rounds.reject_extraction(conn, xid)
    hs = holes_of(conn, "r-1001")
    assert all(h["putts"] is None and h["stats_src"] is None for h in hs.values())
    assert [h["strokes"] for h in hs.values()] == EXPORT["hole_strokes"]


def test_second_extraction_of_the_same_round_fills_the_same_screenshot_round(conn, cfg, fake_llm, shots, tmp_path):
    push_round(fake_llm, data())
    first = rounds.extract_round(conn, cfg, shots)
    scores_view = data()
    for h in scores_view["holes"]:
        h.update(stats_visible=False, putts=-1, fairway="not_visible", gir="not_visible", gir_miss="not_visible",
                 chips=-1, sand=-1, penalties=-1, si=-1)
    scores_view["displayed"].update(total_putts=-1, fairways_hit=-1, gir=-1, penalties=-1)
    push_round(fake_llm, scores_view)
    second = rounds.extract_round(conn, cfg, make_pngs(cfg.data_dir / "raw" / "screenshots" / "other", 2, seed=7))
    assert second["round_id"] == first["round_id"] and second["status"] == "auto_accepted"
    hs = holes_of(conn, first["round_id"])
    assert hs[4]["putts"] == 2 and hs[4]["si"] == 1                    # earlier stats and SI are kept
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1


# ----------------------------------------------------------------- merging
def test_screenshot_round_merges_into_a_later_export(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    ss = rounds.extract_round(conn, cfg, shots)
    assert ss["round_id"].startswith("ss-")
    conn.execute("INSERT INTO round_overrides(round_id, reason) VALUES (?, 'windy')", (ss["round_id"],))

    seed_export(conn)                                                # the export arrives later
    actions = rounds.reconcile_screenshot_rounds(conn)
    assert [a["action"] for a in actions] == ["merged"]
    assert actions[0]["extractions"] == [{"extraction_id": ss["extraction_id"], "status": "auto_accepted"}]
    assert conn.execute("SELECT round_id FROM rounds").fetchall()[0][0] == "r-1001"
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1
    assert xrow(conn, ss["extraction_id"])["round_id"] == "r-1001"
    assert conn.execute("SELECT reason FROM round_overrides WHERE round_id = 'r-1001'").fetchone()[0] == "windy"
    hs = holes_of(conn, "r-1001")
    assert {h["strokes_src"] for h in hs.values()} == {"18b_export"} and sum(h["putts"] for h in hs.values()) == 36
    assert rounds.reconcile_screenshot_rounds(conn) == []


def test_merge_that_breaks_the_export_checksum_goes_back_to_review(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    ss = rounds.extract_round(conn, cfg, shots)
    strokes = list(EXPORT["hole_strokes"])
    strokes[4] = 4                                                   # one hole differs: same round, R1 fires
    seed_export(conn, strokes=strokes)
    actions = rounds.reconcile_screenshot_rounds(conn)
    assert actions[0]["extractions"][0]["status"] == "needs_review"
    hs = holes_of(conn, "r-1001")
    assert hs[5]["strokes"] == 4 and all(h["stats_src"] is None for h in hs.values())
    assert any(f["code"] == "R1" for f in rounds.get_review(conn, ss["extraction_id"])["flags"])


def test_pending_extraction_is_rematched_when_the_export_arrives(conn, cfg, fake_llm, shots):
    d = data()
    hole(d, 4)["uncertain_fields"] = ["putts"]
    hole(d, 9)["uncertain_fields"] = ["par"]
    push_round(fake_llm, d)
    out = rounds.extract_round(conn, cfg, shots, reread=False)
    assert out["status"] == "needs_review" and out["round_id"].startswith("ss-")
    seed_export(conn)
    actions = rounds.reconcile_screenshot_rounds(conn)
    assert actions == [{"action": "rematched", "extraction_id": out["extraction_id"], "round_id": "r-1001",
                        "status": "needs_review"}]


# ------------------------------------------------------------------- inbox
def _touch(p: Path, t: float) -> None:
    os.utime(p, (t, t))


def test_group_inbox_by_folder_and_capture_time(cfg):
    root = cfg.inbox_dir / "screenshots"
    folder = make_pngs(root / "2026-09-20 maple", 2, seed=1)
    loose = make_pngs(root, 3, seed=2)
    t0 = time.mktime((2026, 9, 21, 9, 0, 0, 0, 0, -1))
    _touch(loose[0], t0)
    _touch(loose[1], t0 + 5 * 60)
    _touch(loose[2], t0 + 2 * 3600)
    exif_shot = root / "IMG_exif.png"
    tags = Image.Exif()
    tags.get_ifd(0x8769)[0x9003] = "2026:09:21 09:03:00"            # capture time beats the file clock
    Image.new("RGB", (60, 120), (1, 2, 3)).save(exif_shot, exif=tags.tobytes())
    _touch(exif_shot, t0 + 5 * 3600)
    (root / ".DS_Store").write_text("x")
    (root / "notes.txt").write_text("x")

    groups = inbox.group_inbox(cfg)
    assert [(g["kind"], [p.name for p in g["paths"]]) for g in groups] == [
        ("folder", [p.name for p in folder]),
        ("loose", [loose[0].name, exif_shot.name, loose[1].name]),
        ("loose", [loose[2].name]),
    ]
    assert groups[0]["played_on"] == "2026-09-20" and groups[1]["key"] == "loose-20260921-0900"


def test_process_inbox_extracts_once_and_archives(conn, cfg, fake_llm):
    seed_export(conn)
    folder = cfg.inbox_dir / "screenshots" / "2026-09-20 maple"
    paths = make_pngs(folder, 3, seed=3)
    blobs = [p.read_bytes() for p in paths]

    planned = inbox.process_inbox(conn, cfg, dry_run=True)
    assert [(g["action"], g["played_on"]) for g in planned] == [("extract", "2026-09-20")] and fake_llm.requests == []

    push_round(fake_llm, data())
    done = inbox.process_inbox(conn, cfg)
    assert [g["action"] for g in done] == ["extracted"] and done[0]["outcome"]["status"] == "auto_accepted"
    archived = cfg.raw_dir / "screenshots" / "2026-09-20"
    assert sorted(p.name for p in archived.iterdir()) == sorted(p.name for p in paths)
    assert not folder.exists()
    xid = done[0]["extraction_id"]
    assert json.loads(xrow(conn, xid)["image_paths"]) == [f"raw/screenshots/2026-09-20/{p.name}" for p in paths]
    assert inbox.process_inbox(conn, cfg) == []

    for p, b in zip(paths, blobs):                                  # the same screenshots dropped in again
        (cfg.inbox_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (cfg.inbox_dir / "screenshots" / p.name).write_bytes(b)
    again = inbox.process_inbox(conn, cfg)
    assert [(g["action"], g["extraction_id"]) for g in again] == [("skipped", xid)] and len(fake_llm.requests) == 1
    assert len(list(archived.iterdir())) == 6
    assert json.loads(xrow(conn, xid)["image_paths"]) == [f"raw/screenshots/2026-09-20/{p.name}" for p in paths]


def test_process_inbox_leaves_files_when_the_model_output_is_unusable(conn, cfg, fake_llm):
    paths = make_pngs(cfg.inbox_dir / "screenshots", 2, seed=4)
    fake_llm.push("not json")
    res = inbox.process_inbox(conn, cfg)
    assert res[0]["action"] == "error" and all(p.exists() for p in paths)
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


# ---------------------------------------------------------------- QA regressions
def test_unreadable_image_is_a_clear_error_not_a_crash(conn, cfg, tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    text = tmp_path / "x.png"
    text.write_text("not an image")
    ok = make_pngs(tmp_path / "ok", 1)
    assert [p.name for p, _ in rounds.unreadable_images([empty, text, *ok])] == ["empty.png", "x.png"]
    for bad in (empty, text):
        with pytest.raises(ValueError, match="not a readable image"):
            rounds.extract_round(conn, cfg, [*ok, bad])
        with pytest.raises(ValueError, match="not a readable image"):
            rounds.check_readable([bad])
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_one_unreadable_file_never_blocks_the_inbox(conn, cfg, fake_llm):
    bad_folder = cfg.inbox_dir / "screenshots" / "2026-09-19 bad"
    left = make_pngs(bad_folder, 1, seed=5)
    (bad_folder / "empty.png").write_bytes(b"")
    make_pngs(cfg.inbox_dir / "screenshots" / "2026-09-20 maple", 3, seed=6)
    push_round(fake_llm, data())

    res = {g["key"]: g for g in inbox.process_inbox(conn, cfg)}
    bad = res["2026-09-19 bad"]
    assert bad["action"] == "error" and "empty.png: not a readable image" in bad["error"]
    assert (cfg.inbox_dir / "rejected" / "2026-09-19 bad" / "empty.png").exists()
    assert left[0].exists()                                   # the readable screenshot waits for a retry
    assert res["2026-09-20 maple"]["action"] == "extracted"   # the group after the bad one still ran
    assert [(g["key"], g["action"]) for g in inbox.process_inbox(conn, cfg, dry_run=True)] == [
        ("2026-09-19 bad", "extract")]


def test_an_unexpected_failure_in_one_group_is_reported_and_the_rest_continue(conn, cfg, fake_llm, monkeypatch):
    make_pngs(cfg.inbox_dir / "screenshots" / "2026-09-19 a", 1, seed=7)
    make_pngs(cfg.inbox_dir / "screenshots" / "2026-09-20 b", 2, seed=8)
    real = rounds.extract_round

    def flaky(conn, cfg, paths, **kw):
        if kw.get("played_on") == "2026-09-19":
            raise RuntimeError("disk hiccup")
        return real(conn, cfg, paths, **kw)

    monkeypatch.setattr(rounds, "extract_round", flaky)
    push_round(fake_llm, data())
    res = [(g["key"], g["action"], g.get("error")) for g in inbox.process_inbox(conn, cfg)]
    assert res[0] == ("2026-09-19 a", "error", "RuntimeError: disk hiccup") and res[1][1] == "extracted"


def test_api_outage_still_stops_the_inbox_run(conn, cfg, monkeypatch):
    make_pngs(cfg.inbox_dir / "screenshots" / "2026-09-20 b", 1, seed=9)

    def down(request, use_fallbacks=False):
        raise llm.LLMUnavailable("Claude API unreachable or busy: connection error")

    monkeypatch.setattr(llm, "SEND", down)
    with pytest.raises(llm.LLMUnavailable):
        inbox.process_inbox(conn, cfg)


def test_corrections_to_holes_outside_1_to_18_are_refused(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    xid = rounds.extract_round(conn, cfg, shots)["extraction_id"]
    for h in (0, 19, 25, "x"):
        with pytest.raises(ValueError, match="1-18|1 to 18"):
            rounds.apply_corrections(conn, xid, [{"hole": h, "field": "putts", "value": "2"}])
    assert sorted(h["hole"] for h in rounds.get_review(conn, xid)["holes"]) == list(range(1, 19))


def test_a_date_correction_rematches_an_extraction_already_matched(conn, cfg, fake_llm, shots):
    seed_export(conn)                                           # r-1001, played 2026-09-20
    push_round(fake_llm, data())
    out = rounds.extract_round(conn, cfg, shots)
    xid = out["extraction_id"]
    assert (out["round_id"], out["status"]) == ("r-1001", "auto_accepted")

    moved = rounds.apply_corrections(conn, xid, [{"hole": None, "field": "date_iso", "value": "2026-09-21"}])
    assert moved["round_id"].startswith("ss-") and moved["status"] == "accepted"
    assert rounds.get_review(conn, xid)["played_on"] == "2026-09-21"
    assert conn.execute("SELECT played_on_local FROM rounds WHERE round_id = ?",
                        (moved["round_id"],)).fetchone()[0] == "2026-09-21"
    assert {h["stats_src"] for h in holes_of(conn, "r-1001").values()} == {None}     # taken back out of r-1001

    back = rounds.apply_corrections(conn, xid, [{"hole": None, "field": "date_iso", "value": "2026-09-20"}])
    assert back["round_id"] == "r-1001" and back["status"] == "accepted"
    assert conn.execute("SELECT COUNT(*) FROM rounds WHERE source = 'screenshot'").fetchone()[0] == 0
    assert {h["stats_src"] for h in holes_of(conn, "r-1001").values()} == {"screenshot"}


def test_rerun_with_a_different_date_warns_instead_of_ignoring_it(conn, cfg, fake_llm, shots):
    push_round(fake_llm, data())
    first = rounds.extract_round(conn, cfg, shots)
    again = rounds.extract_round(conn, cfg, shots, played_on="2026-09-22")
    assert again["cached"] and f"golf review fix {first['extraction_id']} - date_iso 2026-09-22" in again["warning"]
    assert "warning" not in rounds.extract_round(conn, cfg, shots, played_on="2026-09-20")
    assert len(fake_llm.requests) == 1
