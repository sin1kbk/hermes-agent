"""Tests for the Claude bridge stream-json subprocess protocol.

Every subprocess is a controllable asyncio fake.  These tests deliberately
exercise the persistent process boundary; no test invokes a real ``claude``
binary or network service.
"""

import asyncio
import json
import logging
import time
from collections import deque
from unittest.mock import AsyncMock

import pytest

from gateway.claude_bridge import ClaudeBridge, _ClaudeProcess
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


class _FakeReader:
    def __init__(self):
        self._lines = asyncio.Queue()
        self._eof = False
        self.read_sizes = []

    def feed_lines(self, lines):
        for line in lines:
            self._lines.put_nowait(line if isinstance(line, bytes) else line.encode("utf-8"))

    def feed_eof(self):
        if not self._eof:
            self._eof = True
            self._lines.put_nowait(None)

    async def readline(self):
        line = await self._lines.get()
        return b"" if line is None else line

    async def read(self, size=-1):
        self.read_sizes.append(size)
        line = await self._lines.get()
        return b"" if line is None else line


class _FakeWriter:
    def __init__(self, process):
        self._process = process
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        self._process._on_input(data)

    async def drain(self):
        return None


class _FakeProc:
    def __init__(self, responses=(), *, eof_after_response=False, stderr_lines=()):
        self.stdout = _FakeReader()
        self.stderr = _FakeReader()
        self.stdin = _FakeWriter(self)
        self._responses = deque(responses)
        self._eof_after_response = eof_after_response
        self.returncode = None
        self.killed = False
        self.kill_count = 0
        self.stderr.feed_lines(stderr_lines)
        self.stderr.feed_eof()

    def _on_input(self, _data):
        if self._responses:
            self.stdout.feed_lines(self._responses.popleft())
            if self._eof_after_response:
                self.stdout.feed_eof()

    def kill(self):
        self.killed = True
        self.kill_count += 1
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
    )


def _result(text, session_id="sess-1", **extra):
    return (json.dumps({"type": "result", "result": text, "session_id": session_id,
                        "is_error": False, **extra}) + "\n").encode("utf-8")


def _bridge(home, **overrides):
    return ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=str(home), **overrides))


@pytest.mark.asyncio
async def test_two_turns_reuse_one_process_and_write_stream_json_to_stdin(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("first")], [_result("second")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("one")) == "first"
    assert await bridge.handle_message(_event("two")) == "second"

    assert spawn.await_count == 1
    sent = [json.loads(write.decode("utf-8")) for write in proc.stdin.writes]
    assert [item["message"]["content"][0]["text"] for item in sent] == ["one", "two"]
    args = spawn.call_args.args
    assert args[:6] == (
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
    )
    assert "--verbose" in args
    assert spawn.call_args.kwargs["limit"] == 10 * 1024 * 1024
    await bridge.close()


@pytest.mark.asyncio
async def test_configured_model_is_appended_after_extra_args(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home, extra_args=["--model", "claude-haiku-4"])

    assert await bridge.handle_message(_event("hello"), model="claude-opus-5") == "ok"

    args = spawn.call_args.args
    last_model = len(args) - 1 - args[::-1].index("--model")
    assert args[last_model + 1] == "claude-opus-5"
    await bridge.close()


@pytest.mark.asyncio
async def test_configured_effort_is_appended_after_extra_args(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home, extra_args=["--effort", "low"])

    assert await bridge.handle_message(_event("hello"), effort="high") == "ok"

    args = spawn.call_args.args
    last_effort = len(args) - 1 - args[::-1].index("--effort")
    assert args[last_effort + 1] == "high"
    await bridge.close()


@pytest.mark.asyncio
async def test_empty_or_invalid_effort_omits_effort_argument(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("first")], [_result("second")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("one"), effort="") == "first"
    assert "--effort" not in spawn.call_args_list[0].args
    assert await bridge.handle_message(_event("two"), effort="ultra") == "second"
    assert spawn.await_count == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_empty_model_omits_model_argument(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("hello"), model="") == "ok"

    assert "--model" not in spawn.call_args.args
    await bridge.close()


@pytest.mark.asyncio
async def test_legacy_model_is_normalized_to_empty_before_spawn(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("hello"), model="claude-code") == "ok"
    assert "--model" not in spawn.call_args.args
    await bridge.close()


@pytest.mark.asyncio
async def test_legacy_resident_model_does_not_trigger_a_respawn(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("first")], [_result("second")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("one")) == "first"
    bridge._procs["discord:c1"].model = "claude-code"
    assert await bridge.handle_message(_event("two")) == "second"

    assert spawn.await_count == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_model_change_replaces_resident_process_and_resumes_session(
    monkeypatch, hermes_home, caplog
):
    first = _FakeProc(responses=[[_result("first", "session-1")]])
    second = _FakeProc(responses=[[_result("second", "session-1")]])
    respawned_dead_process = _FakeProc(responses=[[_result("third", "session-1")]])
    spawn = AsyncMock(side_effect=[first, second, respawned_dead_process])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    with caplog.at_level(logging.INFO, logger="gateway.claude_bridge"):
        assert await bridge.handle_message(_event("one"), model="claude-sonnet-4") == "first"
        assert await bridge.handle_message(_event("two"), model="claude-opus-5") == "second"

    assert first.killed is True
    assert "--resume" in spawn.call_args_list[1].args
    assert "session-1" in spawn.call_args_list[1].args
    assert spawn.call_args_list[1].args[-2:] == ("--model", "claude-opus-5")
    assert any(
        record.message
        == "claude_bridge: model changed for discord:c1 "
        "(claude-sonnet-4 -> claude-opus-5); respawning"
        for record in caplog.records
    )
    second.returncode = 1
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="gateway.claude_bridge"):
        assert await bridge.handle_message(_event("three"), model="claude-haiku-4") == "third"
    assert not any("model changed" in record.message for record in caplog.records)
    await bridge.close()


@pytest.mark.asyncio
async def test_effort_change_replaces_resident_process_and_resumes_session(
    monkeypatch, hermes_home, caplog
):
    first = _FakeProc(responses=[[_result("first", "session-1")]])
    second = _FakeProc(responses=[[_result("second", "session-1")]])
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    with caplog.at_level(logging.INFO, logger="gateway.claude_bridge"):
        assert await bridge.handle_message(_event("one"), effort="low") == "first"
        assert await bridge.handle_message(_event("two"), effort="high") == "second"

    assert first.killed is True
    assert "--resume" in spawn.call_args_list[1].args
    assert "session-1" in spawn.call_args_list[1].args
    assert spawn.call_args_list[1].args[-2:] == ("--effort", "high")
    assert any(
        record.message
        == "claude_bridge: effort changed for discord:c1 (low -> high); respawning"
        for record in caplog.records
    )
    await bridge.close()


@pytest.mark.asyncio
async def test_non_result_and_unparseable_event_lines_are_ignored(monkeypatch, hermes_home, caplog):
    proc = _FakeProc(responses=[[
        b"not json\n",
        b'{"type":"assistant","message":{"content":[]}}\n',
        _result("usable"),
    ]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("hello")) == "usable"
    assert any("unparseable stream-json" in record.message for record in caplog.records)
    await bridge.close()


@pytest.mark.asyncio
async def test_spawn_failure_is_reported(monkeypatch, hermes_home):
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("claude: not found")),
    )
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("hello"))

    assert "failed to spawn" in reply
    await bridge.close()


@pytest.mark.asyncio
async def test_stdout_eof_reports_stderr_tail(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[]], eof_after_response=True, stderr_lines=[b"cli failed\n"])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("hello"))

    assert "ended before a result" in reply
    assert "cli failed" in reply
    await bridge.close()


@pytest.mark.asyncio
async def test_timeout_kills_process_then_next_turn_respawns_with_resume(monkeypatch, hermes_home):
    timed_out = _FakeProc()
    resumed = _FakeProc(responses=[[_result("resumed", "stale-session")]])
    spawn = AsyncMock(side_effect=[timed_out, resumed])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home, timeout_seconds=0.01)
    bridge._sessions.set("discord:c1", "stale-session")

    reply = await bridge.handle_message(_event("slow"))
    assert "timed out" in reply
    assert timed_out.killed is True

    assert await bridge.handle_message(_event("again")) == "resumed"
    assert spawn.await_count == 2
    assert "--resume" in spawn.call_args_list[1].args
    assert "stale-session" in spawn.call_args_list[1].args
    await bridge.close()


@pytest.mark.asyncio
async def test_resume_not_found_result_retries_once_as_fresh_process(monkeypatch, hermes_home):
    not_found = _FakeProc(responses=[[(json.dumps({
        "type": "result", "is_error": True, "result": "missing", "session_id": "stale-session",
        "subtype": "error_during_execution", "num_turns": 0,
        "errors": ["No conversation found with session ID: stale-session"],
    }) + "\n").encode()]])
    fresh = _FakeProc(responses=[[_result("fresh reply", "new-session")]])
    spawn = AsyncMock(side_effect=[not_found, fresh])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    reply = await bridge.handle_message(_event("hello"))

    assert "fresh reply" in reply
    assert "started a new Claude session" in reply
    assert not_found.killed is True
    assert "--resume" in spawn.call_args_list[0].args
    assert "--resume" not in spawn.call_args_list[1].args
    assert bridge._sessions.get("discord:c1") == "new-session"
    await bridge.close()


@pytest.mark.asyncio
async def test_resume_failure_logs_the_session_and_the_cli_error_text(
    monkeypatch, hermes_home, caplog
):
    """The warning must name the cause; the process is gone by the time anyone looks."""
    not_found = _FakeProc(responses=[[(json.dumps({
        "type": "result", "is_error": True, "result": "missing", "session_id": "stale-session",
        "subtype": "error_during_execution", "num_turns": 0,
        "errors": ["No conversation found with session ID: stale-session"],
    }) + "\n").encode()]], stderr_lines=[b"claude: could not read transcript\n"])
    fresh = _FakeProc(responses=[[_result("fresh reply", "new-session")]])
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[not_found, fresh]),
    )
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        await bridge.handle_message(_event("hello"))

    warning = next(r.getMessage() for r in caplog.records if "resume failed" in r.getMessage())
    assert "stale-session" in warning
    assert "transcript not found" in warning
    assert "No conversation found with session ID" in warning
    assert "could not read transcript" in warning
    await bridge.close()


@pytest.mark.asyncio
async def test_restart_failure_is_logged_apart_from_a_missing_transcript(
    monkeypatch, hermes_home, caplog
):
    """An error_during_execution with no `errors` list is a different failure."""
    stuck = _FakeProc(responses=[[(json.dumps({
        "type": "result", "is_error": True, "result": "could not restart",
        "session_id": "saved-session", "subtype": "error_during_execution", "num_turns": 0,
    }) + "\n").encode()]])
    fresh = _FakeProc(responses=[[_result("fresh reply", "new-session")]])
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[stuck, fresh]),
    )
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "saved-session")

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        await bridge.handle_message(_event("hello"))

    warning = next(r.getMessage() for r in caplog.records if "resume failed" in r.getMessage())
    assert "error_during_execution with num_turns=0" in warning
    assert "could not restart" in warning
    assert "transcript not found" not in warning
    await bridge.close()


@pytest.mark.asyncio
async def test_background_output_keeps_an_idle_session_from_being_reaped(
    monkeypatch, hermes_home
):
    """A session working on its own between turns is not idle.

    Reaping it mid-tool-call leaves a transcript ending on an unanswered
    tool_use, which the next turn can no longer resume.
    """
    proc = _FakeProc(responses=[[_result("ok", "saved-session")]])
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    )
    bridge = _bridge(hermes_home, idle_timeout_seconds=1)
    await bridge.handle_message(_event("hello"))
    resident = bridge._procs["discord:c1"]
    resident.last_used = time.monotonic() - 2

    # A background turn's own stream event, not a reply to anything we sent.
    proc.stdout.feed_lines([
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    ])
    await asyncio.sleep(0)
    await bridge._reap_idle_processes()

    assert proc.killed is False
    assert bridge._procs.get("discord:c1") is resident
    await bridge.close()


@pytest.mark.asyncio
async def test_resume_failure_warning_stays_on_one_log_line(monkeypatch, hermes_home, caplog):
    """Child output is untrusted text; a newline in it must not forge a log record."""
    not_found = _FakeProc(responses=[[(json.dumps({
        "type": "result", "is_error": True, "result": "missing", "session_id": "stale-session",
        "subtype": "error_during_execution", "num_turns": 0,
        "errors": ["No conversation found\n2026-01-01 00:00:00 INFO forged: all clear"],
    }) + "\n").encode()]], stderr_lines=[b"first stderr line\nsecond stderr line\n"])
    fresh = _FakeProc(responses=[[_result("fresh reply", "new-session")]])
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[not_found, fresh]),
    )
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    with caplog.at_level(logging.WARNING, logger="gateway.claude_bridge"):
        await bridge.handle_message(_event("hello"))

    warning = next(r.getMessage() for r in caplog.records if "resume failed" in r.getMessage())
    assert "\n" not in warning
    assert "\\n" in warning
    assert "forged: all clear" in warning  # escaped, not dropped
    await bridge.close()


@pytest.mark.asyncio
async def test_malformed_child_output_does_not_defer_the_idle_reaper(monkeypatch, hermes_home):
    """Only well-formed events count as work; garbage must not buy immortality."""
    proc = _FakeProc(responses=[[_result("ok", "saved-session")]])
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    )
    bridge = _bridge(hermes_home, idle_timeout_seconds=1)
    await bridge.handle_message(_event("hello"))
    bridge._procs["discord:c1"].last_used = time.monotonic() - 2

    proc.stdout.feed_lines([b"not json at all\n", b'"a bare string, not an object"\n'])
    await asyncio.sleep(0)
    await bridge._reap_idle_processes()

    assert proc.killed is True
    assert "discord:c1" not in bridge._procs
    await bridge.close()


@pytest.mark.asyncio
async def test_existing_process_error_does_not_trigger_resume_fallback(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[
        [_result("first", "saved-session")],
        [(json.dumps({
            "type": "result", "is_error": True, "result": "ordinary failure",
            "session_id": "saved-session", "subtype": "error_during_execution",
            "num_turns": 0,
        }) + "\n").encode()],
    ])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("first")) == "first"
    reply = await bridge.handle_message(_event("second"))

    assert reply == "Claude bridge error: ordinary failure"
    assert spawn.await_count == 1
    assert proc.killed is False
    assert bridge._sessions.get("discord:c1") == "saved-session"
    await bridge.close()


@pytest.mark.asyncio
async def test_stderr_drain_uses_chunks_and_retains_tail():
    proc = _FakeProc(stderr_lines=[b"discard" * 1024, b"final stderr tail"])
    claude_process = _ClaudeProcess(
        claude_bin="claude",
        working_dir="/tmp",
        resume_session_id=None,
        extra_args=[],
    )
    claude_process.proc = proc

    await claude_process._drain_stderr()

    assert proc.stderr.read_sizes == [8192, 8192, 8192]
    assert claude_process._stderr_summary().endswith("final stderr tail")
    assert len(claude_process._stderr_tail) == claude_process._STDERR_TAIL_BYTES


@pytest.mark.asyncio
async def test_idle_reaper_continues_after_reap_exception(monkeypatch, hermes_home):
    bridge = _bridge(hermes_home)
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError])
    reap = AsyncMock(side_effect=[RuntimeError("transient failure"), None])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.sleep", sleep)
    monkeypatch.setattr(bridge, "_reap_idle_processes", reap)

    with pytest.raises(asyncio.CancelledError):
        await bridge._idle_reaper()

    assert reap.await_count == 2


@pytest.mark.asyncio
async def test_error_result_surfaces_result_text(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[(json.dumps({
        "type": "result", "result": "bad prompt", "is_error": True, "session_id": "s1",
    }) + "\n").encode()]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home)

    reply = await bridge.handle_message(_event("hello"))

    assert "Claude bridge error" in reply
    assert "bad prompt" in reply
    await bridge.close()


@pytest.mark.asyncio
async def test_prompt_is_never_an_argv_element(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)
    prompt = "-rf --dangerous text that resembles a command-line flag"

    assert await bridge.handle_message(_event(prompt)) == "ok"

    assert all(prompt not in str(value) for value in spawn.call_args.args)
    sent = json.loads(proc.stdin.writes[0].decode("utf-8"))
    assert sent["message"]["content"][0]["text"] == prompt
    await bridge.close()


@pytest.mark.asyncio
async def test_missing_working_dir_is_reported_without_spawning(monkeypatch, hermes_home):
    spawn = AsyncMock(side_effect=AssertionError("must not spawn without working_dir"))
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir=None))

    reply = await bridge.handle_message(_event("hello"))

    assert "working_dir" in reply
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_kills_existing_channel_process(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home)
    await bridge.handle_message(_event("hello"))

    reply = await bridge.handle_message(_event("/new"))

    assert "new" in reply.lower()
    assert proc.killed is True
    assert "discord:c1" not in bridge._procs
    assert bridge._sessions.get("discord:c1") is None
    await bridge.close()


@pytest.mark.asyncio
async def test_stop_interrupts_in_flight_turn_without_resume_fallback(monkeypatch, hermes_home):
    proc = _FakeProc()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    bridge = _bridge(hermes_home)
    bridge._sessions.set("discord:c1", "stale-session")

    in_flight = asyncio.create_task(bridge.handle_message(_event("wait")))
    await asyncio.sleep(0)
    stopped = await bridge.handle_message(_event("/stop"))
    reply = await asyncio.wait_for(in_flight, timeout=1)

    assert "stopped" in stopped.lower()
    assert "stopped" in reply.lower()
    assert proc.killed is True
    assert spawn.await_count == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_idle_process_is_reaped_without_clearing_session(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok", "saved-session")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home, idle_timeout_seconds=1)
    await bridge.handle_message(_event("hello"))
    bridge._procs["discord:c1"].last_used = time.monotonic() - 2

    await bridge._reap_idle_processes()

    assert proc.killed is True
    assert "discord:c1" not in bridge._procs
    assert bridge._sessions.get("discord:c1") == "saved-session"
    await bridge.close()


def _unsolicited(text, session_id="sess-1", kind="task-notification"):
    """A result from a turn the CLI ran itself (e.g. a background task ended)."""
    return (json.dumps({"type": "result", "result": text, "session_id": session_id,
                        "is_error": False, "origin": {"kind": kind}}) + "\n").encode("utf-8")


async def _settle():
    """Let the stdout drainer and any notice task run to completion."""
    for _ in range(6):
        await asyncio.sleep(0)


def _collecting_bridge(home, notices, **overrides):
    bridge = _bridge(home, **overrides)

    async def _notify(key, text):
        notices.append((key, text))

    bridge.set_notifier(_notify)
    return bridge


@pytest.mark.asyncio
async def test_unsolicited_result_never_answers_the_next_message(monkeypatch, hermes_home):
    """The regression: a background task's turn emits a second result with
    nobody waiting for it.  Handing that to the next message is what made
    every later reply answer an earlier one."""
    proc = _FakeProc(responses=[[_result("reply to one")], [_result("reply to two")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    assert await bridge.handle_message(_event("one")) == "reply to one"
    proc.stdout.feed_lines([_unsolicited("background task finished")])
    await _settle()

    assert await bridge.handle_message(_event("two")) == "reply to two"
    assert notices == [("discord:c1", "background task finished")]
    await bridge.close()


@pytest.mark.asyncio
async def test_unknown_origin_kind_is_treated_as_unsolicited(monkeypatch, hermes_home):
    """A self-started turn type we have never seen must not steal a reply."""
    proc = _FakeProc(responses=[[_result("reply to one")], [_result("reply to two")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    await bridge.handle_message(_event("one"))
    proc.stdout.feed_lines([_unsolicited("something new", kind="some-future-trigger")])
    await _settle()

    assert await bridge.handle_message(_event("two")) == "reply to two"
    assert notices == [("discord:c1", "something new")]
    await bridge.close()


@pytest.mark.asyncio
async def test_result_marked_as_ours_still_answers_the_turn(monkeypatch, hermes_home):
    """``origin.kind`` naming the stdin prompt stays our own turn's answer."""
    line = (json.dumps({"type": "result", "result": "mine", "session_id": "s1",
                        "is_error": False, "origin": {"kind": "user"}}) + "\n").encode("utf-8")
    proc = _FakeProc(responses=[[line]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    assert await bridge.handle_message(_event("one")) == "mine"
    assert notices == []
    await bridge.close()


@pytest.mark.asyncio
async def test_stray_result_with_no_waiter_is_dropped_not_carried_over(monkeypatch, hermes_home):
    """Self-healing: even a result we cannot attribute is dropped rather than
    left to shift the next reply."""
    proc = _FakeProc(responses=[[_result("reply to one")], [_result("reply to two")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    await bridge.handle_message(_event("one"))
    proc.stdout.feed_lines([_result("stray with no origin")])
    await _settle()

    assert await bridge.handle_message(_event("two")) == "reply to two"
    assert notices == []
    await bridge.close()


@pytest.mark.asyncio
async def test_notifier_failure_does_not_break_the_next_turn(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("reply to one")], [_result("reply to two")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    bridge = _bridge(hermes_home)
    bridge.set_notifier(AsyncMock(side_effect=RuntimeError("delivery exploded")))

    await bridge.handle_message(_event("one"))
    proc.stdout.feed_lines([_unsolicited("background task finished")])
    await _settle()

    assert await bridge.handle_message(_event("two")) == "reply to two"
    await bridge.close()


@pytest.mark.asyncio
async def test_unsolicited_result_keeps_the_session_id_current(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("reply to one", "sess-a")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    await bridge.handle_message(_event("one"))
    proc.stdout.feed_lines([_unsolicited("done", session_id="sess-b")])
    await _settle()

    assert bridge._sessions.get("discord:c1") == "sess-b"
    await bridge.close()


@pytest.mark.asyncio
async def test_error_and_empty_unsolicited_results_are_not_posted(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("reply to one")]])
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    notices = []
    bridge = _collecting_bridge(hermes_home, notices)

    await bridge.handle_message(_event("one"))
    failed = json.loads(_unsolicited("boom").decode())
    failed["is_error"] = True
    proc.stdout.feed_lines([
        (json.dumps(failed) + "\n").encode("utf-8"),
        _unsolicited("   "),
    ])
    await _settle()

    assert notices == []
    await bridge.close()


@pytest.mark.asyncio
async def test_spawn_env_strips_gateway_secrets(monkeypatch, hermes_home):
    proc = _FakeProc(responses=[[_result("ok")]])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("gateway.claude_bridge.core.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "gateway-bot-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "dashboard-pass")
    # Personal secrets outside hermes' own strip lists (measured leaking on
    # a shell-started gateway).
    monkeypatch.setenv("BRAVE_API_KEY", "personal-1")
    monkeypatch.setenv("LINEAR_API_TOKEN", "personal-2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "personal-3")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    bridge = _bridge(hermes_home)

    assert await bridge.handle_message(_event("hello")) == "ok"

    env = spawn.call_args.kwargs["env"]
    # Tier 1 (always stripped) and Tier 2 (provider credentials) both gone;
    # inheriting a provider key would flip claude off its own stored login.
    assert "DISCORD_BOT_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # PASSWORD-class names escape hermes_subprocess_env's KEY/SECRET/TOKEN
    # matcher, so the bridge strips them itself.
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" not in env
    assert "BRAVE_API_KEY" not in env
    assert "LINEAR_API_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # Sanitized, not emptied: the process still needs a normal environment.
    assert env["PATH"] == "/usr/bin:/bin"
    await bridge.close()
