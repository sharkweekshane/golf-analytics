"""entities.yaml parsing: the hash feeds the extraction cache key, so it must not depend on the process."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from golf.notes.entities import parse_entities

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRICKY = {
    "coaches": [{"name": "Mike Rossi", "aliases": ["Mike", "mike", "coach Mike"], "initials": ["MR", "mr", "Mr"]}],
    "locations": [{"name": "Range Barn", "kind": "range", "aliases": ["barn", "Barn", "BARN"]}],
    "bag": ["Driver", "7i"],
}


def test_aliases_differing_only_in_case_have_a_total_order():
    e = parse_entities(TRICKY)
    assert e.coaches["Mike Rossi"] == ["coach Mike", "Mike", "mike", "MR", "Mr", "mr"]
    assert e.locations["Range Barn"]["aliases"] == ["BARN", "Barn", "barn"]
    assert parse_entities({**TRICKY, "coaches": [dict(TRICKY["coaches"][0], aliases=["mike", "Mike"])]}).hash == \
        parse_entities({**TRICKY, "coaches": [dict(TRICKY["coaches"][0], aliases=["Mike", "mike"])]}).hash


def test_hash_is_the_same_under_every_hash_seed():
    """QA: with aliases [Mike, mike] the hash flipped with PYTHONHASHSEED, re-billing every note."""
    code = ("import json, sys; from golf.notes.entities import parse_entities as p; "
            "e = p(json.loads(sys.argv[1])); print(e.hash, e.prompt_context())")
    import json

    outputs = set()
    for seed in ("1", "2", "3", "4", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(PROJECT_ROOT)}
        out = subprocess.run([sys.executable, "-c", code, json.dumps(TRICKY)], env=env, capture_output=True,
                             text=True, check=True, cwd=PROJECT_ROOT)
        outputs.add(out.stdout)
    assert len(outputs) == 1
    assert parse_entities(TRICKY).hash in outputs.pop()
