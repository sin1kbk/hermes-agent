"""Configuration for the Claude Code CLI bridge provider.

Deliberately does not import ``gateway.config`` — that module imports THIS
one (to re-export ``ClaudeBridgeConfig`` as part of ``GatewayConfig``), so
the reverse import would cycle.  The two coercers below are tiny private
copies of ``gateway.config``'s for that reason.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Hardcoded: these validation warnings logged under ``gateway.config`` before
# the move, and operators filter startup config warnings by that logger name.
logger = logging.getLogger("gateway.config")


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce bool-ish config values, preserving a caller-provided default."""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    from utils import is_truthy_value
    return is_truthy_value(value, default=default)


def _coerce_int(value: Any, default: int) -> int:
    """Coerce integer config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Values accepted by `claude --permission-mode`.  A mode outside this set
# makes the CLI exit at startup, so unknown entries are dropped at config
# load rather than reaching a spawn.
CLAUDE_PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"

# How a message arriving while a channel's turn is still running is handled.
# "steer": written straight into the active turn (bypasses the gateway's
# native busy-session queue/interrupt machinery entirely, since a resident
# claude process has no equivalent of "kill and restart" without losing
# --resume context). "queue": falls through to the gateway's existing
# behavior for this session (merged as a follow-up turn after the current
# one finishes) — the pre-steering default.
CLAUDE_BRIDGE_BUSY_MODES = ("steer", "queue")
DEFAULT_CLAUDE_BRIDGE_BUSY_MODE = "steer"


@dataclass
class ClaudeBridgeConfig:
    """Configuration for exposing the Claude Code CLI bridge provider.

    When ``enabled`` is False (the default), the provider is unavailable and
    the gateway's normal agent loop handles every message.
    """
    enabled: bool = False
    # cwd for the spawned `claude` process. Required when enabled (validated
    # by ClaudeBridge, not here, so config loading never raises on this).
    working_dir: Optional[str] = None
    claude_bin: str = "claude"
    max_concurrency: int = 2
    timeout_seconds: int = 900
    # Reap an unused persistent CLI process after this many seconds.  Its
    # session_id remains in sessions.json and the next message resumes it.
    # 0 or a negative value disables idle reaping.
    idle_timeout_seconds: int = 1800
    # User IDs allowed to send !halt / !unhalt. Empty = anyone may.
    halt_users: List[str] = field(default_factory=list)
    # Extra argv appended to the `claude -p ...` invocation.
    extra_args: List[str] = field(default_factory=list)
    # Models selectable for the bridge.
    models: List[str] = field(default_factory=list)
    # Permission mode a channel runs under until `/mode` overrides it.  Passed
    # explicitly on every spawn rather than inherited from the user's
    # ~/.claude/settings.json, so a gateway restart always lands on a known
    # mode.  Overrides are in-memory, so a restart also drops them.
    default_permission_mode: str = DEFAULT_CLAUDE_PERMISSION_MODE
    # Modes `/mode` may switch a channel to. ``default_permission_mode`` is
    # always reachable regardless of this list.
    allowed_permission_modes: List[str] = field(
        default_factory=lambda: list(CLAUDE_PERMISSION_MODES)
    )
    # Channel IDs the escalation outbox is allowed to post decisions into.
    # Empty = unrestricted (any channel_id in an outbox file is honored).
    # Outbox JSON is written by the running claude session itself, not by an
    # external/untrusted party, but a non-empty allowlist still bounds where
    # a buggy or compromised prompt could direct a decision post.
    decision_channels: List[str] = field(default_factory=list)
    # See CLAUDE_BRIDGE_BUSY_MODES.
    busy_mode: str = DEFAULT_CLAUDE_BRIDGE_BUSY_MODE

    @property
    def resolved_working_dir(self) -> Optional[str]:
        """``working_dir`` with a leading ``~`` expanded, for use as a cwd.

        ``working_dir`` itself keeps the unexpanded string so ``to_dict`` round-
        trips it: config.yaml is shared between machines with different $HOME,
        and writing the expanded path back on a config save would pin the
        workspace to whichever machine saved last.
        """
        if not self.working_dir:
            return None
        return os.path.expanduser(self.working_dir)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "working_dir": self.working_dir,
            "claude_bin": self.claude_bin,
            "max_concurrency": self.max_concurrency,
            "timeout_seconds": self.timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "halt_users": list(self.halt_users),
            "extra_args": list(self.extra_args),
            "models": list(self.models),
            "default_permission_mode": self.default_permission_mode,
            "allowed_permission_modes": list(self.allowed_permission_modes),
            "decision_channels": list(self.decision_channels),
            "busy_mode": self.busy_mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClaudeBridgeConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        halt_users = data.get("halt_users") or []
        if not isinstance(halt_users, list):
            halt_users = []
        extra_args = data.get("extra_args") or []
        if not isinstance(extra_args, list):
            extra_args = []
        models = data.get("models")
        if models is None:
            models = []
        elif not isinstance(models, list):
            logger.warning(
                "claude_bridge.models must be a list, got %r; using no models",
                models,
            )
            models = []
        decision_channels = data.get("decision_channels") or []
        if not isinstance(decision_channels, list):
            decision_channels = []
        busy_mode = str(data.get("busy_mode") or DEFAULT_CLAUDE_BRIDGE_BUSY_MODE)
        if busy_mode not in CLAUDE_BRIDGE_BUSY_MODES:
            logger.warning(
                "claude_bridge.busy_mode=%r is not one of %s; falling back to "
                "'queue' (the pre-steering behavior)",
                busy_mode, CLAUDE_BRIDGE_BUSY_MODES,
            )
            busy_mode = "queue"
        working_dir = data.get("working_dir")
        default_mode = str(
            data.get("default_permission_mode") or DEFAULT_CLAUDE_PERMISSION_MODE
        )
        if default_mode not in CLAUDE_PERMISSION_MODES:
            logger.warning(
                "claude_bridge.default_permission_mode=%r is not a claude "
                "--permission-mode value; falling back to %s",
                default_mode,
                DEFAULT_CLAUDE_PERMISSION_MODE,
            )
            default_mode = DEFAULT_CLAUDE_PERMISSION_MODE
        raw_modes = data.get("allowed_permission_modes")
        if raw_modes is None:
            # Unset means "no opinion", which is the documented full set.
            raw_modes = list(CLAUDE_PERMISSION_MODES)
        elif not isinstance(raw_modes, list):
            # A malformed value is an operator narrowing the set and getting
            # the syntax wrong, so it falls to the most restrictive reading
            # (default mode only) rather than the widest one.
            logger.warning(
                "claude_bridge.allowed_permission_modes must be a list, got %r; "
                "restricting /mode to %s",
                raw_modes,
                default_mode,
            )
            raw_modes = []
        allowed_modes = []
        for mode in raw_modes:
            if str(mode) in CLAUDE_PERMISSION_MODES:
                allowed_modes.append(str(mode))
            else:
                logger.warning(
                    "claude_bridge.allowed_permission_modes: dropping %r, which "
                    "is not a claude --permission-mode value",
                    mode,
                )
        if default_mode not in allowed_modes:
            allowed_modes.append(default_mode)
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            working_dir=str(working_dir) if working_dir else None,
            claude_bin=data.get("claude_bin") or "claude",
            max_concurrency=_coerce_int(data.get("max_concurrency"), 2),
            timeout_seconds=_coerce_int(data.get("timeout_seconds"), 900),
            idle_timeout_seconds=_coerce_int(data.get("idle_timeout_seconds"), 1800),
            halt_users=[str(u) for u in halt_users],
            extra_args=[str(a) for a in extra_args],
            models=[
                str(model).strip()
                for model in models
                if str(model).strip()
                and str(model).strip().lower() != "claude-code"
            ],
            default_permission_mode=default_mode,
            allowed_permission_modes=allowed_modes,
            decision_channels=[str(c) for c in decision_channels],
            busy_mode=busy_mode,
        )


def extract_claude_bridge_section(
    yaml_cfg: Dict[str, Any], gateway_section: Any
) -> Optional[Dict[str, Any]]:
    """Nested-lookup for ``claude_bridge``: top-level key, else ``gateway.claude_bridge``.

    Mirrors the fallback already established for other gateway settings (e.g.
    ``gateway.streaming``): a top-level ``claude_bridge:`` key wins, and the
    nested form (written by ``hermes config set gateway.claude_bridge.*``) is
    consulted only when the top-level key is absent.
    """
    claude_bridge_cfg = yaml_cfg.get("claude_bridge")
    if not isinstance(claude_bridge_cfg, dict):
        claude_bridge_cfg = (
            gateway_section.get("claude_bridge")
            if isinstance(gateway_section, dict)
            else None
        )
    return claude_bridge_cfg if isinstance(claude_bridge_cfg, dict) else None
