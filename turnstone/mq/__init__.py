"""Message queue integration for turnstone.

Provides a bridge service (turnstone-bridge) that connects message queues to the
turnstone-server HTTP API, and a client library for external systems to publish
commands and subscribe to progress.
"""

from turnstone.mq.broker import MessageBroker, RedisBroker
from turnstone.mq.client import TurnResult, TurnstoneClient

__all__ = [
    "AsyncRedisBroker",
    "MessageBroker",
    "RedisBroker",
    "TurnstoneClient",
    "TurnResult",
]


def __getattr__(name: str) -> object:
    if name == "AsyncRedisBroker":
        from turnstone.mq.async_broker import AsyncRedisBroker

        return AsyncRedisBroker
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
