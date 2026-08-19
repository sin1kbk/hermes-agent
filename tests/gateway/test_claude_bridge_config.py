"""Tests for gateway.claude_bridge.config.ClaudeBridgeConfig."""

import os

from gateway.claude_bridge.config import ClaudeBridgeConfig


class TestClaudeBridgeConfig:
    def test_defaults_are_disabled_and_unrestricted(self):
        restored = ClaudeBridgeConfig.from_dict({})
        assert restored.enabled is False
        assert restored.working_dir is None
        assert restored.decision_channels == []
        assert restored.halt_users == []
        assert restored.models == []

    def test_roundtrips_decision_channels(self):
        cfg = ClaudeBridgeConfig(
            enabled=True, working_dir="/repo", decision_channels=["123", "456"],
            idle_timeout_seconds=321, models=["claude-opus-5"],
        )
        restored = ClaudeBridgeConfig.from_dict(cfg.to_dict())
        assert restored.decision_channels == ["123", "456"]
        assert restored.idle_timeout_seconds == 321
        assert restored.models == ["claude-opus-5"]

    def test_legacy_claude_code_model_is_not_selectable(self):
        restored = ClaudeBridgeConfig.from_dict(
            {"models": ["claude-code", "claude-opus-5"]}
        )
        assert restored.models == ["claude-opus-5"]

    def test_non_list_decision_channels_falls_back_to_empty(self):
        restored = ClaudeBridgeConfig.from_dict({"decision_channels": "not-a-list"})
        assert restored.decision_channels == []

    def test_from_dict_malformed_section_falls_back_to_defaults(self):
        restored = ClaudeBridgeConfig.from_dict("oops")
        assert restored.enabled is False
        assert restored.decision_channels == []

    def test_resolved_working_dir_expands_tilde_without_rewriting_config(self):
        restored = ClaudeBridgeConfig.from_dict(
            {"enabled": True, "working_dir": "~/.hermes/claude-workspace"}
        )
        assert restored.resolved_working_dir == os.path.expanduser(
            "~/.hermes/claude-workspace"
        )
        # The tilde survives a save so a shared config.yaml stays portable.
        assert restored.to_dict()["working_dir"] == "~/.hermes/claude-workspace"

    def test_resolved_working_dir_is_none_when_unset(self):
        assert ClaudeBridgeConfig.from_dict({}).resolved_working_dir is None

    def test_busy_mode_defaults_to_steer(self):
        assert ClaudeBridgeConfig.from_dict({}).busy_mode == "steer"

    def test_busy_mode_queue_is_preserved(self):
        restored = ClaudeBridgeConfig.from_dict({"busy_mode": "queue"})
        assert restored.busy_mode == "queue"
        assert restored.to_dict()["busy_mode"] == "queue"

    def test_invalid_busy_mode_falls_back_to_queue(self):
        restored = ClaudeBridgeConfig.from_dict({"busy_mode": "interrupt"})
        assert restored.busy_mode == "queue"
