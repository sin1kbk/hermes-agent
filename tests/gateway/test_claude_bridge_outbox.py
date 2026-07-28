"""Tests for gateway.claude_bridge's escalation outbox/decisions file IPC.

No Discord dependency here — `poster` is a plain async callable, matching
how ``OutboxWatcher`` is used platform-agnostically (see claude_bridge.py's
module docstring). Discord's button rendering is exercised separately via
the plugin's own adapter tests.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from gateway.claude_bridge import (
    MAX_DECISION_ID_LEN,
    MAX_OPTION_ID_LEN,
    OutboxWatcher,
    decisions_dir,
    outbox_dir,
    outbox_failed_dir,
    outbox_processed_dir,
    write_decision_answer,
    write_decision_timeout,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def hermes_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _write_outbox_file(name: str, data) -> None:
    outbox_dir().mkdir(parents=True, exist_ok=True)
    (outbox_dir() / name).write_text(json.dumps(data), encoding="utf-8")


def test_valid_decision_posted_and_moved_to_processed(hermes_home):
    decision = {
        "decision_id": "d1",
        "channel_id": "123",
        "question": "pick one",
        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    }
    _write_outbox_file("d1.json", decision)

    posted = []

    async def _poster(data):
        posted.append(data)

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert posted == [decision]
    assert not (outbox_dir() / "d1.json").exists()
    assert (outbox_processed_dir() / "d1.json").exists()


def test_invalid_json_moved_to_failed(hermes_home):
    outbox_dir().mkdir(parents=True, exist_ok=True)
    (outbox_dir() / "bad.json").write_text("{not json", encoding="utf-8")

    async def _poster(data):
        raise AssertionError("poster must not be called for invalid JSON")

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert not (outbox_dir() / "bad.json").exists()
    assert (outbox_failed_dir() / "bad.json").exists()


def test_missing_required_keys_moved_to_failed(hermes_home):
    _write_outbox_file("incomplete.json", {"decision_id": "d1"})

    async def _poster(data):
        raise AssertionError("poster must not be called for schema-invalid input")

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert (outbox_failed_dir() / "incomplete.json").exists()


def test_poster_failure_moves_to_failed_not_processed(hermes_home):
    decision = {"decision_id": "d2", "channel_id": "1", "question": "q", "options": []}
    _write_outbox_file("d2.json", decision)

    async def _poster(data):
        raise RuntimeError("discord unreachable")

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert (outbox_failed_dir() / "d2.json").exists()
    assert not (outbox_processed_dir() / "d2.json").exists()


def test_multiple_files_all_processed_in_one_tick(hermes_home):
    for i in range(3):
        _write_outbox_file(
            f"d{i}.json",
            {"decision_id": f"d{i}", "channel_id": "1", "question": "q", "options": []},
        )

    posted = []

    async def _poster(data):
        posted.append(data["decision_id"])

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert sorted(posted) == ["d0", "d1", "d2"]
    assert len(list(outbox_processed_dir().glob("*.json"))) == 3


def test_write_decision_answer_schema(hermes_home):
    write_decision_answer("d3", "a", "user-42")

    data = json.loads((decisions_dir() / "d3.json").read_text(encoding="utf-8"))
    assert data["decision_id"] == "d3"
    assert data["choice"] == "a"
    assert data["user_id"] == "user-42"
    assert "answered_at" in data


def test_write_decision_timeout_schema(hermes_home):
    write_decision_timeout("d4")

    data = json.loads((decisions_dir() / "d4.json").read_text(encoding="utf-8"))
    assert data == {"choice": None, "timed_out": True}


def test_write_decision_answer_is_atomic_no_tmp_left_behind(hermes_home):
    write_decision_answer("d5", "a", None)
    assert not (decisions_dir() / "d5.json.tmp").exists()
    assert (decisions_dir() / "d5.json").exists()


# --- C3: decision_channels allowlist ---------------------------------------


def test_channel_outside_allowlist_moved_to_failed(hermes_home):
    decision = {"decision_id": "d6", "channel_id": "999", "question": "q", "options": []}
    _write_outbox_file("d6.json", decision)

    async def _poster(data):
        raise AssertionError("poster must not be called for a non-allowlisted channel")

    watcher = OutboxWatcher(_poster, interval=999, allowed_channels=["123", "456"])
    asyncio.run(watcher._tick())

    assert (outbox_failed_dir() / "d6.json").exists()


def test_channel_inside_allowlist_is_posted(hermes_home):
    decision = {"decision_id": "d7", "channel_id": "123", "question": "q", "options": []}
    _write_outbox_file("d7.json", decision)

    posted = []

    async def _poster(data):
        posted.append(data["decision_id"])

    watcher = OutboxWatcher(_poster, interval=999, allowed_channels=["123", "456"])
    asyncio.run(watcher._tick())

    assert posted == ["d7"]
    assert (outbox_processed_dir() / "d7.json").exists()


def test_empty_allowlist_means_unrestricted(hermes_home):
    decision = {"decision_id": "d8", "channel_id": "anything", "question": "q", "options": []}
    _write_outbox_file("d8.json", decision)

    posted = []

    async def _poster(data):
        posted.append(data["decision_id"])

    watcher = OutboxWatcher(_poster, interval=999, allowed_channels=[])
    asyncio.run(watcher._tick())

    assert posted == ["d8"]


# --- F8: decision_id / option id length bound (Discord custom_id budget) ---


def test_oversized_decision_id_moved_to_failed(hermes_home):
    decision = {
        "decision_id": "x" * (MAX_DECISION_ID_LEN + 1),
        "channel_id": "1", "question": "q", "options": [],
    }
    _write_outbox_file("long_id.json", decision)

    async def _poster(data):
        raise AssertionError("poster must not be called for an oversized decision_id")

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert (outbox_failed_dir() / "long_id.json").exists()


def test_oversized_option_id_moved_to_failed(hermes_home):
    decision = {
        "decision_id": "d9", "channel_id": "1", "question": "q",
        "options": [{"id": "y" * (MAX_OPTION_ID_LEN + 1), "label": "L"}],
    }
    _write_outbox_file("long_opt.json", decision)

    async def _poster(data):
        raise AssertionError("poster must not be called for an oversized option id")

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert (outbox_failed_dir() / "long_opt.json").exists()


def test_ids_at_the_limit_are_accepted(hermes_home):
    decision = {
        "decision_id": "x" * MAX_DECISION_ID_LEN,
        "channel_id": "1", "question": "q",
        "options": [{"id": "y" * MAX_OPTION_ID_LEN, "label": "L"}],
    }
    _write_outbox_file("at_limit.json", decision)

    posted = []

    async def _poster(data):
        posted.append(data)

    watcher = OutboxWatcher(_poster, interval=999)
    asyncio.run(watcher._tick())

    assert posted == [decision]
    assert (outbox_processed_dir() / "at_limit.json").exists()


# --- F6: a move-to-processed failure after a successful post must not loop -


def test_move_failure_after_successful_post_renames_in_place(hermes_home):
    """If the decision was already posted but the processed/ move fails, the
    file must be renamed out of the *.json glob so it isn't reposted forever."""
    decision = {"decision_id": "d10", "channel_id": "1", "question": "q", "options": []}
    _write_outbox_file("d10.json", decision)

    posted = []

    async def _poster(data):
        posted.append(data["decision_id"])

    watcher = OutboxWatcher(_poster, interval=999)

    with patch.object(OutboxWatcher, "_move", staticmethod(lambda path, dest: False)):
        asyncio.run(watcher._tick())

    assert posted == ["d10"]  # it WAS posted
    assert not (outbox_dir() / "d10.json").exists()  # excluded from next glob
    assert (outbox_dir() / "d10.json.posted").exists()

    # A second tick must not re-post it.
    asyncio.run(watcher._tick())
    assert posted == ["d10"]
