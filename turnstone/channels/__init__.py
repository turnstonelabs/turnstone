"""Shared channel infrastructure for turnstone communication integrations.

Provides the :class:`ChannelAdapter` protocol, the :class:`ChannelRouter`
for workstream mapping, and shared formatting / configuration utilities.
"""

from turnstone.channels._protocol import ChannelAdapter
from turnstone.channels._routing import ChannelRouter

__all__ = [
    "ChannelAdapter",
    "ChannelRouter",
]
