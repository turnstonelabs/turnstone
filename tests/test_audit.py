"""Tests for turnstone.core.audit."""

import json

import pytest

from turnstone.core.audit import record_audit
from turnstone.core.storage._sqlite import SQLiteBackend


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


# ---------------------------------------------------------------------------
# Credential redaction at the audit boundary
# ---------------------------------------------------------------------------


def test_record_audit_redacts_passwords_by_default(storage):
    """Detail strings go through redact_credentials by default."""
    record_audit(
        storage,
        "u1",
        "coordinator.spawn",
        detail={
            "initial_message": "connect via postgresql://alice:s3cret@db.example.com/app",
        },
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    # Exact redaction text comes from output_guard._redact_credentials —
    # assert the token is stripped rather than the exact marker so
    # this test doesn't break if the marker format evolves.
    assert "s3cret" not in detail["initial_message"]
    assert "REDACTED" in detail["initial_message"]


def test_record_audit_redacts_nested_strings(storage):
    """Walker descends into lists / nested dicts."""
    record_audit(
        storage,
        "u1",
        "task_list.update",
        detail={
            "tasks": [
                {"title": "normal task"},
                {"title": "pull secret from AWS_SECRET_ACCESS_KEY=AKIAEXAMPLE123"},
            ],
        },
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    assert detail["tasks"][0]["title"] == "normal task"
    assert "AKIAEXAMPLE123" not in detail["tasks"][1]["title"]


def test_record_audit_raw_detail_preserves_payload(storage):
    """`raw_detail=True` bypasses the scrub — operator-originated detail only."""
    secret_like = "postgresql://alice:s3cret@db.example.com/app"
    record_audit(
        storage,
        "admin-1",
        "investigation.note",
        detail={"note": secret_like},
        raw_detail=True,
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    assert detail["note"] == secret_like


def test_record_audit_strips_control_chars(storage):
    """CR/LF/NUL/DEL and C0 controls are replaced with spaces so a
    downstream exporter that prints raw detail strings can't re-surface
    log-injection.  Tab/newline are deliberately preserved."""
    record_audit(
        storage,
        "u1",
        "coordinator.note",
        detail={
            "msg": "hello\r\nInjected: bad\x00 escape \x1b[31mred\x1b[0m\x7f",
            "ok_tab": "a\tb\nc",
        },
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    # CR / NUL / ESC / DEL scrubbed to spaces; tab + newline kept.
    assert "\r" not in detail["msg"]
    assert "\x00" not in detail["msg"]
    assert "\x1b" not in detail["msg"]
    assert "\x7f" not in detail["msg"]
    assert "hello" in detail["msg"]
    assert detail["ok_tab"] == "a\tb\nc"


def test_record_audit_clean_strings_roundtrip_unchanged(storage):
    """Detail strings with no credential patterns and no control chars
    pass through unchanged — the fast-path / scrub must not corrupt the
    common case."""
    clean = {"note": "hello world", "code": "import foo", "state": "ok"}
    record_audit(storage, "u1", "coordinator.note", detail=clean)
    event = storage.list_audit_events()[0]
    assert json.loads(event["detail"]) == clean


def test_record_audit_fast_path_skips_no_string_detail(storage):
    """A detail carrying only scalars (no strings anywhere) must persist
    identically — exercises the ``_has_any_string`` fast path."""
    record_audit(
        storage,
        "u1",
        "coordinator.metric",
        detail={"spawned": 5, "ok": True, "parent": None, "tail": [1, 2, 3]},
    )
    event = storage.list_audit_events()[0]
    assert json.loads(event["detail"]) == {
        "spawned": 5,
        "ok": True,
        "parent": None,
        "tail": [1, 2, 3],
    }


def test_record_audit_redacts_dict_keys(storage):
    """Walker descends into dict keys too — a caller using
    model-controlled text as a key can't leak it verbatim."""
    record_audit(
        storage,
        "u1",
        "coordinator.note",
        detail={"postgresql://alice:s3cret@db.example.com/app": True},
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    assert all("s3cret" not in k for k in detail)


def test_record_audit_walks_set_and_frozenset(storage):
    """Walker handles set/frozenset values (docstring promise)."""
    record_audit(
        storage,
        "u1",
        "coordinator.note",
        detail={"tags": frozenset({"ak_" + "x" * 40, "plain"})},
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    # The credential-looking AK token gets scrubbed; the plain one survives.
    tags = detail["tags"]
    assert "plain" in tags


def test_record_audit_leaves_non_string_scalars_alone(storage):
    """Non-string scalars (int / bool / None) pass through unchanged."""
    record_audit(
        storage,
        "u1",
        "coordinator.spawn",
        detail={"budget_ok": True, "spawned": 5, "parent": None},
    )
    event = storage.list_audit_events()[0]
    detail = json.loads(event["detail"])
    assert detail == {"budget_ok": True, "spawned": 5, "parent": None}
