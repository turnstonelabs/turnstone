"""Bootstrap secrets that may be references: resolved once at startup, or the process stops."""

from __future__ import annotations

from turnstone.core.config import ConfigError
from turnstone.core.log import get_logger
from turnstone.core.secret_refs._errors import SecretError
from turnstone.core.secret_refs._reference import is_reference
from turnstone.core.secret_refs._resolver import get_resolver, register_bootstrap_secret

log = get_logger(__name__)


def _fetch_bootstrap(value: str) -> str:
    secret = get_resolver().fetch(value, bootstrap=True)
    register_bootstrap_secret(secret)
    return secret


def resolve_bootstrap_secret(setting: str, value: str) -> str:
    """Return *value*, or the value it names when it is a ``secret://`` reference.

    A bootstrap secret (the JWT signing secret, the OIDC client secret) is read once, early in
    startup, and nothing can run without it. A reference that cannot be resolved (no backend for
    it, a store that is down with nothing cached, a secret that does not exist) therefore ends the
    process after logging why; the service manager's restart policy is the retry. The value is not
    refreshed afterwards: a rotation in the store reaches a process at its next start. Literal or
    resolved, the value is registered so that no model key or MCP header reference may resolve to
    it.
    """
    if not is_reference(value):
        register_bootstrap_secret(value)
        return value
    try:
        return _fetch_bootstrap(value)
    except ConfigError as exc:  # the loader has logged why; the gateway reads the file here first
        raise SystemExit(1) from exc
    except SecretError as exc:
        log.error(
            "secret_refs.bootstrap_unresolved", setting=setting, reference=value, error=str(exc)
        )
        raise SystemExit(1) from exc


def try_resolve_bootstrap_secret(setting: str, value: str) -> str | None:
    """Like :func:`resolve_bootstrap_secret`, for paths that must not stop the process.

    A reference that cannot be resolved logs a warning and answers ``None``; the caller goes on
    without the secret.
    """
    if not is_reference(value):
        register_bootstrap_secret(value)
        return value
    try:
        return _fetch_bootstrap(value)
    except SecretError as exc:
        log.warning(
            "secret_refs.bootstrap_unresolved", setting=setting, reference=value, error=str(exc)
        )
        return None
