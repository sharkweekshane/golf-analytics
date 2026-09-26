"""World Handicap System math (2024 Rules of Handicapping), always UNOFFICIAL.

Pure functions plus `recompute`, which rebuilds handicap_history in date order (the Course Handicap
used for net double bogey is the HI in effect *before* each round, so order matters).

Why "unofficial": PCC needs at least 8 same-day scores from the field, so it is always 0 here; the
USGA's expected 9-hole differential is unpublished, so 9-hole scores use a labelled public
approximation (0.52 x HI + 1.2); and ratings come from Shane's own courses.yaml.

9-hole scores (Shane plays mostly nines) follow the 2024 rules, checked on 2026-09-25 against the
Rules of Handicapping (2024 edition, as republished by GolfRSA) and the USGA's 2024 9-hole FAQ
(usga.org/.../2024-treatment-of-9-hole-scores-FAQ.html; same text in the GAM and AGA FAQ PDFs):
- Rule 2.2b: a 9-hole score counts only if all 9 holes were played over a 9-hole rating.
- Rule 5.1b: 9-hole Score Differential = (113 / 9-hole Slope) x (9-hole AGS - 9-hole CR - 0.5 PCC).
  It is combined with the player's expected score over the other nine to make an 18-hole Score
  Differential, and "remains unrounded until after it has been combined".
- Pre-2024 practice of pairing two 9-hole scores is gone for new players too. The USGA FAQ answer
  to "When establishing a Handicap Index, how are 9-hole scores treated?": 54 holes are needed,
  from 9- and/or 18-hole scores, and "the use of expected score does not come into play until a
  golfer plays and posts 54 individual hole scores". Once 54 holes are posted and an expected
  score can be determined, an 18-hole differential is calculated for each 9-hole score, and the
  initial Handicap Index is established (Rule 4.5 also sets the 54-hole minimum).
- Rule 3.1a: before an index exists, every hole is capped at par + 5; Rule 3.1b afterwards caps at
  net double bogey, with par + 5 again on holes where a Course Handicap above 54 gives 4+ strokes.
- Rule 5.2a: an initial index above 54.0 is set to 54.0 (Rule 5.3: the maximum).
How the USGA "determines" the expected score for a player who has no index yet is not published.
This module uses the self-consistent value: the index h for which converting every waiting 9-hole
score with expected(h) and applying the Rule 5.2 table gives h back (see solve_initial_index). So a
golfer whose first six rounds are nines gets an index on the sixth; every later nine uses the index
in effect before it, like any other score.

Rounding: WHS rounds to the nearest tenth (or whole) with halves going up, so negative values move
toward zero (-1.55 -> -1.5). Decimal arithmetic keeps the half cases exact.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, NamedTuple, Sequence, Union

from .config import Config
from .db import loads, upsert

Number = Union[int, float, Decimal]

MAX_HI = 54.0
RECORD_SIZE = 20                  # most recent scores considered
LOW_HI_WINDOW_DAYS = 365
SOFT_CAP = Decimal("3.0")
HARD_CAP = Decimal("5.0")
MIN_SCORES = 3                    # at least three differentials for the Rule 5.2 table
MIN_HOLES = 54                    # Rule 4.5: holes needed for an initial index (9s and/or 18s)
META_INCLUDE_NINE = "whs_include_nine"   # meta key: '1' (default) counts 9-hole scores, '0' leaves them out

# Rule 5.2 table: number of differentials in the record -> (lowest n averaged, adjustment)
_FEWER_THAN_20: dict[int, tuple[int, str]] = {
    3: (1, "-2.0"), 4: (1, "-1.0"), 5: (1, "0"), 6: (2, "-1.0"), 7: (2, "0"), 8: (2, "0"),
    9: (3, "0"), 10: (3, "0"), 11: (3, "0"), 12: (4, "0"), 13: (4, "0"), 14: (4, "0"),
    15: (5, "0"), 16: (5, "0"), 17: (6, "0"), 18: (6, "0"), 19: (7, "0"), 20: (8, "0"),
}

# UNOFFICIAL: public approximation of the USGA's unpublished expected 9-hole differential.
# It reproduces the USGA example exactly (HI 14.0 -> 8.48; 7.2 + 8.48 -> 15.7).
NINE_EXPECTED_SLOPE = Decimal("0.52")
NINE_EXPECTED_INTERCEPT = Decimal("1.2")


class HoleScore(NamedTuple):
    strokes: int
    par: int
    si: int


# ------------------------------------------------------------------ rounding
def _dec(x: Number) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _round(x: Number, exp: str) -> Decimal:
    d = _dec(x)
    return d.quantize(Decimal(exp), rounding=ROUND_HALF_UP if d >= 0 else ROUND_HALF_DOWN)


def round_tenth(x: Number) -> float:
    """Nearest 0.1, halves up: 1.55 -> 1.6, -1.55 -> -1.5, -1.56 -> -1.6."""
    return float(_round(x, "0.1"))


def round_whole(x: Number) -> int:
    return int(_round(x, "1"))


# ------------------------------------------------------- course handicap / NDB
def course_handicap(hi: Number, slope: int, cr: Number, par: int) -> int:
    """HI x Slope/113 + (CR - Par), rounded once at the end."""
    return round_whole(_dec(hi) * Decimal(slope) / Decimal(113) + (_dec(cr) - Decimal(par)))


def nine_hole_course_handicap(hi: Number, slope9: int, cr9: Number, par9: int) -> int:
    """(HI/2 rounded to 0.1) x Slope9/113 + (CR9 - Par9)."""
    return course_handicap(_round(_dec(hi) / 2, "0.1"), slope9, cr9, par9)


def strokes_received(hole_si: int, ch: int, n_holes: int = 18) -> int:
    """Strokes received on the hole ranked `hole_si` (1 = hardest) of `n_holes`.

    floor(CH/n) + (SI <= CH mod n). A plus handicap (CH < 0) gives strokes back starting from the
    easiest hole, so the result is negative there.
    """
    if ch >= 0:
        return ch // n_holes + (1 if hole_si <= ch % n_holes else 0)
    n = -ch
    return -(n // n_holes + (1 if hole_si > n_holes - n % n_holes else 0))


def net_double_bogey(par: int, received: int | None, course_handicap: int | None = None,
                     n_holes: int = 18) -> int:
    """Maximum hole score for AGS. `received` None means no Handicap Index yet: par + 5 (Rule 3.1a).

    With a Course Handicap above 54, a hole where 4+ strokes are received is also capped at par + 5
    (Rule 3.1b). Over nine holes the same point is a 9-hole Course Handicap above 27 (our reading:
    the rule is written for 18 holes).
    """
    if received is None:
        return par + 5
    if course_handicap is not None and course_handicap > 54 * n_holes // 18 and received >= 4:
        return par + 5
    return par + 2 + received


def si_ranks(sis: Sequence[int]) -> list[int]:
    """Rank stroke indexes 1..n within the holes played.

    A nine on an 18-hole course takes strokes in ascending order of the published 18-hole SIs
    (Appendix E); for a full 18 with SIs 1..18 the ranks equal the SIs.
    """
    order = sorted(range(len(sis)), key=lambda i: (sis[i], i))
    ranks = [0] * len(sis)
    for rank, i in enumerate(order, 1):
        ranks[i] = rank
    return ranks


def adjusted_gross_score(holes: Sequence[HoleScore | tuple[int, int, int]],
                         course_handicap: int | None) -> int:
    """Sum of (strokes, par, si) holes, each capped at net double bogey.

    `course_handicap` None means the player has no Handicap Index yet (every hole capped at par + 5).
    Pass the 9-hole Course Handicap for a nine; strokes are spread over the holes given.
    """
    holes = [HoleScore(*h) for h in holes]
    ranks = si_ranks([h.si for h in holes])
    total = 0
    for h, rank in zip(holes, ranks):
        received = None if course_handicap is None else strokes_received(rank, course_handicap, len(holes))
        total += min(h.strokes, net_double_bogey(h.par, received, course_handicap, len(holes)))
    return total


# --------------------------------------------------------------- differentials
def _raw_differential(ags: Number, cr: Number, slope: int, pcc: Number = 0) -> Decimal:
    return Decimal(113) / Decimal(slope) * (_dec(ags) - _dec(cr) - _dec(pcc))


def score_differential(ags: Number, cr: Number, slope: int, pcc: Number = 0) -> float:
    """(113 / Slope) x (AGS - CR - PCC), rounded to 0.1. PCC is always 0 in this app."""
    return round_tenth(_raw_differential(ags, cr, slope, pcc))


def expected_nine_differential(hi: Number) -> float:
    """UNOFFICIAL approximation (0.52 x HI + 1.2) of the unpublished USGA expected 9-hole score."""
    return float(NINE_EXPECTED_SLOPE * _dec(hi) + NINE_EXPECTED_INTERCEPT)


def nine_to_eighteen(diff9: Number, hi: Number) -> float:
    """18-hole differential = unrounded 9-hole differential + expected 9-hole differential, rounded."""
    return round_tenth(_dec(diff9) + NINE_EXPECTED_SLOPE * _dec(hi) + NINE_EXPECTED_INTERCEPT)


def nine_hole_differential(ags9: Number, cr9: Number, slope9: int, hi: Number, pcc: Number = 0) -> float:
    """UNOFFICIAL 18-hole equivalent of a 9-hole score (PCC counts half over nine holes)."""
    return nine_to_eighteen(_raw_differential(ags9, cr9, slope9, _dec(pcc) / 2), hi)


# ------------------------------------------------------------- handicap index
def handicap_index(differentials: Sequence[Number]) -> float | None:
    """Rule 5.2 from a chronological list (oldest first); only the most recent 20 count.

    Returns None until 3 scores (54 holes) exist. No 0.96 multiplier (that was pre-WHS).
    """
    recent = [_dec(d) for d in differentials][-RECORD_SIZE:]
    if len(recent) < MIN_SCORES:
        return None
    count, adjustment = _FEWER_THAN_20[len(recent)]
    value = sum(sorted(recent)[:count]) / Decimal(count) + Decimal(adjustment)
    return min(round_tenth(value), MAX_HI)


def solve_initial_index(scores: Sequence[tuple[Number | None, Number | None]]) -> tuple[float | None, list[float]]:
    """Initial HI when some of the first 54 holes are 9-hole scores still waiting for an expected score.

    `scores` are chronological (differential, unrounded 9-hole differential) pairs; a waiting 9-hole
    score has differential None. Returns (HI, the 18-hole differentials with every waiting score
    converted as nine_to_eighteen(diff9, HI)).

    UNOFFICIAL: the USGA does not publish how it determines a new player's expected score. We use
    the self-consistent index: HI = handicap_index(converted differentials at HI). The map is
    non-decreasing in HI with slope <= 0.52, so iterating down from 54.0 reaches its largest fixed
    point in a few steps (the result never exceeds 54.0, per Rule 5.2a).

    Rounding to a tenth can leave several self-consistent values a tenth or two apart (Shane's first six
    nines: both 50.2 and 50.3 map to themselves). The LARGEST is chosen on purpose: it gives the higher
    expected score for the other nine, so the new player's first index errs high, the conservative side
    (a too-low index would give fewer strokes than the golf so far supports).
    """
    def converted(hi: Number) -> list[Decimal]:
        return [_dec(d) if d is not None else _dec(nine_to_eighteen(d9, hi)) for d, d9 in scores]

    hi: float | None = MAX_HI
    for _ in range(500):
        nxt = handicap_index(converted(hi))
        if nxt is None or nxt == hi:
            hi = nxt
            break
        hi = nxt
    return hi, [float(d) for d in converted(hi if hi is not None else MAX_HI)]


def exceptional_score_reduction(differential: Number, hi_before: Number | None) -> float:
    """-1.0 if the differential is 7.0-9.9 below the HI in effect, -2.0 if 10.0+ below, else 0."""
    if hi_before is None:
        return 0.0
    gap = _dec(hi_before) - _dec(differential)
    if gap >= 10:
        return -2.0
    if gap >= 7:
        return -1.0
    return 0.0


def apply_caps(hi: Number, low_hi: Number | None) -> tuple[float, str | None]:
    """Soft cap halves any rise beyond Low HI + 3.0; hard cap stops it at Low HI + 5.0."""
    value = _dec(hi)
    if low_hi is None or value <= _dec(low_hi) + SOFT_CAP:
        return float(value), None
    low = _dec(low_hi)
    soft = low + SOFT_CAP + (value - low - SOFT_CAP) / 2
    if soft > low + HARD_CAP:
        return round_tenth(low + HARD_CAP), "hard"
    return round_tenth(soft), "soft"


def low_handicap_index(revisions: Sequence[tuple[date, float | None]], played_on: date) -> float | None:
    """Lowest HI in the 365 days before `played_on` (Rule 5.7).

    `revisions` are (date, HI after that round), oldest first. The HI already in effect when the
    window opened counts too, since it was the player's index on those days.
    """
    start = played_on - timedelta(days=LOW_HI_WINDOW_DAYS)
    window = [hi for d, hi in revisions if start <= d < played_on and hi is not None]
    before = [hi for d, hi in revisions if d < start and hi is not None]
    if before:
        window.append(before[-1])
    return min(window) if window else None


# ------------------------------------------------------------------ settings
def include_nine_setting(conn: sqlite3.Connection) -> bool:
    """Whether 9-hole scores count toward the HI: meta 'whs_include_nine', default yes ('1').

    Shane's golf is mostly nine holes, so leaving them out would leave him without an index.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_INCLUDE_NINE,)).fetchone()
    return row is None or row[0] is None or str(row[0]).strip() != "0"


def set_include_nine(conn: sqlite3.Connection, include: bool) -> None:
    """Persist the choice (`golf recompute --include-nine / --no-include-nine`) for every later recompute."""
    with conn:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (META_INCLUDE_NINE, "1" if include else "0"))


# ------------------------------------------------------------------ recompute
@dataclass
class _Score:
    differential: Decimal | None              # None: a 9-hole score waiting for the initial index
    esr_adjustment: Decimal = Decimal(0)      # cumulative ESR applied while this score is in the last 20
    diff9: Decimal | None = None              # unrounded 9-hole differential (9-hole scores)
    row: dict[str, Any] = field(default_factory=dict)


class Scored(NamedTuple):
    """One round's contribution to the scoring record."""
    ags: int
    differential: float | None     # 18-hole (equivalent) differential; None = 9-hole score waiting for an index
    kind: str                      # handicap_history.differential_kind
    holes: int                     # 9 or 18: counts toward the 54 holes of an initial index
    diff9: Decimal | None = None   # unrounded 9-hole Score Differential (9-hole scores only)


def _tee(conn: sqlite3.Connection, tee_id: str | None, course_key: str | None) -> sqlite3.Row | None:
    """The round's tee, else its course's default tee (the export never records the tee)."""
    if tee_id:
        return conn.execute("SELECT * FROM tees WHERE tee_id = ?", (tee_id,)).fetchone()
    if course_key:
        return conn.execute("SELECT * FROM tees WHERE course_key = ? AND is_default = 1 ORDER BY tee_id",
                            (course_key,)).fetchone()
    return None


def _tee_holes(conn: sqlite3.Connection, tee_id: str) -> dict[int, tuple[int, int]]:
    rows = conn.execute("SELECT hole, par, si FROM tee_holes WHERE tee_id = ?", (tee_id,))
    return {r["hole"]: (r["par"], r["si"]) for r in rows if r["par"] is not None and r["si"] is not None}


def _hole_card(conn: sqlite3.Connection, rnd: sqlite3.Row, tee_holes: dict[int, tuple[int, int]],
               holes: range) -> list[HoleScore] | None:
    """Per-hole (strokes, par, si) when every hole is known and trustworthy, else None."""
    if rnd["entry_mode"] != "hole_by_hole" or "sum_mismatch" in loads(rnd["dq_flags"], []):
        return None
    strokes = {r["hole"]: r["strokes"] for r in conn.execute(
        "SELECT hole, strokes FROM round_holes WHERE round_id = ? AND strokes IS NOT NULL", (rnd["round_id"],))}
    if any(h not in strokes or h not in tee_holes for h in holes):
        return None
    return [HoleScore(strokes[h], *tee_holes[h]) for h in holes]


def _par(tee: sqlite3.Row | None, tee_holes: dict[int, tuple[int, int]], holes: range,
         fallback: int | None) -> int | None:
    if all(h in tee_holes for h in holes):
        return sum(tee_holes[h][0] for h in holes)
    if tee is not None and len(holes) == 18 and tee["par"] is not None:
        return tee["par"]
    return fallback


def score_round(conn: sqlite3.Connection, rnd: sqlite3.Row, hi_before: float | None,
                include_nine: bool = True, *, expected_hi: float | None = None) -> Scored | str:
    """Scored(AGS, differential, kind, holes, diff9) for one v_rounds row, or the reason it doesn't count.

    `hi_before` is the HI in effect when the round was played (None = no index yet: holes capped at
    par + 5). `expected_hi` is the HI whose expected score completes a 9-hole score (default
    hi_before); with neither, a 9-hole score comes back with differential None, waiting for the
    initial index (recompute converts it once 54 holes are posted).
    """
    holes = rnd["holes_played"] or 0
    if rnd["entry_mode"] == "partial":
        return "partial"
    if holes < 9:
        return "under_9_holes"
    if 9 < holes < 18:
        return "holes_10_to_17"
    if holes == 9 and not include_nine:
        return "nine_hole_excluded"
    if rnd["gross"] is None:
        return "no_gross"
    tee = _tee(conn, rnd["tee_id_eff"], rnd["course_key_eff"])
    if tee is None:
        return "no_tee"
    tee_holes = _tee_holes(conn, tee["tee_id"])

    if holes == 18:
        if tee["cr18"] is None or tee["slope18"] is None:
            return "tee_unrated"
        span = range(1, 19)
        par = _par(tee, tee_holes, span, rnd["par_played"])
        card = _hole_card(conn, rnd, tee_holes, span)
        if card is None or par is None:
            return Scored(rnd["gross"], score_differential(rnd["gross"], tee["cr18"], tee["slope18"]),
                          "gross_upper_bound", 18)
        ch = None if hi_before is None else course_handicap(hi_before, tee["slope18"], tee["cr18"], par)
        ags = adjusted_gross_score(card, ch)
        return Scored(ags, score_differential(ags, tee["cr18"], tee["slope18"]), "ndb_adjusted", 18)

    nine = rnd["nine"]
    if nine not in ("front", "back"):
        return "nine_unknown"
    cr9, slope9 = (tee["cr_f9"], tee["slope_f9"]) if nine == "front" else (tee["cr_b9"], tee["slope_b9"])
    if cr9 is None or slope9 is None:
        return "nine_unrated"
    span = range(1, 10) if nine == "front" else range(10, 19)
    par9 = _par(None, tee_holes, span, rnd["par_played"])
    card = _hole_card(conn, rnd, tee_holes, span)
    if card is None or par9 is None:
        ags = rnd["gross"]              # total only: an upper bound, no per-hole cap possible
    else:
        ch9 = None if hi_before is None else nine_hole_course_handicap(hi_before, slope9, cr9, par9)
        ags = adjusted_gross_score(card, ch9)
    diff9 = _raw_differential(ags, cr9, slope9)            # unrounded until combined (Rule 5.1b)
    hi = expected_hi if expected_hi is not None else hi_before
    differential = None if hi is None else nine_to_eighteen(diff9, hi)
    return Scored(ags, differential, "nine_hole_scaled", 9, diff9)


def _hi_in_effect(revisions: list[tuple[date, float | None]], day: date) -> float | None:
    """The HI from the latest scored day before `day` (an index updates overnight)."""
    for d, hi in reversed(revisions):
        if d < day:
            return hi
    return None


def recompute(conn: sqlite3.Connection, cfg: Config | None = None, *, include_nine: bool | None = None) -> dict:
    """Rebuild handicap_history from scratch in chronological order (PCC = 0; unofficial).

    Walks v_rounds (overrides applied, excluded rounds skipped). Only rounds that produce a
    differential get a handicap_history row; the rest are counted by reason in the summary.
    `include_nine` None means the saved setting (include_nine_setting; default: include). An
    explicit value is used for this run only; set_include_nine() persists a choice.

    Until an index exists, 9-hole scores wait (differential None) and every score counts toward the
    54 holes of Rule 4.5; the round that reaches 54 establishes the index, and the waiting nines are
    converted with solve_initial_index. After that each 9-hole score uses the expected score of the
    HI in effect before it (on the establishing day itself, of the index just established).
    `cfg` is accepted for CLI symmetry; nothing in it changes WHS math today.
    """
    source = "explicit"
    if include_nine is None:
        include_nine, source = include_nine_setting(conn), "setting"
    rounds = conn.execute(
        "SELECT * FROM v_rounds WHERE excluded = 0 ORDER BY played_on_local, played_at_utc, round_id"
    ).fetchall()
    record: list[_Score] = []
    revisions: list[tuple[date, float | None]] = []
    rows: list[dict] = []
    skipped: dict[str, list[str]] = defaultdict(list)
    holes_posted = 0
    established_on: str | None = None
    current_hi: float | None = None

    for rnd in rounds:
        day = date.fromisoformat(rnd["played_on_local"])
        hi_before = _hi_in_effect(revisions, day)
        scored = score_round(conn, rnd, hi_before, include_nine,
                             expected_hi=hi_before if hi_before is not None else current_hi)
        if isinstance(scored, str):
            skipped[scored].append(rnd["round_id"])
            continue
        holes_posted += scored.holes
        esr = 0.0 if scored.differential is None else exceptional_score_reduction(scored.differential, hi_before)
        row = {
            "round_id": rnd["round_id"], "as_of": rnd["played_on_local"], "n_scores": len(record) + 1,
            "ags": scored.ags, "differential": scored.differential, "differential_kind": scored.kind,
            "hi_before": hi_before, "hi_after": None, "low_hi": None, "cap_applied": None, "esr": esr or None,
        }
        record.append(_Score(None if scored.differential is None else _dec(scored.differential),
                             diff9=scored.diff9, row=row))
        rows.append(row)
        if esr:
            for s in record[-RECORD_SIZE:]:
                s.esr_adjustment += _dec(esr)
        if established_on is None and holes_posted >= MIN_HOLES:
            established_on = rnd["played_on_local"]
            if any(s.differential is None for s in record):
                _, converted = solve_initial_index([(s.differential, s.diff9) for s in record])
                for s, d in zip(record, converted):
                    if s.differential is None:
                        s.differential = _dec(d)
                        s.row["differential"] = d
        if established_on is None:
            revisions.append((day, None))
            continue
        calculated = handicap_index([s.differential + s.esr_adjustment for s in record])
        low_hi = low_handicap_index(revisions, day) if len(record) >= RECORD_SIZE else None
        hi_after, cap = (None, None) if calculated is None else apply_caps(calculated, low_hi)
        row.update(hi_after=hi_after, low_hi=low_hi, cap_applied=cap)
        revisions.append((day, hi_after))
        current_hi = hi_after if hi_after is not None else current_hi

    waiting = [r["round_id"] for r in rows if r["differential"] is None]
    if waiting:
        skipped["nine_awaiting_54_holes"] = waiting
    rows = [r for r in rows if r["differential"] is not None]
    with conn:
        conn.execute("DELETE FROM handicap_history")
        for row in rows:
            upsert(conn, "handicap_history", row, ["round_id"])

    latest = next((r for r in reversed(rows) if r["hi_after"] is not None), {})
    return {
        "rounds_considered": len(rounds),
        "scored": len(rows),
        "by_kind": dict(Counter(r["differential_kind"] for r in rows)),
        "skipped": {k: len(v) for k, v in skipped.items()},
        "skipped_rounds": dict(skipped),
        "handicap_index": latest.get("hi_after"),
        "low_hi": latest.get("low_hi"),
        "as_of": latest.get("as_of"),
        "n_scores": len(rows),
        "holes_posted": holes_posted,
        "holes_needed": max(0, MIN_HOLES - holes_posted) if established_on is None else 0,
        "established_on": established_on,
        "include_nine": include_nine,
        "include_nine_source": source,
        "pcc": 0,
        "unofficial": True,
    }


def compare_18birdies(conn: sqlite3.Connection) -> list[dict]:
    """Our 18-hole(-equivalent) differential next to 18Birdies' own roundHandicap, round by round.

    Rows (date order): round_id, date, holes, course_key, differential (ours, from handicap_history;
    None when the round isn't scored, e.g. no rating), differential_kind, round_handicap_18b, delta
    (ours - theirs, rounded to 0.1). Both are unofficial; 18Birdies' method is not published (for
    Shane's 9-hole rounds it looks like twice the 9-hole differential, where the WHS adds an
    expected score instead), so the delta is a cross-check, not an error. Run recompute first.
    """
    out = []
    for r in conn.execute(
            "SELECT v.round_id, v.played_on_local, v.played_at_utc, v.holes_played, v.course_key_eff, "
            "v.round_handicap_18b, v.excluded, h.differential, h.differential_kind "
            "FROM v_rounds v LEFT JOIN handicap_history h ON h.round_id = v.round_id "
            "WHERE v.round_handicap_18b IS NOT NULL ORDER BY v.played_on_local, v.played_at_utc, v.round_id"):
        ours, theirs = r["differential"], r["round_handicap_18b"]
        out.append({
            "round_id": r["round_id"], "date": r["played_on_local"], "holes": r["holes_played"],
            "course_key": r["course_key_eff"], "excluded": bool(r["excluded"]),
            "differential": ours, "differential_kind": r["differential_kind"], "round_handicap_18b": theirs,
            "delta": None if ours is None else round_tenth(_dec(ours) - _dec(theirs)),
        })
    return out
