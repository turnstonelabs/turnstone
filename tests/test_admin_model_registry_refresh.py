"""Console model-definition admin surface: registry refresh + the auth write gate.

Covers coord_registry auto-refresh on CRUD and explicit reload (in-place
mutation preserves object identity for the session factory's closure,
failures leave the existing registry intact, refused swaps surface as
``registry_warning``), first-row bootstrap and the keyless-console guard,
the dynamic-auth write gate (neutral-field set, two-tier validator,
pure-disable carve-out, ``_derive_auth_gate``), the ``admin.mcp``-gated
auth-constraints endpoint, and the schema-classification partition that
fails until a newly added column is classified.
"""

from __future__ import annotations

import json
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
from tests._oidc_test_helpers import make_oidc_config
from turnstone.console.server import (
    _derive_auth_gate,
    _maybe_bootstrap_coord_subsystem,
    _refresh_coord_registry,
    admin_create_model_definition,
    admin_delete_model_definition,
    admin_list_model_definitions,
    admin_model_auth_constraints,
    admin_model_reload,
    admin_update_model_definition,
)
from turnstone.core.model_registry import (
    DYNAMIC_MODEL_AUTH_MODES,
    ModelConfig,
    ModelRegistry,
)
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
    auth_mode: str = "static",
    obo_audience: str = "",
    capabilities: str = "{}",
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
        capabilities=capabilities,
        enabled=enabled,
        created_by="admin",
        auth_mode=auth_mode,
        obo_audience=obo_audience,
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


@pytest.fixture(autouse=True)
def _no_host_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every handler off the developer's real config.toml.

    ``load_config()`` caches the host file process-wide, so a same-named
    ``[models.<alias>]`` would shadow seeded DB rows. Patched WHERE USED:
    ``model_registry`` binds ``load_config`` at import time, so patching
    only ``turnstone.core.config`` misses ``load_model_registry``.
    """
    import turnstone.core.config as _cfg
    import turnstone.core.model_registry as _mr

    monkeypatch.setattr(_cfg, "load_config", lambda section=None: {})
    monkeypatch.setattr(_mr, "load_config", lambda section=None: {})


def _stub_console_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the MCP-manager side effect of a dynamic write (no mcp-loop thread)."""
    from turnstone.console import server as server_module

    monkeypatch.setattr(
        server_module,
        "_ensure_console_mcp_client",
        lambda _app: {"skipped": "test"},
    )


def _make_client(
    storage: SQLiteBackend,
    registry: ModelRegistry | None,
    perms: str = "admin.models",
) -> TestClient:
    """Build a TestClient wired to the five model-definition endpoints.

    ``perms`` feeds the header-driven ``_AuthMiddleware``; escalation-gate
    tests pass ``"admin.models,admin.mcp"``.
    """
    app = Starlette(
        routes=[
            Route(
                "/v1/api/admin/model-definitions",
                admin_list_model_definitions,
                methods=["GET"],
            ),
            # Static path before the {definition_id} routes, as in the real
            # route table — else it matches definition_id="auth-constraints".
            Route(
                "/v1/api/admin/model-definitions/auth-constraints",
                admin_model_auth_constraints,
                methods=["GET"],
            ),
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
    app.state.config_store.get.side_effect = lambda key, default=None: (
        "api://approved"
        if key == "model.auth_audience_allowlist"
        # Answering model.default_alias keeps the list handler off its
        # load_config() fallback (which caches the host config process-wide).
        else "local"
        if key == "model.default_alias"
        else default
    )
    # A full OIDC posture: the model-auth helpers read enabled,
    # discovery_retryable and obo_grant_profile, not just one field.
    app.state.oidc_config = make_oidc_config()
    app.state.mcp_token_store = MagicMock()
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": perms})
    return client


def _dynamic_create(client: TestClient, **overrides: Any) -> Any:
    """POST a dynamic-auth create; overrides patch the shared approved body."""
    body: dict[str, Any] = {
        "alias": "obo-alias",
        "model": "x",
        "auth_mode": "entra_obo",
        "obo_audience": "api://approved",
    }
    body.update(overrides)
    return client.post("/v1/api/admin/model-definitions", json=body)


def test_list_carries_no_auth_constraints(storage: SQLiteBackend) -> None:
    """The list answers to plain ``admin.models``, so it must not carry the
    approved-audience set — that lives on the admin.mcp-gated sub-route.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.get("/v1/api/admin/model-definitions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"models", "default_alias"}


def test_auth_constraints_requires_admin_mcp(storage: SQLiteBackend) -> None:
    """admin.models alone gets a flat 403 from the constraints route."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.get("/v1/api/admin/model-definitions/auth-constraints")

    assert resp.status_code == 403, resp.text
    assert "api://approved" not in resp.text


def test_auth_constraints_serves_allowlist_and_profile(
    storage: SQLiteBackend,
) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )

    resp = client.get("/v1/api/admin/model-definitions/auth-constraints")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auth_audience_allowlist"] == ["api://approved"]
    assert body["auth_grant_profile"] == "entra"
    # Server-derived, so the shelf's mode affordances track the registry's
    # classification by data (the client hand-list is only a fail-open fallback).
    assert body["dynamic_auth_modes"] == sorted(DYNAMIC_MODEL_AUTH_MODES)


def test_auth_constraints_empty_allowlist_is_present_not_absent(
    storage: SQLiteBackend,
) -> None:
    """An unset allow-list is an empty list, never a missing key: the shelf
    distinguishes "none registered yet" from "the fetch failed".
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    # Allow-list unset, but model.default_alias still answered: a blanket
    # `default` lambda would send the list handler into load_config().
    client.app.state.config_store.get.side_effect = lambda key, default=None: (
        "local" if key == "model.default_alias" else default
    )

    resp = client.get("/v1/api/admin/model-definitions/auth-constraints")

    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_audience_allowlist"] == []


def test_auth_constraints_profile_empty_when_oidc_unconfigured(
    storage: SQLiteBackend,
) -> None:
    """No-SSO reports an EMPTY profile: ``load_oidc_config`` defaults
    ``obo_grant_profile`` to "entra" even when nothing is configured.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(enabled=False)

    resp = client.get("/v1/api/admin/model-definitions/auth-constraints")

    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_grant_profile"] == ""


def test_auth_constraints_profile_survives_transient_discovery_outage(
    storage: SQLiteBackend,
) -> None:
    """``discovery_retryable`` reports the CONFIGURED profile, not no-SSO: a
    console that booted during an IdP blip is still fully configured.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(
        enabled=False, discovery_retryable=True, token_endpoint=""
    )

    resp = client.get("/v1/api/admin/model-definitions/auth-constraints")

    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_grant_profile"] == "entra"


def test_no_oidc_deployment_still_serves_and_writes_static_models(
    storage: SQLiteBackend,
) -> None:
    """A deployment with no OIDC is unaffected: ``oidc_config`` is absent
    rather than disabled, so no model-auth path may raise on the missing
    attribute.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))
    delattr(client.app.state, "oidc_config")
    delattr(client.app.state, "mcp_token_store")

    listing = client.get("/v1/api/admin/model-definitions")
    assert listing.status_code == 200, listing.text

    # No dynamic fields means no escalation: admin.models alone still creates.
    created = client.post(
        "/v1/api/admin/model-definitions",
        json={"alias": "plain", "model": "gpt-4o", "provider": "openai"},
    )
    assert created.status_code == 200, created.text

    # No fallback: a create that stops returning definition_id must fail here
    # rather than silently retarget the seeded row.
    definition_id = created.json()["definition_id"]
    updated = client.put(
        f"/v1/api/admin/model-definitions/{definition_id}",
        json={"temperature": 0.5},
    )
    assert updated.status_code == 200, updated.text


def test_dynamic_write_checks_permission_before_config(storage: SQLiteBackend) -> None:
    """The scope gate runs before validation, so a 400 never leaks the
    deployment's OIDC posture or allow-list to an unscoped prober.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))
    # admin.models only — no admin.mcp.
    resp = _dynamic_create(client, alias="probe", obo_audience="api://definitely-not-approved")

    assert resp.status_code == 403, resp.text
    assert "allowlist" not in resp.text
    assert "oidc" not in resp.text.lower()


def test_entra_obo_allowed_without_user_credential_capture(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_user_credential`` must NOT gate the write: the mint never
    reads it, redeeming whatever credential is already stored.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(capture_user_credential=False)
    _stub_console_mcp(monkeypatch)

    resp = _dynamic_create(client)
    assert resp.status_code == 200, resp.text


def test_entra_app_allowed_before_oidc_discovery(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``token_endpoint`` must NOT gate the write: discovery is a
    PER-PROCESS result, and the nodes that mint may already have it.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(token_endpoint="")
    _stub_console_mcp(monkeypatch)

    resp = _dynamic_create(client, alias="app-alias", auth_mode="entra_app")
    assert resp.status_code == 200, resp.text


def test_dynamic_write_rejected_without_token_encryption_key(
    storage: SQLiteBackend,
) -> None:
    """No token store means no mint and no cache row — refuse at the write.

    Unlike discovery, the Fernet keyring is deployment-wide, so its absence
    is a sound signal rather than one process's opinion.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.mcp_token_store = None

    resp = _dynamic_create(client)

    # 503, matching the MCP sibling: a missing key is a deployment fault, not
    # a bad request, and the refusal names the knob the boot guard names.
    assert resp.status_code == 503, resp.text
    assert "mcp_token_encryption" in resp.json()["error"]


def test_base_url_edit_allowed_despite_typod_profile(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile checks are POSTURE, not row validity: a row saved before the
    deployment's profile broke stays editable for non-auth fields.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(obo_grant_profile="entrra")
    _stub_console_mcp(monkeypatch)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "auth_mode": "entra_obo",
            "obo_audience": "api://approved",
            "base_url": "https://replacement.example/v1",
        },
    )

    assert resp.status_code == 200, resp.text


def test_base_url_edit_allowed_on_entra_app_after_profile_flip(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same tier ruling for the entra_app/profile pairing check."""
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_app",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(obo_grant_profile="rfc8693")
    _stub_console_mcp(monkeypatch)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "auth_mode": "entra_app",
            "obo_audience": "api://approved",
            "base_url": "https://replacement.example/v1",
        },
    )

    assert resp.status_code == 200, resp.text


def test_model_crud_does_not_revive_keyless_coordinator(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime bootstrap shares the lifespan twin's key guard: a plain
    model write must not stand a keyless coordinator up.
    """
    from turnstone.console import server as server_module

    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gateway",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *_a, **_k: bootstrapped.append(True),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            coord_mgr=None,
            config_store=MagicMock(),
            collector=MagicMock(),
            console_metrics=MagicMock(),
            mcp_token_store=None,
            coord_registry_error="dynamic model auth ... key missing (from boot)",
        )
    )

    server_module._maybe_bootstrap_coord_subsystem(app, storage)

    assert not bootstrapped
    assert "mcp_token_encryption" in app.state.coord_registry_error


def test_dynamic_write_rejected_when_oidc_unconfigured(
    storage: SQLiteBackend,
) -> None:
    """Flipping into dynamic auth on a no-SSO deployment refuses plainly."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(
        enabled=False, capture_user_credential=False, token_endpoint=""
    )

    resp = _dynamic_create(client)

    assert resp.status_code == 400, resp.text
    assert "single sign-on is not set up" in resp.json()["error"]


def test_dynamic_write_accepted_during_transient_discovery_outage(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``discovery_retryable`` counts as configured at write time: a transient
    IdP outage must not block config work (MCP-parity ruling).
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(
        enabled=False, discovery_retryable=True, token_endpoint=""
    )
    _stub_console_mcp(monkeypatch)

    resp = _dynamic_create(client)

    assert resp.status_code == 200, resp.text


def test_base_url_edit_skips_posture_on_unchanged_pair(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Posture is flip-gated: with the pair unchanged and the audience still
    allow-listed, a URL fix passes the row tier and skips the posture tier
    even though the key was removed after the row was saved.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.mcp_token_store = None
    _stub_console_mcp(monkeypatch)

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "auth_mode": "entra_obo",
            "obo_audience": "api://approved",
            "base_url": "https://replacement-gateway.example/v1",
        },
    )

    assert resp.status_code == 200, resp.text


def test_delisted_audience_blocks_base_url_edit(storage: SQLiteBackend) -> None:
    """Row validity always runs: the allow-list is the one check that must
    survive every auth-touching write, so a revoked audience cannot be
    re-pointed at a new host.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://revoked",
    )
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    # The fixture allow-lists only api://approved; api://revoked has left it.

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "auth_mode": "entra_obo",
            "obo_audience": "api://revoked",
            "base_url": "https://attacker.example/v1",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "allowlist" in resp.json()["error"]
    assert storage.get_model_definition("m1")["base_url"] != "https://attacker.example/v1"


def test_console_bootstrap_refuses_dynamic_auth_without_key(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The console-side twin of the node boot guard: the coordinator bootstrap
    re-checks the key against the REGISTRY (config.toml overrides DB) and
    reports through ``coord_registry_error`` rather than failing the boot.
    """
    from turnstone.console import server as server_module

    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gateway",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *_a, **_k: bootstrapped.append(True),
    )
    app = SimpleNamespace(state=SimpleNamespace(mcp_token_store=None, coord_registry_error=""))

    with caplog.at_level("ERROR", logger="turnstone.console.server"):
        server_module._load_and_bootstrap_coord_subsystem(app, storage, MagicMock())

    assert not bootstrapped
    assert "mcp_token_encryption" in app.state.coord_registry_error
    assert any("model_auth_key_missing" in r.message for r in caplog.records)


def test_console_bootstrap_proceeds_with_key_present(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a token store wired, the same dynamic registry bootstraps normally."""
    from turnstone.console import server as server_module

    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gateway",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        server_module,
        "_bootstrap_coord_subsystem",
        lambda *_a, **_k: bootstrapped.append(True),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(mcp_token_store=MagicMock(), coord_registry_error="")
    )

    server_module._load_and_bootstrap_coord_subsystem(app, storage, MagicMock())

    assert bootstrapped
    assert app.state.coord_registry_error == ""


def test_unknown_grant_profile_echoed_in_rejection(
    storage: SQLiteBackend,
) -> None:
    """A typo'd profile is rejected with the configured value quoted back, so
    the operator need not hunt startup logs for what was configured.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(obo_grant_profile="entrra")

    resp = _dynamic_create(client)

    assert resp.status_code == 400, resp.text
    assert "'entrra'" in resp.json()["error"]


def test_create_rejects_unknown_auth_mode(storage: SQLiteBackend) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={"alias": "bad-auth", "model": "x", "auth_mode": "bogus"},
    )

    assert resp.status_code == 400, resp.text
    assert "auth_mode" in resp.json()["error"]


def test_create_rejects_entra_obo_without_audience(storage: SQLiteBackend) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={"alias": "missing-aud", "model": "x", "auth_mode": "entra_obo"},
    )

    assert resp.status_code == 400, resp.text
    assert "obo_audience" in resp.json()["error"]


def test_update_rejects_entra_obo_when_stored_audience_empty(
    storage: SQLiteBackend,
) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"auth_mode": "entra_obo"},
    )

    assert resp.status_code == 400, resp.text
    assert "obo_audience" in resp.json()["error"]


def test_update_rejects_clearing_audience_on_entra_obo(
    storage: SQLiteBackend,
) -> None:
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"obo_audience": ""},
    )

    assert resp.status_code == 400, resp.text
    assert "obo_audience" in resp.json()["error"]


def test_dynamic_auth_create_requires_admin_mcp(storage: SQLiteBackend) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = _dynamic_create(client, alias="gateway")

    assert resp.status_code == 403, resp.text
    assert "admin.mcp" in resp.json()["error"]


def test_dynamic_auth_create_rejects_unapproved_audience(
    storage: SQLiteBackend,
) -> None:
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )

    resp = _dynamic_create(client, alias="gateway", obo_audience="api://not-approved")

    assert resp.status_code == 400, resp.text
    assert "allowlist" in resp.json()["error"]


def test_dynamic_alias_base_url_change_requires_admin_mcp(
    storage: SQLiteBackend,
) -> None:
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        base_url="https://approved.example/v1",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"base_url": "https://attacker.example/v1"},
    )

    assert resp.status_code == 403, resp.text
    assert "admin.mcp" in resp.json()["error"]
    assert storage.get_model_definition("m1")["base_url"] == "https://approved.example/v1"


def test_entra_app_create_rejects_non_entra_profile(
    storage: SQLiteBackend,
) -> None:
    """entra_app has no RFC 8693 leg, so a non-entra profile must refuse it.

    The helper's ``enabled=True`` is load-bearing: OIDC-less would refuse
    first and leave the profile branch untested.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(obo_grant_profile="rfc8693")

    resp = _dynamic_create(client, alias="gateway", auth_mode="entra_app")

    assert resp.status_code == 400, resp.text
    assert "RFC 8693" in resp.json()["error"]


def test_entra_obo_allowed_on_rfc8693_profile(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delegated leg is a refresh-token grant and works under either
    profile — the pair above proves the check discriminates per mode.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = make_oidc_config(obo_grant_profile="rfc8693")
    _stub_console_mcp(monkeypatch)

    resp = _dynamic_create(client, alias="gateway")
    assert resp.status_code == 200, resp.text


def test_unchanged_dynamic_auth_fields_do_not_require_admin_mcp(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin form always submits both fields; equality, not presence,
    decides whether the capability-escalation permission is needed."""
    from turnstone.console import server as server_module

    _seed_model_def(
        storage,
        definition_id="m1",
        alias="local",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="local", model="m"))
    monkeypatch.setattr(
        server_module,
        "_ensure_console_mcp_client",
        lambda _app: {"skipped": "test"},
    )

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "auth_mode": "entra_obo",
            "obo_audience": "api://approved",
            "temperature": 0.4,
        },
    )

    assert resp.status_code == 200, resp.text


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


# ---------------------------------------------------------------------------
# Default-deny gate pins + schema coverage (neutral-set, chokepoint, schema)
# ---------------------------------------------------------------------------


def test_enabled_flip_on_dynamic_row_requires_admin_mcp(
    storage: SQLiteBackend,
) -> None:
    """Re-arming a dynamic row is an auth change: any non-neutral value change
    on a row that is or becomes dynamic escalates, the pure disable being the
    one directional exception.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        enabled=False,
        auth_mode="entra_obo",
        obo_audience="api://revoked",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))
    # Default headers grant admin.models only: the flip must be refused flat.
    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": True})

    assert resp.status_code == 403, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_enabled_flip_with_admin_mcp_still_blocked_by_delisted_audience(
    storage: SQLiteBackend,
) -> None:
    """Row validity runs on the enable flip: re-enabling a row whose audience
    left the allow-list refuses even with the scope, and the row tier runs
    before posture so the de-listed audience is the named refusal.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        enabled=False,
        auth_mode="entra_obo",
        obo_audience="api://revoked",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": True})

    assert resp.status_code == 400, resp.text
    assert "allowlist" in resp.json()["error"]
    assert not storage.get_model_definition("m1")["enabled"]


def test_enabled_flip_with_admin_mcp_and_listed_audience_succeeds(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate escalates, it does not lock out: with a healthy posture the
    scoped operator's re-arm passes both tiers and lands.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        enabled=False,
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )
    _stub_console_mcp(monkeypatch)

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": True})

    assert resp.status_code == 200, resp.text
    assert storage.get_model_definition("m1")["enabled"]


def test_keyless_reenable_of_dynamic_row_returns_503(
    storage: SQLiteBackend,
) -> None:
    """Arming is a posture event: enabled false→true resumes minting, so a
    keyless re-enable must 503 like the create twin even though the (mode,
    audience) pair is unchanged.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        enabled=False,
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.mcp_token_store = None

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": True})

    assert resp.status_code == 503, resp.text
    # Same remediation the create twin's refusal and the boot guard carry.
    assert "mcp_token_encryption" in resp.json()["error"]
    assert not storage.get_model_definition("m1")["enabled"]


def test_keyless_pure_disable_still_succeeds(storage: SQLiteBackend) -> None:
    """Posture-on-arming must not leak into de-escalation: the posture tier
    guards what a write ARMS, and a disable arms nothing.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))
    client.app.state.mcp_token_store = None

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": False})

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_pure_disable_needs_only_admin_models_even_when_audience_delisted(
    storage: SQLiteBackend,
) -> None:
    """The disarm lever must never be held hostage: de-listing an audience
    does not stop minting, so pure disable (the only non-neutral change is
    enabled true→false) stays available under admin.models and audits as a
    disarm rather than a gated write.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://revoked",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": False})

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]
    audit_details = [
        json.loads(row["detail"])
        for row in storage.list_audit_events(limit=10)
        if row["action"] == "model_definition.update"
    ]
    assert audit_details, "the disarm must write an audit row"
    assert audit_details[0].get("auth_disarmed") is True
    assert "auth_gated" not in audit_details[0]
    # Placement is contract: the audit tab renders only a detail's first
    # three keys, so the marker must lead the dict to be visible at all.
    assert next(iter(audit_details[0])) == "auth_disarmed"


def test_pure_disable_with_admin_mcp_skips_validator_on_delisted_audience(
    storage: SQLiteBackend,
) -> None:
    """The carve-out skips the validator for every caller: with admin.mcp the
    row tier would still 400 on the de-listed audience if it ran.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://revoked",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": False})

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_disable_bundled_with_gated_change_still_requires_admin_mcp(
    storage: SQLiteBackend,
) -> None:
    """The carve-out is exact: a disarm that also re-points base_url is not
    de-escalation (the row can be re-enabled against the new host), so
    bundling forfeits it and the whole write meets the admin.mcp gate.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"enabled": False, "base_url": "https://other.example/v1"},
    )

    assert resp.status_code == 403, resp.text
    row = storage.get_model_definition("m1")
    assert row["enabled"]
    assert row["base_url"] == "http://localhost:8000/v1"


def test_empty_audience_dynamic_row_disarms_under_admin_models(
    storage: SQLiteBackend,
) -> None:
    """De-escalation precedes the audience-required refusal: a row stored
    dynamic with an EMPTY audience (a DB-direct write) fails the post-merge
    audience check on every submission, so unless the disarm takes precedence
    the row could never be disabled. The lone-field flip lands under
    admin.models alone.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": False})

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_empty_audience_dynamic_row_full_form_disarm_lands(
    storage: SQLiteBackend,
) -> None:
    """The admin UI submits the whole form: unchanged fields riding along
    with the flip must not forfeit the disarm on the empty-audience row.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "alias": "gw",
            "model": "m",
            "provider": "openai-compatible",
            "base_url": "http://localhost:8000/v1",
            "api_key": "***",
            "context_window": 8192,
            "capabilities": {},
            "enabled": False,
            "auth_mode": "entra_obo",
            "obo_audience": "",
        },
    )

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_empty_audience_dynamic_row_non_disarm_write_still_refused(
    storage: SQLiteBackend,
) -> None:
    """The exemption is exactly the disarm: any other write on the
    empty-audience dynamic row still meets the audience-required 400, even
    for a fully scoped caller.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"enabled": False, "base_url": "https://other.example/v1"},
    )

    assert resp.status_code == 400, resp.text
    assert "obo_audience is required" in resp.json()["error"]
    row = storage.get_model_definition("m1")
    assert row["enabled"]
    assert row["base_url"] == "http://localhost:8000/v1"


def test_update_twin_refuses_non_finite_context_window(
    storage: SQLiteBackend,
) -> None:
    """json admits NaN/Infinity literals, which pass the numeric isinstance
    check; int() on them raises, so the twins refuse them as invalid values
    rather than crashing the handler.
    """
    _seed_model_def(storage, definition_id="m1", alias="gw", model="m")
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    # Raw content: the test client's own serializer rejects NaN, but the
    # wire accepts the literal (stdlib json.loads on the server side).
    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        content=b'{"context_window": NaN}',
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400, resp.text
    assert "context_window" in resp.json()["error"]
    assert storage.get_model_definition("m1")["context_window"] == 8192


def test_corrupt_stored_capabilities_does_not_block_pure_disable(
    storage: SQLiteBackend,
) -> None:
    """The disarm carve-out survives a stored blob the parser cannot read.

    The capabilities compare fail-closes on an unparseable STORED blob for
    every other gating purpose; for the pure-disable derivation only, it
    must not hold the disarm hostage to bytes nobody can compare.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://revoked",
        capabilities="{not json",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    # The shelf's full-form save: unchanged fields re-submitted, the
    # corrupt blob re-serialized as {}, plus the flip.
    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"alias": "gw", "model": "m", "capabilities": {}, "enabled": False},
    )

    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m1")["enabled"]


def test_corrupt_stored_capabilities_still_gates_non_disarm_writes(
    storage: SQLiteBackend,
) -> None:
    """The corrupt-blob exception is disarm-only: on the same row, a save that
    does not disarm still counts the capabilities column as a gated change.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
        capabilities="{not json",
    )
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"capabilities": {}},
    )

    assert resp.status_code == 403, resp.text
    assert storage.get_model_definition("m1")["capabilities"] == "{not json"


def test_auth_markers_render_in_audit_detail_first_keys(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both audit markers lead their detail dict: the audit tab renders only
    the first three keys, and a full-form save buries an appended marker.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(
        storage, _make_registry(alias="gw", model="m"), perms="admin.models,admin.mcp"
    )
    _stub_console_mcp(monkeypatch)

    # A gated full-form save: neutral fields re-submitted ahead of the gated
    # change, the shape that buries an appended marker.
    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={
            "alias": "gw",
            "model": "m",
            "context_window": 8192,
            "temperature": None,
            "max_tokens": None,
            "base_url": "https://moved.example/v1",
        },
    )
    assert resp.status_code == 200, resp.text

    # The disarm arm, same placement rule.
    resp = client.put("/v1/api/admin/model-definitions/m1", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    details = [
        json.loads(row["detail"])
        for row in storage.list_audit_events(limit=10)
        if row["action"] == "model_definition.update"
    ]
    # Select by marker, not list position: both writes land in the same
    # second and the timestamp tie-break is not part of this contract.
    gated = [d for d in details if "auth_gated" in d]
    disarmed = [d for d in details if "auth_disarmed" in d]
    assert gated and disarmed, details
    assert next(iter(gated[0])) == "auth_gated"
    assert gated[0]["auth_gated"] is True
    assert next(iter(disarmed[0])) == "auth_disarmed"
    assert "auth_gated" not in disarmed[0]


def test_reserialized_capabilities_do_not_defeat_pure_disable(
    storage: SQLiteBackend,
) -> None:
    """Serialization noise never gates: the capabilities diff compares
    CANONICAL forms, so the shelf's reordered-but-equal blob rides the
    carve-out (test_capabilities_value_change_still_gates_dynamic_row holds
    the fail-closed half).
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    storage.update_model_definition("m1", capabilities='{"b": 1, "a": 2}')
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"enabled": False, "capabilities": {"a": 2, "b": 1}},
    )

    assert resp.status_code == 200, resp.text
    row = storage.get_model_definition("m1")
    assert not row["enabled"]
    audit_details = [
        json.loads(audit_row["detail"])
        for audit_row in storage.list_audit_events(limit=10)
        if audit_row["action"] == "model_definition.update"
    ]
    assert audit_details and audit_details[0].get("auth_disarmed") is True


def test_capabilities_value_change_still_gates_dynamic_row(
    storage: SQLiteBackend,
) -> None:
    """The canonical compare fails closed: the blob selects the provider
    factory and merges extra_body into requests, so a VALUE change gates
    both bundled with a disable and alone.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    storage.update_model_definition("m1", capabilities='{"b": 1, "a": 2}')
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    bundled = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"enabled": False, "capabilities": {"a": 3, "b": 1}},
    )
    assert bundled.status_code == 403, bundled.text
    row = storage.get_model_definition("m1")
    assert row["enabled"]
    assert row["capabilities"] == '{"b": 1, "a": 2}'

    alone = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"capabilities": {"a": 3, "b": 1}},
    )
    assert alone.status_code == 403, alone.text


def test_reserialized_capabilities_alone_do_not_require_admin_mcp(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value-identical full-form save is not an auth change at all."""
    _stub_console_mcp(monkeypatch)
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    storage.update_model_definition("m1", capabilities='{"b": 1, "a": 2}')
    client = _make_client(storage, _make_registry(alias="gw", model="m"))

    resp = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"capabilities": {"a": 2, "b": 1}, "temperature": 0.4},
    )

    assert resp.status_code == 200, resp.text
    row = storage.get_model_definition("m1")
    assert row["temperature"] == 0.4
    assert json.loads(row["capabilities"]) == {"a": 2, "b": 1}


def test_capabilities_canonical_compare_distinguishes_bool_from_int() -> None:
    """The helper's contract is a sort_keys re-dump, not parsed ``==``: Python
    collapses ``True == 1`` where JSON does not, reordering compares equal,
    and an unparseable stored blob compares as changed.
    """
    from turnstone.console.server import _capabilities_value_changed

    assert not _capabilities_value_changed('{"b": 1, "a": 2}', '{"a": 2, "b": 1}')
    assert _capabilities_value_changed('{"a": 1}', '{"a": true}')
    assert _capabilities_value_changed("not json", '{"a": 1}')


def test_integral_float_capabilities_spelling_does_not_gate() -> None:
    """Integral floats normalize to ints before the canonical dump.

    JSON.stringify collapses whole-number floats, so a Python-written
    ``1.0`` comes back as ``1``. Normalization is recursive, real value
    differences still gate, and bool is checked BEFORE the float branch
    (bool subclasses int).
    """
    from turnstone.console.server import _capabilities_value_changed

    # Spelling only: stored by Python, resubmitted after a JS round-trip.
    assert not _capabilities_value_changed(
        '{"extra_body": {"top_k": 1.0}}', '{"extra_body": {"top_k": 1}}'
    )
    # Recursive: whole-number floats inside lists normalize too.
    assert not _capabilities_value_changed('{"a": [1.0, 2.5]}', '{"a": [1, 2.5]}')
    # Real numeric changes still gate.
    assert _capabilities_value_changed('{"a": 1.5}', '{"a": 1}')
    # bool survives the normalization in both directions.
    assert _capabilities_value_changed('{"a": true}', '{"a": 1}')
    assert _capabilities_value_changed('{"a": 1.0}', '{"a": true}')


def test_static_create_rejects_staged_audience(storage: SQLiteBackend) -> None:
    """The create twin: a static row cannot STORE an audience, which would
    park a never-validated value for a later flip to inherit.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.post(
        "/v1/api/admin/model-definitions",
        json={
            "alias": "sneaky",
            "model": "x",
            "auth_mode": "static",
            "obo_audience": "api://never-allowlisted",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "dynamic auth_mode" in resp.json()["error"]
    assert storage.get_model_definition_by_alias("sneaky") is None


def test_static_update_rejects_new_audience_but_allows_stale_resend(
    storage: SQLiteBackend,
) -> None:
    """The update twin: changed-to-non-empty is the exact staging rule — a NEW
    audience on a static row is refused for every scope, while re-submitting
    stored residue or clearing it still saves.
    """
    _seed_model_def(
        storage,
        definition_id="m1",
        alias="legacy",
        model="m",
        auth_mode="static",
        obo_audience="api://stale-residue",
    )
    client = _make_client(
        storage, _make_registry(alias="legacy", model="m"), perms="admin.models,admin.mcp"
    )

    changed = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"obo_audience": "api://brand-new"},
    )
    assert changed.status_code == 400, changed.text
    assert "dynamic auth_mode" in changed.json()["error"]
    assert storage.get_model_definition("m1")["obo_audience"] == "api://stale-residue"

    resend = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"obo_audience": "api://stale-residue", "model": "m2"},
    )
    assert resend.status_code == 200, resend.text

    cleared = client.put(
        "/v1/api/admin/model-definitions/m1",
        json={"obo_audience": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert storage.get_model_definition("m1")["obo_audience"] == ""


def test_capabilities_non_object_rejected_not_wiped(storage: SQLiteBackend) -> None:
    """Null or string capabilities must refuse, not erase: GET serves
    capabilities as a serialized STRING, so a read-modify-write that PUTs it
    back must not coerce to "{}" and drop server_compat or calibration.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    storage.update_model_definition("m1", capabilities='{"server_compat": {"api_surface": "chat"}}')
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    for bad in ('{"server_compat": {"api_surface": "chat"}}', None):
        resp = client.put(
            "/v1/api/admin/model-definitions/m1",
            json={"capabilities": bad},
        )
        assert resp.status_code == 400, f"{bad!r}: {resp.text}"
        assert "omit to leave unchanged" in resp.json()["error"]
        stored = storage.get_model_definition("m1")["capabilities"]
        assert stored == '{"server_compat": {"api_surface": "chat"}}', stored


def test_capabilities_absent_leaves_stored_unchanged(storage: SQLiteBackend) -> None:
    """The companion contract: omitting the key touches nothing."""
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    storage.update_model_definition("m1", capabilities='{"server_compat": {"api_surface": "chat"}}')
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"model": "m2"})

    assert resp.status_code == 200, resp.text
    row = storage.get_model_definition("m1")
    assert row["model"] == "m2"
    assert row["capabilities"] == '{"server_compat": {"api_surface": "chat"}}'


def test_create_and_update_twins_agree_on_non_dict_capabilities(
    storage: SQLiteBackend,
) -> None:
    """The create twin mirrors the update twin's capabilities refusal.

    Parity's other half: an ABSENT key still defaults to {} on create
    (there is nothing to leave unchanged), unlike update.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    for bad in ('{"server_compat": {"api_surface": "chat"}}', None, [1]):
        resp = client.post(
            "/v1/api/admin/model-definitions",
            json={"alias": "clone", "model": "x", "capabilities": bad},
        )
        assert resp.status_code == 400, f"{bad!r}: {resp.text}"
        assert "omit for defaults" in resp.json()["error"]
        assert storage.get_model_definition_by_alias("clone") is None

    ok = client.post(
        "/v1/api/admin/model-definitions",
        json={"alias": "clone", "model": "x"},
    )
    assert ok.status_code == 200, ok.text
    assert storage.get_model_definition_by_alias("clone")["capabilities"] == "{}"


def test_blank_auth_mode_row_accepts_neutral_edit_under_admin_models(
    storage: SQLiteBackend,
) -> None:
    """Blank auth_mode residue must not fake a pair change: the eff_*
    existing-side fallbacks normalize "" to static like the old_* pair, so
    an update omitting auth_mode is not read as a flip.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    with storage._conn() as conn:
        conn.exec_driver_sql(
            "UPDATE model_definitions SET auth_mode = '' WHERE definition_id = 'm1'"
        )
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"temperature": 0.5})

    assert resp.status_code == 200, resp.text
    assert storage.get_model_definition("m1")["temperature"] == 0.5


def test_trailing_space_stored_audience_accepts_neutral_edit_and_disarm(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored audience residue must not fake a pair change either: the stored
    side passes through the same ``_clean_oauth_text`` normalization before
    the compare, so the shelf's canonical re-submission is not a change.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    with storage._conn() as conn:
        conn.exec_driver_sql(
            "UPDATE model_definitions SET obo_audience = 'api://approved ' "
            "WHERE definition_id = 'm2'"
        )
    _stub_console_mcp(monkeypatch)
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    # The shelf's full-form save: pair re-submitted in canonical form beside
    # the one real (neutral) change.
    resp = client.put(
        "/v1/api/admin/model-definitions/m2",
        json={"temperature": 0.5, "auth_mode": "entra_obo", "obo_audience": "api://approved"},
    )
    assert resp.status_code == 200, resp.text

    # And the disarm lever stays reachable through the same full form.
    resp = client.put(
        "/v1/api/admin/model-definitions/m2",
        json={"enabled": False, "auth_mode": "entra_obo", "obo_audience": "api://approved"},
    )
    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("m2")["enabled"]


def test_write_response_carries_registry_warning_on_keyless_refusal(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 that the live registry refused to adopt must say so.

    Keyless console, dynamic row from a peer console's DB: the same-pair
    base_url edit passes the write gates and stores, but this console's
    ``_refresh_coord_registry`` refuses the swap and the running
    coordinator keeps streaming to the OLD base_url.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    _stub_console_mcp(monkeypatch)
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.mcp_token_store = None

    resp = client.put(
        "/v1/api/admin/model-definitions/m2",
        json={"base_url": "http://moved.example/v1"},
    )

    assert resp.status_code == 200, resp.text
    assert "mcp_token_encryption" in resp.json().get("registry_warning", "")
    # The DB write itself landed — the warning qualifies, never negates.
    assert storage.get_model_definition("m2")["base_url"] == "http://moved.example/v1"


def test_write_response_has_no_registry_warning_on_clean_swap(
    storage: SQLiteBackend,
) -> None:
    """The negative control: an adopted swap carries no warning key at all,
    so clients can treat presence as the signal.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(storage, _make_registry(alias="local", model="m"))

    resp = client.put("/v1/api/admin/model-definitions/m1", json={"temperature": 0.7})

    assert resp.status_code == 200, resp.text
    assert "registry_warning" not in resp.json()


def test_delete_response_carries_registry_warning_on_keyless_refusal(
    storage: SQLiteBackend,
) -> None:
    """A delete the live registry refused to adopt must say so.

    Keyless console whose DB holds a peer-written dynamic row: deleting a
    DIFFERENT, static row leaves the dynamic row in the reload set, so the
    swap is refused and the live registry keeps SERVING the deleted alias.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    _seed_model_def(storage, definition_id="m3", alias="victim", model="v")
    registry = _make_registry(alias="local", model="m", extras={"victim": "v"})
    client = _make_client(storage, registry)
    client.app.state.mcp_token_store = None

    resp = client.delete("/v1/api/admin/model-definitions/m3")

    assert resp.status_code == 200, resp.text
    assert "mcp_token_encryption" in resp.json().get("registry_warning", "")
    # The DB row is gone — the warning qualifies, never negates...
    assert storage.get_model_definition("m3") is None
    # ...while the refused swap left the live registry serving the alias.
    assert registry.has_alias("victim")


def test_reload_response_carries_registry_warning_on_keyless_refusal(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reload button's purpose is DB→live sync, so a refused console
    swap is the one outcome it must not report as unqualified success.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )

    async def _noop_publish(_request: Any) -> None:
        return None

    async def _noop_notify(_request: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("turnstone.console.server._publish_config_change", _noop_publish)
    monkeypatch.setattr("turnstone.console.server._notify_nodes_model_reload", _noop_notify)
    registry = _make_registry(alias="local", model="m")
    client = _make_client(storage, registry)
    client.app.state.mcp_token_store = None

    resp = client.post("/v1/api/admin/model-definitions/reload")

    assert resp.status_code == 200, resp.text
    assert "mcp_token_encryption" in resp.json().get("registry_warning", "")
    assert not registry.has_alias("gw")  # the refused swap installed nothing


class TestDeriveAuthGate:
    """Direct unit pins on the pure gate derivation: the exclusivity
    invariants stated on ``ModelAuthGateDecision``, asserted against the
    function itself rather than through an endpoint.
    """

    @staticmethod
    def _row(**overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "definition_id": "m1",
            "alias": "gw",
            "model": "m",
            "provider": "openai-compatible",
            "base_url": "http://a.example/v1",
            "api_key": "sk",
            "context_window": 8192,
            "capabilities": "{}",
            "enabled": 1,
            "auth_mode": "entra_obo",
            "obo_audience": "api://approved",
        }
        row.update(overrides)
        return row

    def test_pure_disable_excludes_every_gated_outcome(self) -> None:
        gate = _derive_auth_gate(self._row(), {"enabled": False})
        assert gate.pure_disable
        assert gate.dynamic_involved
        assert not gate.pair_changed
        assert not gate.auth_config_changed
        assert not gate.posture_event

    def test_pure_disable_exempts_the_audience_required_violation(self) -> None:
        """``pure_disable`` implies ``not audience_required_violation``: a row
        stored dynamic with an EMPTY audience must stay disarmable — lone-field
        and full-form — while every non-disarm write keeps the violation."""
        lone = _derive_auth_gate(self._row(obo_audience=""), {"enabled": False})
        assert lone.pure_disable
        assert not lone.audience_required_violation
        assert not lone.auth_config_changed

        full = _derive_auth_gate(
            self._row(obo_audience=""),
            {"auth_mode": "entra_obo", "obo_audience": "", "enabled": False},
        )
        assert full.pure_disable
        assert not full.audience_required_violation

        bundled = _derive_auth_gate(
            self._row(obo_audience=""),
            {"enabled": False, "base_url": "http://b.example/v1"},
        )
        assert not bundled.pure_disable
        assert bundled.audience_required_violation

    def test_disable_bundled_with_gated_change_still_gates(self) -> None:
        gate = _derive_auth_gate(self._row(), {"enabled": False, "base_url": "http://b.example/v1"})
        assert not gate.pure_disable
        assert gate.auth_config_changed
        # No posture event: the pair is untouched and the row is disarming.
        assert not gate.posture_event

    def test_arming_is_a_posture_event_and_never_pure_disable(self) -> None:
        gate = _derive_auth_gate(self._row(enabled=0), {"enabled": True})
        assert gate.enabled_armed
        assert gate.posture_event
        assert not gate.pure_disable
        assert gate.auth_config_changed

    def test_pair_change_forecloses_pure_disable(self) -> None:
        gate = _derive_auth_gate(self._row(), {"enabled": False, "obo_audience": "api://other"})
        assert gate.pair_changed
        assert not gate.pure_disable
        assert gate.auth_config_changed
        assert gate.posture_event

    def test_static_row_neutral_edit_engages_nothing(self) -> None:
        gate = _derive_auth_gate(
            self._row(auth_mode="static", obo_audience=""), {"temperature": 0.5}
        )
        assert not gate.pair_changed
        assert not gate.dynamic_involved
        assert not gate.auth_config_changed
        assert not gate.pure_disable
        assert not gate.posture_event

    def test_posture_event_is_exactly_pair_or_arming(self) -> None:
        pair_only = _derive_auth_gate(self._row(), {"obo_audience": "api://other"})
        assert pair_only.pair_changed and not pair_only.enabled_armed
        assert pair_only.posture_event

        arm_only = _derive_auth_gate(self._row(enabled=0), {"enabled": True})
        assert arm_only.enabled_armed and not arm_only.pair_changed
        assert arm_only.posture_event

        neither = _derive_auth_gate(self._row(), {"temperature": 0.1})
        assert not neither.posture_event

    def test_stored_audience_residue_normalized_before_compare(self) -> None:
        gate = _derive_auth_gate(
            self._row(obo_audience="api://approved "),
            {"auth_mode": "entra_obo", "obo_audience": "api://approved", "enabled": False},
        )
        assert not gate.pair_changed
        assert gate.pure_disable

    def test_audience_required_violation_on_flip_without_audience(self) -> None:
        gate = _derive_auth_gate(
            self._row(auth_mode="static", obo_audience=""), {"auth_mode": "entra_obo"}
        )
        assert gate.audience_required_violation

    def test_static_new_audience_violation_is_value_change_only(self) -> None:
        staged = _derive_auth_gate(
            self._row(auth_mode="static", obo_audience=""), {"obo_audience": "api://new"}
        )
        assert staged.static_new_audience_violation

        residue_resubmit = _derive_auth_gate(
            self._row(auth_mode="static", obo_audience="api://stale "),
            {"obo_audience": "api://stale"},
        )
        assert not residue_resubmit.static_new_audience_violation

        clearing = _derive_auth_gate(
            self._row(auth_mode="static", obo_audience="api://stale"),
            {"obo_audience": ""},
        )
        assert not clearing.static_new_audience_violation


def test_refresh_does_not_swap_dynamic_alias_into_keyless_console(
    storage: SQLiteBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The live-swap path shares the key refusal: ``_refresh_coord_registry``
    mutates a LIVE registry, so a keyless console must not acquire a
    peer-written dynamic alias — last-good keeps serving, banner recorded.
    """
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    registry = _make_registry(alias="local", model="m")
    app_state = SimpleNamespace(
        coord_registry=registry,
        mcp_token_store=None,
        coord_registry_error="",
    )

    with caplog.at_level("ERROR", logger="turnstone.console.server"):
        server_module._refresh_coord_registry(app_state, storage)

    assert not registry.has_alias("gw")
    assert not registry.has_dynamic_auth()
    assert "mcp_token_encryption" in app_state.coord_registry_error
    assert any("model_auth_key_missing" in r.message for r in caplog.records)


def test_refresh_swaps_dynamic_alias_with_key_present(
    storage: SQLiteBackend,
) -> None:
    """With the token store wired, the same refresh admits the dynamic row."""
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    registry = _make_registry(alias="local", model="m")
    app_state = SimpleNamespace(
        coord_registry=registry,
        mcp_token_store=MagicMock(),
        coord_registry_error="",
    )

    server_module._refresh_coord_registry(app_state, storage)

    assert registry.has_alias("gw")
    assert registry.has_dynamic_auth()
    assert app_state.coord_registry_error == ""


def test_refresh_clears_key_refusal_after_recovery(storage: SQLiteBackend) -> None:
    from turnstone.console import server as server_module

    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    _seed_model_def(
        storage,
        definition_id="m2",
        alias="gw",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    registry = _make_registry(alias="local", model="m")
    app_state = SimpleNamespace(
        coord_registry=registry,
        mcp_token_store=None,
        coord_registry_error="",
    )

    server_module._refresh_coord_registry(app_state, storage)
    assert "mcp_token_encryption" in app_state.coord_registry_error  # stamped

    storage.update_model_definition("m2", enabled=False)
    server_module._refresh_coord_registry(app_state, storage)

    assert app_state.coord_registry_error == ""
    assert not registry.has_alias("gw")


def test_no_sso_posture_refusal_names_sso_not_profile(
    storage: SQLiteBackend,
) -> None:
    """Posture-tier order is token store, then OIDC-configured, then profile,
    so a host that never wired OIDC is told SSO is missing rather than that
    its (nonexistent) profile is a typo.
    """
    _seed_model_def(storage, definition_id="m1", alias="local", model="m")
    client = _make_client(
        storage, _make_registry(alias="local", model="m"), perms="admin.models,admin.mcp"
    )
    client.app.state.oidc_config = None

    resp = _dynamic_create(client)

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "single sign-on is not set up" in error
    assert "grant profile" not in error


def test_model_definition_schema_auth_classification(storage: SQLiteBackend) -> None:
    """Every model_definitions column is classified — or the suite fails.

    A migration adding a column breaks the partition assert until it is
    placed in exactly one of the three sets; the gated set is asserted
    literally so a column drifting OUT of it is as loud as a new column.
    """
    from turnstone.console.server import MODEL_AUTH_NEUTRAL_FIELDS
    from turnstone.core.storage._utils import MODEL_DEFINITION_MUTABLE

    with storage._conn() as conn:
        live_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(model_definitions)")
        }

    server_side = {"definition_id", "created_by", "created", "updated"}
    # Disjoint, exhaustive partition of the live schema.
    assert MODEL_AUTH_NEUTRAL_FIELDS <= MODEL_DEFINITION_MUTABLE
    assert not (server_side & MODEL_DEFINITION_MUTABLE)
    assert live_columns == server_side | MODEL_DEFINITION_MUTABLE, (
        "model_definitions gained or lost a column: classify it in "
        "MODEL_AUTH_NEUTRAL_FIELDS (provably auth-neutral), leave it gated, "
        "or add it to the server-side set here — see the neutral set's "
        "comment in turnstone/console/server.py"
    )
    assert {
        "alias",
        "model",
        "provider",
        "base_url",
        "api_key",
        "capabilities",
        "enabled",
        "auth_mode",
        "obo_audience",
    } == MODEL_DEFINITION_MUTABLE - MODEL_AUTH_NEUTRAL_FIELDS


def test_every_mutable_column_probes_its_classification(
    storage: SQLiteBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each column's classification is enforced, not just declared: on a
    dynamic row an admin.models-only caller gets 200 for every neutral column
    and 403 for every gated one. ``enabled`` is the one DIRECTIONAL column —
    the arm direction is probed in the loop, the disarm carve-out below.
    """
    from turnstone.console.server import MODEL_AUTH_NEUTRAL_FIELDS
    from turnstone.core.storage._utils import MODEL_DEFINITION_MUTABLE

    probes: dict[str, Any] = {
        "alias": "gw2",
        "model": "m2",
        "provider": "openai",
        "base_url": "https://other.example/v1",
        "api_key": "sk-new",
        "context_window": 4096,
        "capabilities": {"note": "probe"},
        # Arm direction: the loop seeds THIS row disabled, so the probe is the
        # gated false→true flip rather than the carved-out disarm.
        "enabled": True,
        "temperature": 0.9,
        "max_tokens": 512,
        "reasoning_effort": "low",
        "surface_persisted_reasoning": False,
        "replay_reasoning_to_model": True,
        "auth_mode": "entra_app",
        "obo_audience": "api://other",
    }
    assert set(probes) == set(MODEL_DEFINITION_MUTABLE), (
        "a mutable column has no probe value — add one so its classification "
        "is exercised, not just declared"
    )
    _stub_console_mcp(monkeypatch)

    for column in sorted(MODEL_DEFINITION_MUTABLE):
        definition_id = f"probe-{column.replace('_', '-')}"
        _seed_model_def(
            storage,
            definition_id=definition_id,
            alias=f"gw-{column}",
            model="m",
            enabled=(column != "enabled"),
            auth_mode="entra_obo",
            obo_audience="api://approved",
        )
        client = _make_client(storage, _make_registry(alias=f"gw-{column}", model="m"))
        resp = client.put(
            f"/v1/api/admin/model-definitions/{definition_id}",
            json={column: probes[column]},
        )
        if column in MODEL_AUTH_NEUTRAL_FIELDS:
            assert resp.status_code == 200, f"{column}: {resp.text}"
        else:
            assert resp.status_code == 403, f"{column}: {resp.text}"

    # The paired disarm direction: the same lone-column PUT on an ENABLED row
    # is the pure-disable carve-out, so admin.models suffices.
    _seed_model_def(
        storage,
        definition_id="probe-enabled-disarm",
        alias="gw-enabled-disarm",
        model="m",
        auth_mode="entra_obo",
        obo_audience="api://approved",
    )
    client = _make_client(storage, _make_registry(alias="gw-enabled-disarm", model="m"))
    resp = client.put(
        "/v1/api/admin/model-definitions/probe-enabled-disarm",
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert not storage.get_model_definition("probe-enabled-disarm")["enabled"]
