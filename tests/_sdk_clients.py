"""Assertions for the SDK clients ``create_client`` builds."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx2

from turnstone.core.providers._http_client import _ShutdownBackend


def anthropic_module_mock() -> MagicMock:
    """A stand-in for the ``anthropic`` module behind ``_ensure_anthropic``.

    ``Anthropic`` records how it was constructed; ``DefaultHttpxClient`` is the
    SDK's real one, since ``create_client`` builds Turnstone's HTTP client from it.
    """
    module = MagicMock()
    module.Anthropic = MagicMock()
    module.DefaultHttpxClient = anthropic.DefaultHttpxClient
    return module


def assert_built_with(sdk_client_cls: MagicMock, **expected: Any) -> None:
    """The SDK client was constructed once, with *expected* plus Turnstone's HTTP client.

    The HTTP client is the SDK's default one wrapped so that closing it wakes a
    read blocked in another thread (see ``turnstone.core.providers._http_client``).
    """
    sdk_client_cls.assert_called_once()
    assert sdk_client_cls.call_args.args == ()
    kwargs = dict(sdk_client_cls.call_args.kwargs)
    http_client = kwargs.pop("http_client")
    assert kwargs == expected
    assert isinstance(http_client, httpx2.Client)
    transports = [http_client._transport, *http_client._mounts.values()]
    assert all(
        isinstance(transport._pool._network_backend, _ShutdownBackend)
        for transport in transports
        if isinstance(transport, httpx2.HTTPTransport)
    )
    http_client.close()
