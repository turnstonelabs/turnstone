"""Unified channel gateway entry point.

Launches one or more channel adapters (Discord, Slack, etc.) connected to
the turnstone server via HTTP.  An HTTP server runs alongside for inbound
notification delivery from the server.

Run as: ``turnstone-channel --discord-token $TURNSTONE_DISCORD_TOKEN``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import sys
import time
from typing import TYPE_CHECKING, cast

from turnstone.core.log import add_log_args, configure_logging_from_args, get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turnstone.channels._protocol import ChannelAdapter
    from turnstone.core.storage import StorageBackend

    # uvicorn ASGIApp is unions of several protocols; use a loose alias here.
    _ASGIApp = Callable[..., Awaitable[None]]

log = get_logger(__name__)

_DISCOVERY_BUDGET_S = 30.0  # cap total wall-clock wait on startup discovery
_DISCOVERY_INITIAL_DELAY_S = 1.0  # first retry delay
_DISCOVERY_MAX_DELAY_S = 8.0  # cap per-attempt sleep


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="turnstone channel gateway — bridges messaging platforms to the turnstone cluster"
    )

    # -- Server connection ---------------------------------------------------
    parser.add_argument(
        "--server-url",
        default=os.environ.get("TURNSTONE_SERVER_URL", ""),
        help="Turnstone server URL (default: $TURNSTONE_SERVER_URL, or auto-discovered)",
    )
    parser.add_argument(
        "--console-url",
        default=os.environ.get("TURNSTONE_CONSOLE_URL", ""),
        help="Console URL for multi-node routing (default: $TURNSTONE_CONSOLE_URL). "
        "When set, control-plane POSTs route through the console; "
        "SSE connects to node_url from the create response.",
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

    # -- Slack ---------------------------------------------------------------
    parser.add_argument(
        "--slack-token",
        default=os.environ.get("TURNSTONE_SLACK_TOKEN", ""),
        help="Slack bot token (default: $TURNSTONE_SLACK_TOKEN)",
    )
    parser.add_argument(
        "--slack-app-token",
        default=os.environ.get("TURNSTONE_SLACK_APP_TOKEN", ""),
        help="Slack app-level token for Socket Mode (default: $TURNSTONE_SLACK_APP_TOKEN)",
    )
    parser.add_argument(
        "--slack-channels",
        default=os.environ.get("TURNSTONE_SLACK_CHANNELS", ""),
        help="Comma-separated list of allowed Slack channel IDs (default: all)",
    )
    parser.add_argument(
        "--slack-slash-command",
        default=os.environ.get("TURNSTONE_SLACK_SLASH_COMMAND", "/turnstone"),
        help="Slack slash command name (default: /turnstone)",
    )

    # -- HTTP server ---------------------------------------------------------
    parser.add_argument(
        "--http-host",
        default="127.0.0.1",
        help="HTTP server bind address (default: %(default)s)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.environ.get("TURNSTONE_CHANNEL_PORT", "8091")),
        help="HTTP server port (default: $TURNSTONE_CHANNEL_PORT or 8091)",
    )

    # -- TLS -----------------------------------------------------------------
    parser.add_argument("--ssl-certfile", default=None, help="SSL certificate file for HTTPS")
    parser.add_argument("--ssl-keyfile", default=None, help="SSL private key file")
    parser.add_argument("--ssl-ca-certs", default=None, help="SSL CA certs for client verification")

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
    add_log_args(parser)

    return parser


def _build_token_factories(
    jwt_secret: str,
) -> tuple[Callable[[], str] | None, Callable[[], str] | None]:
    """Return ``(console_factory, server_factory)`` when a JWT secret is set."""
    if not jwt_secret:
        return None, None

    from turnstone.core.auth import JWT_AUD_CONSOLE, JWT_AUD_SERVER, ServiceTokenManager

    scopes = frozenset({"read", "write", "approve", "service"})
    console_mgr = ServiceTokenManager(
        user_id="channel-gateway",
        scopes=scopes,
        source="channel",
        secret=jwt_secret,
        audience=JWT_AUD_CONSOLE,
        expiry_hours=1,
    )
    server_mgr = ServiceTokenManager(
        user_id="channel-gateway",
        scopes=scopes,
        source="channel",
        secret=jwt_secret,
        audience=JWT_AUD_SERVER,
        expiry_hours=1,
    )

    def console_factory() -> str:
        return console_mgr.token

    def server_factory() -> str:
        return server_mgr.token

    return console_factory, server_factory


def _resolve_service_urls(
    storage: StorageBackend,
    console_url: str,
    server_url: str,
) -> tuple[str, str]:
    """Fill in missing console / server URLs from the service registry.

    Retries with exponential backoff up to ``_DISCOVERY_BUDGET_S`` seconds
    since the console / servers may still be starting up.  Returns
    ``(console_url, server_url)``.
    """
    if console_url and server_url:
        return console_url, server_url

    try:
        log.info("channel.discovering_services")
        deadline = time.monotonic() + _DISCOVERY_BUDGET_S
        delay = _DISCOVERY_INITIAL_DELAY_S
        while True:
            if not console_url:
                consoles = storage.list_services("console", max_age_seconds=3600)
                if consoles:
                    console_url = consoles[0]["url"]
                    log.info("channel.discovered_console", url=console_url)
            if not server_url:
                servers = storage.list_services("server", max_age_seconds=120)
                if servers:
                    server_url = servers[0]["url"]
                    log.info("channel.discovered_server", url=server_url)
            if console_url or server_url:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "channel.discovery_timeout",
                    console_url=console_url,
                    server_url=server_url,
                )
                break

            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _DISCOVERY_MAX_DELAY_S)
    except Exception:
        log.warning("channel.discovery_failed", exc_info=True)

    return console_url, server_url


def _build_adapters(
    args: argparse.Namespace,
    storage: StorageBackend,
    *,
    server_url: str,
    console_url: str,
    console_token_factory: Callable[[], str] | None,
    server_token_factory: Callable[[], str] | None,
) -> dict[str, ChannelAdapter]:
    """Instantiate the channel adapters selected by the provided args."""
    adapters: dict[str, ChannelAdapter] = {}

    if args.discord_token:
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.channels.discord.config import DiscordConfig

        allowed_channels: list[int] = []
        if args.discord_channels:
            allowed_channels = [
                int(c.strip()) for c in args.discord_channels.split(",") if c.strip()
            ]

        discord_config = DiscordConfig(
            server_url=server_url,
            model=args.model,
            auto_approve=args.auto_approve,
            bot_token=args.discord_token,
            guild_id=args.discord_guild,
            allowed_channels=allowed_channels,
        )
        discord_bot = TurnstoneBot(
            discord_config,
            server_url,
            storage,
            console_url=console_url,
            console_token_factory=console_token_factory,
            server_token_factory=server_token_factory,
        )
        adapters[discord_bot.channel_type] = cast("ChannelAdapter", discord_bot)

    if args.slack_token:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig

        slack_config = SlackConfig(
            model=args.model,
            auto_approve=args.auto_approve,
            bot_token=args.slack_token,
            app_token=args.slack_app_token,
            allowed_channels=[c.strip() for c in args.slack_channels.split(",") if c.strip()],
            slash_command=args.slack_slash_command,
        )
        slack_bot = TurnstoneSlackBot(
            slack_config,
            server_url=server_url,
            storage=storage,
            console_url=console_url,
            console_token_factory=console_token_factory,
            server_token_factory=server_token_factory,
        )
        adapters[slack_bot.channel_type] = cast("ChannelAdapter", slack_bot)

    return adapters


def _resolve_advertise_url(args: argparse.Namespace) -> str:
    """Compute the URL the gateway should advertise in the service registry."""
    override = os.environ.get("TURNSTONE_CHANNEL_ADVERTISE_URL", "").strip()
    if override:
        return override

    advertise_host = socket.gethostname() if args.http_host in ("0.0.0.0", "::") else args.http_host
    scheme = "https" if args.ssl_certfile else "http"
    return f"{scheme}://{advertise_host}:{args.http_port}"


async def _heartbeat_loop(storage: StorageBackend, service_id: str) -> None:
    """Periodically update the channel service heartbeat."""
    from turnstone.core.storage._registry import StorageUnavailableError

    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(storage.heartbeat_service, "channel", service_id)
        except StorageUnavailableError:
            pass  # already logged by storage layer
        except Exception:
            log.exception("channel.heartbeat_failed")


async def _run_gateway(
    adapters: dict[str, ChannelAdapter],
    channel_app: _ASGIApp,
    storage: StorageBackend,
    args: argparse.Namespace,
) -> None:
    """Run all adapters + HTTP server + service heartbeat concurrently."""
    import uvicorn

    from turnstone.channels._http import _get_service_id

    service_id = _get_service_id()
    service_url = _resolve_advertise_url(args)

    storage.register_service("channel", service_id, service_url)
    log.info("channel.service_registered", service_id=service_id, url=service_url)

    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        print(
            "Both --ssl-certfile and --ssl-keyfile are required for TLS",
            file=sys.stderr,
        )
        sys.exit(1)

    uv_config = uvicorn.Config(
        channel_app,
        host=args.http_host,
        port=args.http_port,
        log_level="warning",
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        ssl_ca_certs=args.ssl_ca_certs,
    )
    server = uvicorn.Server(uv_config)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(storage, service_id))
    try:
        await asyncio.gather(
            *(adapter.start() for adapter in adapters.values()),
            server.serve(),
        )
    finally:
        heartbeat_task.cancel()
        # Await the task so its CancelledError propagates before we tear
        # down the adapters and the service registry below.  CancelledError
        # is the expected outcome after task.cancel(); suppress it.
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

        # Stop adapters so SSE tasks, httpx clients, and Slack socket
        # handlers close cleanly before we deregister from the service
        # registry.
        await asyncio.gather(
            *(adapter.stop() for adapter in adapters.values()),
            return_exceptions=True,
        )

        await asyncio.to_thread(storage.deregister_service, "channel", service_id)
        log.info("channel.service_deregistered", service_id=service_id)


def main() -> None:
    """Parse arguments, initialize storage, and run adapters."""
    from turnstone.channels._http import create_channel_app
    from turnstone.core.storage._registry import get_storage, init_storage

    parser = _build_parser()
    args = parser.parse_args()

    configure_logging_from_args(args, "channel")

    init_storage(
        backend=os.environ.get("TURNSTONE_DB_BACKEND", "sqlite"),
        url=os.environ.get("TURNSTONE_DB_URL", ""),
        path=os.environ.get("TURNSTONE_DB_PATH", ""),
    )
    storage = get_storage()

    jwt_secret = os.environ.get("TURNSTONE_JWT_SECRET", "").strip()
    console_token_factory, server_token_factory = _build_token_factories(jwt_secret)

    console_url, server_url = _resolve_service_urls(
        storage,
        args.console_url,
        args.server_url,
    )

    if not console_url and not server_url:
        print(
            "Error: no console or server URL available. Set --server-url, "
            "--console-url, or ensure the database is reachable and services "
            "are registered.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.discord_token and not args.slack_token:
        print(
            "Error: no channel adapters configured. "
            "Set --discord-token / $TURNSTONE_DISCORD_TOKEN "
            "or --slack-token / $TURNSTONE_SLACK_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    if bool(args.slack_token) != bool(args.slack_app_token):
        raise SystemExit("--slack-token and --slack-app-token must be provided together")

    adapters = _build_adapters(
        args,
        storage,
        server_url=server_url,
        console_url=console_url,
        console_token_factory=console_token_factory,
        server_token_factory=server_token_factory,
    )

    channel_app = create_channel_app(adapters, storage, jwt_secret=jwt_secret)

    log.info(
        "channel.starting",
        adapters=list(adapters.keys()),
        http_port=args.http_port,
        server_url=server_url,
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_gateway(adapters, channel_app, storage, args))


if __name__ == "__main__":
    main()
