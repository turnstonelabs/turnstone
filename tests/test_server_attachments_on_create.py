"""Tests for the multipart variant of POST /v1/api/workstreams/new.

Exercises:
- The pure helpers ``validate_and_save_uploaded_files`` (stages to the
  per-node buffer) and ``resolve_staged_attachments`` (peeks them back).
- The full create endpoint via TestClient with a FakeSession factory so
  the initial-message dispatch thread runs end-to-end without an LLM.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest
from starlette.testclient import TestClient

# Magic-byte-valid 1x1 PNG (matches the fixture in test_server_attachments_endpoints.py)
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
        # ``workstreams.create`` is now a real gate on POST /workstreams/new
        # — see PR adding 057_role_permission_overrides.  Embed the perm so
        # the multipart-create flow under test stays exercising the create
        # path and not the new 403.
        permissions=frozenset({"workstreams.create"}),
    )


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_jwt(user)}"}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestValidateAndSaveUploadedFiles:
    def test_stages_image_and_text_to_buffer(self, tmp_path):
        import hashlib

        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.attachments import (
            validate_and_save_uploaded_files as _validate_and_save_uploaded_files,
        )

        buf = get_attachment_buffer()
        buf._entries.clear()
        try:
            files = [
                ("hi.png", "image/png", PNG_1x1),
                ("notes.md", "text/markdown", b"# Hello\n"),
            ]
            ids, err = _validate_and_save_uploaded_files(files, "ws-X", "userA")
            assert err is None
            assert len(ids) == 2
            # Ids are content hashes (content-addressed staging).
            assert ids[0] == hashlib.sha256(PNG_1x1).hexdigest()
            staged = buf.list_for(ws_id="ws-X", user_id="userA")
            assert len(staged) == 2
            assert {s.kind for s in staged} == {"image", "text"}
        finally:
            buf._entries.clear()

    def test_rejects_oversized_image(self, tmp_path):
        from turnstone.core.attachments import IMAGE_SIZE_CAP
        from turnstone.core.attachments import (
            validate_and_save_uploaded_files as _validate_and_save_uploaded_files,
        )
        from turnstone.core.storage import init_storage, reset_storage

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            # Magic-byte-valid PNG header padded past the cap.
            oversized = PNG_1x1 + b"\x00" * (IMAGE_SIZE_CAP + 1)
            files = [("big.png", "image/png", oversized)]
            ids, err = _validate_and_save_uploaded_files(files, "ws-X", "userA")
            assert err is not None
            assert err.status_code == 413
            assert ids == []
        finally:
            reset_storage()

    def test_rejects_unsupported_text(self, tmp_path):
        from turnstone.core.attachments import (
            validate_and_save_uploaded_files as _validate_and_save_uploaded_files,
        )
        from turnstone.core.storage import init_storage, reset_storage

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            # No image magic, MIME isn't text/*, extension not allowlisted
            files = [("evil.bin", "application/octet-stream", b"\x00\x01\x02")]
            ids, err = _validate_and_save_uploaded_files(files, "ws-X", "userA")
            assert err is not None
            assert err.status_code == 400
            assert ids == []
        finally:
            reset_storage()


# (The per-user pending-upload cap was removed with the content-addressing
# cutover; ``validate_and_save_uploaded_files`` no longer 409s — the buffer's
# own size/TTL ceilings bound a flood.)


class TestResolveStagedAttachments:
    def _stage(self, ws_id, user_id, filename, mime, kind, content):
        from turnstone.core.attachment_buffer import get_attachment_buffer

        return get_attachment_buffer().stage(
            ws_id=ws_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime,
            kind=kind,
            content=content,
        )

    def test_resolves_staged_to_attachments(self):
        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.attachments import Attachment
        from turnstone.core.attachments import (
            resolve_staged_attachments as _resolve_staged_attachments,
        )

        buf = get_attachment_buffer()
        buf._entries.clear()
        try:
            a1 = self._stage("ws-X", "userA", "a.txt", "text/plain", "text", b"hello")
            a2 = self._stage("ws-X", "userA", "b.png", "image/png", "image", PNG_1x1)
            resolved, taken, dropped = _resolve_staged_attachments(
                [a1.attachment_id, a2.attachment_id], "ws-X", "userA"
            )
            assert taken == [a1.attachment_id, a2.attachment_id]
            assert dropped == []
            assert len(resolved) == 2
            assert all(isinstance(a, Attachment) for a in resolved)
            assert [a.kind for a in resolved] == ["text", "image"]
        finally:
            buf._entries.clear()

    def test_unknown_id_is_dropped(self):
        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.attachments import (
            resolve_staged_attachments as _resolve_staged_attachments,
        )

        buf = get_attachment_buffer()
        buf._entries.clear()
        try:
            a1 = self._stage("ws-X", "userA", "a.txt", "text/plain", "text", b"hello")
            resolved, taken, dropped = _resolve_staged_attachments(
                [a1.attachment_id, "never-staged"], "ws-X", "userA"
            )
            assert taken == [a1.attachment_id]
            assert dropped == ["never-staged"]
            assert len(resolved) == 1
        finally:
            buf._entries.clear()

    def test_cross_user_id_not_resolved(self):
        # A staged id scoped to another user (same ws) must not resolve.
        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.attachments import (
            resolve_staged_attachments as _resolve_staged_attachments,
        )

        buf = get_attachment_buffer()
        buf._entries.clear()
        try:
            other = self._stage("ws-X", "userB", "a.txt", "text/plain", "text", b"hello")
            resolved, taken, dropped = _resolve_staged_attachments(
                [other.attachment_id], "ws-X", "userA"
            )
            assert resolved == []
            assert dropped == [other.attachment_id]
        finally:
            buf._entries.clear()


# ---------------------------------------------------------------------------
# End-to-end create endpoint tests (multipart variant)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in: records send() invocations from the dispatch thread.

    Knows its own ``ws_id`` and ``user_id`` so it can faithfully simulate the
    real ChatSession's attachment-consume step against storage.  Tests then
    assert that pending attachments are gone after dispatch.
    """

    def __init__(self, ws_id: str = "", user_id: str = ""):
        self.ws_id = ws_id
        self.user_id = user_id
        self.model = "test-model"
        self.model_alias = "test-model"
        self.messages = []
        self.sends: list[tuple[str, list, str | None]] = []
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self.notify_targets = ""
        self._notify_on_complete = "[]"

    def send(self, text, attachments=None, send_id=None):
        with self._lock:
            self.sends.append((text, list(attachments or []), send_id))
            # Simulate the real ChatSession commit: write each attachment
            # content-addressed, record the ref-list on a (synthetic) message
            # row, and drain the staged handles from the buffer — so callers
            # can assert the lifecycle landed.
            if attachments and self.ws_id and self.user_id:
                from turnstone.core.attachment_buffer import get_attachment_buffer
                from turnstone.core.memory import (
                    save_attachment,
                    save_message,
                    set_message_attachments,
                )

                mid = save_message(self.ws_id, "user", text)
                buf = get_attachment_buffer()
                ref_ids = []
                for a in attachments:
                    save_attachment(
                        a.attachment_id,
                        self.ws_id,
                        self.user_id,
                        a.filename,
                        a.mime_type,
                        len(a.content),
                        a.kind,
                        a.content,
                    )
                    ref_ids.append(a.attachment_id)
                    buf.discard(a.attachment_id, ws_id=self.ws_id, user_id=self.user_id)
                set_message_attachments(self.ws_id, mid, ref_ids)

    # Methods the create handler may call but we don't care about
    def set_watch_runner(self, *_a, **_kw):
        pass

    def queue_message(self, *_a, **_kw):
        return ("", "normal", "msg-x")

    def request_title_refresh(self, *_a, **_kw):
        pass

    def resume(self, *_a, **_kw):
        return False


class _FakeUI:
    def __init__(self, ws_id="", user_id=""):
        self.ws_id = ws_id
        self._user_id = user_id
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
        self.events: list[dict] = []
        self._enqueued: list[dict] = []

    def _enqueue(self, ev):
        self._enqueued.append(ev)

    def on_stream_end(self):
        pass

    def on_state_change(self, state):
        self.events.append({"type": "state_change", "state": state})

    def on_error(self, msg):
        self.events.append({"type": "error", "message": msg})


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """End-to-end app with a fake session factory + SessionManager."""
    from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
    from turnstone.core.metrics import MetricsCollector
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.storage import get_storage, init_storage, reset_storage
    from turnstone.server import WebUI, create_app

    reset_storage()
    init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)

    metrics = MetricsCollector()
    metrics.model = "test-model"
    monkeypatch.setattr("turnstone.server._metrics", metrics)
    # Replace WebUI with our fake so the create handler's isinstance check passes.
    monkeypatch.setattr("turnstone.server.WebUI", _FakeUI)

    fake_sessions: list[_FakeSession] = []

    def _factory(ui, _model, ws_id, **_kw):
        # The user_id rides on the WebUI factory closure; pull it off the
        # ui instance so the FakeSession's consume step uses the right scope.
        user_id = getattr(ui, "_user_id", "")
        s = _FakeSession(ws_id=ws_id, user_id=user_id)
        fake_sessions.append(s)
        return s

    gq: queue.Queue[dict] = queue.Queue(maxsize=1000)
    WebUI._global_queue = gq
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=lambda ws: _FakeUI(
            ws_id=ws.id,
            user_id=ws.user_id,
        ),
        session_factory=_factory,
    )
    mgr = SessionManager(
        adapter, storage=get_storage(), max_active=10, node_id="node-test", event_emitter=adapter
    )

    app = create_app(
        workstreams=mgr,
        global_queue=gq,
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_TEST_JWT_SECRET,
    )
    # Pending uploads live in the process-global per-node buffer; clear it so
    # staged uploads can't leak across tests.
    from turnstone.core.attachment_buffer import get_attachment_buffer

    get_attachment_buffer()._entries.clear()

    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, fake_sessions, gq
    finally:
        client.close()
        get_attachment_buffer()._entries.clear()
        reset_storage()


class TestCreateMultipart:
    def test_create_with_image_and_initial_message(self, app_client):
        import hashlib

        from turnstone.core.attachment_buffer import get_attachment_buffer
        from turnstone.core.memory import attachment_referenced_in_ws, get_attachment

        client, sessions, _gq = app_client
        meta = {"name": "demo", "initial_message": "describe this image"}
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": json.dumps(meta)},
            files=[("file", ("tiny.png", PNG_1x1, "image/png"))],
            headers=_auth("userA"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        ws_id = data["ws_id"]
        assert ws_id
        assert len(data["attachment_ids"]) == 1
        aid = data["attachment_ids"][0]
        assert aid == hashlib.sha256(PNG_1x1).hexdigest()  # content-addressed id

        # Wait briefly for the dispatch thread
        deadline = time.time() + 2.0
        while time.time() < deadline and not sessions:
            time.sleep(0.02)
        deadline = time.time() + 2.0
        while time.time() < deadline and not sessions[0].sends:
            time.sleep(0.02)
        assert sessions
        assert sessions[0].sends, "session.send was not invoked"
        text, atts, send_id = sessions[0].sends[0]
        assert text == "describe this image"
        assert send_id  # tracking token threaded through
        assert len(atts) == 1
        assert atts[0].kind == "image"

        # Lifecycle: the FakeSession commit wrote the blob content-addressed +
        # recorded the ref-list + drained the buffer.  Poll for the worker.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if get_attachment(aid) is not None and attachment_referenced_in_ws(aid, ws_id):
                break
            time.sleep(0.02)
        row = get_attachment(aid)
        assert row is not None and row["refcount"] >= 1
        assert attachment_referenced_in_ws(aid, ws_id) is True
        # Drained from the buffer post-commit.
        assert get_attachment_buffer().get(aid, ws_id=ws_id, user_id="userA") is None

    def test_create_with_attachments_no_initial_message_keeps_staged(self, app_client):
        import hashlib

        from turnstone.core.attachment_buffer import get_attachment_buffer

        client, _, _gq = app_client
        meta = {"name": "stash"}
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": json.dumps(meta)},
            files=[("file", ("notes.md", b"# hello\n", "text/markdown"))],
            headers=_auth("userA"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        ws_id = data["ws_id"]
        assert data["attachment_ids"] == [hashlib.sha256(b"# hello\n").hexdigest()]
        # No initial_message → no send → the upload stays staged in the buffer.
        staged = get_attachment_buffer().list_for(ws_id=ws_id, user_id="userA")
        assert len(staged) == 1
        assert staged[0].filename == "notes.md"

    def test_create_rejects_oversized_image_and_rolls_back(self, app_client):
        from turnstone.core.attachments import IMAGE_SIZE_CAP

        client, _, gq = app_client
        oversized = PNG_1x1 + b"\x00" * (IMAGE_SIZE_CAP + 1)
        meta = {"name": "fails"}
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": json.dumps(meta)},
            files=[("file", ("big.png", oversized, "image/png"))],
            headers=_auth("userA"),
        )
        assert resp.status_code == 413
        # Regression: ws_created must NOT have been emitted for a request
        # that's about to be rejected.  Otherwise SSE consumers see a
        # phantom workstream flash on dashboards.
        events: list[dict] = []
        while not gq.empty():
            events.append(gq.get_nowait())
        kinds = {e.get("type") for e in events}
        assert "ws_created" not in kinds, f"phantom ws_created emitted for failed create: {events}"

    def test_create_missing_meta_returns_400(self, app_client):
        client, _, _gq = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            files=[("file", ("notes.md", b"hello", "text/markdown"))],
            headers=_auth("userA"),
        )
        assert resp.status_code == 400

    def test_create_invalid_meta_json_returns_400(self, app_client):
        client, _, _gq = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": "{not json}"},
            files=[],
            headers=_auth("userA"),
        )
        assert resp.status_code == 400

    def test_attachments_with_resume_ws_returns_400(self, app_client):
        from turnstone.core.memory import register_workstream

        client, _, _gq = app_client
        register_workstream("ws-resume-target", name="resume target")
        meta = {"name": "fork", "resume_ws": "ws-resume-target"}
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": json.dumps(meta)},
            files=[("file", ("notes.md", b"hello", "text/markdown"))],
            headers=_auth("userA"),
        )
        assert resp.status_code == 400


class TestCreateJsonStillWorks:
    """The JSON path must remain byte-for-byte identical (back-compat)."""

    def test_create_json_no_attachments(self, app_client):
        client, _, _gq = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "json-only"},
            headers=_auth("userA"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ws_id"]
        # New optional field, but always emitted (empty list when absent)
        assert data["attachment_ids"] == []
