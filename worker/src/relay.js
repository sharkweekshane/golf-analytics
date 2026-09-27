// The Caddie relay: a Cloudflare Worker between Shane's GitHub Pages caddie page and Meta's Model API.
// (src/index.js is the Worker's entry point; this module holds the logic, so the tests can import its parts.
// The entry module must export nothing but the handler: workerd treats every named export as an entrypoint.)
//
// The page (sharkweekshane.github.io/golf-analytics/caddie/) runs the conversation and the golf lookups in
// the browser; this Worker only holds the Meta API key (secret MUSE_API_KEY) and forwards one Responses API
// call at a time. It answers exactly two requests: OPTIONS /chat (CORS preflight) and POST /chat.
//
// What it enforces, in order:
//   1. Origin: only ALLOWED_ORIGIN (exact match, no wildcard). Anything else gets 403 and no CORS headers.
//   2. A per-IP rate limit on every POST (brakes passcode guessing).
//   3. Configuration: both secrets set, a passcode of at least 12 characters, a Standard-tier model
//      (never *-contributor), a known effort, an https META_BASE. Otherwise 503 "not configured".
//   4. The passcode in X-Caddie-Passcode, compared in constant time (SHA-256 of both sides).
//      A correct passcode with an empty JSON object {} gets 400 "empty_request": the page uses that to
//      check a passcode without calling Meta.
//   5. The body: JSON, at most 200 KB, only {instructions, input, tools, final} with the shapes below.
//   6. A per-passcode rate limit on calls that reach Meta.
// The Worker itself sets model, store:false, reasoning.effort, max_output_tokens, tool_choice "auto" and
// parallel_tool_calls; the page cannot change them. Built-in tools (web_search and the like) are refused:
// only flat {type:"function"} tools pass. The reply is trimmed to {output, usage, status, ...}; Meta's own
// error text is never forwarded (an auth error can echo part of the key), only a fixed code and message.
// Nothing is logged: no bodies, no headers, no secrets.

export const LIMITS = Object.freeze({
  bodyBytes: 200 * 1024,
  instructionsChars: 60 * 1024,
  messageChars: 40 * 1024,
  argumentsChars: 16 * 1024,
  outputChars: 24 * 1024,
  inputItems: 200,
  tools: 12,
  descriptionChars: 4096,
  parametersChars: 16 * 1024,
  metaResponseBytes: 4 * 1024 * 1024,
  passcodeMinChars: 12,
  // Fallback limits for when a rate-limit binding is missing (wrangler dev, or a plan without it).
  // They match [[ratelimits]] in wrangler.toml.
  callsPerMinute: 20,
  requestsPerMinutePerIp: 30,
});
export const UPSTREAM_TIMEOUT_MS = 150_000;
const EFFORTS = new Set(["minimal", "low", "medium", "high", "xhigh"]);
const TOOL_NAME = /^[a-zA-Z0-9_-]{1,64}$/;
const CALL_ID = /^[\x21-\x7e]{1,256}$/;           // printable ASCII, no spaces
const SAFE_TOKEN = /^[a-z_]{1,40}$/;
const CREDIT_WORDS = ["credit", "quota", "billing", "balance", "insufficient"];
const TOP_LEVEL = new Set(["instructions", "input", "tools", "final"]);

// Fixed messages per failure. The page has its own wording for each code; these are for anything else.
export const ERRORS = Object.freeze({
  forbidden_origin: [403, "This relay only answers Shane's caddie page."],
  not_found: [404, "Not found."],
  method_not_allowed: [405, "Use POST."],
  not_configured: [503, "The caddie is not configured yet."],
  no_passcode: [401, "Enter the passcode."],
  bad_passcode: [401, "That passcode is not right."],
  too_many_requests: [429, "Too many requests in a minute. Wait a moment, then ask again."],
  unsupported_media_type: [415, "Send JSON."],
  too_large: [413, "That request is too large. Clear the chat and ask again."],
  invalid_json: [400, "The request was not valid JSON."],
  empty_request: [400, "Nothing to ask."],
  invalid_request: [400, "The request was not in the expected shape."],
  key_rejected: [502, "Meta rejected the relay's API key."],
  out_of_credits: [502, "The Meta Model API account is out of credits."],
  rate_limited: [502, "Meta's API is rate-limiting the caddie right now."],
  model_unavailable: [502, "Meta's API won't serve the configured model to this key."],
  too_long: [502, "The conversation is too long for one request."],
  bad_request: [502, "Meta's API refused the request."],
  timeout: [502, "Meta's API took too long to answer."],
  upstream_error: [502, "Meta's API is unreachable or having problems."],
  internal: [500, "The relay hit an unexpected error."],
});

class RequestError extends Error {
  constructor(code, message) {
    super(message || ERRORS[code][1]);
    this.code = code;
  }
}

// ------------------------------------------------------------------ responses
function reply(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...headers,
    },
  });
}

function failure(code, cors, message, extra = {}) {
  const [status, text] = ERRORS[code];
  return reply(status, { error: { code, message: message || text } }, { ...cors, ...extra });
}

function corsHeaders(origin) {
  return { "Access-Control-Allow-Origin": origin, Vary: "Origin" };
}

// ------------------------------------------------------------------ secrets and config
async function sha256(text) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

// Both sides are SHA-256 digests (always 32 bytes), so length never leaks and the loop never exits early.
export function sameDigest(a, b) {
  if (a.length !== b.length) return false;
  const subtle = globalThis.crypto && crypto.subtle;
  if (subtle && typeof subtle.timingSafeEqual === "function") return subtle.timingSafeEqual(a, b);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// What is wrong with the Worker's configuration (null = fine). Never includes a secret's value.
export function configProblem(env) {
  if (!String(env.ALLOWED_ORIGIN || "").trim()) return "ALLOWED_ORIGIN is not set";
  if (!String(env.MUSE_API_KEY || "").trim()) return "the Meta API key secret is not set";
  const pass = String(env.CADDIE_PASSCODE || "").trim();
  if (!pass) return "the passcode secret is not set";
  if (pass.length < LIMITS.passcodeMinChars) return `the passcode is shorter than ${LIMITS.passcodeMinChars} characters`;
  const model = String(env.MODEL || "").trim();
  if (!model) return "MODEL is not set";
  if (/contributor/i.test(model)) return "MODEL is a contributor-tier model (Meta may train on those); use a Standard one";
  if (!EFFORTS.has(String(env.EFFORT || "").trim())) return "EFFORT is not one of minimal, low, medium, high, xhigh";
  const maxOut = Number(env.MAX_OUTPUT_TOKENS);
  if (!Number.isInteger(maxOut) || maxOut < 256 || maxOut > 64000) return "MAX_OUTPUT_TOKENS must be a whole number from 256 to 64000";
  let base;
  try { base = new URL(String(env.META_BASE || "")); } catch { return "META_BASE is not a URL"; }
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname);
  if (base.username || base.password) return "META_BASE must not carry credentials";
  if (!(base.protocol === "https:" && (base.hostname === "api.meta.ai" || loopback)) && !(base.protocol === "http:" && loopback)) {
    return "META_BASE must be https://api.meta.ai/... (or a loopback address for local testing)";
  }
  return null;
}

// ------------------------------------------------------------------ rate limiting
// The Workers Rate Limiting binding when it is bound; otherwise a best-effort counter in this isolate
// (each Cloudflare location and isolate counts separately, so it is a brake, not an accounting system).
const localWindows = new Map();

export function resetLocalLimits() {
  localWindows.clear();
}

function localAllow(key, limit, now = Date.now()) {
  const since = now - 60_000;
  const hits = (localWindows.get(key) || []).filter((t) => t > since);
  if (hits.length >= limit) {
    localWindows.set(key, hits);
    return false;
  }
  hits.push(now);
  localWindows.set(key, hits);
  if (localWindows.size > 5000) {             // never grow without bound
    for (const [k, v] of localWindows) if (!v.some((t) => t > since)) localWindows.delete(k);
  }
  return true;
}

async function allow(binding, key, limit) {
  if (binding && typeof binding.limit === "function") {
    try {
      const { success } = await binding.limit({ key });
      return Boolean(success);
    } catch {
      // the binding failed: fall back to the local counter rather than let everything through
    }
  }
  return localAllow(key, limit);
}

// ------------------------------------------------------------------ the body
async function readBody(request, max) {
  const declared = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > max) throw new RequestError("too_large");
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel().catch(() => {});
      throw new RequestError("too_large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) { bytes.set(c, at); at += c.byteLength; }
  return new TextDecoder().decode(bytes);
}

const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const invalid = (message) => new RequestError("invalid_request", message);

function onlyKeys(obj, allowed, where) {
  for (const k of Object.keys(obj)) {
    if (!allowed.includes(k)) {
      throw invalid(`${where} has an unexpected field${/^[A-Za-z0-9_]{1,40}$/.test(k) ? ` "${k}"` : ""}.`);
    }
  }
}

function text(value, where, max, { allowEmpty = false } = {}) {
  if (typeof value !== "string") throw invalid(`${where} must be a string.`);
  if (!allowEmpty && !value.trim()) throw invalid(`${where} is empty.`);
  if (value.length > max) throw invalid(`${where} is longer than ${max} characters.`);
  return value;
}

function checkInput(input) {
  if (!Array.isArray(input) || !input.length) throw invalid("input must be a non-empty array.");
  if (input.length > LIMITS.inputItems) throw invalid(`input has more than ${LIMITS.inputItems} items.`);
  const calls = new Set();
  let users = 0;
  const out = input.map((item, i) => {
    const where = `input[${i}]`;
    if (!isObject(item)) throw invalid(`${where} must be an object.`);
    if ("role" in item) {
      onlyKeys(item, ["role", "content"], where);
      if (item.role !== "user" && item.role !== "assistant") throw invalid(`${where}.role must be "user" or "assistant".`);
      if (item.role === "user") users += 1;
      return { role: item.role, content: text(item.content, `${where}.content`, LIMITS.messageChars) };
    }
    if (item.type === "function_call") {
      onlyKeys(item, ["type", "call_id", "name", "arguments"], where);
      if (typeof item.call_id !== "string" || !CALL_ID.test(item.call_id)) throw invalid(`${where}.call_id is not valid.`);
      if (typeof item.name !== "string" || !TOOL_NAME.test(item.name)) throw invalid(`${where}.name is not a valid tool name.`);
      calls.add(item.call_id);
      return { type: "function_call", call_id: item.call_id, name: item.name,
               arguments: text(item.arguments, `${where}.arguments`, LIMITS.argumentsChars, { allowEmpty: true }) };
    }
    if (item.type === "function_call_output") {
      onlyKeys(item, ["type", "call_id", "output"], where);
      if (typeof item.call_id !== "string" || !calls.has(item.call_id)) {
        throw invalid(`${where}.call_id does not answer an earlier function_call.`);
      }
      return { type: "function_call_output", call_id: item.call_id,
               output: text(item.output, `${where}.output`, LIMITS.outputChars, { allowEmpty: true }) };
    }
    throw invalid(`${where} must be a {role, content} message, a function_call or a function_call_output.`);
  });
  if (!users) throw invalid("input needs at least one user message.");
  return out;
}

function checkTools(tools) {
  if (tools === undefined) return [];
  if (!Array.isArray(tools)) throw invalid("tools must be an array.");
  if (tools.length > LIMITS.tools) throw invalid(`At most ${LIMITS.tools} tools.`);
  const names = new Set();
  return tools.map((t, i) => {
    const where = `tools[${i}]`;
    if (!isObject(t)) throw invalid(`${where} must be an object.`);
    if (t.type !== "function") throw invalid(`${where}.type must be "function" (built-in tools are not allowed).`);
    onlyKeys(t, ["type", "name", "description", "parameters", "strict"], where);
    if (typeof t.name !== "string" || !TOOL_NAME.test(t.name)) throw invalid(`${where}.name is not a valid tool name.`);
    if (names.has(t.name)) throw invalid(`${where}.name is used twice.`);
    names.add(t.name);
    const description = text(t.description ?? "", `${where}.description`, LIMITS.descriptionChars, { allowEmpty: true });
    if (!isObject(t.parameters)) throw invalid(`${where}.parameters must be an object.`);
    if (JSON.stringify(t.parameters).length > LIMITS.parametersChars) throw invalid(`${where}.parameters is too large.`);
    if (t.strict !== undefined && t.strict !== false) throw invalid(`${where}.strict must be false.`);
    return { type: "function", name: t.name, description, parameters: t.parameters, strict: false };
  });
}

export function validate(body) {
  if (!isObject(body)) throw invalid("The body must be a JSON object.");
  if (!Object.keys(body).length) throw new RequestError("empty_request");
  for (const k of Object.keys(body)) {
    if (!TOP_LEVEL.has(k)) throw invalid(`Unexpected field${/^[A-Za-z0-9_]{1,40}$/.test(k) ? ` "${k}"` : ""}.`);
  }
  if (body.final !== undefined && typeof body.final !== "boolean") throw invalid("final must be true or false.");
  return {
    instructions: text(body.instructions, "instructions", LIMITS.instructionsChars),
    input: checkInput(body.input),
    tools: checkTools(body.tools),
    final: body.final === true,
  };
}

// The request Meta gets. Everything but instructions, input and tools is the Worker's choice.
export function metaRequest(req, env) {
  const body = {
    model: String(env.MODEL).trim(),
    instructions: req.instructions,
    input: req.input,
    store: false,
    reasoning: { effort: String(env.EFFORT).trim() },
    max_output_tokens: Number(env.MAX_OUTPUT_TOKENS),
  };
  if (!req.final && req.tools.length) {
    body.tools = req.tools;
    body.tool_choice = "auto";
    body.parallel_tool_calls = true;
  }
  return body;
}

// ------------------------------------------------------------------ Meta's reply
const count = (v) => (Number.isFinite(Number(v)) && Number(v) >= 0 ? Math.floor(Number(v)) : 0);
const token = (v) => (typeof v === "string" && SAFE_TOKEN.test(v) ? v : null);

export function trimResponse(data, env) {
  const output = [];
  for (const item of Array.isArray(data && data.output) ? data.output : []) {
    if (!isObject(item)) continue;
    if (item.type === "function_call") {
      const name = typeof item.name === "string" ? item.name : "";
      const callId = typeof item.call_id === "string" ? item.call_id : "";
      if (!TOOL_NAME.test(name) || !CALL_ID.test(callId)) continue;
      const args = typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments ?? {});
      output.push({ type: "function_call", call_id: callId, name, arguments: args });
    } else if (item.type === "message") {
      for (const part of Array.isArray(item.content) ? item.content : []) {
        if (isObject(part) && part.type === "output_text" && typeof part.text === "string" && part.text) {
          output.push({ type: "output_text", text: part.text });
        }
      }
    }
  }
  const usage = isObject(data && data.usage) ? data.usage : {};
  const details = isObject(data && data.incomplete_details) ? data.incomplete_details : {};
  return {
    output,
    usage: { input_tokens: count(usage.input_tokens), output_tokens: count(usage.output_tokens) },
    status: token(data && data.status) || "completed",
    incomplete_reason: token(details.reason),
    model: String(env.MODEL).trim(),
  };
}

// A fixed code per failure; Meta's error body is read only to tell "out of credits" from a plain 429.
export function mapUpstream(status, errorWords) {
  const words = String(errorWords || "").toLowerCase();
  if (status === 401) return "key_rejected";
  if (status === 402 || (status === 429 && CREDIT_WORDS.some((w) => words.includes(w)))) return "out_of_credits";
  if (status === 429) return "rate_limited";
  if (status === 403 || status === 404 || words.includes("model_not_found")) return "model_unavailable";
  if (status === 413 || words.includes("context_length_exceeded") || words.includes("payload_too_large")) return "too_long";
  if (status === 408 || status === 504) return "timeout";
  if (status === 400 || status === 422) return "bad_request";
  return "upstream_error";
}

async function errorWords(res) {
  try {
    const j = await res.json();
    const e = isObject(j) && isObject(j.error) ? j.error : {};
    return [e.code, e.type].filter((x) => typeof x === "string").join(" ").slice(0, 200);
  } catch {
    return "";
  }
}

async function callMeta(env, body, cors) {
  const url = String(env.META_BASE).trim().replace(/\/+$/, "") + "/responses";
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${String(env.MUSE_API_KEY).trim()}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (e) {
    return failure(e && (e.name === "TimeoutError" || e.name === "AbortError") ? "timeout" : "upstream_error", cors);
  }
  if (!res.ok) return failure(mapUpstream(res.status, await errorWords(res)), cors);
  let data;
  try {
    const declared = Number(res.headers.get("Content-Length"));
    if (Number.isFinite(declared) && declared > LIMITS.metaResponseBytes) throw new Error("too big");
    const raw = await res.text();
    if (raw.length > LIMITS.metaResponseBytes) throw new Error("too big");
    data = JSON.parse(raw);
  } catch {
    return failure("upstream_error", cors);
  }
  return reply(200, trimResponse(data, env), cors);
}

// ------------------------------------------------------------------ the handler
async function handle(request, env) {
  const url = new URL(request.url);
  if (url.pathname !== "/chat") return failure("not_found", {});
  const allowed = String(env.ALLOWED_ORIGIN || "").trim();
  const origin = request.headers.get("Origin");
  if (!allowed || origin !== allowed) return failure("forbidden_origin", {});
  const cors = corsHeaders(origin);

  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        ...cors,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Caddie-Passcode",
        "Access-Control-Max-Age": "600",
      },
    });
  }
  if (request.method !== "POST") return failure("method_not_allowed", cors, null, { Allow: "POST, OPTIONS" });

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (!(await allow(env.CADDIE_IP_LIMITER, `ip:${ip}`, LIMITS.requestsPerMinutePerIp))) {
    return failure("too_many_requests", cors, null, { "Retry-After": "60" });
  }
  if (configProblem(env)) return failure("not_configured", cors);

  const given = (request.headers.get("X-Caddie-Passcode") || "").trim();
  if (!given) return failure("no_passcode", cors);
  const [givenHash, realHash] = await Promise.all([sha256(given), sha256(String(env.CADDIE_PASSCODE).trim())]);
  if (!sameDigest(givenHash, realHash)) return failure("bad_passcode", cors);

  const type = (request.headers.get("Content-Type") || "").toLowerCase();
  if (!type.startsWith("application/json")) return failure("unsupported_media_type", cors);
  let req;
  try {
    const raw = await readBody(request, LIMITS.bodyBytes);
    let parsed;
    try { parsed = JSON.parse(raw); } catch { throw new RequestError("invalid_json"); }
    req = validate(parsed);
  } catch (e) {
    if (e instanceof RequestError) return failure(e.code, cors, e.message);
    throw e;
  }

  if (!(await allow(env.CADDIE_LIMITER, `pc:${hex(realHash).slice(0, 16)}`, LIMITS.callsPerMinute))) {
    return failure("too_many_requests", cors, null, { "Retry-After": "60" });
  }
  return callMeta(env, metaRequest(req, env), cors);
}

export async function respond(request, env) {
  try {
    return await handle(request, env);
  } catch {
    // No details: an exception here could carry request data. The origin is re-checked for CORS.
    const origin = request.headers.get("Origin");
    const ok = origin && origin === String(env.ALLOWED_ORIGIN || "").trim();
    return failure("internal", ok ? corsHeaders(origin) : {});
  }
}
