"""Tests for the unified ``approve_tools`` body, viewed from the coord side.

The body itself is exercised by ``test_webui_auto_approve_visibility``;
this file pins down the coord-specific contracts that lifting the body
to ``SessionUIBase`` automatically enables:

- Tool-policy gating now applies to coord tool calls (was interactive-only).
- Heuristic verdicts persist on coord (was interactive-only).
- The activity tag fields populate on coord during pending approval.
- ``judge_pending`` is dynamic on the coord ``approve_request``
  (was hardcoded ``False``).
- The auto-approve fall-through emits ``tool_info`` (was
  ``tools_auto_approved``).
- ``_record_judge_metric`` is a no-op on coord (no Prometheus on console).
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

from turnstone.console.coordinator_ui import ConsoleCoordinatorUI


def _make_items(*specs: tuple[str, str], needs_approval: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "call_id": call_id,
            "header": f"Tool: {func}",
            "preview": "preview text",
            "func_name": func,
            "approval_label": func,
            "needs_approval": needs_approval,
        }
        for call_id, func in specs
    ]


def _patch_storage(storage: Any):
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def _patch_policies(verdicts: dict[str, str]):
    return patch(
        "turnstone.core.policy.evaluate_tool_policies_batch",
        return_value=verdicts,
    )


# ---------------------------------------------------------------------------
# Inheritance regression — the unification itself
# ---------------------------------------------------------------------------


def test_coord_inherits_approve_tools_from_base() -> None:
    """``ConsoleCoordinatorUI`` must NOT define its own ``approve_tools``;
    the shared body lives on :class:`SessionUIBase`. A future drift —
    adding a coord-only override — is exactly the kind of bug this
    unification is meant to prevent, so guard it explicitly."""
    assert "approve_tools" not in ConsoleCoordinatorUI.__dict__, (
        "ConsoleCoordinatorUI shouldn't redefine approve_tools — "
        "the shared body on SessionUIBase covers both kinds."
    )
    assert ConsoleCoordinatorUI.approve_tools.__qualname__ == "SessionUIBase.approve_tools"


# ---------------------------------------------------------------------------
# Tool-policy gating now applies to coord
# ---------------------------------------------------------------------------


def test_coord_tool_policy_deny_blocks_coord_tool() -> None:
    """Admin-defined ``deny`` policies now fire on coord tool calls.
    Pre-lift this was interactive-only; an admin who wanted to block
    e.g. ``delete_workstream`` on the coord couldn't."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "delete_workstream"))

    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({"delete_workstream": "deny"}):
        approved, err = ui.approve_tools(items)

    assert approved is False
    assert err == "Blocked by tool policy"
    assert items[0].get("denied") is True


def test_coord_tool_policy_allow_tags_with_policy_source() -> None:
    """Admin ``allow`` rule auto-approves the item with
    ``AutoApproveReason.POLICY``. This was a no-op on coord pre-lift."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "spawn_workstream"))

    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({"spawn_workstream": "allow"}):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "spawn_workstream"
    assert snapshot[0]["auto_approve_reason"] == "policy"


def test_coord_tool_policy_mixed_allow_deny_records_allowed_sibling() -> None:
    """Same ``mixed-policy`` audit-leak fix that
    ``test_webui_auto_approve_visibility`` validates for interactive,
    now auto-applies to coord via the lifted body."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "delete_workstream"), ("c2", "list_workstreams"))

    storage = MagicMock()
    with (
        _patch_storage(storage),
        _patch_policies({"delete_workstream": "deny", "list_workstreams": "allow"}),
    ):
        approved, _err = ui.approve_tools(items)

    assert approved is False
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "list_workstreams"
    assert snapshot[0]["auto_approve_reason"] == "policy"


# ---------------------------------------------------------------------------
# Heuristic-verdict persistence + metric hook
# ---------------------------------------------------------------------------


def test_coord_heuristic_verdict_persists_to_storage() -> None:
    """Heuristic verdicts attached to items now flow through to
    ``storage.create_intent_verdicts_bulk`` on coord. Pre-lift coord
    silently dropped them; only LLM-tier verdicts (from the daemon
    judge thread via ``on_intent_verdict``) reached storage. Post
    perf-2 the path uses bulk INSERT so a fan-out turn pays one commit
    instead of N."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    hv = {
        "verdict_id": "v1",
        "call_id": "c1",
        "func_name": "spawn_workstream",
        "tier": "heuristic",
        "risk_level": "high",
        "confidence": 0.75,
        "recommendation": "review",
        "reasoning": "spawning child with bash skill",
        "evidence": ["bash"],
        "latency_ms": 12,
    }
    items = _make_items(("c1", "spawn_workstream"))
    items[0]["_heuristic_verdict"] = hv

    storage = MagicMock()
    timer = threading.Timer(0.05, lambda: ui.resolve_approval(False))
    timer.start()
    try:
        with _patch_storage(storage):
            ui.approve_tools(items)
    finally:
        timer.cancel()

    storage.create_intent_verdicts_bulk.assert_called_once()
    rows = storage.create_intent_verdicts_bulk.call_args.args[0]
    assert len(rows) == 1
    assert rows[0]["verdict_id"] == "v1"
    assert rows[0]["tier"] == "heuristic"
    assert rows[0]["ws_id"] == "coord-1"


def test_coord_record_judge_metric_fires_console_metrics() -> None:
    """``_record_judge_metric`` increments the console's
    ``ConsoleMetrics`` judge counter when the class attribute is wired,
    so coord verdicts surface on the console's /metrics endpoint
    alongside the per-node series."""
    from turnstone.console.metrics import ConsoleMetrics

    cm = ConsoleMetrics()
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    try:
        ConsoleCoordinatorUI._console_metrics = cm
        ui._record_judge_metric({"tier": "heuristic", "risk_level": "high", "latency_ms": 12})
    finally:
        ConsoleCoordinatorUI._console_metrics = None

    text = cm.generate_text()
    assert 'turnstone_judge_verdicts_total{tier="heuristic",risk_level="high"} 1' in text


def test_coord_record_judge_metric_safe_when_unwired() -> None:
    """No /metrics instance set → silent no-op. Test fixtures that
    don't spin up a full console app must not crash on judge
    verdicts during the shared ``approve_tools`` body."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    # Sanity: class attribute is None at module import time outside
    # the lifespan — exactly the test-fixture state.
    assert ConsoleCoordinatorUI._console_metrics is None
    # Should not raise.
    ui._record_judge_metric({"tier": "heuristic", "risk_level": "low"})


def test_coord_on_intent_verdict_fires_metric_for_llm_tier() -> None:
    """Async LLM verdicts from the daemon judge thread land at
    ``on_intent_verdict``. Coord overrides it to fire the same
    ``record_judge_verdict`` call WebUI does — different tier label,
    same cluster-wide histogram."""
    from turnstone.console.metrics import ConsoleMetrics

    cm = ConsoleMetrics()
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    try:
        ConsoleCoordinatorUI._console_metrics = cm
        with _patch_storage(MagicMock()):
            ui.on_intent_verdict(
                {
                    "verdict_id": "v1",
                    "call_id": "c1",
                    "tier": "llm",
                    "risk_level": "medium",
                    "latency_ms": 250,
                }
            )
    finally:
        ConsoleCoordinatorUI._console_metrics = None

    text = cm.generate_text()
    assert 'turnstone_judge_verdicts_total{tier="llm",risk_level="medium"} 1' in text


# ---------------------------------------------------------------------------
# Activity tagging during pending approval
# ---------------------------------------------------------------------------


def test_coord_pending_approval_sets_activity_tag() -> None:
    """The shared body tags ``_ws_current_activity`` /
    ``_ws_activity_state`` so the cluster collector's coord-row
    snapshot reflects the approval wait. Pre-lift coord left these
    fields empty during pending approval."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "delete_workstream"))

    captured: dict[str, str] = {}

    def _capture_activity() -> None:
        captured["activity"] = ui._ws_current_activity
        captured["state"] = ui._ws_activity_state
        ui.resolve_approval(False)

    timer = threading.Timer(0.05, _capture_activity)
    timer.start()
    try:
        with _patch_storage(MagicMock()):
            ui.approve_tools(items)
    finally:
        timer.cancel()

    assert "Awaiting approval" in captured["activity"]
    assert "delete_workstream" in captured["activity"]
    assert captured["state"] == "approval"


def test_coord_auto_approve_sets_tool_activity_tag() -> None:
    """Blanket auto-approve flips activity to the ``⚙ {tool}: {preview}``
    shape WebUI has used; coord row now mirrors it."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True
    items = _make_items(("c1", "spawn_workstream"))

    with _patch_storage(MagicMock()):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    assert "spawn_workstream" in ui._ws_current_activity
    assert ui._ws_activity_state == "tool"


# ---------------------------------------------------------------------------
# judge_pending flag + event-name parity
# ---------------------------------------------------------------------------


def test_coord_judge_pending_flag_dynamic_when_heuristic_present() -> None:
    """Pre-lift coord hardcoded ``judge_pending=False`` on every
    ``approve_request``; the unified body computes the bool from the
    items, matching WebUI."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "spawn_workstream"))
    items[0]["_heuristic_verdict"] = {"verdict_id": "v1", "tier": "heuristic"}

    captured_events: list[dict[str, Any]] = []
    ui._enqueue = captured_events.append  # type: ignore[method-assign]

    timer = threading.Timer(0.05, lambda: ui.resolve_approval(False))
    timer.start()
    try:
        with _patch_storage(MagicMock()):
            ui.approve_tools(items)
    finally:
        timer.cancel()

    approve_requests = [e for e in captured_events if e.get("type") == "approve_request"]
    assert len(approve_requests) == 1
    assert approve_requests[0]["judge_pending"] is True


def test_coord_blanket_auto_approve_emits_tool_info() -> None:
    """Event-name parity: the auto-approve fall-through emits
    ``tool_info`` for both kinds. Pre-lift coord emitted
    ``tools_auto_approved`` — the rename happens implicitly via
    inheritance."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True
    items = _make_items(("c1", "spawn_workstream"))

    captured_events: list[dict[str, Any]] = []
    ui._enqueue = captured_events.append  # type: ignore[method-assign]

    with _patch_storage(MagicMock()):
        ui.approve_tools(items)

    types = [e.get("type") for e in captured_events]
    assert "tool_info" in types
    assert "tools_auto_approved" not in types


def test_coord_judge_pending_false_when_no_heuristic_verdict() -> None:
    """Counterpart to ``test_coord_judge_pending_flag_dynamic_when_heuristic_present``:
    items with no ``_heuristic_verdict`` produce ``approve_request`` with
    ``judge_pending=False``. Without this case pinned, a regression that
    hardcodes ``judge_pending=True`` (the inverse of the pre-lift coord
    bug) would slip through unnoticed."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "spawn_workstream"))
    # Deliberately no _heuristic_verdict on any item.

    captured_events: list[dict[str, Any]] = []
    ui._enqueue = captured_events.append  # type: ignore[method-assign]

    timer = threading.Timer(0.05, lambda: ui.resolve_approval(False))
    timer.start()
    try:
        with _patch_storage(MagicMock()):
            ui.approve_tools(items)
    finally:
        timer.cancel()

    approve_requests = [e for e in captured_events if e.get("type") == "approve_request"]
    assert len(approve_requests) == 1
    assert approve_requests[0]["judge_pending"] is False


# ---------------------------------------------------------------------------
# Per-tool auto-approve via auto_approve_tools (set membership)
# ---------------------------------------------------------------------------


def test_coord_per_tool_auto_approve_tags_with_source() -> None:
    """When a coord tool name lands in ``auto_approve_tools`` (e.g. via a
    skill template's ``allowed_tools``), the lifted body short-circuits
    the prompt and tags the item with ``AutoApproveReason.AUTO_APPROVE_TOOLS``
    (or the per-tool source from ``_auto_approve_tools_source``).
    Mirrors the WebUI test ``test_auto_approve_tools_skill_source_renders_as_skill``
    on the coord side so the unified body gains parity coverage."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve_tools = {"spawn_workstream"}
    ui._auto_approve_tools_source = {"spawn_workstream": "skill"}
    items = _make_items(("c1", "spawn_workstream"))

    storage = MagicMock()
    with _patch_storage(storage):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "spawn_workstream"
    assert snapshot[0]["auto_approve_reason"] == "skill"


# ---------------------------------------------------------------------------
# __budget_override__ carve-out — sec-2 hardening
# ---------------------------------------------------------------------------


def test_coord_budget_override_prompts_even_under_blanket_auto_approve() -> None:
    """The carve-out promises ``__budget_override__`` always prompts the
    operator. Pin that behavior on the coord side so a future regression
    of the post-filter / pre-filter check (sec-2) gets caught.

    ``__budget_override__`` is interactive-only today (coord workstreams
    don't have token budgets), but the synthetic item can be threaded
    through ``approve_tools`` directly the same way ``ChatSession.send``
    does on the interactive side. The carve-out fires uniformly across
    both kinds."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True  # blanket flag — should NOT bypass the carve-out
    items = [
        {
            "call_id": "c1",
            "header": "Token budget exhausted",
            "preview": "Token budget (200,000) exhausted. Approve to continue.",
            "func_name": "__budget_override__",
            "approval_label": "__budget_override__",
            "needs_approval": True,
        }
    ]

    captured_events: list[dict[str, Any]] = []
    ui._enqueue = captured_events.append  # type: ignore[method-assign]

    timer = threading.Timer(0.05, lambda: ui.resolve_approval(True))
    timer.start()
    try:
        with _patch_storage(MagicMock()):
            approved, _err = ui.approve_tools(items)
    finally:
        timer.cancel()

    assert approved is True
    # The carve-out forces the prompt path, NOT the auto-approve fall-through.
    types = [e.get("type") for e in captured_events]
    assert "approve_request" in types, (
        "Budget override must produce an approve_request even under blanket auto_approve"
    )
    assert "tool_info" not in types, (
        "Auto-approve fall-through must not fire when a budget override is present"
    )


def test_coord_budget_override_survives_wildcard_allow_policy() -> None:
    """A wildcard ``*: allow`` policy must not strip ``__budget_override__``
    from the gate. Pre-sec-2, the policy block could mark the item
    ``needs_approval=False`` and remove it from ``pending``, after which
    the carve-out (which read ``pending``) would see no override and
    blanket auto-approve would silently fire. Post-fix the carve-out
    reads from the pre-filter ``items`` list AND the policy block skips
    matching the synthetic name entirely."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True
    items = [
        {
            "call_id": "c1",
            "header": "Token budget exhausted",
            "preview": "Token budget exhausted. Approve to continue.",
            "func_name": "__budget_override__",
            "approval_label": "__budget_override__",
            "needs_approval": True,
        }
    ]

    captured_events: list[dict[str, Any]] = []
    ui._enqueue = captured_events.append  # type: ignore[method-assign]

    timer = threading.Timer(0.05, lambda: ui.resolve_approval(True))
    timer.start()
    try:
        with _patch_storage(MagicMock()), _patch_policies({"__budget_override__": "allow"}):
            approved, _err = ui.approve_tools(items)
    finally:
        timer.cancel()

    assert approved is True
    types = [e.get("type") for e in captured_events]
    assert "approve_request" in types, "Wildcard allow must not strip the budget-override prompt"


# ---------------------------------------------------------------------------
# Cluster-bus broadcast hooks — _broadcast_intent_verdict / _approval_resolved
# ---------------------------------------------------------------------------


class TestBroadcastIntentVerdict:
    """``ConsoleCoordinatorUI._broadcast_intent_verdict`` overrides the
    no-op base hook to push the verdict onto the cluster bus via
    ``ClusterCollector.emit_console_ws_intent_verdict``. The far more
    common path is the per-node ``WebUI`` override (covered in
    test_webui_content.py); this lights up the rare coord-self path
    (a coord that runs its own LLM judge).
    """

    def test_calls_collector_emit_with_ws_id_and_verdict(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        ConsoleCoordinatorUI._collector = collector
        try:
            verdict = {
                "call_id": "c1",
                "risk_level": "high",
                "confidence": 0.91,
            }
            ui._broadcast_intent_verdict(verdict)
            collector.emit_console_ws_intent_verdict.assert_called_once_with(
                "coord-a",
                verdict,
            )
        finally:
            ConsoleCoordinatorUI._collector = None

    def test_no_op_when_collector_unset(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        ConsoleCoordinatorUI._collector = None
        # Doesn't raise.
        ui._broadcast_intent_verdict({"call_id": "c1"})

    def test_collector_exception_swallowed(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        collector.emit_console_ws_intent_verdict.side_effect = RuntimeError("boom")
        ConsoleCoordinatorUI._collector = collector
        try:
            # Doesn't raise — collector failures are observational only.
            ui._broadcast_intent_verdict({"call_id": "c1"})
        finally:
            ConsoleCoordinatorUI._collector = None


class TestBroadcastApprovalResolved:
    """``ConsoleCoordinatorUI._broadcast_approval_resolved`` overrides
    the base hook to push the resolution onto the cluster bus via
    ``ClusterCollector.emit_console_ws_approval_resolved``."""

    def test_calls_collector_with_decision_fields(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        ConsoleCoordinatorUI._collector = collector
        try:
            ui._broadcast_approval_resolved(True, "lgtm", always=True)
            collector.emit_console_ws_approval_resolved.assert_called_once_with(
                "coord-a",
                approved=True,
                feedback="lgtm",
                always=True,
            )
        finally:
            ConsoleCoordinatorUI._collector = None

    def test_normalises_none_feedback_to_empty_string(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        ConsoleCoordinatorUI._collector = collector
        try:
            ui._broadcast_approval_resolved(False, None)
            collector.emit_console_ws_approval_resolved.assert_called_once_with(
                "coord-a",
                approved=False,
                feedback="",
                always=False,
            )
        finally:
            ConsoleCoordinatorUI._collector = None

    def test_no_op_when_collector_unset(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        ConsoleCoordinatorUI._collector = None
        # Doesn't raise.
        ui._broadcast_approval_resolved(True, None)

    def test_collector_exception_swallowed(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        collector.emit_console_ws_approval_resolved.side_effect = RuntimeError("boom")
        ConsoleCoordinatorUI._collector = collector
        try:
            # Doesn't raise.
            ui._broadcast_approval_resolved(True, "ok")
        finally:
            ConsoleCoordinatorUI._collector = None


class TestBroadcastApproveRequest:
    """Coord-side override for the approve_request push. Same rationale
    as the WebUI override — the coord-self path is rare today, but
    parity keeps the override symmetric with the rest of the broadcast
    family."""

    def test_calls_collector_emit_with_ws_id_and_detail(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        ConsoleCoordinatorUI._collector = collector
        try:
            detail = {
                "type": "approve_request",
                "items": [{"call_id": "c1", "header": "tool x"}],
                "judge_pending": True,
            }
            ui._broadcast_approve_request(detail)
            collector.emit_console_ws_approve_request.assert_called_once_with(
                "coord-a",
                detail,
            )
        finally:
            ConsoleCoordinatorUI._collector = None

    def test_no_op_when_collector_unset(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        ConsoleCoordinatorUI._collector = None
        # Doesn't raise.
        ui._broadcast_approve_request({"items": []})

    def test_collector_exception_swallowed(self) -> None:
        ui = ConsoleCoordinatorUI(ws_id="coord-a", user_id="u1")
        collector = MagicMock()
        collector.emit_console_ws_approve_request.side_effect = RuntimeError("boom")
        ConsoleCoordinatorUI._collector = collector
        try:
            # Doesn't raise.
            ui._broadcast_approve_request({"items": []})
        finally:
            ConsoleCoordinatorUI._collector = None
