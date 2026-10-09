"""Tests for tool policy enforcement in the approval gates: the CLI's and the shared one."""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import resolve_when_pending
from turnstone.cli import TerminalUI
from turnstone.core.policy import POLICIES_UNREADABLE_DENIAL
from turnstone.server import WebUI

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLIPolicyEnforcement:
    """Tool policies should be enforced in CLI approve_tools()."""

    def _make_items(self, *tool_names: str) -> list[dict]:
        return [
            {
                "call_id": f"call_{i}",
                "header": f"Tool: {name}",
                "preview": "",
                "func_name": name,
                "approval_label": name,
                "needs_approval": True,
            }
            for i, name in enumerate(tool_names)
        ]

    def test_deny_policy_blocks_tool(self):
        """A 'deny' policy verdict should block the tool without prompting."""
        ui = TerminalUI()
        items = self._make_items("bash")

        with (
            patch(
                "turnstone.core.policy.evaluate_loaded_tool_policies",
                return_value={"bash": "deny"},
            ),
            patch(
                "turnstone.core.storage._registry.get_storage",
                return_value=MagicMock(),
            ),
        ):
            approved, _ = ui.approve_tools(items)

        assert items[0].get("denied") is True
        assert items[0].get("error")
        assert "policy" in items[0]["error"].lower()

    def test_allow_policy_auto_approves(self):
        """An 'allow' policy verdict should auto-approve without prompting."""
        ui = TerminalUI()
        items = self._make_items("read_file")

        with (
            patch(
                "turnstone.core.policy.evaluate_loaded_tool_policies",
                return_value={"read_file": "allow"},
            ),
            patch(
                "turnstone.core.storage._registry.get_storage",
                return_value=MagicMock(),
            ),
        ):
            approved, _ = ui.approve_tools(items)

        assert approved is True

    def test_no_storage_skips_policies(self):
        """When storage is unavailable, policies are skipped (best-effort)."""
        ui = TerminalUI()
        items = self._make_items("bash")

        with (
            patch(
                "turnstone.core.storage._registry.get_storage",
                return_value=None,
            ),
            patch("builtins.input", return_value="y"),
        ):
            approved, _ = ui.approve_tools(items)

        # Should fall through to normal prompt (which we answered 'y')
        assert approved is True

    def test_unreadable_policies_refuse_every_call_even_under_skip_permissions(self):
        ui = TerminalUI()
        ui.auto_approve = True
        items = self._make_items("bash")

        with (
            patch("turnstone.core.policy.evaluate_loaded_tool_policies", return_value=None),
            patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
            patch("builtins.input", side_effect=AssertionError("prompted")),
        ):
            ui.approve_tools(items)

        assert items[0]["denied"] is True
        assert items[0]["error"] == POLICIES_UNREADABLE_DENIAL

    def test_a_policy_evaluation_that_raises_refuses_too(self):
        ui = TerminalUI()
        ui.auto_approve = True
        items = self._make_items("bash")

        with patch("turnstone.core.storage._registry.get_storage", side_effect=OSError("disk")):
            ui.approve_tools(items)

        assert items[0]["error"] == POLICIES_UNREADABLE_DENIAL

    def test_auto_approve_list_covering_every_call_skips_the_prompt(self):
        ui = TerminalUI()
        ui.auto_approve_tools = {"bash", "read_file"}
        items = self._make_items("bash", "read_file")

        with (
            patch("turnstone.core.policy.evaluate_loaded_tool_policies", return_value={}),
            patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
            patch("builtins.input", side_effect=AssertionError("prompted")),
        ):
            approved, _ = ui.approve_tools(items)

        assert approved is True

    def test_auto_approve_list_missing_a_call_still_prompts(self):
        ui = TerminalUI()
        ui.auto_approve_tools = {"read_file"}
        items = self._make_items("bash", "read_file")

        with (
            patch("turnstone.core.policy.evaluate_loaded_tool_policies", return_value={}),
            patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
            patch("builtins.input", return_value="n") as prompt,
        ):
            approved, _ = ui.approve_tools(items)

        prompt.assert_called_once()
        assert approved is False


# ---------------------------------------------------------------------------
# Shared gate (node and coordinator sessions)
# ---------------------------------------------------------------------------


@pytest.fixture
def web_ui() -> Iterator[WebUI]:
    WebUI._global_queue = queue.Queue()
    yield WebUI(ws_id="ws-policy")
    WebUI._global_queue = None


def _bash_and_a_read() -> list[dict[str, Any]]:
    """A call that needs approval and one that does not."""
    return [
        {
            "call_id": f"c-{name}",
            "header": f"Tool: {name}",
            "preview": "",
            "func_name": name,
            "approval_label": name,
            "needs_approval": needs_approval,
        }
        for name, needs_approval in (("bash", True), ("read_file", False))
    ]


@pytest.mark.parametrize(
    "approval", ["a person", "skip-permissions", "auto-approve tools", "unattended grant", "judge"]
)
def test_unreadable_policies_refuse_every_call_needing_approval(web_ui: WebUI, approval: str):
    """No deny rule could be checked, so nothing needing approval runs, on any approval."""
    if approval == "skip-permissions":
        web_ui.auto_approve = True
    elif approval == "auto-approve tools":
        web_ui.auto_approve_tools = {"bash"}
    elif approval == "unattended grant":
        assert web_ui.grant_unattended()
    elif approval == "judge":
        web_ui.smart_approvals_enabled = True
    items = _bash_and_a_read()
    prompt = resolve_when_pending(web_ui, True)  # a person would approve a prompt
    prompt.start()
    try:
        with (
            patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
            patch("turnstone.core.policy.evaluate_loaded_tool_policies", return_value=None),
            patch.object(WebUI, "_apply_smart_approvals", side_effect=AssertionError("judged")),
        ):
            approved, reason = web_ui.approve_tools(items)
    finally:
        prompt.cancel()

    assert (approved, reason) == (False, "Tool policies could not be read")
    bash, read_file = items
    assert (bash["denied"], bash["denial_msg"]) == (True, POLICIES_UNREADABLE_DENIAL)
    assert bash["_refused_by"] == "policy"  # no person rejected it: no denial nudge
    assert not read_file.get("denied")  # needs no approval: the session still runs it
    assert web_ui.serialize_recent_auto_approvals() == []


def test_a_policy_evaluation_that_raises_refuses_the_same_way(web_ui: WebUI):
    web_ui.auto_approve = True
    items = _bash_and_a_read()

    with patch("turnstone.core.storage._registry.get_storage", side_effect=OSError("disk")):
        approved, _reason = web_ui.approve_tools(items)

    assert approved is False
    assert items[0]["denial_msg"] == POLICIES_UNREADABLE_DENIAL


def test_a_storage_that_cannot_list_policies_refuses_end_to_end(web_ui: WebUI):
    """Nothing patched in the policy module: the real cached read fails, the gate refuses."""

    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("database unavailable")

    web_ui.auto_approve = True
    items = _bash_and_a_read()

    with patch("turnstone.core.storage._registry.get_storage", return_value=BrokenStorage()):
        approved, reason = web_ui.approve_tools(items)

    assert (approved, reason) == (False, "Tool policies could not be read")
    assert items[0]["denial_msg"] == POLICIES_UNREADABLE_DENIAL


def test_a_deny_rule_marks_its_refusal_as_policy(web_ui: WebUI):
    items = _bash_and_a_read()

    with (
        patch("turnstone.core.storage._registry.get_storage", return_value=MagicMock()),
        patch(
            "turnstone.core.policy.evaluate_loaded_tool_policies",
            return_value={"bash": "deny"},
        ),
    ):
        approved, reason = web_ui.approve_tools(items)

    assert (approved, reason) == (False, "Blocked by tool policy")
    assert items[0]["_refused_by"] == "policy"


# ---------------------------------------------------------------------------
# Channel router (a second check, after the node's gate asked a person)
# ---------------------------------------------------------------------------


def _router_verdict(storage: Any) -> Any:
    import asyncio

    from turnstone.channels._routing import ChannelRouter

    router = ChannelRouter("http://server.example", storage)
    return asyncio.run(router.evaluate_tool_policies(_bash_and_a_read()))


def test_the_channel_router_defers_on_unreadable_policies():
    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("database unavailable")

    verdict = _router_verdict(BrokenStorage())

    assert (verdict.kind, verdict.tool_names) == ("defer", ["bash"])


def test_the_channel_router_applies_a_readable_deny(tmp_path: Any):
    from turnstone.core.storage._sqlite import SQLiteBackend

    storage = SQLiteBackend(str(tmp_path / "policies.db"))
    try:
        storage.create_tool_policy("p1", "block-bash", "bash", "deny", 0)
        verdict = _router_verdict(storage)
    finally:
        storage.close()

    assert (verdict.kind, verdict.denied_tools) == ("deny", ["bash"])
