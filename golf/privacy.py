"""Pre-commit privacy guard: keep personal data out of git (RESEARCH_PLAN §6 "Privacy").

Everything personal lives under data/, which is gitignored; this check catches the ways it can still
leak: a force-added file under data/, a copied 18Birdies archive, the .env, a screenshot or notebook
photo, an email address or phone number, the export's PII keys with values, or a line of Shane's own
note text pasted into code or docs (read from golf.db, compared locally, never printed back).

A file that is synthetic on purpose (a test fixture with fake PII markers) opts out of the *content*
checks with the marker `privacy-check: synthetic` in its first lines; path checks still apply.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SYNTHETIC_MARKER = "privacy-check: synthetic"
MARKER_LINES = 15
PII_KEYS = ("mobileNumber", "email", "birthYear", "friends", "paymentMethod")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff"}
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
MAX_SCAN_BYTES = 5_000_000
MIN_NOTE_LINE = 24               # shorter note lines ("range 45 min") are too generic to be evidence

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_SAFE_EMAIL_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|invalid|example|test|localhost|"
                                 r"users\.noreply\.github\.com)$", re.IGNORECASE)   # GitHub's public no-reply
_ROLE_MAILBOXES = {"support", "help", "info", "noreply", "no-reply", "privacy", "hello", "contact", "legal"}
# North American numbers with or without separators (617-867-5309, (617) 867 5309, +16178675309,
# 6178675309). Area code and exchange start with 2-9, and the number must stand alone, so ids, hashes,
# decimals and 10-digit Unix timestamps (1.7e9: area code 17x) do not match.
_PHONE = re.compile(r"(?<![\w.+-])(?:\+?1[-. ]?)?\(?([2-9]\d{2})\)?[-. ]?([2-9]\d{2})[-. ]?(\d{4})(?![\w-])")
# A key with a value, as JSON/dict ("email": ...) or YAML (email: ...); prose mentions don't match.
_KEYS = "|".join(PII_KEYS)
_PII_KEY = re.compile(rf"""(?:["']({_KEYS})["']|^\s*-?\s*({_KEYS}))\s*:\s*(?=\S)""")
# API keys: an Anthropic key (sk-ant-api03-..., sk-ant-oat01-...), a Meta Model API key (LLM|<id>|<secret>,
# also seen with underscores), or MUSE_API_KEY given a value. Short test placeholders (sk-ant-...,
# LLM|fake...) don't match. The Caddie's key belongs in the Keychain, never in a file.
_API_KEY = re.compile(r"sk-ant-[a-z]{2,8}\d{2}-[A-Za-z0-9_-]{32,}|\bLLM[|_]\d{6,}[|_][A-Za-z0-9_-]{12,}|"
                      r"\bMUSE_API_KEY\s*[=:]\s*[\"']?[^\s\"'$<>{}()]{6,}")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int                    # 0 = the whole file
    code: str                    # data_dir | env | archive | database | image | email | phone | pii_key | secret | note_text
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: [{self.code}] {self.message}"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def path_findings(rel: str) -> list[Finding]:
    """Checks on the name alone: these files must never be committed, whatever they contain."""
    p = Path(rel)
    name, suffix = p.name, p.suffix.lower()
    out = []
    if p.parts and p.parts[0] == "data":
        out.append(Finding(rel, 0, "data_dir", "file under data/ (personal data lives there and is never committed)"))
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        out.append(Finding(rel, 0, "env", "environment file (holds the API key)"))
    if name.lower().startswith("18birdies_archive"):
        out.append(Finding(rel, 0, "archive", "18Birdies export (email, phone, friends, payment status)"))
    if suffix in DB_SUFFIXES:
        out.append(Finding(rel, 0, "database", "database file (golf.db holds rounds and note text)"))
    if suffix in IMAGE_SUFFIXES:
        out.append(Finding(rel, 0, "image", "image file: screenshots and notebook photos belong under data/"))
    return out


def _is_fictional_phone(m: re.Match) -> bool:
    """Area code 555 is unassigned and 555-0100..0199 is reserved for fiction; test fixtures use them."""
    return m.group(1) == "555" or (m.group(2) == "555" and m.group(3).startswith("01"))


def content_findings(rel: str, text: str, note_lines: frozenset[str] = frozenset()) -> list[Finding]:
    head = "\n".join(text.splitlines()[:MARKER_LINES])
    if SYNTHETIC_MARKER in head:
        return []
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        for m in _EMAIL.finditer(line):
            if not (_SAFE_EMAIL_DOMAINS.search(m.group(1)) or m.group(0).split("@")[0].lower() in _ROLE_MAILBOXES):
                out.append(Finding(rel, n, "email", "email address"))
        for m in _PHONE.finditer(line):
            if not _is_fictional_phone(m):
                out.append(Finding(rel, n, "phone", "phone number"))
        m = _PII_KEY.search(line)
        if m:
            out.append(Finding(rel, n, "pii_key", f"18Birdies PII key '{m.group(1) or m.group(2)}' with a value"))
        if _API_KEY.search(line):
            out.append(Finding(rel, n, "secret", "an API key (Anthropic or Meta Model API); keys stay out of files"))
        if note_lines and _has_note_line(_norm(line), note_lines):
            out.append(Finding(rel, n, "note_text", "a line of your own note text (from golf.db)"))
    return out


def _has_note_line(line: str, note_lines: frozenset[str]) -> bool:
    """A note line pasted anywhere in the file line (inside a string literal, a comment, a table cell)."""
    if len(line) < MIN_NOTE_LINE:
        return False
    return line in note_lines or any(n in line for n in note_lines if len(n) <= len(line))


def _norm(line: str) -> str:
    return " ".join(line.split()).casefold()


def note_text_lines(db_path: Path | str | None, *, strict: bool = False) -> frozenset[str] | None:
    """Distinctive lines of Shane's notes and notebook transcriptions, normalised, for exact matching.
    With strict=True, None when golf.db exists but can't be read (so the caller can fail closed)."""
    if not db_path or not Path(db_path).exists():
        return frozenset()
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=15)
        try:
            rows = conn.execute("SELECT text FROM source_docs WHERE text IS NOT NULL AND doc_id NOT LIKE 'demo-%'"
                                ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return frozenset() if "no such table" in str(e) or not strict else None
    except sqlite3.Error:
        return None if strict else frozenset()
    return frozenset(n for (text,) in rows for line in text.splitlines()
                     if len(n := _norm(line)) >= MIN_NOTE_LINE)


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}


def expand_paths(paths: Iterable[Path | str], root: Path | str) -> list[str]:
    """Files to scan for the given arguments: a file as is, a folder as every file under it. Inside a git
    repository a folder expands to what git would commit (tracked plus untracked-but-not-ignored, via
    `git ls-files`), so the gitignored data/ folder is not reported; elsewhere every file is scanned."""
    root = Path(root)
    out: list[str] = []
    for arg in paths:
        path = Path(arg) if Path(arg).is_absolute() else root / arg
        if not path.is_dir():
            if not path.exists():
                raise FileNotFoundError(f"No such file or folder: {arg}")
            out.append(str(arg))
            continue
        proc = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
                              cwd=path, capture_output=True)
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
            files = [path / n for n in names]
        else:
            files = [f for f in path.rglob("*") if f.is_file()
                     and not any(part in SKIP_DIRS for part in f.relative_to(path).parts[:-1])]
        out += [str(f) for f in sorted(files) if f.is_file()]
    return list(dict.fromkeys(out))


def staged_files(root: Path) -> list[str]:
    """Paths staged for commit (added, copied, modified, renamed), relative to the repo root."""
    proc = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root,
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "git diff --cached failed")
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _staged_text(root: Path, rel: str) -> str | None:
    """The staged blob (what will actually be committed), not the working-tree file."""
    proc = subprocess.run(["git", "show", f":{rel}"], cwd=root, capture_output=True)
    return _decode(proc.stdout) if proc.returncode == 0 else None


def _decode(blob: bytes) -> str | None:
    if len(blob) > MAX_SCAN_BYTES or b"\0" in blob[:8192]:
        return None                      # binary or huge: the path checks are what matter
    return blob.decode("utf-8", "replace")


def privacy_check(paths: Iterable[Path | str], *, root: Path | str, db_path: Path | str | None = None,
                  staged: bool = False) -> list[Finding]:
    """Findings for the given files (relative to `root` or absolute). With staged=True the staged blob
    is scanned instead of the working-tree file."""
    root = Path(root)
    notes = note_text_lines(db_path, strict=True)
    out: list[Finding] = []
    if notes is None:                   # fail closed: note text can't be checked, so nothing passes
        out.append(Finding("golf.db", 0, "note_check", "could not read your note text to check against "
                                                       "(database busy or unreadable); try again"))
        notes = frozenset()
    for p in paths:
        path = Path(p) if Path(p).is_absolute() else root / p
        rel = _rel(path, root)
        out += path_findings(rel)
        if staged:
            text = _staged_text(root, rel)
        else:
            text = _decode(path.read_bytes()) if path.is_file() else None
        if text is not None:
            out += content_findings(rel, text, notes)
    return out


HOOK_MARKER = "# installed by `golf init`: privacy check"


def hook_script(python: str, root: Path | str) -> str:
    """PYTHONPATH is set explicitly: macOS can flag the venv's .pth files hidden, and Python then skips
    them, so the editable install alone is not enough to import golf."""
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "# Blocks commits that stage personal data. Bypass only if you are sure: git commit --no-verify\n"
        f'PYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{python}" -m golf.cli privacy-check --staged\n'
    )


def install_hook(root: Path | str, python: str) -> str:
    """Install .git/hooks/pre-commit. Never overwrites a hook someone else wrote. Returns what happened."""
    git_dir = Path(root) / ".git"
    if not git_dir.is_dir():
        return "skipped (not a git repository)"
    hook = git_dir / "hooks" / "pre-commit"
    if hook.exists() and HOOK_MARKER not in hook.read_text(errors="replace"):
        return f"skipped ({hook} already exists; add `golf privacy-check --staged` to it yourself)"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hook_script(python, Path(root).resolve()))
    hook.chmod(0o755)
    return f"installed {hook}"


# ------------------------------------------------------------------ the published site (golf publish)
# What may go public (DECISIONS: Shane said "let them see everything"): rounds, scores, stats, the
# handicap, lessons and note summaries. What may not: anything that locates this Mac or its files, the
# screenshots and notebook photos, GPS coordinates of shots, API spend, keys, and contact details.
SITE_RULES: tuple[tuple[str, re.Pattern, str], ...] = (
    ("local_path", re.compile(r"(?:/Users/|/home/|/private/(?:var|tmp)/|/var/folders/|/Volumes/|file:/|~/|"
                              r"[A-Za-z]:\\)"), "a local file path"),
    # A path to a file in data/ (the folder names alone, as in "save it in data/raw/18birdies/", are fine).
    ("data_path", re.compile(r"\b(?:raw|inbox)/(?:screenshots|notebook|18birdies|rejected)/\w[^\s\"'<>)\]]+|"
                             r"/files/(?:raw|inbox)/|18Birdies_archive[^\s\"'<>/]*\d{8}[^\s\"'<>/]*\.json",
                             re.IGNORECASE),
     "a path to a file inside data/ (a screenshot, notebook photo or export)"),
    ("image", re.compile(r"data:image/(?:png|jpe?g|heic|heif|webp|gif|tiff)|<img\b|"
                         r"[\w\-.()]+\.(?:png|jpe?g|heic|heif|webp|tiff?)\b", re.IGNORECASE),
     "an image or image file name (screenshots and notebook photos stay on the Mac)"),
    ("gps", re.compile(r"""["']?\b(?:start_?lat|start_?lon|end_?lat|end_?lon|latitude|longitude|startPoint|endPoint|"""
                       r"""lat|lng|lon)\b["']?\s*:\s*[-\d{\[]""", re.IGNORECASE), "GPS coordinates"),
    ("precise_number", re.compile(r"(?<![\w.])-?\d{1,3}\.\d{5,}(?![\d])"),
     "a number with 5+ decimals (the dashboard rounds to 4; raw GPS coordinates look like this)"),
    ("api_cost", re.compile(r"\b(?:cost_usd|cost_total_usd|spend_usd|total_spend|billed_calls|llm_calls|api_calls|"
                            r"input_tokens|output_tokens)\b", re.IGNORECASE), "API cost or usage"),
    ("secret", re.compile(r"sk-ant-|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|MUSE_API_KEY|"
                          r"\bLLM[|_]\d{6,}[|_][A-Za-z0-9_-]{12,}"), "an API key or its name"),
    ("local_host", re.compile(r"127\.0\.0\.1|\blocalhost\b|\[::1\]"), "a local-machine address"),
    # 18Birdies round and club ids are UUIDs (its round ids are version 1: they encode when the round was
    # created); the public page uses its own r1..rN, so any UUID there is a leak.
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
     "an internal id (18Birdies round and club ids stay on the Mac)"),
)
_FLOAT = re.compile(r"-?\d+\.\d{4,}")


def _site_text(raw: str) -> str:
    """The dashboard escapes '/' as '\\/' inside its JSON; undo that (and \\u escapes of <, >, &) so
    paths are seen as paths."""
    return (raw.replace("\\/", "/").replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))


def local_markers(root: Path | str | None = None, data_dir: Path | str | None = None) -> list[str]:
    """Strings that identify this machine: its host name, the home folder and the project paths."""
    import socket

    marks = {str(Path.home())}
    host = socket.gethostname().strip()
    for h in (host, host.removesuffix(".local")):
        if len(h) >= 4 and h.lower() not in ("localhost", "local"):
            marks.add(h)
    for p in (root, data_dir):
        if p:
            marks.add(str(Path(p)))
            marks.add(str(Path(p).resolve()))
    return sorted(m for m in marks if len(m) >= 4 and m not in ("/", "/tmp"))


def shot_points(db_path: Path | str | None, *, strict: bool = False) -> list[tuple[float, float]] | None:
    """Every GPS point of Shane's tracked shots (golf.db table shots), read locally and never printed.

    [] when there is no database or no shots table. When golf.db exists but can't be read (locked by
    a writer, corrupt), strict=True returns None so the caller can fail closed; otherwise []."""
    if not db_path or not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=15)
        try:
            rows = conn.execute("SELECT start_lat, start_lon FROM shots UNION SELECT end_lat, end_lon FROM shots"
                                ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        return None if strict else []
    except sqlite3.Error:
        return None if strict else []
    return [(float(a), float(b)) for a, b in rows if a is not None and b is not None]


def site_findings(site_dir: Path | str, *, db_path: Path | str | None = None, root: Path | str | None = None,
                  data_dir: Path | str | None = None, extra_markers: Iterable[str] = ()) -> list[Finding]:
    """Checks a built public site before it is pushed: every file under site_dir. Images of any kind,
    databases and archives fail on their name; text files are scanned for contact details, local paths
    and machine names, embedded images, GPS coordinates (by key name, by precision, and against the shot
    coordinates in golf.db) and API cost. The synthetic-marker opt-out does not apply here."""
    site = Path(site_dir)
    markers = [m for m in [*local_markers(root, data_dir), *extra_markers] if m]
    points = shot_points(db_path, strict=True)
    out: list[Finding] = []
    if points is None:                  # fail closed: the one check that catches a 4-decimal coordinate
        out.append(Finding("golf.db", 0, "gps_check", "could not read the shot coordinates to check the site "
                                                     "against them (database busy or unreadable); try again"))
        points = []
    for f in sorted(p for p in site.rglob("*") if p.is_file()):
        rel = f.relative_to(site).as_posix()
        if rel.startswith(".git/"):
            continue
        out += [x for x in path_findings(rel) if x.code != "data_dir"]
        text = _decode(f.read_bytes())
        if text is None:
            if f.stat().st_size:
                out.append(Finding(rel, 0, "binary", "binary file (the site should be text only)"))
            continue
        text = _site_text(text)
        lines = text.splitlines()
        for n, line in enumerate(lines, start=1):
            for m in _EMAIL.finditer(line):
                if not (_SAFE_EMAIL_DOMAINS.search(m.group(1)) or m.group(0).split("@")[0].lower() in _ROLE_MAILBOXES):
                    out.append(Finding(rel, n, "email", "email address"))
            if any(not _is_fictional_phone(m) for m in _PHONE.finditer(line)):
                out.append(Finding(rel, n, "phone", "phone number"))
            if _PII_KEY.search(line):
                out.append(Finding(rel, n, "pii_key", "18Birdies PII key with a value"))
            for code, rx, message in SITE_RULES:
                if rx.search(line):
                    out.append(Finding(rel, n, code, message))
            low = line.lower()
            if any(m.lower() in low for m in markers):
                out.append(Finding(rel, n, "machine", "this Mac's name or a local folder path"))
        if points:
            tokens = {round(float(t), 4) for t in _FLOAT.findall(text)}
            if any(round(lat, 4) in tokens and round(lon, 4) in tokens for lat, lon in points):
                out.append(Finding(rel, 0, "gps", "the GPS position of a tracked shot"))
    return out
