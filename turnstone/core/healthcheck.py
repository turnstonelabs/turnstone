"""Background LLM backend health monitor with circuit breaker."""

from __future__ import annotations

import enum
import threading
import time
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import OpenAI

log = get_logger(__name__)


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
        *,
        provider: str = "openai",
        initial_model: str = "",
        on_model_changed: Callable[[str, int | None], None] | None = None,
        on_state_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._probe_interval = probe_interval
        self._probe_timeout = probe_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown

        # Model change detection
        self._provider = provider
        self._last_detected_model = initial_model
        self._on_model_changed = on_model_changed
        self._on_state_changed = on_state_changed

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_state_change = time.monotonic()
        # Set True on OPEN→HALF_OPEN; consumed by first acquire_request_permit() call
        self._half_open_permit = False

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

    def _fire_state_callback(self, state_val: str | None) -> None:
        """Fire on_state_changed callback outside the lock."""
        if state_val is not None and self._on_state_changed is not None:
            try:
                self._on_state_changed(state_val)
            except Exception:
                log.debug("on_state_changed callback error", exc_info=True)

    def record_success(self) -> None:
        """Called on successful LLM call.  Resets failure count, closes circuit."""
        state_to_dispatch: str | None = None
        with self._lock:
            self._consecutive_failures = 0
            if self._state != CircuitState.CLOSED:
                prev = self._state
                self._state = CircuitState.CLOSED
                self._half_open_permit = False
                self._last_state_change = time.monotonic()
                log.info("Circuit breaker CLOSED (was %s): backend recovered", prev.value)
                self._update_metrics()
                state_to_dispatch = self._state.value
        self._fire_state_callback(state_to_dispatch)

    def record_failure(self) -> None:
        """Called on LLM call failure.  May open circuit."""
        state_to_dispatch: str | None = None
        with self._lock:
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed in HALF_OPEN — re-open immediately
                self._state = CircuitState.OPEN
                self._half_open_permit = False
                self._last_state_change = time.monotonic()
                log.warning("Circuit breaker OPEN: probe failed in HALF_OPEN")
                self._update_metrics()
                state_to_dispatch = self._state.value
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
                state_to_dispatch = self._state.value
        self._fire_state_callback(state_to_dispatch)

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

    def acquire_request_permit(self) -> bool:
        """Consume one request permit if available.

        Returns True when the caller may proceed.  In HALF_OPEN, only one probe
        request is allowed — subsequent callers are blocked until the probe
        completes (via ``record_success`` or ``record_failure``).
        """
        with self._lock:
            if self._state == CircuitState.OPEN:
                if (time.monotonic() - self._last_state_change) >= self._cooldown:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_permit = False  # consumed by this caller
                    self._last_state_change = time.monotonic()
                    log.info("Circuit breaker HALF_OPEN: cooldown elapsed, one probe permitted")
                    self._update_metrics()
                    return True  # this caller is the probe
                return False
            if self._state == CircuitState.HALF_OPEN:
                # Only one probe request allowed; subsequent callers block
                if self._half_open_permit:
                    self._half_open_permit = False
                    return True
                return False
            return True  # CLOSED

    # ------------------------------------------------------------------
    # Background probe
    # ------------------------------------------------------------------

    def _probe_loop(self) -> None:
        """Background: probe backend every interval.

        An initial jitter (derived from the PID) staggers probes across
        cluster nodes so they don't all hit the LLM backend at once.
        """
        import os

        # Deterministic per-process jitter: spread across half the interval
        jitter = ((os.getpid() * 2654435761) & 0x7FFFFFFF) / 0x7FFFFFFF * (self._probe_interval / 2)
        self._stop_event.wait(jitter)
        while not self._stop_event.is_set():
            self._stop_event.wait(self._probe_interval)
            if self._stop_event.is_set():
                break
            # When circuit is OPEN, only probe after cooldown expires.
            with self._lock:
                if self._state == CircuitState.OPEN:
                    elapsed = time.monotonic() - self._last_state_change
                    remaining = self._cooldown - elapsed
                    if remaining > 0:
                        # Wait precisely for cooldown rather than skipping
                        # a full probe_interval (which could overshoot).
                        self._lock.release()
                        try:
                            self._stop_event.wait(remaining)
                        finally:
                            self._lock.acquire()
                        if self._stop_event.is_set():
                            break
                    # Transition to HALF_OPEN for the probe.  The background
                    # probe itself is the single HALF_OPEN request — keep
                    # _half_open_permit False so concurrent user requests
                    # are blocked until the probe completes.
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_permit = False
                    self._last_state_change = time.monotonic()
                    log.info("Circuit breaker HALF_OPEN: cooldown elapsed, probing")
                    self._update_metrics()
            success = self._probe_once()
            if success:
                self.record_success()
            else:
                self.record_failure()

    def _probe_once(self) -> bool:
        """Single probe: call ``client.models.list()``.  Returns True on success."""
        try:
            resp = self._client.with_options(timeout=self._probe_timeout).models.list()
            self._check_model_change(resp)
            return True
        except Exception:
            return False

    def _check_model_change(self, resp: Any) -> None:
        """Compare detected model against last known and fire callback if changed."""
        if not self._on_model_changed or not resp.data:
            return
        try:
            from turnstone.core.model_registry import (
                _extract_context_window,
                _select_best_model,
            )

            all_ids = [m.id for m in resp.data]
            selected = _select_best_model(all_ids, self._provider)
            if selected == self._last_detected_model:
                return
            model_obj = next((m for m in resp.data if m.id == selected), None)
            ctx = _extract_context_window(model_obj, self._provider) if model_obj else None
            log.info(
                "Backend model changed: %s -> %s (ctx=%s)",
                self._last_detected_model,
                selected,
                ctx,
            )
            self._last_detected_model = selected
            self._on_model_changed(selected, ctx)
        except Exception:
            log.debug("Model change check failed", exc_info=True)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _update_metrics(self) -> None:
        """Push state to metrics collector.  Called with *self._lock* held.

        Also schedules the ``on_state_changed`` callback to fire *after* the
        lock is released (via ``record_success``/``record_failure`` callers).
        We capture the state string here so the callback receives it.
        """
        from turnstone.core.metrics import metrics

        metrics.set_backend_status(self._state == CircuitState.CLOSED)
        state_int = {
            CircuitState.CLOSED: 0,
            CircuitState.OPEN: 1,
            CircuitState.HALF_OPEN: 2,
        }
        metrics.set_circuit_state(state_int[self._state])
