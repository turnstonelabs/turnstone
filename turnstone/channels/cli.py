"""Unified channel gateway entry point.

Launches one or more channel adapters (Discord, Slack, etc.) connected to
the turnstone cluster via Redis MQ.  Currently supports Discord; future
adapters will be added as additional ``--*-token`` flags.

Run as: ``turnstone-channel --discord-token $TURNSTONE_DISCORD_TOKEN``
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Parse arguments, initialize storage and broker, and run adapters."""
    import argparse

    parser = argparse.ArgumentParser(
        description="turnstone channel gateway — bridges messaging platforms to the turnstone cluster"
    )

    # -- Redis ---------------------------------------------------------------
    parser.add_argument(
        "--redis-host",
        default=os.environ.get("REDIS_HOST", "localhost"),
        help="Redis host (default: $REDIS_HOST or localhost)",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(os.environ.get("REDIS_PORT", "6379")),
        help="Redis port (default: %(default)s)",
    )
    parser.add_argument(
        "--redis-password",
        default=os.environ.get("REDIS_PASSWORD"),
        help="Redis password (default: $REDIS_PASSWORD)",
    )
    parser.add_argument(
        "--redis-db",
        type=int,
        default=0,
        help="Redis DB number (default: %(default)s)",
    )

    # -- Discord -------------------------------------------------------------
    parser.add_argument(
        "--discord-token",
        default=os.environ.get("TURNSTONE_DISCORD_TOKEN", ""),
        help="Discord bot token (default: $TURNSTONE_DISCORD_TOKEN)",
    )
    parser.add_argument(
        "--discord-guild",
        type=int,
        default=0,
        help="Restrict to a single Discord guild (0 = all, default: %(default)s)",
    )
    parser.add_argument(
        "--discord-channels",
        default="",
        help="Comma-separated list of allowed Discord channel IDs (default: all)",
    )

    # -- Workstream defaults -------------------------------------------------
    parser.add_argument(
        "--model",
        default="",
        help="Default model for new workstreams (default: server default)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Auto-approve all tool calls",
    )

    # -- Logging -------------------------------------------------------------
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: %(default)s)",
    )
    parser.add_argument(
        "--log-format",
        default="auto",
        choices=["auto", "json", "text"],
        help="Log output format (default: auto -- JSON when stderr is not a TTY)",
    )

    args = parser.parse_args()

    # -- Logging setup -------------------------------------------------------
    from turnstone.core.log import configure_logging

    configure_logging(
        level=args.log_level,
        json_output={"json": True, "text": False}.get(args.log_format),
        service="channel",
    )

    from turnstone.core.log import get_logger

    log = get_logger(__name__)

    # -- Storage -------------------------------------------------------------
    from turnstone.core.storage._registry import init_storage

    db_backend = os.environ.get("TURNSTONE_DB_BACKEND", "sqlite")
    db_url = os.environ.get("TURNSTONE_DB_URL", "")
    db_path = os.environ.get("TURNSTONE_DB_PATH", "")

    init_storage(
        backend=db_backend,
        url=db_url,
        path=db_path,
    )

    # -- Broker --------------------------------------------------------------
    from turnstone.mq.async_broker import AsyncRedisBroker

    broker = AsyncRedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
    )

    # -- Adapter selection ---------------------------------------------------
    adapters_configured = False

    if args.discord_token:
        adapters_configured = True

    if not adapters_configured:
        print(
            "Error: no channel adapters configured. "
            "Set --discord-token or $TURNSTONE_DISCORD_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Run -----------------------------------------------------------------
    if args.discord_token:
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.channels.discord.config import DiscordConfig
        from turnstone.core.storage._registry import get_storage

        allowed_channels: list[int] = []
        if args.discord_channels:
            allowed_channels = [
                int(c.strip()) for c in args.discord_channels.split(",") if c.strip()
            ]

        config = DiscordConfig(
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_db=args.redis_db,
            redis_password=args.redis_password,
            model=args.model,
            auto_approve=args.auto_approve,
            bot_token=args.discord_token,
            guild_id=args.discord_guild,
            allowed_channels=allowed_channels,
        )

        bot = TurnstoneBot(config, broker, get_storage())
        log.info("channel.starting", adapter="discord", guild_id=config.guild_id)
        bot.run()


if __name__ == "__main__":
    main()
