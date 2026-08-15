"""Regression tests for bridge session commands.

The gateway's own agent loop treats these as session-scoped commands.  The
bridge intercepts them instead of sending a literal prompt to Claude; /stop
now interrupts a persistent process when one is active.
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


def _never_spawn(monkeypatch):
    async def _fail(*args, **kwargs):
        raise AssertionError("must not spawn claude for a session-scoped command")

    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(side_effect=_fail),
    )


def _successful_process():
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    # One result per prompt written, and silence in between.  The bridge
    # drains stdout for the whole life of the process, so a mock that repeats
    # its result on every read would model a CLI that does not exist.
    lines = asyncio.Queue()
    result_line = (json.dumps({
        "type": "result", "result": "ok", "session_id": "s1", "is_error": False,
    }) + "\n").encode()
    proc.stdin.write = lambda _data: lines.put_nowait(result_line)
    proc.stdout.readline = lines.get
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)

    def _kill():
        proc.returncode = -9

    proc.kill.side_effect = _kill
    return proc


@pytest.mark.parametrize("command", ["/new", "/reset"])
def test_new_and_reset_clear_session_without_spawning(hermes_home, monkeypatch, command):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    reply = asyncio.run(bridge.handle_message(_event(command)))

    assert bridge._sessions.get("discord:c1") is None
    assert reply is not None
    assert "new" in reply.lower()


def test_stop_returns_stopped_message_without_spawning(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)

    reply = asyncio.run(bridge.handle_message(_event("/stop")))

    assert reply is not None
    assert "stopped" in reply.lower()


def test_new_with_trailing_text_still_recognized_as_command(hermes_home, monkeypatch):
    """MessageEvent.get_command() strips args — "/new please" is still /new."""
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    reply = asyncio.run(bridge.handle_message(_event("/new please")))

    assert bridge._sessions.get("discord:c1") is None
    assert reply is not None


def test_non_command_slash_text_is_not_intercepted(hermes_home, monkeypatch):
    """Only recognized command names are intercepted — ordinary text starting
    with '/' (e.g. a shell path in a prompt) still reaches claude."""
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_successful_process()),
    )
    bridge = _bridge(hermes_home)

    reply = asyncio.run(bridge.handle_message(_event("/usr/bin/env check this path")))

    assert reply == "ok"
