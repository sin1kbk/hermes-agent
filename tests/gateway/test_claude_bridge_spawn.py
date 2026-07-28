"""Tests for gateway.claude_bridge's `claude -p` spawn wrapper.

Covers JSON-output parsing, non-zero exit / unparseable-output / timeout
error surfacing, and the resume-failure -> fresh-spawn fallback in
``ClaudeBridge._handle_locked``. All subprocess creation is mocked — no real
``claude`` binary is invoked.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.claude_bridge import ClaudeBridge, _SpawnOutcome, _spawn_claude
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


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
    )


async def _spawn(monkeypatch, tmp_path, fake_proc, **kwargs):
    monkeypatch.setattr(
        "gateway.claude_bridge.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    )
    return await _spawn_claude(
        claude_bin="claude",
        working_dir=str(tmp_path),
        prompt=kwargs.pop("prompt", "hi"),
        resume_session_id=kwargs.pop("resume_session_id", None),
        extra_args=kwargs.pop("extra_args", []),
        timeout_seconds=kwargs.pop("timeout_seconds", 5),
    )


def test_spawn_parses_json_result(monkeypatch, tmp_path):
    payload = {
        "result": "hi there", "session_id": "sess-1",
        "is_error": False, "total_cost_usd": 0.01,
    }
    fake = _FakeProc(stdout=json.dumps(payload).encode())

    outcome = asyncio.run(_spawn(monkeypatch, tmp_path, fake))

    assert outcome.error is None
    assert outcome.parsed["result"] == "hi there"
    assert outcome.parsed["session_id"] == "sess-1"


def test_spawn_reports_nonzero_exit_with_stderr(monkeypatch, tmp_path):
    fake = _FakeProc(stderr=b"boom", returncode=1)

    outcome = asyncio.run(_spawn(monkeypatch, tmp_path, fake))

    assert outcome.parsed is None
    assert "exited 1" in outcome.error
    assert "boom" in outcome.error


def test_spawn_reports_unparseable_output(monkeypatch, tmp_path):
    fake = _FakeProc(stdout=b"not json at all")

    outcome = asyncio.run(_spawn(monkeypatch, tmp_path, fake))

    assert outcome.parsed is None
    assert "unparseable" in outcome.error


def test_spawn_reports_non_object_json(monkeypatch, tmp_path):
    fake = _FakeProc(stdout=b"[1, 2, 3]")

    outcome = asyncio.run(_spawn(monkeypatch, tmp_path, fake))

    assert outcome.parsed is None
    assert "non-object" in outcome.error


def test_spawn_failure_to_launch_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gateway.claude_bridge.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("claude: not found")),
    )

    outcome = asyncio.run(
        _spawn_claude(
            claude_bin="claude", working_dir=str(tmp_path), prompt="hi",
            resume_session_id=None, extra_args=[], timeout_seconds=5,
        )
    )

    assert outcome.parsed is None
    assert "failed to spawn" in outcome.error


def test_spawn_times_out_and_kills_process(monkeypatch, tmp_path):
    fake = _FakeProc(hang=True)

    outcome = asyncio.run(_spawn(monkeypatch, tmp_path, fake, timeout_seconds=0.05))

    assert outcome.parsed is None
    assert outcome.timed_out is True
    assert fake.killed is True


def test_resume_uses_resume_flag(monkeypatch, tmp_path):
    payload = {"result": "ok", "session_id": "sess-1", "is_error": False}
    fake = _FakeProc(stdout=json.dumps(payload).encode())
    exec_mock = AsyncMock(return_value=fake)
    monkeypatch.setattr("gateway.claude_bridge.asyncio.create_subprocess_exec", exec_mock)

    asyncio.run(
        _spawn_claude(
            claude_bin="claude", working_dir=str(tmp_path), prompt="hi",
            resume_session_id="sess-1", extra_args=["--foo"], timeout_seconds=5,
        )
    )

    called_args = exec_mock.call_args.args
    assert "--resume" in called_args
    assert "sess-1" in called_args
    assert "--foo" in called_args


def test_resume_failure_falls_back_to_fresh_spawn(monkeypatch, hermes_home):
    calls = []

    async def _fake_spawn(*, resume_session_id, **kwargs):
        calls.append(resume_session_id)
        if resume_session_id:
            return _SpawnOutcome(error="unknown session")
        return _SpawnOutcome(
            parsed={"result": "fresh reply", "session_id": "new-sess", "is_error": False}
        )

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fake_spawn)

    bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home)))
    bridge._sessions.set("discord:c1", "stale-sess")

    reply = asyncio.run(bridge.handle_message(_event("hello")))

    assert calls == ["stale-sess", None]
    assert "fresh reply" in reply
    assert "started a new Claude session" in reply
    assert bridge._sessions.get("discord:c1") == "new-sess"


def test_timeout_does_not_trigger_resume_fallback(monkeypatch, hermes_home):
    calls = []

    async def _fake_spawn(*, resume_session_id, **kwargs):
        calls.append(resume_session_id)
        return _SpawnOutcome(error="claude timed out after 1s", timed_out=True)

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fake_spawn)

    bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home)))
    bridge._sessions.set("discord:c1", "stale-sess")

    reply = asyncio.run(bridge.handle_message(_event("hello")))

    assert calls == ["stale-sess"]  # no fresh-spawn retry
    assert "timed out" in reply


def test_is_error_response_surfaces_result_text(monkeypatch, hermes_home):
    from gateway.claude_bridge import _SpawnOutcome as _SO

    async def _fake_spawn(**kwargs):
        return _SO(parsed={"result": "bad prompt", "is_error": True, "session_id": "s1"})

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fake_spawn)

    bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=str(hermes_home)))
    reply = asyncio.run(bridge.handle_message(_event("hello")))

    assert "Claude bridge error" in reply
    assert "bad prompt" in reply


def test_missing_working_dir_is_reported_without_spawning(monkeypatch, hermes_home):
    called = False

    async def _fail_spawn(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not spawn without working_dir")

    monkeypatch.setattr("gateway.claude_bridge._spawn_claude", _fail_spawn)

    bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=None))
    reply = asyncio.run(bridge.handle_message(_event("hello")))

    assert "working_dir" in reply
    assert called is False
