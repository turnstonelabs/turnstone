"""Client library for interacting with turnstone through a message broker.

Usage::

    from turnstone.mq.client import TurnstoneClient

    client = TurnstoneClient()
    result = client.send_and_wait(
        "What files are in the current directory?",
        auto_approve=True,
    )
    print(result.content)
    client.close()
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turnstone.mq.broker import MessageBroker, RedisBroker
from turnstone.mq.protocol import (
    ApproveMessage,
    CloseWorkstreamMessage,
    CommandMessage,
    ContentEvent,
    CreateWorkstreamMessage,
    ErrorEvent,
    HealthMessage,
    ListWorkstreamsMessage,
    OutboundEvent,
    PlanFeedbackMessage,
    ReasoningEvent,
    SendMessage,
    ToolResultEvent,
    TurnCompleteEvent,
    WorkstreamCreatedEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class TurnResult:
    """Aggregated result of a send_and_wait call."""

    correlation_id: str = ""
    ws_id: str = ""
    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.errors


class TurnstoneClient:
    """Client library for turnstone message queue integration.

    All methods are synchronous.  The broker handles background threads
    for pub/sub subscriptions.
    """

    def __init__(
        self,
        broker: MessageBroker | None = None,
        prefix: str = "turnstone",
        **redis_kwargs: object,
    ) -> None:
        """Create a client.

        Pass ``broker`` for a custom broker, or provide Redis kwargs
        (``host``, ``port``, ``db``, ``password``) to use the default
        RedisBroker.
        """
        self._broker: MessageBroker = broker or RedisBroker(**redis_kwargs)  # type: ignore[arg-type]
        self._prefix = prefix

    # -- fire-and-forget commands -------------------------------------------

    def send(
        self,
        message: str,
        ws_id: str = "",
        name: str = "",
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        target_node: str = "",
    ) -> str:
        """Send a message.  Returns correlation_id for tracking.

        If *target_node* is set, the message is pushed to that node's
        dedicated queue.  If *ws_id* is set and *target_node* is not,
        the client looks up the workstream's owning node and routes
        accordingly.
        """
        msg = SendMessage(
            message=message,
            ws_id=ws_id,
            name=name,
            auto_approve=auto_approve,
            auto_approve_tools=auto_approve_tools or [],
            target_node=target_node,
        )
        node = target_node or (self._broker.get_ws_owner(ws_id) if ws_id else "") or ""
        self._broker.push_inbound(msg.to_json(), node_id=node)
        return msg.correlation_id

    def create_workstream(
        self,
        name: str = "",
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        target_node: str = "",
        initial_message: str = "",
    ) -> str:
        """Create a workstream.  Returns correlation_id."""
        msg = CreateWorkstreamMessage(
            name=name,
            auto_approve=auto_approve,
            auto_approve_tools=auto_approve_tools or [],
            target_node=target_node,
            initial_message=initial_message,
        )
        self._broker.push_inbound(msg.to_json(), node_id=target_node)
        return msg.correlation_id

    def close_workstream(self, ws_id: str) -> str:
        """Close a workstream.  Returns correlation_id."""
        msg = CloseWorkstreamMessage(ws_id=ws_id)
        self._broker.push_inbound(msg.to_json())
        return msg.correlation_id

    def command(self, ws_id: str, command: str) -> str:
        """Execute a slash command.  Returns correlation_id."""
        msg = CommandMessage(ws_id=ws_id, command=command)
        self._broker.push_inbound(msg.to_json())
        return msg.correlation_id

    def list_workstreams(self) -> str:
        """Request workstream list.  Returns correlation_id."""
        msg = ListWorkstreamsMessage()
        self._broker.push_inbound(msg.to_json())
        return msg.correlation_id

    def health(self) -> str:
        """Request health status.  Returns correlation_id."""
        msg = HealthMessage()
        self._broker.push_inbound(msg.to_json())
        return msg.correlation_id

    def list_nodes(self) -> list[dict[str, Any]]:
        """List active bridge nodes (reads directly from broker)."""
        return self._broker.list_nodes()

    # -- approval / plan response -------------------------------------------

    def approve(
        self,
        request_id: str,
        ws_id: str = "",
        approved: bool = True,
        feedback: str | None = None,
        always: bool = False,
    ) -> None:
        """Respond to a tool approval request."""
        msg = ApproveMessage(
            ws_id=ws_id,
            request_id=request_id,
            approved=approved,
            feedback=feedback,
            always=always,
        )
        self._broker.push_response(request_id, msg.to_json())

    def plan_feedback(
        self,
        request_id: str,
        ws_id: str = "",
        feedback: str = "",
    ) -> None:
        """Respond to a plan review request."""
        msg = PlanFeedbackMessage(
            ws_id=ws_id,
            request_id=request_id,
            feedback=feedback,
        )
        self._broker.push_response(request_id, msg.to_json())

    # -- blocking send -------------------------------------------------------

    def send_and_wait(
        self,
        message: str,
        ws_id: str = "",
        name: str = "",
        auto_approve: bool = True,
        auto_approve_tools: list[str] | None = None,
        target_node: str = "",
        timeout: float = 600,
        on_event: Callable[[OutboundEvent], None] | None = None,
    ) -> TurnResult:
        """Send a message and block until the turn completes.

        Returns a TurnResult with aggregated content, tool results, etc.
        """
        # Build the message but don't send yet — subscribe first to avoid
        # a race where the bridge processes the message before we subscribe.
        msg = SendMessage(
            message=message,
            ws_id=ws_id,
            name=name,
            auto_approve=auto_approve,
            auto_approve_tools=auto_approve_tools or [],
            target_node=target_node,
        )
        cid = msg.correlation_id

        result = TurnResult(correlation_id=cid, ws_id=ws_id)
        done = threading.Event()
        actual_ws_id = ws_id

        def _on_global(raw: str) -> None:
            nonlocal actual_ws_id
            event = OutboundEvent.from_json(raw)
            if on_event:
                on_event(event)

            if isinstance(event, WorkstreamCreatedEvent) and event.correlation_id == cid:
                actual_ws_id = event.ws_id
                result.ws_id = event.ws_id
                self._broker.subscribe_outbound(f"{self._prefix}:events:{actual_ws_id}", _on_ws)

        def _on_ws(raw: str) -> None:
            event = OutboundEvent.from_json(raw)
            if on_event:
                on_event(event)

            if isinstance(event, ContentEvent):
                result.content_parts.append(event.text)
            elif isinstance(event, ReasoningEvent):
                result.reasoning_parts.append(event.text)
            elif isinstance(event, ToolResultEvent):
                result.tool_results.append((event.name, event.output))
            elif isinstance(event, ErrorEvent):
                result.errors.append(event.message)
            elif isinstance(event, TurnCompleteEvent) and event.correlation_id == cid:
                done.set()

        # Subscribe BEFORE pushing — ensures we don't miss early events
        self._broker.subscribe_outbound(f"{self._prefix}:events:global", _on_global)
        if actual_ws_id:
            self._broker.subscribe_outbound(f"{self._prefix}:events:{actual_ws_id}", _on_ws)

        # Now push the message (route to target node or ws owner if known)
        node = target_node or (self._broker.get_ws_owner(ws_id) if ws_id else "") or ""
        self._broker.push_inbound(msg.to_json(), node_id=node)

        done.wait(timeout=timeout)

        # Cleanup
        self._broker.unsubscribe_outbound(f"{self._prefix}:events:global")
        if actual_ws_id:
            self._broker.unsubscribe_outbound(f"{self._prefix}:events:{actual_ws_id}")

        result.ws_id = actual_ws_id
        result.timed_out = not done.is_set()
        return result

    # -- subscription --------------------------------------------------------

    def subscribe(
        self,
        callback: Callable[[OutboundEvent], None],
        ws_id: str = "",
    ) -> None:
        """Subscribe to events for a specific workstream or global events."""
        channel = f"{self._prefix}:events:{ws_id}" if ws_id else f"{self._prefix}:events:global"

        def _cb(raw: str) -> None:
            event = OutboundEvent.from_json(raw)
            callback(event)

        self._broker.subscribe_outbound(channel, _cb)

    def unsubscribe(self, ws_id: str = "") -> None:
        """Unsubscribe from a workstream or global channel."""
        channel = f"{self._prefix}:events:{ws_id}" if ws_id else f"{self._prefix}:events:global"
        self._broker.unsubscribe_outbound(channel)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Clean up broker connection."""
        self._broker.close()

    def __enter__(self) -> TurnstoneClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
