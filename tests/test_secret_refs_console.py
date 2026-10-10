"""Console: secret:// references are validated on write, shown verbatim on read and
materialised for probes; MCP env takes no references."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from tests._oidc_test_helpers import make_oidc_config
from tests._secret_refs_helpers import FakeBackend, install_fake_resolver
from turnstone.console.server import (
    _check_mcp_secret_fields,
    _coord_registry_warning,
    _mask_mcp_secrets,
    _mask_model_secrets,
    _mcp_secret_fields_problem,
    _secret_reference_problem,
    admin_create_model_definition,
    admin_detect_model,
    admin_import_mcp_config,
    admin_list_model_definitions,
    admin_update_mcp_server,
    admin_update_model_definition,
)
from turnstone.core.auth import AuthResult
from turnstone.core.model_registry import ModelRegistry
from turnstone.core.oauth.context import oauth_context
from turnstone.core.secret_refs._errors import SecretBackendError

REF = "secret://fake/openai"


@pytest.fixture(autouse=True)
def _no_host_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    return install_fake_resolver(monkeypatch, {"/openai": "sk-from-store"})


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
        # Only a whole-value reference is refused in env; a literal that merely
        # mentions the scheme is the child process's business.
        assert asyncio.run(_mcp_secret_fields_problem({}, {"URI": f"x?src={REF}"})) is None
        problem = asyncio.run(_mcp_secret_fields_problem({"Authorization": f"Bearer {REF}"}, {}))
        assert problem is not None and "whole value" in problem[0]
        problem = asyncio.run(
            _mcp_secret_fields_problem({"Authorization": "secret://fake/nope"}, {})
        )
        assert problem is not None and problem[1] == 400 and "'Authorization'" in problem[0]

    def test_a_header_reference_takes_admin_mcp_without_the_service_bypass(
        self, fake_store: FakeBackend
    ) -> None:
        def request_for(scopes: set[str], permissions: set[str]) -> Any:
            auth = AuthResult(
                user_id="u",
                scopes=frozenset(scopes),
                token_source="jwt",
                permissions=frozenset(permissions),
            )
            return SimpleNamespace(state=SimpleNamespace(auth_result=auth))

        service = request_for({"service"}, set())
        # A literal header is fine for a service token (the handler's own
        # admin.mcp check lets the bypass through); a reference is not.
        assert asyncio.run(_check_mcp_secret_fields(service, {"X": "plain"}, {})) is None
        refused = asyncio.run(_check_mcp_secret_fields(service, {"Authorization": REF}, {}))
        assert refused is not None and refused.status_code == 403
        holder = request_for({"write"}, {"admin.mcp"})
        assert asyncio.run(_check_mcp_secret_fields(holder, {"Authorization": REF}, {})) is None


class TestRegistryWarning:
    def test_error_wins_then_skipped_summary(self) -> None:
        state = MagicMock(coord_registry_error="refused", coord_registry=None)
        assert _coord_registry_warning(state) == "refused"
        registry = ModelRegistry({}, default="", skipped_aliases={"b": "nope", "a": "r"})
        state = MagicMock(coord_registry_error="", coord_registry=registry)
        assert _coord_registry_warning(state) == (
            "model definitions skipped on the console: a: r; b: nope"
        )
        registry.skipped_aliases = {}
        assert _coord_registry_warning(state) == ""
        assert (
            _coord_registry_warning(MagicMock(coord_registry_error="", coord_registry=None)) == ""
        )


class _ServiceTokenMiddleware(BaseHTTPMiddleware):
    """A service-scope token with no explicit permissions, as a node or CLI presents."""

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="svc", scopes=frozenset({"service"}), token_source="jwt"
        )
        return await call_next(request)


def _make_client(storage: Any, perms: str, *, service: bool = False) -> TestClient:
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
            Route("/v1/api/admin/mcp-servers/import", admin_import_mcp_config, methods=["POST"]),
            Route(
                "/v1/api/admin/mcp-servers/{server_id}", admin_update_mcp_server, methods=["PUT"]
            ),
        ],
        middleware=[Middleware(_ServiceTokenMiddleware if service else _AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.coord_registry = None
    app.state.collector = MagicMock()
    app.state.collector.get_all_nodes.return_value = []
    app.state.proxy_client = MagicMock()
    app.state.config_store = MagicMock()
    app.state.config_store.get.side_effect = lambda key, default=None: {
        "model.default_alias": "local",
        "cluster.mcp_max_servers": 50,
    }.get(key, default)
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
        # So does the reference echoed back unchanged (GET shows it verbatim), with
        # neither the gate nor a store round trip: a read-modify-write client
        # holding admin.models alone can still flip other fields.
        fake_store.fail = SecretBackendError("store unavailable", retryable=True)
        resp = plain.put(
            f"/v1/api/admin/model-definitions/{did}", json={"api_key": REF, "enabled": False}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["api_key"] == REF and resp.json()["enabled"] is False

    def test_audit_names_the_reference(self, storage: Any, fake_store: FakeBackend) -> None:
        client = _make_client(storage, "admin.models,admin.mcp")
        did = client.post("/v1/api/admin/model-definitions", json=self._body(REF)).json()[
            "definition_id"
        ]
        client.put(f"/v1/api/admin/model-definitions/{did}", json={"api_key": "sk-literal"})
        client.put(
            f"/v1/api/admin/model-definitions/{did}", json={"api_key": "secret://fake/openai"}
        )
        details = [
            json.loads(e["detail"])
            for e in storage.list_audit_events(resource_id=did)
            if e["action"].startswith("model_definition.")
        ]
        keys = [d.get("api_key") for d in details]
        assert keys.count(REF) == 2  # the create and the second update
        assert "(updated)" in keys and "sk-literal" not in keys

    def test_re_pasting_the_same_literal_is_still_a_write(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        client = _make_client(storage, "admin.models")
        did = client.post("/v1/api/admin/model-definitions", json=self._body("sk-literal")).json()[
            "definition_id"
        ]
        resp = client.put(f"/v1/api/admin/model-definitions/{did}", json={"api_key": "sk-literal"})
        assert resp.status_code == 200, resp.text
        updates = [
            e for e in storage.list_audit_events(resource_id=did) if e["action"].endswith(".update")
        ]
        assert len(updates) == 1 and json.loads(updates[0]["detail"])["api_key"] == "(updated)"

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
        # The stored reference was introduced through the admin.mcp gate, so
        # probing it needs only admin.models.
        plain = _make_client(storage, "admin.models")
        resp = plain.post(
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
        # A reference typed into the request is resolved with the console's
        # store role and sent to the base_url of the same request: the gate a
        # save would apply applies here too.
        seen.clear()
        resp = plain.post(
            "/v1/api/admin/model-definitions/detect",
            json={
                "provider": "openai",
                "base_url": "https://attacker.example/v1",
                "api_key": REF,
                "model": "gpt-5",
            },
        )
        assert resp.status_code == 403
        assert seen == {}
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


class TestMcpEndpoints:
    MCP_REF = "secret://fake/mcp"

    @pytest.fixture(autouse=True)
    def _mcp_secret(self, fake_store: FakeBackend) -> None:
        fake_store.values["/mcp"] = "Bearer live"

    def _server(self, storage: Any, headers: dict[str, str]) -> str:
        storage.create_mcp_server(
            server_id="srv1",
            name="srv",
            transport="streamable-http",
            url="https://mcp.example.com/mcp",
            headers=json.dumps(headers),
        )
        return "srv1"

    def test_unchanged_reference_echoed_back_is_keep_existing(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        sid = self._server(storage, {"Authorization": self.MCP_REF, "X-Plain": "v"})
        # Neither the gate (a service token here) nor the store (down) is
        # consulted for a header value that did not change...
        fake_store.fail = SecretBackendError("store unavailable", retryable=True)
        svc = _make_client(storage, "", service=True)
        body = {"headers": {"Authorization": self.MCP_REF, "X-Plain": "v"}, "enabled": False}
        resp = svc.put(f"/v1/api/admin/mcp-servers/{sid}", json=body)
        assert resp.status_code == 200, resp.text
        assert storage.get_mcp_server(sid)["enabled"] is False
        # ...while a new reference faces both.
        body = {"headers": {"Authorization": "secret://fake/other"}}
        assert svc.put(f"/v1/api/admin/mcp-servers/{sid}", json=body).status_code == 403
        fake_store.fail = None
        holder = _make_client(storage, "admin.mcp")
        resp = holder.put(f"/v1/api/admin/mcp-servers/{sid}", json=body)
        assert resp.status_code == 400 and "not found" in resp.json()["error"]

    def test_import_gates_and_audits_header_references(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        config = {
            "mcpServers": {
                "imp": {
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": self.MCP_REF},
                }
            }
        }
        svc = _make_client(storage, "", service=True)
        resp = svc.post("/v1/api/admin/mcp-servers/import", json={"config": config})
        assert resp.status_code == 403
        assert storage.get_mcp_server_by_name("imp") is None
        holder = _make_client(storage, "admin.mcp")
        resp = holder.post("/v1/api/admin/mcp-servers/import", json={"config": config})
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == ["imp"]
        events = [e for e in storage.list_audit_events(action="mcp_server.import")]
        assert json.loads(events[0]["detail"])["header_references"] == {
            "imp": {"Authorization": self.MCP_REF}
        }


class TestConsoleRefresh:
    def test_a_load_that_drops_every_alias_names_them_and_keeps_the_live_registry(
        self, storage: Any, fake_store: FakeBackend
    ) -> None:
        from turnstone.console.server import _refresh_coord_registry
        from turnstone.core.model_registry import ModelConfig

        storage.create_model_definition(
            definition_id="d1",
            alias="a",
            model="gpt-5",
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="secret://fake/gone",
            context_window=8192,
        )
        live = ModelRegistry(
            {"a": ModelConfig(alias="a", base_url="https://x", api_key="sk-live", model="gpt-5")},
            default="a",
        )
        state = SimpleNamespace(coord_registry=live, coord_registry_error="")
        _refresh_coord_registry(state, storage)
        assert state.coord_registry is live and live.get_config("a").api_key == "sk-live"
        assert "not found" in live.skipped_aliases["a"]
        assert "a: " in _coord_registry_warning(state)
