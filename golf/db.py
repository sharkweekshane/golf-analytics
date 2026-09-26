"""SQLite access. One connection per process/request; schema applied idempotently."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 3                    # PRAGMA user_version once migrate() has run

# Columns added after the first release, as (table, column, declaration). schema.sql has them too;
# CREATE TABLE IF NOT EXISTS can't add them to an existing table, so migrate() does.
MIGRATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("rounds", "round_handicap_18b", "REAL"),
    ("rounds", "sg_overall", "REAL"),
    ("rounds", "sg_tee_to_green", "REAL"),
    ("rounds", "gir_no_chance", "INTEGER"),
    ("rounds", "tee_name", "TEXT"),
    ("round_overrides", "nine", "TEXT CHECK(nine IN ('front','back'))"),
    ("round_overrides", "putts", "TEXT CHECK(putts IN ('full','partial'))"),
    ("llm_calls", "billed_calls", "INTEGER NOT NULL DEFAULT 1"),
    ("llm_calls", "cost_total_usd", "REAL"),
)
VIEWS = ("timeline_raw", "v_rounds")    # dropped and recreated on upgrade (they select r.*)


def connect(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open golf.db. A writable connection also creates missing tables and upgrades an older
    database in place (see migrate); a read-only one (MCP) never writes."""
    db_path = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        apply_schema(conn)
    return conn


def memory_db() -> sqlite3.Connection:
    """Fresh in-memory database with the schema applied (tests)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> list[str]:
    """schema.sql (idempotent CREATE ... IF NOT EXISTS), then migrate(). Returns the columns added."""
    script = SCHEMA_PATH.read_text()
    conn.executescript(script)
    return migrate(conn, script)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection, script: str | None = None) -> list[str]:
    """Upgrade a golf.db created by an older schema.sql in place. Idempotent and cheap when current.

    - adds every MIGRATION_COLUMNS column a table lacks (ALTER TABLE ADD COLUMN, guarded by
      PRAGMA table_info, so re-running never fails);
    - backfills llm_calls.cost_total_usd from cost_usd (each old row was one billed call);
    - drops and recreates the views, then records SCHEMA_VERSION in PRAGMA user_version.
    Nothing is written when the database is already current (no lock taken on every connect).
    """
    added = []
    for table, column, decl in MIGRATION_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    if not added and conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return added
    with conn:
        conn.execute("UPDATE llm_calls SET cost_total_usd = cost_usd "
                     "WHERE cost_total_usd IS NULL AND cost_usd IS NOT NULL")
        for view in VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
    conn.executescript(script if script is not None else SCHEMA_PATH.read_text())   # recreates the views
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return added


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    """Deterministic JSON for storage and hashing."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def upsert(conn: sqlite3.Connection, table: str, row: dict, key: Iterable[str]) -> None:
    """INSERT ... ON CONFLICT(key) DO UPDATE for every non-key column in `row`."""
    key = list(key)
    cols = list(row)
    updates = [c for c in cols if c not in key]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    sql += f" ON CONFLICT({', '.join(key)}) DO "
    sql += ("UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)) if updates else "NOTHING"
    conn.execute(sql, [row[c] for c in cols])


def add_flag(flags_json: str | None, flag: str | dict) -> str:
    flags = loads(flags_json, [])
    if flag not in flags:
        flags.append(flag)
    return dumps(flags)
