"""Background LLM backend health monitor with circuit breaker."""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

log = logging.getLogger(__name__)


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BackendHealthMonitor:
    """Monitors LLM backend health via periodic probes and passive failure tracking.

    Circuit breaker state machine:
      CLOSED  -- backend responding, all requests pass
      OPEN    -- backend unreachable, fast-fail for cooldown period
      HALF_OPEN -- cooldown expired, next probe decides
    """

    def __init__(
        self,
        client: OpenAI,
        probe_interval: float = 30.0,
        probe_timeout: float = 5.0,
        failure_threshold: int = 5,
        cooldown: float = 60.0,
    ) -> None:
        self._client = client
        self._probe_interval = probe_interval
        self._probe_timeout = probe_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_state_change = time.monotonic()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start background probe daemon thread."""
        self._thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the probe thread to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Passive tracking (called by request path)
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """Called on successful LLM call.  Resets failure count, closes circuit."""
        with self._lock:
            self._consecutive_failures = 0
            if self._state != CircuitState.CLOSED:
                self._state = CircuitState.CLOSED
                self._last_state_change = time.monotonic()
                self._update_metrics()

    def record_failure(self) -> None:
        """Called on LLM call failure.  May open circuit."""
        with self._lock:
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed in HALF_OPEN — re-open immediately
                self._state = CircuitState.OPEN
                self._last_state_change = time.monotonic()
                log.warning("Circuit breaker OPEN: probe failed in HALF_OPEN")
                self._update_metrics()
            elif (
                self._state == CircuitState.CLOSED
                and self._consecutive_failures >= self._failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._last_state_change = time.monotonic()
                log.warning(
                    "Circuit breaker OPEN: %d consecutive failures",
                    self._consecutive_failures,
                )
                self._update_metrics()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return self._state == CircuitState.CLOSED

    @property
    def circuit_state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def should_allow_request(self) -> bool:
        """False only if circuit is OPEN (fast-fail).  HALF_OPEN allows requests through."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if cooldown has elapsed -> transition to HALF_OPEN
                if (time.monotonic() - self._last_state_change) >= self._cooldown:
                    self._state = CircuitState.HALF_OPEN
                    self._last_state_change = time.monotonic()
                    log.info("Circuit breaker HALF_OPEN: cooldown elapsed")
                    self._update_metrics()
                    return True
                return False
            return True

    # ------------------------------------------------------------------
    # Background probe
    # ------------------------------------------------------------------

    def _probe_loop(self) -> None:
        """Background: probe backend every interval."""
        while not self._stop_event.is_set():
            self._stop_event.wait(self._probe_interval)
            if self._stop_event.is_set():
                break
            success = self._probe_once()
            if success:
                self.record_success()
            else:
                self.record_failure()

    def _probe_once(self) -> bool:
        """Single probe: call ``client.models.list()``.  Returns True on success."""
        try:
            self._client.with_options(timeout=self._probe_timeout).models.list()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _update_metrics(self) -> None:
        """Push state to metrics collector.  Called with *self._lock* held."""
        from turnstone.core.metrics import metrics

        metrics.set_backend_status(self._state == CircuitState.CLOSED)
        state_int = {
            CircuitState.CLOSED: 0,
            CircuitState.OPEN: 1,
            CircuitState.HALF_OPEN: 2,
        }
        metrics.set_circuit_state(state_int[self._state])
