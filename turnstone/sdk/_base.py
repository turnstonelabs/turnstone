"""Shared HTTP client base for the turnstone SDK."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, Self, TypeVar, overload

import httpx

from turnstone.sdk._types import TurnstoneAPIError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

T = TypeVar("T")


class _BaseClient:
    """Async HTTP client base shared by server and console clients."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the client.

        When *httpx_client* is provided it is used directly and *base_url*,
        *token*, and *timeout* are ignored — configure headers and base URL
        on the injected client instead.
        """
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if httpx_client is not None:
            self._client = httpx_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=base_url, timeout=timeout, headers=headers, follow_redirects=True
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
        resp = await self._client.request(method, path, json=json_body, params=params)
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

        async with aconnect_sse(self._client, "GET", path, params=params) as source:
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
