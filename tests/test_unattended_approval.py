"""A watch restore's unattended approval (#988).

Watch restore opens a workstream for the watch alone and auto-approves its tool
batches, so the delivery never blocks on a prompt nobody sees. That grant is
kept apart from skip-permissions and ends, for good, at the first client that
attaches to or starts work in the workstream.
"""

from __future__ import annotations

import ast
import queue
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from tests.conftest import resolve_when_pending
from turnstone.core import session_worker
from turnstone.core.auth import AuthResult
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_approve_handler,
    make_cancel_handler,
    make_detail_handler,
    make_open_handler,
)
from turnstone.core.workstream import Workstream
from turnstone.server import WebUI


@pytest.fixture(autouse=True)
def _global_queue():
    WebUI._global_queue = queue.Queue()
    yield
    WebUI._global_queue = None


def _approve(ui: WebUI, *, reject_prompt: bool = False) -> tuple[bool, Any]:
    items = [
        {
            "call_id": "c1",
            "header": "Tool: bash",
            "preview": "",
            "func_name": "bash",
            "approval_label": "bash",
            "needs_approval": True,
        }
    ]
    timer = resolve_when_pending(ui, False) if reject_prompt else None
    if timer is not None:
        timer.start()
    try:
        with patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()):
            return ui.approve_tools(items)
    finally:
        if timer is not None:
            timer.cancel()


def test_an_unattended_grant_approves_and_says_why() -> None:
    ui = WebUI(ws_id="ws-u")
    assert ui.grant_unattended() is True

    approved, _err = _approve(ui)

    assert approved is True
    [entry] = ui.serialize_recent_auto_approvals()
    assert entry["auto_approve_reason"] == "unattended_watch"


def test_skip_permissions_still_reads_as_blanket() -> None:
    ui = WebUI(ws_id="ws-u")
    ui.auto_approve = True
    ui.grant_unattended()

    _approve(ui)

    [entry] = ui.serialize_recent_auto_approvals()
    assert entry["auto_approve_reason"] == "blanket"


def _approve_under_policy(
    ui: WebUI, verdicts: dict[str, str], *, resolve: bool | None
) -> tuple[bool, list[dict[str, Any]]]:
    """Approve a ``bash`` + ``read_file`` batch with those policy verdicts.

    ``resolve`` answers a prompt (``None``: no prompt expected).
    """
    items = [
        {
            "call_id": f"c-{name}",
            "header": f"Tool: {name}",
            "preview": "",
            "func_name": name,
            "approval_label": name,
            "needs_approval": True,
        }
        for name in ("bash", "read_file")
    ]
    timer = resolve_when_pending(ui, resolve) if resolve is not None else None
    if timer is not None:
        timer.start()
    try:
        with (
            patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
            patch(
                "turnstone.core.policy.evaluate_loaded_tool_policies",
                side_effect=lambda _storage, names, *a, **k: {n: verdicts.get(n) for n in names},
            ),
        ):
            approved, _err = ui.approve_tools(items)
    finally:
        if timer is not None:
            timer.cancel()
    return approved, items


@pytest.mark.parametrize("resolve", [True, False])
def test_an_ask_policy_waits_for_a_person_during_an_unattended_turn(resolve: bool) -> None:
    ui = WebUI(ws_id="ws-u")
    ui.grant_unattended()

    approved, items = _approve_under_policy(ui, {"bash": "ask"}, resolve=resolve)

    assert approved is resolve  # the person's answer decides
    bash, read_file = items
    assert not bash.get("auto_approved")
    # The unmatched call still runs on the grant, without crossing the gate.
    assert read_file["auto_approve_reason"] == "unattended_watch"
    assert read_file["needs_approval"] is False


def test_skip_permissions_still_overrides_an_ask_policy() -> None:
    ui = WebUI(ws_id="ws-u")
    ui.auto_approve = True
    ui.grant_unattended()

    approved, items = _approve_under_policy(ui, {"bash": "ask"}, resolve=None)

    assert approved is True
    assert [item["auto_approve_reason"] for item in items] == ["blanket", "blanket"]


def test_a_deny_policy_still_blocks_an_unattended_turn() -> None:
    ui = WebUI(ws_id="ws-u")
    ui.grant_unattended()

    _approved, items = _approve_under_policy(ui, {"bash": "deny"}, resolve=None)

    bash, read_file = items
    assert bash["denied"] is True
    assert read_file["auto_approve_reason"] == "unattended_watch"


def test_the_first_client_ends_the_grant_for_good() -> None:
    ui = WebUI(ws_id="ws-u")
    assert ui.grant_unattended()
    ui.note_client()

    approved, _err = _approve(ui, reject_prompt=True)

    assert approved is False  # the batch prompted, and the "human" rejected it
    assert ui.serialize_recent_auto_approvals() == []
    assert ui.grant_unattended() is False  # never re-armed
    assert ui.client_seen()


def test_a_client_seen_before_the_grant_refuses_it() -> None:
    """The restore marks only after open returns; a client that got there first wins."""
    ui = WebUI(ws_id="ws-u")
    ui.note_client()

    assert ui.grant_unattended() is False
    assert ui._unattended is False


def test_attaching_a_listener_ends_the_grant() -> None:
    ui = WebUI(ws_id="ws-u")
    assert ui.grant_unattended()

    ui._register_listener()

    assert ui._unattended is False and ui.client_seen()


def test_the_replay_preamble_never_reports_the_grant_as_skip_permissions() -> None:
    ui = WebUI(ws_id="ws-u")
    ui.grant_unattended()

    assert ui.auto_approve is False


@pytest.mark.parametrize("attended", [True, False])
def test_client_dispatch_ends_the_grant_and_a_wake_keeps_it(attended: bool) -> None:
    ui = WebUI(ws_id="ws-u")
    ui.grant_unattended()
    ws = Workstream(id="ws-u", ui=ui)

    assert session_worker.send(ws, enqueue=lambda: None, run=lambda: None, attended=attended)
    if ws.worker_thread is not None:
        ws.worker_thread.join(2)

    assert ui._unattended is (not attended)


def test_only_the_worker_dispatch_claims_a_worker() -> None:
    """``session_worker.send`` is the one place that sets ``_worker_running``, so its
    client hook sees every client turn."""
    root = Path(__file__).resolve().parents[1] / "turnstone"
    claimers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute) and target.attr == "_worker_running"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            ):
                claimers.add(path.relative_to(root).as_posix())
    assert claimers == {"core/session_worker.py"}


# -- a client reaching the workstream over HTTP ---------------------------------


class _Auth(BaseHTTPMiddleware):
    async def dispatch(self, request: Any, call_next: Any) -> Any:
        scopes = frozenset({"read", "write", "approve"})
        request.state.auth_result = AuthResult(
            user_id="owner", scopes=scopes, token_source="config", permissions=scopes
        )
        return await call_next(request)


def _client(*, loaded: bool) -> tuple[TestClient, MagicMock, WebUI]:
    """The approve, cancel, open and detail routes over a manager holding a watch-restored
    workstream.

    ``loaded`` says whether the manager already serves the slot; otherwise the
    routes open it.
    """
    ui = WebUI(ws_id="ws-u", user_id="owner")
    assert ui.grant_unattended()
    ws = Workstream(id="ws-u", name="watched", ui=ui, user_id="owner")
    mgr = MagicMock()
    mgr.get.return_value = ws
    mgr.loaded.return_value = ws if loaded else None
    mgr.open_with_outcome.return_value = (ws, True)
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _r: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
    )
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/approve",
                        make_approve_handler(cfg),
                        methods=["POST"],
                    ),
                    Route(
                        "/api/workstreams/{ws_id}/open", make_open_handler(cfg), methods=["POST"]
                    ),
                    Route(
                        "/api/workstreams/{ws_id}/cancel",
                        make_cancel_handler(cfg),
                        methods=["POST"],
                    ),
                    Route("/api/workstreams/{ws_id}", make_detail_handler(cfg), methods=["GET"]),
                ],
            )
        ],
        middleware=[Middleware(_Auth)],
    )
    app.state.workstreams = mgr
    app.state.global_queue = queue.Queue()
    return TestClient(app), mgr, ui


def test_stopping_the_workstream_ends_the_grant() -> None:
    client, _mgr, ui = _client(loaded=True)

    client.post("/v1/api/workstreams/ws-u/cancel", json={})

    assert ui._unattended is False and ui.client_seen()


def test_an_approval_click_ends_the_grant() -> None:
    client, _mgr, ui = _client(loaded=True)

    client.post("/v1/api/workstreams/ws-u/approve", json={"approved": False})

    assert ui._unattended is False and ui.client_seen()


@pytest.mark.parametrize("loaded", [True, False], ids=["already-loaded", "opened-here"])
def test_opening_the_workstream_ends_the_grant(loaded: bool) -> None:
    client, mgr, ui = _client(loaded=loaded)

    r = client.post("/v1/api/workstreams/ws-u/open")

    assert r.status_code == 200
    assert mgr.open_with_outcome.called is (not loaded)
    assert ui._unattended is False and ui.client_seen()


@pytest.mark.parametrize("loaded", [True, False], ids=["already-loaded", "opened-here"])
def test_viewing_the_workstream_ends_the_grant(loaded: bool) -> None:
    client, mgr, ui = _client(loaded=loaded)

    r = client.get("/v1/api/workstreams/ws-u")

    assert r.status_code == 200
    assert mgr.open_with_outcome.called is (not loaded)
    assert ui._unattended is False and ui.client_seen()
