"""End-to-end coverage for the ``open_preview`` tool wiring.

Spans the seams the preview descriptor rides: preparer validation +
approval posture, executor target resolution (mocked ``httpx`` for URLs,
tmp files for paths, monkeypatched storage for attachments), the
``_tool_previews`` side channel + live SSE event, the ``Turn.meta``
round-trip, the ``/history`` projection, the storage reconstruct routing,
and the auth scope of the serving route.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from turnstone.core.session import ChatSession
from turnstone.core.trajectory import Role, turn_from_dict, turn_to_dict

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _RecordingUI:
    """SessionUI double that records tool_result calls (kwargs included)."""

    def __init__(self):
        self.tool_results = []

    def __getattr__(self, name):
        # Every other SessionUI hook is an inert no-op.
        def _noop(*args, **kwargs):
            return None

        return _noop

    def on_tool_result(self, call_id, name, output, **kwargs):
        self.tool_results.append((call_id, name, output, kwargs))


def _make_session(**kwargs):
    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=_RecordingUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=5,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _fake_response(url, body, content_type):
    import httpx

    resp = SimpleNamespace()
    # A real httpx.URL so the executor's userinfo-strip path runs unmocked.
    resp.url = httpx.URL(url)
    resp.content = body
    resp.text = body.decode("utf-8", errors="replace")
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = lambda: None
    return resp


# ---------------------------------------------------------------------------
# Preparer
# ---------------------------------------------------------------------------


class TestPrepareOpenPreview:
    def test_missing_target_errors(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {})
        assert item["error"].startswith("Error: missing target")

    def test_invalid_kind_errors(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "a.txt", "kind": "hologram"})
        assert "kind must be one of" in item["error"]

    def test_url_target_needs_approval(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "https://example.com/x"})
        assert item["needs_approval"] is True
        assert item["target_kind"] == "url"
        assert item["approval_label"] == "open_preview"
        assert "error" not in item

    def test_private_url_blocked_pre_approval(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "http://169.254.169.254/meta"})
        assert "error" in item
        assert item["needs_approval"] is False

    def test_path_target_runs_unprompted(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "~/notes.md"})
        assert item["needs_approval"] is False
        assert item["target_kind"] == "path"
        assert not item["path"].startswith("~")

    def test_attachment_target(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "attachment:abc123"})
        assert item["needs_approval"] is False
        assert item["target_kind"] == "attachment"
        assert item["attachment_id"] == "abc123"
        empty = s._prepare_open_preview("c1", {"target": "attachment:"})
        assert "error" in empty


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class TestExecOpenPreview:
    def test_url_html_builds_web_descriptor(self, monkeypatch):
        s = _make_session()
        body = b"<html><head><title>Acme Pricing</title></head><body>x</body></html>"
        monkeypatch.setattr(
            "turnstone.core.session.fetch_with_ssrf_guard",
            lambda url, **kw: _fake_response(url, body, "text/html; charset=utf-8"),
        )
        item = s._prepare_open_preview("c1", {"target": "https://acme.com/pricing"})
        call_id, msg = s._exec_open_preview(item)
        assert call_id == "c1"
        assert "Acme Pricing" in msg
        descriptor, att = s._tool_previews["c1"]
        assert descriptor["kind"] == "web"
        assert descriptor["title"] == "Acme Pricing"
        assert descriptor["source"] == "https://acme.com/pricing"
        assert descriptor["content_type"].startswith("text/html")
        assert att.kind == "preview"
        # The stored bytes gained a base for relative-asset resolution.
        assert b'<base href="https://acme.com/pricing">' in att.content
        # The live event carried the descriptor.
        results = s.ui.tool_results
        assert results and results[-1][3].get("preview") == descriptor

    def test_url_userinfo_stripped_from_descriptor(self, monkeypatch):
        s = _make_session()
        body = b"<html><head></head><body>x</body></html>"
        monkeypatch.setattr(
            "turnstone.core.session.fetch_with_ssrf_guard",
            lambda url, **kw: _fake_response(url, body, "text/html"),
        )
        item = s._prepare_open_preview("c1", {"target": "https://user:sekret@acme.com/page"})
        s._exec_open_preview(item)
        descriptor, att = s._tool_previews["c1"]
        assert "sekret" not in descriptor["source"]
        assert "sekret" not in descriptor["title"]
        assert b"sekret" not in att.content  # the injected <base href>

    def test_redirect_into_private_space_blocked(self, monkeypatch):
        s = _make_session()

        # The guarded fetch raises BEFORE requesting a private hop — the
        # executor's ValueError lane turns that into a tool error.
        def _blocked(url, **kw):
            raise ValueError("Blocked: URL resolves to private/internal address (169.254.169.254)")

        monkeypatch.setattr("turnstone.core.session.fetch_with_ssrf_guard", _blocked)
        item = s._prepare_open_preview("c1", {"target": "https://innocent.example/"})
        _, msg = s._exec_open_preview(item)
        assert msg.startswith("Error: fetch failed: Blocked")
        assert "c1" not in s._tool_previews

    def test_oversized_web_content_errors(self, monkeypatch):
        s = _make_session()
        big = b"<html>" + b"x" * (4 * 1024 * 1024 + 16) + b"</html>"
        monkeypatch.setattr(
            "turnstone.core.session.fetch_with_ssrf_guard",
            lambda url, **kw: _fake_response(url, big, "text/html"),
        )
        item = s._prepare_open_preview("c1", {"target": "https://example.com/big"})
        _, msg = s._exec_open_preview(item)
        assert msg.startswith("Error:")
        assert "too large" in msg

    def test_path_image(self, tmp_path):
        s = _make_session()
        p = tmp_path / "chart.png"
        p.write_bytes(PNG_1x1)
        item = s._prepare_open_preview("c1", {"target": str(p)})
        _, msg = s._exec_open_preview(item)
        assert not msg.startswith("Error:")
        descriptor, att = s._tool_previews["c1"]
        assert descriptor["kind"] == "image"
        assert descriptor["content_type"] == "image/png"
        assert descriptor["title"] == "chart.png"
        assert att.content == PNG_1x1

    def test_path_csv_is_table(self, tmp_path):
        s = _make_session()
        p = tmp_path / "results.csv"
        p.write_text("name,score\na,1\nb,2\n")
        item = s._prepare_open_preview("c1", {"target": str(p)})
        s._exec_open_preview(item)
        descriptor, _ = s._tool_previews["c1"]
        assert descriptor["kind"] == "table"
        assert descriptor["content_type"].startswith("text/csv")

    def test_path_missing_errors(self):
        s = _make_session()
        item = s._prepare_open_preview("c1", {"target": "/nonexistent/nowhere.txt"})
        _, msg = s._exec_open_preview(item)
        assert msg.startswith("Error: file not found")

    def test_path_binary_unpreviewable(self, tmp_path):
        s = _make_session()
        p = tmp_path / "blob.bin"
        p.write_bytes(b"\x00\x01\x02\x03" * 64)
        item = s._prepare_open_preview("c1", {"target": str(p)})
        _, msg = s._exec_open_preview(item)
        assert "not previewable" in msg

    def test_attachment_target_requires_ws_reference(self, monkeypatch):
        s = _make_session(ws_id="ws-1")
        monkeypatch.setattr(
            "turnstone.core.memory.get_attachment",
            lambda aid: {"content": b"# doc", "mime_type": "text/markdown", "filename": "d.md"},
        )
        monkeypatch.setattr(
            "turnstone.core.memory.attachment_referenced_in_ws",
            lambda aid, ws: False,
        )
        item = s._prepare_open_preview("c1", {"target": "attachment:deadbeef"})
        _, msg = s._exec_open_preview(item)
        assert msg.startswith("Error: attachment not found")

    def test_attachment_target_happy_path(self, monkeypatch):
        s = _make_session(ws_id="ws-1")
        monkeypatch.setattr(
            "turnstone.core.memory.get_attachment",
            lambda aid: {"content": b"# doc", "mime_type": "text/markdown", "filename": "d.md"},
        )
        monkeypatch.setattr(
            "turnstone.core.memory.attachment_referenced_in_ws",
            lambda aid, ws: True,
        )
        item = s._prepare_open_preview("c1", {"target": "attachment:deadbeef"})
        _, msg = s._exec_open_preview(item)
        assert not msg.startswith("Error:")
        descriptor, _ = s._tool_previews["c1"]
        assert descriptor["kind"] == "markdown"
        assert descriptor["title"] == "d.md"

    def test_title_override_wins(self, tmp_path):
        s = _make_session()
        p = tmp_path / "x.csv"
        p.write_text("a,b\n")
        item = s._prepare_open_preview("c1", {"target": str(p), "title": "Q3 numbers"})
        s._exec_open_preview(item)
        descriptor, _ = s._tool_previews["c1"]
        assert descriptor["title"] == "Q3 numbers"


# ---------------------------------------------------------------------------
# Trajectory / history / storage seams
# ---------------------------------------------------------------------------


class TestDescriptorSeams:
    DESCRIPTOR = {
        "kind": "web",
        "title": "T",
        "source": "https://a.io",
        "attachment_id": "abc",
        "content_type": "text/html; charset=utf-8",
        "size": 7,
    }

    def test_turn_roundtrip(self):
        turn = turn_from_dict(
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "Preview shown",
                "_preview": self.DESCRIPTOR,
            }
        )
        assert turn.meta.extra["preview"] == self.DESCRIPTOR
        out = turn_to_dict(turn)
        assert out["_preview"] == self.DESCRIPTOR

    def test_history_projection_carries_preview(self):
        from turnstone.core.history_decoration import project_history_messages

        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "open_preview", "arguments": "{}"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "Preview shown to the user: T (web, 7 bytes)",
                "_preview": self.DESCRIPTOR,
            },
        ]
        history = project_history_messages(msgs)
        tool_entries = [h for h in history if h.get("role") == "tool"]
        assert tool_entries and tool_entries[0]["preview"] == self.DESCRIPTOR

    def test_reconstruct_routes_tool_preview_meta(self):
        import json

        from turnstone.core.storage._utils import reconstruct_turns

        # Row layout per reconstruct_turns' unpack: (row_id, role, content,
        # tool_name, tool_call_id, provider_data, tool_calls_json, source,
        # event_id, is_error, meta).
        row = (
            1,
            "tool",
            "ok",
            "open_preview",
            "c1",
            None,
            None,
            None,
            7,
            0,
            json.dumps({"effect_status": "unknown", "preview": self.DESCRIPTOR}),
        )
        turns = reconstruct_turns([row], "ws-1", attachments_by_msg={})
        assert turns[0].role is Role.TOOL
        assert turns[0].meta.extra["preview"] == self.DESCRIPTOR
        assert turns[0].meta.extra["effect_status"] == "unknown"

    def test_reconstruct_skips_preview_blob_refs(self):
        """A preview blob on a tool row's ref-list must NOT become a content
        block — it is meta-addressed frontend content, and a content block
        would be materialized onto the wire on reload."""
        from turnstone.core.storage._utils import reconstruct_turns

        row = (
            1,
            "tool",
            "ok",
            "open_preview",
            "c1",
            None,
            None,
            None,
            None,
            0,
            None,
        )
        atts = {
            1: [
                {
                    "attachment_id": "abc",
                    "kind": "preview",
                    "filename": "preview-web",
                    "mime_type": "text/html; charset=utf-8",
                    "size_bytes": 7,
                },
                {
                    "attachment_id": "img1",
                    "kind": "image",
                    "filename": "shot.png",
                    "mime_type": "image/png",
                    "size_bytes": 9,
                },
            ]
        }
        turns = reconstruct_turns([row], "ws-1", attachments_by_msg=atts)
        kinds = [b.kind for b in turns[0].content if b.__class__.__name__ == "AttachmentRef"]
        # The vision lane still reconstructs; the preview blob does not.
        assert kinds == ["image"]

    def test_preview_route_scope_is_read(self):
        from turnstone.core.auth import required_scope

        assert required_scope("GET", "/v1/api/workstreams/ws1/attachments/abc/preview") == "read"
        assert (
            required_scope("GET", "/node/n1/v1/api/workstreams/ws1/attachments/abc/preview")
            == "read"
        )


# ---------------------------------------------------------------------------
# fetch_with_ssrf_guard — per-hop redirect screening (core/web.py)
# ---------------------------------------------------------------------------


class _FakeHop:
    def __init__(self, status, headers=None, url=""):
        self.status_code = status
        self.headers = headers or {}
        self.url = url


class _FakeClient:
    """httpx.Client double: serves a scripted {url: response} table."""

    calls: list[str] = []
    table: dict[str, _FakeHop] = {}

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        _FakeClient.calls.append(url)
        return _FakeClient.table[url]


class TestFetchWithSsrfGuard:
    def _wire(self, monkeypatch, table):
        _FakeClient.calls = []
        _FakeClient.table = table
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)

    def test_follows_public_redirect_chain(self, monkeypatch):
        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {
                "https://a.example/": _FakeHop(302, {"location": "https://b.example/x"}),
                "https://b.example/x": _FakeHop(200, {}, url="https://b.example/x"),
            },
        )
        monkeypatch.setattr("turnstone.core.web.check_ssrf", lambda url: None)
        resp = fetch_with_ssrf_guard("https://a.example/", timeout=5)
        assert resp.status_code == 200
        assert _FakeClient.calls == ["https://a.example/", "https://b.example/x"]

    def test_private_hop_blocked_before_request(self, monkeypatch):
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {
                "https://a.example/": _FakeHop(302, {"location": "http://169.254.169.254/latest"}),
            },
        )
        blocked = {"http://169.254.169.254/latest": "Blocked: private"}
        monkeypatch.setattr("turnstone.core.web.check_ssrf", lambda url: blocked.get(url))
        with pytest.raises(ValueError, match="Blocked: private"):
            fetch_with_ssrf_guard("https://a.example/", timeout=5)
        # The load-bearing assertion: the private hop was NEVER requested.
        assert _FakeClient.calls == ["https://a.example/"]

    def test_relative_location_resolves_against_current(self, monkeypatch):
        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {
                "https://a.example/start": _FakeHop(301, {"location": "/moved"}),
                "https://a.example/moved": _FakeHop(200, {}, url="https://a.example/moved"),
            },
        )
        monkeypatch.setattr("turnstone.core.web.check_ssrf", lambda url: None)
        resp = fetch_with_ssrf_guard("https://a.example/start", timeout=5)
        assert resp.status_code == 200

    def test_redirect_loop_capped(self, monkeypatch):
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {"https://a.example/": _FakeHop(302, {"location": "https://a.example/"})},
        )
        monkeypatch.setattr("turnstone.core.web.check_ssrf", lambda url: None)
        with pytest.raises(ValueError, match="redirects"):
            fetch_with_ssrf_guard("https://a.example/", timeout=5)


# ---------------------------------------------------------------------------
# Cancelled-batch synthesis — a staged preview whose descriptor already
# reached the frontend must commit, not vanish (session.py review fix)
# ---------------------------------------------------------------------------


class TestCancelledBatchPreservesPreview:
    def test_synthesize_commits_staged_preview(self, monkeypatch):
        import json as _json

        from turnstone.core.attachments import Attachment
        from turnstone.core.trajectory import Turn

        s = _make_session(ws_id="ws-1")
        descriptor = {
            "kind": "web",
            "title": "T",
            "source": "https://a.io",
            "attachment_id": "abc",
            "content_type": "text/html; charset=utf-8",
            "size": 7,
        }
        att = Attachment(
            attachment_id="abc",
            filename="preview-web",
            mime_type="text/html; charset=utf-8",
            kind="preview",
            content=b"<p>x</p>",
        )
        s._tool_previews["c1"] = (descriptor, att)
        # Assistant turn with one UNANSWERED call — the cancel shape.
        s.messages.append(
            turn_from_dict(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "open_preview", "arguments": "{}"},
                        }
                    ],
                }
            )
        )
        s._msg_tokens.append(1)

        saved = {}
        monkeypatch.setattr(
            "turnstone.core.session.save_message",
            lambda ws, role, content, name, **kw: (
                saved.update({"meta": kw.get("meta"), "row": 42}) or 42
            ),
        )
        persisted = {}
        monkeypatch.setattr(
            ChatSession,
            "_persist_attachment_refs",
            lambda self, row_id, atts, origin="upload": persisted.update(
                {"row": row_id, "ids": [a.attachment_id for a in atts], "origin": origin}
            ),
        )

        s._synthesize_cancelled_results("Cancelled by user.")

        # Side channel drained; descriptor + blob committed with the turn.
        assert "c1" not in s._tool_previews
        meta = _json.loads(saved["meta"])
        assert meta["preview"] == descriptor
        assert meta["effect_status"] == "unknown"
        assert persisted == {"row": 42, "ids": ["abc"], "origin": "tool"}
        # The in-memory synthesized turn carries the descriptor too.
        tool_turns = [t for t in s.messages if isinstance(t, Turn) and t.role is Role.TOOL]
        assert tool_turns and tool_turns[-1].meta.extra.get("preview") == descriptor
