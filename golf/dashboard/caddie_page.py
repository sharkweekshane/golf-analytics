"""The Caddie on the public site (GitHub Pages): caddie/index.html beside golf-data.json.

The page is caddie_page.html: the claude.ai Caddie's card and chat, rebuilt to run anywhere. Its two lookup
tools (query_table, get_round) run in the browser over golf-data.json; the model is Meta's Muse Spark,
reached through a Cloudflare Worker (worker/ in this repo) that holds the Meta API key as a secret and asks
for a passcode. The key is never in the page, in golf-data.json or in this repo. The Worker's address comes
from [publish] caddie_worker_url in config.toml; left empty, the page says the caddie isn't connected yet
and the dashboard shows no link to it.

The page is self-contained (no CDN, no web fonts). Its Content-Security-Policy allows scripts only by the
hash of its own inline script and network requests only to this site (golf-data.json) and the Worker's
origin, and it blocks form submission, so the passcode can never end up in a URL.

golf-data.json here is golf.chat.chat_bundle without the notes' verbatim excerpts: the public site shows
note summaries, never Shane's own words (the pre-commit hook keeps those out of git for the same reason).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Config

TEMPLATE_PATH = Path(__file__).with_name("caddie_page.html")
CONFIG_PLACEHOLDER = "__CADDIE_CONFIG__"
CSP_PLACEHOLDER = "__CADDIE_CSP__"
PAGE_DIR = "caddie"                 # the page is <site>/caddie/index.html
PAGE_HREF = "caddie/"               # the dashboard's link to it (relative: the site lives under /golf-analytics/)
MAX_ROUNDS = 6                      # rounds of lookups per question, then one final call without tools
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def normalize_worker_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/")


def worker_url_problem(url: Any) -> str | None:
    """What is wrong with [publish] caddie_worker_url (None = fine; empty = not connected, also fine)."""
    url = normalize_worker_url(url)
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port                                  # raises ValueError for a port that isn't a number
    except ValueError:
        return f"[publish] caddie_worker_url {url!r} is not a URL."
    loopback = host in LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        return (f"[publish] caddie_worker_url must be the Worker's https:// address (like "
                f"https://golf-caddie.<you>.workers.dev); got {url!r}.")
    if not host or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return (f"[publish] caddie_worker_url must be just the Worker's address, with no path, login, query or "
                f"#fragment (like https://golf-caddie.<you>.workers.dev); got {url!r}.")
    if not re.fullmatch(r"[a-z0-9.-]+|\[?[0-9a-f:]+\]?", host):
        return f"[publish] caddie_worker_url has an unusual host name: {url!r}."
    return None


def worker_origin(url: Any) -> str:
    """scheme://host[:port] of the Worker, for the page's connect-src."""
    parts = urlsplit(normalize_worker_url(url))
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _json_for_html(obj: Any) -> str:
    """JSON that can't close its <script> block or read as a raw URL (same escaping as the dashboard)."""
    blob = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("/", "\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def script_hash(template: str) -> str:
    scripts = _SCRIPT.findall(template)
    if len(scripts) != 1:
        raise RuntimeError("caddie_page.html must have exactly one inline <script> (plus the JSON config block)")
    return "sha256-" + base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")


def content_security_policy(template: str, worker_url: str) -> str:
    connect = " ".join(x for x in ("'self'", worker_origin(worker_url)) if x)
    return (f"default-src 'none'; script-src '{script_hash(template)}'; style-src 'unsafe-inline'; "
            f"connect-src {connect}; base-uri 'none'; form-action 'none'")


def render_caddie_page(worker_url: str = "", *, max_rounds: int = MAX_ROUNDS) -> str:
    worker_url = normalize_worker_url(worker_url)
    problem = worker_url_problem(worker_url)
    if problem:
        raise ValueError(problem)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for ph in (CONFIG_PLACEHOLDER, CSP_PLACEHOLDER):
        if template.count(ph) != 1:
            raise RuntimeError(f"caddie_page.html must contain {ph} exactly once")
    csp = content_security_policy(template, worker_url)
    config = _json_for_html({"worker_url": worker_url, "max_rounds": int(max_rounds)})
    return template.replace(CSP_PLACEHOLDER, html.escape(csp, quote=True)).replace(CONFIG_PLACEHOLDER, config)


def site_bundle(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """golf-data.json for the public site: the chat bundle without the notes' verbatim excerpts."""
    from ..chat import chat_bundle

    return chat_bundle(conn, cfg, excerpts=False)


def write_caddie(conn: sqlite3.Connection, cfg: Config, site: Path | str, worker_url: str = "") -> list[Path]:
    """<site>/caddie/index.html and <site>/golf-data.json. Returns both paths."""
    from ..chat import DATA_FILE

    site = Path(site)
    page = site / PAGE_DIR / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_caddie_page(worker_url), encoding="utf-8")
    data = site / DATA_FILE
    data.write_text(json.dumps(site_bundle(conn, cfg), ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                    encoding="utf-8")
    return [page, data]
