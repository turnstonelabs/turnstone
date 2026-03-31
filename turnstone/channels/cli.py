"""Unified channel gateway entry point.

Launches one or more channel adapters (Discord, Slack, etc.) connected to
the turnstone server via HTTP.  An HTTP server runs alongside for inbound
notification delivery from the server.

Run as: ``turnstone-channel --discord-token $TURNSTONE_DISCORD_TOKEN``
"""

from __future__ import annotations

import os
import socket
import sys


def main() -> None:
    """Parse arguments, initialize storage, and run adapters."""
    import argparse

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

    # -- Auth ----------------------------------------------------------------
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_CHANNEL_AUTH_TOKEN", ""),
        help="Static auth token for /v1/api/notify (default: $TURNSTONE_CHANNEL_AUTH_TOKEN)",
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
    from turnstone.core.log import add_log_args

    add_log_args(parser)

    args = parser.parse_args()

    # -- Logging setup -------------------------------------------------------
    from turnstone.core.log import configure_logging_from_args

    configure_logging_from_args(args, "channel")

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

    # -- Auth config ---------------------------------------------------------
    auth_token = os.environ.get("TURNSTONE_AUTH_TOKEN", "") or args.auth_token
    jwt_secret = os.environ.get("TURNSTONE_JWT_SECRET", "").strip()

    # Prefer auto-rotating service JWTs when jwt_secret is available.
    # Two separate token factories: one for console (aud=turnstone-console)
    # and one for server nodes (aud=turnstone-server, used for SSE).
    _console_token_factory = None
    _server_token_factory = None
    if jwt_secret:
        from turnstone.core.auth import JWT_AUD_CONSOLE, JWT_AUD_SERVER, ServiceTokenManager

        _scopes = frozenset({"read", "write", "approve"})
        _console_mgr = ServiceTokenManager(
            user_id="channel-gateway",
            scopes=_scopes,
            source="channel",
            secret=jwt_secret,
            audience=JWT_AUD_CONSOLE,
            expiry_hours=1,
        )
        _server_mgr = ServiceTokenManager(
            user_id="channel-gateway",
            scopes=_scopes,
            source="channel",
            secret=jwt_secret,
            audience=JWT_AUD_SERVER,
            expiry_hours=1,
        )
        _console_token_factory = lambda: _console_mgr.token  # noqa: E731
        _server_token_factory = lambda: _server_mgr.token  # noqa: E731
        auth_token = ""  # don't also pass static token

    server_url: str = args.server_url
    console_url: str = args.console_url

    # Auto-discover console and server from services table.
    # Retry until at least one is found — the console/servers may still be
    # starting up.  If the DB is down the cluster isn't functional anyway.
    if not console_url or not server_url:
        import time as _time

        try:
            from turnstone.core.storage._registry import get_storage as _get_st

            _disc_storage = _get_st()
            log.info("channel.discovering_services")
            for _attempt in range(30):  # up to 30s
                if not console_url:
                    consoles = _disc_storage.list_services("console", max_age_seconds=3600)
                    if consoles:
                        console_url = consoles[0]["url"]
                        log.info("channel.discovered_console", url=console_url)
                if not server_url:
                    servers = _disc_storage.list_services("server", max_age_seconds=120)
                    if servers:
                        server_url = servers[0]["url"]
                        log.info("channel.discovered_server", url=server_url)
                if console_url or server_url:
                    break
                _time.sleep(1)
            else:
                log.warning("channel.discovery_timeout", console=console_url, server=server_url)
        except Exception:
            log.warning("channel.discovery_failed", exc_info=True)

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
        import asyncio

        from turnstone.channels._http import _get_service_id, create_channel_app
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.channels.discord.config import DiscordConfig
        from turnstone.core.storage._registry import get_storage

        allowed_channels: list[int] = []
        if args.discord_channels:
            allowed_channels = [
                int(c.strip()) for c in args.discord_channels.split(",") if c.strip()
            ]

        config = DiscordConfig(
            server_url=server_url,
            model=args.model,
            auto_approve=args.auto_approve,
            bot_token=args.discord_token,
            guild_id=args.discord_guild,
            allowed_channels=allowed_channels,
        )

        storage = get_storage()
        bot = TurnstoneBot(
            config,
            server_url,
            storage,
            api_token=auth_token,
            console_url=console_url,
            console_token_factory=_console_token_factory,
            server_token_factory=_server_token_factory,
        )
        adapters = {"discord": bot}

        # Create HTTP app for notification delivery
        channel_app = create_channel_app(
            adapters,  # type: ignore[arg-type]
            storage,
            auth_token=auth_token,
            jwt_secret=jwt_secret,
        )

        log.info(
            "channel.starting",
            adapter="discord",
            guild_id=config.guild_id,
            http_port=args.http_port,
            server_url=server_url,
        )

        async def _run_all() -> None:
            """Run Discord bot + HTTP server + service heartbeat concurrently."""
            import uvicorn

            service_id = _get_service_id()

            # Resolve advertise URL — env override for Docker/K8s,
            # otherwise derive from bind address.
            advertise_url = os.environ.get("TURNSTONE_CHANNEL_ADVERTISE_URL", "").strip()
            if not advertise_url:
                if args.http_host in ("0.0.0.0", "::"):
                    advertise_host = socket.gethostname()
                else:
                    advertise_host = args.http_host
                scheme = "https" if args.ssl_certfile else "http"
                advertise_url = f"{scheme}://{advertise_host}:{args.http_port}"
            service_url = advertise_url

            # Register in service registry
            storage.register_service("channel", service_id, service_url)
            log.info(
                "channel.service_registered",
                service_id=service_id,
                url=service_url,
            )

            async def _heartbeat_loop() -> None:
                """Periodically update service heartbeat."""
                while True:
                    await asyncio.sleep(30)
                    try:
                        await asyncio.to_thread(storage.heartbeat_service, "channel", service_id)
                    except Exception:
                        log.exception("channel.heartbeat_failed")

            # TLS: use cert files if available (from bootstrap or TLSClient)
            ssl_certfile = getattr(args, "ssl_certfile", None)
            ssl_keyfile = getattr(args, "ssl_keyfile", None)
            ssl_ca_certs = getattr(args, "ssl_ca_certs", None)
            if bool(ssl_certfile) != bool(ssl_keyfile):
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
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
                ssl_ca_certs=ssl_ca_certs,
            )
            server = uvicorn.Server(uv_config)

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            try:
                await asyncio.gather(
                    bot.start(),
                    server.serve(),
                )
            finally:
                heartbeat_task.cancel()
                await asyncio.to_thread(storage.deregister_service, "channel", service_id)
                log.info("channel.service_deregistered", service_id=service_id)

        import contextlib

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_all())


if __name__ == "__main__":
    main()
