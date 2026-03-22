"""Tests for channel_users and channel_routes storage CRUD."""

from __future__ import annotations


class TestChannelUserCRUD:
    """Tests for channel_users table operations."""

    def test_create_and_get(self, db):
        db.create_channel_user("discord", "12345", "u_abc")
        result = db.get_channel_user("discord", "12345")
        assert result is not None
        assert result["channel_type"] == "discord"
        assert result["channel_user_id"] == "12345"
        assert result["user_id"] == "u_abc"
        assert "created" in result

    def test_get_nonexistent(self, db):
        assert db.get_channel_user("discord", "99999") is None

    def test_create_duplicate_noop(self, db):
        db.create_channel_user("discord", "12345", "u_abc")
        db.create_channel_user("discord", "12345", "u_different")
        result = db.get_channel_user("discord", "12345")
        assert result is not None
        assert result["user_id"] == "u_abc"  # first write wins

    def test_same_user_different_channels(self, db):
        db.create_channel_user("discord", "d_123", "u_abc")
        db.create_channel_user("slack", "s_456", "u_abc")
        d = db.get_channel_user("discord", "d_123")
        s = db.get_channel_user("slack", "s_456")
        assert d is not None and d["user_id"] == "u_abc"
        assert s is not None and s["user_id"] == "u_abc"

    def test_list_by_user(self, db):
        db.create_channel_user("discord", "d_123", "u_abc")
        db.create_channel_user("slack", "s_456", "u_abc")
        db.create_channel_user("discord", "d_999", "u_other")
        results = db.list_channel_users_by_user("u_abc")
        assert len(results) == 2
        types = {r["channel_type"] for r in results}
        assert types == {"discord", "slack"}

    def test_list_by_user_empty(self, db):
        assert db.list_channel_users_by_user("u_nobody") == []

    def test_delete(self, db):
        db.create_channel_user("discord", "12345", "u_abc")
        assert db.delete_channel_user("discord", "12345") is True
        assert db.get_channel_user("discord", "12345") is None

    def test_delete_nonexistent(self, db):
        assert db.delete_channel_user("discord", "99999") is False

    def test_delete_user_cascades_channel_users(self, db):
        """Deleting a turnstone user should cascade to channel_users."""
        db.create_user("u_abc", "admin", "Admin", "hash123")
        db.create_channel_user("discord", "12345", "u_abc")
        db.delete_user("u_abc")
        assert db.get_channel_user("discord", "12345") is None


class TestChannelRouteCRUD:
    """Tests for channel_routes table operations."""

    def test_create_and_get(self, db):
        db.create_channel_route("discord", "thread_123", "ws_abc", "node_1")
        result = db.get_channel_route("discord", "thread_123")
        assert result is not None
        assert result["channel_type"] == "discord"
        assert result["channel_id"] == "thread_123"
        assert result["ws_id"] == "ws_abc"
        assert result["node_id"] == "node_1"
        assert "created" in result

    def test_get_nonexistent(self, db):
        assert db.get_channel_route("discord", "thread_999") is None

    def test_create_duplicate_noop(self, db):
        db.create_channel_route("discord", "thread_123", "ws_abc")
        db.create_channel_route("discord", "thread_123", "ws_different")
        result = db.get_channel_route("discord", "thread_123")
        assert result is not None
        assert result["ws_id"] == "ws_abc"  # first write wins

    def test_default_empty_node_id(self, db):
        db.create_channel_route("discord", "thread_123", "ws_abc")
        result = db.get_channel_route("discord", "thread_123")
        assert result is not None
        assert result["node_id"] == ""

    def test_get_by_ws(self, db):
        db.create_channel_route("discord", "thread_123", "ws_abc", "node_1")
        result = db.get_channel_route_by_ws("ws_abc")
        assert result is not None
        assert result["channel_id"] == "thread_123"
        assert result["ws_id"] == "ws_abc"

    def test_get_by_ws_nonexistent(self, db):
        assert db.get_channel_route_by_ws("ws_nobody") is None

    def test_delete(self, db):
        db.create_channel_route("discord", "thread_123", "ws_abc")
        assert db.delete_channel_route("discord", "thread_123") is True
        assert db.get_channel_route("discord", "thread_123") is None

    def test_delete_nonexistent(self, db):
        assert db.delete_channel_route("discord", "thread_999") is False

    def test_multiple_channels_same_type(self, db):
        db.create_channel_route("discord", "thread_1", "ws_1")
        db.create_channel_route("discord", "thread_2", "ws_2")
        r1 = db.get_channel_route("discord", "thread_1")
        r2 = db.get_channel_route("discord", "thread_2")
        assert r1 is not None and r1["ws_id"] == "ws_1"
        assert r2 is not None and r2["ws_id"] == "ws_2"

    def test_different_channel_types(self, db):
        db.create_channel_route("discord", "thread_1", "ws_1")
        db.create_channel_route("slack", "channel_1", "ws_2")
        d = db.get_channel_route("discord", "thread_1")
        s = db.get_channel_route("slack", "channel_1")
        assert d is not None and d["ws_id"] == "ws_1"
        assert s is not None and s["ws_id"] == "ws_2"
