"""Tests for the services registry storage methods."""

from __future__ import annotations

import pytest

from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


class TestServiceRegistry:
    def test_register_and_list(self, storage):
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        services = storage.list_services("channel", max_age_seconds=120)
        assert len(services) == 1
        assert services[0]["service_type"] == "channel"
        assert services[0]["service_id"] == "ch-1"
        assert services[0]["url"] == "http://localhost:8091"

    def test_register_upsert(self, storage):
        storage.register_service("channel", "ch-1", "http://old:8091")
        storage.register_service("channel", "ch-1", "http://new:8091")
        services = storage.list_services("channel", max_age_seconds=120)
        assert len(services) == 1
        assert services[0]["url"] == "http://new:8091"

    def test_heartbeat(self, storage):
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        result = storage.heartbeat_service("channel", "ch-1")
        assert result is True

    def test_heartbeat_nonexistent(self, storage):
        result = storage.heartbeat_service("channel", "nonexistent")
        assert result is False

    def test_list_filters_stale(self, storage):
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        # Manually set heartbeat to the past so it's stale
        from datetime import UTC, datetime, timedelta

        import sqlalchemy as sa

        from turnstone.core.storage._schema import services

        old_time = (datetime.now(UTC) - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S")
        with storage._engine.connect() as conn:
            conn.execute(sa.update(services).values(last_heartbeat=old_time))
            conn.commit()

        # Should be excluded with 120s max age
        result = storage.list_services("channel", max_age_seconds=120)
        assert len(result) == 0

    def test_list_empty(self, storage):
        services = storage.list_services("channel", max_age_seconds=120)
        assert services == []

    def test_list_filters_by_type(self, storage):
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        storage.register_service("bridge", "br-1", "http://localhost:8080")
        channels = storage.list_services("channel", max_age_seconds=120)
        bridges = storage.list_services("bridge", max_age_seconds=120)
        assert len(channels) == 1
        assert len(bridges) == 1

    def test_deregister(self, storage):
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        result = storage.deregister_service("channel", "ch-1")
        assert result is True
        services = storage.list_services("channel", max_age_seconds=120)
        assert services == []

    def test_deregister_nonexistent(self, storage):
        result = storage.deregister_service("channel", "nonexistent")
        assert result is False

    def test_metadata(self, storage):
        storage.register_service(
            "channel", "ch-1", "http://localhost:8091", metadata='{"adapter": "discord"}'
        )
        services = storage.list_services("channel", max_age_seconds=120)
        assert services[0]["metadata"] == '{"adapter": "discord"}'
