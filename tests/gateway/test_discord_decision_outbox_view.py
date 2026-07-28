"""Tests for DecisionOutboxView (claude_bridge escalation buttons) and
DiscordAdapter.post_claude_bridge_decision.

Mirrors tests/gateway/test_discord_clarify_buttons.py's fixture style.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Triggers the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    DecisionOutboxView,
    DiscordAdapter,
)
from gateway.config import PlatformConfig  # noqa: E402


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_interaction(*, user_id="42", display_name="Tester", include_message=True):
    user = SimpleNamespace(id=user_id, display_name=display_name)
    response = SimpleNamespace(
        edit_message=AsyncMock(),
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )
    if include_message:
        embed = MagicMock()
        embed.color = None
        embed.set_footer = MagicMock()
        message = SimpleNamespace(embeds=[embed])
    else:
        message = None
    return SimpleNamespace(user=user, response=response, message=message)


OPTIONS = [
    {"id": "a", "label": "Option A", "detail": "do the safe thing"},
    {"id": "b", "label": "Option B"},
]


class TestDecisionOutboxViewConstruction:
    def test_renders_one_button_per_option(self):
        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        assert len(view.children) == 2
        ids = [b.custom_id for b in view.children]
        assert ids == ["claude_bridge_decision:d1:a", "claude_bridge_decision:d1:b"]

    def test_recommended_option_gets_primary_style(self):
        import discord

        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id="b", timeout_seconds=None,
        )
        styles = {b.custom_id.rsplit(":", 1)[-1]: b.style for b in view.children}
        assert styles["b"] == discord.ButtonStyle.primary
        assert styles["a"] == discord.ButtonStyle.secondary

    def test_options_missing_id_are_skipped(self):
        view = DecisionOutboxView(
            decision_id="d1",
            options=[{"label": "no id here"}, {"id": "c", "label": "C"}],
            recommended_id=None,
            timeout_seconds=None,
        )
        assert len(view.children) == 1
        assert view.children[0].custom_id.endswith(":c")


class TestDecisionOutboxViewAnswer:
    @pytest.mark.asyncio
    async def test_answer_writes_decision_then_resolves(self, monkeypatch):
        written = []
        monkeypatch.setattr(
            "gateway.claude_bridge.write_decision_answer",
            lambda decision_id, choice, user_id: written.append((decision_id, choice, user_id)),
        )

        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        interaction = _make_interaction(user_id="42")

        await view._answer(interaction, "a", "Option A")

        assert written == [("d1", "a", "42")]
        assert view.resolved is True
        assert all(b.disabled for b in view.children)
        interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_answered_click_is_rejected(self, monkeypatch):
        monkeypatch.setattr("gateway.claude_bridge.write_decision_answer", lambda *a, **k: None)
        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        view.resolved = True
        interaction = _make_interaction()

        await view._answer(interaction, "a", "Option A")

        interaction.response.send_message.assert_called_once()
        interaction.response.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_failure_leaves_view_unresolved_for_retry(self, monkeypatch):
        """F3: a failed write_decision_answer must not silently confirm the
        click — the message shows an error and the buttons stay clickable so
        the user can retry, instead of the answer being lost forever."""
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("gateway.claude_bridge.write_decision_answer", _boom)

        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        interaction = _make_interaction(user_id="42")

        await view._answer(interaction, "a", "Option A")

        assert view.resolved is False
        assert all(not b.disabled for b in view.children)
        interaction.response.edit_message.assert_called_once()
        # The failure edit must be visibly distinct (error color), not the
        # success green — otherwise a lost answer looks identical to a saved
        # one.
        import discord
        assert interaction.message.embeds[0].color == discord.Color.red()

    @pytest.mark.asyncio
    async def test_write_success_shows_green_confirmation(self, monkeypatch):
        monkeypatch.setattr("gateway.claude_bridge.write_decision_answer", lambda *a, **k: None)
        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        interaction = _make_interaction(user_id="42")

        await view._answer(interaction, "a", "Option A")

        import discord
        assert interaction.message.embeds[0].color == discord.Color.green()

    @pytest.mark.asyncio
    async def test_on_timeout_writes_timeout_and_disables_buttons(self, monkeypatch):
        written = []
        monkeypatch.setattr(
            "gateway.claude_bridge.write_decision_timeout",
            lambda decision_id: written.append(decision_id),
        )
        view = DecisionOutboxView(
            decision_id="d1", options=OPTIONS, recommended_id=None, timeout_seconds=None,
        )
        embed = MagicMock()
        embed.color = None
        embed.set_footer = MagicMock()
        msg = SimpleNamespace(embeds=[embed], edit=AsyncMock())
        view._message = msg

        await view.on_timeout()

        assert written == ["d1"]
        assert view.resolved is True
        assert all(b.disabled for b in view.children)
        msg.edit.assert_called_once()


class TestPostClaudeBridgeDecision:
    @pytest.mark.asyncio
    async def test_posts_embed_with_view_and_stores_message_ref(self):
        adapter = _make_adapter()
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=999)))
        adapter._client.get_channel.return_value = channel

        decision = {
            "decision_id": "d1",
            "channel_id": "123",
            "question": "Which way?",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "recommended": "b",
        }

        await adapter.post_claude_bridge_decision(decision)

        channel.send.assert_called_once()
        _args, kwargs = channel.send.call_args
        view = kwargs["view"]
        assert isinstance(view, DecisionOutboxView)
        assert view._message.id == 999
