"""The Caddie (golf.caddie + golf.secrets), offline: a fake Keychain runner, a fake OpenAI client standing in
for Meta's Responses API, and synthetic demo data. Nothing here reads the real Keychain or calls the API.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
import traceback
from datetime import date

import httpx
import openai
import pytest
from typer.testing import CliRunner

import golf.cli as cli
from golf import caddie, secrets
from golf import mcp_server as mcp
from golf.chat import chat_bundle
from golf.db import connect
from golf.demo import seed_demo

FAKE_KEY = "LLM|fakeaccount77|fake-secret-DO-NOT-LEAK-caddie"
TODAY = date(2026, 9, 26)
runner = CliRunner()


# ------------------------------------------------------------------ fakes
# tests/conftest.py (autouse, every test file): secrets.RUNNER and caddie.CLIENT_FACTORY fail the test if
# used, the retry wait is instant, the degrade memory starts empty, MUSE_API_KEY / OPENAI_* are unset.


class FakeOpenAI:
    """client.responses.create(**kw) replays a script of Responses API results (dicts) or exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.init: dict | None = None
        outer = self

        class _Responses:
            def create(self, **kw):
                outer.requests.append(copy.deepcopy(kw))
                if not outer.script:
                    raise AssertionError("unexpected extra API call")
                item = outer.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        self.responses = _Responses()

    def factory(self, **kw):
        self.init = kw
        return self


def resp(*items, status="completed", usage=(1000, 200), cached=0, reason=None, model="muse-spark-1.1"):
    d = {"id": "resp_1", "object": "response", "status": status, "model": model, "output": list(items),
         "usage": {"input_tokens": usage[0], "input_tokens_details": {"cached_tokens": cached},
                   "output_tokens": usage[1], "output_tokens_details": {"reasoning_tokens": 40}}}
    if reason:
        d["incomplete_details"] = {"reason": reason}
    return d


def fcall(name, args, cid):
    return {"type": "function_call", "id": f"fc_{cid}", "call_id": cid, "name": name, "arguments": json.dumps(args),
            "status": "completed"}


def text(t):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": t, "annotations": []}]}


def reasoning(enc="ENC-opaque-1"):
    return {"type": "reasoning", "id": "rs_1", "encrypted_content": enc, "summary": []}


def api_error(cls, status, *, body=None, message=None):
    req = httpx.Request("POST", "https://api.meta.ai/v1/responses")
    return cls(message or f"Error code: {status} - key {FAKE_KEY} was rejected", response=httpx.Response(status, request=req),
               body=body)


def demo(cfg):
    conn = connect(cfg.db_path)
    seed_demo(conn)
    conn.commit()
    conn.close()


def set_caddie(cfg, **values):
    lines = ["[caddie]"] + [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    (cfg.root / "config.toml").write_text((cfg.root / "config.toml").read_text() + "\n" + "\n".join(lines) + "\n")


def run_ask(cfg, fake, question="How far do I hit my driver?", key=FAKE_KEY, **kw):
    return list(caddie.ask(cfg, [{"role": "user", "content": question}], key_reader=lambda: key,
                           client_factory=fake.factory if fake else None, today=TODAY, **kw))


def rows(cfg, sql, *args):
    conn = connect(cfg.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


# ------------------------------------------------------------------ golf.secrets
def completed(code=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def test_keychain_item_is_read_with_security_and_an_argument_list():
    seen = []

    def fake(args):
        seen.append(args)
        return completed(0, FAKE_KEY + "\n")

    assert secrets.get_secret("muse-spark", runner=fake) == FAKE_KEY
    assert seen == [["/usr/bin/security", "find-generic-password", "-s", "golf-analytics.muse-spark", "-a",
                     "muse-spark", "-w"]]
    assert secrets.has_secret("muse-spark", runner=fake)
    assert secrets.secret_source("muse-spark", runner=fake) == "macOS Keychain"


def test_a_missing_keychain_item_is_none():
    missing = lambda args: completed(44, "", "security: SecKeychainSearchCopyNext: not found")  # noqa: E731
    assert secrets.get_secret("muse-spark", runner=missing) is None
    assert not secrets.has_secret("muse-spark", runner=missing)
    assert secrets.secret_source("muse-spark", runner=missing) is None
    assert secrets.get_secret("muse-spark", runner=lambda a: completed(0, "  \n")) is None


def test_the_environment_variable_overrides_the_keychain(monkeypatch):
    monkeypatch.setenv("MUSE_API_KEY", "  env-key-for-tests \n")
    assert secrets.get_secret("muse-spark") == "env-key-for-tests"          # RUNNER would fail the test
    assert secrets.secret_source("muse-spark") == "environment variable MUSE_API_KEY"
    assert secrets.get_secret("muse-spark", env={}, runner=lambda a: completed(44)) is None


@pytest.mark.parametrize("failure", [
    lambda args: completed(51, FAKE_KEY, FAKE_KEY),
    lambda args: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, 15, output=FAKE_KEY, stderr=FAKE_KEY)),
    lambda args: (_ for _ in ()).throw(PermissionError(f"denied {FAKE_KEY}")),
])
def test_keychain_failures_never_carry_the_key(failure):
    with pytest.raises(secrets.SecretError) as info:
        secrets.get_secret("muse-spark", runner=failure)
    shown = str(info.value) + "".join(traceback.format_exception(info.type, info.value, info.tb))
    assert FAKE_KEY not in shown and "fake-secret" not in shown
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert not secrets.has_secret("muse-spark", runner=failure)


def test_no_security_tool_means_no_key():
    def gone(args):
        raise FileNotFoundError(args[0])

    assert secrets.get_secret("muse-spark", runner=gone) is None


def test_the_default_runner_uses_no_shell_and_a_timeout(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        return completed(44)

    monkeypatch.setattr(secrets, "RUNNER", None)
    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    assert secrets.get_secret("muse-spark") is None
    (args, kw), = calls
    assert args == secrets.keychain_args("muse-spark") and isinstance(args, list)
    assert kw["shell"] is False and kw["capture_output"] and kw["timeout"] > 0 and kw["check"] is False


def test_setup_help_names_the_exact_command_and_unknown_names_fail():
    assert secrets.SETUP_COMMAND == "security add-generic-password -U -s golf-analytics.muse-spark -a muse-spark -w"
    assert secrets.SETUP_COMMAND in secrets.SETUP_HELP
    with pytest.raises(ValueError):
        secrets.get_secret("anthropic")


# ------------------------------------------------------------------ messages
def test_messages_are_validated_and_trimmed():
    ok = caddie.validate_messages([{"role": "user", "content": " hi "}])
    assert ok == [{"role": "user", "content": "hi"}]
    long = [{"role": r, "content": f"m{i}"} for i in range(21) for r in ["user" if i % 2 == 0 else "assistant"]]
    trimmed = caddie.validate_messages(long)
    assert len(trimmed) <= caddie.MAX_TURNS and trimmed[0]["role"] == "user" and trimmed[-1]["content"] == "m20"
    for bad in (None, [], [{"role": "system", "content": "x"}], [{"role": "user", "content": ""}],
                [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                [{"role": "user", "content": "x" * (caddie.MAX_USER_CHARS + 1)}], ["just text"]):
        with pytest.raises(ValueError):
            caddie.validate_messages(bad)


# ------------------------------------------------------------------ tools
@pytest.fixture
def ctx(cfg):
    demo(cfg)
    conn = connect(cfg.db_path)
    conn.execute("UPDATE rounds SET raw = '{\"secret\": \"raw-record\"}'")
    conn.execute("INSERT INTO imports(kind, path, sha256, imported_at) VALUES ('18b_export', '/Users/someone/x.json', "
                 "'abc', '2026-09-25')")
    conn.commit()
    conn.close()
    ro = mcp.open_readonly(cfg.db_path)
    yield caddie.ToolContext(ro, chat_bundle(ro, cfg, today=TODAY))
    ro.close()


def test_tools_use_aliases_and_hide_what_stays_on_the_mac(ctx):
    real_ids = list(ctx.to_alias)
    out, act = ctx.run("list_rounds", json.dumps({"limit": 3}))
    ids = [r["round_id"] for r in out["rounds"]]
    assert all(re.fullmatch(r"r\d+", i) for i in ids) and act == "looked up 3 rounds"
    assert not any(rid in json.dumps(out) for rid in real_ids)

    one, act = ctx.run("get_round", {"round": ids[0]})
    assert one["round"]["round_id"] == ids[0] and one["holes"] and "raw" not in one["round"]
    assert act.startswith("opened the round of ")
    by_date, _ = ctx.run("get_round", {"round": one["round"]["played_on_local"]})
    assert by_date["round"]["round_id"] == ids[0]

    q, act = ctx.run("query_sql", {"sql": f"SELECT round_id, raw, gross FROM v_rounds WHERE round_id = '{ids[1]}'"})
    assert q["rows"] == [[ids[1], None, q["rows"][0][2]]] and act == "ran a query: 1 row"
    q, _ = ctx.run("query_sql", {"sql": "SELECT start_lat, end_lon, distance_yards FROM shots LIMIT 2"})
    assert all(r[0] is None and r[1] is None for r in q["rows"])
    q, _ = ctx.run("query_sql", {"sql": "SELECT path, kind FROM imports"})
    assert q["rows"] and all(r[0] is None and r[1] for r in q["rows"])

    clubs, act = ctx.run("club_distances", {})
    assert clubs["clubs"] and act.startswith("read club distances")
    schema, _ = ctx.run("db_schema", "{}")
    assert "CREATE TABLE" in schema["schema"]
    for name, args in (("trend", {"metric": "putts"}), ("timeline", {"event_type": "lesson"}), ("lesson_effects", {})):
        out, act = ctx.run(name, args)
        assert "error" not in out, (name, out)


@pytest.mark.parametrize("name,args,needle", [
    ("query_sql", {"sql": "DELETE FROM rounds"}, "SELECT"),
    ("query_sql", {"sql": "SELECT 1; SELECT 2"}, "one statement"),
    ("trend", {"metric": "vibes"}, "Unknown metric"),
    ("get_round", {"round": "r999"}, "No counted round"),
    ("get_round", {}, "Give round"),
    ("teleport", {}, "Unknown tool"),
    ("list_rounds", "{not json", "not valid JSON"),
    ("list_rounds", "[1, 2]", "JSON object"),
    ("list_rounds", {"limit": "many"}, "invalid literal"),
])
def test_bad_tool_calls_come_back_as_errors_for_the_model(ctx, name, args, needle):
    out, act = ctx.run(name, args)
    assert set(out) == {"error"} and needle in out["error"], out
    assert not any(rid in out["error"] for rid in ctx.to_alias)


def test_stray_arguments_are_ignored(ctx):
    out, _ = ctx.run("list_rounds", {"limit": 2, "include_everything": True, "start": None})
    assert out["count"] == 2


def test_tool_results_are_capped():
    big = {"columns": ["a"], "rows": [[i, "x" * 40] for i in range(5000)], "row_count": 5000}
    text = caddie.fit(big, 20000)
    data = json.loads(text)
    assert len(text) <= 20000 and data["truncated"] and 0 < len(data["rows"]) < 5000 and "narrow" in data["truncated_note"]
    s = caddie.fit({"schema": "y" * 50000}, 20000)
    assert len(s) <= 20000 and s.endswith("narrow the request]")


# ------------------------------------------------------------------ the loop: native tool calling
def test_native_tool_calling_loop(cfg):
    demo(cfg)
    fake = FakeOpenAI(
        resp(reasoning(), fcall("list_rounds", {"limit": 3}, "c1"), fcall("club_distances", {}, "c2"), usage=(5000, 300),
             cached=1000),
        resp(text("Your **Driver** goes about 191 yd."), usage=(7000, 120)),
    )
    events = run_ask(cfg, fake)
    kinds = [e["type"] for e in events]
    assert kinds == ["activity", "activity", "delta", "done"], events
    assert events[0]["text"] == "looked up 3 rounds" and events[1]["text"].startswith("read club distances")
    assert events[2]["text"] == "Your **Driver** goes about 191 yd."
    done = events[-1]
    assert done["mode"] == "native" and done["lookups"] == 2 and done["api_calls"] == 2
    assert (done["input_tokens"], done["output_tokens"]) == (12000, 420)

    assert fake.init == {"api_key": FAKE_KEY, "base_url": "https://api.meta.ai/v1", "timeout": 120.0, "max_retries": 0}
    first, second = fake.requests
    assert first["model"] == "muse-spark-1.1" and first["store"] is False
    assert first["include"] == ["reasoning.encrypted_content"] and first["reasoning"] == {"effort": "low"}
    assert [t["name"] for t in first["tools"]] == caddie.TOOL_NAMES
    assert all(t["type"] == "function" and t["strict"] is False and "function" not in t for t in first["tools"])
    for never in ("safety_identifier", "metadata", "user", "previous_response_id", "tool_choice"):
        assert never not in first
    assert "Today is 2026-09-26" in first["instructions"] and "\nr1," in first["instructions"]
    assert "demo-0" not in first["instructions"]                       # real round ids stay on the Mac
    assert first["instructions"] == second["instructions"]            # resent every call (stateless)

    replay = second["input"]
    assert replay[0] == {"role": "user", "content": "How far do I hit my driver?"}
    assert replay[1] == {"type": "reasoning", "encrypted_content": "ENC-opaque-1", "summary": []}
    assert replay[2] == {"type": "function_call", "call_id": "c1", "name": "list_rounds", "arguments": '{"limit": 3}'}
    assert replay[3]["call_id"] == "c2" and "id" not in replay[3] and "status" not in replay[3]
    outputs = [i for i in replay if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2"]
    assert json.loads(outputs[0]["output"])["rounds"][0]["round_id"] == "r24"

    logged = rows(cfg, "SELECT * FROM llm_calls WHERE purpose = 'caddie' ORDER BY call_id")
    assert len(logged) == 2 and all(r["response_json"] is None for r in logged)
    assert logged[0]["input_tokens"] == 4000 and logged[0]["cache_read_tokens"] == 1000
    assert logged[0]["cost_usd"] == pytest.approx((4000 * 1.25 + 1000 * 0.15 + 300 * 4.25) / 1e6)
    assert logged[0]["stop_reason"] == "tool_calls" and logged[1]["stop_reason"] == "completed"
    meta = json.loads(logged[0]["request_meta"])
    assert meta["step"] == 1 and meta["tool_calls"] == 2 and meta["mode"] == "native"
    everything = json.dumps(rows(cfg, "SELECT * FROM llm_calls"))
    assert "driver" not in everything.lower() and FAKE_KEY not in everything and "fakeaccount" not in everything


def test_the_lookup_budget_makes_the_next_call_the_last(cfg):
    """After max_tool_rounds rounds of lookups the next call is the last: tool_choice "none" plus a note, so
    the model answers in text instead of spending another paid call on lookups it can't have."""
    demo(cfg)
    set_caddie(cfg, max_tool_rounds=2)
    fake = FakeOpenAI(
        resp(fcall("list_rounds", {}, "a")), resp(fcall("trend", {"metric": "putts"}, "b")),
        resp(text("Here's what I have.")),
    )
    events = run_ask(cfg, fake)
    assert events[-2] == {"type": "delta", "text": "Here's what I have."} and events[-1]["api_calls"] == 3
    first, second, last = fake.requests
    assert "tool_choice" not in first and "tool_choice" not in second
    assert last["tool_choice"] == "none" and last["tools"]
    assert last["input"][-1] == {"role": "user", "content": caddie.LAST_CALL_NOTE}
    assert [i["call_id"] for i in last["input"] if i.get("type") == "function_call_output"] == ["a", "b"]
    assert any("lookup limit" in e.get("text", "") for e in events)

    # still asking for lookups on the last call: its text is the answer, or no_answer when there is none
    fake = FakeOpenAI(resp(fcall("list_rounds", {}, "x0")), resp(fcall("list_rounds", {}, "x1")),
                      resp(fcall("list_rounds", {}, "x2")))
    events = run_ask(cfg, fake)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "no_answer" and len(fake.requests) == 3
    fake = FakeOpenAI(resp(fcall("list_rounds", {}, "x0")), resp(fcall("list_rounds", {}, "x1")),
                      resp(text("Best I can say."), fcall("list_rounds", {}, "x2")))
    assert run_ask(cfg, fake)[-2] == {"type": "delta", "text": "Best I can say."}


def test_answers_cut_short_or_failed(cfg):
    demo(cfg)
    events = run_ask(cfg, FakeOpenAI(resp(text("Partial answer"), status="incomplete", reason="max_output_tokens")))
    assert events[-2]["text"] == "Partial answer" and events[-1]["truncated"] is True
    events = run_ask(cfg, FakeOpenAI(resp(status="failed")))
    assert events[-1]["code"] == "unavailable"
    events = run_ask(cfg, FakeOpenAI(resp(text("   "))))
    assert events[-1]["code"] == "empty"


# ------------------------------------------------------------------ the loop: documented fallback
def test_lookup_fallback_protocol(cfg):
    demo(cfg)
    set_caddie(cfg, tools="lookup")
    fake = FakeOpenAI(
        resp(text('Let me check.\n{"lookup": {"tool": "get_round", "args": {"round": "r2"}}}')),
        resp(text('{"answer": "Round **r2** was a 101."}')),
    )
    events = run_ask(cfg, fake, question="How was r2?")
    assert [e["type"] for e in events] == ["activity", "delta", "done"]
    assert events[1]["text"] == "Round **r2** was a 101." and events[2]["mode"] == "lookup"
    first, second = fake.requests
    for absent in ("tools", "include", "reasoning", "parallel_tool_calls"):
        assert absent not in first
    assert first["store"] is False and "Looking things up" in first["instructions"]
    assert second["input"][1]["role"] == "assistant" and second["input"][2]["content"].startswith("LOOKUP RESULT (get_round)")
    assert '"round_id":"r2"' in second["input"][2]["content"]


def test_parse_lookup():
    assert caddie.parse_lookup('{"lookup": {"tool": "trend", "args": {"metric": "putts"}}}') == ("trend", {"metric": "putts"})
    assert caddie.parse_lookup('```json\n{"lookup": {"tool": "db_schema"}}\n```') == ("db_schema", {})
    assert caddie.parse_lookup('{"answer": "Plain."}') == "Plain."
    assert caddie.parse_lookup("You averaged +7 {per nine} lately.") is None
    assert caddie.parse_lookup("No JSON at all.") is None


def test_auto_mode_degrades_to_lookups_and_remembers_it(cfg):
    demo(cfg)
    bad = lambda: api_error(openai.BadRequestError, 400, body={"code": "unsupported_parameter", "param": "tools"})  # noqa: E731
    fake = FakeOpenAI(bad(), bad(), resp(text("Fine without tools.")))
    events = run_ask(cfg, fake)
    assert events[-1]["mode"] == "lookup" and events[-2]["text"] == "Fine without tools."
    notes = [e["text"] for e in events if e["type"] == "activity"]
    assert notes == [caddie.DEGRADE_NOTES[1], caddie.DEGRADE_NOTES[2]]          # two different lines
    assert "include" in fake.requests[0] and "include" not in fake.requests[1] and "tools" in fake.requests[1]
    assert "tools" not in fake.requests[2]

    again = FakeOpenAI(resp(text("Straight to lookups.")))
    assert run_ask(cfg, again)[-1]["mode"] == "lookup" and "tools" not in again.requests[0]


def test_a_rejected_reasoning_replay_drops_it_and_keeps_the_lookups(cfg):
    """Meta refusing the replayed reasoning mid-question: the reasoning is dropped and the question carries
    on from where it was (no restart, no tool run twice), and plain tool calling is remembered."""
    demo(cfg)
    fake = FakeOpenAI(
        resp(reasoning(), fcall("list_rounds", {"limit": 2}, "c1")),
        api_error(openai.BadRequestError, 400, body={"code": "invalid_value", "param": "input[1]"}),
        resp(text("Two rounds.")),
    )
    events = run_ask(cfg, fake)
    assert events[-1]["mode"] == "native-plain" and events[-2]["text"] == "Two rounds."
    assert [e["text"] for e in events if e["type"] == "activity"] == [
        "looked up 2 rounds", "Meta refused the replayed reasoning; carrying on without it"]
    third = fake.requests[2]
    assert "tools" in third and "include" not in third and "reasoning" not in third
    assert not any(i.get("type") == "reasoning" for i in third["input"])
    assert [i["call_id"] for i in third["input"] if i.get("type") == "function_call_output"] == ["c1"]
    assert caddie._LEVELS[("https://api.meta.ai/v1", "muse-spark-1.1", "auto")] == 1
    assert events[-1]["api_calls"] == 2 and events[-1]["lookups"] == 1


def test_a_400_later_in_a_question_is_an_error_not_a_restart(cfg):
    demo(cfg)
    fake = FakeOpenAI(
        resp(fcall("list_rounds", {"limit": 2}, "c1")),                        # level 0, no reasoning to replay
        api_error(openai.BadRequestError, 400, body={"code": "invalid_value", "param": "input[2].output"}),
    )
    events = run_ask(cfg, fake)
    assert [e["type"] for e in events] == ["activity", "error"] and events[-1]["code"] == "bad_request"
    assert "invalid_value, param input[2].output" in events[-1]["message"] and len(fake.requests) == 2
    assert caddie._LEVELS == {}                                                  # nothing downgraded


# ------------------------------------------------------------------ errors
RETRIED = {"rate_limited", "unavailable", "network"}     # retried once by the Caddie (never a timeout)


@pytest.mark.parametrize("exc,code", [
    (lambda: api_error(openai.AuthenticationError, 401), "key_rejected"),
    (lambda: api_error(openai.APIStatusError, 402), "no_credits"),
    (lambda: api_error(openai.RateLimitError, 429, body={"code": "insufficient_quota"}), "no_credits"),
    (lambda: api_error(openai.RateLimitError, 429, body={"code": "rate_limit_exceeded"}), "rate_limited"),
    (lambda: api_error(openai.PermissionDeniedError, 403), "forbidden"),
    (lambda: api_error(openai.NotFoundError, 404, body={"code": "model_not_found"}), "model_not_found"),
    (lambda: api_error(openai.APIStatusError, 413), "too_long"),
    (lambda: api_error(openai.InternalServerError, 503, body={"code": "service_overloaded"}), "unavailable"),
    (lambda: api_error(openai.APIStatusError, 504), "timeout"),
    (lambda: openai.APIConnectionError(message=f"conn {FAKE_KEY}", request=httpx.Request("POST", "https://x")), "network"),
    (lambda: openai.APITimeoutError(request=httpx.Request("POST", "https://x")), "timeout"),
    (lambda: api_error(openai.BadRequestError, 400, body={"code": "content_policy_violation"}), "refused"),
    (lambda: api_error(openai.BadRequestError, 400, body={"code": "context_length_exceeded"}), "too_long"),
    (lambda: RuntimeError(f"something odd with {FAKE_KEY}"), "error"),
])
def test_errors_are_friendly_and_never_show_the_key(cfg, caplog, exc, code):
    demo(cfg)
    caplog.set_level(logging.DEBUG)
    fake = FakeOpenAI(exc(), exc())
    events = run_ask(cfg, fake)
    err = events[-1]
    assert err["type"] == "error" and err["code"] == code, err
    assert len(fake.requests) == (2 if code in RETRIED else 1), code
    everything = json.dumps(events) + caplog.text + json.dumps(rows(cfg, "SELECT * FROM llm_calls"))
    for part in (FAKE_KEY, "fakeaccount77", "fake-secret-DO-NOT-LEAK-caddie"):
        assert part not in everything
    if code == "key_rejected":
        assert secrets.SETUP_COMMAND in err["message"]


def test_a_400_in_lookup_mode_says_what_meta_objected_to(cfg):
    demo(cfg)
    set_caddie(cfg, tools="lookup")
    events = run_ask(cfg, FakeOpenAI(api_error(openai.BadRequestError, 400,
                                               body={"code": "invalid_value", "param": "max_output_tokens"})))
    assert events[-1]["code"] == "bad_request" and "invalid_value, param max_output_tokens" in events[-1]["message"]
    events = run_ask(cfg, FakeOpenAI(api_error(openai.BadRequestError, 400,
                                               body={"code": "<script>free text</script>", "param": "a b"})))
    assert "<script>" not in events[-1]["message"] and "a b" not in events[-1]["message"]


def test_no_key_explains_the_keychain_command_and_calls_nothing(cfg):
    demo(cfg)
    events = run_ask(cfg, FakeOpenAI(), key=None)
    assert events == [{"type": "error", "code": "no_key", "message": caddie.NO_KEY_MESSAGE}]
    assert secrets.SETUP_COMMAND in caddie.NO_KEY_MESSAGE

    def locked():
        raise secrets.SecretError("The macOS Keychain did not answer in time.")

    events = list(caddie.ask(cfg, [{"role": "user", "content": "q"}], key_reader=locked, today=TODAY))
    assert events[-1]["code"] == "keychain" and "did not answer" in events[-1]["message"]


def test_the_key_is_read_at_call_time_through_the_default_reader(cfg, monkeypatch):
    demo(cfg)
    reads = []

    def keychain(args):
        reads.append(args)
        return completed(0, FAKE_KEY)

    monkeypatch.setattr(secrets, "RUNNER", keychain)
    fake = FakeOpenAI(resp(text("One.")), resp(text("Two.")))
    for _ in range(2):
        list(caddie.ask(cfg, [{"role": "user", "content": "q"}], client_factory=fake.factory, today=TODAY))
    assert len(reads) == 2 and fake.init["api_key"] == FAKE_KEY


@pytest.mark.parametrize("setting,needle", [
    ({"model": "muse-spark-1.3-contributor"}, "contributor"),
    ({"base_url": "http://api.meta.ai/v1"}, "https://"),
    ({"provider": "openai"}, "provider"),
    ({"effort": "none"}, "effort"),
    ({"tools": "sometimes"}, "tools"),
])
def test_unsafe_or_unknown_settings_are_refused_before_the_key_is_read(cfg, setting, needle):
    demo(cfg)
    set_caddie(cfg, **setting)

    def must_not_read():
        raise AssertionError("key read despite a config problem")

    events = list(caddie.ask(cfg, [{"role": "user", "content": "q"}], key_reader=must_not_read, today=TODAY))
    assert events[-1]["code"] == "config" and needle in events[-1]["message"]


def test_no_database_yet(cfg):
    events = run_ask(cfg, FakeOpenAI())
    assert events[-1]["code"] == "no_data"


def test_scrub_removes_the_key_and_its_parts():
    assert caddie.scrub(f"a {FAKE_KEY} b", FAKE_KEY) == "a [redacted] b"
    assert "fakeaccount77" not in caddie.scrub("id fakeaccount77 leaked", FAKE_KEY)
    assert caddie.scrub("nothing here", None) == "nothing here"


# ------------------------------------------------------------------ usage, prices, status
def test_prices_and_usage_summary(cfg):
    assert caddie.call_cost("muse-spark-1.1", {"input_tokens": 1_000_000, "cached_tokens": 0, "output_tokens": 0}) == 1.25
    assert caddie.call_cost("muse-spark-1.3-20260901", {"input_tokens": 0, "output_tokens": 1_000_000}) == 4.25
    assert caddie.call_cost("mystery-model", {"input_tokens": 10}) is None
    demo(cfg)
    run_ask(cfg, FakeOpenAI(resp(text("A."), model="mystery-model")))
    conn = connect(cfg.db_path)
    u = caddie.usage_summary(conn)
    conn.close()
    assert u["questions"] == 1 and u["calls"] == 1 and u["unpriced"] == 1 and u["spend_usd"] == 0


def test_status_says_whether_a_key_is_set_never_what_it_is(cfg):
    conn = connect(cfg.db_path)
    info = caddie.status_info(conn, cfg, key_reader=lambda: FAKE_KEY)
    assert info["key_set"] and info["model"] == "muse-spark-1.1" and FAKE_KEY not in json.dumps(info)
    assert "Key: set" in caddie.format_status(info)
    info = caddie.status_info(conn, cfg, key_reader=lambda: None)
    assert "NOT set" in caddie.format_status(info)
    conn.close()


def test_card_data_is_the_public_safe_summary(cfg):
    demo(cfg)
    conn = connect(cfg.db_path)
    card = caddie.card_data(conn, cfg, today=TODAY)
    raw_ids = [r[0] for r in conn.execute("SELECT round_id FROM rounds")]
    conn.close()
    assert card["kpis"] and card["club_medians"] and len(card["rounds"]) == 24
    assert set(card["rounds"][0]) == {"date", "course", "holes", "gross", "to_par", "to_par_per9"}
    assert not any(rid in json.dumps(card) for rid in raw_ids)


# ------------------------------------------------------------------ CLI
def cli_run(*args, code=0):
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == code, f"exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result.output


def test_cli_caddie_status_and_key(cfg, monkeypatch):
    out = cli_run("caddie", "key")                        # never touches the Keychain (RUNNER would fail)
    assert secrets.SETUP_COMMAND in out
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(0, FAKE_KEY))
    out = cli_run("caddie", "status")
    assert "Key: set" in out and "muse-spark-1.1" in out and "https://api.meta.ai/v1" in out
    assert FAKE_KEY not in out and "fakeaccount" not in out
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(44))
    assert "NOT set" in cli_run("caddie", "status")


def test_cli_caddie_ask(cfg, monkeypatch):
    demo(cfg)
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(44))
    out = cli_run("caddie", "ask", "Am I improving?", code=2)
    assert secrets.SETUP_COMMAND in out
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(0, FAKE_KEY))
    fake = FakeOpenAI(resp(fcall("trend", {"metric": "outcome"}, "t1")), resp(text("Yes, **2.8** better per 9.")))
    monkeypatch.setattr(caddie, "CLIENT_FACTORY", fake.factory)
    out = cli_run("caddie", "ask", "Am I improving?")
    assert "trend of outcome" in out and "Yes, **2.8** better per 9." in out and FAKE_KEY not in out
    assert "(muse-spark-1.1, native tool calling, 1 lookup, 2 API calls)" in out
    monkeypatch.setattr(caddie, "CLIENT_FACTORY", FakeOpenAI(api_error(openai.RateLimitError, 429),
                                                             api_error(openai.RateLimitError, 429)).factory)
    out = cli_run("caddie", "ask", "Again?", code=1)
    assert "rate-limiting" in out


def test_golf_status_keeps_caddie_calls_out_of_the_claude_figures(cfg):
    conn = connect(cfg.db_path)
    conn.execute("INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, request_meta, cost_usd,"
                 " created_at, billed_calls, cost_total_usd) VALUES ('a', 'note', 'claude-opus-5', 'v1', 'v1', '{}', 0.1,"
                 " '2026-09-25', 1, 0.1), ('caddie-x', 'caddie', 'muse-spark-1.1', 'caddie-1', 'native',"
                 " '{\"question\": \"q1\"}', 0.02, '2026-09-26', 1, 0.02)")
    conn.commit()
    conn.close()
    out = cli_run("status")
    assert "1 call billed, $0.10 spent so far" in out
    assert "Caddie (Muse Spark): 1 question, 1 API call, $0.02 so far" in out


def test_claude_spend_is_never_negative_zero_next_to_caddie_calls(cfg):
    demo(cfg)
    for _ in range(3):
        run_ask(cfg, FakeOpenAI(resp(text("A."), usage=(1234, 567))))
    out = cli_run("status")
    assert "0 calls billed, $0.00 spent so far" in out and "-0.00" not in out
    assert "Caddie (Muse Spark): 3 questions, 3 API calls" in out


def test_the_real_sdk_sends_one_bearer_request_to_meta(cfg, monkeypatch):
    """The OpenAI SDK itself, over a mock transport: the path, the headers and the body that go on the wire."""
    demo(cfg)
    monkeypatch.setenv("OPENAI_ORG_ID", "org-should-not-go-to-meta")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    wire = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        if len(wire) == 1:
            return httpx.Response(200, json=resp(reasoning(), fcall("trend", {"metric": "putts"}, "t1")))
        if len(wire) == 2:
            return httpx.Response(200, json=resp(text("Putting is steady.")))
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key {FAKE_KEY}", "type":
                                                   "authentication_error", "code": "invalid_api_key"}})

    def factory(**kw):
        return caddie._openai_client(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    events = list(caddie.ask(cfg, [{"role": "user", "content": "Putting?"}], key_reader=lambda: FAKE_KEY,
                             client_factory=factory, today=TODAY))
    assert events[-2] == {"type": "delta", "text": "Putting is steady."}
    first = wire[0]
    assert str(first.url) == "https://api.meta.ai/v1/responses" and first.method == "POST"
    assert first.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "openai-organization" not in first.headers and "openai-project" not in first.headers
    body = json.loads(first.content)
    assert body["store"] is False and body["model"] == "muse-spark-1.1" and body["tools"][0]["type"] == "function"
    second = json.loads(wire[1].content)
    assert second["input"][-1] == {"type": "function_call_output", "call_id": "t1", "output": second["input"][-1]["output"]}

    events = list(caddie.ask(cfg, [{"role": "user", "content": "Again?"}], key_reader=lambda: FAKE_KEY,
                             client_factory=factory, today=TODAY))
    assert len(wire) == 3                                     # a 401 is not retried
    assert events == [{"type": "error", "code": "key_rejected", "message": events[0]["message"]}]
    assert FAKE_KEY not in events[0]["message"] and "fakeaccount77" not in events[0]["message"]


# ------------------------------------------------------------------ review fixes (2026-09-26)
def test_status_checks_presence_without_ever_asking_for_the_secret(cfg, monkeypatch):
    """/status, /caddie and `golf caddie status` run security WITHOUT -w: the secret never enters the process."""
    seen = []

    def keychain(args):
        seen.append(list(args))
        return completed(0, 'keychain: "login.keychain-db"\nattributes: ...' if "-w" not in args else FAKE_KEY)

    monkeypatch.setattr(secrets, "RUNNER", keychain)
    state = caddie.key_state()
    assert state == {"set": True, "source": "macOS Keychain", "error": None, "dotenv_ignored": False}
    assert secrets.has_secret("muse-spark") and secrets.secret_source("muse-spark") == "macOS Keychain"
    conn = connect(cfg.db_path)
    info = caddie.status_info(conn, cfg)
    conn.close()
    assert info["key_set"] and "Key: set (macOS Keychain)" in caddie.format_status(info)
    assert cli_run("caddie", "status").count("Key: set") == 1
    assert seen and all("-w" not in a for a in seen), seen
    assert seen[0] == secrets.presence_args("muse-spark") == ["/usr/bin/security", "find-generic-password", "-s",
                                                              "golf-analytics.muse-spark", "-a", "muse-spark"]
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(44))
    assert caddie.key_state()["set"] is False
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(51))
    locked = caddie.key_state()
    assert locked["set"] is False and "exited with 51" in locked["error"]


def test_status_says_when_the_environment_overrides_the_keychain(cfg, monkeypatch):
    monkeypatch.setenv("MUSE_API_KEY", "env-key-for-tests")
    state = caddie.key_state()                  # RUNNER would fail the test: the env var answers first
    assert state["set"] and state["source"] == "environment variable MUSE_API_KEY"
    conn = connect(cfg.db_path)
    out = caddie.format_status(caddie.status_info(conn, cfg))
    conn.close()
    assert "from the environment variable MUSE_API_KEY, which overrides the Keychain" in out
    assert "env-key-for-tests" not in out


def test_muse_api_key_is_never_loaded_from_dotenv(cfg, tmp_path, monkeypatch):
    import golf.config as config_mod

    env_file = tmp_path / "dot-env-fixture"
    name = "MUSE_API_KEY"                   # built here, so the privacy check doesn't flag this file
    env_file.write_text(f"SOME_SETTING=on\nexport {name}=value-from-a-file\n{name} = other\n")
    monkeypatch.delenv("SOME_SETTING", raising=False)
    config_mod.load_dotenv(env_file)
    assert name not in os.environ and os.environ["SOME_SETTING"] == "on"
    assert config_mod.DOTENV_IGNORED == {"MUSE_API_KEY"}
    monkeypatch.setattr(secrets, "RUNNER", lambda args: completed(44))
    conn = connect(cfg.db_path)
    info = caddie.status_info(conn, cfg)
    conn.close()
    out = caddie.format_status(info)
    assert info["key_in_dotenv_ignored"] and caddie.DOTENV_NOTE in out and "value-from-a-file" not in out


def test_api_errors_carry_no_chained_sdk_exception(cfg):
    """The SDK exception holds the Bearer key in its request headers: it must not ride along as
    __context__ / __cause__ on what the Caddie raises."""
    st = caddie.CaddieSettings(tools="lookup")
    for exc in (api_error(openai.AuthenticationError, 401), api_error(openai.BadRequestError, 400),
                openai.APITimeoutError(request=httpx.Request("POST", "https://api.meta.ai/v1/responses"))):
        provider = caddie.MuseProvider(st, FakeOpenAI(exc).factory)
        provider.open(FAKE_KEY)
        with pytest.raises(caddie.CaddieError) as info:
            provider.create(instructions="i", items=[{"role": "user", "content": "q"}], level=2)
        assert info.value.__context__ is None and info.value.__cause__ is None
        shown = "".join(traceback.format_exception(info.type, info.value, info.tb))
        assert FAKE_KEY not in shown and "fakeaccount77" not in shown
    provider = caddie.MuseProvider(caddie.CaddieSettings(), FakeOpenAI(api_error(openai.BadRequestError, 400)).factory)
    provider.open(FAKE_KEY)
    with pytest.raises(caddie._Degrade) as info:
        provider.create(instructions="i", items=[], level=0, can_degrade=True)
    assert info.value.__context__ is None


def test_a_400_shows_only_known_codes_and_field_paths():
    """A key in underscore form fits the plain-identifier pattern: code/param echoing it are never shown."""
    st = caddie.CaddieSettings()
    key = "LLM_TEST_SECRET_123"
    for body in ({"code": key[:-1], "param": key[4:]}, {"code": "made_up_code", "param": "input.secretpart"}):
        err = caddie.map_api_error(api_error(openai.BadRequestError, 400, body=body), st)
        assert err.code == "bad_request" and "TEST_SECRET" not in str(err) and "made_up" not in str(err)
        assert "secretpart" not in str(err) and "(400)" in str(err)
    err = caddie.map_api_error(api_error(openai.BadRequestError, 400,
                                         body={"code": "unsupported_parameter", "param": "tools[0].strict"}), st)
    assert "(400: unsupported_parameter, param tools[0].strict)" in str(err)


def test_the_key_only_goes_to_meta_unless_allowed(cfg):
    def must_not_read():
        raise AssertionError("key read for a foreign host")

    demo(cfg)
    for url in ("https://evil.example/v1", "https://api.meta.ai.evil.example/v1", "https://api.meta.ai@evil.example/v1"):
        st = caddie.CaddieSettings(base_url=url)
        problems = caddie.settings_problems(st)
        assert problems and "api.meta.ai" in problems[0], url
        events = list(caddie.ask(cfg, [{"role": "user", "content": "q"}], key_reader=must_not_read, st=st,
                                 today=TODAY))
        assert events[-1]["code"] == "config"
    assert caddie.settings_problems(caddie.CaddieSettings(base_url="https://api.meta.ai/v1")) == []
    assert caddie.settings_problems(caddie.CaddieSettings(base_url="http://127.0.0.1:9999/v1")) == []
    set_caddie(cfg, base_url="https://proxy.example/v1", allow_any_base_url=True)
    st = caddie.settings(cfg)
    assert st.allow_any_base_url and caddie.settings_problems(st) == []


def _wire_client(handler):
    def factory(**kw):
        return caddie._openai_client(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return factory


def test_environment_headers_never_reach_meta(cfg, monkeypatch):
    """OPENAI_CUSTOM_HEADERS, OPENAI_ORG_ID and OPENAI_PROJECT_ID are read by the SDK; none go to Meta, and
    an Authorization line there can't replace the key."""
    demo(cfg)
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Other-Service-Key: other-secret-abc\nAuthorization: Bearer hijack\n"
                                                "Helicone-Auth: Bearer hk-fake")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-should-not-go")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-should-not-go")
    wire = []

    def handler(request):
        wire.append(request)
        return httpx.Response(200, json=resp(text("Fine.")))

    events = list(caddie.ask(cfg, [{"role": "user", "content": "q"}], key_reader=lambda: FAKE_KEY,
                             client_factory=_wire_client(handler), today=TODAY))
    assert events[-2] == {"type": "delta", "text": "Fine."}
    (sent,) = wire
    names = {k.lower() for k in sent.headers}
    assert sent.headers["authorization"] == f"Bearer {FAKE_KEY}"
    for header in ("x-other-service-key", "helicone-auth", "openai-organization", "openai-project"):
        assert header not in names, header
    assert "other-secret-abc" not in str(sent.headers) and "hijack" not in str(sent.headers)


@pytest.mark.parametrize("responses,code,attempts", [
    (["timeout"], "timeout", 1),                                                          # never re-sent
    ([(429, {"error": {"code": "insufficient_quota", "message": "x"}})], "no_credits", 1),
    ([(429, {"error": {"code": "rate_limit_exceeded", "message": "x"}}, {"retry-after": "60"})], "rate_limited", 1),
    ([(429, {"error": {"code": "rate_limit_exceeded", "message": "x"}}, {"retry-after": "1"}), (200, None)], None, 2),
    ([(503, {"error": {"message": "x"}}), (503, {"error": {"message": "x"}})], "unavailable", 2),
    ([(401, {"error": {"code": "invalid_api_key", "message": f"bad key {FAKE_KEY}"}})], "key_rejected", 1),
])
def test_retries_through_the_real_sdk(cfg, monkeypatch, responses, code, attempts):
    """One retry for a rate limit (a short Retry-After) or a 5xx; none for a timeout, credits or a bad key."""
    demo(cfg)
    set_caddie(cfg, timeout_seconds=10)
    waits = []
    monkeypatch.setattr(caddie, "SLEEP", waits.append)
    script, wire = list(responses), []

    def handler(request):
        wire.append(request)
        item = script.pop(0)
        if item == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        status, body, *headers = item
        return httpx.Response(status, json=body if body is not None else resp(text("OK.")),
                              headers=headers[0] if headers else None)

    events = list(caddie.ask(cfg, [{"role": "user", "content": "q"}], key_reader=lambda: FAKE_KEY,
                             client_factory=_wire_client(handler), today=TODAY))
    assert len(wire) == attempts, [e for e in events]
    if code is None:
        assert events[-2] == {"type": "delta", "text": "OK."} and waits == [1.0]
    else:
        assert events[-1]["code"] == code and FAKE_KEY not in json.dumps(events)
    if code == "timeout":
        assert "within 10 seconds" in events[-1]["message"]


def test_a_refused_tool_choice_resends_the_last_call_without_tools(cfg):
    demo(cfg)
    set_caddie(cfg, max_tool_rounds=1)
    fake = FakeOpenAI(resp(fcall("list_rounds", {}, "a")),
                      api_error(openai.BadRequestError, 400, body={"code": "unsupported_parameter", "param": "tool_choice"}),
                      resp(text("Answered.")))
    events = run_ask(cfg, fake)
    assert events[-2] == {"type": "delta", "text": "Answered."} and len(fake.requests) == 3
    assert fake.requests[1]["tool_choice"] == "none"
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(fake.requests[2])


def test_tool_output_per_question_is_capped(cfg, monkeypatch):
    demo(cfg)
    monkeypatch.setattr(caddie, "TOOL_BUDGET_CHARS", 3000)
    fake = FakeOpenAI(resp(fcall("list_rounds", {"limit": 30}, "a"), fcall("db_schema", {}, "b"),
                           fcall("trend", {"metric": "putts"}, "c")),
                      resp(text("Enough.")))
    events = run_ask(cfg, fake)
    assert events[-2] == {"type": "delta", "text": "Enough."} and len(fake.requests) == 2
    outputs = [i["output"] for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert sum(len(o) for o in outputs[:2]) <= 3000 + 2000 and "data budget" in outputs[-1]
    assert fake.requests[1]["tool_choice"] == "none"
    assert any("data limit" in e.get("text", "") for e in events)


def test_query_sql_has_a_deadline_and_no_huge_values(ctx):
    runaway = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(mcp.QueryRejected, match="longer than 0.2 seconds"):
        caddie.query_sql(ctx.conn, runaway, seconds=0.2)
    assert caddie.query_sql(ctx.conn, "SELECT 1")["rows"] == [[1]]          # the connection still works
    for fn in ("zeroblob(50000000)", "randomblob(10)", "ZEROBLOB(1)"):
        with pytest.raises(mcp.QueryRejected, match="not authorized|prohibited"):
            caddie.query_sql(ctx.conn, f"SELECT {fn}")
    out = caddie.query_sql(ctx.conn, "SELECT X'00FF10', substr(printf('%.9000c', 'x'), 1, 9000)")
    assert out["rows"][0][0] == "<blob 3 bytes>" and out["rows"][0][1].endswith("[cut: 9000 characters]")
    assert caddie.fit({"b": b"\x00" * 50}) == '{"b":"<blob 50 bytes>"}'


def test_query_sql_hides_the_import_summary_and_hash(ctx):
    conn = connect(ctx.conn.execute("PRAGMA database_list").fetchone()[2])
    conn.execute("UPDATE imports SET summary = '{\"account_fp\": \"fp-abcdef\", \"file\": \"18Birdies_archive.json\"}'")
    conn.commit()
    conn.close()
    out, _ = ctx.run("query_sql", {"sql": "SELECT * FROM imports"})
    blob = json.dumps(out)
    assert out["rows"] and "fp-abcdef" not in blob and "18Birdies_archive" not in blob and "/Users/" not in blob
    assert "abc" not in [v for row in out["rows"] for v in row]                           # sha256 hidden too


def test_tool_errors_read_plainly_on_the_page(ctx):
    out, act = ctx.run("list_rounds", {"limit": "many"})
    assert "invalid literal" in out["error"] and "invalid literal" not in act and act.startswith("list_rounds: ")


def test_a_long_earlier_answer_is_cut_not_refused():
    long = "x" * (caddie.MAX_ASSISTANT_CHARS + 5000)
    msgs = caddie.validate_messages([{"role": "user", "content": "q"}, {"role": "assistant", "content": long},
                                     {"role": "user", "content": "and?"}])
    assert len(msgs[1]["content"]) <= caddie.MAX_ASSISTANT_CHARS and msgs[1]["content"].endswith(
        "cut to keep the conversation short]")


def test_one_question_too_long_says_so(cfg):
    demo(cfg)
    events = run_ask(cfg, FakeOpenAI(api_error(openai.APIStatusError, 413)))
    assert events[-1]["message"] == caddie.TOO_LONG_QUESTION
    events = list(caddie.ask(cfg, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                   {"role": "user", "content": "c"}], key_reader=lambda: FAKE_KEY,
                             client_factory=FakeOpenAI(api_error(openai.APIStatusError, 413)).factory, today=TODAY))
    assert events[-1]["message"] == caddie.TOO_LONG_CONVERSATION


def test_the_prompt_describes_only_the_csv_columns(cfg):
    demo(cfg)
    conn = connect(cfg.db_path)
    bundle = chat_bundle(conn, cfg, today=TODAY)
    conn.close()
    prompt = caddie.system_prompt(bundle, mode="native", max_lookups=6)
    described = prompt.split("Rounds table columns: ", 1)[1].split("\n", 1)[0]
    assert [c.split(" (")[0] for c in described.split("; ")] == caddie.ROUND_COLS
    header = prompt.split("as CSV:\n", 1)[1].split("\n", 1)[0]
    assert header.split(",") == caddie.ROUND_COLS
    assert "differential_kind" not in described and "tee (" not in described
    assert prompt.lower().count("unofficial (") <= 1 and "never by alias" in prompt


def test_the_card_counts_lessons_for_the_starter_chips(cfg):
    from golf.web.app import CADDIE_LESSON_CHIP, CADDIE_NO_LESSON_CHIP, caddie_suggestions

    demo(cfg)
    conn = connect(cfg.db_path)
    card = caddie.card_data(conn, cfg, today=TODAY)
    conn.close()
    assert isinstance(card["lessons"], int)
    assert CADDIE_LESSON_CHIP not in caddie_suggestions(0) and CADDIE_NO_LESSON_CHIP in caddie_suggestions(0)
    assert CADDIE_LESSON_CHIP in caddie_suggestions(2) and len(caddie_suggestions(0)) == len(caddie_suggestions(2))
