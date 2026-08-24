"""``_claude_bridge_handle_busy_message`` — the native busy-session hook.

A claude-bridge turn never populates ``turn.agent``, so the gateway's native
queue/interrupt/steer machinery in ``GatewayRunner._handle_active_session_busy_message``
always treated it as un-steerable and fell back to queueing, regardless of
config (measured 2026-08-19: a Discord follow-up sent while a bridge turn was
running never reached ``ClaudeBridge.handle_message`` at all — it was merged
into the native pending-message queue first). This hook gives the bridge
first refusal on its own busy message before that machinery runs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event(text: str = "extra instruction") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
    )


def _runner(*, enabled: bool = True, provider: str = "claude-bridge", busy_mode: str = "steer"):
    runner = object.__new__(GatewayRunner)
    runner.claude_bridge = MagicMock()
    runner.claude_bridge.enabled = enabled
    runner.claude_bridge.config = MagicMock(busy_mode=busy_mode)
    runner.claude_bridge.try_steer = AsyncMock(return_value="⏩ sent")
    runner._resolve_effective_message_provider = MagicMock(return_value=(provider, ""))
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    return runner


def _adapter():
    adapter = MagicMock()
    adapter._send_with_retry = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_steers_and_acks_a_plain_follow_up():
    runner = _runner()
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is True
    runner.claude_bridge.try_steer.assert_awaited_once_with("discord:c1", "extra instruction")
    adapter._send_with_retry.assert_awaited_once()
    assert adapter._send_with_retry.call_args.kwargs["content"] == "⏩ sent"


@pytest.mark.asyncio
async def test_non_bridge_provider_falls_through_untouched():
    runner = _runner(provider="")
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is False
    runner.claude_bridge.try_steer.assert_not_awaited()
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_disabled_falls_through_without_resolving_provider():
    runner = _runner(enabled=False)
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is False
    runner._resolve_effective_message_provider.assert_not_called()
    runner.claude_bridge.try_steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_mode_queue_falls_through_preserving_pre_steering_behavior():
    runner = _runner(busy_mode="queue")
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is False
    runner.claude_bridge.try_steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_is_never_steered_as_literal_prompt_text():
    runner = _runner()
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(
        _event("/mode bypassPermissions"), "session-key", adapter,
    )

    assert handled is False
    runner.claude_bridge.try_steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_active_turn_falls_through_to_native_queue():
    runner = _runner()
    runner.claude_bridge.try_steer = AsyncMock(return_value=None)
    adapter = _adapter()

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is False
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_send_failure_still_returns_true_to_avoid_double_delivery():
    """The steer already landed on the CLI's stdin before the ack is sent —
    that side effect can't be undone, so a failed ack must never make the
    caller fall through to the native queue and deliver the same text twice."""
    runner = _runner()
    adapter = _adapter()
    adapter._send_with_retry.side_effect = RuntimeError("discord unreachable")

    handled = await runner._claude_bridge_handle_busy_message(_event(), "session-key", adapter)

    assert handled is True
    runner.claude_bridge.try_steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_steer_carries_the_attachment_note(tmp_path):
    """The busy path never reaches ClaudeBridge.handle_message, so it has to
    build the attachment note itself — otherwise a message sent mid-turn
    silently loses its attachments."""
    attachment = tmp_path / "shot.png"
    attachment.write_bytes(b"fake")
    event = _event("look at this")
    event.media_urls = [str(attachment)]
    event.media_types = ["image/png"]
    runner = _runner()

    handled = await runner._claude_bridge_handle_busy_message(event, "session-key", _adapter())

    assert handled is True
    steered = runner.claude_bridge.try_steer.await_args.args[1]
    assert str(attachment) in steered
    assert steered.endswith("look at this")


@pytest.mark.asyncio
async def test_busy_steer_handles_an_attachment_only_message(tmp_path):
    """An attachment with no caption used to fall through to the native queue
    because the text was empty."""
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fake")
    event = _event("")
    event.media_urls = [str(attachment)]
    event.media_types = ["application/pdf"]
    runner = _runner()

    handled = await runner._claude_bridge_handle_busy_message(event, "session-key", _adapter())

    assert handled is True
    assert str(attachment) in runner.claude_bridge.try_steer.await_args.args[1]


@pytest.mark.asyncio
async def test_busy_steer_without_text_or_attachments_falls_through():
    runner = _runner()

    handled = await runner._claude_bridge_handle_busy_message(_event(""), "session-key", _adapter())

    assert handled is False
    runner.claude_bridge.try_steer.assert_not_awaited()
