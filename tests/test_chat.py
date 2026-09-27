from __future__ import annotations

import json
import re

from golf.chat import DICTIONARY, chat_bundle, write_bundle
from golf.demo import seed_demo


def test_bundle_tables_match_dictionary(conn, cfg):
    seed_demo(conn)
    b = chat_bundle(conn, cfg)
    for table, cols in DICTIONARY.items():
        for row in b["tables"][table]:
            assert set(row) == set(cols), table
    assert b["tables"]["rounds"] and b["tables"]["holes"] and b["tables"]["shots"]
    assert b["headline"]["kpis"]


def test_bundle_is_public_safe(conn, cfg):
    seed_demo(conn)
    raw_ids = [r[0] for r in conn.execute("SELECT round_id FROM rounds")]
    text = json.dumps(chat_bundle(conn, cfg))
    assert not any(rid in text for rid in raw_ids)            # only r1..rN aliases
    b = chat_bundle(conn, cfg)
    keys = {k for rows in b["tables"].values() for r in rows for k in r}
    assert not any("lat" in k or "lon" in k for k in keys)
    assert not re.search(r"-?\d{1,3}\.\d{5,}", text.replace(b["generated_at"], ""))   # no coordinate-like numbers
    assert "cost" not in text and "/Users/" not in text
    aliases = [r["round"] for r in chat_bundle(conn, cfg)["tables"]["rounds"]]
    assert aliases == [f"r{i}" for i in range(1, len(aliases) + 1)]


def test_partial_putts_hidden(conn, cfg):
    seed_demo(conn)
    conn.execute("UPDATE rounds SET dq_flags = '[\"putts_partial\"]', putts_tracked = 0 WHERE round_id = "
                 "(SELECT round_id FROM rounds ORDER BY played_on_local LIMIT 1)")
    first = chat_bundle(conn, cfg)["tables"]["rounds"][0]
    assert first["putts"] is None or first["putts_partial"] is False


def test_write_bundle(conn, cfg, tmp_path):
    seed_demo(conn)
    path = write_bundle(conn, cfg, tmp_path / "chat")
    data = json.loads(path.read_text())
    assert data["schema"] == 1 and path.name == "golf-data.json"


def test_empty_db(conn, cfg):
    b = chat_bundle(conn, cfg)
    assert b["tables"]["rounds"] == [] and b["data_through"] is None


def test_public_site_bundle_leaves_out_verbatim_note_excerpts(conn, cfg):
    seed_demo(conn)
    full = chat_bundle(conn, cfg)
    assert any(e.get("excerpt") for e in full["tables"]["events"])       # the claude.ai copy keeps them
    public = chat_bundle(conn, cfg, excerpts=False)
    assert public["tables"]["events"] and all("excerpt" not in e for e in public["tables"]["events"])
    assert "excerpt" not in public["dictionary"]["events"] and "excerpt" in DICTIONARY["events"]
    for table, cols in public["dictionary"].items():
        for row in public["tables"][table]:
            assert set(row) == set(cols), table
    text = json.dumps(public)
    assert not any(e["excerpt"] in text for e in full["tables"]["events"] if e.get("excerpt") and len(e["excerpt"]) > 20)
