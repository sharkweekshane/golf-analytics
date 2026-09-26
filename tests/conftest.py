from __future__ import annotations

import pytest

import golf.config as config_mod
import golf.llm as llm
from golf.db import memory_db


@pytest.fixture
def conn():
    c = memory_db()
    yield c
    c.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Config pointing at a temp data dir; also what golf.config.get_config() returns."""
    (tmp_path / "config.toml").write_text(
        '[player]\nname = "Shane"\ntimezone = "America/New_York"\n'
        '[llm]\nmodel = "claude-opus-5"\neffort = "high"\nuse_fallbacks = false\n'
        '[notes]\napple_notes_folder = "Golf"\n[paths]\ndata_dir = "data"\n'
    )
    c = config_mod.load_config(tmp_path)
    monkeypatch.setattr(config_mod, "get_config", lambda: c)
    monkeypatch.setattr(llm, "get_config", lambda: c)
    return c


@pytest.fixture
def fake_llm(monkeypatch):
    """Queue of canned model outputs: fake_llm.push(json_text) then run code that calls the API."""

    class Fake:
        def __init__(self):
            self.outputs: list[str] = []
            self.requests: list[dict] = []

        def push(self, text: str, stop_reason: str = "end_turn"):
            self.outputs.append((text, stop_reason))

        def __call__(self, request, use_fallbacks=False):
            self.requests.append(request)
            if not self.outputs:
                raise AssertionError("fake_llm: unexpected API call (no canned output queued)")
            text, stop = self.outputs.pop(0)
            return llm.SendResult(text=text, stop_reason=stop, model=request["model"],
                                  usage={"input_tokens": 4000, "output_tokens": 1500})

    fake = Fake()
    monkeypatch.setattr(llm, "SEND", fake)
    return fake
