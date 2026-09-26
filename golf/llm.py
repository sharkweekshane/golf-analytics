"""Claude API wrapper shared by screenshot, note and notebook extraction.

- Structured output via output_config.format (JSON schema generated from our Pydantic models).
  We deliberately use messages.stream + our own lenient validation instead of messages.parse(), so an
  enum-case slip doesn't throw away the raw output, and every raw response is kept in llm_calls.
- Every call is keyed by a content hash (model, prompts, image hashes, schema, versions). A repeat of
  the same request is served from llm_calls: re-running a sync never re-bills, and tests replay offline.
- Spend: a request that is sent again (a retry after truncation, refusal or output that failed
  validation, or force=True) is billed again, so its llm_calls row counts billed_calls and accumulates
  cost_total_usd; total_spend sums that. Only answers that validate are ever served from the cache. A
  model id with no known price is logged with cost NULL and a warning, never guessed.
- API failures become LLMUnavailable (key, permission, unknown model: fix something) or its
  subclass LLMTransient (network, 429, 5xx/529 overloaded: re-run later), each with a message that
  says what to do, so callers never show a raw SDK traceback.
- Tests monkeypatch `golf.llm.SEND` to avoid the network.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .config import get_config
from .db import dumps, now_iso

log = logging.getLogger(__name__)

# $ per million tokens: (input, output, cache read). The cache-read rate is per model: 0.1x input on most,
# but $0.20 on claude-opus-5-5 (0.05x) and $0.25 on claude-fable-5-1 (0.025x), per the published price
# table (checked 2026-09-25). Cache writes (5-minute TTL, the one requested here) bill at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
}
CACHE_WRITE_MULTIPLIER = 1.25
# Suffixes that don't change the price: a dated snapshot (-20260115), "-latest", a context tag ([1m]).
_PRICE_SUFFIX = re.compile(r"(\[[^\]]*\]|-\d{8}|-latest)$")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
API_IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
MAX_IMAGE_BYTES = 4_500_000          # stay under the API's per-image size limit after base64
MAX_LONG_EDGE = 7900                 # API rejects > 8000 px on a side
RERUN_HINT = "Re-run the same command later: finished work is kept and is not billed again."


class LLMUnavailable(RuntimeError):
    """No credentials / SDK problem: the caller should tell the user how to fix it."""


class LLMTransient(LLMUnavailable):
    """The API is unreachable, rate-limited or overloaded right now; retrying later should work.

    A subclass of LLMUnavailable, so every existing `except LLMUnavailable` stops cleanly on it."""


class LLMOutputError(RuntimeError):
    """The call completed but the output is unusable (refusal, truncation, invalid JSON/schema)."""

    def __init__(self, message: str, raw_text: str = "", call_id: int | None = None):
        super().__init__(message)
        self.raw_text = raw_text
        self.call_id = call_id


@dataclass
class ImageInput:
    path: Path
    media_type: str
    data_b64: str
    sha256: str                       # of the ORIGINAL file bytes (stable dedupe key)
    width: int
    height: int


@dataclass
class SendResult:
    text: str
    stop_reason: str
    model: str
    usage: dict[str, int]


@dataclass
class LLMResult:
    parsed: BaseModel
    raw_text: str
    call_id: int
    cached: bool
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0                 # 0.0 when cached, or when the model's price is unknown (see price_known)
    price_known: bool = True


# ------------------------------------------------------------------ images
def load_image(path: Path | str, *, max_long_edge: int | None = None) -> ImageInput:
    """Read an image for the API. HEIC (iOS screenshots/photos) and other formats become PNG/JPEG;
    camera photos are EXIF-rotated; oversized images are downscaled/re-encoded."""
    from PIL import Image, ImageOps
    import pillow_heif

    pillow_heif.register_heif_opener()
    path = Path(path)
    original = path.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    img = Image.open(io.BytesIO(original))
    fmt = (img.format or "").upper()
    transposed = ImageOps.exif_transpose(img)
    needs_reencode = fmt not in API_IMAGE_TYPES or transposed is not img
    img = transposed
    limit = min(max_long_edge or MAX_LONG_EDGE, MAX_LONG_EDGE)
    if max(img.size) > limit:
        scale = limit / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        needs_reencode = True

    if not needs_reencode and len(original) <= MAX_IMAGE_BYTES:
        data, media_type = original, API_IMAGE_TYPES[fmt]
    else:
        data, media_type = _encode(img)
    return ImageInput(path, media_type, base64.standard_b64encode(data).decode(), sha, img.width, img.height)


def _encode(img) -> tuple[bytes, str]:
    from PIL import Image

    buf = io.BytesIO()
    rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    rgb.save(buf, "PNG", optimize=True)
    if buf.tell() <= MAX_IMAGE_BYTES:
        return buf.getvalue(), "image/png"
    quality, current = 92, rgb
    while True:  # photos: JPEG, shrinking until it fits
        buf = io.BytesIO()
        current.save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES or max(current.size) < 1200:
            return buf.getvalue(), "image/jpeg"
        current = current.resize((int(current.width * 0.85), int(current.height * 0.85)), Image.LANCZOS)
        quality = max(80, quality - 4)


def image_blocks(images: list[ImageInput]) -> list[dict[str, Any]]:
    """Label each image ("Image 1:") so the model can cite image indices; images before the ask."""
    blocks: list[dict[str, Any]] = []
    for i, im in enumerate(images, start=1):
        blocks.append({"type": "text", "text": f"Image {i}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": im.media_type, "data": im.data_b64}})
    return blocks


# ------------------------------------------------------------------- calls
def wire_schema(model: type[BaseModel]) -> dict[str, Any]:
    from anthropic import transform_schema

    return transform_schema(model)


def price_for(model: str | None) -> tuple[float, float, float] | None:
    """(input, output, cache read) $/Mtok for a model id, ignoring a date/-latest/[1m] suffix; None if unknown.

    Deliberately no prefix guessing: 'claude-opus-5-5' must never be priced as 'claude-opus-5'.
    """
    name = (model or "").strip().lower()
    while name and name not in PRICES:
        stripped = _PRICE_SUFFIX.sub("", name)
        if stripped == name:
            return None
        name = stripped
    return PRICES.get(name)


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    """Cost of one call, or None (with a logged warning) when the model id has no known price."""
    price = price_for(model)
    if price is None:
        log.warning("No price known for model %r: this call's cost is logged as unknown. Add it to "
                    "golf.llm.PRICES.", model)
        return None
    pin, pout, pread = price
    return round(
        (usage.get("input_tokens", 0) * pin
         + usage.get("output_tokens", 0) * pout
         + usage.get("cache_read_input_tokens", 0) * pread
         + usage.get("cache_creation_input_tokens", 0) * pin * CACHE_WRITE_MULTIPLIER) / 1e6,
        6,
    )


def _fingerprint(content: list[dict[str, Any]]) -> list[str]:
    out = []
    for block in content:
        if block.get("type") == "image":
            out.append("img:" + hashlib.sha256(block["source"]["data"].encode()).hexdigest())
        else:
            out.append("txt:" + hashlib.sha256(dumps(block).encode()).hexdigest())
    return out


def cache_key_for(*, model: str, purpose: str, system: str, content: list[dict], schema: dict,
                  prompt_version: str, schema_version: str, effort: str, extra: str = "") -> str:
    blob = dumps({
        "model": model, "purpose": purpose, "effort": effort,
        "system": hashlib.sha256(system.encode()).hexdigest(),
        "content": _fingerprint(content),
        "schema": hashlib.sha256(dumps(schema).encode()).hexdigest(),
        "prompt_version": prompt_version, "schema_version": schema_version, "extra": extra,
    })
    return hashlib.sha256(blob.encode()).hexdigest()


def _send_real(request: dict[str, Any], *, use_fallbacks: bool) -> SendResult:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise LLMUnavailable(
            "No Anthropic API key. Copy .env.example to .env and set ANTHROPIC_API_KEY "
            "(create one at https://console.anthropic.com/settings/keys)."
        )
    import anthropic

    # Pin the public endpoint so a stray ANTHROPIC_BASE_URL (e.g. from another tool) is never used.
    client = anthropic.Anthropic(base_url=os.environ.get("GOLF_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    try:
        if use_fallbacks:
            with client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **request) as stream:
                msg = stream.get_final_message()
        else:
            with client.messages.stream(**request) as stream:
                msg = stream.get_final_message()
    except anthropic.BadRequestError as e:
        if use_fallbacks and "fallback" in str(e).lower():
            return _send_real(request, use_fallbacks=False)
        raise
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable(f"Anthropic API rejected the key: {e}") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMUnavailable("The Anthropic API key is not allowed to make this request (403). Check the key's "
                             "workspace and limits at https://console.anthropic.com/settings/keys.") from e
    except anthropic.NotFoundError as e:
        raise LLMUnavailable(f"The Claude API does not know the model {request.get('model')!r} (404). Check "
                             "[llm].model in config.toml (or GOLF_MODEL); `golf status` shows the model in use.") from e
    except anthropic.RateLimitError as e:
        raise LLMTransient(f"The Claude API rate limit was reached (429). Wait a minute. {RERUN_HINT}") from e
    except anthropic.APIConnectionError as e:        # includes APITimeoutError
        raise LLMTransient("Could not reach the Claude API (no connection, or it timed out). Check the internet "
                           f"connection. {RERUN_HINT}") from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:                     # 500, 503, 504, and 529 "overloaded"
            busy = "overloaded" if e.status_code == 529 else "having problems"
            raise LLMTransient(f"The Claude API is {busy} right now ({e.status_code}). {RERUN_HINT}") from e
        raise
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    u = msg.usage
    usage = {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    return SendResult(text=text, stop_reason=str(msg.stop_reason), model=msg.model, usage=usage)


# Tests replace this with a fake: SEND(request, use_fallbacks=...) -> SendResult
SEND: Callable[..., SendResult] = _send_real


def structured_call(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    system: str,
    content: list[dict[str, Any]],
    output_model: type[BaseModel],
    prompt_version: str,
    schema_version: str,
    request_meta: dict[str, Any] | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int | None = None,
    cache_extra: str = "",
    force: bool = False,
) -> LLMResult:
    """One structured-output call, served from llm_calls when an identical request was made before.

    Raises LLMUnavailable (no key, bad model; LLMTransient when the API is unreachable or busy) or
    LLMOutputError (refusal / truncation / unparseable output; the raw text is still stored in
    llm_calls for debugging). A request sent again (force, or a retry after an unusable answer) is
    billed again: its row's billed_calls and cost_total_usd grow, cost_usd is the latest call's.
    A cached answer that no longer validates (e.g. it never did, or the output model changed) counts
    as a miss and is sent again, so one bad answer can't be replayed forever."""
    cfg = get_config()
    model = model or cfg.model
    effort = effort or cfg.effort
    schema = wire_schema(output_model)
    key = cache_key_for(model=model, purpose=purpose, system=system, content=content, schema=schema,
                        prompt_version=prompt_version, schema_version=schema_version, effort=effort,
                        extra=cache_extra)

    if not force:
        row = conn.execute(
            "SELECT call_id, response_json, model, input_tokens, output_tokens, cache_read_tokens, cost_usd, stop_reason "
            "FROM llm_calls WHERE cache_key = ?", (key,)).fetchone()
        if row and row["response_json"] and row["stop_reason"] in ("end_turn", "stop_sequence"):
            try:
                parsed = _validate(output_model, row["response_json"], row["call_id"])
            except LLMOutputError:
                parsed = None           # an unusable answer is not a cache hit: ask again (billed again)
            if parsed is not None:
                usage = {"input_tokens": row["input_tokens"] or 0, "output_tokens": row["output_tokens"] or 0,
                         "cache_read_input_tokens": row["cache_read_tokens"] or 0}
                return LLMResult(parsed, row["response_json"], row["call_id"], True, row["model"], usage, 0.0)

    request = {
        "model": model,
        "max_tokens": max_tokens or cfg.max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }
    result = SEND(request, use_fallbacks=cfg.use_fallbacks)
    cost = cost_usd(result.model, result.usage)
    conn.execute(
        "INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, request_meta, response_json,"
        " stop_reason, input_tokens, output_tokens, cache_read_tokens, cost_usd, created_at, billed_calls,"
        " cost_total_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)"
        " ON CONFLICT(cache_key) DO UPDATE SET response_json=excluded.response_json, stop_reason=excluded.stop_reason,"
        " model=excluded.model, input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,"
        " cache_read_tokens=excluded.cache_read_tokens, cost_usd=excluded.cost_usd, created_at=excluded.created_at,"
        " billed_calls=llm_calls.billed_calls + 1,"
        " cost_total_usd=COALESCE(llm_calls.cost_total_usd, llm_calls.cost_usd, 0) + COALESCE(excluded.cost_usd, 0)",
        (key, purpose, result.model, prompt_version, schema_version, dumps(request_meta or {}), result.text,
         result.stop_reason, result.usage.get("input_tokens"), result.usage.get("output_tokens"),
         result.usage.get("cache_read_input_tokens"), cost, now_iso(), cost),
    )
    conn.commit()
    call_id = conn.execute("SELECT call_id FROM llm_calls WHERE cache_key = ?", (key,)).fetchone()[0]

    if result.stop_reason == "refusal":
        raise LLMOutputError("The model declined this request (stop_reason=refusal).", result.text, call_id)
    if result.stop_reason == "max_tokens":
        raise LLMOutputError("Output was truncated (max_tokens). Raise [llm].max_tokens.", result.text, call_id)
    parsed = _validate(output_model, result.text, call_id)
    return LLMResult(parsed, result.text, call_id, False, result.model, result.usage, cost or 0.0,
                     price_known=cost is not None)


def _validate(output_model: type[BaseModel], text: str, call_id: int | None) -> BaseModel:
    try:
        return output_model.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        raise LLMOutputError(f"Model output did not match {output_model.__name__}: {e}", text, call_id) from e


def total_spend(conn: sqlite3.Connection) -> float:
    """Every billed call, including re-sent requests (calls with an unknown price count as 0)."""
    return float(conn.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_total_usd, cost_usd, 0)), 0) FROM llm_calls").fetchone()[0])


def spend_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """For status pages: total_usd, billed_calls (every call sent), requests (distinct cache keys),
    unpriced (requests whose latest call had no known price, so total_usd under-counts them)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_total_usd, cost_usd, 0)), 0), COALESCE(SUM(billed_calls), 0), COUNT(*), "
        "COALESCE(SUM(cost_usd IS NULL), 0) FROM llm_calls").fetchone()
    return {"total_usd": round(float(row[0]), 6), "billed_calls": int(row[1]), "requests": int(row[2]),
            "unpriced": int(row[3])}
