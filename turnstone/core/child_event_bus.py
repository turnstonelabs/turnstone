"""Per-workstream wakeup primitive for in-process child state-change subscribers.

Retires the polling pattern in ``CoordinatorClient.wait_for_workstream``,
where the coord LLM's wait tool issued a storage snapshot every 0.5 s
regardless of whether anything had changed. The dispatch path
(:meth:`turnstone.console.coordinator_adapter.CoordinatorAdapter._dispatch_child_event`)
now calls :meth:`ChildEventBus.notify` after each translated child event;
waiters block on a per-call :class:`threading.Event` returned by
:meth:`register_waiter` and re-read storage only when an event fires or
the heartbeat cap expires.

Bus is in-process only. Cross-process / cross-node child events are
already merged into ``_dispatch_child_event`` via the cluster collector's
SSE multiplex before the bus sees them — there is no locality branching
in the bus itself.

Design constraints:

- Waiter primitive is :class:`threading.Event` because the wait tool runs
  on the coordinator's sync worker thread, not an asyncio loop.
- Concurrent ``register`` / ``unregister`` / ``notify`` is safe — a
  single ``threading.Lock`` guards the dict. ``Event.set`` itself is
  thread-safe and is called outside the lock so a slow waker can't block
  registration.
- ``notify`` with no subscribers is a no-op (the steady state — most
  state-change events fire while no wait tool is active).
- A waiter watching multiple ws_ids fires once on any of them; the
  caller's ``_snapshot_all`` re-read resolves which one changed.
- Empty per-ws_id buckets are popped on unregister so long-lived buses
  don't accumulate dead keys after wait churn.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class ChildEventBus:
    """Fan ``notify(child_ws_id)`` to every :class:`threading.Event`
    registered against that ws_id.

    Use :meth:`register_waiter` once per wait call to obtain an Event,
    then call :meth:`unregister_waiter` in a ``finally`` so a crash mid-
    wait doesn't leak the registration. The dispatch side calls
    :meth:`notify` on every translated child state-change event; an
    empty bucket is a cheap dict lookup + immediate return.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, set[threading.Event]] = {}
        self._lock = threading.Lock()

    def register_waiter(self, child_ws_ids: Iterable[str]) -> threading.Event:
        """Return a fresh Event registered against every listed ws_id.

        A wait on ``[A, B, C]`` returns a single Event that fires when
        *any* of A/B/C changes. The caller's snapshot re-read resolves
        which one. Empty / falsy ids are silently skipped — callers that
        clean their input upstream (e.g. ``wait_for_workstream``'s
        dedup + cap) don't need to filter again here.
        """
        event = threading.Event()
        with self._lock:
            for wid in child_ws_ids:
                if not wid:
                    continue
                self._waiters.setdefault(wid, set()).add(event)
        return event

    def unregister_waiter(
        self,
        child_ws_ids: Iterable[str],
        event: threading.Event,
    ) -> None:
        """Remove ``event`` from each listed ws_id's waiter set.

        Idempotent — already-removed Events silently no-op. Pops empty
        sets so a long-lived bus doesn't accumulate dead keys after
        many waits have come and gone. Must be called from the same
        ``finally`` that paired with :meth:`register_waiter` so a
        crash mid-wait doesn't leak the registration past one wait's
        lifetime.
        """
        with self._lock:
            for wid in child_ws_ids:
                if not wid:
                    continue
                bucket = self._waiters.get(wid)
                if bucket is None:
                    continue
                bucket.discard(event)
                if not bucket:
                    self._waiters.pop(wid, None)

    def notify(self, child_ws_id: str) -> None:
        """Wake every Event registered for ``child_ws_id``.

        Called from the coord dispatch sink after each translated child
        event. Snapshot the bucket under the lock, then call
        ``Event.set`` outside the lock so a slow waker doesn't block
        ``register`` / ``unregister`` / further ``notify``. ``Event.set``
        is thread-safe and idempotent — re-firing a still-set Event is
        a no-op.

        Empty / falsy ws_ids are silently dropped; the same hot path
        runs for every dispatched event regardless of whether anyone's
        waiting, so the empty-bucket case must stay cheap.
        """
        if not child_ws_id:
            return
        with self._lock:
            bucket = self._waiters.get(child_ws_id)
            if not bucket:
                return
            events = list(bucket)
        for event in events:
            event.set()
