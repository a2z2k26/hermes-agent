"""Discord exec-approval buttons acknowledge the interaction before resolving work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.discord import adapter as discord_mod
from plugins.platforms.discord.adapter import ExecApprovalView


@pytest.mark.asyncio
async def test_exec_approval_defers_before_resolving_and_edits_afterward(monkeypatch):
    """A component click must be acknowledged before approval resolution can delay it."""
    response = SimpleNamespace(
        defer=AsyncMock(),
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, display_name="alice", roles=[]),
        response=response,
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(embeds=[]),
    )
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})

    def resolve(_session_key, _choice):
        assert response.defer.await_count == 1
        return 0

    from tools import approval as approval_mod

    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", resolve)
    monkeypatch.setattr(discord_mod.discord.Color, "dark_grey", lambda: "grey", raising=False)

    await view._resolve(interaction, "always", discord_mod.discord.Color.purple(), "Approved permanently")

    response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    response.edit_message.assert_not_awaited()
    assert view.resolved is True
    assert all(getattr(child, "disabled", False) for child in view.children)
