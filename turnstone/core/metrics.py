"""Thread-safe Prometheus-compatible metrics collector for the turnstone web server."""

import threading
import time
from collections import defaultdict


class MetricsCollector:
    """Collects server metrics and generates Prometheus text exposition format."""

    BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.monotonic()
        self.model: str = ""
        # counters
        self._req_total: dict = defaultdict(int)  # (method, endpoint, status) -> int
        self._tokens: dict = defaultdict(int)  # ("prompt"|"completion") -> int
        self._messages: int = 0
        self._tool_calls: dict = defaultdict(int)  # tool_name -> int
        self._errors: int = 0
        # histograms: (method, endpoint) -> {buckets: [count…], sum: float, count: int}
        self._req_duration: dict = {}
        # gauge
        self._context_ratio: float = 0.0

    def record_request(self, method: str, endpoint: str, status: int, duration: float):
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

    def record_tokens(self, prompt: int, completion: int):
        with self._lock:
            self._tokens["prompt"] += prompt
            self._tokens["completion"] += completion

    def record_tool_call(self, tool_name: str):
        with self._lock:
            self._tool_calls[tool_name] += 1

    def record_error(self):
        with self._lock:
            self._errors += 1

    def record_message_sent(self):
        with self._lock:
            self._messages += 1

    def record_context_ratio(self, ratio: float):
        with self._lock:
            self._context_ratio = ratio

    def generate_text(
        self,
        workstream_states: dict,
        total_workstreams: int,
        workstream_metrics: list[dict] | None = None,
    ) -> str:
        """Return Prometheus text exposition format (v0.0.4)."""
        lines: list[str] = []

        def gauge(name, help_text, value, labels=None):
            lstr = _fmt_labels(labels)
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{lstr} {_fmt_value(value)}")

        def counter(name, help_text, value, labels=None):
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

        # turnstone_build_info
        lines.append("# HELP turnstone_build_info Server version and model info")
        lines.append("# TYPE turnstone_build_info gauge")
        lines.append(f'turnstone_build_info{{version="0.2.0",model="{model}"}} 1')

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
                f'turnstone_http_request_duration_seconds{{method="{method}",'
                f'endpoint="{endpoint}"'
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
        for tok_type in ("prompt", "completion"):
            lines.append(
                f'turnstone_tokens_total{{type="{tok_type}"}} {tokens.get(tok_type, 0)}'
            )

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

        # Per-workstream metrics (only when data is provided)
        if workstream_metrics:
            # turnstone_workstream_info — exposes session_id as a label for joining,
            # without propagating that high-cardinality label to counters.
            lines.append(
                "# HELP turnstone_workstream_info Workstream metadata"
                " (join on session_id for per-session queries)"
            )
            lines.append("# TYPE turnstone_workstream_info gauge")
            for wm in workstream_metrics:
                lstr = _fmt_labels(
                    {
                        "ws_id": wm["ws_id"],
                        "name": wm["name"],
                        "session_id": wm["session_id"],
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
                    f"turnstone_workstream_completion_tokens_total{lstr}"
                    f" {wm['completion_tokens']}"
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
                    lstr = _fmt_labels(
                        {"ws_id": wm["ws_id"], "name": wm["name"], "tool": tool}
                    )
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

        lines.append("")  # trailing newline
        return "\n".join(lines)


def _fmt_labels(labels: dict | None) -> str:
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
