"""HTTP endpoint tests for workstream attachments.

Uses Starlette's TestClient against an in-process app with a mocked
SessionManager.  Exercises: upload happy path, size/mime rejection,
pending-list, GET /content, DELETE, auth isolation, and the extended
/api/send handler with both explicit and auto-consumed attachment ids.
"""

from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

# Magic-byte-valid 1x1 PNG
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _make_jwt(user_id: str) -> str:
    from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

    return create_jwt(
        user_id=user_id,
        scopes=frozenset({"read", "write"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
    )


@pytest.fixture
def app_client(tmp_path):
    """Spin up an in-process Starlette app with a mocked SessionManager
    and a fresh SQLite storage."""
    import sqlalchemy as sa

    import turnstone.server as srv_mod
    from turnstone.core.memory import register_workstream
    from turnstone.core.metrics import MetricsCollector
    from turnstone.core.storage import init_storage, reset_storage
    from turnstone.core.storage._registry import get_storage
    from turnstone.core.storage._schema import workstreams as ws_tbl

    # Fresh DB per test
    db_path = tmp_path / "test.db"
    reset_storage()
    init_storage("sqlite", path=str(db_path), run_migrations=False)

    srv_mod._metrics = MetricsCollector()
    srv_mod._metrics.model = "test-model"

    # Register two workstreams with different owners
    register_workstream("ws-A", name="A")
    register_workstream("ws-B", name="B")
    # Seed user_id on the rows so ownership checks take the scoped path
    with get_storage()._conn() as conn:
        conn.execute(sa.update(ws_tbl).where(ws_tbl.c.ws_id == "ws-A").values(user_id="userA"))
        conn.execute(sa.update(ws_tbl).where(ws_tbl.c.ws_id == "ws-B").values(user_id="userB"))
        conn.commit()

    # SessionManager mock returns None for get(); send endpoint handles that,
    # but we bypass send to focus on attachments.  get() returning a mock is
    # only needed for /api/send; upload/list/content/delete don't use mgr.
    mock_mgr = MagicMock()
    mock_mgr.get.return_value = None
    mock_mgr.list_all.return_value = []
    mock_mgr.max_active = 10

    app = srv_mod.create_app(
        workstreams=mock_mgr,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_TEST_JWT_SECRET,
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, mock_mgr
    finally:
        client.close()
        reset_storage()


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_jwt(user)}"}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestUploadHappyPath:
    def test_upload_png(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("tiny.png", PNG_1x1, "image/png")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "image"
        assert body["mime_type"] == "image/png"
        assert body["size_bytes"] == len(PNG_1x1)
        assert body["filename"] == "tiny.png"
        assert body["attachment_id"]

    def test_upload_markdown_text(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("notes.md", b"# hi\n", "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "text"
        assert body["mime_type"] == "text/markdown"

    def test_upload_by_extension_when_mime_missing(self, app_client):
        client, _ = app_client
        # Send an application/octet-stream body — only extension should save it.
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("script.py", b"print('hi')\n", "application/octet-stream")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "text"


class TestUploadRejections:
    def test_oversize_image_rejected(self, app_client):
        client, _ = app_client
        # 5 MB PNG header followed by junk — triggers the 4 MiB cap
        big = PNG_1x1 + b"\x00" * (5 * 1024 * 1024)
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("big.png", big, "image/png")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 413
        assert resp.json().get("code") == "too_large"

    def test_oversize_text_rejected(self, app_client):
        client, _ = app_client
        big = b"x" * (600 * 1024)  # 600 KiB > 512 KiB text cap
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("big.md", big, "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 413

    def test_unsupported_mime_rejected(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("blob.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400
        assert "code" in resp.json()

    def test_non_utf8_text_rejected(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("bad.txt", b"\xff\xfe\x00\x00", "text/plain")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["error"]

    def test_empty_file_rejected(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("empty.md", b"", "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400

    def test_missing_file_field_400(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            data={"not_file": "x"},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400

    def test_unknown_workstream_404(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-DOES-NOT-EXIST/attachments",
            files={"file": ("x.md", b"x", "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 404

    def test_any_caller_can_attach_to_workstream(self, app_client):
        # Trusted-team model: attaching to any workstream is gated on
        # scope auth, not ownership.  The attachment is filed under
        # the ws's persisted owner so existing storage shape holds.
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-B/attachments",
            files={"file": ("x.md", b"x", "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200


class TestPendingCap:
    def test_tenth_attachment_accepted_eleventh_rejected(self, app_client):
        client, _ = app_client
        for i in range(10):
            resp = client.post(
                "/v1/api/workstreams/ws-A/attachments",
                files={"file": (f"n{i}.md", b"x", "text/markdown")},
                headers=_auth("userA"),
            )
            assert resp.status_code == 200, resp.text
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("overflow.md", b"x", "text/markdown")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 409
        assert resp.json().get("code") == "too_many"

    def test_cap_is_serialized_under_concurrent_uploads(self, app_client):
        # Pre-fill to cap-1, then fire two concurrent uploads.  Exactly
        # one must succeed; the other must be rejected with 409.
        client, _ = app_client
        for i in range(9):
            assert (
                client.post(
                    "/v1/api/workstreams/ws-A/attachments",
                    files={"file": (f"pre{i}.md", b"x", "text/markdown")},
                    headers=_auth("userA"),
                ).status_code
                == 200
            )

        import concurrent.futures

        def attempt(idx: int) -> int:
            return client.post(
                "/v1/api/workstreams/ws-A/attachments",
                files={"file": (f"race{idx}.md", b"x", "text/markdown")},
                headers=_auth("userA"),
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(attempt, i) for i in range(2)]
            results = sorted(f.result() for f in futures)
        # One success (200) + one cap-exceeded (409); never 200+200.
        assert results == [200, 409]


# ---------------------------------------------------------------------------
# List / Get content / Delete
# ---------------------------------------------------------------------------


def _upload(client, ws_id: str, user: str, filename: str, data: bytes, mime: str) -> str:
    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/attachments",
        files={"file": (filename, data, mime)},
        headers=_auth(user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["attachment_id"]


class TestListAttachments:
    def test_list_pending_returns_metadata_only(self, app_client):
        client, _ = app_client
        _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")
        resp = client.get("/v1/api/workstreams/ws-A/attachments", headers=_auth("userA"))
        assert resp.status_code == 200
        atts = resp.json()["attachments"]
        assert len(atts) == 2
        # No content bytes in list payload
        assert all("content" not in a for a in atts)
        assert {a["filename"] for a in atts} == {"a.md", "b.md"}

    def test_list_visible_cluster_wide(self, app_client):
        # Trusted-team visibility: any authenticated caller can list
        # the attachments on any workstream.  Attachments are filed
        # under the ws's owner uid so a cross-caller lister still sees
        # the owner's pending uploads.
        client, _ = app_client
        _upload(client, "ws-A", "userA", "mine.md", b"mine", "text/markdown")
        resp = client.get("/v1/api/workstreams/ws-A/attachments", headers=_auth("userB"))
        assert resp.status_code == 200
        atts = resp.json()["attachments"]
        assert {a["filename"] for a in atts} == {"mine.md"}


class TestGetContent:
    def test_get_content_returns_bytes_with_mime(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.png", PNG_1x1, "image/png")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/content",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content == PNG_1x1
        # Defense-in-depth headers
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert "default-src 'none'" in resp.headers.get("content-security-policy", "")
        assert resp.headers.get("content-disposition", "").startswith("inline;")

    def test_get_content_forces_text_plain_for_text_kinds(self, app_client):
        # Uploading an HTML-ish file as text/html must NOT be served back
        # with Content-Type: text/html from our origin (XSS vector).
        client, _ = app_client
        aid = _upload(
            client,
            "ws-A",
            "userA",
            "evil.html",
            b"<script>alert(1)</script>",
            "text/html",
        )
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/content",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_get_content_visible_cluster_wide(self, app_client):
        # Trusted-team visibility: any authenticated caller can fetch
        # the content of an attachment on any workstream.  Attachments
        # are keyed by the ws's persisted owner uid so userB still
        # resolves userA's blob via _require_ws_access's owner return.
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.md", b"x", "text/markdown")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/content",
            headers=_auth("userB"),
        )
        assert resp.status_code == 200
        assert resp.content == b"x"

    def test_get_content_cross_workstream_id_404(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.md", b"x", "text/markdown")
        # Request via ws-B (owned by userB) with the id from ws-A — must 403 or 404
        resp = client.get(
            f"/v1/api/workstreams/ws-B/attachments/{aid}/content",
            headers=_auth("userB"),
        )
        # userB owns ws-B, so the ws-access check passes; the id-mismatch then
        # returns 404 to avoid leaking existence.
        assert resp.status_code == 404

    def test_get_content_unowned_ws_user_isolation(self, app_client):
        # Regression for PR #356 review: in a workstream without an
        # explicit owner (user_id == ""), one user's attachment must
        # not be fetchable by another user via id-guessing.
        client, _ = app_client
        from turnstone.core.memory import register_workstream

        register_workstream("ws-shared", name="shared")
        a_aid = _upload(client, "ws-shared", "userA", "secret.md", b"S", "text/markdown")
        # userB can reach ws-shared (owner blank → no ownership gate)
        # but must NOT be able to fetch userA's blob.
        resp = client.get(
            f"/v1/api/workstreams/ws-shared/attachments/{a_aid}/content",
            headers=_auth("userB"),
        )
        assert resp.status_code == 404
        # userA still gets their own
        resp = client.get(
            f"/v1/api/workstreams/ws-shared/attachments/{a_aid}/content",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.content == b"S"


class TestDelete:
    def test_delete_pending(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.md", b"x", "text/markdown")
        resp = client.delete(f"/v1/api/workstreams/ws-A/attachments/{aid}", headers=_auth("userA"))
        assert resp.status_code == 200
        # Gone now
        resp = client.delete(f"/v1/api/workstreams/ws-A/attachments/{aid}", headers=_auth("userA"))
        assert resp.status_code == 404

    def test_delete_cluster_wide(self, app_client):
        # Trusted-team model: any authenticated caller can delete an
        # attachment on any workstream.  The filed ``user_id`` stays
        # for audit even after a cross-caller delete.
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.md", b"x", "text/markdown")
        resp = client.delete(f"/v1/api/workstreams/ws-A/attachments/{aid}", headers=_auth("userB"))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/send with attachment_ids
# ---------------------------------------------------------------------------


class TestSendMessageAttachments:
    def _wire_ws(self, mgr, ws_id: str, user_id: str):
        """Install a mock Workstream that captures session.send kwargs."""
        from turnstone.core.workstream import WorkstreamState

        session = MagicMock()
        session._cancel_event = threading.Event()
        session.queue_message = MagicMock()
        captured: dict = {}

        def fake_send(message, attachments=None, send_id=None):
            captured["message"] = message
            captured["attachments"] = attachments
            captured["send_id"] = send_id

        session.send = fake_send

        ui = MagicMock()
        ui._ws_lock = threading.Lock()
        ui._ws_messages = 0
        ui._ws_turn_tool_calls = 0

        ws = MagicMock()
        ws.id = ws_id
        ws.state = WorkstreamState.IDLE
        ws.ui = ui
        ws.session = session
        ws.worker_thread = None
        ws._lock = threading.RLock()
        mgr.get.return_value = ws
        return captured, session

    def test_send_explicit_attachment_ids_resolves_and_passes(self, app_client):
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        aid = _upload(client, "ws-A", "userA", "n.md", b"hi", "text/markdown")

        resp = client.post(
            "/v1/api/send",
            json={"message": "review", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        # Give the worker thread a moment to run fake_send
        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        assert captured.get("message") == "review"
        atts = captured["attachments"]
        assert atts is not None and len(atts) == 1
        assert atts[0].attachment_id == aid
        assert atts[0].kind == "text"

    def test_send_auto_consumes_pending_when_ids_omitted(self, app_client):
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")

        resp = client.post(
            "/v1/api/send",
            json={"message": "do", "ws_id": "ws-A"},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        assert captured["attachments"] is not None
        assert len(captured["attachments"]) == 2

    def test_send_empty_list_disables_autoconsume(self, app_client):
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")

        resp = client.post(
            "/v1/api/send",
            json={"message": "plain", "ws_id": "ws-A", "attachment_ids": []},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        assert captured["attachments"] is None  # send got None, no attachments

    def test_send_preserves_explicit_attachment_id_order(self, app_client):
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        a = _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        b = _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")
        c = _upload(client, "ws-A", "userA", "c.md", b"C", "text/markdown")

        # Request order: c, a, b — must be preserved through resolution
        resp = client.post(
            "/v1/api/send",
            json={
                "message": "ordered",
                "ws_id": "ws-A",
                "attachment_ids": [c, a, b],
            },
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        atts = captured["attachments"]
        assert [x.attachment_id for x in atts] == [c, a, b]

    def test_send_oversized_attachment_ids_list_rejected(self, app_client):
        # Hostile / buggy clients should not be able to push an
        # arbitrarily long IN (...) clause through reservation.
        client, mgr = app_client
        self._wire_ws(mgr, "ws-A", "userA")
        from turnstone.core.attachments import MAX_PENDING_ATTACHMENTS_PER_USER_WS

        too_many = [f"id-{i}" for i in range(MAX_PENDING_ATTACHMENTS_PER_USER_WS + 1)]
        resp = client.post(
            "/v1/api/send",
            json={"message": "x", "ws_id": "ws-A", "attachment_ids": too_many},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400
        assert resp.json().get("code") == "too_many"

    def test_send_forged_id_from_other_user_ignored(self, app_client):
        client, mgr = app_client
        # userB uploads to ws-B
        _upload(client, "ws-B", "userB", "secret.md", b"secret", "text/markdown")
        # userA tries to include userB's attachment id in their send on ws-A
        resp = client.get("/v1/api/workstreams/ws-B/attachments", headers=_auth("userB"))
        atts = resp.json()["attachments"]
        assert len(atts) == 1
        stolen_id = atts[0]["attachment_id"]

        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        resp = client.post(
            "/v1/api/send",
            json={
                "message": "sneaky",
                "ws_id": "ws-A",
                "attachment_ids": [stolen_id],
            },
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        # Forged id is scope-rejected — no attachments reach session.send
        assert captured["attachments"] is None


class TestQueuedSendWithAttachments:
    """When the worker is busy, send queues the message + attachment_ids
    so the multimodal turn isn't silently reduced to text on dequeue."""

    def _wire_busy_ws(self, mgr, ws_id: str):
        """Mock ws whose worker_thread is always 'alive' (forces queue path).
        Captures args passed to queue_message so the test can assert on
        ordered attachment_ids.
        """
        from turnstone.core.workstream import WorkstreamState

        captured: dict = {}

        def fake_queue_message(text, attachment_ids=None, queue_msg_id=None):
            captured["text"] = text
            captured["attachment_ids"] = list(attachment_ids or ())
            captured["queue_msg_id"] = queue_msg_id
            # Return the supplied id so server-side tracking is coherent
            return text, "notice", queue_msg_id or "q-msg-1"

        session = MagicMock()
        session._cancel_event = threading.Event()
        session.queue_message = fake_queue_message

        ui = MagicMock()
        ui._ws_lock = threading.Lock()
        ui._ws_messages = 0
        ui._ws_turn_tool_calls = 0

        # worker_thread needs .is_alive() → True to hit the queue branch
        worker = MagicMock()
        worker.is_alive = MagicMock(return_value=True)

        ws = MagicMock()
        ws.id = ws_id
        ws.state = WorkstreamState.RUNNING
        ws.ui = ui
        ws.session = session
        ws.worker_thread = worker
        ws._lock = threading.RLock()
        mgr.get.return_value = ws
        return captured

    def test_busy_queue_carries_ordered_attachment_ids(self, app_client):
        client, mgr = app_client
        captured = self._wire_busy_ws(mgr, "ws-A")
        a = _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        b = _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")

        resp = client.post(
            "/v1/api/send",
            json={
                "message": "ping",
                "ws_id": "ws-A",
                "attachment_ids": [b, a],  # intentionally reversed
            },
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert captured["text"] == "ping"
        # Ordered ids must reach queue_message so dequeue flushes them
        # as a properly-ordered multipart turn.
        assert captured["attachment_ids"] == [b, a]


class TestQueuedAttachmentReservation:
    """Once a queued send reserves its attachments, concurrent operations
    (delete, auto-consume, explicit reuse) must not disturb them."""

    def _wire_busy_ws(self, mgr, ws_id: str):
        """Mock ws whose worker is always alive (forces queue path).
        Uses the real ChatSession's queue_message so reservation runs."""
        from turnstone.core.session import ChatSession
        from turnstone.core.workstream import WorkstreamState

        session = ChatSession(
            client=MagicMock(),
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.3,
            max_tokens=1024,
            tool_timeout=10,
            user_id="userA",
        )
        # Pin the session to ws-A so queue_message / dequeue_message
        # operate on the expected storage rows.
        session._ws_id = ws_id

        ui = MagicMock()
        ui._ws_lock = threading.Lock()
        ui._ws_messages = 0
        ui._ws_turn_tool_calls = 0

        worker = MagicMock()
        worker.is_alive = MagicMock(return_value=True)

        ws = MagicMock()
        ws.id = ws_id
        ws.state = WorkstreamState.RUNNING
        ws.ui = ui
        ws.session = session
        ws.worker_thread = worker
        ws._lock = threading.RLock()
        mgr.get.return_value = ws
        return ws, session

    def _queue_with_attachment(self, client, mgr, ws_id: str, filename: str = "q.md"):
        aid = _upload(client, ws_id, "userA", filename, b"Q", "text/markdown")
        ws, session = self._wire_busy_ws(mgr, ws_id)
        resp = client.post(
            "/v1/api/send",
            json={"message": "queued", "ws_id": ws_id, "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        return aid, body["msg_id"], session

    def test_reserved_attachment_hidden_from_pending_listing(self, app_client):
        client, mgr = app_client
        aid, _mid, _session = self._queue_with_attachment(client, mgr, "ws-A")
        resp = client.get("/v1/api/workstreams/ws-A/attachments", headers=_auth("userA"))
        # Reserved attachment is not in the pending listing
        ids = [a["attachment_id"] for a in resp.json()["attachments"]]
        assert aid not in ids

    def test_reserved_attachment_cannot_be_deleted(self, app_client):
        client, mgr = app_client
        aid, _mid, _session = self._queue_with_attachment(client, mgr, "ws-A")
        resp = client.delete(
            f"/v1/api/workstreams/ws-A/attachments/{aid}",
            headers=_auth("userA"),
        )
        # Delete silently masks reserved ones as not-found (can't delete
        # while tied to a queued message).
        assert resp.status_code == 404
        # Still exists on the backend
        from turnstone.core.memory import get_attachment

        assert get_attachment(aid) is not None

    def test_reserved_attachment_not_auto_consumed_by_later_send(self, app_client):
        client, mgr = app_client
        aid, _mid, session = self._queue_with_attachment(client, mgr, "ws-A")

        # Swap the busy worker for an idle one and capture the next
        # session.send call so we can assert on its attachment list.
        captured: dict = {}

        def fake_send(message, attachments=None, send_id=None):
            captured["message"] = message
            captured["attachments"] = attachments
            captured["send_id"] = send_id

        session.send = fake_send  # type: ignore[method-assign]
        ws = mgr.get.return_value
        ws.worker_thread = None  # idle → non-queue path

        # Auto-consume on a follow-up send: reserved attachment must not
        # be picked up (another turn isn't entitled to it).
        resp = client.post(
            "/v1/api/send",
            json={"message": "follow up", "ws_id": "ws-A"},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        atts = captured.get("attachments")
        if atts is not None:
            assert aid not in [a.attachment_id for a in atts]

    def test_reserved_attachment_rejected_in_explicit_ids(self, app_client):
        client, mgr = app_client
        aid, _mid, session = self._queue_with_attachment(client, mgr, "ws-A")

        captured: dict = {}

        def fake_send(message, attachments=None, send_id=None):
            captured["attachments"] = attachments

        session.send = fake_send  # type: ignore[method-assign]
        ws = mgr.get.return_value
        ws.worker_thread = None

        # A second send explicitly naming the reserved id: scope check
        # rejects it, so the attachment list is empty.
        resp = client.post(
            "/v1/api/send",
            json={"message": "take mine", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        atts = captured.get("attachments")
        # Either None (all scope-rejected → empty list collapses to None)
        # or an empty list — never contains the reserved id.
        if atts is not None:
            assert aid not in [a.attachment_id for a in atts]

    def test_dequeue_releases_reservation(self, app_client):
        client, mgr = app_client
        aid, mid, session = self._queue_with_attachment(client, mgr, "ws-A")

        # Cancel the queued message — DELETE /api/send with msg_id
        resp = client.request(
            "DELETE",
            "/v1/api/send",
            json={"ws_id": "ws-A", "msg_id": mid},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.json().get("status") == "removed"

        # Attachment is back to pending — visible + deletable
        resp = client.get("/v1/api/workstreams/ws-A/attachments", headers=_auth("userA"))
        ids = [a["attachment_id"] for a in resp.json()["attachments"]]
        assert aid in ids
        resp = client.delete(
            f"/v1/api/workstreams/ws-A/attachments/{aid}",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200


class TestReserveThenDispatchRace:
    """Reservation happens BEFORE queue_message / worker start, so an
    overlapping request can't select the same row."""

    def test_overlapping_idle_send_cannot_resteal(self, app_client):
        # Kick off an idle send that reserves attachment A but blocks
        # inside session.send — then a second send with the same
        # explicit id must NOT receive A.
        client, mgr = app_client
        from turnstone.core.session import ChatSession
        from turnstone.core.workstream import WorkstreamState

        aid = _upload(client, "ws-A", "userA", "hold.md", b"hold", "text/markdown")

        # Real ChatSession for the idle path (reservation must be real)
        session = ChatSession(
            client=MagicMock(),
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.3,
            max_tokens=1024,
            tool_timeout=10,
            user_id="userA",
        )
        session._ws_id = "ws-A"

        first_captured: dict = {}
        gate = threading.Event()

        def first_send(message, attachments=None, send_id=None):
            first_captured["attachments"] = attachments
            first_captured["send_id"] = send_id
            gate.wait(timeout=5.0)  # Hold the worker so #2 races against us

        session.send = first_send  # type: ignore[method-assign]

        ui = MagicMock()
        ui._ws_lock = threading.Lock()
        ui._ws_messages = 0
        ui._ws_turn_tool_calls = 0

        ws = MagicMock()
        ws.id = "ws-A"
        ws.state = WorkstreamState.IDLE
        ws.ui = ui
        ws.session = session
        ws.worker_thread = None
        ws._lock = threading.RLock()
        mgr.get.return_value = ws

        # First send — reserves A under its send_id, worker blocks
        resp1 = client.post(
            "/v1/api/send",
            json={"message": "one", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp1.status_code == 200

        import time

        for _ in range(50):
            if first_captured.get("attachments") is not None:
                break
            time.sleep(0.01)
        assert first_captured["attachments"] is not None
        assert [a.attachment_id for a in first_captured["attachments"]] == [aid]
        first_send_id = first_captured["send_id"]
        assert first_send_id

        # Second send — idle path busy-check still sees the mock's
        # worker as "not alive" (we didn't update it) so this enters
        # the idle branch.  Reservation must skip the already-reserved
        # row, so send sees no attachments.
        second_captured: dict = {}

        def second_send(message, attachments=None, send_id=None):
            second_captured["attachments"] = attachments

        session.send = second_send  # type: ignore[method-assign]

        resp2 = client.post(
            "/v1/api/send",
            json={"message": "two", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp2.status_code == 200

        for _ in range(50):
            if "attachments" in second_captured:
                break
            time.sleep(0.01)
        # The second send either got no attachments or an empty list —
        # it never saw the row that the first send reserved.
        seen = second_captured.get("attachments")
        assert not seen or aid not in [a.attachment_id for a in (seen or [])]

        # Release the first worker so the fixture can tear down cleanly
        gate.set()

    def test_worker_exception_releases_reservation(self, app_client):
        # If session.send raises inside the worker thread, the
        # reservation must be released so the attachment isn't
        # permanently soft-locked.
        client, mgr = app_client
        from turnstone.core.memory import get_attachment

        aid = _upload(client, "ws-A", "userA", "boom.md", b"x", "text/markdown")

        captured, session = TestSendMessageAttachments._wire_ws(
            TestSendMessageAttachments, mgr, "ws-A", "userA"
        )

        def exploding_send(message, attachments=None, send_id=None):
            raise RuntimeError("worker blew up")

        session.send = exploding_send  # type: ignore[method-assign]

        resp = client.post(
            "/v1/api/send",
            json={"message": "boom", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        # Wait for the worker thread to finish (crash + cleanup).
        import time

        for _ in range(100):
            row = get_attachment(aid)
            if row and row.get("reserved_for_msg_id") is None:
                break
            time.sleep(0.01)

        row = get_attachment(aid)
        # Reservation must be released so the attachment is usable again.
        assert row is not None
        assert row["reserved_for_msg_id"] is None
        assert row["message_id"] is None

    def test_partial_reservation_proceeds_with_reserved_subset(self, app_client):
        # Pre-reserve one id; a send listing two explicit ids should
        # proceed with only the non-reserved one.
        client, mgr = app_client
        from turnstone.core.memory import reserve_attachments

        a = _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        b = _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")
        # Pre-reserve 'a' under a fake prior send
        reserve_attachments([a], "prior-send", "ws-A", "userA")

        captured: dict = {}

        def fake_send(message, attachments=None, send_id=None):
            captured["attachments"] = attachments

        ws_tuple = TestSendMessageAttachments._wire_ws(
            TestSendMessageAttachments, mgr, "ws-A", "userA"
        )
        captured = ws_tuple[0]  # _wire_ws returns (captured, session)
        ws_tuple[1].send = fake_send  # type: ignore[method-assign]

        resp = client.post(
            "/v1/api/send",
            json={"message": "both", "ws_id": "ws-A", "attachment_ids": [a, b]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        atts = captured.get("attachments") or []
        ids = [x.attachment_id for x in atts]
        # Only the un-pre-reserved id survives
        assert ids == [b]


class TestServiceScopedActorFlow:
    """Service-scoped tokens bypass ownership checks and file attachments
    under the workstream owner; send() must consume them using the same
    owner-resolution helper (not the raw caller id)."""

    def test_service_upload_then_send_consumes(self, app_client):
        client, mgr = app_client

        # Service token: has the 'service' scope
        from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

        service_token = create_jwt(
            user_id="svc-bot",
            scopes=frozenset({"read", "write", "service"}),
            source="test",
            secret=_TEST_JWT_SECRET,
            audience=JWT_AUD_SERVER,
        )
        svc_headers = {"Authorization": f"Bearer {service_token}"}

        # Upload to ws-A (owned by userA) as service — file should land
        # under owner "userA", not "svc-bot"
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("svc.md", b"svc", "text/markdown")},
            headers=svc_headers,
        )
        assert resp.status_code == 200
        aid = resp.json()["attachment_id"]

        from turnstone.core.memory import get_attachment

        row = get_attachment(aid)
        assert row["user_id"] == "userA"  # filed under the owner

        # Now drive /api/send as the service token — the resolver uses
        # the ws owner (userA) to look up attachments, so the upload is
        # found and passed through.
        captured, _ = TestSendMessageAttachments._wire_ws(
            TestSendMessageAttachments(),
            mgr,
            "ws-A",
            "userA",
        )
        resp = client.post(
            "/v1/api/send",
            json={"message": "svc send", "ws_id": "ws-A", "attachment_ids": [aid]},
            headers=svc_headers,
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        atts = captured["attachments"]
        assert atts is not None and len(atts) == 1
        assert atts[0].attachment_id == aid
