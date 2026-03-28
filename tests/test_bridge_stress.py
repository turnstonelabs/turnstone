"""Stress tests for bridge.py threading — race conditions in approval,
plan review, and workstream lifecycle.

Each scenario is run many times (ITERATIONS) with threading.Barrier to
maximize timing overlap.  Uses mock broker (no Redis) and no HTTP calls.

Races tested:
1. Duplicate approval on SSE reconnect (TOCTOU in _pending_approvals)
2. Duplicate plan review on SSE reconnect (TOCTOU in _pending_plan_reviews)
3. approve_set stale reference escape during concurrent update
4. _running flag visibility across threads on shutdown
5. Approval thread exits within bounded time after timeout
6. Concurrent approval + workstream close leaves no orphaned state
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from unittest.mock import MagicMock, patch

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
    bridge = Bridge(**defaults)
    # Replace real httpx client with a mock so daemon threads spawned by
    # _handle_approval / _handle_plan_review don't make real HTTP calls
    # after the test's patch context exits.
    bridge._http.close()
    bridge._http = MagicMock()
    return bridge


def _approval_items(tool_name: str = "bash") -> list[dict]:
    return [{"func_name": tool_name, "needs_approval": True, "approval_label": tool_name}]


def _wait_pending_resolved(bridge: Bridge, key: str, attr: str, deadline_s: float = 3.0) -> bool:
    """Poll until the pending entry is resolved (tombstone) or absent."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        with bridge._lock:
            entries = getattr(bridge, attr)
            if key not in entries:
                return True
            _, resolved_at = entries[key]
            if resolved_at > 0:
                return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Race 1: Duplicate approval on SSE reconnect
# ---------------------------------------------------------------------------


class TestDuplicateApproval:
    """Two threads call _handle_approval for the same ws_id simultaneously.
    Only one should create a pending entry; the other should be skipped."""

    def test_no_duplicate_approvals(self):
        sent_count = Counter()

        for _ in range(ITERATIONS):
            bridge = _make_bridge()
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
                assert not t1.is_alive(), "Thread 1 hung"
                assert not t2.is_alive(), "Thread 2 hung"

                # Wait for spawned _wait_approval threads to resolve
                _wait_pending_resolved(bridge, "ws-1", "_pending_approvals")

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

    def test_no_duplicate_plan_reviews(self):
        sent_count = Counter()

        for _ in range(ITERATIONS):
            bridge = _make_bridge()
            bridge._broker.pop_response.return_value = (
                '{"type": "plan_feedback", "feedback": "looks good"}'
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
                assert not t1.is_alive(), "Thread 1 hung"
                assert not t2.is_alive(), "Thread 2 hung"

                # Wait for spawned _wait_plan threads to resolve
                _wait_pending_resolved(bridge, "ws-1", "_pending_plan_reviews")

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
            assert not t1.is_alive(), "Reader hung"
            assert not t2.is_alive(), "Writer hung"

            snap = results[0]
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

        time.sleep(0.01)
        bridge._running = False

        for t in threads_running:
            t.join(timeout=1)
            assert not t.is_alive(), "Thread did not observe _running=False"

        assert observed_false.is_set()


# ---------------------------------------------------------------------------
# Race 5: Approval thread exits within bounded time
# ---------------------------------------------------------------------------


class TestApprovalThreadTimeout:
    """An approval thread blocked on pop_response should exit within the
    configured approval_timeout, not hang indefinitely."""

    def test_approval_thread_exits_within_timeout(self):
        for _ in range(10):
            bridge = _make_bridge(approval_timeout=0.5)

            def _slow_pop(queue_name, timeout=300):
                time.sleep(min(timeout, 0.5))
                return None

            bridge._broker.pop_response.side_effect = _slow_pop

            with patch.object(bridge, "_publish_ws"), patch.object(bridge, "_api_approve"):
                bridge._handle_approval("ws-1", {"items": _approval_items()})

            # The pending entry should be resolved within the timeout
            resolved = _wait_pending_resolved(bridge, "ws-1", "_pending_approvals", deadline_s=3.0)
            assert resolved, "Approval thread did not exit within expected timeout"


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
                with (
                    patch.object(bridge, "_publish_global"),
                    patch.object(bridge, "_publish_cluster"),
                ):
                    bridge._handle_global_event({"type": "ws_closed", "ws_id": "ws-1"})

            t1 = threading.Thread(target=_send_approval)
            t2 = threading.Thread(target=_close_ws)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
            assert not t1.is_alive(), "Approval thread hung"
            assert not t2.is_alive(), "Close thread hung"

            # Wait for spawned _wait_approval thread to resolve (if close
            # didn't remove the entry first)
            resolved = _wait_pending_resolved(bridge, "ws-1", "_pending_approvals")
            assert resolved, "Orphaned pending approval"


# ---------------------------------------------------------------------------
# Race 7: Plan review refinement loop (tombstone → cleanup → re-entry)
# ---------------------------------------------------------------------------


class TestPlanReviewRefinementLoop:
    """After a plan review is resolved, a ws_state event should clean up the
    tombstone so the refinement-loop plan_review event is handled correctly."""

    def test_refinement_loop_allows_reentry(self):
        for _ in range(ITERATIONS):
            bridge = _make_bridge()
            bridge._broker.pop_response.return_value = (
                '{"type": "plan_feedback", "feedback": "refine this"}'
            )

            # Step 1: first plan review — creates pending entry, resolves it
            with patch.object(bridge, "_publish_ws"), patch.object(bridge._http, "post"):
                bridge._handle_plan_review("ws-1", {"content": "plan v1"})

            _wait_pending_resolved(bridge, "ws-1", "_pending_plan_reviews")

            # Verify tombstone is present (resolved_at > 0)
            with bridge._lock:
                assert "ws-1" in bridge._pending_plan_reviews
                assert bridge._pending_plan_reviews["ws-1"][1] > 0

            # Step 2: ws_state event cleans up the resolved tombstone
            with (
                patch.object(bridge, "_publish_ws"),
                patch.object(bridge, "_publish_global"),
                patch.object(bridge, "_publish_cluster"),
            ):
                bridge._handle_global_event(
                    {"type": "ws_state", "ws_id": "ws-1", "state": "working"}
                )

            with bridge._lock:
                assert "ws-1" not in bridge._pending_plan_reviews

            # Step 3: refinement plan_review arrives — should create new entry
            with patch.object(bridge, "_publish_ws"), patch.object(bridge._http, "post"):
                bridge._handle_plan_review("ws-1", {"content": "plan v2"})

            _wait_pending_resolved(bridge, "ws-1", "_pending_plan_reviews")

            with bridge._lock:
                assert "ws-1" in bridge._pending_plan_reviews
