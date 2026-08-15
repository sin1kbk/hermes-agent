"""Tests for gateway.claude_bridge halt/unhalt kill-switch behavior.

!halt / !unhalt must never reach the LLM/spawn path, must respect
``halt_users`` when configured, and halt must block every subsequent spawn
until explicitly lifted.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.claude_bridge import ClaudeBridge, halt_flag_file
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


def _event(text: str, user_id: str = "u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id=user_id),
    )


def _bridge(hermes_home, **overrides) -> ClaudeBridge:
    cfg = ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home), **overrides)
    return ClaudeBridge(cfg)


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


def test_halt_and_unhalt_roundtrip(hermes_home):
    bridge = _bridge(hermes_home)

    reply = asyncio.run(bridge.handle_message(_event("!halt")))
    assert reply == "halted"
    assert halt_flag_file().exists()

    reply = asyncio.run(bridge.handle_message(_event("!unhalt")))
    assert reply == "unhalted"
    assert not halt_flag_file().exists()


def test_unhalt_without_prior_halt_is_a_noop_success(hermes_home):
    bridge = _bridge(hermes_home)
    reply = asyncio.run(bridge.handle_message(_event("!unhalt")))
    assert reply == "unhalted"
    assert not halt_flag_file().exists()


def test_halt_restricted_to_configured_users(hermes_home):
    bridge = _bridge(hermes_home, halt_users=["allowed"])

    reply = asyncio.run(bridge.handle_message(_event("!halt", user_id="someone_else")))
    assert "Not authorized" in reply
    assert not halt_flag_file().exists()

    reply = asyncio.run(bridge.handle_message(_event("!halt", user_id="allowed")))
    assert reply == "halted"


def test_unhalt_restricted_to_configured_users(hermes_home):
    bridge = _bridge(hermes_home, halt_users=["allowed"])
    asyncio.run(bridge.handle_message(_event("!halt", user_id="allowed")))

    reply = asyncio.run(bridge.handle_message(_event("!unhalt", user_id="someone_else")))
    assert "Not authorized" in reply
    assert halt_flag_file().exists()  # still halted


def test_empty_halt_users_allows_anyone(hermes_home):
    bridge = _bridge(hermes_home, halt_users=[])
    reply = asyncio.run(bridge.handle_message(_event("!halt", user_id="anyone")))
    assert reply == "halted"


def test_halted_state_blocks_spawn_without_calling_it(hermes_home, monkeypatch):
    bridge = _bridge(hermes_home)
    asyncio.run(bridge.handle_message(_event("!halt")))

    called = False

    async def _fail_spawn(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not spawn while halted")

    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=_fail_spawn),
    )

    reply = asyncio.run(bridge.handle_message(_event("hello claude")))
    assert "halted" in reply.lower()
    assert called is False


def test_unhalt_allows_spawn_again(hermes_home, monkeypatch):
    bridge = _bridge(hermes_home)
    asyncio.run(bridge.handle_message(_event("!halt")))
    asyncio.run(bridge.handle_message(_event("!unhalt")))

    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_successful_process()),
    )

    reply = asyncio.run(bridge.handle_message(_event("hello claude")))
    assert reply == "ok"
