"""Shared HTTP client base for the turnstone SDK."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
from typing import TYPE_CHECKING, Any, Self, TypeVar, overload

import httpx2

from turnstone.core.sse_limits import MAX_SSE_EVENT_SIZE
from turnstone.sdk._types import TurnstoneAPIError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

T = TypeVar("T")


def _tls_context(
    ca_cert: str | None, client_cert: str | None, client_key: str | None
) -> ssl.SSLContext:
    """One TLS context holding the CAs to trust and the client certificate to present.

    httpx2 takes these only as a context: it deprecates a CA path and a ``cert`` pair, and refuses
    the two together. Without *ca_cert* the context trusts what httpx2 trusts by default.
    """
    if ca_cert and os.path.isdir(ca_cert):
        ctx = ssl.create_default_context(capath=ca_cert)
    elif ca_cert:
        ctx = ssl.create_default_context(cafile=ca_cert)
    else:
        ctx = httpx2.create_ssl_context()
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    return ctx


def _error_fields(resp: httpx2.Response) -> tuple[str, str]:
    """``(message, code)`` for a non-2xx response.

    The message is the JSON body's ``error`` (or ``detail``), else the first 200
    characters of the text; ``code`` is the body's string ``code``, else ``""``.
    """
    msg = ""
    code = ""
    with contextlib.suppress(Exception):
        body = resp.json()
        msg = body.get("error", body.get("detail", ""))
        raw_code = body.get("code", "")
        code = raw_code if isinstance(raw_code, str) else ""
    if not msg:
        msg = resp.text[:200]
    return msg, code


class _BaseClient:
    """Async HTTP client base shared by server and console clients."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
        httpx_client: httpx2.AsyncClient | None = None,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        """Initialise the client.

        When *httpx_client* (an ``httpx2.AsyncClient``) is provided it is
        used directly: *base_url*, *token*, *timeout* and the TLS arguments
        are ignored, so configure them on the injected client instead.
        *token_factory* still applies, adding each request's
        ``Authorization`` header.

        *token_factory* is called before each request to get the current
        auth token.  Use this with :class:`ServiceTokenManager` for
        auto-rotating JWTs::

            mgr = ServiceTokenManager(...)
            client = AsyncTurnstoneServer(token_factory=lambda: mgr.token)

        A static *token* string is set once at construction.  If both
        *token* and *token_factory* are provided, *token_factory* wins.

        For mTLS, pass *ca_cert* (a CA bundle file, or an OpenSSL CA directory whose entries are
        named by subject hash, as ``openssl rehash`` leaves them), *client_cert* and *client_key*
        (client certificate + key paths). They are loaded into one TLS context here, so a file that
        can't be read fails construction. OpenSSL reads a directory only when a handshake first
        needs one of its CAs, and the context keeps what it loaded, so a directory that was never
        hashed fails every request and a later change to it may not reach this client.
        """
        self._token_factory = token_factory
        headers: dict[str, str] = {}
        if token and not token_factory:
            headers["Authorization"] = f"Bearer {token}"
        if httpx_client is not None:
            # Refused here rather than at the first request: a client from another library (the
            # httpx library, which Turnstone used before 1.9) would otherwise fail later,
            # mid-request or mid-stream, with errors that don't name the cause.
            if not isinstance(httpx_client, httpx2.AsyncClient):
                kind = type(httpx_client)
                raise TypeError(
                    "httpx_client must be an httpx2.AsyncClient, not "
                    f"{kind.__module__}.{kind.__qualname__}"
                )
            self._client = httpx_client
            self._owns_client = False
        else:
            if (client_cert or client_key) and not (client_cert and client_key):
                raise ValueError("Both client_cert and client_key must be provided for mTLS")
            verify: ssl.SSLContext | bool = True
            if ca_cert or client_cert:
                verify = _tls_context(ca_cert, client_cert, client_key)
            self._client = httpx2.AsyncClient(
                base_url=base_url,
                timeout=timeout,
                headers=headers,
                follow_redirects=True,
                verify=verify,
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
        files: list[tuple[str, tuple[str, bytes, str]]] | None = ...,
        data: dict[str, Any] | None = ...,
        timeout: httpx2.Timeout | None = ...,
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
        files: list[tuple[str, tuple[str, bytes, str]]] | None = ...,
        data: dict[str, Any] | None = ...,
        timeout: httpx2.Timeout | None = ...,
        response_model: None = ...,
    ) -> dict[str, Any]: ...

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        data: dict[str, Any] | None = None,
        timeout: httpx2.Timeout | None = None,
        response_model: type[Any] | None = None,
    ) -> Any:
        """Execute an HTTP request and return parsed response data.

        When *files* is provided, the request is sent as
        ``multipart/form-data`` with the named file parts (and any
        *data* fields as plain form fields).  Mutually exclusive with
        *json_body*.  *timeout* replaces the client's for this request;
        None keeps the client's.  Raises :class:`TurnstoneAPIError` on
        non-2xx responses.
        """
        limits = httpx2.USE_CLIENT_DEFAULT if timeout is None else timeout
        headers: dict[str, str] | None = None
        if self._token_factory is not None:
            headers = {"Authorization": f"Bearer {self._token_factory()}"}
        if files is not None:
            # httpx2 infers multipart from the files= kwarg and sets the
            # Content-Type + boundary itself; do not pass json= alongside.
            resp = await self._client.request(
                method,
                path,
                files=files,
                data=data,
                params=params,
                headers=headers,
                timeout=limits,
            )
        else:
            resp = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=headers,
                timeout=limits,
            )
        if resp.status_code >= 400:
            # Try to extract error message from JSON body
            msg, code = _error_fields(resp)
            raise TurnstoneAPIError(resp.status_code, msg, code)
        body_data: dict[str, Any] = resp.json()
        if response_model is not None:
            return response_model.model_validate(body_data)
        return body_data

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> bytes:
        """Execute a request and return the raw response bytes.

        Used for endpoints like the attachment ``/content`` route that
        return arbitrary binary or text payloads with their own
        ``Content-Type``.  Raises :class:`TurnstoneAPIError` on non-2xx.
        """
        headers: dict[str, str] | None = None
        if self._token_factory is not None:
            headers = {"Authorization": f"Bearer {self._token_factory()}"}
        resp = await self._client.request(method, path, params=params, headers=headers)
        if resp.status_code >= 400:
            msg, code = _error_fields(resp)
            raise TurnstoneAPIError(resp.status_code, msg, code)
        return resp.content

    async def _stream_sse(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open an SSE stream and yield parsed JSON dicts."""
        sse_kwargs: dict[str, Any] = {}
        if params:
            sse_kwargs["params"] = params
        if self._token_factory is not None:
            sse_kwargs["headers"] = {"Authorization": f"Bearer {self._token_factory()}"}
        async with self._client.sse(
            path, max_event_size=MAX_SSE_EVENT_SIZE, **sse_kwargs
        ) as source:
            async for sse in source:
                if sse.data:
                    with contextlib.suppress(ValueError, RecursionError):
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
