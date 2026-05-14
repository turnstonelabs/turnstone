"""Tests for watches storage CRUD."""

from __future__ import annotations

import sqlalchemy as sa

from turnstone.core.storage._schema import watches as watches_table


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


class TestIsWatchActive:
    def test_active_row_returns_true(self, db):
        db.create_watch(**_make_watch_kwargs())
        assert db.is_watch_active("watch_001") is True

    def test_inactive_row_returns_false(self, db):
        db.create_watch(**_make_watch_kwargs())
        db.update_watch("watch_001", active=False)
        assert db.is_watch_active("watch_001") is False

    def test_missing_row_returns_false(self, db):
        assert db.is_watch_active("nope") is False


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

    def test_find_by_name_returns_inactive(self, db):
        """``find_watch_by_name`` ignores the active filter — that is
        what lets the cancel-by-name UX distinguish 'already completed'
        from 'no such watch.'
        """
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1", name="completed"))
        db.update_watch("w1", active=False)

        row = db.find_watch_by_name("ws-1", "completed")
        assert row is not None
        assert row["watch_id"] == "w1"
        assert not row["active"]

    def test_find_by_name_matches_watch_id_prefix(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="abcdef123", ws_id="ws-1", name="x"))
        row = db.find_watch_by_name("ws-1", "abc")
        assert row is not None
        assert row["watch_id"] == "abcdef123"

    def test_find_by_name_scoped_to_ws(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1", name="shared"))
        db.create_watch(**_make_watch_kwargs(watch_id="w2", ws_id="ws-2", name="shared"))

        row = db.find_watch_by_name("ws-1", "shared")
        assert row is not None
        assert row["watch_id"] == "w1"

    def test_find_by_name_returns_none_when_missing(self, db):
        assert db.find_watch_by_name("ws-1", "ghost") is None

    def test_find_by_name_empty_input_returns_none(self, db):
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1", name="x"))
        assert db.find_watch_by_name("ws-1", "") is None

    def test_find_by_name_treats_percent_as_literal(self, db):
        """A model-supplied '%' must NOT match arbitrary watch_ids.

        Pre-escape, ``watch_id.like(f"{name_or_prefix}%")`` would
        interpret '%' as 'match anything' and pick up the first row in
        the workstream regardless of name.
        """
        db.create_watch(**_make_watch_kwargs(watch_id="w1", ws_id="ws-1", name="real-watch"))
        assert db.find_watch_by_name("ws-1", "%") is None

    def test_find_by_name_treats_underscore_as_literal(self, db):
        """Same as the '%' case for the single-char LIKE wildcard."""
        db.create_watch(**_make_watch_kwargs(watch_id="abcd", ws_id="ws-1", name="real-watch"))
        # '_' would otherwise match any single char, picking up
        # watch_ids beginning with 'a', 'b', etc.
        assert db.find_watch_by_name("ws-1", "_") is None

    def test_find_by_name_prefers_active_over_newer_inactive(self, db):
        """If a same-name pair exists where the inactive row is NEWER
        than the active row, find_watch_by_name must still return the
        active row.  Pre-fix the query was ``ORDER BY created DESC
        LIMIT 1`` — which would return the newer inactive row and
        cause the cancel UX to report 'already completed' for a name
        whose live row is still polling.

        Reachable in practice because storage allows out-of-band
        writes (e.g. ``delete_watches_for_ws`` cleanup followed by
        re-create, an admin manually flipping ``active``, or test
        scaffolding) that bypass the create-time duplicate-name
        guard.
        """
        # Older active watch.
        db.create_watch(**_make_watch_kwargs(watch_id="w-active", ws_id="ws-1", name="recurring"))
        # Newer inactive watch with the same name.  ``create_watch``
        # stamps ``created`` to ``now`` at second resolution, so we
        # bypass the API to give the inactive row a deterministically
        # later timestamp.
        db.create_watch(**_make_watch_kwargs(watch_id="w-inactive", ws_id="ws-1", name="recurring"))
        with db._conn() as conn:
            conn.execute(
                sa.update(watches_table)
                .where(watches_table.c.watch_id == "w-inactive")
                .values(active=0, next_poll="", created="2099-01-01T00:00:00")
            )
            conn.commit()

        row = db.find_watch_by_name("ws-1", "recurring")
        assert row is not None
        assert row["watch_id"] == "w-active"
        assert row["active"]

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
