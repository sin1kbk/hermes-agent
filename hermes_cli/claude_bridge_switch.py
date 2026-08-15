"""Claude-bridge validation and result-construction for ``switch_model``.

Split out of ``hermes_cli.model_switch`` so the bridge's virtual-provider
branches don't clutter that module's PATH A / PATH B / common-path pipeline.
Imports ``ModelSwitchResult`` lazily inside the functions below because
``hermes_cli.model_switch`` imports this module at its own top level, and a
top-level back-import here would cycle.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from hermes_cli.claude_bridge_defs import CLAUDE_BRIDGE_MODEL_ID, CLAUDE_BRIDGE_PROVIDER_ID


def configured_claude_bridge_models(models: Optional[List[str]]) -> List[str]:
    """Return configured bridge models that are valid user-selectable choices."""
    return [
        candidate
        for configured_model in models or []
        if (candidate := str(configured_model or "").strip())
        and candidate.lower() != CLAUDE_BRIDGE_MODEL_ID
    ]


def validate_claude_bridge_selection(
    new_model: str,
    *,
    allow_claude_bridge: bool,
    claude_bridge_models: Optional[List[str]],
    claude_bridge_default_model: str,
    provider_label: str,
    is_global: bool,
) -> Tuple[Optional[Any], str]:
    """Validate a claude-bridge target and resolve its default model.

    Returns ``(error_result, new_model)``: ``error_result`` is a failed
    ``ModelSwitchResult`` when the bridge is disabled or has no configured
    models (``new_model`` is meaningless in that case), else ``None`` with
    ``new_model`` defaulted when the caller passed none.
    """
    from hermes_cli.model_switch import ModelSwitchResult

    if not allow_claude_bridge:
        return (
            ModelSwitchResult(
                success=False, target_provider=CLAUDE_BRIDGE_PROVIDER_ID,
                provider_label=provider_label, is_global=is_global,
                error_message=(
                    "Provider 'claude-bridge' is unavailable because "
                    "claude_bridge.enabled is false."
                ),
            ),
            new_model,
        )
    allowed_models = configured_claude_bridge_models(claude_bridge_models)
    if not allowed_models:
        return (
            ModelSwitchResult(
                success=False, target_provider=CLAUDE_BRIDGE_PROVIDER_ID,
                provider_label=provider_label, is_global=is_global,
                error_message=(
                    "Provider 'claude-bridge' has no configured models. "
                    "Set claude_bridge.models in config.yaml."
                ),
            ),
            new_model,
        )
    if not new_model:
        default_model = str(claude_bridge_default_model or "").strip()
        new_model = default_model if default_model in allowed_models else allowed_models[0]
    return None, new_model


def claude_bridge_switch_result(
    new_model: str,
    *,
    allow_claude_bridge: bool,
    claude_bridge_models: Optional[List[str]],
    claude_bridge_default_model: str,
    provider_label: str,
    is_global: bool,
    provider_changed: bool = False,
) -> Optional[Any]:
    """Validate, default, and build the final result for a claude-bridge switch.

    Used from ``switch_model``'s common path, where ``target_provider`` is
    already known to be the bridge — the PATH A branch instead calls
    ``validate_claude_bridge_selection`` directly (validation + default only,
    no membership check, no success result: PATH A falls through to the
    common path for that).
    """
    from hermes_cli.model_switch import ModelSwitchResult

    error, new_model = validate_claude_bridge_selection(
        new_model, allow_claude_bridge=allow_claude_bridge,
        claude_bridge_models=claude_bridge_models,
        claude_bridge_default_model=claude_bridge_default_model,
        provider_label=provider_label, is_global=is_global,
    )
    if error is not None:
        return error
    allowed_models = configured_claude_bridge_models(claude_bridge_models)
    if new_model not in allowed_models:
        return ModelSwitchResult(
            success=False,
            target_provider=CLAUDE_BRIDGE_PROVIDER_ID,
            provider_label=provider_label,
            is_global=is_global,
            error_message=(
                f"Model '{new_model}' is not configured for provider "
                f"'claude-bridge'. Available models: {', '.join(allowed_models)}."
            ),
        )
    return ModelSwitchResult(
        success=True,
        new_model=new_model,
        target_provider=CLAUDE_BRIDGE_PROVIDER_ID,
        provider_changed=provider_changed,
        provider_label=provider_label,
        is_global=is_global,
    )


def claude_bridge_provider_row(
    claude_bridge_models: Optional[List[str]],
    current_provider_norm: str,
    get_label: Callable[[str], str],
) -> Optional[dict]:
    """Picker row for the virtual claude-bridge provider, or None if unconfigured."""
    bridge_models = configured_claude_bridge_models(claude_bridge_models)
    if not bridge_models:
        return None
    return {
        "slug": CLAUDE_BRIDGE_PROVIDER_ID,
        "name": get_label(CLAUDE_BRIDGE_PROVIDER_ID),
        "is_current": current_provider_norm == CLAUDE_BRIDGE_PROVIDER_ID,
        "is_user_defined": False,
        "models": bridge_models,
        "total_models": len(bridge_models),
        "source": "virtual",
    }
