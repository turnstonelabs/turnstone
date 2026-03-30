"""Thread-safe Prometheus-compatible metrics collector for the console server."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class ConsoleMetrics:
    """Collects console routing and ring metrics in Prometheus text exposition format.

    Lighter-weight than the server's MetricsCollector — tracks only router
    request counters, ring membership gauges, and rebalancer activity.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._router_requests: dict[tuple[str, str], int] = defaultdict(int)
        self._router_duration_sum: dict[str, float] = defaultdict(float)
        self._router_duration_count: dict[str, int] = defaultdict(int)
        self._ring_membership: int = 0
        self._ring_version: int = 0
        self._rebalance_total: dict[str, int] = defaultdict(int)
        self._migrations_total: int = 0
        self._start_time: float = time.monotonic()

    def record_route(self, method: str, status: int, duration: float) -> None:
        """Record a routed request with its status bucket and duration."""
        bucket = f"{status // 100}xx"
        with self._lock:
            self._router_requests[(method, bucket)] += 1
            self._router_duration_sum[method] += duration
            self._router_duration_count[method] += 1

    def set_ring_info(self, membership: int, version: int) -> None:
        """Update the current ring membership size and version."""
        with self._lock:
            self._ring_membership = membership
            self._ring_version = version

    def record_rebalance(self, result: str) -> None:
        """Record a rebalance pass outcome (noop/seeded/rebalanced)."""
        with self._lock:
            self._rebalance_total[result] += 1

    def record_migrations(self, count: int) -> None:
        """Record eager migration count from a rebalance pass."""
        with self._lock:
            self._migrations_total += count

    def generate_text(self) -> str:
        """Return Prometheus text exposition format (v0.0.4)."""
        lines: list[str] = []

        with self._lock:
            router_requests = dict(self._router_requests)
            duration_sum = dict(self._router_duration_sum)
            duration_count = dict(self._router_duration_count)
            ring_membership = self._ring_membership
            ring_version = self._ring_version
            rebalance_total = dict(self._rebalance_total)
            migrations_total = self._migrations_total

        # turnstone_router_requests_total
        lines.append("# HELP turnstone_router_requests_total Console-routed requests")
        lines.append("# TYPE turnstone_router_requests_total counter")
        for (method, status), count in sorted(router_requests.items()):
            lines.append(
                f'turnstone_router_requests_total{{method="{method}",status="{status}"}} {count}'
            )

        # turnstone_router_request_duration_seconds
        lines.append(
            "# HELP turnstone_router_request_duration_seconds Routing and proxy latency in seconds"
        )
        lines.append("# TYPE turnstone_router_request_duration_seconds summary")
        for method in sorted(duration_count):
            lines.append(
                f'turnstone_router_request_duration_seconds_sum{{method="{method}"}}'
                f" {_fmt_value(duration_sum[method])}"
            )
            lines.append(
                f'turnstone_router_request_duration_seconds_count{{method="{method}"}}'
                f" {duration_count[method]}"
            )

        # turnstone_ring_membership_size
        lines.append("# HELP turnstone_ring_membership_size Current ring node count")
        lines.append("# TYPE turnstone_ring_membership_size gauge")
        lines.append(f"turnstone_ring_membership_size {ring_membership}")

        # turnstone_ring_version
        lines.append("# HELP turnstone_ring_version Current ring version")
        lines.append("# TYPE turnstone_ring_version gauge")
        lines.append(f"turnstone_ring_version {ring_version}")

        # turnstone_ring_rebalance_total
        lines.append("# HELP turnstone_ring_rebalance_total Rebalancer runs by result")
        lines.append("# TYPE turnstone_ring_rebalance_total counter")
        for result, count in sorted(rebalance_total.items()):
            lines.append(f'turnstone_ring_rebalance_total{{result="{result}"}} {count}')

        # turnstone_ring_migrations_total
        lines.append("# HELP turnstone_ring_migrations_total Workstream migrations from rebalancer")
        lines.append("# TYPE turnstone_ring_migrations_total counter")
        lines.append(f"turnstone_ring_migrations_total {migrations_total}")

        lines.append("")  # trailing newline
        return "\n".join(lines)


def _fmt_value(v: float) -> str:
    if isinstance(v, int):
        return str(v)
    return f"{v:.6g}"
