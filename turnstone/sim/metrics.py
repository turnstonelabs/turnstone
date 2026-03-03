"""Simulation metrics collection."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class MetricsCollector:
    """Thread-safe metrics collector for simulation runs.

    Uses ``threading.Lock`` so it works from both sync and async contexts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn_latencies: list[float] = []
        self._inject_times: list[float] = []
        self._complete_times: list[float] = []
        self._errors: int = 0
        self._error_details: list[tuple[float, str, str]] = []
        self._node_kills: list[tuple[float, str]] = []
        self._turns_per_node: dict[str, int] = defaultdict(int)
        self._ws_counts: list[dict[str, int]] = []  # utilization snapshots

    def record_turn(self, ws_id: str, node_id: str, latency: float) -> None:
        with self._lock:
            self._turn_latencies.append(latency)
            self._complete_times.append(time.monotonic())
            self._turns_per_node[node_id] += 1

    def record_inject(self) -> None:
        with self._lock:
            self._inject_times.append(time.monotonic())

    def record_error(self, node_id: str, message: str) -> None:
        with self._lock:
            self._errors += 1
            self._error_details.append((time.monotonic(), node_id, message))

    def record_node_kill(self, node_id: str) -> None:
        with self._lock:
            self._node_kills.append((time.monotonic(), node_id))

    def snapshot_utilization(self, ws_counts: dict[str, int]) -> None:
        """Record workstream-per-node counts at a point in time."""
        with self._lock:
            self._ws_counts.append(dict(ws_counts))

    def summary(self) -> dict[str, Any]:
        """Generate final metrics report with percentiles and aggregates."""
        with self._lock:
            latencies = sorted(self._turn_latencies)
            n = len(latencies)

            if n > 0 and self._inject_times and self._complete_times:
                duration = self._complete_times[-1] - self._inject_times[0]
            else:
                duration = 0.0

            # Utilization from latest snapshot
            util: dict[str, Any] = {}
            if self._ws_counts:
                last = self._ws_counts[-1]
                counts = list(last.values())
                if counts:
                    util = {
                        "mean_ws_per_node": sum(counts) / len(counts),
                        "max_ws_per_node": max(counts),
                        "nodes_with_zero_ws": sum(1 for c in counts if c == 0),
                    }

            return {
                "total_turns": n,
                "total_errors": self._errors,
                "duration_seconds": round(duration, 2),
                "throughput": {
                    "messages_per_sec": round(
                        len(self._inject_times) / duration,
                        2,
                    )
                    if duration > 0
                    else 0,
                    "turns_per_sec": round(
                        n / duration,
                        2,
                    )
                    if duration > 0
                    else 0,
                },
                "latency": {
                    "p50": _percentile(latencies, 0.50),
                    "p90": _percentile(latencies, 0.90),
                    "p99": _percentile(latencies, 0.99),
                    "mean": round(sum(latencies) / n, 4) if n else 0,
                    "max": round(latencies[-1], 4) if n else 0,
                },
                "utilization": util,
                "node_kills": len(self._node_kills),
                "turns_per_node": dict(self._turns_per_node),
            }


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * pct)
    idx = min(idx, len(sorted_values) - 1)
    return round(sorted_values[idx], 4)
