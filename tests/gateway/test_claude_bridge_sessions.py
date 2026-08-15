"""Tests for gateway.claude_bridge._SessionMap persistence."""

import json
from unittest.mock import patch

from gateway.claude_bridge import _SessionMap


def test_session_map_roundtrip(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)

    assert store.get("discord:c1") is None

    store.set("discord:c1", "sess-abc")
    assert store.get("discord:c1") == "sess-abc"
    assert json.loads(path.read_text(encoding="utf-8"))["discord:c1"] == "sess-abc"

    # A fresh instance re-reads the persisted map from disk.
    reloaded = _SessionMap(path)
    assert reloaded.get("discord:c1") == "sess-abc"


def test_session_map_clear_removes_key(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)
    store.set("discord:c1", "sess-abc")

    store.clear("discord:c1")

    assert store.get("discord:c1") is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_session_map_clear_of_missing_key_is_a_noop(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)
    store.clear("never-set")  # must not raise or create the file
    assert not path.exists()


def test_session_map_write_is_atomic_no_tmp_left_behind(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)
    store.set("k", "v")
    assert not (tmp_path / "sessions.json.tmp").exists()
    assert path.exists()


def test_session_map_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")

    store = _SessionMap(path)
    assert store.get("anything") is None

    # A write after a corrupt read still succeeds and overwrites the file.
    store.set("k", "v")
    assert json.loads(path.read_text(encoding="utf-8"))["k"] == "v"


def test_session_map_multiple_keys_independent(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)
    store.set("discord:c1", "sess-1")
    store.set("discord:c2", "sess-2")

    assert store.get("discord:c1") == "sess-1"
    assert store.get("discord:c2") == "sess-2"

    store.clear("discord:c1")
    assert store.get("discord:c1") is None
    assert store.get("discord:c2") == "sess-2"


def test_session_map_set_survives_persistence_failure(tmp_path):
    """F4: a spawn already succeeded by the time set() is called — a disk
    write failure here must be swallowed (logged, not raised) so the
    already-generated claude reply isn't discarded on top of it."""
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)

    with patch("gateway.claude_bridge.core._atomic_write_json", side_effect=OSError("disk full")):
        store.set("discord:c1", "sess-1")  # must not raise

    # In-memory state still reflects the write even though persistence failed.
    assert store.get("discord:c1") == "sess-1"


def test_session_map_clear_survives_persistence_failure(tmp_path):
    path = tmp_path / "sessions.json"
    store = _SessionMap(path)
    store.set("discord:c1", "sess-1")

    with patch("gateway.claude_bridge.core._atomic_write_json", side_effect=OSError("disk full")):
        store.clear("discord:c1")  # must not raise

    assert store.get("discord:c1") is None
