"""Hands-off import of new 18Birdies exports (`golf watch`).

Updating the app is meant to be: sign in at 18birdies.com/download-account-data/, click Request My
Data, done. The browser saves 18Birdies_archive.json into ~/Downloads (or, from the phone, iCloud
Drive's Downloads folder). scan_once() notices it, waits until the download has finished, keeps a
dated copy under data/raw/18birdies/, and runs the whole pipeline: import -> courses sync -> fold in
screenshot rounds -> handicap -> local dashboard -> GitHub Pages (when [publish] is on).

The user's file is only ever read: never moved, renamed or deleted. A file already imported (same
sha256) is skipped whatever its name, so "18Birdies_archive (1).json" duplicates cost nothing.

Unattended means careful: a file is applied (and published) on its own only when it is an export of
the account already imported and marks none of its rounds deleted. Anything else (someone else's
export saved on this Mac, a file with no account id, a snapshot missing rounds) is held, never copied,
and reported with the exact `golf import-18b` command that applies it by hand.

It runs from three places, all calling scan_once():
- `golf watch --once`, which the LaunchAgent (com.golfanalytics.watch) starts whenever a watched
  folder changes and every 30 minutes;
- `golf watch`, a foreground loop;
- `golf serve`, whose background worker scans every 60 seconds.
A lock file keeps two of them from importing the same file at once.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from golf.config import Config
from golf.db import now_iso

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib

LABEL = "com.golfanalytics.watch"
DEFAULT_DIRS = ("~/Downloads", "~/Library/Mobile Documents/com~apple~CloudDocs/Downloads")
DEFAULT_PATTERN = "18Birdies_archive*.json"
DEFAULT_INTERVAL = 300               # seconds between scans of the foreground `golf watch` loop
SERVE_INTERVAL = 60                  # golf serve's background scan
AGENT_INTERVAL = 1800                # the LaunchAgent's StartInterval (WatchPaths also wakes it)
STABLE_WAIT = 2.0                    # a download must keep the same size and mtime across this pause
PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".partial", ".tmp")
LAST_SCAN_KEY = "watch_last_scan"
LAST_IMPORT_KEY = "watch_last_import"
REJECTED_KEY = "watch_rejected"      # sha256s of JSON files that are not exports: not retried every scan
HELD_KEY = "watch_held"              # {sha256: reason} exports not applied unattended (see hold_reason)
LOG_NAME = "watch.log"
LOG_MAX_BYTES = 1_000_000

Runner = Callable[[list[str]], Any]  # launchctl stand-in: args -> object with returncode/stdout/stderr


class WatchError(RuntimeError):
    """Installing or removing the LaunchAgent failed; the message says what to do."""


# ------------------------------------------------------------------ settings
def read_section(cfg: Config, name: str) -> dict[str, Any]:
    """One [section] of config.toml, read fresh (the web app picks up edits without a restart)."""
    path = Path(cfg.root) / "config.toml"
    try:
        data = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get(name)
    return section if isinstance(section, dict) else {}


@dataclass(frozen=True)
class WatchSettings:
    dirs: tuple[Path, ...]
    pattern: str = DEFAULT_PATTERN
    interval: int = DEFAULT_INTERVAL
    python: str | None = None


def settings(cfg: Config) -> WatchSettings:
    raw = read_section(cfg, "watch")
    dirs = raw.get("dirs", list(DEFAULT_DIRS))
    if isinstance(dirs, str):
        dirs = [dirs]
    return WatchSettings(
        dirs=tuple(Path(os.path.expanduser(str(d))) for d in dirs),
        pattern=str(raw.get("pattern", DEFAULT_PATTERN)),
        interval=max(10, int(raw.get("interval_seconds", DEFAULT_INTERVAL))),
        python=raw.get("python") or None,
    )


# ------------------------------------------------------------------ small helpers
def logs_dir(cfg: Config) -> Path:
    return cfg.data_dir / "logs"


def log(cfg: Config, text: str) -> None:
    """Append to data/logs/watch.log (rolled over at 1 MB). File names only, never file contents."""
    folder = logs_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOG_NAME
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(folder / (LOG_NAME + ".1"))
        stamp = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            for line in text.splitlines() or [""]:
                f.write(f"{stamp}  {line}\n")
    except OSError:
        pass


@contextmanager
def _lock(cfg: Config) -> Iterator[bool]:
    """Non-blocking exclusive lock: yields False when another scan (LaunchAgent, golf serve) holds it."""
    import fcntl

    folder = logs_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "watch.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _sha(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        _SHA_CACHE[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _SHA_CACHE[key]


def _meta_json(conn, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else default
    except ValueError:
        return default


def _meta_put(conn, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, sort_keys=True)))
    conn.commit()


def _imported_shas(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT sha256 FROM imports WHERE kind = '18b_export'")}


# ------------------------------------------------------------------ finding downloads
def find_downloads(dirs: tuple[Path, ...] | list[Path], pattern: str = DEFAULT_PATTERN
                   ) -> tuple[list[Path], list[dict[str, str]], list[str]]:
    """(complete-looking candidates, pending, folders we may not read) in the watched folders. Matching
    ignores case, so '18Birdies_archive (1).json' and '18birdies_archive-2.json' count. A file with a
    browser's partial-download sibling (x.json.crdownload / .download / .part) is pending, and so is an
    iCloud placeholder (.x.json.icloud: listed in Finder, not downloaded to this Mac yet)."""
    pat = pattern.lower()
    found: list[Path] = []
    pending: list[dict[str, str]] = []
    denied: list[str] = []
    for folder in dirs:
        try:
            names = os.listdir(folder)
        except PermissionError:  # macOS privacy: this python may not read Downloads / iCloud Drive yet
            denied.append(str(folder))
            continue
        except OSError:          # missing folder
            continue
        lower = {n.lower() for n in names}
        for name in sorted(names):
            low = name.lower()
            if low.startswith(".") and low.endswith(".icloud") and fnmatch.fnmatch(low[1:-7], pat):
                pending.append({"file": name[1:-7], "reason": "in iCloud Drive, not downloaded to this Mac yet"})
                continue
            if low.startswith(".") or not fnmatch.fnmatch(low, pat):
                continue
            path = Path(folder) / name
            if not path.is_file():
                continue
            if any(low + sfx in lower for sfx in PARTIAL_SUFFIXES):
                pending.append({"file": name, "reason": "still downloading"})
                continue
            found.append(path)
    return found, pending, denied


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:                   # gone between listing and now (the browser renamed it)
        return float("inf")


def _stable(paths: list[Path], wait: float, sleep: Callable[[float], None]) -> tuple[list[Path], list[Path]]:
    """Files whose size and mtime did not change across `wait` seconds (a download in progress grows)."""
    def sig(p: Path) -> tuple[int, int] | None:
        try:
            st = p.stat()
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    before = {p: sig(p) for p in paths}
    if paths and wait > 0:
        sleep(wait)
    stable = [p for p in paths if before[p] is not None and sig(p) == before[p] and before[p][0] > 0]
    return stable, [p for p in paths if p not in stable]


def hold_reason(preview: dict[str, Any], path: Path) -> str | None:
    """Why an export must not be applied unattended (None: safe). See birdies_export.preview_export."""
    by_hand = f"golf import-18b '{path}'"
    if preview["account"] == "different":
        return ("an export of a different 18Birdies account than your rounds; it was not imported or copied. "
                f"If you switched accounts: {by_hand} --new-account")
    if preview["account"] == "unknown":
        return f"the file has no 18Birdies account id, so it can't be checked as yours. If it is yours: {by_hand}"
    if preview["would_delete"]:
        n, of = preview["would_delete"], preview["live_rounds"]
        return (f"it would mark {n} of your {of} rounds as deleted in 18Birdies, so it was not applied "
                f"automatically. If you did delete {'it' if n == 1 else 'them'} in the app: {by_hand}")
    return None


# ------------------------------------------------------------------ one scan
def scan_once(conn, cfg: Config, *, dirs: list[Path] | tuple[Path, ...] | None = None,
              stable_wait: float | None = None, sleep: Callable[[float], None] = time.sleep,
              build: bool = True, publish: bool | None = None, publish_runner: Any = None,
              source: str = "cli") -> dict[str, Any]:
    """Look once for new 18Birdies exports and import them. Returns a summary (format_scan prints it).

    - Partial downloads (a .crdownload/.download sibling, a size still changing, invalid JSON) are
      'pending' and simply retried on the next scan; they are not errors.
    - A file whose sha256 was already imported is skipped, whatever its name.
    - A new file is copied (never moved) to data/raw/18birdies/18Birdies_archive_<mtime YYYYMMDD>.json,
      then imported with the full pipeline; the local dashboard is rebuilt and, when [publish] enabled
      and auto are on (or publish=True), the public site is pushed.
    - An export of another account, one without an account id, or one that would mark rounds deleted
      is 'held' (hold_reason): not copied, not imported, not published, and reported until it is
      applied by hand or goes away.
    """
    from golf import cli as ops
    from golf.ingest.birdies_export import ExportFormatError, preview_export

    st = settings(cfg)
    dirs = tuple(Path(d) for d in (dirs if dirs is not None else st.dirs))
    summary: dict[str, Any] = {
        "at": now_iso(), "source": source, "dirs": [str(d) for d in dirs if d.is_dir()],
        "missing_dirs": [str(d) for d in dirs if not d.is_dir()], "denied_dirs": [], "imported": [], "already": [],
        "ignored": [], "pending": [], "errors": [], "held": [], "built": None, "published": None,
        "publish_error": None, "publish_skipped": None, "busy": False, "news": False,
    }
    with _lock(cfg) as got:
        if not got:
            summary["busy"] = True
            return summary
        found, summary["pending"], summary["denied_dirs"] = find_downloads(dirs, st.pattern)
        done = _imported_shas(conn)
        rejected = set(_meta_json(conn, REJECTED_KEY, []))
        held: dict[str, str] = {k: v for k, v in (_meta_json(conn, HELD_KEY, {}) or {}).items() if k not in done}
        fresh: list[Path] = []
        seen: set[str] = set()
        for p in sorted(found, key=_mtime):
            try:
                sha = _sha(p)
            except OSError as e:
                summary["pending"].append({"file": p.name, "reason": f"not readable yet ({e.strerror or e})"})
                continue
            if sha in done or sha in seen:
                summary["already"].append(p.name)
            elif sha in rejected:
                summary["ignored"].append(p.name)
            elif sha in held:
                seen.add(sha)
                summary["held"].append({"file": p.name, "reason": held[sha]})
            else:
                seen.add(sha)
                fresh.append(p)
        stable, moving = _stable(fresh, STABLE_WAIT if stable_wait is None else stable_wait, sleep)
        summary["pending"] += [{"file": p.name, "reason": "still downloading (size changing)"} for p in moving]

        applied = 0
        deleted = 0
        for p in stable:
            try:
                blob = p.read_bytes()
                json.loads(blob)
            except (OSError, ValueError):
                summary["pending"].append({"file": p.name, "reason": "not complete JSON yet; retried next scan"})
                continue
            sha = hashlib.sha256(blob).hexdigest()
            try:
                preview = preview_export(conn, p, cfg)
            except ExportFormatError as e:
                rejected.add(sha)
                _meta_put(conn, REJECTED_KEY, sorted(rejected))
                summary["errors"].append({"file": p.name, "error": f"not an 18Birdies export ({e}); it is not "
                                                                    "retried unless its content changes"})
                continue
            reason = hold_reason(preview, p)
            if reason:
                held[sha] = reason
                _meta_put(conn, HELD_KEY, held)
                summary["held"].append({"file": p.name, "reason": reason})
                continue
            try:
                stored = ops.store_export(cfg, p)
            except OSError as e:
                summary["errors"].append({"file": p.name, "error": f"could not copy it: {e}"})
                continue
            try:
                out = ops.import_18b(conn, cfg, stored)
            except Exception as e:  # keep the watcher alive; the log says what broke
                conn.rollback()
                summary["errors"].append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
                continue
            imp = out["import"]
            stale = bool(imp.get("stale_snapshot"))
            applied += 0 if stale else 1
            deleted += int(imp.get("deleted_in_source") or 0)
            summary["imported"].append({
                "file": p.name, "stored_as": ops._display_path(cfg, stored), "snapshot_date": imp.get("snapshot_date"),
                "stale": stale, "text": ops.format_import(out)})
            _meta_put(conn, LAST_IMPORT_KEY, {"at": now_iso(), "file": p.name, "stored_as": stored.name,
                                              "snapshot_date": imp.get("snapshot_date")})

        if applied and build:
            try:
                from golf.dashboard import build as build_dashboard

                summary["built"] = str(build_dashboard(conn, cfg))
            except Exception as e:
                summary["errors"].append({"file": "dashboard", "error": f"build failed: {type(e).__name__}: {e}"})
        if applied:
            from golf import publish as publish_mod

            pset = publish_mod.settings(cfg)
            if not (publish if publish is not None else (pset.enabled and pset.auto)):
                pass
            elif deleted:            # never publish unattended after rounds went missing: Shane checks first
                summary["publish_skipped"] = (f"{deleted} round(s) were marked deleted in 18Birdies; check the "
                                              "dashboard, then run `golf publish`")
            else:
                try:
                    summary["published"] = publish_mod.publish(conn, cfg, runner=publish_runner)
                except Exception as e:
                    summary["publish_error"] = str(e)

        previous = _meta_json(conn, LAST_SCAN_KEY, None) or {}
        state = {"pending": summary["pending"], "errors": summary["errors"], "denied_dirs": summary["denied_dirs"],
                 "held": summary["held"], "publish_error": summary["publish_error"]}
        # "news": something to tell Shane. A leftover .crdownload or a file that keeps failing is reported
        # once, not every minute (golf serve scans that often).
        summary["news"] = bool(summary["imported"]) or any(v and v != previous.get(k) for k, v in state.items())
        _meta_put(conn, LAST_SCAN_KEY, {"at": summary["at"], "source": source, "dirs": summary["dirs"],
                                        "imported": [x["file"] for x in summary["imported"]], **state})
    if summary["news"]:
        log(cfg, f"[{source}] " + format_scan(summary))
    return summary


def format_scan(s: dict[str, Any]) -> str:
    if s.get("busy"):
        return "Another scan is running; skipped."
    lines = []
    for x in s["imported"]:
        lines.append(f"Imported {x['file']} -> {x['stored_as']}" + (" (older snapshot; not applied)" if x["stale"]
                                                                    else ""))
        lines += ["  " + t for t in x["text"].splitlines()]
    for x in s["pending"]:
        lines.append(f"Waiting: {x['file']} ({x['reason']}).")
    for x in s["errors"]:
        lines.append(f"Error: {x['file']}: {x['error']}")
    for x in s.get("held") or []:
        lines.append(f"Not imported automatically: {x['file']}: {x['reason']}")
    for d in s.get("denied_dirs") or []:
        lines.append(f"No permission to read {d}: allow this Python in System Settings > Privacy & Security "
                     "(see `golf watch status`).")
    if s.get("built"):
        lines.append(f"Dashboard rebuilt: {s['built']}")
    pub = s.get("published")
    if pub:
        lines.append(format_publish_line(pub))
    if s.get("publish_error"):
        lines.append(f"Publish failed: {s['publish_error']}")
    if s.get("publish_skipped"):
        lines.append(f"Not published automatically: {s['publish_skipped']}.")
    if not lines:
        where = ", ".join(s["dirs"]) or "no watched folder exists"
        lines.append(f"No new 18Birdies export ({len(s['already'])} already imported). Watching: {where}.")
    return "\n".join(lines)


def format_publish_line(pub: dict[str, Any]) -> str:
    if pub.get("action") == "pushed":
        return f"Published to {pub.get('site_url')} (commit {pub.get('commit')})."
    if pub.get("action") == "unchanged":
        return "Public site unchanged since the last publish; nothing pushed."
    return f"Publish: {pub.get('action')}."


def run_forever(conn_factory: Callable[[], Any], cfg: Config, *, interval: int | None = None,
                echo: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                max_scans: int | None = None) -> None:
    """`golf watch`: scan, report anything new, sleep, repeat (Ctrl-C stops it)."""
    every = interval or settings(cfg).interval
    n = 0
    while max_scans is None or n < max_scans:
        conn = conn_factory()
        try:
            s = scan_once(conn, cfg, source="loop")
        finally:
            conn.close()
        if s.get("news") or n == 0:
            echo(f"[{datetime.now(ZoneInfo(cfg.timezone)):%H:%M:%S}] " + format_scan(s))
        n += 1
        if max_scans is None or n < max_scans:
            sleep(every)


# ------------------------------------------------------------------ LaunchAgent
def agent_python(cfg: Config) -> str:
    """The interpreter the LaunchAgent runs: [watch] python, else this venv's python by its real folder
    (~/.venvs/golf-analytics, not the .venv symlink inside iCloud Desktop)."""
    st = settings(cfg)
    if st.python:
        return str(Path(os.path.expanduser(st.python)))
    if sys.prefix != sys.base_prefix:
        return str(Path(sys.prefix).resolve() / "bin" / "python")
    return sys.executable


def python_is_dedicated(python: str) -> bool:
    """Whether the agent's interpreter is a binary of its own (a `python -m venv --copies` venv), not a
    symlink to a Python other programs share. macOS grants folder access to the real binary, so a grant
    to a shared one (Anaconda, Homebrew) covers everything else that runs it too."""
    real = Path(os.path.realpath(python))
    return real.parent == Path(python).parent.resolve() and not Path(python).is_symlink()


PERMISSION_HELP = """\
macOS must let the LaunchAgent's Python read Downloads, iCloud Drive and the Desktop (this project):
  1. Preferred: Files and Folders. The first background scans make macOS ask whether "{name}" may access
     files in your Downloads / Desktop / iCloud Drive folders: allow each. They are listed later under
     System Settings > Privacy & Security > Files and Folders.
  2. Only if no prompt appears and `golf watch status` still says "No permission": Full Disk Access.
{shared}"""
SHARED_PYTHON_NOTE = """\
     Careful: that Python is {real}, a general-purpose interpreter other programs also run. Full Disk
     Access for it would cover every one of them (Mail, Messages and Safari data included), not just
     this app. Give the agent a Python of its own first, then grant that one:
       python3 -m venv --copies ~/.venvs/golf-agent
       ~/.venvs/golf-agent/bin/pip install -e '{root}'
       add  python = "~/.venvs/golf-agent/bin/python"  under [watch] in config.toml, then `golf watch install`"""
DEDICATED_PYTHON_NOTE = """\
     Add {real} (System Settings > Privacy & Security > Full Disk Access > +, Cmd-Shift-G pastes a path).
     It is a copy used only by this app, so the grant covers nothing else."""


def permission_help(cfg: Config, python: str | None = None) -> str:
    py = python or agent_python(cfg)
    real = os.path.realpath(py)
    note = (DEDICATED_PYTHON_NOTE if python_is_dedicated(py) else SHARED_PYTHON_NOTE).format(
        real=real, root=Path(cfg.root).resolve())
    return PERMISSION_HELP.format(name=Path(real).name, shared=note)


def plist_path(agents_dir: Path | None = None) -> Path:
    return Path(agents_dir or settings_default_agents_dir()) / f"{LABEL}.plist"


def settings_default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_dict(cfg: Config, *, python: str | None = None) -> dict[str, Any]:
    root = str(Path(cfg.root).resolve())
    logs = logs_dir(cfg).resolve()
    dirs = [str(d) for d in settings(cfg).dirs if d.is_dir()]
    out: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [python or agent_python(cfg), "-m", "golf.cli", "watch", "--once"],
        "WorkingDirectory": root,
        "EnvironmentVariables": {
            "GOLF_ROOT": root,
            "PYTHONPATH": root,      # belt and braces: a hidden .pth file must not break `import golf`
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        },
        "StartInterval": AGENT_INTERVAL,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(logs / "watch.out.log"),
        "StandardErrorPath": str(logs / "watch.err.log"),
    }
    if dirs:
        out["WatchPaths"] = dirs
    if cfg.data_dir.resolve() != (Path(root) / "data").resolve():
        out["EnvironmentVariables"]["GOLF_DATA_DIR"] = str(cfg.data_dir.resolve())
    return out


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=60)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(cfg: Config, *, agents_dir: Path | None = None, launchctl: Runner | None = None,
            python: str | None = None) -> dict[str, Any]:
    """Write ~/Library/LaunchAgents/com.golfanalytics.watch.plist and load it (replacing an older copy)."""
    run = launchctl or _launchctl
    path = plist_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir(cfg).mkdir(parents=True, exist_ok=True)
    data = plist_dict(cfg, python=python)
    run(["bootout", f"{_domain()}/{LABEL}"])            # not loaded yet is fine
    with path.open("wb") as f:
        plistlib.dump(data, f)
    r = run(["bootstrap", _domain(), str(path)])
    if getattr(r, "returncode", 1) != 0:
        err = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip()
        raise WatchError(f"launchctl bootstrap failed ({err or 'no message'}). The plist is at {path}; "
                         f"try: launchctl bootstrap {_domain()} '{path}'")
    return {"plist": str(path), "python": data["ProgramArguments"][0], "watch_paths": data.get("WatchPaths", []),
            "interval": data["StartInterval"]}


def uninstall(cfg: Config, *, agents_dir: Path | None = None, launchctl: Runner | None = None) -> dict[str, Any]:
    run = launchctl or _launchctl
    path = plist_path(agents_dir)
    r = run(["bootout", f"{_domain()}/{LABEL}"])
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"plist": str(path), "removed": existed, "was_loaded": getattr(r, "returncode", 1) == 0}


def agent_status(cfg: Config, *, agents_dir: Path | None = None, launchctl: Runner | None = None,
                 check_loaded: bool = True) -> dict[str, Any]:
    path = plist_path(agents_dir)
    out: dict[str, Any] = {"plist": str(path), "installed": path.exists(), "loaded": None, "last_exit": None,
                           "python": None, "watch_paths": []}
    if path.exists():
        try:
            with path.open("rb") as f:
                data = plistlib.load(f)
            out["python"] = (data.get("ProgramArguments") or [None])[0]
            out["watch_paths"] = data.get("WatchPaths", [])
        except Exception:
            pass
    if check_loaded:
        r = (launchctl or _launchctl)(["print", f"{_domain()}/{LABEL}"])
        out["loaded"] = getattr(r, "returncode", 1) == 0
        text = getattr(r, "stdout", "") or ""
        for line in text.splitlines():
            if "last exit code" in line:
                out["last_exit"] = line.split("=", 1)[-1].strip()
    return out


def status_info(conn, cfg: Config, *, agents_dir: Path | None = None, launchctl: Runner | None = None,
                check_loaded: bool = False) -> dict[str, Any]:
    """Everything the 18Birdies card on /status and `golf watch status` show."""
    st = settings(cfg)
    real_python = os.path.realpath(agent_python(cfg))
    return {
        "dirs": [{"path": str(d), "exists": d.is_dir()} for d in st.dirs],
        "pattern": st.pattern,
        "agent": agent_status(cfg, agents_dir=agents_dir, launchctl=launchctl, check_loaded=check_loaded),
        "python_for_permissions": real_python,
        "python_dedicated": python_is_dedicated(agent_python(cfg)),
        "last_scan": _meta_json(conn, LAST_SCAN_KEY, None),
        "last_import": _meta_json(conn, LAST_IMPORT_KEY, None),
        "log": str(logs_dir(cfg) / LOG_NAME),
    }
