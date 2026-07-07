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

# Magic-byte-valid minimal WAV (RIFF....WAVE) for audio-kind uploads.
WAV_12 = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16

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
    # Pending uploads live in the process-global per-node buffer now; clear it
    # so staged uploads can't leak across tests.
    from turnstone.core.attachment_buffer import get_attachment_buffer

    get_attachment_buffer().clear()

    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, mock_mgr
    finally:
        client.close()
        get_attachment_buffer().clear()
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


# (The per-user pending-upload cap was removed with the content-addressing
# cutover — pending uploads live in the per-node buffer, bounded by its own
# size/TTL ceilings rather than a per-(ws,user) count.  The cap tests that
# lived here are gone.)


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


class TestGetThumbnail:
    """The /thumbnail handler + the shared _resolve_served_blob gate it reuses:
    200+png for image/pdf, 415 for non-thumbnailable kinds or a failed render,
    and a 404 (no existence leak) for cross-ws / cross-user id access."""

    def _thumb_url(self, ws_id: str, aid: str) -> str:
        return f"/v1/api/workstreams/{ws_id}/attachments/{aid}/thumbnail"

    def test_image_thumbnail_200_png_with_hardening_headers(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.png", PNG_1x1, "image/png")
        resp = client.get(self._thumb_url("ws-A", aid), headers=_auth("userA"))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert "default-src 'none'" in resp.headers.get("content-security-policy", "")
        assert "max-age=300" in resp.headers.get("cache-control", "")

    def test_audio_thumbnail_415(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "a.wav", WAV_12, "audio/wav")
        resp = client.get(self._thumb_url("ws-A", aid), headers=_auth("userA"))
        assert resp.status_code == 415

    def test_text_thumbnail_415(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "n.md", b"# hi\n", "text/markdown")
        resp = client.get(self._thumb_url("ws-A", aid), headers=_auth("userA"))
        assert resp.status_code == 415

    def test_thumbnail_unavailable_returns_415(self, app_client, monkeypatch):
        # kind is image (reaches make_thumbnail) but the render yields None.
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.png", PNG_1x1, "image/png")
        monkeypatch.setattr("turnstone.core.thumbnails.make_thumbnail", lambda *a, **k: None)
        resp = client.get(self._thumb_url("ws-A", aid), headers=_auth("userA"))
        assert resp.status_code == 415

    def test_thumbnail_cross_workstream_id_404(self, app_client):
        client, _ = app_client
        aid = _upload(client, "ws-A", "userA", "t.png", PNG_1x1, "image/png")
        # userB owns ws-B; the id belongs to ws-A → 404 (no existence leak).
        resp = client.get(self._thumb_url("ws-B", aid), headers=_auth("userB"))
        assert resp.status_code == 404

    def test_thumbnail_unowned_ws_user_isolation_404(self, app_client):
        client, _ = app_client
        from turnstone.core.memory import register_workstream

        register_workstream("ws-shared-thumb", name="shared")
        aid = _upload(client, "ws-shared-thumb", "userA", "s.png", PNG_1x1, "image/png")
        # Blank-owner ws is reachable by userB, but userA's blob must not be.
        resp = client.get(self._thumb_url("ws-shared-thumb", aid), headers=_auth("userB"))
        assert resp.status_code == 404
        resp = client.get(self._thumb_url("ws-shared-thumb", aid), headers=_auth("userA"))
        assert resp.status_code == 200


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
        ws._worker_running = False
        ws._lock = threading.RLock()
        mgr.get.return_value = ws
        return captured, session

    def test_send_explicit_attachment_ids_resolves_and_passes(self, app_client):
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        aid = _upload(client, "ws-A", "userA", "n.md", b"hi", "text/markdown")

        resp = client.post(
            "/v1/api/workstreams/ws-A/send",
            json={"message": "review", "attachment_ids": [aid]},
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
            "/v1/api/workstreams/ws-A/send",
            json={"message": "do"},
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
            "/v1/api/workstreams/ws-A/send",
            json={"message": "plain", "attachment_ids": []},
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
            "/v1/api/workstreams/ws-A/send",
            json={
                "message": "ordered",
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

    def test_send_unknown_ids_resolve_to_nothing(self, app_client):
        # The old oversized-IN-clause / cap rejection is gone (no DB
        # reservation, no per-user cap).  Unknown ids simply don't resolve
        # from the buffer — the send proceeds with no attachments rather
        # than 400-ing.
        client, mgr = app_client
        captured, _ = self._wire_ws(mgr, "ws-A", "userA")
        many = [f"id-{i}" for i in range(50)]
        resp = client.post(
            "/v1/api/workstreams/ws-A/send",
            json={"message": "x", "attachment_ids": many},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200

        import time

        for _ in range(50):
            if "attachments" in captured:
                break
            time.sleep(0.01)
        assert captured["attachments"] is None

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
            "/v1/api/workstreams/ws-A/send",
            json={
                "message": "sneaky",
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

        def fake_queue_message(
            text, attachment_ids=None, queue_msg_id=None, interjector_user_id=""
        ):
            captured["text"] = text
            captured["attachment_ids"] = list(attachment_ids or ())
            captured["queue_msg_id"] = queue_msg_id
            captured["interjector_user_id"] = interjector_user_id
            # Return the supplied id so server-side tracking is coherent
            return text, "notice", queue_msg_id or "q-msg-1"

        session = MagicMock()
        session._cancel_event = threading.Event()
        session.queue_message = fake_queue_message

        ui = MagicMock()
        ui._ws_lock = threading.Lock()
        ui._ws_messages = 0
        ui._ws_turn_tool_calls = 0

        # _worker_running=True forces session_worker.send onto the queue path
        worker = MagicMock()
        worker.is_alive = MagicMock(return_value=True)

        ws = MagicMock()
        ws.id = ws_id
        ws.state = WorkstreamState.RUNNING
        ws.ui = ui
        ws.session = session
        ws.worker_thread = worker
        ws._worker_running = True
        ws._lock = threading.RLock()
        mgr.get.return_value = ws
        return captured

    def test_busy_queue_carries_ordered_attachment_ids(self, app_client):
        client, mgr = app_client
        captured = self._wire_busy_ws(mgr, "ws-A")
        a = _upload(client, "ws-A", "userA", "a.md", b"A", "text/markdown")
        b = _upload(client, "ws-A", "userA", "b.md", b"B", "text/markdown")

        resp = client.post(
            "/v1/api/workstreams/ws-A/send",
            json={
                "message": "ping",
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


class TestBusyWorkerAttachments:
    """An attachment-bearing send to a busy worker can't ride the text-only
    queue seam — it returns ``attachments_busy`` and the staged bytes stay in
    the buffer (a peek, not a drain) so the client can retry once idle."""

    def _wire_busy_ws(self, mgr, ws_id: str):
        """Mock ws whose worker is always alive (forces the queue path)."""
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

    def test_send_with_attachments_to_busy_worker_returns_attachments_busy(self, app_client):
        client, mgr = app_client
        aid = _upload(client, "ws-A", "userA", "x.md", b"X", "text/markdown")
        self._wire_busy_ws(mgr, "ws-A")
        resp = client.post(
            "/v1/api/workstreams/ws-A/send",
            json={"message": "with file", "attachment_ids": [aid]},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "attachments_busy"
        assert body["attached_ids"] == []
        assert body["dropped_attachment_ids"] == [aid]
        # The staged upload was peeked (not drained), so it's still pending
        # and visible for a retry once the worker idles.
        resp = client.get("/v1/api/workstreams/ws-A/attachments", headers=_auth("userA"))
        ids = [a["attachment_id"] for a in resp.json()["attachments"]]
        assert aid in ids


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

        # Upload to ws-A (owned by userA) as service — the staged upload is
        # filed under the owner "userA" (the resolver's uid), not "svc-bot",
        # so the later owner-resolved send can find it in the buffer.
        resp = client.post(
            "/v1/api/workstreams/ws-A/attachments",
            files={"file": ("svc.md", b"svc", "text/markdown")},
            headers=svc_headers,
        )
        assert resp.status_code == 200
        aid = resp.json()["attachment_id"]

        from turnstone.core.attachment_buffer import get_attachment_buffer

        # Staged under the owner uid (userA), not the service caller (svc-bot).
        assert get_attachment_buffer().get(aid, ws_id="ws-A", user_id="userA") is not None
        assert get_attachment_buffer().get(aid, ws_id="ws-A", user_id="svc-bot") is None

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
            "/v1/api/workstreams/ws-A/send",
            json={"message": "svc send", "attachment_ids": [aid]},
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


# ---------------------------------------------------------------------------
# Voice I/O (STT / TTS) endpoints
# ---------------------------------------------------------------------------


class _VoiceConfigStore:
    def __init__(self, **values: str) -> None:
        self._values = dict(values)

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)


@pytest.fixture
def voice_app_client(tmp_path):
    """App wired with an audio-capable registry alias + a mocked OpenAI client.

    The mock is injected into ``registry._clients`` so the real endpoint →
    resolve_role_alias → transcribe/synthesize path runs end-to-end with only
    the SDK network call stubbed.
    """
    import sqlalchemy as sa

    import turnstone.server as srv_mod
    from turnstone.core.memory import register_workstream
    from turnstone.core.metrics import MetricsCollector
    from turnstone.core.model_registry import ModelConfig, ModelRegistry
    from turnstone.core.storage import init_storage, reset_storage
    from turnstone.core.storage._registry import get_storage
    from turnstone.core.storage._schema import workstreams as ws_tbl

    db_path = tmp_path / "voice.db"
    reset_storage()
    init_storage("sqlite", path=str(db_path), run_migrations=False)

    srv_mod._metrics = MetricsCollector()
    srv_mod._metrics.model = "test-model"

    register_workstream("ws-A", name="A")
    with get_storage()._conn() as conn:
        conn.execute(sa.update(ws_tbl).where(ws_tbl.c.ws_id == "ws-A").values(user_id="userA"))
        conn.commit()

    registry = ModelRegistry(
        models={
            "voice": ModelConfig(
                "voice",
                "http://localhost:9/v1",
                "none",
                "gpt-4o-mini-tts",
                capabilities={
                    "supports_transcription": True,
                    "supports_speech_synthesis": True,
                },
            ),
        },
        default="voice",
    )
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = MagicMock(text="hello from speech")
    speech = MagicMock()
    speech.read.return_value = b"RIFF\x00\x00fakeaudio"
    mock_client.audio.speech.create.return_value = speech
    registry._clients["voice"] = mock_client  # bypass real SDK client construction

    config_store = _VoiceConfigStore(
        **{
            "audio.stt_model_alias": "voice",
            "audio.tts_model_alias": "voice",
            "audio.tts_voice": "alloy",
        }
    )

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
        registry=registry,
        config_store=config_store,
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, mock_client
    finally:
        client.close()
        reset_storage()


class TestSpeechToText:
    def test_unconfigured_returns_503(self, app_client):
        client, _ = app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/speech-to-text",
            files={"audio": ("speech.webm", b"RIFFfake", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["error"]

    def test_happy_path_returns_transcript(self, voice_app_client):
        client, mock_client = voice_app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/speech-to-text",
            files={"audio": ("speech.webm", b"RIFFfake", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["transcript"] == "hello from speech"
        assert body["model_alias"] == "voice"
        assert mock_client.audio.transcriptions.create.called

    def test_empty_upload_returns_400(self, voice_app_client):
        client, _ = voice_app_client
        resp = client.post(
            "/v1/api/workstreams/ws-A/speech-to-text",
            files={"audio": ("speech.webm", b"", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 400

    def test_silence_returns_422(self, voice_app_client):
        # A successful transcription with no speech is not a backend failure.
        client, mock_client = voice_app_client
        mock_client.audio.transcriptions.create.return_value = MagicMock(text="   ")
        resp = client.post(
            "/v1/api/workstreams/ws-A/speech-to-text",
            files={"audio": ("speech.webm", b"RIFFfake", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 422
        assert "No speech detected" in resp.json()["error"]

    def test_backend_failure_returns_masked_502(self, voice_app_client):
        # Backend SDK error detail must not leak into the client-facing body.
        client, mock_client = voice_app_client
        mock_client.audio.transcriptions.create.side_effect = RuntimeError(
            "Error code: 401 - internal-host:9 invalid_api_key"
        )
        resp = client.post(
            "/v1/api/workstreams/ws-A/speech-to-text",
            files={"audio": ("speech.webm", b"RIFFfake", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"] == "Speech transcription backend failed"
        assert "internal-host" not in body["error"]

    def test_unknown_workstream_404(self, voice_app_client):
        # Trusted-team semantics: ownership isn't row-enforced, but a
        # nonexistent workstream is masked as 404 (no enumeration).
        client, _ = voice_app_client
        resp = client.post(
            "/v1/api/workstreams/ws-DOES-NOT-EXIST/speech-to-text",
            files={"audio": ("speech.webm", b"RIFFfake", "audio/webm")},
            headers=_auth("userA"),
        )
        assert resp.status_code == 404


class TestTextToSpeech:
    def test_unconfigured_returns_503(self, app_client):
        client, _ = app_client
        resp = client.post("/v1/api/tts", json={"text": "hello"}, headers=_auth("userA"))
        assert resp.status_code == 503

    def test_happy_path_returns_audio(self, voice_app_client):
        client, mock_client = voice_app_client
        resp = client.post("/v1/api/tts", json={"text": "hello"}, headers=_auth("userA"))
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("audio/")
        assert resp.content == b"RIFF\x00\x00fakeaudio"
        assert resp.headers.get("x-model-alias") == "voice"
        # audio.tts_voice setting supplies the voice when the body omits one.
        assert mock_client.audio.speech.create.call_args.kwargs["voice"] == "alloy"

    def test_empty_text_returns_400(self, voice_app_client):
        client, _ = voice_app_client
        resp = client.post("/v1/api/tts", json={"text": "   "}, headers=_auth("userA"))
        assert resp.status_code == 400

    def test_too_long_text_returns_400(self, voice_app_client):
        client, _ = voice_app_client
        resp = client.post("/v1/api/tts", json={"text": "x" * 9000}, headers=_auth("userA"))
        assert resp.status_code == 400

    def test_backend_failure_returns_masked_502(self, voice_app_client):
        client, mock_client = voice_app_client
        mock_client.audio.speech.create.side_effect = RuntimeError(
            "Error code: 500 - internal-host:9 boom"
        )
        resp = client.post("/v1/api/tts", json={"text": "hello"}, headers=_auth("userA"))
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"] == "Speech synthesis backend failed"
        assert "internal-host" not in body["error"]


# ---------------------------------------------------------------------------
# GET /preview — the renderable serving route (preview pane)
# ---------------------------------------------------------------------------


def _seed_committed(ws_id: str, kind: str, mime: str, body: bytes, filename: str) -> str:
    """Commit a blob the way the open_preview fold does: content-addressed
    save + a tool row whose ref-list names it (the serving ownership gate)."""
    import hashlib

    from turnstone.core.memory import save_attachment, save_message, set_message_attachments

    aid = hashlib.sha256(body).hexdigest()
    save_attachment(aid, filename, mime, len(body), kind, body, "tool")
    row_id = save_message(ws_id, "tool", "Preview shown", "open_preview", tool_call_id="c1")
    assert row_id is not None
    set_message_attachments(ws_id, row_id, [aid])
    return aid


class TestGetPreview:
    def test_html_served_renderable_with_bare_sandbox_csp(self, app_client):
        client, _ = app_client
        body = b'<html><head><base href="https://acme.com/"></head><body>x</body></html>'
        aid = _seed_committed("ws-A", "preview", "text/html; charset=utf-8", body, "preview-web")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.content == body
        # Renderable but locked down: bare sandbox (no default-src 'none' —
        # the page's own subresources must load), nosniff, inline, no-store.
        assert resp.headers.get("content-security-policy") == "sandbox"
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("content-disposition", "").startswith("inline;")
        assert resp.headers.get("cache-control") == "private, no-store"

    def test_pdf_served_without_csp(self, app_client):
        client, _ = app_client
        aid = _seed_committed("ws-A", "preview", "application/pdf", b"%PDF-1.4 x", "d.pdf")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        # Chromium's viewer refuses sandboxed contexts — the route omits CSP.
        assert "content-security-policy" not in resp.headers

    def test_image_keeps_full_csp(self, app_client):
        client, _ = app_client
        aid = _seed_committed("ws-A", "preview", "image/png", PNG_1x1, "chart.png")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert "default-src 'none'" in resp.headers.get("content-security-policy", "")

    def test_non_renderable_mime_415(self, app_client):
        client, _ = app_client
        aid = _seed_committed("ws-A", "audio", "audio/wav", WAV_12, "a.wav")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 415

    def test_uploaded_attachment_also_previews(self, app_client):
        # An UPLOADED image (committed via the normal user lane) renders
        # through /preview too — the pane serves attachment: targets.
        client, _ = app_client
        aid = _seed_committed("ws-A", "image", "image/png", PNG_1x1, "up.png")
        resp = client.get(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")

    def test_unreferenced_id_404(self, app_client):
        client, _ = app_client
        aid = _seed_committed("ws-A", "preview", "text/html", b"<p>x</p>", "p")
        resp = client.get(
            f"/v1/api/workstreams/ws-B/attachments/{aid}/preview",
            headers=_auth("userB"),
        )
        assert resp.status_code == 404

    def test_head_preflight_supported(self, app_client):
        # The pane preflights src-loaded kinds with HEAD (the persist race);
        # Starlette derives HEAD from the GET route.
        client, _ = app_client
        aid = _seed_committed("ws-A", "preview", "text/html", b"<p>x</p>", "p")
        resp = client.head(
            f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
            headers=_auth("userA"),
        )
        assert resp.status_code == 200
