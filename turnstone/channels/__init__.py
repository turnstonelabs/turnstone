"""Shared channel infrastructure for turnstone communication integrations.

Provides the :class:`ChannelAdapter` protocol, the :class:`ChannelEvent`
normalized event type, the :class:`ChannelRouter` for workstream mapping,
and shared formatting / configuration utilities.
"""

from turnstone.channels._protocol import ChannelAdapter, ChannelEvent
from turnstone.channels._routing import ChannelRouter

__all__ = [
    "ChannelAdapter",
    "ChannelEvent",
    "ChannelRouter",
]
