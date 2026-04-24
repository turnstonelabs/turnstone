"""Tests for turnstone ChatSession and server endpoints.

Mock-based tests verify streaming, tool calling, multi-turn conversation,
and session configuration WITHOUT a running LLM backend.  The mocks replace
only the OpenAI streaming layer -- tool execution (bash, math, read_file)
still runs real subprocesses.

The TestBackendConnectivity class is marked @pytest.mark.live and requires a
running llama-server (or compatible OpenAI API) on localhost:8000.

The TestServerHealthMetrics class spins up an in-process HTTP server and
needs no LLM backend at all.

Run all non-live tests:
    pytest tests/test_server_live.py -v -m "not live"

Run everything (needs backend):
    pytest tests/test_server_live.py -v --timeout=120
"""

import json
import os
import queue
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI

from turnstone.core.session import ChatSession
from turnstone.core.storage import init_storage, reset_storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("TURNSTONE_TEST_BASE_URL", "http://localhost:8000/v1")


@pytest.fixture(scope="module")
def live_client():
    """Create an OpenAI client pointed at the local backend (live tests only)."""
    return OpenAI(
        base_url=BASE_URL,
        api_key=os.environ.get("TURNSTONE_TEST_API_KEY", "not-needed"),
    )


@pytest.fixture(scope="module")
def live_model_id(live_client):
    """Auto-detect the model name from the backend (live tests only)."""
    models = live_client.models.list()
    ids = [m.id for m in models.data]
    assert len(ids) > 0, "No models found on the backend"
    return ids[0]


class RecordingUI:
    """Minimal SessionUI that captures events for assertions."""

    def __init__(self):
        self.events: list[tuple[str, ...]] = []
        self.content_tokens: list[str] = []
        self.reasoning_tokens: list[str] = []
        self.tool_results: list[tuple[str, str, str]] = []
        self.tool_chunks: list[tuple[str, str]] = []
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

    def on_tool_result(self, call_id, name, output, **kwargs):
        self.tool_results.append((call_id, name, output))

    def on_tool_output_chunk(self, call_id, chunk):
        self.tool_chunks.append((call_id, chunk))

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

    def on_output_warning(self, call_id, assessment):
        pass

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
    reset_storage()
    init_storage("sqlite", path=path, run_migrations=False)
    yield path
    reset_storage()
    os.unlink(path)


def _make_session(client, model_id, tmp_db, **kwargs) -> tuple[ChatSession, RecordingUI]:
    """Create a ChatSession with RecordingUI and sensible test defaults."""
    from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

    ui = RecordingUI()
    defaults = dict(
        client=client,
        model=model_id,
        ui=ui,
        instructions=None,
        temperature=0.3,
        max_tokens=2048,
        tool_timeout=30,
        reasoning_effort="low",
    )
    defaults.update(kwargs)
    session = ChatSession(**defaults)
    # Mock-based tests use Chat Completions format (client.chat.completions)
    session._provider = OpenAIChatCompletionsProvider()
    session.auto_approve = True
    return session, ui


# ---------------------------------------------------------------------------
# Mock streaming helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    *,
    content=None,
    reasoning_content=None,
    tool_calls=None,
    finish_reason=None,
    usage=None,
):
    """Build a single mock streaming chunk matching the OpenAI format.

    The chunk structure mirrors openai.types.chat.ChatCompletionChunk:
      chunk.choices[0].delta.content
      chunk.choices[0].delta.reasoning_content
      chunk.choices[0].delta.tool_calls
      chunk.choices[0].finish_reason
      chunk.usage
    """
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=None,
        tool_calls=tool_calls,
        role=None,
        model_extra=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    chunk = SimpleNamespace(choices=[choice], usage=usage)
    return chunk


def _make_tool_call_deltas(call_id, name, arguments):
    """Build a list of tool_call delta objects for a single tool call.

    Returns a list with one element (single tool call at index 0).
    """
    fn = SimpleNamespace(name=name, arguments=arguments)
    return [SimpleNamespace(index=0, id=call_id, function=fn)]


def _usage(prompt=100, completion=50, total=None):
    """Build a mock usage object."""
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or (prompt + completion),
    )


def make_mock_stream(
    content_tokens=None,
    reasoning_tokens=None,
    tool_calls=None,
    finish_reason="stop",
    usage=None,
):
    """Create an iterable of mock chunks simulating an OpenAI streaming response.

    Parameters
    ----------
    content_tokens : list[str] | None
        Content token strings, each emitted as a separate chunk.
    reasoning_tokens : list[str] | None
        Reasoning token strings, emitted before content.
    tool_calls : list[tuple[str, str, str]] | None
        Each entry is (call_id, function_name, arguments_json).
        When provided, finish_reason defaults to "tool_calls".
    finish_reason : str
        Finish reason on the last content/tool chunk.
    usage : SimpleNamespace | None
        Usage object for the final chunk.  Defaults to a sensible value.
    """
    chunks = []

    if reasoning_tokens:
        for token in reasoning_tokens:
            chunks.append(_make_chunk(reasoning_content=token))

    if content_tokens:
        for i, token in enumerate(content_tokens):
            is_last = (i == len(content_tokens) - 1) and not tool_calls
            chunks.append(
                _make_chunk(
                    content=token,
                    finish_reason=finish_reason if is_last else None,
                )
            )

    if tool_calls:
        for i, (call_id, name, arguments) in enumerate(tool_calls):
            is_last = i == len(tool_calls) - 1
            tc_deltas = _make_tool_call_deltas(call_id, name, arguments)
            chunks.append(
                _make_chunk(
                    tool_calls=tc_deltas,
                    finish_reason="tool_calls" if is_last else None,
                )
            )

    # Final usage-only chunk (no choices)
    if usage is None:
        usage = _usage()
    chunks.append(SimpleNamespace(choices=[], usage=usage))

    return iter(chunks)


def _mock_client():
    """Create a mock OpenAI client with a patchable chat.completions.create."""
    client = MagicMock(spec=OpenAI)
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Tests -- Backend connectivity (live, requires running LLM)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestBackendConnectivity:
    """Verify the LLM backend is reachable and returns valid responses."""

    def test_models_endpoint(self, live_client):
        models = live_client.models.list()
        assert len(models.data) > 0

    def test_model_id_detected(self, live_model_id):
        assert isinstance(live_model_id, str)
        assert len(live_model_id) > 0

    def test_basic_completion(self, live_client, live_model_id):
        """Raw API call -- no turnstone involved."""
        resp = live_client.chat.completions.create(
            model=live_model_id,
            messages=[{"role": "user", "content": "Say 'hello'"}],
            max_completion_tokens=200,
            temperature=0.0,
            stream=False,
        )
        assert resp.choices[0].message.content or resp.choices[0].message.reasoning_content
        assert resp.usage.total_tokens > 0


# ---------------------------------------------------------------------------
# Tests -- Streaming session (mocked)
# ---------------------------------------------------------------------------


class TestStreamingSession:
    """Test ChatSession.send() with mocked streaming responses."""

    def test_simple_response(self, tmp_db):
        """Mock returns content tokens; verify RecordingUI captures them."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            content_tokens=["Hello", " ", "world"],
        )

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True  # prevent background title generation

        session.send("Say hello")

        assert "Hello world" in ui.full_content

    def test_reasoning_tokens_appear(self, tmp_db):
        """Mock returns reasoning tokens then content; verify both captured."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            reasoning_tokens=["Let me", " think..."],
            content_tokens=["The answer", " is 56"],
        )

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True

        session.send("What is 7 * 8?")

        assert len(ui.reasoning_tokens) > 0
        assert "think" in ui.full_reasoning.lower()
        assert "56" in ui.full_content

    def test_stream_end_event(self, tmp_db):
        """stream_end event is emitted after response."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            content_tokens=["Hi"],
        )

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True

        session.send("Say hi")

        event_types = [e[0] for e in ui.events]
        assert "stream_end" in event_types

    def test_thinking_lifecycle(self, tmp_db):
        """thinking_start and thinking_stop bracket the response."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            content_tokens=["Hi", " there"],
        )

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True

        session.send("Say hi")

        event_types = [e[0] for e in ui.events]
        assert "thinking_start" in event_types
        assert "thinking_stop" in event_types
        start_idx = event_types.index("thinking_start")
        stop_idx = event_types.index("thinking_stop")
        assert start_idx < stop_idx


# ---------------------------------------------------------------------------
# Tests -- Tool calling (mocked LLM, real tool execution)
# ---------------------------------------------------------------------------


class TestToolCalling:
    """Test that mocked tool_calls trigger real tool execution."""

    def test_math_tool(self, tmp_db):
        """First call returns tool_call for math(code='2+2'), second returns content."""
        client = _mock_client()

        # First create() call: model requests math tool
        stream1 = make_mock_stream(
            tool_calls=[("call_math_1", "math", json.dumps({"code": "2+2"}))],
        )
        # Second create() call: model produces final answer
        stream2 = make_mock_stream(
            content_tokens=["The result is ", "4"],
        )
        client.chat.completions.create.side_effect = [stream1, stream2]

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True

        session.send("Calculate 2+2")

        # math tool was invoked and returned a result
        math_results = [r for r in ui.tool_results if r[1] == "math"]
        assert len(math_results) > 0
        assert "4" in math_results[0][2]

        # Final content contains the answer
        assert "4" in ui.full_content

    def test_bash_tool(self, tmp_db):
        """First call returns tool_call for bash, second returns content."""
        client = _mock_client()

        stream1 = make_mock_stream(
            tool_calls=[("call_bash_1", "bash", json.dumps({"command": "echo hello"}))],
        )
        stream2 = make_mock_stream(
            content_tokens=["The command printed: ", "hello"],
        )
        client.chat.completions.create.side_effect = [stream1, stream2]

        session, ui = _make_session(client, "mock-model", tmp_db)
        session._title_generated = True

        session.send("Run echo hello")

        bash_results = [r for r in ui.tool_results if r[1] == "bash"]
        assert len(bash_results) > 0
        assert "hello" in bash_results[0][2]

    def test_read_file_tool(self, tmp_db):
        """First call returns tool_call for read_file, second returns content."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("SECRET_CONTENT_42\n")
            path = f.name

        try:
            client = _mock_client()

            stream1 = make_mock_stream(
                tool_calls=[("call_read_1", "read_file", json.dumps({"path": path}))],
            )
            stream2 = make_mock_stream(
                content_tokens=["The file says: SECRET_CONTENT_42"],
            )
            client.chat.completions.create.side_effect = [stream1, stream2]

            session, ui = _make_session(client, "mock-model", tmp_db)
            session._title_generated = True

            session.send(f"Read {path}")

            read_results = [r for r in ui.tool_results if r[1] == "read_file"]
            assert len(read_results) > 0

            # Model relays the content
            assert "SECRET_CONTENT_42" in ui.full_content
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests -- Multi-turn conversation (mocked)
# ---------------------------------------------------------------------------


class TestMultiTurn:
    """Test multi-turn conversation state with mocked responses."""

    def test_context_retained(self, tmp_db):
        """Second send references context from the first."""
        client = _mock_client()

        stream1 = make_mock_stream(
            content_tokens=["I'll remember ", "Zephyr"],
        )
        stream2 = make_mock_stream(
            content_tokens=["Your name is ", "Zephyr"],
        )
        client.chat.completions.create.side_effect = [stream1, stream2]

        session, ui = _make_session(client, "mock-model", tmp_db, max_tokens=1024)
        session._title_generated = True

        session.send("My name is Zephyr. Remember it.")

        # Reset UI tracking for second turn
        ui.content_tokens.clear()
        ui.reasoning_tokens.clear()

        session.send("What is my name?")

        assert "zephyr" in ui.full_content.lower()

    def test_message_list_grows(self, tmp_db):
        """Each send adds user + assistant messages."""
        client = _mock_client()

        stream1 = make_mock_stream(content_tokens=["Hello"])
        stream2 = make_mock_stream(content_tokens=["World"])
        client.chat.completions.create.side_effect = [stream1, stream2]

        session, ui = _make_session(client, "mock-model", tmp_db, max_tokens=512)
        session._title_generated = True

        initial_count = len(session.messages)
        session.send("Hello")
        after_first = len(session.messages)
        assert after_first >= initial_count + 2

        session.send("World")
        after_second = len(session.messages)
        assert after_second >= after_first + 2


# ---------------------------------------------------------------------------
# Tests -- Session configuration (mocked)
# ---------------------------------------------------------------------------


class TestSessionConfig:
    """Test session construction and configuration with mocked responses."""

    def test_creative_mode_no_tools(self, tmp_db):
        """In creative mode, create() is called WITHOUT tools kwarg."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            content_tokens=["A haiku about code"],
        )

        session, ui = _make_session(client, "mock-model", tmp_db, max_tokens=256)
        session._title_generated = True
        session.creative_mode = True
        # Re-init system messages so creative_mode takes effect
        session._init_system_messages()

        session.send("Write a haiku about code.")

        # Verify create() was called without 'tools' in kwargs
        call_kwargs = client.chat.completions.create.call_args
        assert "tools" not in call_kwargs.kwargs, "tools should not be passed in creative mode"

        # Should get content back without tool calls
        assert len(ui.full_content) > 0
        assert len(ui.tool_results) == 0

    def test_custom_instructions(self, tmp_db):
        """Custom instructions appear in system messages."""
        client = _mock_client()
        client.chat.completions.create.return_value = make_mock_stream(
            content_tokens=["Hello. ENDMARKER"],
        )

        session, ui = _make_session(
            client,
            "mock-model",
            tmp_db,
            instructions="Always end your response with ENDMARKER.",
            max_tokens=512,
        )
        session._title_generated = True

        # Verify custom instructions appear in system messages
        dev_msg = session.system_messages[0]
        assert "ENDMARKER" in dev_msg["content"]

        session.send("Say hello briefly.")
        assert len(ui.errors) == 0


# ---------------------------------------------------------------------------
# Tests -- /health and /metrics endpoints (no live LLM required)
# ---------------------------------------------------------------------------


_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _server_jwt() -> str:
    from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

    return create_jwt(
        user_id="test-server-live",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
    )


_SERVER_AUTH_HEADERS = {"Authorization": f"Bearer {_server_jwt()}"}


class TestServerHealthMetrics:
    """Verify /health and /metrics endpoints using a Starlette TestClient.

    These tests create a Starlette app with a mock SessionManager
    so no live LLM backend is required.  Run them independently with:

        pytest tests/test_server_live.py::TestServerHealthMetrics -v
    """

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        import turnstone.server as srv_mod
        from turnstone.core.metrics import MetricsCollector
        from turnstone.core.workstream import WorkstreamState

        srv_mod._metrics = MetricsCollector()
        srv_mod._metrics.model = "test-model"

        mock_ui = MagicMock()
        mock_ui._ws_lock = threading.Lock()
        mock_ui._ws_prompt_tokens = 0
        mock_ui._ws_completion_tokens = 0
        mock_ui._ws_messages = 0
        mock_ui._ws_tool_calls = {}
        mock_ui._ws_context_ratio = 0.0

        mock_session = MagicMock()
        mock_session.ws_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.ui = mock_ui
        mock_ws.session = mock_session

        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]
        mock_mgr.max_active = 10

        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret=_TEST_JWT_SECRET,
        )
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.client.close()

    def _get(self, path) -> tuple[int, str, str]:
        """Make a GET request; return (status, content_type, body_str)."""
        resp = self.client.get(path)
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
        resp = self.client.get("/does-not-exist", headers=_SERVER_AUTH_HEADERS)
        assert resp.status_code == 404

    def test_health_contains_backend_field(self):
        _, _, body = self._get("/health")
        data = json.loads(body)
        assert "backend" in data
        assert data["backend"]["status"] in ("up", "down")

    def test_metrics_contains_sse_connections(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_sse_connections_active" in body

    def test_metrics_contains_ratelimit_counter(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_ratelimit_rejected_total" in body

    def test_metrics_contains_backend_up(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_backend_up" in body

    def test_metrics_no_circuit_state(self):
        """Circuit state metric was removed (passive health tracking only)."""
        _, _, body = self._get("/metrics")
        assert "turnstone_circuit_state" not in body

    def test_metrics_contains_eviction_counter(self):
        _, _, body = self._get("/metrics")
        assert "turnstone_workstreams_evicted_total" in body


class TestServerRateLimiting:
    """Verify per-IP rate limiting returns 429 with Retry-After header.

    Creates a Starlette app with a tight rate limiter (rate=2, burst=3)
    and verifies that requests beyond the burst are rejected.
    """

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        import turnstone.server as srv_mod
        from turnstone.core.metrics import MetricsCollector
        from turnstone.core.ratelimit import RateLimiter
        from turnstone.core.workstream import WorkstreamState

        srv_mod._metrics = MetricsCollector()
        srv_mod._metrics.model = "test-model"

        mock_ui = MagicMock()
        mock_ui._ws_lock = threading.Lock()
        mock_ui._ws_prompt_tokens = 0
        mock_ui._ws_completion_tokens = 0
        mock_ui._ws_messages = 0
        mock_ui._ws_tool_calls = {}
        mock_ui._ws_context_ratio = 0.0

        mock_session = MagicMock()
        mock_session.ws_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.ui = mock_ui
        mock_ws.session = mock_session

        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]
        mock_mgr.max_active = 10

        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret=_TEST_JWT_SECRET,
            rate_limiter=RateLimiter(enabled=True, rate=2.0, burst=3),
        )
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.client.close()

    def _get(self, path) -> httpx.Response:
        return self.client.get(path)

    def test_burst_requests_succeed(self):
        """First burst requests should all succeed."""
        for _ in range(3):
            resp = self._get("/health")
            assert resp.status_code == 200

    def test_exceeded_rate_returns_429(self):
        """After exhausting burst on a non-exempt endpoint, get 429."""
        # Exhaust burst on a non-exempt endpoint
        for _ in range(5):
            self.client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS)
        # At least one should be 429
        statuses = [
            self.client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS).status_code
            for _ in range(3)
        ]
        assert 429 in statuses

    def test_429_includes_retry_after(self):
        """429 response includes Retry-After header."""
        # Burn through burst
        for _ in range(10):
            resp = self.client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS)
            if resp.status_code == 429:
                assert "retry-after" in resp.headers
                data = resp.json()
                assert "retry_after" in data
                return
        pytest.skip("Did not hit rate limit in 10 requests")

    def test_health_exempt_from_ratelimit(self):
        """Health endpoint is always accessible regardless of rate limit."""
        # Burn through bucket on non-exempt path
        for _ in range(10):
            self.client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS)
        # Health should still work
        resp = self._get("/health")
        assert resp.status_code == 200

    def test_metrics_exempt_from_ratelimit(self):
        """Metrics endpoint is always accessible regardless of rate limit."""
        for _ in range(10):
            self.client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS)
        resp = self._get("/metrics")
        assert resp.status_code == 200
