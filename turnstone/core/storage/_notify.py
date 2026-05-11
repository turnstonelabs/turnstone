"""Cross-process notification primitive shared by all storage backends.

Provides a uniform ``notify`` / ``listen`` shape over PostgreSQL's
``LISTEN`` / ``NOTIFY`` and a SQLite synthetic-sweep fallback.

Consumers subscribe to one or more channels, drain a
:class:`NotifyStream` via :meth:`NotifyStream.poll`, and reconcile by
re-reading the relevant rows on every wake-up. Payloads are signal-only
(<= 8 KiB on Postgres) — full event content is delivered by SSE or
in-process callbacks elsewhere; this primitive is the "go re-read these
rows" wake-up channel, nothing more.

The PostgreSQL implementation requires a session-mode connection
(``pgbouncer`` in transaction mode is incompatible with LISTEN).  See
the ``listen`` docs on each backend for the deployment-config detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Notify:
    """One notification draining out of a :class:`NotifyStream`."""

    channel: str
    payload: str
    pid: int


class NotifyConnectionError(Exception):
    """Raised when a :class:`NotifyStream`'s underlying connection drops.

    Consumers handle this by closing the stream, reconciling against the
    relevant table (re-reading whatever rows the channel describes), and
    reopening with a fresh :meth:`StorageBackend.listen` call.
    """


class NotifyStream(Protocol):
    """Bounded-blocking pull interface for cross-process notifications.

    Returned by :meth:`StorageBackend.listen` as a context manager; the
    consumer drains via :meth:`poll` in a loop, typically with a short
    timeout so the loop can also observe a shutdown flag.
    """

    def poll(self, timeout: float) -> list[Notify]:
        """Wait up to ``timeout`` seconds for notifications.

        Returns the list of notifications received during the wait
        (possibly empty on timeout).  Raises :class:`NotifyConnectionError`
        if the underlying connection was dropped — the caller reconciles
        and re-listens.
        """
        ...

    def close(self) -> None:
        """Stop the stream; subsequent :meth:`poll` calls return ``[]``."""
        ...
