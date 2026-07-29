"""Tests for routing turns Claude ran unprompted back to their channel.

A finished background task makes the CLI run a turn no message asked for.
Its report is not a reply to anything, so the gateway posts it into the
channel the bridge keyed the session on.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.claude_bridge import ClaudeBridge, parse_channel_key
from gateway.config import ClaudeBridgeConfig, Platform
from gateway.run import GatewayRunner


def test_parse_channel_key_inverts_channel_key():
    assert parse_channel_key("discord:c1:t1") == ("discord", "c1", "t1")
    assert parse_channel_key("discord:c1") == ("discord", "c1", None)
    assert parse_channel_key("discord:-") == ("discord", None, None)


def _adapter(platform=Platform.DISCORD):
    adapter = MagicMock()
    adapter.platform = platform
    adapter.send = AsyncMock()
    return adapter


def _runner(adapter, *, enabled=True):
    runner = object.__new__(GatewayRunner)
    runner.claude_bridge = ClaudeBridge(
        ClaudeBridgeConfig(enabled=enabled, working_dir="/tmp")
    )
    runner.adapters = {Platform.DISCORD: adapter} if adapter is not None else {}
    return runner


@pytest.mark.asyncio
async def test_notice_is_posted_into_the_thread_it_belongs_to():
    adapter = _adapter()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1:t1", "background task finished")

    adapter.send.assert_awaited_once_with(
        "c1", "background task finished", metadata={"thread_id": "t1"}
    )


@pytest.mark.asyncio
async def test_notice_without_a_thread_goes_to_the_channel():
    adapter = _adapter()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1", "done")

    adapter.send.assert_awaited_once_with("c1", "done", metadata=None)


@pytest.mark.asyncio
async def test_key_from_another_platform_is_not_posted_to_this_adapter():
    adapter = _adapter()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("signal:c1", "done")

    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_without_a_chat_id_is_not_posted():
    adapter = _adapter()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:-", "done")

    adapter.send.assert_not_awaited()


def test_adapter_without_send_leaves_the_bridge_unbound():
    runner = _runner(None)

    runner._bind_claude_bridge_notifier(object())

    assert runner.claude_bridge._notifier is None


def test_disabled_bridge_binds_nothing():
    adapter = _adapter()
    runner = _runner(adapter, enabled=False)

    runner._bind_claude_bridge_notifier(adapter)

    assert runner.claude_bridge._notifier is None
