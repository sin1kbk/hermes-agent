"""Periodic Discord thread re-title for claude-bridge sessions.

Bridge turns bypass the native agent loop, so ``maybe_auto_title`` never runs
for them and a Hermes-auto-created thread keeps the opening-message excerpt the
Discord adapter named it with for life. ``_schedule_claude_bridge_retitle``
reuses the native titler plus the adapter's own rename op to keep that name
tracking the work.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

TITLE = "Refactor the outbox watcher"


def _event(text: str = "please refactor the outbox watcher") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="c1",
            user_id="u1",
            chat_type="thread",
            thread_id="t1",
        ),
    )


def _runner(*, auto_thread_lane: bool = True, adapter=None):
    runner = object.__new__(GatewayRunner)
    runner._claude_bridge_retitle_counts = {}
    runner._claude_bridge_retitle_tasks = set()
    runner._is_discord_auto_thread_lane = MagicMock(return_value=auto_thread_lane)
    runner._sanitize_discord_thread_title = MagicMock(side_effect=lambda t: t)
    runner._adapter_for_source = MagicMock(
        return_value=adapter if adapter is not None else _adapter()
    )
    return runner


def _adapter(*, renameable: bool = True):
    adapter = MagicMock()
    if renameable:
        adapter.rename_thread = AsyncMock(return_value=True)
    else:
        del adapter.rename_thread
    return adapter


async def _drain(runner):
    """Let the fire-and-forget re-title tasks run to completion."""
    tasks = list(runner._claude_bridge_retitle_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture
def titler(monkeypatch):
    """Stub the native titler — no auxiliary LLM call in tests."""
    import agent.title_generator as tg

    generate = MagicMock(return_value=TITLE)
    monkeypatch.setattr(tg, "generate_title", generate)
    return generate


@pytest.mark.asyncio
async def test_first_turn_renames_the_thread(titler):
    adapter = _adapter()
    runner = _runner(adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event())
    await _drain(runner)

    adapter.rename_thread.assert_awaited_once_with("t1", TITLE)


@pytest.mark.asyncio
async def test_renames_every_fifth_turn(titler):
    adapter = _adapter()
    runner = _runner(adapter=adapter)

    for _ in range(6):
        runner._schedule_claude_bridge_retitle(_event())
        await _drain(runner)

    # Turns 1 and 6 fire; 2-5 do not.
    assert adapter.rename_thread.await_count == 2
    assert runner._claude_bridge_retitle_counts["discord:c1:t1"] == 6


@pytest.mark.asyncio
async def test_command_turn_is_not_counted(titler):
    adapter = _adapter()
    runner = _runner(adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event("/mode default"))
    await _drain(runner)

    assert runner._claude_bridge_retitle_counts == {}
    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_auto_thread_source_is_not_counted(titler):
    adapter = _adapter()
    runner = _runner(auto_thread_lane=False, adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event())
    await _drain(runner)

    assert runner._claude_bridge_retitle_counts == {}
    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_machine_authored_text_is_not_counted(titler):
    adapter = _adapter()
    runner = _runner(adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event("[Runtime note: model switched]"))
    await _drain(runner)

    assert runner._claude_bridge_retitle_counts == {}
    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_title_generated_means_no_rename(titler):
    titler.return_value = None
    adapter = _adapter()
    runner = _runner(adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event())
    await _drain(runner)

    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_without_rename_thread_is_survivable(titler):
    runner = _runner(adapter=_adapter(renameable=False))

    runner._schedule_claude_bridge_retitle(_event())
    await _drain(runner)  # must not raise


@pytest.mark.asyncio
async def test_rename_failure_is_swallowed(titler):
    adapter = _adapter()
    adapter.rename_thread = AsyncMock(side_effect=RuntimeError("discord 403"))
    runner = _runner(adapter=adapter)

    runner._schedule_claude_bridge_retitle(_event())
    await _drain(runner)  # must not raise

    adapter.rename_thread.assert_awaited_once()
