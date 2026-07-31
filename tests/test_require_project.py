"""``server.require_project`` — the opt-in, default-off gate refusing projectless
interactive and coordinator creates.

Four surfaces:
  * the predicate matrix (``require_project_enabled`` / ``require_project_denies_create``);
  * the fork/resume project inheritance + the cross-tenant 403-vs-400 oracle in the
    interactive create validator (``_interactive_create_validate_request``);
  * the console cluster-create proxy's surface-only-require_project / mask-everything
    -else policy (``create_workstream``);
  * the coordinator create mount on the console (gate wired on the real
    ``coord_endpoint_config``, operator tokens not exempt).

Validator tests drive the coroutine synchronously via ``asyncio.run`` so they need no
async-plugin marker. Storage is a MagicMock patched onto the singleton getter that both
the RAW resume-resolve and ``ensure_project_attachable`` read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

# ---------------------------------------------------------------------------
# Predicate matrix
# ---------------------------------------------------------------------------


class _Auth:
    """Minimal AuthResult stand-in: ``has_scope`` + ``token_source``."""

    def __init__(
        self, scopes: tuple[str, ...] = (), token_source: str = "jwt", user_id: str = "alice"
    ) -> None:
        self._scopes = frozenset(scopes)
        self.token_source = token_source
        self.user_id = user_id

    def has_scope(self, scope: str) -> bool:
        return scope in self._scopes


class TestRequireProjectPredicate:
    def test_enabled_off_by_default(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_enabled

        assert require_project_enabled(make_config_store()) is False

    def test_enabled_when_set(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_enabled

        assert (
            require_project_enabled(make_config_store(**{"server.require_project": True})) is True
        )

    def test_enabled_none_config_store_fails_open(self) -> None:
        from turnstone.core.auth import require_project_enabled

        assert require_project_enabled(None) is False

    def test_denies_projectless_when_on(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(), "") is True

    def test_allows_when_off(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        # Flag off: even a projectless create is allowed (byte-identical to today).
        assert require_project_denies_create(make_config_store(), _Auth(), "") is False

    def test_allows_none_config_store(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        # Storage unwired: fail open.
        assert require_project_denies_create(None, _Auth(), "") is False

    def test_allows_with_project(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(), "p1") is False

    def test_whitespace_project_is_projectless(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(), "   ") is True

    @pytest.mark.parametrize("bad", [123, True, ["p1"], {"id": "p1"}, 1.5])
    def test_truthy_non_string_project_is_projectless(
        self, bad: Any, make_config_store: Any
    ) -> None:
        # A truthy non-string project_id must NOT stringify past the gate: the
        # create path coerces non-strings to absent (build_kwargs → None), so
        # the gate has to deny them under require_project or a projectless
        # session gets minted with the policy on. Regression for the
        # str(project_id or "") stringification bypass.
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(), bad) is True

    def test_service_scope_exempt(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(scopes=("service",)), "") is False

    def test_coordinator_source_exempt(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, _Auth(token_source="coordinator"), "") is False

    def test_console_proxy_human_not_exempt(self, make_config_store: Any) -> None:
        # The normal proxied human carries their OWN scopes (no service) and
        # token_source "console-proxy" — gated, not exempt.
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        auth = _Auth(scopes=("read", "write"), token_source="console-proxy")
        assert require_project_denies_create(cs, auth, "") is True

    def test_admin_operator_not_exempt(self, make_config_store: Any) -> None:
        # An operator carries admin-derived scopes (approve) but never `service`,
        # so `admin.coordinator` humans are gated — the predicate keys on scope /
        # token_source, never a permission.
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        auth = _Auth(scopes=("read", "write", "approve"), token_source="jwt")
        assert require_project_denies_create(cs, auth, "") is True

    def test_none_auth_denied_when_projectless(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, None, "") is True

    def test_none_auth_allowed_with_project(self, make_config_store: Any) -> None:
        from turnstone.core.auth import require_project_denies_create

        cs = make_config_store(**{"server.require_project": True})
        assert require_project_denies_create(cs, None, "p1") is False


# ---------------------------------------------------------------------------
# Fork/resume inheritance + the 403-vs-400 cross-tenant oracle (node validator)
# ---------------------------------------------------------------------------


def _src_storage(
    *,
    project_id: str | None = None,
    project_visibility: str = "private",
    project_owner: str = "other",
    members: tuple[str, ...] = (),
    resolve_none: bool = False,
    get_project_missing: bool = False,
) -> MagicMock:
    """Storage double for the resume source: resolve + get_workstream (RAW) and
    the get_project/is_project_member surface ``ensure_project_attachable`` reads."""
    storage = MagicMock()
    storage.resolve_workstream.side_effect = lambda _x: None if resolve_none else "src-canon"
    storage.get_workstream.return_value = {
        "ws_id": "src-canon",
        "project_id": project_id,
        "user_id": "other",
    }
    if get_project_missing or project_id is None:
        storage.get_project.return_value = None
    else:
        storage.get_project.return_value = {
            "project_id": project_id,
            "name": "P",
            "owner_id": project_owner,
            "visibility": project_visibility,
            "state": "active",
        }
    storage.is_project_member.side_effect = lambda pid, uid: uid in members
    return storage


def _validate(monkeypatch: Any, body: dict[str, Any], uid: str, cs: Any, storage: Any) -> Any:
    """Run ``_interactive_create_validate_request`` with a patched storage getter."""
    import turnstone.server as server_mod

    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_store=cs)))
    return asyncio.run(server_mod._interactive_create_validate_request(req, body, uid, []))


class TestResumeInheritanceOracle:
    def _on(self, make_config_store: Any) -> Any:
        return make_config_store(**{"server.require_project": True})

    def test_inherits_attachable_source_project(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        storage = _src_storage(project_id="ppub", project_visibility="public")
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body["project_id"] == "ppub"  # inherited (attachable)

    def test_member_of_private_source_inherits(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        storage = _src_storage(
            project_id="psecret", project_visibility="private", members=("alice",)
        )
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body["project_id"] == "psecret"

    def test_private_source_no_403_oracle(self, monkeypatch: Any, make_config_store: Any) -> None:
        # Source under a private project alice can't access → MUST NOT surface a
        # distinguishable 403; drop to projectless so the gate 400s it uniformly.
        storage = _src_storage(project_id="psecret", project_visibility="private", members=())
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None  # NOT a 403 JSONResponse
        assert body.get("project_id", "") == ""

    def test_projectless_source_no_inherit(self, monkeypatch: Any, make_config_store: Any) -> None:
        storage = _src_storage(project_id=None)
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body.get("project_id", "") == ""

    def test_nonexistent_source_no_inherit(self, monkeypatch: Any, make_config_store: Any) -> None:
        storage = _src_storage(resolve_none=True)
        body: dict[str, Any] = {"resume_ws": "ghost", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body.get("project_id", "") == ""

    def test_dangling_source_project_no_oracle(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        # Source's project was deleted → attach 400 → drop (uniform with the rest).
        storage = _src_storage(project_id="pdead", get_project_missing=True)
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body.get("project_id", "") == ""

    def test_private_and_projectless_indistinguishable(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        # The R1 core: private-source and projectless-source produce IDENTICAL
        # observable outcomes — no cross-tenant oracle.
        cs = self._on(make_config_store)
        b_priv: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        _validate(monkeypatch, b_priv, "alice", cs, _src_storage(project_id="psecret"))
        b_none: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        _validate(monkeypatch, b_none, "alice", cs, _src_storage(project_id=None))
        assert b_priv.get("project_id", "") == b_none.get("project_id", "") == ""

    def test_flag_off_never_resolves(self, monkeypatch: Any, make_config_store: Any) -> None:
        # Byte-identical when off: the source is never resolved, nothing inherited.
        storage = _src_storage(project_id="ppub", project_visibility="public")
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive"}
        res = _validate(monkeypatch, body, "alice", make_config_store(), storage)
        assert res is None
        assert body.get("project_id", "") == ""
        storage.resolve_workstream.assert_not_called()

    def test_explicit_project_discarded_for_projected_source(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        # A fork DISCARDS any explicit project_id and inherits its SOURCE's
        # project — an explicit pick can never re-file a fork's history ([1]).
        storage = _src_storage(project_id="ppub", project_visibility="public")
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive", "project_id": "pchosen"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body["project_id"] == "ppub"  # overridden to the source's project
        storage.resolve_workstream.assert_called()  # a fork always resolves its source

    def test_explicit_project_discarded_projectless_source(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        # The safe-vs-leaky discriminator: a fork of a PROJECTLESS source carrying
        # an explicit owned project_id must NOT file under the pick — the pick is
        # discarded, nothing inherited, so it funnels to the uniform projectless
        # "" (400 downstream), indistinguishable from inaccessible/nonexistent.
        storage = _src_storage(project_id=None)
        body: dict[str, Any] = {"resume_ws": "src", "kind": "interactive", "project_id": "powned"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body.get("project_id", "") == ""

    def test_explicit_project_discarded_nonexistent_source(
        self, monkeypatch: Any, make_config_store: Any
    ) -> None:
        # Same discriminator for a NONEXISTENT source + explicit owned pid: "".
        storage = _src_storage(resolve_none=True)
        body: dict[str, Any] = {"resume_ws": "ghost", "kind": "interactive", "project_id": "powned"}
        res = _validate(monkeypatch, body, "alice", self._on(make_config_store), storage)
        assert res is None
        assert body.get("project_id", "") == ""


# ---------------------------------------------------------------------------
# Console cluster-create proxy: surface only require_project, mask the rest
# ---------------------------------------------------------------------------

_CONSOLE_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _console_headers() -> dict[str, str]:
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    tok = create_jwt(
        user_id="op",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_CONSOLE_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )
    return {"Authorization": f"Bearer {tok}"}


def _node_resp(
    status_code: int, json_body: dict[str, Any] | None = None, content: bytes | None = None
) -> httpx.Response:
    req = httpx.Request("POST", "http://a:8080/v1/api/workstreams/new")
    if content is not None:
        return httpx.Response(status_code, content=content, request=req)
    return httpx.Response(status_code, json=json_body if json_body is not None else {}, request=req)


@contextlib.contextmanager
def _console_client(
    node_response: httpx.Response | None = None, raise_exc: BaseException | None = None
) -> Any:
    """A console TestClient whose proxied node create returns *node_response* (an
    ``httpx.Response``) or raises *raise_exc*. Lifespan is not entered (matches the
    existing cluster-create tests) so the manually-attached proxy_client survives."""
    from starlette.testclient import TestClient

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    collector = MagicMock(spec=ClusterCollector)
    collector.get_node_detail.return_value = {
        "node_id": "node-a",
        "server_url": "http://a:8080",
        "health": {},
        "workstreams": [],
        "aggregate": {},
        "reachable": True,
    }
    collector.get_nodes.return_value = (
        [{"node_id": "node-a", "reachable": True, "max_ws": 10, "ws_total": 1}],
        1,
    )
    collector.get_all_nodes.side_effect = lambda: collector.get_nodes.return_value[0]
    collector.get_overview.return_value = {
        "nodes": 1,
        "workstreams": 0,
        "states": {"running": 0, "idle": 0, "thinking": 0, "attention": 0, "error": 0},
        "aggregate": {"total_tokens": 0, "total_tool_calls": 0},
    }

    _load_static()
    app = create_app(collector=collector, jwt_secret=_CONSOLE_JWT_SECRET)

    async def _mock_post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        assert node_response is not None
        return node_response

    mock_proxy = MagicMock(spec=httpx.AsyncClient)
    mock_proxy.post = MagicMock(side_effect=_mock_post)
    app.state.proxy_client = mock_proxy

    client = TestClient(app, raise_server_exceptions=False, headers=_console_headers())
    try:
        yield client
    finally:
        client.close()


def _create(client: Any) -> httpx.Response:
    return client.post("/v1/api/cluster/workstreams/new", json={"node_id": "node-a", "name": "x"})


class TestConsoleRequireProjectSurfacing:
    def test_require_project_400_surfaced(self) -> None:
        from turnstone.core.auth import REQUIRE_PROJECT_CODE, REQUIRE_PROJECT_ERROR

        node = _node_resp(400, {"error": REQUIRE_PROJECT_ERROR, "code": REQUIRE_PROJECT_CODE})
        with _console_client(node) as client:
            resp = _create(client)
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == REQUIRE_PROJECT_CODE
        assert data["error"] == REQUIRE_PROJECT_ERROR

    def test_uncoded_400_masked_no_leak(self) -> None:
        node = _node_resp(400, {"error": "cannot fork abc: SECRETPERSONA missing"})
        with _console_client(node) as client:
            resp = _create(client)
        assert resp.status_code == 502
        assert "SECRETPERSONA" not in resp.text
        assert resp.json()["error"] == "Dispatch to node node-a failed"

    def test_other_coded_400_masked(self) -> None:
        node = _node_resp(400, {"error": "too many files", "code": "too_many"})
        with _console_client(node) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_401_masked(self) -> None:
        with _console_client(_node_resp(401, {"error": "unauthorized"})) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_429_masked(self) -> None:
        with _console_client(_node_resp(429, {"error": "capacity"})) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_attach_denied_403_masked(self) -> None:
        node = _node_resp(
            403, {"error": "cannot attach a workstream to a private project you don't belong to"}
        )
        with _console_client(node) as client:
            resp = _create(client)
        assert resp.status_code == 502
        assert "private project" not in resp.text

    def test_500_masked(self) -> None:
        with _console_client(_node_resp(500, {"error": "boom"})) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_non_json_2xx_masked(self) -> None:
        # R7: a 2xx with no JSON body must mask to 502, not crash the console.
        with _console_client(_node_resp(200, content=b"<html>not json</html>")) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_network_error_masked(self) -> None:
        with _console_client(raise_exc=httpx.ConnectError("boom")) as client:
            resp = _create(client)
        assert resp.status_code == 502

    def test_success_200_regression(self) -> None:
        with _console_client(_node_resp(200, {"ws_id": "ws_new", "name": "x"})) as client:
            resp = _create(client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["correlation_id"] == "ws_new"
        assert data["target_node"] == "node-a"


# ---------------------------------------------------------------------------
# Node gate kind-scoping — drives the REAL make_create_handler for BOTH kinds.
# The handler emits `code: "require_project"` ONLY at the gate, so that marker's
# presence/absence in the response is an exact witness of whether the gate fired
# — closing the load-bearing `cfg.list_kind == "interactive"` guard end-to-end.
# ---------------------------------------------------------------------------


def _gate_request(body: dict[str, Any], cs: Any, auth: Any) -> Any:
    """A minimal Starlette Request: JSON body + auth_result on request.state +
    config_store on request.app.state — enough to reach the require_project gate."""
    from starlette.requests import Request

    payload = json.dumps(body).encode()
    delivered = {"done": False}

    async def _receive() -> dict[str, Any]:
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/api/workstreams/new",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(config_store=cs)),
        "state": {"auth_result": auth},
    }
    return Request(scope, _receive)


def _is_require_project_400(resp: Any) -> bool:
    if resp.status_code != 400:
        return False
    try:
        return json.loads(resp.body).get("code") == "require_project"
    except Exception:
        return False


def _run_gate(list_kind: Any, flag_on: bool, body: dict[str, Any], make_config_store: Any) -> Any:
    """Drive make_create_handler.create() to the require_project gate for one
    (kind, flag, body). A gate pass-through raises out of a build_kwargs stub that
    the handler's own try/except turns into a non-require_project response."""
    from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler

    def _build(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("stopped just past the require_project gate")

    mgr = MagicMock()
    mgr.kind = list_kind
    cfg = SessionEndpointConfig(
        permission_gate=lambda _req: None,
        manager_lookup=lambda _req: (mgr, None),
        tenant_check=None,
        not_found_label="workstream",
        audit_action_prefix="ws",
        list_kind=list_kind,
        # Both real mounts wire the gate on (interactive on the node,
        # coordinator on the console); production keeps it a declarative
        # field, not a kind check.
        create_gate_require_project=True,
        create_validate_request=None,
        create_build_kwargs=_build,
        create_supports_attachments=False,
        create_supports_user_id_override=False,
    )
    handler = make_create_handler(cfg)
    cs = make_config_store(**({"server.require_project": True} if flag_on else {}))
    auth = _Auth(scopes=("read", "write"), token_source="jwt")
    return asyncio.run(handler(_gate_request(body, cs, auth)))


class TestGateKindParity:
    """The gate is kind-INDEPENDENT: both real mounts wire it on
    (``create_gate_require_project=True``), so the (kind × flag × project)
    matrix must apply uniformly. These synthetic-cfg cases assert that
    parity — the exemption lives in ``require_project_denies_create``
    (token_source / service scope), never in the mount kind."""

    def test_interactive_projectless_gated(self, make_config_store: Any, tmp_db: Any) -> None:
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.INTERACTIVE, True, {}, make_config_store)
        assert _is_require_project_400(resp)

    def test_interactive_with_project_passes(self, make_config_store: Any, tmp_db: Any) -> None:
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.INTERACTIVE, True, {"project_id": "p1"}, make_config_store)
        assert not _is_require_project_400(resp)

    def test_interactive_flag_off_passes(self, make_config_store: Any, tmp_db: Any) -> None:
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.INTERACTIVE, False, {}, make_config_store)
        assert not _is_require_project_400(resp)

    def test_coordinator_projectless_gated(self, make_config_store: Any, tmp_db: Any) -> None:
        # The coordinator mount wires create_gate_require_project=True, so a
        # projectless coordinator create is refused the same as interactive.
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.COORDINATOR, True, {}, make_config_store)
        assert _is_require_project_400(resp)

    def test_coordinator_with_project_passes(self, make_config_store: Any, tmp_db: Any) -> None:
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.COORDINATOR, True, {"project_id": "p1"}, make_config_store)
        assert not _is_require_project_400(resp)

    def test_coordinator_flag_off_passes(self, make_config_store: Any, tmp_db: Any) -> None:
        from turnstone.core.workstream import WorkstreamKind

        resp = _run_gate(WorkstreamKind.COORDINATOR, False, {}, make_config_store)
        assert not _is_require_project_400(resp)


# ---------------------------------------------------------------------------
# Coordinator create mount wiring — the REAL console mount wires
# create_gate_require_project=True on coord_endpoint_config. The synthetic-cfg
# tests above can't catch a mis-wire on the actual mount, so drive the mounted
# console endpoint end-to-end (mirrors TestRequireProjectMountWiring in
# test_server_authz.py for the interactive node mount).
# ---------------------------------------------------------------------------


def _operator_headers() -> dict[str, str]:
    """An ``admin.coordinator`` operator WITHOUT the ``service`` scope.

    Service identities are exempt from the gate; the operator's own token
    must not be, so the gate tests would silently pass-through if this
    reused ``_console_headers()`` (which carries ``service``).
    """
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    tok = create_jwt(
        user_id="op",
        scopes=frozenset({"read", "write", "approve"}),
        source="test",
        secret=_CONSOLE_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
        permissions=frozenset({"admin.coordinator"}),
    )
    return {"Authorization": f"Bearer {tok}"}


@contextlib.contextmanager
def _console_coord_client(tmp_path: Any, cs: Any) -> Any:
    """A console TestClient with the real coord create mount live: a real
    ``SessionManager(CoordinatorAdapter)`` over SQLite, a fake model registry
    (passes the 503 gates), and the given config store."""
    from starlette.testclient import TestClient

    from tests._coord_test_helpers import _build_mgr, _fake_registry
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app
    from turnstone.core.storage._sqlite import SQLiteBackend

    _load_static()
    storage = SQLiteBackend(str(tmp_path / "coord-gate.db"))
    app = create_app(collector=MagicMock(spec=ClusterCollector), jwt_secret=_CONSOLE_JWT_SECRET)
    mgr = _build_mgr(storage)
    app.state.coord_mgr = mgr
    app.state.coord_adapter = mgr._adapter
    app.state.coord_registry = _fake_registry()
    app.state.coord_registry_error = ""
    app.state.config_store = cs
    app.state.auth_storage = storage
    client = TestClient(app, raise_server_exceptions=False, headers=_operator_headers())
    try:
        yield client
    finally:
        client.close()


class TestCoordMountWiring:
    def test_projectless_coord_create_gated_when_on(
        self, tmp_path: Any, make_config_store: Any
    ) -> None:
        cs = make_config_store(**{"server.require_project": True})
        with _console_coord_client(tmp_path, cs) as client:
            resp = client.post("/v1/api/workstreams/new", json={"name": "no-project"})
        assert resp.status_code == 400, resp.text
        assert resp.json().get("code") == "require_project"

    def test_coord_create_with_project_passes_when_on(
        self, tmp_path: Any, make_config_store: Any
    ) -> None:
        # A projected create must clear the gate; it 400s in the coord
        # validator instead (unknown project_id on the fresh DB) — asserting
        # NOT-the-coded-400 pins the gate without needing a seeded project.
        cs = make_config_store(**{"server.require_project": True})
        with _console_coord_client(tmp_path, cs) as client:
            resp = client.post(
                "/v1/api/workstreams/new", json={"name": "x", "project_id": "p-missing"}
            )
        assert resp.json().get("code") != "require_project", resp.text

    def test_projectless_coord_create_allowed_when_off(
        self, tmp_path: Any, make_config_store: Any
    ) -> None:
        with _console_coord_client(tmp_path, make_config_store()) as client:
            resp = client.post("/v1/api/workstreams/new", json={"name": "no-project"})
        body = resp.json()
        assert resp.status_code == 200, body
        assert body.get("ws_id")

    def test_non_string_project_id_cannot_bypass_gate(
        self, tmp_path: Any, make_config_store: Any
    ) -> None:
        # End-to-end regression: the create path coerces a non-string
        # project_id to absent (validator skips its attach check, build_kwargs
        # → None), so a truthy non-string must be refused by the gate rather
        # than minting a projectless coordinator with the policy on. Pins the
        # three wired sites (validator / gate / build_kwargs) agreeing.
        cs = make_config_store(**{"server.require_project": True})
        with _console_coord_client(tmp_path, cs) as client:
            resp = client.post("/v1/api/workstreams/new", json={"name": "x", "project_id": 123})
        assert resp.status_code == 400, resp.text
        assert resp.json().get("code") == "require_project"


# ---------------------------------------------------------------------------
# list_projects advisory field — the frontend composer reads data.require_project
# into requireProject(); pin that the endpoint actually emits it (and reflects
# the flag), so a refactor can't silently drop it and fail the picker open.
# ---------------------------------------------------------------------------


def _list_projects_request(cs: Any, auth: Any) -> Any:
    from starlette.requests import Request

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/api/projects",
        "headers": [],
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(config_store=cs)),
        "state": {"auth_result": auth},
    }
    return Request(scope, _receive)


class TestListProjectsAdvisory:
    def test_require_project_field_on(self, make_config_store: Any, tmp_db: Any) -> None:
        import turnstone.server as server_mod

        cs = make_config_store(**{"server.require_project": True})
        auth = _Auth(scopes=("service",))  # service scope bypasses require_permission
        resp = asyncio.run(server_mod.list_projects(_list_projects_request(cs, auth)))
        data = json.loads(resp.body)
        assert data["require_project"] is True
        assert "projects" in data

    def test_require_project_field_off_by_default(
        self, make_config_store: Any, tmp_db: Any
    ) -> None:
        import turnstone.server as server_mod

        cs = make_config_store()
        auth = _Auth(scopes=("service",))
        resp = asyncio.run(server_mod.list_projects(_list_projects_request(cs, auth)))
        assert json.loads(resp.body)["require_project"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
