"""Canonical identifiers for the Claude Code CLI bridge provider.

Zero-dependency by design (stdlib only): both ``hermes_cli`` and ``gateway``
modules need these without risking an import cycle, and ``hermes_cli.providers``
re-exports them so existing ``from hermes_cli.providers import CLAUDE_BRIDGE_*``
call sites keep working unchanged.
"""

CLAUDE_BRIDGE_PROVIDER_ID = "claude-bridge"
CLAUDE_BRIDGE_MODEL_ID = "claude-code"
# Claude Code CLI accepts this strict subset of Hermes' broader reasoning
# ladder. Keep the bridge boundary and its UI on the same canonical values.
CLAUDE_BRIDGE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
