"""Apple Notes connector and the idempotent incremental sync (RESEARCH_PLAN §4 "Incremental sync").

A read-only JXA script (notes_dump.js) is run through osascript; it needs only the Automation
permission, never Full Disk Access. Bodies are fetched only for unlocked notes whose modification date
changed (equality, not a cursor: late iCloud edits can carry older timestamps), and a note is
re-extracted only when its text or the prompt/model/entities changed, so a re-sync with no edits makes
no API calls and changes no events. The runner is injectable so tests never touch the real Notes app.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import golf.llm as llm
from golf.config import Config
from golf.db import dumps, now_iso, upsert

from .entities import load_entities
from .extract import content_hash, extract_doc, extraction_signature, load_prompt, needs_extraction, normalize_text
from .reconcile import flag_duplicates, flag_list, link_rounds, make_flag

SCRIPT = Path(__file__).with_name("notes_dump.js")
BODY_CHUNK = 40
TIMEOUT_S = 300
BULK_CREATED_MIN = 3          # >= 3 notes created in the same second = a bulk import; creation dates unreliable

PERMISSION_HELP = (
    "macOS blocked access to the Notes app (error -1743).\n"
    "Open System Settings > Privacy & Security > Automation, find the app you ran this from "
    "(Terminal, iTerm, VS Code, or the golf web app's Python) and switch on 'Notes' under it, then sync again.\n"
    "If the app is not listed there, run `tccutil reset AppleEvents com.apple.Terminal` "
    "(use your terminal's bundle id) and sync again to get the permission prompt back."
)

Runner = Callable[[list[str]], Any]   # returns an object with .returncode, .stdout, .stderr (like CompletedProcess)


class NotesError(RuntimeError):
    """The Notes folder could not be read; the message says what to do."""


class NotesPermissionError(NotesError):
    """Automation permission for Notes was denied (-1743)."""


@dataclass(frozen=True)
class NoteMeta:
    id: str
    title: str
    folder: str
    account: str
    created: datetime        # local, second precision
    modified: datetime
    locked: bool


# ------------------------------------------------------------------ osascript
def _default_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)


def _raise_for(message: str, number: Any = None) -> None:
    low = (message or "").lower()
    if number == -1743 or "-1743" in low or "not authorized" in low or "not allowed to send apple events" in low:
        raise NotesPermissionError(PERMISSION_HELP)
    if number in (-600, -609, -1712) or any(code in low for code in ("-600", "-609", "-1712")):
        raise NotesError(f"The Notes app did not respond ({message.strip()}). Open Notes, wait for iCloud to "
                         "finish syncing, and sync again.")
    raise NotesError(f"Reading Apple Notes failed: {message.strip() or 'no output'}")


def run_script(mode: str, folder: str, ids: Iterable[str] = (), *, runner: Runner | None = None) -> dict[str, Any]:
    args = ["osascript", "-l", "JavaScript", str(SCRIPT), mode, folder, *ids]
    try:
        proc = (runner or _default_runner)(args)
    except FileNotFoundError as e:
        raise NotesError("osascript was not found: the Apple Notes sync only runs on macOS.") from e
    except subprocess.TimeoutExpired as e:
        raise NotesError(f"The Notes app did not answer within {TIMEOUT_S // 60} minutes. Open Notes, let iCloud "
                         "finish syncing, and sync again.") from e
    out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
    if proc.returncode != 0:
        _raise_for(err or out)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise NotesError(f"Unexpected output from notes_dump.js: {out[:200]!r} {err[:200]}") from e
    if not data.get("ok"):
        _raise_for(str(data.get("error", "")), data.get("errorNumber"))
    return data


def _to_local(iso_utc: str, tz: str) -> datetime:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz)).replace(microsecond=0)


def folder_spec(cfg: Config) -> tuple[str | None, str]:
    """[notes].apple_notes_folder is 'Golf' or 'Account/Golf' (to pick one of several 'Golf' folders)."""
    spec = cfg.apple_notes_folder.strip()
    if "/" in spec:
        account, name = spec.split("/", 1)
        return account.strip() or None, name.strip()
    return None, spec


def fetch_meta(cfg: Config, *, runner: Runner | None = None) -> tuple[list[NoteMeta], dict[str, Any]]:
    """Bulk metadata for the configured folder, with first-run diagnostics (missing / duplicate folder,
    nested sub-folders, a folder outside iCloud)."""
    account, name = folder_spec(cfg)
    data = run_script("meta", name, runner=runner)
    folders = data.get("folders") or []
    if account:
        folders = [f for f in folders if (f.get("account") or "").casefold() == account.casefold()]
    accounts = ", ".join(data.get("accounts") or []) or "none"
    if not folders:
        seen = data.get("available") or []
        listing = "; ".join(seen[:25]) + (" ..." if len(seen) > 25 else "")
        where = f" in account '{account}'" if account else ""
        raise NotesError(
            f"No Apple Notes folder named '{name}'{where}. Accounts: {accounts}. Folders: {listing or 'none'}.\n"
            f"Create a folder named '{name}' under iCloud (not 'On My iPhone'), or set [notes].apple_notes_folder "
            "in config.toml.")
    if len(folders) > 1:
        places = "; ".join(f"{f.get('account')}: {f.get('name')} ({len(f.get('notes') or [])} notes)" for f in folders)
        raise NotesError(
            f"Found {len(folders)} Apple Notes folders named '{name}' ({places}).\n"
            f"Set [notes].apple_notes_folder = \"<account>/{name}\" in config.toml (for example \"iCloud/{name}\"), "
            "or rename the extra folder.")
    f = folders[0]
    warnings = []
    subs = f.get("subfolders") or []
    if subs:
        warnings.append(f"'{name}' has sub-folders; their notes are included: "
                        + ", ".join(f"{s['path']} ({s['notes']} notes)" for s in subs))
    if "icloud" not in (f.get("account") or "").casefold():
        warnings.append(f"'{name}' is in the '{f.get('account')}' account, not iCloud: notes written on your "
                        "iPhone only reach this Mac through iCloud.")
    metas = [NoteMeta(id=n["id"], title=n.get("name") or "", folder=n.get("folder") or name,
                      account=f.get("account") or "",
                      created=_to_local(n.get("created") or n["modified"], cfg.timezone),
                      modified=_to_local(n.get("modified") or n["created"], cfg.timezone), locked=bool(n.get("locked")))
             for n in f.get("notes") or []]
    info = {"folder": name, "account": f.get("account"), "subfolders": [s["path"] for s in subs], "warnings": warnings}
    return metas, info


def fetch_bodies(ids: list[str], folder: str, *, runner: Runner | None = None
                 ) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """(plaintext by id, ids found locked, errors by id) for the given notes, in chunks."""
    texts: dict[str, str] = {}
    locked: set[str] = set()
    errors: dict[str, str] = {}
    for i in range(0, len(ids), BODY_CHUNK):
        data = run_script("bodies", folder, ids[i:i + BODY_CHUNK], runner=runner)
        for b in data.get("bodies") or []:
            if "error" in b:
                if b.get("errorNumber") == -1743:
                    raise NotesPermissionError(PERMISSION_HELP)
                errors[b["id"]] = str(b["error"])
            elif b.get("locked"):
                locked.add(b["id"])
            else:
                texts[b["id"]] = b.get("plaintext") or ""
    return texts, locked, errors


# ------------------------------------------------------------------ sync
def doc_id_for(apple_id: str) -> str:
    return "an-" + hashlib.sha256(apple_id.encode()).hexdigest()[:16]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _doc_flags(prev: dict | None, unreliable: bool) -> str:
    flags = [f for f in flag_list(prev["flags"] if prev else None) if f.get("code") != "created_unreliable"]
    if unreliable:
        flags.insert(0, make_flag("created_unreliable", "many notes share this creation second (bulk import)"))
    return dumps(flags)


def _plan(metas: list[NoteMeta], rows: dict[str, dict], texts: dict[str, str], body_locked: set[str],
          failed: set[str], bulk: set[str]) -> tuple[list[tuple[str, NoteMeta, dict | None, str | None, str | None]], list[dict]]:
    current = {m.id for m in metas}
    gone = {ext: r for ext, r in rows.items() if ext not in current}
    actions = []
    for m in metas:
        prev = rows.get(m.id)
        if m.id in failed:
            continue                                   # body read failed: leave the doc as it was (reported)
        if m.locked or m.id in body_locked:
            actions.append(("locked", m, prev, None, None))
            continue
        if m.id in texts:
            text = normalize_text(texts[m.id])
            h = content_hash(text)
        elif prev is not None and prev["text"] is not None:
            text, h = prev["text"], prev["content_hash"]
        else:
            continue                                   # no body came back for a new note
        if prev is None:
            match = next((r for r in gone.values() if r["created_at"] == _iso(m.created) and r["content_hash"] == h), None)
            if match is not None:                      # Notes re-keyed the note (store rebuilt): keep our doc
                gone.pop(match["external_id"])
                actions.append(("rekeyed", m, match, text, h))
            else:
                actions.append(("new", m, None, text, h))
            continue
        flags = _doc_flags(prev, m.id in bulk)
        if prev["content_hash"] != h:
            kind = "changed"
        elif prev["status"] != "active":
            kind = "restored"
        elif (prev["modified_at"], prev["title"], prev["folder"], prev["flags"]) != (_iso(m.modified), m.title, m.folder, flags):
            kind = "touched"
        else:
            kind = "unchanged"
        actions.append((kind, m, prev, text, h))
    deleted = [r for r in gone.values() if r["status"] != "deleted"]
    return actions, deleted


def _empty_summary(dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run, "folder": None, "account": None, "warnings": [], "subfolders": [],
        "docs": dict.fromkeys(("new", "changed", "unchanged", "touched", "restored", "rekeyed", "locked", "deleted"), 0),
        "events": dict.fromkeys(("new", "updated", "unchanged", "restored", "orphaned", "superseded",
                                 "auto_accepted", "pending"), 0),
        "extracted": 0, "extract_pending": 0, "not_golf": 0, "api_calls": 0, "cost_usd": 0.0,
        "duplicates_flagged": 0, "rounds_linked": 0, "errors": [], "llm_unavailable": None,
    }


def sync_apple_notes(conn: sqlite3.Connection, cfg: Config, *, runner: Runner | None = None, dry_run: bool = False,
                     extract: bool = True, force: bool = False) -> dict[str, Any]:
    """Sync the configured Apple Notes folder into source_docs and (with extract=True) the timeline.

    dry_run reads Notes and reports what would change without writing anything or calling the API.
    force=True re-extracts every note bypassing the LLM cache (re-bills). Raises NotesError /
    NotesPermissionError when the folder can't be read; per-note problems land in summary['errors']."""
    summary = _empty_summary(dry_run)
    metas, info = fetch_meta(cfg, runner=runner)
    summary.update(folder=info["folder"], account=info["account"], warnings=info["warnings"],
                   subfolders=info["subfolders"])
    rows = {r["external_id"]: dict(r) for r in conn.execute("SELECT * FROM source_docs WHERE source = 'apple_notes'")}
    stamp = Counter(_iso(m.created) for m in metas)
    bulk = {m.id for m in metas if stamp[_iso(m.created)] >= BULK_CREATED_MIN}

    need = [m.id for m in metas if not m.locked and (
        m.id not in rows or rows[m.id]["modified_at"] != _iso(m.modified)
        or rows[m.id]["text"] is None or rows[m.id]["status"] == "locked")]
    texts, body_locked, body_errors = fetch_bodies(need, info["folder"], runner=runner) if need else ({}, set(), {})
    titles = {m.id: m.title for m in metas}
    summary["errors"] += [{"note": titles.get(i, i), "error": e} for i, e in body_errors.items()]
    actions, deleted = _plan(metas, rows, texts, body_locked, set(body_errors), bulk)

    ents = load_entities(cfg)
    system = load_prompt("notes_tl1.md")
    queue: list[str] = []
    now = now_iso()
    for kind, m, prev, text, h in actions:
        summary["docs"][kind] += 1
        doc_id = prev["doc_id"] if prev else doc_id_for(m.id)
        if kind == "locked":
            unchanged = prev is not None and prev["status"] == "locked" and prev["modified_at"] == _iso(m.modified)
            if not dry_run and not unchanged:
                upsert(conn, "source_docs", {
                    "doc_id": doc_id, "source": "apple_notes", "external_id": m.id, "title": m.title,
                    "folder": m.folder, "created_at": _iso(m.created), "modified_at": _iso(m.modified),
                    "status": "locked", "flags": _doc_flags(prev, m.id in bulk), "deleted_at": None}, ["doc_id"])
            continue
        if kind != "unchanged" and not dry_run:
            upsert(conn, "source_docs", {
                "doc_id": doc_id, "source": "apple_notes", "external_id": m.id, "title": m.title, "folder": m.folder,
                "created_at": _iso(m.created), "modified_at": _iso(m.modified), "text": text, "content_hash": h,
                "status": "active", "flags": _doc_flags(prev, m.id in bulk), "deleted_at": None}, ["doc_id"])
        signature = extraction_signature(cfg, h, ents, system)
        doc_state = {"extracted_key": prev["extracted_key"] if prev else None, "flags": prev["flags"] if prev else None}
        if force or needs_extraction(doc_state, signature):
            queue.append(doc_id)
    for r in deleted:
        summary["docs"]["deleted"] += 1
        if not dry_run:
            conn.execute("UPDATE source_docs SET status = 'deleted', deleted_at = ? WHERE doc_id = ?", (now, r["doc_id"]))
    if not dry_run:
        conn.commit()

    new_ids: list[str] = []
    if extract and not dry_run:
        for doc_id in queue:
            try:
                res = extract_doc(conn, cfg, doc_id, force=force, entities=ents)
            except llm.LLMUnavailable as e:
                summary["llm_unavailable"] = str(e)
                summary["errors"].append({"note": None, "error": str(e)})
                break
            except llm.LLMOutputError as e:
                title = conn.execute("SELECT title FROM source_docs WHERE doc_id = ?", (doc_id,)).fetchone()[0]
                summary["errors"].append({"note": title, "error": str(e)})
                continue
            summary["extracted"] += 1
            summary["api_calls"] += res["api_calls"]
            summary["cost_usd"] = round(summary["cost_usd"] + res["cost_usd"], 6)
            summary["not_golf"] += res["note_kind"] == "not_golf"
            for k, v in res["events"].items():
                if k == "new_ids":
                    new_ids += v
                else:
                    summary["events"][k] += v
    summary["extract_pending"] = len(queue) - summary["extracted"]
    if not dry_run:
        summary["duplicates_flagged"] = flag_duplicates(conn, new_ids=new_ids)
        summary["rounds_linked"] = link_rounds(conn)
        conn.execute("INSERT INTO imports(kind, path, sha256, imported_at, summary) VALUES ('note_sync', ?, NULL, ?, ?)",
                     (cfg.apple_notes_folder, now, dumps({k: v for k, v in summary.items() if k != "errors"})))
        conn.commit()
    return summary


def format_sync_summary(s: dict[str, Any]) -> str:
    """One short paragraph for the CLI / web page."""
    d, e = s["docs"], s["events"]
    lines = [
        f"{'[dry run] ' if s['dry_run'] else ''}Apple Notes '{s['folder']}' ({s['account']}): "
        f"{d['new']} new, {d['changed']} changed, {d['unchanged'] + d['touched']} unchanged, "
        f"{d['locked']} locked (skipped), {d['deleted']} deleted"
        + (f", {d['rekeyed']} re-keyed" if d["rekeyed"] else "") + ".",
        f"Events: {e['new']} new ({e['auto_accepted']} auto-accepted, {e['pending']} need review), "
        f"{e['updated']} updated, {e['orphaned']} orphaned, {e['superseded']} superseded.",
        f"API: {s['api_calls']} call(s), ${s['cost_usd']:.2f}."
        + (f" {s['extract_pending']} note(s) still to extract." if s["extract_pending"] else ""),
    ]
    lines += [f"Warning: {w}" for w in s["warnings"]]
    lines += [f"Error{' (' + str(x['note']) + ')' if x.get('note') else ''}: {x['error']}" for x in s["errors"]]
    return "\n".join(lines)
