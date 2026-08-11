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

import pytest

from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage
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


@pytest.fixture
def _no_network_screen(monkeypatch):
    """Keep prepare-time SSRF screening off the network.

    Opt-in, NOT autouse: as a module-wide fixture it also stubbed the tests
    whose whole point is the screen, so ``test_screen_public_url_passes``
    asserted on the stub and would have passed even if screen_url refused
    every hostname. Request it only where the hostname is incidental.

    Screening fails closed on a resolution failure, so a test naming a
    third-party host (``example.com``) would otherwise depend on live public
    DNS and break on an isolated CI runner. IP literals are passed through to
    the real screen — they resolve locally, and the tests that exercise the
    private/never lanes are written with literals precisely so they exercise
    the real classifier.
    """
    import ipaddress
    from urllib.parse import urlparse

    from turnstone.core import web

    real = web.screen_url
    permissive = _screen_stub()

    def _screen(url):
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return real(url)
        if not host:
            return real(url)  # malformed URLs are the real screen's business
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return permissive(url)
        return real(url)

    monkeypatch.setattr("turnstone.core.session.screen_url", _screen)


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

    def test_url_target_needs_approval(self, _no_network_screen):
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
    def test_url_html_builds_web_descriptor(self, _no_network_screen, monkeypatch):
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

    def test_url_userinfo_stripped_from_descriptor(self, _no_network_screen, monkeypatch):
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

    def test_redirect_into_private_space_blocked(self, _no_network_screen, monkeypatch):
        s = _make_session()

        # The guarded fetch raises BEFORE requesting a private hop — the
        # executor's ValueError lane turns that into a tool error.
        def _blocked(url, **kw):
            raise ValueError("Blocked: URL resolves to private/internal address (169.254.169.254)")

        monkeypatch.setattr("turnstone.core.session.fetch_with_ssrf_guard", _blocked)
        # The prepare-time screen fails closed on an unresolvable host, and
        # innocent.example does not resolve — stub it so this test exercises
        # the executor's ValueError lane rather than the screen.
        monkeypatch.setattr("turnstone.core.session.screen_url", _screen_stub())
        item = s._prepare_open_preview("c1", {"target": "https://innocent.example/"})
        _, msg = s._exec_open_preview(item)
        assert msg.startswith("Error: fetch failed: Blocked")
        assert "c1" not in s._tool_previews

    def test_oversized_web_content_errors(self, _no_network_screen, monkeypatch):
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

    def test_url_pdf_over_10mb_previews_to_kind_cap(self, _no_network_screen, monkeypatch):
        # Review finding (PR #800): a flat 10 MB URL pre-check rejected PDFs
        # the 32 MiB pdf kind cap allows — the fetch ceiling must track the
        # widest kind cap and leave the per-kind caps as the authority.
        from turnstone.core.preview import PREVIEW_SIZE_CAPS

        s = _make_session()
        body = b"%PDF-1.7\n" + b"a" * (12 * 1024 * 1024)
        seen = {}

        def _capture(url, **kw):
            seen.update(kw)
            return _fake_response(url, body, "application/pdf")

        monkeypatch.setattr("turnstone.core.session.fetch_with_ssrf_guard", _capture)
        item = s._prepare_open_preview("c1", {"target": "https://acme.com/report.pdf"})
        _, msg = s._exec_open_preview(item)
        assert not msg.startswith("Error:")
        descriptor, _ = s._tool_previews["c1"]
        assert descriptor["kind"] == "pdf"
        assert descriptor["size"] == len(body)
        assert seen["max_bytes"] == max(PREVIEW_SIZE_CAPS.values())

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

    def test_preview_blob_id_salted_out_of_upload_namespace(self, tmp_path):
        import hashlib

        s = _make_session()
        p = tmp_path / "chart.png"
        p.write_bytes(PNG_1x1)
        item = s._prepare_open_preview("c1", {"target": str(p)})
        s._exec_open_preview(item)
        _, att = s._tool_previews["c1"]
        # Uploads are keyed bare sha256(body) and save_attachment freezes
        # `kind` at first insert — an unsalted preview of identical bytes
        # would collide with (or pre-empt) a real upload's row.
        assert att.attachment_id != hashlib.sha256(PNG_1x1).hexdigest()
        assert att.attachment_id == hashlib.sha256(b"preview:" + PNG_1x1).hexdigest()

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

    def test_legacy_charset_table_stored_as_utf8(self, monkeypatch):
        # A latin-1 CSV attachment previews as a table, and the executor
        # transcodes it to UTF-8 at store time so "café" round-trips instead of
        # erroring "not previewable".
        s = _make_session(ws_id="ws-1")
        latin1_csv = "name,city\nRené,Montréal\n".encode("iso-8859-1")
        monkeypatch.setattr(
            "turnstone.core.memory.get_attachment",
            lambda aid: {
                "content": latin1_csv,
                "mime_type": "text/csv; charset=iso-8859-1",
                "filename": "people.csv",
            },
        )
        monkeypatch.setattr(
            "turnstone.core.memory.attachment_referenced_in_ws",
            lambda aid, ws: True,
        )
        item = s._prepare_open_preview("c1", {"target": "attachment:deadbeef"})
        _, msg = s._exec_open_preview(item)
        assert not msg.startswith("Error:")
        descriptor, att = s._tool_previews["c1"]
        assert descriptor["kind"] == "table"
        assert descriptor["content_type"].startswith("text/csv")
        # Stored bytes are valid UTF-8 with the accented characters preserved.
        assert att.content.decode("utf-8") == "name,city\nRené,Montréal\n"

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
    """client.stream() double: a context manager yielding chunked body bytes."""

    def __init__(self, status, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = body if isinstance(body, list) else [body]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield from self._chunks


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

    def stream(self, method, url):
        _FakeClient.calls.append(url)
        return _FakeClient.table[url]


def _screen_stub(blocked=None):
    """Stand in for the real per-hop screen, so tests need no DNS.

    Patches what the guard actually calls. Patching a function the guard has
    stopped calling would leave the test green while screening nothing.
    """
    from turnstone.core.ip_classify import AddressLane
    from turnstone.core.web import UrlScreen

    table = blocked or {}

    def _screen(url):
        err = table.get(url)
        if err is None:
            return UrlScreen(AddressLane.PUBLIC, None, False)
        return UrlScreen(AddressLane.NEVER, err, False)

    return _screen


class TestFetchWithSsrfGuard:
    def _wire(self, monkeypatch, table, blocked=None):
        _FakeClient.calls = []
        _FakeClient.table = table
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)
        monkeypatch.setattr("turnstone.core.web.screen_url", _screen_stub(blocked))

    def test_follows_public_redirect_chain(self, monkeypatch):
        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {
                "https://a.example/": _FakeHop(302, {"location": "https://b.example/x"}),
                "https://b.example/x": _FakeHop(200, {}, body=b"landed"),
            },
        )
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
            blocked={"http://169.254.169.254/latest": "Blocked: private"},
        )
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
                "https://a.example/moved": _FakeHop(200, {}),
            },
        )
        resp = fetch_with_ssrf_guard("https://a.example/start", timeout=5)
        assert resp.status_code == 200
        # The realized response carries the FINAL hop's URL — open_preview's
        # descriptor source and stored <base href> both key off it.
        assert str(resp.url) == "https://a.example/moved"

    def test_redirect_loop_capped(self, monkeypatch):
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {"https://a.example/": _FakeHop(302, {"location": "https://a.example/"})},
        )
        with pytest.raises(ValueError, match="redirects"):
            fetch_with_ssrf_guard("https://a.example/", timeout=5)

    def test_body_over_budget_aborts(self, monkeypatch):
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {"https://a.example/": _FakeHop(200, {}, body=[b"aaaa", b"bbbb", b"cccc"])},
        )
        with pytest.raises(ValueError, match="fetch limit"):
            fetch_with_ssrf_guard("https://a.example/", timeout=5, max_bytes=10)

    def test_redirect_hop_body_never_read(self, monkeypatch):
        from turnstone.core.web import fetch_with_ssrf_guard

        class _BodyBomb(_FakeHop):
            def iter_bytes(self):
                raise AssertionError("redirect hop body must not be read")

        self._wire(
            monkeypatch,
            {
                "https://a.example/": _BodyBomb(302, {"location": "https://b.example/x"}),
                "https://b.example/x": _FakeHop(200, {}, body=b"ok"),
            },
        )
        resp = fetch_with_ssrf_guard("https://a.example/", timeout=5)
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_stale_framing_headers_dropped(self, monkeypatch):
        from turnstone.core.web import fetch_with_ssrf_guard

        self._wire(
            monkeypatch,
            {
                "https://a.example/": _FakeHop(
                    200,
                    {
                        "content-encoding": "gzip",
                        "content-length": "999",
                        "content-type": "text/html; charset=utf-8",
                    },
                    body=b"<html>hi</html>",
                )
            },
        )
        resp = fetch_with_ssrf_guard("https://a.example/", timeout=5)
        # iter_bytes() hands the guard content-DECODED bytes — a surviving
        # content-encoding would make .text try to gunzip plain text, and the
        # upstream content-length no longer describes the body carried.
        assert "content-encoding" not in resp.headers
        assert resp.headers.get("content-length") != "999"
        assert resp.headers.get("content-type") == "text/html; charset=utf-8"
        assert resp.text == "<html>hi</html>"


# ---------------------------------------------------------------------------
# Cancelled-batch synthesis — a staged preview whose descriptor already
# reached the frontend must commit, not vanish (session.py review fix)
# ---------------------------------------------------------------------------


class TestCancelledBatchPreservesPreview:
    def test_synthesize_commits_staged_preview(self, tmp_db):
        from turnstone.core.attachments import Attachment
        from turnstone.core.trajectory import Turn

        s = _make_session(ws_id="ws-1")
        storage = get_storage()
        storage.register_workstream(
            s.ws_id,
            user_id=s._user_id,
            kind=s._kind,
            parent_ws_id=s._parent_ws_id,
        )
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

        s._synthesize_cancelled_results("Cancelled by user.")

        # Side channel drained; descriptor + blob committed with the turn.
        assert "c1" not in s._tool_previews
        stored = storage.load_message_turns(s.ws_id, checkpointed=False)
        assert len(stored) == 1
        assert stored[0].meta.extra["preview"] == descriptor
        assert stored[0].meta.extra["effect_status"] == "unknown"
        assert stored[0].meta.extra["storage_attachment_ids"] == ["abc"]
        blob = storage.get_attachment("abc")
        assert blob is not None
        assert blob["content"] == b"<p>x</p>"
        assert blob["origin"] == "tool"
        assert blob["refcount"] == 1
        # The in-memory synthesized turn carries the descriptor too.
        tool_turns = [t for t in s.messages if isinstance(t, Turn) and t.role is Role.TOOL]
        assert tool_turns and tool_turns[-1].meta.extra.get("preview") == descriptor


# ---------------------------------------------------------------------------
# tools.allow_private_network — the self-hoster opt-in (admin Settings → Tools)
# ---------------------------------------------------------------------------


class TestAllowPrivateNetwork:
    def test_screen_public_url_passes(self):
        from turnstone.core.session import _screen_tool_url

        err, private, _block = _screen_tool_url("https://93.184.216.34/x", False)
        assert err is None and private is False

    def test_screen_private_blocked_with_discoverable_hint(self):
        from turnstone.core.session import _screen_tool_url

        err, private, _block = _screen_tool_url("http://10.0.0.7/grafana", False)
        assert err is not None and private is False
        # The refusal teaches the knob (mirrors the oidc opt-in hint pattern).
        assert "tools.allow_private_network" in err
        assert "Settings" in err

    def test_screen_private_allowed_when_opted_in(self):
        from turnstone.core.session import _screen_tool_url

        err, private, _block = _screen_tool_url("http://10.0.0.7/grafana", True)
        assert err is None and private is True

    def test_screen_invalid_url_never_hints(self):
        from turnstone.core.session import _screen_tool_url

        err, private, _block = _screen_tool_url("http://", True)
        assert err is not None and private is False
        assert "allow_private_network" not in err

    def test_bare_session_defaults_strict(self):
        # No ConfigStore (CLI / eval surface) → no admin opted in → strict.
        s = _make_session()
        assert s._allow_private_network() is False

    def test_prepare_web_fetch_private_opted_in(self, monkeypatch):
        s = _make_session()
        monkeypatch.setattr(ChatSession, "_allow_private_network", lambda self: True)
        item = s._prepare_web_fetch(
            "c1", {"url": "http://192.168.1.50:3000/d/home", "question": "what is shown?"}
        )
        assert "error" not in item
        assert item["needs_approval"] is True  # the human gate stays
        assert "(private network)" in item["header"]
        assert item["allow_private_origin"] is True

    def test_prepare_open_preview_private_opted_in(self, monkeypatch):
        s = _make_session()
        monkeypatch.setattr(ChatSession, "_allow_private_network", lambda self: True)
        item = s._prepare_open_preview("c1", {"target": "http://192.168.1.50:3000/d/home"})
        assert "error" not in item
        assert item["needs_approval"] is True
        assert "(private network)" in item["header"]
        assert item["allow_private_origin"] is True

    def test_prepare_private_still_blocked_by_default(self, monkeypatch):
        s = _make_session()
        monkeypatch.setattr(ChatSession, "_allow_private_network", lambda self: False)
        for prepare, args in (
            (s._prepare_web_fetch, {"url": "http://10.0.0.7/x", "question": "q"}),
            (s._prepare_open_preview, {"target": "http://10.0.0.7/x"}),
        ):
            item = prepare("c1", args)
            assert "error" in item
            assert "tools.allow_private_network" in item["error"]

    def test_executor_passes_private_origin_to_guard(self, monkeypatch):
        s = _make_session()
        monkeypatch.setattr(ChatSession, "_allow_private_network", lambda self: True)
        seen = {}

        def _capture(url, **kw):
            seen.update(kw, url=url)
            return _fake_response(url, b"<html><head></head><body>x</body></html>", "text/html")

        monkeypatch.setattr("turnstone.core.session.fetch_with_ssrf_guard", _capture)
        item = s._prepare_open_preview("c1", {"target": "http://10.0.0.7/status"})
        s._exec_open_preview(item)
        assert seen["allow_private_origin"] is True

    def test_guard_permits_private_hops_for_an_approved_private_origin(self, monkeypatch):
        """Screening is not skipped for a private origin — it is WIDENED.

        The operator approved a private URL, so the PRIVATE lane is acceptable
        on this chain; every hop is still classified. (These are IP literals,
        so the real screen resolves them without touching the network.)
        """
        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://10.0.0.7/a": _FakeHop(302, {"location": "http://10.0.0.8/b"}),
            "http://10.0.0.8/b": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)
        resp = fetch_with_ssrf_guard("http://10.0.0.7/a", timeout=5, allow_private_origin=True)
        assert resp.status_code == 200
        assert _FakeClient.calls == ["http://10.0.0.7/a", "http://10.0.0.8/b"]

    def test_private_hop_refused_without_the_private_origin_flag(self, monkeypatch):
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {"http://10.0.0.7/a": _FakeHop(200, {})}
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)
        with pytest.raises(ValueError, match="private/internal"):
            fetch_with_ssrf_guard("http://10.0.0.7/a", timeout=5)
        assert _FakeClient.calls == []

    def test_public_bounce_cannot_return_to_the_origin_host(self, monkeypatch):
        """``private -> public -> back to the origin`` must not be re-admitted.

        An origin-host exemption (added to let a dual-stack host redirect to
        itself) made this reachable: the permission was keyed on the hostname
        and never cleared, so a public hop could send the fetcher back to the
        approved host at a path of its choosing. Mixed-record origins are now
        refused before the fetch instead, so the guard needs no exemption.
        """
        import socket

        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://home.example/": _FakeHop(302, {"location": "http://attacker.example/"}),
            "http://attacker.example/": _FakeHop(302, {"location": "http://home.example/admin"}),
            "http://home.example/admin": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)

        def _resolve(host, port=None, *a, **kw):
            if host == "home.example":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 0)),
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 0),
                    ),
                ]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))
            ]

        monkeypatch.setattr("socket.getaddrinfo", _resolve)
        with pytest.raises(ValueError, match="private/internal"):
            fetch_with_ssrf_guard("http://home.example/", timeout=5, allow_private_origin=True)
        assert _FakeClient.calls == ["http://home.example/", "http://attacker.example/"]

    def test_dual_stack_origin_cannot_redirect_to_another_private_host(self, monkeypatch):
        """The approval covers that host, not the rest of the network."""
        import socket

        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://grafana.home.arpa/": _FakeHop(302, {"location": "http://10.0.0.1/admin"}),
            "http://10.0.0.1/admin": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0)),
        ]
        monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: infos)
        with pytest.raises(ValueError, match="private/internal"):
            fetch_with_ssrf_guard("http://grafana.home.arpa/", timeout=5, allow_private_origin=True)
        assert _FakeClient.calls == ["http://grafana.home.arpa/"]

    def test_mixed_record_hop_revokes_the_private_permission(self, monkeypatch):
        """A hop that merely CONTAINS a private record must not keep the permission.

        The revocation used to key on the folded lane, so a hop resolving to
        both a private and an attacker-controlled public record folded to
        PRIVATE, was fetched, and did NOT revoke — letting the attacker's
        server steer the next hop back into private space.
        """
        import pytest

        from turnstone.core import web
        from turnstone.core.ip_classify import AddressLane
        from turnstone.core.web import UrlScreen, fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://10.0.0.7/a": _FakeHop(302, {"location": "http://mixed.example/b"}),
            "http://mixed.example/b": _FakeHop(302, {"location": "http://10.0.0.1/admin"}),
            "http://10.0.0.1/admin": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)

        real = web.screen_url

        def _screen(url):
            if "mixed.example" in url:
                # Worst lane PRIVATE, but not wholly private.
                return UrlScreen(AddressLane.PRIVATE, "Blocked: private/internal", False)
            return real(url)

        monkeypatch.setattr("turnstone.core.web.screen_url", _screen)
        with pytest.raises(ValueError, match="private/internal"):
            fetch_with_ssrf_guard("http://10.0.0.7/a", timeout=5, allow_private_origin=True)
        assert _FakeClient.calls == ["http://10.0.0.7/a", "http://mixed.example/b"]

    def test_public_bounce_revokes_the_private_permission(self, monkeypatch):
        """``private -> public -> private`` must not reach the final hop.

        The operator approved their own hosts, not whatever a public site
        picks next. Without this, a LAN page serving attacker-authored content
        could steer the fetcher into internal endpoints of the attacker's
        choosing and hand back the response.
        """
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://10.0.0.7/wiki": _FakeHop(302, {"location": "http://93.184.216.34/"}),
            "http://93.184.216.34/": _FakeHop(302, {"location": "http://10.0.0.1/admin"}),
            "http://10.0.0.1/admin": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)
        with pytest.raises(ValueError, match="private/internal"):
            fetch_with_ssrf_guard("http://10.0.0.7/wiki", timeout=5, allow_private_origin=True)
        assert _FakeClient.calls == ["http://10.0.0.7/wiki", "http://93.184.216.34/"]

    def test_guard_still_blocks_metadata_hop_from_a_private_origin(self, monkeypatch):
        """Approving a private origin says "this is my network" — not "and the IMDS".

        The PRIVATE lane is not re-screened for an approved private origin (see
        the test above), but the NEVER lane is absolute: a LAN host that is
        compromised, or simply serving attacker-authored content, must not be
        able to bounce the fetcher into the cloud metadata endpoint.
        """
        import pytest

        from turnstone.core.web import fetch_with_ssrf_guard

        _FakeClient.calls = []
        _FakeClient.table = {
            "http://10.0.0.7/a": _FakeHop(
                302, {"location": "http://169.254.169.254/latest/meta-data/"}
            ),
            "http://169.254.169.254/latest/meta-data/": _FakeHop(200, {}),
        }
        monkeypatch.setattr("turnstone.core.web.httpx.Client", _FakeClient)

        with pytest.raises(ValueError, match="link-local"):
            fetch_with_ssrf_guard("http://10.0.0.7/a", timeout=5, allow_private_origin=True)
        assert _FakeClient.calls == ["http://10.0.0.7/a"], "metadata hop was never issued"

    def test_registry_entry_shape(self):
        from turnstone.core.settings_registry import SETTINGS

        d = SETTINGS["tools.allow_private_network"]
        assert d.type == "bool"
        assert d.default is False
        assert d.section == "tools"
        assert d.help  # the admin form renders this — it must explain the caveat
