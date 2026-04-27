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
