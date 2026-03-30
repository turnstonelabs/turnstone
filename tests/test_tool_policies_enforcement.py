"""Tests for tool policy enforcement in the CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from turnstone.cli import TerminalUI

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
                "turnstone.core.policy.evaluate_tool_policies_batch",
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
                "turnstone.core.policy.evaluate_tool_policies_batch",
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
