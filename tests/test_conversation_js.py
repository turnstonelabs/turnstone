"""Guards for the shared conversational-pane module
(``turnstone/shared_static/conversation.js``).

Born in step 5e.1: the deduplicated substrate BOTH the interactive pane
(shared_static/interactive.js) and the coordinator pane
(console/static/coordinator/coordinator.js) import.  These pin the exports plus
the load-bearing invariants (operator-context marker, null-safe ANSI strip, no
innerHTML) so a regression in the shared module fails loudly here rather than
silently in one pane.
"""

from __future__ import annotations

from pathlib import Path

_CONVERSATION_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/shared_static/conversation.js"
)


def _body() -> str:
    return _CONVERSATION_JS.read_text(encoding="utf-8")


def test_exports_the_shared_helpers() -> None:
    """The three helpers both panes import must be exported — drop one and the
    importing pane module fails to load entirely."""
    body = _body()
    for name in ("stripAnsi", "buildWatchResultCard", "buildSystemNudgeMarker"):
        assert f"export function {name}" in body, f"{name} must be exported"


def test_strip_ansi_is_null_safe() -> None:
    """Unified on the coordinator's null-safe variant: a non-string argument
    coerces to "" rather than throwing (interactive's old copy did not guard,
    so this is a strict-superset behaviour for its call sites)."""
    body = _body()
    assert 'String(s == null ? "" : s).replace(' in body, (
        "stripAnsi must coerce its argument before .replace"
    )


def test_watch_card_carries_operator_context_marker() -> None:
    """The watch-result card keeps the shared ``operator-context`` marker (the
    retry-walk in both panes skips rows carrying it) and stays textContent-only."""
    body = _body()
    assert '"msg watch-result operator-context"' in body
    assert 'setAttribute("data-ts-role", "watch")' in body
    for part in (
        "msg-watch-header",
        "msg-watch-cmd",
        "msg-watch-body",
        "msg-watch-footer",
    ):
        assert part in body, f"watch card missing {part}"


def test_nudge_marker_shape() -> None:
    body = _body()
    assert '"msg user system-nudge"' in body
    assert 'setAttribute("data-source", "system_nudge")' in body


def test_no_inner_html() -> None:
    """House style: programmatic DOM only — no innerHTML *usage* in the shared
    module (the header comment names it; guard the access pattern)."""
    assert ".innerHTML" not in _body()


def test_normalize_risk_level_unknown_to_medium() -> None:
    """Unified canonical fallback (step 5e.1b): an unknown / unrecognized risk
    normalizes to "medium" (the user's decision; the coordinator's old rank used
    "high").  The crit/med abbreviations alias to critical/medium so a 'crit'
    verdict no longer renders as medium (the latent interactive bug)."""
    body = _body()
    assert 'return RISK_LEVELS.indexOf(s) >= 0 ? s : "medium";' in body
    assert 'crit: "critical"' in body and 'med: "medium"' in body


def test_risk_rank_and_max_severity_exported() -> None:
    """riskRank + maxSeverityItem (lifted from the coordinator's _riskRank /
    _maxSeverityItem) are exported and build on the canonical normalize, so the
    rank and the display can't disagree on the fallback.  An item with no verdict
    ranks below low so it never wins the max-severity pick."""
    body = _body()
    assert "export function riskRank(" in body
    assert "export function maxSeverityItem(" in body
    assert "? riskRank(v.risk_level) : -1;" in body


# --- step 5e.2b: the shared approval-card builders ---------------------------


def test_card_builders_exported() -> None:
    """The leaf DOM builders both panes' orchestration calls (5e.2c).  Drop one
    and the calling pane fails to construct its half of the converged card."""
    body = _body()
    for name in (
        "buildConvBatchShell",
        "buildConvRow",
        "buildConvCmd",
        "buildConvVerdict",
        "buildConvWarning",
        "buildConvButton",
        "buildConvActions",
        "buildConvStatus",
        "buildConvResult",
    ):
        assert f"export function {name}(" in body, f"{name} must be exported"


def test_builders_emit_conv_vocabulary() -> None:
    """The builders speak ONLY the neutral .conv-* vocabulary (conversation.css)
    — no leaked .coord-tool-* / .ts-approval-* / .verdict-* class strings."""
    body = _body()
    for cls in (
        '"conv-batch"',
        '"conv-row"',
        '"conv-row-call"',
        '"conv-verdict"',
        '"conv-warning conv-warning--"',
        '"conv-actions"',
        '"conv-btn conv-btn--"',
        '"conv-status"',
        '"conv-row-result"',
    ):
        assert cls in body, f"builders missing {cls}"
    for stale in ("coord-tool-", "ts-approval-", "verdict-badge"):
        assert stale not in body, f"builders leaked stale vocab: {stale}"


def test_approve_all_label_unified() -> None:
    """Button language (BRIEFING): the persistent action reads 'Approve all'
    (a dashed --ok ghost), NOT the coordinator's old 'Always'.  The trio is
    Approve / Deny / Approve all on the .conv-btn--{role} vocabulary."""
    body = _body()
    assert '"Approve all"' in body  # unified persistent-action label
    assert '"Always"' not in body  # the coordinator's old label is gone
    assert 'buildConvButton("approve", "Approve"' in body
    assert 'buildConvButton("deny", "Deny"' in body
    assert "conv-btn conv-btn--" in body


def test_warning_and_verdict_normalize_risk() -> None:
    """Both risk-bearing builders route risk through normalizeRiskLevel, so the
    per-site `|| "medium"` fallbacks collapse onto the canonical unknown->medium
    fold (5e.1b) and 'crit' aliases to 'critical'."""
    body = _body()
    assert "normalizeRiskLevel(verdict.risk_level)" in body, "verdict must normalize"
    assert "normalizeRiskLevel(a.risk_level)" in body, "warning must normalize"
    assert '"conv-warning conv-warning--" + risk' in body
    assert 'badge.classList.add("conv-verdict--" + risk)' in body


def test_unbounded_render_inputs_are_capped() -> None:
    """Perf-audit P0: the two builders that used to render unbounded input.
    The diff preview caps rendered lines and appends incrementally — the old
    single ``diff.append(...nodes)`` spread threw RangeError past engine
    spread-arity limits, killing the tool card (and the approval gate) for
    the batch.  The raw result body clamps at RAW_CAP so one multi-MB tool
    output can't become a multi-MB pre-wrap text node rebuilt on every
    re-render."""
    body = _body()
    assert "MAX_PREVIEW_LINES" in body
    assert "diff.append(...nodes)" not in body, (
        "preview nodes must append incrementally, not via one spread call"
    )
    assert "more preview lines not shown" in body
    assert "RAW_CAP" in body
    assert "truncated for display" in body
