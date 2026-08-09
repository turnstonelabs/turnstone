"""Live request-count assertions for parallel intent-judge batches.

These tests run :class:`turnstone.core.judge.IntentJudge` through the real
OpenAI-compatible backend configured by ``TURNSTONE_TEST_BASE_URL`` (the same
contract as :mod:`tests.test_server_live`).  A local threaded proxy counts the
HTTP fan-out and fully drains each genuine upstream response.  It then returns
a deterministic, valid Chat Completions SSE verdict so the assertions measure
Turnstone's scheduler rather than a live model's JSON-formatting reliability.

Run explicitly with a live backend::

    pytest tests/test_judge_parallel_live.py -m live -v
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from openai import OpenAI

from tests.test_model_admission_live import (
    _HOP_BY_HOP_HEADERS,
    _LIVE_API_KEY,
    _LIVE_BASE_URL,
    _ConcurrencyCounter,
)
from turnstone.core.judge import IntentJudge, IntentVerdict, JudgeConfig
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import resolve_model_binding

if TYPE_CHECKING:
    from types import TracebackType


_VERDICT_CONTENT = json.dumps(
    {
        "intent_summary": "Live scheduler probe",
        "risk_level": "low",
        "confidence": 0.99,
        "recommendation": "approve",
        "reasoning": "The deterministic proxy verdict confirms one completed judge dispatch.",
        "evidence": ["The genuine upstream response was drained before this verdict."],
    },
    separators=(",", ":"),
)


def _deterministic_sse() -> bytes:
    chunks = (
        {
            "id": "turnstone-live-judge",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "turnstone-live-judge",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": _VERDICT_CONTENT},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "turnstone-live-judge",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "turnstone-live-judge",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    framed = "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
    return (framed + "data: [DONE]\n\n").encode()


class _JudgeCountingProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, counter: _ConcurrencyCounter) -> None:
        super().__init__(("127.0.0.1", 0), _JudgeCountingProxyHandler)
        self.counter = counter
        self.upstream_base_url = _LIVE_BASE_URL.rstrip("/")
        self.upstream_authorization = f"Bearer {_LIVE_API_KEY}"
        self.alias_by_authorization: dict[str, str] = {}


class _JudgeCountingProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> _JudgeCountingProxyServer:
        if not isinstance(self.server, _JudgeCountingProxyServer):
            raise TypeError("judge counting handler requires _JudgeCountingProxyServer")
        return self.server

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        alias = self._proxy.alias_by_authorization.get(self.headers.get("Authorization", ""))
        if alias is None:
            self._write_json_error(401, "unknown live-test alias credential")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json_error(400, "invalid content length")
            return
        body = self.rfile.read(content_length)
        # The test measures dispatch, admission, and full upstream drain.  Keep
        # the real probe inexpensive even if the configured model tends to
        # deliberate: its content is replaced only after the response completes.
        try:
            upstream_body = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            upstream_body = None
        if isinstance(upstream_body, dict):
            if "max_tokens" in upstream_body:
                upstream_body["max_tokens"] = min(int(upstream_body["max_tokens"]), 16)
            if "max_completion_tokens" in upstream_body:
                upstream_body["max_completion_tokens"] = min(
                    int(upstream_body["max_completion_tokens"]), 16
                )
            body = json.dumps(upstream_body, separators=(",", ":")).encode()

        counter = self._proxy.counter
        counter.enter(alias)
        completed = False
        try:
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in _HOP_BY_HOP_HEADERS
                and key.lower() not in {"authorization", "host"}
            }
            headers["Authorization"] = self._proxy.upstream_authorization
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(body))
            counter.begin_forward(alias)
            try:
                upstream = httpx.post(
                    f"{self._proxy.upstream_base_url}{self.path}",
                    content=body,
                    headers=headers,
                    timeout=httpx.Timeout(60.0, connect=5.0),
                )
            finally:
                counter.end_forward(alias)
            # ``httpx.post`` has consumed the complete streaming body here.
            # A genuine backend rejection must fail the probe; only a successful
            # response is replaced with deterministic judge output.
            upstream.raise_for_status()
            payload = _deterministic_sse()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            completed = True
        except Exception:
            self._write_json_error(502, "live backend judge proxy failure")
        finally:
            counter.leave(alias, completed=completed)

    def log_message(self, _format: str, *args: Any) -> None:
        """Keep expected local proxy traffic out of pytest output."""

    def _write_json_error(self, status: int, detail: str) -> None:
        payload = json.dumps({"error": {"message": detail}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            pass


class _JudgeCountingProxy:
    def __init__(self, counter: _ConcurrencyCounter) -> None:
        self._server = _JudgeCountingProxyServer(counter)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="judge-parallel-live-proxy",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        if not isinstance(host, str) or not isinstance(port, int):
            raise TypeError("judge counting proxy did not bind an IPv4 TCP address")
        return f"http://{host}:{port}"

    def credential_for(self, alias: str) -> str:
        credential = f"turnstone-live-judge-{alias}"
        self._server.alias_by_authorization[f"Bearer {credential}"] = alias
        return credential

    def __enter__(self) -> _JudgeCountingProxy:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise AssertionError("live judge counting proxy did not stop")


@pytest.fixture(scope="module")
def live_model_id() -> str:
    """Auto-detect the model using the established live-backend contract."""
    client = OpenAI(base_url=_LIVE_BASE_URL, api_key=_LIVE_API_KEY)
    try:
        models = client.models.list()
    finally:
        client.close()
    ids = [model.id for model in models.data]
    assert ids, "No models found on the live backend"
    return ids[0]


@dataclass(frozen=True)
class _JudgeRun:
    heuristics: list[IntentVerdict]
    finals: list[IntentVerdict]
    done_calls: int


def _run_live_judge_batch(
    proxy: _JudgeCountingProxy,
    model: str,
    *,
    alias_limit: int,
    parallel_evaluations: int,
    batch_size: int,
) -> _JudgeRun:
    alias = "live-judge"
    config = ModelConfig(
        alias=alias,
        base_url=proxy.base_url,
        api_key=proxy.credential_for(alias),
        model=model,
        provider="openai-compatible",
        max_concurrency=alias_limit,
    )
    registry = ModelRegistry({alias: config}, default=alias)
    done = threading.Event()
    finals: list[IntentVerdict] = []
    done_calls = 0

    def _done() -> None:
        nonlocal done_calls
        done_calls += 1
        done.set()

    try:
        binding = resolve_model_binding(registry, alias)
        judge = IntentJudge(
            JudgeConfig(
                enabled=True,
                read_only_tools=False,
                timeout=60.0,
                parallel_evaluations=parallel_evaluations,
            ),
            binding,
        )
        items = [
            {
                "func_name": "bash",
                "func_args": {"command": f"echo live-{index}"},
                "approval_label": "bash",
                "call_id": f"live-call-{index}",
            }
            for index in range(batch_size)
        ]
        heuristics = judge.evaluate(
            items,
            [{"role": "user", "content": "Run the independent live scheduler probes."}],
            finals.append,
            done_callback=_done,
        )
        assert done.wait(90.0), "live intent-judge batch did not finish"
        return _JudgeRun(heuristics=heuristics, finals=finals, done_calls=done_calls)
    finally:
        registry.shutdown()


def _assert_exact_batch(run: _JudgeRun, *, batch_size: int) -> None:
    expected_ids = {f"live-call-{index}" for index in range(batch_size)}
    assert len(run.heuristics) == batch_size
    assert len(run.finals) == batch_size
    assert Counter(verdict.call_id for verdict in run.finals) == Counter(
        {call_id: 1 for call_id in expected_ids}
    )
    assert all(verdict.tier == "llm" for verdict in run.finals)
    assert all(verdict.intent_summary == "Live scheduler probe" for verdict in run.finals)
    assert run.done_calls == 1


@pytest.mark.live
class TestLiveParallelJudgeCounts:
    """Count genuine upstream calls made by the intent-judge scheduler."""

    def test_parallel_width_sets_exact_batch_peak(self, live_model_id: str) -> None:
        counter = _ConcurrencyCounter(rendezvous_size=3)
        with _JudgeCountingProxy(counter) as proxy:
            run = _run_live_judge_batch(
                proxy,
                live_model_id,
                alias_limit=0,
                parallel_evaluations=3,
                batch_size=5,
            )

        _assert_exact_batch(run, batch_size=5)
        counts = counter.snapshot()
        assert counts.requests == 5
        assert counts.forwarded == 5
        assert counts.completed == 5
        assert counts.failed == 0
        assert counts.active == 0
        assert counts.peak == 3
        assert counts.forward_active == 0
        assert counts.forward_peak == 3
        assert counts.requests_by_alias == {"live-judge": 5}
        assert counts.forwarded_by_alias == {"live-judge": 5}
        assert counts.completed_by_alias == {"live-judge": 5}
        assert counts.peak_by_alias == {"live-judge": 3}
        assert counts.forward_peak_by_alias == {"live-judge": 3}
        assert counts.rendezvous_timed_out is False

    def test_alias_cap_reduces_parallel_judge_peak(self, live_model_id: str) -> None:
        counter = _ConcurrencyCounter(rendezvous_size=2)
        with _JudgeCountingProxy(counter) as proxy:
            run = _run_live_judge_batch(
                proxy,
                live_model_id,
                alias_limit=2,
                parallel_evaluations=4,
                batch_size=4,
            )

        _assert_exact_batch(run, batch_size=4)
        counts = counter.snapshot()
        assert counts.requests == 4
        assert counts.forwarded == 4
        assert counts.completed == 4
        assert counts.failed == 0
        assert counts.active == 0
        assert counts.peak == 2
        assert counts.forward_active == 0
        assert counts.forward_peak == 2
        assert counts.requests_by_alias == {"live-judge": 4}
        assert counts.forwarded_by_alias == {"live-judge": 4}
        assert counts.completed_by_alias == {"live-judge": 4}
        assert counts.peak_by_alias == {"live-judge": 2}
        assert counts.forward_peak_by_alias == {"live-judge": 2}
        assert counts.rendezvous_timed_out is False
