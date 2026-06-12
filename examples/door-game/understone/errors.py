"""Shared exception types for Understone.

These never cross the MCP boundary — the server layer catches everything
and renders an in-fiction line — but they let internal layers fail with a
readable, specific message.
"""

from __future__ import annotations


class UnderstoneError(Exception):
    """Base class for all Understone errors."""


class WorldLoadError(UnderstoneError):
    """Raised when a content pack fails to parse or validate.

    The message is written to be readable by a pack author: it names the
    file, the offending field, and what was expected.
    """


class PersistenceError(UnderstoneError):
    """Raised when the save store cannot be opened or migrated."""
