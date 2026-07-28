"""Regression tests for F5: /new, /reset, /stop must not reach claude as a
literal prompt.

The gateway's own agent loop treats these as session-scoped commands;
ClaudeBridge has no equivalent concept of an in-flight turn to interrupt or
reset, so it must intercept them explicitly instead of spawning `claude -p
"/new"`.
"""

import asyncio

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

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fail)


@pytest.mark.parametrize("command", ["/new", "/reset"])
def test_new_and_reset_clear_session_without_spawning(hermes_home, monkeypatch, command):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    reply = asyncio.run(bridge.handle_message(_event(command)))

    assert bridge._sessions.get("discord:c1") is None
    assert reply is not None
    assert "new" in reply.lower()


def test_stop_returns_explicit_unsupported_message_without_spawning(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)

    reply = asyncio.run(bridge.handle_message(_event("/stop")))

    assert reply is not None
    assert "stop" in reply.lower()
    assert "support" in reply.lower()


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
    from gateway.claude_bridge import _SpawnOutcome

    async def _fake_spawn(**kwargs):
        return _SpawnOutcome(parsed={"result": "ok", "session_id": "s1", "is_error": False})

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fake_spawn)
    bridge = _bridge(hermes_home)

    reply = asyncio.run(bridge.handle_message(_event("/usr/bin/env check this path")))

    assert reply == "ok"
