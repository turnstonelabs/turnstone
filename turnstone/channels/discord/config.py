"""Discord-specific configuration."""

from __future__ import annotations

from dataclasses import dataclass, field

from turnstone.channels._config import ChannelConfig


@dataclass
class DiscordConfig(ChannelConfig):
    """Configuration for the Discord channel adapter.

    Extends :class:`ChannelConfig` with Discord-specific settings such as
    the bot token, guild restriction, and streaming parameters.
    """

    bot_token: str = ""
    guild_id: int = 0  # 0 = all guilds
    allowed_channels: list[int] = field(default_factory=list)  # empty = all
    thread_auto_archive: int = 1440  # minutes (24h)
    max_message_length: int = 2000
    streaming_edit_interval: float = 1.5  # seconds between message edits
