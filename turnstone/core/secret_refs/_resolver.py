"""The resolver: backends, the TTL cache and the process-wide instance."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turnstone.core.http_headers import HEADER_VALUE_RE
from turnstone.core.log import get_logger
from turnstone.core.secret_refs._config import SecretsConfig, parse_secrets_config
from turnstone.core.secret_refs._errors import (
    SecretBackendError,
    SecretConfigError,
    SecretError,
    SecretReferenceError,
    SecretResolveError,
)
from turnstone.core.secret_refs._reference import parse_reference

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from turnstone.core.secret_refs._reference import SecretReference

log = get_logger(__name__)

# After a backend fails in a retryable way, further references to it are
# answered from the cache (or refused) without a round trip for this long, so
# a registry load during a store outage costs one timeout, not one per
# reference.
FAILURE_COOLDOWN_SECONDS = 5.0


class SecretBackend(Protocol):
    """One store a reference can name: ``secret://<name>/...``."""

    name: str

    def fetch(self, path: str, key: str | None) -> str: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Entry:
    value: str
    fetched_at: float
    # The resolver's invalidation count when the fetch started: the value is
    # trusted only while no invalidate() has happened since, and stays the
    # fallback after one.
    generation: int


class SecretResolver:
    """Resolve references through the configured backends with a TTL cache.

    A reference is fetched at most once per ``cache_ttl_seconds`` by callers
    that do not race each other, so one Sync or one MCP reconnect round costs
    one round trip per distinct reference. When a backend fails in a way that
    says nothing about the secret itself (the store is unreachable, sealed,
    rate-limited, or refused the login), the value fetched last is served with
    a warning, until the process restarts or the store answers definitively; a
    definitive answer (not found, permission denied, a missing field) raises
    and forgets the cached value. :meth:`invalidate` makes the next resolve ask
    the store again but keeps the last value as that fallback, so an
    operator-triggered reload during an outage degrades to the values already
    in use instead of losing them. Thread-safe.
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
        # Bumped by invalidate(). An entry stamped with an older count is asked
        # for again, including one whose fetch was in flight when the count
        # moved, so a value read just before a rotation is not trusted for a
        # full TTL.
        self._generation = 0
        # Backend name -> (clock time before which the backend is not asked,
        # the failure that started the cool-down).
        self._retry_after: dict[str, tuple[float, SecretBackendError]] = {}
        self._backends: dict[str, SecretBackend] = (
            dict(backends) if backends is not None else _build_backends(config)
        )

    @property
    def backend_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    @property
    def cache_ttl_seconds(self) -> float:
        return self._config.cache_ttl_seconds

    def fetch(self, reference: str) -> str:
        ref = parse_reference(reference)
        backend = self._backend_for(ref)
        now = self._clock()
        with self._lock:
            entry = self._cache.get(ref.text)
            generation = self._generation
            cooling = self._retry_after.get(ref.backend)
        if (
            entry is not None
            and entry.generation == generation
            and now - entry.fetched_at < self._config.cache_ttl_seconds
        ):
            return entry.value
        if cooling is not None and now < cooling[0]:
            return self._serve_stale(ref, entry, cooling[1], now)
        try:
            value = backend.fetch(ref.path, ref.key)
            problem = _value_problem(value)
            if problem is not None:
                raise SecretBackendError(problem, retryable=False)
        except SecretBackendError as exc:
            if exc.retryable:
                # Read the clock again: a timeout has just consumed as long as
                # the cool-down itself.
                failed_at = self._clock()
                with self._lock:
                    # A failure from before an invalidate() must not cool down
                    # the reload that invalidate() was called for.
                    if self._generation == generation:
                        self._retry_after[ref.backend] = (
                            failed_at + FAILURE_COOLDOWN_SECONDS,
                            exc,
                        )
                return self._serve_stale(ref, entry, exc, failed_at)
            with self._lock:
                self._cache.pop(ref.text, None)
            log.error(
                "secret_refs.resolve_failed", reference=ref.text, error=str(exc), detail=exc.detail
            )
            raise SecretResolveError(f"{ref.text}: {exc}", retryable=False) from exc
        except SecretReferenceError as exc:
            raise SecretResolveError(f"{ref.text}: {exc}") from exc
        with self._lock:
            self._retry_after.pop(ref.backend, None)
            self._cache[ref.text] = _Entry(value, now, generation)
        return value

    def _serve_stale(
        self, ref: SecretReference, entry: _Entry | None, exc: SecretBackendError, now: float
    ) -> str:
        """Answer a retryable failure from the cache, or raise when it is empty."""
        if entry is not None:
            log.warning(
                "secret_refs.resolve_failed_serving_cached",
                reference=ref.text,
                error=str(exc),
                detail=exc.detail,
                cached_age_seconds=round(now - entry.fetched_at, 1),
            )
            return entry.value
        log.error(
            "secret_refs.resolve_failed", reference=ref.text, error=str(exc), detail=exc.detail
        )
        raise SecretResolveError(f"{ref.text}: {exc}", retryable=True) from exc

    def invalidate(self) -> None:
        """Ask the store again at the next resolve; keep the last values as the fallback."""
        with self._lock:
            self._generation += 1
            self._retry_after.clear()

    def close(self) -> None:
        for backend in self._backends.values():
            try:
                backend.close()
            except Exception:
                log.debug("secret_refs.backend_close_failed", backend=backend.name, exc_info=True)

    def _backend_for(self, ref: SecretReference) -> SecretBackend:
        backend = self._backends.get(ref.backend)
        if backend is None:
            raise SecretReferenceError(
                f"{ref.text}: backend {ref.backend!r} is not configured "
                f"([secrets.{ref.backend}] in config.toml)"
            )
        return backend


def _value_problem(value: str) -> str | None:
    """Why a store's *value* cannot be used, worded without quoting it, or None.

    Every value a reference names goes out in an HTTP header (a model's API key, an MCP
    server's header), so it must be one a header can carry (:data:`HEADER_VALUE_RE`).
    """
    if not value:
        return "resolved to an empty value"
    if not HEADER_VALUE_RE.fullmatch(value):
        return (
            "resolved to a value an HTTP header cannot carry: a space at either end, a control "
            "character such as a trailing line break, or a character outside ASCII"
        )
    return None


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

    Built once: config.toml is read once per process, so there is nothing to
    rebuild for. Building it parses the section and constructs the backends
    without touching any store, and raises :class:`SecretConfigError` for
    anything wrong with the configuration (a malformed table, an unreadable CA
    bundle). The hosts call it at startup so that surfaces as a boot failure
    rather than at the first reference.
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            config = load_secrets_config()
            try:
                _instance = SecretResolver(config)
            except SecretError:
                raise
            except Exception as exc:
                raise SecretConfigError(
                    f"config.toml [secrets]: cannot set up the backends: {exc}"
                ) from exc
            if _instance.backend_names:
                log.info("secret_refs.resolver_built", backends=list(_instance.backend_names))
        return _instance


def invalidate_cache() -> None:
    """Make the next resolve ask the store again, if a resolver was built; never builds one."""
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
