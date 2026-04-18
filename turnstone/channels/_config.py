"""Base configuration shared by all channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field

# Shared adapter constants.
SSE_RECONNECT_DELAY: float = 2.0
SSE_MAX_RECONNECT_DELAY: float = 30.0
MAX_NOTIFY_TRACKING: int = 100
CREATE_LOCK_CAP: int = 1024  # LRU bound on ChannelRouter per-channel creation locks


@dataclass
class ChannelConfig:
    """Base configuration shared by all channel adapters.

    Individual adapters extend this with platform-specific fields (tokens,
    guild IDs, etc.).
    """

    server_url: str = "http://localhost:8080"
    model: str = ""
    auto_approve: bool = False
    auto_approve_tools: list[str] = field(default_factory=list)
    skill: str = ""
