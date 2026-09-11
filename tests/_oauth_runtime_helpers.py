"""Explicit runtime construction for OAuth adapter tests."""

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import Any
from unittest.mock import Mock

import httpx

from turnstone.core.oauth.context import OAuthContext
from turnstone.core.oauth.runtime import OAuthRuntime

adapter_runner: contextvars.ContextVar[asyncio.Runner] = contextvars.ContextVar("adapter_runner")


def run_adapter[T](operation: Coroutine[Any, Any, T]) -> T:
    """Keep repeated synchronous test calls on one MCP owner loop per test."""
    return adapter_runner.get().run(operation)


def make_oauth_context(*, http_client: httpx.AsyncClient, **fields: Any) -> OAuthContext:
    """Run adapter operations on a real OAuth loop with a controlled transport.

    The suite's runtime fixture joins these threads at teardown, including
    runtimes started lazily by the production adapter entry points.
    """
    if isinstance(http_client, Mock):
        http_client.is_closed = False
    context = OAuthContext(**fields)
    context.runtime = OAuthRuntime(context, client_factory=lambda: http_client)
    context.runtime.start()
    return context
