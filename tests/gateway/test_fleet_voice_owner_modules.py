"""Fleet voice helpers that changed owner module in the v2026.9.24 port (FA-04-02).

v2026.9.24 decomposed ``gateway/run.py`` and ``plugins/platforms/discord/adapter.py``. The fleet port
re-homed these helpers; one test per helper pins both the new owner and the behaviour:

- ``_voice_event_guild_id``, ``_discord_dm_voice_join_enabled``, ``_configured_discord_voice_channel``,
  ``_play_voice_join_greeting``: ``GatewayRunner`` (run.py) -> ``GatewayVoiceMixin`` (run_voice.py)
- interlocutor guard: inline ``_handle_message`` block (run.py) -> ``GatewayInboundMixin`` (run_inbound.py)
- ``send_voice`` bound-VC branch: ``DiscordAdapter`` (adapter.py) -> ``DiscordMediaMixin`` (adapter_media.py)
- libopus probe: inline in ``connect()`` -> module function ``_load_opus_codec`` (adapter.py)
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import SessionSource
from gateway.platforms.event import MessageEvent, MessageType


def _make_runner(tmp_path):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._VOICE_MODE_PATH = tmp_path / "gateway_voice_mode.json"
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._is_user_authorized = lambda source: True
    return runner


def _make_discord_event(chat_id="555", guild_id=None):
    source = SessionSource(chat_id=chat_id, user_id="user1", platform=MagicMock())
    source.platform.value = "discord"
    source.thread_id = None
    event = MessageEvent(text="/voice join", message_type=MessageType.TEXT, source=source)
    event.message_id = "msg42"
    event.raw_message = SimpleNamespace(guild_id=guild_id, guild=None)
    return event


@pytest.mark.parametrize("name", [
    "_voice_event_guild_id",
    "_discord_dm_voice_join_enabled",
    "_configured_discord_voice_channel",
    "_play_voice_join_greeting",
])
def test_fleet_voice_helpers_are_owned_by_gateway_voice_mixin(name):
    from gateway.run import GatewayRunner
    from gateway.run_voice import GatewayVoiceMixin

    assert name in GatewayVoiceMixin.__dict__
    owner = next(k for k in GatewayRunner.__mro__ if name in k.__dict__)
    assert owner is GatewayVoiceMixin


def test_voice_event_guild_id_resolves_dm_bound_text_chat(tmp_path):
    runner = _make_runner(tmp_path)
    adapter = SimpleNamespace(_voice_text_channels={111: 555})

    assert runner._voice_event_guild_id(_make_discord_event(chat_id="555"), adapter) == 111
    assert runner._voice_event_guild_id(_make_discord_event(chat_id="999"), adapter) is None
    assert runner._voice_event_guild_id(_make_discord_event(guild_id=222), adapter) == 222
    assert runner._voice_event_guild_id(
        _make_discord_event(chat_id="555"), SimpleNamespace(_voice_text_channels=["junk"])) is None


@pytest.mark.asyncio
async def test_dm_voice_join_uses_single_configured_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_DM_VOICE_JOIN", "true")
    monkeypatch.setenv("DISCORD_ALLOWED_VOICE_CHANNELS", "999")
    monkeypatch.delenv("DISCORD_VOICE_JOIN_GREETING", raising=False)
    runner = _make_runner(tmp_path)
    channel = SimpleNamespace(id=999, name="Lounge", guild=SimpleNamespace(id=111))
    adapter = AsyncMock()
    adapter.client = MagicMock()
    adapter.client.get_channel = MagicMock(return_value=channel)
    adapter.join_voice_channel = AsyncMock(return_value=True)
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_input_callback = None
    event = _make_discord_event(chat_id="555")  # DM: no guild on the raw message
    runner.adapters[event.source.platform] = adapter

    result = await runner._handle_voice_channel_join(event)

    assert "Lounge" in result
    adapter.client.get_channel.assert_called_once_with(999)
    adapter.join_voice_channel.assert_awaited_once_with(channel)
    adapter.get_user_voice_channel.assert_not_called()
    assert adapter._voice_text_channels[111] == 555
    assert runner._voice_mode["discord:555"] == "voice_only"


@pytest.mark.asyncio
async def test_dm_voice_join_disabled_keeps_server_only_reply(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_DM_VOICE_JOIN", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_VOICE_CHANNELS", "999")
    runner = _make_runner(tmp_path)
    adapter = AsyncMock()
    event = _make_discord_event(chat_id="555")
    runner.adapters[event.source.platform] = adapter

    result = await runner._handle_voice_channel_join(event)

    assert result == "This command only works in a Discord server."
    adapter.join_voice_channel.assert_not_called()


@pytest.mark.asyncio
async def test_play_voice_join_greeting_plays_in_vc_and_cleans_up(tmp_path, monkeypatch):
    import gateway.run_voice as run_voice
    import tools.tts_tool as tts_tool

    monkeypatch.setenv("DISCORD_VOICE_JOIN_GREETING", "Hello from the fleet")
    out = tmp_path / "greeting.mp3"
    monkeypatch.setattr(run_voice, "build_auto_tts_output_path", lambda _platform: str(out))
    spoken = []

    def fake_tts(text, output_path):
        spoken.append(text)
        open(output_path, "wb").write(b"mp3")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)
    runner = _make_runner(tmp_path)
    adapter = SimpleNamespace(
        is_in_voice_channel=MagicMock(return_value=True),
        play_in_voice_channel=AsyncMock(return_value=True),
    )

    played = await runner._play_voice_join_greeting(adapter, 111, _make_discord_event(guild_id=111))

    assert played is True
    assert spoken == ["Hello from the fleet"]
    adapter.play_in_voice_channel.assert_awaited_once_with(111, str(out))
    assert not out.exists()


@pytest.mark.asyncio
async def test_interlocutor_guard_is_owned_by_inbound_mixin_and_skips_internal_events(tmp_path):
    from gateway.run import GatewayRunner
    from gateway.run_inbound import GatewayInboundMixin

    owner = next(k for k in GatewayRunner.__mro__ if "_hm_interlocutor_policy_blocked" in k.__dict__)
    assert owner is GatewayInboundMixin
    runner = _make_runner(tmp_path)
    runner.config = SimpleNamespace(interlocutor_policy={"enabled": True, "chat_only_user_ids": ["user1"]})
    event = _make_discord_event()

    assert await runner._hm_interlocutor_policy_blocked(event, event.source, True) is False


def test_send_voice_bound_vc_branch_lives_in_media_mixin():
    import inspect

    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.discord.adapter_media import DiscordMediaMixin

    assert DiscordAdapter.send_voice is DiscordMediaMixin.send_voice
    assert "_voice_text_channels" in inspect.getsource(DiscordMediaMixin.send_voice)


def test_load_opus_codec_falls_back_to_pyav_bundled_libopus(tmp_path, monkeypatch):
    import ctypes.util

    import plugins.platforms.discord.adapter as adapter_mod

    av_dir = tmp_path / "av"
    (av_dir / ".dylibs").mkdir(parents=True)
    av_opus = av_dir / ".dylibs" / "libopus.0.dylib"
    av_opus.write_bytes(b"")
    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(__file__=str(av_dir / "__init__.py")))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    loaded = []
    fake_opus = SimpleNamespace(
        load_opus=lambda path: loaded.append(path),
        is_loaded=lambda: str(av_opus) in loaded,
    )
    monkeypatch.setattr(adapter_mod, "discord", SimpleNamespace(opus=fake_opus))

    adapter_mod._load_opus_codec()

    assert loaded[-1] == str(av_opus)
    assert os.path.isfile(loaded[-1])
