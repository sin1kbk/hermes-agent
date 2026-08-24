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


@pytest.mark.asyncio
async def test_unsolicited_notice_with_media_tag_delivers_the_attachment(tmp_path):
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fake")
    adapter = _adapter()
    adapter.extract_media = MagicMock(return_value=([(str(attachment), False)], "done"))
    adapter.filter_media_delivery_paths = MagicMock(return_value=[(str(attachment), False)])
    send_result = MagicMock(success=True)
    adapter.send_document = AsyncMock(return_value=send_result)
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier(
        "discord:c1", f"done\nMEDIA:{attachment}"
    )

    # The tag itself must not reach the chat — extraction strips it, exactly
    # as base.py does on the normal turn path.
    adapter.send.assert_awaited_once_with("c1", "done", metadata=None)
    adapter.extract_media.assert_called_once_with(f"done\nMEDIA:{attachment}")
    # The delivery-path filter is the only guard keeping a credential path
    # out of chat on this route, so pin the call itself, not just its result.
    adapter.filter_media_delivery_paths.assert_called_once_with(
        [(str(attachment), False)]
    )
    adapter.send_document.assert_awaited_once_with(
        chat_id="c1", file_path=str(attachment), metadata=None,
    )


@pytest.mark.asyncio
async def test_unsolicited_notice_without_media_tag_sends_no_attachment():
    adapter = _adapter()
    adapter.extract_media = MagicMock()
    adapter.send_document = AsyncMock()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1", "just text, nothing to attach")

    adapter.send.assert_awaited_once_with(
        "c1", "just text, nothing to attach", metadata=None
    )
    adapter.extract_media.assert_not_called()
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsolicited_voice_media_keeps_its_voice_delivery(tmp_path):
    """is_voice must survive this route, or a voice note arrives as a plain
    file attachment — unlike the same output on a normal turn."""
    clip = tmp_path / "note.ogg"
    clip.write_bytes(b"fake")
    adapter = _adapter()
    adapter.extract_media = MagicMock(return_value=([(str(clip), True)], "done"))
    adapter.filter_media_delivery_paths = MagicMock(return_value=[(str(clip), True)])
    adapter.send_voice = AsyncMock(return_value=MagicMock(success=True))
    adapter.send_document = AsyncMock()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1", f"done\nMEDIA:{clip}")

    adapter.send_voice.assert_awaited_once_with(
        chat_id="c1", audio_path=str(clip), metadata=None,
    )
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsolicited_attachment_failure_reaches_the_user(tmp_path):
    """A server log is invisible in chat: a failed upload must still say so."""
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fake")
    adapter = _adapter()
    adapter.extract_media = MagicMock(return_value=([(str(attachment), False)], "done"))
    adapter.filter_media_delivery_paths = MagicMock(return_value=[(str(attachment), False)])
    adapter.send_document = AsyncMock(return_value=MagicMock(success=False, error="boom"))
    adapter._notify_media_delivery_failure = AsyncMock()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1", f"done\nMEDIA:{attachment}")

    adapter._notify_media_delivery_failure.assert_awaited_once_with(
        "c1", str(attachment), is_voice=False, metadata=None,
    )


@pytest.mark.asyncio
async def test_unsolicited_attachment_exception_reaches_the_user(tmp_path):
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fake")
    adapter = _adapter()
    adapter.extract_media = MagicMock(return_value=([(str(attachment), False)], "done"))
    adapter.filter_media_delivery_paths = MagicMock(return_value=[(str(attachment), False)])
    adapter.send_document = AsyncMock(side_effect=RuntimeError("network gone"))
    adapter._notify_media_delivery_failure = AsyncMock()
    runner = _runner(adapter)
    runner._bind_claude_bridge_notifier(adapter)

    await runner.claude_bridge._notifier("discord:c1", f"done\nMEDIA:{attachment}")

    adapter._notify_media_delivery_failure.assert_awaited_once_with(
        "c1", str(attachment), is_voice=False, metadata=None,
    )
