"""Tests for turnstone.core.healthcheck — backend health monitor with circuit breaker."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

from turnstone.core.healthcheck import BackendHealthMonitor, CircuitState

# ---------------------------------------------------------------------------
# CircuitState enum
# ---------------------------------------------------------------------------


class TestCircuitState:
    def test_closed(self) -> None:
        assert CircuitState.CLOSED.value == "closed"

    def test_open(self) -> None:
        assert CircuitState.OPEN.value == "open"

    def test_half_open(self) -> None:
        assert CircuitState.HALF_OPEN.value == "half_open"


# ---------------------------------------------------------------------------
# BackendHealthMonitor
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.models.list.return_value.data = [MagicMock(id="test-model")]
    return client


@pytest.fixture
def mock_metrics() -> Generator[MagicMock]:
    """Patch the metrics singleton so set_backend_status / set_circuit_state exist."""
    m = MagicMock()
    with (
        patch("turnstone.core.healthcheck.metrics", m, create=True),
        patch("turnstone.core.metrics.metrics", m, create=True),
    ):
        yield m


def _make_monitor(
    client: MagicMock,
    failure_threshold: int = 3,
    cooldown: float = 60.0,
) -> BackendHealthMonitor:
    return BackendHealthMonitor(
        client=client,
        probe_interval=1.0,
        probe_timeout=1.0,
        failure_threshold=failure_threshold,
        cooldown=cooldown,
    )


class TestBackendHealthMonitor:
    def test_starts_closed(self, mock_client: MagicMock) -> None:
        mon = _make_monitor(mock_client)
        assert mon.circuit_state == CircuitState.CLOSED
        assert mon.is_healthy is True

    def test_record_failure_increments(
        self, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """Failures below threshold do not open the circuit."""
        mon = _make_monitor(mock_client, failure_threshold=5)
        for _ in range(4):
            mon.record_failure()
        assert mon.circuit_state == CircuitState.CLOSED

    def test_opens_after_threshold(self, mock_client: MagicMock, mock_metrics: MagicMock) -> None:
        mon = _make_monitor(mock_client, failure_threshold=3)
        for _ in range(3):
            mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN
        assert mon.is_healthy is False

    def test_should_reject_when_open(self, mock_client: MagicMock, mock_metrics: MagicMock) -> None:
        mon = _make_monitor(mock_client, failure_threshold=1, cooldown=9999.0)
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN
        assert mon.acquire_request_permit() is False

    @patch("turnstone.core.healthcheck.time")
    def test_half_open_after_cooldown(
        self, mock_time: MagicMock, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """After cooldown elapses, should_allow_request transitions to HALF_OPEN."""
        t = 1000.0
        mock_time.monotonic.return_value = t

        mon = _make_monitor(mock_client, failure_threshold=1, cooldown=60.0)
        # Override _last_state_change to use our mocked time
        mon._last_state_change = t
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

        # Advance past cooldown
        mock_time.monotonic.return_value = t + 61.0
        assert mon.acquire_request_permit() is True
        assert mon.circuit_state == CircuitState.HALF_OPEN  # type: ignore[comparison-overlap]

    def test_success_resets(self, mock_client: MagicMock, mock_metrics: MagicMock) -> None:
        """record_success resets failures and closes circuit from any state."""
        mon = _make_monitor(mock_client, failure_threshold=1)
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

        mon.record_success()
        assert mon.circuit_state == CircuitState.CLOSED  # type: ignore[comparison-overlap]
        assert mon.is_healthy is True
        # Internal counter should be reset
        assert mon._consecutive_failures == 0

    def test_should_allow_when_closed(self, mock_client: MagicMock) -> None:
        mon = _make_monitor(mock_client)
        assert mon.acquire_request_permit() is True

    def test_half_open_allows_only_one_request(
        self, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """HALF_OPEN permits exactly one probe; subsequent callers are blocked."""
        mon = _make_monitor(mock_client, failure_threshold=1)
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

        # Force into HALF_OPEN with permit
        with mon._lock:
            mon._state = CircuitState.HALF_OPEN
            mon._half_open_permit = True

        # First caller gets through
        assert mon.acquire_request_permit() is True
        # Second caller is blocked
        assert mon.acquire_request_permit() is False
        # Third caller is also blocked
        assert mon.acquire_request_permit() is False

    def test_half_open_success_reopens_to_all(
        self, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """After probe succeeds in HALF_OPEN, circuit closes and all requests pass."""
        mon = _make_monitor(mock_client, failure_threshold=1)
        mon.record_failure()
        with mon._lock:
            mon._state = CircuitState.HALF_OPEN
            mon._half_open_permit = False  # permit already consumed

        # Probe succeeds
        mon.record_success()
        assert mon.circuit_state == CircuitState.CLOSED  # type: ignore[comparison-overlap]
        # All callers pass now
        assert mon.acquire_request_permit() is True
        assert mon.acquire_request_permit() is True

    def test_half_open_failure_blocks_all(
        self, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """After probe fails in HALF_OPEN, circuit reopens and all requests blocked."""
        mon = _make_monitor(mock_client, failure_threshold=1, cooldown=9999.0)
        mon.record_failure()
        with mon._lock:
            mon._state = CircuitState.HALF_OPEN
            mon._half_open_permit = False

        # Probe fails
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN
        assert mon.acquire_request_permit() is False

    def test_half_open_failure_reopens(
        self, mock_client: MagicMock, mock_metrics: MagicMock
    ) -> None:
        """A failure in HALF_OPEN re-opens the circuit immediately."""
        mon = _make_monitor(mock_client, failure_threshold=1)
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

        # Force into HALF_OPEN
        with mon._lock:
            mon._state = CircuitState.HALF_OPEN
            mon._update_metrics()

        # Another failure should reopen
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

    def test_probe_success_closes(self, mock_client: MagicMock, mock_metrics: MagicMock) -> None:
        """A successful probe closes the circuit."""
        mon = _make_monitor(mock_client, failure_threshold=1)
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN

        # Simulate probe success
        assert mon._probe_once() is True
        mon.record_success()
        assert mon.circuit_state == CircuitState.CLOSED  # type: ignore[comparison-overlap]

    def test_probe_failure_opens(self, mock_client: MagicMock, mock_metrics: MagicMock) -> None:
        """Enough probe failures open the circuit."""
        mock_client.with_options.return_value.models.list.side_effect = ConnectionError("down")
        mon = _make_monitor(mock_client, failure_threshold=2)

        assert mon._probe_once() is False
        mon.record_failure()
        assert mon.circuit_state == CircuitState.CLOSED  # only 1 failure

        assert mon._probe_once() is False
        mon.record_failure()
        assert mon.circuit_state == CircuitState.OPEN  # type: ignore[comparison-overlap]

    def test_stop_thread(self, mock_client: MagicMock) -> None:
        """stop() signals the probe loop to exit."""
        mon = _make_monitor(mock_client)
        mon.start()
        assert mon._thread is not None
        assert mon._thread.is_alive()

        mon.stop()
        mon._thread.join(timeout=3.0)
        assert not mon._thread.is_alive()
