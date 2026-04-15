"""Slack-specific configuration."""

from __future__ import annotations

from dataclasses import dataclass, field

from turnstone.channels._config import ChannelConfig


@dataclass
class SlackConfig(ChannelConfig):
    """Configuration for the Slack channel adapter.

    Uses Socket Mode so no public URL or API Gateway is required —
    Slack connects outbound to the instance via a WebSocket.

    Requires two tokens:
    - bot_token (xoxb-...): for posting messages via the Web API
    - app_token (xapp-...): for Socket Mode WebSocket connection

    To create these:
    1. Go to https://api.slack.com/apps and create a new app
    2. Enable Socket Mode under Settings > Socket Mode — this generates the app_token
    3. Under OAuth & Permissions add bot scopes:
       chat:write, chat:write.public, channels:history, im:history,
       groups:history, mpim:history, reactions:write
    4. Under Event Subscriptions (via Socket Mode) subscribe to:
       message.channels, message.im, message.groups
    5. Install the app to your workspace to get the bot_token
    """

    bot_token: str = ""           # xoxb-...  (Bot User OAuth Token)
    app_token: str = ""           # xapp-...  (App-Level Token for Socket Mode)
    allowed_channels: list[str] = field(default_factory=list)  # empty = all
    max_message_length: int = 3000
    streaming_edit_interval: float = 1.5  # seconds between message edits
    slash_command: str = "/turnstone"
