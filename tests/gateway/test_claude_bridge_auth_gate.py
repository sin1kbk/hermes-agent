"""Regression tests for C1: the Claude bridge must not bypass the gateway's
pairing/allowlist gate.

Before this fix, ``_select_message_handler()`` routed bridge-enabled
gateways straight to ``ClaudeBridge.handle_message`` (which has no notion of
gateway auth state), skipping the unauthorized-sender handling that
``_handle_message`` applies. An unpaired sender could reach ``claude -p``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.claude_bridge import ClaudeBridge
from gateway.config import ClaudeBridgeConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.providers import CLAUDE_BRIDGE_PROVIDER_ID


def _event(text="hello claude", user_id="user1", chat_type="dm", chat_id="c1"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
        ),
    )


def _make_runner(authorized_users=frozenset({"user1"})):
    """Minimal GatewayRunner double, mirroring the pattern used by
    tests/gateway/test_busy_session_auth_bypass.py."""
    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock()
    runner._is_user_authorized = lambda source: source.user_id in authorized_users
    runner._get_unauthorized_dm_behavior = lambda *a, **k: "pair"
    runner.pairing_store = MagicMock()
    runner.pairing_store._is_rate_limited.return_value = False
    runner.pairing_store.generate_code.return_value = "ABC123"
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner._adapter_for_source = lambda source: adapter
    runner.claude_bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir="/tmp"))
    return runner, adapter


class TestGateUnauthorizedMessage:
    @pytest.mark.asyncio
    async def test_authorized_user_passes(self):
        runner, _adapter = _make_runner()
        assert await runner._gate_unauthorized_message(_event(user_id="user1")) is True

    @pytest.mark.asyncio
    async def test_unauthorized_dm_blocked_and_offered_pairing(self):
        runner, adapter = _make_runner()
        allowed = await runner._gate_unauthorized_message(_event(user_id="stranger"))
        assert allowed is False
        adapter.send.assert_awaited_once()
        assert "pairing code" in adapter.send.await_args.args[1]

    @pytest.mark.asyncio
    async def test_unauthorized_group_blocked_silently(self):
        runner, adapter = _make_runner()
        allowed = await runner._gate_unauthorized_message(
            _event(user_id="stranger", chat_type="group")
        )
        assert allowed is False
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_internal_event_bypasses_gate(self):
        runner, adapter = _make_runner()
        event = _event(user_id="stranger")
        event.internal = True
        assert await runner._gate_unauthorized_message(event) is True
        adapter.send.assert_not_awaited()


class TestClaudeBridgeHandlerAuthorization:
    @pytest.mark.asyncio
    async def test_unauthorized_sender_never_reaches_claude_bridge(self):
        runner, adapter = _make_runner()
        called = False

        async def _fail_handle(event):
            nonlocal called
            called = True
            raise AssertionError("must not spawn claude for an unauthorized sender")

        runner.claude_bridge.handle_message = _fail_handle

        reply = await runner._claude_bridge_handler(_event(user_id="stranger"))

        assert reply is None
        assert called is False
        adapter.send.assert_awaited_once()  # pairing code offered instead

    @pytest.mark.asyncio
    async def test_authorized_sender_reaches_claude_bridge(self):
        runner, _adapter = _make_runner()
        seen = []

        async def _fake_handle(event):
            seen.append(event.text)
            return "ok from claude"

        runner.claude_bridge.handle_message = _fake_handle

        reply = await runner._claude_bridge_handler(_event(user_id="user1", text="hi"))

        assert reply == "ok from claude"
        assert seen == ["hi"]

    @pytest.mark.asyncio
    async def test_select_message_handler_returns_dynamic_router_when_enabled(self):
        runner, _adapter = _make_runner()
        handler = runner._select_message_handler()
        assert handler == runner._route_message
        assert handler != runner.claude_bridge.handle_message

    @pytest.mark.parametrize("provider", [CLAUDE_BRIDGE_PROVIDER_ID, "ollama-launch"])
    @pytest.mark.asyncio
    async def test_dynamic_router_preserves_authorization_gate_for_both_paths(
        self, monkeypatch, provider
    ):
        runner, adapter = _make_runner()
        runner._resolve_effective_message_provider = lambda _source: provider
        runner._scale_to_zero_note_real_inbound = lambda: None
        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook", lambda *args, **kwargs: []
        )
        runner.claude_bridge.handle_message = AsyncMock(
            side_effect=AssertionError("unauthorized sender reached the bridge")
        )

        reply = await runner._select_message_handler()(
            _event(user_id="stranger")
        )

        assert reply is None
        adapter.send.assert_awaited_once()
        runner.claude_bridge.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_ignored_channel_dropped_before_bridge(self):
        """The runner-level Slack ignored-channel guard (#51899) must hold on
        the bridge path too — ``ClaudeBridge`` is platform-agnostic and has
        no channel blacklist of its own."""
        runner, adapter = _make_runner()
        runner.config = SimpleNamespace(
            platforms={
                Platform.SLACK: SimpleNamespace(extra={"ignored_channels": ["C999"]})
            }
        )
        called = False

        async def _fail_handle(event):
            nonlocal called
            called = True
            raise AssertionError("ignored Slack channel must not reach claude_bridge")

        runner.claude_bridge.handle_message = _fail_handle

        event = MessageEvent(
            text="hello",
            source=SessionSource(
                platform=Platform.SLACK,
                chat_id="C999",
                chat_type="group",
                user_id="user1",
            ),
        )

        reply = await runner._claude_bridge_handler(event)

        assert reply is None
        assert called is False
        adapter.send.assert_not_awaited()
