"""Base configuration shared by all channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


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
