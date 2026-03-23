"""Tests for tool policy enforcement across CLI, bridge, and channel entry points."""

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


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class TestBridgePolicyEnforcement:
    """Tool policies should be enforced in bridge _handle_approval()."""

    def _make_bridge(self):
        from turnstone.mq.bridge import Bridge

        broker = MagicMock()
        return Bridge(
            server_url="http://localhost:8080",
            broker=broker,
            node_id="test-node",
            approval_timeout=1,
        )

    def _approval_items(self, *tool_names: str) -> list[dict]:
        return [
            {"func_name": name, "needs_approval": True, "approval_label": name}
            for name in tool_names
        ]

    def test_deny_policy_rejects_approval(self):
        """A 'deny' policy should reject the approval."""
        bridge = self._make_bridge()

        with (
            patch(
                "turnstone.core.policy.evaluate_tool_policies_batch",
                return_value={"bash": "deny"},
            ),
            patch(
                "turnstone.core.storage._registry._storage",
                new=MagicMock(),
            ),
            patch.object(bridge, "_api_approve") as mock_approve,
            patch.object(bridge, "_publish_ws"),
        ):
            bridge._handle_approval("ws-1", {"items": self._approval_items("bash")})

        mock_approve.assert_called_once()
        assert mock_approve.call_args.kwargs.get("approved") is False

    def test_allow_policy_approves(self):
        """An 'allow' policy should auto-approve."""
        bridge = self._make_bridge()

        with (
            patch(
                "turnstone.core.policy.evaluate_tool_policies_batch",
                return_value={"read_file": "allow"},
            ),
            patch(
                "turnstone.core.storage._registry.get_storage",
                return_value=MagicMock(),
            ),
            patch.object(bridge, "_api_approve") as mock_approve,
            patch.object(bridge, "_publish_ws"),
        ):
            bridge._handle_approval("ws-1", {"items": self._approval_items("read_file")})

        mock_approve.assert_called_once()
        assert mock_approve.call_args.kwargs.get("approved") is True

    def test_mixed_deny_rejects_batch(self):
        """If any tool is denied, the whole batch is rejected."""
        bridge = self._make_bridge()

        with (
            patch(
                "turnstone.core.policy.evaluate_tool_policies_batch",
                return_value={"bash": "deny", "read_file": "allow"},
            ),
            patch(
                "turnstone.core.storage._registry._storage",
                new=MagicMock(),
            ),
            patch.object(bridge, "_api_approve") as mock_approve,
            patch.object(bridge, "_publish_ws"),
        ):
            bridge._handle_approval("ws-1", {"items": self._approval_items("bash", "read_file")})

        mock_approve.assert_called_once()
        assert mock_approve.call_args.kwargs.get("approved") is False
