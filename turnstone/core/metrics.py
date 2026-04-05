"""Thread-safe Prometheus-compatible metrics collector for the turnstone web server."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class MetricsCollector:
    """Collects server metrics and generates Prometheus text exposition format."""

    BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_time = time.monotonic()
        self.model: str = ""
        # counters
        self._req_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._tokens: dict[str, int] = defaultdict(int)  # "prompt"|"completion" -> int
        self._messages: int = 0
        self._tool_calls: dict[str, int] = defaultdict(int)  # tool_name -> int
        self._errors: int = 0
        # histograms: (method, endpoint) -> {buckets: [count…], sum: float, count: int}
        self._req_duration: dict[tuple[str, str], dict[str, Any]] = {}
        # gauge
        self._context_ratio: float = 0.0
        self._sse_connections: int = 0  # gauge: active SSE connections
        self._backend_up: bool = True  # gauge: 1 if up, 0 if down
        # counters (continued)
        self._ratelimit_rejects: int = 0  # counter: total 429 responses
        self._evictions: int = 0  # counter: workstreams evicted
        # judge metrics
        self._judge_verdicts: dict[tuple[str, str], int] = defaultdict(int)
        self._judge_latency: dict[str, Any] = {
            "buckets": [0] * len(self.BUCKETS),
            "sum": 0.0,
            "count": 0,
        }
        self._judge_enabled: bool = False

    def record_request(self, method: str, endpoint: str, status: int, duration: float) -> None:
        with self._lock:
            self._req_total[(method, endpoint, str(status))] += 1
            key = (method, endpoint)
            if key not in self._req_duration:
                self._req_duration[key] = {
                    "buckets": [0] * len(self.BUCKETS),
                    "sum": 0.0,
                    "count": 0,
                }
            h = self._req_duration[key]
            for i, b in enumerate(self.BUCKETS):
                if duration <= b:
                    h["buckets"][i] += 1
            h["sum"] += duration
            h["count"] += 1

    def record_tokens(self, prompt: int, completion: int) -> None:
        with self._lock:
            self._tokens["prompt"] += prompt
            self._tokens["completion"] += completion

    def record_cache_tokens(self, cache_creation: int, cache_read: int) -> None:
        with self._lock:
            self._tokens["cache_creation"] += cache_creation
            self._tokens["cache_read"] += cache_read

    def record_tool_call(self, tool_name: str) -> None:
        with self._lock:
            self._tool_calls[tool_name] += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def record_message_sent(self) -> None:
        with self._lock:
            self._messages += 1

    def record_context_ratio(self, ratio: float) -> None:
        with self._lock:
            self._context_ratio = ratio

    def record_sse_connect(self) -> None:
        with self._lock:
            self._sse_connections += 1

    def record_sse_disconnect(self) -> None:
        with self._lock:
            self._sse_connections = max(0, self._sse_connections - 1)

    def record_ratelimit_reject(self) -> None:
        with self._lock:
            self._ratelimit_rejects += 1

    def set_backend_status(self, up: bool) -> None:
        with self._lock:
            self._backend_up = up

    def record_eviction(self) -> None:
        with self._lock:
            self._evictions += 1

    def set_judge_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._judge_enabled = enabled

    def record_judge_verdict(self, tier: str, risk_level: str, latency_ms: int) -> None:
        """Record an intent validation verdict."""
        with self._lock:
            self._judge_verdicts[(tier, risk_level)] += 1
            # Track LLM latency separately (heuristic is sub-ms, not interesting)
            if tier == "llm":
                seconds = latency_ms / 1000.0
                for i, b in enumerate(self.BUCKETS):
                    if seconds <= b:
                        self._judge_latency["buckets"][i] += 1
                self._judge_latency["sum"] += seconds
                self._judge_latency["count"] += 1

    def generate_text(
        self,
        workstream_states: dict[str, int],
        total_workstreams: int,
        workstream_metrics: list[dict[str, Any]] | None = None,
        mcp_info: dict[str, int] | None = None,
    ) -> str:
        """Return Prometheus text exposition format (v0.0.4)."""
        lines: list[str] = []

        def gauge(
            name: str,
            help_text: str,
            value: float | int,
            labels: dict[str, str] | None = None,
        ) -> None:
            lstr = _fmt_labels(labels)
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{lstr} {_fmt_value(value)}")

        def counter(
            name: str,
            help_text: str,
            value: float | int,
            labels: dict[str, str] | None = None,
        ) -> None:
            lstr = _fmt_labels(labels)
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{lstr} {_fmt_value(value)}")

        with self._lock:
            uptime = time.monotonic() - self.start_time
            model = self.model
            req_total = dict(self._req_total)
            tokens = dict(self._tokens)
            messages = self._messages
            tool_calls = dict(self._tool_calls)
            errors = self._errors
            req_duration = {k: dict(v) for k, v in self._req_duration.items()}
            context_ratio = self._context_ratio
            sse_connections = self._sse_connections
            ratelimit_rejects = self._ratelimit_rejects
            backend_up = self._backend_up
            evictions = self._evictions
            judge_verdicts = dict(self._judge_verdicts)
            judge_latency = dict(self._judge_latency)
            judge_enabled = self._judge_enabled

        # turnstone_build_info
        lines.append("# HELP turnstone_build_info Server version and model info")
        lines.append("# TYPE turnstone_build_info gauge")
        from turnstone import __version__

        lines.append(f'turnstone_build_info{{version="{__version__}",model="{model}"}} 1')

        # turnstone_uptime_seconds
        gauge("turnstone_uptime_seconds", "Server uptime in seconds", uptime)

        # turnstone_workstreams_active_total
        gauge(
            "turnstone_workstreams_active_total",
            "Number of active workstreams",
            total_workstreams,
        )

        # turnstone_workstreams_by_state
        lines.append("# HELP turnstone_workstreams_by_state Workstreams grouped by state")
        lines.append("# TYPE turnstone_workstreams_by_state gauge")
        for state, count in sorted(workstream_states.items()):
            lines.append(f'turnstone_workstreams_by_state{{state="{state}"}} {count}')

        # turnstone_http_requests_total
        lines.append("# HELP turnstone_http_requests_total Total HTTP requests handled")
        lines.append("# TYPE turnstone_http_requests_total counter")
        for (method, endpoint, status), count in sorted(req_total.items()):
            lines.append(
                f'turnstone_http_requests_total{{method="{method}",'
                f'endpoint="{endpoint}",status_code="{status}"}} {count}'
            )

        # turnstone_http_request_duration_seconds (histogram)
        lines.append(
            "# HELP turnstone_http_request_duration_seconds HTTP request duration in seconds"
        )
        lines.append("# TYPE turnstone_http_request_duration_seconds histogram")
        for (method, endpoint), h in sorted(req_duration.items()):
            prefix = (
                f'turnstone_http_request_duration_seconds{{method="{method}",endpoint="{endpoint}"'
            )
            for i, b in enumerate(self.BUCKETS):
                lines.append(f'{prefix},le="{b}"}} {h["buckets"][i]}')
            lines.append(f'{prefix},le="+Inf"}} {h["count"]}')
            lines.append(
                f'turnstone_http_request_duration_seconds_sum{{method="{method}",'
                f'endpoint="{endpoint}"}} {_fmt_value(h["sum"])}'
            )
            lines.append(
                f'turnstone_http_request_duration_seconds_count{{method="{method}",'
                f'endpoint="{endpoint}"}} {h["count"]}'
            )

        # turnstone_messages_sent_total
        counter("turnstone_messages_sent_total", "Total user messages sent to AI", messages)

        # turnstone_tokens_total
        lines.append("# HELP turnstone_tokens_total Total tokens consumed")
        lines.append("# TYPE turnstone_tokens_total counter")
        for tok_type in ("prompt", "completion", "cache_creation", "cache_read"):
            lines.append(f'turnstone_tokens_total{{type="{tok_type}"}} {tokens.get(tok_type, 0)}')

        # turnstone_tool_calls_total
        lines.append("# HELP turnstone_tool_calls_total Total tool executions by name")
        lines.append("# TYPE turnstone_tool_calls_total counter")
        for tool, count in sorted(tool_calls.items()):
            lines.append(f'turnstone_tool_calls_total{{tool="{tool}"}} {count}')

        # turnstone_errors_total
        counter("turnstone_errors_total", "Total errors reported by workstreams", errors)

        # turnstone_context_window_used_ratio
        gauge(
            "turnstone_context_window_used_ratio",
            "Fraction of context window currently used (0.0 - 1.0)",
            context_ratio,
        )

        # turnstone_sse_connections_active
        gauge(
            "turnstone_sse_connections_active",
            "Number of active SSE connections",
            sse_connections,
        )

        # turnstone_ratelimit_rejected_total
        counter(
            "turnstone_ratelimit_rejected_total",
            "Total requests rejected by rate limiter",
            ratelimit_rejects,
        )

        # turnstone_backend_up
        gauge(
            "turnstone_backend_up",
            "Whether the LLM backend is reachable (1=up, 0=down)",
            1 if backend_up else 0,
        )

        # turnstone_workstreams_evicted_total
        counter(
            "turnstone_workstreams_evicted_total",
            "Total workstreams evicted to make room for new ones",
            evictions,
        )

        # turnstone_judge_enabled
        gauge(
            "turnstone_judge_enabled",
            "Whether intent validation judge is enabled (1=on, 0=off)",
            1 if judge_enabled else 0,
        )

        # turnstone_judge_verdicts_total
        if judge_verdicts:
            lines.append("# HELP turnstone_judge_verdicts_total Total intent validation verdicts")
            lines.append("# TYPE turnstone_judge_verdicts_total counter")
            for (tier, risk), cnt in sorted(judge_verdicts.items()):
                lines.append(
                    f'turnstone_judge_verdicts_total{{tier="{tier}",risk_level="{risk}"}} {cnt}'
                )

        # turnstone_judge_llm_latency_seconds (histogram)
        if judge_latency["count"] > 0:
            lines.append(
                "# HELP turnstone_judge_llm_latency_seconds LLM judge evaluation latency in seconds"
            )
            lines.append("# TYPE turnstone_judge_llm_latency_seconds histogram")
            for i, b in enumerate(self.BUCKETS):
                lines.append(
                    f'turnstone_judge_llm_latency_seconds{{le="{b}"}} {judge_latency["buckets"][i]}'
                )
            lines.append(
                f'turnstone_judge_llm_latency_seconds{{le="+Inf"}} {judge_latency["count"]}'
            )
            lines.append(
                f"turnstone_judge_llm_latency_seconds_sum {_fmt_value(judge_latency['sum'])}"
            )
            lines.append(f"turnstone_judge_llm_latency_seconds_count {judge_latency['count']}")

        # Per-workstream metrics (only when data is provided)
        if workstream_metrics:
            lines.append("# HELP turnstone_workstream_info Workstream metadata")
            lines.append("# TYPE turnstone_workstream_info gauge")
            for wm in workstream_metrics:
                lstr = _fmt_labels(
                    {
                        "ws_id": wm["ws_id"],
                        "name": wm["name"],
                    }
                )
                lines.append(f"turnstone_workstream_info{lstr} 1")

            lines.append(
                "# HELP turnstone_workstream_prompt_tokens_total"
                " Prompt tokens consumed per workstream (lifetime of workstream)"
            )
            lines.append("# TYPE turnstone_workstream_prompt_tokens_total counter")
            for wm in workstream_metrics:
                lstr = _fmt_labels({"ws_id": wm["ws_id"], "name": wm["name"]})
                lines.append(
                    f"turnstone_workstream_prompt_tokens_total{lstr} {wm['prompt_tokens']}"
                )

            lines.append(
                "# HELP turnstone_workstream_completion_tokens_total"
                " Completion tokens generated per workstream (lifetime of workstream)"
            )
            lines.append("# TYPE turnstone_workstream_completion_tokens_total counter")
            for wm in workstream_metrics:
                lstr = _fmt_labels({"ws_id": wm["ws_id"], "name": wm["name"]})
                lines.append(
                    f"turnstone_workstream_completion_tokens_total{lstr} {wm['completion_tokens']}"
                )

            lines.append(
                "# HELP turnstone_workstream_messages_total"
                " User messages sent per workstream (lifetime of workstream)"
            )
            lines.append("# TYPE turnstone_workstream_messages_total counter")
            for wm in workstream_metrics:
                lstr = _fmt_labels({"ws_id": wm["ws_id"], "name": wm["name"]})
                lines.append(f"turnstone_workstream_messages_total{lstr} {wm['messages']}")

            lines.append(
                "# HELP turnstone_workstream_tool_calls_total"
                " Tool executions per workstream per tool (lifetime of workstream)"
            )
            lines.append("# TYPE turnstone_workstream_tool_calls_total counter")
            for wm in workstream_metrics:
                for tool, cnt in sorted(wm["tool_calls"].items()):
                    lstr = _fmt_labels({"ws_id": wm["ws_id"], "name": wm["name"], "tool": tool})
                    lines.append(f"turnstone_workstream_tool_calls_total{lstr} {cnt}")

            lines.append(
                "# HELP turnstone_workstream_context_ratio"
                " Current context window utilisation per workstream (0.0-1.0)"
            )
            lines.append("# TYPE turnstone_workstream_context_ratio gauge")
            for wm in workstream_metrics:
                lstr = _fmt_labels({"ws_id": wm["ws_id"], "name": wm["name"]})
                lines.append(
                    f"turnstone_workstream_context_ratio{lstr} {_fmt_value(wm['context_ratio'])}"
                )

        # MCP gauges (optional)
        if mcp_info:
            gauge(
                "turnstone_mcp_servers",
                "Number of connected MCP servers",
                mcp_info.get("servers", 0),
            )
            gauge(
                "turnstone_mcp_resources",
                "Number of MCP resources available",
                mcp_info.get("resources", 0),
            )
            gauge(
                "turnstone_mcp_prompts",
                "Number of MCP prompts available",
                mcp_info.get("prompts", 0),
            )
            gauge(
                "turnstone_mcp_server_errors",
                "Number of MCP servers currently in error state",
                mcp_info.get("errors", 0),
            )

        lines.append("")  # trailing newline
        return "\n".join(lines)


def _fmt_labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def _fmt_value(v: float) -> str:
    if isinstance(v, int):
        return str(v)
    # Use full precision but strip trailing zeros
    return f"{v:.6g}"


# Module-level metrics instance — shared across all requests and WebUI instances.
metrics = MetricsCollector()
