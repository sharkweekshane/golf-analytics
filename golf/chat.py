"""Data for the Golf chat page on claude.ai, where Claude answers questions about Shane's golf.

The page asks Claude through the claude.ai `sample` capability, so questions run on Shane's own
Claude plan (no API key). It reads `golf-data.json`, published next to it: the headline facts Claude
always sees, plus small tables (rounds, holes, shots, events, handicap, course cards) that the page's
query tool filters and aggregates when a question needs detail.

Same privacy rules as the public dashboard: round ids are the public aliases r1..rN (18Birdies' own
ids are time-stamped UUIDs), and there are no file paths, GPS coordinates or API cost. `write_bundle`
runs the public-site privacy scan on the output and refuses to leave a file that fails it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from .analytics.stats import hi_series, load_shots, load_timeline, round_facts, shot_validity, tag_logging
from .config import Config
from .dashboard.build import dashboard_data
from .db import now_iso

SCHEMA = 1
DATA_FILE = "golf-data.json"

# What each table's columns mean. The page puts this in front of Claude with every question.
DICTIONARY: dict[str, dict[str, str]] = {
    "rounds": {
        "round": "round alias (r1 = oldest)", "date": "date played (YYYY-MM-DD, local)", "course": "course name",
        "holes": "9 or 18", "nine": "front/back for 9-hole rounds", "gross": "strokes", "par": "par of the holes played",
        "to_par": "gross minus par", "to_par_per9": "to_par scaled to 9 holes (18-hole rounds halved)",
        "differential": "unofficial WHS score differential, 18-hole equivalent (lower is better)",
        "differential_kind": "how it was computed", "hi_after": "unofficial Handicap Index after this round",
        "b18_handicap": "18Birdies' own per-round handicap figure, for comparison",
        "putts": "total putts (null when putts were only partly tracked)", "putts_partial": "true = putts not tracked on every hole",
        "fairways_hit": "fairways hit", "fairway_chances": "par-4/5 tee shots tracked",
        "gir": "greens in regulation", "gir_chances": "holes with GIR tracked",
        "birdies": "birdies or better", "pars": "pars", "bogeys": "bogeys", "double_plus": "double bogey or worse",
        "triple_plus": "triple bogey or worse", "tee": "tee played",
    },
    "holes": {
        "round": "round alias", "date": "date", "course": "course", "hole": "hole number", "par": "par",
        "si": "stroke index (1 = hardest)", "strokes": "strokes", "to_par": "strokes minus par",
        "putts": "putts (null = not tracked)", "fairway": "hit/left/right/short/long/not_applicable (null = not tracked)",
        "gir": "green in regulation (null = not tracked)", "penalties": "penalty strokes (null = not tracked)",
    },
    "shots": {
        "round": "round alias", "date": "date", "course": "course", "hole": "hole", "seq": "shot order in the round",
        "club": "club (Driver, 3W, 5H, 6i..9i, PW, SW, Putter)",
        "yards": "GPS distance to where the next shot started (includes roll; not carry)",
        "logged": "live = tracked during play; after = tapped on the map after the round (less accurate)",
        "invalid": "why the distance is unusable (null = usable)",
    },
    "events": {
        "date": "event date", "precision": "exact/day/week/month/inferred", "type": "lesson, practice, equipment_change, ...",
        "coach": "coach", "focus": "what it worked on", "drills": "drills", "swing_thoughts": "swing thoughts",
        "equipment": "equipment changes", "summary": "one-line summary", "excerpt": "the note's own words",
    },
    "handicap": {
        "round": "round alias", "date": "date", "hi": "unofficial Handicap Index after that round",
        "low_hi": "lowest index of the past year", "n_scores": "scores counted",
    },
    "courses": {"course": "course", "tee": "tee name", "hole": "hole", "par": "par", "si": "stroke index", "yards": "yards"},
}

NOTES = [
    "Scores are compared per 9 holes because most rounds are 9 holes; to_par_per9 puts 9- and 18-hole rounds on one scale.",
    "The Handicap Index is computed by this app under the 2024 WHS rules and is unofficial (no GHIN, PCC = 0).",
    "18Birdies doubles a 9-hole score; WHS adds an expected score for the other nine, so b18_handicap differs from ours.",
    "Rounds with putts_partial = true did not track every putt: leave them out of putting questions.",
    "Most club distances were tapped on the map after the round (logged = after); live-tracked shots are more reliable.",
]


def _r(x: Any, nd: int = 1) -> Any:
    return round(x, nd) if isinstance(x, float) else x


def chat_bundle(conn: sqlite3.Connection, cfg: Config, *, today: date | None = None) -> dict[str, Any]:
    """Everything the chat page needs, JSON-serialisable and public-safe."""
    dash = dashboard_data(conn, cfg, public=True, today=today, n_boot=1000)
    facts = round_facts(conn)
    alias = {f["round_id"]: f"r{i}" for i, f in enumerate(facts, start=1)}   # the public dashboard's aliases
    course_of = {f["round_id"]: f.get("course_name") or f.get("club_name") for f in facts}

    rounds, holes = [], []
    for f in facts:
        rid = alias[f["round_id"]]
        partial = bool(f.get("putts_partial"))
        rounds.append({
            "round": rid, "date": f["date"], "course": course_of[f["round_id"]], "holes": f["holes"],
            "nine": f.get("nine"), "gross": f.get("gross"), "par": f.get("par_played"), "to_par": f.get("to_par"),
            "to_par_per9": _r(f.get("to_par_9")), "differential": _r(f.get("differential")),
            "differential_kind": f.get("differential_kind"), "hi_after": _r(f.get("hi_after")),
            "b18_handicap": _r(f.get("round_handicap_18b")),
            "putts": None if partial else f.get("putts"), "putts_partial": partial,
            "fairways_hit": f.get("fw_hit"), "fairway_chances": f.get("fw_chances"),
            "gir": f.get("gir_hit"), "gir_chances": f.get("gir_chances"),
            "birdies": f.get("birdies"), "pars": f.get("pars"), "bogeys": f.get("bogeys"),
            "double_plus": f.get("dbl_plus"), "triple_plus": f.get("triple_plus"), "tee": f.get("tee_name"),
        })
        for h in f.get("holes_detail") or []:
            strokes, par = h.get("strokes"), h.get("par")
            gir = h.get("gir")
            holes.append({
                "round": rid, "date": f["date"], "course": course_of[f["round_id"]], "hole": h.get("hole"),
                "par": par, "si": h.get("si"), "strokes": strokes,
                "to_par": strokes - par if strokes is not None and par is not None else None,
                "putts": None if partial else h.get("putts"), "fairway": h.get("fairway"),
                "gir": None if gir is None else bool(gir), "penalties": h.get("penalties"),
            })

    shots = [{
        "round": alias.get(s["round_id"], "?"), "date": s.get("date"), "course": course_of.get(s["round_id"]),
        "hole": s.get("hole"), "seq": s.get("seq"), "club": s.get("club"), "yards": _r(s.get("distance_yards")),
        "logged": s.get("logged"), "invalid": shot_validity(s.get("distance_yards")),
    } for s in tag_logging(load_shots(conn)) if s["round_id"] in alias]

    events = [{
        "date": e.get("date") or e.get("date_start"), "precision": e.get("date_precision"), "type": e.get("event_type"),
        "coach": e.get("coach") or None,
        "focus": [": ".join(x for x in (fa.get("game_area"), fa.get("detail")) if x) for fa in e.get("focus_areas") or []],
        "drills": [d.get("name") or d.get("description") for d in e.get("drills") or [] if isinstance(d, dict)],
        "swing_thoughts": e.get("swing_thoughts") or [],
        "equipment": [" ".join(str(x) for x in (q.get("action"), q.get("brand"), q.get("model"), q.get("category")) if x)
                      for q in e.get("equipment") or [] if isinstance(q, dict)],
        "summary": e.get("summary"), "excerpt": e.get("source_excerpt"),
    } for e in load_timeline(conn)]

    handicap = [{"round": alias.get(h["round_id"], "?"), "date": h["date"], "hi": _r(h.get("hi")),
                 "low_hi": _r(h.get("low_hi")), "n_scores": h.get("n_scores")} for h in hi_series(conn)]

    courses = [dict(r) for r in conn.execute(
        "SELECT c.name AS course, t.name AS tee, th.hole, th.par, th.si, th.yards FROM tee_holes th "
        "JOIN tees t ON t.tee_id = th.tee_id JOIN courses c ON c.course_key = t.course_key "
        "WHERE t.tee_id IN (SELECT DISTINCT tee_id_eff FROM v_rounds WHERE tee_id_eff IS NOT NULL) "
        "   OR t.is_default = 1 ORDER BY c.name, t.name, th.hole")]

    clubs = [{k: _r(v) for k, v in c.items() if k in ("club", "n", "median", "q1", "q3", "n_live", "median_live", "n_after")}
             for c in ((dash.get("clubs") or {}).get("distances") or {}).get("clubs") or []]

    bundle = {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "today": dash["today"],
        "player": cfg.player_name,
        "data_through": dash.get("data_through"),
        "headline": {
            "averages": averages_sentence(rounds),
            "kpis": [{"label": k.get("label"), "value": k.get("display"), "note": k.get("sub")} for k in dash.get("kpis") or []],
            "trend": (dash.get("hero") or {}).get("verdict"),
            "scoring_split": (dash.get("identity") or {}).get("sentence"),
            "lesson_sensitivity": (dash.get("lessons") or {}).get("mde_sentence"),
            "club_medians": clubs,
        },
        "notes": NOTES,
        "dictionary": DICTIONARY,
        "tables": {"rounds": rounds, "holes": holes, "shots": shots, "events": events, "handicap": handicap,
                   "courses": courses},
    }
    bundle["content_sha"] = content_sha(bundle)
    return bundle


def averages_sentence(rounds: list[dict[str, Any]]) -> str | None:
    """The plain scoring averages, stated outright. Without this line a model reading the headline took the
    last-5 figure (or the putts-tracked subset's) for the all-rounds average."""
    vals = [r["to_par_per9"] for r in rounds if isinstance(r.get("to_par_per9"), (int, float))]
    if not vals:
        return None
    mean = lambda xs: sum(xs) / len(xs)
    parts = [f"Scoring average over all {len(vals)} rounds: {mean(vals):+.1f} to par per 9 holes",
             f"last 5 rounds {mean(vals[-5:]):+.1f}" if len(vals) >= 5 else None,
             f"best 9 {min(vals):+.0f}"]
    by_course: dict[str, list[float]] = {}
    for r in rounds:
        if isinstance(r.get("to_par_per9"), (int, float)):
            by_course.setdefault(r.get("course") or "?", []).append(r["to_par_per9"])
    if len(by_course) > 1:
        parts.append("by course: " + ", ".join(f"{c} {mean(v):+.1f} ({len(v)} round{'s' if len(v) != 1 else ''})"
                                                for c, v in sorted(by_course.items(), key=lambda kv: -len(kv[1]))))
    return "; ".join(p for p in parts if p) + "."


def content_sha(bundle: dict[str, Any]) -> str:
    """Fingerprint of what the page shows (not when it was built), so a refresh can skip an unchanged file."""
    stable = {k: v for k, v in bundle.items() if k not in ("generated_at", "today", "content_sha")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def write_bundle(conn: sqlite3.Connection, cfg: Config, out_dir: Path | str | None = None) -> Path:
    """Write golf-data.json into out_dir (default data/site/chat/) after the public privacy scan."""
    from .privacy import site_findings

    out = Path(out_dir) if out_dir else cfg.site_dir / "chat"
    out.mkdir(parents=True, exist_ok=True)
    path = out / DATA_FILE
    path.write_text(json.dumps(chat_bundle(conn, cfg), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    findings = [f for f in site_findings(out, db_path=cfg.db_path, root=cfg.root, data_dir=cfg.data_dir)
                if f.path == DATA_FILE or f.path == "golf.db"]
    if findings:
        path.unlink(missing_ok=True)
        raise RuntimeError("chat data failed the privacy check, nothing written:\n" + "\n".join(map(str, findings)))
    return path
