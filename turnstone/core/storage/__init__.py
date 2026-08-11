"""Pluggable storage backend for turnstone persistence.

Supports SQLite (default, zero-config) and PostgreSQL (multi-node, production).
"""

from turnstone.core.storage._protocol import (
    AttachmentWrite,
    ConversationCommitConflictError,
    ConversationCommitWorkstreamGoneError,
    ForkCloneError,
    ForkCloneExpectation,
    ForkCloneSnapshot,
    ForkDestinationConflictError,
    ForkSourceUnavailableError,
    StorageBackend,
    StorageConflictError,
)
from turnstone.core.storage._registry import (
    StorageUnavailableError,
    get_storage,
    init_storage,
    is_storage_initialized,
    reset_storage,
)

__all__ = [
    "AttachmentWrite",
    "ConversationCommitConflictError",
    "ConversationCommitWorkstreamGoneError",
    "StorageBackend",
    "StorageConflictError",
    "ForkCloneError",
    "ForkCloneExpectation",
    "ForkCloneSnapshot",
    "ForkDestinationConflictError",
    "ForkSourceUnavailableError",
    "StorageUnavailableError",
    "get_storage",
    "init_storage",
    "is_storage_initialized",
    "reset_storage",
]
