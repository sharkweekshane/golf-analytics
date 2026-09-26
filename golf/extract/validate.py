"""Deterministic checks on an ExtractedRound (RESEARCH_PLAN §3: R1-R7 errors, W1-W4 warnings).

Beyond the plan: P1 (the screens name another player), W5 (a screenshot-only round whose hole scores
nothing independent can check), and RR (a targeted re-read disagreed with the first reading). The
rounds module adds D1 (no played date) and M1 (a same-day, same-club record with different scores).

An error (E) means a value contradicts an independent record or is impossible: any E sends the round
to review. A warning (W) is a doubt: auto-accept tolerates one. Every flag names the hole and field it
is about, so the review page can colour that exact cell and the targeted re-read knows what to ask.

Flags are plain dicts {code, severity, hole, field, message}. Two optional keys are added later:
`reread` (an independent second reading of that cell) and `cleared` (a warning resolved by a re-read
that agreed with the first reading). Errors are never cleared by a re-read: two reads agreeing on a
value that contradicts the export means the export (or the round) needs a look, not that all is well.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Literal, Mapping, get_args

from rapidfuzz import fuzz

from ..schemas.rounds import ExtractedRound, HoleField, HoleRow

Severity = Literal["E", "W"]

HOLE_FIELDS: tuple[str, ...] = get_args(HoleField)
INT_FIELDS = ("par", "si", "strokes", "putts", "penalties", "chips", "sand")
STATS_FIELDS = ("putts", "fairway", "gir", "gir_miss", "penalties", "chips", "sand")
FAIRWAY_RESULTS = ("hit", "left", "right", "short", "long", "miss")
MISS_DIRECTIONS = ("left", "right", "short", "long")
FIELD_CHOICES: dict[str, tuple[str, ...]] = {
    f: get_args(HoleRow.model_fields[f].annotation) for f in ("symbol", "fairway", "gir", "gir_miss")
}
ONE_NINE, BACK_NINE, EIGHTEEN = frozenset(range(1, 10)), frozenset(range(10, 19)), frozenset(range(1, 19))

# strokes - par each symbol allows; max_star (18Birdies' "Max") is deliberately not checked.
SYMBOL_OK = {
    "none": lambda d: d == 0,
    "circle": lambda d: d == -1,
    "double_circle": lambda d: d <= -2,
    "square": lambda d: d == 1,
    "double_square": lambda d: d >= 2,
}
_STALE_EXPORT_NOTE = " A re-read agrees with the first reading, so the export may be stale: download it again."


def flag(code: str, severity: Severity, hole: int | None, field: str | None, message: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "hole": hole, "field": field, "message": message}


def active_flags(flags: Iterable[dict]) -> list[dict]:
    return [f for f in flags if not f.get("cleared")]


def cell_key(hole: int | None, field: str | None) -> str:
    """'7:putts' for a cell, ':putts' for a round-level total, '7:' for a whole hole. A string, so a
    review payload stays JSON-serialisable."""
    return f"{'' if hole is None else hole}:{field or ''}"


def flags_by_cell(flags: Iterable[dict]) -> dict[str, list[dict]]:
    """Active flags keyed by cell_key(hole, field)."""
    out: dict[str, list[dict]] = {}
    for f in active_flags(flags):
        out.setdefault(cell_key(f["hole"], f["field"]), []).append(f)
    return out


def _known(v: int) -> bool:
    return v is not None and v >= 0


# ------------------------------------------------------------------ rules
def validate_extraction(
    conn: sqlite3.Connection,
    extracted: ExtractedRound,
    round_row: Mapping[str, Any] | None,
    tee_holes: Mapping[int, Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None,
    *,
    expected_holes: int | None = None,
    player_name: str | None = None,
) -> list[dict[str, Any]]:
    """All flags for one extraction.

    `round_row` is the matched rounds row (sqlite3.Row or dict). R1/R2 need it to be an independent
    record (source '18b_export' or 'manual'); a screenshot round is just another reading, so with one
    (or none) the strokes checksum falls back to the totals printed on the screens (R1, still an E),
    because those strokes become the round's truth. R3 compares per-hole par with `tee_holes` when the
    tee is known, and Σ par with the export's strokes - score when that is known."""
    ref = dict(round_row) if round_row is not None else None
    if ref is not None and ref.get("source") == "screenshot":
        ref = None
    ref_strokes = reference_strokes(conn, ref["round_id"]) if ref else {}
    tees = _tee_map(tee_holes)
    holes = extracted.holes

    flags: list[dict[str, Any]] = []
    flags += _player(extracted, player_name)
    if ref:
        flags += _r1_reference(holes, ref_strokes)
        flags += _r2(holes, ref)
    else:
        checked, mismatches = _strokes_vs_printed(holes, extracted, "R1", "E")
        flags += mismatches
        if not checked and any(h.strokes >= 1 for h in holes):
            flags.append(flag("W5", "W", None, "strokes",
                              "Nothing independent checks these hole scores: no export round matched and no "
                              "Out / In / Gross total was captured."))
    flags += _r3(holes, tees, ref, ref_strokes)
    for h in holes:
        flags += _r4(h) + _r5(h) + _r7(h) + _w1(h) + _w2(h)
    flags += _r6(holes, ref_strokes, expected_holes)
    flags += _w3(extracted)
    if ref:
        flags += _strokes_vs_printed(holes, extracted, "W4", "W")[1]
    flags += _w4(holes, extracted)
    return flags


def reference_strokes(conn: sqlite3.Connection, round_id: str) -> dict[int, int]:
    rows = conn.execute(
        "SELECT hole, strokes FROM round_holes WHERE round_id = ? AND strokes > 0", (round_id,)).fetchall()
    return {r["hole"]: r["strokes"] for r in rows}


def _tee_map(tee_holes) -> dict[int, dict]:
    if not tee_holes:
        return {}
    if isinstance(tee_holes, Mapping):
        return {int(k): dict(v) for k, v in tee_holes.items()}
    return {int(r["hole"]): dict(r) for r in tee_holes}


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _player(ex: ExtractedRound, player_name: str | None) -> list[dict]:
    """Reading another player's row is the one misread no per-cell check can catch."""
    if not player_name or not ex.player_name.strip():
        return []
    a, b = _norm_name(player_name), _norm_name(ex.player_name)
    if a and b and (a in b or b in a or fuzz.partial_ratio(a, b) >= 85):
        return []
    return [flag("P1", "E", None, None,
                 f"The screens show player '{ex.player_name}', not '{player_name}'. Check that the right "
                 "row was read, or fix [player].name in config.toml.")]


def _r1_reference(holes: list[HoleRow], ref_strokes: dict[int, int]) -> list[dict]:
    out = []
    for h in holes:
        if h.strokes < 1:
            continue
        exp = ref_strokes.get(h.hole)
        if exp is None:
            out.append(flag("R1", "E", h.hole, "strokes",
                            f"Hole {h.hole}: read {h.strokes} strokes, but the export has no score for this hole."))
        elif exp != h.strokes:
            out.append(flag("R1", "E", h.hole, "strokes",
                            f"Hole {h.hole}: read {h.strokes} strokes, the export has {exp}."))
    return out


def _stat_holes(holes: list[HoleRow]) -> list[HoleRow]:
    return [h for h in holes if h.stats_visible]


def _r2(holes: list[HoleRow], ref: dict) -> list[dict]:
    """Round totals the export tracked vs what the per-hole reads add up to."""
    seen = _stat_holes(holes)
    if not seen:
        return []
    missing = len(holes) - len(seen)
    tail = f" ({missing} holes' stats were not captured.)" if missing else ""
    out = []
    if ref.get("putts_tracked") and ref.get("putts") is not None:
        total = sum(h.putts for h in seen if _known(h.putts))
        if total != ref["putts"]:
            out.append(flag("R2", "E", None, "putts", f"Putts add up to {total}; the export has {ref['putts']}.{tail}"))
    if (ref.get("fw_chances") or 0) > 0:
        for result, col in (("hit", "fw_hit"), ("left", "fw_left"), ("right", "fw_right"),
                            ("short", "fw_short"), ("long", "fw_long")):
            if ref.get(col) is None:
                continue
            n = sum(h.fairway == result for h in seen)
            if n != ref[col]:
                out.append(flag("R2", "E", None, "fairway",
                                f"{n} fairways read as '{result}'; the export has {ref[col]}.{tail}"))
    if (ref.get("gir_chances") or 0) > 0:
        if ref.get("gir") is not None:
            n = sum(h.gir == "hit" for h in seen)
            if n != ref["gir"]:
                out.append(flag("R2", "E", None, "gir",
                                f"{n} greens in regulation read; the export has {ref['gir']}.{tail}"))
        for d in MISS_DIRECTIONS:
            col = f"gir_{d}"
            if ref.get(col) is None:
                continue
            n = sum(h.gir == "miss" and h.gir_miss == d for h in seen)
            if n != ref[col]:
                out.append(flag("R2", "E", None, "gir_miss",
                                f"{n} greens missed '{d}' read; the export has {ref[col]}.{tail}"))
    return out


def _r3(holes: list[HoleRow], tees: dict[int, dict], ref: dict | None, ref_strokes: dict[int, int]) -> list[dict]:
    out = []
    for h in holes:
        tee = tees.get(h.hole)
        if not tee:
            continue
        for fld, alt in (("par", "par_alt"), ("si", "si_alt")):
            want, got = tee.get(fld), getattr(h, fld)
            if want is None or not _known(got) or want in (got, getattr(h, alt)):
                continue
            out.append(flag("R3", "E", h.hole, fld, f"Hole {h.hole}: read {fld} {got}, the course data has {want}."))
    if ref and ref.get("par_played") is not None:
        played = set(ref_strokes) or {h.hole for h in holes if h.strokes > 0}
        pars = {h.hole: h.par for h in holes if h.hole in played}
        if played and len(pars) == len(played) and all(_known(p) for p in pars.values()):
            total = sum(pars.values())
            if total != ref["par_played"]:
                out.append(flag("R3", "E", None, "par",
                                f"Par of the holes played adds up to {total}; the export implies {ref['par_played']} "
                                "(strokes - score)."))
    return out


def _r4(h: HoleRow) -> list[dict]:
    out = []

    def bad(fld: str, why: str) -> None:
        out.append(flag("R4", "E", h.hole, fld, f"Hole {h.hole}: {fld} {why}."))

    for fld in INT_FIELDS:
        if getattr(h, fld) < -1:
            bad(fld, f"= {getattr(h, fld)} is not a valid value")
    if _known(h.par) and h.par not in (3, 4, 5, 6):
        bad("par", f"= {h.par} is not 3-6")
    if _known(h.strokes) and not 1 <= h.strokes <= (h.par + 10 if _known(h.par) else 15):
        bad("strokes", f"= {h.strokes} is implausible")
    if _known(h.putts):
        if h.putts > 6:
            bad("putts", f"= {h.putts} is more than 6")
        elif h.strokes >= 1 and h.putts > h.strokes - 1:
            bad("putts", f"= {h.putts} leaves no stroke to reach the green ({h.strokes} strokes)")
    for fld in ("penalties", "chips", "sand"):
        if getattr(h, fld) > 6:
            bad(fld, f"= {getattr(h, fld)} is more than 6")
    return out


def _r5(h: HoleRow) -> list[dict]:
    if not _known(h.par):
        return []
    if h.par == 3 and h.fairway in FAIRWAY_RESULTS:
        return [flag("R5", "E", h.hole, "fairway", f"Hole {h.hole} is a par 3 but has fairway '{h.fairway}'.")]
    if h.par != 3 and h.fairway == "not_applicable":
        return [flag("R5", "E", h.hole, "fairway", f"Hole {h.hole} is a par {h.par} but fairway is 'not_applicable'.")]
    return []


def _r6(holes: list[HoleRow], ref_strokes: dict[int, int], expected_holes: int | None) -> list[dict]:
    if not holes:
        return [flag("R6", "E", None, None, "No hole grid was read (total only); there is no per-hole data to keep.")]
    out = []
    numbers = [h.hole for h in holes]
    seen = set(numbers)
    for n in sorted({n for n in numbers if numbers.count(n) > 1}):
        out.append(flag("R6", "E", n, None, f"Hole {n} appears {numbers.count(n)} times."))
    for n in sorted(seen - EIGHTEEN):
        out.append(flag("R6", "E", n, None, f"Hole number {n} is outside 1-18."))
    if ref_strokes:
        missing, unscored = set(ref_strokes) - seen, set()
    else:
        card = played_card(holes, expected_holes)
        missing = card - seen
        unscored = {h.hole for h in holes if h.hole in card and h.strokes < 1}
    for n in sorted(missing):
        out.append(flag("R6", "E", n, None, f"Hole {n} is missing from the screenshots."))
    for n in sorted(unscored):
        out.append(flag("R6", "E", n, "strokes", f"Hole {n}: no score was read, and no export round supplies one."))

    si = [(h.hole, h.si) for h in holes if _known(h.si)]
    values = [v for _, v in si]
    for hole, v in si:
        if not 1 <= v <= 18:
            out.append(flag("R6", "E", hole, "si", f"Hole {hole}: stroke index {v} is outside 1-18."))
        elif values.count(v) > 1:
            out.append(flag("R6", "E", hole, "si", f"Hole {hole}: stroke index {v} is used on more than one hole."))
    return out


def played_card(holes: list[HoleRow], expected_holes: int | None = None) -> frozenset[int]:
    """The holes this round should cover: the smallest standard card (front, back, 18) holding every
    hole with a score. A Scores view shows all 18 columns even for a 9-hole round, so blank columns
    don't make a card bigger."""
    basis = {h.hole for h in holes if h.strokes >= 1} or {h.hole for h in holes}
    cards = {18: (EIGHTEEN,), 9: (ONE_NINE, BACK_NINE)}.get(expected_holes, (ONE_NINE, BACK_NINE, EIGHTEEN))
    for card in cards:
        if basis <= card:
            return card
    return max(cards, key=lambda c: len(basis & c))


def _r7(h: HoleRow) -> list[dict]:
    check = SYMBOL_OK.get(h.symbol)
    if check is None or not (_known(h.strokes) and h.strokes >= 1 and _known(h.par)):
        return []
    if check(h.strokes - h.par):
        return []
    return [flag("R7", "E", h.hole, "symbol",
                 f"Hole {h.hole}: symbol '{h.symbol}' does not fit {h.strokes} strokes on a par {h.par}.")]


def _w1(h: HoleRow) -> list[dict]:
    """Warning only: a putt from the fringe breaks it, and with 'Automatic Stats Calculation' on the
    recorded GIR was itself derived from these numbers."""
    if h.gir not in ("hit", "miss") or not (h.strokes >= 1 and _known(h.putts) and _known(h.par)):
        return []
    regulation = h.strokes - h.putts <= h.par - 2
    if (h.gir == "hit") == regulation:
        return []
    return [flag("W1", "W", h.hole, "gir",
                 f"Hole {h.hole}: GIR '{h.gir}' but {h.strokes} strokes with {h.putts} putts on a par {h.par}.")]


def _w2(h: HoleRow) -> list[dict]:
    parts = (h.putts, h.chips, h.sand, h.penalties)
    if h.strokes < 1 or not all(_known(p) for p in parts):
        return []
    need = 1 + sum(parts)
    if h.strokes >= need:
        return []
    return [flag("W2", "W", h.hole, "strokes",
                 f"Hole {h.hole}: {h.strokes} strokes is fewer than tee shot + putts + chips + sand + penalties "
                 f"= {need}.")]


def _w3(ex: ExtractedRound) -> list[dict]:
    out = []
    for h in ex.holes:
        for fld in dict.fromkeys(h.uncertain_fields):
            out.append(flag("W3", "W", h.hole, fld, f"Hole {h.hole}: the model was unsure of {fld}."))
        if h.confidence == "low":
            out.append(flag("W3", "W", h.hole, None, f"Hole {h.hole}: read with low confidence."))
    notes = list(ex.issues) + [f"Image {im.image_index}: {im.problems}" for im in ex.images if im.problems.strip()]
    if notes:
        out.append(flag("W3", "W", None, None, "Issues noted while reading: " + "; ".join(notes)))
    if ex.overall_confidence == "low":
        out.append(flag("W3", "W", None, None, "Overall confidence is low."))
    return out


def _strokes_vs_printed(holes: list[HoleRow], ex: ExtractedRound, code: str, sev: Severity) -> tuple[int, list[dict]]:
    """Σ hole strokes vs the Out / In / Gross printed on the screens: (checks run, mismatch flags)."""
    known = {h.hole: h.strokes for h in holes if h.strokes >= 1}
    played = frozenset(known)
    checked, out = 0, []
    for name, nine in (("front", ONE_NINE), ("back", BACK_NINE)):
        printed = getattr(ex.displayed, name)
        if _known(printed) and nine <= played:
            checked += 1
            total = sum(known[n] for n in nine)
            if total != printed:
                out.append(flag(code, sev, None, "strokes",
                                f"Holes {min(nine)}-{max(nine)} add up to {total}; the screen prints {name} {printed}."))
    gross = ex.displayed.gross
    if _known(gross) and played in (ONE_NINE, BACK_NINE, EIGHTEEN):
        checked += 1
        total = sum(known.values())
        if total != gross:
            out.append(flag(code, sev, None, "strokes",
                            f"Hole scores add up to {total}; the screen prints gross {gross}."))
    return checked, out


def parse_to_par(text: str) -> int | None:
    s = text.strip().replace("−", "-").replace("–", "-")
    if s.upper() in ("E", "EVEN"):
        return 0
    m = re.fullmatch(r"([+-]?)\s*(\d+)", s)
    if not m:
        return None
    return -int(m.group(2)) if m.group(1) == "-" else int(m.group(2))


def _w4(holes: list[HoleRow], ex: ExtractedRound) -> list[dict]:
    """Stats totals printed on the screens (Benchmarks / Basic Stats) vs the per-hole reads."""
    d, seen, out = ex.displayed, _stat_holes(holes), []
    if seen:
        for printed, total, fld, label in (
            (d.total_putts, sum(h.putts for h in seen if _known(h.putts)), "putts", "putts"),
            (d.fairways_hit, sum(h.fairway == "hit" for h in seen), "fairway", "fairways hit"),
            (d.gir, sum(h.gir == "hit" for h in seen), "gir", "greens in regulation"),
            (d.penalties, sum(h.penalties for h in seen if _known(h.penalties)), "penalties", "penalties"),
        ):
            if _known(printed) and printed != total:
                out.append(flag("W4", "W", None, fld,
                                f"Per-hole reads give {total} {label}; the screen prints {printed}."))
    to_par = parse_to_par(d.to_par_text) if d.to_par_text else None
    played = [h for h in holes if h.strokes >= 1]
    if to_par is not None and played and all(_known(h.par) for h in played):
        got = sum(h.strokes for h in played) - sum(h.par for h in played)
        if got != to_par:
            out.append(flag("W4", "W", None, "par",
                            f"Strokes minus par comes to {got:+d}; the screen prints {d.to_par_text}."))
    return out


# ------------------------------------------------------- acceptance rules
def auto_accept(flags: Iterable[dict], extracted: ExtractedRound) -> bool:
    """RESEARCH_PLAN §3: no E, at most one W, no low-confidence holes. A low-confidence hole stops
    counting once every field it listed as uncertain was confirmed by an agreeing re-read."""
    flags = list(flags)
    act = active_flags(flags)
    if any(f["severity"] == "E" for f in act) or sum(f["severity"] == "W" for f in act) > 1:
        return False
    confirmed = confirmed_cells(flags)
    for h in extracted.holes:
        doubts_confirmed = bool(h.uncertain_fields) and all((h.hole, f) in confirmed for f in h.uncertain_fields)
        if h.confidence == "low" and not doubts_confirmed:
            return False
    return True


def is_clean(flags: Iterable[dict]) -> bool:
    """After a human correction: no E and at most one W (a reviewer has looked at the grid)."""
    act = active_flags(flags)
    return not any(f["severity"] == "E" for f in act) and sum(f["severity"] == "W" for f in act) <= 1


# ---------------------------------------------------------------- re-reads
def cell_value(extracted: ExtractedRound, hole: int, field: str) -> str | None:
    """The current reading of one cell as text (-1 stays "-1"); None if the hole is absent."""
    for h in extracted.holes:
        if h.hole == hole:
            return str(getattr(h, field))
    return None


def norm_reading(field: str, value: str) -> str:
    """Normalise a re-read answer to the ExtractedRound representation. "" means unreadable."""
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_") if field not in INT_FIELDS else str(value).strip()
    if field in INT_FIELDS:
        if s in ("-", "–", "—", "none", "not_recorded"):
            return "-1"
        m = re.fullmatch(r"(\d+)(\s*/\s*\d+)?", s)
        return str(int(m.group(1))) if m else ""
    return s if s in FIELD_CHOICES.get(field, ()) else ""


def reread_agrees(hole: int, field: str, original: str | None, reread) -> bool:
    """Agreement needs a readable, not-low-confidence answer about the cell that was asked."""
    if original is None or reread.hole != hole or reread.field != field or reread.confidence == "low":
        return False
    got = norm_reading(field, reread.value)
    return got != "" and got == original


def confirmed_cells(flags: Iterable[dict]) -> set[tuple[int, str]]:
    return {(f["hole"], f["field"]) for f in flags if (f.get("reread") or {}).get("agrees")}


def apply_rereads(flags: list[dict], rereads: Mapping[tuple[int, str], dict], extracted: ExtractedRound) -> list[dict]:
    """Attach re-read records to the flags of their cells. A record only counts while the cell still
    holds the value it was compared against; an agreeing re-read clears warnings, never errors, and a
    readable disagreement becomes an error (RR) so the two readings go to Shane."""
    for (hole, field), rec in rereads.items():
        original = cell_value(extracted, hole, field)
        got = norm_reading(field, rec.get("value", ""))
        if rec.get("original") != original or rec.get("agrees") or not got or got == original:
            continue
        if not any(f["code"] == "RR" and (f["hole"], f["field"]) == (hole, field) for f in flags):
            flags.append(flag("RR", "E", hole, field,
                              f"Hole {hole}: a second reading of {field} gave {rec.get('value')!r}; "
                              f"the first gave {original!r}."))
    for f in flags:
        rec = rereads.get((f["hole"], f["field"]))
        if not rec or rec.get("original") != cell_value(extracted, f["hole"], f["field"]):
            continue
        f["reread"] = dict(rec)
        if rec.get("agrees"):
            if f["severity"] == "W":
                f["cleared"] = True
            elif f["code"] == "R1" and not f["message"].endswith(_STALE_EXPORT_NOTE):
                f["message"] += _STALE_EXPORT_NOTE
    confirmed = confirmed_cells(flags)
    by_hole = {h.hole: h for h in extracted.holes}
    for f in flags:
        h = by_hole.get(f["hole"]) if f["hole"] is not None else None
        if (f["code"] == "W3" and f["field"] is None and h is not None and h.uncertain_fields
                and all((h.hole, u) in confirmed for u in h.uncertain_fields)):
            f["cleared"] = True
    return flags


def rereads_of(flags: Iterable[dict]) -> dict[tuple[int, str], dict]:
    return {(f["hole"], f["field"]): f["reread"] for f in flags if f.get("reread")}


def carry_over(old_flags: Iterable[dict], new_flags: list[dict], extracted: ExtractedRound) -> list[dict]:
    """Keep earlier re-read results when flags are recomputed (after a correction or a merge)."""
    return apply_rereads(new_flags, rereads_of(old_flags), extracted)


def reread_targets(flags: Iterable[dict], limit: int = 5) -> list[tuple[int, str]]:
    """Cells worth a second look, errors first; cells already re-read are skipped."""
    cells: list[tuple[int, str]] = []
    act = [f for f in active_flags(flags) if f["hole"] is not None and f["field"] in HOLE_FIELDS and not f.get("reread")]
    for f in sorted(act, key=lambda f: f["severity"] != "E"):
        cell = (f["hole"], f["field"])
        if cell not in cells:
            cells.append(cell)
    return cells[:limit]
