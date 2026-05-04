"""Pluggable storage backend for turnstone persistence.

Supports SQLite (default, zero-config) and PostgreSQL (multi-node, production).
"""

from turnstone.core.storage._protocol import StorageBackend, StorageConflictError
from turnstone.core.storage._registry import (
    StorageUnavailableError,
    get_storage,
    init_storage,
    reset_storage,
)

__all__ = [
    "StorageBackend",
    "StorageConflictError",
    "StorageUnavailableError",
    "get_storage",
    "init_storage",
    "reset_storage",
]
