"""Tests for workstream template runtime — template application, token budget, config persistence."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from turnstone.core.session import ChatSession
from turnstone.mq.protocol import CreateWorkstreamMessage
from turnstone.server import WebUI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class NullUI:
    """UI adapter that discards all output."""

    def on_thinking_start(self):
        pass

    def on_thinking_stop(self):
        pass

    def on_reasoning_token(self, text):
        pass

    def on_content_token(self, text):
        pass

    def on_stream_end(self):
        pass

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass

    def on_state_change(self, state):
        pass

    def on_rename(self, name):
        pass


def _make_session(ui=None, **kwargs):
    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=ui or NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


# ---------------------------------------------------------------------------
# Template application — defaults and constructor
# ---------------------------------------------------------------------------


def test_session_token_budget_default_zero(tmp_db):
    session = _make_session()
    assert session._token_budget == 0


def test_session_save_config_includes_ws_template_fields(tmp_db):
    session = _make_session()
    session._token_budget = 50000
    session._ws_template_id = "tpl-abc"
    session._ws_template_version = 3
    session._notify_on_complete = '{"url": "http://example.com"}'
    session._save_config()

    from turnstone.core.memory import load_workstream_config

    config = load_workstream_config(session._ws_id)
    assert config["token_budget"] == "50000"
    assert config["ws_template_id"] == "tpl-abc"
    assert config["ws_template_version"] == "3"
    assert config["notify_on_complete"] == '{"url": "http://example.com"}'


def test_session_resume_restores_token_budget(tmp_db):
    s1 = _make_session()
    s1._token_budget = 100000
    s1._save_config()
    # Seed at least one message so resume can load the workstream
    s1.messages.append({"role": "user", "content": "hello"})
    from turnstone.core.memory import save_message

    save_message(s1._ws_id, "user", "hello")

    s2 = _make_session()
    assert s2.resume(s1._ws_id)
    assert s2._token_budget == 100000


def test_session_resume_restores_ws_template_id(tmp_db):
    s1 = _make_session()
    s1._ws_template_id = "tpl-xyz"
    s1._save_config()
    from turnstone.core.memory import save_message

    save_message(s1._ws_id, "user", "ping")

    s2 = _make_session()
    assert s2.resume(s1._ws_id)
    assert s2._ws_template_id == "tpl-xyz"


def test_session_resume_restores_ws_template_version(tmp_db):
    s1 = _make_session()
    s1._ws_template_version = 7
    s1._save_config()
    from turnstone.core.memory import save_message

    save_message(s1._ws_id, "user", "ping")

    s2 = _make_session()
    assert s2.resume(s1._ws_id)
    assert s2._ws_template_version == 7


def test_session_resume_restores_notify_on_complete(tmp_db):
    s1 = _make_session()
    s1._notify_on_complete = '{"channel": "#ops"}'
    s1._save_config()
    from turnstone.core.memory import save_message

    save_message(s1._ws_id, "user", "ping")

    s2 = _make_session()
    assert s2.resume(s1._ws_id)
    assert s2._notify_on_complete == '{"channel": "#ops"}'


# ---------------------------------------------------------------------------
# Token budget tracking
# ---------------------------------------------------------------------------


def test_budget_warning_at_80_percent(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (True, None)
    session = _make_session(ui=ui)
    session._token_budget = 10000
    # Simulate usage at 80% of budget
    session._last_usage = {"prompt_tokens": 7500, "completion_tokens": 500}
    session._update_token_table({"role": "assistant", "content": "hi"})
    assert session._budget_warned is True
    ui.on_info.assert_called_once()
    assert "80%" in ui.on_info.call_args[0][0]


def test_budget_exhausted_at_100_percent(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (True, None)
    session = _make_session(ui=ui)
    session._token_budget = 10000
    session._last_usage = {"prompt_tokens": 9000, "completion_tokens": 1500}
    session._update_token_table({"role": "assistant", "content": "hi"})
    assert session._budget_exhausted is True


def test_budget_zero_no_tracking(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (True, None)
    session = _make_session(ui=ui)
    assert session._token_budget == 0
    session._last_usage = {"prompt_tokens": 999999, "completion_tokens": 999999}
    session._update_token_table({"role": "assistant", "content": "hi"})
    assert session._budget_warned is False
    assert session._budget_exhausted is False
    ui.on_info.assert_not_called()


def test_budget_warning_only_once(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (True, None)
    session = _make_session(ui=ui)
    session._token_budget = 10000
    # First call at 80%
    session._last_usage = {"prompt_tokens": 7500, "completion_tokens": 500}
    session._update_token_table({"role": "assistant", "content": "a"})
    assert session._budget_warned is True
    assert ui.on_info.call_count == 1
    # Second call still above 80% — should not warn again
    session._last_usage = {"prompt_tokens": 8500, "completion_tokens": 500}
    session._update_token_table({"role": "assistant", "content": "b"})
    assert session._budget_warned is True
    assert ui.on_info.call_count == 1


# ---------------------------------------------------------------------------
# Token budget approval gate in send()
# ---------------------------------------------------------------------------


def test_send_blocked_when_budget_exhausted(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (False, None)
    session = _make_session(ui=ui)
    session._budget_exhausted = True
    session._token_budget = 5000
    session.send("hello")
    # approve_tools should have been called with __budget_override__
    ui.approve_tools.assert_called_once()
    items = ui.approve_tools.call_args[0][0]
    assert len(items) == 1
    assert items[0]["func_name"] == "__budget_override__"
    assert "5,000" in items[0]["preview"]
    # on_error should have been called since approval was denied
    ui.on_error.assert_called_once()
    assert "budget" in ui.on_error.call_args[0][0].lower()


def test_send_continues_after_budget_approval(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (True, None)
    session = _make_session(ui=ui)
    session._budget_exhausted = True
    session._budget_warned = True
    session._token_budget = 5000

    # Patch _create_stream_with_retry to avoid actual LLM call
    with (
        patch.object(session, "_create_stream_with_retry"),
        patch.object(session, "_stream_response") as mock_resp,
        patch.object(session, "_update_token_table"),
        patch.object(session, "_print_status_line"),
    ):
        mock_resp.return_value = {"role": "assistant", "content": "ok", "tool_calls": []}
        session.send("hello")

    # Budget flags should be reset
    assert session._budget_exhausted is False
    assert session._budget_warned is False
    # approve_tools was called for budget gate
    ui.approve_tools.assert_called_once()


def test_send_returns_when_budget_denied(tmp_db):
    ui = MagicMock(spec_set=NullUI)
    ui.approve_tools.return_value = (False, None)
    session = _make_session(ui=ui)
    session._budget_exhausted = True
    session._token_budget = 5000

    # Patch to detect if _create_stream_with_retry is called (it shouldn't be)
    with patch.object(session, "_create_stream_with_retry") as mock_stream:
        session.send("hello")
        mock_stream.assert_not_called()

    # Message should NOT have been appended
    assert len(session.messages) == 0


# ---------------------------------------------------------------------------
# WebUI auto_approve_tools
# ---------------------------------------------------------------------------


def test_webui_auto_approve_tools_default_empty():
    webui = WebUI(ws_id="ws-1")
    assert webui.auto_approve_tools == set()


def test_webui_auto_approve_tools_subset_approves():
    webui = WebUI(ws_id="ws-1")
    webui.auto_approve_tools = {"bash", "read_file", "write_file"}
    items = [
        {"func_name": "bash", "preview": "ls", "needs_approval": True},
        {"func_name": "read_file", "preview": "/tmp/x", "needs_approval": True},
    ]
    # Patch out policy evaluation and global queue to isolate auto_approve_tools
    with patch("turnstone.server.WebUI._global_queue", None):
        approved, _ = webui.approve_tools(items)
    assert approved is True


def test_webui_auto_approve_tools_partial_no_approve():
    webui = WebUI(ws_id="ws-1")
    webui.auto_approve_tools = {"bash"}
    items = [
        {"func_name": "bash", "preview": "ls", "needs_approval": True},
        {"func_name": "write_file", "preview": "/tmp/x", "needs_approval": True},
    ]
    # write_file is NOT in auto_approve_tools, so it won't auto-approve.
    # The method will block on _approval_event, so we set it immediately.
    webui._approval_event = MagicMock()
    webui._approval_event.wait.return_value = None
    webui._approval_result = (False, None)
    with patch("turnstone.server.WebUI._global_queue", None):
        approved, _ = webui.approve_tools(items)
    assert approved is False


def test_webui_auto_approve_tools_empty_no_effect():
    webui = WebUI(ws_id="ws-1")
    webui.auto_approve_tools = set()
    items = [
        {"func_name": "bash", "preview": "ls", "needs_approval": True},
    ]
    # Empty set should not auto-approve; must wait for manual approval.
    webui._approval_event = MagicMock()
    webui._approval_event.wait.return_value = None
    webui._approval_result = (True, None)
    with patch("turnstone.server.WebUI._global_queue", None):
        approved, _ = webui.approve_tools(items)
    # Approval comes from the manual path (we set _approval_result to True)
    assert approved is True
    # The approval event wait should have been called (manual approval path)
    webui._approval_event.wait.assert_called_once()


# ---------------------------------------------------------------------------
# Per-tool "always approve" — interactive "Always" adds to auto_approve_tools
# ---------------------------------------------------------------------------


def test_server_always_approve_adds_tool_names():
    """POST /approve with always=True adds pending tool names to auto_approve_tools."""
    webui = WebUI(ws_id="ws-1")
    webui._pending_approval = {
        "type": "approve_request",
        "items": [
            {"func_name": "bash", "needs_approval": True, "preview": "ls"},
            {"func_name": "read_file", "needs_approval": False, "preview": "/tmp"},
        ],
    }
    items = webui._pending_approval.get("items", [])
    tool_names = {
        it.get("approval_label", "") or it.get("func_name", "")
        for it in items
        if it.get("needs_approval") and it.get("func_name")
    }
    tool_names.discard("")
    tool_names.discard("__budget_override__")
    webui.auto_approve_tools.update(tool_names)

    assert webui.auto_approve_tools == {"bash"}
    assert webui.auto_approve is False  # blanket flag NOT set


def test_server_always_approve_uses_approval_label():
    """When approval_label differs from func_name, approval_label is stored."""
    webui = WebUI(ws_id="ws-1")
    webui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "func_name": "use_prompt",
                "approval_label": "mcp__git__commit_msg",
                "needs_approval": True,
                "preview": "",
            },
        ],
    }
    items = webui._pending_approval.get("items", [])
    tool_names = {
        it.get("approval_label", "") or it.get("func_name", "")
        for it in items
        if it.get("needs_approval") and it.get("func_name")
    }
    tool_names.discard("")
    tool_names.discard("__budget_override__")
    webui.auto_approve_tools.update(tool_names)

    assert "mcp__git__commit_msg" in webui.auto_approve_tools
    assert "use_prompt" not in webui.auto_approve_tools


def test_server_always_approve_excludes_budget_override():
    """__budget_override__ should never be added to auto_approve_tools."""
    webui = WebUI(ws_id="ws-1")
    webui._pending_approval = {
        "type": "approve_request",
        "items": [
            {"func_name": "__budget_override__", "needs_approval": True, "preview": ""},
            {"func_name": "bash", "needs_approval": True, "preview": "ls"},
        ],
    }
    items = webui._pending_approval.get("items", [])
    tool_names = {
        it.get("approval_label", "") or it.get("func_name", "")
        for it in items
        if it.get("needs_approval") and it.get("func_name")
    }
    tool_names.discard("")
    tool_names.discard("__budget_override__")
    webui.auto_approve_tools.update(tool_names)

    assert "__budget_override__" not in webui.auto_approve_tools
    assert webui.auto_approve_tools == {"bash"}


def test_server_always_approve_accumulates():
    """Successive 'always' approvals accumulate tool names."""
    webui = WebUI(ws_id="ws-1")

    # First always-approve: bash
    webui._pending_approval = {
        "type": "approve_request",
        "items": [{"func_name": "bash", "needs_approval": True, "preview": "ls"}],
    }
    items = webui._pending_approval["items"]
    names = {
        it.get("approval_label", "") or it["func_name"] for it in items if it.get("needs_approval")
    }
    names.discard("__budget_override__")
    webui.auto_approve_tools.update(names)

    # Second always-approve: write_file
    webui._pending_approval = {
        "type": "approve_request",
        "items": [{"func_name": "write_file", "needs_approval": True, "preview": ""}],
    }
    items = webui._pending_approval["items"]
    names = {
        it.get("approval_label", "") or it["func_name"] for it in items if it.get("needs_approval")
    }
    names.discard("__budget_override__")
    webui.auto_approve_tools.update(names)

    assert webui.auto_approve_tools == {"bash", "write_file"}


def test_server_always_approve_no_pending_is_noop():
    """If _pending_approval is None, always=True does nothing."""
    webui = WebUI(ws_id="ws-1")
    webui._pending_approval = None
    # The guard `if always and approved and ui._pending_approval:` prevents action
    assert webui.auto_approve_tools == set()
    assert webui.auto_approve is False


# ---------------------------------------------------------------------------
# CLI per-tool "always approve"
# ---------------------------------------------------------------------------


def test_cli_always_adds_tool_names():
    """CLI 'a' adds pending tool names to auto_approve_tools, not blanket flag."""
    from turnstone.cli import TerminalUI

    ui = TerminalUI()
    items = [
        {"func_name": "bash", "header": "bash: ls", "needs_approval": True, "preview": "ls"},
    ]
    with patch("builtins.input", return_value="a"):
        approved, _ = ui.approve_tools(items)
    assert approved is True
    assert ui.auto_approve is False
    assert "bash" in ui.auto_approve_tools


def test_cli_per_tool_auto_approves_subsequent():
    """After 'always' for bash, subsequent bash calls auto-approve silently."""
    from turnstone.cli import TerminalUI

    ui = TerminalUI()
    ui.auto_approve_tools = {"bash"}
    items = [
        {"func_name": "bash", "header": "bash: ls", "needs_approval": True, "preview": "ls"},
    ]
    # Should auto-approve without prompting
    approved, _ = ui.approve_tools(items)
    assert approved is True


def test_cli_per_tool_does_not_approve_unknown():
    """Per-tool set for bash does NOT auto-approve write_file."""
    from turnstone.cli import TerminalUI

    ui = TerminalUI()
    ui.auto_approve_tools = {"bash"}
    items = [
        {
            "func_name": "write_file",
            "header": "write_file: /tmp/x",
            "needs_approval": True,
            "preview": "",
        },
    ]
    with patch("builtins.input", return_value="n"):
        approved, _ = ui.approve_tools(items)
    assert approved is False


def test_cli_always_excludes_budget_override():
    """CLI 'always' should not add __budget_override__ to auto_approve_tools."""
    from turnstone.cli import TerminalUI

    ui = TerminalUI()
    items = [
        {
            "func_name": "__budget_override__",
            "header": "budget",
            "needs_approval": True,
            "preview": "",
        },
    ]
    with patch("builtins.input", return_value="a"):
        approved, _ = ui.approve_tools(items)
    assert approved is True
    assert "__budget_override__" not in ui.auto_approve_tools


# ---------------------------------------------------------------------------
# Bridge per-tool "always approve"
# ---------------------------------------------------------------------------


def test_bridge_always_adds_to_approve_tools():
    """Bridge 'always' adds tool names to _ws_approve_tools, not _ws_auto_approve."""
    import threading

    from turnstone.mq.bridge import DEFAULT_SAFE_TOOLS, Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._lock = threading.Lock()
    bridge._ws_auto_approve = {}
    bridge._ws_approve_tools = {}

    ws_id = "ws-1"
    items = [
        {"func_name": "bash", "needs_approval": True},
        {"func_name": "read_file", "needs_approval": False},
    ]

    # Simulate the always-approve extraction logic from _wait_approval
    tool_names = {
        it.get("func_name", "") for it in items if it.get("needs_approval") and it.get("func_name")
    }
    tool_names.discard("")
    tool_names.discard("__budget_override__")
    if tool_names:
        with bridge._lock:
            existing = bridge._ws_approve_tools.get(ws_id, set(DEFAULT_SAFE_TOOLS))
            bridge._ws_approve_tools[ws_id] = existing | tool_names

    # bash added, and DEFAULT_SAFE_TOOLS preserved
    assert "bash" in bridge._ws_approve_tools[ws_id]
    for name in DEFAULT_SAFE_TOOLS:
        assert name in bridge._ws_approve_tools[ws_id]
    assert ws_id not in bridge._ws_auto_approve


# ---------------------------------------------------------------------------
# Integration tests — POST /v1/api/approve with always=True
# ---------------------------------------------------------------------------


class TestApproveEndpointAlways:
    """Integration tests for the approve handler's per-tool 'always' logic."""

    @staticmethod
    def _make_client(webui):
        import queue
        import threading

        from starlette.testclient import TestClient

        from turnstone.core.auth import AuthConfig
        from turnstone.server import create_app

        mock_ws = MagicMock()
        mock_ws.ui = webui

        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        mock_mgr.list_all.return_value = []

        app = create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            auth_config=AuthConfig(),
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_always_adds_tool_to_auto_approve_tools(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {"func_name": "bash", "needs_approval": True, "preview": "ls"},
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": True, "always": True, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert "bash" in webui.auto_approve_tools
        assert webui.auto_approve is False

    def test_always_uses_approval_label_over_func_name(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {
                    "func_name": "use_prompt",
                    "approval_label": "mcp__git__commit_msg",
                    "needs_approval": True,
                    "preview": "",
                },
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": True, "always": True, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert "mcp__git__commit_msg" in webui.auto_approve_tools
        assert "use_prompt" not in webui.auto_approve_tools

    def test_always_excludes_budget_override(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {"func_name": "__budget_override__", "needs_approval": True, "preview": ""},
                {"func_name": "bash", "needs_approval": True, "preview": "ls"},
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": True, "always": True, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert "__budget_override__" not in webui.auto_approve_tools
        assert "bash" in webui.auto_approve_tools

    def test_always_skips_non_pending_items(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {"func_name": "bash", "needs_approval": True, "preview": "ls"},
                {"func_name": "read_file", "needs_approval": False, "preview": "/tmp"},
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": True, "always": True, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert webui.auto_approve_tools == {"bash"}

    def test_always_false_does_not_add_tools(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {"func_name": "bash", "needs_approval": True, "preview": "ls"},
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": True, "always": False, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert webui.auto_approve_tools == set()

    def test_deny_with_always_does_not_add_tools(self):
        webui = WebUI(ws_id="ws-1")
        webui._pending_approval = {
            "type": "approve_request",
            "items": [
                {"func_name": "bash", "needs_approval": True, "preview": "ls"},
            ],
        }
        client = self._make_client(webui)
        resp = client.post(
            "/v1/api/approve",
            json={"approved": False, "always": True, "ws_id": "ws-1"},
        )
        assert resp.status_code == 200
        assert webui.auto_approve_tools == set()


# ---------------------------------------------------------------------------
# Protocol round-trip — CreateWorkstreamMessage
# ---------------------------------------------------------------------------


def test_create_workstream_message_ws_template():
    msg = CreateWorkstreamMessage(ws_template="deploy-v2")
    assert msg.ws_template == "deploy-v2"
    assert msg.type == "create_workstream"


def test_create_workstream_message_ws_template_default():
    msg = CreateWorkstreamMessage()
    assert msg.ws_template == ""


# ---------------------------------------------------------------------------
# Config persistence round-trip
# ---------------------------------------------------------------------------


def test_save_config_round_trip(tmp_db):
    s1 = _make_session()
    s1._token_budget = 75000
    s1._ws_template_id = "tpl-roundtrip"
    s1._ws_template_version = 12
    s1._notify_on_complete = '{"webhook": "https://hooks.example.com/done"}'
    s1._save_config()

    from turnstone.core.memory import save_message

    save_message(s1._ws_id, "user", "test")

    s2 = _make_session()
    assert s2.resume(s1._ws_id)
    assert s2._token_budget == 75000
    assert s2._ws_template_id == "tpl-roundtrip"
    assert s2._ws_template_version == 12
    assert s2._notify_on_complete == '{"webhook": "https://hooks.example.com/done"}'
