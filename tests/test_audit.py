"""Tests for turnstone.core.audit."""

import json
import pytest
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.audit import record_audit


@pytest.fixture
def storage(tmp_path):
    path = str(tmp_path / "test.db")
    backend = SQLiteBackend(path)
    yield backend
    backend.close()


def test_record_audit_basic(storage):
    record_audit(
        storage, "user-1", "user.create", "user", "u123", {"username": "alice"}, "127.0.0.1"
    )
    events = storage.list_audit_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["user_id"] == "user-1"
    assert ev["action"] == "user.create"
    assert ev["resource_type"] == "user"
    assert ev["resource_id"] == "u123"
    assert ev["ip_address"] == "127.0.0.1"
    detail = json.loads(ev["detail"])
    assert detail["username"] == "alice"


def test_record_audit_no_detail(storage):
    record_audit(storage, "user-1", "token.revoke", "token", "t456")
    events = storage.list_audit_events()
    assert len(events) == 1
    assert events[0]["detail"] == "{}"


def test_record_audit_silent_on_failure():
    """record_audit should not raise even if storage is broken."""

    class BrokenStorage:
        def record_audit_event(self, **kw):
            raise RuntimeError("boom")

    # Should not raise
    record_audit(BrokenStorage(), "u1", "test.action")


def test_record_audit_generates_unique_ids(storage):
    record_audit(storage, "u1", "a.one")
    record_audit(storage, "u1", "a.two")
    events = storage.list_audit_events()
    assert len(events) == 2
    assert events[0]["event_id"] != events[1]["event_id"]
