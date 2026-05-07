"""Tests for :func:`turnstone.core.mcp_oauth.revoke_token_at_as`.

The helper is best-effort RFC 7009 token revocation. It must:
- skip cleanly when the AS metadata doesn't advertise a revocation endpoint
- POST the form body when one is present (with optional client_secret)
- never raise on non-2xx, network errors, or timeouts — caller doesn't
  want try/except in cleanup paths
- never use ``exc_info=True`` — chained ``__context__`` may carry an
  ``httpx.Request`` whose ``Authorization`` header holds a bearer; the
  bearer-leak invariant requires structured fields with type names only
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from turnstone.core.mcp_oauth import (
    ASMetadata,
    MCPOAuthDiscoveryError,
    _attempt_upstream_revoke,
    revoke_token_at_as,
)


def _make_as_metadata(
    *,
    revocation_endpoint: str | None = "https://as.example.com/revoke",
) -> ASMetadata:
    return ASMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint=None,
        revocation_endpoint=revocation_endpoint,
        jwks_uri=None,
        code_challenge_methods_supported=("S256",),
        token_endpoint_auth_methods_supported=("client_secret_basic",),
    )


def _mk_response(status_code: int) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    return resp


class TestRevocationUnsupported:
    def test_revoke_token_skipped_when_revocation_endpoint_none(self) -> None:
        as_meta = _make_as_metadata(revocation_endpoint=None)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )

        client.post.assert_not_called()
        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_unsupported" in info_events


class TestRevocationSuccess:
    def test_revoke_token_succeeds_on_200(self) -> None:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(200))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret="s-secret",
                )
            )

        # POST shape — URL + form body keys.
        client.post.assert_awaited_once()
        call_args = client.post.call_args
        assert call_args.args[0] == "https://as.example.com/revoke"
        body = call_args.kwargs["data"]
        assert body == {
            "token": "r-secret",
            "token_type_hint": "refresh_token",
            "client_id": "client-1",
            "client_secret": "s-secret",
        }
        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_succeeded" in info_events

    def test_revoke_token_omits_client_secret_when_none(self) -> None:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(200))

        asyncio.run(
            revoke_token_at_as(
                as_metadata=as_meta,
                http_client=client,
                refresh_token="r-secret",
                client_id="client-1",
                client_secret=None,
            )
        )

        body = client.post.call_args.kwargs["data"]
        assert "client_secret" not in body
        assert body["token"] == "r-secret"
        assert body["token_type_hint"] == "refresh_token"
        assert body["client_id"] == "client-1"

    def test_revoke_token_succeeds_on_204(self) -> None:
        # RFC 7009 says the AS MAY return any 2xx; treat the whole range
        # as success.
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(204))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_succeeded" in info_events


class TestRevocationFailureLogged:
    def _run_and_capture(self, status: int) -> list[Any]:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mk_response(status))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )
        return mock_log.info.call_args_list

    def test_revoke_token_logs_on_400_does_not_raise(self) -> None:
        calls = self._run_and_capture(400)
        events = [c.args[0] for c in calls]
        assert "mcp_server.oauth.revocation_failed" in events
        # Must include status field.
        failed_call = next(c for c in calls if c.args[0] == "mcp_server.oauth.revocation_failed")
        assert failed_call.kwargs.get("status") == 400

    def test_revoke_token_logs_on_401_does_not_raise(self) -> None:
        calls = self._run_and_capture(401)
        events = [c.args[0] for c in calls]
        assert "mcp_server.oauth.revocation_failed" in events
        failed_call = next(c for c in calls if c.args[0] == "mcp_server.oauth.revocation_failed")
        assert failed_call.kwargs.get("status") == 401

    def test_revoke_token_logs_on_403_does_not_raise(self) -> None:
        calls = self._run_and_capture(403)
        events = [c.args[0] for c in calls]
        assert "mcp_server.oauth.revocation_failed" in events

    def test_revoke_token_logs_on_5xx_does_not_raise(self) -> None:
        calls = self._run_and_capture(500)
        events = [c.args[0] for c in calls]
        assert "mcp_server.oauth.revocation_failed" in events
        failed_call = next(c for c in calls if c.args[0] == "mcp_server.oauth.revocation_failed")
        assert failed_call.kwargs.get("status") == 500


class TestRevocationExceptionPaths:
    def test_revoke_token_handles_network_error(self) -> None:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )

        events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_failed" in events
        failed_call = next(
            c
            for c in mock_log.info.call_args_list
            if c.args[0] == "mcp_server.oauth.revocation_failed"
        )
        assert failed_call.kwargs.get("error") == "ConnectError"

    def test_revoke_token_handles_httpx_timeout(self) -> None:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )

        events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_failed" in events
        failed_call = next(
            c
            for c in mock_log.info.call_args_list
            if c.args[0] == "mcp_server.oauth.revocation_failed"
        )
        assert failed_call.kwargs.get("error") == "TimeoutException"

    def test_revoke_token_handles_asyncio_timeout(self) -> None:
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)

        async def _slow(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(10.0)
            raise AssertionError("should have timed out")

        client.post = AsyncMock(side_effect=_slow)

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                    timeout_seconds=0.05,
                )
            )

        events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "mcp_server.oauth.revocation_failed" in events
        failed_call = next(
            c
            for c in mock_log.info.call_args_list
            if c.args[0] == "mcp_server.oauth.revocation_failed"
        )
        # ``asyncio.timeout`` raises ``TimeoutError`` (Python's builtin)
        # on cancellation.
        assert failed_call.kwargs.get("error") == "TimeoutError"

    def test_revoke_token_no_exc_info_in_logs(self) -> None:
        """Bearer-leak invariant: the revoke path must NEVER set
        ``exc_info=True``. Chained ``__context__`` may include an
        ``httpx.Request`` whose ``Authorization`` header holds a
        bearer; the traceback formatter would render it.
        """
        as_meta = _make_as_metadata()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch("turnstone.core.mcp_oauth.log") as mock_log:
            asyncio.run(
                revoke_token_at_as(
                    as_metadata=as_meta,
                    http_client=client,
                    refresh_token="r-secret",
                    client_id="client-1",
                    client_secret=None,
                )
            )

        # No info call may carry exc_info.
        for call in mock_log.info.call_args_list:
            assert "exc_info" not in call.kwargs, (
                f"mcp_server.oauth log info({call.args[0]!r}) used exc_info — "
                "this violates the bearer-leak invariant"
            )
        # Defensively: also check warning + exception levels for the
        # same call site.
        for call in mock_log.warning.call_args_list:
            assert "exc_info" not in call.kwargs
        mock_log.exception.assert_not_called()


class TestAttemptUpstreamRevokeNeverRaises:
    """Round-2 q-3 regression: ``_attempt_upstream_revoke``'s docstring
    claims ``Never raises``. Background-task semantics make this load-
    bearing — a propagated exception logs ``Task exception was never
    retrieved`` because the ``set.discard`` done-callback doesn't read
    ``task.exception()``.

    The wrapper's narrow inner ``except`` clauses (``MCPOAuthDiscoveryError``,
    ``MCPTokenDecryptError``) leave room for any other exception type
    raised by ``discover_authorization_server`` /
    ``storage.get_mcp_oauth_client_secret_ct`` / ``token_store.cipher.decrypt``
    to escape. The outer ``try/except Exception`` is what keeps the
    contract honest. These tests pin that gate.
    """

    def _build_args(self) -> dict[str, Any]:
        token_store = MagicMock()
        token_store.cipher = MagicMock()
        token_store.cipher.decrypt.return_value = b"shh"
        storage = MagicMock()
        storage.get_mcp_oauth_client_secret_ct.return_value = None
        return {
            "http_client": MagicMock(spec=httpx.AsyncClient),
            "metadata_cache": None,
            "storage": storage,
            "token_store": token_store,
            "server_name": "srv-oauth",
            "server_row": {
                "url": "https://mcp.example.com",
                "oauth_client_id": "client-1",
                "oauth_authorization_server_url": None,
                "oauth_as_issuer_cached": None,
            },
            "server_id_for_audit": "srv-id-1",
            "refresh_token": "r-secret",
        }

    def test_attempt_upstream_revoke_swallows_unexpected_exception(self) -> None:
        """A generic exception from a path the inner handlers don't
        cover MUST be caught at the outer boundary and logged with type
        name only (no exc_info=True per the bearer-leak invariant).
        """
        args = self._build_args()

        async def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("network blew up")

        with (
            patch("turnstone.core.mcp_oauth.discover_authorization_server", side_effect=_boom),
            patch("turnstone.core.mcp_oauth.log") as mock_log,
        ):
            # MUST NOT raise.
            asyncio.run(_attempt_upstream_revoke(**args))

        events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "mcp_server.oauth.upstream_revoke_failed" in events, (
            "outer try/except must log mcp_server.oauth.upstream_revoke_failed "
            "with the exception type name when an unexpected exception escapes "
            "the narrow inner handlers"
        )
        for call in mock_log.info.call_args_list:
            assert "exc_info" not in call.kwargs, (
                "outer-block log must not use exc_info=True — chained "
                "__context__ may carry an httpx.Request bearer"
            )

    def test_attempt_upstream_revoke_logs_discovery_failure(self) -> None:
        """Round-2 bug-1: ``MCPOAuthDiscoveryError`` MUST emit
        ``upstream_revoke_discovery_failed`` so operators have visibility
        into a silent-discovery-failure path that previously logged
        nothing while the audit row recorded ``upstream_revoke_outcome=scheduled``.
        """
        args = self._build_args()

        async def _disc_fail(*_a: Any, **_kw: Any) -> Any:
            raise MCPOAuthDiscoveryError("PRM fetch 503")

        with (
            patch(
                "turnstone.core.mcp_oauth.discover_authorization_server",
                side_effect=_disc_fail,
            ),
            patch("turnstone.core.mcp_oauth.log") as mock_log,
        ):
            asyncio.run(_attempt_upstream_revoke(**args))

        events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "mcp_server.oauth.upstream_revoke_discovery_failed" in events
        assert "mcp_server.oauth.upstream_revoke_failed" not in events
