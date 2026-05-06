"""Observer that nudges idle coordinators with active children.

Subscribes to a coordinator-side :class:`SessionManager`'s state events.
When a coord transitions to :class:`WorkstreamState.IDLE` while still
having active interactive children, enqueues an ``idle_children`` nudge
on the coord's :class:`NudgeQueue`.  The
:class:`turnstone.core.metacognition.IdleNudgeWatcher` (registered
*after* this observer in the lifespan, so subscriber-order has the
observer fire first on the same IDLE event) then peeks the queue and
dispatches the wake send.

Gates (in order):

1. **Coordinator-only.**  Skip non-coord workstreams.  Watcher is
   kind-agnostic; this observer is the kind-aware piece.
2. **Skip if last assistant turn used ``wait_for_workstream``.** The
   coord is already using the right tool — don't pile on with a nudge
   suggesting the same tool.
3. **Per-(ws_id, nudge_type) hard cap** (default 3).  Resets when the
   ws leaves IDLE for any non-wake-driven reason (tracked by
   :class:`ChatSession._wake_source_tag` — see below).
4. **Active children query.**  ``storage.list_workstreams`` filtered to
   interactive kind under the coord's user, with state in
   :data:`_ACTIVE_CHILD_STATES`.  Empty result → no nudge.
5. **Cooldown** via :func:`should_nudge` — default 300s per nudge type.

The enqueue passes ``valid_until`` that re-queries active children at
drain time; if every child finished while the queue waited, the entry
drops without delivering a stale snapshot.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.core.metacognition import (
    _cooldown_allows,
    format_idle_children_nudge,
    should_nudge,
)
from turnstone.core.workstream import WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.workstream import Workstream

log = get_logger(__name__)

# Active = the model can act on the child (it's still working,
# streaming, or waiting on user attention).  Excludes "idle" (the
# child is now waiting and can't be unblocked by the coord), "closed"
# (gone), "deleted" (gone), and "error" (the model can't unblock an
# errored child without operator intervention; cooldown handles repeat
# fires for stuck-error children).
_ACTIVE_CHILD_STATES: frozenset[str] = frozenset(
    {
        WorkstreamState.THINKING.value,
        WorkstreamState.RUNNING.value,
        WorkstreamState.ATTENTION.value,
    }
)

# Hard cap on per-session ``idle_children`` fires.  Even with the
# cooldown and wait-tool skip gate, a coord that ignores every nudge
# shouldn't be hammered indefinitely.  Resets when the ws leaves IDLE
# for a non-wake reason (real user input).
_HARD_CAP_PER_SESSION = 3

# Soft cap on the snapshot query.  Higher than ``WAIT_MAX_WS_IDS`` so
# the SQL ``LIMIT`` (applied before the Python state filter) doesn't
# clip genuinely-active children whose ``updated`` timestamp is older
# than recently-closed siblings.  Realistic coord histories are far
# smaller than this; if a coord ever exceeds it, the formatter still
# truncates to ``WAIT_MAX_WS_IDS`` for the model-facing suggestion.
_ACTIVE_CHILDREN_QUERY_LIMIT = 200


class CoordinatorIdleObserver:
    """Subscribe to a coord SessionManager's IDLE events and enqueue
    ``idle_children`` nudges when active children remain.
    """

    def __init__(self, manager: SessionManager, storage: StorageBackend) -> None:
        self._manager = manager
        self._storage = storage
        self._callback: Callable[[str, WorkstreamState], None] | None = None
        # Per-ws fire counts keyed by ``ws_id`` → ``{nudge_type: count}``.
        # Two-level dict makes the "any caps for this ws?" check at
        # leave-IDLE an O(1) ``ws_id in self._fire_counts`` lookup
        # instead of an O(N_caps) scan over a flat tuple-keyed map.
        # Lock protects against race with the leave-IDLE reset path
        # running on a different thread (state events fire on the
        # calling thread of ``set_state`` — currently always the
        # worker thread that did the transition, but the lock keeps
        # the contract robust).
        self._fire_counts: dict[str, dict[str, int]] = {}
        self._fire_counts_lock = threading.Lock()

    def start(self) -> None:
        """Idempotent — registering twice is a no-op."""
        if self._callback is not None:
            return

        def _on_state(ws_id: str, state: WorkstreamState) -> None:
            if state is not WorkstreamState.IDLE:
                # Reset hard-cap when leaving IDLE for a *real* reason
                # (not a wake-driven exit).  Skip the manager-lock /
                # session-attribute walk entirely when no caps are
                # accumulated for this ws — the common case for the
                # vast majority of state transitions.
                with self._fire_counts_lock:
                    has_caps = ws_id in self._fire_counts
                if not has_caps:
                    return
                ws = self._manager.get(ws_id)
                if ws is None or ws.session is None:
                    return
                # ``_wake_source_tag`` is set on the session iff a
                # wake send is in flight; if set, leaving IDLE is the
                # wake's own IDLE→THINKING→RUNNING transition and the
                # cap should NOT reset.  If unset, the user / a real
                # producer drove the coord forward and the cap should
                # clear so the next genuine idle bracket is fresh.
                if not ws.session._wake_source_tag:
                    self._reset_caps_for(ws_id)
                return

            # state == IDLE branch.
            try:
                self._maybe_enqueue(ws_id)
            except Exception:
                log.exception("coord_idle_observer.maybe_enqueue_failed ws=%s", ws_id[:8])

        self._callback = _on_state
        self._manager.subscribe_to_state(_on_state)

    def shutdown(self) -> None:
        """Unsubscribe; idempotent."""
        cb = self._callback
        if cb is None:
            return
        with contextlib.suppress(Exception):
            self._manager.unsubscribe_from_state(cb)
        self._callback = None

    def _maybe_enqueue(self, ws_id: str) -> None:
        ws = self._manager.get(ws_id)
        if ws is None or ws.session is None:
            return
        if ws.kind is not WorkstreamKind.COORDINATOR:
            return
        session = ws.session

        # Gate ordering matters: cheap checks first, expensive checks
        # last.  The cooldown peek + per-session hard cap are
        # microsecond-cheap dict lookups; ``_last_assistant_used_wait``
        # walks ``session.messages`` reversed; ``_active_children`` and
        # ``_visible_memory_count`` round-trip to storage.  With a 300s
        # cooldown most idle events will short-circuit at the peek.
        cooldown_secs = getattr(session._mem_cfg, "nudge_cooldown", 300)
        if not _cooldown_allows(
            "idle_children", session._metacog_state, cooldown_secs=cooldown_secs
        ):
            return

        # Gate: per-session hard cap on idle_children fires.
        with self._fire_counts_lock:
            ws_caps = self._fire_counts.get(ws_id, {})
            if ws_caps.get("idle_children", 0) >= _HARD_CAP_PER_SESSION:
                return

        # Gate: skip if the coord's last assistant turn already used
        # ``wait_for_workstream``.  Don't nudge toward a tool the
        # model is already using.
        if self._last_assistant_used_wait(session):
            return

        # Gate: query active children.  Empty → nothing to nudge about.
        active = self._active_children(ws)
        if not active:
            return

        # ``should_nudge`` re-checks cooldown AND records the timestamp
        # on success (the peek above only checks; record happens here).
        # Also enforces the message-count > 1 / memory-count > 0 sanity
        # gates we couldn't apply at the cheap-peek stage.
        if not should_nudge(
            "idle_children",
            session._metacog_state,
            message_count=len(session.messages),
            memory_count=session._visible_memory_count(),
            cooldown_secs=cooldown_secs,
        ):
            return

        text = format_idle_children_nudge(active)
        if not text:  # belt-and-braces: formatter empty-input guard
            return

        # Bind ws.id + user_id by closure so the predicate captures the
        # workstream identity (not the live ``ws`` reference, which
        # could mutate).  The predicate runs at drain time outside the
        # queue lock.  Use ``count_workstreams_by_state`` rather than
        # ``list_workstreams`` since the predicate only needs a
        # boolean — saves a row fetch on the chat-loop user-attach
        # path.
        bound_ws_id = ws.id
        bound_user_id = ws.user_id

        def _still_has_active_children() -> bool:
            try:
                counts = self._storage.count_workstreams_by_state(
                    parent_ws_id=bound_ws_id,
                    user_id=bound_user_id,
                )
            except Exception:
                log.debug(
                    "coord_idle_observer.predicate_count_failed ws=%s",
                    bound_ws_id[:8],
                    exc_info=True,
                )
                return False
            return any(counts.get(s, 0) > 0 for s in _ACTIVE_CHILD_STATES)

        session._nudge_queue.enqueue(
            "idle_children",
            text,
            "any",
            valid_until=_still_has_active_children,
        )
        with self._fire_counts_lock:
            ws_caps = self._fire_counts.setdefault(ws_id, {})
            ws_caps["idle_children"] = ws_caps.get("idle_children", 0) + 1

        log.info(
            "coord_idle_observer.enqueued ws=%s active_children=%d",
            ws_id[:8],
            len(active),
        )

    def _last_assistant_used_wait(self, session: ChatSession) -> bool:
        """Walk back to the most recent assistant turn; if it issued a
        ``wait_for_workstream`` tool call, return ``True``.
        """
        for msg in reversed(session.messages):
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                if fn.get("name") == "wait_for_workstream":
                    return True
            return False  # found the most recent assistant turn — done
        return False

    def _active_children(self, ws: Workstream) -> list[dict[str, str]]:
        """Query storage for the coord's interactive children whose state
        is in :data:`_ACTIVE_CHILD_STATES`.  Returns row-mapping shape.

        ``list_workstreams`` orders by ``updated DESC`` and applies its
        ``LIMIT`` in SQL before any state filter, so a coord with many
        recently-closed children could clip out genuinely-active rows
        whose ``updated`` timestamp is older.  We bump the limit well
        above ``NUDGE_IDLE_CHILDREN_WAIT_CAP`` to absorb that —
        realistic coord histories are far smaller than the bumped
        limit.  Pushing the state filter into SQL would be the
        structural fix, but that requires a storage-protocol change;
        flagged as a follow-up.
        """
        try:
            rows = self._storage.list_workstreams(
                limit=_ACTIVE_CHILDREN_QUERY_LIMIT,
                parent_ws_id=ws.id,
                kind=WorkstreamKind.INTERACTIVE,
                user_id=ws.user_id,
            )
        except Exception:
            log.debug("coord_idle_observer.list_failed ws=%s", ws.id[:8], exc_info=True)
            return []

        out: list[dict[str, str]] = []
        for row in rows:
            mapping = getattr(row, "_mapping", row)
            state = mapping["state"]
            if state not in _ACTIVE_CHILD_STATES:
                continue
            out.append(
                {
                    "ws_id": mapping["ws_id"],
                    "name": mapping["name"] or "",
                    "state": state,
                }
            )
        return out

    def _reset_caps_for(self, ws_id: str) -> None:
        """Drop every nudge-type cap counter for ``ws_id`` on a real
        (non-wake) leave-IDLE event — the next genuine idle bracket
        starts fresh.  O(1) with the per-ws nested-dict layout.
        """
        with self._fire_counts_lock:
            self._fire_counts.pop(ws_id, None)
