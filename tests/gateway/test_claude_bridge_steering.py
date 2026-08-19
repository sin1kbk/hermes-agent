"""Mid-turn steering: a message sent while a turn runs reaches the CLI now.

The CLI folds a second stream-json user event into the turn in flight (same
behavior as typing into an interactive session; measured 2026-08-19), so the
bridge writes it straight to the running process's stdin instead of queueing
it behind the per-key lock until the turn ends.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.claude_bridge import ClaudeBridge
from gateway.config import ClaudeBridgeConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def hermes_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
    )


def _bridge(hermes_home) -> ClaudeBridge:
    return ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home)))


def _result_line(text: str, **extra) -> bytes:
    payload = {"type": "result", "result": text, "session_id": "s1", "is_error": False}
    payload.update(extra)
    return (json.dumps(payload) + "\n").encode()


def _controllable_process():
    """A fake CLI whose stdout the test feeds by hand.

    ``written`` records every stdin payload; nothing comes back until the
    test puts a line on ``lines``.
    """
    proc = MagicMock()
    proc.returncode = None
    written = []
    lines = asyncio.Queue()
    proc.stdin = MagicMock()
    proc.stdin.write = written.append
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = lines.get
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)

    def _kill():
        proc.returncode = -9

    proc.kill.side_effect = _kill
    return proc, written, lines


def _spawn(monkeypatch, proc):
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )


async def _wait_for(predicate, timeout=2.0):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _prompt_text(payload: bytes) -> str:
    return json.loads(payload)["message"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_message_during_active_turn_is_written_immediately(hermes_home, monkeypatch):
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    bridge = _bridge(hermes_home)

    turn = asyncio.create_task(bridge.handle_message(_event("long task")))
    await _wait_for(lambda: len(written) == 1)

    reply = await bridge.handle_message(_event("extra instruction"))

    assert "already running" in reply
    assert [_prompt_text(p) for p in written] == ["long task", "extra instruction"]
    # The single result the CLI emits for the steered turn answers the
    # original message; the steered one already got its ack above.
    lines.put_nowait(_result_line("combined answer"))
    assert await asyncio.wait_for(turn, timeout=2) == "combined answer"
    await bridge.close()


@pytest.mark.asyncio
async def test_commands_are_not_steered_into_the_turn(hermes_home, monkeypatch):
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    bridge = _bridge(hermes_home)

    turn = asyncio.create_task(bridge.handle_message(_event("long task")))
    await _wait_for(lambda: len(written) == 1)

    reply = await bridge.handle_message(_event("/stop"))

    assert reply == "Claude turn stopped."
    assert len(written) == 1
    assert await asyncio.wait_for(turn, timeout=2) == "Claude turn stopped."
    await bridge.close()


@pytest.mark.asyncio
async def test_message_while_spawn_holds_the_lock_queues_instead(hermes_home, monkeypatch):
    proc, written, lines = _controllable_process()
    spawn_gate = asyncio.Event()

    async def _slow_spawn(*args, **kwargs):
        await spawn_gate.wait()
        return proc

    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec", _slow_spawn,
    )
    bridge = _bridge(hermes_home)

    first = asyncio.create_task(bridge.handle_message(_event("first")))
    await _wait_for(lambda: bridge._key_locks and next(iter(bridge._key_locks.values())).locked())

    # No turn is active yet (the lock holder is still spawning), so this
    # message must fall through to the queue path, not be lost or acked.
    second = asyncio.create_task(bridge.handle_message(_event("second")))
    await asyncio.sleep(0)
    spawn_gate.set()

    await _wait_for(lambda: len(written) == 1)
    lines.put_nowait(_result_line("answer one"))
    assert await asyncio.wait_for(first, timeout=2) == "answer one"

    await _wait_for(lambda: len(written) == 2)
    assert _prompt_text(written[1]) == "second"
    lines.put_nowait(_result_line("answer two"))
    assert await asyncio.wait_for(second, timeout=2) == "answer two"
    await bridge.close()


@pytest.mark.asyncio
async def test_steering_write_failure_falls_back_to_queueing(hermes_home, monkeypatch):
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    bridge = _bridge(hermes_home)

    turn = asyncio.create_task(bridge.handle_message(_event("long task")))
    await _wait_for(lambda: len(written) == 1)

    real_write = proc.stdin.write

    def _broken_write(_data):
        proc.stdin.write = real_write
        raise BrokenPipeError("stdin gone")

    proc.stdin.write = _broken_write
    second = asyncio.create_task(bridge.handle_message(_event("extra")))
    await asyncio.sleep(0)

    lines.put_nowait(_result_line("first answer"))
    assert await asyncio.wait_for(turn, timeout=2) == "first answer"

    await _wait_for(lambda: len(written) == 2)
    assert _prompt_text(written[1]) == "extra"
    lines.put_nowait(_result_line("second answer"))
    assert await asyncio.wait_for(second, timeout=2) == "second answer"
    await bridge.close()


@pytest.mark.asyncio
async def test_result_with_no_waiter_is_posted_as_a_notice(hermes_home, monkeypatch):
    """The race: a steered message lands just after the turn ended.

    The CLI then runs it as a turn of its own, whose result arrives with no
    waiter.  It must reach the channel via the notifier, not be discarded.
    """
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    bridge = _bridge(hermes_home)
    notices = []

    async def _notify(key, text):
        notices.append((key, text))

    bridge.set_notifier(_notify)

    turn = asyncio.create_task(bridge.handle_message(_event("task")))
    await _wait_for(lambda: len(written) == 1)
    lines.put_nowait(_result_line("answer"))
    assert await asyncio.wait_for(turn, timeout=2) == "answer"

    lines.put_nowait(_result_line("late steered answer", session_id="s2"))
    await _wait_for(lambda: bool(notices))

    assert notices == [("discord:c1", "late steered answer")]
    # Its session id still wins: the steered turn is the newest state.
    assert bridge._sessions.get("discord:c1") == "s2"
    await bridge.close()


@pytest.mark.asyncio
async def test_orphan_error_result_is_posted_as_an_error_notice(hermes_home, monkeypatch):
    """The steered sender already got a 'sent' ack; if the raced turn then
    errors, that error must reach the channel rather than end in a log line."""
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    bridge = _bridge(hermes_home)
    notices = []

    async def _notify(key, text):
        notices.append((key, text))

    bridge.set_notifier(_notify)

    turn = asyncio.create_task(bridge.handle_message(_event("task")))
    await _wait_for(lambda: len(written) == 1)
    lines.put_nowait(_result_line("answer"))
    assert await asyncio.wait_for(turn, timeout=2) == "answer"

    lines.put_nowait(_result_line("boom", is_error=True))
    await _wait_for(lambda: bool(notices))

    assert notices == [("discord:c1", "Claude bridge error: boom")]
    await bridge.close()


@pytest.mark.asyncio
async def test_steering_drain_timeout_is_delivered_not_requeued(hermes_home, monkeypatch):
    """stdin.write already handed the bytes over; a slow drain must not make
    the fallback path send the same text a second time."""
    proc, written, lines = _controllable_process()
    _spawn(monkeypatch, proc)
    monkeypatch.setattr(
        "gateway.claude_bridge.core._ClaudeProcess._INJECT_DRAIN_TIMEOUT", 0.01,
    )
    bridge = _bridge(hermes_home)

    turn = asyncio.create_task(bridge.handle_message(_event("long task")))
    await _wait_for(lambda: len(written) == 1)

    async def _hang():
        await asyncio.Event().wait()

    proc.stdin.drain = _hang
    reply = await bridge.handle_message(_event("extra"))

    assert "already running" in reply
    assert [_prompt_text(p) for p in written] == ["long task", "extra"]
    lines.put_nowait(_result_line("combined answer"))
    assert await asyncio.wait_for(turn, timeout=2) == "combined answer"
    # Give a would-be queued duplicate every chance to surface before asserting.
    await asyncio.sleep(0.05)
    assert len(written) == 2
    await bridge.close()
