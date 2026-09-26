"""Every R1-R7 / W1-W4 rule (positive and negative), the auto-accept rule, and re-read bookkeeping.

Fixtures are synthetic: a fictional 18-hole course and round (tests/fixtures/rounds)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from golf.db import upsert
from golf.extract import validate as V
from golf.schemas.rounds import CellReread, ExtractedRound

FIX = Path(__file__).parent / "fixtures" / "rounds"
CLEAN = json.loads((FIX / "clean_18.json").read_text())
EXPORT = json.loads((FIX / "export_round.json").read_text())


def data() -> dict:
    return copy.deepcopy(CLEAN)


def hole(d: dict, n: int) -> dict:
    return next(h for h in d["holes"] if h["hole"] == n)


def ex(d: dict) -> ExtractedRound:
    return ExtractedRound.model_validate(d)


def seed_export(conn, *, strokes=None, **overrides):
    upsert(conn, "clubs", EXPORT["club"], ["club_id"])
    upsert(conn, "rounds", {**EXPORT["round"], **overrides}, ["round_id"])
    for n, s in enumerate(strokes or EXPORT["hole_strokes"], start=1):
        if s:
            upsert(conn, "round_holes", {"round_id": "r-1001", "hole": n, "strokes": s, "strokes_src": "18b_export"},
                   ["round_id", "hole"])
    return conn.execute("SELECT * FROM rounds WHERE round_id = 'r-1001'").fetchone()


def tee_holes(**changes) -> dict[int, dict]:
    t = {h["hole"]: {"hole": h["hole"], "par": h["par"], "si": h["si"]} for h in CLEAN["holes"]}
    for n, par in changes.items():
        t[int(n[1:])]["par"] = par
    return t


def run(conn, d, round_row=None, tees=None, **kw):
    return V.validate_extraction(conn, ex(d), round_row, tees, player_name="Shane", **kw)


def keys(flags, code=None):
    return {(f["code"], f["hole"], f["field"]) for f in flags if code is None or f["code"] == code}


def test_clean_round_with_export_and_course_data_has_no_flags(conn):
    rr = seed_export(conn)
    flags = run(conn, data(), rr, tee_holes())
    assert flags == []
    assert V.auto_accept(flags, ex(data()))


# ------------------------------------------------------------------ R1
def test_r1_hole_strokes_must_match_export(conn):
    rr = seed_export(conn)
    d = data()
    hole(d, 5)["strokes"], hole(d, 5)["symbol"] = 6, "double_square"
    assert keys(run(conn, d, rr), "R1") == {("R1", 5, "strokes")}
    assert all(f["severity"] == "E" for f in run(conn, d, rr) if f["code"] == "R1")


def test_r1_flags_a_score_on_a_hole_the_export_did_not_play(conn):
    rr = seed_export(conn, strokes=EXPORT["hole_strokes"][:17] + [0])
    assert ("R1", 18, "strokes") in keys(run(conn, data(), rr))


def test_r1_without_export_uses_printed_totals_as_the_checksum(conn):
    d = data()
    d["displayed"]["front"] = 40
    d["displayed"]["gross"] = 82
    flags = run(conn, d)
    assert {(f["code"], f["severity"]) for f in flags} == {("R1", "E")}
    assert len(flags) == 2
    assert run(conn, data()) == []


def test_w5_screenshot_scores_with_nothing_to_check_them(conn):
    d = data()
    d["displayed"].update(gross=-1, front=-1, back=-1)
    assert keys(run(conn, d)) == {("W5", None, "strokes")}
    d["displayed"]["back"] = 42                                        # one printed nine is a check
    assert run(conn, d) == []
    d["displayed"]["back"] = -1
    assert run(conn, d, seed_export(conn)) == []                      # the export is the check


def test_printed_totals_are_only_a_warning_when_the_export_is_the_checksum(conn):
    rr = seed_export(conn)
    d = data()
    d["displayed"]["front"] = 40
    assert keys(run(conn, d, rr)) == {("W4", None, "strokes")}


# ------------------------------------------------------------------ R2
def test_r2_putts_fairways_and_gir_must_match_export_totals(conn):
    rr = seed_export(conn)
    d = data()
    hole(d, 6)["putts"] = 2                                   # Σ putts 37
    hole(d, 1)["fairway"] = "right"                           # hit 8, right 3
    hole(d, 2)["gir"], hole(d, 2)["gir_miss"] = "miss", "left"  # gir 6, left 3
    got = [f for f in run(conn, d, rr) if f["code"] == "R2"]
    assert {f["field"] for f in got} == {"putts", "fairway", "gir", "gir_miss"}
    assert len([f for f in got if f["field"] == "fairway"]) == 2
    assert all(f["severity"] == "E" and f["hole"] is None for f in got)


def test_r2_skips_stats_the_export_did_not_track(conn):
    rr = seed_export(conn, putts=0, putts_tracked=0, fw_chances=0, gir_chances=0)
    d = data()
    hole(d, 6)["putts"] = 2
    hole(d, 1)["fairway"] = "right"
    assert keys(run(conn, d, rr), "R2") == set()


def test_r2_skips_when_no_stats_were_captured(conn):
    rr = seed_export(conn)
    d = data()
    for h in d["holes"]:
        h.update(stats_visible=False, putts=-1, fairway="not_visible", gir="not_visible", gir_miss="not_visible",
                 chips=-1, sand=-1, penalties=-1)
    d["displayed"].update(total_putts=-1, fairways_hit=-1, gir=-1, penalties=-1)
    assert run(conn, d, rr) == []


def test_r2_mentions_uncaptured_holes(conn):
    rr = seed_export(conn)
    d = data()
    hole(d, 18).update(stats_visible=False, putts=-1)
    msg = next(f["message"] for f in run(conn, d, rr) if f["code"] == "R2" and f["field"] == "putts")
    assert "1 holes' stats were not captured" in msg


# ------------------------------------------------------------------ R3
def test_r3_par_against_course_data(conn):
    flags = run(conn, data(), None, tee_holes(h7=4))
    assert ("R3", 7, "par") in keys(flags)
    assert keys(run(conn, data(), None, None), "R3") == set()


def test_r3_accepts_the_alternate_value_of_a_pair(conn):
    d = data()
    hole(d, 7)["par_alt"] = 4
    assert keys(run(conn, d, None, tee_holes(h7=4)), "R3") == set()


def test_r3_sum_of_par_against_export_strokes_minus_score(conn):
    rr = seed_export(conn)
    d = data()
    hole(d, 1)["par"], hole(d, 1)["symbol"] = 5, "none"
    assert ("R3", None, "par") in keys(run(conn, d, rr))


# ------------------------------------------------------------------ R4
@pytest.mark.parametrize("n, changes, field", [
    (2, {"par": 7}, "par"),
    (2, {"putts": 7}, "putts"),
    (2, {"putts": 4}, "putts"),          # 4 strokes, 4 putts: no stroke left to reach the green
    (2, {"chips": 9}, "chips"),
    (2, {"sand": 7}, "sand"),
    (2, {"penalties": 8}, "penalties"),
    (2, {"putts": -3}, "putts"),
    (2, {"strokes": 0}, "strokes"),
])
def test_r4_ranges(conn, n, changes, field):
    d = data()
    hole(d, n).update(changes)
    assert ("R4", n, field) in keys(run(conn, d))


def test_r4_allows_the_unknown_sentinel(conn):
    d = data()
    hole(d, 2).update(putts=-1, chips=-1, sand=-1, penalties=-1)
    d["displayed"]["total_putts"] = -1
    assert keys(run(conn, d), "R4") == set()


# ------------------------------------------------------------------ R5
def test_r5_fairway_not_applicable_iff_par_3(conn):
    d = data()
    hole(d, 3)["fairway"] = "hit"
    hole(d, 1)["fairway"] = "not_applicable"
    assert keys(run(conn, d), "R5") == {("R5", 3, "fairway"), ("R5", 1, "fairway")}
    d = data()
    hole(d, 3)["fairway"] = "not_recorded"
    assert keys(run(conn, d), "R5") == set()


# ------------------------------------------------------------------ R6
def test_r6_duplicate_and_missing_holes(conn):
    d = data()
    d["holes"] = [h for h in d["holes"] if h["hole"] != 18] + [copy.deepcopy(hole(d, 17))]
    got = keys(run(conn, d), "R6")
    assert ("R6", 17, None) in got and ("R6", 18, None) in got


def test_r6_missing_hole_against_export(conn):
    rr = seed_export(conn)
    d = data()
    d["holes"] = [h for h in d["holes"] if h["hole"] != 12]
    assert ("R6", 12, None) in keys(run(conn, d, rr))


def test_r6_stroke_index_must_be_distinct(conn):
    d = data()
    hole(d, 2)["si"] = 7
    assert keys(run(conn, d), "R6") == {("R6", 1, "si"), ("R6", 2, "si")}
    hole(d, 2)["si"] = 19
    assert ("R6", 2, "si") in keys(run(conn, d))


def test_r6_a_back_nine_alone_is_complete(conn):
    d = data()
    d["holes"] = [h for h in d["holes"] if h["hole"] >= 10]
    d["displayed"].update(gross=42, front=-1, to_par_text="+6", total_putts=19, fairways_hit=4, gir=4, penalties=1)
    assert run(conn, d) == []
    assert ("R6", 1, None) in keys(run(conn, d, expected_holes=18))


def test_r6_blank_columns_of_unplayed_holes_are_fine_but_blank_scores_are_not(conn):
    d = data()
    for h in d["holes"]:
        if h["hole"] >= 10:
            h.update(strokes=-1, symbol="not_visible", putts=-1, fairway="not_recorded", gir="not_recorded",
                     gir_miss="none", chips=-1, sand=-1, penalties=-1)
    d["displayed"].update(gross=41, back=-1, to_par_text="+5", total_putts=17, fairways_hit=5, gir=3, penalties=0)
    assert run(conn, d) == []
    hole(d, 4)["strokes"] = -1
    assert ("R6", 4, "strokes") in keys(run(conn, d))


def test_r6_total_only_has_no_grid(conn):
    d = data()
    d["holes"] = []
    assert keys(run(conn, d), "R6") == {("R6", None, None)}


# ------------------------------------------------------------------ R7
def test_r7_symbol_must_fit_score(conn):
    d = data()
    hole(d, 2)["symbol"] = "square"          # 4 on a par 4
    hole(d, 17)["symbol"] = "max_star"       # never checked
    assert keys(run(conn, d), "R7") == {("R7", 2, "symbol")}
    d = data()
    hole(d, 17).update(strokes=8, putts=3, symbol="double_square")  # +3 is still a double square
    assert keys(run(conn, d), "R7") == set()


# ------------------------------------------------------------------ W1-W4
def test_w1_gir_disagreeing_with_strokes_and_putts_is_a_warning(conn):
    d = data()
    hole(d, 1)["gir"], hole(d, 1)["gir_miss"] = "hit", "none"   # 5 strokes, 2 putts, par 4
    hole(d, 2)["gir"] = "miss"                                   # 4 strokes, 2 putts, par 4
    got = [f for f in run(conn, d) if f["code"] == "W1"]
    assert {(f["hole"], f["severity"]) for f in got} == {(1, "W"), (2, "W")}


def test_w2_stroke_budget(conn):
    d = data()
    hole(d, 1)["chips"] = 3
    assert ("W2", 1, "strokes") in keys(run(conn, d))
    assert keys(run(conn, data()), "W2") == set()


def test_w3_model_uncertainty_issues_and_image_problems(conn):
    d = data()
    hole(d, 4)["uncertain_fields"] = ["putts"]
    hole(d, 5)["confidence"] = "low"
    d["issues"] = ["Images 2 and 3 disagree on hole 9 putts"]
    d["images"][1]["problems"] = "glare over holes 8-9"
    got = keys(run(conn, d), "W3")
    assert got == {("W3", 4, "putts"), ("W3", 5, None), ("W3", None, None)}
    msg = next(f["message"] for f in run(conn, d) if f["hole"] is None and f["code"] == "W3")
    assert "glare" in msg and "disagree" in msg


def test_w4_printed_stat_totals(conn):
    d = data()
    d["displayed"].update(total_putts=35, fairways_hit=8, gir=6, penalties=0, to_par_text="+12")
    assert keys(run(conn, d), "W4") == {("W4", None, "putts"), ("W4", None, "fairway"), ("W4", None, "gir"),
                                        ("W4", None, "penalties"), ("W4", None, "par")}


@pytest.mark.parametrize("text, value", [("E", 0), ("+11", 11), ("-2", -2), ("−3", -3), ("Even", 0), ("", None)])
def test_parse_to_par(text, value):
    assert V.parse_to_par(text) == value


def test_player_name_must_match(conn):
    d = data()
    d["player_name"] = "Alex Example"
    assert keys(run(conn, d)) == {("P1", None, None)}
    d["player_name"] = "Shane S."
    assert run(conn, d) == []
    d["player_name"] = ""
    assert run(conn, d) == []


# ---------------------------------------------------------- auto-accept
def w(hole=None, field=None, **extra):
    return {**V.flag("W3", "W", hole, field, "w"), **extra}


def test_auto_accept_rule():
    clean = ex(data())
    assert V.auto_accept([], clean)
    assert V.auto_accept([w(1, "putts")], clean)
    assert not V.auto_accept([w(1, "putts"), w(2, "putts")], clean)
    assert not V.auto_accept([V.flag("R7", "E", 2, "symbol", "e")], clean)
    assert V.auto_accept([w(1, "putts"), w(2, "putts", cleared=True)], clean)


def test_low_confidence_hole_blocks_auto_accept_until_its_doubts_are_confirmed():
    d = data()
    hole(d, 5).update(confidence="low", uncertain_fields=["putts"])
    low = ex(d)
    assert not V.auto_accept([], low)
    confirmed = [w(5, "putts", cleared=True, reread={"agrees": True, "original": "2", "value": "2"})]
    assert V.auto_accept(confirmed, low)
    hole(d, 5)["uncertain_fields"] = []
    assert not V.auto_accept(confirmed, ex(d))


def test_is_clean_is_the_reviewed_rule():
    assert V.is_clean([w(1, "putts")])
    assert not V.is_clean([w(1, "putts"), w(2, "par")])
    assert not V.is_clean([V.flag("R1", "E", 5, "strokes", "e")])


# --------------------------------------------------------------- re-reads
def cell(hole, field, value, confidence="high"):
    return CellReread(hole=hole, field=field, value=value, confidence=confidence, note="")


@pytest.mark.parametrize("field, value, normed", [
    ("putts", "2", "2"), ("putts", " 02 ", "2"), ("putts", "-", "-1"), ("par", "4/5", "4"), ("putts", "two", ""),
    ("symbol", "Double Square", "double_square"), ("fairway", "not recorded", "not_recorded"), ("gir", "maybe", ""),
])
def test_norm_reading(field, value, normed):
    assert V.norm_reading(field, value) == normed


def test_reread_agreement_rules():
    assert V.reread_agrees(4, "putts", "2", cell(4, "putts", "2"))
    assert not V.reread_agrees(4, "putts", "2", cell(4, "putts", "3"))
    assert not V.reread_agrees(4, "putts", "2", cell(4, "putts", ""))          # unreadable
    assert not V.reread_agrees(4, "putts", "2", cell(4, "putts", "2", "low"))  # too unsure to count
    assert not V.reread_agrees(4, "putts", "2", cell(5, "putts", "2"))         # answered another cell
    assert V.reread_agrees(4, "putts", "-1", cell(4, "putts", "-"))


def test_apply_rereads_clears_warnings_keeps_errors_and_escalates_disagreement():
    d = data()
    hole(d, 5)["strokes"], hole(d, 5)["symbol"] = 6, "double_square"
    e = ex(d)
    flags = [V.flag("R1", "E", 5, "strokes", "Hole 5: read 6 strokes, the export has 5."),
             V.flag("W3", "W", 4, "putts", "unsure"), V.flag("W3", "W", 9, "par", "unsure")]
    rereads = {(5, "strokes"): {"value": "6", "agrees": True, "original": "6"},
               (4, "putts"): {"value": "2", "agrees": True, "original": "2"},
               (9, "par"): {"value": "4", "agrees": False, "original": "5"}}
    out = V.apply_rereads(flags, rereads, e)
    by = {(f["code"], f["hole"]): f for f in out}
    assert not by[("R1", 5)].get("cleared") and "stale" in by[("R1", 5)]["message"]
    assert by[("W3", 4)]["cleared"]
    assert not by[("W3", 9)].get("cleared") and ("RR", 9) in by and by[("RR", 9)]["severity"] == "E"
    assert V.reread_targets(out) == []


def test_rereads_stop_counting_once_the_cell_changes():
    d = data()
    flags = [V.flag("W3", "W", 4, "putts", "unsure")]
    old = V.apply_rereads(copy.deepcopy(flags), {(4, "putts"): {"value": "2", "agrees": True, "original": "2"}}, ex(d))
    hole(d, 4)["putts"] = 3
    new = V.carry_over(old, copy.deepcopy(flags), ex(d))
    assert not new[0].get("cleared") and "reread" not in new[0]


def test_reread_targets_prefer_errors_and_skip_round_level_flags():
    flags = [V.flag("W3", "W", 4, "putts", "w"), V.flag("R2", "E", None, "putts", "e"),
             V.flag("R7", "E", 2, "symbol", "e"), V.flag("W1", "W", 2, "gir", "w"), V.flag("W3", "W", 4, "putts", "dup")]
    assert V.reread_targets(flags) == [(2, "symbol"), (4, "putts"), (2, "gir")]
    assert V.reread_targets(flags, limit=1) == [(2, "symbol")]
