"""Read-only MCP server over golf.db (DECISIONS §5): Claude Desktop / Claude Code can ask about rounds,
trends, the timeline and lesson comparisons, and can never change anything.

Read-only is enforced three times: the file is opened with SQLite's mode=ro; query_sql accepts one
SELECT/WITH statement only; and while it runs, an authorizer denies every write, PRAGMA and ATTACH.
Through query_sql the columns that can hold other people's names or whole note bodies read as NULL
(the timeline tool returns the relevant excerpts instead). Each call opens its own connection, so
tool calls on worker threads never share one.
"""
from __future__ import annotations

import math
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from golf.config import Config
from golf.db import connect, loads

MAX_ROWS = 500
HIDDEN_COLUMNS = {
    ("source_docs", "text"),              # whole note bodies (may name friends); excerpts come via timeline
    ("extractions", "result_json"),       # screenshots can list playing partners (other_players)
    ("llm_calls", "response_json"),       # same content as above, as the model returned it
    ("llm_calls", "request_meta"),
    ("shots", "start_lat"), ("shots", "start_lon"),     # GPS coordinates are local-only
    ("shots", "end_lat"), ("shots", "end_lon"),
}
TREND_METRICS = {
    "outcome": "outcome", "differential": "differential", "to_par": "to_par", "gross": "gross",
    "putts": "putts_18", "fir_pct": "fir_pct", "gir_pct": "gir_pct", "doubles": "dbl_18",
    "three_putt_pct": "three_putt_pct", "scramble_pct": "scramble_pct", "to_green": "to_green_18",
    "putts_over_two": "putts_over_18", "par3_avg": "par3_avg", "par4_avg": "par4_avg", "par5_avg": "par5_avg",
    "handicap_index": "hi",
}
INSTRUCTIONS = """\
Shane's personal golf data (golf.db), read-only. Rounds come from his 18Birdies export and screenshots;
events (lessons, practice, equipment, injuries) from his notes. Every Handicap Index here is UNOFFICIAL
(self-computed, playing conditions not applied). Lesson comparisons are descriptive before/after windows
with a minimum detectable effect: never present them as proof a lesson caused a change. Start with
list_rounds, timeline or lesson_effects; use db_schema + query_sql for anything else."""


class QueryRejected(ValueError):
    """query_sql refused the statement (not a single read-only SELECT)."""


# ================================================================ database access
def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"No golf.db at {db_path}: run `golf init` and import data first.")
    return connect(db_path, readonly=True)


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/inf become None, tuples become lists, rows become dicts."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, sqlite3.Row):
        return _clean(dict(obj))
    return obj


# ================================================================ tools (plain functions, testable)
def list_rounds(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                limit: int = 50) -> dict[str, Any]:
    """Counted rounds (overrides applied, 18Birdies deletions and abandoned rounds left out), newest first."""
    limit = max(1, min(int(limit), MAX_ROWS))
    rows = conn.execute(
        """SELECT v.round_id, v.played_on_local AS date, v.source, v.entry_mode, v.holes_played, v.nine,
                  v.gross, v.to_par, v.par_played, v.putts, v.putts_tracked, v.fw_hit, v.fw_chances, v.gir,
                  v.gir_chances, v.birdies, v.pars, v.bogeys, v.dbl_plus, v.excluded, v.dq_flags,
                  COALESCE(c.name, cl.name) AS course, v.tee_id_eff AS tee_id,
                  h.differential, h.differential_kind, h.hi_after AS handicap_index_after
           FROM v_rounds v
           LEFT JOIN courses c ON c.course_key = v.course_key_eff
           LEFT JOIN clubs cl ON cl.club_id = v.club_id
           LEFT JOIN handicap_history h ON h.round_id = v.round_id
           WHERE (? IS NULL OR v.played_on_local >= ?) AND (? IS NULL OR v.played_on_local <= ?)
           ORDER BY v.played_on_local DESC, v.round_id DESC LIMIT ?""",
        (start, start, end, end, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["dq_flags"] = loads(d["dq_flags"], [])
        out.append(d)
    return _clean({"rounds": out, "count": len(out),
                   "note": "Newest first. Differentials and the Handicap Index are unofficial (PCC not applied)."})


def get_round(conn: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    """One round with its per-hole rows, handicap record, course/tee and the timeline events linked to it."""
    r = conn.execute(
        """SELECT v.*, COALESCE(c.name, cl.name) AS course, cl.name AS club FROM v_rounds v
           LEFT JOIN courses c ON c.course_key = v.course_key_eff LEFT JOIN clubs cl ON cl.club_id = v.club_id
           WHERE v.round_id = ?""", (round_id,)).fetchone()
    if r is None:
        raise KeyError(f"No counted round {round_id!r} (see list_rounds).")
    rnd = dict(r)
    rnd.pop("raw", None)
    rnd["dq_flags"] = loads(rnd["dq_flags"], [])
    holes = [dict(h) for h in conn.execute(
        "SELECT hole, strokes, par, si, putts, fairway, gir, gir_miss, penalties, chips, sand, strokes_src, stats_src"
        " FROM round_holes WHERE round_id = ? ORDER BY hole", (round_id,))]
    cols = ("SELECT tee_id, name, gender, par, cr18, slope18, cr_f9, slope_f9, cr_b9, slope_b9, rating_source,"
            " checked_on, is_default FROM tees")
    tee = conn.execute(cols + " WHERE tee_id = ?", (rnd.get("tee_id_eff"),)).fetchone()
    if tee is None and rnd.get("course_key_eff"):   # same fallback as golf.whs: the course's default tee
        tee = conn.execute(cols + " WHERE course_key = ? AND is_default = 1", (rnd["course_key_eff"],)).fetchone()
    hh = conn.execute("SELECT * FROM handicap_history WHERE round_id = ?", (round_id,)).fetchone()
    events = [e for e in timeline_events(conn)["events"] if e.get("linked_round_id") == round_id]
    extractions = [dict(x) for x in conn.execute(
        "SELECT extraction_id, status, created_at, reviewed_at FROM extractions WHERE round_id = ?", (round_id,))]
    return _clean({"round": rnd, "holes": holes, "tee": dict(tee) if tee else None,
                   "handicap": dict(hh) if hh else None, "linked_events": events,
                   "screenshot_extractions": extractions})


def trend(conn: sqlite3.Connection, metric: str = "outcome", window: int = 5) -> dict[str, Any]:
    """Per-round values of one metric, oldest first, with a trailing mean over `window` rounds."""
    from golf.analytics.stats import hi_series, lesson_metric, round_facts, set_outcome

    if metric not in TREND_METRICS:
        raise ValueError(f"Unknown metric {metric!r}; choose one of: {', '.join(TREND_METRICS)}")
    window = max(1, min(int(window), 50))
    key = TREND_METRICS[metric]
    label = metric
    if key == "hi":
        pts = [{"round_id": h["round_id"], "date": h["date"], "value": h["hi"]} for h in hi_series(conn)
               if h["hi"] is not None]
    else:
        facts = round_facts(conn)
        if key == "outcome" and facts:
            scale = lesson_metric(facts)          # the dashboard's lesson scale
            set_outcome(facts, scale)
            label = ("outcome (differential; a 9-hole round is 2 x its 9-hole differential)" if scale == "differential"
                     else "outcome (strokes over par per 9 holes; an 18-hole round is halved)")
        pts = [{"round_id": f["round_id"], "date": f["date"], "value": f.get(key), "holes": f.get("holes")}
               for f in facts if f.get(key) is not None]
    values = [p["value"] for p in pts]
    for i, p in enumerate(pts):
        win = values[max(0, i - window + 1): i + 1]
        p["rolling_mean"] = round(sum(win) / len(win), 3) if len(win) == window else None
    first = sum(values[:window]) / window if len(values) >= window else None
    last = sum(values[-window:]) / window if len(values) >= window else None
    return _clean({
        "metric": label, "window": window, "points": pts, "n": len(pts),
        "first_window_mean": first, "last_window_mean": last,
        "change": (last - first) if first is not None and last is not None else None,
        "note": "Descriptive only. Lower is better for differential, to_par, gross, putts, doubles and handicap_index.",
    })


def timeline_events(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                    event_type: str | None = None) -> dict[str, Any]:
    """Accepted timeline events (auto-accepted or reviewed), oldest first, with review edits applied."""
    from golf.notes import timeline

    keep = ("event_id", "date", "date_start", "date_end", "date_precision", "event_type", "is_planned", "coach",
            "location", "duration_minutes", "summary", "focus_areas", "drills", "swing_thoughts", "equipment",
            "measurements", "injury", "goal_text", "source_excerpt", "doc_title", "doc_source", "linked_round_id")
    events = timeline(conn, start=start, end=end, event_types=[event_type] if event_type else None)
    return _clean({"events": [{k: e.get(k) for k in keep} for e in events], "count": len(events)})


def lesson_effects(conn: sqlite3.Connection) -> dict[str, Any]:
    """Before/after comparison per lesson with bootstrap intervals and the minimum detectable effect."""
    from golf.analytics.lessons import lesson_report
    from golf.analytics.stats import lesson_metric, load_timeline, round_facts, set_outcome

    facts = round_facts(conn)
    rep = lesson_report(set_outcome(facts, lesson_metric(facts)), load_timeline(conn))
    rep["reading_guide"] = ("Descriptive, not causal. A difference smaller than the MDE is indistinguishable "
                            "from round-to-round noise; check each lesson's flags (regression to the mean, "
                            "equipment changes, injuries, off-season gaps).")
    return _clean(rep)


def db_schema() -> str:
    """The golf.db contract (tables, columns, views) for writing query_sql statements."""
    return (Path(__file__).with_name("schema.sql")).read_text()


_WRITE_WORDS = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|pragma|vacuum|reindex|"
                          r"analyze|begin|commit|rollback|savepoint|release)\b", re.IGNORECASE)


def _strip_sql(sql: str) -> str:
    """The statement without comments or string literals (for the keyword checks only)."""
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", s)


def check_select(sql: str) -> str:
    """The statement if it is one read-only SELECT (or WITH ... SELECT); QueryRejected otherwise."""
    text = (sql or "").strip()
    if not text:
        raise QueryRejected("Empty query.")
    bare = _strip_sql(text).strip().rstrip(";").strip()
    if ";" in bare:
        raise QueryRejected("Only one statement per query.")
    if not re.match(r"(select|with)\b", bare, re.IGNORECASE):
        raise QueryRejected("Only SELECT queries are allowed (golf.db is read-only here).")
    word = _WRITE_WORDS.search(bare)
    if word:
        raise QueryRejected(f"'{word.group(1).upper()}' is not allowed: read-only SELECT queries only.")
    return text.rstrip().rstrip(";")


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_IGNORE if (arg1, arg2) in HIDDEN_COLUMNS else sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _allow_all(*_args: Any) -> int:
    return sqlite3.SQLITE_OK


def query_sql(conn: sqlite3.Connection, sql: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only SELECT. Rows beyond `limit` (max 500) are cut off and `truncated` is set."""
    statement = check_select(sql)
    limit = max(1, min(int(limit), MAX_ROWS))
    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(statement)
        rows = cur.fetchmany(limit + 1)
        columns = [d[0] for d in cur.description or []]
    except sqlite3.DatabaseError as e:
        raise QueryRejected(f"SQLite: {e}") from None
    finally:
        # Python 3.10 (this venv) can't clear an authorizer with None: the connection would then refuse
        # every later statement ("not authorized"). An allow-all callback restores normal use there.
        conn.set_authorizer(None if sys.version_info >= (3, 11) else _allow_all)
    truncated = len(rows) > limit
    return _clean({"columns": columns, "rows": [list(r) for r in rows[:limit]], "row_count": min(len(rows), limit),
                   "truncated": truncated})


# ================================================================ MCP wiring
def build_server(cfg: Config):
    """The MCPServer with every tool bound to golf.db opened read-only per call."""
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer("golf", instructions=INSTRUCTIONS)
    ro = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        try:
            conn = open_readonly(cfg.db_path)
        except (FileNotFoundError, sqlite3.Error) as e:
            raise ToolError(str(e)) from None
        try:
            yield conn
        except (ValueError, KeyError) as e:
            raise ToolError(str(e.args[0]) if e.args else str(e)) from None
        finally:
            conn.close()

    @server.tool(name="list_rounds", annotations=ro)
    def list_rounds_tool(start: str | None = None, end: str | None = None, limit: int = 50) -> dict:
        """List counted rounds newest first (date, course, gross, to-par, putts, FIR/GIR, unofficial
        differential). Dates are YYYY-MM-DD, inclusive."""
        with db() as conn:
            return list_rounds(conn, start, end, limit)

    @server.tool(name="get_round", annotations=ro)
    def get_round_tool(round_id: str) -> dict:
        """One round in full: per-hole strokes/par/SI/putts/fairway/GIR, tee rating, handicap record and
        notes linked to that day."""
        with db() as conn:
            return get_round(conn, round_id)

    @server.tool(name="trend", annotations=ro)
    def trend_tool(metric: str = "outcome", window: int = 5) -> dict:
        """A metric per round with a trailing mean over `window` rounds. Metrics: outcome, differential,
        to_par, gross, putts, fir_pct, gir_pct, doubles, three_putt_pct, scramble_pct, to_green,
        putts_over_two, par3_avg, par4_avg, par5_avg, handicap_index."""
        with db() as conn:
            return trend(conn, metric, window)

    @server.tool(name="timeline", annotations=ro)
    def timeline_tool(start: str | None = None, end: str | None = None, event_type: str | None = None) -> dict:
        """Accepted lessons, practice, equipment changes, injuries etc. from Shane's notes, oldest first,
        with the quoted source line. event_type e.g. lesson, practice, equipment_change, injury."""
        with db() as conn:
            return timeline_events(conn, start, end, event_type)

    @server.tool(name="lesson_effects", annotations=ro)
    def lesson_effects_tool() -> dict:
        """Before/after score comparison for each lesson with 80%/95% bootstrap intervals, the minimum
        detectable effect for Shane's own round-to-round spread, focus-matched secondary stats and caveats.
        Descriptive only."""
        with db() as conn:
            return lesson_effects(conn)

    @server.tool(name="db_schema", annotations=ro)
    def db_schema_tool() -> str:
        """The golf.db schema (tables, columns, views such as v_rounds and timeline_raw) for query_sql."""
        return db_schema()

    @server.tool(name="query_sql", annotations=ro)
    def query_sql_tool(sql: str, limit: int = 200) -> dict:
        """Run ONE read-only SELECT (or WITH ... SELECT) against golf.db; anything else is refused. Prefer
        v_rounds over rounds (it applies overrides and drops deleted/abandoned rounds)."""
        with db() as conn:
            return query_sql(conn, sql, limit)

    return server


def run(cfg: Config) -> None:
    """`golf mcp`: serve on stdio until the client disconnects. Diagnostics go to stderr (stdout is the protocol)."""
    if not Path(cfg.db_path).exists():
        print(f"golf mcp: no database at {cfg.db_path}; run `golf init` and import data first.", file=sys.stderr)
    build_server(cfg).run("stdio")
