"""MCP tools against a synthetic demo database opened read-only, plus the MCPServer wiring."""
from __future__ import annotations

import json
import sqlite3

import anyio
import pytest

from golf import mcp_server as M
from golf.db import connect
from golf.demo import seed_demo


@pytest.fixture
def ro(cfg):
    conn = connect(cfg.db_path)
    seed_demo(conn)
    conn.execute("INSERT INTO source_docs(doc_id, source, text) VALUES ('an-x', 'apple_notes', 'private note body')")
    conn.commit()
    conn.close()
    c = M.open_readonly(cfg.db_path)
    yield c
    c.close()


def test_list_rounds_newest_first_with_date_filters(ro):
    out = M.list_rounds(ro, limit=3)
    dates = [r["date"] for r in out["rounds"]]
    assert out["count"] == 3 and dates == sorted(dates, reverse=True) and "unofficial" in out["note"]
    assert isinstance(out["rounds"][0]["dq_flags"], list)
    every = M.list_rounds(ro, limit=500)["rounds"]
    mid = sorted(r["date"] for r in every)[len(every) // 2]
    later = M.list_rounds(ro, start=mid, limit=500)["rounds"]
    assert later and all(r["date"] >= mid for r in later) and len(later) < len(every)
    assert all(r["date"] <= mid for r in M.list_rounds(ro, end=mid, limit=500)["rounds"])
    json.dumps(out)


def test_get_round_has_holes_tee_and_handicap(ro):
    rid = next(r["round_id"] for r in M.list_rounds(ro, limit=50)["rounds"]
               if r["entry_mode"] == "hole_by_hole" and r["holes_played"] == 18
               and r["course"].startswith("Demo Pines"))
    out = M.get_round(ro, rid)
    assert out["round"]["round_id"] == rid and "raw" not in out["round"]
    assert [h["hole"] for h in out["holes"]] == list(range(1, 19))
    assert out["tee"]["is_default"] == 1 and out["tee"]["slope18"]           # default-tee fallback, as in WHS
    with pytest.raises(KeyError):
        M.get_round(ro, "no-such-round")


def test_trend_metrics(ro):
    out = M.trend(ro, "outcome", 5)
    assert out["n"] >= 10 and "differential" in out["metric"]
    assert out["points"][3]["rolling_mean"] is None and out["points"][4]["rolling_mean"] is not None
    assert out["change"] == pytest.approx(out["last_window_mean"] - out["first_window_mean"])
    assert M.trend(ro, "handicap_index", 3)["n"] > 0 and M.trend(ro, "putts", 5)["n"] > 0
    with pytest.raises(ValueError, match="Unknown metric"):
        M.trend(ro, "vibes")


def test_timeline_and_lesson_effects(ro):
    tl = M.timeline_events(ro)
    assert tl["count"] > 0 and all("source_excerpt" in e for e in tl["events"])
    lessons = M.timeline_events(ro, event_type="lesson")["events"]
    assert lessons and {e["event_type"] for e in lessons} == {"lesson"}
    eff = M.lesson_effects(ro)
    assert eff["mde_sentence"] and eff["lessons"] and "not causal" in eff["reading_guide"]
    json.dumps(eff, allow_nan=False)


def test_query_sql_reads_and_hides_private_columns(ro):
    out = M.query_sql(ro, "SELECT COUNT(*) AS n FROM v_rounds;")
    assert out == {"columns": ["n"], "rows": [[24]], "row_count": 1, "truncated": False}
    assert M.query_sql(ro, "select round_id from rounds", limit=5)["truncated"] is True
    hidden = M.query_sql(ro, "SELECT doc_id, text FROM source_docs WHERE doc_id = 'an-x'")
    assert hidden["rows"] == [["an-x", None]]
    ok = M.query_sql(ro, "WITH r AS (SELECT replace(round_id, 'demo-', '') AS id FROM rounds) SELECT COUNT(*) FROM r"
                         " -- trailing comment; delete")
    assert ok["rows"] == [[24]]
    assert M.query_sql(ro, "SELECT 'drop table rounds; delete' AS s")["rows"] == [["drop table rounds; delete"]]
    # The connection is usable afterwards by the other tools (Python 3.10 can't clear an authorizer with None).
    assert ro.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 24
    assert M.lesson_effects(ro)["mde_sentence"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM rounds",
    "UPDATE rounds SET gross = 1",
    "INSERT INTO meta VALUES ('a', 'b')",
    "DROP TABLE rounds",
    "PRAGMA table_info(rounds)",
    "ATTACH DATABASE 'x.db' AS x",
    "SELECT 1; DELETE FROM rounds",
    "WITH x AS (SELECT 1) DELETE FROM rounds",
    "select 1 /* hi */ ; select 2",
    "",
    "VACUUM",
])
def test_query_sql_rejects_anything_but_one_select(ro, sql):
    with pytest.raises(M.QueryRejected):
        M.query_sql(ro, sql)


def test_read_only_even_below_the_checks(ro):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    ro.set_authorizer(M._authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("PRAGMA user_version = 5")
    finally:
        ro.set_authorizer(None)


def test_server_registers_read_only_tools_and_reports_errors(cfg, ro):
    from mcp.server.mcpserver.exceptions import ToolError

    server = M.build_server(cfg)

    async def go():
        tools = {t.name: t for t in await server.list_tools()}
        assert set(tools) == {"list_rounds", "get_round", "trend", "timeline", "lesson_effects", "db_schema",
                              "query_sql"}
        assert all(t.annotations.read_only_hint for t in tools.values())
        res = await server.call_tool("query_sql", {"sql": "SELECT COUNT(*) AS n FROM rounds"})
        assert json.loads(res.content[0].text)["rows"] == [[24]]
        res = await server.call_tool("list_rounds", {"limit": 2})
        assert json.loads(res.content[0].text)["count"] == 2
        with pytest.raises(ToolError, match="read-only"):
            await server.call_tool("query_sql", {"sql": "DELETE FROM rounds"})
        with pytest.raises(ToolError, match="No counted round"):
            await server.call_tool("get_round", {"round_id": "nope"})

    anyio.run(go)


def test_missing_database_is_a_tool_error(cfg):
    from mcp.server.mcpserver.exceptions import ToolError

    server = M.build_server(cfg)

    async def go():
        with pytest.raises(ToolError, match="No golf.db"):
            await server.call_tool("list_rounds", {})

    anyio.run(go)
    assert not cfg.db_path.exists()                    # opening read-only never creates the file


def test_query_sql_stops_a_runaway_query_and_hides_blobs(ro):
    runaway = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(M.QueryRejected, match="longer than 0.2 seconds"):
        M.query_sql(ro, runaway, seconds=0.2)
    assert M.query_sql(ro, "SELECT X'0A0B' AS b")["rows"] == [["<blob 2 bytes>"]]    # the connection still works
