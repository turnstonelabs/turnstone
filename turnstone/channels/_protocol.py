"""Channel adapter protocol and normalized event type.

Defines the :class:`ChannelEvent` data class for inbound events and the
:class:`ChannelAdapter` structural protocol that all bidirectional channel
adapters (Discord, Slack, etc.) must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ChannelEvent:
    """Normalized inbound event from any channel."""

    channel_type: str  # "discord", "slack"
    channel_id: str  # thread/channel ID
    channel_user_id: str  # platform user ID
    message: str
    parent_channel_id: str = ""  # main channel (for thread creation)
    metadata: dict[str, Any] = field(default_factory=dict)


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

    async def edit_message(self, channel_id: str, message_id: str, content: str) -> None:
        """Edit an existing message in a channel."""
        ...

    async def send_approval_request(
        self,
        channel_id: str,
        ws_id: str,
        correlation_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Send an interactive tool-approval prompt to a channel."""
        ...

    async def send_plan_review(
        self,
        channel_id: str,
        ws_id: str,
        correlation_id: str,
        content: str,
    ) -> None:
        """Send a plan-review prompt to a channel."""
        ...

    async def create_thread(self, parent_channel_id: str, name: str, message_id: str = "") -> str:
        """Create a thread under a parent channel. Returns the new thread ID."""
        ...
