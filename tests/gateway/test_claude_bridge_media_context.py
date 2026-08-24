"""Tests for prepending a media-attachment context note to bridged turns.

The platform adapter caches Discord attachments to local paths and puts them
on ``MessageEvent.media_urls`` / ``.media_types``, but ``ClaudeBridge`` used
to read only ``event.text`` — every attachment was silently dropped. These
tests cover the note builder itself, that halt/unhalt/session commands are
unaffected by attachments (the note must be prepended after command
interception, not before), and that both the steer path and the normal
locked-turn path see the prefixed text.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.claude_bridge import ClaudeBridge
from gateway.claude_bridge.core import build_media_context_note
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


def _event(text: str, *, media_urls=None, media_types=None, user_id="u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        media_urls=media_urls or [],
        media_types=media_types or [],
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


def _prompt_text(payload: bytes) -> str:
    return json.loads(payload)["message"]["content"][0]["text"]


def _controllable_process():
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


async def _wait_for(predicate, timeout=2.0):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


# --- 1. builder unit tests -------------------------------------------------


def test_no_media_urls_returns_empty_string():
    assert build_media_context_note(None) == ""
    assert build_media_context_note([]) == ""


def test_local_absolute_path_appears_unmodified(tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"fake")
    note = build_media_context_note([str(f)], ["image/png"])

    assert str(f) in note
    # Not rewritten into any other form (no cache-path translation).
    assert note.count(str(f)) == 1


def test_url_fallback_is_distinguished_from_a_local_path(tmp_path):
    local = tmp_path / "photo.png"
    local.write_bytes(b"fake")
    url = "https://cdn.discordapp.com/attachments/1/2/photo.png"

    note = build_media_context_note([str(local), url], ["image/png", "image/png"])

    assert url in note
    local_line = next(line for line in note.splitlines() if str(local) in line)
    url_line = next(line for line in note.splitlines() if url in line)
    assert local_line != url_line
    assert "remote" in url_line.lower() or "url" in url_line.lower()


def test_path_is_never_rewritten(tmp_path):
    f = tmp_path / "sub dir" / "doc.pdf"
    f.parent.mkdir()
    f.write_bytes(b"fake")
    note = build_media_context_note([str(f)])

    assert str(f) in note


# --- 2. commands are unaffected by attachments ------------------------------


@pytest.mark.parametrize("command", ["!halt", "!unhalt", "/new", "/mode", "/yolo", "/stop"])
def test_command_reply_unaffected_by_media_attachments(hermes_home, monkeypatch, tmp_path, command):
    _never_spawn(monkeypatch)
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"fake")

    plain_bridge = _bridge(hermes_home)
    plain_reply = asyncio.run(plain_bridge.handle_message(_event(command)))

    media_bridge = _bridge(hermes_home)
    media_reply = asyncio.run(
        media_bridge.handle_message(
            _event(command, media_urls=[str(attachment)], media_types=["image/png"])
        )
    )

    assert media_reply == plain_reply


# --- 3. steer path sees the prefixed text -----------------------------------


@pytest.mark.asyncio
async def test_steered_turn_receives_the_media_note(hermes_home, monkeypatch, tmp_path):
    proc, written, lines = _controllable_process()
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    bridge = _bridge(hermes_home)
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"fake")

    turn = asyncio.create_task(bridge.handle_message(_event("long task")))
    await _wait_for(lambda: len(written) == 1)

    reply = await bridge.handle_message(
        _event("look at this", media_urls=[str(attachment)], media_types=["image/png"])
    )

    assert "already running" in reply
    assert len(written) == 2
    steered_text = _prompt_text(written[1])
    assert str(attachment) in steered_text
    assert steered_text.endswith("look at this")

    lines.put_nowait((json.dumps({
        "type": "result", "result": "done", "session_id": "s1", "is_error": False,
    }) + "\n").encode())
    await asyncio.wait_for(turn, timeout=2)
    await bridge.close()


# --- 4. normal locked-turn path sees the prefixed text ----------------------


@pytest.mark.asyncio
async def test_locked_turn_receives_the_media_note(hermes_home, monkeypatch, tmp_path):
    proc, written, lines = _controllable_process()
    monkeypatch.setattr(
        "gateway.claude_bridge.core.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    bridge = _bridge(hermes_home)
    attachment = tmp_path / "doc.pdf"
    attachment.write_bytes(b"fake")

    turn = asyncio.create_task(
        bridge.handle_message(
            _event("summarize this", media_urls=[str(attachment)], media_types=["application/pdf"])
        )
    )
    await _wait_for(lambda: len(written) == 1)

    sent_text = _prompt_text(written[0])
    assert str(attachment) in sent_text
    assert sent_text.endswith("summarize this")

    lines.put_nowait((json.dumps({
        "type": "result", "result": "done", "session_id": "s1", "is_error": False,
    }) + "\n").encode())
    await asyncio.wait_for(turn, timeout=2)
    await bridge.close()
