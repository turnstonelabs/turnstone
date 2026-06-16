"""Tests for turnstone.console.server._validate_regex_pattern.

The catastrophic-backtracking branch is verified by simulating the deadline
firing rather than running a real ReDoS regex — a genuine runaway pattern would
leave a CPU-pinned daemon worker for the rest of the suite.  The daemon-abandon
mechanism itself is covered in tests/test_deadline.py.
"""

from __future__ import annotations

from turnstone.console.server import _validate_regex_pattern
from turnstone.core.deadline import DeadlineExceededError


def test_valid_pattern_returns_none() -> None:
    assert _validate_regex_pattern(r"\d{3}-\d{4}") is None


def test_invalid_pattern_returns_error() -> None:
    msg = _validate_regex_pattern(r"(unclosed")
    assert msg is not None
    assert msg.startswith("Invalid regex")


def test_catastrophic_backtracking_returns_message(monkeypatch) -> None:
    def _deadline(*_args, **_kwargs):
        raise DeadlineExceededError

    monkeypatch.setattr("turnstone.console.server.run_with_deadline", _deadline)
    assert _validate_regex_pattern(r"(a+)+$") == "Regex appears to have catastrophic backtracking"


def test_probe_error_returns_generic_message(monkeypatch) -> None:
    def _err(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("turnstone.console.server.run_with_deadline", _err)
    assert _validate_regex_pattern(r"abc") == "Regex caused an error during test"
