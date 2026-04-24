"""Server-side tests for the close_workstream handler's close_reason
persistence — guards the seam that lets coordinator inspect surface
why a workstream was retired without scraping the audit log.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

import turnstone.server as srv_mod
from turnstone.core.auth import JWT_AUD_SERVER, create_jwt
from turnstone.core.metrics import MetricsCollector
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamState

_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _full_hdr() -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_jwt('u1', frozenset({'read', 'write', 'approve'}), 'test', _JWT_SECRET, audience=JWT_AUD_SERVER)}"
        )
    }


def _make_app(storage: Any) -> TestClient:
    srv_mod._metrics = MetricsCollector()
    srv_mod._metrics.model = "test-model"
    mock_session = MagicMock()
    mock_ws = MagicMock()
    mock_ws.id = "ws-target"
    mock_ws.name = "test"
    mock_ws.state = WorkstreamState.IDLE
    mock_ws.session = mock_session
    # Tenant gate (#375) checks ws.user_id == JWT subject; explicit set
    # so MagicMock's auto-generated truthy attribute doesn't reject the
    # request before the persistence path runs.  kind / parent_ws_id
    # land in the audit_detail dict alongside ``reason``.
    mock_ws.user_id = "u1"
    mock_ws.kind = "interactive"
    mock_ws.parent_ws_id = None
    mock_mgr = MagicMock()
    mock_mgr.get.return_value = mock_ws
    mock_mgr.close.return_value = True
    mock_mgr.list_all.return_value = [mock_ws]
    mock_mgr.max_active = 10

    app = srv_mod.create_app(
        workstreams=mock_mgr,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_JWT_SECRET,
        auth_storage=storage,
        cors_origins=["*"],
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "close.db"))


def test_close_with_reason_persists_to_workstream_config(storage):
    client = _make_app(storage)
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": "task complete"},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    assert cfg.get("close_reason") == "task complete"


def test_close_without_reason_does_not_touch_config(storage):
    client = _make_app(storage)
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target"},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    assert "close_reason" not in cfg


def test_close_reason_capped_at_512_bytes(storage):
    """A model that dumps a multi-KB blob (or a captured secret) into the
    close reason must not be able to grow the workstream_config row
    without bound — the handler enforces a 512-byte ceiling.  Tested
    with ASCII (1B/char) so the byte cap and char count coincide."""
    huge = "x" * 5000
    client = _make_app(storage)
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": huge},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    stored = cfg.get("close_reason")
    assert stored is not None
    assert len(stored.encode("utf-8")) <= 512


def test_close_reason_byte_cap_holds_for_multibyte_utf8(storage):
    """Repro for the char-cap-vs-byte-cap mismatch: a CJK-only payload
    of 600 chars would have leaked through a code-point slice at
    600*3=1800 bytes.  The byte-aware cap holds it at <=512 bytes."""
    huge = "\u6f22" * 600  # 3 bytes/char in UTF-8
    client = _make_app(storage)
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": huge},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    stored = cfg.get("close_reason")
    assert stored is not None
    assert len(stored.encode("utf-8")) <= 512


def test_close_with_non_string_reason_drops_silently(storage):
    """A malformed body (reason=dict / list / int) should not crash the
    handler — non-string reasons are coerced to empty and the close
    proceeds without writing to workstream_config."""
    client = _make_app(storage)
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": {"unexpected": "shape"}},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    assert "close_reason" not in cfg


def test_close_reason_redacts_credentials(storage):
    """A model under prompt injection that captures a secret and stuffs
    it into ``reason`` must not get to plant the plaintext secret in
    audit logs / workstream_config.  The output guard's credential-
    redaction pass runs at the close handler boundary."""
    client = _make_app(storage)
    secret = "AKIAIOSFODNN7EXAMPLE"  # AWS access key — output guard catches.
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": f"task done; key={secret}"},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
    cfg = storage.load_workstream_config("ws-target")
    stored = cfg.get("close_reason")
    assert stored is not None
    assert secret not in stored
    assert "[REDACTED:" in stored


def test_close_reason_persistence_failure_does_not_block_close(storage):
    """If the storage save raises, the close still succeeds — persistence
    is best-effort; a transient storage error must not block the user
    from closing a workstream."""
    client = _make_app(storage)

    def _boom(*args, **kwargs):
        raise RuntimeError("storage down")

    storage.save_workstream_config = _boom  # type: ignore[method-assign]
    resp = client.post(
        "/v1/api/workstreams/close",
        json={"ws_id": "ws-target", "reason": "task complete"},
        headers=_full_hdr(),
    )
    assert resp.status_code == 200
