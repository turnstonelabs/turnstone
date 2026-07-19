"""Static smoke guards for ``turnstone/ui/static/app.js``.

The interactive WebUI's app.js has no JS test framework on the
project side. This file holds Python-side string-presence assertions
that catch regressions on critical paths — the kind of one-line
deletion or rename that breaks the UI silently and only surfaces in
manual testing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_APP_JS = Path(__file__).resolve().parent.parent / "turnstone/ui/static/app.js"
_INTERACTIVE_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/interactive.js"
_SHELL_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/shell.js"
_REDACT_CREDENTIALS_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/shared_static/redact_credentials.js"
)
_CONSOLE_APP_JS = Path(__file__).resolve().parent.parent / "turnstone/console/static/app.js"
_CONSOLE_INDEX = Path(__file__).resolve().parent.parent / "turnstone/console/static/index.html"


def _pane_method_offset(body: str, name: str) -> int:
    """Return the start offset of class method ``name`` in ``body``.

    Indent-agnostic — matches the method header at any leading-whitespace
    depth (2 spaces for the current class, 4 if the class is ever
    wrapped in an IIFE or module, etc.) so slice tests survive deferred
    modernization without silent ``ValueError`` failures.  Asserts on
    miss so a refactor that renames the method fails loudly at the
    pinning slice instead of further downstream.
    """
    pattern = re.compile(r"^\s{2,}" + re.escape(name) + r"\(", re.MULTILINE)
    m = pattern.search(body)
    assert m is not None, f"class method {name!r} not found in interactive.js"
    return m.start()


def test_switch_tab_opens_an_interactive_pane() -> None:
    """In the L-shell ``switchTab`` is a thin shim onto the PaneManager: it
    opens/focuses the session as an interactive pane.  The split-pane
    ``createPane`` bootstrap is retired."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("function switchTab(wsId) {")
    fn = body[start : start + 400]
    assert "openSessionPane(wsId)" in fn, (
        "switchTab must delegate to openSessionPane (PaneManager.openPane "
        "'interactive'), not the retired createPane bootstrap."
    )
    assert "createPane" not in body, "the split-pane createPane bootstrap is retired."


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
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # Affirmatively check that an idempotency guard exists somewhere:
    # a ``querySelector(".ts-approval-badge--error")`` lookup is the
    # structural marker of the fix. Pre-fix the modifier never appeared
    # in app.js at all. Loose on quote style and surrounding form (the
    # guard might be a negated ``if (!q) {build...}`` block at a call
    # site, or a positive ``if (q) return;`` early-exit inside an
    # extracted helper) so a later refactor doesn't trip CI on
    # cosmetics.
    # 5e.2c: the resolved/error pills converged onto the shared .conv-status
    # vocabulary; the error variant is .conv-status--error.
    error_guard_re = re.compile(
        r"""querySelector\(\s*['"]\.conv-status--error['"]\s*\)""",
    )
    assert error_guard_re.search(body), (
        "The error-badge code path must guard creation with a "
        "querySelector for .conv-status--error so duplicate fires "
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
        r"""(\w+)\s*=\s*\w+\.querySelector\(\s*(["'])\.conv-status\2\s*\)\s*;"""
        r""".{0,200}?"""
        r"""(?:"""
        r"""\1\.className\s*=\s*(["'])[^"']*\bconv-status--error\b[^"']*\3"""
        r"""|"""
        r"""\1\.classList\.add\([^)]*(["'])conv-status--error\4[^)]*\)"""
        r""")""",
        re.DOTALL,
    )
    assert not overwrite_re.search(body), (
        "Found the badge-overwrite anti-pattern: a queried "
        ".conv-status handle is mutated into the --error variant "
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
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = _pane_method_offset(body, "replayHistory")
    end = _pane_method_offset(body, "_attachRetryToLastAssistant")
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
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = _pane_method_offset(body, "replayHistory")
    end = _pane_method_offset(body, "_attachRetryToLastAssistant")
    fn = body[start:end]
    # Match a `renderVerdictBadge(<something>.verdict, ...)` call inside
    # the replay loop.  Loose on whitespace + identifier so a future
    # rename of the iteration variable doesn't trip CI.
    badge_call_re = re.compile(
        r"buildConvVerdict\(\s*\w+\.verdict\b",
    )
    assert badge_call_re.search(fn), (
        "replayHistory must call buildConvVerdict(tc.verdict, ...) "
        "when a persisted verdict is attached to a tool_call entry — "
        "otherwise the audit-trail data persisted to intent_verdicts "
        "doesn't surface on saved-workstream replays."
    )


def test_refetch_history_seeds_resume_cursor_only_on_initial_connect() -> None:
    """``_refetchHistory`` must seed ``_lastEventId`` from a non-null
    ``data.cursor`` so the initial ``connectSSE`` opens with
    ``?last_event_id=`` and takes the ``replay_ok`` fast-forward —
    rebuilding the executing in-flight turn that ``/history`` omitted
    (the fresh-connect-during-parallel-batch fix).

    Two load-bearing guards are pinned here:

    1. The seed is gated on ``seedCursor`` so ONLY the initial-connect
       caller (``_loadHistoryThenConnect``, which reconnects) seeds it;
       the clear_ui / replay_truncated re-render callers (no reconnect)
       must NOT rewind ``_lastEventId`` off the live stream position.
    2. ``connectSSE`` gates the ``?last_event_id=`` param on
       ``!= null`` (not truthiness) so a valid cursor of 0 — a brand-new
       ws's first-turn boundary — isn't silently dropped to the fresh
       snapshot path."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # ``_refetchHistory`` is an ``async`` method, which the shared
    # ``_pane_method_offset`` header regex doesn't match — anchor on the
    # definition directly and bound at the next method.
    start = body.index("async _refetchHistory(")
    end = body.index("handleEvent(", start)
    fn = body[start:end]
    # (1a) seed is gated on BOTH seedCursor AND a non-null cursor.
    seed_re = re.compile(
        r"if\s*\(\s*seedCursor\s*&&\s*data\.cursor\s*!=\s*null\s*\)\s*"
        r"this\._lastEventId\s*=\s*data\.cursor"
    )
    assert seed_re.search(fn), (
        "_refetchHistory must seed this._lastEventId only when "
        "seedCursor AND data.cursor != null — so re-render callers don't "
        "rewind the live stream and a 0 cursor still fast-forwards."
    )
    assert "seedCursor = false" in fn, (
        "seedCursor must default false so the clear_ui / replay_truncated "
        "re-render callers (which pass only 2 args) never seed the cursor."
    )
    # (1b) the initial-connect path opts in with seedCursor=true.
    assert "_refetchHistory(wsId, token, true)" in body, (
        "_loadHistoryThenConnect must call _refetchHistory(..., true) so "
        "the reconnecting initial-connect path is the only seeder."
    )
    # (2) connectSSE gates the last_event_id param on != null, not truthiness.
    assert re.search(
        r"if\s*\(\s*this\._lastEventId\s*!=\s*null\s*\)\s*\{\s*"
        r"evtUrl\s*\+=\s*\"\?last_event_id=\"",
        body,
    ), (
        "connectSSE must gate the ?last_event_id= param on "
        "this._lastEventId != null (not truthiness) — else a cursor of 0 "
        "(brand-new ws first turn) is dropped to the fresh snapshot path."
    )


def test_shared_utils_no_longer_defines_replay_advisories_after_tool() -> None:
    """Operator context (interjections / guard findings / nudges) no longer
    rides the tool envelope — it is first-class ``{"role": "system"}`` rows
    — so the ``replayAdvisoriesAfterTool`` advisory-walk helper is gone.
    Guard its removal so a stale re-introduction is caught.
    """
    utils_js = Path(__file__).resolve().parent.parent / "turnstone/shared_static/utils.js"
    body = utils_js.read_text(encoding="utf-8")
    assert "replayAdvisoriesAfterTool" not in body, (
        "replayAdvisoriesAfterTool should be deleted — operator context now "
        "rides first-class system rows, not the tool envelope."
    )


def test_replay_renders_system_turn_via_add_system_context() -> None:
    """First-class operator-context ``system`` turns (output-guard findings,
    user interjections, metacognitive nudges) replay through the ``system``
    branch of ``replayHistory``, rendering an operator bubble via
    ``addSystemContext``.  Pins the call site so a refactor that drops the
    branch regresses the operator-context replay shape silently."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = _pane_method_offset(body, "replayHistory")
    end = _pane_method_offset(body, "_attachRetryToLastAssistant")
    fn = body[start:end]
    assert 'msg.role === "system"' in fn, (
        "replayHistory must have a system-role branch for first-class operator-context turns."
    )
    # Whitespace-tolerant: the call carries a 3rd ``meta`` arg now, so the
    # formatter wraps it across lines — match the call + first arg, not a
    # brittle contiguous substring.
    assert re.search(r"addSystemContext\(\s*msg\.content", fn), (
        "the system-role branch must route the turn through addSystemContext "
        "so it renders as an operator bubble."
    )


def test_system_turn_dedups_against_history_by_event_id() -> None:
    """The live ``system_turn`` handler skips an event already painted from
    ``/history`` (matched by ``_event_id``), so an SSE replay that redelivers
    it past the resume cursor doesn't double-render the operator bubble —
    belt-and-braces for the row-vs-event id-alignment fix."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    assert re.search(r"_renderedSystemEventIds\s*\.\s*has\(", body), (
        "the system_turn handler must skip an event whose id was already rendered from /history."
    )
    assert re.search(r"_renderedSystemEventIds\s*\.\s*add\(", body), (
        "replayHistory (and the live handler) must record system-turn ids for the dedup set."
    )

    # Pin the wiring on BOTH read paths, scoped to its method — a refactor that
    # keeps the Set but drops the live-handler consultation (or the
    # replayHistory-side record) silently re-opens the double-render while the
    # file-global checks above still pass.
    live_start = body.index('case "system_turn":')
    # End at the NEXT switch case, not the first ``break;`` — the dedup-skip
    # path breaks before the ``.add(``, so a ``break;``-bounded slice would
    # drop the record half and false-fail the ``.add(`` assertion below.
    # Whitespace-tolerant so a reformat can't silently break the bound.
    next_case = re.search(r'\n\s*case "', body[live_start + 1 :])
    assert next_case, (
        "no switch case found after system_turn to bound the pin slice — if "
        "system_turn became the last case, re-anchor this pin's end marker."
    )
    live_block = body[live_start : live_start + 1 + next_case.start()]
    assert re.search(r"_renderedSystemEventIds[\s\S]*?\.\s*has\(", live_block), (
        "the live system_turn handler must CONSULT the dedup set (skip an id "
        "already painted from /history), not merely reference the Set elsewhere."
    )
    assert re.search(r"_renderedSystemEventIds[\s\S]*?\.\s*add\(", live_block), (
        "the live system_turn handler must RECORD the id it renders so a later "
        "/history re-render (clear_ui) doesn't repaint it."
    )

    replay_start = _pane_method_offset(body, "replayHistory")
    replay_end = _pane_method_offset(body, "_attachRetryToLastAssistant")
    replay_block = body[replay_start:replay_end]
    assert re.search(r"_renderedSystemEventIds[\s\S]*?\.\s*add\(", replay_block), (
        "replayHistory must record each replayed system row's event_id so the "
        "live system_turn handler can dedup against it."
    )


def test_retry_walk_skips_operator_context_cards() -> None:
    """Interactive twin of the coord retry-skip guard.
    ``_attachRetryToLastAssistant`` walks back past ``.operator-context`` rows
    before testing for ``.ts-approval`` — so a watch-result / guard-finding
    card (or a plain system bubble) trailing a tool-only turn doesn't make retry
    attach to a stale earlier assistant turn.  Pin the walk predicate (scoped to
    the method) AND the shared marker on every operator row that can trail a
    tool batch, so adding a card kind without the marker fails loudly here."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = _pane_method_offset(body, "_attachRetryToLastAssistant")
    end = _pane_method_offset(body, "announceToolBlock")
    fn = body[start:end]
    assert 'classList.contains("operator-context")' in fn, (
        "_attachRetryToLastAssistant must walk back past .operator-context "
        "rows so the tool-only retry skip fires even when a card trails."
    )
    # Every operator row that can trail a tool batch carries the shared marker.
    # The watch-result card moved to the shared conversation.js (step 5e.1); the
    # plain system-context + guard-finding cards stay in the pane.
    shared = (_INTERACTIVE_JS.parent / "conversation.js").read_text(encoding="utf-8")
    assert '"msg watch-result operator-context"' in shared, (
        "buildWatchResultCard must carry the operator-context marker."
    )
    for cls in (
        '"msg system-context operator-context"',
        '"msg guard-finding operator-context"',
    ):
        assert cls in body, (
            f"operator row className {cls} must carry the operator-context "
            "marker or the retry walk won't skip it."
        )


def test_operator_nudge_labels_use_shared_helper() -> None:
    """Operator-context nudge bubbles collapse the metacognition nudge types
    (start / resume / correction / denial / completion / repeat) to one
    'metacognition' category via the shared ``utils.js`` ``operatorSourceLabel``
    helper rather than leaking the raw ``_source`` (the 'operator · start'
    regression).  Both panes call the one helper so they can't drift."""
    root = Path(__file__).resolve().parent.parent
    utils = (root / "turnstone/shared_static/utils.js").read_text(encoding="utf-8")
    assert "function operatorSourceLabel(" in utils
    for t in ("start", "resume", "correction", "denial", "completion", "repeat"):
        assert f'{t}: "metacognition"' in utils, f"nudge type {t!r} must label as metacognition"
    assert 'tool_error: "tool error"' in utils
    assert 'skill_hint: "skill hint"' in utils
    app = (root / "turnstone/shared_static/interactive.js").read_text(encoding="utf-8")
    coord = (root / "turnstone/console/static/coordinator/coordinator.js").read_text(
        encoding="utf-8"
    )
    assert "operatorSourceLabel(source)" in app, "interactive pane must use the shared label helper"
    assert "operatorSourceLabel(source)" in coord, "coord pane must use the shared label helper"


# ---------------------------------------------------------------------------
# Phase 8 — Chunk D: MCP error embed + settings panel UX
# ---------------------------------------------------------------------------

_INDEX_HTML = Path(__file__).resolve().parent.parent / "turnstone/ui/static/index.html"
_STYLE_CSS = Path(__file__).resolve().parent.parent / "turnstone/ui/static/style.css"

# Pins the absence of unsafe DOM-write and dynamic-code sinks.  Spell
# the property/identifier names out of literal string concatenation so
# the tooling that flags occurrences in code strings doesn't
# false-positive on the test source.
#
# The pattern catches each of:
#   * plain HTML-assignment   — inner/outer-HTML to a value
#   * concat HTML-assignment  — inner/outer-HTML += value (the
#     ``\+?`` makes the ``+`` optional so a regression switching the
#     sink to concat-assignment doesn't bypass the lint)
#   * insertAdjacent HTML     — ``insertAdjacentHTML(...)`` (the
#     ``HTML\(`` suffix excludes ``insertAdjacentElement``, which
#     takes a DOM node and is not an XSS sink)
#   * legacy doc-write        — ``document`` + ``.write(...)``
#   * string-to-code helpers  — the JS ``ev`` + ``al`` builtin, the
#     dynamic-Function constructor (``new`` + ``Function(...)``), and
#     ``setTimeout``/``setInterval`` whose first arg is a string
#     literal (function-first-arg forms remain unflagged)
#
# The trailing ``(?!=)`` negative-lookahead on the HTML assignments
# excludes ``===`` / ``==`` reads — only the write sinks are flagged.
#
# The scan in ``test_no_unsafe_code_sinks_in_static_assets`` runs the
# regex over the *entire file body* (not line-by-line) so that ``\s*``
# can span newlines and catch multi-line sinks like
# ``el.innerHTML\n  = X``.
_UNSAFE_CODE_SINK_RE = re.compile(
    r"\.(?:inner|outer)"
    + r"HTML\s*\+?=(?!=)"
    + r"|\.insertAdjacent"
    + r"HTML\s*\("
    + r"|"
    + r"document"
    + r"\."
    + r"write"
    + r"\("
    + r"|\b"
    + r"eval\s*\("
    + r"|\bnew\s+"
    + r"Function\s*\("
    + r"|\bset(?:Timeout|Interval)\s*\(\s*['\"`]"
)


def test_phase8_mcp_error_helpers_defined() -> None:
    """``tryParseMcpError`` (envelope detector) + ``buildMcpErrorEmbed``
    (interactive consent / forbidden / operator card) moved into the shared
    interactive module with the Pane.  The consent-badge state
    (``_pendingConsentServers`` / ``_onConsentDetected``) stays in the
    standalone shell — it drives the rail's Manage-row badge — and the pane
    reaches it through the ``host.onConsentDetected`` seam.  The shared host
    bridges that seam to the standalone via ``window.TS_APP.onConsentDetected``
    (undefined on the console, so it stays a no-op there).  Pin both halves and
    the bridge."""
    inter = _INTERACTIVE_JS.read_text(encoding="utf-8")
    assert "function tryParseMcpError" in inter
    assert "function buildMcpErrorEmbed" in inter
    # The actionable branch surfaces consent via the THREADED callback, not a
    # direct shell call — that decoupling is what lets the console no-op it.
    assert "if (onConsent) onConsent(err.server)" in inter
    assert "onConsentDetected(s)" in inter, (
        "the pane must notify consent through host.onConsentDetected"
    )
    # The shared host bridges the seam to the standalone subsystem (feature-
    # detected, so the console — which never defines the hook — no-ops).
    assert "window.TS_APP.onConsentDetected(server)" in inter, (
        "the shared interactive host must bridge onConsentDetected to the TS_APP seam"
    )
    app = _APP_JS.read_text(encoding="utf-8")
    assert "_pendingConsentServers" in app
    assert "function _onConsentDetected" in app
    assert "window.TS_APP.onConsentDetected = _onConsentDetected" in app, (
        "the standalone must expose _onConsentDetected on the TS_APP seam for the pane bridge"
    )


def test_consent_badge_drives_rail_manage_row() -> None:
    """The pending-consent badge was re-homed off the retired settings gear
    (``#settings-btn``, deleted in the L-shell renovation, which silently made
    the badge invisible) onto the rail's Manage > Connections row.  Classic
    app.js can't import the ESM rail module, so it drives the rail's generic
    ``setRowBadge`` hook through the ``window.TS_SHELL`` bridge — keyed on the
    standalone's Connections tab.  Pin the new lane and the absence of the dead
    gear lookup."""
    app = _APP_JS.read_text(encoding="utf-8")
    # The badge refresh must drive the rail bridge, not the deleted gear.
    assert 'getElementById("settings-btn")' not in app, (
        "the consent badge must no longer target the retired #settings-btn gear"
    )
    assert "shell.setRowBadge(_CONSENT_BADGE_TAB" in app, (
        "_refreshConsentBadge must drive the rail Manage-row badge via the TS_SHELL bridge"
    )
    assert 'const _CONSENT_BADGE_TAB = "connections"' in app, (
        "the standalone badge rides the Connections Manage tab (its MCP surface)"
    )
    # The hydrate + clear paths must still funnel through the single refresh.
    assert "function loadPendingConsents" in app and "_refreshConsentBadge()" in app


def test_media_player_activation_not_duplicated_in_standalone() -> None:
    """The media-player activation (``_loadHls`` / ``_activatePlayer`` + the
    click/keydown delegate) moved into the shared interactive pane so BOTH the
    standalone server and the console activate the Play button.  The standalone
    app.js must NOT keep its own copy — a duplicate document-level listener
    would double-fire on the standalone (two players swapped in) while the lift
    is what fixed the console (where app.js was never the host).  Pin the
    standalone clean so the stale copy can't drift back in."""
    app = _APP_JS.read_text(encoding="utf-8")
    for name in ("_loadHls", "_activatePlayer", "_isHlsUrl", "media-play-btn"):
        assert name not in app, (
            f"standalone app.js must not re-declare the lifted media player "
            f"({name!r}) — it lives in shared_static/interactive.js now"
        )
    # The lift target carries the real implementation (the click delegate too).
    inter = _INTERACTIVE_JS.read_text(encoding="utf-8")
    assert "function _activatePlayer(" in inter
    assert "activateMediaPlayButton(btn)" in inter


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
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = _pane_method_offset(body, "appendToolOutput")
    end = _pane_method_offset(body, "sendMessage")
    fn = body[start:end]
    parse_idx = fn.find("tryParseMcpError(")
    # The plain-output render is the shared renderCollapsibleOutput helper; the
    # ordering invariant is unchanged — MCP dispatch must precede it.
    render_idx = fn.find("renderCollapsibleOutput(")
    assert parse_idx >= 0, (
        "appendToolOutput must call tryParseMcpError on the error path "
        "before the plain renderer, otherwise the consent card never "
        "replaces the plain JSON output."
    )
    assert render_idx >= 0, "renderCollapsibleOutput call must remain present"
    assert parse_idx < render_idx, (
        "tryParseMcpError must run BEFORE the plain renderer so the "
        "interactive card path takes precedence over plain rendering."
    )


_UTILS_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/utils.js"
_AUTH_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/auth.js"
_KB_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/kb.js"
_COORD_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/console/static/coordinator/coordinator.js"
)
_CONSOLE_ADMIN_JS = Path(__file__).resolve().parent.parent / "turnstone/console/static/admin.js"
_CONSOLE_GOVERNANCE_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/console/static/governance.js"
)


_UNSAFE_CODE_SINK_LINT_TARGETS = [
    ("turnstone/ui/static/app.js", _INTERACTIVE_JS),
    ("turnstone/shared_static/utils.js", _UTILS_JS),
    ("turnstone/shared_static/auth.js", _AUTH_JS),
    ("turnstone/shared_static/kb.js", _KB_JS),
    ("turnstone/console/static/coordinator/coordinator.js", _COORD_JS),
    ("turnstone/console/static/admin.js", _CONSOLE_ADMIN_JS),
    ("turnstone/console/static/governance.js", _CONSOLE_GOVERNANCE_JS),
    ("turnstone/console/static/app.js", _CONSOLE_APP_JS),
]


@pytest.mark.parametrize(
    "label,path",
    _UNSAFE_CODE_SINK_LINT_TARGETS,
    ids=[label for label, _ in _UNSAFE_CODE_SINK_LINT_TARGETS],
)
def test_no_unsafe_code_sinks_in_static_assets(label: str, path: Path) -> None:
    """Whole-file pin: no direct DOM-write *or* dynamic-code sinks
    in any of the static JS bundles that render LLM output, tool
    results, operator-supplied data, or user input.  Covers
    inner/outer-HTML assignment (plain and concat), insertAdjacentHTML,
    legacy doc-write, string-eval, dynamic-Function constructor, and
    string-first-arg timer scheduling.

    Two distinct cleanup postures across the targets:

    1. **Strict DOM-construction** (``ui/static/app.js``,
       ``shared_static/utils.js``, ``shared_static/auth.js``,
       ``shared_static/kb.js``, ``coordinator.js`` chat entry,
       ``console/static/app.js``): renderer output routes through
       ``setMarkdown`` (or ``setSafeHtml`` for pre-baked HTML strings);
       every other site uses ``createElement`` + ``textContent`` +
       ``append`` / ``replaceChildren``.  Missing escapes are
       structurally impossible — no HTML string is ever interpolated.
    2. **Sink-free string-concat** (``console/static/admin.js``,
       ``console/static/governance.js``): operator-facing admin /
       governance pages still build HTML via ``escapeHtml`` + string
       concat, but the unsafe sink is off the call site (everything
       routes through ``setSafeHtml``).  XSS defence still depends on
       every interpolated value going through escapeHtml; the lint
       catches the sink but cannot catch a missing escape.

    All admin-side bundles are now covered.

    The regex covers inner/outer-HTML assignment (plain and
    concat-assignment), ``insertAdjacentHTML``, legacy doc-write, and
    the dynamic-code constructors (string-eval, dynamic-Function,
    string-first-arg timer scheduling).  ``insertAdjacentElement`` is
    intentionally not flagged — it takes a DOM node, not a string.

    Parametrized so each target is its own pytest case — a failure on
    one file is attributed precisely without masking offenders in the
    others.

    Scans the whole file body (not line-by-line) so the regex's
    ``\\s*`` can span newlines and catch multi-line sinks like
    ``el.innerHTML\\n  = X``.  Match positions map back to line
    numbers for the failure message."""
    body = path.read_text(encoding="utf-8")
    lines = body.splitlines()
    offenders: list[tuple[int, str]] = []
    for m in _UNSAFE_CODE_SINK_RE.finditer(body):
        line_no = body.count("\n", 0, m.start()) + 1
        offenders.append((line_no, lines[line_no - 1].rstrip()))
    assert not offenders, (
        f"Found {len(offenders)} unsafe code/DOM sink(s) in "
        f"{label}:\n"
        + "\n".join(f"  line {n}: {line}" for n, line in offenders[:10])
        + "\nUse DOM construction (createElement + textContent + "
        "append/replaceChildren) or route renderer output through "
        "setMarkdown() / setSafeHtml() in shared/utils.js."
    )


def test_perception_role_surfaced_in_admin_and_filtered_from_settings() -> None:
    """The perception fallback (``perception.model_alias``) landed backend-only —
    its admin UI was missing.  It must (1) appear as a Models → Roles row so an
    operator can point it at a vision / omni model, and (2) be filtered OUT of the
    raw Settings tab.  The Settings filter derives its skip-set from MODEL_ROLES,
    so no role can quietly drift back into the Settings list again (the original
    miss — stt/tts/reranker had leaked the same way)."""
    admin = _CONSOLE_ADMIN_JS.read_text(encoding="utf-8")
    # (1) Roles sub-tab row.
    assert 'label: "Perception"' in admin, "the perception role must carry a UX label"
    assert '"perception.model_alias"' in admin, "perception must have a MODEL_ROLES entry"
    # (2) Settings filter derives the skip-set from MODEL_ROLES — drift-proof, so
    # perception (and every other role alias) is excluded, not hand-listed.
    assert "for (let ri = 0; ri < MODEL_ROLES.length; ri++)" in admin, (
        "the Settings role-key filter must derive from MODEL_ROLES"
    )
    assert "roleKeys[MODEL_ROLES[ri].aliasKey] = 1" in admin


def test_audio_roles_gated_to_openai_sdk_providers() -> None:
    """Voice roles (stt/tts) ride the OpenAI-SDK audio surface — an Anthropic
    (-compatible) model has no audio content block, so admin must exclude it
    from those role dropdowns (mirrors ``_provider_carries_audio`` in
    core/audio.py).  Reranker is a ``/rerank`` endpoint, not audio, so it must
    NOT be provider-gated."""
    admin = _CONSOLE_ADMIN_JS.read_text(encoding="utf-8")
    assert "function _providerCarriesAudio(" in admin, "the provider-audio gate helper must exist"
    body = admin[admin.index("function _audioModelEligible(") :]
    body = body[: body.index("\nfunction ")]
    assert '(mediaRole === "stt" || mediaRole === "tts")' in body, (
        "only the voice roles are provider-gated (reranker is not an audio role)"
    )
    # A blank/unset provider must default to "openai" (matches the backend's
    # _provider_carries_audio), else a provider-less model is wrongly excluded.
    assert '_providerCarriesAudio((md && md.provider) || "openai")' in body


def test_model_response_controls_are_capability_driven_and_sparse() -> None:
    """The model shelf surfaces Responses-only scalar controls without
    hard-coding GPT-5.6 IDs or pinning inherited capability-table values."""
    html = _CONSOLE_INDEX.read_text(encoding="utf-8")
    admin = _CONSOLE_ADMIN_JS.read_text(encoding="utf-8")

    assert 'id="model-response-controls"' in html
    assert 'aria-labelledby="model-response-controls-title"' in html
    assert 'id="model-output-verbosity"' in html
    assert 'for="model-output-verbosity"' in html
    assert 'id="model-reasoning-mode"' in html
    assert 'for="model-reasoning-mode"' in html
    for value in ("low", "medium", "high"):
        assert f'<option value="{value}">' in html
    for value in ("standard", "pro"):
        assert f'<option value="{value}">' in html
    assert 'data-cap="supports_verbosity"' in html
    assert 'data-cap="supports_pro_mode"' in html

    assert '"supports_verbosity"' in admin
    assert '"supports_pro_mode"' in admin
    surface = _slice_function_body(admin, "_modelUsesResponsesSurface")
    assert surface is not None
    assert 'provider === "openai"' in surface
    assert 'provider === "openai-compatible"' in surface
    assert 'value === "responses"' in surface
    visibility = _slice_function_body(admin, "_updateModelResponseControls")
    assert visibility is not None
    assert "_modelGetTile(spec.supportKey)" in visibility
    assert 'supportKey: "supports_verbosity"' in admin
    assert 'supportKey: "supports_pro_mode"' in admin
    assert "gpt-5.6" not in visibility, "visibility must come from capabilities, not model IDs"

    assert "function _captureModelResponseControls(" in admin
    assert "function _mergeModelResponseControls(" in admin
    assert "_captureModelResponseControls(capsObj)" in admin
    assert "_mergeModelResponseControls(caps)" in admin
    assert "let _modelResponseCaptured = {};" in admin
    assert "let _modelResponseDirty = {};" in admin
    assert "_modelResponseCaptured[spec.key] = value" in admin
    assert "nextIdentity === _modelResponseInitialIdentity" in admin
    identity = _slice_function_body(admin, "_modelIdentity")
    assert identity is not None
    assert 'provider === "openai-compatible"' in identity
    assert ': ""' in identity
    merge = _slice_function_body(admin, "_mergeModelResponseControls")
    assert merge is not None
    # The dirty flag (select touched) may only override Advanced JSON for
    # the identity that made it dirty — a stale flag from a renamed row
    # must not delete a hand-typed JSON key.
    assert "if (_modelResponseDirty[spec.key] && sameIdentity) delete caps[spec.key]" in merge
    # The captured-value fallback is load-bearing, not a gating bug: a value
    # lifted out of the row JSON on edit-open must stay visible and re-save
    # for the same identity even when the baseline table says unsupported.
    # The baseline arrives async (or never, on the compat lane); yielding to
    # it would silently drop the pinned value on an unrelated edit-save.
    # Wire safety lives server-side (emission gates on merged supports_*).
    for body in (visibility, merge):
        assert "_modelGetTile(spec.supportKey) || capturedFallback" in body
        assert "sameIdentity" in body
        assert "!(spec.supportKey in _modelCapsExplicit)" in body

    create = _slice_function_body(admin, "showCreateModelModal")
    assert create is not None
    assert "_modelCapsSeq++" in create, "a fresh shelf must invalidate prior lookups"

    assert "displayCaps.supports_verbosity !== false" in admin
    assert "displayCaps.supports_pro_mode !== false" in admin

    change = _slice_function_body(admin, "_onModelFieldChange")
    assert change is not None
    assert "_modelCapsSeq++" in change, "model changes must invalidate in-flight baselines"
    assert "_modelCapsBaseline = {}" in change
    assert 'apiSurfEl.addEventListener("change", _onModelFieldChange)' in admin


def test_shared_utils_defines_set_markdown_helper() -> None:
    """The ``setMarkdown`` helper in ``shared/utils.js`` is the single
    audited entry point for rendering markdown content into a DOM
    element from ``app.js``.  It parses ``renderMarkdown``'s output via
    ``DOMParser`` (avoiding the unsafe sink entirely) and runs
    ``postRenderMarkdown`` on the result.  A refactor that drops or
    renames it would break the two interactive call sites silently at
    runtime."""
    body = _UTILS_JS.read_text(encoding="utf-8")
    assert "function setMarkdown(el, content)" in body, (
        "shared/utils.js must define setMarkdown(el, content) — "
        "app.js routes both renderer-output sites through this helper."
    )
    # The DOMParser path is what avoids the unsafe sink.  The absence
    # of the unsafe assignment inside the helper is pinned by the
    # broader ``test_no_unsafe_code_sinks_in_static_assets`` scan
    # above; pin DOMParser presence here too so a refactor that swaps
    # to e.g. ``Range.createContextualFragment`` forces an explicit
    # reviewer decision.
    assert "DOMParser()" in body, (
        "setMarkdown must parse via DOMParser, not the unsafe DOM-write "
        "sink — that is what keeps the audit surface at one location."
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
    assert not _UNSAFE_CODE_SINK_RE.search(section), (
        "Section 15 must not assign to the unsafe DOM-write property — "
        "server names and scope values flow through here and would be "
        "XSS-injectable. Use textContent / DOM APIs instead."
    )


def test_gear_retired_mcp_in_manage_pane() -> None:
    """Step 6: the floating settings gear is retired — no #settings-btn, no
    toggle/open/close gear handlers.  MCP server connections moved into the
    Admin pane's Connections panel (#view-admin), reached via the rail's
    Manage > Connections row (the TS_ADMIN seam)."""
    index = _INDEX_HTML.read_text(encoding="utf-8")
    app = _APP_JS.read_text(encoding="utf-8")
    assert 'id="settings-btn"' not in index, "the floating settings gear is retired."
    assert "toggleSettingsMenu" not in app, "the gear dropdown handlers are retired."
    assert 'id="view-admin"' in index and 'id="settings-mcp-table"' in index, (
        "MCP connections render into the Admin pane's #view-admin panel."
    )
    assert "window.TS_ADMIN.openTab = function" in app and '"connections"' in app, (
        "the Manage > Connections row opens the MCP panel via the TS_ADMIN seam."
    )


def test_dashboard_is_the_main_pane_body() -> None:
    """In the L-shell the dashboard is the Dashboard pane's body (#main) — the
    shell adopts #main — not a floating overlay.  It holds the launcher + the
    workstreams table and is not a modal."""
    body = _INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="main"' in body, "the dashboard content lives in #main (the Dashboard pane body)."
    start = body.index('id="main"')
    # Window spans the launcher (composer + options) through the workstreams
    # table — it grows as launcher options are added (e.g. the project picker),
    # so the bound just needs to keep BOTH inside #main, not be tight.
    chunk = body[start : start + 4500]
    assert 'id="dashboard-input"' in chunk and 'id="dash-ws-table"' in chunk, (
        "#main must hold the new-session launcher + the workstreams table."
    )
    assert 'class="dashboard-overlay"' not in body, "the fixed dashboard overlay is retired."


def test_mcp_connections_panel_and_revoke_modal_in_index_html() -> None:
    """MCP connections moved from the floating #settings-overlay into the Admin
    pane's Connections panel (#view-admin), reusing the same #settings-mcp-*
    table ids so the render code is unchanged.  The revoke confirm lives on the
    hatch dialog tier (native document-modal)."""
    body = _INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="settings-overlay"' not in body, "the floating MCP settings overlay is retired."
    assert 'id="view-admin"' in body, "the Admin pane host (#view-admin) must exist."
    va = body.index('id="view-admin"')
    panel = body[va : va + 1500]
    assert 'id="settings-mcp-table"' in panel and 'id="settings-mcp-tbody"' in panel, (
        "the MCP table (reused ids) must live inside #view-admin."
    )
    idx = body.index('id="revoke-mcp-dialog"')
    chunk = body[max(0, idx - 200) : idx + 600]
    assert "hatch--dialog" in chunk and 'role="alertdialog"' in chunk, (
        "the revoke confirm is a hatch dialog-tier alertdialog "
        "(native showModal supplies modality — no aria-modal attribute)."
    )


def test_phase8_xss_safe_render_in_build_mcp_error_embed() -> None:
    """Adversarial input — the renderer for an MCP error envelope
    must use ``textContent`` (not the unsafe DOM-write API) for every
    field that flows from the server: ``err.detail``, ``err.server``,
    scopes list. The card builder uses createElement + textContent
    throughout so a script-tag server name renders harmlessly. Pin
    the absence of the unsafe-write inside ``buildMcpErrorEmbed``."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = body.index("function buildMcpErrorEmbed(")
    # Bound to the function body — find its closing brace at column 0.
    rest = body[start:]
    # Closing function brace at line start (matches existing functions)
    end_match = re.search(r"\n}\n", rest)
    assert end_match is not None
    fn = rest[: end_match.end()]
    assert not _UNSAFE_CODE_SINK_RE.search(fn), (
        "buildMcpErrorEmbed must not use the unsafe-DOM-write API — "
        "server names and detail strings flow through here. An "
        "adversarial server name must render harmlessly via "
        "textContent."
    )


def test_mcp_error_button_gated_on_consent_url_not_code_alone() -> None:
    """Review finding: the chat error card rendered a Connect / Re-consent
    button from the error CODE alone, so an oauth_obo error (consent_url=None,
    since sign-in passthrough has no per-server consent flow and /start rejects
    obo rows) produced a button that dead-ended in a 'no consent URL' toast.
    The button must render only when a valid per-server consent URL is present —
    obo errors show the card's honest detail text without a broken affordance."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    start = body.index("function buildMcpErrorEmbed(")
    rest = body[start:]
    end_match = re.search(r"\n}\n", rest)
    assert end_match is not None
    fn = rest[: end_match.end()]
    # The render gate combines the category with a consent-URL presence check.
    assert "hasConsentAffordance" in fn, (
        "buildMcpErrorEmbed must gate the action button on the presence of a "
        "consent URL, not on the error category alone."
    )
    assert 'category === "actionable" && hasConsentAffordance' in fn, (
        "the button-render condition must require BOTH an actionable category "
        "and a real consent URL"
    )


def test_phase8_css_classes_present_in_stylesheet() -> None:
    """The MCP error-embed + connections classes app.js/interactive.js reference
    must keep their CSS rules (else the consent / connections UX silently loses
    its visual treatment). The settings OVERLAY is retired in step 6 — MCP
    connections render in the Admin pane's Connections panel (#view-admin), not a
    floating dialog — so #settings-overlay / #settings-box are no longer pinned.
    The revoke confirm's chrome moved to /shared/hatch.css with the dialog-tier
    conversion, so no #revoke-mcp-* rule is pinned here either.  The pending-
    consent badge moved off the retired settings gear onto the rail's Manage row
    (shell.css `.rail-badge`), so `.settings-consent-badge` is gone from here."""
    css = _STYLE_CSS.read_text(encoding="utf-8")
    for selector in [
        ".mcp-error-card",
        ".mcp-error-icon",
        ".mcp-error-action-btn",
        ".mcp-scope-pill",
        ".settings-revoke-btn",
    ]:
        assert selector in css, f"Missing CSS rule for {selector}"
    # The dead gear-badge rule must be GONE (its host #settings-btn was retired).
    assert ".settings-consent-badge" not in css, (
        "the retired settings-gear consent badge CSS must be removed "
        "(the badge now lives on the rail Manage row — shell.css .rail-badge)"
    )


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
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Post-var-sweep invariants — added by chore/interactive-var-sweep
# ---------------------------------------------------------------------------
#
# After the whole-file var → const/let sweep across these 7 bundles, three
# guards keep the post-sweep state honest:
#   1. ``node --check`` per bundle catches parse-level regressions on any
#      future edit (mis-balanced braces, stray tokens) before they reach
#      the browser.
#   2. A var-free static assertion pins the keyword sweep — any future
#      ``var`` declaration in these bundles fails CI loudly.
#   3. A static const-reassign guard catches the specific bug class that
#      shipped through the original sweep (``const X = …; … X = …``
#      throws ``TypeError`` only at call-time, which ``node --check``
#      does not surface).  This is the same paren/string/regex-aware
#      reassignment check the sweep walker uses.
#
# A fourth guard runs ``_redactApiKeys`` via ``node -e`` as a runtime
# smoke; the function is pure (no DOM dependency) so it transplants
# cleanly into a standalone node invocation.


def _slice_balanced_body(body: str, anchor: int) -> str | None:
    """Slice ``body`` from ``anchor`` (which must point at or just before
    the opening ``{`` of a block) up to and including the matching ``}``.
    Tracks brace depth + string state so the slice is robust to comment
    growth and arbitrary body reorganisation.  Returns ``None`` if the
    matching brace isn't found within a reasonable window.

    Used to slice JS handler / function bodies for static assertions
    without committing to a fixed character window."""
    n = len(body)
    i = body.find("{", anchor)
    if i == -1 or i - anchor > 200:
        return None
    depth = 0
    in_str: str | None = None
    start = i
    while i < n and i - start < 8000:
        ch = body[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return body[start : i + 1]
        i += 1
    return None


def _slice_listener_body(body: str, event_name: str) -> str | None:
    """Return the handler-function body registered via
    ``addEventListener("<event_name>", function ...)``, sliced by
    matching braces (robust to comment / formatting growth)."""
    anchor = body.find(f'addEventListener("{event_name}"')
    if anchor == -1:
        return None
    return _slice_balanced_body(body, anchor)


def _slice_function_body(body: str, fn_name: str) -> str | None:
    """Return the body of ``function <fn_name>(...) { ... }`` sliced by
    matching braces."""
    m = re.search(r"function\s+" + re.escape(fn_name) + r"\s*\(", body)
    if m is None:
        return None
    return _slice_balanced_body(body, m.start())


_REPO_ROOT = Path(__file__).resolve().parent.parent
# CLASSIC bundles that completed the var → const/let sweep.  Add a new JS
# file here only after it has itself been swept — the var-free +
# const-reassign guards below will otherwise fail loudly on any pre-sweep
# `var` it contains.  coordinator.js is intentionally excluded (already
# modern; 3 surviving `var` are by design per the sweep briefing).  The
# shared_static files that used to sit here (auth/kb/utils) are ES modules
# now — test_shell_js.py sweeps them with module semantics.
_SWEPT_BUNDLES = [
    _REPO_ROOT / "turnstone/ui/static/app.js",
    _REPO_ROOT / "turnstone/console/static/admin.js",
    _REPO_ROOT / "turnstone/console/static/governance.js",
    _REPO_ROOT / "turnstone/console/static/app.js",
]

# The const-reassign analysis below is pure text — module vs script semantics
# is irrelevant — so the var-free ES modules ride the same guard (their parse
# + var + sink guards live in test_shell_js.py).
_CONST_GUARD_BUNDLES = _SWEPT_BUNDLES + [
    _REPO_ROOT / "turnstone/shared_static/auth.js",
    _REPO_ROOT / "turnstone/shared_static/kb.js",
    _REPO_ROOT / "turnstone/shared_static/utils.js",
    _REPO_ROOT / "turnstone/shared_static/toast.js",
    _REPO_ROOT / "turnstone/shared_static/shell.js",
    _REPO_ROOT / "turnstone/shared_static/pane.js",
    _REPO_ROOT / "turnstone/shared_static/rail.js",
    _REPO_ROOT / "turnstone/shared_static/interactive.js",
    _REPO_ROOT / "turnstone/shared_static/conversation.js",
    _REPO_ROOT / "turnstone/shared_static/redact_credentials.js",
]


@pytest.mark.parametrize("bundle", _SWEPT_BUNDLES, ids=lambda p: p.name)
def test_swept_bundle_parses(bundle: Path) -> None:
    """``node --check`` each swept bundle.  Catches syntax-level
    regressions (a future edit that drops a brace, mis-balances a
    string, etc.) before they reach the browser.  Skipped silently if
    ``node`` is not on PATH so local dev without Node still passes."""
    node = "node"
    try:
        proc = subprocess.run(
            [node, "--check", str(bundle)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    assert proc.returncode == 0, f"node --check failed for {bundle.name}:\n{proc.stderr}"


@pytest.mark.parametrize("bundle", _SWEPT_BUNDLES, ids=lambda p: p.name)
def test_swept_bundle_has_no_var_decl(bundle: Path) -> None:
    """Pin the var-free post-sweep state across all 7 bundles.  A
    future ``var`` declaration here fails CI loudly so the sweep
    doesn't regress in patches."""
    body = bundle.read_text(encoding="utf-8")
    # Line-start ``var`` declarations.
    line_start = re.findall(r"^\s*var\s+\w", body, re.MULTILINE)
    # ``for (var i …)`` counters anywhere on a line.
    for_init = re.findall(r"\bfor\s*\(\s*var\s+", body)
    stray = line_start + for_init
    assert not stray, (
        f"{bundle.name}: {len(stray)} stray ``var`` declarations found "
        f"after the var-sweep — the post-sweep invariant is broken.  "
        f"Convert to ``const``/``let``."
    )


def _strip_strings_and_line_comments(line: str) -> str:
    """Return ``line`` with string-literal contents and ``// …`` tails
    removed, so simple regex-based scanning can't be tricked by an
    identifier embedded in a CSS class name or HTML attribute.
    Mirrors the sweep walker's helper of the same purpose."""
    out: list[str] = []
    i = 0
    n = len(line)
    in_str: str | None = None
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


_REGEX_OK_KEYWORDS = frozenset(
    {
        "return",
        "throw",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "do",
        "yield",
        "await",
        "case",
        "else",
    }
)


def _is_regex_context_at(text: str, slash_pos: int) -> bool:
    """``text[slash_pos]`` is ``/``.  Return ``True`` if it starts a regex
    literal vs the division operator, by inspecting the previous significant
    char (skipping whitespace and ``/* */`` block comments going backward)."""
    i = slash_pos - 1
    while i >= 0:
        ch = text[i]
        if ch.isspace():
            i -= 1
            continue
        if ch == "/" and i >= 1 and text[i - 1] == "*":
            open_i = text.rfind("/*", 0, i - 1)
            if open_i == -1:
                return True
            i = open_i - 1
            continue
        if ch.isalnum() or ch in "_$":
            k = i
            while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
                k -= 1
            ident = text[k + 1 : i + 1]
            return ident in _REGEX_OK_KEYWORDS
        return ch not in ")]"
    return True


def _consume_regex_at(text: str, start: int) -> tuple[int, bool]:
    """Consume regex literal starting at ``text[start] == '/'``.  Returns
    ``(end_pos, ok)``.  Handles backslash escapes and ``[...]`` char classes
    (a ``/`` inside a class doesn't end the regex)."""
    n = len(text)
    i = start + 1
    in_class = False
    while i < n:
        ch = text[i]
        if ch == "\n":
            return start, False
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            i += 1
            while i < n and text[i] in "gimsuyd":
                i += 1
            return i, True
        i += 1
    return start, False


def _build_brace_map(
    text: str,
) -> tuple[dict[int, int], list[int]]:
    """Walk ``text`` once.  Returns ``(open_to_close, line_starts)`` where
    ``open_to_close[open_off] = close_off`` for matched braces, and
    ``line_starts[i]`` is the char offset where line index ``i`` (0-based)
    begins.  Robust to JS regex literals, strings, ``//`` and ``/* */``
    comments."""
    n = len(text)
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)
    stack: list[int] = []
    open_to_close: dict[int, int] = {}
    in_str: str | None = None
    in_comment: str | None = None
    i = 0
    while i < n:
        ch = text[i]
        if in_comment == "//":
            if ch == "\n":
                in_comment = None
            i += 1
            continue
        if in_comment == "/*":
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                in_comment = None
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                in_comment = "//"
                i += 2
                continue
            if text[i + 1] == "*":
                in_comment = "/*"
                i += 2
                continue
            if _is_regex_context_at(text, i):
                end, ok = _consume_regex_at(text, i)
                if ok:
                    i = end
                    continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            open_to_close[stack.pop()] = i
        i += 1
    return open_to_close, line_starts


def _offset_to_line(line_starts: list[int], off: int) -> int:
    lo, hi = 0, len(line_starts)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= off:
            lo = mid
        else:
            hi = mid
    return lo


def _enclosing_block(
    decl_offset: int,
    open_to_close: dict[int, int],
    line_starts: list[int],
    total_lines: int,
) -> tuple[int, int]:
    """Innermost block containing ``decl_offset``.  ``(start_line, end_line)``
    inclusive.  Returns ``(0, total_lines - 1)`` when at top-level."""
    candidates = [(op, cl) for op, cl in open_to_close.items() if op < decl_offset < cl]
    if not candidates:
        return 0, total_lines - 1
    op, cl = max(candidates, key=lambda x: x[0])
    return (
        _offset_to_line(line_starts, op),
        _offset_to_line(line_starts, cl),
    )


@pytest.mark.parametrize("bundle", _CONST_GUARD_BUNDLES, ids=lambda p: p.name)
def test_swept_bundle_has_no_const_reassign(bundle: Path) -> None:
    """For each ``const X = …`` declaration, fail if X is reassigned
    *within the same block scope* (``X = …``, ``X +=``, ``X++``, ``++X``,
    etc., with lookbehind to skip ``obj.X = …`` property writes).  Block
    scope is found by brace-tracking with regex/string/comment awareness,
    so a same-named ``let X`` in an unrelated function doesn't
    false-positive against a ``const X`` in this one.  Caught the
    original ``_redactApiKeys`` shipped bug (postfix ``redacted = …``)
    and a sibling ``++_paneCounter`` prefix-increment that the first
    iteration of this guard missed — both were ``TypeError`` at
    call-time, invisible to ``node --check`` and to whole-file
    keyword scans."""
    body = bundle.read_text(encoding="utf-8")
    lines = body.splitlines()
    open_to_close, line_starts = _build_brace_map(body)
    const_decl = re.compile(r"^(\s*)const\s+(\w+)\b")
    bugs: list[tuple[int, str, int, str, str]] = []
    for idx, line in enumerate(lines):
        m = const_decl.match(line)
        if not m:
            continue
        name = m.group(2)
        decl_offset = line_starts[idx] + len(m.group(1))
        start_line, end_line = _enclosing_block(decl_offset, open_to_close, line_starts, len(lines))
        # Reassignment forms: postfix `X++`/`X--`, prefix `++X`/`--X`,
        # compound `X +=`/`X -=`/.../`X ??=`, plain `X =` (not ==/===).
        # Negative lookbehind skips property writes (`obj.X = …`).
        pat = re.compile(
            r"(?:"
            r"(?<![A-Za-z0-9_$])(?:\+\+|--)"  # prefix `++X` / `--X`
            + re.escape(name)
            + r"(?![A-Za-z0-9_$])"
            + r"|"
            r"(?<![A-Za-z0-9_$.])"
            + re.escape(name)
            + r"\s*(?:\+\+|--|"  # postfix `X++` / `X--`
            + r"(?:\+|-|\*\*?|/|%|&&?|\|\|?|\^|<<|>>>?|\?\?)=|"  # compound
            + r"=(?!=))"  # plain `X =`
            + r")"
        )
        decl_other = re.compile(
            r"(?:^\s*(?:let|const|var)\s+|\bfor\s*\(\s*(?:let|const|var)\s+)"
            + re.escape(name)
            + r"\b"
        )
        param = re.compile(r"\((?:[^()]*?,\s*)?" + re.escape(name) + r"\s*[,)]")
        for j in range(start_line, end_line + 1):
            if j == idx:
                continue
            stripped = _strip_strings_and_line_comments(lines[j])
            if not pat.search(stripped):
                continue
            if decl_other.search(stripped):
                continue
            if param.search(stripped):
                cleaned = param.sub("(", stripped)
                if not pat.search(cleaned):
                    continue
            bugs.append((idx + 1, name, j + 1, lines[idx].strip(), lines[j].strip()))
            break
    if bugs:
        detail = "\n".join(
            f"  {bundle.name}:{decl_ln} const {name} reassigned at "
            f"{bundle.name}:{reass_ln}\n    decl:   {decl_text}\n    reass:  {reass_text}"
            for decl_ln, name, reass_ln, decl_text, reass_text in bugs[:3]
        )
        suffix = f"\n  ... and {len(bugs) - 3} more" if len(bugs) > 3 else ""
        raise AssertionError(
            f"const declaration(s) reassigned within block scope.  "
            f"Change to `let` or eliminate the reassignment:\n{detail}{suffix}"
        )


def test_redact_credentials_runtime_smoke() -> None:
    """Runtime smoke for ``redactCredentials`` via a temp harness file.
    The function is pure (no DOM dependency).  Tests the shared module
    directly via ESM import (replaces the legacy ``_redactApiKeys`` test
    which now delegates to this).

    The tempfile is written with a ``.mjs`` extension so Node forces ESM
    parsing regardless of any ``package.json`` ``type`` field in parent
    directories.  The ``redact_credentials.js`` source file is imported
    by absolute path so resolution is unambiguous.
    """
    import tempfile

    mod_path = _REDACT_CREDENTIALS_JS.resolve()
    harness = (
        "import { redactCredentials } from "
        + json.dumps(str(mod_path))
        + ";\n"
        + "const q = redactCredentials('https://x?api_key=abc&u=foo');\n"
        + 'if (q !== "https://x?api_key=***&u=foo") '
        + "throw new Error('query-string redact failed: ' + q);\n"
        + 'const j = redactCredentials(\'{"api_key":"abc"}\');\n'
        + 'if (j !== \'{"api_key":"***"}\') '
        + "throw new Error('json-style redact failed: ' + j);\n"
        + "// Bearer token redaction (raw input)\n"
        + "const b = redactCredentials('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.test-token_here');\n"
        + "if (!b.includes('[REDACTED:api_key]')) "
        + "throw new Error('bearer redact failed: ' + b);\n"
        + "// Connection string redaction (raw input)\n"
        + "const c = redactCredentials('postgresql://user:supersecret@localhost/db');\n"
        + "if (!c.includes('[REDACTED:password]')) "
        + "throw new Error('conn-string redact failed: ' + c);\n"
        + "// Authorization JSON key redaction (step 6 comprehensive)\n"
        + 'const a = redactCredentials(\'{"Authorization": "Bearer canstillseethis"}\');\n'
        + "if (!a.includes('[REDACTED:secret]')) "
        + "throw new Error('authorization JSON redact failed: ' + a);\n"
        + "// Single-quote JSON (Python dict repr / JS object literal)\n"
        + "const sq = redactCredentials(\"{'Authorization': 'Bearer canstillseethis'}\");\n"
        + "if (!sq.includes('[REDACTED:secret]')) "
        + "throw new Error('single-quote authorization redact failed: ' + sq);\n"
        + "// mongodb+srv connection string (Atlas SRV)\n"
        + "const ms = redactCredentials('mongodb+srv://u:s3cretpw@cluster.mongodb.net/db');\n"
        + "if (!ms.includes('[REDACTED:password]')) "
        + "throw new Error('mongodb+srv redact failed: ' + ms);\n"
        + "// lowercase bearer scheme (RFC 7235 case-insensitive)\n"
        + "const lb = redactCredentials('authorization: bearer "
        + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig12345');\n"
        + "if (!lb.includes('[REDACTED:api_key]')) "
        + "throw new Error('lowercase bearer redact failed: ' + lb);\n"
        + "// api_key= assignment redacts the whole token, not a garbled api_[REDACTED\n"
        + "const ak = redactCredentials('api_key=abcdefghijklmnopqrstuvwxyz');\n"
        + "if (ak !== '[REDACTED:api_key]') "
        + "throw new Error('api_key= clean redact failed: ' + ak);\n"
        + "// Prefilter fast path: plain text with no anchor substring is unchanged\n"
        + "const fp = redactCredentials('build ok in 42s - 3 tests passed');\n"
        + "if (fp !== 'build ok in 42s - 3 tests passed') "
        + "throw new Error('prefilter fast-path no-op failed: ' + fp);\n"
        + "// Bare credentials with no =, quote or @ anywhere must still redact\n"
        + "// (these pin the prefilter as a superset of the pattern set)\n"
        + "const bk = redactCredentials('loaded sk-abcdefghijklmnopqrstuvwx');\n"
        + "if (bk !== 'loaded [REDACTED:api_key]') "
        + "throw new Error('bare sk- redact failed: ' + bk);\n"
        + "const aw = redactCredentials('using AKIAABCDEFGHIJKLMNOP now');\n"
        + "if (aw !== 'using [REDACTED:api_key] now') "
        + "throw new Error('bare AKIA redact failed: ' + aw);\n"
        + "const bt = redactCredentials('Bearer abcdefghijklmnopqrstuvwxyz');\n"
        + "if (bt !== '[REDACTED:api_key]') "
        + "throw new Error('bare bearer redact failed: ' + bt);\n"
        + "// SQLAlchemy dialect+driver connection URLs (psycopg2/asyncpg)\n"
        + "const pg2 = redactCredentials('postgresql+psycopg2://user:s3cret@db:5432/app');\n"
        + "if (pg2 !== 'postgresql+psycopg2://user:[REDACTED:password]@db:5432/app') "
        + "throw new Error('psycopg2 conn redact failed: ' + pg2);\n"
        + "const apg = redactCredentials('postgresql+asyncpg://user:s3cret@db/app');\n"
        + "if (apg !== 'postgresql+asyncpg://user:[REDACTED:password]@db/app') "
        + "throw new Error('asyncpg conn redact failed: ' + apg);\n"
        + "// RFC 3986 schemes are case-insensitive - uppercase must not bypass\n"
        + "const up = redactCredentials('POSTGRESQL+PSYCOPG2://user:s3cret@db/app');\n"
        + "if (up !== 'POSTGRESQL+PSYCOPG2://user:[REDACTED:password]@db/app') "
        + "throw new Error('uppercase scheme conn redact failed: ' + up);\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        proc = subprocess.run(
            ["node", tmp],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    finally:
        os.unlink(tmp)
    assert proc.returncode == 0, (
        f"redactCredentials runtime smoke failed.  stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_beforeunload_closes_global_sse() -> None:
    """The ``beforeunload`` handler closes ``globalEvtSource`` before navigation.
    In the L-shell the per-pane streams are owned by PaneManager/interactive.js,
    so this handler only owns the global Tier-1 stream."""
    body = _APP_JS.read_text(encoding="utf-8")
    handler = _slice_listener_body(body, "beforeunload")
    assert handler is not None, "beforeunload handler missing."
    assert "globalEvtSource" in handler and ".close()" in handler, (
        "beforeunload must close the global Tier-1 stream."
    )


def test_dead_sse_defensive_reconnect_registered() -> None:
    """visibilitychange + focus listeners re-open the global Tier-1 stream if it
    was closed (e.g. a cancelled navigation).  In the L-shell per-pane streams
    are PaneManager's, so the helper only revives the global SSE."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert 'addEventListener("visibilitychange"' in body
    assert 'addEventListener("focus"' in body
    helper_body = _slice_function_body(body, "_reconnectDeadSSEs")
    assert helper_body is not None, "_reconnectDeadSSEs helper missing."
    assert "EventSource" in helper_body and "connectGlobalSSE()" in helper_body, (
        "_reconnectDeadSSEs must revive the global SSE when closed."
    )


# ---------------------------------------------------------------------------
# PR-D reconnect-with-replay: onerror must preserve native EventSource
# auto-reconnect for transient errors
# ---------------------------------------------------------------------------
#
# PR-D adds a server-side per-ws ring buffer + ``Last-Event-ID`` replay so
# a brief disconnect transparently replays the missed events.  That whole
# foundation is defeated if the browser's ``onerror`` handler explicitly
# closes the EventSource on a transient network error — closing forces a
# CONNECTING -> CLOSED state transition that prevents native auto-reconnect
# from firing.  The post-PR-D contract is: never call ``.close()`` on a
# transient error; let native reconnect run with the ``Last-Event-ID``
# header.  Explicit closes survive only on terminal branches (401 expired
# session, workstream-reassignment to a different ws).  These guards pin
# the contract so a future refactor can't silently regress it.


def _strip_js_comments(src: str) -> str:
    """Strip ``//`` and ``/* */`` comments while preserving string
    literal contents (``"..."``, ``'...'``, `` `...` ``) and keeping
    byte length identical (comments replaced with spaces).

    Limitation — does NOT detect regex literals (``/pattern/flags``).
    A ``//`` inside a regex like ``/abc//`` would be misread as the
    start of a line comment.  Safe today because the regions we scan
    (SSE-handler ``onerror`` bodies, ``connectSSE`` /
    ``connectGlobalSSE`` function bodies) don't contain regex
    literals; if a future caller wants to scan a region with regex
    literals, extend the tracker first.

    Motivation: ``_slice_balanced_body`` doesn't skip comments, so an
    apostrophe inside a comment (``can't``, ``don't``) opens a fake
    string state that swallows braces until the next ``'``.  The new
    onerror handlers carry these comments routinely; stripping
    comments before brace-walking removes the hazard without
    re-architecting the existing slice helper.
    """
    out: list[str] = []
    n = len(src)
    i = 0
    in_str: str | None = None
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # Line comment: replace with spaces up to newline (preserve
        # length so downstream offset math still works).
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment: replace with spaces up to closing */.
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _onerror_block(body: str, anchor_substring: str) -> str | None:
    """Slice an ``X.onerror = ...`` handler body by matching braces.

    ``anchor_substring`` is something that uniquely identifies the
    enclosing function so we don't accidentally pick the wrong
    ``.onerror = function ...`` (the file has several).  Returns the
    body between the matching braces, or ``None`` if not found.
    Strips comments first so apostrophes in comment prose can't
    desync the brace walker.
    """
    stripped = _strip_js_comments(body)
    anchor = stripped.find(anchor_substring)
    if anchor == -1:
        return None
    onerror = stripped.find(".onerror", anchor)
    if onerror == -1:
        return None
    return _slice_balanced_body(stripped, onerror)


def _onerror_preserves_native_reconnect(body: str, source_var: str) -> tuple[bool, str]:
    """Return (passed, reason).

    ``source_var`` is the EventSource handle (e.g. ``this.evtSource``,
    ``globalEvtSource``, ``evtSource``).  An onerror handler passes if:
      1. Either it never calls ``source_var.close()`` directly OR every
         such close is inside a 401-detection branch / login-overlay
         early-return / wsId-reassignment branch (allowed terminal
         exits).
      2. OR the handler explicitly references ``last_event_id`` —
         escape hatch for a future redesign that abandons native
         reconnect entirely but takes explicit responsibility for the
         replay header.
    """
    # If the body threads last_event_id, the implementer has taken
    # explicit responsibility for the replay header — escape hatch.
    if "last_event_id" in body or "lastEventId" in body:
        # Caller still has to ensure the body doesn't ALSO have a
        # naked close() outside a terminal branch; rely on the regex
        # search below as well.
        pass
    # Walk lines, track depth of common terminal branches.  Simple
    # heuristic: any ``source_var.close()`` line that isn't preceded by
    # ``status === 401`` or ``loginOverlay`` or ``disconnectSSE()`` in
    # the surrounding line window is a defect.
    pattern = re.compile(
        re.escape(source_var) + r"\.close\(\s*\)",
    )
    matches = list(pattern.finditer(body))
    if not matches:
        return True, "no close() calls — native reconnect preserved"
    for m in matches:
        start = m.start()
        # Look back ~400 chars for a terminal-branch marker on the
        # same conditional path.  ``r.status === 401`` is the canonical
        # 401-detection guard; ``loginOverlay`` is the login-modal
        # early-return; ``disconnectSSE()`` immediately followed by
        # setting a new wsId is the reassignment path.
        window = body[max(0, start - 400) : start]
        is_401_branch = "status === 401" in window or "r.status === 401" in window
        is_login_branch = "loginOverlay" in window
        is_reassign_branch = "disconnectSSE()" in window
        if not (is_401_branch or is_login_branch or is_reassign_branch):
            snippet = body[max(0, start - 80) : min(len(body), start + 80)]
            return False, (
                f"naked {source_var}.close() at offset {start} — would "
                f"defeat native auto-reconnect for transient errors. "
                f"Context: ...{snippet}..."
            )
    return True, "all close() calls are in terminal branches (401 / login / reassign)"


def test_pane_connectsse_onerror_preserves_native_reconnect() -> None:
    """``Pane.connectSSE``'s onerror must not close evtSource on
    transient errors — PR-D's reconnect-with-replay depends on native
    EventSource auto-reconnect firing with the ``Last-Event-ID`` header."""
    body = _strip_js_comments(_INTERACTIVE_JS.read_text(encoding="utf-8"))
    # Slice the Pane.connectSSE method body, then the onerror handler
    # inside it.  Reuse the indent-agnostic class-method finder.
    method_start = _pane_method_offset(body, "connectSSE")
    method = _slice_balanced_body(body, method_start)
    assert method is not None, "Pane.connectSSE method body not found"
    # ``_onerror_block`` re-strips internally; passing the already-
    # stripped method body is idempotent (no comments left to strip).
    onerror = _onerror_block(method, "this.evtSource.onerror")
    assert onerror is not None, "Pane.connectSSE.onerror not found"
    passed, reason = _onerror_preserves_native_reconnect(onerror, "this.evtSource")
    assert passed, f"Pane.connectSSE.onerror regressed: {reason}"


def test_connectglobalsse_onerror_preserves_native_reconnect() -> None:
    """``connectGlobalSSE`` is the global-SSE counterpart of
    Pane.connectSSE — same close-defeats-reconnect contract."""
    body = _strip_js_comments(_APP_JS.read_text(encoding="utf-8"))
    fn = _slice_function_body(body, "connectGlobalSSE")
    assert fn is not None, "connectGlobalSSE not found"
    onerror = _onerror_block(fn, "globalEvtSource.onerror")
    assert onerror is not None, "globalEvtSource.onerror not found"
    passed, reason = _onerror_preserves_native_reconnect(onerror, "globalEvtSource")
    assert passed, f"connectGlobalSSE.onerror regressed: {reason}"


def test_coord_connectsse_onerror_preserves_native_reconnect() -> None:
    """Coordinator's connectSSE has the same contract — without the
    guard the coord's per-ws SSE silently drops events on any blip."""
    coord_js = _REPO_ROOT / "turnstone/console/static/coordinator/coordinator.js"
    body = _strip_js_comments(coord_js.read_text(encoding="utf-8"))
    onerror = _onerror_block(body, "evtSource.onerror")
    assert onerror is not None, "coordinator.js evtSource.onerror not found"
    passed, reason = _onerror_preserves_native_reconnect(onerror, "evtSource")
    assert passed, f"coordinator.js connectSSE.onerror regressed: {reason}"


# ---------------------------------------------------------------------------
# Coordinator-pane parity for the SSE overflow-recovery companions (issue #806).
# The server-side fixes (emit-time batching, _ListenerQueue poison, out-of-band
# closing) live in SessionUIBase and already cover EVERY SSE stream; these pin
# the CLIENT-side companions ported into coordinator.js so it stops relying on
# native reconnect alone — storm guard + degraded catch-up, close-on-hide /
# replay-on-show, and drop-vs-render-wedge counters.
# ---------------------------------------------------------------------------


def test_coord_imports_shared_overflow_helpers() -> None:
    """coordinator.js consumes the SAME sse_overflow.js helpers as the
    interactive pane (over the /shared mount) so the trip threshold and cooldown
    ladder cannot drift between the two surfaces."""
    body = _COORD_JS.read_text(encoding="utf-8")
    m = re.search(
        r"import \{([^}]*)\} from \"/shared/sse_overflow\.js\";",
        body,
        re.S,
    )
    assert m is not None, "coordinator must import the shared overflow helpers"
    imported = m.group(1)
    for name in (
        "OVERFLOW_TRIP_COUNT",
        "OVERFLOW_TRIP_WINDOW_MS",
        "DEGRADED_COOLDOWN_BASE_MS",
        "DEGRADED_COOLDOWN_MAX_MS",
        "DEGRADED_COOLDOWN_RESET_MS",
        "overflowWindowTripped",
        "degradedCooldownStep",
    ):
        assert name in imported, f"{name} must be imported from /shared/sse_overflow.js"
    # No local fork of the extracted pure functions on the coordinator side.
    assert not re.search(r"^\s*function overflowWindowTripped\(", body, re.M)
    assert not re.search(r"^\s*function degradedCooldownStep\(", body, re.M)


def test_coord_stream_overflow_case_counts_and_rate_limits() -> None:
    """The coordinator handles the id-less ``stream_overflow`` frame: count it
    (drop-vs-wedge field instrumentation) and feed the rolling-window storm
    guard, exactly like the interactive pane."""
    body = _COORD_JS.read_text(encoding="utf-8")
    assert 'case "stream_overflow":' in body
    assert "noteStreamOverflow();" in body
    # The three-way health counter distinguishes dropped events (overflow /
    # malformed frame) from render wedges (dispatch / render throw).
    assert "streamHealth = { overflows: 0, renderThrows: 0, malformedFrames: 0 }" in body
    assert "streamHealth.overflows += 1;" in body
    assert "streamHealth.malformedFrames += 1;" in body
    # Exactly two render-throw increment sites: the noteRenderThrow helper
    # (all three contained render/finalize catches route through it — they
    # recover with a plain-text fallback, so console.warn) and the onmessage
    # dispatch catch (console.error class — the event is dropped outright).
    # The three recovered call sites are pinned by label so a new render path
    # that forgets to count surfaces loudly.
    assert body.count("streamHealth.renderThrows += 1;") == 2
    helper = re.search(r"function noteRenderThrow\(where, err\)\s*\{(.*?)\n  \}", body, re.S)
    assert helper is not None, "noteRenderThrow helper not found"
    assert "streamHealth.renderThrows += 1;" in helper.group(1)
    assert 'noteRenderThrow("streamingRender", e);' in body
    assert 'noteRenderThrow("in_progress_snapshot render", e);' in body
    assert 'noteRenderThrow("streamingRenderFinalize", e);' in body
    note = re.search(r"function noteStreamOverflow\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert note is not None, "noteStreamOverflow not found"
    assert "overflowWindowTripped(" in note.group(1)
    assert "enterDegradedCatchup()" in note.group(1)
    # The trip handler only counts + trips; the cooldown reset lives in
    # enterDegradedCatchup (keyed off lastDegradedAt) — the finding [0] shape.
    assert "degradedCooldownMs" not in note.group(1), (
        "noteStreamOverflow must not touch the cooldown — that reset defeated the ladder escalation"
    )


def test_coord_handleevent_dispatch_is_wedge_guarded() -> None:
    """A throw escaping onmessage does NOT close the EventSource, so an
    unguarded handler throw left the streaming refs stale and wedged every later
    turn.  The coordinator wraps the dispatch and counts the throw (render-wedge
    class) so a field report tells it apart from a dropped-events gap."""
    body = _COORD_JS.read_text(encoding="utf-8")
    m = re.search(r"try \{\s*handleEvent\(data\);\s*\} catch \(err\) \{(.*?)\}", body, re.S)
    assert m is not None, "handleEvent(data) must be wrapped in try/catch in onmessage"
    assert "streamHealth.renderThrows += 1;" in m.group(1)


def test_coord_degraded_catchup_stops_live_stream_and_retries() -> None:
    """Three overflow closes inside the window drop the coordinator to a
    degraded catch-up: suspend the live stream, say so plainly, and reconnect
    after a doubling cooldown — the reconnect replays the gap (or falls to the
    /history floor once it outgrows the ring)."""
    body = _COORD_JS.read_text(encoding="utf-8")
    m = re.search(r"function enterDegradedCatchup\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert m is not None, "enterDegradedCatchup not found"
    method = m.group(1)
    assert "degradedCooldownStep(" in method
    assert "lastDegradedAt = now" in method
    # Suspend the stream BEFORE arming the retry timer (mirrors interactive's
    # disconnect-then-rearm ordering) or the fresh timer is cancelled at once.
    assert method.index("suspendStream()") < method.index("degradedTimer = setTimeout")
    # Plain-language status, not a silent stall.
    assert "catching up" in method
    # A fresh connect must cancel a pending degraded timer so it can't
    # double-open behind the retry — connectSSE's prologue routes through the
    # shared closeStreamTransport teardown, which owns that clear (alongside
    # the reconnect timer + the EventSource close/null).
    conn = re.search(r"function connectSSE\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert conn is not None
    assert "closeStreamTransport();" in conn.group(1)
    teardown = re.search(r"function closeStreamTransport\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert teardown is not None, "closeStreamTransport not found"
    assert "clearTimeout(degradedTimer)" in teardown.group(1)
    assert "clearTimeout(reconnectTimer)" in teardown.group(1)
    assert "evtSource = null;" in teardown.group(1)


def test_coord_visibilitychange_closes_on_hide_reconnects_on_show() -> None:
    """A hidden tab's throttled drain is the worst-case slow SSE consumer.  The
    coordinator installs a visibilitychange handler that closes the stream on
    hide (marking its OWN close via hiddenDisconnect) and reconnects on show from
    the saved lastEventId, and removes the listener on teardown."""
    body = _COORD_JS.read_text(encoding="utf-8")
    assert 'document.addEventListener("visibilitychange", visHandler);' in body
    assert 'document.removeEventListener("visibilitychange", visHandler);' in body
    vis = re.search(r"function onVisibilityChange\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert vis is not None, "onVisibilityChange not found"
    method = vis.group(1)
    assert "document.hidden" in method
    assert "suspendStream()" in method
    assert "hiddenDisconnect = true;" in method
    assert "else if (hiddenDisconnect)" in method
    assert "connectSSE();" in method


def test_coord_connectsse_defers_open_when_tab_hidden() -> None:
    """connectSSE must never open an EventSource into a hidden tab — the single
    chokepoint that also backstops a FIRST connect in a background tab (where the
    close-on-hide handler never fires because there was no open stream).  It
    marks hiddenDisconnect so the show edge owns the reconnect, marks the
    deferral as a GAP (markStreamGap) so the eventual open runs the post-gap
    recovery — without the mark a pane first opened in a background tab
    silently missed every child/task created while hidden — and reports an
    honest paused status instead of pinning "connecting" with no attempt in
    flight."""
    body = _COORD_JS.read_text(encoding="utf-8")
    conn = re.search(r"function connectSSE\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert conn is not None
    method = conn.group(1)
    guard = method.index("if (document.hidden)")
    open_idx = method.index("new EventSource(")
    assert guard < open_idx, "the hidden guard must precede new EventSource"
    head = method[guard:open_idx]
    assert "markStreamGap();" in head, "the hidden deferral must count as a stream gap"
    assert "hiddenDisconnect = true;" in head
    assert "return;" in head
    assert 'setSseStatus("paused' in head, "the deferral must report paused, not connecting"
    # "connecting" is claimed only once an attempt actually starts — after
    # the hidden guard, immediately before the EventSource construction.
    connecting = method.index('setSseStatus("connecting')
    assert guard < connecting < open_idx


def test_coord_destroy_removes_visibility_handler_and_stream_transport() -> None:
    """Teardown must detach the document-level visibilitychange listener (it
    holds a strong ref to the closure) and tear down the stream transport —
    closeStreamTransport closes the EventSource and cancels the reconnect +
    degraded retry timers (pinned in the degraded-catchup test) — or a
    destroyed pane leaks and a show edge / pending retry reopens its stream."""
    body = _COORD_JS.read_text(encoding="utf-8")
    d = re.search(r"function destroy\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert d is not None, "destroy not found"
    method = d.group(1)
    assert "removeVisibilityHandler();" in method
    assert "closeStreamTransport();" in method


def test_coord_close_session_detaches_visibility_reopen() -> None:
    """coordCloseSession suspends the stream AND removes the visibilitychange
    handler BEFORE awaiting the /close POST: a tab hide→show while the POST is
    in flight must not reopen a stream against the workstream the server is
    tearing down (404 / reconnect churn against a dead session).  The failure
    paths resume via connectSSE, which reinstalls the handler at its
    install-once chokepoint — so close-on-hide survives a failed close."""
    body = _COORD_JS.read_text(encoding="utf-8")
    m = re.search(r"async function coordCloseSession\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert m is not None, "coordCloseSession not found"
    method = m.group(1)
    suspend = method.index("suspendStream();")
    unhook = method.index("removeVisibilityHandler();")
    # The quoted URL fragment, not the bare word (comments mention /close too).
    post = method.index('"/close"')
    assert suspend < post, "stream suspension must precede the /close POST"
    assert unhook < post, "visibility detach must precede the /close POST"
    assert "resumeSse()" in method


def test_coord_post_gap_sidebar_refresh_is_replay_aware() -> None:
    """The replace-mode children/tasks refresh (a sidebar rebuild) must NOT
    fire on every reconnect: child_ws_* / task-mutating events are ordinary
    ring-buffer entries, so a cursor reconnect (replay_ok) redelivers them and
    the sidebar heals through the normal handlers — a momentary blur→focus
    under close-on-hide must not rebuild the sidebar.  The refresh fires
    exactly when the replay cannot vouch for the gap: no resume cursor or an
    over-threshold gap at onopen, or the server's replay_truncated envelope
    (ring evicted), deduped per open via gapRefreshedAtOpen."""
    body = _COORD_JS.read_text(encoding="utf-8")
    conn = re.search(r"function connectSSE\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert conn is not None
    method = conn.group(1)
    gate = re.search(
        r"wasReconnecting &&\s*\(lastEventId == null \|\| gapMs > GAP_REFRESH_THRESHOLD_MS\)",
        method,
    )
    assert gate is not None, "onopen must gate the sidebar refresh on replay coverage"
    assert "refreshSidebarAfterGap();" in method
    assert "gapRefreshedAtOpen = true;" in method
    # The ring-evicted signal triggers the same refresh (deduped per open).
    trunc = re.search(r'case "replay_truncated":(.*?)break;', body, re.S)
    assert trunc is not None, "replay_truncated case not found"
    assert "refreshSidebarAfterGap()" in trunc.group(1)
    assert "gapRefreshedAtOpen" in trunc.group(1)
    # Deliberate suspends (hide / overflow / close-session) mark the gap so
    # the next open participates in the recovery decision at all.
    sus = re.search(r"function suspendStream\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert sus is not None, "suspendStream not found"
    assert "markStreamGap();" in sus.group(1)
    # The refresh helper carries the whole replace-mode bundle: children,
    # tasks, and the live-badge purge (permanent 403/404 entries preserved).
    ref = re.search(r"function refreshSidebarAfterGap\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert ref is not None, "refreshSidebarAfterGap not found"
    assert "loadChildren({ replace: true });" in ref.group(1)
    assert "loadTasks();" in ref.group(1)
    assert "_liveBadgeCacheDelete(id)" in ref.group(1)


def test_coord_defers_truncated_resync_and_consumes_at_idle() -> None:
    """replay_truncated seen mid-stream must be DEFERRED, not dropped (matches
    interactive's _pendingTruncatedResync): refetching immediately would detach
    the live bubble (content OR a reasoning-only one), but skipping outright
    leaves the ring-evicted turns lost for the session.  The guard covers both
    streaming targets and latches otherwise; the next state_change=idle consumes
    the flag — which also repairs a turn stranded by close-on-hide (stream_end
    evicted while hidden), resetting the streaming refs first since
    refetchHistory does not null them."""
    body = _COORD_JS.read_text(encoding="utf-8")
    trunc = re.search(r'case "replay_truncated":(.*?)break;', body, re.S)
    assert trunc is not None, "replay_truncated case not found"
    t = trunc.group(1)
    assert "if (!currentAssistantEl && !currentReasoningEl)" in t
    assert "refetchHistory();" in t
    assert "pendingTruncatedResync = true;" in t
    st = re.search(r'case "state_change":(.*?)\n      case ', body, re.S)
    assert st is not None, "state_change case not found"
    s = st.group(1)
    assert "if (pendingTruncatedResync)" in s
    assert "pendingTruncatedResync = false;" in s
    assert "currentAssistantEl = null;" in s
    assert "refetchHistory();" in s
    # Consume the latch, THEN reset the dangling refs and refetch.
    consume = s.index("pendingTruncatedResync = false;")
    refetch = s.index("refetchHistory();")
    assert consume < refetch


def test_coord_detects_server_restart_by_backwards_event_id() -> None:
    """A coordinator process restart resets the per-ws event counter, and the
    replay path reports replay_ok for a stale-high cursor (past the new max), so
    the gap is unsignalled and the sidebar goes stale.  onmessage catches it: a
    live event id below the saved cursor == the counter reset → pull
    authoritative sidebar state (deduped per open against onopen's refresh),
    checked BEFORE the cursor is overwritten."""
    body = _COORD_JS.read_text(encoding="utf-8")
    m = re.search(r"evtSource\.onmessage = function \(event\) \{(.*?)\n    \};", body, re.S)
    assert m is not None, "onmessage handler not found"
    handler = m.group(1)
    assert "Number(evtSource.lastEventId) < Number(lastEventId)" in handler
    assert "!gapRefreshedAtOpen" in handler
    assert "refreshSidebarAfterGap();" in handler
    check = handler.index("Number(evtSource.lastEventId) < Number(lastEventId)")
    overwrite = handler.index("lastEventId = evtSource.lastEventId;")
    assert check < overwrite


def test_interactive_history_is_rest_first_not_sse() -> None:
    """PR A converged interactive onto coord's REST-first history
    model: first paint and post-rewind re-render fetch ``GET /history``
    over REST (``_loadHistoryThenConnect`` / ``_refetchHistory``), and
    the server no longer replays the conversation inline over SSE — so
    the client must no longer consume a ``history`` SSE event. Guards
    against a regression that re-couples first paint to the removed
    inline-history replay."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    assert "_loadHistoryThenConnect" in body, (
        "REST-first first-paint helper missing — interactive must fetch "
        "history via GET /history before connecting SSE (coord's model)."
    )
    assert "_refetchHistory" in body
    # Race/identity guard (PR #595 review follow-up): a load-generation token
    # must discard a superseded refetch so a slow ws-A load cannot render over
    # ws-B after a tab switch / child open.
    assert "_historyLoadToken" in body, (
        "missing the load-generation guard — a stale history refetch can "
        "render the wrong workstream's history"
    )
    # Pre-PR-A interactive had no REST /history fetch; the quoted URL
    # segment only appears in the new fetch concatenation.
    assert '"/history"' in body
    # The inline SSE ``history`` event is no longer emitted server-side,
    # so the client must not handle it (history is REST-only now).
    assert 'case "history":' not in body
    # The server now projects the canonical wire shape at /history
    # (make_history_handler → project_history_messages), so interactive
    # feeds the payload straight to replayHistory — the client-side
    # normaliser (history_normalize.js) was retired.
    assert "normalizeHistoryMessages" not in body, (
        "the client-side history normaliser was retired — interactive must "
        "consume the server-projected /history shape directly"
    )
    assert "this.replayHistory(data.messages" in body, (
        "interactive must feed the projected REST /history payload straight to replayHistory"
    )
    idx = (Path(__file__).resolve().parent.parent / "turnstone/ui/static/index.html").read_text(
        encoding="utf-8"
    )
    assert "/shared/history_normalize.js" not in idx, (
        "the retired history_normalize.js script tag must be removed from index.html"
    )


def test_early_paint_tool_pending_wiring() -> None:
    """The interactive UI must paint a committed tool call on ``tool_pending``
    — before the judge verdict / approval gate resolve — and then UPGRADE
    that same block in place on the authoritative ``tool_info`` /
    ``approve_request`` rather than appending a duplicate.  Pre-fix (PR #621)
    the card waited on the verdict; this guards the early-paint wiring against
    a rename/deletion that would silently revert to post-verdict rendering."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # Dispatch routes the early event to the announce painter.
    assert 'case "tool_pending":' in body
    assert "announceToolBlock(evt.items)" in body
    # The announce painter + the idempotent take-or-build helper exist.
    assert "announceToolBlock(items) {" in body
    assert "_takeAnnouncedBlock(items) {" in body
    # showInlineToolBlock reuses the announced shell rather than always
    # creating + appending a fresh block (the duplicate-card bug).
    assert "this._takeAnnouncedBlock(items)" in body
    assert "if (!announced) this.messagesEl.appendChild(block);" in body


def test_task_agent_steps_never_escape_their_card() -> None:
    """A task agent's sub-tool steps (``parent_call_id`` stamped) must nest in
    the task card, never render as top-level rows that look like the main
    harness issued them.  Two seams keep that true; this guards both against a
    rename/deletion:

    1. ``tool_info`` routes through ``_routeAgentItems`` first — a sub-tool
       auto-resolved by policy / "Always" arrives as a ``tool_info`` and must
       nest, not paint a duplicate top-level block (Copilot review on #732).
    2. A child step whose ``task_agent`` row hasn't painted yet (the 4-wide
       tool pool's ordering window) is BUFFERED and flushed when the row lands,
       instead of escaping to top-level; the card also survives the parent
       row's pending->resolved rebuild.
    3. SAFETY VALVE: a buffered step whose parent row NEVER paints (an id-
       correlation mismatch / aborted agent) is escaped to a top-level row after
       a grace window, so it stays VISIBLE rather than buffered forever.
    """
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # 1. tool_info nests via the same router as tool_pending / approve_request.
    info = body[body.index('case "tool_info":') : body.index('case "approve_request":')]
    assert 'this._routeAgentItems(evt.items, "info")' in info, (
        "tool_info must route a parent-tagged sub-tool into the task card "
        "before any top-level showInlineToolBlock fallback."
    )
    # 2. _routeAgentItems buffers an orphan child (instead of returning false,
    #    which escapes it to top-level) when the parent card isn't painted yet.
    route = body[
        _pane_method_offset(body, "_routeAgentItems") : _pane_method_offset(
            body, "_ensureAgentCard"
        )
    ]
    assert "_bufferAgentOrphan(parentId, items, mode)" in route, (
        "a parent-tagged child with no card yet must buffer, not fall through to a top-level paint."
    )
    # The buffer / flush / escape / relink helpers exist.
    assert "_bufferAgentOrphan(parentId, items, mode) {" in body
    assert "_flushAgentOrphans(parentIds) {" in body
    assert "_escapeAgentOrphans(parentId) {" in body
    assert "_relinkAgentCards(items) {" in body
    assert body.count("this._relinkAgentCards(") >= 2, (
        "both announceToolBlock and showInlineToolBlock must relink + flush so "
        "a buffered step nests as soon as a tool row appears."
    )
    # 3. Safety valve: _bufferAgentOrphan arms a grace timer to _escapeAgentOrphans
    #    so a never-painting parent's steps can't vanish (or leak) — they escape
    #    back to a visible top-level paint.
    buf = body[
        _pane_method_offset(body, "_bufferAgentOrphan") : _pane_method_offset(
            body, "_flushAgentOrphans"
        )
    ]
    assert "setTimeout(" in buf and "_escapeAgentOrphans(parentId)" in buf, (
        "a buffered orphan must arm a grace-window escape so it never stays "
        "buffered (invisible) forever."
    )
    escape = body[
        _pane_method_offset(body, "_escapeAgentOrphans") : _pane_method_offset(
            body, "_relinkAgentCards"
        )
    ]
    assert "announceToolBlock(" in escape, (
        "the escape valve must render the steps top-level (visible), the "
        "pre-buffer behaviour, rather than dropping them."
    )
    # Flush is targeted to the just-painted parents, not the whole map.
    flush = body[
        _pane_method_offset(body, "_flushAgentOrphans") : _pane_method_offset(
            body, "_escapeAgentOrphans"
        )
    ]
    assert "parentIds.forEach" in flush
    # _ensureAgentCard re-attaches a DETACHED card across a parent-row rebuild,
    # but builds fresh on a still-attached (cross-turn reused) call_id rather
    # than stealing the prior agent's steps.
    ensure = body[
        _pane_method_offset(body, "_ensureAgentCard") : _pane_method_offset(
            body, "_bufferAgentOrphan"
        )
    ]
    assert "!card.wrap.isConnected" in ensure
    assert "parentRow.appendChild(card.wrap);" in ensure


def test_risk_level_normalized_before_dom_interpolation() -> None:
    """Server-supplied ``risk_level`` lands in className / data-risk strings the
    verdict + warning CSS depend on, so every interpolation must funnel through
    ``normalizeRiskLevel`` (issue #562).  Post-5e.2c the pane DELEGATES the card
    DOM to the shared builders (conversation.js), which OWN the normalization —
    so the pane must (a) carry no raw ``risk_level || "medium"`` fallback and
    (b) build verdict/warning DOM only via the shared builders, never inline."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # No raw fallback antipattern anywhere in the pane.
    assert 'risk_level || "medium"' not in body
    assert 'risk_level) || "medium"' not in body
    # Verdict + warning DOM is built by the shared builders (which normalize),
    # not by an inline className / data-risk interpolation in the pane.
    assert "buildConvVerdict(" in body
    assert "buildConvWarning(" in body
    # The chokepoint + its enum live in the shared module, and the builders there
    # route the server risk through it.
    shared = (_INTERACTIVE_JS.parent / "conversation.js").read_text(encoding="utf-8")
    assert "export function normalizeRiskLevel(" in shared
    assert "normalizeRiskLevel(verdict.risk_level)" in shared  # buildConvVerdict
    assert "normalizeRiskLevel(a.risk_level)" in shared  # buildConvWarning
    for level in ("low", "medium", "high", "critical"):
        assert f'"{level}"' in shared


def test_announced_rail_outspecifies_inline_cyan_hold() -> None:
    """The early-paint announced card's left rail MUST out-specify
    ``.msg.ts-approval--inline`` (specificity 0,2,0), which deliberately
    sets ``border-left-color: var(--cyan)`` to hold resolved inline cards
    cyan.  A bare ``.ts-approval--announced`` (0,1,0) loses that cascade and
    silently renders the rail cyan — indistinguishable from a normal tool
    card, defeating the whole "spot the committed call and Stop it" signal.
    This is a render-only failure no JS string-guard catches, so pin the
    high-specificity selector + the visible ``--accent`` (not the
    near-invisible 15%-alpha ``--accent-dim``)."""
    css = (_APP_JS.resolve().parent / "style.css").read_text(encoding="utf-8")
    assert ".msg.ts-approval--inline.ts-approval--announced {" in css, (
        "announced rail selector must qualify with .msg.ts-approval--inline to "
        "beat the inline cyan-hold rule (0,2,0) — else it renders dashed cyan"
    )
    # The rule body uses the full --accent amber + dashed style.
    block_start = css.index(".msg.ts-approval--inline.ts-approval--announced {")
    block = css[block_start : block_start + 160]
    assert "border-left-color: var(--accent)" in block
    assert "border-left-style: dashed" in block
    assert "--accent-dim" not in block  # 15%-alpha is invisible at 3px


def test_early_paint_screen_reader_announce() -> None:
    """Screen-reader parity for the early paint: a committed tool call must be
    announced politely (messagesEl is flipped to aria-live="off" mid-stream, so
    the appended shell alone is inaudible), and the announced shell must carry
    aria-busy until the gate resolves.  All silent failures — no JS error, just
    a blind operator who never hears the call land — so pin the wiring."""
    body = _INTERACTIVE_JS.read_text(encoding="utf-8")
    # Dedicated polite SR region (separate from the voice one) + summary builder.
    assert "function toolAnnounce(" in body
    assert "function _toolAnnounceText(" in body
    assert 'setAttribute("aria-live", "polite")' in body
    # Early paint announces + marks the shell busy; the upgrade clears it.
    assert "toolAnnounce(_toolAnnounceText(list))" in body
    assert 'block.setAttribute("aria-busy", "true")' in body
    assert 'block.removeAttribute("aria-busy")' in body


def test_global_stream_recovery_floor_and_render_coalescing() -> None:
    """Perf-audit P0/P1 for the Tier-1 global stream.  The server's recovery
    events for a truncated reconnect gap (``node_snapshot`` as the floor,
    ``replay_truncated`` as the marker) used to fall through the handler
    silently — workstreams created during a long hidden-tab gap never
    rendered again, and missed ``ws_closed`` left ghost rows forever.  A
    malformed frame is the same permanent drift (the cursor advances before
    the parse), so it resyncs too.  ``fireRender`` is rAF-coalesced: every
    ``ws_state`` (≥2 per tool round per workstream) used to trigger a
    synchronous full rail rebuild."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert 'data.type === "node_snapshot"' in body
    assert 'data.type === "replay_truncated"' in body
    assert "function applyRosterSnapshot(" in body
    assert "function resyncRoster(" in body
    assert "malformed frame" in body
    fire = body.index("function fireRender()")
    assert "requestAnimationFrame(" in body[fire : fire + 700], (
        "fireRender must coalesce subscriber repaints to one per frame"
    )


def test_server_global_accels_are_platform_aware_and_scoped() -> None:
    """The standalone's keydown handler owns only the GLOBAL accels — new
    workstream, switch, dashboard.  They pick the modifier per platform (Ctrl on
    macOS where the browser owns Cmd, Alt elsewhere) so Ctrl+T/1-9 aren't eaten
    by the browser off macOS.  The per-pane verbs (edit/refresh/fork/delete/
    close) moved to shell.js, so the handler must not invoke them itself."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert "const IS_MAC" in body and 'navigator.platform.indexOf("Mac")' in body, (
        "the accelerators need a platform check to choose Ctrl vs Alt"
    )
    handler = body[body.index('document.addEventListener("keydown"') :]
    assert "const paneMod" in handler, (
        "global accels must gate on the platform-aware paneMod, not raw ctrlKey"
    )
    assert 'e.ctrlKey && e.key === "t"' not in handler, (
        "Ctrl+T is browser-reserved off macOS — new workstream must bind via paneMod"
    )
    assert "newWorkstream()" in handler and "switchTab(" in handler, (
        "the standalone handler still owns new + switch"
    )
    # macOS Ctrl+T / Ctrl+D are the Cocoa transpose / delete-forward text
    # bindings; the creation/dashboard chords must yield while typing, through
    # the shared TS_SHELL.inEditable guard (not a per-file copy).
    assert "TS_SHELL.inEditable(" in handler, (
        "new + dashboard must yield to text editing (macOS Ctrl+T / Ctrl+D)"
    )
    # The per-pane verbs are shell.js's job now — the standalone handler must not
    # double-bind them (shell.js drives them off the active pane's menu).
    for verb in ("editWorkstreamTitle()", "forkWorkstream()", "confirmDeleteWorkstream()"):
        assert verb not in handler, (
            f"{verb} moved to shell.js — the app.js handler must not also bind it"
        )


def test_shortcut_overlay_labels_match_the_platform_modifier() -> None:
    """The '?' help overlay must advertise the same modifier the handler
    listens for — Ctrl on macOS, Alt on Windows/Linux — instead of a hardcoded
    Ctrl that is wrong (and non-functional) off macOS."""
    index = _INDEX_HTML.read_text(encoding="utf-8")
    assert "const PANE_MOD" in index and 'navigator.platform.indexOf("Mac")' in index, (
        "the overlay must compute its modifier label per platform"
    )
    assert "${PANE_MOD}+T" in index, "the New-workstream badge must render through PANE_MOD"
    assert '<span class="kb-key">Ctrl+T</span>' not in index, (
        "the New-workstream badge must not hardcode Ctrl (wrong off macOS)"
    )


def test_pane_menu_accels_are_shared_and_platform_aware() -> None:
    """shell.js is the single source of truth for the per-pane tab-menu
    shortcuts: the badge string and the keydown handler come from ONE registry,
    so a badge can't advertise a chord the handler ignores.  Badges must be
    platform-aware (no hardcoded Ctrl), and the shared handler must drive the
    ACTIVE pane's own menu so each surface contributes only what it supports."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "PANE_MENU_ACCELS" in shell and "function paneAccelBadge" in shell, (
        "shell.js must own the accel registry + badge builder"
    )
    assert "const PANE_MOD_LABEL" in shell and 'navigator.platform.indexOf("Mac")' in shell, (
        "the shared badge must be platform-aware (Ctrl on macOS, Alt elsewhere)"
    )
    # The tab-menu items carry a stable accel + a computed badge, NOT a hardcoded
    # Ctrl string that would lie on Windows/Linux.
    for accel in ("close-pane", "edit-title", "refresh-title", "delete"):
        assert f'accel: "{accel}"' in shell, f"tab menu must tag the {accel} item"
    assert 'key: "Ctrl+Shift+E"' not in shell and 'key: "Ctrl+W"' not in shell, (
        "tab-menu badges must go through paneAccelBadge, not hardcoded Ctrl"
    )
    # The shared handler resolves the active pane and runs its menu item by accel.
    assert "paneAccelFor(e)" in shell and "pane.tabMenu()" in shell, (
        "the shared keydown handler must drive the active pane's menu by accel"
    )
    # The typing guard is shared (TS_SHELL.inEditable), not copied per surface.
    assert "function inEditable(" in shell and "inEditable," in shell, (
        "shell.js must define + expose the shared inEditable guard on TS_SHELL"
    )
    ui = _APP_JS.read_text(encoding="utf-8")
    console = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    assert "_inEditable" not in ui and "_consoleInEditable" not in console, (
        "surfaces must use TS_SHELL.inEditable, not a per-file copy of the guard"
    )


def test_console_has_matching_pane_hotkeys() -> None:
    """The console regained pane hotkeys to match the standalone: a keydown
    handler for switch (Mod+1-9) + dashboard (Ctrl+D), and a '?' overlay that
    advertises them platform-aware.  New workstream and Fork are intentionally
    omitted (no console fork / blank-new surface)."""
    app = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    assert (
        "_CONSOLE_IS_MAC" in app and "statefulTabs()" in app and 'openPane("dashboard")' in app
    ), "the console must wire switch (statefulTabs) + dashboard hotkeys"
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert "const PANE_MOD" in index and '"Panes"' in index, (
        "the console '?' overlay needs a platform-aware Panes section"
    )
    assert "${PANE_MOD}+W" in index and "${PANE_MOD}+Shift+E" in index, (
        "console badges must render through PANE_MOD"
    )
    assert '"Fork"' not in index and "New workstream" not in index, (
        "Fork + New are intentionally omitted on the console"
    )


def test_fork_hides_project_picker() -> None:
    """A fork must NOT show a project picker. A fork inherits its source's project
    (enforced server-side), and a re-fileable fork picker was a cross-tenant
    history-relocation vector — so showNewWsModal hides the picker for forks and
    the "Keep source's project" fork option is gone."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert "projSelect.hidden = !!_forkFromWsId" in body, (
        "the new-ws project picker must be hidden for forks"
    )
    assert "Keep source's project" not in body, (
        "the fork project picker (and its 'Keep source's project' option) must be removed"
    )


def test_submit_gates_project_on_fork_flag() -> None:
    """submitNewWs must send body.project_id ONLY for a fresh create
    (!_forkFromWsId) — a fork never sends a project (its project is the source's,
    enforced server-side). Not gated on picker visibility or a requireProject()
    re-read."""
    body = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function submitNewWs\(\)\s*\{(.*?)\n\}", body, re.S)
    assert m is not None, "could not locate submitNewWs"
    fn = m.group(1)
    assert "TurnstoneProjects.requireProject" not in fn, (
        "submit must not re-read the requireProject() advisory"
    )
    assert re.search(r"project_id && !_forkFromWsId", fn), (
        "submit must gate project_id on !_forkFromWsId (a fork never sends a project)"
    )


def test_strict_picker_requires_explicit_pick() -> None:
    """Under require_project the fresh picker must NOT auto-select the first
    project (which silently mis-files a required chat under a possibly-shared
    project) — it offers a 'Select a project…' prompt so the user consciously
    chooses."""
    body = _APP_JS.read_text(encoding="utf-8")
    assert "Select a project" in body, (
        "strict picker must offer an explicit 'Select a project…' prompt"
    )
    m = re.search(
        r"function _reconcileRequiredProjectSelection\(sel, choices\)\s*\{(.*?)\n\}",
        body,
        re.S,
    )
    assert m is not None, "could not locate _reconcileRequiredProjectSelection"
    fn = m.group(1)
    assert "real[0]" not in fn, "strict picker must not auto-select the first project"
    # §6b polish: _populateProjectSelect threads its already-computed choices into
    # reconcile rather than forcing a second projectChoices() recompute (the onClose
    # creator caller still passes none and reconcile recomputes for it).
    assert "_reconcileRequiredProjectSelection(sel, choices)" in body, (
        "the populate helper must thread its computed choices into reconcile "
        "(avoids a redundant projectChoices() recompute)"
    )


# ---------------------------------------------------------------------------
# Composer selector FOUC — sync-paint-then-refresh guards
# ---------------------------------------------------------------------------
#
# The project + persona composer selectors are painted from the warm shared
# client cache (window.TurnstoneProjects / window.TurnstonePersonas, warmed by
# the rail at startup) SYNCHRONOUSLY on open, BEFORE the deliberate async
# refresh-and-repaint that catches items created elsewhere.  Pre-fix they were
# painted only inside the refresh().then callback, so every open flashed an
# empty dropdown for a network round-trip even when the data was already in
# memory.  These guards pin the ordering — a synchronous populate must precede
# the refresh().then — for all three composer surfaces (new-ws modal, dashboard
# Options, console launcher).  The model/skill selectors (phase 2b) are covered
# by the guards further down (search "models/skills composer FOUC").


def _slice_top_level_fn(body: str, header: str) -> str:
    """Slice a top-level ``function`` body from ``header`` to the next
    column-0 ``function`` declaration (or EOF).  Unlike
    ``_slice_balanced_body`` this has no fixed-size window, so it is safe
    for large functions like ``showNewWsModal``.  Nested (indented)
    ``function () {…}`` expressions never match the ``\\nfunction `` bound,
    so the slice stops at the next top-level function."""
    start = body.index(header)
    nxt = body.find("\nfunction ", start + 1)
    return body[start:] if nxt < 0 else body[start:nxt]


def test_new_ws_modal_paints_project_and_persona_from_cache_synchronously() -> None:
    """FOUC fix: the new-ws modal's project + persona pickers paint from the warm
    shared cache SYNCHRONOUSLY on open, BEFORE the async refresh-and-repaint — so a
    warm-cache open (the common case; the rail warms the caches at startup) shows
    the populated dropdowns immediately instead of flashing empty for a network
    round-trip.  The refresh-on-open is KEPT (it catches items created elsewhere);
    these guards pin only that a synchronous populate precedes it."""
    body = _APP_JS.read_text(encoding="utf-8")
    fn = _slice_top_level_fn(body, "function showNewWsModal(")
    # Project is painted via the shared _paintProjectPicker helper (fork-gated);
    # the sync-before-async pattern is pinned in test_paint_project_picker_syncs.
    assert "_paintProjectPicker(projSelect" in fn, (
        "the modal must paint the project picker via the shared _paintProjectPicker helper"
    )
    # Persona is painted via the shared _paintPersonaSelect wrapper (fork-gated);
    # its sync-before-async ordering is pinned in the wrapper-internals test.
    assert "_paintPersonaSelect(personaSelect" in fn, (
        "the modal must paint the persona picker via the shared _paintPersonaSelect helper"
    )


def test_dashboard_paints_project_and_persona_from_cache_synchronously() -> None:
    """FOUC fix (dashboard composer twin of the modal): the dashboard Options
    project + persona pickers paint synchronously from the warm cache before the
    async refresh.  Also pins the deferred require_project polish — the dashboard
    Project label carries a ``.label-hint`` span seeded ``required``/``optional``
    from requireProject() synchronously, matching the new-ws modal."""
    body = _APP_JS.read_text(encoding="utf-8")
    fn = _slice_top_level_fn(body, "function _loadDashboardOptionsLists(")
    # Project is painted via the shared _paintProjectPicker helper.
    assert "_paintProjectPicker(projSel" in fn, (
        "the dashboard must paint the project picker via the shared _paintProjectPicker helper"
    )
    # Persona is painted via the shared _paintPersonaSelect wrapper (freshOnOpen:false).
    assert "_paintPersonaSelect(personaSel, { freshOnOpen: false })" in fn, (
        "the dashboard must paint the persona picker via _paintPersonaSelect (preserving)"
    )
    # require_project label-hint parity: the dashboard Project label gained a
    # .label-hint span, seeded synchronously from requireProject() like the modal.
    html = _INDEX_HTML.read_text(encoding="utf-8")
    lbl = html.index('for="dashboard-project"')
    assert 'class="label-hint"' in html[lbl : lbl + 120], (
        "the dashboard Project label must carry a .label-hint span (required/optional cue)"
    )
    # The hint is seeded synchronously inside _paintProjectPicker (asserted there).


def test_console_launcher_paints_project_and_persona_from_cache_synchronously() -> None:
    """FOUC fix (console launcher composer): the project + persona wrappers route
    through the _paintHomeFromCache chokepoint — sync paint from the warm cache,
    then refresh(callOpts)-and-repaint; the ordering discipline is asserted ONCE
    on the helper (in the model/skill twin test).  Each wrapper must pair its OWN
    bridge refresh with its OWN populate helper.  Also pins the persona
    kind-default revert: _populateHomePersonaDropdown must fall back to
    defaultPersona(kind) when the previous pick is no longer a valid choice (the
    interactive/coordinator persona shelves are disjoint), or a kind toggle
    silently degrades the picker to a bare placeholder."""
    body = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    proj_fn = _slice_top_level_fn(body, "function _refreshAndPopulateProjects(")
    assert re.search(
        r"_paintHomeFromCache\(\s*TP && TP\.refreshProjects,\s*_populateHomeProjectDropdown",
        proj_fn,
    ), "the launcher project wrapper must pair its bridge refresh + populate via the chokepoint"
    persona_fn = _slice_top_level_fn(body, "function _refreshAndPopulatePersonas(")
    assert re.search(
        r"_paintHomeFromCache\(\s*TP && TP\.refreshPersonas,\s*_populateHomePersonaDropdown",
        persona_fn,
    ), "the launcher persona wrapper must pair its bridge refresh + populate via the chokepoint"
    pop = _slice_top_level_fn(body, "function _populateHomePersonaDropdown(")
    assert '_restorePick("persona"' in pop, (
        "the persona populate must restore a still-valid pick via _restorePick"
    )
    assert "defaultPersona(_launcherKind)" in pop, (
        "the persona populate must revert to the kind default when the pick is gone"
    )


def test_paint_project_picker_syncs_before_refresh() -> None:
    """The shared _paintProjectPicker (used by BOTH the modal and dashboard, so
    the two can't drift and silently re-introduce the FOUC) seeds the required/
    optional hint + paints via _populateProjectSelect, and routes its
    sync-then-refresh tail through the _paintFromCache chokepoint — where the
    sync-before-async + always-fresh:false discipline is asserted once.  Its
    fork/absent-bridge path returns a resolved promise so the dashboard's chained
    Options-chip recompute still runs.  Both paints reuse _populateProjectSelect
    (preserving the #867 strict-picker invariant)."""
    body = _APP_JS.read_text(encoding="utf-8")
    fn = _slice_top_level_fn(body, "function _paintProjectPicker(")
    assert "_populateProjectSelect(" in fn, (
        "_paintProjectPicker must paint via _populateProjectSelect"
    )
    assert "hint.textContent" in fn, "_paintProjectPicker must seed the required/optional hint"
    assert "opts.fork" in fn, "_paintProjectPicker must skip for a fork"
    assert "return Promise.resolve();" in fn, (
        "_paintProjectPicker's fork/absent-bridge path must return a resolved "
        "promise (the dashboard chains its Options-chip recompute on the return)"
    )
    assert "return _paintFromCache(window.TurnstoneProjects.refreshProjects, paint, opts)" in fn, (
        "_paintProjectPicker's tail must route through the _paintFromCache "
        "chokepoint (sync paint(freshOnOpen) then always-fresh:false repaint)"
    )


# --- models/skills composer FOUC (phase 2b) --------------------------------
#
# The model + skill pickers had NO client cache: every composer open re-fetched
# /v1/api/models and /v1/api/skills inline, flashing an empty dropdown for the
# round-trip.  Phase 2b adds shared caches (models.js / skills.js) on the
# extracted list_cache.js core that the composers read synchronously, then
# refresh-and-repaint — the same pattern the project/persona pickers use.  These
# guards pin the sync-before-async ordering on all three surfaces, the cache
# fail-open/coalesce contract, the two-server-schema exposure, and the
# single-repaint-path wiring.

_LIST_CACHE_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/list_cache.js"
_MODELS_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/models.js"
_SKILLS_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/skills.js"
_PROJECTS_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/projects.js"
_PERSONAS_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/personas.js"


def test_paint_model_and_skill_wrappers_sync_before_refresh() -> None:
    """The _paintFromCache chokepoint (PR #869 review: the three wrapper twins
    repeated the same wiring, so the discipline now lives ONCE) paints from the
    warm cache SYNCHRONOUSLY, then refreshes-and-repaints.  CRITICAL: the sync
    paint mirrors the caller's freshOnOpen but the ASYNC repaint MUST pass
    fresh:false — leaking freshOnOpen into the async paint would clobber a
    mid-window pick (for the project picker that reconciles to "" -> a
    require_project 400).  Each wrapper must pair its OWN bridge refresh with
    its OWN populate helper and thread `fresh` through untouched."""
    body = _APP_JS.read_text(encoding="utf-8")
    helper = _slice_top_level_fn(body, "function _paintFromCache(")
    sync = helper.find("repaint(!!(opts && opts.freshOnOpen))")
    async_ = helper.find("refresh().then")
    assert 0 <= sync < async_, (
        "_paintFromCache must sync-paint (repaint mirroring freshOnOpen) BEFORE "
        "the async refresh repaint"
    )
    assert "repaint(false)" in helper[async_:], (
        "_paintFromCache's async repaint must pass fresh:false (never leak "
        "freshOnOpen, or it clobbers a mid-window pick)"
    )
    # Promise contract: callers (the dashboard Options-chip recompute) chain on
    # the async repaint landing; the no-refresh path must still hand back a
    # resolved promise or the chain throws on a cold bridge.
    assert "return Promise.resolve()" in helper, (
        "_paintFromCache must return an already-resolved promise when there is "
        "no refresh (dashboard callers chain the Options-chip recompute)"
    )
    assert "return refresh().then" in helper, (
        "_paintFromCache must return the async-repaint promise (callers act "
        "after the repaint lands)"
    )
    for wrapper, bridge_refresh, populate in (
        (
            "function _paintModelSelects(",
            "window.TurnstoneModels && window.TurnstoneModels.refreshModels",
            "_populateModelSelect(modelSel, judgeSel, { fresh: fresh })",
        ),
        (
            "function _paintSkillSelect(",
            "window.TurnstoneSkills && window.TurnstoneSkills.refreshSkills",
            "_populateSkillSelect(sel, { fresh: fresh })",
        ),
        (
            "function _paintPersonaSelect(",
            "window.TurnstonePersonas && window.TurnstonePersonas.refreshPersonas",
            "_populatePersonaSelect(sel, { fresh: fresh })",
        ),
    ):
        fn = _slice_top_level_fn(body, wrapper)
        assert "return _paintFromCache(" in fn, f"{wrapper} must return the _paintFromCache promise"
        assert bridge_refresh in fn, f"{wrapper} must pass its own bridge refresh"
        assert populate in fn, f"{wrapper} must thread fresh through to its own populate helper"


def test_new_ws_modal_renders_all_selects_fresh_on_open() -> None:
    """Finding [0] fix — every composer select in the reused new-ws <dialog> renders
    its ACTUAL value fresh on open (no stale carryover from the last open): model +
    skill via the wrappers with freshOnOpen:true, persona via {fresh:true}, project
    via _paintProjectPicker freshOnOpen:true.  The model paint is fork-gated."""
    body = _APP_JS.read_text(encoding="utf-8")
    fn = _slice_top_level_fn(body, "function showNewWsModal(")
    assert "_paintModelSelects(modelSelect, judgeSelect, { freshOnOpen: true })" in fn, (
        "the modal must paint model+judge fresh-on-open via the shared wrapper"
    )
    assert "_paintSkillSelect(tplSelect, { freshOnOpen: true })" in fn, (
        "the modal must paint skill fresh-on-open"
    )
    assert "_paintPersonaSelect(personaSelect, { freshOnOpen: true })" in fn, (
        "the modal must paint persona fresh-on-open via the shared wrapper"
    )
    proj = fn[fn.find("_paintProjectPicker(projSelect") :]
    assert "freshOnOpen: true" in proj[:120], (
        "the modal must paint the project picker fresh-on-open"
    )
    # Fork-gates: the model + persona paints must each be the FIRST statement
    # inside their own `if (!_forkFromWsId)` block.  (A first-gate ordering
    # check is vacuous here — the first gate in showNewWsModal IS the model
    # gate, so it would pass even with the paint hoisted out below it.)
    assert re.search(r"if \(!_forkFromWsId\) \{\s*_paintModelSelects\(modelSelect", fn), (
        "the modal model paint must be skipped for a fork"
    )
    assert re.search(r"if \(!_forkFromWsId\) \{\s*_paintPersonaSelect\(personaSelect", fn), (
        "the modal persona paint must be skipped for a fork"
    )


def test_dashboard_paints_model_and_skill_via_wrappers_preserving() -> None:
    """Dashboard twin — all four pickers paint via the SAME wrappers but
    freshOnOpen:false (a persistent panel preserves a pick across a repaint), the
    old fetch-once ``options.length <= 1`` guard is gone, and EVERY paint chains
    _refreshDashboardOptionsSummary on its async repaint: a repaint can drop a
    server-removed pick (or revert persona to its kind default) without firing
    'change', and the collapsed Options chip must always name what submit sends."""
    body = _APP_JS.read_text(encoding="utf-8")
    fn = _slice_top_level_fn(body, "function _loadDashboardOptionsLists(")
    for paint in (
        "_paintModelSelects(modelSel, judgeSel, { freshOnOpen: false })",
        "_paintSkillSelect(skillSel, { freshOnOpen: false })",
        "_paintProjectPicker(projSel, projHint, { fork: false })",
        "_paintPersonaSelect(personaSel, { freshOnOpen: false })",
    ):
        assert re.search(re.escape(paint) + r"\.then\(\s*_refreshDashboardOptionsSummary", fn), (
            f"dashboard paint must chain the Options-chip recompute: {paint[:36]}"
        )
    assert "options.length <= 1" not in fn, (
        "the dashboard model/skill fetch-once guard must be removed (refresh-on-open now)"
    )


def test_new_ws_modal_fork_inherits_model_and_judge() -> None:
    """Q2 — a fork INHERITS its source's model + judge: the modal hides both selects
    for a fork (like skill/persona/project), and submitNewWs gates body.model AND
    body.judge_model on !_forkFromWsId.  Asserts the model line SPECIFICALLY — its
    guard `model && !_forkFromWsId` is a substring of the judge line, so a
    model-unguarded regression would otherwise false-pass."""
    body = _APP_JS.read_text(encoding="utf-8")
    modal = _slice_top_level_fn(body, "function showNewWsModal(")
    assert "modelSelect.hidden = !!_forkFromWsId" in modal, (
        "modal must hide the model select for a fork"
    )
    assert "judgeSelect.hidden = !!_forkFromWsId" in modal, (
        "modal must hide the judge select for a fork"
    )
    # [4] fix: the skill paint is fork-gated too (skill is hidden for a fork).
    # Tie the gate to the skill paint SPECIFICALLY — a first-gate check would be
    # satisfied by the model gate even if the skill paint were left
    # unconditional, so require the paint to be the FIRST statement inside its
    # own gate block.  (A nearest-preceding-gate proximity window false-passes
    # a gate block that CLOSES before the paint.)
    skill_paint = modal.find("_paintSkillSelect(tplSelect")
    assert skill_paint >= 0, "the modal must paint the skill picker"
    assert re.search(r"if \(!_forkFromWsId\) \{\s*_paintSkillSelect\(tplSelect", modal), (
        "the modal skill paint must be directly wrapped in `if (!_forkFromWsId)` "
        "(skip the wasted fetch + hidden-select rebuild on a fork)"
    )
    submit = _slice_top_level_fn(body, "function submitNewWs(")
    assert "if (model && !_forkFromWsId) body.model = model;" in submit, (
        "submitNewWs must fork-gate body.model (distinct from the judge line)"
    )
    assert "if (judge_model && !_forkFromWsId) body.judge_model = judge_model;" in submit, (
        "submitNewWs must fork-gate body.judge_model"
    )


def test_console_launcher_paints_model_and_skill_from_cache_synchronously() -> None:
    """Console launcher — the four _refreshAndPopulate* wrappers route through the
    _paintHomeFromCache chokepoint: sync paint from the warm cache BEFORE the
    async refresh(callOpts)-and-repaint (callOpts threads {force:true} for the
    models_changed / onLoginSuccess invalidation callers).  The ordering
    discipline is asserted once, on the helper; the wrappers are pairing-checked
    (model/skill here, project/persona in their twin test)."""
    body = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    helper = _slice_top_level_fn(body, "function _paintHomeFromCache(")
    sync = helper.find("populate()")
    async_ = helper.find("refresh(callOpts).then")
    assert 0 <= sync < async_, (
        "_paintHomeFromCache must sync-paint (populate()) BEFORE the async "
        "refresh(callOpts).then repaint"
    )
    model_fn = _slice_top_level_fn(body, "function _refreshAndPopulateModels(")
    assert re.search(
        r"_paintHomeFromCache\(\s*TM && TM\.refreshModels,\s*_populateHomeModelDropdowns", model_fn
    ), "the launcher model wrapper must pair its bridge refresh + populate via the chokepoint"
    skill_fn = _slice_top_level_fn(body, "function _refreshAndPopulateSkills(")
    assert re.search(
        r"_paintHomeFromCache\(\s*TS && TS\.refreshSkills,\s*_populateHomeSkillDropdown", skill_fn
    ), "the launcher skill wrapper must pair its bridge refresh + populate via the chokepoint"


def test_console_relogin_rewarms_all_four_caches_with_force() -> None:
    """onLoginSuccess re-warms ALL FOUR composer caches after auth lands (the boot
    pass runs pre-login -> 401 -> fail-open empty), EACH with {force:true} so a
    still-in-flight failing pre-auth fetch yields a trailing authenticated refetch
    (skills/personas have no *_changed event to recover otherwise). Fixes [0]+[2]."""
    body = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    start = body.index("window.onLoginSuccess = function ()")
    # The four re-warm calls live before the // Active-coordinators marker; an
    # assert falling outside this slice fails loudly rather than silently passing.
    login = body[start : body.index("// Active-coordinators", start)]
    for name in ("Skills", "Models", "Projects", "Personas"):
        assert f"_refreshAndPopulate{name}({{ force: true }})" in login, (
            f"onLoginSuccess must force-re-warm {name.lower()} after login"
        )


def test_models_changed_forces_trailing_single_repaint_path() -> None:
    """The console ``models_changed`` handler repaints via the refresh wrapper with
    {force:true} — so a burst of model-config changes converges to the latest server
    state (trailing refresh) — and the launcher does NOT ALSO subscribe onModelsChange
    (one repaint path, no double rebuild)."""
    body = _CONSOLE_APP_JS.read_text(encoding="utf-8")
    mc = body.index('data.type === "models_changed"')
    handler = body[mc : mc + 700]
    assert "_refreshAndPopulateModels({ force: true })" in handler, (
        "models_changed must force a trailing refresh so it converges to the latest"
    )
    assert "onModelsChange" not in body, (
        "the console must not ALSO subscribe onModelsChange (single repaint path)"
    )


def test_models_label_centralized_on_bridge() -> None:
    """Finding [7] — the "alias (model)" label lives ONCE in models.js: modelLabel is
    exported AND registered on the window.TurnstoneModels bridge (the classic app.js
    bundles reach it only via the bridge — an ES-only export throws at runtime), and
    neither app keeps a local _resolveModelLabel copy."""
    models_src = _MODELS_JS.read_text(encoding="utf-8")
    assert "export function modelLabel(" in models_src, "models.js must export modelLabel"
    assert "modelLabel: modelLabel" in models_src, (
        "models.js must register modelLabel on the window.TurnstoneModels bridge "
        "(classic bundles call it via the bridge)"
    )
    # [7] fix: modelLabel resolves via the core's O(1) keyField index, not a scan.
    assert 'keyField: "alias"' in models_src, (
        "models.js must index by alias (keyField) so modelLabel is an O(1) getByKey"
    )
    assert "getByKey(alias)" in models_src, (
        "modelLabel must resolve via the core's getByKey index (not a per-paint scan)"
    )
    assert "_resolveModelLabel" not in _APP_JS.read_text(encoding="utf-8"), (
        "the ui app must not keep a local _resolveModelLabel (use TurnstoneModels.modelLabel)"
    )
    assert "_resolveModelLabel" not in _CONSOLE_APP_JS.read_text(encoding="utf-8"), (
        "the console app must not keep a local _resolveModelLabel"
    )


def test_models_skills_module_tagged_in_both_apps() -> None:
    """All four data-layer modules (projects/personas/models/skills) are
    ``<script type=module>``-tagged BEFORE /shared/shell.js in BOTH index.html.
    shell.js is the LAST module tag and calls TS_APP.boot (the first dashboard /
    launcher paint); module execution follows tag order, and classic app.js is
    parse-time definitions only — so a data-layer tag reordered below shell.js
    boots the app before that bridge installs: the sync paint no-ops AND the
    refresh is skipped, a silent test-green FOUC regression without this guard."""
    for idx in (_INDEX_HTML, _CONSOLE_INDEX):
        html = idx.read_text(encoding="utf-8")
        for mod in ("projects", "personas", "models", "skills"):
            path = f"/shared/{mod}.js"
            assert path in html, f"{mod}.js must be module-tagged in {idx.name}"
            assert html.index(path) < html.index("/shared/shell.js"), (
                f"{mod}.js must be tagged BEFORE shell.js in {idx.name} — its "
                "bridge must install before TS_APP.boot paints the composers"
            )


def test_list_cache_core_is_failopen_coalesced_and_gated() -> None:
    """The extracted list_cache.js core: non-force callers coalesce onto the in-flight
    refresh; it fails open (keeps the prior cache + records the error, never rejects —
    only the SUCCESS branch calls _setCache); the extra-reset is GATED on
    resetExtraOnError across BOTH error branches (finding [1]); and a force caller
    schedules a trailing refetch that converges to the latest (finding [2])."""
    src = _LIST_CACHE_JS.read_text(encoding="utf-8")
    assert "return _inflight;" in src, "non-force callers must coalesce onto the in-flight refresh"
    assert src.count("_byKey = Object.create(null)") == 2, (
        "both _byKey sites (declaration + _setCache rebuild) must be null-prototype "
        "(a row keyed __proto__ must not swap the map's prototype; inherited members "
        "must not resolve as rows)"
    )
    assert "_lastError = r.status" in src and "_lastError = 0" in src, (
        "a non-OK status and a network/parse error must both be recorded (fail-open)"
    )
    assert src.count("_setCache(data[dataKey]") == 1, (
        "only the success branch may repopulate the cache (non-OK/catch keep the prior cache)"
    )
    # finding [1]: the extra-reset must be gated on resetExtraOnError on BOTH the
    # non-OK branch AND the network/parse .catch branch (a cosmetic extra must
    # survive a network drop too, not just a non-OK status).
    assert src.count("resetExtraOnError && extraDefaults") == 2, (
        "resetExtraOnError must gate the extra-reset on BOTH error branches"
    )
    # finding [2]: a force caller gets a trailing refetch (converges to latest).
    assert "callOpts.force" in src and "_pending" in src, (
        "a force caller must schedule a trailing refetch so it converges to the latest state"
    )
    assert "firstLoad || fp !== _fingerprint" in src, (
        "subscribers must fire on first load or a changed fingerprint only"
    )


def test_reset_extra_policy_per_cache() -> None:
    """require_project (an advisory that GATES the picker) must fail open on error;
    the models default-aliases (a COSMETIC annotation) must keep their last-known
    value.  So projects opts INTO the reset, models opts OUT (finding [1])."""
    assert "resetExtraOnError: true" in _PROJECTS_JS.read_text(encoding="utf-8"), (
        "projects.js must reset the require_project advisory on error (fail-open)"
    )
    assert "resetExtraOnError: false" in _MODELS_JS.read_text(encoding="utf-8"), (
        "models.js must keep last-known default aliases on error (cosmetic, not a gate)"
    )


def test_models_cache_exposes_both_server_schemas() -> None:
    """models.js must carry ALL default-alias fields — the node server sends
    default_alias, the console sends coordinator_default_alias, both send
    judge_default_alias — so each app reads its own (via modelDefaults/captureExtra,
    which drive the "Default — <alias>" placeholder).  The dead onChange
    subscription (+ its fingerprint fold) was removed: models has no live-render
    subscriber (the console repaints via its direct models_changed handler), so it
    exposes no onModelsChange."""
    src = _MODELS_JS.read_text(encoding="utf-8")
    for field in ("default_alias", "judge_default_alias", "coordinator_default_alias"):
        assert field in src, f"models.js must carry {field} for the two server schemas"
    assert "window.TurnstoneModels" in src, "models.js must install the classic bridge"
    assert "makeListCache" in src, "models.js must build on the shared list_cache core"
    assert "onModelsChange" not in src, (
        "models.js must not expose the dead onModelsChange subscription (no subscribers)"
    )


def test_skills_cache_returns_raw_rows() -> None:
    """skills.js exposes raw rows (getSkills) — the ui pickers add a ' [MCP]' suffix
    the console omits, so a single pre-formatted label in the cache would silently
    change one app's labels."""
    src = _SKILLS_JS.read_text(encoding="utf-8")
    assert "window.TurnstoneSkills" in src, "skills.js must install the classic bridge"
    assert "makeListCache" in src, "skills.js must build on the shared list_cache core"
    assert "getSkills" in src, "skills.js must expose raw rows via getSkills"


def test_projects_personas_keep_public_surface_on_factory() -> None:
    """The projects.js / personas.js retrofit onto list_cache.js must preserve their
    full public surface — rail.js and project_creator.js import these by name, and
    the classic bundles read the window bridges."""
    proj = _PROJECTS_JS.read_text(encoding="utf-8")
    assert "makeListCache" in proj, "projects.js must build on the shared core"
    for name in (
        "refreshProjects",
        "getProjects",
        "projectsLoaded",
        "projectsError",
        "requireProject",
        "projectName",
        "projectChoices",
        "onProjectsChange",
        "createProject",
    ):
        assert f"function {name}(" in proj, f"projects.js must still export {name}"
    assert "requireProject: false" in proj, (
        "the require_project advisory must seed / fail-open to false"
    )
    persona = _PERSONAS_JS.read_text(encoding="utf-8")
    assert "makeListCache" in persona, "personas.js must build on the shared core"
    for name in (
        "refreshPersonas",
        "getPersonas",
        "personasLoaded",
        "personasError",
        "personaLabel",
        "defaultPersona",
        "personaChoices",
        "onPersonasChange",
    ):
        assert f"function {name}(" in persona, f"personas.js must still export {name}"
