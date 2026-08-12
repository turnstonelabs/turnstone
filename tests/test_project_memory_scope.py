"""Actor-scoped, live ``project`` memory authorization."""

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


def _execute_prepared_tool(
    session: ChatSession,
    item: dict[str, Any],
) -> tuple[str, str | list[dict[str, Any]]]:
    item.setdefault("_principal_id", session._tool_prepare_principal_id())
    return item["execute"](item)


def _project_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    writable: bool = True,
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
    user_id: str = "u1",
) -> ChatSession:
    """Construct an attached session whose live ACL stays controllable."""
    monkeypatch.setattr(
        auth,
        "resolve_project_access",
        lambda *_a, **_k: auth.ProjectAccess(True, writable, "P", "active"),
    )
    return _session(user_id=user_id, ws_id="ws1", kind=kind, project_id="p1")


class TestLiveProjectAccess:
    """Each access snapshot resolves the attachment for the current actor."""

    def _access(self, can_read: bool, can_write: bool, state: str = "active") -> object:
        return auth.ProjectAccess(can_read, can_write, "P", state)

    def test_resolves_read_and_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True)
        )
        s = _session(user_id="u1", project_id="p1")
        access = s._memory_access()
        assert access.project_id == "p1"
        assert access.project_writable is True
        assert access.project_name == "P"

    def test_read_only_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Read access but no write (e.g. a non-member reading a public project).
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, False)
        )
        s = _session(user_id="u1", project_id="p1")
        access = s._memory_access()
        assert access.project_id == "p1"
        assert access.project_writable is False

    def test_denied_without_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(False, False)
        )
        s = _session(user_id="u1", project_id="p1")
        access = s._memory_access()
        assert access.attached_project_id == "p1"
        assert access.project_id == ""
        assert access.project_writable is False

    def test_archived_project_not_recalled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Full access but archived → not recalled (the owner still reaches it via
        # the management routes; the recall path does not).
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True, "archived")
        )
        s = _session(user_id="u1", project_id="p1")
        access = s._memory_access()
        assert access.attached_project_id == "p1"
        assert access.project_id == ""
        assert access.project_writable is False

    def test_no_project_id_is_inert(self) -> None:
        s = _session(user_id="u1")
        access = s._memory_access()
        assert access.attached_project_id == ""
        assert access.project_id == ""
        assert access.project_writable is False

    def test_unauthenticated_never_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even if the ACL would allow it, an empty user_id short-circuits before
        # the resolver is ever consulted.
        monkeypatch.setattr(
            auth, "resolve_project_access", lambda *a, **k: self._access(True, True)
        )
        s = _session(user_id="", project_id="p1")
        access = s._memory_access()
        assert access.attached_project_id == "p1"
        assert access.project_id == ""

    def test_project_display_name_uses_explicit_principal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []

        def _resolve(principal_id: str, project_id: str) -> object:
            seen.append((principal_id, project_id))
            return self._access(True, True)

        monkeypatch.setattr(auth, "resolve_project_access", _resolve)
        s = _session(user_id="owner", project_id="p1")
        s._acting_user_id = "stale-turn-actor"
        seen.clear()  # Ignore constructor-time system-context composition.

        assert s.project_name_for_principal("reconnecting-viewer") == "P"
        assert seen == [("reconnecting-viewer", "p1")]

    def test_project_display_name_does_not_fallback_for_empty_principal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []

        def _resolve(principal_id: str, project_id: str) -> object:
            seen.append((principal_id, project_id))
            return self._access(True, True)

        monkeypatch.setattr(auth, "resolve_project_access", _resolve)
        s = _session(user_id="owner", project_id="p1")
        seen.clear()

        assert s.project_name_for_principal("") == ""
        assert seen == []


class TestProjectRecall:
    def test_interactive_visible_scopes_includes_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _project_session(monkeypatch)
        scopes = s._visible_scopes()
        assert ("project", "p1") in scopes
        assert ("global", "") in scopes
        assert ("user", "u1") in scopes

    def test_interactive_without_project_has_no_project_scope(self) -> None:
        s = _session(user_id="u1", ws_id="ws1")
        assert all(scope != "project" for scope, _ in s._visible_scopes())

    def test_coordinator_adds_project_keeps_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _project_session(monkeypatch, kind=WorkstreamKind.COORDINATOR)
        scopes = s._visible_scopes()
        assert ("coordinator", "u1") in scopes
        assert ("project", "p1") in scopes
        # Coord stays isolated from global / user / workstream even with a project.
        assert all(scope == "coordinator" or scope == "project" for scope, _ in scopes)

    def test_visible_scopes_omits_empty_project(self) -> None:
        s = _session(user_id="u1", ws_id="ws1")
        assert all(scope != "project" for scope, _ in s._visible_scopes())


class TestProjectScopeResolutionAndValidation:
    def test_resolve_scope_id_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _project_session(monkeypatch)
        assert s._resolve_scope_id("project") == "p1"

    def test_validate_requires_attachment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _session(user_id="u1")
        assert s._validate_scope("project", "cid") is not None  # not attached → rejected
        attached = _project_session(monkeypatch)
        assert attached._validate_scope("project", "cid") is None

    def test_coordinator_allows_project_rejects_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _project_session(monkeypatch, kind=WorkstreamKind.COORDINATOR)
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
    resolves live access; these drive the preparer to assert the gate actually
    fires (the resolution-level check lives in ``TestLiveProjectAccess``)."""

    def _attached(self, monkeypatch: pytest.MonkeyPatch, *, writable: bool) -> ChatSession:
        return _project_session(monkeypatch, writable=writable)

    def test_save_blocked_when_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = self._attached(monkeypatch, writable=False)
        out = s._prepare_memory(
            "cid",
            {
                "action": "save",
                "scope": "project",
                "name": "k",
                "content": "v",
                "description": "Test memory",
            },
        )
        assert "read-only access to the attached project" in out.get("error", "")

    def test_save_allowed_when_writable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = self._attached(monkeypatch, writable=True)
        out = s._prepare_memory(
            "cid",
            {
                "action": "save",
                "scope": "project",
                "name": "k",
                "content": "v",
                "description": "Test memory",
            },
        )
        assert "error" not in out
        assert out.get("execute") is not None  # would proceed to the save exec

    def test_delete_blocked_when_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = self._attached(monkeypatch, writable=False)
        out = s._prepare_memory("cid", {"action": "delete", "scope": "project", "name": "k"})
        assert "read-only access to the attached project" in out.get("error", "")

    def test_delete_allowed_when_writable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = self._attached(monkeypatch, writable=True)
        out = s._prepare_memory("cid", {"action": "delete", "scope": "project", "name": "k"})
        assert "error" not in out
        assert out.get("execute") is not None


class TestActingPrincipalProjectAuthority:
    @staticmethod
    def _tool_call(call_id: str, **arguments: Any) -> dict[str, Any]:
        import json

        return {
            "id": call_id,
            "function": {"name": "memory", "arguments": json.dumps(arguments)},
        }

    def test_guest_cannot_inherit_owner_project_access(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def resolve(user_id: str, _project_id: str, **_kwargs: Any) -> auth.ProjectAccess:
            if user_id == "owner":
                return auth.ProjectAccess(True, True, "Owner Project", "active")
            return auth.ProjectAccess(False, False, "", "")

        monkeypatch.setattr(auth, "resolve_project_access", resolve)
        session = _session(user_id="owner", ws_id="shared", project_id="p1")
        session.bind_acting_user("guest")

        assert all(scope != "project" for scope, _ in session._visible_scopes())
        for action in ("get", "save", "delete"):
            arguments: dict[str, Any] = {
                "action": action,
                "name": "owner_secret",
                "scope": "project",
            }
            if action == "save":
                arguments["content"] = "guest write"
                arguments["description"] = "Guest write attempt"
            item = session._prepare_tool(self._tool_call(action, **arguments))
            assert item["_principal_id"] == "guest"
            assert "error" in item
            assert "acting user cannot access" in item["error"]

    def test_project_delete_revalidates_prepared_principal_and_live_acl(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.memory import (
            get_structured_memory_by_name,
            save_structured_memory,
        )

        guest_access = {"value": auth.ProjectAccess(True, True, "Shared", "active")}

        def resolve(user_id: str, _project_id: str, **_kwargs: Any) -> auth.ProjectAccess:
            if user_id == "guest":
                return guest_access["value"]
            return auth.ProjectAccess(True, True, "Shared", "active")

        monkeypatch.setattr(auth, "resolve_project_access", resolve)
        save_structured_memory(
            "shared_secret",
            "keep",
            description="Shared project secret",
            scope="project",
            scope_id="p1",
        )
        session = _session(user_id="owner", ws_id="shared", project_id="p1")
        session.bind_acting_user("guest")
        item = session._prepare_tool(
            self._tool_call(
                "delete",
                action="delete",
                name="shared_secret",
                scope="project",
            )
        )
        assert "error" not in item
        assert item["_principal_id"] == "guest"

        guest_access["value"] = auth.ProjectAccess(False, False, "", "")
        session.bind_acting_user("owner")
        _, message = _execute_prepared_tool(session, item)

        assert "acting user cannot access" in message
        assert get_structured_memory_by_name("shared_secret", "project", "p1") is not None

    def test_archived_project_is_removed_from_live_visibility(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_state = {"value": "active"}

        monkeypatch.setattr(
            auth,
            "resolve_project_access",
            lambda *_args, **_kwargs: auth.ProjectAccess(
                True, True, "Shared", project_state["value"]
            ),
        )
        session = _session(user_id="owner", ws_id="shared", project_id="p1")
        assert ("project", "p1") in session._visible_scopes()

        project_state["value"] = "archived"

        assert all(scope != "project" for scope, _ in session._visible_scopes())
        item = session._prepare_memory(
            "get", {"action": "get", "name": "anything", "scope": "project"}
        )
        assert "error" in item
        assert "active attached project" in item["error"]


class TestProjectDefaultSaveScope:
    """An attachment is the inherited target even when it is read-only."""

    def test_writable_project_is_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _project_session(monkeypatch)
        assert s._default_memory_scope() == "project"

    def test_read_only_project_remains_inherited_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _project_session(monkeypatch, writable=False)
        assert s._default_memory_scope() == "project"

    def test_no_project_keeps_kind_default(self) -> None:
        assert _session(user_id="u1")._default_memory_scope() == "global"

    def test_coordinator_writable_project_is_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _project_session(monkeypatch, kind=WorkstreamKind.COORDINATOR)
        assert s._default_memory_scope() == "project"

    def test_coordinator_without_project_is_coordinator(self) -> None:
        s = _session(user_id="u1", kind=WorkstreamKind.COORDINATOR)
        assert s._default_memory_scope() == "coordinator"

    def test_save_without_scope_lands_in_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # End-to-end: an unscoped save in a writable-project session resolves to
        # scope=project / scope_id=project_id (not the global default).
        s = _project_session(monkeypatch)
        out = s._prepare_memory(
            "cid",
            {
                "action": "save",
                "name": "k",
                "content": "v",
                "description": "Test memory",
            },
        )
        assert out.get("scope") == "project"
        assert out.get("scope_id") == "p1"


class TestProjectDefaultGetDeleteScope:
    """An attached project is the inherited get/delete target.

    This aligns the name-based lifecycle: a memory saved without an explicit
    scope can be fetched or removed the same way while the workstream remains
    attached to that project.
    """

    @staticmethod
    def _attached(
        monkeypatch: pytest.MonkeyPatch,
        *,
        writable: bool = True,
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
    ) -> ChatSession:
        return _project_session(monkeypatch, writable=writable, kind=kind)

    def test_get_without_scope_targets_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Read access is sufficient for the inherited get target; writability
        # only controls save/delete.
        s = self._attached(monkeypatch, writable=False)
        item = s._prepare_memory("cid", {"action": "get", "name": "k"})
        assert item["scopes_to_try"] == [("project", "p1")]

    def test_delete_without_scope_targets_writable_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = self._attached(monkeypatch)
        item = s._prepare_memory("cid", {"action": "delete", "name": "k"})
        assert item["scopes_to_try"] == [("project", "p1")]

    def test_delete_without_scope_rejects_read_only_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = self._attached(monkeypatch, writable=False)
        item = s._prepare_memory("cid", {"action": "delete", "name": "k"})
        assert "read-only access to the attached project" in item.get("error", "")

    def test_read_only_project_does_not_block_explicit_other_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = self._attached(monkeypatch, writable=False)
        item = s._prepare_memory(
            "cid",
            {"action": "delete", "name": "k", "scope": "global"},
        )
        assert "error" not in item
        assert item["scopes_to_try"] == [("global", "")]

    def test_coordinator_inherits_project_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = self._attached(monkeypatch, kind=WorkstreamKind.COORDINATOR)
        get_item = s._prepare_memory("get", {"action": "get", "name": "k"})
        delete_item = s._prepare_memory("delete", {"action": "delete", "name": "k"})
        assert get_item["scopes_to_try"] == [("project", "p1")]
        assert delete_item["scopes_to_try"] == [("project", "p1")]

    def test_unscoped_get_and_delete_round_trip_project_memory(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.memory import (
            get_structured_memory_by_name,
            save_structured_memory,
        )

        row, _ = save_structured_memory(
            "july_digest",
            "full digest",
            description="July digest",
            scope="project",
            scope_id="p1",
        )
        assert row is not None

        s = self._attached(monkeypatch)
        get_item = s._prepare_memory("get", {"action": "get", "name": "july_digest"})
        _, get_msg = _execute_prepared_tool(s, get_item)
        assert "[general:project] july_digest" in get_msg
        assert "full digest" in get_msg

        delete_item = s._prepare_memory("delete", {"action": "delete", "name": "july_digest"})
        _, delete_msg = _execute_prepared_tool(s, delete_item)
        assert "Deleted memory 'july_digest' (scope=project)" in delete_msg
        assert get_structured_memory_by_name("july_digest", "project", "p1") is None

    def test_wrong_explicit_scope_hints_at_attached_project(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.memory import save_structured_memory

        row, _ = save_structured_memory(
            "july_digest",
            "full digest",
            description="July digest",
            scope="project",
            scope_id="p1",
        )
        assert row is not None

        s = self._attached(monkeypatch)
        for action in ("get", "delete"):
            item = s._prepare_memory(
                action,
                {"action": action, "name": "july_digest", "scope": "global"},
            )
            _, msg = _execute_prepared_tool(s, item)
            assert "not found (scope=global)" in msg
            assert "exists in scope='project'" in msg
            assert "retry with scope='project'" in msg

    def test_project_default_miss_hints_at_other_visible_scope(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.memory import save_structured_memory

        row, _ = save_structured_memory(
            "shared_runbook",
            "global content",
            description="Shared runbook",
            scope="global",
        )
        assert row is not None

        s = self._attached(monkeypatch)
        for action in ("get", "delete"):
            item = s._prepare_memory(
                action,
                {"action": action, "name": "shared_runbook"},
            )
            _, msg = _execute_prepared_tool(s, item)
            assert "not found (scope=project)" in msg
            assert "exists in scope='global'" in msg
            assert "retry with scope='global'" in msg
