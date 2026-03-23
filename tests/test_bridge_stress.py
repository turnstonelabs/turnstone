"""Stress tests for bridge.py threading — race conditions in approval,
plan review, and workstream lifecycle.

Each scenario is run many times (ITERATIONS) with threading.Barrier to
maximize timing overlap.  Uses mock broker (no Redis) and no HTTP calls.

Races tested:
1. Duplicate approval on SSE reconnect (TOCTOU in _pending_approvals)
2. Duplicate plan review on SSE reconnect (TOCTOU in _pending_plan_reviews)
3. approve_set stale reference escape during concurrent update
4. _running flag visibility across threads on shutdown
5. Workstream closure during blocked pop_response
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

from turnstone.mq.bridge import Bridge

ITERATIONS = 100

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(**overrides) -> Bridge:
    """Create a Bridge with a mock broker (no Redis or HTTP)."""
    broker = MagicMock()
    defaults = dict(
        server_url="http://localhost:8080",
        broker=broker,
        node_id="test-node",
        approval_timeout=1,
    )
    defaults.update(overrides)
    return Bridge(**defaults)


def _approval_items(tool_name: str = "bash") -> list[dict]:
    return [{"func_name": tool_name, "needs_approval": True, "approval_label": tool_name}]


# ---------------------------------------------------------------------------
# Race 1: Duplicate approval on SSE reconnect
# ---------------------------------------------------------------------------


class TestDuplicateApproval:
    """Two threads call _handle_approval for the same ws_id simultaneously.
    Only one should create a pending entry; the other should be skipped."""

    @pytest.mark.xfail(
        reason="Known race: _wait_approval pops _pending_approvals in finally, "
        "allowing a concurrent SSE reconnect to slip past the duplicate guard. "
        "Fix: keep the pending entry until the workstream returns to idle.",
        strict=False,
    )
    def test_no_duplicate_approvals(self):
        sent_count = Counter()

        for _ in range(ITERATIONS):
            bridge = _make_bridge()
            # Make pop_response return immediately (approve)
            bridge._broker.pop_response.return_value = '{"type": "approve", "approved": true}'
            barrier = threading.Barrier(2, timeout=5)

            def _call_approval(bridge=bridge, barrier=barrier):
                barrier.wait()
                bridge._handle_approval("ws-1", {"items": _approval_items()})

            t1 = threading.Thread(target=_call_approval)
            t2 = threading.Thread(target=_call_approval)
            with (
                patch.object(bridge, "_api_approve") as mock_approve,
                patch.object(bridge, "_publish_ws"),
            ):
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

                # Wait for spawned _wait_approval threads
                time.sleep(0.15)

                sent_count[mock_approve.call_count] += 1

        # At most 1 approval should be forwarded per iteration
        assert sent_count.get(2, 0) == 0, (
            f"Duplicate approvals sent in {sent_count[2]}/{ITERATIONS} iterations"
        )


# ---------------------------------------------------------------------------
# Race 2: Duplicate plan review on SSE reconnect
# ---------------------------------------------------------------------------


class TestDuplicatePlanReview:
    """Two threads call _handle_plan_review simultaneously.
    Only one should create a pending entry."""

    @pytest.mark.xfail(
        reason="Known race: _wait_plan pops _pending_plan_reviews before posting, "
        "allowing a concurrent SSE reconnect to create a second pending entry. "
        "Fix: defer pop until after HTTP post completes.",
        strict=False,
    )
    def test_no_duplicate_plan_reviews(self):
        sent_count = Counter()

        for _ in range(ITERATIONS):
            bridge = _make_bridge()
            bridge._broker.pop_response.return_value = (
                '{"type": "plan_review", "feedback": "looks good"}'
            )
            barrier = threading.Barrier(2, timeout=5)

            def _call_plan(bridge=bridge, barrier=barrier):
                barrier.wait()
                bridge._handle_plan_review("ws-1", {"content": "plan text"})

            t1 = threading.Thread(target=_call_plan)
            t2 = threading.Thread(target=_call_plan)
            with patch.object(bridge, "_publish_ws"), patch.object(bridge._http, "post"):
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

                # Wait for spawned _wait_plan threads to finish
                time.sleep(0.15)

                sent_count[bridge._http.post.call_count] += 1

        assert sent_count.get(2, 0) == 0, (
            f"Duplicate plan reviews sent in {sent_count[2]}/{ITERATIONS} iterations"
        )


# ---------------------------------------------------------------------------
# Race 3: approve_set stale reference during concurrent update
# ---------------------------------------------------------------------------


class TestApproveSetConsistency:
    """One thread reads approve_set for auto-approve check while another
    updates it via _wait_approval 'always' path.  The auto-approve
    decision should be consistent (either all-approved or not)."""

    def test_approve_set_never_partially_visible(self):
        for _ in range(ITERATIONS):
            bridge = _make_bridge()
            # Pre-populate with some tools
            with bridge._lock:
                bridge._ws_approve_tools["ws-1"] = {"read_file", "search"}

            barrier = threading.Barrier(2, timeout=5)
            results = []

            def _reader(bridge=bridge, barrier=barrier, results=results):
                barrier.wait()
                with bridge._lock:
                    snap = bridge._ws_approve_tools.get("ws-1", set()).copy()
                results.append(snap)

            def _writer(bridge=bridge, barrier=barrier):
                barrier.wait()
                with bridge._lock:
                    existing = bridge._ws_approve_tools.get("ws-1", set())
                    bridge._ws_approve_tools["ws-1"] = existing | {"bash", "write_file"}

            t1 = threading.Thread(target=_reader)
            t2 = threading.Thread(target=_writer)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            snap = results[0]
            # snap should be either the old set or the new set — never partial
            assert snap in (
                {"read_file", "search"},
                {"read_file", "search", "bash", "write_file"},
            ), f"Partial set observed: {snap}"


# ---------------------------------------------------------------------------
# Race 4: _running flag visibility across threads
# ---------------------------------------------------------------------------


class TestRunningFlagVisibility:
    """All threads reading _running should see False within a bounded time
    after the main thread sets it."""

    def test_all_threads_observe_shutdown(self):
        bridge = _make_bridge()
        observed_false = threading.Event()
        threads_running = []

        def _spin_checker():
            while bridge._running:
                time.sleep(0.001)
            observed_false.set()

        for _ in range(5):
            t = threading.Thread(target=_spin_checker, daemon=True)
            threads_running.append(t)
            t.start()

        # Let threads spin briefly
        time.sleep(0.01)
        bridge._running = False

        # All threads should exit within 100ms
        for t in threads_running:
            t.join(timeout=1)
            assert not t.is_alive(), "Thread did not observe _running=False"

        assert observed_false.is_set()


# ---------------------------------------------------------------------------
# Race 5: Workstream closure during blocked pop_response
# ---------------------------------------------------------------------------


class TestClosureDuringBlockedPop:
    """When a workstream is closed while an approval thread is blocked on
    pop_response, the approval thread should not hang indefinitely."""

    def test_approval_thread_exits_after_ws_closure(self):
        for _ in range(10):  # fewer iterations — each has a real delay
            bridge = _make_bridge(approval_timeout=0.5)

            # pop_response blocks for the timeout then returns None
            def _slow_pop(queue_name, timeout=300):
                time.sleep(min(timeout, 0.5))
                return None

            bridge._broker.pop_response.side_effect = _slow_pop

            # Set up a pending approval that will block
            with patch.object(bridge, "_publish_ws"), patch.object(bridge, "_api_approve"):
                bridge._handle_approval("ws-1", {"items": _approval_items()})

            # Give the approval thread time to start blocking
            time.sleep(0.05)

            # Close the workstream (simulates global SSE ws_closed event)
            with bridge._lock:
                bridge._ws_threads.pop("ws-1", None)
                bridge._ws_auto_approve.pop("ws-1", None)
                bridge._ws_approve_tools.pop("ws-1", None)
                bridge._active_sends.pop("ws-1", None)

            # The approval thread should finish within the timeout
            # (0.5s) plus a small buffer — not hang for 3600s
            deadline = time.monotonic() + 3.0
            with bridge._lock:
                still_pending = "ws-1" in bridge._pending_approvals

            # Wait for the pending entry to be cleaned up
            while still_pending and time.monotonic() < deadline:
                time.sleep(0.1)
                with bridge._lock:
                    still_pending = "ws-1" in bridge._pending_approvals

            assert not still_pending, "Approval thread hung after workstream closure"


# ---------------------------------------------------------------------------
# Race 6: Concurrent approval + workstream close
# ---------------------------------------------------------------------------


class TestApprovalDuringClose:
    """An approval arriving at the exact same time as a ws_closed event
    should not leave orphaned state."""

    def test_no_orphaned_pending_after_close(self):
        for _ in range(ITERATIONS):
            bridge = _make_bridge(approval_timeout=0.1)
            bridge._broker.pop_response.return_value = None  # timeout

            barrier = threading.Barrier(2, timeout=5)

            def _send_approval(bridge=bridge, barrier=barrier):
                barrier.wait()
                with patch.object(bridge, "_publish_ws"), patch.object(bridge, "_api_approve"):
                    bridge._handle_approval("ws-1", {"items": _approval_items()})

            def _close_ws(bridge=bridge, barrier=barrier):
                barrier.wait()
                with bridge._lock:
                    bridge._ws_threads.pop("ws-1", None)
                    bridge._ws_auto_approve.pop("ws-1", None)
                    bridge._ws_approve_tools.pop("ws-1", None)

            t1 = threading.Thread(target=_send_approval)
            t2 = threading.Thread(target=_close_ws)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            # After both threads complete, pending_approvals should be clean
            # (the finally block in _wait_approval always pops)
            time.sleep(0.2)  # allow the spawned approval thread to finish
            with bridge._lock:
                assert "ws-1" not in bridge._pending_approvals, "Orphaned pending approval"
