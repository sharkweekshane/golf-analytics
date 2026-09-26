"""Lesson before/after comparisons: MDE, bootstrap reproducibility, windows, caveat flags, wording."""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

import pytest

from golf.analytics import lessons as L
from golf.analytics.stats import load_timeline, round_facts
from golf.demo import seed_demo

BASE = date(2026, 4, 1)


def rnd(day: int, outcome: float, holes: int = 18, **kw) -> dict:
    return {"round_id": f"r{day:03d}", "date": (BASE + timedelta(days=day)).isoformat(), "outcome": outcome,
            "holes": holes, "weight": 1.0 if holes == 18 else 0.5, **kw}


def ev(eid: str, kind: str, day: int, **kw) -> dict:
    return {"event_id": eid, "event_type": kind, "date": (BASE + timedelta(days=day)).isoformat(),
            "is_planned": False, **kw}


# ----------------------------------------------------------------------------- MDE
def test_mde_formula_matches_research_table():
    assert L.mde(3.0, 5) == pytest.approx(2.8 * 3.0 * math.sqrt(2 / 5))
    assert round(L.mde(3.0, 5), 1) == 5.3 and round(L.mde(3.5, 5), 1) == 6.2
    assert round(L.mde(3.0, 10), 1) == 3.8 and round(L.mde(3.5, 20), 1) == 3.1
    assert L.mde(3.0, 5, 10) == pytest.approx(2.8 * 3.0 * math.sqrt(1 / 5 + 1 / 10))


def test_mde_sentence():
    s = L.mde_sentence(3.0, 5)
    assert "σ≈3.0" in s and "about 5.3 strokes" in s and "5 rounds each side" in s
    assert "can't be estimated" in L.mde_sentence(None)


# ----------------------------------------------------------------------------- bootstrap
def test_bootstrap_is_reproducible_and_consistent():
    before, after = [18, 21, 16, 19, 22], [15, 17, 19, 14, 16]
    a = L.bootstrap_diff(before, after, seed=11, n_boot=2000)
    b = L.bootstrap_diff(before, after, seed=11, n_boot=2000)
    c = L.bootstrap_diff(before, after, seed=12, n_boot=2000)
    assert a == b
    assert a["intervals"] != c["intervals"]
    assert a["difference"] == pytest.approx(sum(after) / 5 - sum(before) / 5)
    lo80, hi80 = a["intervals"]["80"]
    lo95, hi95 = a["intervals"]["95"]
    assert lo95 <= lo80 <= a["difference"] <= hi80 <= hi95
    assert "percentile" in a["method"]


def test_bootstrap_weights_and_constant_data():
    out = L.bootstrap_diff([10, 20], [12, 12], w_before=[1, 0.5], seed=1, n_boot=500)
    assert out["mean_before"] == pytest.approx(20 / 1.5)
    flat = L.bootstrap_diff([15, 15, 15], [13, 13, 13], seed=1, n_boot=200)
    assert flat["intervals"]["95"] == pytest.approx([-2.0, -2.0])


# ----------------------------------------------------------------------------- windows
def test_lesson_windows_learning_window_and_sides():
    rounds = [rnd(d, 15.0) for d in (0, 3, 6, 9, 12, 15, 20, 25, 30, 34, 36, 40, 45)]
    rounds.append(rnd(50, None))                          # no outcome: ignored
    lesson = ev("L1", "lesson", 20)
    w = L.lesson_windows(rounds, [lesson, ev("P", "lesson", 90, is_planned=True)], n_before=5, n_after=3)
    assert len(w) == 1
    days = lambda rs: [(date.fromisoformat(r["date"]) - BASE).days for r in rs]  # noqa: E731
    assert days(w[0]["before"]) == [3, 6, 9, 12, 15]
    assert days(w[0]["learning"]) == [20, 25, 30]           # lesson day itself is in the learning window
    assert days(w[0]["after"]) == [34, 36, 40]
    assert w[0]["after_start"] == (BASE + timedelta(days=34)).isoformat()


def test_clean_lesson_has_no_flags_and_honest_summary():
    rounds = [rnd(d, v) for d, v in zip(range(0, 35, 7), (14, 15, 14, 15, 14))]
    rounds += [rnd(d, v) for d, v in zip(range(56, 91, 7), (16, 15, 16, 15, 16))]
    out = L.lesson_effects(rounds, [ev("L1", "lesson", 40, coach="C")], sigma=3.0, n_boot=1000)
    e = out[0]
    assert (e["n_before"], e["n_after"], e["n_learning"]) == (5, 5, 0)
    assert e["mean_before"] == pytest.approx(14.4) and e["mean_after"] == pytest.approx(15.6)
    assert e["difference"] == pytest.approx(1.2)
    assert e["mde"] == pytest.approx(L.mde(3.0, 5))
    assert e["flags"] == []
    assert "higher than the 5 before" in e["summary"] and "smaller than the ~5.3-stroke change" in e["summary"]


def test_caveat_flags():
    before = [rnd(d, v) for d, v in zip((0, 5, 10, 15, 20, 25), (14, 20, 20, 20, 20, 20))]
    learning = [rnd(30, 18), rnd(40, 17)]
    after = [rnd(d, 15) for d in (50, 55, 60, 65, 70)]
    events = [
        ev("L1", "lesson", 30, focus_areas=[{"game_area": "driving", "detail": ""}]),
        ev("E1", "equipment_change", 33),
        ev("L2", "lesson", 62),
        {"event_id": "I1", "event_type": "injury", "date": None, "date_start": "2026-06-01",
         "date_end": "2026-06-05", "is_planned": False},
    ]
    out = {e["event_id"]: e for e in L.lesson_effects(before + learning + after, events, sigma=3.0, n_boot=500)}
    l1, l2 = out["L1"], out["L2"]
    assert {"regression_to_mean_risk", "confounded_with_equipment_change", "overlapping_lessons",
            "injury_in_window"} <= set(l1["flags"])
    assert "too_few_rounds" not in l1["flags"]
    assert l1["mean_before"] > l1["long_run_mean"]
    assert all(l1["flag_text"][f] for f in l1["flags"])
    # L2 has no rounds after its learning window yet.
    assert "too_few_rounds" in l2["flags"] and l2["reading"] == "insufficient_data"
    assert l2["difference"] is None and l2["mde"] is None
    assert l2["summary"].startswith("Not enough rounds to compare yet (5 before, 0 after)")


def test_offseason_gap_flag():
    rounds = [rnd(d, 15) for d in (0, 5, 10, 15, 20)] + [rnd(d, 15) for d in (120, 125, 130, 135, 140)]
    e = L.lesson_effects(rounds, [ev("L1", "lesson", 25)], sigma=3.0, n_boot=200)[0]
    assert "spans_offseason_gap" in e["flags"]


def test_focus_matched_secondary_stats():
    def with_stats(day, fir, putts):
        return rnd(day, 15, fw_hit=fir[0], fw_chances=fir[1], fir_pct=fir[0] / fir[1], putts_9=putts / 2)

    before = [with_stats(0, (1, 2), 34), with_stats(7, (9, 10), 36)]
    after = [with_stats(30, (5, 10), 31), with_stats(37, (6, 10), 33)]
    lessons = [ev("D", "lesson", 14, focus_areas=[{"game_area": "driving", "detail": "tee height"}])]
    sec = L.lesson_effects(before + after, lessons, sigma=3.0, n_boot=200)[0]["secondary"]
    assert sec["label"] == "Fairways hit" and sec["better"] == "higher"
    assert sec["mean_before"] == pytest.approx(10 / 12)          # pooled by chances, not mean of rates
    assert sec["mean_after"] == pytest.approx(11 / 20)
    lessons = [ev("P", "lesson", 14, focus_areas=[{"game_area": "mental", "detail": ""},
                                                  {"game_area": "putting", "detail": ""}])]
    sec = L.lesson_effects(before + after, lessons, sigma=3.0, n_boot=200)[0]["secondary"]
    assert sec["key"] == "putts_9" and sec["label"] == "Putts per 9 holes"
    assert sec["difference"] == pytest.approx((32 - 35) / 2)
    none = L.lesson_effects(before + after, [ev("M", "lesson", 14, focus_areas=[])], sigma=3.0, n_boot=200)[0]
    assert none["secondary"] is None and none["primary_focus"] == ""


# ----------------------------------------------------------------------------- wording
FORBIDDEN = ("caused", "because of the lesson", "thanks to", "led to", "resulted in", "due to the lesson",
             "the lesson worked", "improved your", "effect of the lesson")


def test_wording_never_claims_causality(conn):
    seed_demo(conn, end=date(2026, 9, 20))
    facts = round_facts(conn)
    report = L.lesson_report(facts, load_timeline(conn), n_boot=500)
    texts = [e["summary"] for e in report["lessons"]] + report["caveats"] + list(L.FLAG_TEXT.values())
    texts += [report["mde_sentence"], report["method"]]
    assert len(report["lessons"]) == 3
    for t in texts:
        low = t.lower()
        assert not any(p in low for p in FORBIDDEN), t
    assert any("not causal" in c for c in report["caveats"])
    for e in report["lessons"]:
        if e["reading"] != "insufficient_data":
            assert ("consistent with no change" in e["summary"]) or ("not proof of an effect" in e["summary"])


def test_lesson_report_sigma_from_own_rounds():
    rounds = [rnd(d, v) for d, v in zip(range(0, 50, 5), (12, 15, 18, 14, 16, 13, 17, 15, 14, 16))]
    rounds.append(rnd(51, 40, holes=9))                              # 9-hole doubles don't set sigma
    rep = L.lesson_report(rounds, [], n_boot=100)
    assert rep["sigma"] == pytest.approx(statistics.stdev([12, 15, 18, 14, 16, 13, 17, 15, 14, 16]))
    assert rep["mde_n"] == pytest.approx(L.mde(rep["sigma"], 5)) and rep["lessons"] == []


def test_per9_outcome_uses_nine_hole_sigma_and_two_nine_weights():
    # Per 9 holes an 18-hole round averages two nines: weight 2, and sigma comes from the 9-hole rounds.
    values = (20, 24, 22, 26, 21, 25, 23, 27)
    nines = [rnd(d, v, holes=9, weight=1.0, outcome_metric="to_par_9") for d, v in zip(range(0, 40, 5), values)]
    full = rnd(45, 30.0, holes=18, weight=2.0, outcome_metric="to_par_9")
    rep = L.lesson_report(nines + [full], [], n_boot=100)
    assert rep["sigma"] == pytest.approx(statistics.stdev(values))
    assert rep["sigma_basis"] == "9-hole rounds" and "counts as two nines" in rep["method"]
    lesson = [ev("L", "lesson", 22)]
    before = [rnd(d, 24.0, holes=9, weight=1.0, outcome_metric="to_par_9") for d in (0, 5, 10)]
    after = [rnd(40, 20.0, holes=9, weight=1.0, outcome_metric="to_par_9"),
             rnd(45, 22.0, holes=18, weight=2.0, outcome_metric="to_par_9")]
    e = L.lesson_effects(before + after, lesson, sigma=3.0, n_boot=200)[0]
    assert e["mean_after"] == pytest.approx((20 + 2 * 22) / 3) and e["eff_n_after"] == 3
    assert e["mde"] == pytest.approx(L.mde(3.0, 3, 3))
