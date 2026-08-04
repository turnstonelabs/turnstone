"""Console reranker-calibration endpoint + calibrate-on-detect.

Covers the two server paths added in Phase 3:

- ``POST /api/admin/model-definitions/{id}/calibrate`` — probes a saved
  reranker, persists the three calibration fields onto its capabilities, and
  returns a verdict; a calibration failure is graceful (no 500, nothing
  persisted).
- ``POST /api/admin/model-definitions/detect`` with ``supports_rerank`` —
  calibrates the /rerank endpoint *directly* (a reranker's base_url is its full
  /rerank path, which the ``/v1/models`` probe can't reach), so the probe is
  skipped and the calibration round-trip IS the reachability check: success
  autopopulates the three fields, failure reports ``reachable: False``. A
  non-rerank detect still probes ``/v1/models`` and takes no calibration path.

``calibrate_model`` is monkeypatched (it needs a live /rerank endpoint).
Mirrors the TestClient wiring in test_admin_model_registry_refresh.py.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import (
    admin_calibrate_model_definition,
    admin_detect_model,
)
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.rerank_calibrate import CalibrationResult
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "models.db"))


def _seed_reranker(
    storage: SQLiteBackend,
    *,
    definition_id: str = "r1",
    alias: str = "reranker",
    base_url: str = "http://localhost:9999/rerank",
    caps: dict[str, Any] | None = None,
) -> None:
    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model="bge-reranker",
        provider="openai-compatible",
        base_url=base_url,
        api_key="sk-test",
        context_window=0,
        capabilities=json.dumps(caps if caps is not None else {"supports_rerank": True}),
        created_by="admin",
    )


def _make_registry(alias: str = "reranker") -> ModelRegistry:
    return ModelRegistry(
        {
            alias: ModelConfig(
                alias=alias,
                base_url="http://localhost:9999/rerank",
                api_key="sk-test",
                model="bge-reranker",
                context_window=0,
                provider="openai-compatible",
                source="db",
                capabilities={"supports_rerank": True},
            )
        },
        default=alias,
    )


def _result(*, separated: bool, threshold: float | None) -> CalibrationResult:
    return CalibrationResult(
        model="bge-reranker",
        raw_scale="logit (sigmoid-normalised)",
        separated=separated,
        suggested_threshold=threshold,
        relevant_min=0.7,
        relevant_max=0.95,
        irrelevant_min=0.05,
        irrelevant_max=0.3,
        n_relevant=18,
        n_irrelevant=306,
    )


def _make_client(storage: SQLiteBackend, registry: ModelRegistry | None) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/admin/model-definitions/{definition_id}/calibrate",
                admin_calibrate_model_definition,
                methods=["POST"],
            ),
            Route(
                "/v1/api/admin/model-definitions/detect",
                admin_detect_model,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = registry
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    app.state.config_store.get.return_value = ""
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.models"})
    return client


def _stub_calibrate(monkeypatch: pytest.MonkeyPatch, result: CalibrationResult) -> None:
    monkeypatch.setattr(
        "turnstone.core.rerank_calibrate.calibrate_model",
        lambda base_url, model, api_key, *, instruction="", timeout=60.0: result,
    )


# ---------------------------------------------------------------------------
# Calibrate endpoint
# ---------------------------------------------------------------------------


def test_calibrate_persists_and_returns_verdict(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_reranker(storage)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.42))
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["separated"] is True
    assert body["suggested_threshold"] == 0.42
    assert body["applied"] is True
    assert body["error"] == ""
    assert body["relevant"] == [0.7, 0.95]
    assert body["irrelevant"] == [0.05, 0.3]

    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert caps["rerank_threshold"] == 0.42
    assert caps["rerank_scale"] == "logit (sigmoid-normalised)"
    assert caps["rerank_separated"] is True
    assert caps["supports_rerank"] is True  # merge, not replace


def test_concurrent_capabilities_put_survives_calibrate(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge bases itself on a persist-time re-read, not the pre-probe
    snapshot."""
    _seed_reranker(storage, caps={"supports_rerank": True})

    def _calibrate_and_race(
        base_url: str,
        model: str,
        api_key: str,
        *,
        instruction: str = "",
        timeout: float = 60.0,
    ) -> CalibrationResult:
        # Mid-probe writer: a capabilities PUT lands while the probe runs.
        storage.update_model_definition(
            "r1",
            capabilities=json.dumps(
                {"supports_rerank": True, "server_compat": {"extra_body": {"x": 1}}}
            ),
        )
        return _result(separated=True, threshold=0.45)

    monkeypatch.setattr("turnstone.core.rerank_calibrate.calibrate_model", _calibrate_and_race)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert caps["server_compat"] == {"extra_body": {"x": 1}}
    assert caps["rerank_threshold"] == 0.45
    assert caps["rerank_separated"] is True


def test_calibrate_cas_retries_when_write_lands_between_reread_and_persist(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persist is conditional: a missed compare re-merges onto the
    newer value."""
    _seed_reranker(storage, caps={"supports_rerank": True})
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.45))

    real_update = storage.update_model_definition
    state = {"interleaved": False}

    def _update_with_interleaved_writer(definition_id: str, **kwargs: Any) -> bool:
        if not state["interleaved"]:
            state["interleaved"] = True
            # Lands after the handler's re-read + merge, before its persist.
            real_update(
                definition_id,
                capabilities=json.dumps(
                    {"supports_rerank": True, "server_compat": {"extra_body": {"x": 2}}}
                ),
            )
        return real_update(definition_id, **kwargs)

    monkeypatch.setattr(storage, "update_model_definition", _update_with_interleaved_writer)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert state["interleaved"]
    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert caps["server_compat"] == {"extra_body": {"x": 2}}
    assert caps["rerank_threshold"] == 0.45
    assert caps["rerank_separated"] is True


def test_calibrate_yields_409_under_sustained_concurrent_writes(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries are bounded: under sustained pressure the competing writes
    win, not the calibrate."""
    _seed_reranker(storage, caps={"supports_rerank": True})
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.45))

    real_update = storage.update_model_definition
    state = {"n": 0}

    def _update_with_persistent_writer(definition_id: str, **kwargs: Any) -> bool:
        if "expected_capabilities" in kwargs:
            # A different value before every attempt, so each fresh
            # re-read is stale by persist time.
            state["n"] += 1
            real_update(
                definition_id,
                capabilities=json.dumps({"supports_rerank": True, "rev": state["n"]}),
            )
        return real_update(definition_id, **kwargs)

    monkeypatch.setattr(storage, "update_model_definition", _update_with_persistent_writer)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")

    assert resp.status_code == 409, resp.text
    assert "concurrently" in resp.json()["error"]
    assert state["n"] == 3  # the retry budget
    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert "rerank_threshold" not in caps
    assert caps["rev"] == 3


def test_row_deleted_mid_probe_is_not_found(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_reranker(storage)

    def _calibrate_and_delete(
        base_url: str,
        model: str,
        api_key: str,
        *,
        instruction: str = "",
        timeout: float = 60.0,
    ) -> CalibrationResult:
        storage.delete_model_definition("r1")
        return _result(separated=True, threshold=0.45)

    monkeypatch.setattr("turnstone.core.rerank_calibrate.calibrate_model", _calibrate_and_delete)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")

    assert resp.status_code == 404, resp.text
    assert storage.get_model_definition("r1") is None


def test_calibrate_refuses_merge_that_writes_out_of_band_keys(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge reaching a non-calibration key is refused, not smuggled past
    the admin.mcp gate this endpoint lacks."""
    _seed_reranker(storage)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.42))
    stored_before = storage.get_model_definition("r1")["capabilities"]

    def _rogue_merge(raw_caps: Any, result: CalibrationResult) -> str:
        caps = json.loads(raw_caps or "{}")
        caps["rerank_threshold"] = 0.42
        caps["server_compat"] = {"api_surface": "responses"}
        return json.dumps(caps)

    monkeypatch.setattr("turnstone.core.rerank_calibrate.merge_calibration_into_caps", _rogue_merge)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")

    assert resp.status_code == 500, resp.text
    assert "server_compat" in resp.json()["error"]
    assert storage.get_model_definition("r1")["capabilities"] == stored_before


def test_calibrate_no_separation_persists_marker(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clean split still persists the marker (rerank_scale) + separated=False
    so the chip can warn; threshold is 0.0 (the floor logic disables)."""
    _seed_reranker(storage)
    _stub_calibrate(monkeypatch, _result(separated=False, threshold=None))
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["separated"] is False
    assert body["applied"] is True

    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert caps["rerank_scale"] == "logit (sigmoid-normalised)"
    assert caps["rerank_separated"] is False
    assert caps["rerank_threshold"] == 0.0


def test_calibrate_failure_is_graceful(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """calibrate raising (unreachable / non-reranker) -> verdict with error,
    no 500, model capabilities untouched."""
    _seed_reranker(storage)

    def _boom(base_url, model, api_key, *, instruction="", timeout=60.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("turnstone.core.rerank_calibrate.calibrate_model", _boom)
    client = _make_client(storage, _make_registry())

    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["separated"] is False
    assert "connection refused" in body["error"]

    caps = json.loads(storage.get_model_definition("r1")["capabilities"])
    assert "rerank_scale" not in caps  # nothing persisted on failure


def test_calibrate_unknown_definition_404(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.42))
    client = _make_client(storage, None)
    resp = client.post("/v1/api/admin/model-definitions/nope/calibrate")
    assert resp.status_code == 404


def test_calibrate_no_base_url_graceful(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_reranker(storage, base_url="")
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.42))
    client = _make_client(storage, _make_registry())
    resp = client.post("/v1/api/admin/model-definitions/r1/calibrate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert "base_url" in body["error"]


# ---------------------------------------------------------------------------
# Calibrate-on-detect
# ---------------------------------------------------------------------------


def _stub_probe(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "turnstone.core.model_registry.probe_model_endpoint",
        lambda provider, base_url, api_key, target_model="": dict(result),
    )


def test_detect_reranker_calibrates_directly_skipping_probe(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reranker detect must NOT touch /v1/models (its base_url is the full
    /rerank path) — it calibrates directly and autopopulates the three fields."""
    probe_called: list[Any] = []
    monkeypatch.setattr(
        "turnstone.core.model_registry.probe_model_endpoint",
        lambda *a, **kw: probe_called.append(a) or {"reachable": True},
    )
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.33))
    client = _make_client(storage, None)

    resp = client.post(
        "/v1/api/admin/model-definitions/detect",
        json={
            "provider": "openai-compatible",
            "base_url": "http://localhost:9999/rerank",
            "model": "bge-reranker",
            "supports_rerank": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert probe_called == []  # /v1/models never probed for a reranker
    body = resp.json()
    assert body["reachable"] is True
    assert body["available_models"] == []
    caps = body.get("capabilities", {})
    assert caps["supports_rerank"] is True
    assert caps["rerank_threshold"] == 0.33
    assert caps["rerank_scale"] == "logit (sigmoid-normalised)"
    assert caps["rerank_separated"] is True


def test_detect_reranker_no_separation_notes_but_reachable(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable reranker with no clean split: fields still populate (scale
    marker + separated=False, threshold 0) and a note warns; still reachable."""
    _stub_calibrate(monkeypatch, _result(separated=False, threshold=None))
    client = _make_client(storage, None)

    resp = client.post(
        "/v1/api/admin/model-definitions/detect",
        json={
            "provider": "openai-compatible",
            "base_url": "http://localhost:9999/rerank",
            "model": "bge-reranker",
            "supports_rerank": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    caps = body["capabilities"]
    assert caps["rerank_separated"] is False
    assert caps["rerank_threshold"] == 0.0
    assert "no clean separation" in body["rerank_calibration_note"]


def test_detect_reranker_calibration_failure_is_unreachable(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Can't calibrate -> it isn't a working reranker: reachable False + error,
    no capabilities (there is no independent /v1/models reachability signal)."""

    def _boom(base_url, model, api_key, *, instruction="", timeout=60.0):
        raise RuntimeError("not a reranker")

    monkeypatch.setattr("turnstone.core.rerank_calibrate.calibrate_model", _boom)
    client = _make_client(storage, None)

    resp = client.post(
        "/v1/api/admin/model-definitions/detect",
        json={
            "provider": "openai-compatible",
            "base_url": "http://localhost:9999/rerank",
            "model": "bge-reranker",
            "supports_rerank": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert not body.get("capabilities")
    assert "not a reranker" in body["error"]


def test_detect_reranker_requires_base_url(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reranker has no provider-default URL — an empty base_url is a clear 400
    (not a probe against some default host) and never attempts calibration."""
    called: list[Any] = []
    monkeypatch.setattr(
        "turnstone.core.rerank_calibrate.calibrate_model",
        lambda *a, **kw: called.append(a) or _result(separated=True, threshold=0.5),
    )
    client = _make_client(storage, None)

    resp = client.post(
        "/v1/api/admin/model-definitions/detect",
        json={
            "provider": "openai-compatible",
            "base_url": "",
            "model": "bge-reranker",
            "supports_rerank": True,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "/rerank" in resp.json()["error"]
    assert called == []  # never attempted calibration with an empty url


def test_detect_non_reranker_skips_calibration(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal detect (no supports_rerank) must not call calibrate at all —
    no extra round-trip / slowdown for chat models."""
    _stub_probe(
        monkeypatch,
        {
            "reachable": True,
            "available_models": ["gpt-x"],
            "context_window": 128000,
            "error": None,
        },
    )
    called: list[Any] = []
    monkeypatch.setattr(
        "turnstone.core.rerank_calibrate.calibrate_model",
        lambda *a, **kw: called.append(a) or _result(separated=True, threshold=0.5),
    )
    client = _make_client(storage, None)

    resp = client.post(
        "/v1/api/admin/model-definitions/detect",
        json={
            "provider": "openai-compatible",
            "base_url": "http://localhost:8000/v1",
            "model": "gpt-x",
        },
    )
    assert resp.status_code == 200, resp.text
    assert called == []  # calibrate never invoked
    body = resp.json()
    assert "capabilities" not in body or not body["capabilities"]
    assert body["context_window"] == 128000  # ordinary detect unchanged
