"""Session mint and renewal boundaries with real storage, middleware, and proxies."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_interactive_history_rbac import _SECRET
from tests.test_interactive_history_rbac import history_apps as history_apps
from turnstone.core.auth import (
    JWT_AUD_CONSOLE,
    JWT_AUD_SERVER,
    create_jwt,
    generate_token,
    hash_token,
)

pytestmark = pytest.mark.anyio


def _surface(apps, name):
    if name == "node":
        return apps.node, "/v1/api", JWT_AUD_SERVER
    return apps.console, ("/node/missing" if name == "proxy" else "") + "/v1/api", JWT_AUD_CONSOLE


async def _login(client, prefix, user="viewer"):
    response = await client.post(
        prefix + "/auth/login", json={"username": user, "password": "history-test-password"}
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("surface", ["node", "console", "proxy"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_empty_roles_cannot_mint_through_login_refresh_or_jwt_exchange(
    history_apps, surface, revoke
):
    apps = history_apps
    client, prefix, _ = _surface(apps, surface)
    original = await _login(client, prefix)
    assert original.json()["can_refresh"] is True
    if revoke:
        apps.storage.set_role_overrides("builtin-viewer", set(), {"read"})
    else:
        apps.storage.unassign_role("viewer", "builtin-viewer")
    for endpoint, body, status in (
        ("login", {"username": "viewer", "password": "history-test-password"}, 403),
        ("refresh", {}, 403),
        ("login", {"token": original.json()["jwt"]}, 401),
    ):
        response = await client.post(prefix + "/auth/" + endpoint, json=body)
        assert response.status_code == status, response.text
        assert "jwt" not in response.json()
        assert "set-cookie" not in response.headers
    # Existing JWT claims retain their remaining lifetime; refusal does not mint or revoke one.
    assert (await client.get(prefix + "/auth/whoami")).status_code == 200


@pytest.mark.parametrize("surface", ["node", "console", "proxy"])
async def test_role_store_outage_refuses_mint_and_preserves_existing_cookie(
    history_apps, surface, monkeypatch
):
    client, prefix, _ = _surface(history_apps, surface)
    await _login(client, prefix)
    cookies = dict(client.cookies)

    def unavailable(_user):
        raise RuntimeError("private database diagnostic")

    monkeypatch.setattr(history_apps.storage, "get_user_permissions", unavailable)
    for endpoint, body in (
        ("login", {"username": "viewer", "password": "history-test-password"}),
        ("refresh", {}),
    ):
        response = await client.post(prefix + "/auth/" + endpoint, json=body)
        assert response.status_code == 503
        assert "private" not in response.text
        assert "set-cookie" not in response.headers
        assert "jwt" not in response.json()
    assert dict(client.cookies) == cookies
    assert (await client.get(prefix + "/auth/whoami")).status_code == 200


@pytest.mark.parametrize("surface", ["node", "console", "proxy"])
@pytest.mark.parametrize(
    ("source", "service", "eligible"),
    [
        ("password", False, True),
        ("oidc", False, True),
        ("password", True, False),
        ("oidc", True, False),
        ("database", False, False),
        ("console-proxy", False, False),
        ("coordinator", False, False),
        ("console", True, False),
        ("unknown", False, False),
        ("", False, False),
        ("tls-acme-enrollment", True, False),
    ],
)
async def test_renewal_source_matrix_and_whoami_agree(
    history_apps, surface, source, service, eligible
):
    client, prefix, audience = _surface(history_apps, surface)
    token = create_jwt(
        user_id="viewer",
        scopes=frozenset({"service" if service else "read"}),
        source=source,
        secret=_SECRET,
        audience=audience,
        permissions=frozenset({"read"}),
    )
    headers = {"Authorization": "Bearer " + token}
    whoami = await client.get(prefix + "/auth/whoami", headers=headers)
    if source == "tls-acme-enrollment":
        assert whoami.status_code == 403
    else:
        assert whoami.status_code == 200
        assert whoami.json()["can_refresh"] is eligible
    refresh = await client.post(prefix + "/auth/refresh", headers=headers)
    assert refresh.status_code == (200 if eligible else 403), refresh.text
    if eligible:
        assert refresh.json()["can_refresh"] is True
        assert refresh.json()["scopes"] == "read"
    else:
        assert "set-cookie" not in refresh.headers
    exchange = await client.post(prefix + "/auth/login", json={"token": token})
    assert exchange.status_code == 401
    assert "set-cookie" not in exchange.headers


@pytest.mark.parametrize("surface", ["node", "console", "proxy"])
@pytest.mark.parametrize("roleless", [False, True])
async def test_raw_api_tokens_keep_explicit_scope_and_fixed_session_lifetime(
    history_apps, surface, roleless
):
    client, prefix, _ = _surface(history_apps, surface)
    storage = history_apps.storage
    if roleless:
        storage.unassign_role("admin", "builtin-admin")
    raw = generate_token()
    storage.create_api_token("narrow", hash_token(raw), raw[:8], "admin", "Narrow", "read")
    response = await client.post(prefix + "/auth/login", json={"token": raw})
    assert response.status_code == 200
    assert response.json()["scopes"] == "read"
    assert response.json()["can_refresh"] is False
    derived = response.json()["jwt"]
    for token in (raw, derived):
        headers = {"Authorization": "Bearer " + token}
        assert (await client.get(prefix + "/auth/whoami", headers=headers)).json()[
            "can_refresh"
        ] is False
        refused = await client.post(prefix + "/auth/refresh", headers=headers)
        assert refused.status_code == 403
        assert "set-cookie" not in refused.headers
    storage.delete_api_token("narrow")
    assert (await client.post(prefix + "/auth/login", json={"token": raw})).status_code == 401
    assert (await client.get(prefix + "/auth/whoami")).json()["scopes"] == "read"
    expired = generate_token()
    storage.create_api_token(
        "expired",
        hash_token(expired),
        expired[:8],
        "admin",
        "Expired",
        "read",
        expires=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert (await client.post(prefix + "/auth/login", json={"token": expired})).status_code == 401


@pytest.mark.parametrize(
    ("permissions", "scopes"),
    [("project.read", "read"), ("write", "read,write"), ("approve", "approve,read,write")],
)
async def test_nonempty_permission_floor_and_scope_hierarchy_are_preserved(
    history_apps, permissions, scopes
):
    storage = history_apps.storage
    storage.unassign_role("viewer", "builtin-viewer")
    storage.create_role("custom", "custom", "Custom", permissions, False)
    storage.assign_role("viewer", "custom")
    login = await _login(history_apps.node, "/v1/api")
    assert login.json()["scopes"] == scopes
    refresh = await history_apps.node.post("/v1/api/auth/refresh")
    assert refresh.status_code == 200
    assert refresh.json()["scopes"] == scopes
