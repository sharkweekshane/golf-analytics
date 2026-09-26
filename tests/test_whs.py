"""WHS math (2024 Rules of Handicapping): official examples, caps, ESR, NDB, rounding, 9-hole path."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from fixtures.synthetic_archive import CASES, make_archive, write_archive
from golf import whs
from golf.courses import sync_courses
from golf.ingest.birdies_export import import_export

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "courses.example.yaml"


# ------------------------------------------------------------ official examples
def test_initial_index_from_three_scores():
    assert whs.handicap_index([15.3, 15.2, 16.6]) == 13.2


def test_six_scores_lowest_two_minus_one():
    assert whs.handicap_index([40.0, 38.0, 45.1, 38.8, 41.2, 39.9]) == 37.4      # mean(38.0, 38.8) = 38.4


@pytest.mark.parametrize("n, expected", [
    (1, None), (2, None), (3, 8.0), (4, 9.0), (5, 10.0), (6, 9.5), (7, 10.5), (8, 10.5), (9, 11.0),
    (11, 11.0), (12, 11.5), (14, 11.5), (15, 12.0), (16, 12.0), (17, 12.5), (18, 12.5), (19, 13.0), (20, 13.5),
])
def test_fewer_than_20_table(n, expected):
    diffs = [10.0 + i for i in range(n)][::-1]                 # lowest k are 10, 11, ... regardless of order
    assert whs.handicap_index(diffs) == expected


def test_index_uses_most_recent_20_only():
    assert whs.handicap_index([0.0] * 5 + [10.0] * 20) == 10.0


def test_index_capped_at_54():
    assert whs.handicap_index([70.0] * 20) == 54.0


def test_no_096_multiplier():
    assert whs.handicap_index([12.0] * 20) == 12.0


@pytest.mark.parametrize("value, expected", [
    (-1.54, -1.5), (-1.56, -1.6), (-1.55, -1.5), (1.55, 1.6), (1.54, 1.5), (0.05, 0.1), (-0.05, 0.0),
])
def test_rounding_to_tenth(value, expected):
    assert whs.round_tenth(value) == expected


@pytest.mark.parametrize("ags, cr, expected", [(70, 71.54, -1.5), (70, 71.56, -1.6), (70, 71.55, -1.5)])
def test_negative_differentials_round_toward_zero(ags, cr, expected):
    assert whs.score_differential(ags, cr, 113) == expected


def test_score_differential():
    assert whs.score_differential(95, 71.3, 129) == 20.8                # 113/129 x 23.7 = 20.76
    assert whs.score_differential(85, 72.0, 113) == 13.0
    assert whs.score_differential(85, 72.0, 113, pcc=1) == 12.0


def test_nine_hole_official_example():
    assert whs.nine_to_eighteen(7.2, 14.0) == 15.7                     # 7.2 + (0.52 x 14 + 1.2 = 8.48)
    assert whs.expected_nine_differential(14.0) == pytest.approx(8.48)
    assert whs.nine_hole_differential(43, 35.8, 113, 14.0) == 15.7      # 9-hole diff 7.2


def test_nine_hole_uses_unrounded_nine_differential():
    # 9-hole differential 7.25 (unrounded) + 8.48 = 15.73 -> 15.7; rounding 7.25 -> 7.3 first would give 15.8
    assert whs.nine_hole_differential(43, 35.75, 113, 14.0) == 15.7


# --------------------------------------------------------- course handicap / NDB
def test_course_handicap():
    assert whs.course_handicap(14.0, 130, 72.3, 72) == 16               # 16.106 + 0.3 = 16.41
    assert whs.course_handicap(14.0, 113, 72.5, 72) == 15               # 14.5 rounds up
    assert whs.course_handicap(-2.0, 120, 70.0, 72) == -4               # plus handicap: -2.12 - 2
    assert whs.nine_hole_course_handicap(14.0, 125, 35.5, 36) == 7      # 7.0 x 125/113 - 0.5 = 7.24
    assert whs.nine_hole_course_handicap(14.1, 113, 36.0, 36) == 7      # HI/2 = 7.05 -> 7.1 -> 7


@pytest.mark.parametrize("si, ch, expected", [
    (1, 20, 2), (2, 20, 2), (3, 20, 1), (18, 20, 1), (1, 0, 0), (18, 17, 0), (17, 17, 1),
    (4, 40, 3), (5, 40, 2), (18, -2, -1), (17, -2, -1), (16, -2, 0), (1, -18, -1),
])
def test_strokes_received(si, ch, expected):
    assert whs.strokes_received(si, ch) == expected


def test_strokes_received_on_nine():
    assert [whs.strokes_received(r, 4, 9) for r in range(1, 10)] == [1, 1, 1, 1, 0, 0, 0, 0, 0]


def test_net_double_bogey_before_index_is_par_plus_five():
    assert whs.net_double_bogey(4, None) == 9
    assert whs.adjusted_gross_score([(12, 4, 1)] + [(5, 4, si) for si in range(2, 19)], None) == 9 + 85


def test_net_double_bogey_with_index():
    assert whs.net_double_bogey(4, 2) == 8
    assert whs.net_double_bogey(3, 0) == 5
    assert whs.net_double_bogey(4, -1) == 5                             # plus handicap gives a stroke back
    holes = [(12, 4, 1), (9, 4, 17)] + [(5, 4, si) for si in range(2, 17)] + [(5, 4, 18)]
    assert whs.adjusted_gross_score(holes, 16) == 7 + 6 + 80            # SI 1 gets 1 stroke; SI 17 none


def test_net_double_bogey_course_handicap_over_54():
    # CH 60: 3 strokes everywhere, a 4th on SI 1-6. 4+ strokes on a hole caps at par + 5, not par + 6.
    assert whs.net_double_bogey(4, 4, 60) == 9
    assert whs.net_double_bogey(4, 3, 60) == 9
    assert whs.net_double_bogey(4, 4, 54) == 10


def test_ags_on_a_nine_allocates_by_si_rank():
    # Front-nine SIs are the odd numbers; CH9 4 means strokes on SIs 1, 3, 5, 7.
    sis = [7, 3, 17, 1, 11, 5, 15, 13, 9]
    holes = [(10, 4, si) for si in sis]
    capped = sum(7 if si in (1, 3, 5, 7) else 6 for si in sis)
    assert whs.adjusted_gross_score(holes, 4) == capped
    assert whs.si_ranks(sis) == [4, 2, 9, 1, 6, 3, 8, 7, 5]


# -------------------------------------------------------------- ESR and caps
@pytest.mark.parametrize("diff, hi, expected", [
    (12.5, 20.0, -1.0), (13.0, 20.0, -1.0), (10.1, 20.0, -1.0), (10.0, 20.0, -2.0), (5.0, 20.0, -2.0),
    (13.1, 20.0, 0.0), (12.0, None, 0.0),
])
def test_exceptional_score_reduction(diff, hi, expected):
    assert whs.exceptional_score_reduction(diff, hi) == expected


@pytest.mark.parametrize("calc, low, expected", [
    (12.9, 10.0, (12.9, None)), (13.0, 10.0, (13.0, None)), (14.0, 10.0, (13.5, "soft")),
    (14.5, 10.0, (13.8, "soft")), (18.0, 10.0, (15.0, "hard")), (9.0, 10.0, (9.0, None)),
    (20.0, None, (20.0, None)),
])
def test_caps(calc, low, expected):
    assert whs.apply_caps(calc, low) == expected


def test_low_index_window_counts_index_in_effect():
    revs = [(date(2025, 1, 1), 5.0), (date(2025, 6, 1), 8.0), (date(2026, 1, 10), 9.0)]
    assert whs.low_handicap_index(revs, date(2026, 3, 1)) == 5.0      # 5.0 was in effect on 2025-03-01
    assert whs.low_handicap_index(revs, date(2026, 7, 1)) == 8.0
    assert whs.low_handicap_index(revs, date(2026, 1, 10)) == 5.0     # same-day revision excluded
    assert whs.low_handicap_index([], date(2026, 1, 1)) is None


# ------------------------------------------------------------------- recompute
PARS = [4] * 18
TEE = "tc:White:M"


def _course(conn, *, cr=72.0, slope=113):
    conn.execute("INSERT INTO clubs(club_id, name) VALUES ('c1', 'Test Club')")
    conn.execute("INSERT INTO courses(course_key, club_id, name, holes, par) VALUES ('tc', 'c1', 'Test', 18, 72)")
    conn.execute("INSERT INTO tees(tee_id, course_key, name, gender, par, cr18, slope18, cr_f9, slope_f9, "
                 "cr_b9, slope_b9, is_default) VALUES (?, 'tc', 'White', 'M', 72, ?, ?, 36.0, 113, 36.0, 113, 1)",
                 (TEE, cr, slope))
    for hole in range(1, 19):                                     # SI = hole number
        conn.execute("INSERT INTO tee_holes(tee_id, hole, par, si) VALUES (?, ?, 4, ?)", (TEE, hole, hole))
    conn.execute("INSERT INTO tees(tee_id, course_key, name, gender, par, cr18, slope18) "
                 "VALUES ('tc:Blue:M', 'tc', 'Blue', 'M', 72, 72.0, 113)")          # rated, no holes
    conn.execute("INSERT INTO tees(tee_id, course_key, name, gender, par) VALUES ('tc:Gold:M', 'tc', 'Gold', 'M', 72)")


def _round(conn, rid, day, *, strokes=None, gross=None, mode="hole_by_hole", holes=18, nine=None, tee=TEE,
           flags="[]", first_hole=1, course="tc"):
    gross = gross if gross is not None else sum(strokes)
    conn.execute(
        "INSERT INTO rounds(round_id, source, played_at_utc, played_on_local, club_id, course_key, tee_id, "
        "entry_mode, holes_played, nine, gross, to_par, par_played, dq_flags) "
        "VALUES (?, 'manual', ?, ?, 'c1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, f"{day}T15:00:00+00:00", str(day), course, tee, mode, holes, nine, gross, gross - 4 * holes,
         4 * holes, flags))
    for i, s in enumerate(strokes or []):
        conn.execute("INSERT INTO round_holes(round_id, hole, strokes, strokes_src) VALUES (?, ?, ?, 'manual')",
                     (rid, first_hole + i, s))


def _history(conn) -> dict[str, dict]:
    return {r["round_id"]: dict(r) for r in conn.execute("SELECT * FROM handicap_history")}


D0 = date(2026, 3, 1)


def _day(i: int) -> date:
    return D0 + timedelta(days=7 * i)


def test_recompute_ndb_before_and_after_index(conn, cfg):
    _course(conn)
    _round(conn, "r1", _day(0), strokes=[12] + [5] * 17)          # no HI yet: 12 on a par 4 -> 9
    _round(conn, "r2", _day(1), strokes=[5] * 18)
    _round(conn, "r3", _day(2), strokes=[5] * 18)
    _round(conn, "r4", _day(3), strokes=[12] + [5] * 15 + [9, 5])  # CH 16: SI 1 -> 7, SI 17 -> 6
    summary = whs.recompute(conn, cfg)
    h = _history(conn)
    assert (h["r1"]["ags"], h["r1"]["differential"], h["r1"]["hi_before"]) == (94, 22.0, None)
    assert h["r2"]["hi_after"] is None and h["r3"]["hi_after"] == 16.0            # 18.0 - 2.0
    assert (h["r4"]["hi_before"], h["r4"]["ags"], h["r4"]["differential"]) == (16.0, 93, 21.0)
    assert h["r4"]["hi_after"] == 17.0 and h["r4"]["n_scores"] == 4                 # 18.0 - 1.0
    assert all(r["differential_kind"] == "ndb_adjusted" for r in h.values())
    assert summary["handicap_index"] == 17.0 and summary["unofficial"] and summary["pcc"] == 0


def test_recompute_kinds_and_skips(conn, cfg):
    _course(conn)
    _round(conn, "total", _day(0), gross=95, mode="total_only")
    _round(conn, "mismatch", _day(1), strokes=[5] * 18, gross=93, flags='["sum_mismatch"]')
    _round(conn, "no-holes", _day(2), strokes=[12] + [5] * 17, tee="tc:Blue:M")
    _round(conn, "unrated", _day(3), strokes=[5] * 18, tee="tc:Gold:M")
    _round(conn, "no-tee", _day(4), strokes=[5] * 18, tee=None, course=None)
    _round(conn, "default-tee", _day(4), strokes=[5] * 18, tee=None)          # falls back to the default tee
    _round(conn, "front", _day(5), strokes=[5] * 9, holes=9, nine="front")
    _round(conn, "twelve", _day(6), strokes=[5] * 12, holes=12)
    _round(conn, "partial", _day(7), strokes=[5] * 4, holes=4, mode="partial")
    _round(conn, "excluded", _day(8), strokes=[5] * 18)
    _round(conn, "deleted", _day(9), strokes=[5] * 18)
    conn.execute("INSERT INTO round_overrides(round_id, exclude, reason) VALUES ('excluded', 1, 'test')")
    conn.execute("UPDATE rounds SET deleted_in_source = 1 WHERE round_id = 'deleted'")
    s = whs.recompute(conn, cfg)                                   # default: 9-hole scores count
    h = _history(conn)
    assert {k: h[k]["differential_kind"] for k in h} == {
        "total": "gross_upper_bound", "mismatch": "gross_upper_bound", "no-holes": "gross_upper_bound",
        "default-tee": "ndb_adjusted", "front": "nine_hole_scaled"}
    assert h["total"]["ags"] == 95 and h["mismatch"]["ags"] == 93 and h["no-holes"]["ags"] == 97
    assert s["skipped"] == {"tee_unrated": 1, "no_tee": 1, "holes_10_to_17": 1, "partial": 1}
    assert s["rounds_considered"] == 9 and s["scored"] == 5 and s["include_nine"]
    assert s["established_on"] == str(_day(2))                    # 3 x 18 holes
    s = whs.recompute(conn, cfg, include_nine=False)
    assert s["skipped"]["nine_hole_excluded"] == 1 and s["scored"] == 4


def test_recompute_override_tee_wins(conn, cfg):
    _course(conn)
    _round(conn, "r1", _day(0), strokes=[5] * 18, tee="tc:Gold:M")
    conn.execute("INSERT INTO round_overrides(round_id, tee_id) VALUES ('r1', ?)", (TEE,))
    whs.recompute(conn, cfg)
    assert _history(conn)["r1"]["differential_kind"] == "ndb_adjusted"


def test_recompute_nine_holes_by_setting(conn, cfg):
    _course(conn)
    _round(conn, "early-nine", _day(0), strokes=[5] * 9, holes=9, nine="front")
    for i in range(1, 4):
        _round(conn, f"r{i}", _day(i), strokes=[5] * 18)                       # diff 18.0 each
    _round(conn, "front", _day(4), strokes=[5] * 9, holes=9, nine="front")
    _round(conn, "back", _day(5), strokes=[6] * 9, holes=9, nine="back", first_hole=10)
    _round(conn, "unknown", _day(6), strokes=[5] * 9, holes=9, nine="unknown")

    off = whs.recompute(conn, cfg, include_nine=False)
    assert off["skipped"]["nine_hole_excluded"] == 4 and set(_history(conn)) == {"r1", "r2", "r3"}
    assert _history(conn)["r3"]["hi_after"] == 16.0 and off["include_nine_source"] == "explicit"

    s = whs.recompute(conn, cfg)
    h = _history(conn)
    assert s["skipped"] == {"nine_unknown": 1} and s["include_nine_source"] == "setting"
    # The early nine waits (9 + 18 + 18 = 45 holes), then r3 reaches 63 >= 54 and establishes the index
    # with it: 4 scores -> lowest 1 - 1.0. Self-consistent HI 17.0: 9.0 + (0.52 x 17 + 1.2) = 19.0 > 18.0.
    assert (h["early-nine"]["differential"], h["early-nine"]["hi_before"], h["early-nine"]["hi_after"]) == \
        (19.0, None, None)
    assert h["early-nine"]["differential_kind"] == "nine_hole_scaled" and h["early-nine"]["ags"] == 45
    assert h["r2"]["hi_after"] is None and h["r3"]["hi_after"] == 17.0 and s["established_on"] == str(_day(3))
    assert (h["front"]["ags"], h["front"]["differential"]) == (45, 19.0)       # 9.0 + (0.52 x 17 + 1.2)
    # back nine at HI 18.0 -> CH9 9: a stroke on every hole (cap 7), so the 6s stand
    assert h["back"]["ags"] == 54 and h["back"]["hi_before"] == h["front"]["hi_after"] == 18.0
    assert h["back"]["differential"] == 28.6                                    # 18.0 + 9.36 + 1.2


def test_recompute_same_day_rounds_share_the_index_in_effect(conn, cfg):
    _course(conn)
    for i in range(3):
        _round(conn, f"r{i}", _day(i), strokes=[5] * 18)
    _round(conn, "am", _day(3), gross=80, mode="total_only")
    _round(conn, "pm", _day(3), gross=100, mode="total_only")
    whs.recompute(conn, cfg)
    h = _history(conn)
    assert h["am"]["hi_before"] == h["pm"]["hi_before"] == 16.0
    assert h["pm"]["n_scores"] == 5


def test_recompute_exceptional_score_reduction(conn, cfg):
    _course(conn)
    for i in range(3):
        _round(conn, f"r{i}", _day(i), gross=90, mode="total_only")            # diff 18.0 -> HI 16.0
    _round(conn, "great", _day(3), gross=78, mode="total_only")                 # diff 6.0: 10.0 below
    _round(conn, "after", _day(4), gross=90, mode="total_only")
    whs.recompute(conn, cfg)
    h = _history(conn)
    assert h["great"]["esr"] == -2.0 and h["great"]["differential"] == 6.0       # stored unadjusted
    assert h["great"]["hi_after"] == 3.0                                         # 4 scores: 4.0 - 1.0
    assert h["after"]["esr"] is None and h["after"]["hi_after"] == 4.0           # ESR persists on old scores


def test_recompute_soft_and_hard_caps(conn, cfg):
    _course(conn)
    for i in range(20):
        _round(conn, f"good{i:02d}", _day(i), gross=82, mode="total_only")      # diff 10.0
    for i in range(20, 36):
        _round(conn, f"bad{i:02d}", _day(i), gross=102, mode="total_only")      # diff 30.0
    whs.recompute(conn, cfg)
    h = _history(conn)
    assert h["good02"]["hi_after"] == 8.0 and h["good18"]["low_hi"] is None
    assert h["good19"]["low_hi"] == 8.0 and h["good19"]["hi_after"] == 10.0
    assert h["bad31"]["hi_after"] == 10.0                                       # 8 good scores still in the 20
    assert (h["bad32"]["hi_after"], h["bad32"]["cap_applied"]) == (11.8, "soft")   # calc 12.5
    assert (h["bad33"]["hi_after"], h["bad33"]["cap_applied"]) == (13.0, "soft")   # calc 15.0
    assert (h["bad34"]["hi_after"], h["bad34"]["cap_applied"]) == (13.0, "hard")   # calc 17.5
    assert h["bad35"]["low_hi"] == 8.0


def test_recompute_is_repeatable(conn, cfg):
    _course(conn)
    for i in range(6):
        _round(conn, f"r{i}", _day(i), strokes=[5 + (i % 2)] * 18)
    first = whs.recompute(conn, cfg)
    rows = _history(conn)
    assert whs.recompute(conn, cfg) == first and _history(conn) == rows


def test_recompute_on_synthetic_archive(conn, cfg, tmp_path):
    import_export(conn, write_archive(tmp_path / "a.json", make_archive()), cfg)
    sync_courses(conn, EXAMPLE_YAML)
    s = whs.recompute(conn, cfg, include_nine=False)
    h = _history(conn)
    assert s["by_kind"] == {"ndb_adjusted": 16, "gross_upper_bound": 2}
    assert h[CASES["total_only_18"]]["differential_kind"] == "gross_upper_bound"
    assert h[CASES["sum_mismatch"]]["differential_kind"] == "gross_upper_bound"
    assert s["skipped"] == {"nine_hole_excluded": 4, "partial": 1, "no_tee": 2}
    assert CASES["abandoned"] not in h
    with_nine = whs.recompute(conn, cfg)                                      # the default
    assert with_nine["by_kind"]["nine_hole_scaled"] == 3                      # front, back, resolved 9-array
    assert with_nine["skipped"]["nine_unknown"] == 1                          # the total-only 9
    assert with_nine["established_on"] == s["established_on"]                 # three 18s come first
    dates = [r["as_of"] for r in conn.execute("SELECT as_of FROM handicap_history ORDER BY rowid")]
    assert dates == sorted(dates)


# ------------------------------------------- 9-hole scores before an index (2024)
def test_solve_initial_index_is_self_consistent():
    hi, diffs = whs.solve_initial_index([(None, 9.0), (18.0, None), (18.0, None), (18.0, None)])
    assert (hi, diffs) == (17.0, [19.0, 18.0, 18.0, 18.0])
    assert whs.solve_initial_index([(10.0, None), (12.0, None), (14.0, None)]) == (8.0, [10.0, 12.0, 14.0])
    for nines in ([12.0, 15.5, 11.2, 20.0, 13.3, 14.1], [25.0, 30.0, 22.4, 27.7, 24.9, 26.0],
                  [2.0, 3.5, 1.1, 4.4, 2.2, 3.3]):
        hi, diffs = whs.solve_initial_index([(None, d) for d in nines])
        assert hi == whs.handicap_index(diffs)                                  # a fixed point
        assert diffs == [whs.nine_to_eighteen(d, hi) for d in nines]


def test_solve_initial_index_caps_at_54():
    hi, diffs = whs.solve_initial_index([(None, 45.0)] * 6)
    assert hi == 54.0 and diffs == [whs.nine_to_eighteen(45.0, 54.0)] * 6    # 45 + 29.28 = 74.3


NINE_PARS = [4, 4, 3, 4, 4, 5, 3, 4, 4]                                       # par 35, like a 9-hole course
NINE_TEE = "nc:White:M"
# Invented beginner nines (+19..+30 over par 35), so the par + 5 cap before an index bites.
NINE_CARDS = [[8, 7, 9, 8, 7, 8, 6, 8, 7], [7, 6, 8, 6, 7, 8, 5, 7, 7], [8, 6, 7, 7, 6, 7, 6, 6, 6],
              [6, 7, 6, 8, 8, 7, 5, 8, 8], [6, 6, 6, 7, 8, 8, 5, 6, 6], [8, 7, 6, 7, 8, 8, 5, 8, 7],
              [7, 6, 8, 6, 8, 8, 6, 7, 6], [8, 6, 8, 6, 6, 7, 4, 6, 8]]


def _nine_hole_course(conn, *, cr9=33.4, slope9=112):
    conn.execute("INSERT INTO clubs(club_id, name) VALUES ('c9', 'Nine Club')")
    conn.execute("INSERT INTO courses(course_key, club_id, name, holes, par) VALUES ('nc', 'c9', 'Nine', 9, 35)")
    conn.execute("INSERT INTO tees(tee_id, course_key, name, gender, par, cr_f9, slope_f9, is_default) "
                 "VALUES (?, 'nc', 'White', 'M', 35, ?, ?, 1)", (NINE_TEE, cr9, slope9))
    for hole, par in enumerate(NINE_PARS, 1):
        conn.execute("INSERT INTO tee_holes(tee_id, hole, par, si) VALUES (?, ?, ?, ?)", (NINE_TEE, hole, par, hole))


def _nine(conn, rid, day, card):
    _round(conn, rid, day, strokes=card, holes=9, nine="front", tee=NINE_TEE, course="nc")
    conn.execute("UPDATE rounds SET club_id = 'c9', par_played = 35, to_par = gross - 35 WHERE round_id = ?", (rid,))


def test_mostly_nine_hole_golfer_gets_an_index_after_54_holes(conn, cfg):
    """Shane's pattern: eight 9-hole rounds first, then 18-hole rounds away."""
    _course(conn)
    _nine_hole_course(conn)
    for i, card in enumerate(NINE_CARDS):
        _nine(conn, f"n{i + 1}", _day(i), card)
    _round(conn, "away1", _day(8), strokes=[9] * 18)
    _round(conn, "away2", _day(9), strokes=[8] * 18)
    s = whs.recompute(conn, cfg)
    h = _history(conn)
    assert s["established_on"] == str(_day(5)) and s["holes_needed"] == 0 and s["holes_posted"] == 108
    assert s["by_kind"] == {"nine_hole_scaled": 8, "ndb_adjusted": 2} and not s["skipped"]
    first_six = [h[f"n{i}"] for i in range(1, 7)]
    assert all(r["hi_after"] is None for r in first_six[:5]) and all(r["hi_before"] is None for r in first_six)
    hi = first_six[5]["hi_after"]
    assert hi is not None and hi <= whs.MAX_HI
    assert hi == whs.handicap_index([r["differential"] for r in first_six])     # self-consistent
    assert h["n1"]["ags"] == 8 + 7 + 8 + 8 + 7 + 8 + 6 + 8 + 7                   # 9 on a par 3 -> par + 5
    assert h["n7"]["hi_before"] == hi and h["n8"]["hi_before"] == h["n7"]["hi_after"]
    assert h["away1"]["hi_before"] == h["n8"]["hi_after"] and s["handicap_index"] == h["away2"]["hi_after"]


def test_nines_short_of_54_holes_wait(conn, cfg):
    _nine_hole_course(conn)
    for i, card in enumerate(NINE_CARDS[:5]):
        _nine(conn, f"n{i + 1}", _day(i), card)
    s = whs.recompute(conn, cfg)
    assert s["handicap_index"] is None and s["scored"] == 0 and s["established_on"] is None
    assert s["skipped"] == {"nine_awaiting_54_holes": 5} and s["holes_needed"] == 9
    assert _history(conn) == {}


def test_nine_after_establishing_nine_on_the_same_day(conn, cfg):
    _nine_hole_course(conn)
    for i, card in enumerate(NINE_CARDS[:6]):
        _nine(conn, f"n{i + 1}", _day(i), card)
    _nine(conn, "n6b", _day(5), NINE_CARDS[6])                                 # later that afternoon
    conn.execute("UPDATE rounds SET played_at_utc = ? WHERE round_id = 'n6b'", (f"{_day(5)}T20:00:00+00:00",))
    whs.recompute(conn, cfg)
    h = _history(conn)
    assert h["n6"]["hi_after"] is not None and h["n6b"]["hi_before"] is None      # index updates overnight
    expected = whs.nine_to_eighteen(whs._raw_differential(h["n6b"]["ags"], 33.4, 112), h["n6"]["hi_after"])
    assert h["n6b"]["differential"] == expected                                 # the index just established


def test_include_nine_setting_is_persisted(conn, cfg):
    _course(conn)
    for i in range(3):
        _round(conn, f"r{i}", _day(i), strokes=[5] * 18)
    _round(conn, "front", _day(4), strokes=[5] * 9, holes=9, nine="front")
    assert whs.include_nine_setting(conn) is True
    assert whs.recompute(conn, cfg)["by_kind"].get("nine_hole_scaled") == 1
    whs.set_include_nine(conn, False)
    s = whs.recompute(conn, cfg)
    assert not s["include_nine"] and s["include_nine_source"] == "setting" and s["skipped"] == {"nine_hole_excluded": 1}
    assert whs.recompute(conn, cfg, include_nine=True)["include_nine"]           # one run only
    assert whs.include_nine_setting(conn) is False
    whs.set_include_nine(conn, True)
    assert conn.execute("SELECT value FROM meta WHERE key = 'whs_include_nine'").fetchone()[0] == "1"


def test_net_double_bogey_over_nine_holes_above_27():
    assert whs.net_double_bogey(4, 4, 28, 9) == 9                               # 4 strokes, CH9 over 27
    assert whs.net_double_bogey(4, 3, 27, 9) == 9                               # par + 2 + 3
    assert whs.net_double_bogey(4, 4, 28) == 10                                 # 18 holes: 28 is not over 54


def test_compare_18birdies(conn, cfg):
    _course(conn)
    for i in range(3):
        _round(conn, f"r{i}", _day(i), strokes=[5] * 18)                        # our differential 18.0
    _round(conn, "unrated", _day(3), strokes=[5] * 18, tee="tc:Gold:M")
    _round(conn, "no-18b", _day(4), strokes=[5] * 18)
    for rid, theirs in (("r0", 17.6), ("r1", 18.0), ("r2", 19.25), ("unrated", 20.0)):
        conn.execute("UPDATE rounds SET round_handicap_18b = ? WHERE round_id = ?", (theirs, rid))
    whs.recompute(conn, cfg)
    rows = whs.compare_18birdies(conn)
    assert [r["round_id"] for r in rows] == ["r0", "r1", "r2", "unrated"]
    assert [r["delta"] for r in rows] == [0.4, 0.0, -1.2, None]                  # -1.25 rounds toward zero
    assert rows[0]["differential"] == 18.0 and rows[0]["date"] == str(_day(0)) and rows[0]["holes"] == 18
    assert rows[3]["differential"] is None and rows[3]["round_handicap_18b"] == 20.0
