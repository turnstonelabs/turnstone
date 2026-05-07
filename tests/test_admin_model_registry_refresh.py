"""Console-side coord_registry auto-refresh on model-definition CRUD + reload.

The console builds ``app.state.coord_registry`` once at lifespan startup
and the coordinator session factory closes over that exact instance.
Without these refresh hooks, an admin who edits a model definition
through the UI sees the DB change immediately but coordinator sessions
keep calling the prior model name — the on-disk truth diverges from the
in-process registry until the console is restarted.

These tests cover both the helper (``_refresh_coord_registry``)
and the four wired endpoints (create / update / delete / explicit reload)
to lock in:

- in-place mutation: ``coord_registry`` object identity is preserved
  across refreshes (factory closure must not be invalidated);
- failure isolation: a load or reload failure leaves the existing
  registry intact rather than tearing down a working coordinator;
- no-op safety: the helper short-circuits when ``coord_registry`` is
  ``None`` so a coord-less console (no model rows at boot) doesn't
  500 on routine model-definition CRUD.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import (
    _maybe_bootstrap_coord_subsystem,
    _refresh_coord_registry,
    admin_create_model_definition,
    admin_delete_model_definition,
    admin_model_reload,
    admin_update_model_definition,
)
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.storage._sqlite import SQLiteBackend


def _bootstrap_app(**overrides: Any) -> Any:
    """Build a fake ``app`` with the ``state`` attrs the bootstrap helper
    inspects.  Defaults match a freshly-installed console (no coord
    subsystem yet) with all required prereqs (collector, console_metrics,
    config_store) populated as MagicMocks.  Tests pass overrides to
    suppress individual prereqs or pre-set ``coord_mgr`` etc.
    """
    state_kwargs: dict[str, Any] = {
        "coord_mgr": None,
        "coord_adapter": None,
        "coord_registry": None,
        "coord_registry_error": "",
        "coord_state_writer": None,
        "coord_idle_observer": None,
        "config_store": MagicMock(),
        "collector": MagicMock(),
        "console_metrics": MagicMock(),
    }
    state_kwargs.update(overrides)
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "models.db"))


def _seed_model_def(
    storage: SQLiteBackend,
    *,
    definition_id: str,
    alias: str,
    model: str,
    base_url: str = "http://localhost:8000/v1",
    enabled: bool = True,
) -> None:
    """Insert a model definition row directly via the storage API."""
    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model=model,
        provider="openai-compatible",
        base_url=base_url,
        api_key="sk-test",
        context_window=8192,
        capabilities="{}",
        enabled=enabled,
        created_by="admin",
    )


def _make_config(alias: str, model: str) -> ModelConfig:
    return ModelConfig(
        alias=alias,
        base_url="http://localhost:8000/v1",
        api_key="sk-test",
        model=model,
        context_window=8192,
        provider="openai-compatible",
        source="db",
    )


def _make_registry(
    *,
    alias: str = "local",
    model: str = "old-model",
    extras: dict[str, str] | None = None,
) -> ModelRegistry:
    """Build a real ModelRegistry seeded with ``alias`` (the default) plus
    any ``extras`` (alias → model).  ``ModelRegistry.__init__`` rejects an
    empty model dict so tests that exercise the helper need at least one
    entry; pass ``extras`` for multi-alias scenarios (e.g. delete-by-alias).
    """
    configs = {alias: _make_config(alias, model)}
    for extra_alias, extra_model in (extras or {}).items():
        configs[extra_alias] = _make_config(extra_alias, extra_model)
    return ModelRegistry(configs, default=alias)


class _AppState:
    """Shim mirroring Starlette's ``app.state`` for direct helper tests."""

    coord_registry: ModelRegistry | None = None


# ---------------------------------------------------------------------------
# Helper-level tests — ``_refresh_coord_registry`` semantics
# ---------------------------------------------------------------------------


def test_helper_rebuilds_registry_from_db(storage: SQLiteBackend) -> None:
    """Helper pulls the latest DB rows into the existing registry."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    _refresh_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "new-model"


def test_helper_preserves_object_identity(storage: SQLiteBackend) -> None:
    """The factory closes over the registry object — refresh must mutate
    in place rather than swap the attribute."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry()
    before = id(state.coord_registry)

    _refresh_coord_registry(state, storage)

    assert id(state.coord_registry) == before


def test_helper_noop_when_coord_registry_none(storage: SQLiteBackend) -> None:
    """Console boot with no model rows leaves coord_registry = None.
    The helper must not 500 in that state — CRUD that lands the FIRST
    row would otherwise fail before the operator can recover."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    state = _AppState()
    state.coord_registry = None

    _refresh_coord_registry(state, storage)  # must not raise

    assert state.coord_registry is None


def test_helper_preserves_registry_when_load_fails(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error from ``load_model_registry`` (e.g. config.toml
    parse failure, programming bug) must not tear down a working
    registry — log + leave the existing instance intact."""
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    def _boom(**_kw: Any) -> ModelRegistry:
        raise RuntimeError("simulated loader failure")

    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", _boom)
    _refresh_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "old-model"


def test_helper_preserves_registry_when_strict_load_fails(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_model_registry`` normally swallows storage read errors and
    would return a config.toml-only registry on a transient DB outage —
    applying that via ``reload()`` would silently drop every DB-sourced
    alias.  The helper passes ``strict=True`` so the loader re-raises
    instead, the helper's outer except catches it, and the existing
    registry survives intact."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="db-model")
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="db-model")

    def _broken(**_kw: Any) -> Any:
        raise RuntimeError("simulated transient DB outage")

    monkeypatch.setattr(storage, "list_model_definitions", _broken)
    _refresh_coord_registry(state, storage)

    assert state.coord_registry is not None
    # Existing registry untouched — strict=True surfaced the storage
    # error to the helper before the loader's silent fallback could
    # produce a truncated registry for reload().
    assert state.coord_registry.get_config("local").model == "db-model"


def test_helper_preserves_registry_when_no_enabled_rows(storage: SQLiteBackend) -> None:
    """All rows disabled/deleted: ModelRegistry.__init__ rejects an empty
    model dict (raises ValueError).  Helper must catch and preserve the
    existing registry so coord stays usable while admin restores rows."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m", enabled=False)
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="cached-model")

    _refresh_coord_registry(state, storage)

    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "cached-model"


# ---------------------------------------------------------------------------
# First-row bootstrap tests — ``_maybe_bootstrap_coord_subsystem`` semantics.
# A console booted with no model rows leaves coord_mgr = None; the operator
# adding the first row at runtime must promote the subsystem to ready
# without a console restart.
# ---------------------------------------------------------------------------


def test_bootstrap_noop_when_coord_mgr_already_built(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent fast-path — already-bootstrapped subsystem must not
    re-stand-up a second SessionManager / StateWriter pair."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app(coord_mgr=MagicMock())  # subsystem already built

    calls: list[Any] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *a, **kw: calls.append(a),
    )
    _maybe_bootstrap_coord_subsystem(app, storage)
    assert calls == []


@pytest.mark.parametrize("missing_attr", ["config_store", "collector", "console_metrics"])
def test_bootstrap_noop_when_prerequisites_missing(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
    missing_attr: str,
) -> None:
    """Each strictly-required ``app.state`` attr (config_store, collector,
    console_metrics) must individually short-circuit the bootstrap to a
    no-op — partial init / test harnesses don't have the full set, and a
    CRUD write that already landed mustn't 500 on a missing prereq."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app(**{missing_attr: None})
    calls: list[Any] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *a, **kw: calls.append(a),
    )
    _maybe_bootstrap_coord_subsystem(app, storage)
    assert calls == []
    assert app.state.coord_mgr is None


def test_bootstrap_records_error_when_no_rows(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All rows disabled (or none seeded) — load_model_registry raises
    ValueError.  Helper records the message on app.state so the
    coord-endpoint 503 surfaces a current diagnosis instead of a stale
    one from boot."""
    from turnstone.console import server as server_module

    app = _bootstrap_app()
    calls: list[Any] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *a, **kw: calls.append(a),
    )
    _maybe_bootstrap_coord_subsystem(app, storage)
    assert calls == []
    assert "No model definitions found" in app.state.coord_registry_error


def test_bootstrap_calls_subsystem_builder_on_first_row(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row exists ⇒ helper loads the registry, hands it to the
    subsystem builder, and the builder stamps it on app.state.  Mirrors
    the post-build invariant the real ``_bootstrap_coord_subsystem``
    establishes (coord_registry set iff coord_mgr set) so the stale
    boot-time error string clears as part of the same commit step."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app(coord_registry_error="stale boot-time message")
    captured: dict[str, Any] = {}

    def _fake_build(app_arg: Any, _storage: Any, _cfg: Any, registry_arg: Any) -> None:
        captured["app"] = app_arg
        captured["registry"] = registry_arg
        # Simulate the real builder's final commit step: stamp registry
        # + clear stale error + set coord_mgr atomically.
        app_arg.state.coord_registry = registry_arg
        app_arg.state.coord_registry_error = ""
        app_arg.state.coord_mgr = MagicMock()

    monkeypatch.setattr(server_module, "_bootstrap_coord_subsystem", _fake_build)
    _maybe_bootstrap_coord_subsystem(app, storage)
    assert captured["app"] is app
    assert captured["registry"].has_alias("local")
    assert app.state.coord_registry is captured["registry"]
    assert app.state.coord_registry_error == ""


def test_bootstrap_replaces_stale_error_on_builder_failure(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A builder failure after a successful registry load must not leave
    the stale "no model definitions" message on app.state — that
    diagnosis is demonstrably wrong (rows ARE present, the build failed
    for a different reason).  Replacement message must surface the
    actual exception type so operators can correlate with logs."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app(
        coord_registry_error=(
            "No model definitions found. Provide --model, configure [models.*] "
            "in config.toml, or add model definitions in the admin panel."
        )
    )

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("simulated builder failure")

    monkeypatch.setattr(server_module, "_bootstrap_coord_subsystem", _boom)
    _maybe_bootstrap_coord_subsystem(app, storage)  # must not raise
    assert app.state.coord_mgr is None
    # Stale "no models" message replaced.
    assert "No model definitions found" not in app.state.coord_registry_error
    # New message mentions the actual failure class so the 503 banner
    # gives operators something actionable beyond "look at logs".
    assert "RuntimeError" in app.state.coord_registry_error
    assert "failed to initialise" in app.state.coord_registry_error


def test_bootstrap_tears_down_partial_state_on_builder_failure(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the builder partially stamps handles on app.state and then
    raises, the helper must call the teardown path so a subsequent
    retry doesn't leak a StateWriter daemon / observer subscription."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app()

    state_writer = MagicMock()
    idle_observer = MagicMock()
    coord_adapter = MagicMock()

    def _partial_then_boom(app_arg: Any, *_a: Any, **_kw: Any) -> None:
        # Mirror the real builder's stamp-immediately-after-start order:
        # StateWriter spawned + stamped before SessionManager validates.
        app_arg.state.coord_state_writer = state_writer
        app_arg.state.coord_idle_observer = idle_observer
        app_arg.state.coord_adapter = coord_adapter
        raise RuntimeError("simulated mid-build failure")

    monkeypatch.setattr(server_module, "_bootstrap_coord_subsystem", _partial_then_boom)
    _maybe_bootstrap_coord_subsystem(app, storage)
    # Teardown ran for each partially-stamped handle.
    state_writer.shutdown.assert_called_once()
    idle_observer.shutdown.assert_called_once()
    coord_adapter.shutdown.assert_called_once()
    # And the app.state slots are reset so a retry sees a clean field.
    assert app.state.coord_state_writer is None
    assert app.state.coord_idle_observer is None
    assert app.state.coord_adapter is None
    assert app.state.coord_mgr is None
    assert app.state.coord_registry is None


def test_real_bootstrap_stands_up_subsystem_end_to_end(
    storage: SQLiteBackend,
) -> None:
    """End-to-end: the real ``_bootstrap_coord_subsystem`` constructs a
    working ``SessionManager`` against a real ``ConfigStore`` + real
    ``ClusterCollector`` when an operator adds the first model row to
    a freshly-installed console.

    This is the test that reproduces the user-reported bug — without it,
    all the bootstrap helper-level tests can pass even if the real
    builder never actually completes (the helper-level tests
    monkeypatch the builder out).  Asserts the post-bootstrap invariant
    that ``_require_coord_mgr`` relies on: ``coord_mgr`` is a real
    SessionManager and ``coord_registry_error`` has been cleared.
    """
    from turnstone.console import server as server_module
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.core.config_store import ConfigStore
    from turnstone.core.session_manager import SessionManager

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    config_store = ConfigStore(storage)
    # Disable the idle-cleanup daemon for this test — it has no
    # stop_event hook in the bootstrap (the loop runs until process
    # termination) so leaving the default 120-minute timeout would
    # leak a daemon thread across every test run.
    config_store.set("server.workstream_idle_timeout", 0)
    # ClusterCollector is constructed but NOT started — start() spawns
    # network discovery + SSE manager threads we don't need for this
    # test.  ensure_console_pseudo_node() (called by the bootstrap via
    # start_child_event_fanout) operates on the in-memory snapshot map
    # without requiring the discovery loop to be live.
    collector = ClusterCollector(storage=storage)
    # Snapshot ConsoleCoordinatorUI's class attrs so the test can
    # restore them on teardown — the bootstrap mutates them and they
    # persist across tests at process scope.
    saved_coord_mgr = ConsoleCoordinatorUI._coord_mgr
    saved_collector = ConsoleCoordinatorUI._collector
    saved_metrics = ConsoleCoordinatorUI._console_metrics

    app = SimpleNamespace(
        state=SimpleNamespace(
            coord_mgr=None,
            coord_adapter=None,
            coord_registry=None,
            coord_registry_error=(
                "No model definitions found. Provide --model, configure [models.*] "
                "in config.toml, or add model definitions in the admin panel."
            ),
            coord_state_writer=None,
            coord_idle_observer=None,
            config_store=config_store,
            collector=collector,
            console_metrics=ConsoleMetrics(),
            jwt_secret="x" * 32,
            console_url="http://127.0.0.1:8001",
        )
    )

    try:
        _maybe_bootstrap_coord_subsystem(app, storage)
        # The real builder ran and produced a working SessionManager.
        assert isinstance(app.state.coord_mgr, SessionManager)
        assert app.state.coord_adapter is not None
        # Registry stamped with the seeded alias.
        assert app.state.coord_registry is not None
        assert app.state.coord_registry.has_alias("local")
        # Stale boot-time error string cleared as part of the commit.
        assert app.state.coord_registry_error == ""
        # StateWriter daemon is alive — it's the load-bearing async
        # persistence layer for SessionManager state transitions.
        assert app.state.coord_state_writer is not None
        # Class-level wiring on ConsoleCoordinatorUI is the path
        # on_state_change / on_rename use to fan out to the dashboard.
        assert ConsoleCoordinatorUI._coord_mgr is app.state.coord_mgr
        assert ConsoleCoordinatorUI._collector is collector
    finally:
        # Tear down threads + subscriptions spawned by the bootstrap.
        # ``_teardown_partial_coord_subsystem`` does the same work the
        # runtime-bootstrap failure path does, so reusing it here also
        # exercises that helper end-to-end.
        server_module._teardown_partial_coord_subsystem(app)
        # Restore ConsoleCoordinatorUI class attrs so other tests in
        # the suite see them as they were before this test ran.
        ConsoleCoordinatorUI._coord_mgr = saved_coord_mgr
        ConsoleCoordinatorUI._collector = saved_collector
        ConsoleCoordinatorUI._console_metrics = saved_metrics


def test_real_bootstrap_rolls_back_partial_state_on_side_effect_failure(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``_bootstrap_coord_subsystem`` must roll back from
    locally-held handles when a side-effect step fails mid-build, so
    ``app.state`` is never stamped (no half-built subsystem visible)
    and the started ``StateWriter`` daemon is shut down (no leaked
    thread across retries).

    Exercises the bug-2 fix end-to-end: monkeypatches
    ``install_idle_nudge_watcher`` to raise, drives the real builder,
    and asserts (a) the exception propagates, (b) ``app.state`` shows
    a clean fresh-install state, (c) the started ``StateWriter`` is
    no longer alive.
    """
    from turnstone.console import server as server_module
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.core.config_store import ConfigStore

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    config_store = ConfigStore(storage)
    config_store.set("server.workstream_idle_timeout", 0)
    collector = ClusterCollector(storage=storage)
    saved_coord_mgr = ConsoleCoordinatorUI._coord_mgr
    saved_collector = ConsoleCoordinatorUI._collector
    saved_metrics = ConsoleCoordinatorUI._console_metrics

    app = SimpleNamespace(
        state=SimpleNamespace(
            coord_mgr=None,
            coord_adapter=None,
            coord_registry=None,
            coord_registry_error="boot-time stale message",
            coord_state_writer=None,
            coord_idle_observer=None,
            config_store=config_store,
            collector=collector,
            console_metrics=ConsoleMetrics(),
            jwt_secret="x" * 32,
            console_url="http://127.0.0.1:8001",
        )
    )

    # Monkeypatch a mid-build side-effect to fail AFTER StateWriter +
    # observer have started but BEFORE the atomic commit.  This is the
    # exact failure shape the new local-rollback path is designed to
    # handle cleanly.
    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("simulated mid-build subscription failure")

    monkeypatch.setattr("turnstone.console.server.install_idle_nudge_watcher", _boom, raising=False)
    # The bootstrap helper imports install_idle_nudge_watcher locally
    # at call time (inside the function), so we need to patch the
    # source module too — server.py's import is a name lookup against
    # the module each call.
    monkeypatch.setattr(
        "turnstone.core.idle_nudge_watcher.install_idle_nudge_watcher",
        _boom,
    )

    try:
        # ``_maybe_bootstrap_coord_subsystem`` swallows the exception,
        # logs it, and replaces the stale boot-time error string with
        # a builder-failure-specific one — but the underlying invariant
        # we're testing here is that the real builder cleaned up its
        # own partial side-effects so ``app.state`` is left clean.
        _maybe_bootstrap_coord_subsystem(app, storage)
        # No state stamped — atomic commit never reached.
        assert app.state.coord_mgr is None
        assert app.state.coord_registry is None
        assert app.state.coord_state_writer is None
        assert app.state.coord_idle_observer is None
        assert app.state.coord_adapter is None
        # ConsoleCoordinatorUI class attrs were never stamped because
        # they sit AFTER the side-effect phase — local-rollback never
        # had to touch them, but the post-failure state still matches
        # the lifespan's clean state.
        assert ConsoleCoordinatorUI._coord_mgr is None
        # The error string surfaces the actual failure cause, not the
        # stale boot-time "no models" message.
        assert "RuntimeError" in app.state.coord_registry_error
        assert "failed to initialise" in app.state.coord_registry_error
    finally:
        # Defensive — _maybe_bootstrap should already have torn down,
        # but call once more in case future drift introduces a leak.
        server_module._teardown_partial_coord_subsystem(app)
        ConsoleCoordinatorUI._coord_mgr = saved_coord_mgr
        ConsoleCoordinatorUI._collector = saved_collector
        ConsoleCoordinatorUI._console_metrics = saved_metrics


def test_bootstrap_atomic_commit_no_partial_visibility(
    storage: SQLiteBackend,
) -> None:
    """A concurrent reader scanning ``app.state`` while the bootstrap
    runs must never observe ``coord_mgr`` set with ``coord_registry``
    still ``None`` — that combination would surface a misleading
    "Restart the console after adding a model definition" 503 from
    :func:`_require_coord_mgr` even though the operator just
    successfully added a model.

    Drives the real builder while a separate thread polls
    ``coord_mgr`` / ``coord_registry`` in tight loops; if the bootstrap
    ever stamps ``coord_mgr`` before ``coord_registry``, the polling
    thread will catch it.
    """
    from turnstone.console import server as server_module
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.core.config_store import ConfigStore

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    config_store = ConfigStore(storage)
    config_store.set("server.workstream_idle_timeout", 0)
    collector = ClusterCollector(storage=storage)
    saved_coord_mgr = ConsoleCoordinatorUI._coord_mgr
    saved_collector = ConsoleCoordinatorUI._collector
    saved_metrics = ConsoleCoordinatorUI._console_metrics

    app = SimpleNamespace(
        state=SimpleNamespace(
            coord_mgr=None,
            coord_adapter=None,
            coord_registry=None,
            coord_registry_error="",
            coord_state_writer=None,
            coord_idle_observer=None,
            config_store=config_store,
            collector=collector,
            console_metrics=ConsoleMetrics(),
            jwt_secret="x" * 32,
            console_url="http://127.0.0.1:8001",
        )
    )

    stop_polling = threading.Event()
    violations: list[str] = []

    def _poll_for_partial_state() -> None:
        # Tight loop emulating ``_require_coord_mgr``'s read pattern
        # (coord_mgr first, then coord_registry).  Any iteration that
        # observes coord_mgr set with coord_registry still None is the
        # exact bug Copilot's first finding pointed at.
        while not stop_polling.is_set():
            mgr = app.state.coord_mgr
            reg = app.state.coord_registry
            if mgr is not None and reg is None:
                violations.append(f"mgr={mgr!r} reg={reg!r}")
                return

    poller = threading.Thread(target=_poll_for_partial_state, name="partial-state-poller")
    poller.start()
    try:
        _maybe_bootstrap_coord_subsystem(app, storage)
    finally:
        stop_polling.set()
        poller.join(timeout=2.0)
        server_module._teardown_partial_coord_subsystem(app)
        ConsoleCoordinatorUI._coord_mgr = saved_coord_mgr
        ConsoleCoordinatorUI._collector = saved_collector
        ConsoleCoordinatorUI._console_metrics = saved_metrics

    assert violations == [], (
        "concurrent reader observed coord_mgr set with coord_registry still None — "
        f"atomic commit invariant violated: {violations}"
    )


def test_bootstrap_lock_serialises_concurrent_calls(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two simultaneous CRUD writes both seeing ``coord_mgr is None``
    must serialise via ``_COORD_BOOTSTRAP_LOCK`` and the second caller
    must observe the post-build state on its inside-the-lock re-check —
    so the builder runs exactly once.  Without the lock + double-check,
    both threads enter the build and stamp duplicate SessionManager /
    StateWriter / observer triples on app.state.

    The synchronisation is deterministic, not wall-clock-based: an
    instrumented lock wrapper signals when a second acquirer arrives,
    so the test fails fast and reproducibly on slow CI rather than
    relying on a sleep long enough to "probably" let thread 2 reach
    the lock — a dependence the previous version was rightly criticised
    for.
    """
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    app = _bootstrap_app()
    build_count = 0
    count_lock = threading.Lock()
    in_build = threading.Event()
    release_build = threading.Event()

    def _slow_build(app_arg: Any, *_a: Any, **_kw: Any) -> None:
        nonlocal build_count
        with count_lock:
            build_count += 1
            is_first = build_count == 1
        if is_first:
            # Hold inside the build so the second thread is forced to
            # queue at the lock — without the lock it would race ahead
            # and increment build_count to 2.
            in_build.set()
            release_build.wait(timeout=2.0)
        # Mirror the real builder's commit step.
        app_arg.state.coord_mgr = MagicMock()
        app_arg.state.coord_registry = MagicMock()

    monkeypatch.setattr(server_module, "_bootstrap_coord_subsystem", _slow_build)

    # Instrumented wrapper: delegates to a real ``threading.Lock`` so
    # the production ``with _COORD_BOOTSTRAP_LOCK:`` block keeps doing
    # genuine serialisation work, but counts arrivals so the main
    # thread can wait deterministically until thread 2 is at the lock
    # before releasing thread 1.  If the production code drops the
    # ``with`` block entirely, the wrapper is never entered, the
    # arrival event never fires, and the assertion below times out
    # with a clear error rather than the subtler false-pass a sleep
    # would allow.
    real_lock = threading.Lock()
    arrivals_lock = threading.Lock()
    arrivals = 0
    second_waiter_arrived = threading.Event()

    class _InstrumentedLock:
        def __enter__(self) -> Any:
            nonlocal arrivals
            with arrivals_lock:
                arrivals += 1
                arrival_index = arrivals
            if arrival_index >= 2:
                second_waiter_arrived.set()
            real_lock.acquire()
            return self

        def __exit__(self, *_exc: Any) -> None:
            real_lock.release()

    monkeypatch.setattr(server_module, "_COORD_BOOTSTRAP_LOCK", _InstrumentedLock())

    def _run() -> None:
        _maybe_bootstrap_coord_subsystem(app, storage)

    t1 = threading.Thread(target=_run, name="bootstrap-thread-1")
    t2 = threading.Thread(target=_run, name="bootstrap-thread-2")
    t1.start()
    assert in_build.wait(timeout=2.0), "thread 1 never entered the builder"
    t2.start()
    # Deterministic: block here until thread 2 has reached the lock
    # (or the wait times out, signalling the lock was bypassed entirely).
    assert second_waiter_arrived.wait(timeout=2.0), (
        "thread 2 never reached the lock — concurrency was not exercised, "
        "production code may be skipping the lock"
    )
    release_build.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert build_count == 1, (
        f"builder ran {build_count} times — lock failed to serialise concurrent calls"
    )


def test_helper_preserves_registry_on_reload_validation_error(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload that raises mid-mutation (e.g. validation guard) must
    leave the existing registry instance functional."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="new-model")
    state = _AppState()
    state.coord_registry = _make_registry(alias="local", model="old-model")

    def _broken_reload(*_a: Any, **_kw: Any) -> None:
        raise ValueError("simulated reload validation failure")

    monkeypatch.setattr(state.coord_registry, "reload", _broken_reload)
    _refresh_coord_registry(state, storage)

    # Existing registry still reachable; the broken reload was a no-op
    # at the public-facing level.
    assert state.coord_registry is not None
    assert state.coord_registry.get_config("local").model == "old-model"


# ---------------------------------------------------------------------------
# Endpoint-level integration tests — verify wiring
# ---------------------------------------------------------------------------


def _make_client(storage: SQLiteBackend, registry: ModelRegistry | None) -> TestClient:
    """Build a TestClient wired to the four model-definition endpoints.

    Uses the shared header-driven ``_AuthMiddleware`` from
    ``tests/_coord_test_helpers``; default headers below grant
    ``admin.models`` permission so the endpoint gate passes.
    """
    app = Starlette(
        routes=[
            Route(
                "/v1/api/admin/model-definitions",
                admin_create_model_definition,
                methods=["POST"],
            ),
            Route(
                "/v1/api/admin/model-definitions/reload",
                admin_model_reload,
                methods=["POST"],
            ),
            Route(
                "/v1/api/admin/model-definitions/{definition_id}",
                admin_update_model_definition,
                methods=["PUT"],
            ),
            Route(
                "/v1/api/admin/model-definitions/{definition_id}",
                admin_delete_model_definition,
                methods=["DELETE"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = registry
    # Reload endpoint also touches these — stub them so the test focuses
    # on the registry-refresh behaviour without dragging in a full
    # collector / proxy_client wiring.
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.models"})
    return client


def test_create_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """POST /api/admin/model-definitions bumps the in-process registry
    so newly-spawned coord sessions see the new alias immediately."""
    # Pre-existing alias (registry needs at least one row)
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "fast",
            "model": "fast-model",
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "sk-x",
            "context_window": 4096,
        },
    )
    assert resp.status_code == 200, resp.text
    assert registry.has_alias("fast")
    assert registry.get_config("fast").model == "fast-model"


def test_create_endpoint_bootstraps_subsystem_on_fresh_install(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-visible regression: a console booted with no model rows leaves
    coord_mgr unbuilt; the operator adding their first model via the
    admin panel must promote the subsystem to ready (no console restart).
    Before the fix, ``_refresh_coord_registry`` short-circuited on
    ``coord_registry is None`` and the dashboard's 503 banner persisted
    until the user restarted.
    """
    from turnstone.console import server as server_module

    # Fresh-install state: registry=None, coord_mgr=None, boot-time
    # error string set by the lifespan's ValueError catch.  Build the
    # app explicitly so the test can inspect ``app.state`` after the
    # request completes (TestClient's ``.app`` attribute is typed as
    # ASGIApp, which loses the ``.state`` accessor).
    app = Starlette(
        routes=[
            Route(
                "/v1/api/admin/model-definitions",
                admin_create_model_definition,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = None
    app.state.coord_mgr = None
    app.state.coord_registry_error = (
        "No model definitions found. Provide --model, configure [models.*] "
        "in config.toml, or add model definitions in the admin panel."
    )
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.config_store = MagicMock()
    app.state.console_metrics = MagicMock()
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.models"})

    captured: dict[str, Any] = {}

    def _fake_build(app_arg: Any, _storage: Any, _cfg: Any, registry_arg: Any) -> None:
        captured["registry"] = registry_arg
        # Mirror the real builder's commit step so the post-call asserts
        # see the same invariant a successful real bootstrap establishes.
        app_arg.state.coord_registry = registry_arg
        app_arg.state.coord_registry_error = ""
        app_arg.state.coord_mgr = MagicMock()

    monkeypatch.setattr(server_module, "_bootstrap_coord_subsystem", _fake_build)

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "first",
            "model": "first-model",
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "sk-x",
        },
    )
    assert resp.status_code == 200, resp.text
    # Bootstrap fired with a registry holding the just-added alias.
    assert "registry" in captured and captured["registry"].has_alias("first")
    # coord_mgr is now non-None (bootstrap completed) and the stale
    # boot-time error message has been cleared so subsequent 503s
    # don't lie about current state.
    assert app.state.coord_mgr is not None
    assert app.state.coord_registry_error == ""


def test_update_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """PUT swaps the underlying model name behind a stable alias — the
    user's reported regression."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="old-model")
    registry = _make_registry(alias="local", model="old-model")
    client = _make_client(storage, registry)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"model": "new-model"},
    )
    assert resp.status_code == 200, resp.text
    assert registry.get_config("local").model == "new-model"


def test_update_endpoint_skips_refresh_on_empty_body(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty PUT body must skip the registry refresh — the
    ``if updates:`` gate exists because ``load_model_registry`` is
    non-trivial and a no-op refresh on every PUT would burn cycles
    rebuilding state that hasn't changed.  Spy on the helper to lock
    the gate down: a regression that drops the conditional would
    register a call here and trip the assertion.
    """
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="locked-in")
    registry = _make_registry(alias="local", model="locked-in")
    client = _make_client(storage, registry)

    calls: list[tuple[Any, Any]] = []

    def _spy(app_state: Any, storage: Any) -> None:
        calls.append((app_state, storage))

    monkeypatch.setattr(server_module, "_refresh_coord_registry", _spy)

    resp = client.put("/v1/api/admin/model-definitions/m1", json={})
    assert resp.status_code == 200, resp.text
    assert calls == []  # gate held: empty body did not trigger a refresh


def test_create_rejects_invalid_api_surface(storage: SQLiteBackend) -> None:
    """POST with a bogus server_compat.api_surface returns 400 rather than
    persisting a value that would make get_provider() raise on every later
    ChatSession init for the alias."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "bad",
            "model": "x",
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "sk-x",
            "capabilities": {"server_compat": {"api_surface": "BOGUS"}},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "api_surface" in resp.json()["error"]
    # And the alias is not persisted
    assert not registry.has_alias("bad")


def test_create_rejects_non_canonical_api_surface(storage: SQLiteBackend) -> None:
    """Strict validation: ' Responses ' / 'CHAT' don't round-trip through the
    admin <select>, so they're rejected even though they'd survive a
    case-insensitive membership check."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

    for bad in (" responses ", "RESPONSES", "Chat"):
        resp = client.post(
            "/v1/api/admin/model-definitions",
            json={
                "alias": "noncanon",
                "model": "x",
                "provider": "openai-compatible",
                "base_url": "http://localhost:9000/v1",
                "api_key": "sk-x",
                "capabilities": {"server_compat": {"api_surface": bad}},
            },
        )
        assert resp.status_code == 400, f"{bad!r}: {resp.text}"


def test_create_accepts_valid_api_surface(storage: SQLiteBackend) -> None:
    """Canonical 'chat' / 'responses' / unset are all accepted and persisted."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "responses-alias",
            "model": "x",
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "sk-x",
            "capabilities": {"server_compat": {"api_surface": "responses"}},
        },
    )
    assert resp.status_code == 200, resp.text
    assert registry.has_alias("responses-alias")


def test_update_rejects_invalid_api_surface(storage: SQLiteBackend) -> None:
    """PUT path also gates the validation, so an admin can't smuggle a bad
    value into an existing alias."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"capabilities": {"server_compat": {"api_surface": "junk"}}},
    )
    assert resp.status_code == 400, resp.text
    assert "api_surface" in resp.json()["error"]


def test_delete_endpoint_refreshes_registry(storage: SQLiteBackend) -> None:
    """DELETE drops the alias from the in-process registry too — a
    coord session that tried to resolve the deleted alias would
    otherwise hit a stale cached client."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(storage, definition_id="m2", alias="extra", model="x")
    registry = _make_registry(alias="local", model="m", extras={"extra": "x"})
    client = _make_client(storage, registry)

    resp = client.delete("/v1/api/admin/model-definitions/m2")
    assert resp.status_code == 200, resp.text
    assert not registry.has_alias("extra")
    assert registry.has_alias("local")  # default alias unaffected


def test_reload_endpoint_refreshes_registry(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit reload button must refresh the console's own
    registry — until this PR it only fanned out to nodes."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="initial")
    registry = _make_registry(alias="local", model="initial")
    client = _make_client(storage, registry)

    # Bypass the CRUD endpoints to mimic an out-of-band DB change (e.g.
    # an operator psql session) and verify the explicit reload path
    # still pulls the change in.
    storage.update_model_definition("m1", model="reloaded-model")

    # Stub the async cluster fan-out helpers — they require a fully-wired
    # collector / proxy_client which is orthogonal to the helper under test.
    async def _noop_publish(_request: Any) -> None:
        return None

    async def _noop_notify(_request: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("turnstone.console.server._publish_config_change", _noop_publish)
    monkeypatch.setattr("turnstone.console.server._notify_nodes_model_reload", _noop_notify)

    resp = client.post("/v1/api/admin/model-definitions/reload")
    assert resp.status_code == 200, resp.text
    assert registry.get_config("local").model == "reloaded-model"
