"""Per-call model-backend credential resolution.

The model registry owns immutable endpoint/config snapshots; the host process
owns OAuth mint state.  This module is the single policy seam that joins those
two inputs for every model-backed role without importing session lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from turnstone.core.log import get_logger
from turnstone.core.model_registry import (
    APP_IDENTITY_MODEL_AUTH_MODES,
    DYNAMIC_MODEL_AUTH_MODES,
    MODEL_AUTH_MODE_PROFILES,
    SCOPES_MODEL_AUTH_MODES,
)

if TYPE_CHECKING:
    from turnstone.core.model_registry import ModelConfig

log = get_logger(__name__)


class BackendAuthUnavailableError(RuntimeError):
    """A fail-closed dynamic model credential could not be resolved."""


def _mint_refusal_cause(
    prefix: str,
    alias: str,
    user_id: str = "",
    grant_leg: str | None = None,
) -> str:
    """Return the mint layer's retained refusal cause for a warning."""
    from turnstone.core.mcp_oauth import (
        MODEL_APP_MINT_PRINCIPAL,
        model_app_cache_server,
        model_mint_refusal_cause,
        model_obo_cause_key,
    )

    if prefix == "model_obo":
        return (
            model_mint_refusal_cause(prefix, model_obo_cause_key(alias, grant_leg), user_id)
            or "unknown"
        )
    return (
        model_mint_refusal_cause(prefix, model_app_cache_server(alias), MODEL_APP_MINT_PRINCIPAL)
        or "unknown"
    )


def resolve_model_backend_auth_token(
    alias: str,
    config: ModelConfig | None,
    *,
    principal_id: str,
    config_store: Any | None,
    mint_client: Any | None,
) -> str | None:
    """Resolve the dynamic credential for one pinned alias/config/principal.

    ``None`` means the registry-owned client's explicit static key remains in
    force.  Keyless dynamic aliases and configured fail-closed deployments
    raise :class:`BackendAuthUnavailableError`; the SDK-construction placeholder
    is never allowed onto the wire.
    """
    if not alias or config is None:
        return None
    mode = getattr(config, "auth_mode", "static")
    obo_audience = getattr(config, "obo_audience", "")
    if mode not in DYNAMIC_MODEL_AUTH_MODES or not obo_audience:
        return None
    has_static_key = bool(getattr(config, "api_key", ""))
    configured_fail_closed = bool(
        config_store is not None and config_store.get("model.auth_fail_closed")
    )
    must_fail_closed = configured_fail_closed or not has_static_key
    user_id = ""
    if mode not in APP_IDENTITY_MODEL_AUTH_MODES:
        user_id = principal_id.strip()
        if not user_id:
            log.warning(
                "model_obo.no_user_context",
                alias=alias,
                audience=obo_audience,
                has_static_key=has_static_key,
            )
            raise BackendAuthUnavailableError(
                f"Delegated backend authentication has no user for model alias {alias!r}"
            )
    if mint_client is None:
        log.warning(
            "model_backend_auth.mint_client_unavailable",
            alias=alias,
            auth_mode=mode,
            audience=obo_audience,
            has_static_key=has_static_key,
        )
        if must_fail_closed:
            raise BackendAuthUnavailableError(
                f"Dynamic backend authentication unavailable for model alias {alias!r}"
            )
        return None
    if mode in APP_IDENTITY_MODEL_AUTH_MODES:
        token = mint_client.mint_app_token_sync(alias=alias, audience=obo_audience)
        if not token:
            log.warning(
                "model_app.fallback_to_static",
                alias=alias,
                audience=obo_audience,
                cause=_mint_refusal_cause("model_app", alias),
                has_static_key=has_static_key,
            )
            if must_fail_closed:
                raise BackendAuthUnavailableError(
                    f"App backend authentication unavailable for model alias {alias!r}"
                )
            return None
        return cast("str", token)

    mint_scopes = getattr(config, "obo_scopes", "") if mode in SCOPES_MODEL_AUTH_MODES else ""
    grant_leg = MODEL_AUTH_MODE_PROFILES.get(mode)
    if grant_leg is None:
        log.warning(
            "model_obo.unclassified_mode",
            alias=alias,
            auth_mode=mode,
            audience=obo_audience,
        )
        raise BackendAuthUnavailableError(
            "Delegated backend authentication has no registered "
            f"grant-profile pairing for model alias {alias!r}"
        )
    token = mint_client.mint_model_obo_token_sync(
        user_id=user_id,
        alias=alias,
        audience=obo_audience,
        scopes=mint_scopes,
        grant_leg=grant_leg,
    )
    if not token:
        log.warning(
            "model_obo.fallback_to_static",
            alias=alias,
            audience=obo_audience,
            user_id=user_id,
            cause=_mint_refusal_cause("model_obo", alias, user_id, grant_leg),
            has_static_key=has_static_key,
        )
        if must_fail_closed:
            raise BackendAuthUnavailableError(
                f"Delegated backend authentication unavailable for model alias {alias!r}"
            )
        return None
    return cast("str", token)
