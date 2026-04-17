"""Tests for console _proxy_auth_headers preserving the coordinator src claim.

Verifies C8 of the coordinator plan: when a console handler processes an
inbound request authenticated with a coordinator-minted JWT (``src ==
"coordinator"``), the upstream JWT the console mints for the proxied
request preserves that source plus the ``coord_ws_id`` custom claim.
For non-coordinator inbound tokens the re-mint still uses
``"console-proxy"`` as before — the existing behaviour is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt

from turnstone.console.server import _proxy_auth_headers
from turnstone.core.auth import JWT_AUD_SERVER, AuthResult

_SECRET = "x" * 64


def _build_request(auth_result: AuthResult | None):
    """Minimal Request-alike for _proxy_auth_headers."""
    state = SimpleNamespace(auth_result=auth_result)
    app_state = SimpleNamespace(jwt_secret=_SECRET, proxy_token_mgr=None)
    app = MagicMock()
    app.state = app_state
    req = MagicMock()
    req.state = state
    req.app = app
    return req


def _decode(headers: dict[str, str]) -> dict:
    token = headers["Authorization"].removeprefix("Bearer ")
    return pyjwt.decode(token, _SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER)


def test_console_proxy_uses_console_proxy_source_by_default():
    """Non-coordinator inbound tokens still mint src='console-proxy'."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"write"}),
        token_source="jwt",
        permissions=frozenset(),
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "console-proxy"
    assert "coord_ws_id" not in payload


def test_coordinator_source_is_preserved_on_remint():
    """Inbound src='coordinator' → outbound src='coordinator'."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"approve"}),
        token_source="coordinator",
        permissions=frozenset({"admin.coordinator"}),
        extra_claims={"coord_ws_id": "coord-42"},
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "coordinator"
    assert payload["coord_ws_id"] == "coord-42"


def test_coord_ws_id_absent_when_not_in_inbound_claims():
    """Defensive: if the inbound token is src=coordinator but missing the
    coord_ws_id claim (shouldn't happen in practice), the re-mint skips
    the custom claim rather than panicking."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"write"}),
        token_source="coordinator",
        permissions=frozenset(),
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "coordinator"
    assert "coord_ws_id" not in payload


def test_empty_auth_falls_back_to_service_token_or_empty():
    """Without auth_result.user_id, falls through to ServiceTokenManager."""
    auth = AuthResult(
        user_id="",
        scopes=frozenset(),
        token_source="config",
        permissions=frozenset(),
    )
    # No proxy_token_mgr configured → empty headers.
    headers = _proxy_auth_headers(_build_request(auth))
    assert headers == {}
