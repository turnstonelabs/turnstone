"""Database-backed configuration store with in-memory caching.

Provides a unified ``get()`` API for runtime config access.  Settings
are loaded from the ``system_settings`` table on init and cached in
memory.  Call ``reload()`` to refresh from storage (e.g. on MQ
invalidation event).

Precedence chain for **server** entry point:
  CLI flag  >  ConfigStore (this)  >  registry default

The server's ``apply_config()`` no longer loads ConfigStore-managed
sections from config.toml, so there is no precedence conflict.
config.toml values for these sections are ignored (with a warning).

The **CLI** entry point still reads config.toml directly (no
ConfigStore) — it is a standalone tool, not a cluster node.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.settings_registry import (
    SETTINGS,
    deserialize_value,
    serialize_value,
    validate_key,
    validate_value,
)

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger(__name__)

_UNSET: Any = object()


class ConfigStore:
    """Runtime config accessor with database-backed storage.

    Thread-safe.  Reads are lock-free after initialization (dict
    lookup on an immutable snapshot).  Writes acquire a lock,
    update storage, and swap the cache atomically.
    """

    def __init__(self, storage: StorageBackend, node_id: str = "") -> None:
        self._storage = storage
        self._node_id = node_id
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._version = 0
        self.reload()

    @property
    def version(self) -> int:
        """Monotonic counter incremented on every cache update."""
        return self._version

    def reload(self) -> None:
        """Load all settings from storage into the in-memory cache."""
        try:
            raw = self._storage.get_system_settings_bulk(node_id=self._node_id)
        except Exception:
            log.warning("Failed to load settings from storage", exc_info=True)
            return
        new_cache: dict[str, Any] = {}
        for key, json_val in raw.items():
            try:
                new_cache[key] = deserialize_value(key, json_val)
            except (ValueError, KeyError):
                log.warning("Skipping invalid setting: %s", key)
        with self._lock:
            self._cache = new_cache
            self._version += 1

    def get(self, key: str, default: Any = _UNSET) -> Any:
        """Get a setting value from cache.

        Returns the stored value if present, otherwise the registry
        default.  If *default* is provided, it takes precedence over
        the registry default for unknown keys.
        """
        cache = self._cache  # snapshot for lock-free read
        if key in cache:
            return cache[key]
        if default is not _UNSET:
            return default
        defn = SETTINGS.get(key)
        return defn.default if defn else None

    def set(self, key: str, value: Any, changed_by: str = "") -> Any:
        """Write a setting to storage and update cache.

        Returns the typed value after validation.
        """
        defn = validate_key(key)
        typed_value = validate_value(key, value)
        self._storage.upsert_system_setting(
            key=key,
            value=serialize_value(typed_value),
            node_id=self._node_id,
            is_secret=defn.is_secret,
            changed_by=changed_by,
        )
        with self._lock:
            self._cache = {**self._cache, key: typed_value}
            self._version += 1
        return typed_value

    def delete(self, key: str) -> bool:
        """Remove a setting from storage (reverts to default)."""
        validate_key(key)  # reject unknown keys
        result = self._storage.delete_system_setting(key, node_id=self._node_id)
        with self._lock:
            new_cache = dict(self._cache)
            new_cache.pop(key, None)
            self._cache = new_cache
            self._version += 1
        return result

    def all_effective(self) -> dict[str, Any]:
        """Return all settings with their effective values.

        Merges stored values with registry defaults.
        """
        cache = self._cache
        result: dict[str, Any] = {}
        for key, defn in SETTINGS.items():
            result[key] = cache.get(key, defn.default)
        return result

    def stored_keys(self) -> frozenset[str]:
        """Return the keys that have explicit values in storage."""
        return frozenset(self._cache.keys())
