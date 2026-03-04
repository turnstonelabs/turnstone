"""Pluggable storage backend for turnstone persistence.

Supports SQLite (default, zero-config) and PostgreSQL (multi-node, production).
"""

from turnstone.core.storage._protocol import StorageBackend
from turnstone.core.storage._registry import get_storage, init_storage, reset_storage

__all__ = [
    "StorageBackend",
    "get_storage",
    "init_storage",
    "reset_storage",
]
