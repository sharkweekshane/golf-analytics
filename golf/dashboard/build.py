"""Build the self-contained dashboard: one HTML file with the data inlined as JSON.

dashboard_data() gathers everything the page shows into a plain JSON-serialisable dict (display
strings are made here so they are testable); render_dashboard() inlines it into template.html;
build() writes the file. The page makes no network requests: no CDN, no fonts, no <script src>.

Scale: Shane mostly plays nine holes at one course, so the primary score is strokes over par per 9
holes (an 18-hole round is shown at half its to-par: the average of its two nines) and every
counting stat is per 9 holes. The 18-hole-equivalent differential gets its own chart, beside
18Birdies' own per-round number. A course filter re-scopes the score charts, lessons, stats, club
distances and the rounds table; each filtered slice is precomputed here, so the page does no maths.

public=True: the same golf (rounds, stats, club distances, lessons and notes events, handicap), but no
file paths, screenshot paths, GPS coordinates, API cost or spend, command hints or local-machine
details. Coordinates are never read by this module in either mode (stats.load_shots skips them).
18Birdies' own ids stay home too: its round ids are version-1 UUIDs that encode when the round was
created (and key the round in 18Birdies' backend), so the public page numbers rounds r1..rN by date
(_pseudonymize) and drops club ids.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ..analytics.lessons import lesson_report
from ..analytics.stats import (
    best_nine, club_distances, hi_series, hole_averages, injury_spans, last_rounds_per9,
    lesson_metric as lesson_scale, load_shots, load_timeline, metric_series, outcome_sigma, outcome_trend,
    practice_blocks, round_facts, set_outcome, shot_coverage, summary_stats, weighted_mean,
)
from ..config import EXPORT_STALE_DAYS, Config
from ..db import loads, now_iso
from ..demo import has_demo

TEMPLATE_PATH = Path(__file__).with_name("template.html")
PLACEHOLDER = "__GOLF_DASHBOARD_DATA__"
STALE_EXPORT_DAYS = EXPORT_STALE_DAYS
DOWNLOAD_HINT = "download: 18birdies.com/download-account-data > Request My Data; it imports itself from Downloads"
LAST_N = 5
MIN_COURSE_ROUNDS = 3          # a course gets its own filter entry from this many rounds
MAX_COURSES = 5                # the rest fold into "Other courses"

SCORE = {
    "key": "to_par_9", "label": "Strokes over par per 9 holes", "short": "to par per 9",
    "note": ("Each dot is a 9-hole round. An 18-hole round is a square at half its score to par, the average "
             "of its two nines, and weighs twice as much in the trend."),
}
METRICS = {
    "differential": {
        "key": "differential", "label": "Score differential (18-hole equivalent)", "short": "differential",
        "note": ("Unofficial: playing conditions (PCC) are not applied. A 9-hole round counts as 2 \u00d7 its "
                 "9-hole differential and weighs half."),
    },
    "to_par_9": {
        "key": "to_par_9", "label": "Strokes over par per 9 holes", "short": "to par per 9",
        "note": ("Fewer than 3 rounds (or under half of them) have a course rating, so lessons are compared on "
                 "strokes over par per 9 holes; an 18-hole round counts as two nines."),
    },
}

# key, label, unit, better, tier, equal-span group (same units per pixel), EWMA weight key, pooled key, description
PANELS: list[tuple[str, str, str, str, str, str | None, str | None, str, str]] = [
    ("fir_pct", "Fairways hit", "pct", "higher", "A", None, "fw_chances", "fir",
     "Share of par-4 and par-5 tee shots that finished in the fairway."),
    ("gir_9", "Greens in regulation per 9", "num", "higher", "A", None, "gir_chances", "gir_9",
     "Greens reached in par \u2212 2 strokes or fewer, per 9 holes (9 would be every green)."),
    ("putts_9", "Putts per 9 holes", "num", "lower", "A", None, "weight", "putts_9",
     "A count of putts, not putting skill: putt lengths are unknown and more greens hit means longer putts. "
     "Rounds where putts were entered on only some holes are left out."),
    ("to_green_9", "Reaching the green: strokes over regulation, per 9", "num", "lower", "A", "split", "weight",
     "to_green_9", "(Strokes \u2212 putts) \u2212 (par \u2212 2 per hole), per 9 holes. With the next panel it "
     "adds up exactly to your score to par."),
    ("putts_over_9", "Putts over two per hole, per 9", "num", "lower", "A", "split", "weight", "putts_over_9",
     "Putts \u2212 2 per hole, per 9 holes. Negative means fewer than two putts per hole on average."),
    ("dbl_9", "Double bogey or worse per 9", "num", "lower", "A", None, "weight", "dbl_9",
     "Holes finished two or more over par, per 9 holes."),
    ("triple_9", "Triple bogey or worse per 9", "num", "lower", "B", None, "weight", "triple_9",
     "Holes finished three or more over par, per 9 holes. When most holes are doubles, the triples are "
     "where the big numbers come from (needs the course's per-hole pars)."),
    ("par3_avg", "Par 3s: average to par", "num", "lower", "B", "partype", "par3_n", "par3_avg",
     "Average score relative to par on par 3s (needs the course's per-hole pars)."),
    ("par4_avg", "Par 4s: average to par", "num", "lower", "B", "partype", "par4_n", "par4_avg",
     "Average score relative to par on par 4s."),
    ("par5_avg", "Par 5s: average to par", "num", "lower", "B", "partype", "par5_n", "par5_avg",
     "Average score relative to par on par 5s."),
    ("three_putt_pct", "3-putt rate", "pct", "lower", "C", None, "putt_holes", "three_putt_pct",
     "Share of holes with three or more putts (per-hole putts from screenshots)."),
    ("one_putt_pct", "1-putt rate", "pct", "higher", "C", None, "putt_holes", "one_putt_pct",
     "Share of holes with exactly one putt."),
    ("scramble_pct", "Scrambling", "pct", "higher", "C", None, "scramble_chances", "scramble_pct",
     "Missed greens where you still made par or better."),
    ("putts_per_gir", "Putts per green in regulation", "num", "lower", "C", None, "gir_putt_holes",
     "putts_per_gir", "Average putts on holes where you hit the green in regulation."),
    ("penalties_9", "Penalty strokes per 9", "num", "lower", "C", None, "weight", "penalties_9",
     "Penalty strokes recorded per hole, per 9 holes."),
]
SIGNED_KEYS = frozenset({"par3_avg", "par4_avg", "par5_avg", "to_green_9", "putts_over_9"})
TWO_DECIMALS = frozenset({"putts_per_gir"})
LANES = {"lesson": "lesson", "practice": "practice", "equipment_change": "equipment", "fitting": "equipment",
         "injury": "injury"}


def _today(cfg: Config) -> date:
    try:
        return datetime.now(ZoneInfo(cfg.timezone)).date()
    except (KeyError, ValueError):  # unknown tz name in config: fall back to the machine's date
        return date.today()


def _local_date(ts: str | None, cfg: Config) -> date | None:
    """An ISO timestamp (stored in UTC) as a date in the player's timezone."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return _iso_date(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(ZoneInfo(cfg.timezone)).date()
    except (KeyError, ValueError):
        return dt.date()


def _iso_date(s: Any) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _clean(obj: Any) -> Any:
    """JSON-safe copy: floats rounded to 4 places, NaN/inf -> None, dates -> ISO, tuples -> lists."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 4)
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy scalar
        return _clean(obj.item())
    return obj


def _f1(x: float | None) -> str:
    return "\u2014" if x is None else f"{x:.1f}".replace("-", "\u2212")


def _signed(x: float, digits: int = 1) -> str:
    s = f"{abs(x):.{digits}f}"
    return ("+" if x > 0 and float(s) else "\u2212" if x < 0 and float(s) else "") + s


def _to_par(x: int | None) -> str:
    return "\u2014" if x is None else "E" if x == 0 else ("+" if x > 0 else "\u2212") + str(abs(x))


def _pct(x: float | None) -> str:
    return "\u2014" if x is None else f"{round(100 * x)}%"


def _md(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}"


def _human(area: str) -> str:
    return area.replace("_", " ").capitalize()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _where(f: dict) -> str:
    return f.get("course_name") or f.get("club_name") or "Unmapped club"


# ----------------------------------------------------------------------------- export age
def _export_status(conn: sqlite3.Connection, cfg: Config, today: date) -> dict:
    """Age of the newest real 18Birdies snapshot, as `golf status` measures it.

    The snapshot date (from the file name, in the import summary) is when the data was downloaded, which
    is what "stale" is about; imported_at is only the fallback, converted from UTC to the local date so an
    evening import is not 'tomorrow'. Demo rows are ignored. A future snapshot date is clamped to 0 and
    flagged.
    """
    best: tuple[date, str, str] | None = None
    for r in conn.execute("""SELECT summary, imported_at FROM imports WHERE kind = '18b_export'
                             AND (sha256 IS NULL OR sha256 NOT LIKE 'demo-%')"""):
        try:
            summary = loads(r["summary"], {}) or {}
        except (TypeError, ValueError):
            summary = {}
        snap = _iso_date(summary.get("snapshot_date")) if isinstance(summary, dict) else None
        d, source = (snap, "snapshot") if snap else (_local_date(r["imported_at"], cfg), "imported")
        if d and (best is None or d > best[0]):
            best = (d, source, r["imported_at"])
    if best is None:
        return {"snapshot_date": None, "age_days": None, "future": False, "source": None, "imported_at": None}
    age = (today - best[0]).days
    return {"snapshot_date": best[0].isoformat(), "age_days": max(0, age), "future": age < 0,
            "source": best[1], "imported_at": best[2]}


def _pseudonymize(obj: Any, alias: dict[str, str]) -> Any:
    """Every string that is a round id, replaced by its public alias (r1..rN), anywhere in the data."""
    if isinstance(obj, str):
        return alias.get(obj, obj)
    if isinstance(obj, dict):
        return {k: _pseudonymize(v, alias) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_pseudonymize(v, alias) for v in obj]
    return obj


def _include_nine(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'whs_include_nine'").fetchone()
    return row is None or str(row[0]) != "0"          # default: 9-hole rounds are included


# ----------------------------------------------------------------------------- sections
def _home_nine_handicap(sf: list[dict], hi: float | None) -> dict | None:
    """The unofficial index in the terms Shane plays: 9-hole Course Handicap (strokes per nine) at the
    most-played rated nine, WHS style: (HI/2 to 0.1) x Slope9/113 + (CR9 - Par9), rounded."""
    from ..whs import nine_hole_course_handicap

    nines = [f for f in sf if f["holes"] == 9 and f.get("cr9") and f.get("slope9") and f.get("par_played")]
    if hi is None or not nines:
        return None
    group = Counter(f["group"] for f in nines).most_common(1)[0][0]
    f = [x for x in nines if x["group"] == group][-1]
    return {"strokes": nine_hole_course_handicap(hi, int(f["slope9"]), f["cr9"], int(f["par_played"])),
            "course": _where(f)}


def _kpis(sf: list[dict], his: list[dict], events: list[dict], quality: dict, today: date,
          include_nine: bool, demo: bool, public: bool = False) -> list[dict]:
    tiles: list[dict] = []
    last = last_rounds_per9(sf, LAST_N)
    if last and last["value"] is not None:
        span = _md(last["start"]) + ("" if last["start"] == last["end"] else f" \u2013 {_md(last['end'])}")
        delta = None
        if last["prev_value"] is not None:
            d = last["value"] - last["prev_value"]
            word = "better" if d < 0 else "worse"
            delta = {"value": d, "good": d < 0, "bad": d > 0,
                     "text": (f"{abs(d):.1f} {word} than the {last['prev_n']} before" if round(d, 1)
                              else f"same as the {last['prev_n']} before")}
        tiles.append({"id": "last5", "label": f"Last {last['n']} rounds", "badge": "to par per 9",
                      "value": last["value"], "display": _signed(last["value"]),
                      "sub": f"average over {last['holes']} holes \u00b7 {span}", "delta": delta})
    else:
        tiles.append({"id": "last5", "label": f"Last {LAST_N} rounds", "badge": "to par per 9", "value": None,
                      "display": "\u2014", "sub": "needs a round with a score to par"})
    best = best_nine(sf)
    tiles.append({"id": "best9", "label": "Best 9 holes", "value": best["to_par"] if best else None,
                  "display": _to_par(best["to_par"]) if best else "\u2014",
                  "sub": (f"{best['gross']} at {best['course']} \u00b7 {_md(best['date'])}"
                          + ("" if best["part"] == "9-hole round" else f" ({best['part']})")) if best
                  else "needs a 9-hole score to par"})
    hi = his[-1]["hi"] if his else None
    low = his[-1]["low_hi"] if his else None
    nine_txt = "9-hole rounds included" if include_nine else "18-hole rounds only"
    home = _home_nine_handicap(sf, hi)
    tiles.append({"id": "hi", "label": "Handicap Index", "badge": "unofficial", "value": hi, "display": _f1(hi),
                  "sub": ((f"\u2248 {home['strokes']} strokes per 9 at {home['course']} \u00b7 " if home else "")
                          + f"{nine_txt} \u00b7 PCC not applied"
                          + (f" \u00b7 low {_f1(low)}" if low is not None else ""))
                  if hi is not None else f"needs 54 holes of scores on rated courses ({nine_txt})",
                  "home": home})
    year = sf[-1]["date"][:4] if sf else str(today.year)
    in_year = [f for f in sf if f["date"].startswith(year)]
    n9 = sum(f["holes"] == 9 for f in in_year)
    n18 = sum(f["holes"] == 18 for f in in_year)
    tiles.append({"id": "season", "label": f"Rounds in {year}", "value": len(in_year), "display": str(len(in_year)),
                  "sub": f"{_plural(n9, 'nine')} \u00b7 {_plural(n18, 'eighteen')}"})
    past = [e for e in events if e.get("event_type") == "lesson" and not e.get("is_planned") and e.get("date")
            and e["date"][:10] <= today.isoformat()]
    if past:
        last_lesson = past[-1]["date"][:10]
        days = (today - date.fromisoformat(last_lesson)).days
        since = sum(f["date"] > last_lesson for f in sf)
        tiles.append({"id": "lesson", "label": "Since last lesson", "value": days,
                      "display": f"{days} day{'s' if days != 1 else ''}",
                      "sub": f"{_plural(since, 'round')} since \u00b7 {_md(last_lesson)}"})
    elif not public:        # a first-screen slot that says how to fill it, not a dash (public: no tile)
        tiles.append({"id": "lesson", "label": "Lessons", "value": None, "display": "Log one",
                      "sub": "Inbox > Quick entry (no API key needed), or golf add \"...\" --type lesson",
                      "action": True})
    exp = quality["export"]
    age = exp["age_days"]
    if public:              # visitors care how current the golf is, not about this Mac's export file
        tiles.append({"id": "through", "label": "Rounds through", "value": sf[-1]["date"] if sf else None,
                      "display": _md(sf[-1]["date"]) if sf else "\u2014",
                      "sub": _plural(len(sf), "round") + (f" \u00b7 18Birdies data as of {_md(exp['snapshot_date'])}"
                                                          if exp["snapshot_date"] else "")})
        return tiles
    if age is None:
        sub = "demo data only; no real export yet" if demo else f"none imported yet \u00b7 {DOWNLOAD_HINT}"
    elif exp["future"]:
        sub = f"snapshot dated {_md(exp['snapshot_date'])}, in the future"
    else:
        sub = (f"time for a fresh one \u00b7 {DOWNLOAD_HINT}" if age > STALE_EXPORT_DAYS
               else f"up to date \u00b7 snapshot {_md(exp['snapshot_date'])}")
    tiles.append({"id": "export", "label": "18Birdies export age", "value": age,
                  "display": "none" if age is None else f"{age} day{'s' if age != 1 else ''}", "sub": sub,
                  "status": "warning" if age is None or age > STALE_EXPORT_DAYS or exp["future"] else None})
    return tiles


def _panels(facts: list[dict], summ: dict) -> list[dict]:
    out = []
    for key, label, unit, better, tier, group, wkey, skey, desc in PANELS:
        series = metric_series(facts, key, wkey)
        if not series:
            continue
        pooled = summ.get(skey)
        if pooled is None:
            headline, detail = "", ""
        elif unit == "pct":
            headline = f"{_pct(pooled['value'])} (95% CI {_pct(pooled['lo'])}\u2013{_pct(pooled['hi'])})"
            detail = f"{pooled['k']} of {pooled['n']} \u00b7 {_plural(pooled['rounds'], 'round')}"
        elif key == "gir_9":
            headline = f"{pooled['value']:.1f} (95% CI {pooled['lo']:.1f}\u2013{pooled['hi']:.1f})"
            detail = f"{pooled['k']} of {pooled['n']} greens \u00b7 {_plural(pooled['rounds'], 'round')}"
        else:
            val = pooled["value"]
            headline = (_signed(val) if key in SIGNED_KEYS else f"{val:.2f}" if key in TWO_DECIMALS
                        else f"{val:.1f}")
            detail = (f"{pooled['n']} holes \u00b7 {_plural(pooled['rounds'], 'round')}" if "n" in pooled
                      else _plural(pooled["rounds"], "round"))
        out.append({"key": key, "label": label, "unit": unit, "better": better, "tier": tier, "group": group,
                    "desc": desc, "headline": headline, "detail": detail,
                    "points": [{"id": p["round_id"], "date": p["date"], "v": p["value"], "ewma": p["ewma"],
                                "n": p["weight"], "full": not p["nine"]} for p in series]})
    return out


def _identity(facts: list[dict], who: str = "you") -> dict | None:
    """who: 'you' on Shane's own page; his name on the public one (visitors are not 'you')."""
    rows = [f for f in facts if f["to_green_9"] is not None and f["putts_over_9"] is not None]
    if not rows:
        return None
    w = [f["nines"] for f in rows]
    tg = weighted_mean([f["to_green_9"] for f in rows], w)
    po = weighted_mean([f["putts_over_9"] for f in rows], w)
    verb = "average" if who == "you" else "averages"
    return {"to_green": tg, "putts_over": po, "to_par": tg + po, "rounds": len(rows),
            "sentence": (f"Per 9 holes {who} {verb} {_signed(tg + po)} to par: {_signed(tg)} in strokes to reach "
                         f"the green beyond regulation and {_signed(po)} in putts relative to two per hole "
                         f"({_plural(len(rows), 'round')} with putts tracked on every hole).")}


def _verdict(trend: list[dict]) -> str:
    """The first-screen answer to 'am I getting better?', read off the trend's path rather than its end
    points: the first trend value is just round 1, and a single round says little."""
    pts = [t for t in trend if t.get("value") is not None]
    if len(pts) < 3:
        return ""
    now = pts[-1]
    band = (f" (80% noise band {_signed(now['lo'])} to {_signed(now['hi'])})"
            if now.get("lo") is not None and now.get("hi") is not None else "")
    parts = [f"Trend now {_signed(now['value'])} per 9 holes{band}."]
    cutoff = (date.fromisoformat(now["date"]) - timedelta(days=21)).isoformat()
    then = [t for t in pts if t["date"] <= cutoff]
    if then:
        t = then[-1]
        d = now["value"] - t["value"]
        change = "about the same" if round(d, 1) == 0 else f"{abs(d):.1f} {'better' if d < 0 else 'worse'} now"
        parts.append(f"Three weeks earlier ({_md(t['date'])}) it was {_signed(t['value'])}: {change}.")
    settled = pts[2:]                               # skip the first two levels (one or two rounds each)
    if settled:
        best = min(settled, key=lambda t: t["value"])
        if best is not now:
            parts.append(f"Its best stretch: {_signed(best['value'])} around {_md(best['date'])}.")
    return " ".join(parts)


def _hero(sf: list[dict], sigma: float | None) -> dict:
    pts = [f for f in sf if f["to_par_9"] is not None]
    missing = len(sf) - len(pts)
    trend = outcome_trend(pts, sigma=sigma)
    return {
        "verdict": _verdict(trend),
        "points": [{"id": f["round_id"], "date": f["date"], "y": f["to_par_9"], "holes": f["holes"],
                    "gross": f["gross"], "to_par": f["to_par"], "par": f["par_played"], "course": _where(f),
                    "halves": ({"front": f["halves"]["front"], "back": f["halves"]["back"]} if f["halves"] else None)}
                   for f in pts],
        "trend": [{"date": t["date"], "y": t["value"], "lo": t["lo"], "hi": t["hi"]} for t in trend],
        "missing": missing,
        "missing_note": (f"{_plural(missing, 'round')} without a par to compare against "
                         "not plotted (see the Rounds table)." if missing else ""),
        "sigma": sigma,
    }


def _diff(facts: list[dict], his: list[dict], public: bool) -> dict:
    """Ours = the differential that counts toward the unofficial HI (handicap_history: for a nine, its
    9-hole differential plus the expected score over the other nine), so the dots, the HI step line
    and 18Birdies' own number compare like for like. (The lesson analysis uses diff_equiv instead.)"""
    pts = [f for f in facts if f["differential"] is not None or f["diff_equiv"] is not None
           or f["round_handicap_18b"] is not None]
    n_ours = sum(f["differential"] is not None for f in pts)
    n_18b = sum(f["round_handicap_18b"] is not None for f in pts)
    note = ""
    if pts and not n_ours:
        note = ("No course ratings yet, so only 18Birdies' own per-round numbers are shown."
                + ("" if public else " Add your tees' ratings to courses.yaml (golf courses check) to see yours."))
    elif n_ours and not n_18b:
        note = "18Birdies' own per-round numbers appear here once your export carries them."
    return {
        "points": [{"id": f["round_id"], "date": f["date"], "holes": f["holes"], "ours": f["differential"],
                    "nine9": round(f["diff_equiv"] / 2, 1) if f["holes"] == 9 and f["diff_equiv"] is not None
                    else None, "rated": f["rated"], "kind": f["differential_kind"], "b18": f["round_handicap_18b"],
                    "course": _where(f), "to_par": f["to_par"]} for f in pts],
        "hi": [{"date": h["date"], "hi": h["hi"]} for h in his] if n_ours else [],
        "n_ours": n_ours, "n_18b": n_18b, "note": note,
    }


def _clubs(shots: list[dict], facts: list[dict]) -> dict | None:
    if not shots:
        return None
    return {"distances": club_distances(shots), "coverage": shot_coverage(shots, facts)}


def _event_title(e: dict) -> str:
    kind = e.get("event_type", "other")
    focus = ", ".join(_human(f["game_area"]) for f in e.get("focus_areas") or [] if f.get("game_area"))
    if kind == "lesson":
        return "Lesson" + (f" with {e['coach']}" if e.get("coach") else "") + (f": {focus.lower()}" if focus else "")
    if kind == "practice":
        return "Practice" + (f": {focus.lower()}" if focus else "")
    if kind in ("equipment_change", "fitting"):
        eq = (e.get("equipment") or [{}])[0] if e.get("equipment") else {}
        if isinstance(eq, dict) and eq:
            what = " ".join(x for x in (eq.get("brand"), eq.get("model")) if x)
            return f"{_human(eq.get('category') or 'equipment')} {eq.get('action') or 'changed'}" + \
                (f": {what}" if what else "")
        return "Fitting" if kind == "fitting" else "Equipment change"
    if kind == "injury":
        inj = (e.get("injury") or [{}])[0] if e.get("injury") else {}
        part = inj.get("body_part") if isinstance(inj, dict) else ""
        sev = inj.get("severity") if isinstance(inj, dict) else ""
        return "Injury" + (f": {part}" if part else "") + (f" ({sev})" if sev and sev != "unknown" else "")
    return _human(kind)


def _events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        d = str(e.get("date") or e.get("date_start") or "")[:10]
        if not d:
            continue
        out.append({"id": e["event_id"], "type": e.get("event_type"), "lane": LANES.get(e.get("event_type"), "other"),
                    "date": d, "start": str(e.get("date_start") or d)[:10], "end": str(e.get("date_end") or d)[:10],
                    "planned": bool(e.get("is_planned")), "precision": e.get("date_precision") or "",
                    "title": _event_title(e), "summary": e.get("summary") or "", "coach": e.get("coach") or "",
                    "focus": [_human(f["game_area"]) + (f": {f['detail']}" if f.get("detail") else "")
                              for f in e.get("focus_areas") or [] if f.get("game_area")]})
    return out


def _baseline(prior: Sequence[dict]) -> dict | None:
    if not prior:
        return None
    fw = [(f["fw_hit"], f["fw_chances"]) for f in prior if f["fw_chances"]]
    gi = [(f["gir_hit"], f["gir_chances"]) for f in prior if f["gir_chances"]]
    return {
        "n": len(prior),
        "to_par_9": weighted_mean([f["to_par_9"] for f in prior], [f["nines"] for f in prior]),
        "fir": sum(a for a, _ in fw) / sum(b for _, b in fw) if fw else None,
        "gir": sum(a for a, _ in gi) / sum(b for _, b in gi) if gi else None,
        "putts_9": weighted_mean([f["putts_9"] for f in prior], [f["nines"] for f in prior]),
    }


def _round_rows(sf: list[dict], view_of: dict[str, str]) -> list[dict]:
    rows = []
    for i, f in enumerate(sf):
        card = [] if f["entry_mode"] == "total_only" else [
            {"h": h["hole"], "par": h["par"], "si": h["si"], "s": h["strokes"], "p": h["putts"], "fw": h["fairway"],
             "g": h["gir"], "pen": h["penalties"]} for h in f["holes_detail"]]
        rows.append({
            "id": f["round_id"], "date": f["date"], "course": _where(f), "view": view_of.get(f["group"], "all"),
            "mapped": f["course_key"] is not None, "holes": f["holes"], "nine": f["nine"],
            "entry_mode": f["entry_mode"], "source": f["source"], "gross": f["gross"], "to_par": f["to_par"],
            "to_par_9": f["to_par_9"], "par": f["par_played"], "halves": f["halves"],
            "diff_equiv": f["diff_equiv"], "differential": f["differential"], "diff_kind": f["differential_kind"],
            "rated": f["rated"], "b18": f["round_handicap_18b"], "sg": f["sg_overall"],
            "sg_ttg": f["sg_tee_to_green"], "tee": f["tee_name"],
            "fir": [f["fw_hit"], f["fw_chances"]] if f["fw_chances"] else None,
            "gir": [f["gir_hit"], f["gir_chances"]] if f["gir_chances"] else None,
            "putts": f["putts"], "putts_recorded": f["putts_recorded"], "putts_partial": f["putts_partial"],
            "dbl": f["dbl_plus"], "tier": f["tier"], "flags": f["dq_flags"], "demo": f["is_demo"], "card": card,
            "baseline": _baseline([p for p in sf[max(0, i - 10): i] if p["to_par_9"] is not None]),
        })
    rows.reverse()
    return rows


def _quality(conn: sqlite3.Connection, cfg: Config, facts: list[dict], today: date, demo: bool,
             include_nine: bool, public: bool) -> dict:
    export = _export_status(conn, cfg, today)
    age = export["age_days"]
    unmapped = [{"club_id": None if public else r["club_id"],
                 "name": r["name"] or ("an unnamed club" if public else r["club_id"]) or "(no club id)",
                 "rounds": r["n"]}
                for r in conn.execute(
                    """SELECT v.club_id, c.name, COUNT(*) AS n
                       FROM v_rounds v LEFT JOIN clubs c ON c.club_id = v.club_id
                       WHERE v.excluded = 0 AND v.course_key_eff IS NULL GROUP BY v.club_id ORDER BY n DESC""")]
    unrated = sum(1 for f in facts if f["course_key"] and f["holes"] in (9, 18) and not f["rated"])
    no_history = sum(1 for f in facts if f["rated"] and not f["has_history"])
    flagged = [{"id": f["round_id"], "date": f["date"], "flags": [x for x in f["dq_flags"] if x != "putts_partial"]}
               for f in facts if any(x != "putts_partial" for x in f["dq_flags"])]
    partial = [{"id": f["round_id"], "date": f["date"], "putts": f["putts_recorded"], "holes": f["holes"]}
               for f in facts if f["putts_partial"]]
    excluded = conn.execute("SELECT COUNT(*) FROM v_rounds WHERE excluded = 1").fetchone()[0]
    abandoned = conn.execute("SELECT COUNT(*) FROM rounds WHERE entry_mode = 'abandoned'").fetchone()[0]
    pending_events = conn.execute(
        """SELECT COUNT(*) FROM events e LEFT JOIN event_reviews r ON r.event_id = e.event_id
           WHERE e.status = 'pending' AND r.event_id IS NULL""").fetchone()[0]
    pending_extractions = conn.execute(
        "SELECT COUNT(*) FROM extractions WHERE status IN ('extracted', 'needs_review')").fetchone()[0]
    tiers = {t: sum(bool(f["tiers"][t]) for f in facts) for t in ("A", "B", "C")}

    items: list[dict] = []

    def add(level: str, text: str, hint: str = "") -> None:
        items.append({"level": level, "text": text + ("" if public or not hint else " " + hint)})

    if demo:
        add("warning", "Demo data is loaded: every row with an id starting 'demo-' is synthetic.",
            "Clear it (golf demo clear) before importing your real rounds.")
    if age is None:
        add("warning", "No 18Birdies export imported yet.")
    elif export["future"]:
        add("warning", f"The newest 18Birdies export is dated {export['snapshot_date']}, which is in the future; "
                       "its age is shown as 0 days.", "Check the date in the file name.")
    elif age > STALE_EXPORT_DAYS:
        add("warning", f"The last 18Birdies export is {age} days old; rounds since then are missing here.",
            f"To update, {DOWNLOAD_HINT}.")
    else:
        add("ok", f"The last 18Birdies export is {age} day{'s' if age != 1 else ''} old.")
    for u in unmapped:
        add("warning", f"{u['name']}: {_plural(u['rounds'], 'round')} not mapped to a course, "
                       "so no par, stroke index or rating.", "Run golf courses check.")
    if unrated:
        add("info", f"{_plural(unrated, 'round')} on courses without a rating: to-par only.")
    if no_history:
        add("info", f"{no_history} rated round{'s have' if no_history != 1 else ' has'} no handicap record yet.",
            "Run golf recompute.")
    if partial:
        add("info", f"{_plural(len(partial), 'round')} with putts entered on only some holes (fewer putts than "
                    "holes, or 18Birdies shows holes without putts): shown as partial and left out of every "
                    "putting stat.",
            "If a round did have putts on every hole: golf round putts <date> full.")
    if flagged:
        add("warning", f"{len(flagged)} round{'s carry' if len(flagged) != 1 else ' carries'} data-quality flags "
                       "(per-hole numbers not used).")
    if pending_events or pending_extractions:
        add("info", f"Waiting for review: {_plural(pending_events, 'timeline event')} and "
                    f"{_plural(pending_extractions, 'screenshot extraction')}.")
    if excluded:
        add("info", f"{_plural(excluded, 'round')} excluded by you.")
    if abandoned:
        add("info", f"{_plural(abandoned, 'abandoned round')} ignored.")
    add("info", "The unofficial Handicap Index " + ("includes 9-hole rounds (an unofficial approximation)."
                                                    if include_nine else "uses 18-hole rounds only."))
    if facts and not public:
        add("info", f"Per-hole putts, fairways and greens come from 18Birdies screenshots (Inbox): "
                    f"{_plural(tiers['C'], 'round')} so far. Hole-by-hole scores are on {tiers['B']} of "
                    f"{len(facts)} rounds.")
    export_out = {k: export[k] for k in ("snapshot_date", "age_days", "future", "source")}
    if not public:
        export_out["imported_at"] = export["imported_at"]
    return {"demo": demo, "export": export_out, "unmapped_clubs": unmapped,
            "unrated_rounds": unrated, "no_history": no_history, "flagged_rounds": flagged, "partial_putts": partial,
            "excluded_rounds": excluded, "abandoned_rounds": abandoned, "pending_events": pending_events,
            "pending_extractions": pending_extractions, "tiers": tiers, "include_nine": include_nine,
            "items": items}


def _course_views(facts: list[dict]) -> tuple[list[dict], dict[str, str], dict[str, set[str]]]:
    """Filter options: all courses, each course with at least MIN_COURSE_ROUNDS rounds (at most
    MAX_COURSES), and 'Other courses' for the rest. Returns (options, group -> view key, view -> groups)."""
    counts = Counter(f["group"] for f in facts)
    labels = {f["group"]: _where(f) for f in facts}
    listed = [g for g, n in counts.most_common() if n >= MIN_COURSE_ROUNDS][:MAX_COURSES]
    options = [{"key": "all", "label": "All courses", "n": len(facts)}]
    if len(counts) < 2 or not listed:
        return options, {}, {}
    view_of: dict[str, str] = {}
    members: dict[str, set[str]] = {}
    for i, g in enumerate(listed, start=1):
        key = f"c{i}"
        view_of[g] = key
        members[key] = {g}
        options.append({"key": key, "label": labels[g], "n": counts[g]})
    rest = {g for g in counts if g not in view_of}
    if rest:
        for g in rest:
            view_of[g] = "other"
        members["other"] = rest
        options.append({"key": "other", "label": "Other courses", "n": sum(counts[g] for g in rest)})
    return options, view_of, members


def _view(facts: list[dict], lesson_metric: str, events: list[dict], shots: list[dict], his: list[dict],
          n_boot: int, public: bool, who: str = "you") -> dict:
    """Everything the course filter re-scopes, for one slice of rounds."""
    sf = set_outcome([dict(f) for f in facts], "to_par_9")
    lf = set_outcome([dict(f) for f in facts], lesson_metric)
    ids = {f["round_id"] for f in facts}
    return {
        "n": len(facts),
        "hero": _hero(sf, outcome_sigma(sf)["sigma"]),
        "diff": _diff(sf, his, public),
        "lessons": lesson_report(lf, events, n_boot=n_boot),
        "multiples": _panels(sf, summary_stats(sf)),
        "identity": _identity(sf, who),
        "clubs": _clubs([s for s in shots if s["round_id"] in ids], sf),
        "holes": hole_averages(sf),
    }


# ----------------------------------------------------------------------------- public API
def dashboard_data(conn: sqlite3.Connection, cfg: Config, *, public: bool = False, today: date | None = None,
                   n_boot: int = 4000) -> dict:
    """Everything the dashboard shows, as a JSON-serialisable dict (works on an empty database).

    public=True drops everything that is about this machine rather than the golf (see module docstring).
    """
    today = today or _today(cfg)
    facts = round_facts(conn)
    events = load_timeline(conn)
    lesson_metric = lesson_scale(facts)
    his = hi_series(conn)
    shots = load_shots(conn)
    demo = has_demo(conn)
    include_nine = _include_nine(conn)
    quality = _quality(conn, cfg, facts, today, demo, include_nine, public)
    options, view_of, members = _course_views(facts)
    who = (cfg.player_name or "the player") if public else "you"
    whole = _view(facts, lesson_metric, events, shots, his, n_boot, public, who)
    views = {key: _view([f for f in facts if f["group"] in groups], lesson_metric, events, shots, his, n_boot,
                        public, who) for key, groups in members.items()}
    home = next((o for o in options if o["key"] == "c1"), None)
    home_trend = views["c1"]["hero"]["trend"] if home else []
    if home and home_trend and whole["hero"]["verdict"] and home["n"] < len(facts):
        whole["hero"]["verdict"] += f" At {home['label']} ({_plural(home['n'], 'round')}) it is " \
                                    f"{_signed(home_trend[-1]['y'])}."
    sf = set_outcome([dict(f) for f in facts], "to_par_9")
    summ = summary_stats(set_outcome([dict(f) for f in facts], lesson_metric))
    data = {
        "schema": 2,
        "generated_at": now_iso(),
        "today": today.isoformat(),
        "player": cfg.player_name,
        "public": public,
        "is_demo": demo,
        "empty": not facts,
        "metric": METRICS[lesson_metric],   # the lesson comparisons' scale
        "score": SCORE,                     # the primary score chart's scale
        "kpis": _kpis(sf, his, events, quality, today, include_nine, demo, public),
        "data_through": sf[-1]["date"] if sf else None,
        "courses": options,
        **whole,                            # hero, diff, lessons, multiples, identity, clubs: all courses
        "views": views,                     # the same keys for each filtered slice ("c1".., "other")
        "events": _events(events),
        "spans": {"practice": practice_blocks(events), "injury": injury_spans(events)},
        "rounds": _round_rows(sf, view_of),
        "quality": quality,
        "summary": {k: v for k, v in summ.items() if k in ("outcome", "sigma", "tiers", "putts_partial")},
    }
    if public:
        data = _pseudonymize(data, {f["round_id"]: f"r{i}" for i, f in enumerate(facts, start=1)})
    return _clean(data)


def render_dashboard(data: dict) -> str:
    """Inline the data into template.html.

    The JSON sits in a <script type="application/json"> block; '<', '>', '&' and '/' are escaped so no
    string in the data (a note summary, say) can close the tag or appear as a raw URL in the file.
    """
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    blob = (blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("/", "\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise RuntimeError("dashboard template must contain the data placeholder exactly once")
    return template.replace(PLACEHOLDER, blob)


def build(conn: sqlite3.Connection, cfg: Config, out_path: Path | str | None = None, *, public: bool = False,
          today: date | None = None, caddie_link: str | None = None) -> Path:
    """Render the dashboard to out_path and return the path.

    Default path: <data_dir>/site/index.html, or <data_dir>/site/public/index.html for public=True, so the
    shareable build never overwrites the private one. caddie_link: a relative link to the Caddie page
    ("caddie/"), shown as "Ask the Caddie" in the header (golf.publish.build_site sets it once the Caddie's
    relay is configured).
    """
    default = cfg.site_dir / "public" / "index.html" if public else cfg.site_dir / "index.html"
    out = Path(out_path) if out_path else default
    out.parent.mkdir(parents=True, exist_ok=True)
    data = dashboard_data(conn, cfg, public=public, today=today)
    if caddie_link:
        if "://" in caddie_link or caddie_link.startswith(("/", "\\")) or ".." in caddie_link:
            raise ValueError("caddie_link must be a relative link inside the site, like 'caddie/'")
        data["caddie_url"] = caddie_link
    out.write_text(render_dashboard(data), encoding="utf-8")
    return out
