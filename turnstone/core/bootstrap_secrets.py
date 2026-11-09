"""Register the bootstrap secrets in a tool process, so the guard on runtime references holds."""

from __future__ import annotations


def register_bootstrap_values() -> None:
    """Remember the JWT signing secret and the OIDC client secret as bootstrap values.

    The server and the console register both while loading them at startup. A tool (the CLI, the
    admin commands, doctor) can run without either, yet resolves stored model keys and MCP headers;
    without this, a reference naming a bootstrap secret would resolve there. Nothing here exits: a
    reference that cannot be resolved is logged and left unregistered. A tool registers only the
    values its own environment and config.toml name, so it runs with the services' configuration
    when the guard should hold for their secrets.
    """
    from turnstone.core.auth import jwt_secret_if_configured
    from turnstone.core.oauth.oidc import register_oidc_client_secret

    jwt_secret_if_configured()
    register_oidc_client_secret()
