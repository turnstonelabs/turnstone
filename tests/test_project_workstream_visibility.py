"""Private-project workstream visibility enforcement.

Covers the tenancy predicate (:class:`WorkstreamProjectVisibility`), the
create-time attach gate (:func:`ensure_project_attachable`), the row-access
gate in :func:`resolve_workstream_owner`, and the saved-list filter in
``_collect_saved_rows`` — the choke points that keep workstreams attached
to a private project out of non-members' listings and 403 their direct
access.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.auth import (
    WorkstreamProjectVisibility,
    ensure_project_attachable,
)

pytestmark = pytest.mark.anyio


def _fake_storage(
    *,
    visibility: str = "private",
    owner: str = "alice",
    members: tuple[str, ...] = (),
    missing: bool = False,
) -> MagicMock:
    storage = MagicMock()
    if missing:
        storage.get_project.return_value = None
    else:
        storage.get_project.return_value = {
            "project_id": "p1",
            "name": "P1",
            "owner_id": owner,
            "visibility": visibility,
            "state": "active",
        }
    storage.is_project_member.side_effect = lambda pid, uid: uid in members
    return storage


class _FakeAuth:
    def __init__(
        self,
        user_id: str,
        scopes: tuple[str, ...] = (),
        permissions: tuple[str, ...] = (),
    ) -> None:
        self.user_id = user_id
        self._scopes = set(scopes)
        self._permissions = set(permissions)

    def has_scope(self, scope: str) -> bool:
        return scope in self._scopes

    def has_permission(self, permission: str) -> bool:
        return permission in self._permissions


def _request_for(
    uid: str,
    scopes: tuple[str, ...] = (),
    permissions: tuple[str, ...] = (),
) -> Any:
    return SimpleNamespace(state=SimpleNamespace(auth_result=_FakeAuth(uid, scopes, permissions)))


class TestWsVisiblePredicate:
    def test_no_project_always_visible(self) -> None:
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage())
        assert vis.ws_visible(None)
        assert vis.ws_visible("")

    def test_dangling_project_visible(self) -> None:
        # Project deletion leaves ws links behind — no row, no privacy.
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage(missing=True))
        assert vis.ws_visible("p1")

    def test_public_project_visible_to_anyone(self) -> None:
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage(visibility="public"))
        assert vis.ws_visible("p1")

    def test_private_hidden_from_non_member(self) -> None:
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage())
        assert not vis.ws_visible("p1")

    def test_private_visible_to_project_owner(self) -> None:
        vis = WorkstreamProjectVisibility("alice", storage=_fake_storage())
        assert vis.ws_visible("p1")

    def test_private_visible_to_member(self) -> None:
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage(members=("bob",)))
        assert vis.ws_visible("p1")

    def test_private_visible_to_ws_creator(self) -> None:
        # A workstream's own creator never loses sight of it, even after
        # a membership revoke leaves a legacy private-project link.
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage())
        assert vis.ws_visible("p1", ws_owner="bob")

    def test_private_hidden_from_anonymous(self) -> None:
        vis = WorkstreamProjectVisibility("", storage=_fake_storage())
        assert not vis.ws_visible("p1")

    def test_bypass_sees_everything(self) -> None:
        vis = WorkstreamProjectVisibility("bob", bypass=True, storage=_fake_storage())
        assert vis.ws_visible("p1")

    def test_storage_error_fails_closed(self) -> None:
        storage = MagicMock()
        storage.get_project.side_effect = RuntimeError("db down")
        vis = WorkstreamProjectVisibility("bob", storage=storage)
        assert not vis.ws_visible("p1")

    def test_project_rows_memoized(self) -> None:
        storage = _fake_storage(visibility="public")
        vis = WorkstreamProjectVisibility("bob", storage=storage)
        assert vis.ws_visible("p1")
        assert vis.ws_visible("p1")
        assert storage.get_project.call_count == 1

    def test_for_request_bypass_rules(self) -> None:
        assert WorkstreamProjectVisibility.for_request(
            _request_for("bob", scopes=("service",))
        )._bypass
        assert WorkstreamProjectVisibility.for_request(
            _request_for("bob", permissions=("admin.cluster.inspect",))
        )._bypass
        assert not WorkstreamProjectVisibility.for_request(_request_for("bob"))._bypass


class TestEnsureProjectAttachable:
    def test_no_project_allowed(self) -> None:
        assert ensure_project_attachable("bob", "", storage=_fake_storage()) is None

    def test_unknown_project_is_400(self) -> None:
        denied = ensure_project_attachable("bob", "p1", storage=_fake_storage(missing=True))
        assert denied is not None and denied[0] == 400

    def test_public_project_allowed(self) -> None:
        assert (
            ensure_project_attachable("bob", "p1", storage=_fake_storage(visibility="public"))
            is None
        )

    def test_private_member_and_owner_allowed(self) -> None:
        assert (
            ensure_project_attachable("bob", "p1", storage=_fake_storage(members=("bob",))) is None
        )
        assert ensure_project_attachable("alice", "p1", storage=_fake_storage()) is None

    def test_private_non_member_is_403(self) -> None:
        denied = ensure_project_attachable("bob", "p1", storage=_fake_storage())
        assert denied is not None and denied[0] == 403

    def test_anonymous_private_is_403(self) -> None:
        denied = ensure_project_attachable("", "p1", storage=_fake_storage())
        assert denied is not None and denied[0] == 403

    def test_storage_error_fails_closed(self) -> None:
        storage = MagicMock()
        storage.get_project.side_effect = RuntimeError("db down")
        denied = ensure_project_attachable("bob", "p1", storage=storage)
        assert denied is not None and denied[0] == 403


class TestResolveWorkstreamOwnerProjectGate:
    """Integration against the real (ephemeral) storage: the row-access
    gate every interactive ws-scoped verb inherits via tenant_check."""

    def _seed(self, *, member: bool) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.core.storage import get_storage

        storage = get_storage()
        storage.create_project("p1", "Secret", "alice")
        if member:
            storage.add_project_member("p1", "bob")
        register_workstream("ws-priv", user_id="alice", project_id="p1")

    def test_non_member_gets_403(self, tmp_db: str) -> None:
        from turnstone.core.web_helpers import resolve_workstream_owner

        self._seed(member=False)
        owner, err = resolve_workstream_owner(_request_for("bob"), "ws-priv")
        assert err is not None and err.status_code == 403

    def test_member_resolves_owner(self, tmp_db: str) -> None:
        from turnstone.core.web_helpers import resolve_workstream_owner

        self._seed(member=True)
        owner, err = resolve_workstream_owner(_request_for("bob"), "ws-priv")
        assert err is None
        assert owner == "alice"

    def test_ws_creator_bypasses(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.core.storage import get_storage
        from turnstone.core.web_helpers import resolve_workstream_owner

        storage = get_storage()
        storage.create_project("p1", "Secret", "alice")
        # bob created a ws in alice's private project, then lost access —
        # bob still reaches his own workstream.
        register_workstream("ws-bob", user_id="bob", project_id="p1")
        owner, err = resolve_workstream_owner(_request_for("bob"), "ws-bob")
        assert err is None
        assert owner == "bob"

    def test_admin_inspect_bypasses(self, tmp_db: str) -> None:
        from turnstone.core.web_helpers import resolve_workstream_owner

        self._seed(member=False)
        owner, err = resolve_workstream_owner(
            _request_for("bob", permissions=("admin.cluster.inspect",)), "ws-priv"
        )
        assert err is None
        assert owner == "alice"

    def test_missing_ws_still_404s(self, tmp_db: str) -> None:
        from turnstone.core.web_helpers import resolve_workstream_owner

        owner, err = resolve_workstream_owner(_request_for("bob"), "nope")
        assert err is not None and err.status_code == 404

    def test_public_project_ws_resolves(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.core.storage import get_storage
        from turnstone.core.web_helpers import resolve_workstream_owner

        storage = get_storage()
        storage.create_project("p1", "Open", "alice")
        storage.update_project("p1", visibility="public")
        register_workstream("ws-pub", user_id="alice", project_id="p1")
        owner, err = resolve_workstream_owner(_request_for("bob"), "ws-pub")
        assert err is None
        assert owner == "alice"


class TestSavedListFilter:
    """The saved-sessions collector drops private-project rows server-side
    and carries project_id on surviving rows (real ephemeral DB)."""

    async def test_saved_rows_filtered_and_carry_project_id(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream, save_message
        from turnstone.core.session_routes import (
            SessionEndpointConfig,
            _collect_saved_rows,
        )
        from turnstone.core.storage import get_storage
        from turnstone.core.workstream import WorkstreamKind

        storage = get_storage()
        storage.create_project("p1", "Secret", "alice")
        storage.create_project("p2", "Open", "alice")
        storage.update_project("p2", visibility="public")

        register_workstream("ws-plain", user_id="alice")
        register_workstream("ws-priv", user_id="alice", project_id="p1")
        register_workstream("ws-pub", user_id="alice", project_id="p2")
        register_workstream("ws-own", user_id="bob", project_id="p1")
        for wid in ("ws-plain", "ws-priv", "ws-pub", "ws-own"):
            save_message(wid, "user", "hello")

        cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda request: (None, None),
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            list_kind=WorkstreamKind.INTERACTIVE,
            saved_state_filter=None,
            saved_loaded_lookup=None,
        )

        rows = await _collect_saved_rows(cfg, _request_for("bob"))
        ids = {r["ws_id"] for r in rows}
        # bob: no membership in p1 — alice's private ws is dropped; the
        # public-project ws, the project-less ws, and bob's own
        # private-project ws all survive.
        assert ids == {"ws-plain", "ws-pub", "ws-own"}
        by_id = {r["ws_id"]: r for r in rows}
        assert by_id["ws-pub"]["project_id"] == "p2"
        assert by_id["ws-plain"]["project_id"] is None

        rows_alice = await _collect_saved_rows(cfg, _request_for("alice"))
        assert {r["ws_id"] for r in rows_alice} == {"ws-plain", "ws-priv", "ws-pub", "ws-own"}
