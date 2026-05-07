"""Static smoke guards for ``turnstone/ui/static/app.js``.

The interactive WebUI's app.js has no JS test framework on the
project side. This file holds Python-side string-presence assertions
that catch regressions on critical paths — the kind of one-line
deletion or rename that breaks the UI silently and only surfaces in
manual testing.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP_JS = Path(__file__).resolve().parent.parent / "turnstone/ui/static/app.js"


def test_switch_tab_bootstraps_pane_when_none_exists() -> None:
    """``switchTab`` must create a pane when none exists. A fresh-
    loaded interactive UI with no workstreams shows the dashboard
    and creates no panes (per ``initWorkstreams``); the user's first
    ``create`` or ``open`` then calls ``switchTab(newWsId)``. Pre-fix,
    the early ``if (!pane) return;`` left switchTab with nowhere to
    attach — the chat UI never connected SSE for the freshly-created
    workstream, and only a page refresh fixed it. This test guards
    against accidentally re-introducing the early-return."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("function switchTab(wsId) {")
    # Bound the search to the function body — switchTab is short.
    fn = body[start : start + 2000]
    assert "if (!pane) return;" not in fn, (
        "switchTab must not early-return when no pane exists — that's "
        "the no-chat-after-first-create bug. Bootstrap a pane instead."
    )
    # Affirmatively check the bootstrap path exists.
    assert "createPane(wsId)" in fn, (
        "switchTab must call createPane(wsId) to bootstrap the first "
        "pane when getFocusedPane returns null"
    )


def test_tool_error_does_not_overwrite_approval_badge() -> None:
    """When an approved tool subsequently errors, the existing
    ``✓ approved`` (or ``✓ auto-approved``) pill must remain visible —
    the error indicator is appended as a sibling pill, not by mutating
    the approval pill in place. Pre-fix, both ``appendToolOutput``
    (live) and ``replayHistory`` (history reconstruction) located the
    existing approval badge via ``querySelector(".ts-approval-badge")``
    and overwrote its className + textContent with the ``--error``
    state, so the user lost the record that they had approved the
    call. This test pins the new append-sibling behaviour."""
    body = _APP_JS.read_text(encoding="utf-8")
    # Affirmatively check that an idempotency guard exists somewhere:
    # a ``querySelector(".ts-approval-badge--error")`` lookup is the
    # structural marker of the fix. Pre-fix the modifier never appeared
    # in app.js at all. Loose on quote style and surrounding form (the
    # guard might be a negated ``if (!q) {build...}`` block at a call
    # site, or a positive ``if (q) return;`` early-exit inside an
    # extracted helper) so a later refactor doesn't trip CI on
    # cosmetics.
    error_guard_re = re.compile(
        r"""querySelector\(\s*['"]\.ts-approval-badge--error['"]\s*\)""",
    )
    assert error_guard_re.search(body), (
        "The error-badge code path must guard creation with a "
        "querySelector for .ts-approval-badge--error so duplicate fires "
        "(live + history re-render) do not stack badges."
    )
    # Forbid the mutate-existing-badge sequence: a generic
    # ``.ts-approval-badge`` lookup followed within a handful of lines
    # by mutating that same handle into the ``--error`` state. Two
    # unrelated call sites (history rendering + live tool-output
    # insertion) legitimately query ``.ts-approval-badge`` to position
    # output above it, so the bare query alone is not the anti-pattern;
    # the close pairing with an ``--error`` class mutation is. Accept
    # either quote style and catch both ``className = "..."`` and
    # ``classList.add("ts-approval-badge--error")`` forms.
    overwrite_re = re.compile(
        r"""(\w+)\s*=\s*\w+\.querySelector\(\s*(["'])\.ts-approval-badge\2\s*\)\s*;"""
        r""".{0,200}?"""
        r"""(?:"""
        r"""\1\.className\s*=\s*(["'])[^"']*\bts-approval-badge--error\b[^"']*\3"""
        r"""|"""
        r"""\1\.classList\.add\([^)]*(["'])ts-approval-badge--error\4[^)]*\)"""
        r""")""",
        re.DOTALL,
    )
    assert not overwrite_re.search(body), (
        "Found the badge-overwrite anti-pattern: a queried "
        ".ts-approval-badge handle is mutated into the --error variant "
        "(via className overwrite or classList.add). Append a sibling "
        "badge instead so the approval verdict stays visible alongside "
        "the error."
    )


def test_replay_history_renders_content_before_tool_block() -> None:
    """In ``replayHistory``'s ``role === "assistant"`` branch, the
    ``msg.content`` render must precede the ``msg.tool_calls`` render.

    Two reasons, both load-bearing:

    1. **Structural** — the next loop iteration's ``role === "tool"``
       message anchors to ``lastToolBlock``. The tool-block branch sets
       that anchor; the content branch clears it. If content runs after
       the tool block, the clear silently drops the upcoming tool
       result. Pre-fix, every interactive tool result was missing from
       saved-workstream replays whenever the assistant turn carried
       both narration and tool calls (very common output shape).

    2. **Visual** — the live SSE path renders content first
       (``stream_text`` streams before ``tool_info`` /
       ``approve_request``), so replay should match.

    The test pins the order via the offsets of the ``msg.content`` and
    ``msg.tool_calls`` branch headers inside the function body."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("Pane.prototype.replayHistory = function")
    end = body.index("Pane.prototype._attachRetryToLastAssistant", start)
    fn = body[start:end]
    # Locate the assistant branch and bound the search to its body —
    # the function also handles user / tool roles which would otherwise
    # confuse the offset comparison.
    asst_start = fn.index('msg.role === "assistant"')
    asst_end = fn.index('msg.role === "tool"', asst_start)
    asst = fn[asst_start:asst_end]
    # ``if (msg.content && msg.content.trim())`` guards against a
    # whitespace-only content row (Qwen-style "\n\n" left over after a
    # reasoning-parser model strips ``<think>…</think>`` and emits
    # nothing else before the tool call).  Pre-trim guard, those rows
    # rendered as a visible-but-empty ``.msg.assistant`` card on
    # replay.  Match the substring up to ``msg.content`` so the test
    # tolerates either guard shape without locking the trim() in.
    content_idx = asst.index("if (msg.content")
    tool_calls_idx = asst.index("if (msg.tool_calls && msg.tool_calls.length)")
    assert content_idx < tool_calls_idx, (
        "replayHistory must render msg.content BEFORE msg.tool_calls "
        "inside the assistant branch — otherwise the lastToolBlock "
        "anchor is clobbered before the next iteration's tool result "
        "can attach to it (and the visual order also drifts from the "
        "live SSE flow)."
    )


def test_replay_history_renders_persisted_verdict_badge() -> None:
    """Saved-workstream replays must paint the persisted intent verdict
    next to each tool div, using the same ``renderVerdictBadge`` helper
    the live ``showInlineToolBlock`` path uses. Pre-fix the audit trail
    was complete in storage (``intent_verdicts`` table) but never
    surfaced on replay — operators reviewing a saved workstream
    couldn't see what the heuristic / LLM judge thought of any tool
    call. This test pins the call site so a refactor that drops the
    decoration regresses the audit surface."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("Pane.prototype.replayHistory = function")
    end = body.index("Pane.prototype._attachRetryToLastAssistant", start)
    fn = body[start:end]
    # Match a `renderVerdictBadge(<something>.verdict, ...)` call inside
    # the replay loop.  Loose on whitespace + identifier so a future
    # rename of the iteration variable doesn't trip CI.
    badge_call_re = re.compile(
        r"renderVerdictBadge\(\s*\w+\.verdict\b",
    )
    assert badge_call_re.search(fn), (
        "replayHistory must call renderVerdictBadge(tc.verdict, ...) "
        "when a persisted verdict is attached to a tool_call entry — "
        "otherwise the audit-trail data persisted to intent_verdicts "
        "doesn't surface on saved-workstream replays."
    )


# ---------------------------------------------------------------------------
# Phase 8 — Chunk D: MCP error embed + settings panel UX
# ---------------------------------------------------------------------------

_INDEX_HTML = Path(__file__).resolve().parent.parent / "turnstone/ui/static/index.html"
_STYLE_CSS = Path(__file__).resolve().parent.parent / "turnstone/ui/static/style.css"

# The Phase-8 D-chunk pins the absence of an unsafe DOM-write API
# in two regions of app.js. Spell the property name out of literal
# concatenation so the tooling that flags occurrences in code
# strings doesn't false-positive on the test source.
_UNSAFE_DOM_WRITE_RE = re.compile(r"\.inner" + r"HTML\s*=")


def test_phase8_mcp_error_helpers_defined_in_app_js() -> None:
    """The Phase 8 dashboard renderer adds three load-bearing helpers
    next to the existing media-embed pattern: ``tryParseMcpError``
    (envelope detector), ``buildMcpErrorEmbed`` (interactive card),
    and the ``_pendingConsentServers`` set that drives the gear-icon
    badge. A regression that drops any of them silently degrades the
    OAuth consent UX to a plain JSON dump, so guard their existence
    here."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert "function tryParseMcpError" in body, (
        "tryParseMcpError must remain defined — appendToolOutput's "
        "error branch depends on it to detect the MCP error envelope."
    )
    assert "function buildMcpErrorEmbed" in body, (
        "buildMcpErrorEmbed must remain defined — it renders the "
        "interactive consent / forbidden / operator card."
    )
    assert "_pendingConsentServers" in body, (
        "_pendingConsentServers state must remain — it backs the "
        "gear-icon badge so a user who scrolls past a consent prompt "
        "still has a stable signal that consent is pending."
    )
    # The buildMcpErrorEmbed pattern must also wire the "actionable"
    # branch (consent_required / insufficient_scope) into the badge
    # via _onConsentDetected; pin the helper name.
    assert "_onConsentDetected" in body, (
        "_onConsentDetected must remain — buildMcpErrorEmbed calls it "
        "for the actionable category to surface the gear-icon badge."
    )


def test_phase8_settings_panel_handlers_defined() -> None:
    """The settings modal exposes four entry points that the inline
    ``onclick`` attributes in index.html depend on. Renaming or
    deleting any of them breaks the modal silently (the buttons are
    still rendered but click-to-action is dead). Catch that here."""
    body = _APP_JS.read_text(encoding="utf-8")
    for name in [
        "function openSettingsPanel",
        "function closeSettingsPanel",
        "function confirmRevokeMcp",
        "function cancelRevokeMcp",
    ]:
        assert name in body, f"Missing required handler: {name}"
    # The connections list is fetched against the Phase-7 endpoint —
    # pin the URL so a server-side rename forces an explicit UI bump.
    assert "/v1/api/mcp/oauth/connections" in body, (
        "Settings panel must fetch /v1/api/mcp/oauth/connections — "
        "a server-side rename needs an explicit UI update."
    )


def test_phase8_appendtooloutput_dispatches_mcp_error_before_renderer() -> None:
    """``appendToolOutput`` must call ``tryParseMcpError`` inside its
    ``isError`` branch BEFORE falling through to the plain
    ``renderToolOutput`` path. The ordering is what makes the
    interactive consent card replace the JSON dump; reverse the calls
    and the user sees the raw error envelope as text again."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("Pane.prototype.appendToolOutput = function")
    end = body.index("Pane.prototype.", start + 10)
    fn = body[start:end]
    parse_idx = fn.find("tryParseMcpError(")
    render_idx = fn.find("renderToolOutput(")
    assert parse_idx >= 0, (
        "appendToolOutput must call tryParseMcpError on the error path "
        "before renderToolOutput, otherwise the consent card never "
        "replaces the plain JSON output."
    )
    assert render_idx >= 0, "renderToolOutput call must remain present"
    assert parse_idx < render_idx, (
        "tryParseMcpError must run BEFORE renderToolOutput so the "
        "interactive card path takes precedence over plain rendering."
    )


def test_phase8_no_unsafe_dom_write_in_settings_panel() -> None:
    """Defensive XSS guard: the settings panel renders user-controlled
    server names, scope strings, and timestamp values into the DOM.
    The whole section MUST go through ``textContent``-style APIs; an
    unsafe-DOM-write assignment would be a regression vector. Bound
    the check to the section 15 body to avoid false positives
    elsewhere."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("//  15. MCP server connections settings panel")
    # Bound to the full settings section (terminates at the next
    # top-level keydown handler block).
    end = body.index('document.addEventListener("keydown"', start)
    section = body[start:end]
    assert not _UNSAFE_DOM_WRITE_RE.search(section), (
        "Section 15 must not assign to the unsafe DOM-write property — "
        "server names and scope values flow through here and would be "
        "XSS-injectable. Use textContent / DOM APIs instead."
    )


def test_phase8_settings_button_in_index_html() -> None:
    """The gear-icon entry-point for the settings panel must remain
    in the appbar's actions span. The console proxy IIFE prepends a
    node pill to ``header.firstChild`` (turnstone/console/server.py:
    202); our button is appended inside ``<span class='appbar-actions'>``
    on the right, so they don't collide. Pin both shape constraints
    here so a future appbar refactor keeps them disjoint."""
    body = _INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="settings-btn"' in body, (
        "index.html must keep the #settings-btn — onclick handlers "
        "and the consent badge target it by id."
    )
    assert 'onclick="openSettingsPanel()"' in body, (
        "settings-btn must wire onclick=openSettingsPanel() — losing "
        "the binding leaves the panel unreachable."
    )
    # The button must live inside <span class="appbar-actions"> so the
    # console proxy's header.insertBefore(pill, header.firstChild)
    # leaves it untouched.
    actions_open = body.index('class="appbar-actions"')
    actions_close = body.index("</span>", actions_open)
    assert 'id="settings-btn"' in body[actions_open:actions_close], (
        "settings-btn must be inside <span class='appbar-actions'> "
        "so the console proxy's firstChild prepend doesn't shift it."
    )


def test_phase8_settings_modal_in_index_html() -> None:
    """Both the settings overlay and the revoke-confirmation overlay
    must remain in the modal area. The Escape-key deferral list in
    app.js targets these ids, so removing them silently breaks the
    handler chain."""
    body = _INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="settings-overlay"' in body
    assert 'id="revoke-mcp-overlay"' in body
    # Each overlay must have role="dialog" + aria-modal="true" so
    # screen readers and the existing modal-deferral handlers can
    # treat them like the rest of the modal stack.
    for overlay_id in ("settings-overlay", "revoke-mcp-overlay"):
        idx = body.index(f'id="{overlay_id}"')
        # Bound to ~600 chars after the open tag so we only check this
        # overlay's attributes.
        chunk = body[idx : idx + 600]
        assert 'role="dialog"' in chunk, f"{overlay_id} missing role=dialog"
        assert 'aria-modal="true"' in chunk, f"{overlay_id} missing aria-modal=true"


def test_phase8_xss_safe_render_in_build_mcp_error_embed() -> None:
    """Adversarial input — the renderer for an MCP error envelope
    must use ``textContent`` (not the unsafe DOM-write API) for every
    field that flows from the server: ``err.detail``, ``err.server``,
    scopes list. The card builder uses createElement + textContent
    throughout so a script-tag server name renders harmlessly. Pin
    the absence of the unsafe-write inside ``buildMcpErrorEmbed``."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("function buildMcpErrorEmbed(")
    # Bound to the function body — find its closing brace at column 0.
    rest = body[start:]
    # Closing function brace at line start (matches existing functions)
    end_match = re.search(r"\n}\n", rest)
    assert end_match is not None
    fn = rest[: end_match.end()]
    assert not _UNSAFE_DOM_WRITE_RE.search(fn), (
        "buildMcpErrorEmbed must not use the unsafe-DOM-write API — "
        "server names and detail strings flow through here. An "
        "adversarial server name must render harmlessly via "
        "textContent."
    )


def test_phase8_css_classes_present_in_stylesheet() -> None:
    """The card / badge / modal classes referenced from app.js must
    have CSS rules. Without them the DOM still works but the visual
    treatment is gone, which would silently degrade the consent UX."""
    css = _STYLE_CSS.read_text(encoding="utf-8")
    for selector in [
        ".mcp-error-card",
        ".mcp-error-icon",
        ".mcp-error-action-btn",
        ".mcp-scope-pill",
        "#settings-overlay",
        "#settings-box",
        ".settings-revoke-btn",
        ".settings-consent-badge",
        "#revoke-mcp-overlay",
    ]:
        assert selector in css, f"Missing CSS rule for {selector}"


def test_phase8_consent_url_prefix_check_in_click_handler() -> None:
    """Defence-in-depth: the consent button's click handler must reject
    any ``consent_url`` that doesn't start with the dispatcher's known
    prefix (``/v1/api/mcp/oauth/start``). ``_build_consent_url`` always
    emits a path-relative URL with that exact prefix; a non-prefix
    value implies the producer drifted (or was compromised) and a
    ``window.open("javascript:...")`` would be catastrophic.

    The renderer is the last line of defence before ``window.open`` and
    must not rely on the producer-side guarantee alone. Pin the prefix
    string and the ``startsWith`` form so a future refactor can't
    silently weaken the guard.
    """
    body = _APP_JS.read_text(encoding="utf-8")
    # Bound the search to the click handler region (between the
    # ``buildMcpErrorEmbed`` function and the next top-level helper) to
    # avoid false positives from unrelated string occurrences.
    start = body.index("function buildMcpErrorEmbed(")
    end = body.index("\n}\n", start) + 1
    fn = body[start:end]
    assert 'consentUrl.startsWith("/v1/api/mcp/oauth/start")' in fn, (
        "Click handler must guard window.open with "
        'consentUrl.startsWith("/v1/api/mcp/oauth/start"). Without it '
        "a future producer drift to a non-path-relative URL (or a "
        '"javascript:" injection) would be passed straight to '
        "window.open."
    )
