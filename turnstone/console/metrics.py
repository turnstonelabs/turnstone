"""Thread-safe Prometheus-compatible metrics collector for the console server."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class ConsoleMetrics:
    """Collects console routing and membership metrics in Prometheus text exposition format.

    Lighter-weight than the server's MetricsCollector — tracks router
    request counters and live-membership gauges.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._router_requests: dict[tuple[str, str], int] = defaultdict(int)
        self._router_duration_sum: dict[str, float] = defaultdict(float)
        self._router_duration_count: dict[str, int] = defaultdict(int)
        self._router_membership: int = 0
        self._router_refresh_count: int = 0
        self._start_time: float = time.monotonic()

    def record_route(self, method: str, status: int, duration: float) -> None:
        """Record a routed request with its status bucket and duration."""
        bucket = f"{status // 100}xx"
        with self._lock:
            self._router_requests[(method, bucket)] += 1
            self._router_duration_sum[method] += duration
            self._router_duration_count[method] += 1

    def set_router_info(self, membership: int, refresh_count: int) -> None:
        """Update current live-node count + the router's refresh counter."""
        with self._lock:
            self._router_membership = membership
            self._router_refresh_count = refresh_count

    def generate_text(self) -> str:
        """Return Prometheus text exposition format (v0.0.4)."""
        lines: list[str] = []

        with self._lock:
            router_requests = dict(self._router_requests)
            duration_sum = dict(self._router_duration_sum)
            duration_count = dict(self._router_duration_count)
            router_membership = self._router_membership
            router_refresh_count = self._router_refresh_count

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

        # turnstone_router_membership_size
        lines.append("# HELP turnstone_router_membership_size Current live-node count")
        lines.append("# TYPE turnstone_router_membership_size gauge")
        lines.append(f"turnstone_router_membership_size {router_membership}")

        # turnstone_router_refresh_total — bumped on every successful
        # cache refresh.  A flat counter under churn means the
        # collector's discovery loop is stuck.
        lines.append("# HELP turnstone_router_refresh_total Router cache refresh counter")
        lines.append("# TYPE turnstone_router_refresh_total counter")
        lines.append(f"turnstone_router_refresh_total {router_refresh_count}")

        lines.append("")  # trailing newline
        return "\n".join(lines)


def _fmt_value(v: float) -> str:
    if isinstance(v, int):
        return str(v)
    return f"{v:.6g}"
