"""Publish the dashboard to GitHub Pages from this Mac (`golf publish`).

The public page is the same dashboard built with public=True: rounds, scores, stats, the unofficial
handicap and the lesson timeline (Shane: "let them see everything"), but no file paths, screenshots or
notebook photos, GPS coordinates, API cost, or anything naming this machine. Every build is scanned by
golf.privacy.site_findings before it can leave the Mac, and any finding aborts the publish.

How it is pushed: the site is built in a temporary folder that becomes a one-commit git repository,
force-pushed to the `gh-pages` branch of the project's GitHub remote. That branch holds ONLY the built
site, so replacing it wholesale is safe; the push refuses any other branch name, and any remote that
is not the GitHub repo site_url names (https://<owner>.github.io/<repo>/ -> github.com/<owner>/<repo>),
unless [publish] allow_any_remote = true. Nothing personal from data/ is ever committed, because only
the built folder is in that temporary repository. git runs without the caller's repository-locating
variables (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, ... as set inside a git hook) or identity overrides,
and every gh-pages commit is authored by a fixed no-reply identity, never the project's git email. A
build that is byte-identical to the last published one (apart from its build timestamp) is not pushed
again.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from golf import privacy
from golf.config import Config
from golf.db import now_iso

PAGES_BRANCH = "gh-pages"
DEFAULT_SITE_URL = "https://sharkweekshane.github.io/golf-analytics/"
LAST_SHA_KEY = "publish_last_sha"
LAST_AT_KEY = "publish_last_at"
LAST_COMMIT_KEY = "publish_last_commit"
FALLBACK_NAME = "golf-analytics"
FALLBACK_EMAIL = "golf-analytics@users.noreply.github.com"
# Variables that point git at another repository or index (git rev-parse --local-env-vars) or override
# who commits: inherited from a hook, `git rebase --exec` or `git bisect run`, they would make the
# temporary site repo's commands act on that other repository, or stamp its author on the public branch.
_GIT_ENV_DROP = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})

Runner = Callable[[list[str], Path | None], Any]   # (args, cwd) -> object with returncode/stdout/stderr


class PublishError(RuntimeError):
    """Publishing is off, misconfigured, or git failed; the message says what to do."""


class PublishPrivacyError(PublishError):
    """The public build contains something that must stay on the Mac. Nothing was pushed."""

    def __init__(self, findings: list[privacy.Finding]):
        self.findings = findings
        head = "; ".join(str(f) for f in findings[:5])
        super().__init__(f"privacy check failed ({len(findings)} finding(s)); nothing was pushed: {head}")


@dataclass(frozen=True)
class PublishSettings:
    enabled: bool = False
    auto: bool = True
    remote: str = "origin"
    branch: str = PAGES_BRANCH
    site_url: str = DEFAULT_SITE_URL
    allow_any_remote: bool = False    # tests (a local bare repo) or a deliberate non-GitHub remote


def settings(cfg: Config) -> PublishSettings:
    from golf.watch import read_section

    raw = read_section(cfg, "publish")
    return PublishSettings(
        enabled=bool(raw.get("enabled", False)),
        auto=bool(raw.get("auto", True)),
        remote=str(raw.get("remote", "origin")),
        branch=str(raw.get("branch", PAGES_BRANCH)),
        site_url=str(raw.get("site_url", DEFAULT_SITE_URL)),
        allow_any_remote=bool(raw.get("allow_any_remote", False)),
    )


def _check_branch(branch: str) -> None:
    """The force-push is only acceptable because gh-pages holds nothing but the generated site."""
    if branch != PAGES_BRANCH:
        raise PublishError(f"[publish] branch is {branch!r}: golf publish force-pushes, so it only ever pushes "
                           f"to '{PAGES_BRANCH}' (a branch holding just the built site). Set branch = "
                           f"\"{PAGES_BRANCH}\".")


def github_repo(site_url: str) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub Pages URL: https://owner.github.io/repo/ -> ('owner', 'repo'); a user
    site https://owner.github.io/ -> ('owner', 'owner.github.io'). None for any other URL."""
    parts = urlsplit(site_url.strip())
    host = (parts.hostname or "").lower()
    if not host.endswith(".github.io") or host.count(".") != 2:
        return None
    owner = host[: -len(".github.io")]
    first = next((seg for seg in parts.path.split("/") if seg), None)
    return owner, (first or host)


def remote_matches(url: str, owner: str, repo: str) -> bool:
    """Whether a git remote URL is github.com/<owner>/<repo> (https or ssh, .git optional; case ignored)."""
    clean = re.sub(r"^[a-z+]+://[^@/]*@", "", url.strip(), flags=re.IGNORECASE)     # scheme + credentials
    clean = re.sub(r"^[a-z+]+://", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^[^@/:]+@", "", clean)                                           # git@
    rx = rf"(?:www\.)?github\.com[:/]+{re.escape(owner)}/{re.escape(repo)}(?:\.git)?/?"
    return re.fullmatch(rx, clean, flags=re.IGNORECASE) is not None


def _check_remote(s: PublishSettings, url: str) -> None:
    """The force-push replaces the target's gh-pages, so the target must be the site's own repo."""
    if s.allow_any_remote:
        return
    repo = github_repo(s.site_url)
    if repo is None:
        raise PublishError(f"[publish] site_url {s.site_url!r} is not a GitHub Pages address "
                           "(https://<owner>.github.io/<repo>/), so the push target can't be checked. Fix site_url, "
                           "or set allow_any_remote = true if this is deliberate.")
    if not remote_matches(url, *repo):
        raise PublishError(f"Not pushed: the git remote is {_scrub(url)}, but site_url {s.site_url} is published "
                           f"from github.com/{repo[0]}/{repo[1]}. Force-pushing there would replace another "
                           "repository's gh-pages branch. Point [publish] remote (or the project's origin) at "
                           f"https://github.com/{repo[0]}/{repo[1]}.git, or set allow_any_remote = true.")


# ------------------------------------------------------------------ git plumbing
def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"                          # never hang on a password prompt (LaunchAgent)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _run(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300, env=git_env())


def _scrub(text: str) -> str:
    """No credentials in messages: https://user:token@host -> https://***@host."""
    return re.sub(r"://[^/@\s]+@", "://***@", (text or "").strip())


def _git(run: Runner, args: list[str], cwd: Path | None, what: str) -> str:
    r = run(["git", *args], cwd)
    if getattr(r, "returncode", 1) != 0:
        raise PublishError(f"{what} failed: {_scrub(getattr(r, 'stderr', '') or getattr(r, 'stdout', ''))}")
    return (getattr(r, "stdout", "") or "").strip()


def remote_url(cfg: Config, remote: str, run: Runner) -> str:
    """The URL to push to: [publish] remote is a remote name of the project repo (origin) or a URL/path."""
    if "/" in remote or ":" in remote:
        return remote
    r = run(["git", "remote", "get-url", remote], Path(cfg.root))
    url = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0 or not url:
        raise PublishError(f"The project repo at {cfg.root} has no git remote '{remote}'. Create the GitHub repo "
                           "and add it (git remote add origin <url>), or set [publish] remote to a URL.")
    return url


# ------------------------------------------------------------------ building
def not_found_page(site_url: str) -> str:
    base = urlsplit(site_url).path or "/"
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Not found</title></head>\n"
            "<body style=\"font-family: system-ui, sans-serif; margin: 3rem\"><h1>Not found</h1>"
            f"<p><a href=\"{html.escape(base)}\">Open the golf dashboard</a></p></body></html>\n")


def build_site(conn, cfg: Config, site: Path, site_url: str = DEFAULT_SITE_URL) -> Path:
    """index.html (public dashboard) + .nojekyll + 404.html into `site`. Returns index.html."""
    from golf.dashboard import build as build_dashboard

    site.mkdir(parents=True, exist_ok=True)
    index = build_dashboard(conn, cfg, site / "index.html", public=True)
    (site / ".nojekyll").write_text("")
    (site / "404.html").write_text(not_found_page(site_url), encoding="utf-8")
    return Path(index)


def check_site(cfg: Config, site: Path, *, extra_markers: list[str] | None = None) -> list[privacy.Finding]:
    return privacy.site_findings(site, db_path=cfg.db_path, root=cfg.root, data_dir=cfg.data_dir,
                                 extra_markers=extra_markers or [])


_GENERATED_AT = re.compile(rb'"generated_at":"[^"]*"')


def content_sha(html_bytes: bytes) -> str:
    """sha256 of the page apart from its build timestamp, so an unchanged dashboard is not re-pushed
    just because it was rebuilt a minute later."""
    return hashlib.sha256(_GENERATED_AT.sub(b'"generated_at":""', html_bytes)).hexdigest()


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def last_publish(conn) -> dict[str, Any] | None:
    at = _meta(conn, LAST_AT_KEY)
    return {"at": at, "commit": _meta(conn, LAST_COMMIT_KEY), "sha": _meta(conn, LAST_SHA_KEY)} if at else None


# ------------------------------------------------------------------ publish
def publish(conn, cfg: Config, *, dry_run: bool = False, runner: Runner | None = None,
            force: bool = False) -> dict[str, Any]:
    """Build the public dashboard, privacy-check it, and force-push it to gh-pages.

    dry_run: build and check only (works while [publish] enabled is false); the result is copied to
    data/site/public/ to look at. force: push even if the build is unchanged since the last publish.
    Returns {action: dry_run|unchanged|pushed, site_url, sha256, commit?, preview?}."""
    s = settings(cfg)
    _check_branch(s.branch)
    if not s.enabled and not dry_run:
        raise PublishError("Publishing is off. Once the GitHub repo exists, set enabled = true under [publish] in "
                           "config.toml (`golf publish --dry-run` builds and checks without pushing).")
    run = runner or _run
    tmp = Path(tempfile.mkdtemp(prefix="golf-publish-"))
    try:
        site = tmp / "site"
        index = build_site(conn, cfg, site, s.site_url)
        findings = check_site(cfg, site, extra_markers=[str(tmp), str(tmp.resolve())])
        if findings:
            raise PublishPrivacyError(findings)
        sha = content_sha(index.read_bytes())
        out: dict[str, Any] = {"action": None, "site_url": s.site_url, "sha256": sha, "commit": None,
                               "preview": None, "files": sorted(p.name for p in site.iterdir())}
        if dry_run:
            preview = cfg.site_dir / "public"
            if preview.exists():
                shutil.rmtree(preview)
            shutil.copytree(site, preview)
            out.update(action="dry_run", preview=str(preview / "index.html"))
            return out
        if not force and _meta(conn, LAST_SHA_KEY) == sha:
            out["action"] = "unchanged"
            return out

        url = remote_url(cfg, s.remote, run)
        _check_remote(s, url)
        stamp = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H:%M")
        # -C site on every command as well as cwd: the temporary repo, and nothing else, is what git touches.
        _git(run, ["-C", str(site), "init", "-q"], site, "git init")
        _git(run, ["-C", str(site), "symbolic-ref", "HEAD", f"refs/heads/{PAGES_BRANCH}"], site, "git symbolic-ref")
        _git(run, ["-C", str(site), "add", "-A"], site, "git add")
        # A fixed no-reply author: the project's own git email must never appear on the public branch.
        _git(run, ["-C", str(site), "-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}",
                   "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"Publish golf dashboard ({stamp})"],
             site, "git commit")
        commit = _git(run, ["-C", str(site), "rev-parse", "HEAD"], site, "git rev-parse")
        refspec = f"HEAD:refs/heads/{s.branch}"
        assert refspec == f"HEAD:refs/heads/{PAGES_BRANCH}", "golf publish only ever force-pushes gh-pages"
        _git(run, ["-C", str(site), "push", "--force", "--quiet", url, refspec], site,
             f"git push to {_scrub(url)} ({PAGES_BRANCH})")
        _meta_set(conn, LAST_SHA_KEY, sha)
        _meta_set(conn, LAST_AT_KEY, now_iso())
        _meta_set(conn, LAST_COMMIT_KEY, commit)
        conn.commit()
        out.update(action="pushed", commit=commit[:12])
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def format_publish(out: dict[str, Any]) -> str:
    if out["action"] == "dry_run":
        return (f"Dry run: the public dashboard passed the privacy check ({', '.join(out['files'])}). Nothing was "
                f"pushed. Look at it: open '{out['preview']}'")
    if out["action"] == "unchanged":
        return f"The public dashboard is unchanged since the last publish; nothing pushed ({out['site_url']})."
    return (f"Published to {out['site_url']} (gh-pages commit {out['commit']}). GitHub Pages updates within a "
            "minute or two.")
