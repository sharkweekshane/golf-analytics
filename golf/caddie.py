"""The Caddie: chat about Shane's golf inside the local web app (/caddie) and in Terminal (`golf caddie ask`).

Questions are answered by Meta's Muse Spark model through the Meta Model API with Shane's own key, which
golf.secrets reads from the macOS Keychain at call time (never from a file, never logged, never sent to
the browser). The model looks things up with read-only tools that reuse golf.mcp_server's functions, so it
sees exactly what the MCP server shows Claude Desktop, with GPS and whole note bodies hidden, plus a few
more columns kept on the Mac (file paths, the raw export record).

How a question runs (research: docs/ notes in the README's Caddie section):
- Responses API (`POST {base_url}/responses`) through the OpenAI SDK, stateless: `store: false` on every
  call (Meta keeps no copy of the conversation), and the instructions are resent each time.
- Native tool calling: the model returns function_call items; they run here, and their results go back
  as function_call_output items. At most [caddie].max_tool_rounds rounds of lookups per question (and
  about TOOL_BUDGET_CHARS characters of tool output); after that the next call is the last one and is
  sent with tool_choice "none", so the model has to answer in text.
- If Meta rejects the request shape (HTTP 400) on a question's first call, the Caddie degrades and
  remembers it: first without the replayed encrypted reasoning, then to the documented fallback, a JSON
  "lookup" protocol with no tools parameter at all. `tools = "lookup"` in config.toml starts there. Later
  in a question nothing restarts: a 400 on a call that replayed reasoning drops the reasoning and carries
  on with the lookups already made; any other 400 is an error.
- The key goes only to api.meta.ai (or loopback, for tests) unless [caddie].allow_any_base_url = true,
  and the OpenAI SDK is stripped of what it reads from the environment (OPENAI_ORG_ID, OPENAI_PROJECT_ID,
  OPENAI_CUSTOM_HEADERS), so nothing but the request and its Authorization header goes to Meta.
- No SDK retries: a timed-out generation is not re-sent (it may still be running, and billing). One retry,
  after a short wait, for a rate limit, a 500/502/503 or a failed connection.
- Round ids are the same aliases as the claude.ai chat page (r1 = oldest): 18Birdies' own ids encode
  when a round was created, so they stay on the Mac. Tool inputs accept aliases; outputs are aliased.
- Every API call's token counts, model and latency go to llm_calls (purpose 'caddie'). Never the key,
  the prompt or the answer.
- API errors become CaddieError with a fixed, friendly message per case (no key, key rejected, out of
  credits, rate limited, network...). Meta's own error text is never shown or logged: an auth error can
  echo part of the key.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from golf import mcp_server as mcp
from golf import secrets
from golf.config import Config
from golf.db import connect, dumps, now_iso

log = logging.getLogger(__name__)

SECRET_NAME = "muse-spark"
PURPOSE = "caddie"
PROMPT_VERSION = "caddie-1"
PROVIDERS = ("muse",)
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.1"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
TOOL_MODES = ("auto", "native", "lookup")

MAX_TURNS = 16                 # messages kept per request (user + assistant), oldest dropped first
MAX_MESSAGES_IN = 200
MAX_USER_CHARS = 4000
MAX_ASSISTANT_CHARS = 20000
TOOL_RESULT_CHARS = 20000      # each tool result sent back to the model
MAX_CALLS_PER_STEP = 8         # parallel calls answered in one step; more get an error result
TOOL_BUDGET_CHARS = 80_000     # tool output per question (everything is resent on every call); then answer
QUERY_SECONDS = mcp.QUERY_SECONDS   # query_sql is interrupted after this long (a runaway recursive CTE)
MAX_VALUE_CHARS = 4000         # one text value in a query_sql row; longer ones are cut
PROMPT_ROUNDS = 120            # most recent rounds put in the prompt as CSV
MAX_ACTIVITY_CHARS = 120
RETRY_MAX_WAIT = 10.0          # seconds; a longer Retry-After is reported instead of waited out
ALLOWED_API_HOSTS = frozenset({"api.meta.ai"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# $ per million tokens: (input, cached input, output). Standard tier, dev.meta.ai/docs/pricing-rate-limits
# (read 2026-09-26); reasoning tokens bill as output. An unknown model id is logged with cost NULL.
PRICES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.1": (1.25, 0.15, 4.25),
    "muse-spark-1.2": (1.25, 0.15, 4.25),
    "muse-spark-1.3": (1.25, 0.15, 4.25),
}
_PRICE_SUFFIX = re.compile(r"(-\d{8}|-latest)$")

# Columns kept on the Mac on top of the MCP server's (GPS, note bodies, extraction JSON): local file paths,
# Keychain-free meta values (account fingerprint, scan logs with paths) and the raw export record.
# imports.summary holds the export's account fingerprint and file name; imports.sha256 the file's hash.
EXTRA_HIDDEN = {
    ("rounds", "raw"), ("imports", "path"), ("imports", "summary"), ("imports", "sha256"),
    ("source_docs", "external_id"), ("extractions", "image_paths"), ("meta", "value"),
}
# SQL functions that turn a few characters of query into huge values (a 1 GB zeroblob): refused.
DENIED_FUNCTIONS = frozenset({"zeroblob", "randomblob"})

# Tests inject a fake OpenAI client factory here (the CLI has no other seam); None = openai.OpenAI.
CLIENT_FACTORY: Callable[..., Any] | None = None
SLEEP: Callable[[float], None] = time.sleep     # the wait before the one retry (tests replace it)
# Request shape that worked last, per (base_url, model, tools setting): 0 tools + encrypted reasoning,
# 1 tools without reasoning replay, 2 JSON lookup protocol. Remembered only after a success.
_LEVELS: dict[tuple[str, str, str], int] = {}
LEVEL_NAMES = {0: "native", 1: "native-plain", 2: "lookup"}

# ================================================================ settings
@dataclass(frozen=True)
class CaddieSettings:
    provider: str = "muse"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    effort: str = "low"
    tools: str = "auto"
    max_tool_rounds: int = 6
    max_output_tokens: int = 8000
    timeout_seconds: float = 120.0
    allow_any_base_url: bool = False


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def settings(cfg: Config) -> CaddieSettings:
    """[caddie] from config.toml, read fresh on every question (edits apply without a restart)."""
    from golf.watch import read_section

    raw = read_section(cfg, "caddie")
    try:
        timeout = max(10.0, min(600.0, float(raw.get("timeout_seconds", 120))))
    except (TypeError, ValueError):
        timeout = 120.0
    return CaddieSettings(
        provider=str(raw.get("provider", "muse")).strip().lower(),
        base_url=str(raw.get("base_url", DEFAULT_BASE_URL)).strip().rstrip("/"),
        model=str(raw.get("model", DEFAULT_MODEL)).strip(),
        effort=str(raw.get("effort", "low")).strip().lower(),
        tools=str(raw.get("tools", "auto")).strip().lower(),
        max_tool_rounds=_int(raw.get("max_tool_rounds"), 6, 1, 12),
        max_output_tokens=_int(raw.get("max_output_tokens"), 8000, 256, 64000),
        timeout_seconds=timeout,
        allow_any_base_url=raw.get("allow_any_base_url") is True,
    )


def settings_problems(st: CaddieSettings) -> list[str]:
    """What is wrong with [caddie] (empty = fine). Checked before any key is read or request is sent."""
    out = []
    if st.provider not in PROVIDERS:
        out.append(f"[caddie] provider is {st.provider!r}; the only provider is \"muse\".")
    try:
        parts = urlsplit(st.base_url)
        host = (parts.hostname or "").lower()
    except ValueError:
        parts, host = urlsplit(""), ""
    loopback = host in LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        out.append(f"[caddie] base_url must be an https:// URL (the key goes in its Authorization header); "
                   f"got {st.base_url!r}.")
    elif not (host in ALLOWED_API_HOSTS or loopback or st.allow_any_base_url):
        out.append(f"[caddie] base_url {st.base_url!r} is not Meta's API: the key only goes to "
                   f"{', '.join(sorted(ALLOWED_API_HOSTS))}. If you really mean another host, also set "
                   "allow_any_base_url = true under [caddie].")
    if not st.model:
        out.append("[caddie] model is empty.")
    elif "contributor" in st.model.lower():
        out.append(f"[caddie] model is {st.model!r}: contributor-tier models let Meta train on your prompts and "
                   "outputs, so the Caddie refuses them. Use a Standard model such as muse-spark-1.1.")
    if st.effort not in EFFORTS:
        out.append(f"[caddie] effort is {st.effort!r}; choose one of {', '.join(EFFORTS)}.")
    if st.tools not in TOOL_MODES:
        out.append(f"[caddie] tools is {st.tools!r}; choose one of {', '.join(TOOL_MODES)}.")
    return out


# ================================================================ errors
NO_KEY_MESSAGE = ("No Muse Spark API key yet. Add it to your macOS Keychain with this command in Terminal (it asks "
                  f"for the key, then asks you to retype it): {secrets.SETUP_COMMAND}  Then ask again. "
                  "golf caddie key explains.")
TOO_LONG_CONVERSATION = "The conversation is too long for one request. Clear the chat and ask again."
TOO_LONG_QUESTION = ("That question needed more data than fits in one request. Ask about a shorter date range or "
                     "fewer rounds.")
FIX_KEY_CODES = {"no_key", "key_rejected", "keychain", "forbidden", "config", "model_not_found", "no_data"}
TRANSIENT_CODES = {"network", "timeout", "rate_limited", "unavailable"}
OUT_OF_LOOKUPS = "The Caddie ran out of lookups before it could answer. Ask a narrower question."


class CaddieError(RuntimeError):
    """A question could not be answered. `code` is stable (the web page keys on it); the message is written
    for Shane and never contains the key or Meta's own error text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_CODES


class _Degrade(Exception):
    """Meta rejected this request shape (HTTP 400): try the next, simpler one."""


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,64}$")
_NOT_A_SHAPE_PROBLEM = {"content_policy_violation", "context_length_exceeded", "payload_too_large",
                        "model_not_found", "invalid_api_key", "string_above_max_length"}
_CREDIT_WORDS = ("credit", "quota", "billing", "balance", "insufficient")
# What a 400 message may quote back from Meta's error body. Anything else (an underscore-form key fits the
# plain-identifier pattern) is used to classify the error but never shown.
KNOWN_ERROR_CODES = frozenset({
    "invalid_value", "invalid_type", "invalid_request_error", "invalid_json", "unsupported_parameter",
    "unsupported_value", "unknown_parameter", "missing_required_parameter", "integer_below_min_value",
    "integer_above_max_value", "array_above_max_length", "string_above_max_length", "empty_array",
    "invalid_function_parameters", "invalid_encrypted_content", "unsupported_model",
})
_REQUEST_FIELDS = ("model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "store",
                   "include", "reasoning", "max_output_tokens", "text", "metadata")
_SUB_FIELDS = ("type", "role", "content", "name", "arguments", "call_id", "output", "encrypted_content", "summary",
               "parameters", "strict", "description", "effort", "format", "text", "id", "status")
_PARAM_PATH = re.compile(rf"^(?:{'|'.join(_REQUEST_FIELDS)})(?:\[\d{{1,4}}\])?"
                         rf"(?:\.(?:{'|'.join(_SUB_FIELDS)})(?:\[\d{{1,4}}\])?){{0,3}}$")


def _safe(token: Any) -> str | None:
    """An error code / param from Meta's error body, only if it is a plain identifier (never free text).
    For classifying the error only: _shown_code / _shown_param decide what may appear in a message."""
    text = str(token) if token is not None else ""
    return text if _SAFE_TOKEN.match(text) else None


def _shown_code(code: str | None) -> str | None:
    return code if code in KNOWN_ERROR_CODES else None


def _shown_param(param: str | None) -> str | None:
    return param if param and _PARAM_PATH.match(param) else None


def map_api_error(e: BaseException, st: CaddieSettings) -> CaddieError:
    """A fixed message per failure. Nothing from the exception's message is used: an auth error from an
    OpenAI-compatible API can echo part of the key it rejected."""
    import openai

    code = _safe(getattr(e, "code", None))
    param = _safe(getattr(e, "param", None))
    if isinstance(e, openai.APITimeoutError):
        return CaddieError("timeout", f"No answer from Meta's API within {int(st.timeout_seconds)} seconds. Ask a "
                                      "narrower question, or try again ([caddie].timeout_seconds sets the limit).")
    if isinstance(e, openai.APIConnectionError):
        host = urlsplit(st.base_url).hostname or "the API"
        return CaddieError("network", f"Couldn't reach {host} (no connection, or it dropped). Check the internet "
                                      "connection and ask again.")
    status = getattr(e, "status_code", None)
    if not isinstance(status, int):
        return CaddieError("error", f"The Muse Spark call failed ({type(e).__name__}). Ask again.")
    if status == 401:
        return CaddieError("key_rejected", "Meta rejected the API key (401). Copy it again from the API keys tab at "
                                           "dev.meta.ai and store it exactly as copied, in Terminal: "
                                           f"{secrets.SETUP_COMMAND}  Then ask again.")
    if status == 402 or (status == 429 and code and any(w in code.lower() for w in _CREDIT_WORDS)):
        return CaddieError("no_credits", "Your Meta Model API account is out of credits. Add credits in the billing "
                                         "settings at dev.meta.ai, then ask again.")
    if status == 429:
        return CaddieError("rate_limited", "Meta's API is rate-limiting this key right now (429). Wait a minute and "
                                           "ask again.")
    if status == 403:
        return CaddieError("forbidden", f"This key isn't allowed to use {st.model} (403). Check the key's access at "
                                        "dev.meta.ai, or [caddie].model in config.toml.")
    if status == 404 or code == "model_not_found":
        return CaddieError("model_not_found", f"Meta's API doesn't offer the model {st.model!r} to this key (404). "
                                              "Set [caddie].model in config.toml (default muse-spark-1.1).")
    if status == 413 or code in ("context_length_exceeded", "payload_too_large", "string_above_max_length"):
        return CaddieError("too_long", TOO_LONG_CONVERSATION)
    if code == "content_policy_violation":
        return CaddieError("refused", "Muse Spark declined to answer that one. Try asking it differently.")
    if status == 504:
        return CaddieError("timeout", "Meta's API took too long to answer (504). Ask a narrower question, or try "
                                      "again.")
    if status >= 500:
        return CaddieError("unavailable", f"Meta's API is having problems right now ({status}). Try again in a "
                                          "minute.")
    shown_code, shown_param = _shown_code(code), _shown_param(param)
    detail = ", ".join(x for x in (shown_code, f"param {shown_param}" if shown_param else None) if x)
    return CaddieError("bad_request", f"Meta's API refused the request ({status}{': ' + detail if detail else ''}). "
                                      "If it keeps happening, set tools = \"lookup\" under [caddie] in config.toml.")


def scrub(text: str, key: str | None) -> str:
    """Belt and braces: the key and each |-separated part of it (8+ characters) blanked out of `text`."""
    if not key or not text:
        return text
    for part in sorted({key, *[p for p in re.split(r"[|:_]", key) if len(p) >= 8]}, key=len, reverse=True):
        text = text.replace(part, "[redacted]")
    return text


# ================================================================ messages from the page / CLI
CUT_MARKER = "\n\n[... the rest of this earlier answer was cut to keep the conversation short]"


def validate_messages(raw: Any) -> list[dict[str, str]]:
    """[{role, content}] as the page keeps it: roles user/assistant, text only, the last one the question.
    Trimmed to the last MAX_TURNS messages, starting with a user message. An earlier answer longer than
    MAX_ASSISTANT_CHARS is cut (never refused: an answer the Caddie wrote must not block the next question)."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages must be a non-empty list of {role, content}.")
    if len(raw) > MAX_MESSAGES_IN:
        raise ValueError(f"Too many messages ({len(raw)}); clear the chat.")
    out = []
    for m in raw:
        if not isinstance(m, dict):
            raise ValueError("Each message must be an object {role, content}.")
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            raise ValueError("A message role must be \"user\" or \"assistant\".")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Each message needs some text.")
        content = content.strip()
        if role == "user" and len(content) > MAX_USER_CHARS:
            raise ValueError(f"That question is longer than {MAX_USER_CHARS} characters.")
        if role == "assistant" and len(content) > MAX_ASSISTANT_CHARS:
            content = content[: MAX_ASSISTANT_CHARS - len(CUT_MARKER)].rstrip() + CUT_MARKER
        out.append({"role": role, "content": content})
    if out[-1]["role"] != "user":
        raise ValueError("The last message must be your question (role \"user\").")
    out = out[-MAX_TURNS:]
    while out[0]["role"] != "user":
        out.pop(0)
    return out


# ================================================================ tools
TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "list_rounds",
     "description": "Counted rounds, newest first: date, course, holes, nine, gross, to-par, putts (putts_tracked = "
                    "1 when every hole has putts), fairways and greens hit/chances, birdies/pars/bogeys/doubles, the "
                    "unofficial differential and Handicap Index after the round. Dates are YYYY-MM-DD, inclusive.",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string", "description": "first date, YYYY-MM-DD"},
         "end": {"type": "string", "description": "last date, YYYY-MM-DD"},
         "limit": {"type": "integer", "description": "max rounds (default 30, max 200)"}}}},
    {"name": "get_round",
     "description": "One round in full: every hole (strokes, par, stroke index, putts, fairway, GIR, penalties, "
                    "chips, sand), the tee's rating/slope, the handicap record and notes linked to that day. Look it "
                    "up by alias (r12) or by date (YYYY-MM-DD; a date with two rounds returns both).",
     "parameters": {"type": "object", "properties": {
         "round": {"type": "string", "description": "round alias like r12, or a date YYYY-MM-DD"}},
         "required": ["round"]}},
    {"name": "trend",
     "description": "One metric per round, oldest first, with a trailing mean over `window` rounds and the change "
                    "from the first window to the last. Descriptive only.",
     "parameters": {"type": "object", "properties": {
         "metric": {"type": "string", "enum": list(mcp.TREND_METRICS)},
         "window": {"type": "integer", "description": "rounds in the trailing mean (default 5)"}},
         "required": ["metric"]}},
    {"name": "timeline",
     "description": "Accepted lessons, practice, equipment changes, injuries, goals and swing thoughts from Shane's "
                    "notes, oldest first, with coach, focus areas, drills and the quoted source line.",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string", "description": "YYYY-MM-DD"},
         "end": {"type": "string", "description": "YYYY-MM-DD"},
         "event_type": {"type": "string", "description": "e.g. lesson, practice, equipment_change, injury"}}}},
    {"name": "lesson_effects",
     "description": "Before/after score comparison around each lesson with bootstrap intervals, the minimum "
                    "detectable effect for Shane's own round-to-round spread, focus-matched stats and caveats "
                    "(regression to the mean, equipment changes, off-season gaps). Descriptive, never causal.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "club_distances",
     "description": "Club distance summary from GPS shot tracking: per club the number of shots, median and middle "
                    "half (q1-q3) in yards, plus the median of live-tracked shots only (more reliable than shots "
                    "tapped on the map after the round). Distances run to the next shot, so they include roll.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "db_schema",
     "description": "The golf.db schema (tables, columns, views such as v_rounds, round_holes, shots, timeline_raw), "
                    "for writing query_sql statements.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "query_sql",
     "description": "Run ONE read-only SQLite SELECT (or WITH ... SELECT) on golf.db; anything else is refused. Prefer "
                    "v_rounds over rounds (overrides applied, deleted/abandoned rounds dropped). GPS coordinates, "
                    "note bodies and file paths read as NULL. Round ids come back as aliases r1..rN, and an alias "
                    "in a string literal (WHERE round_id = 'r12') is matched to the real id.",
     "parameters": {"type": "object", "properties": {
         "sql": {"type": "string"},
         "limit": {"type": "integer", "description": "max rows (default 200, max 500)"}},
         "required": ["sql"]}},
]
TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


def responses_tools() -> list[dict[str, Any]]:
    """TOOL_SPECS in the Responses API's flat function format (strict off: the schemas are loose)."""
    return [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"],
             "strict": False} for t in TOOL_SPECS]


def _alias_maps(conn: sqlite3.Connection) -> dict[str, str]:
    """Real round id -> alias, numbered like golf.chat.chat_bundle (r1 = oldest counted round)."""
    from golf.analytics.stats import round_facts

    return {f["round_id"]: f"r{i}" for i, f in enumerate(round_facts(conn), start=1)}


def _authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_READ and (arg1, arg2) in EXTRA_HIDDEN:
        return sqlite3.SQLITE_IGNORE
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return mcp._authorizer(action, arg1, arg2, db, trigger)


def _value(v: Any) -> Any:
    """One query_sql cell for the model: a blob as a short placeholder (never str(bytes), 4x its size) and
    a very long text cut."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob {len(v)} bytes>"
    if isinstance(v, str) and len(v) > MAX_VALUE_CHARS:
        return v[:MAX_VALUE_CHARS] + f"...[cut: {len(v)} characters]"
    return v


def query_sql(conn: sqlite3.Connection, sql: str, limit: int = 200, *, seconds: float = QUERY_SECONDS) -> dict[str, Any]:
    """golf.mcp_server.query_sql (one read-only SELECT, interrupted after `seconds`) with EXTRA_HIDDEN also
    read as NULL, a few blob-making functions refused, and very long text values cut. Same shape."""
    out = mcp.query_sql(conn, sql, limit, authorizer=_authorizer, seconds=seconds)
    out["rows"] = [[_value(v) for v in r] for r in out["rows"]]
    return out


def _fmt_day(iso: Any) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "?")
    return f"{d:%b} {d.day}"


class ToolContext:
    """What the tools run against for one question: golf.db opened read-only, the alias map, the chat bundle."""

    def __init__(self, conn: sqlite3.Connection, bundle: dict[str, Any]):
        self.conn = conn
        self.bundle = bundle
        self.to_alias = _alias_maps(conn)
        self.from_alias = {v: k for k, v in self.to_alias.items()}

    # -------------------------------------------------------- aliasing
    def alias_out(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.to_alias.get(obj, obj)
        if isinstance(obj, dict):
            return {k: self.alias_out(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.alias_out(v) for v in obj]
        return obj

    def alias_text(self, text: str) -> str:
        for real, alias in self.to_alias.items():
            if real in text:
                text = text.replace(real, alias)
        return text

    def real_id(self, ref: str) -> str:
        return self.from_alias.get(ref.strip(), ref.strip())

    def sql_in(self, sql: str) -> str:
        def swap(m: re.Match) -> str:
            real = self.from_alias.get(m.group(1))
            return "'" + real.replace("'", "''") + "'" if real else m.group(0)

        return re.sub(r"'(r\d{1,5})'", swap, sql)

    # -------------------------------------------------------- dispatch
    def run(self, name: str, arguments: Any) -> tuple[dict[str, Any], str]:
        """(result, activity line). Bad input or a refused query comes back as {"error": ...} for the model
        to read and correct; it never raises."""
        if name not in TOOL_NAMES:
            return {"error": f"Unknown tool {name!r}. Tools: {', '.join(TOOL_NAMES)}."}, f"asked for an unknown tool {name}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except json.JSONDecodeError:
            return {"error": "The arguments were not valid JSON."}, f"{name}: arguments were not valid JSON"
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {"error": "The arguments must be a JSON object."}, f"{name}: arguments were not an object"
        try:
            result, activity = getattr(self, "_t_" + name)(**_known_args(name, args))
        except (ValueError, KeyError, TypeError) as e:
            # The model gets the detail to correct itself; the page gets a plain line, not Python internals.
            msg = self.alias_text(str(e.args[0]) if e.args else type(e).__name__)
            return {"error": msg}, f"{name}: that lookup didn't work; the Caddie can try another way"
        except sqlite3.Error as e:
            return {"error": f"SQLite: {e}"}, f"{name}: database error"
        except Exception as e:             # a bug in a tool must not end the conversation
            log.warning("caddie tool %s failed: %s", name, type(e).__name__)
            return {"error": f"The {name} tool failed ({type(e).__name__})."}, f"{name}: failed"
        return self.alias_out(result), activity

    def _t_list_rounds(self, start: str | None = None, end: str | None = None, limit: int = 30):
        out = mcp.list_rounds(self.conn, start or None, end or None, max(1, min(int(limit), 200)))
        span = f" ({start or 'first'} to {end or 'latest'})" if start or end else ""
        return out, f"looked up {out['count']} round{'s' if out['count'] != 1 else ''}{span}"

    def _t_get_round(self, round: str = "", **_: Any):
        ref = str(round or "").strip()
        if not ref:
            raise ValueError("Give round: an alias like r12, or a date YYYY-MM-DD.")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref):
            ids = [r["round_id"] for r in mcp.list_rounds(self.conn, ref, ref, 5)["rounds"]]
            if not ids:
                raise KeyError(f"No counted round on {ref} (see list_rounds).")
            found = [mcp.get_round(self.conn, rid) for rid in ids[:3]]
            out = found[0] if len(found) == 1 else {"rounds": found, "note": f"{len(found)} rounds on {ref}."}
        else:
            found = [mcp.get_round(self.conn, self.real_id(ref))]
            out = found[0]
        where = ", ".join(f"{_fmt_day(f['round'].get('played_on_local'))} at {f['round'].get('course') or '?'}"
                          for f in found)
        return out, f"opened the round of {where}"

    def _t_trend(self, metric: str = "outcome", window: int = 5):
        out = mcp.trend(self.conn, metric, int(window))
        return out, f"trend of {metric} over {out['n']} rounds"

    def _t_timeline(self, start: str | None = None, end: str | None = None, event_type: str | None = None):
        out = mcp.timeline_events(self.conn, start or None, end or None, str(event_type) if event_type else None)
        kind = f" {str(event_type).replace('_', ' ')}" if event_type else ""
        return out, f"read {out['count']}{kind} timeline event{'s' if out['count'] != 1 else ''}"

    def _t_lesson_effects(self):
        out = mcp.lesson_effects(self.conn)
        n = len(out.get("lessons") or [])
        return out, f"compared before/after for {n} lesson{'s' if n != 1 else ''}"

    def _t_club_distances(self):
        clubs = (self.bundle.get("headline") or {}).get("club_medians") or []
        out = {"clubs": clubs, "note": "Yards. median/q1/q3 over every usable shot; median_live over shots tracked "
                                       "during play (n_live); n_after = tapped on the map after the round (less "
                                       "accurate). GPS distance to the next shot: roll included, not carry."}
        return out, f"read club distances ({len(clubs)} club{'s' if len(clubs) != 1 else ''})"

    def _t_db_schema(self):
        return {"schema": mcp.db_schema()}, "read the database layout"

    def _t_query_sql(self, sql: str = "", limit: int = 200):
        out = query_sql(self.conn, self.sql_in(str(sql or "")), int(limit))
        return out, f"ran a query: {out['row_count']} row{'s' if out['row_count'] != 1 else ''}" + (
            " (cut off)" if out["truncated"] else "")


_TOOL_PARAMS = {t["name"]: set(t["parameters"].get("properties", {})) for t in TOOL_SPECS}


def _known_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Only the parameters a tool declares (a stray argument from the model is dropped, not an error);
    JSON nulls count as 'not given'."""
    return {k: v for k, v in args.items() if k in _TOOL_PARAMS[name] and v is not None}


def _json_default(o: Any) -> str:
    if isinstance(o, (bytes, bytearray, memoryview)):
        return f"<blob {len(o)} bytes>"
    return str(o)


def fit(result: Any, cap: int = TOOL_RESULT_CHARS) -> str:
    """JSON for the model, at most `cap` characters: the biggest list is halved until it fits (and the result
    says so); anything still too long is cut with a marker."""
    def enc(x: Any) -> str:
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=_json_default)

    text = enc(result)
    if len(text) <= cap:
        return text
    if isinstance(result, dict):
        data = dict(result)
        for _ in range(16):
            lists = [k for k, v in data.items() if isinstance(v, list) and len(v) > 1]
            if not lists:
                break
            key = max(lists, key=lambda k: len(enc(data[k])))
            data[key] = data[key][: len(data[key]) // 2]
            data["truncated"] = True
            data["truncated_note"] = (f"'{key}' was cut to its first {len(data[key])} items to fit; narrow the "
                                      "request (dates, filters, limit) for the rest.")
            text = enc(data)
            if len(text) <= cap:
                return text
    marker = f" ...[cut: the result is longer than {cap} characters; narrow the request]"
    return text[: cap - len(marker)] + marker


# ================================================================ the prompt
def _csv(rows: list[dict[str, Any]], cols: list[str]) -> str:
    def esc(v: Any) -> str:
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"' if re.search(r'[",\n]', s) else s

    return "\n".join([",".join(cols)] + [",".join(esc(r.get(c)) for c in cols) for r in rows])


ROUND_COLS = ["round", "date", "course", "holes", "nine", "gross", "par", "to_par", "to_par_per9", "differential",
              "hi_after", "b18_handicap", "putts", "putts_partial", "fairways_hit", "fairway_chances", "gir",
              "gir_chances", "birdies", "pars", "bogeys", "double_plus", "triple_plus"]


def system_prompt(bundle: dict[str, Any], *, mode: str, max_lookups: int) -> str:
    """The Caddie's instructions: the same voice and rules as the claude.ai page (golf/chat_page.html),
    today's date, and the headline block from golf.chat.chat_bundle."""
    rounds = bundle["tables"]["rounds"]
    shown = rounds[-PROMPT_ROUNDS:]
    head = bundle.get("headline") or {}
    clubs = "; ".join(
        f"{c['club']} {round(c['median'])} (n={c['n']}"
        + (f", live {round(c['median_live'])}" if c.get("median_live") is not None and (c.get("n_live") or 0) >= 3
           else "") + ")"
        for c in head.get("club_medians") or [] if c.get("median") is not None) or "none tracked yet"
    described = bundle["dictionary"]["rounds"]            # only the columns the CSV below actually has
    rdict = "; ".join(f"{c} ({described[c]})" if c in described else c for c in ROUND_COLS)
    if mode == "lookup":
        tools_rule = ("- Look things up before you claim them (see \"Looking things up\" at the end): hole-by-hole "
                      "scoring, putting detail, club distances, lessons and notes, trends.")
    else:
        tools_rule = ("- Use the tools for anything the rounds table below doesn't cover (hole-by-hole scoring, putting "
                      "detail, club distances, lessons and notes, trends). Look it up before you claim it.")
    lines = [
        f"You are {bundle.get('player') or 'Shane'}'s golf caddie and data analyst, answering inside his personal "
        f"golf app on his Mac. Today is {bundle.get('today')}; his data runs through "
        f"{bundle.get('data_through') or 'no rounds yet'}.",
        "",
        "How to answer:",
        "- Start with the direct answer in a sentence or two, then the numbers behind it. Keep it short; use a small "
        "markdown table only when comparing several items.",
        tools_rule,
        "- Never invent numbers. If the data can't answer (for example putts weren't tracked), say what's missing.",
        f"- Compare scores per 9 holes (to_par_per9) unless he asks otherwise. He has {len(rounds)} rounds, so "
        "mention sample size when it matters, and don't claim that a lesson or change caused an improvement: the "
        "before/after comparisons are descriptive.",
        "- Write dates like \"Sep 24\". He is a beginner with a high handicap: be practical and honest, and when he "
        "asks what to work on, suggest specific beginner-friendly practice (a drill, a target, how many balls).",
        "- Round ids are aliases r1..rN (r1 = oldest), the same in the table below and in every tool. They are "
        "for lookups only: refer to rounds by date and course (like \"Sep 24 at Pine Ridge\"), never by alias.",
        "",
        "Notes about the data:", *[f"- {n}" for n in bundle.get("notes") or []],
        "",
        "Headline figures:",
        *[f"- {k.get('label')}: {k.get('value')}" + (f" ({k['note']})" if k.get("note") else "")
          for k in head.get("kpis") or []],
        *[f"- {s}" for s in (head.get("averages"), head.get("trend"), head.get("scoring_split"),
                              head.get("lesson_sensitivity")) if s],
        "",
        f"Club medians (yards): {clubs}",
        f"Lessons and notes logged: {len(bundle['tables'].get('events') or [])}.",
        "",
        f"Rounds table columns: {rdict}",
        "",
        f"Rounds ({len(shown)}{' most recent of ' + str(len(rounds)) if len(shown) < len(rounds) else ''}), oldest "
        "first, as CSV:",
        _csv(shown, ROUND_COLS) if shown else "(no rounds yet)",
    ]
    if mode == "lookup":
        lines += ["", *lookup_protocol(max_lookups)]
    return "\n".join(lines)


def lookup_protocol(max_lookups: int) -> list[str]:
    tools = []
    for t in TOOL_SPECS:
        props = t["parameters"].get("properties", {})
        req = set(t["parameters"].get("required", []))
        params = ", ".join(f"{p}{'' if p in req else '?'}" for p in props)
        tools.append(f"- {t['name']}({params}): {t['description']}")
    return [
        "Looking things up: to get data, reply with ONLY one JSON object and nothing else, like",
        '{"lookup": {"tool": "list_rounds", "args": {"start": "2026-07-01"}}}',
        f"The app runs it and replies \"LOOKUP RESULT\". You can look things up {max_lookups} times per question. "
        "When you have what you need, write the answer itself in markdown (not JSON).",
        "Tools:", *tools,
    ]


def parse_lookup(text: str) -> tuple[str, dict[str, Any]] | str | None:
    """A lookup request -> (tool, args); {"answer": ...} -> the answer text; anything else -> None (the text
    is the answer). Only the first JSON object in the text counts."""
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            lk = obj.get("lookup")
            if isinstance(lk, dict) and isinstance(lk.get("tool"), str):
                args = lk.get("args")
                return lk["tool"], args if isinstance(args, dict) else {}
            if isinstance(obj.get("answer"), str):
                return obj["answer"]
        return None
    return None


# ================================================================ the provider (Meta Model API, Responses)
@dataclass
class Step:
    text: str
    calls: list[dict[str, Any]]
    replay: list[dict[str, Any]]
    status: str
    incomplete_reason: str | None
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):              # the SDK's pydantic Response
        try:
            return obj.model_dump(warnings=False)
        except TypeError:
            return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def parse_response(resp: Any, *, keep_reasoning: bool) -> Step:
    """A Responses API result -> Step. `replay` is what goes back into `input` next time: reasoning items
    as {type, encrypted_content, summary} and function calls as {type, call_id, name, arguments}, without
    the ids and statuses that only mean something to a server that stored the response."""
    data = _as_dict(resp)
    texts, calls, replay = [], [], []
    for item in data.get("output") or []:
        kind = _get(item, "type")
        if kind == "reasoning":
            enc = _get(item, "encrypted_content")
            if keep_reasoning and enc:
                summary = [_as_dict(s) for s in _get(item, "summary") or []]
                replay.append({"type": "reasoning", "encrypted_content": enc, "summary": summary})
        elif kind == "function_call":
            args = _get(item, "arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {})
            call = {"type": "function_call", "call_id": str(_get(item, "call_id") or ""),
                    "name": str(_get(item, "name") or ""), "arguments": args or "{}"}
            calls.append(call)
            replay.append(call)
        elif kind == "message":
            txt = "".join(str(_get(p, "text") or "") for p in _get(item, "content") or []
                          if _get(p, "type") == "output_text")
            if txt:
                texts.append(txt)
                replay.append({"role": "assistant", "content": txt})
    usage = _as_dict(data.get("usage") or {})
    in_details = _as_dict(usage.get("input_tokens_details") or {})
    out_details = _as_dict(usage.get("output_tokens_details") or {})
    incomplete = _as_dict(data.get("incomplete_details") or {})
    return Step(
        text="".join(texts), calls=calls, replay=replay, status=str(data.get("status") or "completed"),
        incomplete_reason=incomplete.get("reason"), model=str(data.get("model") or ""),
        usage={"input_tokens": int(usage.get("input_tokens") or 0),
               "cached_tokens": int(in_details.get("cached_tokens") or 0),
               "output_tokens": int(usage.get("output_tokens") or 0),
               "reasoning_tokens": int(out_details.get("reasoning_tokens") or 0)},
    )


def _openai_client(*, api_key: str, base_url: str, timeout: float, max_retries: int, http_client: Any = None) -> Any:
    """The OpenAI SDK pointed at Meta (tests pass an httpx client with a mock transport).

    The SDK also reads OPENAI_ORG_ID, OPENAI_PROJECT_ID and OPENAI_CUSTOM_HEADERS from the environment and
    would add them to every request, after the auth header (a custom "Authorization:" line would even
    replace the key). All three are cleared here, so Meta gets the request and the Bearer key, nothing else
    from the environment. base_url and api_key are always passed, so OPENAI_BASE_URL/OPENAI_API_KEY don't apply."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries,
                    http_client=http_client)
    client.organization = None
    client.project = None
    client._custom_headers = {}     # OPENAI_CUSTOM_HEADERS (read into the client's default headers)
    return client


_warned_openai_log = False


def _warn_openai_log() -> None:
    global _warned_openai_log
    if os.environ.get("OPENAI_LOG") and not _warned_openai_log:
        _warned_openai_log = True
        log.warning("caddie: OPENAI_LOG is set, so the OpenAI SDK logs each Caddie request body (your golf data) "
                    "to this terminal. The Authorization header is redacted. Unset OPENAI_LOG to stop it.")


def _retry_after(e: Any) -> float | None:
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _retry_wait(e: BaseException) -> float | None:
    """Seconds to wait before the one retry of a failed call, or None: don't retry. Never a timeout (the
    generation may still be running on Meta's side, and billing) or a credit / auth / request problem."""
    import openai

    if isinstance(e, openai.APITimeoutError):
        return None
    if isinstance(e, openai.APIConnectionError):
        return 1.0
    status = getattr(e, "status_code", None)
    if status == 429:
        code = (_safe(getattr(e, "code", None)) or "").lower()
        if any(w in code for w in _CREDIT_WORDS):
            return None
    elif status not in (500, 502, 503):
        return None
    wait = _retry_after(e)
    if wait is None:
        return 2.0
    return wait if wait <= RETRY_MAX_WAIT else None


class MuseProvider:
    """Muse Spark over the Meta Model API's Responses endpoint. One instance per question."""

    name = "muse"

    def __init__(self, st: CaddieSettings, client_factory: Callable[..., Any] | None = None):
        self.st = st
        self.client_factory = client_factory or CLIENT_FACTORY or _openai_client
        self.client: Any = None

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.st.base_url, self.st.model, self.st.tools)

    def levels(self) -> list[int]:
        allowed = {"auto": [0, 1, 2], "native": [0, 1], "lookup": [2]}[self.st.tools]
        start = _LEVELS.get(self.cache_key, allowed[0])
        return [lv for lv in allowed if lv >= start] or allowed[-1:]

    def open(self, api_key: str) -> None:
        # No SDK retries (it would re-send a timed-out generation, twice): create() retries once itself.
        _warn_openai_log()
        self.client = self.client_factory(api_key=api_key, base_url=self.st.base_url,
                                          timeout=self.st.timeout_seconds, max_retries=0)

    def request(self, *, instructions: str, items: list[dict[str, Any]], level: int,
                final: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.st.model, "instructions": instructions, "input": items, "store": False,
                                "max_output_tokens": self.st.max_output_tokens}
        if level < 2:
            body["tools"] = responses_tools()
            body["parallel_tool_calls"] = True
            if final:                           # the last call of a question: answer in text, no more lookups
                body["tool_choice"] = "none"
        if level == 0:
            body["include"] = ["reasoning.encrypted_content"]
            body["reasoning"] = {"effort": self.st.effort}
        return body

    def create(self, *, instructions: str, items: list[dict[str, Any]], level: int, can_degrade: bool = False,
               final: bool = False) -> Step:
        """One API call. A 400/422 raises _Degrade when a simpler request shape is left to try (and the error
        isn't about the content itself), otherwise the mapped CaddieError. A final call whose tool_choice
        Meta refuses is re-sent once without tools. A rate limit, a 500/502/503 or a failed connection is
        retried once after a short wait; a timeout never is.

        Errors are raised after the `except` block: the SDK exception (its request carries the Bearer key,
        its body may echo it) must not ride along as __context__."""
        import openai

        body = self.request(instructions=instructions, items=items, level=level, final=final)
        retried = False
        while True:
            failure: BaseException | None = None
            action, wait = None, 0.0                # None | "strip" (resend without tools) | "retry"
            t0 = time.monotonic()
            try:
                resp = self.client.responses.create(**body)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                code = _safe(getattr(e, "code", None))
                if "tool_choice" in body and code not in _NOT_A_SHAPE_PROBLEM:
                    action = "strip"
                elif can_degrade and code not in _NOT_A_SHAPE_PROBLEM:
                    failure = _Degrade()
                else:
                    failure = map_api_error(e, self.st)
            except openai.OpenAIError as e:
                failure = map_api_error(e, self.st)
                w = None if retried else _retry_wait(e)
                if w is not None:
                    action, wait = "retry", w
            if action == "strip":
                for k in ("tools", "tool_choice", "parallel_tool_calls"):
                    body.pop(k, None)
                continue
            if action == "retry":
                retried = True
                SLEEP(wait)
                continue
            if failure is not None:
                raise failure
            break
        step = parse_response(resp, keep_reasoning=level == 0)
        step.latency_ms = int((time.monotonic() - t0) * 1000)
        step.model = step.model or self.st.model
        return step


# ================================================================ usage log (llm_calls)
def price_for(model: str) -> tuple[float, float, float] | None:
    name = (model or "").strip().lower()
    while name and name not in PRICES:
        stripped = _PRICE_SUFFIX.sub("", name)
        if stripped == name:
            return None
        name = stripped
    return PRICES.get(name)


def call_cost(model: str, usage: dict[str, int]) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    pin, pcached, pout = price
    cached = min(usage.get("cached_tokens", 0), usage.get("input_tokens", 0))
    return round(((usage.get("input_tokens", 0) - cached) * pin + cached * pcached
                  + usage.get("output_tokens", 0) * pout) / 1e6, 6)


def log_call(cfg: Config, step: Step, *, question_id: str, step_no: int, mode: str) -> None:
    """One llm_calls row per API call: model, token counts, cost, latency. No key, prompt or answer."""
    u = step.usage
    cost = call_cost(step.model, u)
    meta = {"provider": "muse", "question": question_id, "step": step_no, "mode": mode,
            "latency_ms": step.latency_ms, "tool_calls": len(step.calls), "reasoning_tokens": u.get("reasoning_tokens", 0)}
    stop = "tool_calls" if step.calls else step.status + (f":{step.incomplete_reason}" if step.incomplete_reason else "")
    try:
        conn = connect(cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, request_meta,"
                " response_json, stop_reason, input_tokens, output_tokens, cache_read_tokens, cost_usd, created_at,"
                " billed_calls, cost_total_usd) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,1,?)",
                (f"caddie-{uuid.uuid4().hex}", PURPOSE, step.model, PROMPT_VERSION, mode, dumps(meta), stop,
                 u.get("input_tokens", 0) - min(u.get("cached_tokens", 0), u.get("input_tokens", 0)),
                 u.get("output_tokens", 0), u.get("cached_tokens", 0), cost, now_iso(), cost))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:          # a busy database must not lose the answer
        log.warning("caddie: could not log usage to llm_calls (%s)", type(e).__name__)


def usage_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Caddie calls so far: questions, API calls, tokens, spend (priced calls only), unpriced calls, last use."""
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT json_extract(request_meta, '$.question')), COALESCE(SUM(billed_calls), 0),"
            " COALESCE(SUM(COALESCE(cost_total_usd, cost_usd, 0)), 0), COALESCE(SUM(cost_usd IS NULL), 0),"
            " COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(output_tokens), 0),"
            " MAX(created_at) FROM llm_calls WHERE purpose = ?", (PURPOSE,)).fetchone()
    except sqlite3.Error:
        return {"questions": 0, "calls": 0, "spend_usd": 0.0, "unpriced": 0, "input_tokens": 0, "output_tokens": 0,
                "last_at": None}
    return {"questions": int(row[0] or 0), "calls": int(row[1]), "spend_usd": round(float(row[2]), 4),
            "unpriced": int(row[3]), "input_tokens": int(row[4]), "output_tokens": int(row[5]), "last_at": row[6]}


# ================================================================ key + status
def default_key_reader() -> str | None:
    """The key itself, from the Keychain (or MUSE_API_KEY). Only ask() calls this, when a question is sent."""
    return secrets.get_secret(SECRET_NAME)


def key_state(key_reader: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """{'set', 'source', 'error', 'dotenv_ignored'} for status displays and page banners: whether a key is
    there and where from, never the key. The default is a presence check (security WITHOUT -w), so loading
    /status or /caddie never pulls the secret into this process. A reader injected by tests is called."""
    from golf.config import DOTENV_IGNORED

    ignored = secrets.SECRETS[SECRET_NAME].env_var in DOTENV_IGNORED
    try:
        if key_reader is None or key_reader is default_key_reader:
            source = secrets.secret_source(SECRET_NAME)
        else:
            source = "injected reader" if key_reader() else None
    except secrets.SecretError as e:
        return {"set": False, "source": None, "error": str(e), "dotenv_ignored": ignored}
    return {"set": source is not None, "source": source, "error": None, "dotenv_ignored": ignored}


def status_info(conn: sqlite3.Connection, cfg: Config,
                key_reader: Callable[[], str | None] | None = None) -> dict[str, Any]:
    st = settings(cfg)
    ks = key_state(key_reader)
    return {"key_set": ks["set"], "key_source": ks["source"], "key_error": ks["error"],
            "key_in_dotenv_ignored": ks["dotenv_ignored"], "provider": st.provider, "model": st.model,
            "base_url": st.base_url, "tools": st.tools, "effort": st.effort,
            "mode_in_use": LEVEL_NAMES.get(_LEVELS.get((st.base_url, st.model, st.tools), -1)),
            "problems": settings_problems(st), "usage": usage_summary(conn)}


DOTENV_NOTE = ("A MUSE_API_KEY line in .env is ignored: the key belongs in the Keychain. Delete that line from .env "
               "(and rotate the key at dev.meta.ai if it was a real one).")


def format_status(info: dict[str, Any]) -> str:
    u = info["usage"]
    source = info.get("key_source")
    where = ""
    if source and source.startswith("environment"):
        where = f" (from the {source}, which overrides the Keychain)"
    elif source:
        where = f" ({source})"
    key = (f"set{where}" if info["key_set"] else
           f"not readable: {info['key_error']}" if info["key_error"] else "NOT set (golf caddie key shows the command)")
    lines = [
        f"Caddie: Muse Spark via the Meta Model API ({info['provider']}).",
        f"Key: {key}",
        f"Model: {info['model']}   base_url: {info['base_url']}   tools: {info['tools']}   effort: {info['effort']}",
        f"Used: {u['questions']} question{'s' if u['questions'] != 1 else ''}, {u['calls']} API call"
        f"{'s' if u['calls'] != 1 else ''}, {u['input_tokens']:,} tokens in / {u['output_tokens']:,} out, "
        f"${u['spend_usd']:.2f}" + (f" ({u['unpriced']} calls on a model with no known price not in that)"
                                    if u["unpriced"] else "") + ".",
    ]
    lines += [f"Config problem: {p}" for p in info["problems"]]
    if info.get("key_in_dotenv_ignored"):
        lines.append(DOTENV_NOTE)
    return "\n".join(lines)


# ================================================================ the loop
@contextmanager
def _readonly(cfg: Config) -> Iterator[sqlite3.Connection]:
    try:
        conn = mcp.open_readonly(cfg.db_path)
    except (FileNotFoundError, sqlite3.Error):
        raise CaddieError("no_data", "There's no golf data yet: run golf init, then import your 18Birdies "
                                     "export.") from None
    try:
        yield conn
    finally:
        conn.close()


def _today(cfg: Config) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


def card_data(conn: sqlite3.Connection, cfg: Config, *, today: date | None = None) -> dict[str, Any]:
    """The left-hand card of the /caddie page (the claude.ai page's card): headline KPIs, per-round
    to-par for the sparkline, club medians. Public-safe fields only."""
    from golf.chat import chat_bundle

    b = chat_bundle(conn, cfg, today=today or _today(cfg))
    head = b.get("headline") or {}
    events = b["tables"].get("events") or []
    return {"data_through": b.get("data_through"), "kpis": head.get("kpis") or [],
            "club_medians": head.get("club_medians") or [],
            "lessons": sum(1 for e in events if e.get("type") == "lesson"),
            "rounds": [{k: r.get(k) for k in ("date", "course", "holes", "gross", "to_par", "to_par_per9")}
                       for r in b["tables"]["rounds"]]}


LAST_CALL_NOTE = ("(From the app, not Shane: the lookup limit for this question is reached. Answer now, in markdown, "
                  "with what you have, and say what you couldn't check.)")


class _Session:
    """One question: the provider, the tools, the usage log and the events for the page."""

    def __init__(self, cfg: Config, st: CaddieSettings, provider: MuseProvider, ctx: ToolContext,
                 bundle: dict[str, Any], history: list[dict[str, str]]):
        self.cfg, self.st, self.provider, self.ctx, self.bundle = cfg, st, provider, ctx, bundle
        self.history = history
        self.question_id = uuid.uuid4().hex[:12]
        self.api_calls = 0
        self.lookups = 0
        self.tool_chars = 0
        self.data_budget_hit = False
        self.tokens = {"input": 0, "output": 0}
        self.truncated = False
        self.can_degrade = False            # a simpler level is left to try (set by ask())
        self.level = 0

    def call(self, *, instructions: str, items: list[dict[str, Any]], level: int, final: bool = False) -> Step:
        # Degrading restarts the question one level simpler, which is only free before anything ran: on the
        # first call. Later, the one shape problem worth surviving is Meta refusing replayed reasoning
        # (native() then drops it and carries on); every other 400 is an error.
        replays_reasoning = level == 0 and any(i.get("type") == "reasoning" for i in items)
        can_degrade = self.can_degrade and (self.api_calls == 0 or replays_reasoning)
        step = self.provider.create(instructions=instructions, items=items, level=level, can_degrade=can_degrade,
                                    final=final)
        self.api_calls += 1
        self.tokens["input"] += step.usage.get("input_tokens", 0)
        self.tokens["output"] += step.usage.get("output_tokens", 0)
        log_call(self.cfg, step, question_id=self.question_id, step_no=self.api_calls, mode=LEVEL_NAMES[level])
        if step.status == "failed":
            raise CaddieError("unavailable", "Muse Spark could not finish this answer (it failed on Meta's side). Ask "
                                             "again.")
        if step.status == "incomplete":
            if step.incomplete_reason == "content_filter":
                raise CaddieError("refused", "Meta's content filter stopped this answer. Try asking it differently.")
            if step.calls or not step.text:
                raise CaddieError("too_long", f"The answer ran past the output limit ([caddie].max_output_tokens = "
                                              f"{self.st.max_output_tokens}). Ask for less at a time, or raise it.")
            self.truncated = True
        return step

    def tool(self, name: str, arguments: Any) -> tuple[str, dict[str, Any]]:
        """(output for the model, activity event). Past TOOL_BUDGET_CHARS of tool output in this question,
        lookups are refused and the next call is the last one."""
        left = TOOL_BUDGET_CHARS - self.tool_chars
        if left <= 0:
            self.data_budget_hit = True
            return (json.dumps({"error": "The lookup data budget for this question is used up: answer now with what "
                                         "you have."}),
                    {"type": "activity", "text": f"{name}: skipped (this question's data limit is reached)"[:MAX_ACTIVITY_CHARS]})
        result, activity = self.ctx.run(name, arguments)
        self.lookups += 1
        output = fit(result, max(2000, min(TOOL_RESULT_CHARS, left)))
        self.tool_chars += len(output)
        if self.tool_chars >= TOOL_BUDGET_CHARS:
            self.data_budget_hit = True
        return output, {"type": "activity", "text": activity[:MAX_ACTIVITY_CHARS]}

    def out_of_lookups(self, rounds_used: int) -> bool:
        return rounds_used >= self.st.max_tool_rounds or self.data_budget_hit

    # -------------------------------------------------------- native tool calling
    def native(self, level: int) -> Iterator[dict[str, Any]]:
        instructions = system_prompt(self.bundle, mode="native", max_lookups=self.st.max_tool_rounds)
        items: list[dict[str, Any]] = [dict(m) for m in self.history]
        rounds_used = 0
        while True:
            final = self.out_of_lookups(rounds_used)
            try:
                step = self.call(instructions=instructions, items=items, level=level, final=final)
            except _Degrade:
                if self.api_calls == 0:
                    raise                      # nothing ran yet: ask() starts over one level simpler
                # Meta refused the replayed reasoning: keep every lookup made so far, drop the reasoning.
                level = self.level = 1
                items = [i for i in items if i.get("type") != "reasoning"]
                yield {"type": "activity", "text": "Meta refused the replayed reasoning; carrying on without it"}
                continue
            items.extend(step.replay)
            if not step.calls:
                yield {"type": "_answer", "text": step.text}
                return
            if final:                          # told to answer (tool_choice "none") and still asked to look up
                if not step.text.strip():
                    raise CaddieError("no_answer", OUT_OF_LOOKUPS)
                yield {"type": "_answer", "text": step.text}
                return
            for i, c in enumerate(step.calls):
                if i >= MAX_CALLS_PER_STEP:
                    output = json.dumps({"error": f"At most {MAX_CALLS_PER_STEP} lookups at once; ask again."})
                else:
                    output, event = self.tool(c["name"], c["arguments"])
                    yield event
                items.append({"type": "function_call_output", "call_id": c["call_id"], "output": output})
            rounds_used += 1
            if self.out_of_lookups(rounds_used):
                items.append({"role": "user", "content": LAST_CALL_NOTE})
                yield {"type": "activity", "text": "lookup limit reached; answering with what it has"}

    # -------------------------------------------------------- documented fallback: JSON lookups
    def lookup(self) -> Iterator[dict[str, Any]]:
        instructions = system_prompt(self.bundle, mode="lookup", max_lookups=self.st.max_tool_rounds)
        items: list[dict[str, Any]] = [dict(m) for m in self.history]
        used = 0
        while True:
            final = self.out_of_lookups(used)
            step = self.call(instructions=instructions, items=items, level=2)
            parsed = parse_lookup(step.text)
            if not isinstance(parsed, tuple):
                yield {"type": "_answer", "text": parsed if isinstance(parsed, str) else step.text}
                return
            if final:                          # told to answer and still asked to look up
                raise CaddieError("no_answer", OUT_OF_LOOKUPS)
            items.append({"role": "assistant", "content": step.text})
            tool, args = parsed
            output, event = self.tool(tool, args)
            yield event
            used += 1
            content = f"LOOKUP RESULT ({tool}):\n{output}"
            if self.out_of_lookups(used):
                content += "\n\nNo more lookups for this question: answer now, in markdown, with what you have."
                yield {"type": "activity", "text": "lookup limit reached; answering with what it has"}
            items.append({"role": "user", "content": content})


DEGRADE_NOTES = {1: "Meta refused that request shape; retrying without the replayed reasoning",
                 2: "Meta refused tool calling; retrying with plain JSON lookups"}


def ask(cfg: Config, messages: Any, *, key_reader: Callable[[], str | None] | None = None,
        client_factory: Callable[..., Any] | None = None, today: date | None = None,
        st: CaddieSettings | None = None) -> Iterator[dict[str, Any]]:
    """Answer the last user message, yielding events for the page / terminal:

    {"type": "activity", "text": "looked up 9 rounds"}   a tool ran
    {"type": "delta", "text": "..."}                      answer text (the whole answer, for now)
    {"type": "done", "model", "mode", "api_calls", "lookups", "input_tokens", "output_tokens", "truncated"}
    {"type": "error", "code", "message"}                  instead of delta/done; the message is for Shane

    The key is read here, at call time, and lives only in this generator's frame and the SDK client."""
    key: str | None = None
    try:
        history = validate_messages(messages)
    except ValueError as e:
        yield {"type": "error", "code": "bad_input", "message": str(e)}
        return
    try:
        st = st or settings(cfg)
        problems = settings_problems(st)
        if problems:
            raise CaddieError("config", " ".join(problems))
        keychain_problem: str | None = None
        try:
            key = (key_reader or default_key_reader)()
        except secrets.SecretError as e:
            keychain_problem = str(e)
        if keychain_problem is not None:        # raised outside the except block: no __context__
            raise CaddieError("keychain", keychain_problem)
        if not key:
            raise CaddieError("no_key", NO_KEY_MESSAGE)
        provider = MuseProvider(st, client_factory)
        provider.open(key)
        with _readonly(cfg) as ro:
            from golf.chat import chat_bundle

            bundle = chat_bundle(ro, cfg, today=today or _today(cfg))
            session = _Session(cfg, st, provider, ToolContext(ro, bundle), bundle, history)
            answer: str | None = None
            levels = provider.levels()
            for n, level in enumerate(levels):
                session.can_degrade, session.level = n < len(levels) - 1, level
                try:
                    for ev in (session.lookup() if level == 2 else session.native(level)):
                        if ev["type"] == "_answer":
                            answer = ev["text"]
                        else:
                            yield ev
                    _LEVELS[provider.cache_key] = session.level     # native() may have dropped to 1 mid-way
                    break
                except _Degrade:
                    if n == len(levels) - 1:
                        raise CaddieError("bad_request", "Meta's API refused every request shape the Caddie knows "
                                                         "(400). Check [caddie] in config.toml.") from None
                    yield {"type": "activity", "text": DEGRADE_NOTES[levels[n + 1]]}
        if not (answer or "").strip():
            raise CaddieError("empty", "No answer came back. Try a simpler or narrower question.")
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "model": st.model, "mode": LEVEL_NAMES[session.level],
               "api_calls": session.api_calls, "lookups": session.lookups, "input_tokens": session.tokens["input"],
               "output_tokens": session.tokens["output"], "truncated": session.truncated}
    except CaddieError as e:
        message = str(e)
        if message == TOO_LONG_CONVERSATION and len(history) == 1:
            message = TOO_LONG_QUESTION         # one question, no conversation to clear
        yield {"type": "error", "code": e.code, "message": scrub(message, key)}
    except Exception as e:                  # never a traceback on the page; the log gets a scrubbed one
        log.warning("caddie: unexpected %s\n%s", type(e).__name__, scrub(traceback.format_exc(), key))
        yield {"type": "error", "code": "error",
               "message": f"Something went wrong in the Caddie ({type(e).__name__}). Ask again; if it keeps "
                          "happening, look at the golf serve window."}
    finally:
        key = None                          # the SDK client holding it goes with this generator

