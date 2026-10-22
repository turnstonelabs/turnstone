"""Admin token creation, ``POST /v1/api/admin/users/{user_id}/tokens``.

A body the endpoint can't use answers 400 and mints nothing: one that isn't JSON, one nested too
deeply to parse, one that isn't an object, or one whose fields have the wrong type. A body that
couldn't be parsed used to mint a token with the default scopes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._deep_json import too_deep_to_parse
from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

_SECRET = "test-jwt-secret-minimum-32-chars!"
ADMIN = "a" * 32
USER = "b" * 32
PATH = f"/v1/api/admin/users/{USER}/tokens"
_BAD_DAYS = "expires_days must be a whole number of days, at least 1"


def _admin_headers() -> dict[str, str]:
    token = create_jwt(
        ADMIN,
        frozenset({"read", "write", "approve"}),
        "password",
        _SECRET,
        audience=JWT_AUD_CONSOLE,
        permissions=frozenset({"admin.users"}),
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def client(db) -> Any:
    from starlette.testclient import TestClient

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    _load_static()
    db.create_user(ADMIN, "ada", "Ada Admin", "x")
    db.create_user(USER, "jin", "Jin Park", "x")
    app = create_app(
        collector=MagicMock(spec=ClusterCollector), jwt_secret=_SECRET, auth_storage=db
    )
    # Without the app's startup and shutdown, which this route doesn't need.
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    test_client.close()


def test_creates_a_token(client, db) -> None:
    resp = client.post(
        PATH,
        content=b'{"name": "ci", "scopes": "read", "expires_days": 30}',
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    [token] = db.list_api_tokens(USER)
    assert (token["name"], token["scopes"]) == ("ci", "read")
    assert token["expires"]


def test_an_empty_object_takes_the_defaults(client, db) -> None:
    resp = client.post(PATH, content=b"{}", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    [token] = db.list_api_tokens(USER)
    assert (token["name"], token["scopes"]) == ("", "read,write,approve")
    assert not token.get("expires")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"", "Invalid JSON body"),
        (b"{not json", "Invalid JSON body"),
        (b"[]", "Request body must be a JSON object"),
        (b'{"scopes": ["read"]}', "name and scopes must be strings"),
        (b'{"name": 7}', "name and scopes must be strings"),
        (b'{"expires_days": "soon"}', _BAD_DAYS),
        (b'{"expires_days": [30]}', _BAD_DAYS),
        (b'{"expires_days": 1e300}', _BAD_DAYS),
        (b'{"expires_days": 10000000000}', _BAD_DAYS),
        (b'{"expires_days": false}', _BAD_DAYS),
        (b'{"expires_days": 0}', _BAD_DAYS),
        (b'{"expires_days": "30"}', _BAD_DAYS),
        (b'{"expires_days": 1.5}', _BAD_DAYS),
    ],
    ids=[
        "empty",
        "not-json",
        "not-an-object",
        "scopes-list",
        "name-number",
        "days-word",
        "days-list",
        "days-float-overflow",
        "days-int-overflow",
        "days-false",
        "days-zero",
        "days-string",
        "days-fraction",
    ],
)
def test_a_body_it_cannot_use_mints_nothing(client, db, body: bytes, error: str) -> None:
    resp = client.post(PATH, content=body, headers=_admin_headers())
    assert (resp.status_code, resp.json()) == (400, {"error": error})
    assert db.list_api_tokens(USER) == []


def test_a_body_nested_too_deeply_mints_nothing(client, db, monkeypatch) -> None:
    seed = '{"name": "ci"}'
    too_deep_to_parse(monkeypatch, seed)
    resp = client.post(PATH, content=seed, headers=_admin_headers())
    assert (resp.status_code, resp.json()) == (400, {"error": "Invalid JSON body"})
    assert db.list_api_tokens(USER) == []
