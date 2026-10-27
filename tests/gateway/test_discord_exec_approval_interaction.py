"""Discord exec-approval buttons acknowledge the interaction before resolving work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_failed_acknowledgement_allows_retry_only_while_view_is_live(monkeypatch, timed_out):
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})
    # The gateway suite's fake Discord View does not implement the native lifecycle flag.
    monkeypatch.setattr(view, "is_finished", lambda: timed_out, raising=False)

    async def fail_acknowledgement():
        if timed_out:
            await view.on_timeout()
        raise OSError("Discord temporarily unavailable")

    response = SimpleNamespace(
        defer=AsyncMock(side_effect=fail_acknowledgement),
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, display_name="alice", roles=[]),
        response=response,
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(embeds=[]),
    )
    from tools import approval as approval_mod

    resolve = Mock(return_value=1)
    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", resolve)
    await view._resolve(interaction, "once", discord_mod.discord.Color.green(), "Approved once")
    resolve.assert_not_called()
    interaction.edit_original_response.assert_not_awaited()
    assert view.resolved is timed_out

    response.defer = AsyncMock()
    await view._resolve(interaction, "once", discord_mod.discord.Color.green(), "Approved once")
    if timed_out:
        resolve.assert_not_called()
        response.defer.assert_not_awaited()
        response.send_message.assert_awaited_once()
    else:
        resolve.assert_called_once_with("s", "once")
        interaction.edit_original_response.assert_awaited_once()
        assert view.resolved
