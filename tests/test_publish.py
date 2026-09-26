"""golf.publish: GitHub Pages from the Mac, offline. A local bare repository stands in for GitHub (with a
`main` branch that must never be touched), the data is the synthetic demo, and git runs with no user or
system config so nothing on this machine leaks into the test.

privacy-check: synthetic (this file writes fake leaks into temp pages to prove the site check catches them)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import golf.cli as cli
from golf import privacy, publish
from golf.db import connect
from golf.demo import seed_demo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def bare(tmp_path) -> Path:
    """'GitHub': a bare repo whose main branch holds the project; publish may only touch gh-pages."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", cwd=work)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
    (work / "README.md").write_text("project\n")
    git("add", "README.md", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", cwd=work)
    git("push", "-q", str(remote), "main", cwd=work)
    return remote


def configure(cfg, **values) -> None:
    """Set [publish] keys in the temp config.toml (merged with any set earlier in the test)."""
    from golf.watch import read_section

    merged = {**read_section(cfg, "publish"), **values}
    body = "\n".join(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {str(v).lower()}" for k, v in merged.items())
    path = Path(cfg.root) / "config.toml"
    base = path.read_text().split("\n[publish]\n", 1)[0]
    path.write_text(base + "\n[publish]\n" + body + "\n")


@pytest.fixture
def demo(cfg):
    conn = connect(cfg.db_path)
    seed_demo(conn)
    yield conn
    conn.close()


def subcommand(args: list[str]) -> str:
    """The git subcommand of ['git', '-C', dir, '-c', 'k=v', 'push', ...] -> 'push'."""
    rest = list(args[1:])
    while rest and rest[0] in ("-C", "-c"):
        rest = rest[2:]
    return rest[0] if rest else ""


class Recorder:
    """A git runner that records calls; `fail` maps a git subcommand to stderr to return."""

    def __init__(self, fail: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail or {}

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        sub = subcommand(args)
        if sub in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.fail[sub])
        if sub == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="0123456789abcdef0123\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def run(*args, code: int = 0) -> str:
    result = runner.invoke(cli.app, [str(a) for a in args])
    assert result.exit_code == code, f"exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result.output


# ------------------------------------------------------------------ settings + guards
def test_publishing_is_off_until_enabled(cfg, demo):
    s = publish.settings(cfg)
    assert (s.enabled, s.auto, s.remote, s.branch) == (False, True, "origin", "gh-pages")
    assert s.site_url == "https://sharkweekshane.github.io/golf-analytics/"
    rec = Recorder()
    with pytest.raises(publish.PublishError, match="Publishing is off"):
        publish.publish(demo, cfg, runner=rec)
    assert rec.calls == []
    assert "Publishing is off" in run("publish", code=1)


def test_only_the_gh_pages_branch_is_ever_force_pushed(cfg, demo):
    configure(cfg, enabled=True, branch="main")
    rec = Recorder()
    with pytest.raises(publish.PublishError, match="only ever pushes to 'gh-pages'"):
        publish.publish(demo, cfg, runner=rec)
    with pytest.raises(publish.PublishError):
        publish.publish(demo, cfg, runner=rec, dry_run=True)
    assert rec.calls == []


def test_dry_run_builds_and_checks_without_git(cfg, demo):
    rec = Recorder()
    out = publish.publish(demo, cfg, dry_run=True, runner=rec)
    assert out["action"] == "dry_run" and rec.calls == []
    preview = cfg.site_dir / "public"
    assert sorted(p.name for p in preview.iterdir()) == [".nojekyll", "404.html", "index.html"]
    assert "/golf-analytics/" in (preview / "404.html").read_text()
    assert privacy.site_findings(preview, db_path=cfg.db_path, root=cfg.root, data_dir=cfg.data_dir) == []
    assert "passed the privacy check" in run("publish", "--dry-run")
    assert demo.execute("SELECT value FROM meta WHERE key = 'publish_last_sha'").fetchone() is None


# ------------------------------------------------------------------ pushing to a local "GitHub"
def test_publish_pushes_only_the_built_site_to_gh_pages(cfg, demo, bare):
    configure(cfg, enabled=True, remote=str(bare), allow_any_remote=True)
    main_before = git("--git-dir", str(bare), "rev-parse", "main")

    out = publish.publish(demo, cfg)
    assert out["action"] == "pushed" and len(out["commit"]) == 12
    files = git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "gh-pages").splitlines()
    assert files == [".nojekyll", "404.html", "index.html"]
    assert git("--git-dir", str(bare), "rev-parse", "main") == main_before            # main untouched
    assert git("--git-dir", str(bare), "log", "-1", "--format=%an", "gh-pages") == publish.FALLBACK_NAME
    html = git("--git-dir", str(bare), "show", "gh-pages:index.html")
    assert "/Users/" not in html and "cost_usd" not in html and str(cfg.root) not in html
    pages_rev = git("--git-dir", str(bare), "rev-parse", "gh-pages")

    again = publish.publish(demo, cfg)
    assert again["action"] == "unchanged" and git("--git-dir", str(bare), "rev-parse", "gh-pages") == pages_rev
    assert "unchanged since the last publish" in publish.format_publish(again)
    assert publish.publish(demo, cfg, force=True)["action"] == "pushed"
    assert git("--git-dir", str(bare), "rev-list", "--count", "gh-pages") == "1"      # one commit, replaced
    assert publish.last_publish(demo)["commit"]


def test_the_remote_name_is_resolved_from_the_project_repo(cfg, demo, bare):
    root = Path(cfg.root)
    git("init", "-q", cwd=root)
    git("remote", "add", "origin", str(bare), cwd=root)
    configure(cfg, enabled=True)
    assert "Force-pushing there would replace another" in run("publish", code=1)     # not the site's repo
    configure(cfg, allow_any_remote=True)
    out = run("publish")
    assert "Published to https://sharkweekshane.github.io/golf-analytics/" in out
    assert "index.html" in git("--git-dir", str(bare), "ls-tree", "--name-only", "gh-pages")
    assert "unchanged" in run("publish")


def test_a_missing_remote_or_failed_push_is_explained_without_credentials(cfg, demo):
    configure(cfg, enabled=True)
    with pytest.raises(publish.PublishError, match="no git remote 'origin'"):
        publish.publish(demo, cfg, runner=Recorder(fail={"remote": "fatal: not a git repository"}))
    url = "https://shane:ghp_SECRET@github.com/sharkweekshane/golf-analytics.git"
    rec = Recorder(fail={"push": f"fatal: unable to access '{url}/': 403"})
    configure(cfg, remote=url)
    with pytest.raises(publish.PublishError) as e:
        publish.publish(demo, cfg, runner=rec)
    assert "ghp_SECRET" not in str(e.value) and "://***@github.com" in str(e.value)
    push = next(c for c in rec.calls if subcommand(c) == "push")
    assert push[-1] == "HEAD:refs/heads/gh-pages" and "--force" in push
    assert demo.execute("SELECT value FROM meta WHERE key = 'publish_last_sha'").fetchone() is None
    # A remote that is not the site's repo is refused before anything is committed or pushed.
    rec = Recorder()
    configure(cfg, remote="https://shane:ghp_SECRET@github.com/sharkweekshane/other-site.git")
    with pytest.raises(publish.PublishError, match="replace another repository") as e:
        publish.publish(demo, cfg, runner=rec)
    assert "ghp_SECRET" not in str(e.value) and rec.calls == []


@pytest.mark.parametrize("url, ok", [
    ("git@github.com:sharkweekshane/golf-analytics.git", True),
    ("https://github.com/SharkWeekShane/Golf-Analytics", True),
    ("https://x:tok@github.com/sharkweekshane/golf-analytics.git/", True),
    ("ssh://git@github.com/sharkweekshane/golf-analytics.git", True),
    ("git@github.com:sharkweekshane/golf-analytics-copy.git", False),
    ("git@github.com:someoneelse/golf-analytics.git", False),
    ("https://gitlab.com/sharkweekshane/golf-analytics.git", False),
    ("/tmp/remote.git", False),
])
def test_the_push_target_must_be_the_repo_behind_site_url(url, ok):
    assert publish.github_repo("https://sharkweekshane.github.io/golf-analytics/") == ("sharkweekshane",
                                                                                       "golf-analytics")
    assert publish.github_repo("https://sharkweekshane.github.io/") == ("sharkweekshane", "sharkweekshane.github.io")
    assert publish.github_repo("https://example.com/golf/") is None
    assert publish.remote_matches(url, "sharkweekshane", "golf-analytics") is ok


def test_inherited_git_variables_cannot_redirect_publish_into_another_repo(cfg, demo, bare, tmp_path, monkeypatch):
    """QA: under a git hook GIT_DIR pointed publish's init/add/commit at another repo and switched its branch."""
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", cwd=other)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=other)
    (other / "keep.txt").write_text("x\n")
    git("add", "keep.txt", cwd=other)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", cwd=other)
    head = git("rev-parse", "HEAD", cwd=other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "hook.author@example.com")
    configure(cfg, enabled=True, remote=str(bare), allow_any_remote=True)
    assert publish.publish(demo, cfg)["action"] == "pushed"
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(var)
    assert git("symbolic-ref", "HEAD", cwd=other) == "refs/heads/main" and git("rev-parse", "HEAD", cwd=other) == head
    assert git("status", "--porcelain", cwd=other) == ""
    assert git("--git-dir", str(bare), "log", "-1", "--format=%ae", "gh-pages") == publish.FALLBACK_EMAIL
    env = publish.git_env({"GIT_DIR": "/x", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/h"})
    assert "GIT_DIR" not in env and env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_TERMINAL_PROMPT"] == "0"


# ------------------------------------------------------------------ the privacy gate
def test_a_leaky_build_is_never_pushed(cfg, demo, monkeypatch):
    configure(cfg, enabled=True)

    def leaky(conn, cfg, site, site_url=publish.DEFAULT_SITE_URL):
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(f'<p>{cfg.data_dir}/raw/screenshots/2026-09-20/IMG_0001.PNG</p>\n'
                                         '<script>{"cost_usd": 0.18}</script>\n')
        return site / "index.html"

    monkeypatch.setattr(publish, "build_site", leaky)
    rec = Recorder()
    with pytest.raises(publish.PublishPrivacyError) as e:
        publish.publish(demo, cfg, runner=rec)
    assert {f.code for f in e.value.findings} >= {"local_path", "data_path", "api_cost", "image"}
    assert rec.calls == []                                                          # no git at all
    out = run("publish", code=1)
    assert "Not published" in out and "[api_cost]" in out


def test_site_findings_catch_each_kind_of_leak(cfg, tmp_path):
    import socket

    conn = connect(cfg.db_path)
    has_shots = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'shots'").fetchone() is not None
    if has_shots:
        conn.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode) "
                     "VALUES ('r1', '18b_export', '2026-09-20', 'hole_by_hole')")
        conn.execute("INSERT INTO shots(round_id, seq, hole, start_lat, start_lon, end_lat, end_lon) "
                     "VALUES ('r1', 1, 1, 12.3456789, 45.6789012, 12.3461234, 45.6781234)")
        conn.commit()
    conn.close()
    site = tmp_path / "site"
    site.mkdir()
    leaks = {
        "local_path": "see /Users/someone/Desktop/x",
        "data_path": "raw\\/notebook\\/2026\\/page1.jpg",                      # JSON-escaped slashes
        "image": '<img src="data:image/png;base64,AAAA">',
        "gps": '{"start_lat": 12.3, "lon": 45.6}',
        "precise_number": "[12.123456]",
        "api_cost": '{"spend_usd": 3.2}',
        "secret": "ANTHROPIC_API_KEY=sk-ant-xyz",
        "local_host": "http://127.0.0.1:8765/review",
        "email": "golfer.person@gmail.com",
        "phone": "call 6178675309",
        "uuid": '{"id":"0f1e2d3c-b807-11ef-9cd2-0242ac120002"}',          # an 18Birdies-style round id
        "machine": f"built on {socket.gethostname()} " if len(socket.gethostname()) >= 4 else f"{Path.home()}",
    }
    (site / "index.html").write_text("\n".join(leaks.values()) + "\n")
    codes = {f.code for f in privacy.site_findings(site, db_path=cfg.db_path, root=cfg.root)}
    assert codes >= set(leaks), set(leaks) - codes
    if has_shots:
        (site / "index.html").write_text('<script>{"a":[12.3457,3],"b":{"x":45.6789}}</script>\n')
        assert [f.code for f in privacy.site_findings(site, db_path=cfg.db_path)] == ["gps"]

    clean = ("<p>Lesson with coach: keep the trail elbow in front of the hip.</p>\n"
             "<footer>Built locally from golf.db; export saved in data/raw/18birdies/.</footer>\n"
             '<script>{"hi":43.8,"diff":12.3456,"when":"2026-09-20","to_par":-2}</script>\n')
    (site / "index.html").write_text(clean)
    (site / ".nojekyll").write_text("")
    assert privacy.site_findings(site, db_path=cfg.db_path, root=cfg.root, data_dir=cfg.data_dir) == []
    (site / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert [f.code for f in privacy.site_findings(site)] == ["image", "binary"]


def test_the_build_timestamp_alone_does_not_count_as_a_change():
    a = b'<script>{"schema":2,"generated_at":"2026-09-26T01:20:32+00:00","hi":43.8}</script>'
    b = a.replace(b"01:20:32", b"02:05:59")
    assert publish.content_sha(a) == publish.content_sha(b)
    assert publish.content_sha(a) != publish.content_sha(a.replace(b"43.8", b"43.9"))


def test_the_checks_fail_closed_when_golf_db_cannot_be_read(tmp_path):
    bad = tmp_path / "golf.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>fine</p>\n")
    assert [f.code for f in privacy.site_findings(site, db_path=bad)] == ["gps_check"]
    (tmp_path / "a.txt").write_text("nothing personal\n")
    assert [f.code for f in privacy.privacy_check(["a.txt"], root=tmp_path, db_path=bad)] == ["note_check"]
    assert privacy.site_findings(site, db_path=tmp_path / "missing.db") == []        # no database: nothing to check


def test_api_keys_are_caught_before_a_commit_and_a_publish(tmp_path):
    """A Meta Model API key (pipe or underscore form), an Anthropic key, or MUSE_API_KEY given a value is
    blocked by the pre-commit check and the site check. The fakes are built here so this file stays clean."""
    meta_pipe = "LLM" + "|" + "9" * 15 + "|" + "AbCdEf123456-_xyz"
    meta_under = "LLM" + "_" + "8" * 15 + "_" + "AbCdEf123456-_xyz"
    anthropic = "sk-ant-" + "api03-" + "A" * 40
    muse_env = "MUSE_API_KEY" + "=" + "abcdef123456"
    for leak in (meta_pipe, meta_under, anthropic, muse_env, f"export {muse_env}", f"key: '{meta_under}'"):
        codes = [f.code for f in privacy.content_findings("README.md", f"line one\nsee {leak} here\n")]
        assert codes == ["secret"], leak
        site = tmp_path / "site"
        site.mkdir(exist_ok=True)
        (site / "index.html").write_text(f"<p>{leak}</p>\n")
        assert "secret" in {f.code for f in privacy.site_findings(site)}, leak
    for fine in ("ANTHROPIC_API_KEY=sk-ant-...", "sk-ant-test-DO-NOT-PRINT-123", "LLM|fakeaccount77|fake-secret",
                 "MUSE_API_KEY in the environment overrides the Keychain", "MUSE_API_KEY=$KEY",
                 'env_var="MUSE_API_KEY"', "LLM|<id>|<secret>"):
        assert privacy.content_findings("README.md", fine + "\n") == [], fine
