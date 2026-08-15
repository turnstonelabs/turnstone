"""Lifecycle, admission, circuit, and real keep-alive tests for RerankLane."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from turnstone.core.admission import ModelAdmission
from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
from turnstone.core.rerank import (
    CohereJinaRerankClient,
    RerankCircuitOpenError,
    RerankHit,
    RerankLane,
    RerankRuntime,
    RerankRuntimeRetiredError,
    rerank,
)


class _Backend:
    """Thread-safe scripted backend with close and concurrency accounting."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.close_calls = 0
        self.fail = False
        self.entered = threading.Event()
        self.release: threading.Event | None = None
        self.on_call: Any = None
        self._lock = threading.Lock()

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        timeout: float | None = None,
    ) -> list[RerankHit]:
        del query, top_n, timeout
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.on_call is not None:
                self.on_call()
            if self.release is not None:
                assert self.release.wait(5), "test backend release timed out"
            if self.fail:
                raise RuntimeError("rerank endpoint failed")
            return [RerankHit(index=i, score=1.0 - i / 100) for i in range(len(documents))]
        finally:
            with self._lock:
                self.active -= 1

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1


def _lane(
    backend: _Backend,
    *,
    limit: int = 0,
    failure_threshold: int = 3,
    cooldown_seconds: float = 30.0,
    clock: Any = time.monotonic,
) -> RerankLane:
    runtime = RerankRuntime(
        backend,
        alias="rr",
        model="m",
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
        clock=clock,
    )
    return RerankLane(runtime, "rr", "m", ModelAdmission("rr", limit), 0)


def _call(lane: RerankLane, cancel_ref: Any = None) -> list[RerankHit]:
    return rerank(lane, "q", ["d0", "d1"], timeout=2.0, cancel_ref=cancel_ref)


class TestRuntimeLifecycle:
    def test_empty_batch_touches_no_runtime_state(self) -> None:
        backend = _Backend()
        lane = _lane(backend)

        assert rerank(lane, "q", [], timeout=1.0) == []
        snapshot = lane.runtime.snapshot()
        assert backend.calls == 0
        assert snapshot.active_calls == 0
        assert snapshot.circuit.state == "closed"
        assert snapshot.circuit.consecutive_failures == 0

    def test_retire_idle_closes_exactly_once(self) -> None:
        backend = _Backend()
        runtime = _lane(backend).runtime

        runtime.retire()
        runtime.retire()

        assert backend.close_calls == 1
        assert runtime.snapshot().retired
        assert runtime.snapshot().closed
        with pytest.raises(RerankRuntimeRetiredError):
            runtime.acquire_call()

    def test_active_call_drains_before_close_and_new_call_is_refused(self) -> None:
        backend = _Backend()
        backend.release = threading.Event()
        lane = _lane(backend)
        outcome: list[Any] = []

        worker = threading.Thread(target=lambda: outcome.append(_call(lane)), daemon=True)
        worker.start()
        assert backend.entered.wait(2)

        lane.runtime.retire()
        during = lane.runtime.snapshot()
        assert during.retired
        assert not during.closed
        assert backend.close_calls == 0
        with pytest.raises(RerankRuntimeRetiredError):
            _call(lane)
        assert backend.calls == 1

        backend.release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert len(outcome) == 1
        after = lane.runtime.snapshot()
        assert after.closed
        assert backend.close_calls == 1


class TestAdmission:
    def test_shared_alias_cap_bounds_peak_dispatch(self) -> None:
        backend = _Backend()
        backend.release = threading.Event()
        lane = _lane(backend, limit=2)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_call, lane) for _ in range(4)]
            deadline = time.monotonic() + 2
            while backend.active < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert backend.active == 2
            assert lane.admission.snapshot().queued == 2
            backend.release.set()
            assert all(len(f.result(timeout=2)) == 2 for f in futures)

        assert backend.calls == 4
        assert backend.max_active == 2
        assert lane.admission.snapshot().in_flight == 0

    def test_cancelled_waiter_is_removed_without_dispatch_or_circuit_failure(self) -> None:
        backend = _Backend()
        lane = _lane(backend, limit=1)
        held = lane.admission.acquire()
        cancel_ref = StreamAbortRef()
        errors: list[BaseException] = []

        def _waiter() -> None:
            try:
                _call(lane, cancel_ref)
            except BaseException as exc:  # capture the exact cancellation type
                errors.append(exc)

        worker = threading.Thread(target=_waiter, daemon=True)
        worker.start()
        deadline = time.monotonic() + 2
        while lane.admission.snapshot().queued != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lane.admission.snapshot().queued == 1
        cancel_ref.abort()
        worker.join(2)
        held.release()

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DeadlineCancelledError)
        assert backend.calls == 0
        snapshot = lane.runtime.snapshot()
        assert snapshot.circuit.state == "closed"
        assert snapshot.circuit.consecutive_failures == 0

    def test_post_request_cancellation_is_not_endpoint_failure(self) -> None:
        cancel_ref = StreamAbortRef()
        backend = _Backend()
        backend.on_call = cancel_ref.abort
        lane = _lane(backend)

        with pytest.raises(DeadlineCancelledError):
            _call(lane, cancel_ref)

        assert backend.calls == 1
        snapshot = lane.runtime.snapshot()
        assert snapshot.circuit.state == "closed"
        assert snapshot.circuit.consecutive_failures == 0

    def test_cancellation_wins_when_dispatched_request_also_fails(self) -> None:
        cancel_ref = StreamAbortRef()
        backend = _Backend()
        backend.on_call = cancel_ref.abort
        backend.fail = True
        lane = _lane(backend)

        with pytest.raises(DeadlineCancelledError):
            _call(lane, cancel_ref)

        assert backend.calls == 1
        snapshot = lane.runtime.snapshot()
        assert snapshot.circuit.state == "closed"
        assert snapshot.circuit.consecutive_failures == 0


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestCircuit:
    def test_open_fast_fallback_and_successful_half_open_probe(self) -> None:
        clock = _Clock()
        backend = _Backend()
        backend.fail = True
        lane = _lane(backend, failure_threshold=3, cooldown_seconds=30, clock=clock)

        for _ in range(3):
            with pytest.raises(RuntimeError, match="endpoint failed"):
                _call(lane)
        assert backend.calls == 3
        assert lane.runtime.snapshot().circuit.state == "open"

        with pytest.raises(RerankCircuitOpenError):
            _call(lane)
        assert backend.calls == 3  # no HTTP/backend dispatch while open

        clock.now += 30
        backend.fail = False
        assert len(_call(lane)) == 2
        assert backend.calls == 4
        recovered = lane.runtime.snapshot().circuit
        assert recovered.state == "closed"
        assert recovered.consecutive_failures == 0

    def test_open_circuit_preserves_bm25_order_without_another_dispatch(self) -> None:
        from turnstone.core.bm25 import BM25Index

        backend = _Backend()
        backend.fail = True
        lane = _lane(backend, failure_threshold=1)
        documents = ["alpha alpha", "alpha beta", "beta"]
        expected = BM25Index(documents).search("alpha", k=3)

        def _rank(query: str, candidates: list[str]) -> list[int]:
            return [hit.index for hit in rerank(lane, query, candidates, timeout=2.0)]

        index = BM25Index(documents, reranker=_rank)
        assert index.search("alpha", k=3) == expected
        assert backend.calls == 1
        assert index.search("alpha", k=3) == expected
        assert backend.calls == 1

    def test_only_one_half_open_probe_dispatches(self) -> None:
        clock = _Clock()
        backend = _Backend()
        backend.fail = True
        lane = _lane(backend, failure_threshold=1, cooldown_seconds=5, clock=clock)
        with pytest.raises(RuntimeError):
            _call(lane)

        clock.now += 5
        backend.fail = False
        backend.entered.clear()
        backend.release = threading.Event()
        outcome: list[Any] = []
        probe = threading.Thread(target=lambda: outcome.append(_call(lane)), daemon=True)
        probe.start()
        assert backend.entered.wait(2)

        with pytest.raises(RerankCircuitOpenError):
            _call(lane)
        assert backend.calls == 2

        backend.release.set()
        probe.join(2)
        assert not probe.is_alive()
        assert len(outcome) == 1
        assert lane.runtime.snapshot().circuit.state == "closed"

    def test_failed_half_open_probe_starts_fresh_cooldown(self) -> None:
        clock = _Clock()
        backend = _Backend()
        backend.fail = True
        lane = _lane(backend, failure_threshold=1, cooldown_seconds=10, clock=clock)
        with pytest.raises(RuntimeError):
            _call(lane)

        clock.now += 10
        with pytest.raises(RuntimeError):
            _call(lane)
        assert backend.calls == 2

        clock.now += 9
        with pytest.raises(RerankCircuitOpenError):
            _call(lane)
        assert backend.calls == 2

    def test_waiters_admitted_after_open_do_not_dispatch(self) -> None:
        backend = _Backend()
        backend.fail = True
        backend.release = threading.Event()
        lane = _lane(backend, limit=1, failure_threshold=3)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_call, lane) for _ in range(4)]
            assert backend.entered.wait(2)
            deadline = time.monotonic() + 2
            while lane.admission.snapshot().queued != 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert lane.admission.snapshot().queued == 3
            backend.release.set()
            errors = [future.exception(timeout=2) for future in futures]

        assert sum(type(error) is RuntimeError for error in errors) == 3
        assert sum(isinstance(error, RerankCircuitOpenError) for error in errors) == 1
        assert backend.calls == 3
        assert lane.admission.snapshot().in_flight == 0
        assert lane.runtime.snapshot().circuit.state == "open"


class _ConnectionCountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.accepted_connections = 0
        self.requests = 0
        super().__init__(("127.0.0.1", 0), _KeepAliveHandler)

    def get_request(self):
        request, address = super().get_request()
        self.accepted_connections += 1
        return request, address


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("content-length", "0"))
        json.loads(self.rfile.read(length))
        server = self.server
        assert isinstance(server, _ConnectionCountingServer)
        server.requests += 1
        body = json.dumps(
            {"results": [{"index": 0, "relevance_score": 0.9}]},
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args


def test_repeated_reranks_reuse_one_real_tcp_connection() -> None:
    server = _ConnectionCountingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/rerank"
    client = CohereJinaRerankClient(url, model="m")
    lane = RerankLane(
        RerankRuntime(client, alias="rr", model="m"),
        "rr",
        "m",
        ModelAdmission("rr"),
        0,
    )
    try:
        assert [hit.index for hit in _call(lane)] == [0]
        assert [hit.index for hit in _call(lane)] == [0]
        assert server.requests == 2
        assert server.accepted_connections == 1
    finally:
        lane.runtime.retire()
        server.shutdown()
        server.server_close()
        thread.join(2)
