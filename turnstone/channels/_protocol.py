"""Channel adapter protocol.

Defines the :class:`ChannelAdapter` structural protocol that bidirectional
channel adapters (Discord, Slack, etc.) must satisfy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChannelAdapter(Protocol):
    """Protocol for bidirectional channel adapters."""

    channel_type: str

    async def start(self) -> None:
        """Connect to the platform and begin listening for events."""
        ...

    async def stop(self) -> None:
        """Disconnect and release resources."""
        ...

    async def send(self, channel_id: str, content: str) -> str:
        """Send a message to a channel. Returns the platform message ID."""
        ...

    async def send_notification(self, channel_id: str, content: str, ws_id: str) -> str:
        """Send a notification and track the reply mapping. Returns message ID.

        Like :meth:`send` but associates the outgoing message with *ws_id*
        so that replies can be routed back to the originating workstream.
        """
        ...
