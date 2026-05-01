"""Shared builders for the coordinator-endpoint test files.

The four coordinator test modules each ship a copy of the same
``_AuthMiddleware`` / ``_FakeConfigStore`` / ``_fake_registry`` /
``_build_mgr`` helpers — this module is the single home for them so
future edits land once.  Named with a leading underscore so pytest
does not collect it.

``_make_client`` stays local to each test module because the route
list differs per file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from starlette.middleware.base import BaseHTTPMiddleware

from turnstone.console.collector import ClusterCollector
from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.auth import AuthResult
from turnstone.core.session_manager import SessionManager

if TYPE_CHECKING:
    from collections.abc import Iterable


def _seed_children(
    adapter: CoordinatorAdapter, coord_ws_id: str, child_ws_ids: Iterable[str]
) -> None:
    """Seed the coordinator adapter's children registry directly.

    The production path populates the registry via the cluster-event
    fan-out thread observing ``ws_created`` events. These tests just
    need a known-children set for the endpoint handlers to iterate —
    inject directly via the registry's bulk-merge surface rather than
    spinning up the collector + fan-out plumbing.
    """
    adapter._registry.merge_children(coord_ws_id, child_ws_ids)


class _AuthMiddleware(BaseHTTPMiddleware):
    """Inject a configurable AuthResult from a header-based contract.

    Tests set ``X-Test-Perms`` to a comma-separated permission list, and
    ``X-Test-User`` to the user id.  Empty or missing → no auth.
    """

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        perms = request.headers.get("X-Test-Perms", "")
        user_id = request.headers.get("X-Test-User", "")
        if perms or user_id:
            request.state.auth_result = AuthResult(
                user_id=user_id,
                scopes=frozenset({"approve"}),
                token_source="test",
                permissions=frozenset(p for p in perms.split(",") if p),
            )
        return await call_next(request)


class _FakeConfigStore:
    """Minimal ConfigStore stub — returns values from a dict."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def _fake_registry() -> MagicMock:
    """MagicMock whose ``.resolve()`` succeeds so the 503 gate passes."""
    reg = MagicMock()
    reg.resolve.return_value = (MagicMock(), "gpt-4", MagicMock())
    return reg


def _build_mgr_with_factory(storage: Any, session_factory: Any) -> SessionManager:
    """Build a SessionManager(CoordinatorAdapter) with a caller-supplied factory.

    Used by tests that need to capture or assert factory kwargs (e.g.
    per-call ``model`` / ``judge_model`` overrides). Plain :func:`_build_mgr`
    is the right entry point when the test doesn't care about the
    factory.
    """
    adapter = CoordinatorAdapter(
        collector=MagicMock(),
        ui_factory=lambda ws: ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id or ""),
        session_factory=session_factory,
    )
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        node_id=ClusterCollector.CONSOLE_PSEUDO_NODE_ID,
        event_emitter=adapter,
    )
    adapter.attach(mgr)
    return mgr


def _build_mgr(storage: Any) -> SessionManager:
    """Build a SessionManager(CoordinatorAdapter) with stub factories (test default)."""

    def _sf(ui, model_alias=None, ws_id=None, **kw):  # type: ignore[no-untyped-def]
        s = MagicMock()
        s.send.return_value = None
        return s

    return _build_mgr_with_factory(storage, _sf)


class MockStorage:
    """Minimal storage mock that implements ``list_services``.

    Used by the collector tests + the console route-walk tests. The
    collector calls ``list_services("turnstone-server", ...)`` to
    discover nodes; tests that don't care about discovery push an
    empty list (the default).
    """

    def __init__(self) -> None:
        self.services: list[dict[str, str]] = []

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        return list(self.services)
