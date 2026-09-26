"""Synthetic demo data, so the dashboard and web app can be shown before Shane's real data exists.

Every row is labelled: ids start with 'demo-', names say "(synthetic)", and meta holds demo=1.
clear_demo() removes exactly those rows.

The shape follows Shane's real golf: mostly 9-hole rounds at one home nine, a few 18-hole rounds away,
GPS shot tracking (club + distance) on some rounds with a few holes missed and a few impossible
distances, 18Birdies' own per-round handicap on every export round, one round with putts entered on
only some holes, and a high-handicap player whose index is still settling. GPS coordinates are made
up around a point in the mid-Atlantic (0.5 N, 30 W), far from any course.

Handicap rows use a simplified inline WHS (net double bogey, lowest-k-of-20 table, 9-hole rounds
included via 0.52*HI + 1.2) so this module has no dependency on golf.whs; `golf recompute` will
overwrite them with the real engine's numbers.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from .db import dumps, now_iso

DEMO_PREFIX = "demo-"
SPAN_DAYS = 238
COACH = "Coach Rivera (demo)"

# Home: a 9-hole course (like Shane's), played as the front nine every time.
HOME_CLUB_ID = "demo-club-brook"
HOME_KEY = "demo-brook"
HOME_TEE = "demo-brook:White:M"
HOME_PARS = [4, 3, 4, 5, 4, 3, 4, 4, 4]
HOME_SIS = [5, 15, 1, 9, 3, 17, 7, 11, 13]
HOME_YARDS = [338, 152, 384, 468, 362, 141, 327, 349, 305]
HOME_CR9, HOME_SLOPE9 = 32.6, 108

# Away: an 18-hole course with front/back ratings.
CLUB_ID = "demo-club-pines"
COURSE_KEY = "demo-pines"
TEE_ID = "demo-pines:White:M"
PARS = [4, 5, 4, 3, 4, 4, 3, 5, 4, 4, 4, 3, 5, 4, 4, 3, 4, 5]
SIS = [7, 3, 11, 17, 1, 9, 15, 5, 13, 8, 4, 16, 2, 12, 6, 18, 14, 10]
YARDS = [372, 498, 351, 158, 405, 338, 171, 512, 364, 356, 389, 142, 521, 344, 378, 187, 330, 488]
CR18, SLOPE18 = 70.6, 126
CR_F9, SLOPE_F9, CR_B9, SLOPE_B9 = 35.2, 125, 35.4, 127

# Unmapped: a municipal 18 with no courses.yaml entry (18Birdies still has its own rating).
MUNI_CLUB_ID = "demo-club-muni"
MUNI_PARS = [4, 4, 3, 4, 5, 4, 3, 4, 4, 4, 3, 4, 4, 5, 3, 4, 4, 4]
MUNI_YARDS = [352, 371, 162, 340, 480, 366, 150, 330, 345, 361, 170, 322, 355, 470, 148, 338, 350, 344]
MUNI_CR, MUNI_SLOPE = 68.9, 118

# (fraction of the span, focus area, detail, summary)
LESSONS = [
    (0.24, "full_swing", "shallower downswing, stop early extension",
     "Lesson with Coach Rivera on the full swing: shallower downswing."),
    (0.52, "putting", "start-line gate drill and tempo",
     "Putting lesson: start-line gate drill and a slower stroke tempo."),
    (0.80, "driving", "tee it higher, swing out to right field",
     "Driving lesson: higher tee and an in-to-out swing path."),
]
INJURY_SPAN = (0.61, 0.68)

# Round types by position in the season. Early 18-hole rounds establish the index; the rest is mostly
# nines at home, with every other path the dashboard has (unmapped, total-only, sum mismatch,
# screenshot-only, partial putts, a nine on the 18-hole course) represented once.
SPECIAL_KINDS = [(0.0, "pines18"), (0.05, "pines18"), (0.1, "pines18"), (0.2, "unmapped"), (0.3, "pines18"),
                 (0.36, "partial_putts"), (0.45, "total_only"), (0.55, "pines9"), (0.62, "pines18"),
                 (0.7, "mismatch"), (0.93, "screenshot")]

# Club distance model for a high-handicap player (yards incl. roll: mean, sd).
CLUBS: dict[str, tuple[str, str, int, float, float]] = {
    # label: (type, number, loft, mean, sd)
    "Driver": ("WOOD", "1", 0, 192, 32), "3W": ("WOOD", "3", 0, 168, 28), "5H": ("HYBRID", "5", 0, 152, 26),
    "6i": ("IRON", "6", 0, 138, 22), "7i": ("IRON", "7", 0, 126, 20), "8i": ("IRON", "8", 0, 114, 18),
    "9i": ("IRON", "9", 0, 101, 17), "PW": ("WEDGE", "P", 46, 84, 14), "SW": ("WEDGE", "S", 56, 52, 14),
}
DEMO_ORIGIN = (0.5, -30.0)          # synthetic GPS origin in the mid-Atlantic
YARDS_PER_DEG_LAT = 111_320 / 0.9144


def _round1(x: float) -> float:
    """WHS rounding: to 0.1, halves upward (so -1.55 -> -1.5)."""
    return math.floor(round(x * 10, 6) + 0.5) / 10


def _hi_from(diffs: list[float]) -> float | None:
    """Lowest-k of the most recent 20 with the WHS fewer-than-20 adjustments."""
    recent = diffs[-20:]
    n = len(recent)
    if n < 3:
        return None
    table = {3: (1, -2.0), 4: (1, -1.0), 5: (1, 0.0), 6: (2, -1.0), 7: (2, 0.0), 8: (2, 0.0)}
    k, adj = table.get(n, (3 if n <= 11 else 4 if n <= 14 else 5 if n <= 16 else 6 if n <= 18 else 7 if n == 19
                           else 8, 0.0))
    return min(54.0, _round1(sum(sorted(recent)[:k]) / k + adj))


def _strokes_received(ch: int, sis: list[int], n_holes: int) -> list[int]:
    """Handicap strokes per hole; on a nine, by ascending 18-hole SI among the holes played."""
    if ch <= 0:
        return [0] * len(sis)
    rank = {si: r + 1 for r, si in enumerate(sorted(sis))}
    return [ch // n_holes + (rank[si] <= ch % n_holes) for si in sis]


def _level(day: int, lesson_days: list[int], injury: tuple[int, int], driver_day: int) -> float:
    """Synthetic 'true' 18-hole differential: a high handicap drifting down, steps after lessons, dips
    while learning or injured."""
    lvl = 34.0 - 0.02 * day
    steps = [2.4, 1.6, 0.6]
    for ld, step in zip(lesson_days, steps):
        if day >= ld + 14:
            lvl -= step
        elif day >= ld:
            lvl += 1.0
    if injury[0] <= day <= injury[1]:
        lvl += 2.5
    if day >= driver_day:
        lvl -= 0.5
    return lvl


def _play(rng: np.random.Generator, pars: list[int], sis: list[int], target_over: float, *, fw_hit_p: float,
          putt_3_p: float) -> list[dict]:
    """Hole-by-hole play: fairway -> strokes to reach the green -> putts, so the stats hang together."""
    n = len(pars)
    per_hole = target_over / n
    holes = []
    for par, si in zip(pars, sis):
        difficulty = (9.5 - min(si, 18)) / 8.5 * 0.35
        fairway = "not_applicable"
        e_mean = per_hole + difficulty - 0.25
        penalties = 0
        if par >= 4:
            if rng.random() < fw_hit_p:
                fairway, e_mean = "hit", e_mean - 0.25
            else:
                fairway = str(rng.choice(["left", "right", "short", "long"], p=[0.4, 0.45, 0.1, 0.05]))
                e_mean += 0.3
                if rng.random() < 0.16:
                    penalties = 1
        e = max(3 - par, int(round(rng.normal(e_mean, 0.9)))) + penalties
        gir = int(e <= 0)
        if gir:
            putts = int(rng.choice([1, 2, 3], p=[0.08, 0.92 - putt_3_p, putt_3_p]))
        else:
            putts = int(rng.choice([0, 1, 2, 3], p=[0.02, 0.22, 0.76 - 0.6 * putt_3_p, 0.6 * putt_3_p]))
        holes.append({
            "par": par, "si": si, "strokes": par - 2 + e + putts, "putts": putts, "fairway": fairway,
            "gir": gir, "gir_miss": None if gir else str(rng.choice(["left", "right", "short", "long"])),
            "penalties": penalties,
        })
    return holes


def _totals(holes: list[dict]) -> dict[str, Any]:
    over = [h["strokes"] - h["par"] for h in holes]
    fw = [h["fairway"] for h in holes if h["fairway"] != "not_applicable"]
    misses = [h["gir_miss"] for h in holes if not h["gir"]]
    return {
        "gross": sum(h["strokes"] for h in holes), "par_played": sum(h["par"] for h in holes),
        "fw_hit": fw.count("hit"), "fw_left": fw.count("left"), "fw_right": fw.count("right"),
        "fw_short": fw.count("short"), "fw_long": fw.count("long"), "fw_chances": len(fw),
        "gir": sum(h["gir"] for h in holes), "gir_left": misses.count("left"), "gir_right": misses.count("right"),
        "gir_short": misses.count("short"), "gir_long": misses.count("long"), "gir_chances": len(holes),
        "putts": sum(h["putts"] for h in holes),
        "eagles_plus": sum(o <= -2 for o in over), "birdies": over.count(-1), "pars": over.count(0),
        "bogeys": over.count(1), "dbl_plus": sum(o >= 2 for o in over),
    }


def _kinds(n: int) -> list[str]:
    """Round types by position: 'home9' unless a special kind claims the slot."""
    kinds = ["home9"] * n
    for frac, kind in SPECIAL_KINDS:
        i = min(n - 1, int(round(frac * (n - 1))))
        if n >= 6 and kinds[i] == "home9" and (i != n - 1 or kind == "screenshot"):
            kinds[i] = kind
    return kinds


def _layout(kind: str) -> dict[str, Any]:
    """Where a round of this kind is played: holes, pars, SIs, yards, rating for the holes played."""
    if kind == "pines9":
        return {"club": CLUB_ID, "course": COURSE_KEY, "pars": PARS[9:], "sis": SIS[9:], "yards": YARDS[9:],
                "numbers": list(range(10, 19)), "nine": "back", "cr": CR_B9, "slope": SLOPE_B9,
                "b18": (CR_B9, SLOPE_B9)}
    if kind in ("pines18", "total_only"):
        return {"club": CLUB_ID, "course": COURSE_KEY, "pars": PARS, "sis": SIS, "yards": YARDS,
                "numbers": list(range(1, 19)), "nine": None, "cr": CR18, "slope": SLOPE18, "b18": (CR18, SLOPE18)}
    if kind == "unmapped":
        return {"club": MUNI_CLUB_ID, "course": None, "pars": MUNI_PARS, "sis": list(range(1, 19)),
                "yards": MUNI_YARDS, "numbers": list(range(1, 19)), "nine": None, "cr": None, "slope": None,
                "b18": (MUNI_CR, MUNI_SLOPE)}
    return {"club": HOME_CLUB_ID, "course": HOME_KEY, "pars": HOME_PARS, "sis": HOME_SIS, "yards": HOME_YARDS,
            "numbers": list(range(1, 10)), "nine": "front", "cr": HOME_CR9, "slope": HOME_SLOPE9,
            "b18": (HOME_CR9, HOME_SLOPE9)}


def has_demo(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'demo'").fetchone()
    if row and row[0] == "1":
        return True
    return conn.execute("SELECT 1 FROM rounds WHERE round_id LIKE 'demo-%' LIMIT 1").fetchone() is not None


def seed_demo(conn: sqlite3.Connection, *, n_rounds: int = 24, seed: int = 7, end: date | None = None) -> dict:
    """Insert a labelled synthetic history ending at `end` (default today). Replaces earlier demo rows."""
    end = end or date.today()
    start = end - timedelta(days=SPAN_DAYS)
    rng = np.random.default_rng(seed)
    clear_demo(conn)
    now = now_iso()
    lesson_days = [int(f * SPAN_DAYS) for f, *_ in LESSONS]
    driver_day = lesson_days[2] + 6
    injury = (int(INJURY_SPAN[0] * SPAN_DAYS), int(INJURY_SPAN[1] * SPAN_DAYS))
    dstr = lambda d: (start + timedelta(days=int(d))).isoformat()  # noqa: E731
    n_shots = tracked = home_tracked = 0

    with conn:
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         [("demo", "1"), ("demo_seed", str(seed)), ("demo_end", end.isoformat())])
        cur = conn.execute(
            "INSERT INTO imports(kind, path, sha256, imported_at, summary) VALUES ('18b_export', ?, ?, ?, ?)",
            ("demo/18Birdies_archive_SYNTHETIC.json", f"demo-export-{seed}",
             f"{(end - timedelta(days=6)).isoformat()}T12:00:00+00:00",
             dumps({"demo": 1, "snapshot_date": (end - timedelta(days=6)).isoformat()})))
        import_id = cur.lastrowid
        _demo_courses(conn)

        kinds = _kinds(n_rounds)
        grid = np.linspace(2, SPAN_DAYS - 2, n_rounds) if n_rounds > 1 else np.array([SPAN_DAYS - 2.0])
        days = sorted({int(np.clip(round(g + rng.integers(-3, 4)), 0, SPAN_DAYS)) for g in grid})
        while len(days) < n_rounds:
            days = sorted(set(days) | {int(rng.integers(0, SPAN_DAYS + 1))})
        rated: list[dict] = []
        for i, (day, kind) in enumerate(zip(days, kinds)):
            lay = _layout(kind)
            n_holes = len(lay["pars"])
            lvl = _level(day, lesson_days, injury, driver_day) + rng.normal(0, 2.4)
            putt_3_p = 0.14 if day >= lesson_days[1] + 14 else 0.22
            fw_p = 0.42 if day >= driver_day else 0.33
            # Expected strokes over par for the holes played, from the 18-hole differential level.
            if n_holes == 9:
                cr, slope = lay["b18"]
                target = lvl / 2 * slope / 113 + cr - sum(lay["pars"])
            else:
                cr, slope = lay["b18"]
                target = lvl * slope / 113 + cr - sum(lay["pars"])
            holes = _play(rng, lay["pars"], lay["sis"], target, fw_hit_p=fw_p, putt_3_p=putt_3_p)
            t = _totals(holes)
            rid = f"demo-ss-{i:03d}" if kind == "screenshot" else f"demo-{i:03d}"
            source = "screenshot" if kind == "screenshot" else "18b_export"
            gross = t["gross"] + (2 if kind == "mismatch" else 0)
            b18 = None
            if source == "18b_export":            # 18Birdies' own number: from gross, its own rating, no caps
                bcr, bslope = lay["b18"]
                b18 = _round1((2 if n_holes == 9 else 1) * 113 / bslope * (gross - bcr))
            row = {
                "round_id": rid, "source": source, "played_at_utc": f"{dstr(day)}T{14 + i % 5}:10:00+00:00",
                "played_on_local": dstr(day), "club_id": lay["club"], "course_key": lay["course"], "tee_id": None,
                "entry_mode": "total_only" if kind == "total_only" else "hole_by_hole",
                "holes_played": n_holes, "nine": lay["nine"],
                "gross": gross, "to_par": gross - t["par_played"], "par_played": t["par_played"],
                "dq_flags": dumps(["sum_mismatch"] if kind == "mismatch" else
                                  ["putts_partial"] if kind == "partial_putts" else []),
                "raw": dumps({"demo": 1, "synthetic": True}),
                "first_import_id": None if kind == "screenshot" else import_id,
                "last_import_id": None if kind == "screenshot" else import_id,
                "round_handicap_18b": b18,
                "sg_overall": round(float(rng.normal(-14, 3)), 1) if i % 6 == 1 else None,
                "sg_tee_to_green": round(float(rng.normal(-9, 2)), 1) if i % 6 == 1 else None,
            }
            if kind != "total_only" and i % 11 != 5:      # a few rounds with stats not tracked
                row.update({k: t[k] for k in ("fw_hit", "fw_left", "fw_right", "fw_short", "fw_long", "fw_chances",
                                              "gir", "gir_left", "gir_right", "gir_short", "gir_long",
                                              "gir_chances", "putts")})
                row["putts_tracked"] = 1
            if kind == "partial_putts":                     # putts entered on a few holes only
                row["putts"], row["putts_tracked"] = sum(h["putts"] for h in holes[:3]) or 3, 0
            if kind != "total_only":
                row.update({k: t[k] for k in ("eagles_plus", "birdies", "pars", "bogeys", "dbl_plus")})
            track = kind in ("home9", "pines18", "partial_putts") and day >= 40 and rng.random() < 0.7
            if track:
                row["tee_name"] = "White"
            cols = list(row)
            conn.execute(f"INSERT INTO rounds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [row[c] for c in cols])

            stats = kind == "screenshot" or (kind not in ("total_only", "unmapped", "partial_putts")
                                             and rng.random() < 0.45)
            if kind != "total_only":
                for num, h in zip(lay["numbers"], holes):
                    conn.execute(
                        """INSERT INTO round_holes(round_id, hole, strokes, par, si, putts, fairway, gir, gir_miss,
                           penalties, strokes_src, stats_src) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rid, num, h["strokes"], h["par"] if stats else None, h["si"] if stats else None,
                         h["putts"] if stats else None, h["fairway"] if stats else None,
                         h["gir"] if stats else None, h["gir_miss"] if stats else None,
                         h["penalties"] if stats else None, source, "screenshot" if stats else None))
            if track:
                glitch = {1: "retap", 3: "jump", 6: "retap"}.get(tracked)
                after = kind == "home9" and home_tracked in (2, 5)
                n_shots += _demo_shots(conn, rng, rid, dstr(day), 14 + i % 5, lay, holes, glitch, after=after)
                tracked += 1
                home_tracked += kind == "home9"
            if kind != "unmapped":
                rated.append({"round_id": rid, "date": dstr(day), "kind": kind, "gross": gross, "holes": holes,
                              "numbers": lay["numbers"], "cr": lay["cr"], "slope": lay["slope"]})

        _demo_handicap(conn, rated)
        ss = next((r for r in rated if r["kind"] == "screenshot"), None)
        if ss:
            extracted, flags = _demo_extraction(ss)
            conn.execute(
                """INSERT INTO extractions(round_id, image_paths, image_sha256s, status, flags, result_json,
                   created_at) VALUES (?, ?, ?, 'needs_review', ?, ?, ?)""",
                (ss["round_id"], dumps(["demo/IMG_0001_synthetic.PNG"]), dumps(["demo"]), dumps(flags),
                 dumps(extracted), now))
        n_events = _demo_events(conn, start, lesson_days, driver_day, injury, now)
    return {"rounds": len(days), "events": n_events, "lessons": len(LESSONS), "shots": n_shots,
            "start": start.isoformat(), "end": end.isoformat(), "import_id": import_id}


def _demo_courses(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT INTO clubs(club_id, name, city, state) VALUES (?, ?, ?, ?)", [
        (HOME_CLUB_ID, "Demo Brook Nine (synthetic)", "Springfield", "MA"),
        (CLUB_ID, "Demo Pines Golf Club (synthetic)", "Springfield", "MA"),
        (MUNI_CLUB_ID, "Demo Municipal (synthetic, not mapped)", "Springfield", "MA"),
    ])
    conn.executemany("INSERT INTO courses(course_key, club_id, name, holes, par, source) VALUES (?,?,?,?,?,?)", [
        (HOME_KEY, HOME_CLUB_ID, "Demo Brook Nine (synthetic)", 9, sum(HOME_PARS), "demo"),
        (COURSE_KEY, CLUB_ID, "Demo Pines (synthetic)", 18, sum(PARS), "demo"),
    ])
    conn.execute(
        """INSERT INTO tees(tee_id, course_key, name, gender, par, yards, cr_f9, slope_f9, rating_source, is_default)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (HOME_TEE, HOME_KEY, "White", "M", sum(HOME_PARS), sum(HOME_YARDS), HOME_CR9, HOME_SLOPE9,
         "synthetic demo rating (9-hole course)"))
    conn.execute(
        """INSERT INTO tees(tee_id, course_key, name, gender, par, yards, cr18, slope18, cr_f9, slope_f9,
           cr_b9, slope_b9, rating_source, is_default) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (TEE_ID, COURSE_KEY, "White", "M", sum(PARS), sum(YARDS), CR18, SLOPE18, CR_F9, SLOPE_F9,
         CR_B9, SLOPE_B9, "synthetic demo rating"))
    conn.executemany("INSERT INTO tee_holes(tee_id, hole, par, si, yards) VALUES (?,?,?,?,?)",
                     [(HOME_TEE, i + 1, HOME_PARS[i], HOME_SIS[i], HOME_YARDS[i]) for i in range(9)]
                     + [(TEE_ID, i + 1, PARS[i], SIS[i], YARDS[i]) for i in range(18)])


def _pick_club(remaining: float) -> str:
    """The club a high handicapper takes for the yards left (approach and layup shots)."""
    for club, reach in (("3W", 185), ("5H", 160), ("6i", 145), ("7i", 132), ("8i", 120), ("9i", 105), ("PW", 70)):
        if remaining >= reach:
            return club
    return "SW"


def _demo_shots(conn: sqlite3.Connection, rng: np.random.Generator, rid: str, day: str, hour: int, lay: dict,
                holes: list[dict], glitch: str | None = None, after: bool = False) -> int:
    """GPS shots that add up to each hole's strokes: tee shot, approaches until the green, then putts.

    Distances are to where the next shot started (so roll and mishits are in them). One or two holes per
    round are left untracked. glitch adds one impossible reading so the dashboard's invalid-shot handling
    has something to show: 'retap' (a 2-yard wedge) or 'jump' (a 451-yard drive). after=True mimics a
    round entered after play, as Shane often does: every hole, shots 5 seconds apart, and the par-3 tee
    shot left at the app's default spot (the same 131.6 yards every time).
    """
    t0 = datetime.fromisoformat(f"{day}T{hour:02d}:10:00").replace(tzinfo=timezone.utc)
    skip = set(rng.choice(len(holes), size=int(rng.integers(0, 3)), replace=False).tolist())
    if after:
        skip = set()
    seq = 0
    rows = []
    for idx, (num, h, yards) in enumerate(zip(lay["numbers"], holes, lay["yards"])):
        if idx in skip:
            continue
        lat, lon = DEMO_ORIGIN[0] + num * 0.004, DEMO_ORIGIN[1]
        remaining = float(yards)
        full = max(1, h["strokes"] - h["putts"] - h["penalties"])
        for k in range(full + h["putts"]):
            putt = k >= full
            if putt:
                club = "Putter"
                dist = float(rng.uniform(0.5, 9.0)) if k < full + h["putts"] - 1 else float(rng.uniform(0.3, 1.5))
            else:
                last = k == full - 1
                club = ("Driver" if rng.random() < 0.8 else "5H") if (k == 0 and h["par"] >= 4) else \
                    ("7i" if remaining > 150 else "8i" if remaining > 125 else "9i") if (k == 0 and h["par"] == 3) \
                    else _pick_club(remaining)
                _, _, _, mean, sd = CLUBS[club]
                dist = max(6.0, float(rng.normal(mean, sd)))
                if rng.random() < 0.1:
                    dist = max(6.0, dist * 0.45)                   # a topped or chunked shot
                if last:
                    dist = max(6.0, remaining + float(rng.normal(0, 6)))
                dist = max(6.0, min(dist, remaining + 25)) + float(rng.uniform(-0.4, 0.4))   # no exact ties
                remaining = abs(remaining - dist)
            if after and k == 0 and h["par"] == 3:
                dist = 131.6
            if glitch == "jump" and club == "Driver":
                dist, glitch = 451.0, None
            elif glitch == "retap" and club in ("PW", "SW"):
                dist, glitch = 2.0, None
            ctype, cnum, loft = (("PUTTER", "Putter", 0) if putt else CLUBS[club][:3])
            seq += 1
            end_lat = lat + dist / YARDS_PER_DEG_LAT
            at = t0 + (timedelta(hours=3, seconds=5 * seq) if after else timedelta(minutes=12 * idx + 2 * k))
            rows.append((rid, seq, num, at.isoformat(timespec="seconds"),
                         ctype, cnum, club, loft or None, round(dist, 3), "White" if k == 0 else None,
                         round(lat, 7), round(lon, 7), round(end_lat, 7), round(lon, 7)))
            lat = end_lat
    conn.executemany(
        """INSERT INTO shots(round_id, seq, hole, shot_at_utc, club_type, club_number, club, loft, distance_yards,
           tee_name, start_lat, start_lon, end_lat, end_lon) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def _demo_extraction(r: dict) -> tuple[dict, list[dict]]:
    """A synthetic ExtractedRound for the demo screenshot round, plus one E and one W flag in the
    validator's format, so the review queue and review page have something real to render."""
    symbols = {-1: "circle", 0: "none", 1: "square"}
    holes = []
    for num, h in zip(r["numbers"], r["holes"]):
        diff = h["strokes"] - h["par"]
        holes.append({
            "hole": num, "par": h["par"], "par_alt": -1, "si": h["si"], "si_alt": -1, "yards": -1,
            "strokes": h["strokes"],
            "symbol": symbols.get(diff, "double_circle" if diff < -1 else "double_square"),
            "putts": h["putts"], "penalties": h["penalties"], "chips": -1, "sand": -1, "fairway": h["fairway"],
            "gir": "hit" if h["gir"] else "miss", "gir_miss": h["gir_miss"] or "none", "stats_visible": True,
            "confidence": "medium" if num == 7 else "high", "uncertain_fields": ["putts"] if num == 7 else [],
            "source_images": [1],
        })
    putts = sum(h["putts"] for h in r["holes"])
    extracted = {
        "images": [{"image_index": 1, "screen_type": "scorecard_stats", "orientation": "landscape",
                    "first_hole": r["numbers"][0], "last_hole": r["numbers"][-1], "problems": ""}],
        "player_name": "Demo Player", "other_players": [], "course_text": "Demo Brook Nine (synthetic)",
        "tee_text": "White", "date_text": r["date"], "date_iso": r["date"],
        "course_par": sum(h["par"] for h in r["holes"]), "rating_text": "",
        "holes": holes,
        "displayed": {"gross": sum(h["strokes"] for h in r["holes"]), "front": -1, "back": -1, "to_par_text": "",
                      "total_putts": putts - 1, "fairways_hit": -1, "gir": -1, "penalties": -1},
        "overall_confidence": "medium", "issues": [],
    }
    flags = [
        {"code": "RR", "severity": "E", "hole": 7, "field": "putts",
         "message": "Synthetic demo flag: a second reading of this cell disagreed with the first."},
        {"code": "W4", "severity": "W", "hole": None, "field": "putts",
         "message": f"Synthetic demo flag: per-hole putts add up to {putts}, the summary screen says {putts - 1}."},
    ]
    return extracted, flags


def _demo_handicap(conn: sqlite3.Connection, rated: list[dict]) -> None:
    """Chronological mini-WHS so handicap_history has plausible rows (golf recompute replaces them).

    9-hole rounds are included once an index exists (the expected-score method needs one), which is
    the app's default; before that they are recorded without a differential.
    """
    diffs: list[float] = []
    hi: float | None = None
    his: list[tuple[str, float]] = []
    for r in rated:
        nine = len(r["holes"]) == 9
        pars = [h["par"] for h in r["holes"]]
        cr, slope = r["cr"], r["slope"]
        if r["kind"] in ("total_only", "mismatch"):
            ags, kind = r["gross"], "gross_upper_bound"
        else:
            if hi is None:
                caps = [p + 5 for p in pars]
            else:
                ch = round((_round1(hi / 2) if nine else hi) * slope / 113 + (cr - sum(pars)))
                recv = _strokes_received(ch, [h["si"] for h in r["holes"]], len(pars))
                caps = [p + 2 + s for p, s in zip(pars, recv)]
            ags = sum(min(h["strokes"], c) for h, c in zip(r["holes"], caps))
            kind = "nine_hole_scaled" if nine else "ndb_adjusted"
        hi_before = hi
        if nine:
            diff = _round1(113 / slope * (ags - cr) + 0.52 * hi + 1.2) if hi is not None else None
        else:
            diff = _round1(113 / slope * (ags - cr))
        if diff is not None:
            diffs.append(diff)
            hi = _hi_from(diffs)
        if hi is not None:
            his.append((r["date"], hi))
        cutoff = (date.fromisoformat(r["date"]) - timedelta(days=365)).isoformat()
        low = min(v for d, v in his if d >= cutoff) if len(diffs) >= 20 and his else None
        conn.execute(
            """INSERT INTO handicap_history(round_id, as_of, n_scores, ags, differential, differential_kind,
               hi_before, hi_after, low_hi) VALUES (?,?,?,?,?,?,?,?,?)""",
            (r["round_id"], r["date"], len(diffs), ags, diff, kind, hi_before, hi, low))


def _demo_events(conn: sqlite3.Connection, start: date, lesson_days: list[int], driver_day: int,
                 injury: tuple[int, int], now: str) -> int:
    day = lambda d: (start + timedelta(days=int(d))).isoformat()  # noqa: E731
    docs = [
        ("demo-an-lessons", "apple_notes", "Lessons (synthetic demo)"),
        ("demo-an-practice", "apple_notes", "Practice log (synthetic demo)"),
        ("demo-an-gear", "apple_notes", "Gear and body (synthetic demo)"),
        ("demo-nb-page1", "notebook", "Notebook page 1 (synthetic demo)"),
    ]
    for doc_id, source, title in docs:
        conn.execute(
            """INSERT INTO source_docs(doc_id, source, external_id, title, folder, created_at, modified_at, text,
               content_hash) VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, source, doc_id, title, "Golf" if source == "apple_notes" else None, f"{day(0)}T09:00:00",
             f"{day(SPAN_DAYS)}T09:00:00", "Synthetic demo note. Not real data.", doc_id))

    events: list[dict] = []
    for n, ((_, area, detail, summary), ld) in enumerate(zip(LESSONS, lesson_days), start=1):
        events.append({
            "event_id": f"demo-ev-lesson-{n}", "doc_id": "demo-nb-page1" if n == 3 else "demo-an-lessons",
            "status": "pending" if n == 3 else "auto_accepted", "event_type": "lesson", "date": day(ld),
            "coach": COACH, "lesson_format": "in_person", "location": "Demo Brook range", "location_kind": "range",
            "duration_minutes": 60, "focus_areas": dumps([{"game_area": area, "detail": detail}]),
            "game_areas": area, "summary": summary, "source_excerpt": f"(synthetic) lesson {n}: {detail}",
        })
    drills = {1: "slow-motion rehearsals, 50 balls", 2: "gate drill, 3 x 10 from 6 ft",
              3: "tee height ladder, 40 balls"}
    for n, ld in enumerate(lesson_days, start=1):
        for k, offset in enumerate((3, 7, 12)):
            events.append({
                "event_id": f"demo-ev-practice-{n}-{k}", "doc_id": "demo-an-practice", "status": "auto_accepted",
                "event_type": "practice", "date": day(ld + offset), "location": "Demo Brook range",
                "location_kind": "range", "duration_minutes": 45 + 15 * k,
                "focus_areas": dumps([{"game_area": LESSONS[n - 1][1], "detail": "follow-up from lesson"}]),
                "game_areas": LESSONS[n - 1][1], "drills": dumps([{"name": drills[n], "description": "", "dose": ""}]),
                "summary": f"Range session after lesson {n}: {drills[n]}.",
            })
    events.append({
        "event_id": "demo-ev-practice-pending", "doc_id": "demo-nb-page1", "status": "pending",
        "event_type": "practice", "date": day(lesson_days[2] + 16), "summary": "Short-game session (awaiting review).",
        "focus_areas": dumps([{"game_area": "short_game", "detail": "up-and-down ladder"}]), "game_areas": "short_game",
    })
    events.append({
        "event_id": "demo-ev-driver", "doc_id": "demo-an-gear", "status": "auto_accepted",
        "event_type": "equipment_change", "date": day(driver_day),
        "summary": "New driver in the bag (synthetic model).",
        "equipment": dumps([{"action": "replaced", "category": "driver", "brand": "DemoBrand",
                             "model": "X1 (synthetic)", "specs": "10.5 degrees, stiff", "replaces": "old driver"}]),
        "game_areas": "equipment",
    })
    events.append({
        "event_id": "demo-ev-injury", "doc_id": "demo-an-gear", "status": "auto_accepted", "event_type": "injury",
        "date": day(injury[0]), "date_start": day(injury[0]), "date_end": day(injury[1]),
        "injury": dumps([{"body_part": "left wrist", "status": "new", "severity": "minor"}]),
        "summary": "Minor left wrist strain; eased off full swings for two weeks.", "game_areas": "physical",
    })
    for e in events:
        row = {"is_planned": 0, "date_precision": "day", "date_source": "explicit_text", "excerpt_verified": 1,
               "confidence": "high", "flags": "[]", "created_at": now, "updated_at": now, **e}
        cols = list(row)
        conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in cols])
    conn.execute("""INSERT INTO event_reviews(event_id, decision, comment, reviewed_at)
                    VALUES ('demo-ev-lesson-3', 'accepted', 'synthetic demo review', ?)""", (now,))
    return len(events)


def clear_demo(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every demo row (ids prefixed 'demo-') in foreign-key order; returns rows removed per table."""
    like = DEMO_PREFIX + "%"
    steps = [
        ("event_reviews", "event_id LIKE ?"),
        ("events", "event_id LIKE ? OR doc_id LIKE ?"),
        ("source_docs", "doc_id LIKE ?"),
        ("corrections", "entity_id LIKE ?"),
        ("extractions", "round_id LIKE ?"),
        ("handicap_history", "round_id LIKE ?"),
        ("shots", "round_id LIKE ?"),
        ("round_holes", "round_id LIKE ?"),
        ("round_overrides", "round_id LIKE ?"),
        ("rounds", "round_id LIKE ?"),
        ("tee_holes", "tee_id LIKE ?"),
        ("tees", "tee_id LIKE ?"),
        ("courses", "course_key LIKE ?"),
        ("clubs", "club_id LIKE ?"),
        ("imports", "sha256 LIKE ?"),
    ]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    removed: dict[str, int] = {}
    with conn:
        for table, where in steps:
            if table not in tables:          # e.g. shots on a database older than schema v2
                continue
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", [like] * where.count("?"))
            removed[table] = cur.rowcount
        conn.execute("DELETE FROM meta WHERE key IN ('demo', 'demo_seed', 'demo_end')")
    return removed
