"""Base configuration shared by all channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelConfig:
    """Base configuration shared by all channel adapters.

    Individual adapters extend this with platform-specific fields (tokens,
    guild IDs, etc.).
    """

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    prefix: str = "turnstone"
    model: str = ""
    auto_approve: bool = False
    auto_approve_tools: list[str] = field(default_factory=list)
    template: str = ""
