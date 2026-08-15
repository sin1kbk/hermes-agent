"""Claude Code CLI bridge — fork-maintenance package.

This fork tracks upstream (``NousResearch/hermes-agent``) and merges it
regularly. Fork-maintenance rule: ALL bridge logic lives in bridge-owned
modules — this package (``gateway.claude_bridge``),
``hermes_cli.claude_bridge_defs``, ``hermes_cli.claude_bridge_switch``,
``plugins.platforms.discord.claude_bridge_ui``, and
``tests/gateway/test_claude_bridge_*.py``. Upstream-owned files carry only
thin hook points into these modules:

- ``gateway/config.py``: re-exports ``ClaudeBridgeConfig`` and friends from
  ``gateway.claude_bridge.config``, plus a 3-line call to
  ``extract_claude_bridge_section`` inside ``load_gateway_config``.
- ``gateway/run.py``: ``ClaudeBridgeRunnerMixin`` in ``GatewayRunner``'s base
  classes; ``self._init_claude_bridge()`` in ``__init__``; one bare call each
  to ``self._start_claude_bridge_outbox_watcher()`` /
  ``self._bind_claude_bridge_notifier()`` at startup and at every
  reconnect/multiplex-connect site; the
  ``self._handle_message -> self._select_message_handler()`` seams; and
  an optional-method-style call to ``self._close_claude_bridge()`` during
  shutdown (``getattr(self, "_close_claude_bridge", None)``, matching the
  file's existing pattern for optional lifecycle hooks — bare test doubles
  in unrelated shutdown tests carry no gateway mixins at all).
- ``gateway/slash_commands.py``: an early check-and-delegate to
  ``gateway.claude_bridge.slash`` at each ``/model`` switch-completion site,
  and a 3-line call to ``claude_bridge_reasoning_rejection`` inside
  ``_apply_reasoning_selection``.
- ``hermes_cli/providers.py`` / ``hermes_cli/model_switch.py``: re-export the
  identifiers and helpers defined in the two ``hermes_cli.claude_bridge_*``
  modules above.
- ``plugins/platforms/discord/adapter.py``: a delegating
  ``post_claude_bridge_decision`` method, and the ``DecisionOutboxView``
  class assignment inside ``_define_discord_view_classes`` — both delegating
  to ``plugins.platforms.discord.claude_bridge_ui``.

New bridge features extend the modules listed above; they must never add
logic inline to an upstream-owned file — only another thin hook point.

``__getattr__`` below (PEP 562) re-exports every public name lazily: an
eager import here would pull in ``core.py`` (which imports
``gateway.platforms.base``) while ``gateway.config`` is still mid-import via
``gateway.claude_bridge.config`` — an import cycle. See ``core.py``'s
docstring for the ``$HERMES_HOME/claude_bridge/`` on-disk layout.
"""

import importlib
from typing import Any

_EXPORTS = {
    "ClaudeBridge": "core",
    "OutboxWatcher": "core",
    "parse_channel_key": "core",
    "write_decision_answer": "core",
    "write_decision_timeout": "core",
    "halt_flag_file": "core",
    "_SessionMap": "core",
    "_ClaudeProcess": "core",
    "MAX_DECISION_ID_LEN": "core",
    "MAX_OPTION_ID_LEN": "core",
    "decisions_dir": "core",
    "outbox_dir": "core",
    "outbox_failed_dir": "core",
    "outbox_processed_dir": "core",
    "ClaudeBridgeConfig": "config",
    "CLAUDE_PERMISSION_MODES": "config",
    "DEFAULT_CLAUDE_PERMISSION_MODE": "config",
    "ClaudeBridgeRunnerMixin": "runner_mixin",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    submodule_name = _EXPORTS.get(name)
    if submodule_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    submodule = importlib.import_module(f"{__name__}.{submodule_name}")
    return getattr(submodule, name)
