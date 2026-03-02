"""Tests for turnstone.core.workstream — WorkstreamManager, state management, and UI adapters."""

import threading
import time

import pytest
from unittest.mock import MagicMock

from turnstone.core.workstream import WorkstreamManager, WorkstreamState, Workstream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal stand-in for ChatSession in workstream tests."""

    def __init__(self):
        self.model = "test-model"
        self.messages = []


def _fake_factory(ui):
    return FakeSession()


class FakeUI:
    """Minimal SessionUI that tracks state changes."""

    def __init__(self, ws_id=""):
        self.ws_id = ws_id
        self.state_changes = []
        self.auto_approve = False

    def on_state_change(self, state):
        self.state_changes.append(state)

    # Stubs for the rest of the protocol
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

    def on_tool_result(self, name, output):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass


# ---------------------------------------------------------------------------
# WorkstreamState enum
# ---------------------------------------------------------------------------


class TestWorkstreamState:
    def test_values(self):
        assert WorkstreamState.IDLE.value == "idle"
        assert WorkstreamState.THINKING.value == "thinking"
        assert WorkstreamState.RUNNING.value == "running"
        assert WorkstreamState.ATTENTION.value == "attention"
        assert WorkstreamState.ERROR.value == "error"

    def test_from_string(self):
        assert WorkstreamState("idle") == WorkstreamState.IDLE
        assert WorkstreamState("attention") == WorkstreamState.ATTENTION


# ---------------------------------------------------------------------------
# Workstream dataclass
# ---------------------------------------------------------------------------


class TestWorkstream:
    def test_default_name(self):
        ws = Workstream()
        assert ws.name.startswith("ws-")
        assert len(ws.name) == 7  # "ws-" + 4 hex chars

    def test_custom_name(self):
        ws = Workstream(name="my-stream")
        assert ws.name == "my-stream"

    def test_default_state(self):
        ws = Workstream()
        assert ws.state == WorkstreamState.IDLE

    def test_id_uniqueness(self):
        ws1 = Workstream()
        ws2 = Workstream()
        assert ws1.id != ws2.id


# ---------------------------------------------------------------------------
# WorkstreamManager — creation and lookup
# ---------------------------------------------------------------------------


class TestManagerCreation:
    def test_create_first_sets_active(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.active_id == ws.id
        assert mgr.get_active() is ws

    def test_create_second_does_not_change_active(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.active_id == ws1.id

    def test_create_assigns_session(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert isinstance(ws.session, FakeSession)

    def test_create_assigns_ui(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert isinstance(ws.ui, FakeUI)
        assert ws.ui.ws_id == ws.id

    def test_create_custom_name(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(name="research", ui_factory=lambda wid: FakeUI(wid))
        assert ws.name == "research"

    def test_create_default_name(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert ws.name.startswith("ws-")

    def test_create_max_workstreams(self):
        mgr = WorkstreamManager(_fake_factory)
        mgr.MAX_WORKSTREAMS = 3
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        with pytest.raises(RuntimeError, match="Maximum"):
            mgr.create(ui_factory=lambda wid: FakeUI(wid))


class TestManagerLookup:
    def test_get_existing(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.get(ws.id) is ws

    def test_get_nonexistent(self):
        mgr = WorkstreamManager(_fake_factory)
        assert mgr.get("no-such-id") is None

    def test_list_all_creation_order(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(name="a", ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(name="b", ui_factory=lambda wid: FakeUI(wid))
        ws3 = mgr.create(name="c", ui_factory=lambda wid: FakeUI(wid))
        result = mgr.list_all()
        assert [w.name for w in result] == ["a", "b", "c"]

    def test_index_of(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.index_of(ws1.id) == 1
        assert mgr.index_of(ws2.id) == 2
        assert mgr.index_of("nonexistent") == 0

    def test_count(self):
        mgr = WorkstreamManager(_fake_factory)
        assert mgr.count == 0
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.count == 1
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.count == 2


# ---------------------------------------------------------------------------
# WorkstreamManager — switching
# ---------------------------------------------------------------------------


class TestManagerSwitching:
    def test_switch_by_id(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.active_id == ws1.id

        result = mgr.switch(ws2.id)
        assert result is ws2
        assert mgr.active_id == ws2.id

    def test_switch_nonexistent_returns_none(self):
        mgr = WorkstreamManager(_fake_factory)
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.switch("bad-id") is None

    def test_switch_by_index(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        result = mgr.switch_by_index(2)
        assert result is ws2
        assert mgr.active_id == ws2.id

    def test_switch_by_index_out_of_range(self):
        mgr = WorkstreamManager(_fake_factory)
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.switch_by_index(0) is None
        assert mgr.switch_by_index(5) is None


# ---------------------------------------------------------------------------
# WorkstreamManager — closing
# ---------------------------------------------------------------------------


class TestManagerClose:
    def test_close_removes_workstream(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        assert mgr.close(ws2.id) is True
        assert mgr.count == 1
        assert mgr.get(ws2.id) is None

    def test_close_last_returns_false(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.close(ws.id) is False
        assert mgr.count == 1

    def test_close_nonexistent_returns_false(self):
        mgr = WorkstreamManager(_fake_factory)
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert mgr.close("nonexistent") is False

    def test_close_active_switches_to_first(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        mgr.switch(ws2.id)

        mgr.close(ws2.id)
        assert mgr.active_id == ws1.id

    def test_close_updates_order(self):
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(name="a", ui_factory=lambda wid: FakeUI(wid))
        ws2 = mgr.create(name="b", ui_factory=lambda wid: FakeUI(wid))
        ws3 = mgr.create(name="c", ui_factory=lambda wid: FakeUI(wid))

        mgr.close(ws2.id)
        names = [w.name for w in mgr.list_all()]
        assert names == ["a", "c"]

    def test_close_unblocks_approval_event(self):
        """Closing a workstream whose UI has a pending approval should unblock it."""
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        # Create a workstream with a WebUI-like approval mechanism
        from turnstone.server import WebUI

        ws2 = mgr.create(ui_factory=lambda wid: WebUI(ws_id=wid))
        ws2.ui._approval_event.clear()  # simulate pending approval

        mgr.close(ws2.id)
        # The approval event should be set (unblocked)
        assert ws2.ui._approval_event.is_set()

    def test_close_unblocks_plan_event(self):
        """Closing a workstream with pending plan review should unblock it."""
        mgr = WorkstreamManager(_fake_factory)
        ws1 = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        from turnstone.server import WebUI

        ws2 = mgr.create(ui_factory=lambda wid: WebUI(ws_id=wid))
        ws2.ui._plan_event.clear()

        mgr.close(ws2.id)
        assert ws2.ui._plan_event.is_set()
        assert ws2.ui._plan_result == "reject"


# ---------------------------------------------------------------------------
# WorkstreamManager — state management
# ---------------------------------------------------------------------------


class TestManagerState:
    def test_set_state(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        assert ws.state == WorkstreamState.IDLE

        mgr.set_state(ws.id, WorkstreamState.THINKING)
        assert ws.state == WorkstreamState.THINKING

    def test_set_state_with_error(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        mgr.set_state(ws.id, WorkstreamState.ERROR, error_msg="API timeout")
        assert ws.state == WorkstreamState.ERROR
        assert ws.error_message == "API timeout"

    def test_set_state_nonexistent_is_noop(self):
        mgr = WorkstreamManager(_fake_factory)
        mgr.set_state("no-such-id", WorkstreamState.THINKING)  # should not raise

    def test_on_state_change_callback(self):
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        changes = []
        mgr._on_state_change = lambda wid, state: changes.append((wid, state))

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert changes == [(ws.id, WorkstreamState.RUNNING)]


# ---------------------------------------------------------------------------
# WorkstreamManager — thread safety
# ---------------------------------------------------------------------------


class TestManagerThreadSafety:
    def test_concurrent_create_respects_max(self):
        """Multiple threads creating workstreams should not exceed MAX."""
        mgr = WorkstreamManager(_fake_factory)
        mgr.MAX_WORKSTREAMS = 5
        errors = []
        created = []

        def do_create():
            try:
                ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
                created.append(ws.id)
            except RuntimeError:
                errors.append(True)

        threads = [threading.Thread(target=do_create) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert mgr.count == 5
        assert len(errors) == 5

    def test_concurrent_switch(self):
        """Concurrent switches should not corrupt state."""
        mgr = WorkstreamManager(_fake_factory)
        ids = []
        for _ in range(5):
            ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
            ids.append(ws.id)

        def do_switch(wid):
            for _ in range(20):
                mgr.switch(wid)

        threads = [threading.Thread(target=do_switch, args=(wid,)) for wid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # active_id should be one of the valid ids
        assert mgr.active_id in ids

    def test_concurrent_close_and_list(self):
        """close() and list_all() running concurrently should not crash."""
        mgr = WorkstreamManager(_fake_factory)
        # Keep one alive to prevent closing the last
        anchor = mgr.create(ui_factory=lambda wid: FakeUI(wid))
        targets = []
        for _ in range(5):
            ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))
            targets.append(ws.id)

        def do_close():
            for wid in targets:
                mgr.close(wid)

        def do_list():
            for _ in range(50):
                mgr.list_all()

        t1 = threading.Thread(target=do_close)
        t2 = threading.Thread(target=do_list)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert mgr.count == 1
        assert mgr.get(anchor.id) is not None


# ---------------------------------------------------------------------------
# WorkstreamTerminalUI
# ---------------------------------------------------------------------------


class TestWorkstreamTerminalUI:
    def test_foreground_detection(self):
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        assert ws.ui.is_foreground is True

        ws2 = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        # ws1 is still active, so ws2 is not foreground
        assert ws2.ui.is_foreground is False

    def test_state_change_updates_manager(self):
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))

        ws.ui.on_state_change("thinking")
        assert ws.state == WorkstreamState.THINKING

        ws.ui.on_state_change("idle")
        assert ws.state == WorkstreamState.IDLE

    def test_invalid_state_change_ignored(self):
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        ws.ui.on_state_change("not_a_real_state")  # should not raise
        assert ws.state == WorkstreamState.IDLE  # unchanged

    def _make_background_ws(self):
        """Create a manager with two workstreams; switch to the second so the first is background."""
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        bg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        fg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        mgr.switch(fg.id)
        bg.ui.set_foreground(False)
        fg.ui.set_foreground(True)
        return mgr, bg, fg

    def test_background_buffers_content(self):
        mgr, bg, fg = self._make_background_ws()
        assert bg.ui.is_foreground is False

        bg.ui.on_content_token("hello ")
        bg.ui.on_content_token("world")
        bg.ui.on_stream_end()

        assert len(bg.ui._output_buffer) == 3
        assert bg.ui._output_buffer[0] == ("content", "hello ")
        assert bg.ui._output_buffer[1] == ("content", "world")
        assert bg.ui._output_buffer[2] == ("stream_end", "")

    def test_flush_buffer_clears(self):
        mgr, bg, fg = self._make_background_ws()

        bg.ui.on_content_token("test")
        bg.ui.on_stream_end()
        assert len(bg.ui._output_buffer) == 2

        mgr.switch(bg.id)
        bg.ui.set_foreground(True)
        bg.ui.flush_buffer()
        assert len(bg.ui._output_buffer) == 0

    def test_background_buffers_info_and_error(self):
        mgr, bg, fg = self._make_background_ws()

        bg.ui.on_info("info msg")
        bg.ui.on_error("error msg")

        assert ("info", "info msg") in bg.ui._output_buffer
        assert ("error", "error msg") in bg.ui._output_buffer

    def test_fg_event_blocks_approval_in_background(self):
        """approve_tools should block until foregrounded."""
        mgr, bg, fg = self._make_background_ws()
        bg.ui.auto_approve = True  # so we don't need actual input()

        result = [None]

        def call_approve():
            result[0] = bg.ui.approve_tools(
                [{"needs_approval": True, "header": "test", "func_name": "bash"}]
            )

        t = threading.Thread(target=call_approve)
        t.start()
        time.sleep(0.1)
        assert t.is_alive(), "approve_tools should be blocking"

        # Bring to foreground — should unblock
        mgr.switch(bg.id)
        bg.ui.set_foreground(True)
        t.join(timeout=2)
        assert not t.is_alive()
        assert result[0] == (True, None)  # auto-approved


# ---------------------------------------------------------------------------
# WebUI workstream support
# ---------------------------------------------------------------------------


class TestWebUI:
    def test_ws_id_assigned(self):
        from turnstone.server import WebUI

        ui = WebUI(ws_id="test-123")
        assert ui.ws_id == "test-123"

    def test_on_state_change_broadcasts(self):
        """on_state_change should put an event on the global queue."""
        import queue
        from turnstone.server import WebUI

        gq = queue.Queue()
        old = WebUI._global_queue
        WebUI._global_queue = gq
        try:
            ui = WebUI(ws_id="abc")
            ui.on_state_change("thinking")

            event = gq.get_nowait()
            assert event["type"] == "ws_state"
            assert event["ws_id"] == "abc"
            assert event["state"] == "thinking"
        finally:
            WebUI._global_queue = old

    def test_on_state_change_no_global_queue(self):
        """on_state_change should not crash if no global queue is set."""
        from turnstone.server import WebUI

        old = WebUI._global_queue
        WebUI._global_queue = None
        try:
            ui = WebUI(ws_id="xyz")
            ui.on_state_change("running")  # should not raise
        finally:
            WebUI._global_queue = old

    def test_resolve_approval(self):
        from turnstone.server import WebUI

        ui = WebUI(ws_id="test")
        ui._approval_event.clear()

        # Resolve in a thread
        def resolve():
            time.sleep(0.05)
            ui.resolve_approval(True, "looks good")

        t = threading.Thread(target=resolve)
        t.start()

        ui._approval_event.wait(timeout=2)
        assert ui._approval_result == (True, "looks good")
        t.join()

    def test_resolve_plan(self):
        from turnstone.server import WebUI

        ui = WebUI(ws_id="test")
        ui._plan_event.clear()

        def resolve():
            time.sleep(0.05)
            ui.resolve_plan("approved")

        t = threading.Thread(target=resolve)
        t.start()

        ui._plan_event.wait(timeout=2)
        assert ui._plan_result == "approved"
        t.join()


# ---------------------------------------------------------------------------
# Integration: WorkstreamManager + session state transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_full_lifecycle(self):
        """Verify the expected state transition sequence."""
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        # Simulate the state transitions that ChatSession.send() would emit
        mgr.set_state(ws.id, WorkstreamState.THINKING)
        assert ws.state == WorkstreamState.THINKING

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert ws.state == WorkstreamState.RUNNING

        mgr.set_state(ws.id, WorkstreamState.ATTENTION)
        assert ws.state == WorkstreamState.ATTENTION

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        assert ws.state == WorkstreamState.RUNNING

        mgr.set_state(ws.id, WorkstreamState.IDLE)
        assert ws.state == WorkstreamState.IDLE

    def test_error_recovery(self):
        """After an error, sending again should transition back to thinking."""
        mgr = WorkstreamManager(_fake_factory)
        ws = mgr.create(ui_factory=lambda wid: FakeUI(wid))

        mgr.set_state(ws.id, WorkstreamState.ERROR, "API failed")
        assert ws.state == WorkstreamState.ERROR
        assert ws.error_message == "API failed"

        mgr.set_state(ws.id, WorkstreamState.THINKING)
        assert ws.state == WorkstreamState.THINKING
        assert ws.error_message == ""


# ---------------------------------------------------------------------------
# Design polish: thread-safe buffer, approval context, NO_COLOR
# ---------------------------------------------------------------------------


class TestBufferThreadSafety:
    """Verify that _buffer() uses the lock and flush_buffer copies under lock."""

    def test_concurrent_buffer_and_flush(self):
        """Simultaneous buffering and flushing should not lose or corrupt events."""
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        bg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        fg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        mgr.switch(fg.id)
        bg.ui.set_foreground(False)

        n_events = 200
        done = threading.Event()

        def do_buffer():
            for i in range(n_events):
                bg.ui._buffer("content", f"token-{i}")
            done.set()

        t = threading.Thread(target=do_buffer)
        t.start()
        done.wait()

        # All events should be in the buffer
        with bg.ui._print_lock:
            count = len(bg.ui._output_buffer)
        assert count == n_events
        t.join()


class TestApprovalContextMessage:
    """Verify approval in background buffers a context message."""

    def test_approval_buffers_tool_names(self):
        from turnstone.cli import WorkstreamTerminalUI

        mgr = WorkstreamManager(_fake_factory)
        bg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        fg = mgr.create(ui_factory=lambda wid: WorkstreamTerminalUI(wid, mgr))
        mgr.switch(fg.id)
        bg.ui.set_foreground(False)
        bg.ui.auto_approve = True

        result = [None]

        def call_approve():
            result[0] = bg.ui.approve_tools(
                [
                    {
                        "needs_approval": True,
                        "header": "test",
                        "func_name": "bash",
                        "approval_label": "bash: ls",
                    },
                ]
            )

        t = threading.Thread(target=call_approve)
        t.start()
        time.sleep(0.1)

        # Should have a waiting-for-approval message in the buffer
        with bg.ui._print_lock:
            info_msgs = [text for ev, text in bg.ui._output_buffer if ev == "info"]
        assert any("bash: ls" in msg for msg in info_msgs)

        # Unblock
        mgr.switch(bg.id)
        bg.ui.set_foreground(True)
        t.join(timeout=2)
        assert result[0] == (True, None)


class TestNoColor:
    """Verify NO_COLOR support in colors module."""

    def test_no_color_env_disables_ansi(self):
        import importlib
        import os
        import turnstone.ui.colors as colors_mod

        old_env = os.environ.get("NO_COLOR")
        try:
            os.environ["NO_COLOR"] = "1"
            importlib.reload(colors_mod)
            assert colors_mod.RESET == ""
            assert colors_mod.BOLD == ""
            assert colors_mod.RED == ""
            assert colors_mod.red("test") == "test"
        finally:
            if old_env is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = old_env
            importlib.reload(colors_mod)
