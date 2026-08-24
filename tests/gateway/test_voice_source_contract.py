"""The stored voice source must always rebuild into a SessionSource.

Regression guard: `join_voice_channel(..., source=...)` is a public entry point
documented for "automatic/programmatic voice joins", but a caller passing a
partial dict produced a `_voice_sources` entry that `SessionSource.from_dict()`
could not rebuild. The resulting `KeyError: 'platform'` surfaced inside the voice
listen loop, where it was caught and logged as a generic warning -- speech was
transcribed correctly and then discarded, with no indication why.
"""
import pytest

from gateway.session import SessionSource


def _normalise(source, text_channel_id=None, default_text_channel_id=None):
    """Mirror of the normalisation join_voice_channel applies before storing."""
    normalised = dict(source)
    normalised.setdefault("platform", "discord")
    if not normalised.get("chat_id"):
        bound = text_channel_id or default_text_channel_id
        if bound:
            normalised["chat_id"] = str(bound)
    normalised.setdefault("chat_type", "channel")
    return normalised


def test_partial_source_is_rebuildable_after_normalisation():
    raw = {"kind": "voice_auto_join", "user_id": "424242424242424242"}
    stored = _normalise(raw, text_channel_id=737373737373737373)
    rebuilt = SessionSource.from_dict(stored)
    assert rebuilt.platform.value == "discord"
    assert rebuilt.chat_id == "737373737373737373"
    assert rebuilt.user_id == "424242424242424242"


def test_partial_source_without_normalisation_is_not_rebuildable():
    """Documents the defect this guards against."""
    raw = {"kind": "voice_auto_join", "user_id": "424242424242424242"}
    with pytest.raises(KeyError):
        SessionSource.from_dict(raw)


def test_explicit_chat_id_is_preserved():
    raw = {"platform": "discord", "chat_id": "111111111111111111"}
    stored = _normalise(raw, text_channel_id=999999999999999999)
    assert SessionSource.from_dict(stored).chat_id == "111111111111111111"


def test_default_text_channel_is_used_when_no_binding_given():
    stored = _normalise({"kind": "x"}, default_text_channel_id=555555555555555555)
    assert SessionSource.from_dict(stored).chat_id == "555555555555555555"


def test_source_with_no_channel_at_all_is_left_unrebuildable():
    """Nothing to bind to: the caller must be told, not handed a broken source."""
    stored = _normalise({"kind": "x"})
    assert "chat_id" not in stored
