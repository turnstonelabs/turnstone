"""The resolver: backends, the TTL cache and the process-wide instance."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.log import get_logger
from turnstone.core.secret_refs._config import SecretsConfig, parse_secrets_config
from turnstone.core.secret_refs._errors import (
    SecretBackendError,
    SecretReferenceError,
    SecretResolveError,
)
from turnstone.core.secret_refs._reference import is_reference, parse_reference

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

log = get_logger(__name__)


class SecretBackend(Protocol):
    """One store a reference can name: ``secret://<name>/...``."""

    name: str

    def fetch(self, path: str, key: str | None) -> str: ...

    def close(self) -> None: ...


@dataclass
class _Entry:
    value: str
    fetched_at: float


class SecretResolver:
    """Resolve references through the configured backends with a TTL cache.

    A value that is not a reference is returned unchanged. A reference is
    fetched at most once per ``cache_ttl_seconds``, so one Sync or one MCP
    reconnect round costs one round trip per distinct reference. When a
    backend fails in a way that says nothing about the secret itself (the
    store is unreachable, sealed, rate-limited, or refused the login), the
    value fetched last is served with a warning, until the process restarts
    or the store answers definitively; a definitive answer (not found,
    permission denied, a missing field) raises and forgets the cached value.
    Thread-safe.
    """

    def __init__(
        self,
        config: SecretsConfig,
        *,
        backends: Mapping[str, SecretBackend] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, _Entry] = {}
        self._backends: dict[str, SecretBackend] = (
            dict(backends) if backends is not None else _build_backends(config)
        )

    @property
    def config(self) -> SecretsConfig:
        return self._config

    @property
    def backend_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    def resolve(self, value: str) -> str:
        """Return the secret a reference names, or *value* itself when it is not one."""
        if not is_reference(value):
            return value
        return self.fetch(value)

    def resolve_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve every string value of *mapping* that is a reference; copy the rest."""
        return {
            key: self.resolve(value) if isinstance(value, str) else value
            for key, value in mapping.items()
        }

    def validate(self, value: str) -> None:
        """Raise :class:`SecretReferenceError` unless *value* is a well-formed
        reference to a configured backend. Does not fetch anything."""
        self._backend_for(parse_reference(value))

    def fetch(self, reference: str) -> str:
        ref = parse_reference(reference)
        backend = self._backend_for(ref)
        now = self._clock()
        with self._lock:
            entry = self._cache.get(ref.text)
        if entry is not None and now - entry.fetched_at < self._config.cache_ttl_seconds:
            return entry.value
        try:
            value = backend.fetch(ref.path, ref.key)
        except SecretBackendError as exc:
            if entry is not None and exc.retryable:
                log.warning(
                    "secret_refs.resolve_failed_serving_cached",
                    reference=ref.text,
                    error=str(exc),
                    detail=exc.detail,
                    cached_age_seconds=round(now - entry.fetched_at, 1),
                )
                return entry.value
            if entry is not None:
                with self._lock:
                    self._cache.pop(ref.text, None)
            log.error(
                "secret_refs.resolve_failed", reference=ref.text, error=str(exc), detail=exc.detail
            )
            raise SecretResolveError(f"{ref.text}: {exc}", retryable=exc.retryable) from exc
        except SecretReferenceError as exc:
            raise SecretResolveError(f"{ref.text}: {exc}") from exc
        if not value:
            with self._lock:
                self._cache.pop(ref.text, None)
            raise SecretResolveError(f"{ref.text}: resolved to an empty value")
        with self._lock:
            self._cache[ref.text] = _Entry(value=value, fetched_at=now)
        return value

    def invalidate(self, reference: str | None = None) -> None:
        """Forget cached values so the next resolve asks the store again."""
        with self._lock:
            if reference is None:
                self._cache.clear()
            else:
                self._cache.pop(reference, None)

    def close(self) -> None:
        for backend in self._backends.values():
            try:
                backend.close()
            except Exception:
                log.debug("secret_refs.backend_close_failed", backend=backend.name, exc_info=True)

    def _backend_for(self, ref: Any) -> SecretBackend:
        backend = self._backends.get(ref.backend)
        if backend is None:
            raise SecretReferenceError(
                f"{ref.text}: backend {ref.backend!r} is not configured "
                f"([secrets.{ref.backend}] in config.toml)"
            )
        return backend


def _build_backends(config: SecretsConfig) -> dict[str, SecretBackend]:
    backends: dict[str, SecretBackend] = {}
    if config.file is not None:
        from turnstone.core.secret_refs._file import FileBackend

        backends["file"] = FileBackend(config.file.root)
    if config.vault is not None:
        from turnstone.core.secret_refs._vault import VaultBackend

        backends["vault"] = VaultBackend(config.vault)
    return backends


# ---------------------------------------------------------------------------
# The process-wide instance
# ---------------------------------------------------------------------------

_instance_lock = threading.Lock()
_instance: SecretResolver | None = None


def load_secrets_config() -> SecretsConfig:
    """Parse ``[secrets]`` from config.toml; raises :class:`SecretConfigError`."""
    from turnstone.core.config import config_file_path, load_config

    return parse_secrets_config(load_config("secrets"), config_path=config_file_path())


def get_resolver() -> SecretResolver:
    """Return the resolver built from ``[secrets]`` in config.toml.

    Built once, on first use: config.toml is read once per process, so there
    is nothing to rebuild for. Raises :class:`SecretConfigError` when the
    section is malformed; hosts call :func:`load_secrets_config` at startup so
    that surfaces as a boot failure rather than at the first reference.
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = SecretResolver(load_secrets_config())
            log.info("secret_refs.resolver_built", backends=list(_instance.backend_names))
        return _instance


def invalidate_cache() -> None:
    """Forget every cached value, if a resolver was built; never builds one."""
    with _instance_lock:
        instance = _instance
    if instance is not None:
        instance.invalidate()


def reset_for_tests() -> None:
    """Drop the process-wide resolver so the next use rebuilds it from config."""
    global _instance
    with _instance_lock:
        previous = _instance
        _instance = None
    if previous is not None:
        previous.close()
