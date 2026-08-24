"""``GatewayRunner`` mixin: claude-bridge message routing and lifecycle.

Only ever mixed into ``GatewayRunner`` (same pattern as
``GatewaySlashCommandsMixin``), so it freely reaches into runner internals —
``self._handle_message``, ``self._session_key_for_source``,
``self._peek_session_state``, ``self._normalize_source_for_session_key``,
``self._rehydrate_session_model_override``, ``self._resolve_session_reasoning_config``,
``self._gate_unauthorized_message``, ``self._session_model_overrides``,
``self.adapters``, ``self.config`` — that only exist there.

``gateway.run`` imports this module at the top of the file, so its own
module-level helpers (``_get_channel_override``, ``_load_gateway_runtime_config``,
``_is_slack_ignored_channel``) are imported lazily, inside the method bodies
that need them, to avoid importing a not-yet-fully-loaded module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from gateway.claude_bridge.core import (
    ClaudeBridge,
    OutboxWatcher,
    build_media_context_note,
    channel_key,
    parse_channel_key,
)
from gateway.claude_bridge.config import ClaudeBridgeConfig
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.providers import (
    CLAUDE_BRIDGE_MODEL_ID,
    CLAUDE_BRIDGE_PROVIDER_ID,
    CLAUDE_BRIDGE_REASONING_EFFORTS,
)

# Matches gateway.run's own logger name: these methods moved out of that
# file, and their logs (plus a couple of pinned tests) key on it.
logger = logging.getLogger("gateway.run")


def _claude_bridge_effort_from_reasoning_config(reasoning_config: object) -> str:
    """Translate Hermes reasoning state into the Claude Code CLI's subset."""
    if not isinstance(reasoning_config, dict) or reasoning_config.get("enabled") is not True:
        return ""
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    if effort in CLAUDE_BRIDGE_REASONING_EFFORTS:
        return effort
    if effort:
        logger.warning(
            "claude_bridge: ignoring unsupported reasoning effort %r; supported: %s",
            effort,
            ", ".join(CLAUDE_BRIDGE_REASONING_EFFORTS),
        )
    return ""


class ClaudeBridgeRunnerMixin:
    """Routes gateway messages to the Claude bridge and owns its lifecycle."""

    # Disabled-by-default singleton so ``GatewayRunner.__new__(GatewayRunner)``
    # test doubles (common in this suite) don't need to know about the Claude
    # bridge to exercise unrelated methods via ``_select_message_handler()``.
    claude_bridge: "ClaudeBridge" = ClaudeBridge(ClaudeBridgeConfig())
    _claude_bridge_outbox_watcher: Optional["OutboxWatcher"] = None
    _claude_bridge_outbox_watcher_task: Optional[asyncio.Task] = None
    # Per-channel bridge turn counts driving the periodic thread re-title.
    # Lazily created per instance (never a shared mutable class default) and
    # deliberately in-memory: a restart just re-titles on the next turn.
    _claude_bridge_retitle_counts: Optional[Dict[str, int]] = None
    _claude_bridge_retitle_tasks: Optional[set] = None
    # Re-title on every Nth bridge turn (1, 6, 11, ...). Turn 1 upgrades the
    # raw message excerpt the Discord adapter named the thread with; the later
    # fires let the name follow the work as it moves.
    _RETITLE_EVERY = 5

    def _init_claude_bridge(self) -> None:
        self.claude_bridge = ClaudeBridge(self.config.claude_bridge)
        self._claude_bridge_outbox_watcher: Optional[OutboxWatcher] = None
        self._claude_bridge_outbox_watcher_task: Optional[asyncio.Task] = None
        self._claude_bridge_retitle_counts: Dict[str, int] = {}
        self._claude_bridge_retitle_tasks: set = set()

    async def _close_claude_bridge(self) -> None:
        """Stop resident bridge subprocesses during gateway shutdown.

        Claude bridge turns are not represented by ``_running_agents``. Closed
        explicitly so shutdown does not leave resident CLI subprocesses (or
        its idle reaper task) behind.
        """
        try:
            await self.claude_bridge.close()
        except Exception as _e:
            logger.warning("Claude bridge cleanup during shutdown failed: %s", _e, exc_info=True)

    def _is_native_command(self, event: MessageEvent, command_name: str) -> bool:
        """Return whether a command (including a quick alias) targets a native handler."""
        command = event.get_command()
        if not command:
            return False

        from hermes_cli.commands import resolve_command

        command_def = resolve_command(command)
        if command_def is not None:
            return command_def.name == command_name

        if isinstance(self.config, dict):
            quick_commands = self.config.get("quick_commands", {}) or {}
        else:
            quick_commands = getattr(self.config, "quick_commands", {}) or {}
        if not isinstance(quick_commands, dict):
            return False

        quick_command = quick_commands.get(command)
        if not isinstance(quick_command, dict) or quick_command.get("type") != "alias":
            return False

        target = (quick_command.get("target") or "").strip()
        target_command = target.lstrip("/").split(maxsplit=1)[0] if target else ""
        target_def = resolve_command(target_command) if target_command else None
        return target_def is not None and target_def.name == command_name

    def _is_model_switch_command(self, event: MessageEvent) -> bool:
        """Return whether a command must be handled by the native model switcher."""
        return self._is_native_command(event, "model")

    def _is_reasoning_command(self, event: MessageEvent) -> bool:
        """Return whether a command must be handled by the native reasoning handler."""
        return self._is_native_command(event, "reasoning")

    def _select_message_handler(self) -> Callable[[MessageEvent], "asyncio.Future"]:
        """Return the handler adapters should register for inbound messages.

        Every message path (``_primary_message_handler`` behind the initial
        connect and reconnect registration sites, the multiplex
        default-profile wrapper, and the per-profile wrappers) routes through
        here. The returned router resolves the effective provider for every
        message, so a session-scoped ``/model`` switch takes effect without
        reconnecting an adapter.
        """
        return self._route_message

    def _resolve_effective_message_provider(
        self, source: SessionSource
    ) -> tuple[str, str]:
        """Resolve provider and model as session override, channel override, then config."""
        from hermes_cli.model_switch import resolve_effective_model

        normalized_source = self._normalize_source_for_session_key(source)
        session_key = self._session_key_for_source(normalized_source)
        self._rehydrate_session_model_override(session_key)

        state = self._peek_session_state(session_key)
        session_override = (
            state.conversation.model_override if state is not None else None
        )
        channel_override = None
        session_provider = ""
        if isinstance(session_override, dict):
            session_provider = str(
                session_override.get("provider") or ""
            ).strip().lower()

        from gateway.run import _get_channel_override, _load_gateway_runtime_config

        config = getattr(self, "config", None)
        channel_provider = ""
        if isinstance(getattr(config, "platforms", None), dict):
            channel_override = _get_channel_override(
                config,
                normalized_source.platform,
                str(normalized_source.chat_id or ""),
                thread_id=(
                    str(getattr(normalized_source, "thread_id", ""))
                    if getattr(normalized_source, "thread_id", None)
                    else None
                ),
                parent_id=(
                    str(getattr(normalized_source, "parent_chat_id", ""))
                    if getattr(normalized_source, "parent_chat_id", None)
                    else None
                ),
            )
            channel_provider = str(
                getattr(channel_override, "provider", "") or ""
            ).strip().lower()

        try:
            runtime_config = _load_gateway_runtime_config()
        except Exception:
            logger.warning(
                "Failed to load model.provider for message routing: "
                "session_key=%s platform=%s chat_id=%s",
                session_key,
                normalized_source.platform,
                normalized_source.chat_id,
                exc_info=True,
            )
            return (
                session_provider or channel_provider,
                resolve_effective_model(session_override, channel_override, None),
            )
        model_config = runtime_config.get("model", {})
        if not isinstance(model_config, dict):
            return (
                session_provider or channel_provider,
                resolve_effective_model(session_override, channel_override, None),
            )
        return (
            session_provider
            or channel_provider
            or str(model_config.get("provider") or "").strip().lower(),
            resolve_effective_model(
                session_override,
                channel_override,
                str(model_config.get("default") or model_config.get("model") or ""),
            ),
        )

    async def _route_message(self, event: MessageEvent) -> Optional[str]:
        """Dispatch one message to the bridge or the native agent loop."""
        if not self.claude_bridge.enabled:
            return await self._handle_message(event)

        resolved_route = await asyncio.to_thread(
            self._resolve_effective_message_provider,
            event.source,
        )
        if isinstance(resolved_route, tuple):
            provider, model = resolved_route
        else:
            provider, model = resolved_route, ""
        if self._is_model_switch_command(event) or self._is_reasoning_command(event):
            return await self._handle_message(event)
        if provider == CLAUDE_BRIDGE_PROVIDER_ID:
            model = model.strip() if isinstance(model, str) else ""
            if model.lower() == CLAUDE_BRIDGE_MODEL_ID:
                model = ""
            reasoning_config = await asyncio.to_thread(
                self._resolve_session_reasoning_config,
                source=event.source,
                model=model,
            )
            effort = _claude_bridge_effort_from_reasoning_config(reasoning_config)
            if not effort:
                return await self._claude_bridge_handler(event, model=model)
            return await self._claude_bridge_handler(event, model=model, effort=effort)
        return await self._handle_message(event)

    async def _claude_bridge_handle_busy_message(
        self, event: MessageEvent, session_key: str, adapter: Any,
    ) -> bool:
        """Give a bridge session first refusal on its own busy-message.

        Called from ``GatewayRunner._handle_active_session_busy_message``
        (the native busy-session hook) before it falls into the
        native-agent-centric queue/interrupt/steer machinery, which has no
        notion of a bridge turn (bridge turns never populate ``turn.agent``,
        so that machinery always degraded to its queue fallback — the
        pre-steering behavior a Discord user saw even after ``ClaudeBridge``
        itself grew a steering path, since messages never reached it while
        the native guard considered the session busy).

        Returns True when this method fully handled the message (steered and
        acked it), so the caller must return immediately without running its
        own queue/interrupt logic. Returns False for every case where the
        bridge has no opinion — wrong provider, ``busy_mode: queue``, a
        command, or no turn to steer into — so the caller's existing
        behavior (queue behind the current turn) is completely unaffected.
        """
        if not self.claude_bridge.enabled:
            return False
        provider, _model = await asyncio.to_thread(
            self._resolve_effective_message_provider, event.source
        )
        if provider != CLAUDE_BRIDGE_PROVIDER_ID:
            return False
        if self.claude_bridge.config.busy_mode != "steer":
            return False
        # A bridge-specific command (/mode, /yolo, /stop, ...) must reach
        # ClaudeBridge.handle_message's own command interception, not be
        # steered in as literal prompt text — same as a plain "no turn to
        # steer into" miss, this falls through to the native queue fallback.
        if event.get_command():
            return False
        text = (event.text or "").strip()
        # This path never reaches ClaudeBridge.handle_message, so it has to
        # build the attachment note itself — otherwise a message sent while a
        # turn is running loses its attachments, and an attachment-only
        # message (empty text) is not steered at all.
        media_note = build_media_context_note(
            getattr(event, "media_urls", None), getattr(event, "media_types", None)
        )
        if not text and not media_note:
            return False
        text = f"{media_note}{text}" if media_note else text

        reply = await self.claude_bridge.try_steer(channel_key(event), text)
        if reply is None:
            return False

        # The steer already landed on the CLI's stdin — that side effect is
        # committed and cannot be undone. From here on this method MUST
        # return True unconditionally: returning False after a successful
        # steer would fall through to the native queue path and deliver the
        # same text to the CLI a second time as a follow-up turn. A failure
        # sending the ack is a lost notification, not grounds to re-deliver.
        try:
            reply_anchor = self._reply_anchor_for_event(event)
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=reply,
                reply_to=reply_anchor,
                metadata=self._thread_metadata_for_source(event.source, reply_anchor),
            )
        except Exception:
            logger.warning(
                "claude_bridge: steered the active turn for %s but failed to "
                "send the ack",
                session_key,
                exc_info=True,
            )
        return True

    def _bind_claude_bridge_notifier(self, adapter: Optional[Any] = None) -> None:
        """Give the bridge somewhere to post turns Claude ran unprompted.

        A finished background task makes the CLI run a turn nobody sent a
        message for, so its report has nothing to be the reply to. Called
        from the same connect paths as ``_start_claude_bridge_outbox_watcher``
        and re-binds freely: the adapter object changes across reconnects.
        Callable opportunistically from a connect/reconnect flow — any
        failure here is logged and swallowed so it can never break that
        caller's own flow.
        """
        try:
            if not self.claude_bridge.enabled:
                return
            if adapter is None:
                adapter = self.adapters.get(Platform.DISCORD)
            send = getattr(adapter, "send", None)
            if send is None:
                return
            adapter_platform = getattr(adapter, "platform", None)
            platform_value = getattr(adapter_platform, "value", None)

            async def _post_unsolicited(key: str, text: str) -> None:
                platform, chat_id, thread_id = parse_channel_key(key)
                if chat_id is None or (platform_value and platform != platform_value):
                    logger.warning(
                        "claude_bridge: no route for unsolicited result on key %r", key,
                    )
                    return
                metadata = {"thread_id": thread_id} if thread_id else None

                # A background-task turn has no _process_message_background
                # run of its own to extract its MEDIA: tags, so without this
                # a MEDIA:<path> tag would show up as inert text with
                # nothing attached. Extraction has to precede the send for
                # the same reason base.py extracts first: the tag is stripped
                # from the text that reaches the chat. Cheap substring check
                # first so a plain-text notice never touches extraction.
                media_files = []
                if "MEDIA:" in text:
                    media_files, text = adapter.extract_media(text)
                    media_files = adapter.filter_media_delivery_paths(media_files)

                if text.strip():
                    await send(chat_id, text, metadata=metadata)
                if not text.strip() and not media_files:
                    logger.warning(
                        "claude_bridge: unsolicited notice for %s had nothing "
                        "left to deliver after media extraction", key,
                    )
                for media_path, is_voice in media_files:
                    try:
                        if is_voice:
                            result = await adapter.send_voice(
                                chat_id=chat_id, audio_path=media_path,
                                metadata=metadata,
                            )
                        else:
                            result = await adapter.send_document(
                                chat_id=chat_id, file_path=media_path,
                                metadata=metadata,
                            )
                        if not result.success:
                            logger.warning(
                                "claude_bridge: failed to deliver unsolicited "
                                "attachment %s: %s", media_path, result.error,
                            )
                            # A server-side log is invisible to the person
                            # waiting in chat; the normal turn path answers
                            # the same failure with this notice (#66797).
                            await adapter._notify_media_delivery_failure(
                                chat_id, media_path, is_voice=is_voice,
                                metadata=metadata,
                            )
                    except Exception:
                        logger.warning(
                            "claude_bridge: error sending unsolicited attachment %s",
                            media_path, exc_info=True,
                        )
                        try:
                            await adapter._notify_media_delivery_failure(
                                chat_id, media_path, is_voice=is_voice,
                                metadata=metadata,
                            )
                        except Exception:
                            logger.warning(
                                "claude_bridge: could not even notify the failed "
                                "delivery of %s", media_path, exc_info=True,
                            )

            self.claude_bridge.set_notifier(_post_unsolicited)
        except Exception:
            logger.debug("claude_bridge: notifier bind failed", exc_info=True)

    def _start_claude_bridge_outbox_watcher(self, adapter: Optional[Any] = None) -> None:
        """Start the Claude bridge escalation-outbox watcher, once.

        Idempotent and safe to call opportunistically from anywhere an
        adapter just came online — initial startup, the failed-platform
        reconnect watcher, and the multiplex secondary-profile connect path
        all call this on every successful connect (F1). Discord may not be
        connected yet the first time this runs (e.g. it's the platform that
        failed and is retrying), so without those extra call sites the
        watcher would stay off for the gateway's entire lifetime.

        Discord is the only platform with a decision-button ``View`` today
        (``DiscordAdapter.post_claude_bridge_decision``); ``adapter`` lets
        callers pass the adapter that just connected directly instead of
        re-deriving it, and non-Discord adapters are simply ignored (no
        ``post_claude_bridge_decision`` attribute). Any failure here is
        logged and swallowed so it can never break its caller's own
        connect/reconnect flow.
        """
        try:
            if self.claude_bridge.enabled and self._claude_bridge_outbox_watcher is None:
                self._spawn_claude_bridge_outbox_watcher(adapter)
        except Exception:
            logger.debug("claude_bridge: outbox watcher start failed", exc_info=True)

    def _spawn_claude_bridge_outbox_watcher(self, adapter: Optional[Any] = None) -> None:
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        poster = getattr(adapter, "post_claude_bridge_decision", None)
        if poster is None:
            return

        self._claude_bridge_outbox_watcher = OutboxWatcher(
            poster, allowed_channels=self.claude_bridge.config.decision_channels,
        )
        task = asyncio.create_task(self._claude_bridge_outbox_watcher.run())
        # Hold a strong reference — asyncio.create_task() only keeps a weak
        # one, so an unreferenced task can be GC'd mid-run (see the
        # _restart_task comment on the same pattern).
        self._claude_bridge_outbox_watcher_task = task

        def _on_watcher_done(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "claude_bridge: escalation outbox watcher died: %s", exc, exc_info=exc,
                )

        task.add_done_callback(_on_watcher_done)
        logger.info("claude_bridge: escalation outbox watcher started (discord)")

    async def _claude_bridge_handler(
        self, event: MessageEvent, *, model: str = "", effort: str = ""
    ) -> Optional[str]:
        """Authorization-gated entry point used when the bridge provider is active.

        ``ClaudeBridge.handle_message`` is deliberately platform/auth-agnostic
        (no ``GatewayRunner`` reference — see ``gateway/claude_bridge/core.py``)
        so it never checks pairing/allowlist state on its own. This wrapper is
        what makes that safe: it applies the exact same gate ``_handle_message``
        does before handing off, so bridge mode can't be used to reach
        ``claude -p`` as an unpaired sender.

        It also mirrors ``_handle_message``'s pre-auth guards that matter on
        this path: the 🔴 cross-session leak reset (this handler runs in the
        same per-message ``create_task()`` context and spawns the ``claude``
        CLI through the same subprocess-env bridge, so inherited foreign
        ``HERMES_SESSION_*`` ContextVars must be cleared before any spawn) and
        the Slack ignored-channel drop (``ClaudeBridge`` is platform-agnostic
        and would otherwise dispatch channels the operator explicitly
        blacklisted).
        """
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug(
                "reset_session_vars failed at bridge handler entry", exc_info=True
            )

        from gateway.run import _is_slack_ignored_channel

        is_internal = bool(getattr(event, "internal", False))
        source = event.source
        if (
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(
                getattr(self, "config", None), getattr(source, "chat_id", None)
            )
        ):
            logger.info(
                "Dropping Slack message from configured ignored channel %s "
                "(claude_bridge path)",
                getattr(source, "chat_id", None),
            )
            return None

        if not await self._gate_unauthorized_message(event, is_internal=is_internal):
            return None
        model = model.strip() if isinstance(model, str) else ""
        if model.lower() == CLAUDE_BRIDGE_MODEL_ID:
            model = ""
        effort = effort.strip().lower() if isinstance(effort, str) else ""
        if not model and not effort:
            reply = await self.claude_bridge.handle_message(event)
        else:
            reply = await self.claude_bridge.handle_message(
                event, model=model, effort=effort
            )
        if reply is not None:
            self._schedule_claude_bridge_retitle(event)
        return reply

    def _schedule_claude_bridge_retitle(self, event: MessageEvent) -> None:
        """Count this bridge turn and re-title its Discord thread on every Nth.

        Bridge turns never reach the native agent loop, so ``maybe_auto_title``
        (agent/turn_context.py) never runs for them and a Hermes-created thread
        keeps the opening-message excerpt it was born with for life. This lane
        reuses the native titler and the adapter's own rename op to keep that
        name tracking the work. Fire-and-forget: a rename must never sit on the
        reply's path, and a failure to rename is cosmetic.
        """
        try:
            from agent.title_generator import is_titleable_user_message

            if event.get_command():
                return
            source = event.source
            if not self._is_discord_auto_thread_lane(source):
                return
            text = (event.text or "").strip()
            if not is_titleable_user_message(text):
                return

            counts = self._claude_bridge_retitle_counts
            if counts is None:
                counts = self._claude_bridge_retitle_counts = {}
            key = channel_key(event)
            count = counts.get(key, 0) + 1
            counts[key] = count
            if count % self._RETITLE_EVERY != 1:
                return

            task = asyncio.create_task(
                self._retitle_claude_bridge_thread(source, text)
            )
            # Hold a strong reference: create_task keeps only a weak one, so an
            # unreferenced task can be collected mid-run (same pattern as the
            # outbox watcher task).
            tasks = self._claude_bridge_retitle_tasks
            if tasks is None:
                tasks = self._claude_bridge_retitle_tasks = set()
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except Exception:
            logger.debug(
                "claude_bridge: thread re-title scheduling failed", exc_info=True
            )

    async def _retitle_claude_bridge_thread(
        self, source: SessionSource, text: str
    ) -> None:
        """Generate a title for ``text`` and rename ``source``'s Discord thread."""
        try:
            from agent.title_generator import generate_title

            thread_id = str(getattr(source, "thread_id", "") or "")
            if not thread_id:
                return
            # Sync auxiliary LLM call — off the event loop.
            title = await asyncio.to_thread(generate_title, text)
            if not title:
                return
            name = self._sanitize_discord_thread_title(title)
            rename_thread = getattr(
                self._adapter_for_source(source), "rename_thread", None
            )
            if rename_thread is None:
                return
            # No no-clobber guard on purpose: replacing the thread's current
            # name is this lane's whole job.
            await rename_thread(thread_id, name)
            logger.info(
                "claude_bridge: re-titled discord thread %s to %r", thread_id, name
            )
        except Exception:
            logger.debug("claude_bridge: thread re-title failed", exc_info=True)
