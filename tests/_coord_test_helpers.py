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

from typing import Any
from unittest.mock import MagicMock

from starlette.middleware.base import BaseHTTPMiddleware

from turnstone.console.coordinator import CoordinatorManager
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.auth import AuthResult


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


def _build_mgr(storage: Any) -> CoordinatorManager:
    """Build a CoordinatorManager with stub factories (test default)."""

    def _sf(ui, model_alias=None, ws_id=None, **kw):  # type: ignore[no-untyped-def]
        s = MagicMock()
        s.send.return_value = None
        return s

    return CoordinatorManager(
        session_factory=_sf,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=3,
    )
