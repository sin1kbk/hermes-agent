"""Regression tests for the bridge's /mode and /yolo permission-mode commands.

The mode is a spawn flag, so a switch is only observable on the argv of the
next `claude -p` process.  These tests assert on that argv wherever the
behavior is user-visible, rather than on internal state.
"""

import asyncio
import json
import logging
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


def _event(text: str, user_id: str = "u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id=user_id),
    )


def _bridge(hermes_home, **overrides) -> ClaudeBridge:
    return ClaudeBridge(
        ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home), **overrides)
    )


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


def _spawn_mock(monkeypatch, count: int = 1) -> AsyncMock:
    spawn = AsyncMock(side_effect=[_successful_process() for _ in range(count)])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    return spawn


def _spawned_mode(call) -> str:
    """Mode the CLI would settle on: the value after the last --permission-mode."""
    argv = list(call.args)
    last = len(argv) - 1 - argv[::-1].index("--permission-mode")
    return argv[last + 1]


@pytest.mark.asyncio
async def test_mode_without_args_reports_current_and_allowed(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, allowed_permission_modes=["auto", "plan"])

    reply = await bridge.handle_message(_event("/mode"))

    assert "auto" in reply
    assert "plan" in reply


@pytest.mark.asyncio
async def test_default_mode_is_passed_on_every_spawn(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("hello")) == "ok"

    assert _spawned_mode(spawn.call_args) == "auto"
    await bridge.close()


@pytest.mark.asyncio
async def test_runtime_mode_outranks_permission_mode_in_extra_args(hermes_home, monkeypatch):
    """The CLI honors the last occurrence, so the injected flag must trail."""
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home, extra_args=["--permission-mode", "plan"])

    await bridge.handle_message(_event("hello"))

    assert _spawned_mode(spawn.call_args) == "auto"
    await bridge.close()


@pytest.mark.asyncio
async def test_mode_switch_applies_to_the_next_spawn(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("/mode plan"))
    await bridge.handle_message(_event("hello"))

    assert "plan" in reply
    assert _spawned_mode(spawn.call_args) == "plan"
    await bridge.close()


@pytest.mark.asyncio
async def test_mode_matching_is_case_insensitive(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("/mode Acceptedits"))
    await bridge.handle_message(_event("hello"))

    assert _spawned_mode(spawn.call_args) == "acceptEdits"
    await bridge.close()


@pytest.mark.asyncio
async def test_mode_switch_replaces_the_resident_process(hermes_home, monkeypatch):
    first, second = _successful_process(), _successful_process()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("hello"))
    await bridge.handle_message(_event("/mode plan"))
    await bridge.handle_message(_event("hello again"))

    assert first.kill.called
    assert spawn.await_count == 2
    assert _spawned_mode(spawn.call_args_list[0]) == "auto"
    assert _spawned_mode(spawn.call_args_list[1]) == "plan"
    # The replacement resumes the same conversation rather than starting over.
    assert "--resume" in spawn.call_args_list[1].args
    await bridge.close()


@pytest.mark.asyncio
async def test_requesting_the_mode_already_in_effect_keeps_the_process(hermes_home, monkeypatch):
    """A no-op switch replaces nothing, so it must not kill a live process."""
    proc = _successful_process()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("hello"))
    await bridge.handle_message(_event("/mode auto"))
    await bridge.handle_message(_event("hello again"))

    assert not proc.kill.called
    assert spawn.await_count == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_clearing_an_absent_override_keeps_the_process(hermes_home, monkeypatch):
    proc = _successful_process()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("hello"))
    await bridge.handle_message(_event("/mode default"))

    assert not proc.kill.called
    await bridge.close()


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected_without_spawning(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("/mode turbo"))

    assert "turbo" in reply
    assert bridge._mode_overrides == {}


@pytest.mark.asyncio
async def test_mode_outside_allowed_list_is_rejected(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, allowed_permission_modes=["auto", "plan"])

    reply = await bridge.handle_message(_event("/mode bypassPermissions"))

    assert "bypassPermissions" in reply
    assert bridge._mode_overrides == {}


@pytest.mark.asyncio
async def test_mode_switch_requires_authorization(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, halt_users=["u2"])

    reply = await bridge.handle_message(_event("/mode plan", user_id="u1"))

    assert "Not authorized" in reply
    assert bridge._mode_overrides == {}


@pytest.mark.asyncio
async def test_reading_the_mode_needs_no_authorization(hermes_home, monkeypatch):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, halt_users=["u2"])

    reply = await bridge.handle_message(_event("/mode", user_id="u1"))

    assert "Not authorized" not in reply
    assert "auto" in reply


@pytest.mark.asyncio
async def test_mode_default_clears_the_override(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("/mode plan"))
    await bridge.handle_message(_event("/mode default"))
    await bridge.handle_message(_event("hello"))

    assert bridge._mode_overrides == {}
    assert _spawned_mode(spawn.call_args) == "auto"
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/reset"])
async def test_new_session_clears_the_override(hermes_home, monkeypatch, command):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("/mode plan"))
    reply = await bridge.handle_message(_event(command))
    await bridge.handle_message(_event("hello"))

    assert bridge._mode_overrides == {}
    assert "auto" in reply
    assert _spawned_mode(spawn.call_args) == "auto"
    await bridge.close()


def test_config_drops_unknown_modes_and_keeps_default_reachable():
    config = ClaudeBridgeConfig.from_dict({
        "enabled": True,
        "default_permission_mode": "plan",
        "allowed_permission_modes": ["auto", "turbo"],
    })

    assert config.default_permission_mode == "plan"
    assert config.allowed_permission_modes == ["auto", "plan"]


def test_config_falls_back_when_default_mode_is_unknown():
    config = ClaudeBridgeConfig.from_dict({"enabled": True, "default_permission_mode": "turbo"})

    assert config.default_permission_mode == "auto"
    assert "auto" in config.allowed_permission_modes


def test_config_malformed_allowed_modes_restricts_to_the_default(caplog):
    """A list written as a scalar must narrow, never widen to every mode."""
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        config = ClaudeBridgeConfig.from_dict({
            "enabled": True,
            "allowed_permission_modes": "auto",
        })

    assert config.allowed_permission_modes == ["auto"]
    assert "bypassPermissions" not in config.allowed_permission_modes
    assert "must be a list" in caplog.text


def test_config_warns_when_dropping_an_unknown_mode(caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        ClaudeBridgeConfig.from_dict({
            "enabled": True,
            "allowed_permission_modes": ["auto", "turbo"],
        })

    assert "turbo" in caplog.text


@pytest.mark.asyncio
async def test_unauthorized_mode_change_is_logged_with_sender(hermes_home, monkeypatch, caplog):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, halt_users=["u2"])

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        await bridge.handle_message(_event("/mode bypassPermissions", user_id="u1"))

    assert "u1" in caplog.text
    assert "unauthorized" in caplog.text


@pytest.mark.asyncio
async def test_mode_change_is_logged_with_sender(hermes_home, monkeypatch, caplog):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home)

    with caplog.at_level(logging.INFO, logger="gateway.claude_bridge"):
        await bridge.handle_message(_event("/mode bypassPermissions", user_id="u1"))

    assert "u1" in caplog.text
    assert "bypassPermissions" in caplog.text


@pytest.mark.asyncio
async def test_yolo_switches_to_bypass_on_the_next_spawn(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("/yolo"))
    await bridge.handle_message(_event("hello"))

    assert "bypassPermissions" in reply
    assert _spawned_mode(spawn.call_args) == "bypassPermissions"
    await bridge.close()


@pytest.mark.asyncio
async def test_yolo_toggles_back_to_the_default(hermes_home, monkeypatch):
    spawn = _spawn_mock(monkeypatch)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("/yolo"))
    reply = await bridge.handle_message(_event("/yolo"))
    await bridge.handle_message(_event("hello"))

    assert bridge._mode_overrides == {}
    assert "auto" in reply
    assert _spawned_mode(spawn.call_args) == "auto"
    await bridge.close()


@pytest.mark.asyncio
async def test_yolo_off_reports_that_the_default_still_bypasses(hermes_home, monkeypatch):
    """Clearing the override cannot go below a bypassing configured default."""
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, default_permission_mode="bypassPermissions")

    reply = await bridge.handle_message(_event("/yolo"))

    assert bridge._mode_overrides == {}
    assert "still bypasses" in reply


@pytest.mark.asyncio
async def test_yolo_is_rejected_when_bypass_is_not_allowed(hermes_home, monkeypatch, caplog):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, allowed_permission_modes=["auto", "plan"])

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        reply = await bridge.handle_message(_event("/yolo", user_id="u1"))

    assert "bypassPermissions" in reply
    assert bridge._mode_overrides == {}
    # Every rejection is an audit-log entry, as it is for /mode.
    assert "u1" in caplog.text
    assert "not allowed" in caplog.text


@pytest.mark.asyncio
async def test_unauthorized_yolo_cannot_read_the_allowed_modes(hermes_home, monkeypatch):
    """The rejection must not differ by config: that would leak the mode list."""
    _never_spawn(monkeypatch)
    narrow = _bridge(hermes_home, halt_users=["u2"], allowed_permission_modes=["auto"])
    wide = _bridge(hermes_home, halt_users=["u2"])

    narrow_reply = await narrow.handle_message(_event("/yolo", user_id="u1"))
    wide_reply = await wide.handle_message(_event("/yolo", user_id="u1"))

    assert narrow_reply == wide_reply
    assert "Not authorized" in narrow_reply


@pytest.mark.asyncio
async def test_yolo_requires_authorization(hermes_home, monkeypatch, caplog):
    _never_spawn(monkeypatch)
    bridge = _bridge(hermes_home, halt_users=["u2"])

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        reply = await bridge.handle_message(_event("/yolo", user_id="u1"))

    assert "Not authorized" in reply
    assert bridge._mode_overrides == {}
    assert "u1" in caplog.text
    assert "unauthorized" in caplog.text


@pytest.mark.asyncio
async def test_yolo_replaces_the_resident_process(hermes_home, monkeypatch):
    """The mode is a spawn flag, so the running CLI must not survive the switch."""
    spawn = _spawn_mock(monkeypatch, count=2)
    bridge = _bridge(hermes_home)

    await bridge.handle_message(_event("hello"))
    await bridge.handle_message(_event("/yolo"))
    await bridge.handle_message(_event("hello again"))

    assert _spawned_mode(spawn.call_args_list[0]) == "auto"
    assert _spawned_mode(spawn.call_args_list[1]) == "bypassPermissions"
    await bridge.close()


def test_config_round_trips_permission_mode_settings():
    config = ClaudeBridgeConfig.from_dict({
        "enabled": True,
        "default_permission_mode": "plan",
        "allowed_permission_modes": ["plan", "auto"],
    })

    assert ClaudeBridgeConfig.from_dict(config.to_dict()) == config
