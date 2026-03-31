"""Shared HTTP client base for the turnstone SDK."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, Self, TypeVar, overload

import httpx

from turnstone.sdk._types import TurnstoneAPIError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

T = TypeVar("T")


class _BaseClient:
    """Async HTTP client base shared by server and console clients."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
        httpx_client: httpx.AsyncClient | None = None,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        """Initialise the client.

        When *httpx_client* is provided it is used directly and all other
        params are ignored — configure headers, base URL, and TLS on the
        injected client instead.

        *token_factory* is called before each request to get the current
        auth token.  Use this with :class:`ServiceTokenManager` for
        auto-rotating JWTs::

            mgr = ServiceTokenManager(...)
            client = AsyncTurnstoneServer(token_factory=lambda: mgr.token)

        A static *token* string is set once at construction.  If both
        *token* and *token_factory* are provided, *token_factory* wins.

        For mTLS, pass *ca_cert* (CA bundle path), *client_cert* and
        *client_key* (client certificate + key paths).
        """
        self._token_factory = token_factory
        headers: dict[str, str] = {}
        if token and not token_factory:
            headers["Authorization"] = f"Bearer {token}"
        if httpx_client is not None:
            self._client = httpx_client
            self._owns_client = False
        else:
            tls_kwargs: dict[str, Any] = {}
            if ca_cert:
                tls_kwargs["verify"] = ca_cert
            if client_cert or client_key:
                if not (client_cert and client_key):
                    raise ValueError("Both client_cert and client_key must be provided for mTLS")
                tls_kwargs["cert"] = (client_cert, client_key)
            self._client = httpx.AsyncClient(
                base_url=base_url,
                timeout=timeout,
                headers=headers,
                follow_redirects=True,
                **tls_kwargs,
            )
            self._owns_client = True

    # -- request helpers -----------------------------------------------------

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = ...,
        params: dict[str, Any] | None = ...,
        response_model: type[T],
    ) -> T: ...

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = ...,
        params: dict[str, Any] | None = ...,
        response_model: None = ...,
    ) -> dict[str, Any]: ...

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        response_model: type[Any] | None = None,
    ) -> Any:
        """Execute an HTTP request and return parsed response data.

        Raises :class:`TurnstoneAPIError` on non-2xx responses.
        """
        headers: dict[str, str] | None = None
        if self._token_factory is not None:
            headers = {"Authorization": f"Bearer {self._token_factory()}"}
        resp = await self._client.request(
            method,
            path,
            json=json_body,
            params=params,
            headers=headers,
        )
        if resp.status_code >= 400:
            # Try to extract error message from JSON body
            msg = ""
            with contextlib.suppress(Exception):
                body = resp.json()
                msg = body.get("error", body.get("detail", ""))
            if not msg:
                msg = resp.text[:200]
            raise TurnstoneAPIError(resp.status_code, msg)
        data: dict[str, Any] = resp.json()
        if response_model is not None:
            return response_model.model_validate(data)
        return data

    async def _stream_sse(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open an SSE stream and yield parsed JSON dicts."""
        from httpx_sse import aconnect_sse

        sse_kwargs: dict[str, Any] = {}
        if params:
            sse_kwargs["params"] = params
        if self._token_factory is not None:
            sse_kwargs["headers"] = {"Authorization": f"Bearer {self._token_factory()}"}
        async with aconnect_sse(self._client, "GET", path, **sse_kwargs) as source:
            async for sse in source.aiter_sse():
                if sse.data:
                    with contextlib.suppress(json.JSONDecodeError):
                        yield json.loads(sse.data)

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client (if owned)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
