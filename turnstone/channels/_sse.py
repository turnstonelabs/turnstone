"""Shared SSE listener loop for channel adapters.

Both the Discord and Slack adapters subscribe to per-workstream SSE event
streams with identical reconnect / 404-stale-route / backoff behaviour.
:func:`run_sse_stream` extracts that loop so each adapter supplies only
its platform-specific ``on_event`` and ``on_stale`` callbacks.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import httpx_sse

from turnstone.channels._config import SSE_MAX_RECONNECT_DELAY, SSE_RECONNECT_DELAY
from turnstone.core.log import get_logger
from turnstone.sdk.events import ServerEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)


async def run_sse_stream(
    *,
    http_client: httpx.AsyncClient,
    log_prefix: str,
    ws_id: str,
    node_url_fn: Callable[[str], Awaitable[str]],
    token_factory: Callable[[], str] | None,
    on_event: Callable[[ServerEvent], Awaitable[None]],
    on_stale: Callable[[], Awaitable[None]],
) -> None:
    """Run an SSE subscription loop with reconnect/backoff for one workstream.

    Parameters
    ----------
    http_client:
        Shared ``httpx.AsyncClient`` for all SSE connections.
    log_prefix:
        Platform tag used in log events (e.g. ``"discord"`` / ``"slack"``).
    ws_id:
        Workstream identifier, passed as a query parameter.
    node_url_fn:
        Async callable returning the base server URL for *ws_id* on each
        connection attempt (so reconnects pick up router cache refreshes).
    token_factory:
        Optional callable returning an ``Authorization: Bearer ...`` token
        per connection (supports auto-rotating service JWTs).
    on_event:
        Async callback invoked once per parsed :class:`ServerEvent`.
        Exceptions are logged and do not kill the stream.
    on_stale:
        Async callback invoked when the server returns 404 for *ws_id*,
        indicating the workstream was evicted/closed. After ``on_stale``
        returns, the loop exits (does not reconnect).
    """
    delay = SSE_RECONNECT_DELAY
    url = ""

    while True:
        try:
            node_base = await node_url_fn(ws_id)
            url = f"{node_base}/v1/api/events"

            sse_headers: dict[str, str] | None = None
            if token_factory is not None:
                sse_headers = {"Authorization": f"Bearer {token_factory()}"}

            async with httpx_sse.aconnect_sse(
                http_client,
                "GET",
                url,
                params={"ws_id": ws_id},
                headers=sse_headers,
            ) as event_source:
                status = event_source.response.status_code
                if status == 404:
                    log.info(f"{log_prefix}.sse_ws_gone", ws_id=ws_id)
                    # The 404-stops-reconnect invariant belongs to this loop,
                    # not to the caller — if on_stale raises we still exit.
                    try:
                        await on_stale()
                    except Exception:
                        log.warning(
                            f"{log_prefix}.sse_on_stale_failed",
                            ws_id=ws_id,
                            exc_info=True,
                        )
                    return

                if status >= 400:
                    log.warning(
                        f"{log_prefix}.sse_upstream_error",
                        ws_id=ws_id,
                        status=status,
                    )
                    raise httpx.HTTPStatusError(
                        f"SSE upstream {status}",
                        request=event_source.response.request,
                        response=event_source.response,
                    )

                delay = SSE_RECONNECT_DELAY  # reset on successful connect
                async for sse in event_source.aiter_sse():
                    if sse.event != "message" and sse.event:
                        continue
                    try:
                        data = json.loads(sse.data)
                    except json.JSONDecodeError:
                        log.debug(
                            f"{log_prefix}.sse_invalid_json",
                            ws_id=ws_id,
                            data=sse.data[:200],
                        )
                        continue

                    event = ServerEvent.from_dict(data)
                    try:
                        await on_event(event)
                    except Exception:
                        log.warning(
                            f"{log_prefix}.event_dispatch_failed",
                            ws_id=ws_id,
                            exc_info=True,
                        )

        except httpx.HTTPStatusError as exc:
            # Already logged at WARNING inside the try block (the raise
            # was our own — status was captured there).  Caught here to
            # fall through to backoff + retry.
            log.debug(
                f"{log_prefix}.sse_http_status_error",
                ws_id=ws_id,
                error=str(exc),
            )
        except httpx.RemoteProtocolError:
            log.debug(f"{log_prefix}.sse_remote_closed", ws_id=ws_id)
        except asyncio.CancelledError:
            return
        except httpx.ReadTimeout:
            log.info(f"{log_prefix}.sse_read_timeout", ws_id=ws_id)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            log.warning(
                f"{log_prefix}.sse_connect_failed",
                ws_id=ws_id,
                url=url,
                error=str(exc),
            )
        except Exception:
            log.warning(f"{log_prefix}.sse_error", ws_id=ws_id, exc_info=True)

        await asyncio.sleep(delay)
        delay = min(delay * 2, SSE_MAX_RECONNECT_DELAY)
