"""Claude Code CLI bridge.

Routes inbound gateway messages to one persistent ``claude -p`` process per
channel instead of the built-in agent loop, when opted in via
``GatewayConfig.claude_bridge``.  The process speaks line-delimited
``stream-json`` on stdin/stdout and remains alive across turns.
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
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gateway.claude_bridge.config import ClaudeBridgeConfig
from gateway.platforms.base import MessageEvent
from hermes_cli.providers import (
    CLAUDE_BRIDGE_MODEL_ID,
    CLAUDE_BRIDGE_REASONING_EFFORTS,
)
from hermes_constants import get_hermes_home
from utils import atomic_replace

# Hardcoded: this module was the flat ``gateway/claude_bridge.py`` before the
# package split, and external log filters key on the exact logger name.
logger = logging.getLogger("gateway.claude_bridge")

DECISION_REQUIRED_KEYS = ("decision_id", "channel_id", "question", "options")

# Permission mode `/yolo` switches a channel to. The native `/yolo` bypasses
# Hermes' own dangerous-command approval gate, which the bridged CLI never
# passes through; the closest thing on this path is the CLI's own permission
# mode, so the shared name means "stop asking me" on both.
YOLO_PERMISSION_MODE = "bypassPermissions"

# How much of a failed --resume result's error text reaches the log. Long
# enough to name the cause, short enough that one line stays one line.
_RESUME_ERROR_EXCERPT_CHARS = 400

# Env var name fragments that mark a value as secret-like. Broader than
# hermes_subprocess_env's own lists on purpose: over-stripping costs the
# bridged session nothing (its claude authenticates from its own stored
# login, not env), while under-stripping hands secrets to a session driven
# by whoever talks to the bot. `_KEY` also catches `_API_KEY`/`ACCESS_KEY_ID`.
_SECRET_NAME_FRAGMENTS = (
    "PASSWORD",
    "PASSPHRASE",
    "SECRET",
    "CREDENTIAL",
    "APIKEY",
    "_TOKEN",
    "_KEY",
)


def _flatten_for_log(text: str) -> str:
    """Render untrusted child output as exactly one log line.

    The resume-failure excerpt and the stderr tail are the CLI's own output,
    which echoes whatever conversation it was processing.  An embedded newline
    there would forge log records that read as separate, legitimate entries.
    """
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def build_media_context_note(
    media_urls: Optional[List[str]], media_types: Optional[List[str]] = None
) -> str:
    """Prompt prefix telling the CLI what the event's attachments are and
    where they live, so it reads them instead of asking the user to paste
    their contents (same failure this bridge is prone to as the one
    ``_build_document_context_note`` in ``gateway/run.py`` documents).

    ``media_urls`` entries are almost always local absolute paths written by
    the platform adapter's own attachment cache. A bare URL only appears when
    that caching failed and the adapter fell back to the platform's CDN link
    (see Discord's adapter). Paths are never rewritten: this bridge execs the
    CLI directly on the host (``create_subprocess_exec(cwd=working_dir)``),
    so a raw host-absolute path is already valid for the CLI's own tools —
    unlike the Docker/Modal terminal backends, no cache-path translation
    applies on this path.
    """
    # Both fields arrive straight from a platform adapter, so neither the
    # element type nor the two lists' lengths are guaranteed here.
    urls = [str(u).strip() for u in (media_urls or []) if str(u or "").strip()]
    if not urls:
        return ""
    types = list(media_types or [])
    lines = []
    for i, url in enumerate(urls):
        raw_type = types[i] if i < len(types) else None
        mtype = f" ({raw_type})" if isinstance(raw_type, str) and raw_type.strip() else ""
        if os.path.isabs(url):
            # An absolute path stays a path even when the file is already
            # gone (the cache is pruned on a timer): say so rather than
            # mislabelling it a remote URL the CLI could try to fetch.
            missing = "" if os.path.exists(url) else " [no longer on disk]"
            lines.append(f"- {url}{mtype}{missing}")
        else:
            lines.append(f"- {url}{mtype} [remote URL, not a local file]")
    return (
        "[The user attached the following file(s) with this message:\n"
        + "\n".join(lines)
        + "\nThese are binary attachments; their content is not inlined here. "
        "Read each one yourself with your own tools (e.g. Read, or the "
        "terminal) before answering, instead of asking the user what they "
        "contain.]\n\n"
    )


def _normalize_bridge_model(model: object) -> str:
    value = model.strip() if isinstance(model, str) else ""
    return "" if value.lower() == CLAUDE_BRIDGE_MODEL_ID else value


def _normalize_bridge_effort(effort: object) -> str:
    value = effort.strip().lower() if isinstance(effort, str) else ""
    return value if value in CLAUDE_BRIDGE_REASONING_EFFORTS else ""


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


def parse_channel_key(key: str) -> tuple[str, Optional[str], Optional[str]]:
    """Inverse of ``channel_key``: ``(platform, chat_id, thread_id)``.

    Used to address a channel that no inbound message is pointing at — a turn
    the CLI ran by itself has no message to reply to.
    """
    parts = key.split(":")
    platform = parts[0] if parts else ""
    chat_id = parts[1] if len(parts) > 1 and parts[1] not in ("", "-") else None
    thread_id = parts[2] if len(parts) > 2 and parts[2] not in ("", "-") else None
    return platform, chat_id, thread_id


def _event_user_id(event: MessageEvent) -> str:
    """Sender ID for audit logging; ``-`` when the platform omits one."""
    source = getattr(event, "source", None)
    user_id = getattr(source, "user_id", None) if source is not None else None
    return str(user_id) if user_id else "-"


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
        # A persistence failure here must not discard a spawn that already
        # succeeded — the in-memory map still has it for this process, and
        # worst case a future message just starts a fresh claude session
        # instead of resuming (degraded, not lost).
        try:
            _atomic_write_json(self._path, self._data)
        except Exception:
            logger.warning(
                "claude_bridge: failed to persist session map to %s",
                self._path, exc_info=True,
            )

    def clear(self, key: str) -> None:
        self._load()
        if self._data.pop(key, None) is not None:
            try:
                _atomic_write_json(self._path, self._data)
            except Exception:
                logger.warning(
                    "claude_bridge: failed to persist session map to %s",
                    self._path, exc_info=True,
                )


@dataclass
class _SpawnOutcome:
    parsed: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timed_out: bool = False
    interrupted: bool = False


class _ClaudeProcess:
    """One long-lived ``claude -p`` stream-json subprocess for a channel.

    The user prompt deliberately travels only through a JSON line on stdin,
    never argv.  Besides avoiding argv injection from messages beginning with
    ``-``, this avoids the operating system's argv-size limit for large chat
    messages.

    Not every turn the CLI runs was asked for over stdin: a completed
    background task makes it run one on its own and emit a second ``result``.
    Those results carry an ``origin`` field, ours carry none, and stdout is
    drained continuously so an unsolicited result is routed away from the
    turn waiting for its own answer instead of being handed to it.
    """

    _STDERR_TAIL_BYTES = 4 * 1024
    # How long to wait for a child that closed stdout to actually exit.  The
    # reap happens on the drainer, outside any turn's timeout, so an unbounded
    # wait here would strand the turn until its own (much longer) deadline.
    _EOF_REAP_TIMEOUT = 5
    # Drain deadline for a mid-turn steering write.  Short on purpose: a
    # stdin pipe that stays full this long means the child stopped reading,
    # and the caller falls back to queueing behind the turn instead.
    _INJECT_DRAIN_TIMEOUT = 10
    # ``origin.kind`` values that still mean "this answers the prompt we put
    # on stdin".  Anything else — today only ``task-notification`` — is a turn
    # the CLI started by itself.  Unknown kinds are treated as unsolicited so
    # a future self-started turn type cannot silently steal a reply.
    _SELF_ORIGIN_KINDS = frozenset({"user", "prompt", "stdin", "cli"})

    def __init__(
        self,
        *,
        claude_bin: str,
        working_dir: str,
        resume_session_id: Optional[str],
        extra_args: List[str],
        model: str = "",
        effort: str = "",
        on_unsolicited_result: Optional[Callable[[Dict[str, Any], bool], None]] = None,
    ):
        self._claude_bin = claude_bin
        self._working_dir = working_dir
        self._resume_session_id = resume_session_id
        self._extra_args = list(extra_args)
        self.model = _normalize_bridge_model(model)
        self.effort = _normalize_bridge_effort(effort)
        self._on_unsolicited_result = on_unsolicited_result
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_tail = ""
        self._completed_result_count = 0
        self._turn_waiter: Optional[asyncio.Future] = None
        self.last_used = time.monotonic()
        self.is_turn_active = False
        self.intentional_stop = False

    @property
    def is_alive(self) -> bool:
        """Usable for a turn: the child runs *and* something still reads it.

        A dead drainer means nothing will ever resolve a turn's waiter, so a
        process in that state is retired and respawned rather than written to.
        """
        if self.proc is None or self.proc.returncode is not None:
            return False
        return self._stdout_task is None or not self._stdout_task.done()

    @property
    def is_unproven_resume(self) -> bool:
        return self._resume_session_id is not None and self._completed_result_count == 0

    async def start(self) -> Optional[_SpawnOutcome]:
        """Launch the stream-json process and begin draining stderr."""
        args = [
            self._claude_bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self._resume_session_id:
            args += ["--resume", self._resume_session_id]
        args += self._extra_args
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]

        try:
            # The gateway's environment carries every injected secret (bot
            # tokens, provider API keys). None of them belongs in the bridged
            # session: its shell is driven by whoever talks to the bot, and an
            # inherited provider key would also flip the CLI from its own
            # stored login onto direct API billing. Lazy import mirrors the
            # other spawn sites' circular-import avoidance.
            from tools.environments.local import hermes_subprocess_env

            # hermes_subprocess_env strips Hermes's OWN secrets (fixed lists
            # plus narrow AUXILIARY_*/GATEWAY_RELAY_* patterns) — it knows
            # nothing about personal secrets the gateway's environment may
            # carry (measured on live spawns: a dashboard basic-auth PASSWORD,
            # and BRAVE/OBSIDIAN/LINEAR/CONTEXT7 API keys inherited from a
            # shell-started gateway). Drop anything secret-looking by name;
            # a bridged session has no legitimate use for any of them.
            env = {
                k: v
                for k, v in hermes_subprocess_env().items()
                if not any(f in k for f in _SECRET_NAME_FRAGMENTS)
            }

            # Claude can emit a result line far larger than asyncio's default
            # 64 KiB StreamReader limit, so keep a deliberately generous cap.
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._working_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except Exception as exc:
            return _SpawnOutcome(error=f"failed to spawn `{self._claude_bin}`: {exc}")

        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="claude-bridge-stderr"
        )
        # stdout is drained even between turns: an unsolicited turn's events
        # would otherwise sit unread until the next message, and its result
        # would be the first thing that message's read saw.
        self._stdout_task = asyncio.create_task(
            self._drain_stdout(), name="claude-bridge-stdout"
        )
        return None

    async def _drain_stderr(self) -> None:
        """Continuously drain stderr so the child can never block on its pipe."""
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            while True:
                chunk = await self.proc.stderr.read(8192)
                if not chunk:
                    return
                self._stderr_tail = (
                    self._stderr_tail + chunk.decode("utf-8", errors="replace")
                )[-self._STDERR_TAIL_BYTES:]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "claude_bridge: stderr drain stopped; child may block on stderr",
                exc_info=True,
            )

    def _stderr_summary(self) -> str:
        return self._stderr_tail.strip() or "(no stderr)"

    @staticmethod
    def _user_event_payload(prompt: str) -> bytes:
        event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        return (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    async def send_turn(self, prompt: str, timeout_seconds: int) -> _SpawnOutcome:
        """Write one user event and wait for the result event that answers it."""
        if not self.is_alive or self.proc is None or self.proc.stdin is None:
            return _SpawnOutcome(error="claude process is not running")
        if self.intentional_stop:
            return _SpawnOutcome(error="Claude turn was stopped", interrupted=True)

        payload = self._user_event_payload(prompt)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._turn_waiter = waiter
        self.is_turn_active = True
        self.last_used = time.monotonic()
        try:
            self.proc.stdin.write(payload)
            await asyncio.wait_for(self.proc.stdin.drain(), timeout=timeout_seconds)
            return await asyncio.wait_for(waiter, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self.terminate()
            return _SpawnOutcome(
                error=f"claude timed out after {timeout_seconds}s", timed_out=True,
            )
        except Exception as exc:
            if self.intentional_stop:
                return _SpawnOutcome(error="Claude turn was stopped", interrupted=True)
            await self.terminate()
            return _SpawnOutcome(error=f"claude stream failed: {exc}")
        finally:
            # Cleared even when the awaiting task is cancelled outright, so a
            # late result can never be delivered to an abandoned turn.
            if self._turn_waiter is waiter:
                self._turn_waiter = None
            self.is_turn_active = False
            self.last_used = time.monotonic()

    async def inject_prompt(self, prompt: str) -> Optional[str]:
        """Write an extra user event into the turn in flight; no waiter.

        The CLI folds a mid-turn user message into the active turn (same
        steering as typing into an interactive session), so the one result
        the turn's own waiter is holding for answers both prompts.  If the
        turn happens to finish before this message is read, the CLI runs it
        as a turn of its own and its result arrives with no waiter — which
        ``_consume_line`` routes to the unsolicited notifier, so the text
        still reaches the channel.

        Returns an error string, or None on success.
        """
        if not self.is_alive or self.proc is None or self.proc.stdin is None:
            return "claude process is not running"
        if self.intentional_stop:
            return "Claude turn was stopped"
        try:
            self.proc.stdin.write(self._user_event_payload(prompt))
        except Exception as exc:
            return f"claude stream failed: {exc}"
        try:
            await asyncio.wait_for(
                self.proc.stdin.drain(), timeout=self._INJECT_DRAIN_TIMEOUT
            )
        except asyncio.TimeoutError:
            # write() already handed the bytes to the transport irrevocably;
            # a slow drain is backpressure, not a failed delivery.  Reporting
            # failure here would make the caller queue the same text again
            # and deliver it twice once the pipe empties.
            logger.warning(
                "claude_bridge: steering drain exceeded %ss; treating the "
                "message as delivered", self._INJECT_DRAIN_TIMEOUT,
            )
        except Exception as exc:
            # The transport is gone (e.g. broken pipe), so the bytes were
            # not delivered — the caller may safely queue the message.
            return f"claude stream failed: {exc}"
        self.last_used = time.monotonic()
        return None

    async def _drain_stdout(self) -> None:
        """Read stream-json events for the process's whole life, not per turn."""
        if self.proc is None or self.proc.stdout is None:
            self._resolve_turn(_SpawnOutcome(error="claude process has no stdout stream"))
            return
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    await self._handle_stdout_eof()
                    return
                self._consume_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("claude_bridge: stdout drain failed", exc_info=True)
            self._resolve_turn(_SpawnOutcome(error=f"claude stream failed: {exc}"))

    async def _handle_stdout_eof(self) -> None:
        if self.intentional_stop:
            self._resolve_turn(
                _SpawnOutcome(error="Claude turn was stopped", interrupted=True)
            )
            return
        # Give the always-running stderr task one event-loop turn to consume
        # bytes that arrived alongside stdout's final EOF.  Do not wait for it
        # to finish: a misbehaving child could keep stderr open after closing
        # stdout, and this error path must still resolve the turn promptly.
        if self._stderr_task is not None and not self._stderr_task.done():
            await asyncio.sleep(0)
        try:
            returncode = (
                await asyncio.wait_for(self.proc.wait(), timeout=self._EOF_REAP_TIMEOUT)
                if self.proc is not None else None
            )
        except Exception:
            # Includes the timeout: report whatever returncode we have and let
            # the turn fail now rather than holding it open on a stuck child.
            returncode = self.proc.returncode if self.proc is not None else None
        self._resolve_turn(
            _SpawnOutcome(
                error=(
                    "claude process ended before a result "
                    f"(exit {returncode}): {self._stderr_summary()}"
                )
            )
        )

    def _consume_line(self, line: bytes) -> None:
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("claude_bridge: ignoring unparseable stream-json line: %s", exc)
            return
        if not isinstance(event, dict):
            logger.warning("claude_bridge: ignoring non-object stream-json event")
            return
        # A well-formed event proves the child is still working, including on a
        # turn it started on its own between ours.  Refreshing only at turn
        # start/end let the idle clock run while a background turn was in
        # flight, so the reaper killed the session mid-tool-call and left a
        # transcript ending on a tool_use nothing ever answered.
        #
        # Deliberately not paired with a hard max-lifetime cap: any such cap
        # eventually kills a child that is legitimately working, which is the
        # exact failure this refresh exists to prevent.  Malformed output is
        # excluded above so a child looping on garbage still ages out.
        self.last_used = time.monotonic()
        if event.get("type") != "result":
            return
        self._completed_result_count += 1
        if self._is_unsolicited(event):
            self._emit_unsolicited(event)
            return
        if not self._resolve_turn(_SpawnOutcome(parsed=event)):
            # Nobody is waiting, so this cannot be an answer we owe a turn —
            # handing it to a waiter would shift every later reply by one.
            # It can still be text the channel deserves (a steering message
            # that raced the turn's end and ran as its own turn, or a turn
            # abandoned by cancellation), so it goes out as a notice.
            logger.info(
                "claude_bridge: routing a result that arrived with no turn "
                "waiting for it to the notifier (session=%s)",
                event.get("session_id"),
            )
            self._emit_unsolicited(event, orphaned=True)

    @classmethod
    def _is_unsolicited(cls, event: Dict[str, Any]) -> bool:
        """True when the CLI ran this turn on its own, not from our stdin."""
        origin = event.get("origin")
        if not origin:
            return False
        kind = origin.get("kind") if isinstance(origin, dict) else origin
        return str(kind) not in cls._SELF_ORIGIN_KINDS

    def _emit_unsolicited(self, event: Dict[str, Any], *, orphaned: bool = False) -> None:
        """``orphaned``: a result no turn was waiting for (vs. a turn the CLI
        started on its own) — e.g. a steered message that raced its turn's end."""
        if self._on_unsolicited_result is None:
            return
        try:
            self._on_unsolicited_result(event, orphaned)
        except Exception:
            logger.exception("claude_bridge: unsolicited-result handler failed")

    def _resolve_turn(self, outcome: _SpawnOutcome) -> bool:
        waiter = self._turn_waiter
        if waiter is None or waiter.done():
            return False
        self._turn_waiter = None
        waiter.set_result(outcome)
        return True

    async def terminate(self) -> None:
        """Kill the subprocess and cancel its stream drainers, idempotently."""
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                logger.debug("claude_bridge: failed to kill claude process", exc_info=True)
            try:
                await proc.wait()
            except Exception:
                logger.debug("claude_bridge: failed to reap claude process", exc_info=True)

        for task in (self._stderr_task, self._stdout_task):
            if task is None or task is asyncio.current_task() or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("claude_bridge: stream task cleanup failed", exc_info=True)

        # The drainer that would have reported EOF is gone, so a turn still
        # waiting here would otherwise hang until its own timeout.
        self._resolve_turn(
            _SpawnOutcome(
                error=(
                    "Claude turn was stopped" if self.intentional_stop
                    else "claude process was terminated before a result"
                ),
                interrupted=self.intentional_stop,
            )
        )


class ClaudeBridge:
    """Owns persistent Claude processes, session continuity, and halt state."""

    def __init__(
        self,
        config: ClaudeBridgeConfig,
        notifier: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.config = config
        self._sessions = _SessionMap(sessions_file())
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._key_locks_guard = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
        self._procs: Dict[str, _ClaudeProcess] = {}
        # Per-channel `--permission-mode` overrides set by /mode.  Deliberately
        # in-memory: a gateway restart returns every channel to the configured
        # default instead of silently resuming a widened mode.
        self._mode_overrides: Dict[str, str] = {}
        self._turns_in_progress: set[str] = set()
        self._stop_requested_keys: set[str] = set()
        self._idle_reaper_task: Optional[asyncio.Task] = None
        self._notifier = notifier
        self._notice_tasks: set[asyncio.Task] = set()
        self._closed = False
        # Surface a misconfiguration at startup rather than staying silent
        # until the first inbound message hits the same check in
        # handle_message() (F7) — a gateway that never receives a message on
        # this channel would otherwise never reveal the problem.
        if config.enabled and not config.working_dir:
            logger.warning(
                "claude_bridge.enabled=true but claude_bridge.working_dir is "
                "unset — every bridged message will fail until it's configured"
            )
        # halt_users is fail-open by design (an empty list lets anyone stop the
        # bridge), but /mode and /yolo reuse it to *widen* permissions.
        # Surface the one combination where that inversion has teeth.
        if (
            config.enabled
            and not config.halt_users
            and "bypassPermissions" in self._allowed_modes()
        ):
            logger.warning(
                "claude_bridge: /mode and /yolo can raise a channel to "
                "bypassPermissions and claude_bridge.halt_users is empty, so "
                "any paired sender may do it — set halt_users, or drop "
                "bypassPermissions from allowed_permission_modes"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def set_notifier(
        self, notifier: Optional[Callable[[str, str], Awaitable[None]]]
    ) -> None:
        """Bind the sink for turns the CLI ran without a prompt from us.

        Kept platform-agnostic like ``OutboxWatcher``'s poster: the caller
        supplies ``(channel_key, text) -> awaitable``.  Without one, a
        background task's completion report is logged and dropped rather than
        surfacing in the chat that started it.
        """
        self._notifier = notifier

    def _handle_unsolicited_result(
        self, key: str, event: Dict[str, Any], *, orphaned: bool = False
    ) -> None:
        """Route a result no turn is waiting for away from the message stream."""
        session_id = event.get("session_id")
        if session_id:
            self._sessions.set(key, str(session_id))
        origin = event.get("origin")
        logger.info(
            "claude_bridge: unsolicited turn for key=%s origin=%s is_error=%s "
            "orphaned=%s",
            key, origin, event.get("is_error"), orphaned,
        )
        text = event.get("result")
        if event.get("is_error"):
            if not orphaned:
                # A background turn's error is log noise, not a reply anyone
                # is waiting on (pinned behavior).
                return
            # An orphaned turn is typically a steered message that raced its
            # turn's end — its sender already got a "sent" ack, so a silent
            # drop here would be the last they ever hear of it.
            body = text.strip() if isinstance(text, str) else ""
            text = f"Claude bridge error: {body or '(no result text)'}"
        elif not isinstance(text, str) or not text.strip():
            return
        notifier = self._notifier
        if notifier is None:
            logger.info(
                "claude_bridge: no notifier bound — dropping unsolicited text for %s", key
            )
            return
        task = asyncio.create_task(self._deliver_notice(notifier, key, text))
        # Held so the loop cannot garbage-collect an in-flight delivery.
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _deliver_notice(
        self,
        notifier: Callable[[str, str], Awaitable[None]],
        key: str,
        text: str,
    ) -> None:
        try:
            await notifier(key, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "claude_bridge: failed to deliver unsolicited result for %s", key
            )

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

    def _is_halted(self) -> bool:
        return halt_flag_file().exists()

    def _effective_mode(self, key: str) -> str:
        return self._mode_overrides.get(key, self.config.default_permission_mode)

    def _allowed_modes(self) -> List[str]:
        allowed = list(self.config.allowed_permission_modes)
        if self.config.default_permission_mode not in allowed:
            allowed.append(self.config.default_permission_mode)
        return allowed

    def _is_halt_authorized(self, event: MessageEvent) -> bool:
        if not self.config.halt_users:
            return True
        source = getattr(event, "source", None)
        user_id = getattr(source, "user_id", None) if source is not None else None
        return bool(user_id) and str(user_id) in set(self.config.halt_users)

    def _start_idle_reaper(self) -> None:
        """Start one deferred reaper once the first child exists."""
        if self._closed or self._idle_reaper_task is not None:
            return
        self._idle_reaper_task = asyncio.create_task(
            self._idle_reaper(), name="claude-bridge-idle-reaper"
        )

    async def _idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._reap_idle_processes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("claude_bridge: idle process reaper failed")

    async def _reap_idle_processes(self) -> None:
        """Terminate idle children; their persisted session IDs remain intact."""
        timeout = self.config.idle_timeout_seconds
        if timeout <= 0:
            return
        now = time.monotonic()
        for key, proc in list(self._procs.items()):
            if proc.is_turn_active or now - proc.last_used < timeout:
                continue
            logger.info("claude_bridge: reaping idle process for %s", key)
            await self._discard_process(key, proc)

    async def _discard_process(
        self,
        key: str,
        process: Optional[_ClaudeProcess] = None,
        *,
        intentional_stop: bool = False,
    ) -> None:
        """Remove and terminate a process, without altering its session map."""
        proc = process or self._procs.get(key)
        if proc is None:
            return
        if self._procs.get(key) is proc:
            self._procs.pop(key, None)
        if intentional_stop:
            proc.intentional_stop = True
        await proc.terminate()

    async def _discard_all_processes(self, *, intentional_stop: bool = False) -> None:
        processes = list(self._procs.items())
        self._procs.clear()
        for _key, proc in processes:
            if intentional_stop:
                proc.intentional_stop = True
        await asyncio.gather(
            *(proc.terminate() for _key, proc in processes), return_exceptions=True,
        )

    async def _get_or_spawn_process(
        self, key: str, resume_session_id: Optional[str], model: str, effort: str
    ) -> tuple[Optional[_ClaudeProcess], Optional[_SpawnOutcome]]:
        model = _normalize_bridge_model(model)
        effort = _normalize_bridge_effort(effort)
        existing = self._procs.get(key)
        if existing is not None and existing.is_alive:
            existing.model = _normalize_bridge_model(existing.model)
            existing.effort = _normalize_bridge_effort(existing.effort)
            if existing.model == model and existing.effort == effort:
                return existing, None
        if existing is not None and existing.is_alive:
            model_changed = existing.model != model
            effort_changed = existing.effort != effort
            if model_changed and effort_changed:
                logger.info(
                    "claude_bridge: model and effort changed for %s "
                    "(model %s -> %s; effort %s -> %s); respawning",
                    key, existing.model, model, existing.effort, effort,
                )
            elif model_changed:
                logger.info(
                    "claude_bridge: model changed for %s (%s -> %s); respawning",
                    key, existing.model, model,
                )
            else:
                logger.info(
                    "claude_bridge: effort changed for %s (%s -> %s); respawning",
                    key, existing.effort, effort,
                )
        if existing is not None:
            await self._discard_process(key, existing)

        proc = _ClaudeProcess(
            claude_bin=self.config.claude_bin,
            working_dir=self.config.resolved_working_dir,
            resume_session_id=resume_session_id,
            # The mode is read at spawn time, not cached on the process, so a
            # respawn after /mode or an idle reap picks up the current value.
            # It trails extra_args because the CLI honors the last occurrence:
            # a --permission-mode written into extra_args must not outrank the
            # runtime state.
            extra_args=[
                *self.config.extra_args,
                "--permission-mode",
                self._effective_mode(key),
            ],
            model=model,
            effort=effort,
            on_unsolicited_result=(
                lambda event, orphaned, key=key: self._handle_unsolicited_result(
                    key, event, orphaned=orphaned
                )
            ),
        )
        spawn_error = await proc.start()
        if spawn_error is not None:
            return None, spawn_error
        # /stop is intentionally handled outside the per-key lock so it can
        # interrupt an active read.  It may arrive while this process is still
        # spawning, in which case consume the pending stop before any input.
        if key in self._stop_requested_keys or self._is_halted():
            await self._discard_process(key, proc, intentional_stop=True)
            return None, _SpawnOutcome(error="Claude turn was stopped", interrupted=True)
        self._procs[key] = proc
        self._start_idle_reaper()
        return proc, None

    @staticmethod
    def _resume_failure_reason(parsed: Dict[str, Any]) -> Optional[str]:
        """Why a ``--resume`` turn failed, or None when it did not fail that way.

        The two branches have different causes — a transcript the CLI could not
        locate, versus a session it located but could not restart — and only
        this text tells them apart afterwards.  Callers log it verbatim; a bare
        "resume failed" leaves the cause unrecoverable once the process is gone.
        """
        errors = parsed.get("errors")
        error_text = (
            "\n".join(str(item) for item in errors)
            if isinstance(errors, list)
            else str(errors or "")
        ).strip()
        if "No conversation found" in error_text:
            return f"transcript not found: {error_text[:_RESUME_ERROR_EXCERPT_CHARS]}"
        if (
            parsed.get("subtype") == "error_during_execution"
            and parsed.get("num_turns") == 0
        ):
            detail = error_text or str(parsed.get("result") or "").strip()
            suffix = f": {detail[:_RESUME_ERROR_EXCERPT_CHARS]}" if detail else ""
            return f"error_during_execution with num_turns=0{suffix}"
        return None

    def _save_result_session(self, key: str, outcome: _SpawnOutcome) -> None:
        parsed = outcome.parsed
        if parsed is None:
            return
        session_id = parsed.get("session_id")
        if session_id:
            self._sessions.set(key, str(session_id))

    async def close(self) -> None:
        """Stop the reaper and all resident subprocesses during gateway exit."""
        self._closed = True
        task = self._idle_reaper_task
        self._idle_reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        notices = list(self._notice_tasks)
        self._notice_tasks.clear()
        for notice in notices:
            notice.cancel()
        if notices:
            await asyncio.gather(*notices, return_exceptions=True)
        await self._discard_all_processes(intentional_stop=True)

    async def handle_message(
        self, event: MessageEvent, *, model: str = "", effort: str = ""
    ) -> Optional[str]:
        """Gateway ``MessageHandler`` for sessions routed to this provider."""
        model = _normalize_bridge_model(model)
        effort = _normalize_bridge_effort(effort)
        text = (event.text or "").strip()

        if text == "!halt":
            if not self._is_halt_authorized(event):
                return "Not authorized to halt the Claude bridge."
            halt_flag_file().parent.mkdir(parents=True, exist_ok=True)
            halt_flag_file().touch(exist_ok=True)
            await self._discard_all_processes(intentional_stop=True)
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

        # Session-scoped commands must not be sent through as literal Claude
        # prompts.  /stop stays outside the per-key lock to interrupt a turn
        # that is currently awaiting its result event.
        key = channel_key(event)
        command = event.get_command()
        if command in ("new", "reset"):
            lock = await self._lock_for(key)
            async with lock:
                await self._discard_process(key)
                self._sessions.clear(key)
                # A fresh session starts from the configured mode: keeping a
                # widened override alive across /new would outlive the task it
                # was granted for.
                self._mode_overrides.pop(key, None)
            return (
                "Started a new Claude session for this channel "
                f"(permission mode {self.config.default_permission_mode})."
            )
        if command == "mode":
            return await self._handle_mode_command(event, key)
        if command == "yolo":
            return await self._handle_yolo_command(event, key)
        if command == "stop":
            if key in self._turns_in_progress:
                self._stop_requested_keys.add(key)
            await self._discard_process(key, intentional_stop=True)
            return "Claude turn stopped."

        if not self.config.working_dir:
            logger.error(
                "claude_bridge.enabled=true but claude_bridge.working_dir is unset"
            )
            return "Claude bridge is misconfigured: working_dir is not set."

        media_note = build_media_context_note(
            getattr(event, "media_urls", None), getattr(event, "media_types", None)
        )
        if media_note:
            text = f"{media_note}{text}"

        lock = await self._lock_for(key)
        if lock.locked():
            steer_reply = await self._try_steer_active_turn(key, text)
            if steer_reply is not None:
                return steer_reply
        async with self._semaphore:
            async with lock:
                return await self._handle_locked(key, text, model, effort)

    async def _try_steer_active_turn(self, key: str, text: str) -> Optional[str]:
        """Feed a message into this channel's in-flight turn, if one exists.

        This is what lets a mid-run Discord message reach the CLI immediately
        instead of queueing behind the per-key lock until the turn ends.  Only
        plain prompts land here — commands are intercepted earlier.  Returns
        the reply to post, or None to fall through to the queue path (no turn
        actually active, e.g. the lock holder is still spawning, or the
        process stopped taking input).  A model/effort override on a steered
        message is deliberately not applied — respawning would kill the very
        turn being steered; it takes effect from the next queued turn.
        """
        proc = self._procs.get(key)
        if proc is None or not proc.is_alive or not proc.is_turn_active:
            return None
        error = await proc.inject_prompt(text)
        if error is not None:
            logger.warning(
                "claude_bridge: steering the active turn for %s failed (%s) "
                "— queueing the message instead", key, error,
            )
            return None
        logger.info("claude_bridge: steered the active turn for %s", key)
        return "⏩ Sent to the Claude turn already running in this channel."

    async def try_steer(self, key: str, text: str) -> Optional[str]:
        """Public entry point for a caller outside ``handle_message``'s own
        lock — the gateway's busy-session hook, which intercepts a follow-up
        message before it would otherwise be queued behind the native
        per-session guard. Same contract as the internal steering path:
        returns the reply to send, or None when there is no turn to steer
        into (the caller should fall back to its own queueing)."""
        return await self._try_steer_active_turn(key, text)

    async def _handle_mode_command(self, event: MessageEvent, key: str) -> str:
        """``/mode [name|default]`` — read or switch this channel's mode.

        Matching is case-insensitive: the CLI's mode names are camelCase and
        mobile keyboards capitalize a leading word.
        """
        allowed = self._allowed_modes()
        requested = event.get_command_args().strip()
        if not requested:
            return (
                f"Permission mode: {self._effective_mode(key)} "
                f"(default {self.config.default_permission_mode}). "
                f"Switch with /mode <{' | '.join(allowed)}>, or /mode default."
            )
        # Every attempt to widen the mode is logged with its sender: this
        # command is reachable from a chat client, so the server log is the
        # only audit trail of who changed it.
        user_id = _event_user_id(event)
        if not self._is_halt_authorized(event):
            logger.warning(
                "claude_bridge: rejected unauthorized /mode %r from %s in %s",
                requested[:40],
                user_id,
                key,
            )
            return "Not authorized to change the Claude permission mode."

        canonical = {mode.lower(): mode for mode in allowed}
        if requested.lower() in ("default", "reset"):
            target = None
        elif requested.lower() in canonical:
            target = canonical[requested.lower()]
        else:
            logger.warning(
                "claude_bridge: rejected /mode %r from %s in %s (not allowed)",
                requested[:40],
                user_id,
                key,
            )
            return (
                f"Unknown permission mode: {requested}. "
                f"Allowed: {' | '.join(allowed)}, or default."
            )

        effective = await self._apply_mode(key, target, user_id)
        return (
            f"Permission mode for this channel is now {effective}, starting with "
            f"the next message. A gateway restart returns it to "
            f"{self.config.default_permission_mode}."
        )

    async def _handle_yolo_command(self, event: MessageEvent, key: str) -> str:
        """``/yolo`` — toggle this channel between its default mode and bypass.

        Mirrors the native gateway command of the same name: no arguments, a
        plain toggle.  It acts on a different layer (see
        ``YOLO_PERMISSION_MODE``), so the reply names the mode it landed on
        rather than claiming the native command's effect.
        """
        # Authorization first, like /mode: the configured mode list must not be
        # readable by a sender who is not allowed to change the mode, and every
        # rejection has to leave its sender in the log.
        user_id = _event_user_id(event)
        if not self._is_halt_authorized(event):
            logger.warning(
                "claude_bridge: rejected unauthorized /yolo from %s in %s",
                user_id,
                key,
            )
            return "Not authorized to change the Claude permission mode."

        allowed = self._allowed_modes()
        if YOLO_PERMISSION_MODE not in allowed:
            logger.warning(
                "claude_bridge: rejected /yolo from %s in %s (%s not allowed)",
                user_id,
                key,
                YOLO_PERMISSION_MODE,
            )
            return (
                f"/yolo needs {YOLO_PERMISSION_MODE}, which "
                f"claude_bridge.allowed_permission_modes does not include "
                f"({' | '.join(allowed)})."
            )

        turning_on = self._effective_mode(key) != YOLO_PERMISSION_MODE
        effective = await self._apply_mode(
            key, YOLO_PERMISSION_MODE if turning_on else None, user_id
        )
        if turning_on:
            return (
                f"YOLO on: permission mode {effective} for this channel, "
                "starting with the next message. Send /yolo again to turn it "
                "off; a gateway restart also does."
            )
        if effective == YOLO_PERMISSION_MODE:
            # Clearing the override cannot go below the configured default.
            return (
                f"YOLO override cleared, but claude_bridge."
                f"default_permission_mode is {effective}, so this channel "
                "still bypasses permission checks."
            )
        return (
            f"YOLO off: permission mode is back to {effective}, starting with "
            "the next message."
        )

    async def _apply_mode(
        self, key: str, target: Optional[str], user_id: str
    ) -> str:
        """Set (``target``) or clear (``None``) the override; return what took effect."""
        lock = await self._lock_for(key)
        async with lock:
            previous = self._effective_mode(key)
            if target is None:
                self._mode_overrides.pop(key, None)
            else:
                self._mode_overrides[key] = target
            effective = self._effective_mode(key)
            # The mode is a spawn flag, so a resident process keeps the old one
            # until it is replaced.  Its session ID is left in place, so the
            # next turn resumes the same conversation under the new mode.  A
            # request that lands on the mode already in effect replaces
            # nothing, so the process is left alive.
            if effective != previous:
                await self._discard_process(key)
        logger.info(
            "claude_bridge: %s set permission mode for %s to %s",
            user_id,
            key,
            effective,
        )
        return effective

    async def _handle_locked(self, key: str, text: str, model: str, effort: str) -> str:
        resume_id = self._sessions.get(key)
        self._turns_in_progress.add(key)
        try:
            proc, spawn_error = await self._get_or_spawn_process(
                key, resume_id, model, effort
            )
            attempted_resume = proc is not None and proc.is_unproven_resume
            outcome = spawn_error or await proc.send_turn(text, self.config.timeout_seconds)
            self._save_result_session(key, outcome)

            # A process which timed out or ended mid-turn is unusable.  Its
            # saved session ID is intentionally retained so a later turn can
            # launch --resume and preserve context.
            if outcome.parsed is None:
                if proc is not None:
                    await self._discard_process(key, proc)
                if outcome.interrupted:
                    return "Claude turn stopped."
                return f"Claude bridge error: {outcome.error}"

            fallback_note = ""
            # A stale --resume ID is reported as an error *result*, not a
            # non-zero process exit.  Retry exactly once as a fresh process;
            # timeouts and intentional /stop interruptions never retry.
            resume_failure = (
                self._resume_failure_reason(outcome.parsed)
                if (
                    attempted_resume
                    and outcome.parsed.get("is_error")
                    and not outcome.timed_out
                    and not outcome.interrupted
                )
                else None
            )
            if resume_failure is not None:
                logger.warning(
                    "claude_bridge: resume failed for %s (session=%s): %s | stderr: %s"
                    " — retrying as a new session",
                    key,
                    resume_id,
                    _flatten_for_log(resume_failure),
                    _flatten_for_log(proc._stderr_summary()) if proc is not None else "",
                )
                if proc is not None:
                    await self._discard_process(key, proc)
                self._sessions.clear(key)
                proc, spawn_error = await self._get_or_spawn_process(
                    key, None, model, effort
                )
                outcome = spawn_error or await proc.send_turn(text, self.config.timeout_seconds)
                self._save_result_session(key, outcome)
                fallback_note = "\n\n(resume failed — started a new Claude session)"

            if outcome.parsed is None:
                if proc is not None:
                    await self._discard_process(key, proc)
                if outcome.interrupted:
                    return "Claude turn stopped."
                return f"Claude bridge error: {outcome.error}{fallback_note}"

            parsed = outcome.parsed
            session_id = parsed.get("session_id")
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
        finally:
            self._turns_in_progress.discard(key)
            self._stop_requested_keys.discard(key)


# Sized for Discord's 100-char button custom_id budget
# ("claude_bridge_decision:<decision_id>:<option_id>" — a 23-char prefix
# plus 2 separators leaves 76 chars for decision_id + option_id combined).
# Discord is the only decision renderer today, but the cap is enforced here
# (platform-agnostic) so any future renderer inherits the same safe bound
# instead of discovering its own limit via a posting exception.
MAX_DECISION_ID_LEN = 32
MAX_OPTION_ID_LEN = 32


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
        allowed_channels: Optional[List[str]] = None,
    ):
        self._poster = poster
        self._interval = interval
        self._stop = asyncio.Event()
        # Non-empty restricts which outbox channel_id values are honored
        # (C3) — empty (the default) means unrestricted.
        self._allowed_channels = set(allowed_channels) if allowed_channels else None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
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
        # Re-created every tick (cheap, idempotent) rather than once in
        # run(): a failure here must go through the same per-tick
        # try/except as the rest of the loop and be retried next tick,
        # not kill the whole watcher task before it logs anything.
        outbox_dir().mkdir(parents=True, exist_ok=True)
        outbox_processed_dir().mkdir(parents=True, exist_ok=True)
        outbox_failed_dir().mkdir(parents=True, exist_ok=True)
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

        decision_id = str(data.get("decision_id"))
        if len(decision_id) > MAX_DECISION_ID_LEN:
            logger.warning(
                "claude_bridge: outbox file %s decision_id is %d chars (max %d)",
                path.name, len(decision_id), MAX_DECISION_ID_LEN,
            )
            self._move(path, outbox_failed_dir())
            return
        for opt in (data.get("options") or []):
            opt_id = str(opt.get("id")) if isinstance(opt, dict) else ""
            if len(opt_id) > MAX_OPTION_ID_LEN:
                logger.warning(
                    "claude_bridge: outbox file %s has an option id of %d chars (max %d)",
                    path.name, len(opt_id), MAX_OPTION_ID_LEN,
                )
                self._move(path, outbox_failed_dir())
                return

        if self._allowed_channels is not None:
            channel_id = str(data.get("channel_id"))
            if channel_id not in self._allowed_channels:
                logger.warning(
                    "claude_bridge: outbox file %s channel_id %r is not in "
                    "claude_bridge.decision_channels — refusing to post",
                    path.name, channel_id,
                )
                self._move(path, outbox_failed_dir())
                return

        try:
            await self._poster(data)
        except Exception:
            logger.exception("claude_bridge: poster failed for outbox file %s", path.name)
            self._move(path, outbox_failed_dir())
            return

        if not self._move(path, outbox_processed_dir()):
            # The decision was already posted — leaving the source file in
            # outbox/ would re-post it every tick forever. Rename it out of
            # the *.json glob in place so it's excluded from the next scan,
            # even though it isn't in processed/ where it belongs.
            logger.error(
                "claude_bridge: posted %s but could not move it to processed/ "
                "— renamed in place to avoid a duplicate-post loop; move it "
                "to outbox/processed/ manually",
                path.name,
            )
            try:
                path.replace(path.with_name(path.name + ".posted"))
            except Exception:
                logger.exception(
                    "claude_bridge: failed to even rename %s out of the outbox "
                    "glob — it WILL be re-posted next tick", path,
                )

    @staticmethod
    def _move(path: Path, dest_dir: Path) -> bool:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(dest_dir / path.name)
            return True
        except Exception:
            logger.exception("claude_bridge: failed to move %s -> %s", path, dest_dir)
            return False
