"""Phase 4: the ``project`` memory scope.

Covers construction-time access resolution (``_project_id`` / ``_project_writable``)
and its effect on recall — ``_visible_scopes`` / ``_resolve_scope_id`` /
``_validate_scope`` — for both interactive and coordinator sessions.  The ACL is
monkeypatched (it is unit-tested in ``test_project_storage.py``); here we assert
the session wiring around it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from turnstone.core import auth
from turnstone.core.session import ChatSession
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    import pytest


def _session(**kwargs: Any) -> ChatSession:
    """Construct a ChatSession with minimal mocked plumbing (no UI calls here)."""
    defaults: dict[str, Any] = dict(
        client=MagicMock(),
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


class TestConstructionResolvesProjectAccess:
    """Construction resolves the attached project through a single
    ``resolve_project_access`` call; recall is gated on read access AND a
    non-archived project."""

    def _access(self, can_read: bool, can_write: bool, state: str = "active") -> object:
        return auth.ProjectAccess(can_read, can_write, "P", state)

    def test_resolves_read_and_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True)
        )
        s = _session(user_id="u1", project_id="p1")
        assert s._project_id == "p1"
        assert s._project_writable is True
        assert s._project_name == "P"

    def test_read_only_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Read access but no write (e.g. a non-member reading a public project).
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, False)
        )
        s = _session(user_id="u1", project_id="p1")
        assert s._project_id == "p1"
        assert s._project_writable is False

    def test_denied_without_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(False, False)
        )
        s = _session(user_id="u1", project_id="p1")
        assert s._project_id == ""
        assert s._project_writable is False

    def test_archived_project_not_recalled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Full access but archived → not recalled (the owner still reaches it via
        # the management routes; the recall path does not).
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True, "archived")
        )
        s = _session(user_id="u1", project_id="p1")
        assert s._project_id == ""
        assert s._project_writable is False

    def test_no_project_id_is_inert(self) -> None:
        s = _session(user_id="u1")
        assert s._project_id == ""
        assert s._project_writable is False

    def test_unauthenticated_never_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even if the ACL would allow it, an empty user_id short-circuits before
        # the resolver is ever consulted.
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True)
        )
        s = _session(user_id="", project_id="p1")
        assert s._project_id == ""


class TestProjectRecall:
    def test_interactive_visible_scopes_includes_project(self) -> None:
        s = _session(user_id="u1", ws_id="ws1")
        s._project_id = "p1"
        scopes = s._visible_scopes()
        assert ("project", "p1") in scopes
        assert ("global", "") in scopes
        assert ("user", "u1") in scopes

    def test_interactive_without_project_has_no_project_scope(self) -> None:
        s = _session(user_id="u1", ws_id="ws1")
        assert all(scope != "project" for scope, _ in s._visible_scopes())

    def test_coordinator_adds_project_keeps_isolation(self) -> None:
        s = _session(user_id="u1", kind=WorkstreamKind.COORDINATOR)
        s._project_id = "p1"
        scopes = s._visible_scopes()
        assert ("coordinator", "u1") in scopes
        assert ("project", "p1") in scopes
        # Coord stays isolated from global / user / workstream even with a project.
        assert all(scope == "coordinator" or scope == "project" for scope, _ in scopes)

    def test_visible_scopes_omits_empty_project(self) -> None:
        s = _session(user_id="u1", ws_id="ws1")
        s._project_id = ""
        assert all(scope != "project" for scope, _ in s._visible_scopes())


class TestProjectScopeResolutionAndValidation:
    def test_resolve_scope_id_project(self) -> None:
        s = _session(user_id="u1")
        s._project_id = "p1"
        assert s._resolve_scope_id("project") == "p1"

    def test_validate_requires_attachment(self) -> None:
        s = _session(user_id="u1")
        assert s._validate_scope("project", "cid") is not None  # not attached → rejected
        s._project_id = "p1"
        assert s._validate_scope("project", "cid") is None

    def test_coordinator_allows_project_rejects_global(self) -> None:
        s = _session(user_id="u1", kind=WorkstreamKind.COORDINATOR)
        s._project_id = "p1"
        assert s._validate_scope("project", "cid") is None  # project allowed for coord
        assert s._validate_scope("global", "cid") is not None  # global still rejected


class TestProjectInSystemContext:
    """The attached project's name renders in the system message Session Context."""

    def test_build_context_includes_project_when_set(self) -> None:
        from turnstone.prompts import SessionContext, _build_context

        ctx = SessionContext(
            current_datetime="2026-06-26T12:00",
            timezone="UTC",
            username="alice",
            project="NC Data Centers",
        )
        out = _build_context(ctx, WorkstreamKind.INTERACTIVE)
        assert "- **Project:** NC Data Centers" in out
        assert "- **User:** alice" in out

    def test_build_context_omits_project_when_empty(self) -> None:
        from turnstone.prompts import SessionContext, _build_context

        ctx = SessionContext(
            current_datetime="2026-06-26T12:00",
            timezone="UTC",
            username="alice",
        )
        out = _build_context(ctx, WorkstreamKind.INTERACTIVE)
        assert "Project:" not in out


class TestProjectWriteGate:
    """The save AND delete memory paths block writes to a project the session
    can read but not write (a read-only member of a public project).  Construction
    resolves ``_project_writable``; these drive the preparer to assert the gate
    actually fires (the resolution-level check lives in
    ``TestConstructionResolvesProjectAccess``)."""

    def _attached(self, *, writable: bool) -> ChatSession:
        s = _session(user_id="u1")
        s._project_id = "p1"
        s._project_writable = writable
        return s

    def test_save_blocked_when_read_only(self) -> None:
        s = self._attached(writable=False)
        out = s._prepare_memory(
            "cid", {"action": "save", "scope": "project", "name": "k", "content": "v"}
        )
        assert "read-only access to this project" in out.get("error", "")

    def test_save_allowed_when_writable(self) -> None:
        s = self._attached(writable=True)
        out = s._prepare_memory(
            "cid", {"action": "save", "scope": "project", "name": "k", "content": "v"}
        )
        assert "error" not in out
        assert out.get("execute") is not None  # would proceed to the save exec

    def test_delete_blocked_when_read_only(self) -> None:
        s = self._attached(writable=False)
        out = s._prepare_memory("cid", {"action": "delete", "scope": "project", "name": "k"})
        assert "read-only access to this project" in out.get("error", "")

    def test_delete_allowed_when_writable(self) -> None:
        s = self._attached(writable=True)
        out = s._prepare_memory("cid", {"action": "delete", "scope": "project", "name": "k"})
        assert "error" not in out
        assert out.get("execute") is not None


class TestProjectDefaultSaveScope:
    """A writable attached project becomes the DEFAULT save scope (both kinds);
    a read-only or unattached session keeps the kind default."""

    def test_writable_project_is_default(self) -> None:
        s = _session(user_id="u1")
        s._project_id = "p1"
        s._project_writable = True
        assert s._default_memory_scope() == "project"

    def test_read_only_project_keeps_kind_default(self) -> None:
        s = _session(user_id="u1")
        s._project_id = "p1"
        s._project_writable = False
        assert s._default_memory_scope() == "global"

    def test_no_project_keeps_kind_default(self) -> None:
        assert _session(user_id="u1")._default_memory_scope() == "global"

    def test_coordinator_writable_project_is_default(self) -> None:
        s = _session(user_id="u1", kind=WorkstreamKind.COORDINATOR)
        s._project_id = "p1"
        s._project_writable = True
        assert s._default_memory_scope() == "project"

    def test_coordinator_without_project_is_coordinator(self) -> None:
        s = _session(user_id="u1", kind=WorkstreamKind.COORDINATOR)
        assert s._default_memory_scope() == "coordinator"

    def test_save_without_scope_lands_in_project(self) -> None:
        # End-to-end: an unscoped save in a writable-project session resolves to
        # scope=project / scope_id=project_id (not the global default).
        s = _session(user_id="u1")
        s._project_id = "p1"
        s._project_writable = True
        out = s._prepare_memory("cid", {"action": "save", "name": "k", "content": "v"})
        assert out.get("scope") == "project"
        assert out.get("scope_id") == "p1"
