"""Test-side enforcement of the storage-row ``_mapping`` contract.

See the class-level docstring on
:class:`turnstone.core.storage._protocol.StorageBackend`: list-style
storage methods return SQLAlchemy ``Row`` objects, and every caller
(production or fake) must access columns through ``row._mapping``.
Positional indexing silently corrupts the projection when a SELECT
reorders or a new trailing column appears.

Use :func:`assert_row_like` in a test fixture or fake factory to fail
fast — a test double that forgets ``_mapping`` should raise at setup
time, not ten assertions later when a downstream consumer reads the
wrong column.
"""

from __future__ import annotations

from typing import Any


def assert_row_like(row: Any, *, required_keys: list[str] | None = None) -> None:
    """Assert ``row`` satisfies the storage-row contract.

    - Must expose a ``_mapping`` attribute supporting ``[key]`` and
      ``.get(key)``.
    - If ``required_keys`` is supplied, every key must resolve via
      ``_mapping.get``.

    Raises :class:`AssertionError` with a specific reason so a failing
    fake is obvious from the test output alone.
    """
    mapping = getattr(row, "_mapping", None)
    if mapping is None:
        raise AssertionError(
            "row is missing the ._mapping attribute — positional "
            "indexing is not a supported access pattern; fakes must "
            "expose the same mapping shape the SQLAlchemy Row does"
        )
    try:
        _ = mapping.get
    except AttributeError as exc:
        raise AssertionError(
            "row._mapping does not support .get() — provide a "
            "dict-like mapping so callers can use .get(key) / [key]"
        ) from exc
    for key in required_keys or ():
        if mapping.get(key) is None and key not in mapping:
            raise AssertionError(
                f"row._mapping missing required key {key!r} — either "
                "extend the fake or update the test's required_keys"
            )
