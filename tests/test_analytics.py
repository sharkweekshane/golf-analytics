"""Analytics math on small hand-computed cases (golf.analytics.stats)."""
from __future__ import annotations

import math
import statistics

import pytest

from golf.analytics import stats
from golf.db import dumps, now_iso


# ----------------------------------------------------------------------------- pure functions
def test_wilson_matches_published_values():
    lo, hi = stats.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = stats.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert stats.wilson(3, 0) is None


def test_ewma_is_normalised_and_skips_missing():
    assert stats.ewma([10, 20], lam=0.5) == pytest.approx([10, 25 / 1.5])
    assert stats.ewma([10, None, 20], lam=0.5) == pytest.approx([10, 10, 25 / 1.5])
    assert stats.ewma([None, 5]) == [None, 5]
    # A half-weight observation moves the level half as much: (0.5*10 + 0.5*20) / (0.5 + 0.5).
    assert stats.ewma([10, 20], lam=0.5, weights=[1, 0.5])[1] == pytest.approx(15)


def test_ewma_band_single_point_is_plus_minus_z_sigma():
    band = stats.ewma_band([10.0], sigma=2.0, z=1.0)
    assert band[0] == pytest.approx((8.0, 12.0))
    # With more rounds the band narrows but stays centred on the EWMA.
    band = stats.ewma_band([10, 10, 10, 10], lam=0.25, sigma=2.0)
    lo, hi = band[-1]
    assert (lo + hi) / 2 == pytest.approx(10) and hi - lo < 2 * stats.Z80 * 2.0
    assert stats.ewma_band([10.0], sigma=None) == [None]


def test_spread_hand_computed():
    s = stats.spread([1, 2, 3, 4, 5])
    assert s["n"] == 5 and s["mean"] == 3
    assert s["sd"] == pytest.approx(math.sqrt(2.5))
    assert s["mad"] == 1 and s["mad_sigma"] == pytest.approx(1.4826)
    assert s["mssd_sigma"] == pytest.approx(math.sqrt(4 / 8))
    assert stats.spread([])["mean"] is None
    assert stats.spread([7])["sd"] is None


def test_rolling_window():
    r = stats.rolling([1, 2, 3, 4], window=2, min_periods=2)
    assert r[0] is None
    assert [x["mean"] for x in r[1:]] == [1.5, 2.5, 3.5]


def test_identity_split_adds_up_to_to_par():
    to_green, putts_over = stats.identity_split(gross=90, putts=34, par=72, holes=18)
    assert (to_green, putts_over) == (20, -2)
    assert to_green + putts_over == 90 - 72
    assert sum(stats.identity_split(45, 15, 36, 9)) == 9


def test_hole_stats_counts():
    holes = [
        {"par": 4, "strokes": 4, "putts": 2, "fairway": "hit", "gir": 1, "penalties": 0},
        {"par": 3, "strokes": 4, "putts": 1, "fairway": "not_applicable", "gir": 0, "penalties": 0},
        {"par": 5, "strokes": 7, "putts": 3, "fairway": "left", "gir": None, "penalties": 1},   # GIR derived: 4 > 3
        {"par": 4, "strokes": 4, "putts": 1, "fairway": "right", "gir": 0, "penalties": 0},     # scramble
    ]
    s = stats.hole_stats(holes)
    assert s["par_type"] == {3: [1, 1], 4: [0, 2], 5: [2, 1]}
    assert s["dbl_holes"] == 1
    assert (s["putt_holes"], s["putts_sum"], s["three_putts"], s["one_putts"]) == (4, 7, 1, 2)
    assert (s["fw_chances"], s["fw_hit"]) == (3, 1)
    assert (s["gir_holes"], s["gir_hit"], s["gir_derived"]) == (4, 1, 1)
    assert (s["gir_putt_holes"], s["gir_putts_sum"]) == (1, 2)
    assert (s["scramble_chances"], s["scrambles"]) == (3, 1)
    assert (s["penalty_holes"], s["penalties"]) == (4, 1)
    assert s["strokes_complete"] and s["pars_complete"]


def test_pooled_rate_and_mean():
    facts = [{"fw_hit": 5, "fw_chances": 14, "x": 30.0, "weight": 1.0},
             {"fw_hit": 2, "fw_chances": 7, "x": 40.0, "weight": 0.5},
             {"fw_hit": None, "fw_chances": None, "x": None, "weight": 1.0}]
    p = stats.pooled_rate(facts, "fw_hit", "fw_chances")
    assert (p["k"], p["n"], p["rounds"]) == (7, 21, 2)
    assert (p["lo"], p["hi"]) == pytest.approx(stats.wilson(7, 21))
    assert stats.pooled_mean(facts, "x")["value"] == pytest.approx((30 + 20) / 1.5)
    assert stats.pooled_rate([], "fw_hit", "fw_chances") is None


def test_outcome_sigma_prefers_18_hole_rounds():
    facts = [{"outcome": v, "holes": 18} for v in (10, 12, 14)] + [{"outcome": 40, "holes": 9}]
    sig = stats.outcome_sigma(facts)
    assert sig["sigma"] == pytest.approx(2.0) and sig["n"] == 3 and sig["basis"] == "18-hole rounds"
    few = [{"outcome": 10, "holes": 18}, {"outcome": 20, "holes": 9}]
    assert stats.outcome_sigma(few)["sigma"] is None


def test_choose_metric():
    rated = [{"diff_equiv": 10.0, "to_par_equiv": 12} for _ in range(3)]
    unrated = [{"diff_equiv": None, "to_par_equiv": 12} for _ in range(2)]
    assert stats.choose_metric(rated + unrated) == "differential"
    assert stats.choose_metric(rated[:2] + unrated) == "to_par"
    assert stats.choose_metric([]) == "differential"


def test_merge_patch_rfc7386():
    assert stats.merge_patch({"a": 1, "b": 2, "c": {"d": 1}}, {"b": None, "e": 3, "c": {"d": 2}}) == \
        {"a": 1, "c": {"d": 2}, "e": 3}
    assert stats.merge_patch({"a": [1]}, {"a": [2, 3]}) == {"a": [2, 3]}


def test_practice_blocks_and_injury_spans():
    ev = [{"event_type": "practice", "date": d} for d in ("2026-05-01", "2026-05-05", "2026-05-20")]
    ev.append({"event_type": "practice", "date": "2026-06-01", "is_planned": True})
    assert stats.practice_blocks(ev) == [{"start": "2026-05-01", "end": "2026-05-05", "sessions": 2},
                                         {"start": "2026-05-20", "end": "2026-05-20", "sessions": 1}]
    spans = stats.injury_spans([{"event_id": "i1", "event_type": "injury", "date": "2026-07-01",
                                 "injury": [{"body_part": "wrist", "severity": "minor"}]}])
    assert spans[0]["end"] == "2026-07-15" and spans[0]["approximate"] and spans[0]["body_part"] == "wrist"


# ----------------------------------------------------------------------------- database readers
def _course(conn):
    conn.execute("INSERT INTO clubs(club_id, name) VALUES ('c1', 'Test Club')")
    conn.execute("INSERT INTO courses(course_key, club_id, name, holes, par) VALUES ('k1', 'c1', 'Test', 18, 72)")
    conn.execute("""INSERT INTO tees(tee_id, course_key, name, gender, par, cr18, slope18, cr_f9, slope_f9, cr_b9,
                    slope_b9, is_default) VALUES ('k1:W:M', 'k1', 'W', 'M', 72, 70.0, 113, 35.0, 113, 35.0, 113, 1)""")
    for hole in range(1, 19):
        conn.execute("INSERT INTO tee_holes(tee_id, hole, par, si) VALUES ('k1:W:M', ?, 4, ?)", (hole, hole))


def _round(conn, rid, day, *, club="c1", course="k1", mode="hole_by_hole", holes=18, nine=None, gross=90,
           to_par=None, strokes=None, **extra):
    par = 4 * holes
    row = {"round_id": rid, "source": "18b_export", "played_on_local": day, "club_id": club, "course_key": course,
           "entry_mode": mode, "holes_played": holes, "nine": nine, "gross": gross,
           "to_par": gross - par if to_par is None else to_par, "par_played": par, **extra}
    cols = list(row)
    conn.execute(f"INSERT INTO rounds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(row.values()))
    first = 10 if nine == "back" else 1
    for i, s in enumerate(strokes or []):
        conn.execute("INSERT INTO round_holes(round_id, hole, strokes) VALUES (?, ?, ?)", (rid, first + i, s))


def test_round_facts_tiers_outcomes_and_exclusions(conn):
    _course(conn)
    strokes18 = [5] * 18                         # 90, all bogeys
    _round(conn, "r1", "2026-05-01", strokes=strokes18, putts=34, putts_tracked=1, fw_hit=7, fw_chances=14,
           gir=4, gir_chances=18, dbl_plus=0)
    conn.execute("""INSERT INTO handicap_history(round_id, as_of, ags, differential, differential_kind, hi_after)
                    VALUES ('r1', '2026-05-01', 90, 19.5, 'ndb_adjusted', 18.0)""")
    _round(conn, "r2", "2026-05-08", holes=9, nine="front", gross=45, strokes=[5] * 9)
    conn.execute("""INSERT INTO handicap_history(round_id, as_of, ags, differential, differential_kind)
                    VALUES ('r2', '2026-05-08', 44, 21.0, 'nine_hole_scaled')""")
    _round(conn, "r3", "2026-05-15", mode="total_only", gross=95)
    _round(conn, "r4", "2026-05-20", club="c2", course=None, gross=88, to_par=18, strokes=[5] * 17 + [3])
    _round(conn, "r5", "2026-05-25", strokes=strokes18)
    conn.execute("INSERT INTO round_overrides(round_id, exclude, reason) VALUES ('r5', 1, 'scramble')")
    _round(conn, "r6", "2026-05-26", mode="abandoned", gross=0)
    _round(conn, "r7", "2026-05-27", strokes=strokes18, dq_flags=dumps(["sum_mismatch"]), gross=92)

    facts = {f["round_id"]: f for f in stats.round_facts(conn)}
    assert set(facts) == {"r1", "r2", "r3", "r4", "r7"}
    r1, r2, r3, r4, r7 = (facts[k] for k in ("r1", "r2", "r3", "r4", "r7"))
    assert r1["outcome_metric"] == "differential"
    # 18 holes: handicap_history differential; pars came from tee_holes, so tier B and par-4 average +1.
    assert r1["outcome"] == 19.5 and r1["tier"] == "B" and r1["par4_avg"] == 1.0
    assert r1["to_green"] == 20 and r1["putts_over"] == -2 and r1["fir_pct"] == 0.5
    # 9 holes: 2 x own 9-hole differential from AGS 44 (not the WHS-scaled 21.0), half weight.
    assert r2["outcome"] == pytest.approx(2 * (44 - 35.0)) and r2["weight"] == 0.5 and r2["is_nine"]
    # Total-only: gross-based differential, tier A, no per-hole or false-zero double count.
    assert r3["outcome"] == pytest.approx(95 - 70.0) and r3["tier"] == "A" and r3["dbl_plus"] is None
    # Unmapped club: not rated, so no outcome on the differential scale; to-par is still there.
    assert not r4["rated"] and r4["outcome"] is None and r4["to_par_equiv"] == 18
    # Sum mismatch: totals kept, per-hole scoring not used.
    assert r7["tier"] == "A" and r7["par4_avg"] is None and r7["gross"] == 92

    forced = {f["round_id"]: f for f in stats.round_facts(conn, metric="to_par")}
    assert forced["r2"]["outcome"] == 2 * (45 - 36) and forced["r4"]["outcome"] == 18


def test_hi_series_orders_and_skips_nulls(conn):
    _course(conn)
    for i, (day, hi) in enumerate([("2026-05-01", None), ("2026-05-08", 18.2), ("2026-05-15", 17.9)]):
        _round(conn, f"h{i}", day)
        conn.execute("INSERT INTO handicap_history(round_id, as_of, hi_after) VALUES (?, ?, ?)", (f"h{i}", day, hi))
    assert [(h["date"], h["hi"]) for h in stats.hi_series(conn)] == [("2026-05-08", 18.2), ("2026-05-15", 17.9)]


def _event(conn, eid, status, **kw):
    now = now_iso()
    row = {"event_id": eid, "doc_id": "d1", "status": status, "event_type": "lesson", "date": "2026-05-01",
           "created_at": now, "updated_at": now, **kw}
    cols = list(row)
    conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(row.values()))


def test_load_timeline_applies_reviews(conn):
    conn.execute("INSERT INTO source_docs(doc_id, source) VALUES ('d1', 'manual')")
    _event(conn, "auto", "auto_accepted", focus_areas=dumps([{"game_area": "putting", "detail": "gate"}]))
    _event(conn, "pending", "pending")
    _event(conn, "edited", "pending", coach="Old", game_areas="driving")
    _event(conn, "rejected", "auto_accepted")
    now = now_iso()
    conn.execute("INSERT INTO event_reviews(event_id, decision, edits_json, reviewed_at) VALUES (?, 'edited', ?, ?)",
                 ("edited", dumps({"coach": "New", "date": "2026-04-01"}), now))
    conn.execute("INSERT INTO event_reviews(event_id, decision, reviewed_at) VALUES ('rejected', 'rejected', ?)",
                 (now,))
    ev = stats.load_timeline(conn)
    assert [e["event_id"] for e in ev] == ["edited", "auto"]
    assert ev[0]["coach"] == "New" and ev[0]["date"] == "2026-04-01"
    assert ev[0]["focus_areas"] == [{"game_area": "driving", "detail": ""}]
    assert ev[1]["focus_areas"] == [{"game_area": "putting", "detail": "gate"}] and ev[1]["is_planned"] is False


# ----------------------------------------------------------------------------- per 9 holes, putts, 18Birdies
def test_putt_rule_partial_tracking_is_left_out(conn):
    _course(conn)
    # Old database: putts_tracked = 1 but 7 putts over 9 holes is partial tracking.
    _round(conn, "p1", "2026-07-01", holes=9, nine="front", gross=60, strokes=[6, 7, 7, 6, 7, 7, 7, 6, 7],
           putts=7, putts_tracked=1)
    # New importer: putts_tracked = 0 and the flag.
    _round(conn, "p2", "2026-07-02", holes=9, nine="front", gross=58, putts=5, putts_tracked=0,
           dq_flags=dumps(["putts_partial"]))
    _round(conn, "p3", "2026-07-03", holes=9, nine="front", gross=57, putts=9, putts_tracked=1)
    _round(conn, "p4", "2026-07-04", holes=9, nine="front", gross=56, putts=0, putts_tracked=0)
    # Partial total, but complete per-hole putts from screenshots: those count.
    _round(conn, "p5", "2026-07-05", holes=9, nine="front", gross=45, putts=4, putts_tracked=0,
           dq_flags=dumps(["putts_partial"]), strokes=[5] * 9)
    conn.execute("UPDATE round_holes SET putts = 2 WHERE round_id = 'p5'")
    facts = {f["round_id"]: f for f in stats.round_facts(conn)}
    assert facts["p1"]["putts"] is None and facts["p1"]["putts_partial"] and facts["p1"]["putts_recorded"] == 7
    assert facts["p1"]["putts_9"] is None and facts["p1"]["to_green_9"] is None
    assert facts["p2"]["putts"] is None and facts["p2"]["putts_partial"]
    assert facts["p3"]["putts"] == 9 and facts["p3"]["putts_9"] == 9 and not facts["p3"]["putts_partial"]
    assert facts["p4"]["putts"] is None and not facts["p4"]["putts_partial"]           # not tracked at all
    assert facts["p5"]["putts"] == 18 and not facts["p5"]["putts_partial"]
    summ = stats.summary_stats(list(facts.values()))
    assert summ["putts_9"]["rounds"] == 2 and summ["putts_partial"] == 2


def test_putts_override_beats_the_importer_rule_both_ways(conn):
    from golf import cli as ops

    _course(conn)
    _round(conn, "q1", "2026-07-01", holes=9, nine="front", gross=57, putts=20, putts_tracked=0,
           dq_flags=dumps(["putts_partial"]))                       # rule says partial; Shane says full
    _round(conn, "q2", "2026-07-02", holes=9, nine="front", gross=58, putts=22, putts_tracked=1)
    assert ops.set_putts_override(conn, "q1", "full")["putts"] == 20
    ops.set_putts_override(conn, "2026-07-02", "partial")           # by date: one round that day
    facts = {f["round_id"]: f for f in stats.round_facts(conn)}
    assert facts["q1"]["putts"] == 20 and not facts["q1"]["putts_partial"]
    assert facts["q2"]["putts"] is None and facts["q2"]["putts_partial"]
    ops.set_putts_override(conn, "q2", "auto")
    assert {f["round_id"]: f for f in stats.round_facts(conn)}["q2"]["putts"] == 22
    with pytest.raises(ValueError, match="No counted round"):
        ops.set_putts_override(conn, "2026-01-01", "full")
    with pytest.raises(ValueError, match="full, partial or auto"):
        ops.set_putts_override(conn, "q1", "maybe")


def test_per9_fields_halves_and_18birdies_numbers(conn):
    _course(conn)
    strokes = [6] * 9 + [5] * 9                           # front +18, back +9 on all-par-4 holes
    _round(conn, "f1", "2026-07-01", gross=99, strokes=strokes, putts=36, putts_tracked=1, gir=3, gir_chances=18,
           dbl_plus=9, round_handicap_18b="27.4", sg_overall=100, sg_tee_to_green=-6.5, tee_name="White")
    _round(conn, "n1", "2026-07-02", holes=9, nine="front", gross=58, putts=18, putts_tracked=1, gir=1,
           gir_chances=9, round_handicap_18b=43.8)
    f1, n1 = stats.round_facts(conn)
    assert f1["nines"] == 2 and f1["to_par_9"] == pytest.approx(27 / 2) and f1["putts_9"] == 18
    assert f1["gir_9"] == pytest.approx(1.5) and f1["dbl_9"] == pytest.approx(4.5)
    assert f1["halves"] == {"front": 18, "back": 9, "front_gross": 54, "back_gross": 45}
    assert f1["round_handicap_18b"] == pytest.approx(27.4) and f1["sg_overall"] is None
    assert f1["sg_tee_to_green"] == -6.5 and f1["tee_name"] == "White" and f1["group"] == "k1"
    assert n1["nines"] == 1 and n1["to_par_9"] == 22 and n1["halves"] is None and n1["gir_9"] == 1
    assert n1["round_handicap_18b"] == 43.8


def test_set_outcome_scales_and_native_sigma():
    facts = [{"diff_equiv": 10.0 + i, "to_par_equiv": 40.0 + 2 * i, "to_par_9": 20.0 + i, "nines": 1.0, "holes": 9}
             for i in range(4)]
    facts.append({"diff_equiv": 30.0, "to_par_equiv": 50.0, "to_par_9": 25.0, "nines": 2.0, "holes": 18})
    stats.set_outcome(facts, "to_par_9")
    assert [f["weight"] for f in facts] == [1, 1, 1, 1, 2] and facts[-1]["outcome"] == 25.0
    sig = stats.outcome_sigma(facts)
    assert sig["basis"] == "9-hole rounds" and sig["sigma"] == pytest.approx(statistics.stdev([20, 21, 22, 23]))
    stats.set_outcome(facts, "to_par")
    assert [f["weight"] for f in facts] == [0.5, 0.5, 0.5, 0.5, 1] and facts[0]["outcome"] == 40.0
    assert stats.outcome_sigma(facts)["basis"] == "all rounds (9-hole doubled)"
    with pytest.raises(ValueError):
        stats.set_outcome(facts, "vibes")


def test_last_rounds_per9_and_best_nine():
    def f(day, holes, to_par, gross, halves=None):
        return {"round_id": f"r{day}", "date": f"2026-07-{day:02d}", "holes": holes, "nines": holes / 9,
                "to_par": to_par, "to_par_9": to_par * 9 / holes, "gross": gross, "halves": halves,
                "course_name": "Home", "club_name": None}
    halves = {"front": 22, "back": 28, "front_gross": 57, "back_gross": 63}
    rounds = [f(1, 9, 30, 65), f(2, 9, 28, 63), f(3, 18, 50, 120, halves), f(4, 9, 25, 60), f(5, 9, 22, 57),
              f(6, 9, 22, 57)]
    last = stats.last_rounds_per9(rounds, 3)
    assert last["value"] == pytest.approx((25 + 22 + 22) / 3) and last["holes"] == 27
    assert last["prev_value"] == pytest.approx((30 + 28 + 50) / 4)           # the 18 counts as two nines
    assert (last["start"], last["end"]) == ("2026-07-04", "2026-07-06")
    best = stats.best_nine(rounds)
    assert best["to_par"] == 22 and best["date"] == "2026-07-06" and best["part"] == "9-hole round"   # latest tie
    best = stats.best_nine(rounds[:3])
    assert best["to_par"] == 22 and best["part"] == "front nine of 18" and best["gross"] == 57
    assert stats.best_nine([]) is None and stats.last_rounds_per9([]) is None


# ----------------------------------------------------------------------------- shots and club distances
def test_club_labels_and_bag_order():
    lab = stats.club_label
    assert [lab("WOOD", "1"), lab("WOOD", "3"), lab("HYBRID", "5"), lab("IRON", "7"), lab("WEDGE", "P", 46),
            lab("WEDGE", "S", 56), lab("WEDGE", "52"), lab("PUTTER", "Putter"), lab("IRON", "P")] == \
        ["Driver", "3W", "5H", "7i", "PW", "SW", "52°", "Putter", "PW"]
    assert lab(None, None) is None
    bag = ["SW", "7i", "Driver", "PW", "5H", "3W", "52°", "9i", "LW", "4i", "Mystery"]
    assert sorted(bag, key=stats.club_order) == ["Driver", "3W", "5H", "4i", "7i", "9i", "PW", "52°", "SW",
                                                 "LW", "Mystery"]


def test_club_distances_median_iqr_and_invalid_shots():
    def s(club, d, hole=1):
        return {"round_id": "r", "club": club, "distance_yards": d, "date": "2026-07-01", "hole": hole}
    shots = [s("Driver", d) for d in (150, 200, 210, 220, 260)]
    shots += [s("Driver", 451.0), s("PW", 2.0), s("PW", 80), s("PW", None), s("Putter", 3)]
    out = stats.club_distances(shots)
    drv, pw = out["clubs"]
    assert drv["club"] == "Driver" and drv["n"] == 5 and drv["median"] == 210
    assert (drv["q1"], drv["q3"]) == (200, 220) and (drv["min"], drv["max"]) == (150, 260)
    assert drv["n_invalid"] == 1 and pw["n"] == 1 and pw["median"] == pw["q1"] == pw["q3"] == 80
    assert out["n_putts"] == 1 and out["n_valid"] == 6 and out["n_shots"] == 10
    reasons = sorted((x["club"], x["reason"]) for x in out["invalid"])
    assert reasons == [("Driver", "over 400 yards"), ("PW", "no distance"), ("PW", "under 5 yards")]
    assert stats.shot_validity(5.0) is None and stats.shot_validity(400.0) is None


def test_load_shots_and_coverage_never_touch_coordinates(conn):
    _course(conn)
    _round(conn, "g1", "2026-07-01", holes=9, nine="front", gross=58, strokes=[6, 7, 7, 6, 7, 7, 7, 6, 5])
    _round(conn, "g2", "2026-07-02", holes=9, nine="front", gross=58)
    rows = [("g1", 1, 1, "WOOD", "1", None, 0, 201.5, 42.123456, -71.5),
            ("g1", 2, 1, "IRON", "9", "9i", None, 95.0, 42.124, -71.51),
            ("g1", 3, 1, "PUTTER", "Putter", "Putter", None, 3.0, 42.125, -71.52),
            ("g1", 4, 3, "WEDGE", "S", None, 56, 30.0, 42.126, -71.53)]
    conn.executemany("""INSERT INTO shots(round_id, seq, hole, club_type, club_number, club, loft, distance_yards,
                        start_lat, start_lon) VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
    shots = stats.load_shots(conn)
    assert [s["club"] for s in shots] == ["Driver", "9i", "Putter", "SW"]
    assert not any(k.endswith(("_lat", "_lon")) for s in shots for k in s)
    assert shots[0]["date"] == "2026-07-01"
    cov = stats.shot_coverage(shots, stats.round_facts(conn))
    assert (cov["rounds_total"], cov["rounds_with_shots"]) == (2, 1)
    assert (cov["holes_played"], cov["holes_with_shots"]) == (9, 2)
    by_hole = {h["hole"]: h for h in cov["by_hole"]}
    assert by_hole[1] == {"hole": 1, "played": 1, "rounds": 1, "shots": 3} and by_hole[2]["rounds"] == 0


def test_after_round_logging_and_default_positions():
    def shot(rid, seq, hole, club, d, t):
        return {"round_id": rid, "seq": seq, "hole": hole, "club": club, "distance_yards": d,
                "shot_at_utc": f"2026-07-01T{t}+00:00", "date": "2026-07-01"}
    live = [shot("L", 1, 1, "Driver", 201.3, "14:00:00"), shot("L", 2, 1, "7i", 120.2, "14:03:10"),
            shot("L", 3, 2, "Driver", 188.8, "14:12:40"), shot("L", 4, 2, "9i", 83.2392, "14:15:00")]
    after = [shot("A", 1, 1, "Driver", 206.71405, "18:00:00"), shot("A", 2, 1, "PW", 52.1, "18:00:04"),
             shot("A", 3, 2, "Driver", 206.71405, "18:00:09"), shot("A", 4, 2, "6i", 83.2392, "18:00:12")]
    few = [shot("F", 1, 1, "Driver", 180.0, "10:00:00")]
    tagged = stats.tag_logging(live + after + few)
    assert {s["round_id"]: s["logged"] for s in tagged} == {"L": "live", "A": "after", "F": "unknown"}
    out = stats.club_distances(tagged)
    # 83.2392 on hole 2 twice (two rounds) is an app default; 206.71405 on holes 1 and 2 is not a repeat.
    reasons = [(x["club"], x["d"], x["reason"]) for x in out["invalid"]]
    assert sorted(r[:2] for r in reasons) == [("6i", 83.2), ("9i", 83.2)]
    assert all(r[2].startswith("same distance as another shot on this hole") for r in reasons)
    drv = next(c for c in out["clubs"] if c["club"] == "Driver")
    assert drv["n"] == 5 and drv["n_after"] == 2 and out["n_after"] == 3
