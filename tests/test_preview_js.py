"""Static guards for the preview pane frontend (shared_static/preview.js and
its wiring through conversation.js / interactive.js / shell.js).

Same posture as ``test_shell_js.py``: Python-side string-presence assertions
that catch the silent one-line regression (a renamed export, a dropped
sandbox attribute, a de-registered pane type).  Parse + sink + var guards for
``preview.js`` itself live in ``test_shell_js.py``'s bundle sweeps.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_PREVIEW_JS = _SHARED / "preview.js"
_CONVERSATION_JS = _SHARED / "conversation.js"
_INTERACTIVE_JS = _SHARED / "interactive.js"
_SHELL_JS = _SHARED / "shell.js"
_PREVIEW_CSS = _SHARED / "preview.css"
_UI_INDEX = _ROOT / "turnstone/ui/static/index.html"
_CONSOLE_INDEX = _ROOT / "turnstone/console/static/index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestPreviewPaneModule:
    def test_factory_exported(self) -> None:
        assert "export function createPreviewPane" in _read(_PREVIEW_JS)

    def test_web_iframe_is_fully_sandboxed(self) -> None:
        """The web renderer must keep the empty-sandbox attribute — every
        capability (scripts, same-origin, forms, popups) stays off.  Dropping
        or loosening it turns fetched pages into live documents."""
        body = _read(_PREVIEW_JS)
        assert 'frame.setAttribute("sandbox", "")' in body
        assert 'frame.setAttribute("referrerpolicy", "no-referrer")' in body

    def test_pdf_iframe_is_not_sandboxed(self) -> None:
        """Deliberate asymmetry: Chromium's PDF viewer refuses to paint in a
        sandboxed context.  The renderer comment carries the rationale; this
        pins that renderPdf never gained a sandbox attribute by copy-paste."""
        body = _read(_PREVIEW_JS)
        pdf_fn = body.split("const renderPdf")[1].split("const renderImage")[0]
        assert "sandbox" not in pdf_fn or "No sandbox attribute" in pdf_fn

    def test_content_loads_through_authfetch_preflight(self) -> None:
        """src-loaded kinds preflight with authFetch HEAD (surfaces the
        persist race + auth failures as a typed error card, and rides the
        401-refresh retry that a bare iframe/img src can't)."""
        body = _read(_PREVIEW_JS)
        assert 'authFetch(url, { method: "HEAD" })' in body

    def test_markdown_uses_the_sanctioned_html_lane(self) -> None:
        body = _read(_PREVIEW_JS)
        assert "setSafeHtml(doc, renderMarkdown(text))" in body

    def test_history_is_bounded(self) -> None:
        assert "HISTORY_CAP" in _read(_PREVIEW_JS)

    def test_table_renderer_caps_rows(self) -> None:
        assert "TABLE_ROW_CAP" in _read(_PREVIEW_JS)

    def test_url_builder_encodes_path_parts(self) -> None:
        body = _read(_PREVIEW_JS)
        assert "encodeURIComponent(ws)" in body
        assert 'encodeURIComponent(descriptor.attachment_id || "")' in body


class TestTranscriptChip:
    def test_chip_builder_exported(self) -> None:
        assert "export function buildPreviewChip" in _read(_CONVERSATION_JS)

    def test_live_path_gates_auto_open_on_focus(self) -> None:
        """A backgrounded session must not commandeer the split — the live
        path auto-opens only while the originating pane is focused; the chip
        is the deliberate reopen everywhere else."""
        body = _read(_INTERACTIVE_JS)
        assert "if (this._host.isFocused(this)) this._host.onPreview(preview);" in body

    def test_replay_path_renders_chip_without_auto_open(self) -> None:
        body = _read(_INTERACTIVE_JS)
        # The replay branch builds the chip…
        assert "buildPreviewChip(msg.preview" in body
        # …and the auto-open call appears exactly once (the live path).
        assert body.count("this._host.onPreview(preview)") == 1

    def test_tool_result_event_passes_preview(self) -> None:
        assert "evt.preview," in _read(_INTERACTIVE_JS)

    def test_host_bridge_carries_transport_ctx(self) -> None:
        """The preview pane fetches blobs from the ORIGINATING workstream
        through the same node proxy — the bridge must pass both base and
        wsId, not just the descriptor."""
        body = _read(_INTERACTIVE_JS)
        assert "window.TS_SHELL.openPreview(descriptor, { base: base, wsId: wsId })" in body


class TestShellWiring:
    def test_pane_type_registered(self) -> None:
        body = _read(_SHELL_JS)
        assert 'pm.registerType("preview"' in body
        assert "createPreviewPane" in body

    def test_opens_beside_the_conversation(self) -> None:
        """openPaneBeside is the load-bearing gesture — the preview coexists
        with the conversation that spawned it instead of replacing it."""
        body = _read(_SHELL_JS)
        assert 'pm.openPaneBeside("preview")' in body

    def test_seam_exported_on_ts_shell(self) -> None:
        assert "openPreview," in _read(_SHELL_JS)


class TestStylesheets:
    def test_both_surfaces_link_preview_css(self) -> None:
        for page in (_UI_INDEX, _CONSOLE_INDEX):
            assert "/shared/preview.css" in _read(page), page.name

    def test_stylesheet_uses_ds_tokens_not_legacy_vars(self) -> None:
        """conv-* card rule: DS tokens only — chat.css legacy vars
        (--green/--red/--fg) must not creep into the new sheet."""
        body = _read(_PREVIEW_CSS)
        assert "var(--ink-" in body
        assert "var(--hair)" in body
        for legacy in ("var(--green)", "var(--red)", "var(--fg)"):
            assert legacy not in body
