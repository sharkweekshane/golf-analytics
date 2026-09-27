"""The Caddie page for GitHub Pages (golf.dashboard.caddie_page): config and CSP injection, the relay
address check, self-containment, the public-safe data file, and the page's JS (syntax, and the markdown
renderer's escaping, run in Node when it is installed)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import golf.cli as cli
from golf import privacy
from golf.dashboard import caddie_page
from golf.demo import seed_demo

WORKER = "https://golf-caddie.shane-example.workers.dev"
CONFIG_RE = re.compile(r'<script type="application/json" id="caddie-config">(.*?)</script>', re.S)
CSP_RE = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">')
SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def _config(html: str) -> dict:
    return json.loads(CONFIG_RE.search(html).group(1))


def _csp(html: str) -> str:
    return CSP_RE.search(html).group(1).replace("&#x27;", "'")


def _node() -> str | None:
    for cand in (shutil.which("node"), os.path.expanduser("~/.nvm/versions/node/v22.22.2/bin/node")):
        if cand and Path(cand).exists():
            return cand
    return None


def test_not_connected_page_is_self_contained_and_locked_down():
    html = caddie_page.render_caddie_page("")
    assert _config(html) == {"worker_url": "", "max_rounds": caddie_page.MAX_ROUNDS}
    csp = _csp(html)
    assert csp.startswith("default-src 'none'; ") and "connect-src 'self';" in csp and "form-action 'none'" in csp
    # Scripts run only by the hash of the page's own inline script.
    [script] = SCRIPT_RE.findall(html)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    assert f"script-src 'sha256-{digest}';" in csp and "unsafe-inline" not in csp.split("style-src")[0]
    # No CDN, no web fonts, no external anything.
    assert not re.search(r"<script[^>]*\bsrc\s*=", html) and "<link" not in html and "@import" not in html
    assert "url(" not in html and "googleapis" not in html and "cdnjs" not in html
    for text in ("The caddie isn't connected yet", "Forget passcode", "golf-caddie.passcode", "X-Caddie-Passcode",
                 'type="password"', "Stop", "Clear chat"):
        assert text in html, text
    for never in ("MUSE_API_KEY", "localhost", "127.0.0.1", "input_tokens", "output_tokens", "Bearer"):
        assert never not in html, never
    assert html.count("__CADDIE_") == 0


def test_connected_page_talks_only_to_its_worker():
    html = caddie_page.render_caddie_page(WORKER + "/")
    assert _config(html)["worker_url"] == WORKER                       # trailing slash dropped
    assert '"worker_url":"https:\\/\\/golf-caddie.' in html            # JSON escaped like the dashboard's
    assert f"connect-src 'self' {WORKER};" in _csp(html)


@pytest.mark.parametrize("url, ok", [
    ("", True), (WORKER, True), (WORKER + "/", True), ("https://caddie.example.org", True),
    ("http://127.0.0.1:8787", True),                                   # wrangler dev (the privacy check blocks publishing it)
    ("http://golf-caddie.example.workers.dev", False), ("ftp://x.example", False), ("golf-caddie.workers.dev", False),
    (WORKER + "/chat", False), (WORKER + "?a=1", False), (WORKER + "#x", False), ("https://u:p@x.example", False),
    ('https://x.example"onload="', False), ("https://x.example:notaport", False),
])
def test_worker_address_check(url, ok):
    assert (caddie_page.worker_url_problem(url) is None) is ok
    if not ok:
        with pytest.raises(ValueError):
            caddie_page.render_caddie_page(url)


def test_site_files_pass_the_public_privacy_check_without_note_excerpts(conn, cfg, tmp_path):
    seed_demo(conn)
    site = tmp_path / "site"
    page, data = caddie_page.write_caddie(conn, cfg, site, WORKER)
    assert page == site / "caddie" / "index.html" and data == site / "golf-data.json"
    bundle = json.loads(data.read_text())
    assert bundle["tables"]["rounds"] and bundle["tables"]["events"]
    assert all("excerpt" not in e for e in bundle["tables"]["events"])
    assert privacy.site_findings(site, db_path=cfg.db_path, root=cfg.root, data_dir=cfg.data_dir) == []


def test_golf_build_public_writes_the_whole_site(cfg):
    runner = CliRunner()
    assert runner.invoke(cli.app, ["demo", "seed"]).exit_code == 0
    result = runner.invoke(cli.app, ["build", "--public"])
    assert result.exit_code == 0, result.output
    assert "Public dashboard written to" in result.output and "not connected" in result.output
    public = cfg.site_dir / "public"
    assert (public / "caddie" / "index.html").exists() and (public / "golf-data.json").exists()


@pytest.mark.skipif(_node() is None, reason="node not installed")
def test_page_script_parses_and_its_markdown_is_escaped(tmp_path):
    html = caddie_page.render_caddie_page(WORKER)
    [script] = SCRIPT_RE.findall(html)
    (tmp_path / "page.js").write_text(script)
    check = subprocess.run([_node(), "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    # The renderer on its own: everything between its section markers, then a few hostile inputs.
    start = script.index("// ---------------------------------------------------------------- markdown")
    end = script.index("// ---------------------------------------------------------------- talking to the relay")
    harness = script[start:end] + """
const cases = [
  "<script>alert(1)</script> **bold** and `<b>code</b>`",
  "[click](javascript:alert(1)) and [ok](https://example.com/a?b=1&c=2)",
  "| a | b |\\n|---|--:|\\n| <img src=x onerror=alert(1)> | 2 |",
  "- one\\n- two <iframe>\\n\\n> quote \\"x\\"",
];
process.stdout.write(JSON.stringify(cases.map(markdown)));
"""
    (tmp_path / "md.js").write_text(harness)
    run = subprocess.run([_node(), str(tmp_path / "md.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    joined = "".join(out)
    assert "<script" not in joined and "<img" not in joined and "<iframe" not in joined and "<b>" not in joined
    assert "&lt;script&gt;" in out[0] and "<strong>bold</strong>" in out[0] and "<code>&lt;b&gt;code&lt;/b&gt;</code>" in out[0]
    assert "javascript:" not in out[1].split("click")[0] and 'href="javascript' not in out[1]
    assert '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">ok</a>' in out[1]
    assert '<table>' in out[2] and '<td class="r">2</td>' in out[2]
    assert "<ul><li>one</li><li>two &lt;iframe&gt;</li></ul>" in out[3] and "&quot;x&quot;" in out[3]
