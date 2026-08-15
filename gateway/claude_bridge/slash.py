"""Bridge halves of the gateway ``/model`` and ``/reasoning`` handlers.

``gateway.slash_commands`` calls into this module at the points where a
switch/selection touches the claude-bridge provider, so its own native
``/model`` and ``/reasoning`` flows stay close to upstream's form.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.i18n import t
from hermes_cli.claude_bridge_defs import CLAUDE_BRIDGE_PROVIDER_ID

# Matches gateway.slash_commands's own logger name: this module's persistence
# helpers were moved out of that file, and its failure logs belong under the
# same name for continuity with existing log filtering/dashboards.
logger = logging.getLogger("gateway.run")

CLAUDE_BRIDGE_CONTEXT_NOTICE = (
    "Context is not carried between Claude Bridge and native Hermes sessions."
)


def claude_bridge_picker_context(mixin_self: Any) -> tuple[bool, list]:
    """(claude_bridge_enabled, claude_bridge_models) for the ``/model`` picker."""
    bridge = getattr(mixin_self, "claude_bridge", None)
    enabled = bool(getattr(bridge, "enabled", False))
    models = list(getattr(getattr(bridge, "config", None), "models", []) or [])
    return enabled, models


def crosses_claude_bridge(current_provider: str, target_provider: str) -> bool:
    current = str(current_provider or "").strip().lower()
    target = str(target_provider or "").strip().lower()
    return current != target and CLAUDE_BRIDGE_PROVIDER_ID in {current, target}


def claude_bridge_one_turn_error(
    current_provider: str, target_provider: str
) -> Optional[str]:
    """One-turn (``--once``) switches are unsupported when either side is the bridge.

    The bridge has no native ``finally`` block to restore a one-turn override
    afterwards, so a ``--once`` switch touching it is rejected outright.
    """
    current = str(current_provider or "").strip().lower()
    if current != CLAUDE_BRIDGE_PROVIDER_ID and target_provider != CLAUDE_BRIDGE_PROVIDER_ID:
        return None
    return t(
        "gateway.model.error_prefix",
        error=(
            "One-turn switch (--once) is not supported "
            "for the claude-bridge provider."
        ),
    )


async def finish_claude_bridge_model_switch(
    mixin_self: Any,
    *,
    result: Any,
    event: Any,
    source: Any,
    session_key: str,
    current_model: str,
    current_provider: str,
    current_base_url: str,
    current_api_key: str,
    persist_global: bool,
    config_path: Any,
) -> str:
    """Apply a successful claude-bridge model switch and build its reply.

    Delegated to by both ``/model`` completion sites the instant
    ``result.target_provider`` is the bridge, so it never runs the steps that
    only make sense for a real LLM endpoint: the in-place cached-agent
    ``switch_model`` (a bridge session is the CLI subprocess, not a cached
    ``AIAgent``), the context-length probe, and the cost-selection guard.
    Everything else — DB/session-store persistence, the pending model note,
    the config write, and the reply's "saved" line — mirrors the native flow
    exactly, calling the same shared helpers on ``mixin_self``.

    One-turn switches never reach here: ``claude_bridge_one_turn_error``
    rejects them at both call sites before this runs.
    """
    from hermes_cli.config import (
        clear_model_endpoint_credentials,
        read_user_config_raw,
        save_config,
    )
    from hermes_cli.model_switch import format_model_for_display

    _sess_db = getattr(mixin_self, "_session_db", None)
    if _sess_db is not None:
        try:
            _sess_entry = await mixin_self.async_session_store.get_or_create_session(source)
            if getattr(_sess_entry, "was_auto_reset", False):
                _sess_entry.was_auto_reset = False
            await _sess_db.update_session_model(
                _sess_entry.session_id, result.new_model,
                provider=result.target_provider,
            )
        except Exception as exc:
            logger.debug("Failed to persist model switch to DB: %s", exc)

    if not hasattr(mixin_self, "_pending_model_notes"):
        mixin_self._pending_model_notes = {}
    mixin_self._pending_model_notes[session_key] = (
        f"[Note: model was just switched from {format_model_for_display(current_model)} "
        f"to {format_model_for_display(result.new_model)} "
        f"via {result.provider_label or result.target_provider}. "
        f"Adjust your self-identification accordingly.]"
    )

    mixin_self._session_model_overrides[session_key] = {
        "model": result.new_model,
        "provider": result.target_provider,
        "api_key": result.api_key,
        "base_url": result.base_url,
        "api_mode": result.api_mode,
    }
    if hasattr(mixin_self, "_pending_one_turn_model_restores"):
        mixin_self._pending_one_turn_model_restores.pop(session_key, None)

    try:
        await mixin_self.async_session_store.set_model_override(
            session_key,
            mixin_self._session_model_overrides[session_key],
        )
    except Exception:
        logger.debug("Failed to persist session model override", exc_info=True)

    mixin_self._evict_cached_agent(session_key)

    if persist_global:
        try:
            from hermes_cli.route_identity import should_clear_context_pin_async

            cfg = read_user_config_raw(config_path)
            raw_model = cfg.get("model")
            if isinstance(raw_model, dict):
                model_cfg = raw_model
            elif isinstance(raw_model, str) and raw_model.strip():
                model_cfg = {"default": raw_model.strip()}
                cfg["model"] = model_cfg
            else:
                model_cfg = {}
                cfg["model"] = model_cfg
            try:
                if await should_clear_context_pin_async(
                    model_cfg.get("default") or model_cfg.get("model"),
                    result.new_model,
                    model_cfg.get("base_url"),
                    result.base_url,
                    model_cfg.get("provider"),
                    result.target_provider,
                ):
                    model_cfg.pop("context_length", None)
            except Exception:
                model_cfg.pop("context_length", None)
            model_cfg["default"] = result.new_model
            model_cfg["provider"] = result.target_provider
            # Named providers (claude-bridge included) always resolve
            # base_url/api_mode fresh, so any leftover is cleared
            # unconditionally — mirrors the native flow's "custom" carve-out,
            # which never applies to the bridge.
            _is_custom_target = str(result.target_provider or "").strip().lower() == "custom"
            if result.base_url:
                model_cfg["base_url"] = result.base_url
            elif _is_custom_target:
                model_cfg.pop("base_url", None)
            if _is_custom_target:
                if result.api_mode:
                    model_cfg["api_mode"] = result.api_mode
                else:
                    model_cfg.pop("api_mode", None)
            else:
                clear_model_endpoint_credentials(model_cfg, clear_base_url=True)
            save_config(cfg)
        except Exception as e:
            logger.warning("Failed to persist model switch: %s", e)

    provider_label = result.provider_label or result.target_provider
    lines = [t("gateway.model.switched", model=format_model_for_display(result.new_model))]
    lines.append(t("gateway.model.provider_label", provider=provider_label))

    if result.warning_message:
        lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))

    if crosses_claude_bridge(current_provider, result.target_provider):
        lines.append(CLAUDE_BRIDGE_CONTEXT_NOTICE)

    if persist_global:
        lines.append(t("gateway.model.saved_global"))
    else:
        lines.append(t("gateway.model.session_only_hint"))

    return "\n".join(lines)


def claude_bridge_reasoning_rejection(
    effective_provider: str, parsed: dict, raw_value: str
) -> Optional[str]:
    """Reject a ``/reasoning`` selection the bridge's CLI can't honor.

    The Claude Code CLI's ``--effort`` flag only accepts
    ``CLAUDE_BRIDGE_REASONING_EFFORTS``, a strict subset of Hermes' broader
    reasoning ladder (e.g. it has no "disabled" state).
    """
    from hermes_cli.claude_bridge_defs import CLAUDE_BRIDGE_REASONING_EFFORTS

    if effective_provider == CLAUDE_BRIDGE_PROVIDER_ID and (
        parsed.get("enabled") is not True
        or parsed.get("effort") not in CLAUDE_BRIDGE_REASONING_EFFORTS
    ):
        return t("gateway.reasoning.claude_bridge_unsupported_effort", effort=raw_value)
    return None
