"""Console: secret:// references are validated on write, shown verbatim on read and
materialised for probes; MCP env takes no references."""

from __future__ import annotations

import asyncio
import json
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
    _coord_registry_warning,
    _mask_mcp_secrets,
    _mask_model_secrets,
    _mcp_secret_fields_problem,
    _secret_reference_problem,
    admin_create_model_definition,
    admin_detect_model,
    admin_list_model_definitions,
    admin_update_model_definition,
)
from turnstone.core.oauth.context import oauth_context
from turnstone.core.secret_refs import SecretResolver, reset_for_tests
from turnstone.core.secret_refs import _resolver as resolver_module
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError

REF = "secret://fake/openai"


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.values: dict[str, str] = {"/openai": "sk-from-store"}
        self.fail: SecretBackendError | None = None

    def fetch(self, path: str, key: str | None) -> str:
        if self.fail is not None:
            raise self.fail
        if path not in self.values:
            raise SecretBackendError(f"{path.strip('/')!r} not found", retryable=False)
        return self.values[path]

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_host_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = FakeBackend()
    resolver = SecretResolver(parse_secrets_config({}), backends={"fake": backend})
    monkeypatch.setattr(resolver_module, "_instance", resolver)
    yield backend
    reset_for_tests()


class TestMasks:
    def test_model_mask_keeps_a_reference(self) -> None:
        assert _mask_model_secrets({"api_key": "sk-x"})["api_key"] == "***"
        assert _mask_model_secrets({"api_key": REF})["api_key"] == REF
        assert _mask_model_secrets({"api_key": ""})["api_key"] == ""

    def test_mcp_mask_keeps_header_references(self) -> None:
        row = {
            "headers": json.dumps({"Authorization": "Bearer x", "X-Ref": REF}),
            "env": json.dumps({"TOKEN": "t"}),
            "oauth_client_secret_ct": None,
        }
        masked = _mask_mcp_secrets(row)
        assert json.loads(masked["headers"]) == {"Authorization": "***", "X-Ref": REF}
        assert json.loads(masked["env"]) == {"TOKEN": "***"}
        revealed = _mask_mcp_secrets(row, reveal=True)
        assert json.loads(revealed["headers"])["X-Ref"] == REF


class TestWriteTimeChecks:
    def test_reference_problems(self, fake_store: FakeBackend) -> None:
        assert asyncio.run(_secret_reference_problem(REF)) is None
        message, status = asyncio.run(_secret_reference_problem("secret://fake/nope"))  # type: ignore[misc]
        assert status == 400 and "not found" in message
        message, status = asyncio.run(_secret_reference_problem("secret://vault/x#k"))  # type: ignore[misc]
        assert status == 400 and "not configured" in message
        message, status = asyncio.run(_secret_reference_problem("secret://fake/a b"))  # type: ignore[misc]
        assert status == 400
        # The first resolve above is cached, so an outage is invisible for REF; a
        # reference never seen before surfaces the outage as a 503.
        fake_store.fail = SecretBackendError("store unavailable", retryable=True)
        assert asyncio.run(_secret_reference_problem(REF)) is None
        message, status = asyncio.run(_secret_reference_problem("secret://fake/fresh"))  # type: ignore[misc]
        assert status == 503 and "store unavailable" in message

    def test_mcp_fields(self, fake_store: FakeBackend) -> None:
        assert asyncio.run(_mcp_secret_fields_problem({"Authorization": REF}, {})) is None
        assert asyncio.run(_mcp_secret_fields_problem({"X": "plain"}, {"K": "v"})) is None
        problem = asyncio.run(_mcp_secret_fields_problem({}, {"TOKEN": REF}))
        assert problem is not None and problem[1] == 400 and "env does not take" in problem[0]
        problem = asyncio.run(_mcp_secret_fields_problem({"Authorization": f"Bearer {REF}"}, {}))
        assert problem is not None and "whole value" in problem[0]
        problem = asyncio.run(
            _mcp_secret_fields_problem({"Authorization": "secret://fake/nope"}, {})
        )
        assert problem is not None and problem[1] == 400 and "'Authorization'" in problem[0]


class TestRegistryWarning:
    def test_error_wins_then_skipped_summary(self) -> None:
        state = MagicMock(coord_registry_error="refused", coord_registry_skipped={"a": "r"})
        assert _coord_registry_warning(state) == "refused"
        state = MagicMock(coord_registry_error="", coord_registry_skipped={"b": "nope", "a": "r"})
        assert _coord_registry_warning(state) == (
            "model definitions skipped on the console: a: r; b: nope"
        )
        assert (
            _coord_registry_warning(MagicMock(coord_registry_error="", coord_registry_skipped={}))
            == ""
        )


def _make_client(storage: Any, perms: str) -> TestClient:
    app = Starlette(
        routes=[
            Route("/v1/api/admin/model-definitions", admin_list_model_definitions, methods=["GET"]),
            Route(
                "/v1/api/admin/model-definitions", admin_create_model_definition, methods=["POST"]
            ),
            Route("/v1/api/admin/model-definitions/detect", admin_detect_model, methods=["POST"]),
            Route(
                "/v1/api/admin/model-definitions/{definition_id}",
                admin_update_model_definition,
                methods=["PUT"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = None
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    app.state.config_store.get.side_effect = lambda key, default=None: (
        "local" if key == "model.default_alias" else default
    )
    oauth_context(app.state).oidc_config = make_oidc_config()
    oauth_context(app.state).token_store = MagicMock()
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": perms})
    return client


@pytest.fixture
def storage(tmp_path: Any, sqlite_backend_factory: Any) -> Any:
    return sqlite_backend_factory(str(tmp_path / "models.db"))


@pytest.fixture(autouse=True)
def _stub_console_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from turnstone.console import server as server_module

    monkeypatch.setattr(server_module, "_ensure_console_mcp_client", lambda _app: {"skipped": "t"})
    monkeypatch.setattr(server_module, "_maybe_bootstrap_coord_subsystem", lambda _app, _s: None)


class TestModelEndpoints:
    def _body(self, api_key: str) -> dict[str, Any]:
        return {"alias": "m", "model": "gpt-5", "provider": "openai", "api_key": api_key}

    def test_reference_needs_admin_mcp(self, storage: Any, fake_store: FakeBackend) -> None:
        client = _make_client(storage, "admin.models")
        resp = client.post("/v1/api/admin/model-definitions", json=self._body(REF))
        assert resp.status_code == 403
        assert storage.get_model_definition_by_alias("m") is None

    def test_reference_is_resolved_once_then_stored_and_shown_verbatim(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        client = _make_client(storage, "admin.models,admin.mcp")
        resp = client.post("/v1/api/admin/model-definitions", json=self._body(REF))
        assert resp.status_code == 200, resp.text
        assert resp.json()["api_key"] == REF
        row = storage.get_model_definition_by_alias("m")
        assert row is not None and row["api_key"] == REF  # the literal never reaches the DB
        listed = client.get("/v1/api/admin/model-definitions").json()["models"]
        assert [m["api_key"] for m in listed if m["alias"] == "m"] == [REF]

    def test_bad_reference_is_refused_before_the_write(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        client = _make_client(storage, "admin.models,admin.mcp")
        resp = client.post("/v1/api/admin/model-definitions", json=self._body("secret://fake/nope"))
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"]
        resp = client.post(
            "/v1/api/admin/model-definitions", json=self._body("secret://fake/a%20b")
        )
        assert resp.status_code == 400
        assert storage.get_model_definition_by_alias("m") is None

    def test_update_to_a_reference_is_gated_and_checked(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        client = _make_client(storage, "admin.models,admin.mcp")
        created = client.post(
            "/v1/api/admin/model-definitions", json=self._body("sk-literal")
        ).json()
        did = created["definition_id"]
        plain = _make_client(storage, "admin.models")
        assert (
            plain.put(f"/v1/api/admin/model-definitions/{did}", json={"api_key": REF}).status_code
            == 403
        )
        resp = client.put(f"/v1/api/admin/model-definitions/{did}", json={"api_key": REF})
        assert resp.status_code == 200, resp.text
        assert resp.json()["api_key"] == REF
        # '***' keeps the stored reference, as it keeps a literal.
        resp = client.put(f"/v1/api/admin/model-definitions/{did}", json={"api_key": "***"})
        assert resp.json()["api_key"] == REF

    def test_detect_materialises_the_stored_reference(
        self, storage: Any, fake_store: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(storage, "admin.models,admin.mcp")
        did = client.post("/v1/api/admin/model-definitions", json=self._body(REF)).json()[
            "definition_id"
        ]
        seen: dict[str, Any] = {}

        def fake_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen["api_key"] = args[2] if len(args) > 2 else kwargs.get("api_key")
            return {"reachable": True, "model_found": True, "context_window": 200000}

        monkeypatch.setattr("turnstone.core.model_registry.probe_model_endpoint", fake_probe)
        resp = client.post(
            "/v1/api/admin/model-definitions/detect",
            json={
                "provider": "openai",
                "base_url": "",
                "api_key": "***",
                "model": "gpt-5",
                "definition_id": did,
            },
        )
        assert resp.status_code == 200, resp.text
        assert seen["api_key"] == "sk-from-store"
        # A reference typed into the form resolves too (detect before save); one
        # the store does not know is refused before any probe runs.
        resp = client.post(
            "/v1/api/admin/model-definitions/detect",
            json={
                "provider": "openai",
                "base_url": "",
                "api_key": "secret://fake/nope",
                "model": "gpt-5",
            },
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"]
