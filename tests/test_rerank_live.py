"""Live lifecycle and admission checks for the managed rerank runtime.

The local counting proxy preserves real endpoint responses while making the
client-side pool and admission boundary observable. Run explicitly against a
Cohere/Jina-compatible endpoint::

    TURNSTONE_LIVE_RERANK_URL=http://127.0.0.1:8000/rerank \
    TURNSTONE_LIVE_RERANK_MODEL=my-reranker \
    pytest tests/test_rerank_live.py -m live -v

``TURNSTONE_LIVE_RERANK_API_KEY`` is optional.
"""

from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.rerank import RerankHit, RerankLane, rerank

if TYPE_CHECKING:
    from types import TracebackType


_LIVE_URL = os.environ.get("TURNSTONE_LIVE_RERANK_URL", "").strip()
_LIVE_MODEL = os.environ.get("TURNSTONE_LIVE_RERANK_MODEL", "").strip()
_LIVE_API_KEY = os.environ.get("TURNSTONE_LIVE_RERANK_API_KEY", "").strip()
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True)
class _Observed:
    requests: int
    active: int
    peak: int
    accepted_connections: int


class _Counter:
    """Count proxy requests and optionally hold the first admitted cohort."""

    def __init__(self, *, rendezvous_size: int = 1, hold_seconds: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._rendezvous = threading.Event()
        self._rendezvous_size = rendezvous_size
        self._hold_seconds = hold_seconds
        self._requests = 0
        self._active = 0
        self._peak = 0
        self._accepted_connections = 0
        if rendezvous_size <= 1:
            self._rendezvous.set()

    def accepted(self) -> None:
        with self._lock:
            self._accepted_connections += 1

    def enter(self) -> None:
        with self._lock:
            self._requests += 1
            self._active += 1
            self._peak = max(self._peak, self._active)
            if self._active >= self._rendezvous_size:
                self._rendezvous.set()
        assert self._rendezvous.wait(10), "live rerank requests did not overlap"
        if self._hold_seconds:
            time.sleep(self._hold_seconds)

    def leave(self) -> None:
        with self._lock:
            self._active -= 1

    def snapshot(self) -> _Observed:
        with self._lock:
            return _Observed(
                requests=self._requests,
                active=self._active,
                peak=self._peak,
                accepted_connections=self._accepted_connections,
            )


class _LiveProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, counter: _Counter) -> None:
        self.counter = counter
        headers = {"Authorization": f"Bearer {_LIVE_API_KEY}"} if _LIVE_API_KEY else {}
        self.upstream = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        super().__init__(("127.0.0.1", 0), _LiveProxyHandler)

    def get_request(self):
        request, address = super().get_request()
        self.counter.accepted()
        return request, address


class _LiveProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> _LiveProxyServer:
        if not isinstance(self.server, _LiveProxyServer):
            raise TypeError("live rerank handler requires _LiveProxyServer")
        return self.server

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self._proxy.counter.enter()
        try:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "authorization"
            }
            response = self._proxy.upstream.post(_LIVE_URL, content=body, headers=headers)
            payload = response.content
            self.send_response(response.status_code)
            content_type = response.headers.get("content-type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        finally:
            self._proxy.counter.leave()

    def log_message(self, _format: str, *args: Any) -> None:
        del args


class _LiveProxy:
    def __init__(self, counter: _Counter) -> None:
        self._server = _LiveProxyServer(counter)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="rerank-live-proxy",
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/rerank"

    def __enter__(self) -> _LiveProxy:
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
        self._server.upstream.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError("live rerank proxy did not stop")


def _registry_lane(url: str, *, limit: int) -> tuple[ModelRegistry, RerankLane]:
    cfg = ModelConfig(
        alias="live-reranker",
        base_url=url,
        api_key="",
        model=_LIVE_MODEL,
        max_concurrency=limit,
        capabilities={"supports_rerank": True},
    )
    registry = ModelRegistry({cfg.alias: cfg}, default=cfg.alias)
    return registry, registry.resolve_rerank_lane(cfg.alias)


def _dispatch(lane: RerankLane) -> list[RerankHit]:
    return rerank(
        lane,
        "What is the capital of France?",
        ["Paris is the capital of France.", "Whales are mammals."],
        timeout=30.0,
    )


@pytest.mark.live
@pytest.mark.skipif(
    not _LIVE_URL or not _LIVE_MODEL,
    reason="TURNSTONE_LIVE_RERANK_URL and TURNSTONE_LIVE_RERANK_MODEL are required",
)
class TestLiveRerankRuntime:
    def test_real_scores_and_reuses_one_client_connection(self) -> None:
        counter = _Counter()
        with _LiveProxy(counter) as proxy:
            registry, first_lane = _registry_lane(proxy.url, limit=2)
            try:
                second_lane = registry.resolve_rerank_lane("live-reranker")
                assert second_lane.runtime is first_lane.runtime
                first = _dispatch(first_lane)
                second = _dispatch(second_lane)
            finally:
                registry.shutdown()

        assert first[0].index == second[0].index == 0
        assert all(math.isfinite(hit.score) for hit in [*first, *second])
        observed = counter.snapshot()
        assert observed.requests == 2
        assert observed.active == 0
        assert observed.accepted_connections == 1

    def test_real_endpoint_never_exceeds_alias_cap(self) -> None:
        counter = _Counter(rendezvous_size=2, hold_seconds=0.25)
        with _LiveProxy(counter) as proxy:
            registry, lane = _registry_lane(proxy.url, limit=2)
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(_dispatch, lane) for _ in range(4)]
                    results = [future.result(timeout=75) for future in futures]
            finally:
                registry.shutdown()

        assert all(result and result[0].index == 0 for result in results)
        observed = counter.snapshot()
        assert observed.requests == 4
        assert observed.active == 0
        assert observed.peak == 2
