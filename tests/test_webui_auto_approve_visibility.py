"""Tests for the policy + auto-approve recording paths in WebUI.approve_tools.

The visibility patch added a ring buffer (_recent_auto_approvals) and an
audit emit for every tool call that bypasses the operator approval gate.
The fall-through point at the end of approve_tools handles the common
"all auto-approved" path, but two policy-resolution branches need
explicit recording calls or the policy bypass is invisible to /dashboard:

1. **Early-return-on-deny** — policy resolves every item, some are
   denied AND some are allowed.  The early return emits ``tool_info``
   without falling through to the recording site.
2. **Partial resolve** — policy allows some items but ``still_pending``
   remains non-empty.  The auto-approve-tools / blanket branches don't
   match (no ``auto_approve_tools`` / no blanket flag), so the prompt
   path fires WITHOUT visiting the recording site.

Both leaks let the policy bypass slip past the dashboard pill silently —
exactly the case the visibility fix is meant to surface.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.server import WebUI


@pytest.fixture(autouse=True)
def _global_queue():
    """Reset the WebUI shared queue around each test."""
    WebUI._global_queue = queue.Queue()
    yield
    WebUI._global_queue = None


def _make_items(*specs: tuple[str, str]) -> list[dict[str, Any]]:
    """Build approval items.  Each spec is ``(call_id, func_name)``."""
    return [
        {
            "call_id": call_id,
            "header": f"Tool: {func}",
            "preview": "",
            "func_name": func,
            "approval_label": func,
            "needs_approval": True,
        }
        for call_id, func in specs
    ]


def _patch_storage(storage: Any):
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def _patch_policies(verdicts: dict[str, str]):
    return patch(
        "turnstone.core.policy.evaluate_tool_policies_batch",
        return_value=verdicts,
    )


# ---------------------------------------------------------------------------
# LEAK 1 — early-return-on-deny path
# ---------------------------------------------------------------------------


def test_policy_mixed_allow_deny_records_allowed_items() -> None:
    """When policy resolves every item and at least one is denied, the
    early return must still record the policy-allowed siblings —
    pre-fix the line-325 fall-through never ran on this path, leaving
    the policy bypass invisible to /dashboard + audit."""
    ui = WebUI(ws_id="ws-test")
    items = _make_items(("c1", "bash"), ("c2", "read_file"))

    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({"bash": "deny", "read_file": "allow"}):
        approved, _err = ui.approve_tools(items)

    # Block: at least one tool was denied, so approve_tools returns False.
    assert approved is False
    # The policy-allowed item is now visible on /dashboard via the buffer.
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "read_file"
    assert snapshot[0]["auto_approve_reason"] == "policy"
    # And persisted to audit so the operator has a forensic trail.
    storage.record_audit_event.assert_called_once()
    audit_kwargs = storage.record_audit_event.call_args.kwargs
    assert audit_kwargs["action"] == "tool.auto_approved"


# ---------------------------------------------------------------------------
# LEAK 2 — policy-partial-resolve falls through to the prompt path
# ---------------------------------------------------------------------------


def test_policy_partial_allow_then_prompt_records_allowed_items() -> None:
    """Policy allows one tool but another still needs operator approval —
    falls through to the prompt path with ``pending`` non-empty and no
    blanket auto_approve.  The line-325 record never fires; the new
    pre-prompt record call is what surfaces the policy bypass."""
    ui = WebUI(ws_id="ws-test")
    items = _make_items(("c1", "read_file"), ("c2", "bash"))

    # ``approve_tools`` blocks on ``_approval_event.wait`` for the
    # prompt path.  Schedule a deny-by-operator on a tiny timer so
    # the wait returns promptly; this test asserts on ring-buffer
    # state, not the verdict outcome, so a deny is fine.
    timer = threading.Timer(0.05, lambda: ui.resolve_approval(False))
    timer.start()

    storage = MagicMock()
    try:
        with _patch_storage(storage), _patch_policies({"read_file": "allow"}):
            # bash gets no policy verdict → falls into still_pending → prompt.
            ui.approve_tools(items)
    finally:
        timer.cancel()

    # The policy-allowed read_file is captured in the buffer despite
    # the prompt path running — this is the leak fix.
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["func_name"] == "read_file"
    assert snapshot[0]["auto_approve_reason"] == "policy"
    # Audit row recorded on the prompt path too.
    storage.record_audit_event.assert_called_once()


# ---------------------------------------------------------------------------
# Regression — existing fall-through path still records
# ---------------------------------------------------------------------------


def test_policy_all_allow_no_deny_records_via_fallthrough() -> None:
    """Sanity check on the line-325 fall-through path so the leak
    fixes aren't masking a regression of the existing behaviour."""
    ui = WebUI(ws_id="ws-test")
    items = _make_items(("c1", "read_file"), ("c2", "list_dir"))

    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({"read_file": "allow", "list_dir": "allow"}):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 2
    assert {entry["func_name"] for entry in snapshot} == {"read_file", "list_dir"}
    for entry in snapshot:
        assert entry["auto_approve_reason"] == "policy"


def test_blanket_auto_approve_records_pending_items() -> None:
    """``auto_approve=True`` (blanket flag) drains every pending item —
    each gets tagged with reason='blanket' and recorded.  Sanity check
    on the blanket branch's tag + record discipline."""
    ui = WebUI(ws_id="ws-test")
    ui.auto_approve = True
    items = _make_items(("c1", "bash"), ("c2", "edit_file"))

    storage = MagicMock()
    with _patch_storage(storage):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 2
    for entry in snapshot:
        assert entry["auto_approve_reason"] == "blanket"


def test_auto_approve_tools_skill_source_renders_as_skill() -> None:
    """When the workstream's auto_approve_tools were populated by a
    skill template, the per-tool source map records ``skill`` and the
    ring-buffer entry surfaces the same — this is the exact path the
    user flagged ('child workstreams occasionally getting approved
    without prompting because of a parent skill's allowlist')."""
    ui = WebUI(ws_id="ws-test")
    ui.auto_approve_tools = {"bash"}
    ui._auto_approve_tools_source = {"bash": "skill"}
    items = _make_items(("c1", "bash"))

    storage = MagicMock()
    with _patch_storage(storage):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1
    assert snapshot[0]["auto_approve_reason"] == "skill"


def test_no_auto_approve_no_pending_recording() -> None:
    """An items list of read-only tools (every entry already has
    ``needs_approval=False``) must NOT enter the ring buffer — those
    aren't bypasses, they're tools that never required approval in
    the first place.  Buffer growth is reserved for actual gate
    bypasses."""
    ui = WebUI(ws_id="ws-test")
    items = [
        {
            "call_id": "c1",
            "header": "Tool: read_file",
            "preview": "",
            "func_name": "read_file",
            "approval_label": "read_file",
            "needs_approval": False,  # read-only — never needed approval
        }
    ]
    storage = MagicMock()
    with _patch_storage(storage):
        approved, _err = ui.approve_tools(items)

    assert approved is True
    assert ui.serialize_recent_auto_approvals() == []
    storage.record_audit_event.assert_not_called()
