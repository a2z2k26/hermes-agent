"""Voice-channel / auto-TTS methods for GatewayRunner (split out of ``gateway/run.py``; bound via
the MRO). ``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
import sys
import time
import weakref
from contextlib import suppress
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.platforms.base import build_auto_tts_output_path
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")  # log-record parity with the origin module

# Adapter-side per-chat auto-TTS override sets (``/voice off`` vs explicit ``/voice on``/``tts``).
_OFF_SET, _ON_SET = "_auto_tts_disabled_chats", "_auto_tts_enabled_chats"
_VOICE_MODES = {"off", "voice_only", "all"}


class GatewayVoiceMixin:
    def _voice_key(self, platform: Platform, chat_id: str, profile: Optional[str] = None) -> str:
        """``<profile>:<platform>:<chat_id>`` under multiplexing (else two bots in one channel
        share a key and one ``/voice`` flips the other's); default keeps ``<platform>:<chat>``.

        Under multiplexing the key is additionally namespaced by the profile whose bot speaks in the chat
        (``<profile>:<platform>:<chat_id>``); the default profile keeps the historical
        ``<platform>:<chat_id>`` shape so persisted state stays valid. See #75198.
        """
        base = f"{platform.value}:{chat_id}"
        profile = profile.strip() if isinstance(profile, str) else ""
        return base if not profile or profile == "default" else f"{profile}:{base}"

    def _voice_key_for_source(self, source: SessionSource) -> str:
        """Voice mode belongs to the (bot, chat) pair: namespace is the profile that OWNS the
        receiving adapter, not the routed profile."""
        profile = self._adapter_profile_for_source(source)
        return self._voice_key(source.platform, source.chat_id, profile=profile)

    def _bind_voice_input_callback(self, adapter) -> None:
        """Route voice transcripts back through the adapter that captured them."""
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = functools.partial(
                self._handle_voice_channel_input, adapter=adapter)

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        items = {str(k): m for k, m in data.items() if m in _VOICE_MODES}
        for key in (k for k in items if ":" not in k):  # legacy unprefixed key: warn and skip
            logger.warning(
                "Skipping legacy unprefixed voice mode key %r during migration. "
                "Re-enable voice mode on that chat to rebuild the prefixed key.", key)
        return {k: m for k, m in items.items() if ":" in k}

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._voice_mode, indent=2)
            self._VOICE_MODE_PATH.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    @staticmethod
    def _toggle_adapter_auto_tts_set(adapter, chat_id: str, on: bool, *, enable: bool) -> None:
        """Add/discard ``chat_id`` in the adapter's enabled (``enable=True``) or disabled set;
        adding also clears the other set (``/voice off`` and ``/voice on``/``tts`` override)."""
        add_to, clear_from = (_ON_SET, _OFF_SET) if enable else (_OFF_SET, _ON_SET)
        if not isinstance(target := getattr(adapter, add_to, None), set):
            return
        if not on:
            target.discard(chat_id)
            return
        target.add(chat_id)
        if isinstance(other := getattr(adapter, clear_from, None), set):
            other.discard(chat_id)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        self._toggle_adapter_auto_tts_set(adapter, chat_id, disabled, enable=False)

    def _set_adapter_auto_tts_enabled(self, adapter, chat_id: str, enabled: bool) -> None:
        self._toggle_adapter_auto_tts_set(adapter, chat_id, enabled, enable=True)

    def _apply_voice_mode(self, adapter, voice_key: str, chat_id: str, mode: str) -> None:
        """Record+persist ``mode``; mirror into adapter sets (``off`` -> disabled, else enabled)."""
        self._voice_mode[voice_key] = mode
        self._save_voice_modes()
        self._toggle_adapter_auto_tts_set(adapter, chat_id, True, enable=mode != "off")

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live adapter: ``_auto_tts_default`` from
        ``voice.auto_tts``; enabled (voice_only/all) / disabled (off) sets from ``_voice_mode``."""
        platform = getattr(adapter, "platform", None)
        if not isinstance(platform, Platform):
            return
        chat_sets = [
            (chats, modes)
            for name, modes in ((_OFF_SET, {"off"}), (_ON_SET, {"voice_only", "all"}))
            if isinstance(chats := getattr(adapter, name, None), set)
        ]
        if not chat_sets:
            return
        try:
            from hermes_cli.config import load_config  # lazy: no gateway -> hermes_cli module dep
            auto_tts_default = bool((load_config().get("voice") or {}).get("auto_tts", False))
        except Exception:
            auto_tts_default = False
        if hasattr(adapter, "_auto_tts_default"):
            adapter._auto_tts_default = auto_tts_default
        prefix = self._voice_key(platform, "", profile=getattr(adapter, "_owner_profile", None))
        for chats, modes in chat_sets:
            chats.clear()
            chats.update(key[len(prefix):] for key, mode in self._voice_mode.items()
                         if mode in modes and key.startswith(prefix))

    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        raw = getattr(event, "raw_message", None)
        if getattr(raw, "guild_id", None):  # slash command interaction
            return int(raw.guild_id)
        return raw.guild.id if getattr(raw, "guild", None) else None  # regular message

    # ── Fleet voice helpers (ported from fleet ee2a96a154 + be19f47ce9 merge fixes; formerly
    # GatewayRunner methods in gateway/run.py) ──
    def _voice_event_guild_id(self, event: MessageEvent, adapter: Any = None) -> Optional[int]:
        """Resolve the active Discord VC guild for server or DM-bound voice sessions."""
        guild_id = self._get_guild_id(event)
        if guild_id:
            return guild_id
        if adapter is not None:
            chat_id = str(getattr(event.source, "chat_id", ""))
            voice_text_channels = getattr(adapter, "_voice_text_channels", {})
            if not isinstance(voice_text_channels, dict):
                voice_text_channels = {}
            for gid, text_channel_id in voice_text_channels.items():
                if str(text_channel_id) == chat_id:
                    try:
                        return int(gid)
                    except (TypeError, ValueError):
                        return None
        return None

    @staticmethod
    def _discord_dm_voice_join_enabled() -> bool:
        """Return whether /voice join from DM may use the configured VC allowlist."""
        return os.environ.get("DISCORD_DM_VOICE_JOIN", "").strip().lower() in {
            "1", "true", "yes", "on"}

    async def _configured_discord_voice_channel(self, adapter: Any):
        """Resolve the single configured Discord voice channel for DM /voice join."""
        if not self._discord_dm_voice_join_enabled():
            return None
        allowed_voice_channel_ids = {
            item.strip()
            for item in os.environ.get("DISCORD_ALLOWED_VOICE_CHANNELS", "").split(",")
            if item.strip()
        }
        if len(allowed_voice_channel_ids) != 1:
            return None
        channel_id = next(iter(allowed_voice_channel_ids))
        try:
            numeric_channel_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        client = getattr(adapter, "client", None) or getattr(adapter, "_client", None)
        if client is None:
            return None
        channel = None
        get_channel = getattr(client, "get_channel", None)
        if callable(get_channel):
            channel = get_channel(numeric_channel_id)
        fetch_channel = getattr(client, "fetch_channel", None)
        if channel is None and callable(fetch_channel):
            fetch_result = fetch_channel(numeric_channel_id)
            channel = await fetch_result if inspect.isawaitable(fetch_result) else fetch_result
        return channel

    async def _play_voice_join_greeting(self, adapter: Any, guild_id: int, event: MessageEvent) -> bool:
        """Play an env-configured greeting in the active Discord voice channel."""
        greeting = os.environ.get("DISCORD_VOICE_JOIN_GREETING", "").strip()
        if not greeting:
            return False
        play_in_voice_channel = getattr(adapter, "play_in_voice_channel", None)
        is_in_voice_channel = getattr(adapter, "is_in_voice_channel", None)
        if not callable(play_in_voice_channel) or not callable(is_in_voice_channel):
            return False
        connected = is_in_voice_channel(guild_id)
        if inspect.isawaitable(connected) or not connected:
            return False
        audio_path = None
        actual_paths: List[str] = []
        try:
            from tools.tts_tool import text_to_speech_tool

            audio_path = build_auto_tts_output_path(event.source.platform)
            result_json = await asyncio.to_thread(
                text_to_speech_tool, text=greeting, output_path=audio_path)
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Voice join greeting TTS returned invalid JSON: %s",
                               result_json[:200] if result_json else result_json)
                return False
            actual_paths = result.get("file_paths") or [result.get("file_path", audio_path)]
            actual_paths = [str(path) for path in actual_paths if path and os.path.isfile(path)]
            if not result.get("success") or not actual_paths:
                logger.warning("Voice join greeting TTS failed: %s", result.get("error"))
                return False
            played = False
            for actual_path in actual_paths:
                played = bool(await play_in_voice_channel(guild_id, actual_path)) or played
            return played
        except Exception as e:
            logger.warning("Voice join greeting playback failed: %s", e, exc_info=True)
            return False
        finally:
            for p in ({audio_path, *actual_paths} - {None}):
                with suppress(OSError):
                    os.unlink(p)

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel, or a configured VC from DM."""
        adapter = self._delivery_adapter_for(event.source)
        if not hasattr(adapter, "join_voice_channel"):
            return "Voice channels are not supported on this platform."
        # Optional deployment guard: when set, Hermes may only join the configured Discord
        # voice channel(s). Protects shared servers from /voice join in the wrong VC.
        allowed_voice_channel_ids = {
            item.strip()
            for item in os.environ.get("DISCORD_ALLOWED_VOICE_CHANNELS", "").split(",")
            if item.strip()
        }
        guild_id = self._get_guild_id(event)
        voice_channel = None
        if not guild_id:
            voice_channel = await self._configured_discord_voice_channel(adapter)
            if voice_channel is None:
                return "This command only works in a Discord server."
            guild_id = int(getattr(getattr(voice_channel, "guild", None), "id", 0) or 0)
            if not guild_id:
                return "Could not resolve the configured Discord voice channel's server."
        if voice_channel is None:
            voice_channel = await adapter.get_user_voice_channel(guild_id, event.source.user_id)
            if not voice_channel:
                return "You need to be in a voice channel first."
        if allowed_voice_channel_ids and str(getattr(voice_channel, "id", "")) not in allowed_voice_channel_ids:
            allowed_channels = ", ".join(
                f"<#{channel_id}>" for channel_id in sorted(allowed_voice_channel_ids))
            return f"I can only join the configured voice channel: {allowed_channels}."
        # Wire callbacks BEFORE join so voice input arriving right after connection is not lost.
        self._bind_voice_input_callback(adapter)
        voice_profile = self._adapter_profile_for_source(event.source)
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = functools.partial(
                self._handle_voice_timeout_cleanup, adapter=adapter)
        # Let the adapter's inactivity timer see the live voice-reply mode so it doesn't
        # disconnect a deliberately text-only (/voice off) session.
        if hasattr(adapter, "_voice_mode_getter"):
            adapter._voice_mode_getter = lambda chat_id: self._voice_mode.get(
                self._voice_key(Platform.DISCORD, str(chat_id), profile=voice_profile), "off")
        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to join voice channel: %s", e)
            adapter._voice_input_callback = None
            if not any(tok in str(e).lower() for tok in ("pynacl", "nacl", "davey")):
                return f"Failed to join voice channel: {e}"
            return ("Voice dependencies are missing (PyNaCl / davey). "
                    f"Install with: `{sys.executable} -m pip install PyNaCl`")
        if not success:
            adapter._voice_input_callback = None
            return "Failed to join voice channel. Check bot permissions (Connect + Speak)."
        adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
        if hasattr(adapter, "_voice_sources"):
            adapter._voice_sources[guild_id] = event.source.to_dict()
        # Fleet 739e47e78a: voice_only so the bound text chat stays text.
        self._apply_voice_mode(adapter, self._voice_key_for_source(event.source),
                               event.source.chat_id, "voice_only")
        greeting_played = await self._play_voice_join_greeting(adapter, guild_id, event)
        greeting_note = "\nGreeting played in the voice channel." if greeting_played else ""
        voice_channel_name = getattr(voice_channel, "name", "configured voice channel")
        return (f"Joined voice channel **{voice_channel_name}**.\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
                f"{greeting_note}")

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        adapter = self._delivery_adapter_for(event.source)
        guild_id = self._voice_event_guild_id(event, adapter)
        if not (guild_id and hasattr(adapter, "leave_voice_channel")
                and hasattr(adapter, "is_in_voice_channel")
                and adapter.is_in_voice_channel(guild_id)):
            return "Not in a voice channel."
        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning("Error leaving voice channel: %s", e)
        # Always clean up state even if leave raised an exception
        self._apply_voice_mode(adapter, self._voice_key_for_source(event.source),
                               event.source.chat_id, "off")
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str, *, adapter=None) -> None:
        """Adapter callback on voice-channel timeout: clear runner-side voice_mode state.
        ``adapter`` (bound at join) is that profile's bot, not always ``self.adapters[DISCORD]``."""
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        key = self._voice_key(Platform.DISCORD, chat_id,
                              profile=getattr(adapter, "_owner_profile", None))
        self._apply_voice_mode(adapter, key, chat_id, "off")

    def _is_duplicate_voice_transcript(self, guild_id: int, user_id: int, transcript: str) -> bool:
        """Suppress repeated STT outputs for one recent utterance (voice capture can emit it twice a
        few seconds apart -> a second queued run and overlapping spoken replies)."""
        normalized = re.sub(r"[^\w\s]", "", re.sub(r"\s+", " ", transcript).strip().lower())
        if not normalized:
            return False
        now, key = time.monotonic(), (guild_id, user_id)
        if not isinstance(recent_store := getattr(self, "_recent_voice_transcripts", None), dict):
            recent_store = self._recent_voice_transcripts = {}
        recent = [(ts, txt) for ts, txt in recent_store.get(key, []) if now - ts <= 12.0]
        if any(prior == normalized or (min(len(prior), len(normalized)) >= 16
                                       and SequenceMatcher(None, prior, normalized).ratio() >= 0.95)
               for _, prior in recent):
            recent_store[key] = recent
            return True
        recent_store[key] = (recent + [(now, normalized)])[-5:]
        return False

    @staticmethod
    def _voice_input_source(adapter, guild_id: int, user_id: int, text_ch_id) -> SessionSource:
        """Bound text channel's own source when available (voice shares the text conversation's
        session), else a synthetic one."""
        if source_data := getattr(adapter, "_voice_sources", {}).get(guild_id):
            source = SessionSource.from_dict(source_data)
            source.user_id = source.user_name = str(user_id)
        else:
            source = SessionSource(
                platform=Platform.DISCORD, chat_id=str(text_ch_id), user_id=str(user_id),
                user_name=str(user_id), chat_type="channel",
                profile=getattr(adapter, "_owner_profile", None))
        # Serialization drops transport provenance; auth must still follow the receiving bot.
        source._transport_adapter_ref = weakref.ref(adapter)
        return source

    async def _handle_voice_channel_input(
        self, guild_id: int, user_id: int, transcript: str, *, adapter=None
    ):
        """Handle transcribed voice from a voice channel. ``adapter`` captured the audio; under
        multiplexing each profile's bot dispatches through its own adapter, never the default's."""
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        text_ch_id = adapter._voice_text_channels.get(guild_id) if adapter else None
        if not text_ch_id:
            return
        source = self._voice_input_source(adapter, guild_id, user_id, text_ch_id)
        # The cached source still carries the previous speaker's identity (per-sender routes,
        # #106019): drop the pin so the seam re-resolves for THIS speaker.
        from gateway.session_identity import clear_identity
        clear_identity(source)
        if self._canonicalize(source, transport_profile=getattr(adapter, "_owner_profile", None)) is None:
            logger.warning("Dropping voice input: its profile route targets an unserved profile")
            return
        # Validate the session owner against the current allowlist before auto-resuming. A session created
        # before TELEGRAM_ALLOWED_USERS (or equivalent) was configured, or before the owner was removed from
        # it, must not silently receive a full agent response on gateway restart just because it has a
        # resume-pending marker (issue #23778).
        if not self._is_user_authorized_for_source(source):
            logger.debug("Unauthorized voice input from user %d, ignoring", user_id)
            return
        if self._is_duplicate_voice_transcript(guild_id, user_id, transcript):
            logger.info("Suppressing duplicate voice transcript for guild=%s user=%s: %s",
                        guild_id, user_id, transcript[:100])
            return
        # Echo the transcript into the text channel (after auth, with mention sanitization).
        with suppress(Exception):
            channel = adapter._client.get_channel(text_ch_id)
            if channel:
                safe_text = transcript[:2000].replace("@everyone", "@\u200beveryone")
                safe_text = safe_text.replace("@here", "@\u200bhere")
                await channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")
        # Bound text channel's channel_prompt: voice input gets the same per-channel context.
        channel_prompt = None
        if callable(resolver := getattr(adapter, "_resolve_channel_prompt", None)):
            with suppress(Exception):
                resolved = resolver(str(text_ch_id))
                channel_prompt = resolved if isinstance(resolved, str) else None
        # Synthetic MessageEvent for the normal pipeline; the SimpleNamespace raw_message lets
        # _get_guild_id() extract guild_id so _send_voice_reply() plays audio in the voice channel.
        event = MessageEvent(
            source=source, text=transcript, message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
            channel_prompt=channel_prompt)
        await adapter.handle_message(event)

    def _should_send_voice_reply(
        self, event: MessageEvent, response: str, agent_messages: list, already_sent: bool = False
    ) -> bool:
        """False when voice_mode is off for this chat, the response is empty/an error, the agent
        already called text_to_speech this turn, or voice input + base adapter auto-TTS handled it
        — UNLESS streaming consumed the response (already_sent): then the runner must do it."""
        if not response or response.startswith("Error:"):
            return False
        chat_id = event.source.chat_id
        voice_mode = self._voice_mode.get(self._voice_key_for_source(event.source))
        is_voice_input = event.message_type == MessageType.VOICE
        adapter = self._delivery_adapter_for(event.source)
        adapter_auto_tts = False
        with suppress(Exception):  # adapters without the probe read as False
            adapter_auto_tts = bool(adapter._should_auto_tts_for_chat(chat_id))
        # ``voice.auto_tts`` (synced into the adapter at startup) is the fallback only when the
        # chat has no explicit mode; the chat-level all/voice_only/off choice takes precedence.
        if not (voice_mode == "all" or (voice_mode == "voice_only" and is_voice_input)
                or (voice_mode is None and adapter_auto_tts)):
            logger.debug(
                "Auto voice reply skipped: mode=%s adapter_auto_tts=%s chat=%s platform=%s",
                voice_mode, adapter_auto_tts, chat_id, event.source.platform.value)
            return False
        # Dedup: agent already called the TTS tool in THIS turn (from the last user message on).
        start = next((i for i, m in reversed(list(enumerate(agent_messages)))
                      if m.get("role") == "user"), 0)
        if any((tc.get("function") or {}).get("name") == "text_to_speech"
               for msg in agent_messages[start:] if msg.get("role") == "assistant"
               for tc in (msg.get("tool_calls") or [])):
            return False
        # Dedup: base adapter auto-TTS already handles voice input (play_tts plays in VC when
        # connected) — unless streaming consumed the text (already_sent): then the runner must.
        return not (is_voice_input and not already_sent)

    def _should_echo_stt_transcripts(self) -> bool:
        return bool(getattr(self.config, "stt_echo_transcripts", True))

    async def _send_voice_reply(self, event: MessageEvent, text: str) -> None:
        """Generate TTS audio and send as a voice message before the text reply. The TTS tool
        may return one combined file or several separately valid ones (combination unavailable /
        over a platform limit); legacy single-file results keep working."""
        audio_path, actual_paths = None, []
        try:
            from tools.tts_text_normalize import _strip_markdown_for_tts
            from tools.tts_tool import text_to_speech_tool
            tts_text = _strip_markdown_for_tts(text)
            if not tts_text:
                return
            # Platforms whose native voice bubbles require Ogg/Opus (OPUS_VOICE_PLATFORMS) get an
            # explicit .ogg path; the TTS tool's container repair guarantees real Ogg/Opus bytes.
            audio_path = build_auto_tts_output_path(event.source.platform)
            raw = await asyncio.to_thread(text_to_speech_tool, text=tts_text,
                                          output_path=audio_path)
            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Auto voice reply TTS returned invalid JSON: %s",
                               raw[:200] if raw else raw)
                return
            candidates = result.get("file_paths") or [result.get("file_path", audio_path)]
            paths = [str(p) for p in candidates if p and os.path.isfile(p)]
            if not result.get("success") or not paths:
                logger.warning("Auto voice reply TTS failed: %s", result.get("error"))
                return
            actual_paths = paths
            await self._deliver_voice_reply(event, actual_paths)
        except Exception as e:
            logger.warning("Auto voice reply failed: %s", e, exc_info=True)
        finally:
            for p in ({audio_path, *actual_paths} - {None}):
                with suppress(OSError):
                    os.unlink(p)

    async def _deliver_voice_reply(self, event: MessageEvent, audio_paths: List[str]) -> None:
        """Play the files in the connected voice channel, else send them as voice messages."""
        adapter = self._delivery_adapter_for(event.source)
        guild_id = self._voice_event_guild_id(event, adapter)
        play = getattr(adapter, "play_in_voice_channel", None)
        is_in_vc = getattr(adapter, "is_in_voice_channel", None)
        if guild_id and callable(play) and callable(is_in_vc) and is_in_vc(guild_id):
            for path in audio_paths:
                await play(guild_id, path)
            return
        if not callable(send_voice := getattr(adapter, "send_voice", None)):
            return
        reply_anchor = self._reply_anchor_for_event(event)
        # notify=True mirrors the final-text path in platforms/base.py so notification-gating
        # adapters (Telegram "important" mode) deliver it. Clone: shared w/ typing-indicator state.
        thread_meta = dict(self._thread_metadata_for_source(event.source, reply_anchor) or {})
        thread_meta["notify"] = True
        for path in audio_paths:
            await send_voice(chat_id=event.source.chat_id, audio_path=path, reply_to=reply_anchor,
                             metadata=thread_meta)
