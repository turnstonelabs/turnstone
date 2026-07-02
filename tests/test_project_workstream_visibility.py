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


class TestTriStateVisibility:
    def test_undetermined_on_storage_error(self) -> None:
        storage = MagicMock()
        storage.get_project.side_effect = RuntimeError("db down")
        vis = WorkstreamProjectVisibility("bob", storage=storage)
        assert vis.ws_visibility("p1") is None
        # The boolean form stays fail-closed.
        assert vis.ws_visible("p1") is False

    def test_definitive_verdicts(self) -> None:
        assert (
            WorkstreamProjectVisibility("bob", storage=_fake_storage(visibility="public"))
            .ws_visibility("p1")
            is True
        )
        assert WorkstreamProjectVisibility("bob", storage=_fake_storage()).ws_visibility("p1") is False


class _ScriptedVis:
    """ws_visibility stub: per-pid verdict, or a list consumed per call."""

    def __init__(self, verdicts: dict, bypass: bool = False) -> None:
        self.verdicts = dict(verdicts)
        self.bypass = bypass
        self.calls = 0

    def ws_visibility(self, pid, ws_owner=""):
        self.calls += 1
        v = self.verdicts.get(pid or "", True)
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v


class TestClusterTenancyFilter:
    def _snap(self):
        return {
            "nodes": [
                {
                    "node_id": "node-a",
                    "workstreams": [
                        {"ws_id": "w-vis", "state": "running", "project_id": "", "user_id": "a"},
                        {"ws_id": "w-priv", "state": "running", "project_id": "ph", "user_id": "a"},
                    ],
                }
            ],
            "overview": {
                "nodes": 1,
                "workstreams": 2,
                "states": {"running": 2, "thinking": 0, "idle": 0},
            },
        }

    def test_snapshot_filters_rows_and_rederives_overview(self) -> None:
        from turnstone.console.server import _ClusterTenancyFilter

        filt = _ClusterTenancyFilter(_ScriptedVis({"ph": False}))
        snap = filt.filter_snapshot(self._snap())
        assert [w["ws_id"] for w in snap["nodes"][0]["workstreams"]] == ["w-vis"]
        # Overview no longer leaks the hidden row's existence or state.
        assert snap["overview"]["workstreams"] == 1
        assert snap["overview"]["states"] == {"running": 1, "thinking": 0, "idle": 0}
        # Later sparse events for the hidden ws are suppressed.
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w-priv"}) is False
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w-vis"}) is True

    def test_bypass_leaves_snapshot_untouched(self) -> None:
        from turnstone.console.server import _ClusterTenancyFilter

        filt = _ClusterTenancyFilter(_ScriptedVis({"ph": False}, bypass=True))
        snap = filt.filter_snapshot(self._snap())
        assert len(snap["nodes"][0]["workstreams"]) == 2
        assert snap["overview"]["workstreams"] == 2  # collector aggregate preserved
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w-priv"}) is True
        assert filt.event_touches_storage({"type": "ws_created", "ws_id": "x"}) is False

    def test_ws_created_judged_and_closed_cleans_up(self) -> None:
        from turnstone.console.server import _ClusterTenancyFilter

        filt = _ClusterTenancyFilter(_ScriptedVis({"ph": False}))
        created = {"type": "ws_created", "ws_id": "w1", "project_id": "ph", "user_id": "b"}
        assert filt.event_visible(created) is False
        assert filt.event_visible({"type": "ws_rename", "ws_id": "w1"}) is False
        # The close of a never-shown workstream is itself suppressed…
        assert filt.event_visible({"type": "ws_closed", "ws_id": "w1"}) is False
        # …and the state is cleaned, so an unrelated later event passes.
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w1"}) is True

    def test_undetermined_suppresses_then_retries(self) -> None:
        from turnstone.console.server import _ClusterTenancyFilter

        vis = _ScriptedVis({"pu": [None, True]})
        filt = _ClusterTenancyFilter(vis)
        created = {"type": "ws_created", "ws_id": "w1", "project_id": "pu", "user_id": "b"}
        # Storage blip: suppressed but NOT pinned hidden.
        assert filt.event_visible(created) is False
        assert "w1" in filt._unresolved
        # Within the retry interval later events stay suppressed without
        # re-hitting storage.
        calls_before = vis.calls
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w1"}) is False
        assert vis.calls == calls_before
        # Past the interval the row is re-judged and recovers.
        filt._RETRY_INTERVAL_S = 0.0
        filt._retry_after["w1"] = 0.0
        assert filt.event_touches_storage({"type": "cluster_state", "ws_id": "w1"}) is True
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w1"}) is True
        assert "w1" not in filt._unresolved

    def test_denied_verdict_pins_hidden(self) -> None:
        from turnstone.console.server import _ClusterTenancyFilter

        vis = _ScriptedVis({"pu": [None, False]})
        filt = _ClusterTenancyFilter(vis)
        filt._RETRY_INTERVAL_S = 0.0
        assert (
            filt.event_visible(
                {"type": "ws_created", "ws_id": "w1", "project_id": "pu", "user_id": "b"}
            )
            is False
        )
        filt._retry_after["w1"] = 0.0
        assert filt.event_visible({"type": "cluster_state", "ws_id": "w1"}) is False
        assert "w1" in filt._hidden and "w1" not in filt._unresolved


class TestCreateValidatorProjectGate:
    """The interactive create validator's attach gate: explicit ids are
    strict, inherited ids tolerate a deleted project (real ephemeral DB)."""

    async def test_inherited_dangling_project_is_stripped(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.server import _interactive_create_validate_request

        register_workstream(
            "coord-1", user_id="alice", kind="coordinator", project_id="p-gone"
        )
        body: dict = {"kind": "interactive", "parent_ws_id": "coord-1"}
        err = await _interactive_create_validate_request(MagicMock(), body, "alice", [])
        assert err is None
        assert (body.get("project_id") or "") == ""

    async def test_explicit_unknown_project_still_400s(self, tmp_db: str) -> None:
        from turnstone.server import _interactive_create_validate_request

        body: dict = {"kind": "interactive", "project_id": "nope"}
        err = await _interactive_create_validate_request(MagicMock(), body, "alice", [])
        assert err is not None and err.status_code == 400

    async def test_inherited_private_revoked_membership_403s(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.core.storage import get_storage
        from turnstone.server import _interactive_create_validate_request

        get_storage().create_project("p-priv", "P", "zed")
        register_workstream(
            "coord-2", user_id="alice", kind="coordinator", project_id="p-priv"
        )
        body: dict = {"kind": "interactive", "parent_ws_id": "coord-2"}
        err = await _interactive_create_validate_request(MagicMock(), body, "alice", [])
        assert err is not None and err.status_code == 403

    async def test_inherited_accessible_project_passes(self, tmp_db: str) -> None:
        from turnstone.core.memory import register_workstream
        from turnstone.core.storage import get_storage
        from turnstone.server import _interactive_create_validate_request

        storage = get_storage()
        storage.create_project("p-ok", "P", "zed")
        storage.add_project_member("p-ok", "alice")
        register_workstream("coord-3", user_id="alice", kind="coordinator", project_id="p-ok")
        body: dict = {"kind": "interactive", "parent_ws_id": "coord-3"}
        err = await _interactive_create_validate_request(MagicMock(), body, "alice", [])
        assert err is None
        assert body["project_id"] == "p-ok"


class TestSavedListPagination:
    """The saved-list collector pages past invisible rows instead of
    letting a post-SQL filter shrink the window."""

    def _row(self, i: int, project_id: str | None) -> tuple:
        return (
            f"ws-{i:03d}",
            None,
            None,
            f"n{i}",
            "2026-01-01T00:00:00",
            f"{99999 - i}",  # updated: descending with i
            1,
            "node-a",
            "idle",
            "interactive",
            None,
            None,
            0,
            0,
            None,
            project_id,
            "alice",
        )

    def _cfg(self):
        from turnstone.core.session_routes import SessionEndpointConfig
        from turnstone.core.workstream import WorkstreamKind

        return SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda request: (None, None),
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            list_kind=WorkstreamKind.INTERACTIVE,
            saved_state_filter=None,
            saved_loaded_lookup=None,
        )

    def _patch(self, monkeypatch: pytest.MonkeyPatch, rows: list) -> None:
        def _fake(limit=20, *, kind=None, user_id=None, state=None, offset=0):
            return rows[offset : offset + limit]

        monkeypatch.setattr("turnstone.core.memory.list_workstreams_with_history", _fake)
        vis = WorkstreamProjectVisibility("bob", storage=_fake_storage())  # denies any pid
        monkeypatch.setattr(
            WorkstreamProjectVisibility,
            "for_request",
            classmethod(lambda cls, request, storage=None: vis),
        )

    async def test_pages_past_invisible_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from turnstone.core.session_routes import _collect_saved_rows

        rows = [self._row(i, "ph") for i in range(60)] + [
            self._row(i, None) for i in range(60, 130)
        ]
        self._patch(monkeypatch, rows)
        result = await _collect_saved_rows(self._cfg(), MagicMock())
        assert len(result) == 50
        assert result[0]["ws_id"] == "ws-060"
        assert result[-1]["ws_id"] == "ws-109"

    async def test_scan_cap_terminates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from turnstone.core.session_routes import _collect_saved_rows

        rows = [self._row(i, "ph") for i in range(5000)]
        self._patch(monkeypatch, rows)
        result = await _collect_saved_rows(self._cfg(), MagicMock())
        assert result == []
