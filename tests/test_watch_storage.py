"""Tests for watches storage CRUD."""

from __future__ import annotations


def _make_watch_kwargs(**overrides):
    """Build default kwargs for create_watch."""
    defaults = {
        "watch_id": "watch_001",
        "ws_id": "ws-abc",
        "node_id": "node-1",
        "name": "pr-review",
        "command": "gh pr view --json state",
        "interval_secs": 300.0,
        "stop_on": 'data["state"] == "MERGED"',
        "max_polls": 100,
        "created_by": "model",
        "next_poll": "2099-01-01T00:05:00",
    }
    defaults.update(overrides)
    return defaults


class TestWatchCRUD:
    def test_create_and_get(self, db):
        db.create_watch(**_make_watch_kwargs())
        w = db.get_watch("watch_001")
        assert w is not None
        assert w["name"] == "pr-review"
        assert w["command"] == "gh pr view --json state"
        assert w["interval_secs"] == 300.0
        assert w["active"] == 1
        assert w["poll_count"] == 0

    def test_get_nonexistent(self, db):
        assert db.get_watch("nope") is None

    def test_create_idempotent(self, db):
        db.create_watch(**_make_watch_kwargs())
        db.create_watch(**_make_watch_kwargs())  # OR IGNORE
        assert db.get_watch("watch_001") is not None

    def test_update(self, db):
        db.create_watch(**_make_watch_kwargs())
        updated = db.update_watch(
            "watch_001",
            poll_count=5,
            last_output="hello",
            last_exit_code=0,
        )
        assert updated is True
        w = db.get_watch("watch_001")
        assert w["poll_count"] == 5
        assert w["last_output"] == "hello"
        assert w["last_exit_code"] == 0

    def test_update_nonexistent(self, db):
        assert db.update_watch("nope", poll_count=1) is False

    def test_update_active_flag(self, db):
        db.create_watch(**_make_watch_kwargs())
        db.update_watch("watch_001", active=False)
        w = db.get_watch("watch_001")
        assert w["active"] == 0

    def test_delete(self, db):
        db.create_watch(**_make_watch_kwargs())
        assert db.delete_watch("watch_001") is True
        assert db.get_watch("watch_001") is None

    def test_delete_nonexistent(self, db):
        assert db.delete_watch("nope") is False


class TestWatchListQueries:
    def test_list_for_ws(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1", name="a"))
        db.create_watch(**_make_watch_kwargs(watch_id="w2", ws_id="ws-1", name="b"))
        db.create_watch(**_make_watch_kwargs(watch_id="w3", ws_id="ws-2", name="c"))

        ws1 = db.list_watches_for_ws("ws-1")
        assert len(ws1) == 2
        assert {w["name"] for w in ws1} == {"a", "b"}

    def test_list_for_ws_excludes_inactive(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1"))
        db.update_watch("w1", active=False)
        assert db.list_watches_for_ws("ws-1") == []

    def test_list_for_node(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", node_id="n1"))
        db.create_watch(**_make_watch_kwargs(watch_id="w2", node_id="n1"))
        db.create_watch(**_make_watch_kwargs(watch_id="w3", node_id="n2"))

        n1 = db.list_watches_for_node("n1")
        assert len(n1) == 2

    def test_list_due(self, db):
        # Due
        db.create_watch(**_make_watch_kwargs(watch_id="w1", next_poll="2020-01-01T00:00:00"))
        # Not due (far future)
        db.create_watch(**_make_watch_kwargs(watch_id="w2", next_poll="2099-01-01T00:00:00"))
        # Due but inactive
        db.create_watch(**_make_watch_kwargs(watch_id="w3", next_poll="2020-01-01T00:00:00"))
        db.update_watch("w3", active=False)

        due = db.list_due_watches("2025-01-01T00:00:00")
        assert len(due) == 1
        assert due[0]["watch_id"] == "w1"

    def test_delete_for_ws(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1"))
        db.create_watch(**_make_watch_kwargs(watch_id="w2", ws_id="ws-1"))
        db.create_watch(**_make_watch_kwargs(watch_id="w3", ws_id="ws-2"))

        count = db.delete_watches_for_ws("ws-1")
        assert count == 2
        assert db.get_watch("w1") is None
        assert db.get_watch("w2") is None
        assert db.get_watch("w3") is not None
