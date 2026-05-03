"""Smoke tests for the unified SessionManager.

Cover the concurrency-sensitive paths that historically diverged
between ``WorkstreamManager`` and ``CoordinatorManager`` — slot
reservation, eviction, per-ws lock serialization on lazy rehydrate,
state flips, close-unblocks-UI. All exercised through a
``FakeAdapter`` that records ``emit_*`` / ``cleanup_ui`` calls so
tests can assert the transport contract without spinning up real
WebUI / ClusterCollector pipelines. The adapter is wired as both the
manager's ``adapter`` (for ``cleanup_ui`` / ``build_*``) and its
``event_emitter`` (for the ``emit_*`` lifecycle calls); production
adapters do the same — interactive on ``server.py``, coordinator on
``console/server.py``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from turnstone.core.session_manager import SessionKindAdapter, SessionManager
from turnstone.core.workstream import (
    BULK_CLOSE_STATE_VALUES,
    Workstream,
    WorkstreamKind,
    WorkstreamState,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    kind: str  # "created" | "rehydrated" | "state" | "closed"
    ws_id: str
    state: WorkstreamState | None = None
    reason: str | None = None
    name: str | None = None


class FakeUI:
    """Minimal UI stand-in with the events close() needs to unblock."""

    def __init__(self) -> None:
        self.approval_unblocked = False
        self.plan_unblocked = False
        self.fg_unblocked = False
        self.closed_broadcast = False

    def _unblock(self) -> None:
        self.approval_unblocked = True
        self.plan_unblocked = True
        self.fg_unblocked = True

    def broadcast_ws_closed(self) -> None:
        self.closed_broadcast = True


class FakeSession:
    """Minimal ChatSession stand-in; exposes cancel / close / resume."""

    def __init__(self, ws_id: str) -> None:
        self.ws_id = ws_id
        self.cancelled = False
        self.closed = False
        self.resumed = False

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True

    def resume(self, ws_id: str) -> None:
        self.resumed = True


class FakeAdapter:
    """Records events + builds FakeUI / FakeSession for tests."""

    def __init__(
        self,
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
        *,
        build_session_raises: bool = False,
    ) -> None:
        self.kind = kind
        self.events: list[_Event] = []
        self._events_lock = threading.Lock()
        self.cleaned_up: list[str] = []
        self.build_session_calls = 0
        self.build_session_raises = build_session_raises
        self.last_build_model: object | None = None
        # Slow down session build so concurrent tests can race.
        self.build_session_delay = 0.0

    def emit_created(self, ws: Workstream) -> None:
        with self._events_lock:
            self.events.append(_Event("created", ws.id))

    def emit_rehydrated(self, ws: Workstream) -> None:
        # Distinct from "created" so a regression where the manager
        # fires emit_created on the rehydrate path (or emit_rehydrated
        # on the create path) actually fails a test. The
        # SessionEventEmitter Protocol carves these out as semantically
        # different — coord uses emit_rehydrated for the storage-seeded
        # subtree rebuild that emit_created skips.
        with self._events_lock:
            self.events.append(_Event("rehydrated", ws.id))

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        with self._events_lock:
            self.events.append(_Event("state", ws.id, state=state))

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        with self._events_lock:
            self.events.append(_Event("closed", ws_id, reason=reason, name=name))

    def cleanup_ui(self, ws: Workstream) -> None:
        self.cleaned_up.append(ws.id)
        if ws.session is not None:
            ws.session.cancel()
            ws.session.close()
        if ws.ui is not None:
            ws.ui._unblock()
            ws.ui.broadcast_ws_closed()

    def build_ui(self, ws: Workstream) -> Any:
        return FakeUI()

    def build_session(self, ws: Workstream, **kwargs: object) -> Any:
        self.build_session_calls += 1
        # Record the ``model`` kwarg (None on fresh-create, the saved
        # alias on rehydrate) so tests can assert SessionManager.open()
        # threads the persisted alias through to construction instead
        # of letting the adapter resolve the *current* default alias.
        self.last_build_model = kwargs.get("model")
        if self.build_session_delay:
            time.sleep(self.build_session_delay)
        if self.build_session_raises:
            raise RuntimeError("build_session forced failure")
        return FakeSession(ws.id)

    def events_of(self, kind: str) -> list[_Event]:
        with self._events_lock:
            return [e for e in self.events if e.kind == kind]


@dataclass
class _Row:
    ws_id: str
    user_id: str
    name: str
    kind: str
    state: str = "idle"
    parent_ws_id: str | None = None
    updated: str = ""
    node_id: str | None = None


class FakeStorage:
    """In-memory storage that mirrors the StorageBackend surface the manager uses."""

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        self.state_updates: list[tuple[str, str]] = []
        self.touch_calls: list[str] = []
        self.register_raises = False
        self.lock = threading.Lock()
        # Live-services lookup target for close_idle pass 2.  Map
        # service_type → list of live service_ids.  Tests that exercise
        # liveness scoping populate this directly; default empty means
        # "no peers alive" (every row unprotected by liveness).
        self.live_services: dict[str, list[str]] = {}
        self.list_services_raises = False
        # Per-ws config (model_alias, temperature, …).  Populated by
        # tests that exercise the rehydrate-preserves-config path; the
        # SessionManager.open() rehydrate path reads this through
        # ``self._storage.load_workstream_config`` so it can pass the
        # saved alias into ``build_session`` and avoid clobbering the
        # original on construction.
        self.ws_config: dict[str, dict[str, str]] = {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    def register_workstream(
        self,
        ws_id: str,
        *,
        node_id: str | None = None,
        user_id: str | None = None,
        name: str = "",
        kind: WorkstreamKind | str = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
        state: str = "idle",
        updated: str | None = None,
    ) -> None:
        if self.register_raises:
            raise RuntimeError("register forced failure")
        kind_str = kind.value if isinstance(kind, WorkstreamKind) else str(kind)
        with self.lock:
            self.rows[ws_id] = _Row(
                ws_id=ws_id,
                user_id=user_id or "",
                name=name,
                kind=kind_str,
                state=state,
                parent_ws_id=parent_ws_id,
                updated=updated if updated is not None else self._now_iso(),
                node_id=node_id,
            )

    def touch_workstream(self, ws_id: str) -> None:
        with self.lock:
            self.touch_calls.append(ws_id)
            if ws_id in self.rows:
                self.rows[ws_id].updated = self._now_iso()

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        with self.lock:
            self.state_updates.append((ws_id, state))
            if ws_id in self.rows:
                self.rows[ws_id].state = state
                self.rows[ws_id].updated = self._now_iso()

    def bulk_close_stale_orphans(
        self,
        kind: WorkstreamKind | str,
        cutoff: str,
        exclude_ws_ids: list[str],
        live_node_ids: list[str] | None = None,
    ) -> list[str]:
        kind_str = kind.value if isinstance(kind, WorkstreamKind) else str(kind)
        excluded = set(exclude_ws_ids)
        live_set = set(live_node_ids) if live_node_ids else set()
        now = self._now_iso()
        closed: list[str] = []
        with self.lock:
            for ws_id, row in self.rows.items():
                if (
                    row.kind == kind_str
                    and row.state in BULK_CLOSE_STATE_VALUES
                    and row.updated < cutoff
                    and ws_id not in excluded
                ):
                    # Liveness gate: when live_node_ids was provided AND
                    # non-empty, protect rows whose owner is in the live
                    # set.  NULL node_id is always eligible.  When
                    # live_node_ids is None or empty, no protection
                    # (mirror of the real backends).
                    if live_node_ids and row.node_id is not None and row.node_id in live_set:
                        continue
                    row.state = "closed"
                    row.updated = now
                    self.state_updates.append((ws_id, "closed"))
                    closed.append(ws_id)
        return closed

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        if self.list_services_raises:
            raise RuntimeError("list_services forced failure")
        with self.lock:
            return [
                {"service_id": sid, "service_type": service_type}
                for sid in self.live_services.get(service_type, [])
            ]

    def get_workstream(self, ws_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.rows.get(ws_id)
            if row is None:
                return None
            return {
                "ws_id": row.ws_id,
                "user_id": row.user_id,
                "name": row.name,
                "kind": row.kind,
                "state": row.state,
                "parent_ws_id": row.parent_ws_id,
            }

    def delete_workstream(self, ws_id: str) -> None:
        with self.lock:
            self.rows.pop(ws_id, None)

    def count_skill_versions(self, template_id: str) -> int:
        return 0

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        with self.lock:
            return dict(self.ws_config.get(ws_id, {}))

    def save_workstream_config(self, ws_id: str, config: dict[str, str]) -> None:
        # Mirrors the real backend's INSERT OR REPLACE per-key semantics
        # — callers expect a partial save to overwrite only the keys
        # they pass, not the whole row.
        with self.lock:
            row = self.ws_config.setdefault(ws_id, {})
            row.update(config)


_EMITTER_DEFAULT = object()


def _make_manager(
    adapter: FakeAdapter | None = None,
    *,
    max_active: int = 5,
    storage: FakeStorage | None = None,
    event_emitter: Any = _EMITTER_DEFAULT,
    node_id: str | None = None,
) -> tuple[SessionManager, FakeAdapter, FakeStorage]:
    """Build a SessionManager wired to a FakeAdapter for both Protocols.

    ``event_emitter`` defaults to the adapter (production wiring shape);
    pass ``None`` explicitly to disable the lifecycle-event side
    channel for tests that care about no-emitter behaviour.
    """
    adapter = adapter or FakeAdapter()
    storage = storage or FakeStorage()
    # FakeAdapter implements both Protocols (the production adapters
    # do too — wire as both so the emit_* assertions in this file
    # still see the events the manager fires).
    emitter = adapter if event_emitter is _EMITTER_DEFAULT else event_emitter
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=max_active,
        event_emitter=emitter,
        node_id=node_id,
    )
    return mgr, adapter, storage


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_session_manager_constructs_with_adapter() -> None:
    mgr, adapter, _ = _make_manager()
    assert mgr.max_active == 5
    assert mgr.kind == adapter.kind


def test_session_manager_rejects_invalid_max_active() -> None:
    with pytest.raises(ValueError, match="max_active must be >= 1"):
        SessionManager(FakeAdapter(), storage=FakeStorage(), max_active=0)


def test_noop_adapter_satisfies_protocol() -> None:
    adapter: SessionKindAdapter = FakeAdapter()
    assert adapter.kind == WorkstreamKind.INTERACTIVE


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_persists_and_emits_created() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="hello")
    assert ws.user_id == "u1"
    assert ws.name == "hello"
    assert ws.session is not None
    assert ws.ui is not None
    assert ws.id in storage.rows
    assert storage.rows[ws.id].kind == WorkstreamKind.INTERACTIVE.value
    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]


def test_create_with_defer_emit_created_skips_emit() -> None:
    """``defer_emit_created=True`` returns the workstream but skips
    the ``emit_created`` call. The slot, storage row, and built
    session all exist — only the lifecycle event is held back.
    Caller takes ownership of advertising the workstream via
    :meth:`SessionManager.commit_create` (success) or
    :meth:`SessionManager.discard` (rollback)."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="deferred", defer_emit_created=True)
    # Workstream is fully constructed — only the broadcast is deferred.
    assert ws.session is not None
    assert ws.ui is not None
    assert ws.id in storage.rows
    assert mgr.get(ws.id) is ws
    # No created event fired.
    assert adapter.events_of("created") == []


def test_commit_create_fires_deferred_emit_created() -> None:
    """``commit_create`` is the deferred counterpart that fires the
    pending ``emit_created`` event after the caller's post-create
    work (e.g. attachment validation in the lifted HTTP handler)
    confirms the workstream should be advertised."""
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    assert adapter.events_of("created") == []

    mgr.commit_create(ws)

    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]


def test_commit_create_is_noop_without_event_emitter() -> None:
    """``commit_create`` must tolerate a manager constructed without
    an event emitter — the deferred-create + commit pair has to
    work the same shape regardless of whether transport fan-out is
    wired (test fixtures, future kinds without an emitter)."""
    mgr, adapter, _ = _make_manager(event_emitter=None)
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    # Must not raise; nothing observable should happen.
    mgr.commit_create(ws)
    assert adapter.events_of("created") == []


def test_discard_releases_slot_without_emit_closed() -> None:
    """``discard`` is the rollback counterpart to ``commit_create``.
    Releases the in-memory slot + cleans up the UI, but does NOT
    fire ``emit_closed`` because the workstream's existence was
    never advertised (caller used ``defer_emit_created=True``).

    Distinct from ``close`` which DOES fire ``emit_closed`` to
    advertise the transition. Pre-fix, the lifted create handler's
    rollback path called ``close`` and produced a phantom
    create→close pair on the cluster events stream when attachment
    validation failed; ``discard`` is the surgical fix."""
    mgr, adapter, storage = _make_manager(max_active=2)
    ws = mgr.create(user_id="u1", name="will-be-discarded", defer_emit_created=True)
    ws_id = ws.id

    discarded = mgr.discard(ws_id)
    assert discarded is True

    # In-memory slot released — capacity restored.
    assert mgr.get(ws_id) is None
    assert mgr.count == 0
    # UI cleanup ran (cleanup_ui is part of discard's contract).
    assert ws_id in adapter.cleaned_up
    # No advertisement at any point: no created event, no closed event.
    assert adapter.events_of("created") == []
    assert adapter.events_of("closed") == []
    # Storage row survives — caller is responsible for
    # ``delete_workstream`` if they want a complete rollback. Mirrors
    # mgr.create's own session-build-failure path.
    assert ws_id in storage.rows


def test_discard_returns_false_for_unknown_ws_id() -> None:
    """``discard`` is idempotent on an absent ws_id — returns False
    instead of raising so the lifted handler's rollback path can
    safely call discard inside ``contextlib.suppress`` without
    spurious failures swallowing real errors."""
    mgr, _, _ = _make_manager()
    result = mgr.discard("nonexistent-ws-id")
    assert result is False


def test_commit_create_after_discard_is_no_op(caplog) -> None:
    """Caller-bug case: ``commit_create`` after ``discard`` must not
    fire ``emit_created`` for a workstream that's no longer tracked
    by the manager. The workstream object still exists in the
    caller's scope (discard only removed it from the manager's slot
    map), so forwarding it to ``commit_create`` would fire a phantom
    ``ws_created`` for an id the cluster collector / children
    registry will then never see ``ws_closed`` for. The guard logs
    ``session_mgr.commit_create.untracked`` and returns without
    emitting."""
    import logging

    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    discarded = mgr.discard(ws.id)
    assert discarded is True
    assert adapter.events_of("created") == []

    with caplog.at_level(logging.WARNING, logger="turnstone.core.session_manager"):
        mgr.commit_create(ws)

    # No event emitted — the tracked-ws check failed.
    assert adapter.events_of("created") == []
    # Warning surfaced for operator triage.
    assert any("commit_create.untracked" in record.message for record in caplog.records), [
        r.message for r in caplog.records
    ]


def test_commit_create_is_idempotent_on_duplicate_call(caplog) -> None:
    """Caller-bug case: calling ``commit_create`` twice on the same
    workstream must fire ``emit_created`` exactly once. The second
    call hits the ``_emit_created_fired`` guard, logs
    ``session_mgr.commit_create.already_fired``, and returns without
    re-emitting."""
    import logging

    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    mgr.commit_create(ws)
    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]

    with caplog.at_level(logging.WARNING, logger="turnstone.core.session_manager"):
        mgr.commit_create(ws)

    # Still exactly one created event — the guard short-circuited
    # the second call.
    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]
    assert any("commit_create.already_fired" in record.message for record in caplog.records), [
        r.message for r in caplog.records
    ]


def test_discard_after_emit_created_warns_but_releases_slot(caplog) -> None:
    """Caller-bug case: ``discard`` on a workstream where
    ``emit_created`` has already fired (either via the non-deferred
    create path or via ``commit_create``). The intended retraction
    path is ``close`` (which fires ``emit_closed``); ``discard``
    leaves a stale ``ws_created`` on the wire with no matching
    ``ws_closed``, which is exactly the phantom-event bug
    ``defer_emit_created`` was added to fix.

    ``discard`` still releases the in-memory slot (so capacity is
    freed even when the caller misuses the API) but logs a
    ``warning`` so the misuse surfaces in ops logs.
    """
    import logging

    mgr, adapter, _ = _make_manager()
    # Non-deferred create — emit_created fires inside mgr.create.
    ws = mgr.create(user_id="u1", name="created-then-discarded")
    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]

    with caplog.at_level(logging.WARNING, logger="turnstone.core.session_manager"):
        result = mgr.discard(ws.id)

    # Slot released (caller-bug doesn't strand capacity).
    assert result is True
    assert mgr.get(ws.id) is None
    # Warning surfaced for operator triage.
    assert any("discard.after_emit_created" in record.message for record in caplog.records), [
        r.message for r in caplog.records
    ]
    # No ws_closed was fired — the bug being warned about is exactly
    # this asymmetry (created without close on the wire). Operators
    # who actually want a clean retraction should call close instead.
    assert adapter.events_of("closed") == []


def test_create_evicts_oldest_idle_at_capacity() -> None:
    mgr, adapter, _ = _make_manager(max_active=2)
    first = mgr.create(user_id="u1")
    # Nudge the timestamp so 'first' is the clear eviction candidate.
    first.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    # Third create triggers eviction of the oldest IDLE (= first).
    third = mgr.create(user_id="u1")
    assert mgr.get(first.id) is None
    assert mgr.get(third.id) is not None
    # Adapter transport saw the eviction and the new create.
    assert first.id in [e.ws_id for e in adapter.events_of("closed")]
    assert third.id in [e.ws_id for e in adapter.events_of("created")]
    assert first.id in adapter.cleaned_up


def test_create_raises_when_all_active_and_no_idle() -> None:
    mgr, _, _ = _make_manager(max_active=1)
    ws = mgr.create(user_id="u1")
    ws.state = WorkstreamState.RUNNING  # block eviction
    with pytest.raises(RuntimeError, match="slots are active"):
        mgr.create(user_id="u1")


def test_create_rolls_back_slot_on_session_failure() -> None:
    adapter = FakeAdapter(build_session_raises=True)
    mgr, _, storage = _make_manager(adapter=adapter)
    with pytest.raises(RuntimeError, match="build_session forced failure"):
        mgr.create(user_id="u1")
    # Slot freed — no dangling capacity consumption.  The storage row
    # survives construction failure on purpose: the next ``open(ws_id)``
    # retries build_session rather than forcing the user to create a
    # brand-new workstream.
    assert mgr.count == 0
    assert len(storage.rows) == 1


def test_create_rolls_back_slot_on_persist_failure() -> None:
    storage = FakeStorage()
    storage.register_raises = True
    mgr, _, _ = _make_manager(storage=storage)
    with pytest.raises(RuntimeError, match="register forced failure"):
        mgr.create(user_id="u1")
    assert mgr.count == 0


def test_concurrent_create_does_not_exceed_max_active() -> None:
    mgr, _, _ = _make_manager(max_active=3)
    adapter = mgr._adapter  # type: ignore[attr-defined]
    assert isinstance(adapter, FakeAdapter)
    adapter.build_session_delay = 0.02  # widen the race window

    results: list[Workstream | Exception] = []
    lock = threading.Lock()

    def _create() -> None:
        try:
            ws = mgr.create(user_id="u1")
            with lock:
                results.append(ws)
        except Exception as e:
            with lock:
                results.append(e)

    threads = [threading.Thread(target=_create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Manager holds at most max_active; overflow creates must raise
    # (5 overflows raise, 3 succeed — no silent exceed).
    assert mgr.count <= 3
    successes = [r for r in results if isinstance(r, Workstream)]
    assert len(successes) <= 3


# ---------------------------------------------------------------------------
# open — lazy rehydrate
# ---------------------------------------------------------------------------


def test_open_returns_none_for_missing_row() -> None:
    mgr, _, _ = _make_manager()
    opened = mgr.open("missing")
    assert opened is None


def test_open_blocks_deleted_state() -> None:
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    # Flip the row to the tombstone state — open must refuse it.
    storage.rows[ws.id].state = "deleted"
    opened = mgr.open(ws.id)
    assert opened is None


def test_open_resurrects_closed_state() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    mgr.close(ws_id)
    assert mgr.get(ws_id) is None

    reopened = mgr.open(ws_id)
    assert reopened is not None
    assert reopened.id == ws_id
    assert reopened.session is not None
    assert reopened.session.resumed is True  # type: ignore[attr-defined]
    # Open fires emit_rehydrated (NOT emit_created) so observers can
    # gate any extra resurrect-only setup on it (e.g. coord's
    # storage-seeded children rebuild).
    assert ws_id in [e.ws_id for e in adapter.events_of("rehydrated")]


def test_open_threads_saved_model_alias_into_build_session() -> None:
    """Reopening a closed ws must build the session with the *original*
    model alias, not the current registry default.

    Without this, ``build_session(ws)`` is called with ``model=None`` →
    the production session_factory resolves ``_effective_default_alias()``
    → ChatSession's ``__init__`` writes those defaults to
    ``workstream_config`` (INSERT OR REPLACE) → the subsequent
    ``resume()`` restores what is now the default. Net effect: every
    persisted knob (model, temperature, reasoning_effort, max_tokens,
    skill, creative_mode, instructions, …) silently resets on every
    reopen and on every service restart.
    """
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    # Pretend the user set a non-default alias when the ws was created;
    # the real path goes through ChatSession._save_config but the
    # FakeSession in this suite doesn't model that, so seed directly.
    storage.ws_config[ws_id] = {"model_alias": "gpt-5-pro"}
    mgr.close(ws_id)
    adapter.last_build_model = "<unset>"  # sentinel — must be overwritten

    reopened = mgr.open(ws_id)

    assert reopened is not None
    assert adapter.last_build_model == "gpt-5-pro"


def test_open_falls_back_to_none_when_no_saved_alias() -> None:
    """Reopening a ws with no saved alias must pass ``model=None`` to
    ``build_session`` so the adapter's session_factory can fall back to
    the current default — matching the user's intent: best effort
    restore, default when the original is gone."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    # No ws_config row — simulates "alias was never saved" or "saved
    # alias was empty string".
    assert ws_id not in storage.ws_config
    mgr.close(ws_id)
    adapter.last_build_model = "<unset>"

    reopened = mgr.open(ws_id)

    assert reopened is not None
    assert adapter.last_build_model is None


def test_open_touches_workstream_on_rehydrate() -> None:
    """Rehydrating a workstream must bump its ``updated`` so a concurrent
    close_idle pass-2 in this same process can't clobber the freshly-loaded
    row to ``closed`` because its DB ``updated`` is older than the cutoff.
    The touch is best-effort (try/except in open()) but must fire on the
    happy path."""
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    mgr.close(ws_id)
    storage.touch_calls.clear()  # only care about touches from rehydrate

    reopened = mgr.open(ws_id)

    assert reopened is not None
    assert ws_id in storage.touch_calls


def test_open_ignores_owner_mismatch() -> None:
    # Turnstone is a trusted-team tool; row-level ownership is
    # metadata, not an access boundary.  ``open`` no longer cares
    # who the caller is — any authenticated caller can rehydrate
    # any persisted workstream.  Scope-level auth at the HTTP
    # layer is the only gate.
    mgr, _, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    reopened = mgr.open(ws.id)
    assert reopened is not None
    assert reopened.user_id == "u1"  # metadata preserved


def test_open_rejects_wrong_kind() -> None:
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    # Storage row claims a different kind than our adapter's.
    storage.rows[ws.id].kind = WorkstreamKind.COORDINATOR.value
    mgr.close(ws.id)
    opened = mgr.open(ws.id)
    assert opened is None


def test_concurrent_open_for_same_ws_id_returns_same_session() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    mgr.close(ws_id)
    adapter.build_session_calls = 0
    adapter.build_session_delay = 0.02

    results: list[Workstream | None] = []
    lock = threading.Lock()

    def _open() -> None:
        r = mgr.open(ws_id)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_open) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = [r for r in results if r is not None]
    assert len(loaded) == 6
    # All threads got the same Workstream instance — no duplicate session.
    sessions = {id(r.session) for r in loaded}
    assert len(sessions) == 1
    # build_session ran exactly once — the per-ws lock serialized the rest.
    assert adapter.build_session_calls == 1


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_unblocks_ui_and_emits_closed() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id

    closed = mgr.close(ws_id)
    assert closed is True
    assert mgr.get(ws_id) is None
    assert ws_id in adapter.cleaned_up
    assert ws_id in [e.ws_id for e in adapter.events_of("closed")]
    # Storage reflects the close.
    assert (ws_id, "closed") in storage.state_updates
    # UI events unblocked.
    assert ws.ui.approval_unblocked is True  # type: ignore[attr-defined]
    assert ws.ui.plan_unblocked is True  # type: ignore[attr-defined]
    assert ws.ui.closed_broadcast is True  # type: ignore[attr-defined]
    # Session cancelled + closed.
    assert ws.session.cancelled is True  # type: ignore[attr-defined]
    assert ws.session.closed is True  # type: ignore[attr-defined]


def test_close_last_workstream_succeeds() -> None:
    """The WSM 'refuse to close last workstream' guard is gone (#400 follow-up).

    The default startup workstream relic is deleted; the dashboard
    handles the 0-workstream state and callers can close freely.
    """
    mgr, _, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    closed = mgr.close(ws.id)
    assert closed is True
    assert mgr.count == 0


def test_close_unknown_returns_false() -> None:
    mgr, _, _ = _make_manager()
    closed = mgr.close("not-there")
    assert closed is False


# ---------------------------------------------------------------------------
# delete — hard-delete event broadcast (storage delete is the caller's job)
# ---------------------------------------------------------------------------


def test_delete_emits_closed_with_reason_deleted() -> None:
    """Hard-delete a still-loaded workstream: the in-memory slot
    drops AND a ``ws_closed`` event fires with ``reason='deleted'``
    so the cluster collector → coord adapter chain can re-emit as
    ``child_ws_closed`` and the operator's child-tree drops the row.
    Pre-fix the storage row vanished but no event fired, so a
    long-lived dashboard tab would leave the deleted child visible
    (with its last-known state) until a full reload."""
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", name="will-be-deleted")
    ws_id = ws.id

    deleted = mgr.delete(ws_id)
    assert deleted is True
    # In-memory slot released — capacity restored just like close.
    assert mgr.get(ws_id) is None
    # Closed event fired with the deletion reason.
    closed_events = adapter.events_of("closed")
    assert len(closed_events) == 1
    ev = closed_events[0]
    assert ev.ws_id == ws_id
    assert ev.reason == "deleted"
    assert ev.name == "will-be-deleted"
    # UI cleanup ran (mirrors close ordering).
    assert ws_id in adapter.cleaned_up


def test_delete_unloaded_ws_still_fires_event() -> None:
    """A row that was closed (and therefore unloaded from memory)
    before delete still needs the broadcast — otherwise a closed
    row that's then deleted leaves the closed-state child stuck on
    the dashboard tree forever.  The in-memory return is False
    (nothing to release) but the event MUST fire."""
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", name="closed-then-deleted")
    ws_id = ws.id
    mgr.close(ws_id)
    # Drain the close event so we can assert the delete event in isolation.
    pre_delete_closed = list(adapter.events_of("closed"))
    assert len(pre_delete_closed) == 1
    assert pre_delete_closed[0].reason == "closed"

    # Deleting an already-unloaded ws — no in-memory slot to release.
    result = mgr.delete(ws_id, name="closed-then-deleted")
    assert result is False
    # But the event MUST fire — the dashboard hasn't seen the row drop yet.
    closed_events = adapter.events_of("closed")
    assert len(closed_events) == 2
    delete_event = closed_events[1]
    assert delete_event.ws_id == ws_id
    assert delete_event.reason == "deleted"
    assert delete_event.name == "closed-then-deleted"


def test_delete_falls_back_to_workstream_name_when_caller_omits() -> None:
    """When the caller doesn't snapshot a name, the event payload
    falls back to the live workstream's name so operator toasts on
    the global queue still carry useful context."""
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1", name="auto-name-from-ws")

    mgr.delete(ws.id)  # no name kwarg
    closed_events = adapter.events_of("closed")
    assert closed_events[-1].name == "auto-name-from-ws"


def test_delete_returns_false_for_unknown_ws_id() -> None:
    """Idempotent on an absent + never-loaded ws_id — still fires
    the event so a dashboard with a stale row can drop it, and
    returns False so the caller knows nothing was tracked."""
    mgr, adapter, _ = _make_manager()
    result = mgr.delete("never-existed")
    assert result is False
    # Event still fires — a stale dashboard entry is exactly the
    # case where this matters.
    closed_events = adapter.events_of("closed")
    assert len(closed_events) == 1
    assert closed_events[0].ws_id == "never-existed"
    assert closed_events[0].reason == "deleted"


def test_delete_without_event_emitter_is_quiet() -> None:
    """When the manager is constructed without an event emitter
    (e.g. the no-op kind in tests), ``delete`` releases the slot
    silently — the no-emitter branch must not raise."""
    mgr, adapter, _ = _make_manager(event_emitter=None)
    ws = mgr.create(user_id="u1")
    result = mgr.delete(ws.id)
    assert result is True
    # Adapter still gets cleanup_ui — that's the kind-side surface,
    # distinct from the lifecycle-event side channel.
    assert ws.id in adapter.cleaned_up


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


def test_set_state_updates_storage_and_fires_observer() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.set_state(ws.id, WorkstreamState.RUNNING)
    assert ws.state == WorkstreamState.RUNNING
    assert (ws.id, WorkstreamState.RUNNING.value) in storage.state_updates
    state_events = adapter.events_of("state")
    assert any(e.ws_id == ws.id and e.state == WorkstreamState.RUNNING for e in state_events)


def test_set_state_unknown_ws_is_noop() -> None:
    mgr, adapter, _ = _make_manager()
    mgr.set_state("ghost", WorkstreamState.RUNNING)
    assert adapter.events_of("state") == []


# ---------------------------------------------------------------------------
# close_idle / list_all / get / count
# ---------------------------------------------------------------------------


def test_close_idle_closes_old_idle_and_keeps_active() -> None:
    mgr, _, _ = _make_manager()
    old = mgr.create(user_id="u1")
    fresh = mgr.create(user_id="u1")
    running = mgr.create(user_id="u1")
    old.last_active = time.monotonic() - 100
    running.state = WorkstreamState.RUNNING
    running.last_active = time.monotonic() - 100

    closed = mgr.close_idle(max_age_seconds=10.0)
    assert old.id in closed
    assert mgr.get(old.id) is None
    # Fresh stays (not old enough).
    assert mgr.get(fresh.id) is not None
    # Running stays (wrong state).
    assert mgr.get(running.id) is not None


def test_close_idle_on_empty_manager_returns_empty_list() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.close_idle(max_age_seconds=1.0) == []


def test_close_idle_runs_db_orphan_pass() -> None:
    """DB rows of this kind that aren't loaded into the manager get
    bulk-closed when their ``updated`` is older than the cutoff.  Catches
    the orphan-after-process-restart case the original close_idle missed."""
    mgr, _, storage = _make_manager()
    # Orphan rows live in storage but were never loaded via mgr.create.
    storage.register_workstream(
        "orphan-1",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )
    storage.register_workstream(
        "orphan-2",
        kind=WorkstreamKind.INTERACTIVE,
        state="thinking",
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert set(closed) == {"orphan-1", "orphan-2"}
    assert ("orphan-1", "closed") in storage.state_updates
    assert ("orphan-2", "closed") in storage.state_updates
    assert storage.rows["orphan-1"].state == "closed"
    assert storage.rows["orphan-2"].state == "closed"


def test_close_idle_excludes_loaded_workstreams_from_db_pass() -> None:
    """A workstream loaded into memory must NOT be reaped by the DB
    orphan pass even when its storage ``updated`` is stale — the
    in-memory pass owns those.  Verifies the exclude_ws_ids plumbing."""
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    # Force the storage row's ``updated`` to look stale.  In practice
    # ``set_state`` would bump it, but we're simulating a long-running
    # active workstream whose updated drifted older than the cutoff.
    storage.rows[ws.id].updated = "2020-01-01T00:00:00"

    # Huge timeout so the in-memory IDLE pass skips it (stays loaded).
    closed = mgr.close_idle(max_age_seconds=10_000.0)

    assert ws.id not in closed
    assert mgr.get(ws.id) is not None
    assert storage.rows[ws.id].state == "idle"


def test_close_idle_filters_db_orphans_by_kind() -> None:
    """An interactive manager's close_idle must not touch coordinator
    rows in storage and vice versa.  Without this filter, both managers
    would race to close each other's rows."""
    mgr, _, storage = _make_manager()  # interactive by default
    storage.register_workstream(
        "coord-orphan",
        kind=WorkstreamKind.COORDINATOR,
        updated="2020-01-01T00:00:00",
    )
    storage.register_workstream(
        "interactive-orphan",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert "interactive-orphan" in closed
    assert "coord-orphan" not in closed
    assert storage.rows["coord-orphan"].state == "idle"
    assert storage.rows["interactive-orphan"].state == "closed"


def test_close_idle_protects_rows_owned_by_live_services() -> None:
    """Multi-node correctness: rows whose ``node_id`` matches a service
    with a recent heartbeat must NOT be reaped, even when *this* manager
    is on a different node — the alive peer may legitimately have them
    loaded.  Liveness is the rendezvous router's primitive (post-PR-#384);
    using it here keeps reap scoping aligned with routing.

    Default ``_make_manager`` uses an INTERACTIVE adapter, which derives
    ``service_type='server'`` — so live_services seeded under "server"
    are what the manager queries."""
    mgr, _, storage = _make_manager()
    storage.live_services["server"] = ["node-b"]  # only node-b is alive
    storage.register_workstream(
        "ours-from-dead-node",
        node_id="node-a",  # dead pod (not in live_services)
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )
    storage.register_workstream(
        "theirs-still-alive",
        node_id="node-b",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert closed == ["ours-from-dead-node"]
    assert storage.rows["ours-from-dead-node"].state == "closed"
    assert storage.rows["theirs-still-alive"].state == "idle"


def test_close_idle_protects_live_services_for_coordinator_kind() -> None:
    """Coord-side parity: a coordinator manager derives
    ``service_type='console'``, so live_services seeded under "console"
    are what gets queried.  Mirrors the interactive test to ensure both
    halves of the production wiring are exercised."""
    coord_adapter = FakeAdapter(kind=WorkstreamKind.COORDINATOR)
    mgr, _, storage = _make_manager(coord_adapter)
    storage.live_services["console"] = ["console"]  # console is alive
    storage.register_workstream(
        "alive-console-coord",
        node_id="console",
        kind=WorkstreamKind.COORDINATOR,
        updated="2020-01-01T00:00:00",
    )
    storage.register_workstream(
        "dead-console-coord",
        node_id="dead-console-instance",  # not in live set
        kind=WorkstreamKind.COORDINATOR,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert closed == ["dead-console-coord"]
    assert storage.rows["alive-console-coord"].state == "idle"
    assert storage.rows["dead-console-coord"].state == "closed"


def test_close_idle_reaps_rows_with_null_node_id() -> None:
    """A row with no ``node_id`` has no owner identity — age alone gates
    the reap.  Defends against a NULL silently propagating through ``NOT
    IN (live)`` and protecting orphans forever."""
    mgr, _, storage = _make_manager()
    storage.live_services["server"] = ["node-a"]
    storage.register_workstream(
        "no-owner",
        node_id=None,
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert closed == ["no-owner"]


def test_close_idle_reaps_all_orphans_when_no_peers_alive() -> None:
    """When ``list_services`` returns an empty list (no heartbeating
    peers), every stale orphan is unprotected and gets reaped.  This is
    the cold-start / single-process / dead-cluster-recovery case."""
    mgr, _, storage = _make_manager()
    # storage.live_services["server"] left empty — no peers heartbeating
    storage.register_workstream(
        "any-node-1",
        node_id="node-a",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )
    storage.register_workstream(
        "any-node-2",
        node_id="node-b",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert set(closed) == {"any-node-1", "any-node-2"}


def test_close_idle_skips_pass_2_when_list_services_fails() -> None:
    """Conservative fallback: if list_services fails we can't enumerate
    live owners safely, so pass 2 must skip rather than reap blind.  Pass
    1 (in-memory IDLE) still runs."""
    mgr, _, storage = _make_manager()
    storage.list_services_raises = True
    storage.register_workstream(
        "would-be-orphan",
        node_id="node-a",
        kind=WorkstreamKind.INTERACTIVE,
        updated="2020-01-01T00:00:00",
    )

    closed = mgr.close_idle(max_age_seconds=0.0)

    assert closed == []
    assert storage.rows["would-be-orphan"].state == "idle"


def test_list_all_returns_creation_order() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    c = mgr.create(user_id="u1")
    assert [ws.id for ws in mgr.list_all()] == [a.id, b.id, c.id]


def test_count_reflects_live_workstreams() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.count == 0
    a = mgr.create(user_id="u1")
    assert mgr.count == 1
    mgr.create(user_id="u1")
    assert mgr.count == 2
    mgr.close(a.id)
    assert mgr.count == 1


# ---------------------------------------------------------------------------
# Eviction transport — adapter contract
# ---------------------------------------------------------------------------


def test_eviction_fires_emit_closed_to_adapter_transport() -> None:
    mgr, adapter, _ = _make_manager(max_active=2)
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    mgr.create(user_id="u1")  # triggers eviction of 'a'
    closed_events = adapter.events_of("closed")
    evicted = [e for e in closed_events if e.ws_id == a.id]
    assert evicted and evicted[0].reason == "evicted"
    assert a.id in adapter.cleaned_up


def test_manual_close_uses_closed_reason() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    closed_events = [e for e in adapter.events_of("closed") if e.ws_id == ws.id]
    assert closed_events and closed_events[0].reason == "closed"


# ---------------------------------------------------------------------------
# CLI focus state
# ---------------------------------------------------------------------------


def test_active_id_seeded_on_first_create() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.active_id is None
    ws = mgr.create(user_id="u1")
    assert mgr.active_id == ws.id
    assert mgr.get_active() is ws


def test_active_id_unchanged_on_subsequent_creates() -> None:
    mgr, _, _ = _make_manager()
    first = mgr.create(user_id="u1")
    mgr.create(user_id="u1")
    # Creating a second workstream doesn't change focus.
    assert mgr.active_id == first.id


def test_switch_moves_active_id() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    assert mgr.active_id == a.id
    result = mgr.switch(b.id)
    assert result is b
    assert mgr.active_id == b.id
    # Unknown id → no change.
    assert mgr.switch("ghost") is None
    assert mgr.active_id == b.id


def test_switch_by_index_uses_1_based_ordering() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    c = mgr.create(user_id="u1")
    assert mgr.switch_by_index(2) is b
    assert mgr.active_id == b.id
    assert mgr.switch_by_index(3) is c
    assert mgr.switch_by_index(0) is None
    assert mgr.switch_by_index(99) is None
    # Still on c after the invalid switches.
    assert mgr.active_id == c.id
    assert mgr.index_of(a.id) == 1
    assert mgr.index_of("ghost") == 0


def test_active_id_moves_on_eviction() -> None:
    mgr, _, _ = _make_manager(max_active=2)
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    # First create seeded active to a; eviction of a must re-home active.
    assert mgr.active_id == a.id
    c = mgr.create(user_id="u1")  # evicts a
    assert mgr.active_id != a.id
    assert mgr.active_id in (mgr._order[0], c.id)  # type: ignore[attr-defined]


def test_active_id_moves_on_close() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    mgr.switch(a.id)
    mgr.close(a.id)
    assert mgr.active_id == b.id


def test_eviction_count_tracks_evictions() -> None:
    mgr, _, _ = _make_manager(max_active=2)
    assert mgr.eviction_count == 0
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    mgr.create(user_id="u1")  # evicts a
    assert mgr.eviction_count == 1


# ---------------------------------------------------------------------------
# Storage access patterns with mocks (defensive coverage)
# ---------------------------------------------------------------------------


def test_create_uses_configured_node_id() -> None:
    storage = MagicMock()
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter, storage=storage, max_active=3, node_id="node-xyz", event_emitter=adapter
    )
    mgr.create(user_id="u1")
    assert storage.register_workstream.call_args.kwargs["node_id"] == "node-xyz"


# ---------------------------------------------------------------------------
# StateWriter integration — bug-3 invariant under write-behind
# ---------------------------------------------------------------------------


class TestSessionManagerWithStateWriter:
    """When a buffered StateWriter is wired in, ``set_state`` no longer
    blocks ``ws._lock`` on a sync DB write — but ``close`` must still
    leave 'closed' as the durable final state for the row, never
    a buffered transient that flushes after close."""

    def _make_with_writer(
        self, *, flush_interval: float = 0.05
    ) -> tuple[SessionManager, FakeStorage, Any]:
        from turnstone.core.state_writer import StateWriter

        storage = FakeStorage()
        writer = StateWriter(storage, flush_interval=flush_interval)
        adapter = FakeAdapter()
        mgr = SessionManager(
            adapter,
            storage=storage,
            max_active=3,
            state_writer=writer,
            event_emitter=adapter,
        )
        return mgr, storage, writer

    def test_set_state_buffers_through_writer(self) -> None:
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        # Initial register write may have landed; clear and inspect afterwards.
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Long flush_interval → buffered, no sync write yet.
        assert storage.state_updates == []
        # Drain the buffer manually (test stand-in for the periodic flush).
        writer.flush()
        assert (ws.id, "running") in storage.state_updates

    def test_set_state_error_flushes_sync(self) -> None:
        """Terminal ERROR transitions must be durable on return — error
        surfacing paths (audit, dashboard) need the row to reflect
        the failure before any observer sees it."""
        mgr, storage, _writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.ERROR, error_msg="boom")
        # No buffer-drain needed — error path bypasses.
        assert (ws.id, "error") in storage.state_updates

    def test_close_after_buffered_set_state_writes_closed_not_transient(self) -> None:
        """The bug-3 invariant under write-behind. close() must call
        state_writer.discard BEFORE its sync 'closed' write, so any
        buffered 'running' state can't be flushed AFTER 'closed'.
        """
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        # Buffer a transient transition.
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Now close — must drain/discard the buffer + write 'closed' sync.
        ok = mgr.close(ws.id)
        assert ok is True
        # Force a flush; the buffered 'running' must already be gone.
        writer.flush()

        # The final write for ws.id must be 'closed', and 'running' must
        # NOT have landed in storage at all.
        ws_writes = [s for w, s in storage.state_updates if w == ws.id]
        assert "running" not in ws_writes, f"buffered running flushed after close: {ws_writes}"
        assert ws_writes[-1] == "closed"

    def test_close_idle_after_buffered_set_state_writes_closed(self) -> None:
        """Same invariant via close_idle (the idle-cleanup batch path)."""
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Force the workstream into IDLE state before close_idle considers
        # it (close_idle gates on ws.state, not the buffered state).
        with mgr._lock:
            mgr._workstreams[ws.id].state = WorkstreamState.IDLE
            mgr._workstreams[ws.id].last_active = 0.0  # ancient → stale

        closed = mgr.close_idle(max_age_seconds=0)
        assert ws.id in closed
        writer.flush()
        ws_writes = [s for w, s in storage.state_updates if w == ws.id]
        assert "running" not in ws_writes, f"buffered running flushed after close_idle: {ws_writes}"
        assert ws_writes[-1] == "closed"

    def test_set_state_after_close_short_circuits(self) -> None:
        """The existing tombstone (ws._closed) check must still fire
        before reaching state_writer.record — set_state after close
        must NOT enqueue 'running' to the buffer (which would then
        get flushed and resurrect the closed row)."""
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        ws_id = ws.id

        # Close first — sets ws._closed=True synchronously.
        mgr.close(ws_id)
        storage.state_updates.clear()

        # A late set_state call (e.g. from a worker still cleaning up).
        # Should NOT buffer 'running' for this ws.
        mgr.set_state(ws_id, WorkstreamState.RUNNING)
        writer.flush()
        ws_writes = [s for w, s in storage.state_updates if w == ws_id]
        assert "running" not in ws_writes, (
            f"set_state after close enqueued through buffer: {ws_writes}"
        )


# ---------------------------------------------------------------------------
# Multi-subscriber observer — subscribe_to_state / unsubscribe_from_state
# ---------------------------------------------------------------------------


class TestStateSubscribers:
    """Multi-subscriber observer for ``set_state``.

    Used by the CLI's background-attention notifier and by
    ``SameNodeChildSource``. Subscribe / unsubscribe must be safe under
    concurrent dispatch, and dispatch must not skip / repeat callbacks
    when subscribers register or unregister mid-iteration.
    """

    def test_subscribe_fires_on_set_state(self) -> None:
        mgr, _, _ = _make_manager()
        ws = mgr.create(user_id="u1", name="ws", skill=None)
        events: list[tuple[str, str]] = []

        def cb(ws_id: str, state: WorkstreamState) -> None:
            events.append((ws_id, state.value))

        mgr.subscribe_to_state(cb)
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert events == [(ws.id, "running")]

    def test_unsubscribe_stops_firing(self) -> None:
        mgr, _, _ = _make_manager()
        ws = mgr.create(user_id="u1", name="ws", skill=None)
        events: list[str] = []

        def cb(_ws_id: str, state: WorkstreamState) -> None:
            events.append(state.value)

        mgr.subscribe_to_state(cb)
        mgr.unsubscribe_from_state(cb)
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert events == []

    def test_unsubscribe_unknown_is_noop(self) -> None:
        mgr, _, _ = _make_manager()
        # Doesn't raise.
        mgr.unsubscribe_from_state(lambda *_: None)

    def test_multiple_subscribers_fire_in_registration_order(self) -> None:
        mgr, _, _ = _make_manager()
        ws = mgr.create(user_id="u1", name="ws", skill=None)
        order: list[int] = []

        def make(i: int) -> Callable[[str, WorkstreamState], None]:
            def cb(_ws_id: str, _state: WorkstreamState) -> None:
                order.append(i)

            return cb

        mgr.subscribe_to_state(make(1))
        mgr.subscribe_to_state(make(2))
        mgr.subscribe_to_state(make(3))
        mgr.set_state(ws.id, WorkstreamState.IDLE)
        assert order == [1, 2, 3]

    def test_subscriber_exception_does_not_block_others(self) -> None:
        mgr, _, _ = _make_manager()
        ws = mgr.create(user_id="u1", name="ws", skill=None)
        survived: list[str] = []

        def boom(*_: Any) -> None:
            raise RuntimeError("subscriber crash")

        def good(_ws_id: str, state: WorkstreamState) -> None:
            survived.append(state.value)

        mgr.subscribe_to_state(boom)
        mgr.subscribe_to_state(good)
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert survived == ["running"]

    @pytest.mark.parametrize("n_threads", [10, 50])
    def test_concurrent_subscribe(self, n_threads: int) -> None:
        """Subscribe from many threads; all callbacks land in the list.

        Validates the lock around mutation — without it the underlying
        list.append could lose entries under contention.
        """
        mgr, _, _ = _make_manager()
        callbacks = [lambda *_, i=i: None for i in range(n_threads)]
        threads = [threading.Thread(target=mgr.subscribe_to_state, args=(cb,)) for cb in callbacks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Snapshot under the lock to read the count safely.
        with mgr._state_subscribers_lock:
            assert len(mgr._state_subscribers) == n_threads

    def test_subscribe_during_dispatch_does_not_corrupt_iteration(self) -> None:
        """A subscriber that calls subscribe_to_state during its own
        callback must not affect the in-flight dispatch (snapshot
        isolation). This is the bug-1 invariant: mutation during
        iteration can't shift the iterator's index because dispatch
        iterates a snapshot, not the live list.
        """
        mgr, _, _ = _make_manager()
        ws = mgr.create(user_id="u1", name="ws", skill=None)
        fired: list[str] = []

        def late(_ws_id: str, state: WorkstreamState) -> None:
            fired.append("late:" + state.value)

        def first(_ws_id: str, state: WorkstreamState) -> None:
            fired.append("first:" + state.value)
            mgr.subscribe_to_state(late)  # mid-dispatch addition

        mgr.subscribe_to_state(first)
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # ``late`` was added during dispatch but the snapshot was
        # already frozen — so it doesn't fire on this round.
        assert fired == ["first:running"]
        # Next round it does fire, in registration order after first.
        fired.clear()
        mgr.set_state(ws.id, WorkstreamState.IDLE)
        assert fired == ["first:idle", "late:idle"]
