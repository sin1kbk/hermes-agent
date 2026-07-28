"""Regression tests for F1 (outbox watcher must follow reconnects) and F2
(the watcher task needs a strong reference + failure logging).

Before this fix, ``_start_claude_bridge_outbox_watcher`` was only called once
from the initial startup sequence. A gateway that enabled ``claude_bridge``
before Discord's first successful connect (e.g. Discord was the platform
that failed and had to retry, or was only configured on a secondary
multiplex profile) never got a watcher — escalation decisions would pile up
in outbox/ forever with nothing consuming them.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.claude_bridge import ClaudeBridge
from gateway.config import ClaudeBridgeConfig, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner


class _StubAdapter(BasePlatformAdapter):
    """Minimal controllable adapter, optionally decision-button-capable."""

    def __init__(self, *, platform=Platform.TELEGRAM, succeed=True, can_post_decisions=False):
        super().__init__(PlatformConfig(enabled=True, token="test"), platform)
        self._succeed = succeed
        self.connect_calls: list[bool] = []
        if can_post_decisions:
            self.post_claude_bridge_decision = AsyncMock()

    async def connect(self, *, is_reconnect: bool = False):
        self.connect_calls.append(is_reconnect)
        return self._succeed

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _bridge_runner(**bridge_overrides):
    runner = object.__new__(GatewayRunner)
    runner.claude_bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, **bridge_overrides))
    runner._claude_bridge_outbox_watcher = None
    runner._claude_bridge_outbox_watcher_task = None
    runner.adapters = {}
    return runner


class TestStartClaudeBridgeOutboxWatcherUnit:
    def test_disabled_bridge_is_a_noop(self):
        runner = _bridge_runner()
        runner.claude_bridge.config.enabled = False

        runner._start_claude_bridge_outbox_watcher(_StubAdapter(can_post_decisions=True))

        assert runner._claude_bridge_outbox_watcher is None
        assert runner._claude_bridge_outbox_watcher_task is None

    def test_adapter_without_poster_capability_is_a_noop(self):
        runner = _bridge_runner(working_dir="/tmp")

        runner._start_claude_bridge_outbox_watcher(_StubAdapter(can_post_decisions=False))

        assert runner._claude_bridge_outbox_watcher is None

    @pytest.mark.asyncio
    async def test_discord_capable_adapter_starts_watcher_with_strong_task_ref(self):
        runner = _bridge_runner(working_dir="/tmp")
        adapter = _StubAdapter(platform=Platform.DISCORD, can_post_decisions=True)

        runner._start_claude_bridge_outbox_watcher(adapter)

        assert runner._claude_bridge_outbox_watcher is not None
        # F2: create_task()'s return value must be retained somewhere, not
        # dropped on the floor where GC could reap it mid-run.
        assert isinstance(runner._claude_bridge_outbox_watcher_task, asyncio.Task)

        runner._claude_bridge_outbox_watcher.stop()
        await asyncio.wait_for(runner._claude_bridge_outbox_watcher_task, timeout=2)

    @pytest.mark.asyncio
    async def test_second_call_does_not_start_a_second_watcher(self):
        runner = _bridge_runner(working_dir="/tmp")
        adapter = _StubAdapter(platform=Platform.DISCORD, can_post_decisions=True)

        runner._start_claude_bridge_outbox_watcher(adapter)
        first_watcher = runner._claude_bridge_outbox_watcher
        first_task = runner._claude_bridge_outbox_watcher_task

        runner._start_claude_bridge_outbox_watcher(adapter)

        assert runner._claude_bridge_outbox_watcher is first_watcher
        assert runner._claude_bridge_outbox_watcher_task is first_task

        first_watcher.stop()
        await asyncio.wait_for(first_task, timeout=2)

    @pytest.mark.asyncio
    async def test_watcher_task_failure_is_logged_not_silent(self, caplog):
        runner = _bridge_runner(working_dir="/tmp")
        adapter = _StubAdapter(platform=Platform.DISCORD, can_post_decisions=True)

        with patch(
            "gateway.claude_bridge.OutboxWatcher.run",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with caplog.at_level("ERROR", logger="gateway.run"):
                runner._start_claude_bridge_outbox_watcher(adapter)
                task = runner._claude_bridge_outbox_watcher_task
                with pytest.raises(RuntimeError):
                    await asyncio.wait_for(task, timeout=2)
                # Let the done-callback (scheduled via call_soon) run.
                await asyncio.sleep(0)

        assert any("outbox watcher died" in rec.message for rec in caplog.records)


def _make_reconnect_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="test")}
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.session_store = MagicMock()
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner.claude_bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir="/tmp"))
    runner._claude_bridge_outbox_watcher = None
    runner._claude_bridge_outbox_watcher_task = None
    return runner


class TestReconnectWatcherStartsOutboxWatcher:
    @pytest.mark.asyncio
    async def test_discord_reconnect_starts_outbox_watcher(self):
        runner = _make_reconnect_runner()
        runner._failed_platforms[Platform.DISCORD] = {
            "config": PlatformConfig(enabled=True, token="test"),
            "attempts": 1,
            "next_retry": time.monotonic() - 1,
        }
        adapter = _StubAdapter(platform=Platform.DISCORD, succeed=True, can_post_decisions=True)
        real_sleep = asyncio.sleep

        with patch.object(runner, "_create_adapter", return_value=adapter):
            with patch("gateway.run.build_channel_directory", create=True):
                call_count = 0

                async def fake_sleep(n):
                    nonlocal call_count
                    call_count += 1
                    if call_count > 1:
                        runner._running = False
                    await real_sleep(0)

                with patch("asyncio.sleep", side_effect=fake_sleep):
                    await runner._platform_reconnect_watcher()

        assert Platform.DISCORD in runner.adapters
        assert runner._claude_bridge_outbox_watcher is not None

        runner._claude_bridge_outbox_watcher.stop()
        await asyncio.wait_for(runner._claude_bridge_outbox_watcher_task, timeout=2)


class TestMultiplexProfileStartsOutboxWatcher:
    @pytest.mark.asyncio
    async def test_secondary_profile_discord_starts_outbox_watcher(self, monkeypatch):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner._profile_adapters = {}
        runner.session_store = MagicMock()
        runner.claude_bridge = ClaudeBridge(ClaudeBridgeConfig(enabled=True, working_dir="/tmp"))
        runner._claude_bridge_outbox_watcher = None
        runner._claude_bridge_outbox_watcher_task = None

        adapter = _StubAdapter(platform=Platform.DISCORD, can_post_decisions=True)
        profile_cfg = GatewayConfig(multiplex_profiles=True)
        profile_cfg.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, token="t")}

        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: profile_cfg)
        monkeypatch.setattr(runner, "_create_adapter", lambda p, c: adapter)

        connected = await runner._start_one_profile_adapters("coder", "/tmp/x", {})

        assert connected == 1
        assert runner._claude_bridge_outbox_watcher is not None

        runner._claude_bridge_outbox_watcher.stop()
        await asyncio.wait_for(runner._claude_bridge_outbox_watcher_task, timeout=2)
