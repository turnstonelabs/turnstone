"""Stage 1 scaffolding tests for the unified SessionManager.

These cover construction only — the real lifecycle tests
(``test_create_evicts_oldest_idle_at_capacity``, etc.) land alongside
the Step 2 port of ``create`` / ``open`` / ``close`` / ``set_state``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turnstone.core.session_manager import SessionKindAdapter, SessionManager
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState


class _NoopAdapter:
    """Structural implementation of SessionKindAdapter for construction tests."""

    def __init__(self, kind: WorkstreamKind = WorkstreamKind.INTERACTIVE) -> None:
        self.kind = kind

    def emit_created(self, ws: Workstream) -> None:
        pass

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        pass

    def emit_closed(self, ws_id: str) -> None:
        pass

    def cleanup_ui(self, ws: Workstream) -> None:
        pass

    def build_ui(self, ws: Workstream):  # type: ignore[no-untyped-def]
        return MagicMock()

    def build_session(self, ws: Workstream, **_: object):  # type: ignore[no-untyped-def]
        return MagicMock()


def test_session_manager_constructs_with_adapter() -> None:
    adapter = _NoopAdapter(kind=WorkstreamKind.INTERACTIVE)
    mgr = SessionManager(adapter, storage=MagicMock(), max_active=5)
    assert mgr.max_active == 5
    assert mgr.kind == WorkstreamKind.INTERACTIVE


def test_session_manager_rejects_invalid_max_active() -> None:
    adapter = _NoopAdapter()
    with pytest.raises(ValueError, match="max_active must be >= 1"):
        SessionManager(adapter, storage=MagicMock(), max_active=0)


def test_session_manager_kind_reflects_adapter() -> None:
    adapter = _NoopAdapter(kind=WorkstreamKind.COORDINATOR)
    mgr = SessionManager(adapter, storage=MagicMock(), max_active=3)
    assert mgr.kind == WorkstreamKind.COORDINATOR


def test_noop_adapter_satisfies_protocol() -> None:
    """Structural check — a class with the right methods should satisfy
    the Protocol without explicit inheritance."""
    adapter: SessionKindAdapter = _NoopAdapter()
    assert adapter.kind == WorkstreamKind.INTERACTIVE
