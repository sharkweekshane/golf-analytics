"""Round-level facts and descriptive statistics (RESEARCH_PLAN §5, tiers A/B/C).

Only `round_facts`, `hi_series` and `load_timeline` read the database; everything else is a pure
function over plain lists/dicts so it can be checked on hand-computed cases. Every number here is
descriptive: rates carry Wilson intervals, trends are exponentially weighted means, nothing is causal.

Outcome per round ("outcome"), one of three scales (set_outcome):
- "differential": the score differential when the tee is rated. 9-hole rounds enter as 2 x their own
  9-hole differential at weight 0.5, never as the WHS-scaled 18-hole value, because half of that value
  is the prior Handicap Index and would pull every before/after comparison toward "no change".
- "to_par": strokes over par on the 18-hole scale (9-hole rounds doubled, weight 0.5).
- "to_par_9": strokes over par per 9 holes (18-hole rounds halved, weight 2 = two nines). This is the
  dashboard's primary score for a golfer who mostly plays nine holes.

Per-round counting stats come per 18 (putts_18, dbl_18, ...; MCP and older callers) and per 9
(putts_9, dbl_9, ...; the dashboard). Putts count only when tracked on every hole: a total below one
putt per hole, or a round whose export shows holes without putts, is partial tracking and is left out
of every putting stat (unless Shane says otherwise with `golf round putts`).
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from ..db import loads

EWMA_LAMBDA = 0.25                 # NIST suggests 0.2-0.3; 0.25 ~ an effective window of 7 rounds
Z80 = 1.2815515655446004
Z95 = 1.959963984540054
FAIRWAY_MISSES = frozenset({"left", "right", "short", "long", "miss"})
EVENT_JSON_COLS = ("focus_areas", "drills", "swing_thoughts", "equipment", "measurements", "injury", "flags")
OUTCOME_METRICS = ("differential", "to_par", "to_par_9")
SG_SENTINEL = 100                  # 18Birdies writes strokesGained = 100 when it has no value
MIN_SHOT_YARDS = 5.0               # GPS shot distances outside [5, 400] yards are treated as invalid
MAX_SHOT_YARDS = 400.0
AFTER_ROUND_GAP_S = 45             # a round's shots logged a median < 45 s apart were entered after play


# ----------------------------------------------------------------------------- pure helpers
def wilson(k: float, n: float, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for k/n. Unlike p +/- z*se it stays inside [0, 1] and is honest at small n."""
    if not n:
        return None
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def weighted_mean(values: Sequence[float | None], weights: Sequence[float] | None = None) -> float | None:
    num = den = 0.0
    for i, v in enumerate(values):
        w = 1.0 if weights is None else weights[i]
        if v is None or not w:
            continue
        num += w * v
        den += w
    return num / den if den else None


def ewma(values: Sequence[float | None], lam: float = EWMA_LAMBDA,
         weights: Sequence[float] | None = None) -> list[float | None]:
    """Running exponentially weighted mean, normalised like pandas ewm(alpha=lam, adjust=True).

    Normalising (instead of seeding with the first value) keeps round 1 from dominating a short history.
    `weights` scale each observation (9-hole rounds at 0.5; rates weighted by chances, which makes the
    result a ratio of smoothed hits to smoothed chances). None values are skipped and the last level
    carried forward, so time is counted in rounds that have the stat.
    """
    num = den = 0.0
    out: list[float | None] = []
    for i, v in enumerate(values):
        w = 1.0 if weights is None else weights[i]
        if v is not None and w:
            num = num * (1 - lam) + w * v
            den = den * (1 - lam) + w
        out.append(num / den if den else None)
    return out


def ewma_band(values: Sequence[float | None], lam: float = EWMA_LAMBDA,
              weights: Sequence[float] | None = None, *, sigma: float | None,
              z: float = Z80) -> list[tuple[float, float] | None]:
    """Interval for the smoothed level if rounds were independent with sd sigma/sqrt(weight).

    It shows how far the line could wobble from round-to-round noise alone. It is not a forecast band.
    """
    levels = ewma(values, lam, weights)
    s1 = s2 = 0.0
    out: list[tuple[float, float] | None] = []
    for i, v in enumerate(values):
        w = 1.0 if weights is None else weights[i]
        if v is not None and w:
            s1 = s1 * (1 - lam) + w
            s2 = s2 * (1 - lam) ** 2 + w
        m = levels[i]
        if m is None or sigma is None or not s1:
            out.append(None)
        else:
            se = sigma * math.sqrt(s2) / s1
            out.append((m - z * se, m + z * se))
    return out


def spread(values: Sequence[float | None]) -> dict[str, Any]:
    """Mean, SD, MAD (raw and x1.4826 as a sigma estimate) and the successive-difference sigma.

    The MSSD sigma, sqrt(sum (x_t - x_{t-1})^2 / 2(n-1)), is barely inflated by a slow trend or a
    one-off step, so it is a useful check on the plain SD when scores are genuinely changing.
    """
    v = [float(x) for x in values if x is not None]
    n = len(v)
    out: dict[str, Any] = {"n": n, "mean": None, "sd": None, "mad": None, "mad_sigma": None, "mssd_sigma": None}
    if not n:
        return out
    med = statistics.median(v)
    mad = statistics.median(abs(x - med) for x in v)
    out.update(mean=statistics.fmean(v), mad=mad, mad_sigma=1.4826 * mad)
    if n >= 2:
        out["sd"] = statistics.stdev(v)
        out["mssd_sigma"] = math.sqrt(sum((b - a) ** 2 for a, b in zip(v, v[1:])) / (2 * (n - 1)))
    return out


def rolling(values: Sequence[float | None], window: int = 20, min_periods: int = 3) -> list[dict | None]:
    """spread() over the trailing `window` values at each position (None until min_periods values)."""
    out: list[dict | None] = []
    for i in range(len(values)):
        win = [x for x in values[max(0, i - window + 1): i + 1] if x is not None]
        out.append(spread(win) if len(win) >= min_periods else None)
    return out


def identity_split(gross: int, putts: int, par: int, holes: int) -> tuple[int, int]:
    """Exact split of to-par: (strokes to reach the green over regulation, putts over two per hole).

    to_par = [(gross - putts) - (par - 2*holes)] + [putts - 2*holes]. Needs only round totals.
    """
    return (gross - putts) - (par - 2 * holes), putts - 2 * holes


def hole_stats(holes: Sequence[dict]) -> dict[str, Any]:
    """Tier B/C counts from per-hole rows (strokes, par, putts, fairway, gir, penalties).

    GIR missing on a hole with strokes, putts and par is derived as strokes - putts <= par - 2 and
    counted in `gir_derived` (chip-ins and fringe putts can fool it, so it is never written back).
    """
    s: dict[str, Any] = defaultdict(int)
    s["par_type"] = {3: [0, 0], 4: [0, 0], 5: [0, 0]}
    s["strokes_complete"] = bool(holes) and all(h.get("strokes") is not None for h in holes)
    s["pars_complete"] = bool(holes) and all(h.get("par") is not None for h in holes)
    for h in holes:
        strokes, par, putts = h.get("strokes"), h.get("par"), h.get("putts")
        if strokes is not None and par is not None:
            s["holes_scored"] += 1
            if par in s["par_type"]:
                s["par_type"][par][0] += strokes - par
                s["par_type"][par][1] += 1
            if strokes >= par + 2:
                s["dbl_holes"] += 1
            if strokes >= par + 3:
                s["triple_holes"] += 1
        if putts is not None:
            s["putt_holes"] += 1
            s["putts_sum"] += putts
            s["three_putts"] += putts >= 3
            s["one_putts"] += putts == 1
        fw = h.get("fairway")
        if fw == "hit" or fw in FAIRWAY_MISSES:
            s["fw_chances"] += 1
            s["fw_hit"] += fw == "hit"
        gir = h.get("gir")
        if gir is None and None not in (strokes, putts, par):
            gir = int(strokes - putts <= par - 2)
            s["gir_derived"] += 1
        if gir is not None:
            s["gir_holes"] += 1
            s["gir_hit"] += bool(gir)
            if gir and putts is not None:
                s["gir_putt_holes"] += 1
                s["gir_putts_sum"] += putts
            if not gir and strokes is not None and par is not None:
                s["scramble_chances"] += 1
                s["scrambles"] += strokes <= par
        if h.get("penalties") is not None:
            s["penalty_holes"] += 1
            s["penalties"] += h["penalties"]
    return dict(s)


def _ratio(k: float | None, n: float | None) -> float | None:
    return k / n if (k is not None and n) else None


def _nine_rating(tee: dict | None, nine: str | None) -> tuple[float | None, float | None]:
    """9-hole CR/Slope for the nine played; unknown nine falls back to half the 18-hole rating."""
    if not tee:
        return None, None
    if nine == "front" and tee.get("cr_f9") and tee.get("slope_f9"):
        return tee["cr_f9"], tee["slope_f9"]
    if nine == "back" and tee.get("cr_b9") and tee.get("slope_b9"):
        return tee["cr_b9"], tee["slope_b9"]
    if tee.get("cr18") and tee.get("slope18"):
        return tee["cr18"] / 2, tee["slope18"]
    return None, None


# ----------------------------------------------------------------------------- database readers
def round_facts(conn: sqlite3.Connection, metric: str | None = None) -> list[dict]:
    """One dict per counted round (v_rounds, excluded = 0), oldest first.

    Merges round totals, per-hole rows (par/SI filled from the effective tee when the export lacks
    them), handicap_history and derived Tier A/B/C stats. `metric` forces the outcome
    ("differential" | "to_par"); by default differentials are used when at least 3 rounds and half of
    all rounds have one, otherwise to-par. Rounds without the chosen outcome get outcome=None.
    """
    rows = conn.execute(
        """SELECT v.*, c.name AS course_name, cl.name AS club_name
           FROM v_rounds v
           LEFT JOIN courses c ON c.course_key = v.course_key_eff
           LEFT JOIN clubs cl ON cl.club_id = v.club_id
           WHERE v.excluded = 0
           ORDER BY v.played_on_local, v.played_at_utc, v.round_id"""
    ).fetchall()
    tees = {r["tee_id"]: dict(r) for r in conn.execute("SELECT * FROM tees")}
    default_tee: dict[str, str] = {}
    for r in conn.execute("SELECT course_key, tee_id FROM tees WHERE is_default = 1 ORDER BY tee_id"):
        default_tee.setdefault(r["course_key"], r["tee_id"])
    tee_holes: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM tee_holes"):
        tee_holes[r["tee_id"]][r["hole"]] = dict(r)
    holes_by_round: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM round_holes ORDER BY round_id, hole"):
        holes_by_round[r["round_id"]].append(dict(r))
    hist = {r["round_id"]: dict(r) for r in conn.execute("SELECT * FROM handicap_history")}

    facts = [_round_fact(dict(r), tees, default_tee, tee_holes, holes_by_round, hist) for r in rows]
    return set_outcome(facts, metric or choose_metric(facts))


def choose_metric(facts: Sequence[dict]) -> str:
    """Differentials when enough rounds are rated; otherwise to-par (never a mix on one axis)."""
    n_diff = sum(f["diff_equiv"] is not None for f in facts)
    n_par = sum(f["to_par_equiv"] is not None for f in facts)
    return "differential" if n_diff >= 3 and n_diff >= 0.5 * n_par else ("to_par" if n_par else "differential")


def lesson_metric(facts: Sequence[dict]) -> str:
    """The scale lesson comparisons use everywhere (dashboard, `golf analyze lessons`, MCP): score
    differentials when enough rounds are rated, else strokes over par per 9 holes."""
    return "differential" if choose_metric(facts) == "differential" else "to_par_9"


def set_outcome(facts: list[dict], metric: str) -> list[dict]:
    """Set outcome, outcome_metric and weight on every fact, in place (returns the same list).

    weight is the round's information in the metric's own unit: on the 18-hole scales an 18-hole round
    is 1 and a nine 0.5; per 9 holes a nine is 1 and an 18-hole round 2. Weighted means do not care,
    but noise bands and the MDE do, because sigma is estimated in the native unit (outcome_sigma).
    """
    if metric not in OUTCOME_METRICS:
        raise ValueError(f"unknown outcome metric {metric!r}")
    key = {"differential": "diff_equiv", "to_par": "to_par_equiv", "to_par_9": "to_par_9"}[metric]
    for f in facts:
        f["outcome_metric"] = metric
        f["outcome"] = f[key]
        f["weight"] = f["nines"] if metric == "to_par_9" else f["nines"] / 2
    return facts


def group_of(course_key: str | None, club_id: str | None) -> str:
    """Stable id for 'where the round was played': the mapped course, else the 18Birdies club."""
    return course_key or (f"club:{club_id}" if club_id else "club:unknown")


def _num(v: Any) -> float | None:
    """REAL-ish column value -> float (18Birdies sends some numbers as strings, e.g. '43.8')."""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _sg(v: Any) -> float | None:
    x = _num(v)
    return None if x is None or abs(x) >= SG_SENTINEL else x


def _round_fact(r: dict, tees: dict, default_tee: dict, tee_holes: dict, holes_by_round: dict,
                hist: dict) -> dict:
    rid = r["round_id"]
    tee_id = r.get("tee_id_eff") or default_tee.get(r.get("course_key_eff"))
    tee = tees.get(tee_id)
    th = tee_holes.get(tee_id, {})
    dq = loads(r.get("dq_flags"), []) or []
    holes = []
    for h in holes_by_round.get(rid, []):
        ref = th.get(h["hole"], {})
        holes.append({
            "hole": h["hole"], "strokes": h["strokes"],
            "par": h["par"] if h["par"] is not None else ref.get("par"),
            "si": h["si"] if h["si"] is not None else ref.get("si"),
            "putts": h["putts"], "fairway": h["fairway"], "gir": h["gir"], "gir_miss": h["gir_miss"],
            "penalties": h["penalties"], "stats_src": h["stats_src"],
        })
    per_hole_ok = (r["entry_mode"] == "hole_by_hole" and "sum_mismatch" not in dq
                   and bool(holes) and all(h["strokes"] is not None for h in holes))
    hs = hole_stats(holes if per_hole_ok else [dict(h, strokes=None) for h in holes])

    n_holes = r.get("holes_played") or (len(holes) if holes else None)
    gross = r.get("gross")
    par_played = r.get("par_played")
    if par_played is None and gross is not None and r.get("to_par") is not None:
        par_played = gross - r["to_par"]
    if par_played is None and per_hole_ok and hs["pars_complete"]:
        par_played = sum(h["par"] for h in holes)
    to_par = r.get("to_par")
    if to_par is None and gross is not None and par_played is not None:
        to_par = gross - par_played

    fw_hit, fw_ch = r.get("fw_hit"), r.get("fw_chances")
    if not fw_ch:
        fw_hit, fw_ch = (hs.get("fw_hit"), hs.get("fw_chances")) if hs.get("fw_chances") else (None, None)
    gir_hit, gir_ch = r.get("gir"), r.get("gir_chances")
    if not gir_ch:
        gir_hit, gir_ch = (hs.get("gir_hit"), hs.get("gir_holes")) if hs.get("gir_holes") else (None, None)
    # Putt rule: a round's putt total counts only when tracked on every hole. The importer decides
    # (>= 1 putt per hole, and 18Birdies' GIR coverage when the export has it) and marks the rest
    # putts_tracked = 0 plus 'putts_partial'; fewer putts than holes is caught here too (older
    # databases). Shane's own call (round_overrides.putts, as v_rounds.putts_override) beats the rule
    # both ways. Complete per-hole putts (from screenshots) stand in for a partial or missing total.
    putts_recorded = r.get("putts") or None
    override = r.get("putts_override")
    total_partial = "putts_partial" in dq or bool(putts_recorded and n_holes and putts_recorded < n_holes)
    tracked = bool(r.get("putts_tracked"))
    if override == "partial":
        total_partial, tracked = True, False
    elif override == "full" and putts_recorded:
        total_partial, tracked = False, True
    putts = putts_recorded if (tracked and putts_recorded and not total_partial) else None
    if putts is None and holes and all(h["putts"] is not None for h in holes):
        putts = sum(h["putts"] for h in holes)
        if n_holes and putts < n_holes:
            putts = None
    putts_partial = total_partial and putts is None

    dbl = r.get("dbl_plus") if r["entry_mode"] == "hole_by_hole" else None
    if dbl is None and per_hole_ok and hs["pars_complete"]:
        dbl = hs.get("dbl_holes", 0)
    # Triple bogey or worse needs every hole's strokes and par (the export only counts doubles-or-worse):
    # for a high handicapper nearly every hole is a double, so triples separate good rounds from bad.
    triple = hs.get("triple_holes", 0) if per_hole_ok and hs["pars_complete"] else None

    to_green = putts_over = None
    if None not in (gross, putts, par_played, n_holes):
        to_green, putts_over = identity_split(gross, putts, par_played, n_holes)

    per18 = (18 / n_holes) if n_holes else None
    scale = (lambda x: x * per18 if (x is not None and per18) else None)
    scale9 = (lambda x: x * 9 / n_holes if (x is not None and n_holes) else None)

    # Outcome equivalents (see module docstring).
    h = hist.get(rid) or {}
    cr, slope = (tee or {}).get("cr18"), (tee or {}).get("slope18")
    nine = r.get("nine_eff") or r.get("nine")      # round_overrides.nine wins when the view carries it
    cr9, slope9 = _nine_rating(tee, nine)
    rated = bool(cr9 and slope9) if n_holes == 9 else bool(cr and slope)
    ags = h.get("ags") if h.get("ags") is not None else gross
    diff_equiv = to_par_equiv = None
    weight = 1.0 if n_holes == 18 else 0.5 if n_holes == 9 else 0.0
    if n_holes == 18:
        if h.get("differential") is not None and h.get("differential_kind") != "nine_hole_scaled":
            diff_equiv = h["differential"]
        elif rated and ags is not None:
            diff_equiv = 113 / slope * (ags - cr)
    elif n_holes == 9 and rated and ags is not None:
        diff_equiv = 2 * 113 / slope9 * (ags - cr9)
    if to_par is not None and n_holes in (9, 18):
        to_par_equiv = to_par * 18 / n_holes

    tier_b = per_hole_ok and hs["pars_complete"]
    # An 18-hole round's two nines, when every hole has strokes and par (for the 'best 9' figure).
    halves = None
    if tier_b and n_holes == 18 and len(holes) == 18:
        front = [x for x in holes if x["hole"] <= 9]
        back = [x for x in holes if x["hole"] > 9]
        if len(front) == 9 and len(back) == 9:
            halves = {"front": sum(x["strokes"] - x["par"] for x in front),
                      "back": sum(x["strokes"] - x["par"] for x in back),
                      "front_gross": sum(x["strokes"] for x in front),
                      "back_gross": sum(x["strokes"] for x in back)}
    tier_c = any(x["putts"] is not None or x["gir"] is not None or x["fairway"] is not None for x in holes)
    pt = hs["par_type"]
    fact = {
        "round_id": rid, "date": r["played_on_local"], "source": r["source"], "entry_mode": r["entry_mode"],
        "holes": n_holes, "nine": nine, "is_nine": n_holes == 9,
        "gross": gross, "to_par": to_par, "par_played": par_played,
        "club_id": r.get("club_id"), "club_name": r.get("club_name"),
        "course_key": r.get("course_key_eff"), "course_name": r.get("course_name"),
        "tee_id": tee_id, "rated": rated, "cr": cr, "slope": slope,
        "fw_hit": fw_hit, "fw_chances": fw_ch, "fir_pct": _ratio(fw_hit, fw_ch),
        "gir_hit": gir_hit, "gir_chances": gir_ch, "gir_pct": _ratio(gir_hit, gir_ch),
        "putts": putts, "putts_18": scale(putts),
        "dbl_plus": dbl, "dbl_rate": _ratio(dbl, n_holes), "dbl_18": scale(dbl),
        "eagles_plus": r.get("eagles_plus"), "birdies": r.get("birdies"), "pars": r.get("pars"),
        "bogeys": r.get("bogeys"),
        "to_green": to_green, "putts_over": putts_over,
        "to_green_18": scale(to_green), "putts_over_18": scale(putts_over),
        "par3_avg": _ratio(*pt[3]) if tier_b else None, "par3_n": pt[3][1] if tier_b else 0,
        "par4_avg": _ratio(*pt[4]) if tier_b else None, "par4_n": pt[4][1] if tier_b else 0,
        "par5_avg": _ratio(*pt[5]) if tier_b else None, "par5_n": pt[5][1] if tier_b else 0,
        "putt_holes": hs.get("putt_holes", 0), "three_putts": hs.get("three_putts", 0),
        "one_putts": hs.get("one_putts", 0),
        "three_putt_pct": _ratio(hs.get("three_putts"), hs.get("putt_holes")),
        "one_putt_pct": _ratio(hs.get("one_putts"), hs.get("putt_holes")),
        "gir_putt_holes": hs.get("gir_putt_holes", 0), "gir_putts_sum": hs.get("gir_putts_sum", 0),
        "putts_per_gir": _ratio(hs.get("gir_putts_sum"), hs.get("gir_putt_holes")),
        "scrambles": hs.get("scrambles", 0), "scramble_chances": hs.get("scramble_chances", 0),
        "scramble_pct": _ratio(hs.get("scrambles"), hs.get("scramble_chances")),
        "gir_derived": hs.get("gir_derived", 0),
        "penalties": hs.get("penalties") if hs.get("penalty_holes") == len(holes) and holes else None,
        "differential": h.get("differential"), "differential_kind": h.get("differential_kind"),
        "has_history": bool(h), "ags": h.get("ags"), "hi_after": h.get("hi_after"), "low_hi": h.get("low_hi"),
        "diff_equiv": diff_equiv, "to_par_equiv": to_par_equiv, "weight": weight,
        "tiers": {"A": gross is not None, "B": bool(tier_b), "C": bool(tier_c)},
        "dq_flags": dq, "is_demo": rid.startswith("demo-"),
        "holes_detail": holes,
        # Per 9 holes (the dashboard's scale) and where the round was played.
        "nines": n_holes / 9 if n_holes in (9, 18) else 0.0,
        "to_par_9": to_par * 9 / n_holes if (to_par is not None and n_holes in (9, 18)) else None,
        "halves": halves,
        "putts_9": scale9(putts), "dbl_9": scale9(dbl), "to_green_9": scale9(to_green),
        "triple_plus": triple, "triple_9": scale9(triple),
        "cr9": cr9 if n_holes == 9 else None, "slope9": slope9 if n_holes == 9 else None,
        "putts_over_9": scale9(putts_over),
        "gir_9": 9 * gir_hit / gir_ch if (gir_hit is not None and gir_ch) else None,
        "putts_recorded": putts_recorded, "putts_partial": putts_partial,
        "group": group_of(r.get("course_key_eff"), r.get("club_id")),
        # 18Birdies' own numbers, shown beside ours and never mixed into them.
        "round_handicap_18b": _num(r.get("round_handicap_18b")),
        "sg_overall": _sg(r.get("sg_overall")), "sg_tee_to_green": _sg(r.get("sg_tee_to_green")),
        "gir_no_chance": r.get("gir_no_chance"), "tee_name": r.get("tee_name"),
    }
    fact["penalties_18"] = scale(fact["penalties"])
    fact["penalties_9"] = scale9(fact["penalties"])
    fact["tier"] = "C" if tier_c else "B" if tier_b else "A"
    return fact


def hi_series(conn: sqlite3.Connection) -> list[dict]:
    """Unofficial Handicap Index after each scoring record (handicap_history is written by `golf recompute`)."""
    rows = conn.execute(
        """SELECT h.round_id, h.as_of, h.hi_after, h.low_hi, h.n_scores
           FROM handicap_history h JOIN rounds r ON r.round_id = h.round_id
           WHERE h.hi_after IS NOT NULL AND r.deleted_in_source = 0
           ORDER BY h.as_of, r.played_at_utc, h.round_id"""
    ).fetchall()
    return [{"round_id": r["round_id"], "date": r["as_of"][:10], "hi": r["hi_after"], "low_hi": r["low_hi"],
             "n_scores": r["n_scores"]} for r in rows]


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge-patch (how event_reviews.edits_json is applied)."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def load_timeline(conn: sqlite3.Connection) -> list[dict]:
    """Counted events (timeline_raw) with review edits applied and JSON columns decoded, date order.

    Mirrors golf.notes.timeline(); kept local so analytics has no import-time dependency on the
    notes pipeline.
    """
    out = []
    for row in conn.execute("SELECT * FROM timeline_raw"):
        e = dict(row)
        edits = loads(e.pop("review_edits", None), None)
        e.pop("review_decision", None)
        for k in EVENT_JSON_COLS:
            val = e.get(k)
            e[k] = (loads(val, []) if isinstance(val, str) else val) or []
        if isinstance(edits, dict):
            e = merge_patch(e, edits)
        e["focus_areas"] = [_norm_focus(f) for f in (e.get("focus_areas") or [])]
        if not e["focus_areas"] and e.get("game_areas"):
            e["focus_areas"] = [{"game_area": g.strip(), "detail": ""}
                                for g in str(e["game_areas"]).split(",") if g.strip()]
        if isinstance(e.get("injury"), dict):
            e["injury"] = [e["injury"]]
        e["is_planned"] = bool(e.get("is_planned"))
        out.append(e)
    out.sort(key=lambda e: (e.get("date") or e.get("date_start") or "9999", e["event_id"]))
    return out


def _norm_focus(f: Any) -> dict:
    if isinstance(f, dict):
        return {"game_area": str(f.get("game_area") or ""), "detail": str(f.get("detail") or "")}
    return {"game_area": str(f), "detail": ""}


# ----------------------------------------------------------------------------- summaries
def outcome_sigma(facts: Sequence[dict]) -> dict[str, Any]:
    """Round-to-round SD of the outcome, from rounds of the metric's native length.

    On the 18-hole scales that is 18-hole rounds (9-hole doubles are noisier by ~sqrt 2); per 9 holes it
    is 9-hole rounds (an 18-hole round halved averages two nines, so it is quieter). Falls back to all
    rounds when there are fewer than three native ones. The plain SD is used, not the MSSD, because it
    also absorbs any real trend and so errs toward a larger (more cautious) MDE.
    """
    per9 = bool(facts) and facts[0].get("outcome_metric") == "to_par_9"
    native = 9 if per9 else 18
    full = [f["outcome"] for f in facts if f.get("outcome") is not None and f.get("holes") == native]
    basis = f"{native}-hole rounds"
    if len(full) < 3:
        full = [f["outcome"] for f in facts if f.get("outcome") is not None]
        basis = "all rounds (18-hole halved)" if per9 else "all rounds (9-hole doubled)"
    s = spread(full)
    return {"sigma": s["sd"] if s["n"] >= 3 else None, "n": s["n"], "basis": basis,
            "mad_sigma": s["mad_sigma"], "mssd_sigma": s["mssd_sigma"]}


def pooled_rate(facts: Sequence[dict], k_key: str, n_key: str) -> dict[str, Any] | None:
    """Pooled k/n over rounds with a Wilson 95% interval (rounds with n = 0/None are skipped)."""
    pairs = [(f[k_key], f[n_key]) for f in facts if f.get(n_key) and f.get(k_key) is not None]
    if not pairs:
        return None
    k, n = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    lo, hi = wilson(k, n)
    return {"value": k / n, "lo": lo, "hi": hi, "k": k, "n": n, "rounds": len(pairs)}


def pooled_mean(facts: Sequence[dict], key: str, weight: str | None = "weight") -> dict[str, Any] | None:
    """Weighted mean of a per-round value (9-hole rounds at half weight by default)."""
    pts = [(f[key], (f.get(weight) or 0) if weight else 1.0) for f in facts if f.get(key) is not None]
    pts = [p for p in pts if p[1]]
    if not pts:
        return None
    return {"value": weighted_mean([p[0] for p in pts], [p[1] for p in pts]), "rounds": len(pts)}


def summary_stats(facts: Sequence[dict]) -> dict[str, Any]:
    """Pooled Tier A/B/C figures over all counted rounds (inputs to KPI tiles and panel headers)."""
    def hole_pool(avg_key: str, n_key: str) -> dict | None:
        pts = [(f[avg_key] * f[n_key], f[n_key]) for f in facts if f.get(avg_key) is not None and f.get(n_key)]
        if not pts:
            return None
        n = sum(p[1] for p in pts)
        return {"value": sum(p[0] for p in pts) / n, "n": n, "rounds": len(pts)}

    ppg = [(f["gir_putts_sum"], f["gir_putt_holes"]) for f in facts if f.get("gir_putt_holes")]
    dbl = [(f["dbl_plus"], f["holes"]) for f in facts if f.get("dbl_plus") is not None and f.get("holes")]
    tri = [(f["triple_plus"], f["holes"]) for f in facts if f.get("triple_plus") is not None and f.get("holes")]
    return {
        "fir": pooled_rate(facts, "fw_hit", "fw_chances"),
        "gir": pooled_rate(facts, "gir_hit", "gir_chances"),
        "putts_18": pooled_mean(facts, "putts_18"),
        "to_green_18": pooled_mean(facts, "to_green_18"),
        "putts_over_18": pooled_mean(facts, "putts_over_18"),
        "to_par_18": pooled_mean(facts, "to_par_equiv"),
        "dbl_18": ({"value": 18 * sum(d[0] for d in dbl) / sum(d[1] for d in dbl), "rounds": len(dbl)}
                   if dbl and sum(d[1] for d in dbl) else None),
        "par3_avg": hole_pool("par3_avg", "par3_n"),
        "par4_avg": hole_pool("par4_avg", "par4_n"),
        "par5_avg": hole_pool("par5_avg", "par5_n"),
        "three_putt_pct": pooled_rate(facts, "three_putts", "putt_holes"),
        "one_putt_pct": pooled_rate(facts, "one_putts", "putt_holes"),
        "scramble_pct": pooled_rate(facts, "scrambles", "scramble_chances"),
        "putts_per_gir": ({"value": sum(p[0] for p in ppg) / sum(p[1] for p in ppg), "n": sum(p[1] for p in ppg),
                           "rounds": len(ppg)} if ppg else None),
        "penalties_18": pooled_mean(facts, "penalties_18"),
        # Per 9 holes: the same pools on the dashboard's scale.
        "gir_9": _times(pooled_rate(facts, "gir_hit", "gir_chances"), 9),
        "putts_9": pooled_mean(facts, "putts_9"),
        "to_green_9": pooled_mean(facts, "to_green_9"),
        "putts_over_9": pooled_mean(facts, "putts_over_9"),
        "to_par_9": pooled_mean(facts, "to_par_9"),
        "dbl_9": ({"value": 9 * sum(d[0] for d in dbl) / sum(d[1] for d in dbl), "rounds": len(dbl)}
                  if dbl and sum(d[1] for d in dbl) else None),
        "triple_9": ({"value": 9 * sum(d[0] for d in tri) / sum(d[1] for d in tri), "rounds": len(tri)}
                     if tri and sum(d[1] for d in tri) else None),
        "penalties_9": pooled_mean(facts, "penalties_9"),
        "putts_partial": sum(bool(f.get("putts_partial")) for f in facts),
        "outcome": spread([f["outcome"] for f in facts if f.get("outcome") is not None]),
        "sigma": outcome_sigma(facts),
        "tiers": {t: sum(bool(f["tiers"][t]) for f in facts) for t in ("A", "B", "C")},
    }


def outcome_trend(facts: Sequence[dict], lam: float = EWMA_LAMBDA, sigma: float | None = None) -> list[dict]:
    """EWMA "ability" line over rounds with an outcome, with an 80% noise band (see ewma_band)."""
    pts = [f for f in facts if f.get("outcome") is not None]
    vals = [f["outcome"] for f in pts]
    wts = [f["weight"] for f in pts]
    if sigma is None:
        sigma = outcome_sigma(facts)["sigma"]
    levels = ewma(vals, lam, wts)
    band = ewma_band(vals, lam, wts, sigma=sigma)
    return [{"round_id": f["round_id"], "date": f["date"], "value": m,
             "lo": b[0] if b else None, "hi": b[1] if b else None} for f, m, b in zip(pts, levels, band)]


def metric_series(facts: Sequence[dict], key: str, weight_key: str | None = None,
                  lam: float = EWMA_LAMBDA) -> list[dict]:
    """Per-round points for one stat plus its EWMA (weighted by chances for rates)."""
    pts = [f for f in facts if f.get(key) is not None]
    wts = [(f.get(weight_key) or 0) if weight_key else 1.0 for f in pts]
    trend = ewma([f[key] for f in pts], lam, wts)
    return [{"round_id": f["round_id"], "date": f["date"], "value": f[key], "weight": w, "ewma": t,
             "nine": f.get("is_nine", False)} for f, w, t in zip(pts, wts, trend)]


def practice_blocks(events: Sequence[dict], gap_days: int = 7) -> list[dict]:
    """Group practice sessions no more than `gap_days` apart into blocks (the hero chart's shaded spans)."""
    days = sorted({str(e.get("date") or e.get("date_start"))[:10] for e in events
                   if e.get("event_type") == "practice" and not e.get("is_planned")
                   and (e.get("date") or e.get("date_start"))})
    blocks: list[dict] = []
    for d in days:
        if blocks and (date.fromisoformat(d) - date.fromisoformat(blocks[-1]["end"])).days <= gap_days:
            blocks[-1]["end"] = d
            blocks[-1]["sessions"] += 1
        else:
            blocks.append({"start": d, "end": d, "sessions": 1})
    return blocks


def injury_spans(events: Sequence[dict], default_days: int = 14) -> list[dict]:
    """Injury events as date spans; an open-ended injury is drawn for `default_days` and marked approximate."""
    out = []
    for e in events:
        if e.get("event_type") != "injury" or e.get("is_planned"):
            continue
        start = str(e.get("date_start") or e.get("date") or "")[:10]
        if not start:
            continue
        end = str(e.get("date_end") or "")[:10]
        approximate = not end
        if not end:
            end = (date.fromisoformat(start) + timedelta(days=default_days)).isoformat()
        inj = (e.get("injury") or [{}])[0] if isinstance(e.get("injury"), list) else {}
        out.append({"event_id": e["event_id"], "start": start, "end": end, "approximate": approximate,
                    "body_part": (inj or {}).get("body_part", ""), "severity": (inj or {}).get("severity", ""),
                    "summary": e.get("summary") or ""})
    return out


def _times(pool: dict | None, k: float) -> dict | None:
    """A pooled rate rescaled to a count per k holes (e.g. greens in regulation per 9)."""
    if pool is None:
        return None
    return dict(pool, value=pool["value"] * k, lo=pool["lo"] * k, hi=pool["hi"] * k)


# ----------------------------------------------------------------------------- per-9 score figures
def last_rounds_per9(facts: Sequence[dict], n: int = 5) -> dict[str, Any] | None:
    """Average to par per 9 holes over the last n rounds (an 18-hole round counts as two nines),
    and the same for the n rounds before them, for a 'last 5' tile with a delta."""
    pts = [f for f in facts if f.get("to_par_9") is not None and f.get("nines")]
    if not pts:
        return None

    def avg(rs: Sequence[dict]) -> float | None:
        nines = sum(f["nines"] for f in rs)
        return sum(f["to_par_9"] * f["nines"] for f in rs) / nines if nines else None

    last, prev = pts[-n:], pts[-2 * n:-n] if len(pts) > n else []
    return {"value": avg(last), "n": len(last), "start": last[0]["date"], "end": last[-1]["date"],
            "prev_value": avg(prev) if len(prev) == n else None, "prev_n": len(prev),
            "holes": sum(f["holes"] for f in last)}


def hole_averages(facts: Sequence[dict], min_rounds: int = 5) -> dict[str, Any] | None:
    """Average score to par on each hole of the most-played course, when it has at least `min_rounds`
    rounds with per-hole strokes and pars: which holes cost the most. Also the latest round per hole."""
    by_course: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        if f.get("course_key") and f["tiers"]["B"]:
            by_course[f["course_key"]].append(f)
    if not by_course:
        return None
    key, rounds = max(by_course.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(rounds) < min_rounds:
        return None
    per: dict[int, dict[str, Any]] = {}
    for f in rounds:
        for h in f["holes_detail"]:
            if h["strokes"] is None or h["par"] is None:
                continue
            d = per.setdefault(h["hole"], {"hole": h["hole"], "par": h["par"], "si": h["si"], "sum": 0, "n": 0,
                                           "last": None, "last_date": None})
            d["sum"] += h["strokes"] - h["par"]
            d["n"] += 1
            d["last"], d["last_date"] = h["strokes"] - h["par"], f["date"]     # facts are oldest first
    holes = [dict(d, avg=d["sum"] / d["n"]) for _, d in sorted(per.items())]
    for d in holes:
        del d["sum"]
    return {"course_key": key, "course": rounds[0].get("course_name") or key, "rounds": len(rounds),
            "last_date": rounds[-1]["date"], "holes": holes}


def best_nine(facts: Sequence[dict]) -> dict[str, Any] | None:
    """Lowest to-par over nine holes: a 9-hole round, or either half of an 18-hole round when every
    hole has strokes and par (to_par/2 is an average, not a nine anyone played). Ties go to the latest."""
    cands = []
    for f in facts:
        where = f.get("course_name") or f.get("club_name") or "an unmapped club"
        if f.get("holes") == 9 and f.get("to_par") is not None:
            cands.append({"to_par": f["to_par"], "gross": f.get("gross"), "date": f["date"],
                          "round_id": f["round_id"], "course": where, "part": "9-hole round"})
        elif f.get("halves"):
            for side in ("front", "back"):
                cands.append({"to_par": f["halves"][side], "gross": f["halves"][f"{side}_gross"],
                              "date": f["date"], "round_id": f["round_id"], "course": where,
                              "part": f"{side} nine of 18"})
    if not cands:
        return None
    return min(cands, key=lambda c: (c["to_par"], _neg_date(c["date"])))


def _neg_date(iso: str) -> int:
    return -date.fromisoformat(iso[:10]).toordinal()


# ----------------------------------------------------------------------------- shots and club distances
_WEDGE_LETTER = {"P": "PW", "G": "GW", "A": "GW", "S": "SW", "L": "LW"}
_WEDGE_LOFT = {"PW": 46, "GW": 50, "SW": 56, "LW": 60}


def club_label(club_type: str | None, number: Any, loft: Any = None) -> str | None:
    """18Birdies stickTypeAndNumber -> the bag label (Driver, 3W, 5H, 7i, PW, SW, Putter...)."""
    t = str(club_type or "").strip().upper()
    n = str(number if number is not None else "").strip()
    if t == "PUTTER" or n.lower() == "putter":
        return "Putter"
    if t == "WOOD":
        return "Driver" if n.upper() in ("1", "D", "DRIVER") else (f"{n}W" if n else "Wood")
    if t == "HYBRID":
        return f"{n}H" if n else "Hybrid"
    if t == "IRON":
        if n.upper() in _WEDGE_LETTER:
            return _WEDGE_LETTER[n.upper()]
        return f"{n}i" if n else "Iron"
    if t == "WEDGE":
        if n.upper() in _WEDGE_LETTER:
            return _WEDGE_LETTER[n.upper()]
        if n.isdigit():
            return f"{n}°"
        lf = _num(loft)
        return f"{int(lf)}°" if lf else "Wedge"
    return None


def club_order(label: str) -> float:
    """Bag order: Driver, woods, hybrids, irons, then wedges by loft; unknown labels last."""
    if label == "Driver":
        return 0
    if label in _WEDGE_LOFT:
        return 70 + _WEDGE_LOFT[label] / 10
    if label.endswith("°") and label[:-1].isdigit():
        return 70 + int(label[:-1]) / 10
    head, tail = label[:-1], label[-1:]
    if head.isdigit():
        base = {"W": 10, "H": 30, "i": 50}.get(tail)
        if base is not None:
            return base + int(head)
    return {"Wood": 19, "Hybrid": 39, "Iron": 59, "Wedge": 76}.get(label, 99)


def load_shots(conn: sqlite3.Connection) -> list[dict]:
    """GPS shots of counted rounds, oldest first. Coordinates are never read here: nothing downstream
    (the dashboard included) may see them, so they stay in the database."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shots)")}
    if not cols:
        return []
    want = [c for c in ("round_id", "seq", "hole", "club_type", "club_number", "club", "loft", "distance_yards",
                        "tee_name", "shot_at_utc") if c in cols]
    rows = conn.execute(
        f"""SELECT {', '.join('s.' + c for c in want)}, v.played_on_local AS date
            FROM shots s JOIN v_rounds v ON v.round_id = s.round_id
            WHERE v.excluded = 0
            ORDER BY v.played_on_local, v.played_at_utc, s.round_id, s.seq"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["club"] = d.get("club") or club_label(d.get("club_type"), d.get("club_number"), d.get("loft")) or "Unknown"
        d["distance_yards"] = _num(d.get("distance_yards"))
        out.append(d)
    return tag_logging(out)


def tag_logging(shots: list[dict]) -> list[dict]:
    """Set s['logged'] per round: 'live' when shots were tracked during play, 'after' when the whole
    round was entered afterwards (median gap between shots under AFTER_ROUND_GAP_S: nobody walks to
    the next ball in seconds), 'unknown' with fewer than 3 timestamps. After-the-round positions are
    map taps (or the app's default spot), so their distances are rough. In place; returns the list."""
    by_round: dict[str, list[dict]] = defaultdict(list)
    for s in shots:
        by_round[s["round_id"]].append(s)
    for ss in by_round.values():
        ts = []
        for s in sorted(ss, key=lambda x: x.get("seq") or 0):
            try:
                ts.append(datetime.fromisoformat(str(s.get("shot_at_utc"))))
            except ValueError:
                pass
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        mode = "unknown" if len(ts) < 3 else "after" if statistics.median(gaps) < AFTER_ROUND_GAP_S else "live"
        for s in ss:
            s["logged"] = mode
    return shots


def shot_validity(distance: float | None) -> str | None:
    """None when a GPS distance is usable, else the reason it is not."""
    if distance is None:
        return "no distance"
    if distance < MIN_SHOT_YARDS:
        return f"under {MIN_SHOT_YARDS:g} yards"
    if distance > MAX_SHOT_YARDS:
        return f"over {MAX_SHOT_YARDS:g} yards"
    return None


def _quartiles(v: Sequence[float]) -> tuple[float, float, float]:
    if len(v) == 1:
        return v[0], v[0], v[0]
    q1, med, q3 = statistics.quantiles(v, n=4, method="inclusive")
    return q1, med, q3


def club_distances(shots: Sequence[dict]) -> dict[str, Any]:
    """Per club (bag order, putter left out): median, middle half (IQR), range and every shot.

    A GPS shot distance is measured to where the next shot started, so it includes roll, mishits
    and anything else that happened to the ball. Distances under 5 or over 400 yards are GPS or entry
    errors (a re-tap, a missed next shot), and exact repeats on a hole are app defaults: all are
    dropped from the figures and listed separately with the reason.
    """
    by_club: dict[str, dict[str, list]] = defaultdict(lambda: {"ok": [], "bad": []})
    putts = 0
    # Two shots on the same hole with exactly the same distance (to 1/10000 yd) are the app's default
    # positions, not measurements: real exports carry them for shots entered after the round, and GPS
    # noise makes a genuine repeat essentially impossible.
    seen: Counter = Counter(_repeat_key(s) for s in shots if _repeat_key(s))
    for s in shots:
        club = s.get("club") or "Unknown"
        if club == "Putter":
            putts += 1
            continue
        d = s.get("distance_yards")
        reason = shot_validity(d)
        if not reason and seen[_repeat_key(s)] > 1:
            reason = "same distance as another shot on this hole (an app default, not a measurement)"
        item = {"d": None if d is None else round(d, 1), "date": s.get("date"), "hole": s.get("hole"),
                "after": s.get("logged") == "after"}
        if reason:
            by_club[club]["bad"].append(dict(item, reason=reason))
        else:
            by_club[club]["ok"].append(item)
    rows, invalid = [], []
    for club in sorted(by_club, key=lambda c: (club_order(c), c)):
        ok, bad = by_club[club]["ok"], by_club[club]["bad"]
        invalid += [dict(b, club=club) for b in bad]
        if not ok:
            continue
        vals = sorted(x["d"] for x in ok)
        q1, med, q3 = _quartiles(vals)
        # Shots logged during play only: after-the-round entries are map taps, so for clubs hit mostly
        # that way (Shane's wedges) the live median is the one to trust, when there is one.
        live = sorted(x["d"] for x in ok if not x["after"])
        rows.append({"club": club, "order": club_order(club), "n": len(vals), "n_invalid": len(bad),
                     "n_after": sum(x["after"] for x in ok), "n_live": len(live),
                     "median_live": _quartiles(live)[1] if live else None,
                     "median": med, "q1": q1, "q3": q3, "min": vals[0], "max": vals[-1],
                     "shots": sorted(ok, key=lambda x: (x["date"] or "", x["hole"] or 0))})
    n_full = sum(r["n"] for r in rows)
    return {"clubs": rows, "invalid": invalid, "n_shots": len(shots), "n_putts": putts, "n_valid": n_full,
            "n_after": sum(r["n_after"] for r in rows), "min_yards": MIN_SHOT_YARDS, "max_yards": MAX_SHOT_YARDS}


def _repeat_key(s: dict) -> tuple[int, float] | None:
    d = s.get("distance_yards")
    if s.get("club") == "Putter" or s.get("hole") is None or shot_validity(d):
        return None
    return s["hole"], round(d, 4)


def shot_coverage(shots: Sequence[dict], facts: Sequence[dict]) -> dict[str, Any]:
    """How much of the golf has GPS shots: rounds with any, and per hole on those rounds."""
    by_round: dict[str, list[dict]] = defaultdict(list)
    for s in shots:
        by_round[s["round_id"]].append(s)
    per_round = []
    for f in facts:
        ss = by_round.get(f["round_id"])
        if not ss:
            continue
        holes = sorted({s["hole"] for s in ss if s.get("hole") is not None})
        per_round.append({"id": f["round_id"], "date": f["date"],
                          "course": f.get("course_name") or f.get("club_name") or "Unmapped club",
                          "holes_played": f.get("holes"), "holes_with_shots": len(holes), "shots": len(ss),
                          "putts": sum(1 for s in ss if s.get("club") == "Putter"),
                          "logged": ss[0].get("logged", "unknown")})
    # Per hole number: of the tracked rounds that played it, how many have a shot on it.
    shots_on = Counter(s["hole"] for s in shots if s.get("hole") is not None)
    played: Counter = Counter()
    covered: Counter = Counter()
    for f in facts:
        ss = by_round.get(f["round_id"])
        if not ss:
            continue
        numbers = {h["hole"] for h in f.get("holes_detail") or []} or set(_hole_numbers(f))
        played.update(numbers)
        covered.update({s["hole"] for s in ss if s.get("hole") in numbers})
    return {
        "rounds_total": len(facts), "rounds_with_shots": len(per_round),
        "rounds_logged_after": sum(r["logged"] == "after" for r in per_round),
        "holes_played": sum(r["holes_played"] or 0 for r in per_round),
        "holes_with_shots": sum(r["holes_with_shots"] for r in per_round),
        "by_hole": [{"hole": h, "played": played[h], "rounds": covered[h], "shots": shots_on[h]}
                    for h in sorted(set(played) | set(shots_on))],
        "rounds": list(reversed(per_round)),
    }


def _hole_numbers(f: dict) -> range:
    """Hole numbers of a round without per-hole rows: 1-9 / 10-18 for a nine, 1-18 otherwise."""
    if f.get("holes") == 9:
        return range(10, 19) if f.get("nine") == "back" else range(1, 10)
    return range(1, (f.get("holes") or 18) + 1)
