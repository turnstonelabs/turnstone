"""Download behavior and static guards for the shared preview pane frontend.

The node harness exercises asynchronous loading, transport context, and history.
Static assertions cover wiring through conversation.js / interactive.js / shell.js;
parse and HTML-sink guards live in ``test_shell_js.py``'s bundle sweeps.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._js_harness_helpers import FAKE_DOM, demodulize, node_skip, run_node_source

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_PANE_JS = _SHARED / "pane.js"
_PREVIEW_JS = _SHARED / "preview.js"
_CONVERSATION_JS = _SHARED / "conversation.js"
_INTERACTIVE_JS = _SHARED / "interactive.js"
_SHELL_JS = _SHARED / "shell.js"
_PREVIEW_CSS = _SHARED / "preview.css"
_UI_INDEX = _ROOT / "turnstone/ui/static/index.html"
_CONSOLE_INDEX = _ROOT / "turnstone/console/static/index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _run_preview(scenario: str) -> None:
    source = (
        FAKE_DOM
        + f"\nconst {{ ShellPane }} = await import({json.dumps(_PANE_JS.as_uri())});\n"
        + r"""
const assert = (value, message) => { if (!value) throw new Error(message); };
const requests = [];
const timers = [];
const authFetch = (url) => new Promise((resolve, reject) => requests.push({url, resolve, reject}));
const redactCredentials = (text) => text;
globalThis.setTimeout = (fn) => timers.push(fn);
const flush = () => new Promise(setImmediate);
const descriptor = (id, kind = "text") => ({attachment_id: id, kind, title: id, source: id});
const mount = (extra = null) => {
  const pane = createPreviewPane(extra, {});
  pane.el = document.createElement("section");
  pane.bodyEl = document.createElement("div");
  pane.el.append(pane.bodyEl);
  pane.onMount();
  return pane;
};
const succeed = async (request, body = "saved bytes") => {
  request.resolve(new Response(body));
  await flush();
};
const href = (pane) => pane._downloadLink.getAttribute("href");
"""
        + demodulize(_PREVIEW_JS)
        + "\n"
        + scenario
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, f"preview harness failed:\n{proc.stderr}\n{proc.stdout}"


@node_skip
def test_download_tracks_ready_content_history_and_rehydration() -> None:
    _run_preview(r"""
const pane = mount();
assert(pane._downloadLink.hidden, "empty pane must not offer a download");
const d = descriptor("same id");
const a = {base: "/node/node-A", wsId: "ws/A"};
const b = {base: "/node/node-B", wsId: "ws B"};
pane.showPreview(d, a);
assert(!href(pane), "pending file must not be downloadable");
assert(pane._downloadLink.getAttribute("aria-disabled") === "true", "pending is disabled");
await succeed(requests.shift());
const urlA = "/node/node-A/v1/api/workstreams/ws%2FA/attachments/same%20id/content";
assert(href(pane) === urlA, "download must use the originating transport and encoded path");
assert(pane._downloadLink.hasAttribute("download"), "download must save rather than navigate");
assert(!pane._downloadLink.hasAttribute("tabindex"), "ready link must be keyboard reachable");
pane.showPreview({...d, title: "Updated title"}, a);
await succeed(requests.shift());
assert(pane._stack.length === 1, "reopening the same file must not duplicate history");
assert(pane._titleEl.textContent === "Updated title", "reopening must refresh display metadata");
pane.showPreview(d, b);
assert(!href(pane), "changing workstreams must clear the previous download");
await succeed(requests.shift());
const urlB = "/node/node-B/v1/api/workstreams/ws%20B/attachments/same%20id/content";
assert(href(pane) === urlB, "identical bytes on another workstream must use its own context");
pane._backBtn.click();
await succeed(requests.shift());
assert(href(pane) === urlA, "back must restore the prior file and transport");
pane._fwdBtn.click();
await succeed(requests.shift());
assert(href(pane) === urlB, "forward must restore the next file and transport");
const restored = mount({descriptor: d, ctx: a});
assert(!href(restored), "restored files must be checked before enabling download");
await succeed(requests.shift());
assert(href(restored) === urlA, "reload must restore the download context");
""")


@node_skip
def test_stale_loads_errors_and_close_cannot_enable_download() -> None:
    _run_preview(r"""
const pane = mount();
const ctx = {base: "", wsId: "ws"};
pane.showPreview(descriptor("old"), ctx);
const old = requests.shift();
pane.showPreview(descriptor("current", "pdf"), ctx);
await succeed(old);
assert(!href(pane), "stale text must not enable a download for a pending preview");
assert(requests[0].url.endsWith("/current/preview?probe=1"), "PDF must preflight");
await succeed(requests.shift());
assert(href(pane).endsWith("/current/content"), "probe success must enable the current file");
pane.showPreview(descriptor("missing"), ctx);
for (let attempt = 0; attempt < 5; attempt++) {
  requests.shift().resolve(new Response("missing", {status: 404}));
  await flush();
  assert(!href(pane), "unavailable files must remain disabled throughout retries");
  if (timers.length) timers.shift()();
}
assert(pane.bodyEl.querySelector(".preview-error"), "retry exhaustion must show an error");
pane.bodyEl.querySelector(".preview-error").querySelector("button").click();
await succeed(requests.shift());
assert(href(pane).endsWith("/missing/content"), "manual retry must recover the download");
pane.showPreview(descriptor("closing", "image"), ctx);
pane.onClose();
await succeed(requests.shift());
assert(!href(pane), "a load completing after close must not enable its link");
""")


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

    def test_content_loads_through_authfetch_probe(self) -> None:
        """src-loaded kinds preflight with a probe request (authFetch of
        ?probe=1), NOT a HEAD.  The console reverse proxy forwards a HEAD as a
        full GET, so a real HEAD would drag the whole blob across the hop just
        to discard it; the probe still surfaces the persist race + auth
        failures as a typed error card and rides the 401-refresh retry a bare
        iframe/img src can't."""
        body = _read(_PREVIEW_JS)
        assert "authFetch(probeUrl)" in body
        assert "probe=1" in body
        # The old full-GET HEAD preflight is gone.
        assert 'method: "HEAD"' not in body

    def test_markdown_uses_the_sanctioned_html_lane(self) -> None:
        body = _read(_PREVIEW_JS)
        assert "setSafeHtml(doc, renderMarkdown(text))" in body

    def test_markdown_runs_vendor_post_pass(self) -> None:
        """The pane runs renderer.js's post-render pass (hljs token coloring +
        mermaid) like the conversation pane — dropping it silently regresses
        code highlighting and diagram rendering in previews."""
        body = _read(_PREVIEW_JS)
        assert "postRenderMarkdown(" in body

    def test_remote_assets_toggle_is_default_off(self) -> None:
        """The remote-assets opt-in defaults OFF: a previewed page must not
        contact its origin site until the user asks.  Pins the label / tooltip
        copy and the sticky-boolean initializer."""
        body = _read(_PREVIEW_JS)
        assert "Load remote images & styles" in body
        assert "Off keeps this preview from contacting the site" in body
        assert "pane._assetsOn = false" in body

    def test_assets_flag_only_rides_behind_toggle(self) -> None:
        """assets=1 reaches the URL only when the per-pane toggle is on."""
        body = _read(_PREVIEW_JS)
        assert "assets=1" in body
        assert "pane._assetsOn" in body

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
        assert "!accepted && !isError && this._host.isFocused(this)" in body
        assert "this._host.onPreview(preview);" in body

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


class TestEphemeralDismiss:
    """The preview is an ephemeral pane: dismissing its split cell CLOSES it
    (tab and content gone) instead of parking an orphan tab whose only reopen
    is the transcript chip.  Regression guard for the pane/tab desync."""

    def test_preview_pane_is_ephemeral(self) -> None:
        """createPreviewPane must flag the pane ephemeral — the whole fix keys
        off this bit."""
        body = _read(_PREVIEW_JS)
        assert "ephemeral: true" in body, "the preview pane must declare itself ephemeral"

    def test_shellpane_carries_the_ephemeral_flag(self) -> None:
        body = _read(_PANE_JS)
        assert "this.ephemeral = opts.ephemeral || false;" in body, (
            "ShellPane must accept and default the ephemeral flag"
        )

    def test_tab_dismiss_closes_ephemeral_pane_outright(self) -> None:
        """In a split the dismiss button normally HIDES the cell (closeCell); for an
        ephemeral pane it must fall through to close() — the `!pane.ephemeral`
        guard is what routes it there.  Pin BOTH the guard and where the
        skipped case lands (the else), or gutting the else regresses the fix
        while the guard string survives verbatim."""
        body = _read(_PANE_JS)
        assert "if (this._layout && this._leafFor(pane.id) && !pane.ephemeral)" in body, (
            "tab dismissal must skip closeCell for an ephemeral pane"
        )
        assert "else this.close(pane.id);" in body, (
            "the skipped (ephemeral / single-pane) case must land on close()"
        )

    def test_unsplit_closes_ephemeral_non_survivors(self) -> None:
        """Collapsing the split from the OTHER pane must not orphan the preview
        either — unsplit closes ephemeral panes it isn't keeping."""
        body = _read(_PANE_JS)
        assert "const keep = this._activeId;" in body, (
            "the unsplit survivor must be the FOCUSED pane — the filter's "
            "`id !== keep` guard is only correct if keep is _activeId"
        )
        assert "for (const id of doomed) this.close(id);" in body, (
            "unsplit must destroy ephemeral panes it does not keep"
        )
        assert "return id !== keep && p && p.ephemeral;" in body, (
            "unsplit must spare the focused survivor and non-ephemeral panes"
        )
