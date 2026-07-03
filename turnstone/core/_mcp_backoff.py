"""Interim shim: give the MCP SDK's streamable-HTTP reconnect real backoff.

The upstream SDK (``mcp.client.streamable_http``, pinned ``>=1.27,<2``) hard-codes
its GET-stream reconnect policy as two module-level constants with **no** public
configuration seam::

    DEFAULT_RECONNECTION_DELAY_MS = 1000   # flat 1s, no growth
    MAX_RECONNECTION_ATTEMPTS     = 2      # give up after two tries

Neither ``streamablehttp_client(...)`` nor ``ClientSession`` exposes a way to
tune these, and the delay is applied flat on every attempt — there is no
exponential backoff. In practice this means a server that is merely *restarting*
(a sub-second-to-few-second window) permanently loses its GET stream: the client
retries once ~1s later, hits the still-booting server (502 / connection refused),
exhausts its 2-attempt budget, and gives up silently until the whole worker
process is restarted. That is exactly the Keepalive-MCP failure we hit after a
routine service redeploy.

Because the constants are bare module globals referenced by name *inside* the
reconnect loop bodies, raising the count alone cannot introduce backoff — the
delay computation itself has to change. So this shim rebinds the two offending
coroutine methods on ``StreamableHTTPTransport`` with faithful copies whose only
behavioural change is:

  * a larger, capped attempt budget (:data:`MAX_RECONNECTION_ATTEMPTS`), and
  * capped exponential backoff with light jitter instead of a flat delay.

Everything else (last-event-id tracking, server-sent ``retry:`` honouring,
resumption-token plumbing, completion detection) is preserved verbatim.

This is deliberately a runtime patch, not a vendored fork: ``mcp`` is a ranged
pip dependency and we do not carry a ``patches/`` tree. :func:`install` is
idempotent and called once from :meth:`MCPClientManager.start`, before any
transport is constructed. Remove it when the SDK grows a real knob (track
upstream) — the call site is a single line.

NOTE (out of scope for this shim): reconnecting the GET stream restores
server-initiated notifications, but an ``oauth_user`` pool entry whose session
was already torn down still reconnects lazily on the next dispatch. Hardening
that path is a separate change.
"""

from __future__ import annotations

import random

import mcp.client.streamable_http as _sh
from mcp.types import JSONRPCRequest

from turnstone.core.log import get_logger

log = get_logger("turnstone.mcp")

# ── tunables ────────────────────────────────────────────────────────────────
# Total reconnect attempts during a single continuous outage before giving up.
# The attempt counter resets to 0 whenever a stream re-establishes cleanly, so
# this bounds a *sustained* outage, not the lifetime of the connection.
MAX_RECONNECTION_ATTEMPTS = 10          # SDK default: 2
# First-retry delay; each subsequent attempt doubles it up to the cap.
BASE_RECONNECTION_DELAY_MS = 1000       # SDK default: 1000 (but flat)
# Ceiling so a long outage settles into a steady slow poll rather than growing
# unbounded. 1s→2→4→8→16→30→30… ⇒ ~2.5 min of retrying across 10 attempts.
RECONNECTION_BACKOFF_CAP_MS = 30_000
# Multiplicative jitter (0 = none). Spreads reconnects so N servers / N workers
# that all dropped at the same instant (shared upstream restart) don't stampede.
RECONNECTION_JITTER_FRAC = 0.25

_installed = False


def _backoff_delay_ms(step: int, retry_interval_ms: int | None) -> int:
    """Delay before the next reconnect.

    ``step`` is the 0-based backoff exponent for this attempt. A server-sent
    SSE ``retry:`` value, when present, still wins (faithful to the SDK) — it is
    an explicit instruction from the peer — but is capped so a hostile/buggy
    value can't wedge the client.
    """
    if retry_interval_ms is not None:
        return min(retry_interval_ms, RECONNECTION_BACKOFF_CAP_MS)
    base = min(RECONNECTION_BACKOFF_CAP_MS, BASE_RECONNECTION_DELAY_MS * (2 ** step))
    if RECONNECTION_JITTER_FRAC:
        base = int(base * (1.0 + random.random() * RECONNECTION_JITTER_FRAC))
    return base


async def _handle_get_stream(self, client, read_stream_writer) -> None:
    """Patched ``StreamableHTTPTransport.handle_get_stream`` — see module docstring.

    Faithful copy of the SDK 1.27 method; the only changes are the attempt
    budget and the backoff delay computation (both flagged inline).
    """
    last_event_id: str | None = None
    retry_interval_ms: int | None = None
    attempt: int = 0

    while attempt < MAX_RECONNECTION_ATTEMPTS:  # CHANGED: was SDK MAX_RECONNECTION_ATTEMPTS
        try:
            if not self.session_id:
                return

            headers = self._prepare_headers()
            if last_event_id:
                headers[_sh.LAST_EVENT_ID] = last_event_id

            async with _sh.aconnect_sse(client, "GET", self.url, headers=headers) as event_source:
                event_source.response.raise_for_status()
                _sh.logger.debug("GET SSE connection established")

                async for sse in event_source.aiter_sse():
                    if sse.id:
                        last_event_id = sse.id
                    if sse.retry is not None:
                        retry_interval_ms = sse.retry
                    await self._handle_sse_event(sse, read_stream_writer)

                # Stream ended normally (server closed) - reset attempt counter
                attempt = 0

        except Exception as exc:
            _sh.logger.debug(f"GET stream error: {exc}")
            attempt += 1

        if attempt >= MAX_RECONNECTION_ATTEMPTS:
            _sh.logger.warning(
                "GET stream: giving up after %d reconnect attempts", MAX_RECONNECTION_ATTEMPTS
            )
            return

        # CHANGED: capped exponential backoff instead of a flat delay. In this
        # loop `attempt` is already post-increment on failure (1..N-1), so the
        # backoff exponent is attempt-1; a normal-close reset (attempt==0) waits
        # the base delay.
        delay_ms = _backoff_delay_ms(max(0, attempt - 1), retry_interval_ms)
        _sh.logger.info(f"GET stream disconnected, reconnecting in {delay_ms}ms...")
        await _sh.anyio.sleep(delay_ms / 1000.0)


async def _handle_reconnection(
    self, ctx, last_event_id: str, retry_interval_ms: int | None = None, attempt: int = 0
) -> None:
    """Patched ``StreamableHTTPTransport._handle_reconnection`` — see module docstring.

    Faithful copy of the SDK 1.27 method; the only changes are the attempt
    budget and the backoff delay computation (both flagged inline).
    """
    if attempt >= MAX_RECONNECTION_ATTEMPTS:  # CHANGED: was SDK MAX_RECONNECTION_ATTEMPTS
        _sh.logger.warning(
            "SSE resume: giving up after %d reconnect attempts", MAX_RECONNECTION_ATTEMPTS
        )
        return

    # CHANGED: this method waits BEFORE connecting with the current (pre-increment)
    # attempt, so the backoff exponent is `attempt` directly.
    delay_ms = _backoff_delay_ms(attempt, retry_interval_ms)
    await _sh.anyio.sleep(delay_ms / 1000.0)

    headers = self._prepare_headers()
    headers[_sh.LAST_EVENT_ID] = last_event_id

    original_request_id = None
    if isinstance(ctx.session_message.message.root, JSONRPCRequest):
        original_request_id = ctx.session_message.message.root.id

    try:
        async with _sh.aconnect_sse(ctx.client, "GET", self.url, headers=headers) as event_source:
            event_source.response.raise_for_status()
            _sh.logger.info("Reconnected to SSE stream")

            reconnect_last_event_id: str = last_event_id
            reconnect_retry_ms = retry_interval_ms

            async for sse in event_source.aiter_sse():
                if sse.id:
                    reconnect_last_event_id = sse.id
                if sse.retry is not None:
                    reconnect_retry_ms = sse.retry

                is_complete = await self._handle_sse_event(
                    sse,
                    ctx.read_stream_writer,
                    original_request_id,
                    ctx.metadata.on_resumption_token_update if ctx.metadata else None,
                )
                if is_complete:
                    await event_source.response.aclose()
                    return

            # Stream ended again without response - reconnect again (reset attempt counter)
            _sh.logger.info("SSE stream disconnected, reconnecting...")
            await self._handle_reconnection(ctx, reconnect_last_event_id, reconnect_retry_ms, 0)
    except Exception as e:
        _sh.logger.debug(f"Reconnection failed: {e}")
        await self._handle_reconnection(ctx, last_event_id, retry_interval_ms, attempt + 1)


def install() -> None:
    """Rebind the SDK reconnect methods with backoff-aware versions. Idempotent."""
    global _installed
    if _installed:
        return

    transport = _sh.StreamableHTTPTransport
    # Guard: if the SDK ever renames/removes these, fail loud at startup rather
    # than silently running the un-patched flat-retry policy.
    if not hasattr(transport, "handle_get_stream") or not hasattr(transport, "_handle_reconnection"):
        raise RuntimeError(
            "mcp SDK reconnect methods not found — the backoff shim is out of date; "
            "re-check turnstone/core/_mcp_backoff.py against the installed mcp version"
        )

    # Keep the SDK module constant in sync so any code that reads it (logs,
    # other callers) sees the effective value.
    _sh.MAX_RECONNECTION_ATTEMPTS = MAX_RECONNECTION_ATTEMPTS

    transport.handle_get_stream = _handle_get_stream
    transport._handle_reconnection = _handle_reconnection

    _installed = True
    log.info(
        "MCP reconnect backoff shim installed: max_attempts=%d base=%dms cap=%dms jitter=%.0f%%",
        MAX_RECONNECTION_ATTEMPTS,
        BASE_RECONNECTION_DELAY_MS,
        RECONNECTION_BACKOFF_CAP_MS,
        RECONNECTION_JITTER_FRAC * 100,
    )
