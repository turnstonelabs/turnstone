"""An IdP access token as an API credential: validation, user match, and what it may do."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import make_oidc_test_config
from turnstone.core import auth
from turnstone.core.auth import (
    OIDC_BEARER_TOKEN_SOURCE,
    OIDCBearerContext,
    _oidc_bearer_context,
    check_request,
)
from turnstone.core.oauth.context import oauth_context
from turnstone.core.oauth.oidc import (
    OIDCError,
    OIDCKeyNotFoundError,
    load_oidc_config,
    validate_api_access_token,
)

ISSUER = "https://idp.example.com"
CALLER = "monitoring-app"
SCOPE = "Workstreams.Run"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWKS = {
    "keys": [{**json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key())), "kid": "key1"}]
}


def _config(**overrides: Any) -> Any:
    return make_oidc_test_config(
        **{"api_allowed_clients": (CALLER,), "api_required_scope": SCOPE, **overrides}
    )


def _token(key: Any = _KEY, kid: str = "key1", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": "my-client",
        "sub": "pairwise-for-the-caller",
        "iat": now,
        "exp": now + 300,
        "scp": f"{SCOPE} other",
        "azp": CALLER,
        "oid": "oid-1",
        "tid": "tid-1",
        **overrides,
    }
    claims = {name: value for name, value in claims.items() if value is not None}
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _Storage:
    """The four lookups the bearer path makes."""

    def __init__(self, identities: list[dict[str, str]], permissions: set[str]) -> None:
        self.identities = identities
        self.permissions = permissions

    def list_oidc_identities_by_oid(self, issuer: str, oid: str, tid: str) -> list[dict[str, str]]:
        return [
            row
            for row in self.identities
            if (row["issuer"], row["oid"], row["tid"]) == (issuer, oid, tid)
        ]

    def get_oidc_identity(self, issuer: str, subject: str) -> dict[str, str] | None:
        for row in self.identities:
            if (row["issuer"], row["subject"]) == (issuer, subject):
                return row
        return None

    def get_user(self, user_id: str) -> dict[str, str] | None:
        return None if user_id == "deleted" else {"user_id": user_id}

    def get_user_permissions(self, user_id: str) -> set[str]:
        return set(self.permissions)


def _identity(user_id: str = "u1", subject: str = "pairwise-for-turnstone") -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "subject": subject,
        "user_id": user_id,
        "oid": "oid-1",
        "tid": "tid-1",
    }


def _check(
    token: str,
    storage: _Storage,
    *,
    method: str = "POST",
    path: str = "/v1/api/workstreams/new",
    config: Any = None,
    in_cookie: bool = False,
) -> tuple[bool, int, str, Any]:
    return check_request(
        method,
        path,
        None if in_cookie else f"Bearer {token}",
        f"turnstone_auth_console={token}" if in_cookie else None,
        jwt_secret="x" * 32,
        jwt_audience="turnstone-console",
        storage=storage,
        cookie_name="turnstone_auth_console",
        oidc_bearer=OIDCBearerContext(config or _config(), _JWKS),
    )


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def test_a_delegated_token_from_an_allowed_client_validates() -> None:
    claims = validate_api_access_token(_token(), _JWKS, _config())
    assert (claims["oid"], claims["tid"], claims["azp"]) == ("oid-1", "tid-1", CALLER)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # An ID token has this issuer, audience and signing key, and no scp.
        ({"scp": None, "nonce": "n"}, "lacks the 'Workstreams.Run' scope"),
        ({"scp": "Workstreams.Read"}, "lacks the 'Workstreams.Run' scope"),
        ({"scp": ["Workstreams.Run"]}, "lacks the 'Workstreams.Run' scope"),
        ({"azp": "some-other-app"}, "is not allowed"),
        ({"azp": None}, "is not allowed"),
        ({"aud": "another-api"}, "validation failed"),
        ({"iss": "https://evil.example.com"}, "validation failed"),
        ({"exp": int(time.time()) - 60}, "validation failed"),
        ({"sub": None}, "validation failed"),
        ({"idtyp": "app"}, "app-only"),
    ],
)
def test_a_token_that_is_not_ours_is_refused(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(OIDCError, match=message):
        validate_api_access_token(_token(**overrides), _JWKS, _config())


def test_a_token_signed_by_another_key_is_refused() -> None:
    with pytest.raises(OIDCError, match="validation failed"):
        validate_api_access_token(_token(key=_OTHER_KEY), _JWKS, _config())


def test_an_unknown_signing_key_is_reported_as_such() -> None:
    with pytest.raises(OIDCKeyNotFoundError):
        validate_api_access_token(_token(kid="rotated"), _JWKS, _config())


def test_the_client_id_claim_names_the_caller_when_there_is_no_azp() -> None:
    token = _token(azp=None, client_id=CALLER)
    assert validate_api_access_token(token, _JWKS, _config())["client_id"] == CALLER


def test_extra_audiences_replace_the_client_id() -> None:
    config = _config(api_audiences=("api://turnstone",))
    assert validate_api_access_token(_token(aud="api://turnstone"), _JWKS, config)
    with pytest.raises(OIDCError, match="validation failed"):
        validate_api_access_token(_token(), _JWKS, config)


@pytest.mark.parametrize(
    "overrides",
    [{"api_allowed_clients": ()}, {"api_required_scope": ""}, {"enabled": False}],
)
def test_nothing_validates_until_the_feature_is_fully_configured(overrides: dict[str, Any]) -> None:
    config = _config(**overrides)
    assert not config.api_bearer_enabled
    with pytest.raises(OIDCError, match="not enabled"):
        validate_api_access_token(_token(), _JWKS, config)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr("turnstone.core.config.load_config", lambda section: {})
    monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "my-client")
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "s3cret")
    for name in ("ALLOWED_CLIENTS", "REQUIRED_SCOPE", "AUDIENCE"):
        monkeypatch.delenv(f"TURNSTONE_OIDC_API_{name}", raising=False)
    return monkeypatch


def test_the_feature_is_off_by_default(oidc_env: pytest.MonkeyPatch) -> None:
    assert not load_oidc_config().api_bearer_enabled


def test_the_allow_list_and_scope_together_turn_it_on(oidc_env: pytest.MonkeyPatch) -> None:
    oidc_env.setenv("TURNSTONE_OIDC_API_ALLOWED_CLIENTS", "app-a, app-b,app-a")
    oidc_env.setenv("TURNSTONE_OIDC_API_REQUIRED_SCOPE", SCOPE)
    oidc_env.setenv("TURNSTONE_OIDC_API_AUDIENCE", "api://turnstone")
    config = load_oidc_config()
    assert config.api_bearer_enabled
    assert config.api_allowed_clients == ("app-a", "app-b")
    assert config.api_required_scope == SCOPE
    assert config.api_audiences == ("api://turnstone",)


@pytest.mark.parametrize(
    "env",
    [
        {"TURNSTONE_OIDC_API_ALLOWED_CLIENTS": "app-a"},
        {"TURNSTONE_OIDC_API_REQUIRED_SCOPE": SCOPE},
        {
            "TURNSTONE_OIDC_API_ALLOWED_CLIENTS": "app-a",
            "TURNSTONE_OIDC_API_REQUIRED_SCOPE": "two scopes",
        },
    ],
)
def test_half_a_configuration_leaves_it_off(
    oidc_env: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name, value in env.items():
        oidc_env.setenv(name, value)
    assert not load_oidc_config().api_bearer_enabled


def test_the_allow_list_reads_a_toml_list(oidc_env: pytest.MonkeyPatch) -> None:
    oidc_env.setattr(
        "turnstone.core.config.load_config",
        lambda section: {"api_allowed_clients": ["app-a"], "api_required_scope": SCOPE},
    )
    assert load_oidc_config().api_allowed_clients == ("app-a",)


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------


def test_the_user_is_matched_by_object_id_not_by_the_callers_subject() -> None:
    storage = _Storage([_identity()], {"read", "write", "approve", "admin.users", "persona.read"})
    allowed, status, _, result = _check(_token(), storage)
    assert (allowed, status) == (True, 200)
    assert result.user_id == "u1"
    assert result.token_source == OIDC_BEARER_TOKEN_SOURCE
    assert result.scopes == {"read", "write"}
    assert result.permissions == {"read", "write", "persona.read"}


def test_several_identities_of_one_user_still_match() -> None:
    storage = _Storage([_identity(subject="old-app"), _identity(subject="new-app")], {"write"})
    assert _check(_token(), storage)[3].user_id == "u1"


def test_a_token_without_an_object_id_matches_by_subject() -> None:
    storage = _Storage([_identity(subject="stable-sub")], {"write"})
    token = _token(oid=None, tid=None, sub="stable-sub")
    assert _check(token, storage)[3].user_id == "u1"


@pytest.mark.parametrize(
    "identities",
    [
        [],
        [_identity("u1", "a"), _identity("u2", "b")],
        [_identity("deleted")],
        [{**_identity(), "issuer": "https://another-idp.example.com"}],
        [{**_identity(), "tid": "another-tenant"}],
    ],
)
def test_no_single_existing_user_means_no_access(identities: list[dict[str, str]]) -> None:
    allowed, status, _, result = _check(_token(), _Storage(identities, {"write"}))
    assert (allowed, status, result) == (False, 401, None)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/api/admin/users"),
        ("POST", "/v1/api/workstreams/abc/approve"),
        ("POST", "/v1/api/workstreams/abc/trust"),
        ("POST", "/v1/api/_internal/config-reload"),
        ("GET", "/acme/directory-private"),
    ],
)
def test_an_admin_is_still_not_an_admin_through_this_door(method: str, path: str) -> None:
    storage = _Storage([_identity()], {"read", "write", "approve", "admin.users"})
    allowed, status, _, _ = _check(_token(), storage, method=method, path=path)
    assert (allowed, status) == (False, 403)


def test_the_users_own_roles_still_bound_the_caller() -> None:
    read_only = _Storage([_identity()], {"read"})
    assert _check(_token(), read_only)[:2] == (False, 403)
    assert _check(_token(), read_only, method="GET", path="/v1/api/workstreams")[:2] == (True, 200)

    no_roles = _Storage([_identity()], set())
    assert _check(_token(), no_roles, method="GET", path="/v1/api/workstreams")[:2] == (False, 403)


def test_an_administrator_keeps_write_and_loses_the_admin_permissions() -> None:
    result = _check(_token(), _Storage([_identity()], {"admin.users"}))[3]
    assert result.scopes == {"read", "write"}
    assert result.permissions == frozenset()


def test_the_token_is_never_read_from_the_cookie() -> None:
    storage = _Storage([_identity()], {"write"})
    assert _check(_token(), storage, in_cookie=True)[:2] == (False, 401)


def test_a_refused_token_is_a_plain_401() -> None:
    storage = _Storage([_identity()], {"write"})
    allowed, status, message, _ = _check(_token(azp="some-other-app"), storage)
    assert (allowed, status) == (False, 401)
    assert message == "Unauthorized: missing or invalid token"


def test_without_the_context_an_access_token_is_just_an_invalid_token() -> None:
    result = check_request(
        "GET",
        "/v1/api/workstreams",
        f"Bearer {_token()}",
        jwt_secret="x" * 32,
        storage=_Storage([_identity()], {"write"}),
        cookie_name="turnstone_auth_console",
    )
    assert result[:2] == (False, 401)


# ---------------------------------------------------------------------------
# Middleware context
# ---------------------------------------------------------------------------


def _request(config: Any, jwks: dict[str, Any] | None = _JWKS) -> Any:
    state = SimpleNamespace(jwks_data=jwks, auth_storage=None, oidc_http_client=None)
    oauth_context(state).oidc_config = config
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _context(request: Any, token: str) -> OIDCBearerContext | None:
    return asyncio.run(_oidc_bearer_context(request, f"Bearer {token}"))


def test_the_context_exists_only_for_an_asymmetric_jwt_when_enabled() -> None:
    assert _context(_request(_config()), _token()) == OIDCBearerContext(_config(), _JWKS)
    assert _context(_request(_config(api_allowed_clients=())), _token()) is None
    session_jwt = pyjwt.encode({"sub": "u1"}, "x" * 32, algorithm="HS256")
    assert _context(_request(_config()), session_jwt) is None
    assert _context(_request(_config()), "ts_" + "a" * 64) is None
    assert asyncio.run(_oidc_bearer_context(_request(_config()), None)) is None


def test_an_unknown_key_refreshes_the_key_set_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated = {"keys": [{**_JWKS["keys"][0], "kid": "rotated"}]}
    fetches: list[str] = []

    async def fetch_jwks(uri: str, client: Any = None) -> dict[str, Any]:
        fetches.append(uri)
        return rotated

    monkeypatch.setattr(auth, "fetch_jwks", fetch_jwks)
    request = _request(_config())

    context = _context(request, _token(kid="rotated"))
    assert context is not None and context.jwks_data == rotated
    assert request.app.state.jwks_data == rotated

    # A second unknown key inside the window does not reach the IdP again.
    context = _context(request, _token(kid="forged"))
    assert context is not None and context.jwks_data == rotated
    assert len(fetches) == 1


# ---------------------------------------------------------------------------
# Through the middleware
# ---------------------------------------------------------------------------


def test_a_request_through_the_middleware_runs_as_the_user() -> None:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def who(request: Any) -> JSONResponse:
        result = request.state.auth_result
        return JSONResponse({"user_id": result.user_id, "source": result.token_source})

    app = Starlette(
        routes=[
            Route("/v1/api/workstreams/new", who, methods=["POST"]),
            Route("/v1/api/admin/users", who),
        ],
        middleware=[
            Middleware(
                auth.AuthMiddleware,
                jwt_audience="turnstone-console",
                cookie_name="turnstone_auth_console",
            )
        ],
    )
    app.state.jwt_secret = "x" * 32
    app.state.auth_storage = _Storage([_identity()], {"read", "write", "admin.users"})
    app.state.jwks_data = _JWKS
    oauth_context(app.state).oidc_config = _config()

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token()}"}
    created = client.post("/v1/api/workstreams/new", headers=headers)
    assert created.status_code == 200
    assert created.json() == {"user_id": "u1", "source": OIDC_BEARER_TOKEN_SOURCE}
    assert client.get("/v1/api/admin/users", headers=headers).status_code == 403

    stranger = {"Authorization": f"Bearer {_token(oid='oid-unknown')}"}
    assert client.post("/v1/api/workstreams/new", headers=stranger).status_code == 401

    oauth_context(app.state).oidc_config = _config(api_allowed_clients=())
    assert client.post("/v1/api/workstreams/new", headers=headers).status_code == 401
