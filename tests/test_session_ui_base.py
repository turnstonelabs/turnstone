"""Tests for ``SessionUIBase`` — the shared UI scaffolding.

Covers listener fan-out, approval blocking gates, intent-judge
verdict bookkeeping, and the approval-cycle reset invariant that
prevents a late verdict from inheriting the previous round's
``user_decision``.

These are unit tests exercising the base class directly via a thin
concrete subclass — subclass-specific behaviour (WebUI's per-UI
metrics broadcast, ConsoleCoordinatorUI's collector fan-out) lives
in its own test files.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import resolve_when_pending
from turnstone.core.session_ui_base import SessionUIBase


class _ConcreteUI(SessionUIBase):
    """Minimal concrete subclass — no kind-specific overrides.

    Exists only so we can instantiate the base (it's designed to be
    subclassed). Inherits the full base behaviour verbatim.
    """


def _make_ui(ws_id: str = "ws-1", user_id: str = "u1") -> _ConcreteUI:
    return _ConcreteUI(ws_id=ws_id, user_id=user_id)


@pytest.fixture(autouse=True)
def _per_token_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force per-token flushes (batch window 0) for this whole file.

    These tests pin per-emit invariants — seq advance, mid-stream
    inflight buffer state, snapshot atomicity — that predate emit-time
    token batching and remain the contract AT each flush boundary;
    window 0 makes every token its own flush, which is exactly the
    emit shape they were written against.  Batching cadence itself
    (window/size coalescing, pending-batch visibility, flush-before-
    non-token ordering) is pinned in ``test_sse_token_batching.py``.
    """
    import turnstone.core.session_ui_base as suib

    monkeypatch.setattr(suib, "_TOKEN_BATCH_WINDOW_SECS", 0.0)


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
    # ``_enqueue`` stamps ``_event_id`` on every event so the ring
    # buffer can key replay against ``Last-Event-ID``; non-token
    # events (``hello`` isn't ``content`` / ``reasoning``) skip
    # ``_seq``.  Both listeners observe the SAME dict reference
    # (covered by ``test_listeners_share_dict_reference_warning``).
    assert lq1.get_nowait() == {"type": "hello", "ws_id": "ws-1", "_event_id": 1}
    assert lq2.get_nowait() == {"type": "hello", "ws_id": "ws-1", "_event_id": 1}


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
# Approval gates
# ---------------------------------------------------------------------------


def _register_cycle(
    ui: SessionUIBase,
    call_ids: list[str],
    *,
    judge_event: object | None = None,
) -> Any:
    """Register a live ApprovalCycle the way ``approve_tools`` does.

    Builds the card from wire-shaped items so serializer tests see the
    exact production shape, registers under the lock, and returns the
    cycle for direct assertions.
    """
    from turnstone.core.session_ui_base import ApprovalCycle

    items = [
        {"call_id": cid, "func_name": "bash", "approval_label": "bash", "needs_approval": True}
        for cid in call_ids
    ]
    card: dict[str, Any] = {
        "type": "approve_request",
        "cycle_id": f"cycle-{'-'.join(call_ids)}",
        "items": ui._serialize_approval_items(items),
        "judge_pending": False,
    }
    cycle = ApprovalCycle(items, card, judge_event)
    ui._register_approval_cycle(cycle)
    return cycle


def test_resolve_approval_sets_result_and_unblocks_cycle_event() -> None:
    ui = _make_ui()
    cycle = _register_cycle(ui, ["c1"])
    resolved = ui.resolve_approval(True, "looks good")
    assert resolved == cycle.cycle_id
    assert cycle.result == (True, "looks good")
    assert cycle.resolved is True
    assert cycle.event.is_set()


def test_resolve_approval_without_live_cycle_is_a_noop() -> None:
    """No live cycle → nothing to resolve: returns None and broadcasts
    nothing (the old singleton overwrote a shared result slot and
    leaked a stale ``approval_resolved`` event on idle cancels)."""
    ui = _make_ui()
    lq = ui._register_listener()
    assert ui.resolve_approval(True, "nobody asked") is None
    assert lq.empty()


def test_resolve_approval_broadcasts_approval_resolved() -> None:
    ui = _make_ui()
    cycle = _register_cycle(ui, ["c1"])
    lq = ui._register_listener()
    ui.resolve_approval(False, "nope")
    event = lq.get_nowait()
    assert event["type"] == "approval_resolved"
    assert event["approved"] is False
    assert event["feedback"] == "nope"
    # Cycle identity rides the event so clients dismiss the RIGHT card.
    assert event["cycle_id"] == cycle.cycle_id
    assert event["call_ids"] == ["c1"]


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
    storage.upsert_intent_verdict.assert_called_once()
    kwargs = storage.upsert_intent_verdict.call_args.kwargs
    assert kwargs["verdict_id"] == "v1"
    assert kwargs["ws_id"] == "ws-1"
    assert kwargs["call_id"] == "c1"


def test_on_intent_verdict_parks_on_owning_cycle_when_undecided() -> None:
    ui = _make_ui()
    cycle = _register_cycle(ui, ["c1"])
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
    assert cycle.pending_verdicts == [{"verdict_id": "v1", "call_id": "c1"}]


def test_on_intent_verdict_without_owner_is_cache_only() -> None:
    """No live cycle owns the call (pre-cycle Smart-Approvals arrival):
    the verdict caches for the wait/replay but parks nowhere — the
    gate's registration sweep adopts it when the cycle is created."""
    ui = _make_ui()
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
    assert ui._llm_verdicts["c1"]["verdict_id"] == "v1"
    assert ui._approval_cycles == {}


def test_on_intent_verdict_stamps_immediately_when_decision_already_set() -> None:
    """Late-arriving verdict (after its round resolved) gets
    user_decision stamped from the per-call decision map instead of
    parked — the run-to-completion daemon can deliver seconds after
    the gate closed."""
    storage = MagicMock()
    ui = _make_ui()
    ui._recent_decisions["c-late"] = ("approved", None)
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v-late", "call_id": "c-late"})
    storage.update_intent_verdict.assert_called_once_with("v-late", user_decision="approved")


def test_on_intent_verdict_late_stamp_is_per_call_not_global() -> None:
    """Concurrent-cycles regression: sibling B's late verdict must NOT
    inherit sibling A's decision — the old single ``_last_verdict_decision``
    string stamped every late verdict with whichever round resolved last."""
    storage = MagicMock()
    ui = _make_ui()
    _register_cycle(ui, ["a1"])
    _register_cycle(ui, ["b1"])
    with _patch_get_storage(storage):
        ui.resolve_approval(True, None, call_id="a1")  # approve A only
        ui.on_intent_verdict({"verdict_id": "v-b", "call_id": "b1"})
    # B's verdict is parked on B's still-open cycle, unstamped.
    for call in storage.update_intent_verdict.call_args_list:
        assert call.args[0] != "v-b", "sibling A's decision leaked onto B's verdict"


def test_on_superseded_intent_verdict_persists_without_live_surfaces() -> None:
    """The persist-only audit hook for verdicts that landed after a newer
    turn replaced their judge generation: the row reaches storage with
    user_decision="superseded", but NONE of the live surfaces move — no
    SSE event, no ``_llm_verdicts`` cache entry (Smart Approvals must
    never see a stale call_id), no ``_pending_verdicts`` park (the next
    ``resolve_approval`` must not stamp it with the wrong decision)."""
    storage = MagicMock()
    ui = _make_ui()
    lq = ui._register_listener()
    verdict = {
        "verdict_id": "v-late",
        "call_id": "c-late",
        "func_name": "bash",
        "risk_level": "low",
        "tier": "llm",
    }
    with _patch_get_storage(storage):
        ui.on_superseded_intent_verdict(verdict)
    storage.upsert_intent_verdict.assert_called_once()
    kwargs = storage.upsert_intent_verdict.call_args.kwargs
    assert kwargs["verdict_id"] == "v-late"
    assert kwargs["user_decision"] == "superseded"
    assert lq.empty()  # no SSE delivery
    assert "c-late" not in ui._llm_verdicts  # no replay-cache write
    assert "user_decision" not in verdict  # caller's dict not mutated


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
# Per-round verdict purge — the bug-1 regression, scoped for concurrency
# ---------------------------------------------------------------------------


def test_purge_round_verdicts_is_scoped_to_the_entering_batch() -> None:
    """Successor of the whole-cache reset: entering a gate evicts stale
    verdict state for ITS call_ids only — a concurrent sibling cycle's
    cached verdicts must survive (the old full clear wiped them
    mid-wait and sent qualifying batches to a human)."""
    ui = _make_ui()
    ui._recent_decisions["c-stale"] = ("approved", None)
    ui._llm_verdicts["c-stale"] = {"verdict_id": "stale"}
    ui._verdict_origins["c-stale"] = 123
    ui._llm_verdicts["c-sibling"] = {"verdict_id": "sibling"}
    ui._purge_round_verdicts({"c-stale"})
    assert "c-stale" not in ui._llm_verdicts
    assert "c-stale" not in ui._verdict_origins
    assert "c-stale" not in ui._recent_decisions
    # The concurrent sibling's verdict is untouched.
    assert ui._llm_verdicts["c-sibling"]["verdict_id"] == "sibling"


def test_late_verdict_in_new_round_not_stamped_with_prior_decision() -> None:
    """Regression test for the ultrareview bug-1 finding, cycle-scoped.

    Round 1 (call c1): approve → decision recorded for c1 only.
    Round 2 (call c2, new cycle) begins.
    A verdict fires mid-round 2: must NOT inherit "approved" from
    round 1. Must park on round 2's cycle awaiting ITS resolution.
    """
    storage = MagicMock()
    ui = _make_ui()
    # Round 1 completion.
    _register_cycle(ui, ["c1"])
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
        ui.resolve_approval(True, None)
    assert ui._recent_decisions.get("c1") == ("approved", None)
    # Round 2 begins — approve_tools purges the entering ids and
    # registers a fresh cycle.
    ui._purge_round_verdicts({"c2"})
    cycle2 = _register_cycle(ui, ["c2"])
    # Late judge fires during round 2 BEFORE the user decides.
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v2", "call_id": "c2"})
    # The new verdict parks on round 2's cycle (awaiting its decision),
    # NOT already stamped with round 1's "approved".
    assert cycle2.pending_verdicts == [{"verdict_id": "v2", "call_id": "c2"}]
    for call in storage.update_intent_verdict.call_args_list:
        assert call.args[0] != "v2", "late verdict was stamped with prior round's decision"


def test_both_subclasses_purge_round_state_from_approve_tools() -> None:
    """Regression for bug-1, scoped: the real subclass ``approve_tools``
    bodies must purge the ENTERING batch's stale verdict state at entry
    — a provider that reuses call_ids across turns must not have round
    1's cached ``approve`` pre-satisfy round 2's Smart-Approvals wait.
    A concurrent sibling's cache entry survives.
    """
    import turnstone.server
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI

    webui = turnstone.server.WebUI

    for cls in (webui, ConsoleCoordinatorUI):
        ui = cls(ws_id="ws-x", user_id="u1")
        # Stage state as if a prior round already finished on the SAME
        # call_id this round reuses, plus an unrelated sibling entry.
        ui._recent_decisions["c-reused"] = ("approved", None)
        ui._llm_verdicts["c-reused"] = {"verdict_id": "stale"}
        ui._llm_verdicts["c-sibling"] = {"verdict_id": "sibling"}
        # Entering approve_tools for the new round — the scoped purge
        # must fire for c-reused.  needs_approval=False so the gate
        # returns without blocking on user input.
        with _patch_get_storage(MagicMock()):
            ui.approve_tools([{"call_id": "c-reused", "func_name": "ls", "needs_approval": False}])
        assert "c-reused" not in ui._llm_verdicts, (
            f"{cls.__name__}.approve_tools did not purge the entering batch's "
            "stale cached verdict — round 2 could ride round 1's approve"
        )
        assert "c-reused" not in ui._recent_decisions, (
            f"{cls.__name__}.approve_tools did not purge the entering batch's "
            "stale decision — this round's verdicts would inherit it"
        )
        assert ui._llm_verdicts.get("c-sibling") == {"verdict_id": "sibling"}, (
            f"{cls.__name__}.approve_tools wiped a concurrent sibling's verdict"
        )


def test_on_intent_verdict_decision_check_and_queue_are_atomic() -> None:
    """Regression for the on_intent_verdict ↔ resolve_approval race.

    The owner-check, the park, and the fallback decision-read must
    happen under a SINGLE lock acquisition: ``resolve_approval`` marks
    the cycle resolved and records the per-call decision atomically
    under the same lock, so exactly one side wins — the verdict is
    either parked pre-decision (resolve stamps it from the cycle's
    ``pending_verdicts``) or stamped post-decision (from
    ``_recent_decisions``).  A check-then-release-then-park pattern
    reopens the window where a verdict lands unparked AND unstamped —
    an audit row stuck at "pending" forever.

    This test counts lock acquisitions during one ``on_intent_verdict``
    and fails if the release-then-reacquire pattern returns.
    """
    ui = _make_ui()
    _register_cycle(ui, ["c1"])
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
    # one for owner-check + park-or-stamp. A third acquisition means
    # the release-then-reacquire window is back.
    assert acquire_count == 2, (
        f"on_intent_verdict acquired _ws_lock {acquire_count} times; "
        "owner-check + park-or-stamp must happen under ONE acquisition "
        "to avoid a race with resolve_approval"
    )


def test_resolve_approval_stamps_all_pending_verdicts() -> None:
    """Normal path: multiple verdicts parked on the round's cycle, all
    stamped with the user's decision on resolve."""
    storage = MagicMock()
    ui = _make_ui()
    cycle = _register_cycle(ui, ["c1", "c2"])
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
        ui.on_intent_verdict({"verdict_id": "v2", "call_id": "c2"})
    assert len(cycle.pending_verdicts) == 2
    with _patch_get_storage(storage):
        ui.resolve_approval(False, "too risky")
    # Both verdicts get stamped.
    stamped_ids = {c.args[0] for c in storage.update_intent_verdict.call_args_list}
    assert stamped_ids == {"v1", "v2"}
    # Cycle's park cleared after resolve; decisions recorded per call.
    assert cycle.pending_verdicts == []
    assert ui._recent_decisions.get("c1") == ("denied", None)
    assert ui._recent_decisions.get("c2") == ("denied", None)


# ---------------------------------------------------------------------------
# user_decision value space — pending / approved / denied / timeout
# / auto-approve reasons (policy / blanket / skill / always / auto_approve_tools).
# Guards the "user_decision is never empty for new rows" invariant.
# ---------------------------------------------------------------------------


def test_resolve_approval_timeout_kwarg_writes_timeout_value() -> None:
    """``resolve_approval(False, ..., timeout=True)`` writes
    ``user_decision="timeout"`` so the audit trail can distinguish a
    passive timeout expiry from an active user denial — the feedback
    string used to carry this distinction but the column alone could not."""
    storage = MagicMock()
    ui = _make_ui()
    _register_cycle(ui, ["c1"])
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v1", "call_id": "c1"})
    with _patch_get_storage(storage):
        ui.resolve_approval(False, "expired", timeout=True)
    storage.update_intent_verdict.assert_any_call("v1", user_decision="timeout")
    assert ui._recent_decisions.get("c1") == ("timeout", None)


def test_resolve_approval_timeout_with_approved_raises() -> None:
    """``timeout=True`` is mutually exclusive with ``approved=True`` —
    the combination would land a row whose audit column says
    ``"timeout"`` while the SSE event reports ``approved=True``. Fail
    loud so the inconsistency can't ship silently."""
    import pytest

    ui = _make_ui()
    with pytest.raises(ValueError, match="timeout"):
        ui.resolve_approval(True, timeout=True)


def test_record_auto_approves_populates_reason_lookup() -> None:
    """``_record_auto_approves`` must seed
    ``_auto_approve_reasons[call_id]`` with the per-item reason so a
    late-arriving LLM judge verdict can recover the auto-approve
    reason via ``on_intent_verdict``."""
    storage = MagicMock()
    ui = _make_ui()
    items = [
        {
            "call_id": "c-policy",
            "func_name": "bash",
            "auto_approved": True,
            "auto_approve_reason": "policy",
        },
        {
            "call_id": "c-blanket",
            "func_name": "list_workstreams",
            "auto_approved": True,
            "auto_approve_reason": "blanket",
        },
    ]
    with _patch_get_storage(storage):
        ui._record_auto_approves(items)
    assert "c-policy" in ui._auto_approve_reasons
    assert "c-blanket" in ui._auto_approve_reasons
    assert ui._auto_approve_reasons["c-policy"][0] == "policy"
    assert ui._auto_approve_reasons["c-blanket"][0] == "blanket"


def test_on_intent_verdict_consumes_auto_approve_reason() -> None:
    """A late LLM verdict for a previously auto-approved call_id picks
    up the reason from ``_auto_approve_reasons``, stamps it on the
    verdict before persist, and pops the entry so re-use isn't
    possible. Closes the misdiagnosis bug where auto-approved tools
    landed verdict rows with ``user_decision=""``."""
    storage = MagicMock()
    ui = _make_ui()
    ui._auto_approve_reasons["c-x"] = ("auto_approve_tools", 0.0)
    with _patch_get_storage(storage):
        ui.on_intent_verdict({"verdict_id": "v-x", "call_id": "c-x"})
    storage.upsert_intent_verdict.assert_called_once()
    kwargs = storage.upsert_intent_verdict.call_args.kwargs
    assert kwargs["user_decision"] == "auto_approve_tools"
    # Consumed on read so the same call_id can't double-stamp later.
    assert "c-x" not in ui._auto_approve_reasons


def test_on_intent_verdict_auto_reason_survives_resolve_cycle() -> None:
    """Mixed-batch case: one tool was auto-approved (policy), another
    needs manual approval. The LLM judge fires for the auto-approved
    sibling DURING the manual-approval wait. The verdict must land
    with ``user_decision="policy"`` and stay that way even after
    ``resolve_approval`` fires for the pending sibling — the prior
    bug was that the auto-stamped row got overwritten with
    ``"approved"``/``"denied"`` by the resolve path."""
    storage = MagicMock()
    ui = _make_ui()
    _register_cycle(ui, ["c-pending"])
    ui._auto_approve_reasons["c-auto"] = ("policy", 0.0)
    with _patch_get_storage(storage):
        # LLM verdict fires for the auto-approved sibling.
        ui.on_intent_verdict({"verdict_id": "v-auto", "call_id": "c-auto"})
        # Now the pending sibling gets a verdict + manual resolve.
        ui.on_intent_verdict({"verdict_id": "v-pending", "call_id": "c-pending"})
        ui.resolve_approval(True, "looks good")
    # Only the pending verdict should be UPDATEd to "approved" — the
    # auto-stamped one stays "policy" via its INSERT.
    update_calls = {
        c.args[0]: c.kwargs.get("user_decision")
        for c in storage.update_intent_verdict.call_args_list
    }
    assert update_calls == {"v-pending": "approved"}
    # The auto verdict's INSERT carried the policy reason.
    insert_calls = {
        c.kwargs["verdict_id"]: c.kwargs["user_decision"]
        for c in storage.upsert_intent_verdict.call_args_list
    }
    assert insert_calls["v-auto"] == "policy"
    assert insert_calls["v-pending"] == "pending"


def test_persist_auto_approved_heuristic_verdicts_stamps_reason() -> None:
    """The auto-approve early-return branches in ``approve_tools`` used
    to drop heuristic verdicts on the floor — auditors couldn't tell
    whether the judge ran or the call was simply silently auto-approved.
    ``_persist_auto_approved_heuristic_verdicts`` closes that gap and
    stamps each verdict with the item's reason."""
    storage = MagicMock()
    ui = _make_ui()
    items = [
        {
            "call_id": "c-1",
            "auto_approved": True,
            "auto_approve_reason": "blanket",
            "_heuristic_verdict": {
                "verdict_id": "v-1",
                "call_id": "c-1",
                "risk_level": "low",
                "recommendation": "review",
            },
        },
        # No _heuristic_verdict — skipped (judge didn't run for this item).
        {"call_id": "c-2", "auto_approved": True, "auto_approve_reason": "blanket"},
        # Not auto_approved — skipped (this helper only handles auto-approved).
        {
            "call_id": "c-3",
            "_heuristic_verdict": {"verdict_id": "v-3", "call_id": "c-3"},
        },
    ]
    with _patch_get_storage(storage):
        ui._persist_auto_approved_heuristic_verdicts(items)
    storage.create_intent_verdicts_bulk.assert_called_once()
    rows = storage.create_intent_verdicts_bulk.call_args.args[0]
    assert len(rows) == 1
    assert rows[0]["verdict_id"] == "v-1"
    assert rows[0]["user_decision"] == "blanket"


def test_auto_approve_reasons_ttl_prune_drops_stale_entries() -> None:
    """Lazy TTL eviction at write time: entries older than
    ``_AUTO_APPROVE_REASON_TTL`` are pruned on the next
    ``_record_auto_approves`` call. Without this, a session with the
    LLM judge disabled would accumulate entries that never get
    consumed."""
    import time as time_module

    storage = MagicMock()
    ui = _make_ui()
    # Seed two stale entries (well past the TTL).
    stale_ts = time_module.time() - ui._AUTO_APPROVE_REASON_TTL - 30.0
    ui._auto_approve_reasons["c-stale-1"] = ("policy", stale_ts)
    ui._auto_approve_reasons["c-stale-2"] = ("blanket", stale_ts)
    items = [
        {
            "call_id": "c-fresh",
            "auto_approved": True,
            "auto_approve_reason": "skill",
            "func_name": "bash",
        }
    ]
    with _patch_get_storage(storage):
        ui._record_auto_approves(items)
    # Stale entries pruned; only the fresh one remains.
    assert "c-stale-1" not in ui._auto_approve_reasons
    assert "c-stale-2" not in ui._auto_approve_reasons
    assert "c-fresh" in ui._auto_approve_reasons


# ---------------------------------------------------------------------------
# Output guard persistence
# ---------------------------------------------------------------------------


def test_on_output_warning_enqueues_only() -> None:
    # Persistence was decoupled from on_output_warning when the LLM
    # judge stage landed — the session now calls record_output_assessment
    # directly per tier.  on_output_warning is UI-dispatch only.
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
    storage.record_output_assessment.assert_not_called()


def test_record_output_assessment_persists_with_tier() -> None:
    storage = MagicMock()
    ui = _make_ui()
    assessment = {
        "func_name": "web_fetch",
        "flags": ["camouflaged_injection"],
        "risk_level": "medium",
        "output_length": 4096,
    }
    with _patch_get_storage(storage):
        ui.record_output_assessment(
            "call-2",
            assessment,
            tier="llm",
            reasoning="LLM saw a camouflaged directive",
            judge_model="gpt-5-mini",
            latency_ms=142,
        )
    storage.record_output_assessment.assert_called_once()
    kwargs = storage.record_output_assessment.call_args.kwargs
    assert kwargs["tier"] == "llm"
    assert kwargs["reasoning"] == "LLM saw a camouflaged directive"
    assert kwargs["judge_model"] == "gpt-5-mini"
    assert kwargs["latency_ms"] == 142
    assert kwargs["risk_level"] == "medium"


def test_record_output_assessment_defaults_to_heuristic_tier() -> None:
    storage = MagicMock()
    ui = _make_ui()
    assessment = {
        "func_name": "bash",
        "flags": [],
        "risk_level": "none",
        "output_length": 0,
    }
    with _patch_get_storage(storage):
        ui.record_output_assessment("call-3", assessment)
    kwargs = storage.record_output_assessment.call_args.kwargs
    assert kwargs["tier"] == "heuristic"
    assert kwargs["reasoning"] == ""
    assert kwargs["judge_model"] == ""
    assert kwargs["latency_ms"] == 0


# ---------------------------------------------------------------------------
# Concurrency smoke
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# serialize_pending_approval_details — dashboard projection (per cycle)
# ---------------------------------------------------------------------------


def _register_card_cycle(ui: SessionUIBase, card: dict[str, Any]) -> Any:
    """Register a cycle from a raw approve_request card (shape-exact tests)."""
    from turnstone.core.session_ui_base import ApprovalCycle

    cycle = ApprovalCycle(list(card.get("items") or []), card, None)
    ui._register_approval_cycle(cycle)
    return cycle


def test_serialize_pending_approval_details_empty_when_no_cycles() -> None:
    ui = _make_ui()
    assert ui.serialize_pending_approval_details() == []


def test_serialize_pending_approval_details_skips_empty_items_card() -> None:
    ui = _make_ui()
    _register_card_cycle(
        ui, {"type": "approve_request", "cycle_id": "cy-0", "items": [], "judge_pending": False}
    )
    assert ui.serialize_pending_approval_details() == []


def test_serialize_pending_approval_details_merges_judge_verdict() -> None:
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
            "items": [
                {
                    "call_id": "c-1",
                    "header": "bash",
                    "preview": "$ ls",
                    "func_name": "bash",
                    "approval_label": "bash",
                    "needs_approval": True,
                    "error": None,
                    "heuristic_verdict": {"recommendation": "review", "tier": "heuristic"},
                }
            ],
            "judge_pending": True,
        },
    )
    ui._llm_verdicts["c-1"] = {
        "verdict_id": "v-1",
        "call_id": "c-1",
        "risk_level": "high",
        "recommendation": "deny",
        "tier": "llm",
    }
    details = ui.serialize_pending_approval_details()
    assert len(details) == 1
    detail = details[0]
    assert detail["cycle_id"] == "cy-1"
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


def test_serialize_pending_approval_details_judge_verdict_none_when_missing() -> None:
    """No cached verdict for the call_id → judge_verdict is None,
    not absent or some sentinel."""
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
            "items": [{"call_id": "c-1", "func_name": "ls", "needs_approval": True}],
            "judge_pending": True,
        },
    )
    details = ui.serialize_pending_approval_details()
    assert details[0]["items"][0]["judge_verdict"] is None
    assert details[0]["items"][0]["heuristic_verdict"] is None


def test_serialize_pending_approval_details_multi_item() -> None:
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
            "items": [
                {"call_id": "c-1", "func_name": "bash", "needs_approval": True},
                {"call_id": "c-2", "func_name": "mcp__sf__query", "needs_approval": True},
            ],
            "judge_pending": False,
        },
    )
    ui._llm_verdicts["c-2"] = {"recommendation": "deny", "risk_level": "crit"}
    details = ui.serialize_pending_approval_details()
    detail = details[0]
    assert detail["call_id"] == "c-1"  # primary = first item
    assert len(detail["items"]) == 2
    assert detail["items"][0]["judge_verdict"] is None
    assert detail["items"][1]["judge_verdict"]["recommendation"] == "deny"


def test_serialize_pending_approval_details_one_entry_per_live_cycle() -> None:
    """Parallel task agents: every live cycle serializes, oldest first,
    each addressable by its cycle_id — the single-slot serializer only
    ever showed the one card the last writer left behind."""
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-old",
            "items": [{"call_id": "a-1", "func_name": "bash", "needs_approval": True}],
            "judge_pending": False,
        },
    )
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-new",
            "items": [{"call_id": "b-1", "func_name": "write_file", "needs_approval": True}],
            "judge_pending": False,
        },
    )
    details = ui.serialize_pending_approval_details()
    assert [d["cycle_id"] for d in details] == ["cy-old", "cy-new"]
    assert [d["call_id"] for d in details] == ["a-1", "b-1"]


def test_serialize_pending_approval_details_tool_policy_denied_passthrough() -> None:
    """A tool-policy-denied item carries error + needs_approval=False
    after WebUI.approve_tools mutates the items list. The serializer
    must round-trip both fields so the JS can detect the
    POLICY-BLOCKED matrix row and render the banner instead of
    approve/deny buttons."""
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
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
        },
    )
    item = ui.serialize_pending_approval_details()[0]["items"][0]
    # Both fields are the JS detection keys for the POLICY-BLOCKED
    # branch in renderApprovalBlock — drift here silently regresses
    # to a buttoned approve UI on a server-blocked call.
    assert item["needs_approval"] is False
    assert item["error"] == "Blocked by tool policy (pattern match for 'rm_rf')"


def test_serialize_pending_approval_details_judge_unavailable_path() -> None:
    """No judge_verdict + no heuristic_verdict + judge_pending=False
    is the (judge unavailable) matrix row — the JS detects it via
    !verdict && !judgePending && !policyBlocked. Verify the
    serialized payload preserves the absence of all three signals."""
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
            "items": [
                {
                    "call_id": "c-1",
                    "func_name": "bash",
                    "approval_label": "bash",
                    "needs_approval": True,
                }
            ],
            "judge_pending": False,
        },
    )
    detail = ui.serialize_pending_approval_details()[0]
    assert detail["judge_pending"] is False
    item = detail["items"][0]
    assert item["judge_verdict"] is None
    assert item["heuristic_verdict"] is None
    assert item["needs_approval"] is True
    assert item["error"] is None


def test_serialize_pending_approval_details_returned_dict_is_decoupled() -> None:
    """Mutating the returned dict must not corrupt the cached
    verdict, which other consumers may still read."""
    ui = _make_ui()
    _register_card_cycle(
        ui,
        {
            "type": "approve_request",
            "cycle_id": "cy-1",
            "items": [{"call_id": "c-1", "func_name": "bash", "needs_approval": True}],
            "judge_pending": False,
        },
    )
    ui._llm_verdicts["c-1"] = {"recommendation": "approve"}
    detail = ui.serialize_pending_approval_details()[0]
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
    assert ui._event_id == 1


def test_on_reasoning_token_writes_to_inflight_buffer_only() -> None:
    """Reasoning has no multi-turn IDLE piggyback — only the inflight
    buffer + the seq counter."""
    ui = _make_ui()
    ui.on_reasoning_token("thinking...")
    assert ui._ws_inflight_reasoning == ["thinking..."]
    assert ui._event_id == 1
    # Multi-turn buffer is content-only and untouched by reasoning.
    assert ui._ws_turn_content == []


def test_inflight_seq_advances_on_every_emit_even_at_cap() -> None:
    """Cap-hit content tokens MUST advance ``_event_id``,
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
    seq_at_cap = ui._event_id

    # Cap-hit token: seq MUST advance (no buffer append, but the
    # event still gets a fresh seq for the dedup filter).
    ui.on_content_token(chunk)
    assert ui._event_id == seq_at_cap + 1
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
    seq_pre_commit = ui._event_id
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
    assert ui._event_id == 2

    ui.snapshot_and_consume_state_payload("idle")
    assert ui._event_id == 2

    ui.snapshot_and_consume_state_payload("error")
    assert ui._event_id == 2


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


# ---------------------------------------------------------------------------
# Smart Approvals (judge.smart_approvals)
# ---------------------------------------------------------------------------


class _SeedingUI(_ConcreteUI):
    """Re-delivers seeded LLM verdicts right after the per-round purge
    evicts the entering batch's ids — simulates the async judge daemon
    delivering them via ``on_intent_verdict`` during the Smart Approvals
    wait, which is the only point at which they can land and survive
    the purge.  ``seed_judge_event`` optionally tags the deliveries with
    a generation, for window tests that need a STALE-generation arrival
    between the purge and the cycle registration."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seed_verdicts: list[dict[str, Any]] = []
        self.seed_judge_event: threading.Event | None = None

    def _purge_round_verdicts(
        self,
        call_ids: set[str],
        keep_origin: threading.Event | None = None,
    ) -> None:
        super()._purge_round_verdicts(call_ids, keep_origin=keep_origin)
        for verdict in self.seed_verdicts:
            self.on_intent_verdict(dict(verdict), judge_event=self.seed_judge_event)


def _patch_policies(verdicts: dict[str, str]):  # type: ignore[no-untyped-def]
    """Neutralise the admin tool-policy stage so approve_tools tests
    isolate the Smart Approvals gate."""
    return patch(
        "turnstone.core.policy.evaluate_tool_policies_batch",
        return_value=verdicts,
    )


def _drain(lq: queue.Queue[Any]) -> list[dict[str, Any]]:
    """Drain all currently-queued events off a listener queue."""
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(lq.get_nowait())
        except queue.Empty:
            return out


def _smart_ui() -> _ConcreteUI:
    ui = _make_ui()
    ui.smart_approvals_enabled = True
    ui.smart_approval_threshold = 0.95
    ui.smart_approval_wait_seconds = 1.0
    return ui


def _pending_item(call_id: str, func_name: str = "bash") -> dict[str, Any]:
    """A still-pending tool call carrying a heuristic verdict, matching
    what ``ChatSession._evaluate_intent`` attaches before the gate."""
    return {
        "call_id": call_id,
        "func_name": func_name,
        "approval_label": func_name,
        "header": f"Tool: {func_name}",
        "preview": "",
        "needs_approval": True,
        "_heuristic_verdict": {
            "verdict_id": f"h-{call_id}",
            "call_id": call_id,
            "func_name": func_name,
            "risk_level": "medium",
            "confidence": 0.5,
            "recommendation": "review",
        },
    }


def _llm_verdict(
    call_id: str,
    *,
    recommendation: str = "approve",
    confidence: float = 0.99,
    tier: str = "llm",
) -> dict[str, Any]:
    return {
        "verdict_id": f"v-{call_id}",
        "call_id": call_id,
        "func_name": "bash",
        "risk_level": "low",
        "confidence": confidence,
        "recommendation": recommendation,
        "tier": tier,
        "intent_summary": "",
        "reasoning": "",
        "evidence": [],
    }


def test_smart_approval_clears_high_confidence_llm_approve() -> None:
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=0.99)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == []  # nothing left for a human
    assert item["needs_approval"] is False
    assert item["auto_approved"] is True
    assert item["auto_approve_reason"] == "smart_approval"


def test_smart_approval_clears_at_exact_threshold() -> None:
    """``confidence >= threshold`` — the boundary value auto-approves."""
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=0.95)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == []
    assert item["auto_approved"] is True


def test_smart_approval_holds_just_below_threshold() -> None:
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=0.94)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True
    assert item["needs_approval"] is True


def test_smart_approval_holds_review_and_deny() -> None:
    """Only ``approve`` auto-approves; ``review`` / ``deny`` reach a human
    no matter how confident the judge is."""
    ui = _smart_ui()
    for rec in ("review", "deny"):
        item = _pending_item("c1")
        ui._llm_verdicts = {"c1": _llm_verdict("c1", recommendation=rec, confidence=1.0)}
        with _patch_get_storage(MagicMock()):
            remaining = ui._apply_smart_approvals([item])
        assert remaining == [item], rec
        assert item.get("auto_approved") is not True, rec


def test_smart_approval_holds_llm_fallback_even_if_approve() -> None:
    """A ``llm_fallback`` verdict means the LLM stage timed out / errored
    and the row is the heuristic carry-over.  Even if it reads ``approve``
    at full confidence it must reach a human — errors require attention."""
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict(
        "c1", recommendation="approve", confidence=1.0, tier="llm_fallback"
    )
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True


def test_smart_approval_holds_when_no_verdict_arrives() -> None:
    """Wait budget elapses with no verdict cached → fail closed to the
    human gate."""
    ui = _smart_ui()
    ui.smart_approval_wait_seconds = 0.05  # nothing will be delivered
    item = _pending_item("c1")
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item["needs_approval"] is True


def test_smart_approval_batch_atomic_holds_whole_batch_on_one_failure() -> None:
    """Batch-atomic: a single non-qualifying call (here a review) in a
    parallel batch holds the ENTIRE batch for a human — including the call
    that individually qualified.  Parallel calls are one unit of intent."""
    ui = _smart_ui()
    a = _pending_item("c1")
    b = _pending_item("c2")
    ui._llm_verdicts = {
        "c1": _llm_verdict("c1", recommendation="approve", confidence=0.99),
        "c2": _llm_verdict("c2", recommendation="review", confidence=0.99),
    }
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([a, b])
    assert remaining == [a, b]  # NONE auto-approved
    assert a.get("auto_approved") is not True
    assert b.get("auto_approved") is not True


def test_smart_approval_approves_full_batch_when_all_qualify() -> None:
    """When every call in a parallel batch qualifies, the whole batch is
    auto-approved and nothing is left for a human."""
    ui = _smart_ui()
    a = _pending_item("c1")
    b = _pending_item("c2")
    ui._llm_verdicts = {
        "c1": _llm_verdict("c1", recommendation="approve", confidence=0.99),
        "c2": _llm_verdict("c2", recommendation="approve", confidence=0.96),
    }
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([a, b])
    assert remaining == []
    assert a["auto_approved"] is True and b["auto_approved"] is True
    assert a["needs_approval"] is False and b["needs_approval"] is False


def test_smart_approved_item_serializes_llm_verdict_not_heuristic() -> None:
    """The auto-approved tool row must carry the driving LLM verdict
    (llm/approve) as judge_verdict so the UI doesn't render a contradictory
    heuristic 'review/medium' chip beside the SMART_APPROVAL pill."""
    ui = _smart_ui()
    item = _pending_item("c1")  # heuristic verdict is review / medium
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=0.99)
    with _patch_get_storage(MagicMock()):
        ui._apply_smart_approvals([item])
    serialized = _ConcreteUI._serialize_approval_items([item])[0]
    assert serialized["auto_approved"] is True
    assert serialized["auto_approve_reason"] == "smart_approval"
    judge_verdict = serialized["judge_verdict"]
    assert judge_verdict["tier"] == "llm"
    assert judge_verdict["recommendation"] == "approve"
    # Heuristic still carried, but judge_verdict is what the row renders.
    assert serialized["heuristic_verdict"]["recommendation"] == "review"


def test_smart_approval_holds_batch_when_one_call_has_no_verdict() -> None:
    """A parallel batch where one call never gets a verdict (timeout) holds
    the whole batch, even though its sibling qualified."""
    ui = _smart_ui()
    ui.smart_approval_wait_seconds = 0.05
    a = _pending_item("c1")
    b = _pending_item("c2")
    ui._llm_verdicts = {"c1": _llm_verdict("c1", recommendation="approve", confidence=0.99)}
    # c2 has no verdict — the wait times out and the batch is held.
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([a, b])
    assert remaining == [a, b]
    assert a.get("auto_approved") is not True


def test_smart_approval_skips_budget_override_pseudo_tool() -> None:
    """The synthetic ``__budget_override__`` must always reach a human,
    never smart-approved."""
    ui = _smart_ui()
    item = _pending_item("c1", func_name="__budget_override__")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=1.0)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True


def test_smart_approval_stamps_verdict_user_decision() -> None:
    """The LLM verdict arrived during the wait (cache-only — no cycle
    exists yet at that point); the smart stage stamps ``smart_approval``
    on both the cached dict and the persisted row, and the non-"pending"
    stamp keeps every later cycle-registration sweep away from it."""
    storage = MagicMock()
    ui = _smart_ui()
    item = _pending_item("c1")
    verdict = _llm_verdict("c1", recommendation="approve", confidence=0.99)
    ui._llm_verdicts["c1"] = verdict
    with _patch_get_storage(storage):
        ui._apply_smart_approvals([item])
    assert ui._llm_verdicts["c1"]["user_decision"] == "smart_approval"
    storage.update_intent_verdict.assert_called_once_with("v-c1", user_decision="smart_approval")


def test_approve_tools_smart_approves_whole_batch_without_prompt() -> None:
    """End-to-end through approve_tools: the verdict is delivered after
    the cache reset (via _SeedingUI), the gate auto-approves, and the
    function returns approved without ever emitting an approval prompt."""
    storage = MagicMock()
    ui = _SeedingUI(ws_id="ws-1", user_id="u1")
    ui.smart_approvals_enabled = True
    ui.smart_approval_threshold = 0.95
    ui.smart_approval_wait_seconds = 1.0
    item = _pending_item("c1")
    ui.seed_verdicts = [_llm_verdict("c1", recommendation="approve", confidence=0.99)]
    lq = ui._register_listener()
    with _patch_get_storage(storage), _patch_policies({}):
        approved, feedback = ui.approve_tools([item])
    assert approved is True
    assert feedback is None
    assert item["auto_approved"] is True
    assert item["auto_approve_reason"] == "smart_approval"
    assert item["needs_approval"] is False
    assert ui._pending_approval is None  # operator was never prompted
    assert ui._approval_cycles == {}  # no cycle was ever registered
    # No approval prompt was fanned out to listeners.
    events = []
    while True:
        try:
            events.append(lq.get_nowait()["type"])
        except queue.Empty:
            break
    assert "approve_request" not in events


def test_approve_tools_skips_smart_stage_when_disabled() -> None:
    """With Smart Approvals off (the default), a confident approve verdict
    does NOT bypass the human — approve_tools blocks on the prompt as
    before."""
    storage = MagicMock()
    ui = _SeedingUI(ws_id="ws-1", user_id="u1")
    ui.smart_approvals_enabled = False
    ui.smart_approval_wait_seconds = 1.0
    item = _pending_item("c1")
    ui.seed_verdicts = [_llm_verdict("c1", recommendation="approve", confidence=0.99)]
    timer = resolve_when_pending(ui, True, "ok")
    timer.start()
    try:
        with _patch_get_storage(storage), _patch_policies({}):
            approved, _feedback = ui.approve_tools([item])
    finally:
        timer.cancel()
    assert approved is True  # the human approved, not the judge
    assert item.get("auto_approve_reason") != "smart_approval"
    assert item.get("auto_approved") is not True


def test_await_llm_verdicts_returns_when_verdict_delivered() -> None:
    """The wait wakes as soon as the last needed verdict lands, well
    before the budget elapses."""
    ui = _smart_ui()

    def _deliver() -> None:
        with _patch_get_storage(MagicMock()):
            ui.on_intent_verdict(_llm_verdict("c1"))

    timer = threading.Timer(0.02, _deliver)
    timer.start()
    try:
        # Generous budget; should return on the notify, not the timeout.
        ui._await_llm_verdicts({"c1"}, 5.0)
    finally:
        timer.cancel()
    assert "c1" in ui._llm_verdicts


def test_smart_approval_respects_heuristic_deny_floor() -> None:
    """A high-confidence LLM ``approve`` must NOT override a deterministic
    heuristic ``deny`` — the LLM may escalate the heuristic but never lower
    it.  The call reaches a human."""
    ui = _smart_ui()
    item = _pending_item("c1")
    item["_heuristic_verdict"]["recommendation"] = "deny"
    item["_heuristic_verdict"]["risk_level"] = "critical"
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=1.0)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True
    assert item["needs_approval"] is True


def test_smart_approval_respects_heuristic_critical_floor() -> None:
    """A heuristic ``critical`` risk_level blocks smart approval even when
    the heuristic recommendation itself isn't ``deny``."""
    ui = _smart_ui()
    item = _pending_item("c1")
    item["_heuristic_verdict"]["recommendation"] = "review"
    item["_heuristic_verdict"]["risk_level"] = "critical"
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=1.0)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True


def test_smart_approval_skips_oversized_batch() -> None:
    """A batch with more calls than the FIFO verdict-cache cap can't be
    reliably awaited (older verdicts evict before the wait sees them all),
    so the whole batch reaches a human rather than stalling on the wait."""
    ui = _smart_ui()
    n = ui._LLM_VERDICT_CACHE_MAX + 1
    items = [_pending_item(f"c{i}") for i in range(n)]
    for i in range(n):
        ui._llm_verdicts[f"c{i}"] = _llm_verdict(f"c{i}", recommendation="approve", confidence=1.0)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals(items)
    assert remaining == items  # none auto-approved
    assert all(it.get("auto_approved") is not True for it in items)


def test_replay_pending_verdicts_reemits_cached_verdicts() -> None:
    """The streaming-fix helper re-fans-out each pending call's cached LLM
    verdict as an intent_verdict event."""
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="review", confidence=0.9)
    lq = ui._register_listener()
    ui._replay_pending_verdicts([item])
    intent_events = []
    while True:
        try:
            ev = lq.get_nowait()
        except queue.Empty:
            break
        if ev.get("type") == "intent_verdict":
            intent_events.append(ev)
    assert len(intent_events) == 1
    assert intent_events[0]["call_id"] == "c1"
    assert intent_events[0]["recommendation"] == "review"


def test_approve_tools_reemits_verdict_after_card_on_held_batch() -> None:
    """Streaming regression fix: when Smart Approvals holds a batch (e.g. a
    review verdict), the approve_request card is FOLLOWED by a re-emitted
    intent_verdict so the live chip updates without a browser reload."""
    storage = MagicMock()
    ui = _SeedingUI(ws_id="ws-1", user_id="u1")
    ui.smart_approvals_enabled = True
    ui.smart_approval_threshold = 0.95
    ui.smart_approval_wait_seconds = 1.0
    item = _pending_item("c1")
    ui.seed_verdicts = [_llm_verdict("c1", recommendation="review", confidence=0.99)]
    lq = ui._register_listener()
    timer = resolve_when_pending(ui, False, "no")
    timer.start()
    try:
        with _patch_get_storage(storage), _patch_policies({}):
            ui.approve_tools([item])
    finally:
        timer.cancel()
    events = []
    while True:
        try:
            events.append(lq.get_nowait())
        except queue.Empty:
            break
    types = [e.get("type") for e in events]
    assert "approve_request" in types
    # An intent_verdict is re-emitted AFTER the card (the live chip update).
    ar = types.index("approve_request")
    assert "intent_verdict" in types[ar + 1 :]
    # The wait already collected the verdict, so the card must not claim the
    # judge is still working — no spurious "judge pending" spinner / poll.
    assert events[ar].get("judge_pending") is False


def test_judge_pending_true_when_llm_verdict_not_yet_cached() -> None:
    """Normal async flow (Smart Approvals off): a judged call whose LLM
    verdict hasn't arrived yet → approve_request reports judge_pending=True."""
    ui = _make_ui()  # smart_approvals_enabled defaults False
    item = _pending_item("c1")  # carries _heuristic_verdict, no cached LLM verdict
    lq = ui._register_listener()
    timer = resolve_when_pending(ui, True, "ok")
    timer.start()
    try:
        with _patch_get_storage(MagicMock()), _patch_policies({}):
            ui.approve_tools([item])
    finally:
        timer.cancel()
    reqs = [e for e in _drain(lq) if e.get("type") == "approve_request"]
    assert reqs and reqs[0]["judge_pending"] is True


def test_auto_approve_reason_vocabulary_matches_js() -> None:
    """AutoApproveReason.ALL must stay in lockstep with the JS
    KNOWN_AUTO_APPROVE_REASONS set — a server-sent reason missing from the JS
    set degrades to the 'unknown' pill on the coordinator tree."""
    import re
    from pathlib import Path

    from turnstone.core.session_ui_base import AutoApproveReason

    js = Path(__file__).resolve().parents[1] / "turnstone/console/static/coordinator/coordinator.js"
    m = re.search(
        r"KNOWN_AUTO_APPROVE_REASONS\s*=\s*new Set\(\s*\[(.*?)\]",
        js.read_text(),
        re.S,
    )
    assert m, "KNOWN_AUTO_APPROVE_REASONS set not found in coordinator.js"
    js_reasons = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert js_reasons == AutoApproveReason.ALL


def test_verdict_confidence_rejects_non_finite() -> None:
    """NaN/inf confidence is treated as malformed (0.0), not clamped to 1.0."""
    assert _ConcreteUI._verdict_confidence({"confidence": float("nan")}) == 0.0
    assert _ConcreteUI._verdict_confidence({"confidence": float("inf")}) == 0.0
    assert _ConcreteUI._verdict_confidence({"confidence": 0.97}) == 0.97


def test_smart_approval_holds_nan_confidence() -> None:
    """A NaN confidence (json.loads accepts NaN) must NOT clear the
    auto-approve bar even with recommendation=approve."""
    ui = _smart_ui()
    item = _pending_item("c1")
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=float("nan"))
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True


def test_smart_approval_holds_batch_with_duplicate_call_ids() -> None:
    """Two pending calls sharing a call_id (some local models emit duplicate
    non-empty ids) must not both be cleared by the single shared verdict —
    hold the whole batch."""
    ui = _smart_ui()
    a = _pending_item("dup")
    b = _pending_item("dup")  # same call_id, distinct call
    ui._llm_verdicts["dup"] = _llm_verdict("dup", recommendation="approve", confidence=0.99)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([a, b])
    assert remaining == [a, b]
    assert a.get("auto_approved") is not True


def test_on_intent_verdict_skips_park_for_already_finalized_verdict() -> None:
    """Guards the audit-corruption race: a verdict already stamped with a
    final user_decision (e.g. ``_finalize_smart_verdicts`` ran between this
    verdict's notify and its park) is NOT parked on its owning cycle,
    so that cycle's resolve_approval can't overwrite the audit row."""
    ui = _make_ui()
    cycle = _register_cycle(ui, ["c1"])
    verdict = {"verdict_id": "v1", "call_id": "c1", "user_decision": "smart_approval"}
    with _patch_get_storage(MagicMock()):
        ui.on_intent_verdict(verdict)
    assert cycle.pending_verdicts == []
    assert ui._llm_verdicts["c1"]["user_decision"] == "smart_approval"


# ---------------------------------------------------------------------------
# Early-paint (tool_pending) — render the batch before the judge / gate
# ---------------------------------------------------------------------------


def test_tool_pending_is_first_event_and_precedes_tool_info() -> None:
    """``approve_tools`` emits ``tool_pending`` as its very first event,
    before the auto-approve fall-through emits ``tool_info`` — so the UI
    paints the pending call the instant it lands, not only once the gate
    resolves.  The payload carries the serialised items (keyed by call_id)
    that the later ``tool_info`` / ``approve_request`` upgrades in place."""
    ui = _make_ui()
    lq = ui._register_listener()
    with _patch_get_storage(MagicMock()):
        # needs_approval=False → auto fall-through, no human block.
        ui.approve_tools([{"call_id": "c1", "func_name": "ls", "needs_approval": False}])
    events = _drain(lq)
    types = [e["type"] for e in events]
    assert types[0] == "tool_pending", types
    assert "tool_info" in types
    assert types.index("tool_pending") < types.index("tool_info")
    assert events[0]["items"][0]["call_id"] == "c1"


def test_tool_pending_precedes_smart_approval_gate() -> None:
    """Regression for the #621 block: pre-fix the Smart Approvals verdict
    wait sat AHEAD of the card emit, so nothing painted until the judge
    ruled.  The announce now fires at the top of ``approve_tools`` — already
    on the wire by the time the gate runs — and carries the heuristic
    verdict attached before the gate."""
    ui = _smart_ui()
    lq = ui._register_listener()
    captured: list[str] = []

    def _spy(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Snapshot what the UI has already been told at gate-entry.
        captured.extend(e["type"] for e in _drain(lq))
        return []  # simulate the gate clearing the whole batch (no human, no wait)

    with patch.object(ui, "_apply_smart_approvals", side_effect=_spy), _patch_get_storage(None):
        approved, _feedback = ui.approve_tools([_pending_item("c1")])

    assert approved is True
    assert captured and captured[0] == "tool_pending", captured


# ---------------------------------------------------------------------------
# Sub-agent step tagging (task_agent child events nest under the parent card)
# ---------------------------------------------------------------------------


class TestAgentChildTagging:
    """``note_agent_child`` makes ``_enqueue`` stamp ``parent_call_id`` on a
    sub-tool's events so the UI can nest a task agent's steps under its card.
    Keyed on the immutable child call_id (correct under the parent's parallel
    tool pool); cleared when the task agent finishes."""

    def test_registered_child_event_is_stamped(self) -> None:
        ui = _make_ui()
        lq = ui._register_listener()
        ui.note_agent_child("child-1", "task-A")
        ui._enqueue({"type": "tool_result", "call_id": "child-1", "name": "bash", "output": "ok"})
        assert lq.get_nowait()["parent_call_id"] == "task-A"

    def test_unregistered_call_id_is_not_stamped(self) -> None:
        ui = _make_ui()
        lq = ui._register_listener()
        ui.note_agent_child("child-1", "task-A")
        ui._enqueue({"type": "tool_result", "call_id": "other", "name": "x", "output": "y"})
        assert "parent_call_id" not in lq.get_nowait()

    def test_no_registry_no_stamp(self) -> None:
        """Empty registry short-circuits — events pass through untouched."""
        ui = _make_ui()
        lq = ui._register_listener()
        ui._enqueue({"type": "tool_result", "call_id": "child-1", "name": "x", "output": "y"})
        assert "parent_call_id" not in lq.get_nowait()

    def test_items_payload_is_stamped_per_entry(self) -> None:
        """approve_request / tool_pending carry an ``items`` list; each child
        entry is tagged independently, leaving non-child entries alone."""
        ui = _make_ui()
        lq = ui._register_listener()
        ui.note_agent_child("child-1", "task-A")
        ui._enqueue(
            {
                "type": "tool_pending",
                "items": [
                    {"call_id": "child-1", "func_name": "bash"},
                    {"call_id": "top-level", "func_name": "search"},
                ],
            }
        )
        items = lq.get_nowait()["items"]
        assert items[0]["parent_call_id"] == "task-A"
        assert "parent_call_id" not in items[1]

    def test_clear_agent_children_stops_stamping(self) -> None:
        ui = _make_ui()
        lq = ui._register_listener()
        ui.note_agent_child("child-1", "task-A")
        ui.clear_agent_children("task-A")
        ui._enqueue({"type": "tool_result", "call_id": "child-1", "name": "x", "output": "y"})
        assert "parent_call_id" not in lq.get_nowait()

    def test_clear_is_scoped_to_one_parent(self) -> None:
        """Two task agents in flight: clearing one leaves the other's children
        tagged — the parallel-pool invariant."""
        ui = _make_ui()
        lq = ui._register_listener()
        ui.note_agent_child("child-A", "task-A")
        ui.note_agent_child("child-B", "task-B")
        ui.clear_agent_children("task-A")
        ui._enqueue({"type": "tool_result", "call_id": "child-B", "name": "x", "output": "y"})
        assert lq.get_nowait()["parent_call_id"] == "task-B"


class TestAgentScopeInfoSuppression:
    """While a task agent runs, its ``on_info`` progress chatter ("[task done] N
    chars", a tool's "fetched N chars") carries no call_id, so it can't nest
    under the task card.  The web pane drops it for the duration rather than let
    it escape to the top level; the per-thread contextvar keeps it correct under
    the parent's parallel task pool (siblings in other threads aren't suppressed)."""

    @pytest.fixture(autouse=True)
    def _reset_scope(self):
        # The scope depth is a module-level contextvar that persists across tests
        # in the same thread; reset it around each so an unbalanced test (or a
        # leak from elsewhere) can't bleed suppression into another test.
        from turnstone.core.session_ui_base import _agent_scope_var

        token = _agent_scope_var.set(0)
        yield
        _agent_scope_var.reset(token)

    def test_on_info_suppressed_within_scope(self) -> None:
        ui = _make_ui()
        lq = ui._register_listener()
        ui.begin_agent_scope()
        ui.on_info("fetched 5663 chars, extracting...")
        ui.end_agent_scope()
        assert lq.empty()

    def test_on_info_passes_through_outside_scope(self) -> None:
        ui = _make_ui()
        lq = ui._register_listener()
        ui.on_info("top-level status")
        assert lq.get_nowait() == {
            "type": "info",
            "message": "top-level status",
            "ws_id": "ws-1",
            "_event_id": 1,
        }

    def test_nested_scopes_need_matching_exits(self) -> None:
        """Parallel task agents: info stays suppressed until the LAST one
        leaves (the depth returns to zero)."""
        ui = _make_ui()
        lq = ui._register_listener()
        ui.begin_agent_scope()
        ui.begin_agent_scope()
        ui.end_agent_scope()
        ui.on_info("still inside a sibling task agent")
        assert lq.empty()
        ui.end_agent_scope()
        ui.on_info("now top-level again")
        assert lq.get_nowait()["message"] == "now top-level again"

    def test_end_scope_floored_at_zero(self) -> None:
        """An unmatched ``end_agent_scope`` can't drive the depth negative and
        wedge suppression off."""
        ui = _make_ui()
        lq = ui._register_listener()
        ui.end_agent_scope()
        ui.begin_agent_scope()
        ui.on_info("suppressed")
        assert lq.empty()


class TestAgentTrajectoryStash:
    """The recall store retains a finished task agent's projected sub-trajectory
    keyed by call_id, LRU-bounded.  A miss is the honest "not retained" signal —
    /history then renders the flat parent record, never a fabricated 0-step card."""

    def test_stash_and_get_roundtrip(self) -> None:
        ui = _make_ui()
        steps = [
            {"id": "t1::c1", "name": "search", "arguments": "{}", "output": "ok", "is_error": False}
        ]
        ui.stash_agent_trajectory("t1", steps)
        assert ui.get_agent_trajectory("t1") == steps

    def test_missing_returns_none(self) -> None:
        assert _make_ui().get_agent_trajectory("nope") is None

    def test_empty_call_id_ignored(self) -> None:
        ui = _make_ui()
        ui.stash_agent_trajectory("", [{"id": "x"}])
        assert ui.get_agent_trajectory("") is None

    def test_restash_updates_value(self) -> None:
        ui = _make_ui()
        ui.stash_agent_trajectory("k", [{"id": "v1"}])
        ui.stash_agent_trajectory("k", [{"id": "v2"}])
        assert ui.get_agent_trajectory("k") == [{"id": "v2"}]

    def test_lru_evicts_oldest(self) -> None:
        from turnstone.core.session_ui_base import _AGENT_TRAJECTORY_CAP

        ui = _make_ui()
        for i in range(_AGENT_TRAJECTORY_CAP + 3):
            ui.stash_agent_trajectory(f"t{i}", [{"id": f"t{i}"}])
        # The three oldest fell out → honest None; the newest is retained.
        assert ui.get_agent_trajectory("t0") is None
        assert ui.get_agent_trajectory("t2") is None
        assert ui.get_agent_trajectory(f"t{_AGENT_TRAJECTORY_CAP + 2}") is not None


# ---------------------------------------------------------------------------
# Concurrent approval cycles — parallel task agents each run their own gate.
# Regression matrix for the two 1.7 release blockers: cross-approval (one
# click resolving every parked gate) and the lost-wakeup hang (a sibling's
# gate entry eating a just-fired resolution).
# ---------------------------------------------------------------------------


_Spawn = Callable[[dict[str, Any]], tuple[threading.Thread, dict[str, Any]]]


@contextlib.contextmanager
def _gate_harness(ui: SessionUIBase) -> Iterator[_Spawn]:
    """ONE storage/policy patch pair + spawn + guaranteed teardown for
    concurrent ``approve_tools`` gates.

    The patches are applied ONCE, on the calling thread, and cover every
    spawned gate thread: ``mock.patch`` start/stop of the SAME target
    from concurrent threads corrupts the patcher's restore stack — the
    second stop can reinstall the first thread's mock as the "original",
    leaking it into every later test in the process.

    Teardown keeps sweeping ``resolve_all_approvals`` until every gate
    thread has exited — a gate that registers its cycle after a single
    sweep would otherwise park for the full approval timeout and trip
    the conftest thread-leak guard.
    """
    threads: list[threading.Thread] = []

    def spawn(item: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any]]:
        box: dict[str, Any] = {}

        def _run() -> None:
            approved, feedback = ui.approve_tools([item])
            box["approved"] = approved
            box["feedback"] = feedback

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        threads.append(t)
        return t, box

    with _patch_get_storage(MagicMock()), _patch_policies({}):
        try:
            yield spawn
        finally:
            stop = time.monotonic() + 5.0
            while any(t.is_alive() for t in threads) and time.monotonic() < stop:
                ui.resolve_all_approvals(False, "test teardown")
                time.sleep(0.01)
            for t in threads:
                t.join(timeout=1.0)


def _wait_for_cycles(ui: SessionUIBase, count: int, deadline: float = 5.0) -> None:
    stop = time.monotonic() + deadline
    while time.monotonic() < stop:
        with ui._ws_lock:
            if len(ui._approval_cycles) >= count:
                return
        time.sleep(0.005)
    raise AssertionError(f"never saw {count} live cycles")


def test_concurrent_gates_resolve_independently() -> None:
    """THE cross-approval regression: two parallel gates, two separate
    decisions.  Approving A's cycle must not wake B, and B's later
    denial must reach B's thread — one click can no longer resolve
    every parked batch with the same verdict."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, box_a = spawn(_pending_item("a-1"))
        tb, box_b = spawn(_pending_item("b-1"))
        _wait_for_cycles(ui, 2)
        assert ui.resolve_approval(True, "run it", call_id="a-1") is not None
        ta.join(timeout=5.0)
        assert not ta.is_alive(), "A's gate did not wake on its own resolution"
        # B is still parked — A's approval must NOT have leaked to it.
        assert tb.is_alive(), "resolving A also unblocked B (cross-approval)"
        assert box_a == {"approved": True, "feedback": "run it"}
        assert ui.resolve_approval(False, "not this one", call_id="b-1") is not None
        tb.join(timeout=5.0)
        assert not tb.is_alive()
        assert box_b["approved"] is False
        assert box_b["feedback"] == "not this one"


def test_sibling_gate_entry_cannot_eat_a_resolution() -> None:
    """THE lost-wakeup regression: under the singleton event, sibling B
    entering the gate ran ``event.clear()`` and could erase A's
    just-fired resolution — A then parked for the full 3600s timeout
    ("approval dialog stuck").  Per-cycle events make the interleaving
    structurally impossible: A's resolution lands on A's OWN event, so
    B's registration can't touch it."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, box_a = spawn(_pending_item("a-1"))
        _wait_for_cycles(ui, 1)
        # Resolve A and IMMEDIATELY register sibling B — the old code's
        # clear() window.  A must still return promptly.
        ui.resolve_approval(True, None, call_id="a-1")
        spawn(_pending_item("b-1"))
        ta.join(timeout=5.0)
        assert not ta.is_alive(), (
            "A's gate lost its wakeup when sibling B entered — the singleton-event race is back"
        )
        assert box_a["approved"] is True


def test_selectorless_resolve_hits_oldest_cycle() -> None:
    """Legacy clients (CLI wrappers, channel adapters, old tabs) send no
    selector — the decision lands on the OLDEST live cycle, matching
    the order the prompts were issued."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, box_a = spawn(_pending_item("a-1"))
        _wait_for_cycles(ui, 1)
        tb, _box_b = spawn(_pending_item("b-1"))
        _wait_for_cycles(ui, 2)
        ui.resolve_approval(True, "first in, first out")
        ta.join(timeout=5.0)
        assert not ta.is_alive(), "selector-less resolve missed the oldest cycle"
        assert box_a["approved"] is True
        assert tb.is_alive(), "selector-less resolve hit more than one cycle"


def test_resolve_all_approvals_wakes_every_gate() -> None:
    """The cancel/close sweep: every parked gate wakes with its own
    denied result."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, box_a = spawn(_pending_item("a-1"))
        tb, box_b = spawn(_pending_item("b-1"))
        _wait_for_cycles(ui, 2)
        assert ui.resolve_all_approvals(False, "Cancelled by user") == 2
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)
        assert box_a["approved"] is False
        assert box_b["approved"] is False
        assert "Cancelled by user" in (box_a["feedback"] or "")


def test_resolve_all_approvals_noop_when_idle() -> None:
    """Idle cancels stay silent — no stale approval_resolved broadcast."""
    ui = _make_ui()
    lq = ui._register_listener()
    assert ui.resolve_all_approvals(False, "Cancelled by user") == 0
    assert lq.empty()


def test_double_resolution_is_a_guarded_noop() -> None:
    """A second decision racing the first (two tabs, or timeout racing a
    click) must not re-resolve, re-broadcast, or clobber the recorded
    result."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, box_a = spawn(_pending_item("a-1"))
        _wait_for_cycles(ui, 1)
        first = ui.resolve_approval(True, "yes", call_id="a-1")
        second = ui.resolve_approval(False, "no", call_id="a-1")
        assert first is not None
        assert second is None
        ta.join(timeout=5.0)
        assert box_a == {"approved": True, "feedback": "yes"}


def test_pending_cards_and_legacy_view_track_cycles() -> None:
    """``pending_approval_cards`` lists every live cycle's card (SSE
    replay repaints them all); the legacy ``_pending_approval`` view
    tracks the OLDEST for boolean-ish consumers and rolls forward as
    cycles resolve."""
    ui = _make_ui()
    with _gate_harness(ui) as spawn:
        ta, _box_a = spawn(_pending_item("a-1"))
        _wait_for_cycles(ui, 1)
        spawn(_pending_item("b-1"))
        _wait_for_cycles(ui, 2)
        cards = ui.pending_approval_cards()
        assert [c["items"][0]["call_id"] for c in cards] == ["a-1", "b-1"]
        assert ui._pending_approval is not None
        assert ui._pending_approval["items"][0]["call_id"] == "a-1"
        ui.resolve_approval(True, None, call_id="a-1")
        ta.join(timeout=5.0)
        # View rolls forward to the surviving cycle.
        assert ui._pending_approval is not None
        assert ui._pending_approval["items"][0]["call_id"] == "b-1"


def test_stale_generation_verdict_cannot_touch_live_cycle() -> None:
    """A prior turn's run-to-completion daemon delivering a reused
    call_id must not satisfy the NEW cycle's wait: the delivery's
    generation (its cancel event) is identity-checked against the
    owning cycle's — mismatch persists for audit only, with no cache
    write, no SSE, no park."""
    storage = MagicMock()
    ui = _make_ui()
    fresh_gen = threading.Event()
    stale_gen = threading.Event()
    cycle = _register_cycle(ui, ["c-reused"], judge_event=fresh_gen)
    lq = ui._register_listener()
    with _patch_get_storage(storage):
        ui.on_intent_verdict(
            {"verdict_id": "v-stale", "call_id": "c-reused", "tier": "llm"},
            judge_event=stale_gen,
        )
    assert "c-reused" not in ui._llm_verdicts
    assert cycle.pending_verdicts == []
    assert lq.empty()
    kwargs = storage.upsert_intent_verdict.call_args.kwargs
    assert kwargs["user_decision"] == "superseded"
    # The cycle's OWN generation delivers normally.
    with _patch_get_storage(storage):
        ui.on_intent_verdict(
            {"verdict_id": "v-fresh", "call_id": "c-reused", "tier": "llm"},
            judge_event=fresh_gen,
        )
    assert ui._llm_verdicts["c-reused"]["verdict_id"] == "v-fresh"
    assert cycle.pending_verdicts and cycle.pending_verdicts[0]["verdict_id"] == "v-fresh"


def test_smart_approval_rejects_stale_origin_verdict() -> None:
    """Smart-Approvals qualification requires the cached verdict to have
    been delivered by THIS batch's judge generation — a cached approve
    of unknown/stale origin sends the batch to a human."""
    ui = _smart_ui()
    ui.smart_approval_wait_seconds = 0.05
    fresh_gen = threading.Event()
    item = _pending_item("c1")
    item["_judge_event"] = fresh_gen
    ui._llm_verdicts["c1"] = _llm_verdict("c1", recommendation="approve", confidence=0.99)
    ui._verdict_origins["c1"] = id(object())  # a different generation delivered it
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == [item]
    assert item.get("auto_approved") is not True
    # Same verdict with the RIGHT origin qualifies.
    ui._verdict_origins["c1"] = id(fresh_gen)
    with _patch_get_storage(MagicMock()):
        remaining = ui._apply_smart_approvals([item])
    assert remaining == []
    assert item["auto_approved"] is True


def test_concurrent_smart_gate_and_human_gate() -> None:
    """A smart-qualifying batch auto-approves while a sibling batch is
    parked on a human — the sibling's cycle survives untouched (the old
    whole-cache reset at gate entry wiped its verdicts mid-wait)."""
    ui = _make_ui()
    ui.smart_approvals_enabled = True
    ui.smart_approval_threshold = 0.95
    ui.smart_approval_wait_seconds = 1.0
    # Regression guard: if the smart batch ever falls through to the
    # human gate (it runs on THIS thread), fail in seconds instead of
    # hanging the suite for the full approval timeout.
    ui._APPROVAL_WAIT_TIMEOUT = 10.0
    with _gate_harness(ui) as spawn:
        # Human-gated sibling parks first.
        ta, box_a = spawn(_pending_item("a-1"))
        _wait_for_cycles(ui, 1)
        # Smart-qualifying batch flows straight through on this thread —
        # its verdict was delivered by its OWN generation before the
        # gate was entered, so the entry purge must spare it.
        gen = threading.Event()
        item = _pending_item("s-1")
        item["_judge_event"] = gen
        ui._llm_verdicts["s-1"] = _llm_verdict("s-1", recommendation="approve", confidence=0.99)
        ui._verdict_origins["s-1"] = id(gen)
        approved, _ = ui.approve_tools([item])
        assert approved is True
        assert item["auto_approved"] is True
        # Sibling still parked, its cycle + verdict path intact.
        assert ta.is_alive()
        ui.resolve_approval(True, "ok", call_id="a-1")
        ta.join(timeout=5.0)
        assert box_a["approved"] is True


def test_purge_round_verdicts_keeps_entry_from_the_entering_generation() -> None:
    """``keep_origin``: a verdict the entering batch's OWN judge spawn
    already delivered survives the entry purge.  The judge daemon is
    spawned before the gate is entered, so a fast judge can beat the
    gate to the cache — evicting its verdict as if it were a prior
    round's leftover stalled the Smart-Approvals wait to its full
    budget and sent an already-cleared batch to a human.  Foreign
    generations and prior-round decisions still purge."""
    ui = _make_ui()
    gen = threading.Event()
    other_gen = threading.Event()
    ui._llm_verdicts["c-own"] = {"verdict_id": "own"}
    ui._verdict_origins["c-own"] = id(gen)
    ui._llm_verdicts["c-foreign"] = {"verdict_id": "foreign"}
    ui._verdict_origins["c-foreign"] = id(other_gen)
    ui._recent_decisions["c-own"] = ("approved", None)
    ui._purge_round_verdicts({"c-own", "c-foreign"}, keep_origin=gen)
    assert ui._llm_verdicts.get("c-own") == {"verdict_id": "own"}
    assert ui._verdict_origins.get("c-own") == id(gen)
    assert "c-foreign" not in ui._llm_verdicts
    assert "c-foreign" not in ui._verdict_origins
    # Decisions never survive: this round has not been decided yet.
    assert "c-own" not in ui._recent_decisions


def test_smart_gate_uses_verdict_delivered_before_gate_entry() -> None:
    """Production shape of the generation-aware purge: judge spawned
    before the gate, verdict delivered before ``approve_tools`` runs.
    The entry purge spares the same-generation verdict, so the smart
    wait sees it immediately and the batch auto-approves without a
    human prompt or a full-budget stall."""
    ui = _smart_ui()
    ui.smart_approval_wait_seconds = 3.0
    # Regression guard: a purged verdict sends this batch to the human
    # gate on THIS thread — bound the park so the test fails instead of
    # hanging the suite.
    ui._APPROVAL_WAIT_TIMEOUT = 1.0
    gen = threading.Event()
    item = _pending_item("s-1")
    item["_judge_event"] = gen
    with _patch_get_storage(MagicMock()), _patch_policies({}):
        # The "fast judge": delivery lands before the gate is entered.
        ui.on_intent_verdict(_llm_verdict("s-1"), judge_event=gen)
        approved, _feedback = ui.approve_tools([item])
    assert approved is True, "entry purge evicted this batch's own pre-delivered verdict"
    assert item["auto_approved"] is True


def test_registration_evicts_stale_generation_window_arrival() -> None:
    """A STALE generation delivering into the purge→register window
    (the entry purge can't see arrivals that land during the policy
    round-trip or the smart wait) must not blank the card's "judge
    analysing" cue, be adopted into ``pending_verdicts`` for
    decision-stamping, or linger in the replay cache once the cycle
    registers."""
    ui = _SeedingUI(ws_id="ws-1", user_id="u1")
    stale_gen = threading.Event()
    fresh_gen = threading.Event()
    # _SeedingUI re-delivers right after the entry purge — inside the
    # purge→register window — tagged with the STALE generation.
    ui.seed_verdicts = [_llm_verdict("w-1")]
    ui.seed_judge_event = stale_gen
    item = _pending_item("w-1")
    item["_judge_event"] = fresh_gen
    with _gate_harness(ui) as spawn:
        _t, box = spawn(item)
        _wait_for_cycles(ui, 1)
        with ui._ws_lock:
            cycle = next(iter(ui._approval_cycles.values()))
        assert "w-1" not in ui._llm_verdicts, "stale window arrival survived registration"
        assert "w-1" not in ui._verdict_origins
        assert cycle.card["judge_pending"] is True, "stale verdict blanked the judge cue"
        assert [v["verdict_id"] for v in cycle.pending_verdicts] == ["h-w-1"], (
            "stale window arrival was adopted for decision-stamping"
        )
        ui.resolve_approval(True, None, call_id="w-1")
    assert box["approved"] is True


def test_late_stale_generation_verdict_stamps_superseded() -> None:
    """A late verdict from generation A delivering AFTER its round
    resolved — and after a reused call_id's round from generation B
    also resolved — must not steal B's recorded decision.  Recorded
    decisions are generation-tagged: a mismatched late delivery stamps
    ``superseded`` (same vocabulary as the superseded persist path); a
    same-generation late delivery still stamps the real decision."""
    storage = MagicMock()
    ui = _make_ui()
    gen_a = threading.Event()
    gen_b = threading.Event()
    # Round B (reusing the call_id generation A once judged) resolves
    # and its gate unregisters the cycle — the decision survives only
    # in ``_recent_decisions``, tagged with B's generation.
    cycle_b = _register_cycle(ui, ["c-reuse"], judge_event=gen_b)
    with _patch_get_storage(storage):
        ui.resolve_approval(True, None, call_id="c-reuse")
    ui._unregister_approval_cycle(cycle_b)
    assert ui._recent_decisions["c-reuse"] == ("approved", gen_b)
    # Generation A's run-to-completion daemon delivers late — no live
    # owner, and the decision on file belongs to B.
    with _patch_get_storage(storage):
        ui.on_intent_verdict(
            {"verdict_id": "v-stale-late", "call_id": "c-reuse", "tier": "llm"},
            judge_event=gen_a,
        )
    storage.update_intent_verdict.assert_any_call("v-stale-late", user_decision="superseded")
    # B's own late delivery still stamps B's real decision.
    with _patch_get_storage(storage):
        ui.on_intent_verdict(
            {"verdict_id": "v-b-late", "call_id": "c-reuse", "tier": "llm"},
            judge_event=gen_b,
        )
    storage.update_intent_verdict.assert_any_call("v-b-late", user_decision="approved")
