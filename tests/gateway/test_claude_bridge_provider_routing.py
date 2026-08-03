"""Provider-based routing contracts for the Claude CLI bridge."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.claude_bridge import ClaudeBridge
from gateway.config import (
    ChannelOverride,
    ClaudeBridgeConfig,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.model_switch import ModelSwitchResult
from hermes_cli.providers import (
    CLAUDE_BRIDGE_MODEL_ID,
    CLAUDE_BRIDGE_PROVIDER_ID,
)


def _event(text: str = "hello", chat_id: str = "c1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            chat_type="group",
            user_id="u1",
        ),
    )


def _write_config(
    tmp_path, monkeypatch, provider: str, *, default_model: str = "configured-model"
) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"default": default_model, "provider": provider},
                "claude_bridge": {"enabled": True, "working_dir": str(tmp_path)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)


def _runner(
    *, enabled: bool = True, channel_provider: str = "", channel_model: str = ""
) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    channel_overrides = (
        {"c1": ChannelOverride(provider=channel_provider, model=channel_model or None)}
        if channel_provider or channel_model
        else {}
    )
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="test-token",
                channel_overrides=channel_overrides,
            )
        },
        claude_bridge=ClaudeBridgeConfig(enabled=enabled, working_dir="/tmp"),
    )
    runner.claude_bridge = ClaudeBridge(runner.config.claude_bridge)
    runner.session_store = None
    runner._session_model_overrides = {}
    runner._handle_message = AsyncMock(return_value="native")
    runner._claude_bridge_handler = AsyncMock(return_value="bridge")
    return runner


@pytest.mark.asyncio
async def test_bridge_session_model_override_is_forwarded_to_the_handler(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch, CLAUDE_BRIDGE_PROVIDER_ID)
    runner = _runner()
    session_key = runner._session_key_for_source(_event().source)
    runner._session_model_overrides[session_key] = {
        "model": "claude-opus-5",
        "provider": CLAUDE_BRIDGE_PROVIDER_ID,
    }

    event = _event()
    assert await runner._select_message_handler()(event) == "bridge"
    runner._claude_bridge_handler.assert_awaited_once_with(event, model="claude-opus-5")


@pytest.mark.asyncio
async def test_bridge_channel_model_override_is_forwarded_to_the_handler(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch, "ollama-launch")
    runner = _runner(
        channel_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        channel_model="claude-opus-5",
    )

    event = _event()
    assert await runner._select_message_handler()(event) == "bridge"
    runner._claude_bridge_handler.assert_awaited_once_with(event, model="claude-opus-5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_provider", "expected"),
    [
        (CLAUDE_BRIDGE_PROVIDER_ID, "bridge"),
        ("ollama-launch", "native"),
    ],
)
async def test_enabled_bridge_routes_each_message_by_global_provider(
    tmp_path, monkeypatch, configured_provider, expected
):
    _write_config(tmp_path, monkeypatch, configured_provider)
    runner = _runner()

    response = await runner._select_message_handler()(_event())

    assert response == expected
    if expected == "bridge":
        runner._claude_bridge_handler.assert_awaited_once()
        runner._handle_message.assert_not_awaited()
    else:
        runner._handle_message.assert_awaited_once()
        runner._claude_bridge_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_global_default_model_is_forwarded_to_the_handler(
    tmp_path, monkeypatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        CLAUDE_BRIDGE_PROVIDER_ID,
        default_model="claude-opus-5",
    )
    runner = _runner()
    event = _event()

    assert await runner._select_message_handler()(event) == "bridge"
    runner._claude_bridge_handler.assert_awaited_once_with(event, model="claude-opus-5")


@pytest.mark.asyncio
async def test_session_override_wins_over_channel_and_global_provider(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch, CLAUDE_BRIDGE_PROVIDER_ID)
    runner = _runner(channel_provider=CLAUDE_BRIDGE_PROVIDER_ID)
    session_key = runner._session_key_for_source(_event().source)
    runner._session_model_overrides[session_key] = {
        "model": "qwen3.6:35b-a3b-coding-nvfp4",
        "provider": "ollama-launch",
    }

    assert await runner._select_message_handler()(_event()) == "native"
    runner._handle_message.assert_awaited_once()
    runner._claude_bridge_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_override_wins_over_global_provider(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "ollama-launch")
    runner = _runner(channel_provider=CLAUDE_BRIDGE_PROVIDER_ID)

    assert await runner._select_message_handler()(_event()) == "bridge"
    runner._claude_bridge_handler.assert_awaited_once()
    runner._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_quick_command_alias_routes_bridge_session_to_native(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch, CLAUDE_BRIDGE_PROVIDER_ID)
    runner = _runner()
    runner.config.quick_commands = {
        "switch": {"type": "alias", "target": "/model"}
    }
    event = _event("/switch qwen3.6:35b-a3b-coding-nvfp4")

    assert await runner._select_message_handler()(event) == "native"
    assert event.text == "/switch qwen3.6:35b-a3b-coding-nvfp4"
    runner._handle_message.assert_awaited_once_with(event)
    runner._claude_bridge_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_bridge_routes_every_message_native(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, CLAUDE_BRIDGE_PROVIDER_ID)
    runner = _runner(enabled=False, channel_provider=CLAUDE_BRIDGE_PROVIDER_ID)
    session_key = runner._session_key_for_source(_event().source)
    runner._session_model_overrides[session_key] = {
        "model": CLAUDE_BRIDGE_MODEL_ID,
        "provider": CLAUDE_BRIDGE_PROVIDER_ID,
    }

    assert await runner._select_message_handler()(_event()) == "native"
    runner._handle_message.assert_awaited_once()
    runner._claude_bridge_handler.assert_not_awaited()


class _NoopAsyncSessionStore:
    _store = None

    async def set_model_override(self, _session_key, _override):
        return None


class _PersistedBridgeOverrideStore:
    def get_model_override(self, _session_key):
        return {
            "model": CLAUDE_BRIDGE_MODEL_ID,
            "provider": CLAUDE_BRIDGE_PROVIDER_ID,
            "base_url": None,
        }


def _switchable_runner(tmp_path, monkeypatch) -> GatewayRunner:
    _write_config(tmp_path, monkeypatch, "ollama-launch")
    runner = _runner()
    runner.adapters = {}
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._pending_one_turn_model_restores = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._async_session_store = _NoopAsyncSessionStore()
    runner._evict_cached_agent = lambda _session_key: None

    async def _native_handler(event):
        if event.get_command() == "model":
            return await GatewayRunner._handle_model_command(runner, event)
        return "native"

    runner._handle_message = _native_handler
    runner._claude_bridge_handler = AsyncMock(return_value="bridge")

    def _switch_model(**kwargs):
        target = kwargs["explicit_provider"]
        if target == CLAUDE_BRIDGE_PROVIDER_ID:
            return ModelSwitchResult(
                success=True,
                new_model=CLAUDE_BRIDGE_MODEL_ID,
                target_provider=CLAUDE_BRIDGE_PROVIDER_ID,
                provider_changed=True,
                provider_label="Claude Bridge",
            )
        return ModelSwitchResult(
            success=True,
            new_model="qwen3.6:35b-a3b-coding-nvfp4",
            target_provider="ollama-launch",
            provider_changed=True,
            api_key="no-key-required",
            base_url="http://127.0.0.1:11434/v1",
            api_mode="chat_completions",
            provider_label="ollama-launch",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _switch_model)
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning",
        lambda *args, **kwargs: None,
    )
    return runner


@pytest.mark.asyncio
async def test_model_command_switches_bridge_to_native_and_back(
    tmp_path, monkeypatch
):
    runner = _switchable_runner(tmp_path, monkeypatch)
    session_key = runner._session_key_for_source(_event().source)
    runner.session_store = _PersistedBridgeOverrideStore()
    router = runner._select_message_handler()

    native_switch = await router(
        _event(
            "/model qwen3.6:35b-a3b-coding-nvfp4 "
            "--provider ollama-launch --session"
        )
    )
    assert session_key in runner._session_model_overrides
    assert runner._session_model_overrides[session_key]["provider"] == "ollama-launch"
    assert "Context is not carried" in native_switch
    assert await router(_event("after native switch")) == "native"

    bridge_switch = await router(
        _event(f"/model --provider {CLAUDE_BRIDGE_PROVIDER_ID} --session")
    )
    assert (
        runner._session_model_overrides[session_key]["provider"]
        == CLAUDE_BRIDGE_PROVIDER_ID
    )
    assert "Context is not carried" in bridge_switch
    assert await router(_event("after bridge switch")) == "bridge"


@pytest.mark.asyncio
async def test_model_once_rejects_claude_bridge_without_changing_session_state(
    tmp_path, monkeypatch
):
    runner = _switchable_runner(tmp_path, monkeypatch)
    session_key = runner._session_key_for_source(_event().source)
    runner._session_model_overrides[session_key] = {
        "model": "existing-model",
        "provider": "ollama-launch",
    }
    runner._pending_one_turn_model_restores[session_key] = {
        "had_override": False,
        "override": None,
    }
    overrides_before = deepcopy(runner._session_model_overrides)
    restores_before = deepcopy(runner._pending_one_turn_model_restores)

    response = await runner._select_message_handler()(
        _event(f"/model --once --provider {CLAUDE_BRIDGE_PROVIDER_ID}")
    )

    assert "One-turn switch (--once) is not supported" in response
    assert runner._session_model_overrides == overrides_before
    assert runner._pending_one_turn_model_restores == restores_before


def test_bridge_provider_listing_is_gated_by_enabled_flag(monkeypatch):
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    common = {
        "current_provider": "ollama-launch",
        "user_providers": {},
        "custom_providers": [],
        "max_models": 0,
    }

    enabled_rows = list_authenticated_providers(
        **common,
        include_claude_bridge=True,
    )
    disabled_rows = list_authenticated_providers(
        **common,
        include_claude_bridge=False,
    )

    bridge_rows = [
        row for row in enabled_rows if row["slug"] == CLAUDE_BRIDGE_PROVIDER_ID
    ]
    assert bridge_rows == [
        {
            "slug": CLAUDE_BRIDGE_PROVIDER_ID,
            "name": "Claude Bridge",
            "is_current": False,
            "is_user_defined": False,
            "models": [CLAUDE_BRIDGE_MODEL_ID],
            "total_models": 1,
            "source": "virtual",
        }
    ]
    assert all(
        row["slug"] != CLAUDE_BRIDGE_PROVIDER_ID for row in disabled_rows
    )


def test_bridge_provider_listing_uses_configured_models(monkeypatch):
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    rows = list_authenticated_providers(
        current_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        current_model="claude-opus-5",
        user_providers={},
        custom_providers=[],
        max_models=0,
        include_claude_bridge=True,
        claude_bridge_models=["claude-opus-5"],
    )

    bridge_row = next(row for row in rows if row["slug"] == CLAUDE_BRIDGE_PROVIDER_ID)
    assert bridge_row["is_current"] is True
    assert bridge_row["models"] == [CLAUDE_BRIDGE_MODEL_ID, "claude-opus-5"]
    assert bridge_row["total_models"] == 2


def test_bridge_provider_switch_requires_enabled_flag(monkeypatch):
    from hermes_cli.model_switch import switch_model

    monkeypatch.setattr("agent.models_dev.get_provider_info", lambda _provider: None)

    denied = switch_model(
        raw_input="",
        current_provider="ollama-launch",
        current_model="qwen",
        explicit_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        allow_claude_bridge=False,
    )
    allowed = switch_model(
        raw_input="",
        current_provider="ollama-launch",
        current_model="qwen",
        explicit_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        allow_claude_bridge=True,
    )

    assert denied.success is False
    assert "enabled is false" in denied.error_message
    assert allowed.success is True
    assert allowed.target_provider == CLAUDE_BRIDGE_PROVIDER_ID
    assert allowed.new_model == CLAUDE_BRIDGE_MODEL_ID
    assert allowed.base_url == ""
    assert allowed.api_key == ""


def test_bridge_provider_switch_rejects_models_not_in_config(monkeypatch):
    from hermes_cli.model_switch import switch_model

    monkeypatch.setattr("agent.models_dev.get_provider_info", lambda _provider: None)
    allowed = switch_model(
        raw_input="claude-bridge/claude-opus-5",
        current_provider="ollama-launch",
        current_model="qwen",
        allow_claude_bridge=True,
        claude_bridge_models=["claude-opus-5"],
    )
    denied = switch_model(
        raw_input="gpt-5",
        current_provider="ollama-launch",
        current_model="qwen",
        explicit_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        allow_claude_bridge=True,
        claude_bridge_models=["claude-opus-5"],
    )

    assert allowed.success is True
    assert allowed.new_model == "claude-opus-5"
    assert denied.success is False
    assert "not configured" in denied.error_message


def test_bridge_endpoint_resolution_fails_closed(tmp_path, monkeypatch):
    from agent.auxiliary_client import resolve_provider_client
    from hermes_cli.runtime_provider import resolve_runtime_provider

    with pytest.raises(ValueError, match="has no API endpoint"):
        resolve_runtime_provider(requested=CLAUDE_BRIDGE_PROVIDER_ID)

    client, model = resolve_provider_client(
        CLAUDE_BRIDGE_PROVIDER_ID,
        model=CLAUDE_BRIDGE_MODEL_ID,
        explicit_base_url="http://127.0.0.1:11434/v1",
        explicit_api_key="should-not-be-used",
    )
    assert client is None
    assert model is None

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "fallback_providers": [
                    {
                        "provider": CLAUDE_BRIDGE_PROVIDER_ID,
                        "model": CLAUDE_BRIDGE_MODEL_ID,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    assert gateway_run._try_resolve_fallback_provider() is None
