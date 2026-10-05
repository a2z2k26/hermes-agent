"""Tests for the Muse-ported voice binding persistence (fleet unification 2026-09-01).

Pure-function coverage: persist/clear round-trip against a temp HERMES_HOME.
The reconcile path needs a live discord client and is covered by the fleet's
on-host verifier + ocular tests instead.
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


class _Host:
    """Minimal object carrying just what the ported methods touch."""
    name = "test"

    def __init__(self):
        # bind the real implementations unbound-style
        spec = importlib.util.spec_from_file_location(
            "discord_adapter_mod", REPO / "plugins" / "platforms" / "discord" / "adapter.py"
        )
        self._mod = importlib.util.module_from_spec(spec)
        # Executing the full adapter module needs discord.py etc. — instead,
        # extract the three methods textually and exec them in a tiny namespace.
        src = (REPO / "plugins" / "platforms" / "discord" / "adapter.py").read_text()

        def _extract(name: str) -> str:
            i = src.index(f"    def {name}(")
            j = src.index("\n    def ", i + 10)
            # walk forward past decorator-less siblings until next def at same indent
            return src[i:j]

        ns: dict = {"os": os, "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)}
        block = _extract("_voice_bindings_path") + _extract("_persist_voice_binding") + _extract("_clear_voice_binding")
        exec("class _M:\n" + block, ns)
        M = ns["_M"]
        self._voice_bindings_path = M._voice_bindings_path.__get__(self)
        self._persist_voice_binding = M._persist_voice_binding.__get__(self)
        self._clear_voice_binding = M._clear_voice_binding.__get__(self)


@pytest.fixture()
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return _Host()


def test_persist_creates_atomic_binding_file(host, tmp_path):
    host._persist_voice_binding(111, 222, 333)
    f = tmp_path / "state" / "voice_bindings.json"
    assert f.is_file()
    data = json.loads(f.read_text())
    assert data["bindings"] == [
        {"guild_id": "111", "voice_channel_id": "222", "text_channel_id": "333"}
    ]
    assert not f.with_suffix(".json.tmp").exists()  # temp+rename, no leftovers


def test_persist_replaces_same_guild_keeps_others(host, tmp_path):
    host._persist_voice_binding(111, 222, 333)
    host._persist_voice_binding(999, 888, 777)
    host._persist_voice_binding(111, 444, 333)  # rebind guild 111
    data = json.loads((tmp_path / "state" / "voice_bindings.json").read_text())
    by_guild = {b["guild_id"]: b for b in data["bindings"]}
    assert by_guild["111"]["voice_channel_id"] == "444"
    assert by_guild["999"]["voice_channel_id"] == "888"
    assert len(data["bindings"]) == 2


def test_clear_removes_only_that_guild(host, tmp_path):
    host._persist_voice_binding(111, 222, 333)
    host._persist_voice_binding(999, 888, 777)
    host._clear_voice_binding(111)
    data = json.loads((tmp_path / "state" / "voice_bindings.json").read_text())
    assert [b["guild_id"] for b in data["bindings"]] == ["999"]


def test_clear_on_missing_file_is_silent(host):
    host._clear_voice_binding(123)  # must not raise


def test_persist_survives_corrupt_existing_file(host, tmp_path):
    f = tmp_path / "state" / "voice_bindings.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json")
    host._persist_voice_binding(111, 222, 333)
    data = json.loads(f.read_text())
    assert data["bindings"][0]["guild_id"] == "111"
