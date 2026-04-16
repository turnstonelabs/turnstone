from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackRoute:
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
