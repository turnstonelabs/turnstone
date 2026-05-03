"""Tests for the per-node ``models`` metadata pipeline.

Two helpers in ``server.py`` carry the load:

- ``_collect_node_models_metadata`` projects the live ``ModelRegistry``
  into the node_metadata row shape ``[{alias, provider, healthy}, ...]``.
- ``_publish_models_metadata`` short-circuits redundant writes via a
  payload cache on ``app_state`` and is the helper called from both
  the heartbeat loop and ``internal_model_reload``.

These tests pin the projection shape, the health-flag wiring, the
cache short-circuit, and the model-reload integration.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from turnstone.core.healthcheck import HealthTrackerRegistry
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.server import (
    _collect_node_models_metadata,
    _publish_models_metadata,
)


def _registry(*aliases_with_url: tuple[str, str]) -> ModelRegistry:
    """Build a registry from ``(alias, base_url)`` pairs.

    Two aliases sharing a ``base_url`` deliberately share a tracker —
    that's the contract the cluster-level health surface needs to
    preserve, and it's worth pinning in a test.
    """
    models = {
        alias: ModelConfig(alias=alias, base_url=url, api_key="k", model=alias, provider="openai")
        for alias, url in aliases_with_url
    }
    default = aliases_with_url[0][0]
    return ModelRegistry(models, default=default)


def test_returns_none_when_registry_missing():
    state = SimpleNamespace()
    assert _collect_node_models_metadata(state) is None


def test_projects_all_aliases_with_default_healthy_when_no_tracker():
    """Without a ``health_registry`` (or before any request has flowed
    through a backend), every alias surfaces as ``healthy=True`` —
    operators shouldn't get an empty ``models`` list on a freshly
    started node just because the backends haven't been exercised."""
    reg = _registry(("a", "http://x"), ("b", "http://y"))
    state = SimpleNamespace(registry=reg)
    entry = _collect_node_models_metadata(state)
    assert entry is not None
    key, value, source = entry
    assert key == "models"
    assert source == "auto"
    rows = json.loads(value)
    assert len(rows) == 2
    aliases = {r["alias"] for r in rows}
    assert aliases == {"a", "b"}
    assert all(r["healthy"] is True for r in rows)
    assert all(r["provider"] == "openai" for r in rows)
    # Provider-side model identifier intentionally omitted — coords
    # kept passing it as ``spawn_workstream(model=...)`` when they
    # should have passed the local alias.  Lock the projected keys
    # so a future contributor doesn't reintroduce the footgun.
    for row in rows:
        assert set(row.keys()) == {"alias", "provider", "healthy"}


def test_health_flag_reflects_tracker_state():
    reg = _registry(("a", "http://x"), ("b", "http://y"))
    health_reg = HealthTrackerRegistry(failure_threshold=2)
    # Seed the tracker for "a"'s backend and drive it into the degraded
    # state — two consecutive failures cross the threshold.
    bad_tracker = health_reg.get_tracker(provider="openai", base_url="http://x")
    bad_tracker.record_failure()
    bad_tracker.record_failure()
    assert bad_tracker.is_degraded
    # "b" gets a tracker that has only seen successes.
    good_tracker = health_reg.get_tracker(provider="openai", base_url="http://y")
    good_tracker.record_success()
    state = SimpleNamespace(registry=reg, health_registry=health_reg)
    rows = json.loads(_collect_node_models_metadata(state)[1])
    by_alias = {r["alias"]: r for r in rows}
    assert by_alias["a"]["healthy"] is False
    assert by_alias["b"]["healthy"] is True


def test_two_aliases_sharing_a_backend_share_a_tracker():
    """Two aliases that point at the same ``(provider, base_url)``
    share a single :class:`BackendHealthTracker` — degrading one is
    expected to surface as degraded on the other.  The list_nodes
    projection should respect that, otherwise a coord could see
    ``alias-a`` healthy and ``alias-b`` degraded for the same
    backend."""
    reg = _registry(("alpha", "http://shared"), ("beta", "http://shared"))
    health_reg = HealthTrackerRegistry(failure_threshold=1)
    tracker = health_reg.get_tracker(provider="openai", base_url="http://shared")
    tracker.record_failure()  # threshold=1 — degraded immediately
    state = SimpleNamespace(registry=reg, health_registry=health_reg)
    rows = json.loads(_collect_node_models_metadata(state)[1])
    assert {r["alias"]: r["healthy"] for r in rows} == {"alpha": False, "beta": False}


def test_alias_with_no_tracker_yet_defaults_to_healthy():
    """An alias the registry knows about but whose backend hasn't been
    invoked yet has no tracker.  Default to healthy so a brand-new
    alias is immediately visible to coordinators rather than waiting
    for the first request to seed a tracker.

    The collector calls ``health_reg.get_tracker(...)`` which mints a
    fresh tracker on first lookup — that's the path under test here.
    The freshly minted tracker reports ``is_healthy=True`` (default
    state), so the projection labels the alias healthy.
    """
    reg = _registry(("a", "http://x"))
    health_reg = HealthTrackerRegistry()  # empty — no trackers seeded
    state = SimpleNamespace(registry=reg, health_registry=health_reg)
    rows = json.loads(_collect_node_models_metadata(state)[1])
    assert rows[0]["healthy"] is True


# ---------------------------------------------------------------------------
# _publish_models_metadata — cache short-circuit + projection wiring
# ---------------------------------------------------------------------------


def _publish_state() -> SimpleNamespace:
    """Build an ``app_state`` with a minimal registry + health surface."""
    reg = _registry(("a", "http://x"))
    return SimpleNamespace(registry=reg, health_registry=HealthTrackerRegistry())


def test_publish_writes_when_payload_changes():
    """First publish has nothing in the cache — write happens; cache
    fills.  Second publish on the same unchanged registry skips the
    write entirely."""
    state = _publish_state()
    storage = MagicMock()
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 1
    cached = state._last_models_payload
    assert isinstance(cached, str) and "alias" in cached
    # Second call, same registry, same health: cached payload matches
    # — write must be skipped to avoid the per-30s UPSERT churn.
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 1


def test_publish_records_metric_outcome(monkeypatch):
    """The publish helper feeds ``record_node_models_publish`` so
    Prometheus can expose the hit-rate.  Storage failures must NOT
    record either outcome — counters should reflect actual cache
    decisions, not transient DB errors that will retry.

    Replaces the module-level ``turnstone.server._metrics`` binding
    via string-form monkeypatch (with auto-restore) rather than
    patching an instance attribute on the imported singleton.  Other
    tests in the suite reassign ``srv_mod._metrics`` (some without
    using monkeypatch), so an instance captured at import time can
    diverge from the binding the live ``_publish_models_metadata``
    reads on each call.
    """
    state = _publish_state()
    storage = MagicMock()
    calls: list[bool] = []

    class _FakeMetrics:
        def record_node_models_publish(self, *, written: bool) -> None:
            calls.append(written)

    monkeypatch.setattr("turnstone.server._metrics", _FakeMetrics())

    _publish_models_metadata(state, storage, "node-a")  # first → write
    _publish_models_metadata(state, storage, "node-a")  # second → skip
    assert calls == [True, False]

    # Storage error: no metric recorded.
    storage.set_node_metadata_bulk.side_effect = RuntimeError("db down")
    state._last_models_payload = None  # invalidate cache to force a write attempt
    _publish_models_metadata(state, storage, "node-a")
    assert calls == [True, False]  # unchanged


def test_publish_rewrites_when_health_flips():
    """A health-tracker state change must invalidate the cache and
    drive a fresh write — otherwise the discovery surface would lag
    a flip indefinitely."""
    state = _publish_state()
    storage = MagicMock()
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 1
    # Drive the only tracker to degraded.
    tracker = state.health_registry.get_tracker(provider="openai", base_url="http://x")
    for _ in range(10):
        tracker.record_failure()
    assert tracker.is_degraded
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 2


def test_publish_swallows_storage_error_without_updating_cache():
    """A storage failure must NOT poison the cache — the next call
    should retry the write rather than think it succeeded."""
    state = _publish_state()
    storage = MagicMock()
    storage.set_node_metadata_bulk.side_effect = RuntimeError("db down")
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 1
    assert getattr(state, "_last_models_payload", None) is None
    # Recover: a subsequent successful call writes again.
    storage.set_node_metadata_bulk.side_effect = None
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 2
    assert state._last_models_payload is not None


def test_publish_skips_when_registry_missing():
    """Without a registry there's nothing to project; nothing should
    be written and the cache must not be set."""
    state = SimpleNamespace()
    storage = MagicMock()
    _publish_models_metadata(state, storage, "node-a")
    assert storage.set_node_metadata_bulk.call_count == 0
    assert getattr(state, "_last_models_payload", None) is None


# ---------------------------------------------------------------------------
# internal_model_reload — integration: registry change must rewrite the row
# ---------------------------------------------------------------------------


def test_model_reload_endpoint_rewrites_models_metadata(monkeypatch, tmp_path):
    """A successful ``internal_model_reload`` must refresh
    ``node_metadata.models`` so a coordinator sees the new alias on
    its next ``list_nodes`` without waiting up to 30s for the
    heartbeat tick.

    The endpoint pulls a fresh registry from
    ``load_model_registry(...)`` and reloads in-place — we stub the
    loader to return a registry with a different alias set so the
    publish-cache invalidation is exercised end-to-end.
    """
    from turnstone.core.storage._sqlite import SQLiteBackend
    from turnstone.server import internal_model_reload

    storage = SQLiteBackend(str(tmp_path / "reload.db"))

    # Old registry — single alias "a".
    old_reg = _registry(("a", "http://x"))
    # New registry that ``load_model_registry`` will return — adds "b".
    new_reg = ModelRegistry(
        {
            "a": ModelConfig(
                alias="a", base_url="http://x", api_key="k", model="a", provider="openai"
            ),
            "b": ModelConfig(
                alias="b", base_url="http://y", api_key="k", model="b", provider="openai"
            ),
        },
        default="a",
    )
    health_reg = HealthTrackerRegistry()

    app_state = SimpleNamespace(
        registry=old_reg,
        health_registry=health_reg,
        cli_model_args={
            "base_url": "",
            "api_key": "",
            "model": "",
            "context_window": 0,
            "provider": "openai",
        },
        config_store=None,
        node_id="node-a",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    # Patch the loader and storage accessors used inside the endpoint.
    # ``internal_model_reload`` does ``from turnstone.core.storage._registry
    # import get_storage`` inline, so patching the symbol on that module
    # is what intercepts the call.
    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", lambda **_kw: new_reg)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    # The endpoint also broadcasts schema refreshes to active sessions
    # — stub this out, it's irrelevant to the metadata-write path.
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    response = internal_model_reload(request)  # type: ignore[arg-type]
    assert response.status_code == 200

    rows = storage.get_node_metadata("node-a")
    by_key = {r["key"]: r for r in rows}
    assert "models" in by_key
    payload = json.loads(by_key["models"]["value"])
    assert {r["alias"] for r in payload} == {"a", "b"}


# ---------------------------------------------------------------------------
# Shutdown race: heartbeat write must NOT resurrect post-shutdown delete
# ---------------------------------------------------------------------------


def test_heartbeat_write_awaits_before_shutdown_delete():
    """Pin the shutdown-race fix.

    Before the fix, the lifespan shutdown sequence was:

    1. ``_heartbeat_task.cancel()`` — fire-and-forget
    2. ``delete_node_metadata_by_source(node_id, "auto")``

    A heartbeat tick already inside ``asyncio.to_thread(...)`` for
    the ``set_node_metadata_bulk`` call would complete AFTER step 2,
    resurrecting the deleted ``models`` row.  The fix awaits the
    cancelled task with ``contextlib.suppress(...)`` between (1) and
    (2), so the in-flight write lands first.

    We verify the fix by introspecting ``server.py`` source — the
    real lifespan is hard to test deterministically without a full
    Starlette app, but the textual ordering between
    ``_heartbeat_task.cancel()`` and the delete is a stable contract
    that catches the regression cheaply.
    """
    import inspect
    import sys

    src = inspect.getsource(sys.modules[_collect_node_models_metadata.__module__])
    cancel_idx = src.find("_heartbeat_task.cancel()")
    delete_idx = src.find('delete_node_metadata_by_source, _svc_node_id, "auto"')
    await_idx = src.find("await _heartbeat_task", cancel_idx)
    assert cancel_idx != -1
    assert delete_idx != -1
    assert await_idx != -1
    # The fix-line must sit BETWEEN the cancel and the delete.
    assert cancel_idx < await_idx < delete_idx, (
        "Shutdown race regression: "
        "_heartbeat_task.cancel() must be followed by `await _heartbeat_task` "
        "BEFORE delete_node_metadata_by_source(..., 'auto') so an in-flight "
        "set_node_metadata_bulk lands before the delete."
    )
