"""Shared result types and exceptions for the turnstone SDK."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnResult:
    """Aggregated result of a send_and_wait call.

    Collects content, reasoning, tool results, and errors from
    an HTTP/SSE event stream into a single result object.
    """

    ws_id: str = ""
    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.errors


class TurnstoneAPIError(Exception):
    """Raised when a server returns a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")
