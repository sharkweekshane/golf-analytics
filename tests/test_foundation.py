"""Schema, in-place upgrades, and the Claude API wrapper (cache, spend, prices, error mapping)."""
import sqlite3
from pathlib import Path

import pytest

from golf import db as golf_db
from golf.db import memory_db
from golf.schemas.rounds import CellReread
import golf.llm as llm


def test_schema_applies_and_views_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    for t in ("rounds", "round_holes", "events", "llm_calls", "timeline_raw", "v_rounds"):
        assert t in names


def test_llm_cache_and_cost(conn, cfg, fake_llm):
    fake_llm.push('{"hole":1,"field":"PUTTS","value":"2","confidence":"High","note":""}')
    kw = dict(purpose="t", system="s", content=[{"type": "text", "text": "x"}], output_model=CellReread,
              prompt_version="p", schema_version="s")
    r1 = llm.structured_call(conn, **kw)
    r2 = llm.structured_call(conn, **kw)
    assert r1.parsed.field == "putts" and not r1.cached and r2.cached
    assert r1.cost_usd == round((4000 * 5 + 1500 * 25) / 1e6, 6)
    assert len(fake_llm.requests) == 1


def test_llm_refusal_raises_and_is_logged(conn, cfg, fake_llm):
    fake_llm.push("", stop_reason="refusal")
    with pytest.raises(llm.LLMOutputError):
        llm.structured_call(conn, purpose="t", system="s", content=[{"type": "text", "text": "y"}],
                            output_model=CellReread, prompt_version="p", schema_version="s")
    assert conn.execute("SELECT stop_reason FROM llm_calls").fetchone()[0] == "refusal"


# ------------------------------------------------------------ schema upgrades
V1_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "schema_v1.sql"


def _old_db(path: Path) -> None:
    """A golf.db exactly as the first release created it, with a little data in it."""
    old = sqlite3.connect(path)
    old.executescript(V1_SCHEMA.read_text())
    old.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode, holes_played, gross) "
                "VALUES ('r1', '18b_export', '2026-07-08', 'hole_by_hole', 9, 58)")
    old.execute("INSERT INTO round_overrides(round_id, exclude) VALUES ('r1', 0)")
    old.execute("INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, cost_usd, "
                "created_at) VALUES ('k1', 't', 'claude-opus-5', 'p', 's', 0.25, '2026-09-01')")
    old.commit()
    old.close()


def test_connect_upgrades_an_old_database_in_place(tmp_path):
    path = tmp_path / "golf.db"
    _old_db(path)
    conn = golf_db.connect(path)
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            for t in ("rounds", "round_overrides", "llm_calls")}
    for table, column, _ in golf_db.MIGRATION_COLUMNS:
        assert column in cols[table], (table, column)
    assert "shots" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == golf_db.SCHEMA_VERSION
    r = conn.execute("SELECT * FROM v_rounds WHERE round_id = 'r1'").fetchone()           # view sees new columns
    assert r["gross"] == 58 and r["round_handicap_18b"] is None and r["tee_name"] is None
    row = conn.execute("SELECT billed_calls, cost_total_usd FROM llm_calls").fetchone()
    assert tuple(row) == (1, 0.25) and llm.total_spend(conn) == 0.25                     # backfilled
    conn.execute("UPDATE round_overrides SET nine = 'back' WHERE round_id = 'r1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE round_overrides SET nine = 'middle' WHERE round_id = 'r1'")
    conn.execute("INSERT INTO shots(round_id, seq, hole, club) VALUES ('r1', 1, 1, 'Driver')")
    conn.commit()
    conn.close()

    conn = golf_db.connect(path)                                                          # idempotent
    assert golf_db.migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 1
    assert conn.execute("SELECT nine FROM round_overrides").fetchone()[0] == "back"
    conn.close()


def test_upgraded_and_fresh_databases_have_the_same_columns(tmp_path):
    _old_db(tmp_path / "old.db")
    old, fresh = golf_db.connect(tmp_path / "old.db"), golf_db.connect(tmp_path / "new.db")
    for table in ("rounds", "round_overrides", "llm_calls", "shots", "v_rounds", "timeline_raw"):
        assert [r[1] for r in old.execute(f"PRAGMA table_info({table})")] == \
               [r[1] for r in fresh.execute(f"PRAGMA table_info({table})")], table
    old.close()
    fresh.close()


def test_readonly_connect_never_migrates(tmp_path):
    path = tmp_path / "golf.db"
    _old_db(path)
    ro = golf_db.connect(path, readonly=True)
    assert "tee_name" not in {r[1] for r in ro.execute("PRAGMA table_info(rounds)")}
    ro.close()


def test_deleting_a_round_takes_its_shots(conn):
    conn.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode) "
                 "VALUES ('r1', 'manual', '2026-09-01', 'total_only')")
    conn.execute("INSERT INTO shots(round_id, seq, hole) VALUES ('r1', 1, 1)")
    conn.execute("DELETE FROM rounds WHERE round_id = 'r1'")
    assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0


# ---------------------------------------------------------------- API spend
KW = dict(purpose="t", system="s", content=[{"type": "text", "text": "spend"}], output_model=CellReread,
          prompt_version="p", schema_version="s")
GOOD = '{"hole":1,"field":"PUTTS","value":"2","confidence":"High","note":""}'
ONE_CALL = round((4000 * 5 + 1500 * 25) / 1e6, 6)                                   # $0.0575 at opus-5 rates


def test_retried_and_forced_calls_are_all_billed(conn, cfg, fake_llm):
    """QA: truncated -> retry -> forced gave 1 row and 1 call's cost; 3 calls were billed."""
    fake_llm.push(GOOD[:20], stop_reason="max_tokens")
    with pytest.raises(llm.LLMOutputError):
        llm.structured_call(conn, **KW)
    fake_llm.push(GOOD)
    assert not llm.structured_call(conn, **KW).cached                                # truncation isn't cached
    fake_llm.push(GOOD)
    forced = llm.structured_call(conn, **KW, force=True)
    assert forced.cost_usd == ONE_CALL and llm.structured_call(conn, **KW).cached      # 4th: from the log
    row = conn.execute("SELECT billed_calls, cost_usd, cost_total_usd, stop_reason FROM llm_calls").fetchone()
    assert tuple(row) == (3, ONE_CALL, pytest.approx(3 * ONE_CALL), "end_turn")
    assert llm.total_spend(conn) == pytest.approx(3 * ONE_CALL)
    assert llm.spend_summary(conn) == {"total_usd": pytest.approx(3 * ONE_CALL), "billed_calls": 3,
                                       "requests": 1, "unpriced": 0}


@pytest.mark.parametrize("model, price", [
    ("claude-opus-5", (5.0, 25.0, 0.5)), ("claude-opus-5-5", (4.0, 20.0, 0.2)),
    ("claude-opus-5-5-20260115", (4.0, 20.0, 0.2)), ("claude-sonnet-5-20260301", (2.0, 10.0, 0.2)),
    ("claude-opus-5[1m]", (5.0, 25.0, 0.5)), ("CLAUDE-HAIKU-4-5", (1.0, 5.0, 0.1)),
    ("claude-sonnet-5-latest", (2.0, 10.0, 0.2)), ("claude-fable-5-1", (10.0, 50.0, 0.25)),
    ("claude-opus-6", None), ("claude-opus-5-9", None), ("", None), (None, None),
])
def test_price_lookup_never_guesses(model, price):
    assert llm.price_for(model) == price


@pytest.mark.parametrize("model, per_mtok", [
    ("claude-opus-5", 0.50), ("claude-opus-5-5", 0.20), ("claude-sonnet-5", 0.20), ("claude-haiku-4-5", 0.10),
    ("claude-fable-5-1", 0.25),       # 0.025x input: charging 0.1x would over-count it 4 times
])
def test_cache_reads_are_priced_per_model(model, per_mtok):
    assert llm.cost_usd(model, {"cache_read_input_tokens": 1_000_000}) == pytest.approx(per_mtok)
    pin = llm.price_for(model)[0]
    assert llm.cost_usd(model, {"cache_creation_input_tokens": 1_000_000}) == pytest.approx(pin * 1.25)


def test_a_cached_answer_that_fails_validation_is_asked_again(conn, cfg, fake_llm):
    """QA: 'not json' was cached as end_turn and replayed on every re-run, never re-sent."""
    fake_llm.push("not json")
    with pytest.raises(llm.LLMOutputError):
        llm.structured_call(conn, **KW)
    fake_llm.push(GOOD)
    res = llm.structured_call(conn, **KW)
    assert not res.cached and res.parsed.value == "2" and fake_llm.outputs == []
    assert llm.structured_call(conn, **KW).cached                                   # now a good cache hit
    row = conn.execute("SELECT billed_calls, cost_total_usd, response_json FROM llm_calls").fetchone()
    assert (row[0], row[2]) == (2, GOOD) and row[1] == pytest.approx(2 * ONE_CALL)


def test_unknown_model_price_is_logged_as_unknown(conn, cfg, monkeypatch, caplog):
    def other_model(request, use_fallbacks=False):
        return llm.SendResult(text=GOOD, stop_reason="end_turn", model="claude-mystery-9",
                              usage={"input_tokens": 1000, "output_tokens": 100})

    monkeypatch.setattr(llm, "SEND", other_model)
    with caplog.at_level("WARNING", logger="golf.llm"):
        res = llm.structured_call(conn, **KW)
    assert res.cost_usd == 0.0 and not res.price_known and "claude-mystery-9" in caplog.text
    assert conn.execute("SELECT cost_usd, cost_total_usd FROM llm_calls").fetchone()[:] == (None, None)
    assert llm.spend_summary(conn)["unpriced"] == 1 and llm.total_spend(conn) == 0.0


# ------------------------------------------------------ API errors -> messages
def _api_error(kind: str):
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = lambda code: httpx2.Response(code, request=req)  # noqa: E731
    return {
        "connection": anthropic.APIConnectionError(request=req),
        "timeout": anthropic.APITimeoutError(request=req),
        "rate_limit": anthropic.RateLimitError("rate limited", response=resp(429), body=None),
        "overloaded": anthropic.OverloadedError("Overloaded", response=resp(529), body=None),
        "server": anthropic.InternalServerError("boom", response=resp(500), body=None),
        "unavailable": anthropic.ServiceUnavailableError("down", response=resp(503), body=None),
        "not_found": anthropic.NotFoundError("model: claude-nope", response=resp(404), body=None),
        "forbidden": anthropic.PermissionDeniedError("forbidden", response=resp(403), body=None),
        "auth": anthropic.AuthenticationError("invalid x-api-key", response=resp(401), body=None),
        "too_large": anthropic.RequestTooLargeError("too large", response=resp(413), body=None),
    }[kind]


@pytest.fixture
def failing_client(monkeypatch):
    """Point _send_real at a fake SDK client that raises; the key is a dummy (nothing is sent)."""
    import anthropic

    state = {"error": None}

    class FakeStream:
        def __enter__(self):
            raise state["error"]

        def __exit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, **kw):
            self.messages = self.beta = self
            self.beta.messages = self

        def stream(self, **kw):
            return FakeStream()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-for-tests")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return state


@pytest.mark.parametrize("kind, cls, words", [
    ("connection", llm.LLMTransient, "Could not reach"), ("timeout", llm.LLMTransient, "timed out"),
    ("rate_limit", llm.LLMTransient, "429"), ("overloaded", llm.LLMTransient, "overloaded right now (529)"),
    ("server", llm.LLMTransient, "(500)"), ("unavailable", llm.LLMTransient, "(503)"),
    ("not_found", llm.LLMUnavailable, "[llm].model"), ("forbidden", llm.LLMUnavailable, "403"),
    ("auth", llm.LLMUnavailable, "rejected the key"),
])
def test_api_errors_become_helpful_llm_errors(failing_client, kind, cls, words):
    failing_client["error"] = _api_error(kind)
    with pytest.raises(cls) as err:
        llm._send_real({"model": "claude-opus-5"}, use_fallbacks=False)
    assert words in str(err.value)
    assert isinstance(err.value, llm.LLMUnavailable)                   # callers' existing except clauses
    if cls is llm.LLMTransient:
        assert "finished work is kept" in str(err.value)


def test_other_client_errors_still_raise(failing_client):
    import anthropic

    failing_client["error"] = _api_error("too_large")
    with pytest.raises(anthropic.RequestTooLargeError):
        llm._send_real({"model": "claude-opus-5"}, use_fallbacks=False)


def test_transient_error_bills_and_logs_nothing(conn, cfg, failing_client, monkeypatch):
    monkeypatch.setattr(llm, "SEND", llm._send_real)
    failing_client["error"] = _api_error("overloaded")
    with pytest.raises(llm.LLMTransient):
        llm.structured_call(conn, **KW)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
