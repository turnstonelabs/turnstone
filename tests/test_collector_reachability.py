"""Collector reachability-transition semantics.

Regression cover for the observability gap where the collector logged TLS /
connection failures at DEBUG, so a persistent mTLS-verify failure was invisible
at the default log level. ``_mark_unreachable`` now reports the first
(reachable→unreachable) transition so the SSE loop can log it at WARNING and
stay quiet on subsequent retries.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from turnstone.console.collector import ClusterCollector, NodeSnapshot


def _collector() -> ClusterCollector:
    return ClusterCollector(storage=MagicMock())


def test_first_failure_is_a_transition_then_quiet():
    c = _collector()
    c._nodes["node-1"] = NodeSnapshot(node_id="node-1", reachable=True)

    # First failure flips reachable→unreachable → True (log at WARNING).
    assert c._mark_unreachable("node-1", reason="SSLCertVerificationError") is True
    assert c._nodes["node-1"].reachable is False
    assert c._nodes["node-1"].reachable_reason == "SSLCertVerificationError"

    # Still-down retries are not transitions → False (stay at DEBUG).
    assert c._mark_unreachable("node-1", reason="SSLCertVerificationError") is False


def test_recovery_then_failure_is_a_new_transition():
    c = _collector()
    c._nodes["node-1"] = NodeSnapshot(node_id="node-1", reachable=True)
    c._mark_unreachable("node-1", reason="ConnectError")

    # Node comes back (as _apply_snapshot does), then fails again → new transition.
    c._nodes["node-1"].reachable = True
    assert c._mark_unreachable("node-1", reason="ConnectError") is True


def test_unknown_node_is_not_a_transition():
    c = _collector()
    assert c._mark_unreachable("ghost", reason="ConnectError") is False
