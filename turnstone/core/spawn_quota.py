"""Spawn-rate / spawn-budget controls for coordinator sessions.

Two complementary knobs layered on top of ``spawn_workstream`` /
``spawn_batch``:

- :class:`SpawnBudget` — hard cap on the number of *concurrently
  active* children a coordinator can own.  A cheap in-memory struct;
  the authoritative active count lives in storage and the caller
  passes it in via :meth:`SpawnBudget.check`.  Keeping the count out
  of this class keeps the budget trivial to unit-test without a
  storage fixture.
- :class:`TokenBucket` — soft pacing of spawn attempts.  Refills at a
  configurable per-minute rate up to a burst ceiling.  ``acquire()``
  either consumes a token or returns a ``retry_after_seconds`` hint
  the model can include in its next planning turn.

Both live on the coordinator's :class:`ChatSession` (one pair per
session) and share a tiny ``threading.Lock`` each — the approval layer
can interleave with SSE observers mid-prepare, so the mutators must
not race with the reads.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetCheck:
    """Result of :meth:`SpawnBudget.check`.

    ``allowed`` is True when ``active + cost <= budget``; ``remaining``
    is clamped at zero so the caller can safely do
    ``min(remaining, len(batch))`` to size a partial-success batch.
    """

    allowed: bool
    active: int
    budget: int
    remaining: int


class SpawnBudget:
    """Thread-safe hard cap on concurrent active children."""

    def __init__(self, budget: int) -> None:
        self._budget = max(0, int(budget))
        self._lock = threading.Lock()

    @property
    def budget(self) -> int:
        with self._lock:
            return self._budget

    def set_budget(self, budget: int) -> None:
        with self._lock:
            self._budget = max(0, int(budget))

    def check(self, active: int) -> BudgetCheck:
        """Return whether one more spawn fits given the current active count.

        Does NOT mutate state — batch callers are expected to re-check
        per item with their running in-batch tally added to ``active``
        (a single multi-spawn ``cost`` kwarg would atomically fast-fail
        the whole tail; add it back if that semantic lands).
        """
        with self._lock:
            b = self._budget
        clamped = max(0, active)
        remaining = max(0, b - clamped)
        return BudgetCheck(
            allowed=(clamped + 1) <= b,
            active=clamped,
            budget=b,
            remaining=remaining,
        )


@dataclass(frozen=True)
class BucketAcquire:
    """Result of :meth:`TokenBucket.acquire`.

    On ``allowed=True`` the token has already been consumed; on
    ``allowed=False`` ``retry_after_seconds`` is the wall-clock delay
    until enough tokens exist to satisfy the call (or ``inf`` when
    the refill rate is zero).
    """

    allowed: bool
    retry_after_seconds: float
    tokens_remaining: float


class TokenBucket:
    """Classic token bucket — tokens refill at ``tokens_per_minute`` up to ``burst``.

    Starts full so a fresh session gets its full burst immediately;
    the rate limit exists to pace a *runaway*, not to speed-bump the
    first handful of spawns.
    """

    def __init__(self, tokens_per_minute: float, burst: int) -> None:
        self._per_sec = max(0.0, float(tokens_per_minute)) / 60.0
        self._burst = max(1, int(burst))
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def tokens_per_minute(self) -> float:
        with self._lock:
            return self._per_sec * 60.0

    @property
    def burst(self) -> int:
        with self._lock:
            return self._burst

    @property
    def tokens(self) -> float:
        """Current token count, post-refill.  Intended for admin-read only."""
        now = time.monotonic()
        with self._lock:
            self._refill_locked(now)
            return self._tokens

    def set_rate(self, tokens_per_minute: float, burst: int) -> None:
        """Mutate the refill rate and/or burst ceiling.

        The current token count is clamped DOWN to the new burst — a
        widened burst doesn't retroactively grant free tokens to a
        bucket that was sitting full.  A narrowed burst takes effect
        immediately.
        """
        now = time.monotonic()
        with self._lock:
            self._refill_locked(now)
            self._per_sec = max(0.0, float(tokens_per_minute)) / 60.0
            self._burst = max(1, int(burst))
            if self._tokens > self._burst:
                self._tokens = float(self._burst)

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._per_sec)
        self._last_refill = now

    def acquire(self, *, cost: float = 1.0) -> BucketAcquire:
        """Try to consume ``cost`` tokens.

        Returns ``(True, 0.0, remaining)`` on success; on failure
        returns ``(False, retry_after, current)`` where
        ``retry_after`` is the time until enough tokens exist.  When
        ``tokens_per_minute == 0`` a failing acquire reports
        ``retry_after == inf`` — the caller should surface that as
        "rate limit disabled; change the setting" rather than a
        real retry hint.
        """
        now = time.monotonic()
        with self._lock:
            self._refill_locked(now)
            if self._tokens >= cost:
                self._tokens -= cost
                return BucketAcquire(
                    allowed=True,
                    retry_after_seconds=0.0,
                    tokens_remaining=self._tokens,
                )
            if self._per_sec <= 0:
                return BucketAcquire(
                    allowed=False,
                    retry_after_seconds=float("inf"),
                    tokens_remaining=self._tokens,
                )
            deficit = cost - self._tokens
            retry_after = deficit / self._per_sec
            return BucketAcquire(
                allowed=False,
                retry_after_seconds=retry_after,
                tokens_remaining=self._tokens,
            )
