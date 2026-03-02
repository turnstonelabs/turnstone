"""Integration tests against a live llama.cpp backend on port 8000.

These tests use turnstone's HeadlessSession to run actual LLM inference
and tool execution against the backend. They verify end-to-end behavior:
model connectivity, tool calling, response quality, and session mechanics.

Requires: llama-server (or compatible OpenAI API) running on localhost:8000.

Run with: pytest tests/test_server_live.py -v --timeout=120

The TestServerHealthMetrics class does NOT require a live LLM and can be run
independently: pytest tests/test_server_live.py::TestServerHealthMetrics -v
"""

import json
import os
import queue
import tempfile
import threading
import time
import httpx
import pytest
from openai import OpenAI

from turnstone.core.session import ChatSession
from turnstone.core.tools import TOOLS
import turnstone.core.memory as _memory_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("TURNSTONE_TEST_BASE_URL", "http://localhost:8000/v1")


@pytest.fixture(scope="module")
def client():
    """Create an OpenAI client pointed at the local backend."""
    return OpenAI(
        base_url=BASE_URL,
        api_key=os.environ.get("TURNSTONE_TEST_API_KEY", "not-needed"),
    )


@pytest.fixture(scope="module")
def model_id(client):
    """Auto-detect the model name from the backend."""
    models = client.models.list()
    ids = [m.id for m in models.data]
    assert len(ids) > 0, "No models found on the backend"
    return ids[0]


class RecordingUI:
    """Minimal SessionUI that captures events for assertions."""

    def __init__(self):
        self.events: list[tuple[str, ...]] = []
        self.content_tokens: list[str] = []
        self.reasoning_tokens: list[str] = []
        self.tool_results: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.infos: list[str] = []

    def on_thinking_start(self):
        self.events.append(("thinking_start",))

    def on_thinking_stop(self):
        self.events.append(("thinking_stop",))

    def on_reasoning_token(self, text):
        self.reasoning_tokens.append(text)

    def on_content_token(self, text):
        self.content_tokens.append(text)

    def on_stream_end(self):
        self.events.append(("stream_end",))

    def approve_tools(self, items):
        return True, None  # auto-approve everything

    def on_tool_result(self, name, output):
        self.tool_results.append((name, output))

    def on_status(self, usage, context_window, effort):
        self.events.append(("status",))

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        self.infos.append(message)

    def on_error(self, message):
        self.errors.append(message)

    def on_state_change(self, state):
        self.events.append(("state_change", state))

    def on_rename(self, name: str):
        self.events.append(("rename", name))

    @property
    def full_content(self) -> str:
        return "".join(self.content_tokens)

    @property
    def full_reasoning(self) -> str:
        return "".join(self.reasoning_tokens)


@pytest.fixture
def tmp_db():
    """Temp DB to avoid polluting real conversation history."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    old = _memory_module.db_override
    _memory_module.db_override = path
    _memory_module.db_initialized.discard(path)
    yield path
    _memory_module.db_override = old
    _memory_module.db_initialized.discard(path)
    os.unlink(path)


def _make_session(
    client, model_id, tmp_db, **kwargs
) -> tuple[ChatSession, RecordingUI]:
    """Create a ChatSession with RecordingUI and sensible test defaults."""
    ui = RecordingUI()
    defaults = dict(
        client=client,
        model=model_id,
        ui=ui,
        persona=None,
        instructions=None,
        temperature=0.3,
        max_tokens=2048,
        tool_timeout=30,
        reasoning_effort="low",
    )
    defaults.update(kwargs)
    session = ChatSession(**defaults)
    session.auto_approve = True
    return session, ui


# ---------------------------------------------------------------------------
# Tests — Backend connectivity
# ---------------------------------------------------------------------------


class TestBackendConnectivity:
    """Verify the LLM backend is reachable and returns valid responses."""

    def test_models_endpoint(self, client):
        models = client.models.list()
        assert len(models.data) > 0

    def test_model_id_detected(self, model_id):
        assert isinstance(model_id, str)
        assert len(model_id) > 0

    def test_basic_completion(self, client, model_id):
        """Raw API call — no turnstone involved."""
        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Say 'hello'"}],
            max_completion_tokens=200,
            temperature=0.0,
            stream=False,
        )
        assert (
            resp.choices[0].message.content or resp.choices[0].message.reasoning_content
        )
        assert resp.usage.total_tokens > 0


# ---------------------------------------------------------------------------
# Tests — Streaming session
# ---------------------------------------------------------------------------


class TestStreamingSession:
    """Test ChatSession.send() with streaming against the live backend."""

    def test_simple_response(self, client, model_id, tmp_db):
        """Model responds to a basic prompt via streaming."""
        session, ui = _make_session(client, model_id, tmp_db)
        session.send("Reply with exactly: PONG")

        # Should have gotten some content or reasoning
        total = ui.full_content + ui.full_reasoning
        assert len(total) > 0, "No output from model"

    def test_reasoning_tokens_appear(self, client, model_id, tmp_db):
        """Model produces reasoning tokens (extended thinking)."""
        session, ui = _make_session(client, model_id, tmp_db)
        session.send("What is 7 * 8?")

        # This model uses reasoning_content, so we expect reasoning tokens
        assert len(ui.reasoning_tokens) > 0, "No reasoning tokens received"

    def test_stream_end_event(self, client, model_id, tmp_db):
        """stream_end event is emitted after response."""
        session, ui = _make_session(client, model_id, tmp_db)
        session.send("Say hi")

        event_types = [e[0] for e in ui.events]
        assert "stream_end" in event_types

    def test_thinking_lifecycle(self, client, model_id, tmp_db):
        """thinking_start and thinking_stop bracket the response."""
        session, ui = _make_session(client, model_id, tmp_db)
        session.send("Say hi")

        event_types = [e[0] for e in ui.events]
        assert "thinking_start" in event_types
        assert "thinking_stop" in event_types
        # thinking_start should come before thinking_stop
        start_idx = event_types.index("thinking_start")
        stop_idx = event_types.index("thinking_stop")
        assert start_idx < stop_idx


# ---------------------------------------------------------------------------
# Tests — Tool calling
# ---------------------------------------------------------------------------


class TestToolCalling:
    """Test that the model can invoke tools and turnstone executes them."""

    def test_math_tool(self, client, model_id, tmp_db):
        """Model uses the math tool for computation."""
        session, ui = _make_session(
            client,
            model_id,
            tmp_db,
            instructions="You have tools. Use the math tool to compute results. Always use tools when asked to calculate.",
        )
        session.send("Use the math tool to calculate: 17 * 23. Report the result.")

        # Check if math tool was invoked
        math_results = [r for r in ui.tool_results if r[0] == "math"]
        if math_results:
            # Verify the result contains 391
            assert "391" in math_results[0][1], (
                f"Expected 391, got: {math_results[0][1]}"
            )
        else:
            # Model may have answered directly — check content
            total = ui.full_content + ui.full_reasoning
            assert "391" in total, f"Expected 391 somewhere in output"

    def test_bash_tool(self, client, model_id, tmp_db):
        """Model uses bash to answer a system question."""
        session, ui = _make_session(
            client,
            model_id,
            tmp_db,
            instructions="You have tools. Use the bash tool to run commands. Always use bash when asked about system info.",
        )
        session.send(
            "Use the bash tool to run 'echo hello_from_test' and report what it prints."
        )

        bash_results = [r for r in ui.tool_results if r[0] == "bash"]
        if bash_results:
            assert "hello_from_test" in bash_results[0][1]
        else:
            total = ui.full_content + ui.full_reasoning
            assert "hello_from_test" in total, "Expected bash output in response"

    def test_read_file_tool(self, client, model_id, tmp_db):
        """Model uses read_file to read a known file."""
        # Create a temp file for the model to read
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("SECRET_CONTENT_42\n")
            path = f.name

        try:
            session, ui = _make_session(
                client,
                model_id,
                tmp_db,
                instructions="You have tools. Use the read_file tool to read files. Always use read_file when asked to read a file.",
            )
            session.send(
                f"Use the read_file tool to read {path} and tell me what it says."
            )

            # read_file was invoked (UI gets a summary like "1 lines")
            read_results = [r for r in ui.tool_results if r[0] == "read_file"]
            assert len(read_results) > 0, "read_file tool was not called"

            # The model sees the actual file content and should relay it
            total = ui.full_content + ui.full_reasoning
            assert "SECRET_CONTENT_42" in total, (
                f"Model didn't relay file content. Got: {total[:500]}"
            )
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests — Multi-turn conversation
# ---------------------------------------------------------------------------


class TestMultiTurn:
    """Test multi-turn conversation state."""

    def test_context_retained(self, client, model_id, tmp_db):
        """Second message can reference the first."""
        session, ui = _make_session(client, model_id, tmp_db, max_tokens=1024)
        session.send("My name is Zephyr. Remember it.")

        # Reset UI tracking for second turn
        ui.content_tokens.clear()
        ui.reasoning_tokens.clear()

        session.send("What is my name?")

        total = ui.full_content + ui.full_reasoning
        assert "zephyr" in total.lower(), f"Model forgot the name. Got: {total[:300]}"

    def test_message_list_grows(self, client, model_id, tmp_db):
        """Each send adds user + assistant messages."""
        session, ui = _make_session(client, model_id, tmp_db, max_tokens=512)

        initial_count = len(session.messages)
        session.send("Hello")

        # Should have at least user + assistant
        assert len(session.messages) >= initial_count + 2


# ---------------------------------------------------------------------------
# Tests — Session configuration
# ---------------------------------------------------------------------------


class TestSessionConfig:
    """Test session construction and configuration."""

    def test_creative_mode_no_tools(self, client, model_id, tmp_db):
        """In creative mode, tools are not sent to the API."""
        session, ui = _make_session(client, model_id, tmp_db, max_tokens=256)
        session.creative_mode = True
        session.send("Write a haiku about code.")

        # Should get content back without tool calls
        total = ui.full_content + ui.full_reasoning
        assert len(total) > 0
        assert len(ui.tool_results) == 0

    def test_custom_instructions(self, client, model_id, tmp_db):
        """Custom instructions are included in the session."""
        session, ui = _make_session(
            client,
            model_id,
            tmp_db,
            instructions="Always end your response with ENDMARKER.",
            max_tokens=512,
        )
        session.send("Say hello briefly.")

        total = ui.full_content
        # We can't strictly guarantee the model follows instructions,
        # but we verify the session didn't error out
        assert len(ui.errors) == 0


# ---------------------------------------------------------------------------
# Tests — /health and /metrics endpoints (no live LLM required)
# ---------------------------------------------------------------------------


class TestServerHealthMetrics:
    """Verify /health and /metrics endpoints using an in-process HTTP server.

    These tests spin up a real ThreadedHTTPServer with a mock WorkstreamManager
    so no live LLM backend is required.  Run them independently with:

        pytest tests/test_server_live.py::TestServerHealthMetrics -v
    """

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock
        import turnstone.server as srv_mod
        from turnstone.core.workstream import WorkstreamState

        # Reset module-level metrics so each test run starts fresh
        srv_mod._metrics = srv_mod.MetricsCollector()
        srv_mod._metrics.model = "test-model"

        # Mock WorkstreamManager.list_all() to return one idle workstream
        mock_ws = MagicMock()
        mock_ws.state = WorkstreamState.IDLE
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]

        # Start a server on a random port (port 0 → OS assigns free port)
        cls.server = srv_mod.ThreadedHTTPServer(
            ("127.0.0.1", 0), srv_mod.TurnstoneHTTPHandler
        )
        from turnstone.core.auth import AuthConfig

        cls.server.workstreams = mock_mgr
        cls.server.skip_permissions = False
        cls.server.global_listeners = []
        cls.server.global_queue = queue.Queue()
        cls.server.global_listeners_lock = threading.Lock()
        cls.server.auth_config = AuthConfig()  # auth disabled by default

        port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"

        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def teardown_class(cls):
        cls.server.shutdown()
        cls._thread.join(timeout=5)

    def _get(self, path) -> tuple[int, str, dict]:
        """Make a GET request; return (status, content_type, body_str)."""
        url = self.base + path
        resp = httpx.get(url, timeout=5)
        ct = resp.headers.get("content-type", "")
        return resp.status_code, ct, resp.text

    def test_health_returns_200(self):
        status, _, _ = self._get("/health")
        assert status == 200

    def test_health_content_type_json(self):
        _, ct, _ = self._get("/health")
        assert "application/json" in ct

    def test_health_response_structure(self):
        _, _, body = self._get("/health")
        data = json.loads(body)
        assert data["status"] == "ok"
        assert "version" in data
        assert "uptime_seconds" in data
        assert "model" in data
        assert "workstreams" in data

    def test_health_model_field(self):
        _, _, body = self._get("/health")
        data = json.loads(body)
        assert data["model"] == "test-model"

    def test_health_workstream_counts(self):
        _, _, body = self._get("/health")
        data = json.loads(body)
        wss = data["workstreams"]
        assert wss["total"] == 1
        assert wss["idle"] == 1

    def test_health_uptime_positive(self):
        _, _, body = self._get("/health")
        data = json.loads(body)
        assert data["uptime_seconds"] >= 0

    def test_metrics_returns_200(self):
        status, _, _ = self._get("/metrics")
        assert status == 200

    def test_metrics_content_type_prometheus(self):
        _, ct, _ = self._get("/metrics")
        assert "text/plain" in ct
        assert "version=0.0.4" in ct

    def test_metrics_contains_uptime(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_uptime_seconds" in body

    def test_metrics_contains_build_info(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_build_info" in body
        assert 'model="test-model"' in body

    def test_metrics_contains_workstreams(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_workstreams_active_total" in body
        assert "turnstone_workstreams_by_state" in body

    def test_metrics_contains_token_counters(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_tokens_total" in body
        assert 'type="prompt"' in body
        assert 'type="completion"' in body

    def test_metrics_contains_http_requests(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_http_requests_total" in body

    def test_metrics_request_counter_increments(self):
        """Hitting /health increments the HTTP request counter."""
        # Make a known request to /health
        self._get("/health")
        _, _, body = self._get("/metrics")
        # Counter should mention /health endpoint
        assert 'endpoint="/health"' in body

    def test_metrics_histogram_present(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_http_request_duration_seconds" in body
        assert 'le="' in body
        assert 'le="+Inf"' in body

    def test_unknown_endpoint_returns_404(self):
        status, _, _ = self._get("/does-not-exist")
        assert status == 404
