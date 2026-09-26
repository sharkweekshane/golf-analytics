"""Lesson before/after comparisons, descriptive only (DECISIONS.md item 2: n < 30 rounds, no PyMC).

For each lesson: the n rounds before it vs the n rounds after a learning window (default 14 days,
allowing for the usual post-lesson dip). Rounds inside the learning window, including the lesson
day itself, belong to neither side.

Intervals are percentile bootstraps over rounds. They are for display: at 5 vs 5 a percentile
interval under-covers (about 88% actual coverage for a nominal 95% in the research simulation), so
every comparison is shown next to the minimum detectable effect (MDE) computed from Shane's own
round-to-round spread. Nothing here estimates a causal effect; the wording never claims one.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Sequence

import numpy as np

from .stats import outcome_sigma, weighted_mean

MDE_MULTIPLIER = 2.8          # z(0.975) + z(0.80) = 1.96 + 0.84: 80% power, two-sided alpha = 0.05
OFFSEASON_GAP_DAYS = 60
LESSON_TYPES = frozenset({"lesson"})
EQUIPMENT_TYPES = frozenset({"equipment_change", "fitting"})

# game_area -> (fact key, weight key, label, which direction is better)
SECONDARY: dict[str, tuple[str, str, str, str]] = {
    "putting": ("putts_9", "_w9", "Putts per 9 holes", "lower"),
    "driving": ("fir_pct", "fw_chances", "Fairways hit", "higher"),
    "approach": ("gir_pct", "gir_chances", "Greens in regulation", "higher"),
    "full_swing": ("gir_pct", "gir_chances", "Greens in regulation", "higher"),
    "short_game": ("scramble_pct", "scramble_chances", "Scrambling", "higher"),
    "bunker": ("scramble_pct", "scramble_chances", "Scrambling", "higher"),
    "course_management": ("dbl_rate", "holes", "Double bogey+ per hole", "lower"),
}

FLAG_TEXT = {
    "too_few_rounds": "Fewer rounds than planned on at least one side; the comparison is very noisy.",
    "regression_to_mean_risk": ("The rounds before were worse than the long-run average. Scores tend to "
                                "drift back toward average on their own, which looks like improvement."),
    "confounded_with_equipment_change": ("Equipment changed around the same time, so the two can't be "
                                         "separated."),
    "overlapping_lessons": "Another lesson falls inside these windows, so the comparison mixes both.",
    "injury_in_window": "An injury overlaps these windows.",
    "spans_offseason_gap": (f"There is a gap of more than {OFFSEASON_GAP_DAYS} days between rounds in these "
                            "windows; rust and season can move scores."),
}

CAVEATS = [
    "These are before/after descriptions, not causal estimates: practice, weather, courses, tees, form "
    "and luck all change too.",
    "Regression to the mean: lessons often follow a bad stretch, and scores usually recover toward "
    "average without any help. That can look like a 2-stroke gain when nothing changed.",
    "Equipment changed alongside a lesson cannot be separated from it.",
    "Seasonality, course and tee mix, and playing conditions (PCC is not applied) all add noise.",
    "Intervals are percentile bootstraps over only a few rounds; they are narrower than they should "
    "be at this sample size.",
]


def _d(s: str | date) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def mde(sigma: float, n_before: float, n_after: float | None = None) -> float:
    """Minimum detectable difference in means at 80% power, alpha 0.05: 2.8 * sigma * sqrt(1/n1 + 1/n2).

    With equal windows this is the familiar 2.8 * sigma * sqrt(2/n).
    """
    n_after = n_before if n_after is None else n_after
    return MDE_MULTIPLIER * sigma * math.sqrt(1 / n_before + 1 / n_after)


def mde_sentence(sigma: float | None, n: int = 5, unit: str = "strokes", per9: float | None = None) -> str:
    """The MDE in words. Neutral voice (it also appears on the public page). per9 converts the unit to
    strokes per 9 holes (differential points: slope/113/2), because every other score on the dashboard
    is per nine: 13 differential points are only about 6.5 strokes on a nine at slope 113."""
    if sigma is None:
        return ("The round-to-round spread can't be estimated yet (it needs at least 3 rated rounds), "
                "so no before/after change can be judged.")
    m = mde(sigma, n)
    in_strokes = f" (roughly {m * per9:.1f} strokes per 9 holes)" if per9 else ""
    return (f"With a round-to-round spread of σ≈{sigma:.1f} {unit}, a change must be about "
            f"{m:.1f} {unit}{in_strokes} to stand out with {n} rounds each side "
            f"(about {mde(sigma, 10):.1f} with 10, {mde(sigma, 20):.1f} with 20).")


def outcome_unit(rounds: Sequence[dict]) -> tuple[str, float | None]:
    """(unit name, strokes per 9 holes per unit) for the rounds' outcome metric. A differential point is
    slope/113 strokes over 18 holes, so slope/113/2 per nine (averaged over the rated rounds)."""
    metric = rounds[0].get("outcome_metric") if rounds else None
    if metric != "differential":
        return "strokes", None
    slopes = [(r.get("slope9") if r.get("holes") == 9 else r.get("slope")) for r in rounds
              if r.get("outcome") is not None]
    slopes = [s for s in slopes if s]
    return "differential points", (sum(slopes) / len(slopes) / 113 / 2 if slopes else 0.5)


def _boot_means(rng: np.random.Generator, values: np.ndarray, weights: np.ndarray, n_boot: int) -> np.ndarray:
    idx = rng.integers(0, len(values), size=(n_boot, len(values)))
    w = weights[idx]
    return (values[idx] * w).sum(axis=1) / w.sum(axis=1)


def _pct(draws: np.ndarray, level: float) -> list[float]:
    a = (1 - level) / 2
    return [float(np.quantile(draws, a)), float(np.quantile(draws, 1 - a))]


def bootstrap_diff(before: Sequence[float], after: Sequence[float], *,
                   w_before: Sequence[float] | None = None, w_after: Sequence[float] | None = None,
                   n_boot: int = 4000, seed: int | Sequence[int] = 7,
                   levels: Sequence[float] = (0.8, 0.95)) -> dict[str, Any]:
    """Weighted mean(after) - mean(before) with percentile bootstrap intervals, resampling rounds
    within each window. Also returns each window mean's own intervals (for the dumbbell chart).
    Deterministic for a given seed.
    """
    b = np.asarray(before, float)
    a = np.asarray(after, float)
    wb = np.ones_like(b) if w_before is None else np.asarray(w_before, float)
    wa = np.ones_like(a) if w_after is None else np.asarray(w_after, float)
    rng = np.random.default_rng(seed)
    mb = _boot_means(rng, b, wb, n_boot)
    ma = _boot_means(rng, a, wa, n_boot)
    diff = ma - mb
    key = lambda lv: str(int(round(lv * 100)))  # noqa: E731
    return {
        "mean_before": float((b * wb).sum() / wb.sum()),
        "mean_after": float((a * wa).sum() / wa.sum()),
        "difference": float((a * wa).sum() / wa.sum() - (b * wb).sum() / wb.sum()),
        "intervals": {key(lv): _pct(diff, lv) for lv in levels},
        "before_intervals": {key(lv): _pct(mb, lv) for lv in levels},
        "after_intervals": {key(lv): _pct(ma, lv) for lv in levels},
        "n_boot": n_boot,
        "method": "percentile bootstrap (display only; under-covers at small n)",
    }


def lesson_windows(rounds: Sequence[dict], events: Sequence[dict], n_before: int = 5, n_after: int = 5,
                   learning_window_days: int = 14) -> list[dict]:
    """Split rounds with an outcome around each past lesson.

    before: the last n_before rounds dated before the lesson day.
    learning: rounds from the lesson day up to lesson + learning_window_days (excluded from both sides).
    after: the first n_after rounds on/after lesson + learning_window_days.
    """
    rs = sorted((r for r in rounds if r.get("outcome") is not None), key=lambda r: (r["date"], r["round_id"]))
    lessons = sorted((e for e in events if e.get("event_type") in LESSON_TYPES and not e.get("is_planned")
                      and e.get("date")), key=lambda e: (e["date"], e["event_id"]))
    out = []
    for lesson in lessons:
        day = _d(lesson["date"])
        after_start = day + timedelta(days=learning_window_days)
        out.append({
            "lesson": lesson,
            "after_start": after_start.isoformat(),
            "before": [r for r in rs if _d(r["date"]) < day][-n_before:] if n_before else [],
            "learning": [r for r in rs if day <= _d(r["date"]) < after_start],
            "after": [r for r in rs if _d(r["date"]) >= after_start][:n_after],
        })
    return out


def _secondary(win: dict, focus: str, seed: Sequence[int], n_boot: int) -> dict | None:
    spec = SECONDARY.get(focus)
    if not spec:
        return None
    key, wkey, label, better = spec

    def pts(rs: list[dict]) -> tuple[list[float], list[float]]:
        vals, wts = [], []
        for r in rs:
            w = (r.get("holes") or 0) / 9 if wkey == "_w9" else (r.get(wkey) or 0)
            if r.get(key) is not None and w:
                vals.append(r[key])
                wts.append(w)
        return vals, wts

    bv, bw = pts(win["before"])
    av, aw = pts(win["after"])
    out = {"focus": focus, "key": key, "label": label, "better": better, "unit": "pct" if key.endswith("_pct")
           or key == "dbl_rate" else "count", "n_before": len(bv), "n_after": len(av),
           "mean_before": weighted_mean(bv, bw), "mean_after": weighted_mean(av, aw),
           "difference": None, "interval_80": None}
    if len(bv) >= 2 and len(av) >= 2:
        bt = bootstrap_diff(bv, av, w_before=bw, w_after=aw, n_boot=n_boot, seed=seed, levels=(0.8,))
        out["difference"] = bt["difference"]
        out["interval_80"] = bt["intervals"]["80"]
    elif out["mean_before"] is not None and out["mean_after"] is not None:
        out["difference"] = out["mean_after"] - out["mean_before"]
    return out


def _span(win: dict, learning_window_days: int) -> tuple[date, date]:
    day = _d(win["lesson"]["date"])
    start = _d(win["before"][0]["date"]) if win["before"] else day
    end = _d(win["after"][-1]["date"]) if win["after"] else day + timedelta(days=learning_window_days)
    return start, end


def _flags(win: dict, events: Sequence[dict], long_run_mean: float | None, mean_before: float | None,
           n_before: int, n_after: int, learning_window_days: int) -> list[str]:
    lesson = win["lesson"]
    day = _d(lesson["date"])
    start, end = _span(win, learning_window_days)
    flags = []
    if len(win["before"]) < n_before or len(win["after"]) < n_after:
        flags.append("too_few_rounds")
    if long_run_mean is not None and mean_before is not None and mean_before > long_run_mean:
        flags.append("regression_to_mean_risk")
    lo_equip = day - timedelta(days=learning_window_days)
    for e in events:
        if e.get("is_planned") or not (e.get("date") or e.get("date_start")):
            continue
        e_day = _d(e.get("date") or e["date_start"])
        if e.get("event_type") in EQUIPMENT_TYPES and lo_equip <= e_day <= end:
            flags.append("confounded_with_equipment_change")
        if (e.get("event_type") in LESSON_TYPES and e.get("event_id") != lesson.get("event_id")
                and start <= e_day <= end):
            flags.append("overlapping_lessons")
        if e.get("event_type") == "injury":
            i0 = _d(e.get("date_start") or e["date"])
            i1 = _d(e.get("date_end") or e.get("date") or e["date_start"])
            if i0 <= end and i1 >= start:
                flags.append("injury_in_window")
    days = [_d(r["date"]) for r in win["before"] + win["learning"] + win["after"]]
    if any((b - a).days > OFFSEASON_GAP_DAYS for a, b in zip(days, days[1:])):
        flags.append("spans_offseason_gap")
    return list(dict.fromkeys(flags))


def _fmt_signed(x: float) -> str:
    return f"{x:+.1f}".replace("-", "−")


def _summary(eff: dict, unit: str, n_before_target: int, n_after_target: int) -> str:
    nb, na = eff["n_before"], eff["n_after"]
    if eff["reading"] == "insufficient_data":
        need_b, need_a = max(0, 2 - nb), max(0, 2 - na)
        parts = []
        if need_a:
            parts.append(f"{need_a} more round{'s' if need_a > 1 else ''} after the learning window")
        if need_b:
            parts.append(f"{need_b} more round{'s' if need_b > 1 else ''} before the lesson")
        return (f"Not enough rounds to compare yet ({nb} before, {na} after). "
                f"Needs at least {' and '.join(parts)}; {n_after_target} each side is the plan.")
    d = eff["difference"]
    lo, hi = eff["interval_95"]
    word = "lower" if d < 0 else "higher"
    text = (f"The {na} round{'s' if na != 1 else ''} after averaged {abs(d):.1f} {unit} {word} than the "
            f"{nb} before (95% interval {_fmt_signed(lo)} to {_fmt_signed(hi)}).")
    if eff["mde"] is not None:
        rel = "smaller" if abs(d) < eff["mde"] else "larger"
        text += (f" That is {rel} than the ~{eff['mde']:.1f}-{unit[:-1] if unit.endswith('s') else unit} "
                 "change the round-to-round spread can reliably reveal with these window sizes.")
    if eff["reading"] == "within_noise":
        text += " It is consistent with no change."
    else:
        text += " The difference is clear of zero, but this is a before/after description, not proof of an effect."
    return text


def lesson_effects(rounds: Sequence[dict], events: Sequence[dict], *, n_before: int = 5, n_after: int = 5,
                   learning_window_days: int = 14, sigma: float | None = None, n_boot: int = 4000,
                   seed: int = 7, unit: str = "strokes") -> list[dict]:
    """Per-lesson before/after comparison of the round outcome, with caveat flags.

    `rounds` are round facts (need date, round_id, outcome, weight; secondary stats optional);
    `events` are timeline events (need event_type, date, event_id; focus_areas for the secondary stat).
    sigma defaults to the SD of Shane's own 18-hole outcomes (stats.outcome_sigma).
    reading: insufficient_data (< 2 rounds on a side) | lower_after / higher_after (95% interval clear
    of zero) | within_noise.
    """
    if sigma is None:
        sigma = outcome_sigma(rounds)["sigma"]
    scored = [r for r in rounds if r.get("outcome") is not None]
    long_run = weighted_mean([r["outcome"] for r in scored], [r.get("weight", 1.0) for r in scored])
    results = []
    for i, win in enumerate(lesson_windows(rounds, events, n_before, n_after, learning_window_days)):
        lesson = win["lesson"]
        focus = [f.get("game_area", "") for f in (lesson.get("focus_areas") or []) if isinstance(f, dict)]
        primary = next((f for f in focus if f in SECONDARY), focus[0] if focus else "")
        bv = [r["outcome"] for r in win["before"]]
        bw = [r.get("weight", 1.0) for r in win["before"]]
        av = [r["outcome"] for r in win["after"]]
        aw = [r.get("weight", 1.0) for r in win["after"]]
        eff: dict[str, Any] = {
            "index": i + 1,
            "event_id": lesson.get("event_id"), "date": str(lesson["date"])[:10],
            "coach": lesson.get("coach") or "", "focus_areas": lesson.get("focus_areas") or [],
            "primary_focus": primary, "summary_note": lesson.get("summary") or "",
            "n_before": len(bv), "n_after": len(av), "n_learning": len(win["learning"]),
            "eff_n_before": sum(bw), "eff_n_after": sum(aw),
            "before_round_ids": [r["round_id"] for r in win["before"]],
            "after_round_ids": [r["round_id"] for r in win["after"]],
            "learning_round_ids": [r["round_id"] for r in win["learning"]],
            "after_start": win["after_start"],
            "mean_before": weighted_mean(bv, bw), "mean_after": weighted_mean(av, aw),
            "difference": None, "interval_80": None, "interval_95": None,
            "before_interval_95": None, "after_interval_95": None,
            "mde": None, "long_run_mean": long_run, "sigma": sigma,
            "interval_method": "percentile bootstrap",
        }
        seed_i = [seed, i]
        if len(bv) >= 2 and len(av) >= 2:
            bt = bootstrap_diff(bv, av, w_before=bw, w_after=aw, n_boot=n_boot, seed=seed_i)
            eff.update(difference=bt["difference"], interval_80=bt["intervals"]["80"],
                       interval_95=bt["intervals"]["95"], before_interval_95=bt["before_intervals"]["95"],
                       after_interval_95=bt["after_intervals"]["95"])
            if sigma is not None:
                eff["mde"] = mde(sigma, eff["eff_n_before"], eff["eff_n_after"])
            lo, hi = eff["interval_95"]
            eff["reading"] = "lower_after" if hi < 0 else "higher_after" if lo > 0 else "within_noise"
        else:
            eff["reading"] = "insufficient_data"
        eff["secondary"] = _secondary(win, primary, [seed, i, 1], n_boot) if primary else None
        eff["flags"] = _flags(win, events, long_run, eff["mean_before"], n_before, n_after, learning_window_days)
        eff["flag_text"] = {f: FLAG_TEXT[f] for f in eff["flags"]}
        eff["summary"] = _summary(eff, unit, n_before, n_after)
        results.append(eff)
    return results


def lesson_report(rounds: Sequence[dict], events: Sequence[dict], *, n_before: int = 5, n_after: int = 5,
                  learning_window_days: int = 14, n_boot: int = 4000, seed: int = 7,
                  unit: str | None = None) -> dict[str, Any]:
    """Everything the lesson panel shows: sigma, the MDE sentence, per-lesson effects and caveats.
    The unit follows the outcome metric unless given (differential points carry a per-9 translation)."""
    sig = outcome_sigma(rounds)
    auto_unit, strokes_per9 = outcome_unit(rounds)
    unit = unit or auto_unit
    per9 = bool(rounds) and rounds[0].get("outcome_metric") == "to_par_9"
    weighting = ("an 18-hole round counts as two nines" if per9 else "9-hole rounds count half")
    effects = lesson_effects(rounds, events, n_before=n_before, n_after=n_after,
                             learning_window_days=learning_window_days, sigma=sig["sigma"],
                             n_boot=n_boot, seed=seed, unit=unit)
    return {
        "sigma": sig["sigma"], "sigma_n": sig["n"], "sigma_basis": sig["basis"],
        "sigma_mssd": sig["mssd_sigma"],
        "n_before": n_before, "n_after": n_after, "learning_window_days": learning_window_days,
        "mde_n": mde(sig["sigma"], n_before, n_after) if sig["sigma"] is not None else None,
        "mde_sentence": mde_sentence(sig["sigma"], n_before, unit, strokes_per9 if unit == auto_unit else None),
        "unit": unit, "strokes_per_unit_9": strokes_per9,
        "lessons": effects,
        "caveats": CAVEATS,
        "method": ("Mean outcome of the rounds before each lesson vs the rounds after a "
                   f"{learning_window_days}-day learning window; {weighting}. Intervals are "
                   "percentile bootstraps over rounds (display only)."),
    }
