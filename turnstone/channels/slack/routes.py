"""Slack channel-routing key dataclass.

A :class:`SlackRoute` is the triple that uniquely identifies where a Slack
conversation lives: ``(channel, user_id?, thread_ts?)``.  It round-trips
through a ``channel:user_id:thread_ts`` string so it can be used as the
opaque ``channel_id`` value stored in ``channel_routes``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackRoute:
    """Routing key for a Slack conversation (channel / DM / thread).

    ``to_channel_id`` and ``parse`` are inverses only for values the
    parser is willing to emit:

    - ``SlackRoute("C1")`` ↔ ``"C1"``
    - ``SlackRoute("C1", "U1")`` ↔ ``"C1:U1"``
    - ``SlackRoute("C1", "U1", "ts")`` ↔ ``"C1:U1:ts"``

    Lax cases (not produced by ``to_channel_id`` but accepted by
    ``parse``):

    - Trailing colons drop to ``None`` — ``"C1:"`` → ``SlackRoute("C1")``.
    - Extra colons fold into ``thread_ts`` via ``split(":", 2)``, so
      ``"C1:U1:ts:extra"`` → ``thread_ts="ts:extra"``.  No Slack ts or
      channel/user ID contains ``:`` so this is safe in practice.
    """

    channel: str
    user_id: str | None = None
    thread_ts: str | None = None

    @classmethod
    def parse(cls, channel_id: str) -> SlackRoute:
        parts = channel_id.split(":", 2)
        channel = parts[0] if parts else ""
        user_id = parts[1] if len(parts) >= 2 and parts[1] else None
        thread_ts = parts[2] if len(parts) == 3 and parts[2] else None
        return cls(channel=channel, user_id=user_id, thread_ts=thread_ts)

    def to_channel_id(self) -> str:
        if self.thread_ts:
            return f"{self.channel}:{self.user_id or ''}:{self.thread_ts}"
        if self.user_id:
            return f"{self.channel}:{self.user_id}"
        return self.channel

    @property
    def has_user(self) -> bool:
        return bool(self.user_id)

    @property
    def has_thread(self) -> bool:
        return bool(self.thread_ts)
