"""Per-backend health tracking via passive success/failure recording.

No active probing or circuit breakers — backends are marked *degraded*
after a configurable number of consecutive failures and recover
automatically when a request succeeds.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-backend health tracker
# ---------------------------------------------------------------------------


class BackendHealthTracker:
    """Tracks LLM backend health via passive success/failure recording.

    State machine::

        healthy  --(N consecutive failures)--> degraded
        degraded --(any success)-------------> healthy

    Requests are **never blocked** — the degraded flag is advisory
    (used for observability and fallback ordering).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        on_state_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._on_state_changed = on_state_changed

        self._lock = threading.Lock()
        self._degraded = False
        self._consecutive_failures = 0

    # -- passive tracking ----------------------------------------------------

    def _fire_state_callback(self, state_val: str | None) -> None:
        """Fire on_state_changed callback outside the lock."""
        if state_val is not None and self._on_state_changed is not None:
            try:
                self._on_state_changed(state_val)
            except Exception:
                log.debug("on_state_changed callback error", exc_info=True)

    def record_success(self) -> None:
        """Called on successful LLM call. Clears degraded state."""
        state_to_dispatch: str | None = None
        with self._lock:
            self._consecutive_failures = 0
            if self._degraded:
                self._degraded = False
                log.info("Backend recovered (was degraded)")
                state_to_dispatch = "healthy"
        self._fire_state_callback(state_to_dispatch)

    def record_failure(self) -> None:
        """Called on LLM call failure. May mark backend as degraded."""
        state_to_dispatch: str | None = None
        with self._lock:
            self._consecutive_failures += 1
            if not self._degraded and self._consecutive_failures >= self._failure_threshold:
                self._degraded = True
                log.warning(
                    "Backend degraded: %d consecutive failures",
                    self._consecutive_failures,
                )
                state_to_dispatch = "degraded"
        self._fire_state_callback(state_to_dispatch)

    # -- query helpers -------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return not self._degraded

    @property
    def is_degraded(self) -> bool:
        with self._lock:
            return self._degraded

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures


# ---------------------------------------------------------------------------
# Per-backend health tracker registry
# ---------------------------------------------------------------------------


class HealthTrackerRegistry:
    """Manages per-backend health trackers keyed by ``(provider, base_url)``.

    Two model aliases that point at the same backend share a single
    :class:`BackendHealthTracker`.  Aliases on different backends get
    independent trackers.

    Thread-safe.  Trackers are created eagerly at startup (or on model
    reload) — never lazily from the request path.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        on_state_changed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        # callback(backend_key_str, state_value)
        self._on_state_changed = on_state_changed
        self._trackers: dict[tuple[str, str], BackendHealthTracker] = {}
        self._lock = threading.Lock()

    # -- key helpers ---------------------------------------------------------

    @staticmethod
    def backend_key(provider: str, base_url: str) -> tuple[str, str]:
        """Normalize a ``(provider, base_url)`` pair for use as a dict key."""
        return (provider, base_url.rstrip("/"))

    # -- tracker lifecycle ---------------------------------------------------

    def get_tracker(
        self,
        provider: str,
        base_url: str,
    ) -> BackendHealthTracker:
        """Get or create a tracker for the given backend.  Thread-safe."""
        key = self.backend_key(provider, base_url)
        with self._lock:
            if key not in self._trackers:
                outer = self._on_state_changed

                def _state_cb(state: str, _k: tuple[str, str] = key) -> None:
                    if outer:
                        outer(f"{_k[0]}:{_k[1]}", state)

                tracker = BackendHealthTracker(
                    failure_threshold=self._failure_threshold,
                    on_state_changed=_state_cb,
                )
                self._trackers[key] = tracker
                log.info("Health tracker created for backend %s:%s", key[0], key[1])
            return self._trackers[key]

    def get_tracker_for_alias(
        self,
        registry: Any,
        alias: str,
    ) -> BackendHealthTracker | None:
        """Look up the tracker for a model alias, if one exists.

        Returns ``None`` if the alias is unknown or no tracker has been
        created for its backend yet.
        """
        try:
            cfg = registry.get_config(alias)
        except (ValueError, KeyError):
            return None
        key = self.backend_key(cfg.provider, cfg.base_url)
        with self._lock:
            return self._trackers.get(key)
