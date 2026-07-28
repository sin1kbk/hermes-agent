"""Claude Code CLI bridge.

Routes inbound gateway messages to a spawned ``claude -p`` process instead of
the built-in agent loop, when opted in via ``GatewayConfig.claude_bridge``.
Platform-agnostic by design (see ``AGENTS.md`` — platform specifics such as
Discord's button ``View`` live in the adapter, not here).

Layout under ``$HERMES_HOME/claude_bridge/``::

    sessions.json     channel key -> claude session_id map
    halt              presence = spawning is paused (machine kill switch)
    outbox/*.json      pending escalation decisions, written by the running
                       claude session (via a future tool), consumed by
                       OutboxWatcher
    outbox/processed/ outbox files that were posted successfully
    outbox/failed/     outbox files that failed schema validation or posting
    decisions/<id>.json   answers (or timeouts), written by the platform
                          adapter's button callback for a later resume to read
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gateway.config import ClaudeBridgeConfig
from gateway.platforms.base import MessageEvent
from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

DECISION_REQUIRED_KEYS = ("decision_id", "channel_id", "question", "options")


def _bridge_home() -> Path:
    return get_hermes_home() / "claude_bridge"


def sessions_file() -> Path:
    return _bridge_home() / "sessions.json"


def halt_flag_file() -> Path:
    return _bridge_home() / "halt"


def outbox_dir() -> Path:
    return _bridge_home() / "outbox"


def outbox_processed_dir() -> Path:
    return outbox_dir() / "processed"


def outbox_failed_dir() -> Path:
    return outbox_dir() / "failed"


def decisions_dir() -> Path:
    return _bridge_home() / "decisions"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    atomic_replace(tmp, path)


def write_decision_answer(decision_id: str, choice: str, user_id: Optional[str]) -> None:
    """Persist a button click as the answer to an outstanding decision.

    Consumed by whatever later resumes the originating claude session — this
    module only writes the file, it never waits on it (no in-process sync).
    """
    payload = {
        "decision_id": decision_id,
        "choice": choice,
        "user_id": user_id,
        "answered_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(decisions_dir() / f"{decision_id}.json", payload)


def write_decision_timeout(decision_id: str) -> None:
    """Persist an unanswered decision that expired."""
    _atomic_write_json(
        decisions_dir() / f"{decision_id}.json",
        {"choice": None, "timed_out": True},
    )


def channel_key(event: MessageEvent) -> str:
    """Derive a stable per-channel/thread key for session continuity.

    Distinct from ``gateway.session.build_session_key`` (which encodes the
    gateway's own per-user/per-profile isolation policy) — the bridge always
    keys purely on platform + chat + thread, one claude session per Discord
    channel/thread regardless of who posts in it.
    """
    source = getattr(event, "source", None)
    platform = source.platform.value if source is not None and source.platform else "unknown"
    chat_id = getattr(source, "chat_id", None) if source is not None else None
    thread_id = getattr(source, "thread_id", None) if source is not None else None
    parts = [platform, str(chat_id) if chat_id else "-"]
    if thread_id:
        parts.append(str(thread_id))
    return ":".join(parts)


class _SessionMap:
    """Persists the channel-key -> claude session_id map as JSON."""

    def __init__(self, path: Path):
        self._path = path
        self._data: Dict[str, str] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = raw if isinstance(raw, dict) else {}
        except Exception:
            logger.warning(
                "claude_bridge: %s unreadable, starting with an empty session map",
                self._path, exc_info=True,
            )
            self._data = {}

    def get(self, key: str) -> Optional[str]:
        self._load()
        val = self._data.get(key)
        return str(val) if val else None

    def set(self, key: str, session_id: str) -> None:
        self._load()
        self._data[key] = session_id
        _atomic_write_json(self._path, self._data)

    def clear(self, key: str) -> None:
        self._load()
        if self._data.pop(key, None) is not None:
            _atomic_write_json(self._path, self._data)


@dataclass
class _SpawnOutcome:
    parsed: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timed_out: bool = False


async def _spawn_claude(
    *,
    claude_bin: str,
    working_dir: str,
    prompt: str,
    resume_session_id: Optional[str],
    extra_args: List[str],
    timeout_seconds: int,
) -> _SpawnOutcome:
    """Spawn ``claude -p`` and parse its JSON output.

    Every failure mode (spawn error, non-zero exit, timeout, unparseable
    output) is reported via ``_SpawnOutcome.error`` rather than raised, so
    the caller always has a user-facing message to return — silent failure
    is not an option for a chat-facing bridge.
    """
    args = [claude_bin, "-p", prompt, "--output-format", "json"]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    args += list(extra_args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        return _SpawnOutcome(error=f"failed to spawn `{claude_bin}`: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return _SpawnOutcome(
            error=f"claude timed out after {timeout_seconds}s", timed_out=True,
        )

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        return _SpawnOutcome(
            error=f"claude exited {proc.returncode}: {stderr_text[:2000] or '(no stderr)'}"
        )

    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        return _SpawnOutcome(error=f"claude produced unparseable output: {exc}")

    if not isinstance(parsed, dict):
        return _SpawnOutcome(error="claude produced non-object JSON output")

    return _SpawnOutcome(parsed=parsed)


class ClaudeBridge:
    """Owns spawn serialization, session continuity, and the halt switch."""

    def __init__(self, config: ClaudeBridgeConfig):
        self.config = config
        self._sessions = _SessionMap(sessions_file())
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._key_locks_guard = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrency))

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

    def _is_halted(self) -> bool:
        return halt_flag_file().exists()

    def _is_halt_authorized(self, event: MessageEvent) -> bool:
        if not self.config.halt_users:
            return True
        source = getattr(event, "source", None)
        user_id = getattr(source, "user_id", None) if source is not None else None
        return bool(user_id) and str(user_id) in set(self.config.halt_users)

    async def handle_message(self, event: MessageEvent) -> Optional[str]:
        """Gateway ``MessageHandler``: replaces ``_handle_message`` when enabled."""
        text = (event.text or "").strip()

        if text == "!halt":
            if not self._is_halt_authorized(event):
                return "Not authorized to halt the Claude bridge."
            halt_flag_file().parent.mkdir(parents=True, exist_ok=True)
            halt_flag_file().touch(exist_ok=True)
            return "halted"

        if text == "!unhalt":
            if not self._is_halt_authorized(event):
                return "Not authorized to unhalt the Claude bridge."
            try:
                halt_flag_file().unlink()
            except FileNotFoundError:
                pass
            return "unhalted"

        if self._is_halted():
            return "Claude bridge is halted (send !unhalt to resume)."

        if not self.config.working_dir:
            logger.error(
                "claude_bridge.enabled=true but claude_bridge.working_dir is unset"
            )
            return "Claude bridge is misconfigured: working_dir is not set."

        key = channel_key(event)
        lock = await self._lock_for(key)
        async with self._semaphore:
            async with lock:
                return await self._handle_locked(key, text)

    async def _handle_locked(self, key: str, text: str) -> str:
        resume_id = self._sessions.get(key)
        outcome = await _spawn_claude(
            claude_bin=self.config.claude_bin,
            working_dir=self.config.working_dir,
            prompt=text,
            resume_session_id=resume_id,
            extra_args=self.config.extra_args,
            timeout_seconds=self.config.timeout_seconds,
        )

        fallback_note = ""
        # A resume failure most often means claude no longer recognizes the
        # stored session_id (e.g. its local history was pruned) — fall back
        # to a fresh spawn rather than leaving the user stuck. Skip this for
        # timeouts: a slow *resume* almost certainly means a slow *fresh*
        # spawn too, and retrying would silently double the user's wait.
        if outcome.parsed is None and resume_id and not outcome.timed_out:
            logger.warning(
                "claude_bridge: resume failed for %s (%s); retrying as a new session",
                key, outcome.error,
            )
            self._sessions.clear(key)
            outcome = await _spawn_claude(
                claude_bin=self.config.claude_bin,
                working_dir=self.config.working_dir,
                prompt=text,
                resume_session_id=None,
                extra_args=self.config.extra_args,
                timeout_seconds=self.config.timeout_seconds,
            )
            fallback_note = "\n\n(resume failed — started a new Claude session)"

        if outcome.parsed is None:
            return f"Claude bridge error: {outcome.error}"

        parsed = outcome.parsed
        session_id = parsed.get("session_id")
        if session_id:
            self._sessions.set(key, str(session_id))

        logger.info(
            "claude_bridge: key=%s session=%s cost=%s is_error=%s",
            key, session_id, parsed.get("total_cost_usd"), parsed.get("is_error"),
        )

        result_text = parsed.get("result")
        if parsed.get("is_error"):
            return f"Claude bridge error: {result_text or '(no result text)'}{fallback_note}"

        if not isinstance(result_text, str) or not result_text.strip():
            return f"Claude bridge: empty result from claude{fallback_note}"

        return f"{result_text}{fallback_note}"


class OutboxWatcher:
    """Polls ``claude_bridge/outbox/*.json`` and hands each to ``poster``.

    Kept platform-agnostic: ``poster`` is supplied by whichever platform
    adapter knows how to render an interactive decision (currently Discord's
    ``DiscordAdapter.post_claude_bridge_decision``). This class only owns the
    outbox file lifecycle (pending -> processed/failed) and the poll loop.
    """

    def __init__(
        self,
        poster: Callable[[Dict[str, Any]], Awaitable[None]],
        interval: float = 2.0,
    ):
        self._poster = poster
        self._interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        outbox_dir().mkdir(parents=True, exist_ok=True)
        outbox_processed_dir().mkdir(parents=True, exist_ok=True)
        outbox_failed_dir().mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("claude_bridge: outbox watcher tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        for path in sorted(outbox_dir().glob("*.json")):
            if path.is_file():
                await self._process_one(path)

    async def _process_one(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("claude_bridge: invalid outbox file %s: %s", path.name, exc)
            self._move(path, outbox_failed_dir())
            return

        if not isinstance(data, dict) or not all(k in data for k in DECISION_REQUIRED_KEYS):
            logger.warning(
                "claude_bridge: outbox file %s missing required keys %s",
                path.name, DECISION_REQUIRED_KEYS,
            )
            self._move(path, outbox_failed_dir())
            return

        try:
            await self._poster(data)
        except Exception:
            logger.exception("claude_bridge: poster failed for outbox file %s", path.name)
            self._move(path, outbox_failed_dir())
            return

        self._move(path, outbox_processed_dir())

    @staticmethod
    def _move(path: Path, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(dest_dir / path.name)
        except Exception:
            logger.exception("claude_bridge: failed to move %s -> %s", path, dest_dir)
