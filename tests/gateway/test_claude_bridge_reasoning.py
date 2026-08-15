"""Tests for the claude-bridge /reasoning restrictions (typed and picker)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource


def _make_event(text="/reasoning", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


class _PickerAdapter:
    """Adapter whose *type* exposes ``send_choice_picker`` (the gate the
    handler checks via ``getattr(type(adapter), 'send_choice_picker', None)``)."""

    def __init__(self, success=True):
        self.calls = []
        self._success = success

    async def send_choice_picker(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=self._success, message_id="m1")


def _make_runner(adapter=None):
    """Create a bare GatewayRunner without calling __init__."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    return runner


class TestClaudeBridgeReasoningCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["minimal", "ultra", "none"])
    async def test_claude_bridge_rejects_unsupported_reasoning_efforts(
        self, tmp_path, monkeypatch, effort
    ):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n  provider: claude-bridge\n  default: claude-opus-5\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        runner = _make_runner()
        event = _make_event(f"/reasoning {effort}")
        session_key = runner._session_key_for_source(event.source)

        result = await runner._handle_reasoning_command(event)

        assert "Claude Bridge supports only" in result
        assert session_key not in runner._session_reasoning_overrides


class TestClaudeBridgeReasoningPicker:
    @pytest.mark.asyncio
    async def test_bridge_reasoning_picker_only_offers_claude_cli_efforts(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "config.yaml").write_text(
            "model:\n  provider: claude-bridge\n  default: claude-opus-5\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        adapter = _PickerAdapter()
        runner = _make_runner(adapter)

        assert await runner._handle_reasoning_command(_make_event("/reasoning")) is None

        assert [choice["value"] for choice in adapter.calls[0]["choices"]] == [
            "low", "medium", "high", "xhigh", "max", "reset", "show", "hide",
        ]
