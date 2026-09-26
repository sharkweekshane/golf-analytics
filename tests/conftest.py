from __future__ import annotations

import os

import pytest

import golf.config as config_mod
import golf.llm as llm
from golf import caddie, secrets
from golf.db import memory_db


@pytest.fixture(autouse=True)
def _no_keychain_no_meta_api(monkeypatch):
    """Every test, in every file: the real Keychain is never read (secrets.RUNNER fails the test), no client
    for Meta's API is built (caddie.CLIENT_FACTORY fails it), the Caddie's retry wait is instant, and
    MUSE_API_KEY / OPENAI_* from the shell can't leak in. A test that needs a key injects a fake one."""
    def no_keychain(args):
        raise AssertionError(f"unexpected Keychain call ({' '.join(list(args)[:2])})")

    def no_meta_api(**kw):
        raise AssertionError("unexpected Meta Model API client")

    monkeypatch.setattr(secrets, "RUNNER", no_keychain)
    monkeypatch.setattr(caddie, "CLIENT_FACTORY", no_meta_api)
    monkeypatch.setattr(caddie, "SLEEP", lambda seconds: None)
    monkeypatch.setattr(caddie, "_LEVELS", {})
    monkeypatch.setattr(config_mod, "DOTENV_IGNORED", set())
    for name in list(os.environ):
        if name == "MUSE_API_KEY" or name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


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
