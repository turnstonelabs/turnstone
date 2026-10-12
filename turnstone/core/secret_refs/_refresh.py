"""A periodic refresh for hosts that hold values resolved from ``secret://`` references."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.core.secret_refs._resolver import get_resolver

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# The refresh reloads every model definition, so a short cache_ttl_seconds,
# chosen to reuse values for less time, must not turn it into a reload loop.
MIN_REFRESH_SECONDS = 60.0
# How long a cancelled refresh waits for a run already in flight.
_DRAIN_SECONDS = 30.0


def refresh_seconds() -> float | None:
    """Seconds between periodic refreshes, or ``None`` when ``[secrets]`` configures no store.

    The interval is ``cache_ttl_seconds``, at least :data:`MIN_REFRESH_SECONDS`.
    The hosts build the resolver at startup, so a malformed ``[secrets]`` table
    has stopped them before this runs.
    """
    resolver = get_resolver()
    if not resolver.backend_names:
        return None
    return max(resolver.cache_ttl_seconds, MIN_REFRESH_SECONDS)


def start_refresh(refresh: Callable[[], object], *, what: str) -> asyncio.Task[None] | None:
    """Start running *refresh* every :func:`refresh_seconds` on the running loop.

    Returns ``None`` when ``[secrets]`` configures no store: there is nothing
    to refresh.
    """
    interval = refresh_seconds()
    if interval is None:
        return None
    return asyncio.create_task(
        refresh_periodically(refresh, interval=interval, what=what),
        name=f"secret-refresh:{what}",
    )


async def refresh_periodically(
    refresh: Callable[[], object], *, interval: float, what: str
) -> None:
    """Run *refresh* in a worker thread about every *interval* seconds until cancelled.

    Each wait varies by up to a tenth either way, so hosts restarted together
    drift apart. A run that raises is logged and the loop carries on: the
    values already in use stay, as they do when an operator's reload fails.
    Cancelled while a run is in flight, it waits for that run (for up to
    :data:`_DRAIN_SECONDS`) before ending, so a host's shutdown does not race
    the reload.
    """
    while True:
        await asyncio.sleep(interval * random.uniform(0.9, 1.1))  # noqa: S311 - jitter
        run = asyncio.ensure_future(_run_logged(refresh, what))
        try:
            await asyncio.shield(run)
        except asyncio.CancelledError:
            await asyncio.wait({run}, timeout=_DRAIN_SECONDS)
            raise


async def _run_logged(refresh: Callable[[], object], what: str) -> None:
    try:
        await asyncio.to_thread(refresh)
    except Exception:
        log.warning("secret_refs.refresh_failed", what=what, exc_info=True)
