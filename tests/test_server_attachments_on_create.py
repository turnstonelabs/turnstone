"""Tests for the multipart variant of POST /v1/api/workstreams/new.

Exercises:
- The pure helpers `_validate_and_save_uploaded_files` and
  `_reserve_and_resolve_attachments` (added alongside the multipart path).
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
    )


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_jwt(user)}"}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestValidateAndSaveUploadedFiles:
    def test_saves_image_and_text(self, tmp_path):
        from turnstone.core.memory import list_pending_attachments
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _validate_and_save_uploaded_files

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            files = [
                ("hi.png", "image/png", PNG_1x1),
                ("notes.md", "text/markdown", b"# Hello\n"),
            ]
            ids, err = _validate_and_save_uploaded_files(files, "ws-X", "userA")
            assert err is None
            assert len(ids) == 2
            pending = list_pending_attachments("ws-X", "userA")
            assert len(pending) == 2
            kinds = {p["kind"] for p in pending}
            assert kinds == {"image", "text"}
        finally:
            reset_storage()

    def test_rejects_oversized_image(self, tmp_path):
        from turnstone.core.attachments import IMAGE_SIZE_CAP
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _validate_and_save_uploaded_files

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
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _validate_and_save_uploaded_files

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

    def test_pending_cap_returns_409(self, tmp_path):
        from turnstone.core.attachments import MAX_PENDING_ATTACHMENTS_PER_USER_WS
        from turnstone.core.memory import save_attachment
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _validate_and_save_uploaded_files

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            # Saturate the pending cap
            for i in range(MAX_PENDING_ATTACHMENTS_PER_USER_WS):
                save_attachment(
                    f"pre-{i}", "ws-X", "userA", f"f{i}.txt", "text/plain", 1, "text", b"x"
                )
            files = [("notes.md", "text/markdown", b"hello")]
            ids, err = _validate_and_save_uploaded_files(files, "ws-X", "userA")
            assert err is not None
            assert err.status_code == 409
            assert ids == []
        finally:
            reset_storage()


class TestReserveAndResolveAttachments:
    def test_reserves_and_returns_attachments(self, tmp_path):
        from turnstone.core.attachments import Attachment
        from turnstone.core.memory import save_attachment
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _reserve_and_resolve_attachments

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            save_attachment("a1", "ws-X", "userA", "a.txt", "text/plain", 5, "text", b"hello")
            save_attachment("a2", "ws-X", "userA", "b.png", "image/png", 91, "image", PNG_1x1)
            resolved, ordered, dropped = _reserve_and_resolve_attachments(
                ["a1", "a2"], "send-1", "ws-X", "userA"
            )
            assert ordered == ["a1", "a2"]
            assert dropped == []
            assert len(resolved) == 2
            assert all(isinstance(a, Attachment) for a in resolved)
            kinds = [a.kind for a in resolved]
            assert kinds == ["text", "image"]
        finally:
            reset_storage()

    def test_double_reserve_drops_second(self, tmp_path):
        from turnstone.core.memory import save_attachment
        from turnstone.core.storage import init_storage, reset_storage
        from turnstone.server import _reserve_and_resolve_attachments

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)
        try:
            save_attachment("a1", "ws-X", "userA", "a.txt", "text/plain", 5, "text", b"hello")
            r1, ord1, _ = _reserve_and_resolve_attachments(["a1"], "send-A", "ws-X", "userA")
            assert len(r1) == 1
            r2, ord2, drop2 = _reserve_and_resolve_attachments(["a1"], "send-B", "ws-X", "userA")
            assert r2 == []
            assert ord2 == []
            assert drop2 == ["a1"]
        finally:
            reset_storage()


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
            # Simulate the real ChatSession's consume step against storage
            # so callers can assert the lifecycle landed.
            if attachments and send_id and self.ws_id and self.user_id:
                import uuid as _uuid

                from turnstone.core.memory import mark_attachments_consumed

                ids = [a.attachment_id for a in attachments]
                mark_attachments_consumed(
                    ids,
                    _uuid.uuid4().hex,  # synthetic conversation message id
                    self.ws_id,
                    self.user_id,
                    reserved_for_msg_id=send_id,
                )

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
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, fake_sessions, gq
    finally:
        client.close()
        reset_storage()


class TestCreateMultipart:
    def test_create_with_image_and_initial_message(self, app_client):
        from turnstone.core.memory import list_pending_attachments

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
        assert send_id  # reservation token threaded through
        assert len(atts) == 1
        assert atts[0].kind == "image"

        # Lifecycle: the FakeSession marks them consumed via storage —
        # so the pending-list for this ws should be empty after dispatch.
        assert list_pending_attachments(ws_id, "userA") == []

    def test_create_with_attachments_no_initial_message_keeps_pending(self, app_client):
        from turnstone.core.memory import list_pending_attachments

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
        pending = list_pending_attachments(ws_id, "userA")
        assert len(pending) == 1
        assert pending[0]["filename"] == "notes.md"

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
