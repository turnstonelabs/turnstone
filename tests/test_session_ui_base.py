"""Tests for ``SessionUIBase`` — the shared UI scaffolding.

Covers listener fan-out, approval / plan blocking gates, intent-judge
verdict bookkeeping, and the approval-cycle reset invariant that
prevents a late verdict from inheriting the previous round's
``user_decision``.

These are unit tests exercising the base class directly via a thin
concrete subclass — subclass-specific behaviour (WebUI's per-UI
metrics broadcast, ConsoleCoordinatorUI's collector fan-out) lives
in its own test files.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock, patch

from turnstone.core.session_ui_base import SessionUIBase


class _ConcreteUI(SessionUIBase):
    """Minimal concrete subclass — no kind-specific overrides.

    Exists only so we can instantiate the base (it's designed to be
    subclassed). Inherits the full base behaviour verbatim.
    """


def _make_ui(ws_id: str = "ws-1", user_id: str = "u1") -> _ConcreteUI:
    return _ConcreteUI(ws_id=ws_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Listener fan-out
# ---------------------------------------------------------------------------


def test_register_listener_returns_fresh_queue() -> None:
    ui = _make_ui()
    lq = ui._register_listener()
    assert isinstance(lq, queue.Queue)
    assert lq in ui._listeners


def test_enqueue_fans_out_to_all_listeners() -> None:
    ui = _make_ui()
    lq1 = ui._register_listener()
    lq2 = ui._register_listener()
    ui._enqueue({"type": "hello"})
    assert lq1.get_nowait() == {"type": "hello", "ws_id": "ws-1"}
    assert lq2.get_nowait() == {"type": "hello", "ws_id": "ws-1"}


def test_enqueue_preserves_existing_ws_id() -> None:
    """When payload already carries ws_id, don't overwrite it — this
    supports the coord fan-out path where child events carry their own
    ws_id and parent forwarding mutates in place."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui._enqueue({"type": "child_event", "ws_id": "child-9"})
    assert lq.get_nowait()["ws_id"] == "child-9"


def test_unregister_listener_removes_from_fanout() -> None:
    ui = _make_ui()
    lq = ui._register_listener()
    ui._unregister_listener(lq)
    ui._enqueue({"type": "hello"})
    assert lq.empty()


def test_enqueue_tolerates_full_listener_queue() -> None:
    """A slow SSE consumer shouldn't break the session's fan-out."""
    ui = _make_ui()
    lq = ui._register_listener(maxsize=1)
    lq.put_nowait({"type": "filler"})
    ui._enqueue({"type": "hello"})  # must not raise


# ---------------------------------------------------------------------------
# Approval / plan gates
# ---------------------------------------------------------------------------


def test_resolve_approval_sets_result_and_unblocks_event() -> None:
    ui = _make_ui()
    ui._approval_event.clear()
    ui.resolve_approval(True, "looks good")
    assert ui._approval_result == (True, "looks good")
    assert ui._approval_event.is_set()


def test_resolve_approval_broadcasts_approval_resolved() -> None:
    ui = _make_ui()
    lq = ui._register_listener()
    ui.resolve_approval(False, "nope")
    event = lq.get_nowait()
    assert event["type"] == "approval_resolved"
    assert event["approved"] is False
    assert event["feedback"] == "nope"


def test_resolve_plan_no_pending_signals_but_does_not_broadcast() -> None:
    """cancel_generation calls resolve_plan unconditionally — the
    no-pending path must unblock the event without broadcasting a
    stale plan_resolved."""
    ui = _make_ui()
    ui._pending_plan_review = None
    ui._plan_event.clear()
    lq = ui._register_listener()
    ui.resolve_plan("reject")
    assert ui._plan_result == "reject"
    assert ui._plan_event.is_set()
    assert lq.empty()


def test_resolve_plan_with_pending_broadcasts_plan_resolved() -> None:
    ui = _make_ui()
    ui._pending_plan_review = {"type": "plan_review", "content": "..."}
    ui._plan_event.clear()
    lq = ui._register_listener()
    ui.resolve_plan("accept")
    event = lq.get_nowait()
    assert event == {"type": "plan_resolved", "feedback": "accept", "ws_id": "ws-1"}
    assert ui._pending_plan_review is None
    assert ui._plan_event.is_set()


# ---------------------------------------------------------------------------
# Intent-verdict bookkeeping
# ---------------------------------------------------------------------------


def _mock_storage(storage: Any = None) -> Any:
    storage = storage or MagicMock()
    return storage


def _patch_get_storage(storage: Any):  # type: ignore[no-untyped-def]
    """Patch ``turnstone.core.storage._registry.get_storage`` to return
    the supplied stub so the fire-and-forget persistence paths in
    SessionUIBase are observable under test."""
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def test_on_intent_verdict_caches_for_sse_replay() -> None:
    ui = _make_ui()
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1", "risk_level": "low"})
    assert ui._llm_verdicts["c1"]["verdict_id"] == "v1"


def test_on_intent_verdict_persists_verdict_row() -> None:
    storage = MagicMock()
    ui = _make_ui()
    verdict = {
        "verdict_id": "v1",
        "call_id": "c1",
        "func_name": "bash",
        "risk_level": "medium",
        "confidence": 0.7,
        "recommendation": "review",
        "evidence": ["line-1"],
    }
    with _patch_get_storage(storage):
        ui.on_intent_verdict(verdict)
    storage.create_intent_verdict.assert_called_once()
    kwargs = storage.create_intent_verdict.call_args.kwargs
    assert kwargs["verdict_id"] == "v1"
    assert kwargs["ws_id"] == "ws-1"
    assert kwargs["call_id"] == "c1"


def test_on_intent_verdict_queues_pending_when_decision_unset() -> None:
    ui = _make_ui()
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
    assert ui._pending_verdicts == [{"verdict_id": "v1", "call_id": "c1"}]


def test_on_intent_verdict_stamps_immediately_when_decision_already_set() -> None:
    """Late-arriving verdict (after approval resolved) gets
    user_decision stamped immediately instead of queued."""
    storage = MagicMock()
    ui = _make_ui()
    ui._last_verdict_decision = "approved"
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v-late", "call_id": "c-late"})
    # Not queued — decision was already set.
    assert ui._pending_verdicts == []
    storage.update_intent_verdict.assert_called_once_with("v-late", user_decision="approved")


def test_llm_verdict_cache_evicts_oldest_at_cap() -> None:
    """FIFO eviction at ``_LLM_VERDICT_CACHE_MAX`` prevents unbounded
    growth on a long-running session."""
    ui = _make_ui()
    cap = SessionUIBase._LLM_VERDICT_CACHE_MAX
    with _patch_get_storage(MagicMock()):
        for i in range(cap + 5):
            ui.on_intent_verdict({"verdict_id": f"v{i}", "call_id": f"c{i}"})
    assert len(ui._llm_verdicts) == cap
    # Oldest five should have been evicted.
    assert "c0" not in ui._llm_verdicts
    assert "c4" not in ui._llm_verdicts
    assert f"c{cap + 4}" in ui._llm_verdicts


# ---------------------------------------------------------------------------
# Approval cycle reset — the bug-1 regression
# ---------------------------------------------------------------------------


def test_reset_approval_cycle_clears_decision_and_cache() -> None:
    ui = _make_ui()
    ui._last_verdict_decision = "approved"
    ui._llm_verdicts["c-stale"] = {"verdict_id": "stale"}
    ui._reset_approval_cycle()
    assert ui._last_verdict_decision == ""
    assert ui._llm_verdicts == {}


def test_late_verdict_in_new_round_not_stamped_with_prior_decision() -> None:
    """Regression test for the ultrareview bug-1 finding.

    Round 1: approve → _last_verdict_decision = "approved".
    Round 2 begins: caller calls _reset_approval_cycle().
    A verdict fires mid-round 2: must NOT inherit "approved" from
    round 1. Must land in _pending_verdicts waiting for this round's
    resolution.
    """
    storage = MagicMock()
    ui = _make_ui()
    # Simulate round 1 completion.
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
        ui.resolve_approval(True, None)
    assert ui._last_verdict_decision == "approved"
    # Round 2 begins — subclass approve_tools calls this at entry.
    ui._reset_approval_cycle()
    # Late judge fires during round 2 BEFORE the user decides.
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v2", "call_id": "c2"})
    # The new verdict must be pending (awaiting this round's decision),
    # NOT already stamped with round 1's "approved".
    assert ui._pending_verdicts == [{"verdict_id": "v2", "call_id": "c2"}]
    # update_intent_verdict was only called ONCE: for v1 when round 1
    # resolved. v2 should NOT have been stamped.
    for call in storage.update_intent_verdict.call_args_list:
        assert call.args[0] != "v2", "late verdict was stamped with prior round's decision"


def test_both_subclasses_call_reset_from_approve_tools() -> None:
    """Regression for bug-1: the real subclass ``approve_tools``
    methods must invoke ``_reset_approval_cycle`` at entry. Without
    this, coord sessions that already resolved a prior approval stamp
    the next round's late verdicts with the stale decision.
    """
    import turnstone.server
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI

    webui = turnstone.server.WebUI

    for cls in (webui, ConsoleCoordinatorUI):
        ui = cls(ws_id="ws-x", user_id="u1")
        # Stage state as if a prior approval round already finished.
        ui._last_verdict_decision = "approved"
        ui._llm_verdicts["stale"] = {"verdict_id": "stale"}
        # Entering approve_tools for a new round — the reset must fire.
        # Pass items with needs_approval=False so approve_tools returns
        # without blocking on user input.
        with _patch_get_storage(MagicMock()):
            ui.approve_tools([{"func_name": "ls", "needs_approval": False}])
        assert ui._last_verdict_decision == "", (
            f"{cls.__name__}.approve_tools did not call _reset_approval_cycle "
            "— next round's verdicts would inherit the prior decision"
        )
        assert ui._llm_verdicts == {}, (
            f"{cls.__name__}.approve_tools did not clear the LLM verdict cache"
        )


def test_on_intent_verdict_decision_check_and_queue_are_atomic() -> None:
    """Regression for the on_intent_verdict ↔ resolve_approval race.

    Prior implementation acquired ``_ws_lock`` twice: once to read
    ``_last_verdict_decision``, once to append to
    ``_pending_verdicts``. Between those two acquisitions
    ``resolve_approval`` could swap-and-clear the pending list and
    set the decision — our verdict then got appended to the fresh
    list and stamped with the NEXT round's decision.

    Fix: decision check + append happen under a single lock
    acquisition. This test counts lock acquisitions during one
    ``on_intent_verdict`` and fails if the release-then-reacquire
    pattern returns.
    """
    ui = _make_ui()
    acquire_count = 0
    original_lock = ui._ws_lock

    class _CountingLock:
        def __init__(self, inner: threading.Lock) -> None:
            self._inner = inner

        def __enter__(self) -> None:
            nonlocal acquire_count
            acquire_count += 1
            self._inner.acquire()

        def __exit__(self, *a: Any) -> None:
            self._inner.release()

        def acquire(self, *a: Any, **kw: Any) -> bool:
            return self._inner.acquire(*a, **kw)

        def release(self) -> None:
            self._inner.release()

    ui._ws_lock = _CountingLock(original_lock)  # type: ignore[assignment]
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
    # Two acquisitions: one for the cache write (call_id is truthy),
    # one for decision-check + pending-append. Before the fix there
    # were three, with a window resolve_approval could slip into.
    assert acquire_count == 2, (
        f"on_intent_verdict acquired _ws_lock {acquire_count} times; "
        "decision-check + pending-append must happen under ONE acquisition "
        "to avoid a race with resolve_approval"
    )


def test_resolve_approval_stamps_all_pending_verdicts() -> None:
    """Normal path: multiple verdicts queued during the round, all get
    stamped with the user's decision on resolve."""
    storage = MagicMock()
    ui = _make_ui()
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
        ui.on_intent_verdict({"verdict_id": "v2", "call_id": "c2"})
    assert len(ui._pending_verdicts) == 2
    with _patch_get_storage(storage):
        ui.resolve_approval(False, "too risky")
    # Both verdicts get stamped.
    stamped_ids = {c.args[0] for c in storage.update_intent_verdict.call_args_list}
    assert stamped_ids == {"v1", "v2"}
    # Pending list cleared after resolve.
    assert ui._pending_verdicts == []
    assert ui._last_verdict_decision == "denied"


# ---------------------------------------------------------------------------
# Output guard persistence
# ---------------------------------------------------------------------------


def test_on_output_warning_enqueues_and_persists() -> None:
    storage = MagicMock()
    ui = _make_ui()
    lq = ui._register_listener()
    assessment = {
        "func_name": "bash",
        "flags": ["secret_leak"],
        "risk_level": "high",
        "output_length": 200,
    }
    with _patch_get_storage(storage):
        ui.on_output_warning("call-1", assessment)
    event = lq.get_nowait()
    assert event["type"] == "output_warning"
    assert event["call_id"] == "call-1"
    assert event["risk_level"] == "high"
    storage.record_output_assessment.assert_called_once()


# ---------------------------------------------------------------------------
# Concurrency smoke
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# serialize_pending_approval_detail — dashboard projection
# ---------------------------------------------------------------------------


def test_serialize_pending_approval_detail_returns_none_when_unset() -> None:
    ui = _make_ui()
    assert ui.serialize_pending_approval_detail() is None


def test_serialize_pending_approval_detail_returns_none_when_items_empty() -> None:
    ui = _make_ui()
    ui._pending_approval = {"type": "approve_request", "items": [], "judge_pending": False}
    assert ui.serialize_pending_approval_detail() is None


def test_serialize_pending_approval_detail_merges_judge_verdict() -> None:
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "call_id": "c-1",
                "header": "bash",
                "preview": "$ ls",
                "func_name": "bash",
                "approval_label": "bash",
                "needs_approval": True,
                "error": None,
                "verdict": {"recommendation": "review", "tier": "heuristic"},
            }
        ],
        "judge_pending": True,
    }
    ui._llm_verdicts["c-1"] = {
        "verdict_id": "v-1",
        "call_id": "c-1",
        "risk_level": "high",
        "recommendation": "deny",
        "tier": "llm",
    }
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    assert detail["call_id"] == "c-1"
    assert detail["judge_pending"] is True
    assert len(detail["items"]) == 1
    item = detail["items"][0]
    assert item["call_id"] == "c-1"
    assert item["header"] == "bash"
    assert item["preview"] == "$ ls"
    assert item["heuristic_verdict"] == {"recommendation": "review", "tier": "heuristic"}
    assert item["judge_verdict"]["recommendation"] == "deny"
    assert item["judge_verdict"]["risk_level"] == "high"


def test_serialize_pending_approval_detail_judge_verdict_none_when_missing() -> None:
    """No cached verdict for the call_id → judge_verdict is None,
    not absent or some sentinel."""
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [{"call_id": "c-1", "func_name": "ls", "needs_approval": True}],
        "judge_pending": True,
    }
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    assert detail["items"][0]["judge_verdict"] is None
    assert detail["items"][0]["heuristic_verdict"] is None


def test_serialize_pending_approval_detail_multi_item() -> None:
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {"call_id": "c-1", "func_name": "bash", "needs_approval": True},
            {"call_id": "c-2", "func_name": "mcp__sf__query", "needs_approval": True},
        ],
        "judge_pending": False,
    }
    ui._llm_verdicts["c-2"] = {"recommendation": "deny", "risk_level": "crit"}
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    assert detail["call_id"] == "c-1"  # primary = first item
    assert len(detail["items"]) == 2
    assert detail["items"][0]["judge_verdict"] is None
    assert detail["items"][1]["judge_verdict"]["recommendation"] == "deny"


def test_serialize_pending_approval_detail_tool_policy_denied_passthrough() -> None:
    """A tool-policy-denied item carries error + needs_approval=False
    after WebUI.approve_tools mutates the items list. The serializer
    must round-trip both fields so the JS can detect the
    POLICY-BLOCKED matrix row and render the banner instead of
    approve/deny buttons."""
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "call_id": "c-1",
                "func_name": "rm_rf",
                "approval_label": "rm_rf",
                "needs_approval": False,
                "error": "Blocked by tool policy (pattern match for 'rm_rf')",
            }
        ],
        "judge_pending": False,
    }
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    item = detail["items"][0]
    # Both fields are the JS detection keys for the POLICY-BLOCKED
    # branch in renderApprovalBlock — drift here silently regresses
    # to a buttoned approve UI on a server-blocked call.
    assert item["needs_approval"] is False
    assert item["error"] == "Blocked by tool policy (pattern match for 'rm_rf')"


def test_serialize_pending_approval_detail_judge_unavailable_path() -> None:
    """No judge_verdict + no heuristic_verdict + judge_pending=False
    is the (judge unavailable) matrix row — the JS detects it via
    !verdict && !judgePending && !policyBlocked. Verify the
    serialized payload preserves the absence of all three signals."""
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "call_id": "c-1",
                "func_name": "bash",
                "approval_label": "bash",
                "needs_approval": True,
            }
        ],
        "judge_pending": False,
    }
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    assert detail["judge_pending"] is False
    item = detail["items"][0]
    assert item["judge_verdict"] is None
    assert item["heuristic_verdict"] is None
    assert item["needs_approval"] is True
    assert item["error"] is None


def test_serialize_pending_approval_detail_returned_dict_is_decoupled() -> None:
    """Mutating the returned dict must not corrupt the cached
    verdict, which other consumers may still read."""
    ui = _make_ui()
    ui._pending_approval = {
        "type": "approve_request",
        "items": [{"call_id": "c-1", "func_name": "bash", "needs_approval": True}],
        "judge_pending": False,
    }
    ui._llm_verdicts["c-1"] = {"recommendation": "approve"}
    detail = ui.serialize_pending_approval_detail()
    assert detail is not None
    detail["items"][0]["judge_verdict"]["recommendation"] = "MUTATED"
    assert ui._llm_verdicts["c-1"]["recommendation"] == "approve"


# ---------------------------------------------------------------------------
# Auto-approve visibility — _serialize_approval_items + _record_auto_approves
# + serialize_recent_auto_approvals
# ---------------------------------------------------------------------------


def test_serialize_approval_items_forwards_auto_approve_fields() -> None:
    """When the upstream pipeline tags an item with ``auto_approved`` +
    ``auto_approve_reason``, the serialized payload must carry both
    so the dashboard pill / per-ws SSE consumer can show *which*
    path bypassed the operator gate."""
    ui = _make_ui()
    items = [
        {
            "call_id": "c1",
            "func_name": "bash",
            "approval_label": "bash",
            "needs_approval": False,
            "auto_approved": True,
            "auto_approve_reason": "skill",
        },
        {
            "call_id": "c2",
            "func_name": "read_file",
            "needs_approval": False,
            # No auto_approved tag — read-only tool that never needed approval.
        },
    ]
    out = ui._serialize_approval_items(items)
    assert out[0]["auto_approved"] is True
    assert out[0]["auto_approve_reason"] == "skill"
    # Items not flagged as auto-approved must NOT carry the fields —
    # otherwise the dashboard would show pills for read-only tools too.
    assert "auto_approved" not in out[1]
    assert "auto_approve_reason" not in out[1]


def test_serialize_approval_items_forwards_denial_msg_as_error() -> None:
    """Denied items surface their ``denial_msg`` as ``error`` so the
    /dashboard / SSE consumer renders the policy-block reason
    without exposing the raw item shape."""
    ui = _make_ui()
    items = [
        {
            "call_id": "c1",
            "func_name": "bash",
            "denied": True,
            "denial_msg": "Blocked by tool policy (pattern match for 'bash')",
        }
    ]
    out = ui._serialize_approval_items(items)
    assert out[0]["error"] == "Blocked by tool policy (pattern match for 'bash')"


def test_record_auto_approves_appends_only_tagged_items() -> None:
    """Items without ``auto_approved=True`` are skipped — the ring
    buffer is meant to surface bypassed-the-gate calls, not a
    record of every tool invocation."""
    storage = MagicMock()
    ui = _make_ui()
    items = [
        {
            "call_id": "c1",
            "func_name": "bash",
            "approval_label": "bash",
            "auto_approved": True,
            "auto_approve_reason": "skill",
        },
        {
            "call_id": "c2",
            "func_name": "read_file",
            # No auto_approved tag — read-only tool, gets skipped.
        },
    ]
    with _patch_get_storage(storage):
        ui._record_auto_approves(items)
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "bash"
    assert snapshot[0]["auto_approve_reason"] == "skill"
    # Audit row recorded — one row per call (not per item) so
    # tool-heavy turns don't blow up the audit table.
    storage.record_audit_event.assert_called_once()
    call_kwargs = storage.record_audit_event.call_args.kwargs
    assert call_kwargs["action"] == "tool.auto_approved"


def test_record_auto_approves_caps_buffer_at_max() -> None:
    """Bounded ring buffer — a long-running skill workstream can't
    fill the /dashboard payload with stale rows.  The cap is the
    class-level constant, exercised here to lock the contract."""
    ui = _make_ui()
    cap = ui._RECENT_AUTO_APPROVALS_MAX
    # Push (cap + 5) items; only the most recent ``cap`` survive.
    for i in range(cap + 5):
        with _patch_get_storage(MagicMock()):
            ui._record_auto_approves(
                [
                    {
                        "call_id": f"c{i}",
                        "func_name": f"tool_{i}",
                        "auto_approved": True,
                        "auto_approve_reason": "blanket",
                    }
                ]
            )
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == cap
    # Tail preserved — oldest entries roll off the head.
    assert snapshot[-1]["func_name"] == f"tool_{cap + 5 - 1}"
    assert snapshot[0]["func_name"] == f"tool_{5}"


def test_record_auto_approves_noop_when_no_tagged_items() -> None:
    """No tagged items → no buffer write, no audit — matters for
    the every-tool-call-was-read-only case where ``items`` is
    non-empty but nothing was an auto-approve."""
    storage = MagicMock()
    ui = _make_ui()
    with _patch_get_storage(storage):
        ui._record_auto_approves(
            [{"call_id": "c1", "func_name": "read_file"}]  # no auto_approved tag
        )
    assert ui.serialize_recent_auto_approvals() == []
    storage.record_audit_event.assert_not_called()


def test_record_auto_approves_swallows_audit_failure() -> None:
    """An audit-write exception must not break the tool-execution
    path — visibility is best-effort, the SSE event + ring buffer
    already shipped to operators by the time this fires."""
    storage = MagicMock()
    storage.record_audit_event.side_effect = RuntimeError("audit table down")
    ui = _make_ui()
    items = [
        {
            "call_id": "c1",
            "func_name": "bash",
            "auto_approved": True,
            "auto_approve_reason": "policy",
        }
    ]
    # Must not raise — the docstring explicitly promises best-effort.
    with _patch_get_storage(storage):
        ui._record_auto_approves(items)
    # Buffer write still happened (it's first, before the audit).
    assert len(ui.serialize_recent_auto_approvals()) == 1


def test_replay_recent_auto_approvals_from_audit_seeds_buffer() -> None:
    """Audit-replay seeds the ring buffer on UI construction so the
    dashboard pill survives UI rebuilds (saved-workstream rehydrate /
    coord→node click-through / process restart all create a fresh UI
    whose buffer would otherwise be empty even though the audit row
    is still on disk)."""
    storage = MagicMock()
    storage.list_audit_events.return_value = [
        # DESC order — newest first.
        {
            "timestamp": "2026-04-27T18:00:00",
            "detail": (
                '{"tools": [{"call_id": "c2", "func_name": "edit_file",'
                ' "approval_label": "edit_file", "reason": "policy"}],'
                ' "count": 1}'
            ),
        },
        {
            "timestamp": "2026-04-27T17:00:00",
            "detail": (
                '{"tools": [{"call_id": "c1", "func_name": "bash",'
                ' "approval_label": "bash", "reason": "skill"}],'
                ' "count": 1}'
            ),
        },
    ]
    with _patch_get_storage(storage):
        ui = _make_ui(ws_id="ws-replay")
    # Buffer holds the replayed entries in chronological order
    # (oldest first), matching what live appends produce.
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 2
    assert snapshot[0]["func_name"] == "bash"
    assert snapshot[0]["auto_approve_reason"] == "skill"
    assert snapshot[1]["func_name"] == "edit_file"
    assert snapshot[1]["auto_approve_reason"] == "policy"
    # And the audit query was scoped to this ws + tool.auto_approved.
    storage.list_audit_events.assert_called_once()
    call_kwargs = storage.list_audit_events.call_args.kwargs
    assert call_kwargs["action"] == "tool.auto_approved"
    assert call_kwargs["resource_id"] == "ws-replay"


def test_replay_swallows_audit_storage_failure() -> None:
    """A storage outage at construction time must not break UI
    instantiation — the buffer simply stays empty until the next
    live auto-approve populates it."""
    storage = MagicMock()
    storage.list_audit_events.side_effect = RuntimeError("audit table down")
    with _patch_get_storage(storage):
        ui = _make_ui(ws_id="ws-replay")
    assert ui.serialize_recent_auto_approvals() == []


def test_replay_skips_when_ws_id_missing() -> None:
    """No ws_id → no audit query.  Test fixtures sometimes
    construct a UI with the default empty ws_id; the replay must
    not fire a wildcard query that returns rows from other ws's."""
    storage = MagicMock()
    with _patch_get_storage(storage):
        ui = _make_ui(ws_id="")
    storage.list_audit_events.assert_not_called()
    assert ui.serialize_recent_auto_approvals() == []


def test_replay_tolerates_malformed_audit_detail() -> None:
    """Unparseable / wrong-shape audit detail rows are skipped, not
    propagated.  A historic audit row with a different schema (e.g.
    pre-fix migration leftover) must not crash UI construction."""
    storage = MagicMock()
    storage.list_audit_events.return_value = [
        {"timestamp": "2026-04-27T18:00:00", "detail": "not-json"},
        {"timestamp": "2026-04-27T17:30:00", "detail": '{"tools": "wrong-shape"}'},
        {
            "timestamp": "2026-04-27T17:00:00",
            "detail": '{"tools": [{"func_name": "bash", "reason": "skill"}], "count": 1}',
        },
    ]
    with _patch_get_storage(storage):
        ui = _make_ui(ws_id="ws-replay")
    # Only the well-shaped row contributes.
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "bash"


def test_parse_audit_timestamp_treats_naive_strings_as_utc() -> None:
    """Audit rows are stored as naive UTC strings (e.g.
    ``2026-04-27T18:00:00`` with no timezone marker); a server in
    a non-UTC timezone would mis-stamp pill entries by hours
    without explicit UTC.replace at parse time."""
    from datetime import UTC, datetime

    from turnstone.core.session_ui_base import SessionUIBase

    expected = datetime(2026, 4, 27, 18, 0, 0, tzinfo=UTC).timestamp()
    assert SessionUIBase._parse_audit_timestamp("2026-04-27T18:00:00") == expected
    # Explicit-offset strings parse correctly too — the UTC stamp
    # only applies when tzinfo is None.
    assert SessionUIBase._parse_audit_timestamp("2026-04-27T18:00:00+00:00") == expected


def test_replay_caps_at_buffer_max() -> None:
    """Replay output is bounded by the same cap as live appends.
    A long-lived workstream with hundreds of audit rows must not
    blow past the 10-entry limit during replay."""
    storage = MagicMock()
    # Generate many fake rows.
    storage.list_audit_events.return_value = [
        {
            "timestamp": f"2026-04-27T{i:02d}:00:00",
            "detail": (
                f'{{"tools": [{{"func_name": "tool_{i}", "reason": "skill"}}], "count": 1}}'
            ),
        }
        for i in range(20)
    ]
    with _patch_get_storage(storage):
        ui = _make_ui(ws_id="ws-replay")
    snapshot = ui.serialize_recent_auto_approvals()
    # Cap holds even when audit-replay fans in past it.
    assert len(snapshot) == ui._RECENT_AUTO_APPROVALS_MAX


def test_serialize_recent_auto_approvals_returns_a_copy() -> None:
    """Mutating the returned list must not corrupt the buffer —
    HTTP handler should not be able to drain or reorder it."""
    ui = _make_ui()
    with _patch_get_storage(MagicMock()):
        ui._record_auto_approves(
            [
                {
                    "call_id": "c1",
                    "func_name": "bash",
                    "auto_approved": True,
                    "auto_approve_reason": "skill",
                }
            ]
        )
    snapshot = ui.serialize_recent_auto_approvals()
    snapshot.clear()
    snapshot.append({"poisoned": True})
    # Buffer state survives the caller's mutation.
    fresh = ui.serialize_recent_auto_approvals()
    assert len(fresh) == 1
    assert fresh[0]["func_name"] == "bash"


# ---------------------------------------------------------------------------


def test_concurrent_enqueue_and_listener_registration() -> None:
    """Fan-out under concurrent enqueue + register/unregister shouldn't
    drop events or crash on the lock. Sanity-level stress."""
    ui = _make_ui()

    def _producer() -> None:
        for i in range(100):
            ui._enqueue({"type": "tick", "n": i})

    def _subscriber() -> None:
        for _ in range(20):
            lq = ui._register_listener()
            ui._unregister_listener(lq)

    producer = threading.Thread(target=_producer)
    subscribers = [threading.Thread(target=_subscriber) for _ in range(4)]
    producer.start()
    for s in subscribers:
        s.start()
    producer.join()
    for s in subscribers:
        s.join()
    # Test's job is to surface any RuntimeError / lock inversion
    # during concurrent enqueue + register/unregister. If we got
    # here every thread completed cleanly — assert explicitly so the
    # intent survives optimization-mode assertion stripping.
    assert not producer.is_alive()
    assert all(not s.is_alive() for s in subscribers)


# ---------------------------------------------------------------------------
# Per-turn inflight buffers — SSE refresh-resume snapshot path
# ---------------------------------------------------------------------------


def test_on_content_token_writes_to_both_buffers() -> None:
    """``on_content_token`` writes to the multi-turn buffer (IDLE
    piggyback) AND the per-turn inflight buffer (SSE snapshot)."""
    ui = _make_ui()
    ui.on_content_token("hello")
    assert ui._ws_turn_content == ["hello"]
    assert ui._ws_inflight_content == ["hello"]
    assert ui._ws_inflight_seq == 1


def test_on_reasoning_token_writes_to_inflight_buffer_only() -> None:
    """Reasoning has no multi-turn IDLE piggyback — only the inflight
    buffer + the seq counter."""
    ui = _make_ui()
    ui.on_reasoning_token("thinking...")
    assert ui._ws_inflight_reasoning == ["thinking..."]
    assert ui._ws_inflight_seq == 1
    # Multi-turn buffer is content-only and untouched by reasoning.
    assert ui._ws_turn_content == []


def test_inflight_seq_advances_on_every_emit_even_at_cap() -> None:
    """Cap-hit content tokens MUST advance ``_ws_inflight_seq``,
    even though the buffer rejected the append. If seq stalled at
    high-water-pre-cap, a subscriber registering AFTER the cap is
    hit would capture ``snap_seq == stalled_seq`` and every
    subsequent live token (also tagged with the stalled seq) would
    be filter-dropped by the events handler — silently losing the
    rest of the stream. The cap is a buffer-size limit, not a
    "stop streaming" signal."""
    from turnstone.core.session_ui_base import _MAX_TURN_CONTENT_CHARS

    ui = _make_ui()
    chunk = "x" * 1024
    while ui._ws_inflight_content_size < _MAX_TURN_CONTENT_CHARS:
        ui.on_content_token(chunk)
    seq_at_cap = ui._ws_inflight_seq

    # Cap-hit token: seq MUST advance (no buffer append, but the
    # event still gets a fresh seq for the dedup filter).
    ui.on_content_token(chunk)
    assert ui._ws_inflight_seq == seq_at_cap + 1
    # Buffer remains bounded — the cap-hit token is NOT in inflight.
    assert ui._ws_inflight_content_size <= _MAX_TURN_CONTENT_CHARS + len(chunk)


def test_subscriber_after_cap_hit_receives_subsequent_tokens() -> None:
    """Regression for Copilot's cap+seq finding: a subscriber that
    connects AFTER the inflight buffer is at cap must still receive
    live tokens past the cap. Past-cap tokens are absent from
    ``snap.content`` (the snapshot text was truncated at cap) but
    the live stream past them must NOT be filter-dropped."""
    from turnstone.core.session_ui_base import _MAX_TURN_CONTENT_CHARS

    ui = _make_ui()
    chunk = "x" * 1024
    while ui._ws_inflight_content_size < _MAX_TURN_CONTENT_CHARS:
        ui.on_content_token(chunk)
    # Stream a few tokens PAST the cap before subscribing.
    for _ in range(3):
        ui.on_content_token(chunk)

    lq, snap = ui.register_listener_with_in_progress_snapshot()
    snap_seq = snap["seq"]

    # Live token past cap.
    ui.on_content_token(chunk)
    ev = lq.get_nowait()
    assert ev["type"] == "content"
    # The critical invariant: seq advances per-emit, so the new
    # event's _seq is strictly greater than the snap_seq the
    # subscriber captured. Without this, the events handler's
    # ``seq <= snap_seq`` filter would drop every token past the
    # cap (silent token loss for refresh-past-cap).
    assert ev["_seq"] > snap_seq, (
        f"Token past cap has _seq={ev['_seq']} which is <= "
        f"snap_seq={snap_seq} — would be silently dropped after a "
        f"refresh past the cap."
    )


def test_subscriber_after_reasoning_cap_hit_receives_subsequent_tokens() -> None:
    """Same invariant as content cap: reasoning subscribers past
    cap must keep receiving live reasoning tokens."""
    from turnstone.core.session_ui_base import _MAX_TURN_CONTENT_CHARS

    ui = _make_ui()
    chunk = "x" * 1024
    while ui._ws_inflight_reasoning_size < _MAX_TURN_CONTENT_CHARS:
        ui.on_reasoning_token(chunk)
    for _ in range(3):
        ui.on_reasoning_token(chunk)

    lq, snap = ui.register_listener_with_in_progress_snapshot()
    snap_seq = snap["seq"]

    ui.on_reasoning_token(chunk)
    ev = lq.get_nowait()
    assert ev["type"] == "reasoning"
    assert ev["_seq"] > snap_seq


def test_on_turn_committed_resets_inflight_after_commit() -> None:
    """``on_turn_committed`` fires immediately after each
    ``messages.append(assistant_msg)`` in the send loop. Without it,
    the inflight buffer keeps the just-committed turn's content
    during the post-commit tool-execution window — and a refresh in
    that window would show the assistant turn TWICE (history list
    + in_progress_snapshot)."""
    ui = _make_ui()
    ui.on_content_token("Just-finished turn ")
    ui.on_reasoning_token("Reasoning for the turn ")
    # Sanity: buffer is populated pre-commit.
    assert ui._ws_inflight_content == ["Just-finished turn "]
    assert ui._ws_inflight_reasoning == ["Reasoning for the turn "]

    ui.on_turn_committed()

    # Inflight content + reasoning reset; seq stays monotonic.
    assert ui._ws_inflight_content == []
    assert ui._ws_inflight_reasoning == []
    # Multi-turn buffer is NOT reset by commit (it drains at idle).
    assert ui._ws_turn_content == ["Just-finished turn "]


def test_inflight_snapshot_empty_during_post_commit_tool_window() -> None:
    """Models the user-reported bug: refresh during a tool-execution
    window between commit and the next stream. Pre-fix: snapshot has
    the just-committed turn's text → double-renders against history.
    Post-fix: snapshot is empty → no double-render. Seq stays
    monotonic (carries the high-water mark across turn boundaries)."""
    ui = _make_ui()
    ui.on_content_token("Calling tool with these args: ")
    seq_pre_commit = ui._ws_inflight_seq
    ui.on_turn_committed()  # session.py fires this after messages.append
    # We're now in the tool-execution window. A reconnecting client
    # would call register_listener_with_in_progress_snapshot.
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == ""
    assert snap["reasoning"] == ""
    # Seq did NOT reset — must remain monotonic across turns.
    assert snap["seq"] == seq_pre_commit


def test_on_turn_start_resets_inflight_content_and_reasoning() -> None:
    """``on_turn_start`` clears the per-turn content + reasoning
    buffers but does NOT touch the multi-turn ``_ws_turn_content``
    (which the dashboard's IDLE-piggyback payload depends on) and
    does NOT reset the seq counter (must remain monotonic across
    turn boundaries — see ``test_inflight_seq_monotonic_across_turn_boundaries``)."""
    ui = _make_ui()
    ui.on_content_token("turn-1 ")
    ui.on_reasoning_token("reasoning-1 ")
    multi_pre = list(ui._ws_turn_content)
    multi_pre_size = ui._ws_turn_content_size

    ui.on_turn_start()

    assert ui._ws_inflight_content == []
    assert ui._ws_inflight_content_size == 0
    assert ui._ws_inflight_reasoning == []
    assert ui._ws_inflight_reasoning_size == 0
    # Multi-turn untouched.
    assert ui._ws_turn_content == multi_pre
    assert ui._ws_turn_content_size == multi_pre_size


def test_register_listener_with_in_progress_snapshot_empty() -> None:
    ui = _make_ui()
    lq, snap = ui.register_listener_with_in_progress_snapshot()
    assert isinstance(lq, queue.Queue)
    assert lq in ui._listeners
    assert snap == {"content": "", "reasoning": "", "seq": 0}


def test_register_listener_with_in_progress_snapshot_populated() -> None:
    ui = _make_ui()
    ui.on_content_token("Hello, ")
    ui.on_content_token("world!")
    ui.on_reasoning_token("planning a greeting")
    lq, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == "Hello, world!"
    assert snap["reasoning"] == "planning a greeting"
    # seq counts every successful append across BOTH buffers.
    assert snap["seq"] == 3
    # Listener is registered — later live tokens land in lq.
    ui.on_content_token(" Goodbye.")
    ev = lq.get_nowait()
    assert ev["type"] == "content"
    assert ev["text"] == " Goodbye."
    assert ev["_seq"] == 4


def test_register_listener_with_in_progress_snapshot_only_inflight_not_multi_turn() -> None:
    """The snapshot reflects the in-progress turn only — anything
    cleared by ``on_turn_start`` (a prior committed turn within the
    same send) must NOT appear in the snapshot, even though the
    multi-turn buffer still has it."""
    ui = _make_ui()
    ui.on_content_token("PRIOR_TURN ")
    ui.on_turn_start()  # commit boundary — inflight reset
    ui.on_content_token("CURRENT")
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == "CURRENT"
    # Multi-turn buffer still has both turns (drives the IDLE piggyback).
    assert "".join(ui._ws_turn_content) == "PRIOR_TURN CURRENT"


def test_seq_filter_dedup_round_trip() -> None:
    """End-to-end dedup invariant: every token appears exactly once
    when reconstructing from snapshot + listener queue under live
    writes that race the registration. Models the events handler."""
    ui = _make_ui()
    for ch in "abcde":
        ui.on_content_token(ch)
    lq, snap = ui.register_listener_with_in_progress_snapshot()
    for ch in "fgh":
        ui.on_content_token(ch)

    reconstructed = snap["content"]
    while True:
        try:
            ev = lq.get_nowait()
        except queue.Empty:
            break
        if ev.get("_seq", 0) <= snap["seq"]:
            continue
        reconstructed += ev["text"]
    assert reconstructed == "abcdefgh"


def test_seq_filter_drops_overlap_when_register_lands_after_writer() -> None:
    """Race: writer appends + emits while a second register snapshots
    after the writer. The live event has _seq <= snap.seq → must be
    dropped to avoid double-render."""
    ui = _make_ui()
    # Register a first listener so the writer's enqueue lands somewhere.
    lq1, _ = ui.register_listener_with_in_progress_snapshot()
    ui.on_content_token("X")
    # Second register snapshots AFTER the write — snap has "X" AND
    # the writer's enqueue is in lq1.
    _, snap2 = ui.register_listener_with_in_progress_snapshot()
    assert snap2["content"] == "X"
    # Drain lq1 with the filter against snap2.seq — duplicate dropped.
    duped: list[str] = []
    while True:
        try:
            ev = lq1.get_nowait()
        except queue.Empty:
            break
        if ev.get("_seq", 0) <= snap2["seq"]:
            continue
        duped.append(ev["text"])
    assert duped == []


def test_inflight_seq_monotonic_across_turn_boundaries() -> None:
    """Regression: a subscriber registered mid-turn-N must still
    receive turn N+1's tokens. The seq counter is monotonic across
    turn boundaries — resetting it at on_turn_committed/on_turn_start
    would silently drop turn N+1's first M tokens (M = the snap_seq
    captured mid-turn-N) via the events handler's `seq <= snap_seq`
    filter."""
    ui = _make_ui()
    # Turn N: stream tokens, register a listener mid-turn.
    ui.on_content_token("turn-N tok1 ")
    ui.on_content_token("turn-N tok2 ")
    lq, snap = ui.register_listener_with_in_progress_snapshot()
    snap_seq = snap["seq"]
    assert snap_seq == 2
    # Turn N completes, turn N+1 begins.
    ui.on_turn_committed()
    ui.on_turn_start()
    # Turn N+1's first content token. With the q-1 fix, seq is
    # monotonic (3), not reset to 1. The events handler's
    # `seq <= snap_seq` filter must NOT swallow it.
    ui.on_content_token("turn-N+1 tok1 ")
    ev = lq.get_nowait()
    assert ev["type"] == "content"
    assert ev["text"] == "turn-N+1 tok1 "
    assert ev["_seq"] > snap_seq, (
        f"Token from turn N+1 has _seq={ev['_seq']} which is <= "
        f"snap_seq={snap_seq} — the events handler's dedup filter "
        f"would silently drop it on a long-lived SSE subscription."
    )


def test_snapshot_and_consume_drains_inflight_at_idle() -> None:
    """Regression for the cancel/error path: ``on_turn_committed`` is
    NOT called from cancel handlers, but every exit path eventually
    fires ``_emit_state("idle")`` (cancel) or ``_emit_state("error")``
    (exception). The IDLE/ERROR branches of
    ``snapshot_and_consume_state_payload`` must drain the inflight
    buffers so a refresh post-cancel doesn't double-render the
    cancelled fragment against history's marker'd version."""
    ui = _make_ui()
    ui.on_content_token("partial cancelled text ")
    ui.on_reasoning_token("partial reasoning ")
    assert ui._ws_inflight_content_size > 0
    assert ui._ws_inflight_reasoning_size > 0

    ui.snapshot_and_consume_state_payload("idle")

    assert ui._ws_inflight_content == []
    assert ui._ws_inflight_content_size == 0
    assert ui._ws_inflight_reasoning == []
    assert ui._ws_inflight_reasoning_size == 0


def test_snapshot_and_consume_drains_inflight_at_error() -> None:
    """Regression for the exception path: ERROR-branch must drain
    inflight too (parallel to the IDLE branch)."""
    ui = _make_ui()
    ui.on_content_token("partial errored text ")
    ui.on_reasoning_token("partial errored reasoning ")

    ui.snapshot_and_consume_state_payload("error")

    assert ui._ws_inflight_content == []
    assert ui._ws_inflight_reasoning == []


def test_snapshot_and_consume_does_not_reset_seq_at_idle_or_error() -> None:
    """The IDLE/ERROR drain clears content + reasoning but must NOT
    reset the seq counter — long-lived subscribers' snap_seq must
    stay valid across turn boundaries (see the q-1 invariant test)."""
    ui = _make_ui()
    ui.on_content_token("a")
    ui.on_content_token("b")
    assert ui._ws_inflight_seq == 2

    ui.snapshot_and_consume_state_payload("idle")
    assert ui._ws_inflight_seq == 2

    ui.snapshot_and_consume_state_payload("error")
    assert ui._ws_inflight_seq == 2


def test_listeners_share_dict_reference_warning() -> None:
    """Pinning the shape that necessitated the events-handler shallow
    copy: ``_enqueue`` puts ONE dict reference into every listener
    queue. If multiple SSE coroutines mutate (e.g. ``del event[\"_seq\"]``)
    without copying first, they corrupt each other's view. The fix
    in make_events_handler is ``event = dict(event)`` immediately
    after ``client_queue.get`` — verify the underlying invariant
    here so a future refactor of ``_enqueue`` can't silently break
    the assumption the events handler relies on."""
    ui = _make_ui()
    lq1, _ = ui.register_listener_with_in_progress_snapshot()
    lq2, _ = ui.register_listener_with_in_progress_snapshot()
    ui.on_content_token("X")
    ev1 = lq1.get_nowait()
    ev2 = lq2.get_nowait()
    # Same reference today — consumers MUST shallow-copy before any
    # mutation. If a future _enqueue change makes this no longer
    # true, the events handler's defensive copy becomes redundant
    # but harmless; if this assertion suddenly fails the underlying
    # invariant has shifted and the handler comment should be updated.
    assert ev1 is ev2


def test_concurrent_writer_and_register_with_snapshot_no_loss_no_dup() -> None:
    """Stress: many tokens streaming + a register_with_snapshot landing
    at a random point. End state: snapshot ∪ filtered_live == every
    token written, exactly once."""
    ui = _make_ui()
    n_tokens = 500
    snap_box: dict[str, Any] = {}
    lq_box: dict[str, queue.Queue[Any]] = {}

    def _writer() -> None:
        for i in range(n_tokens):
            ui.on_content_token(f"{i},")

    def _registrar() -> None:
        # Tiny sleep so the writer is mid-flight.
        threading.Event().wait(0.001)
        lq, snap = ui.register_listener_with_in_progress_snapshot()
        snap_box["snap"] = snap
        lq_box["lq"] = lq

    w = threading.Thread(target=_writer)
    r = threading.Thread(target=_registrar)
    w.start()
    r.start()
    w.join()
    r.join()

    snap = snap_box["snap"]
    lq = lq_box["lq"]
    reconstructed = snap["content"]
    while True:
        try:
            ev = lq.get_nowait()
        except queue.Empty:
            break
        if ev.get("_seq", 0) <= snap["seq"]:
            continue
        reconstructed += ev["text"]
    expected = "".join(f"{i}," for i in range(n_tokens))
    assert reconstructed == expected, (
        f"reconstruction mismatch: len(rec)={len(reconstructed)}, len(exp)={len(expected)}"
    )
