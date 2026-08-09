"""Live request-count assertions for per-alias model admission.

These tests exercise the real OpenAI-compatible backend configured by the
same environment contract as :mod:`tests.test_server_live`.  A local threaded
reverse proxy sits between ``model_turn`` and that backend solely to count
HTTP requests.  It buffers each complete upstream response, then relays the
unchanged SSE payload to Turnstone's real streaming provider path.

Run explicitly (a backend must be listening at ``TURNSTONE_TEST_BASE_URL``,
which defaults to ``http://localhost:8000/v1``)::

    pytest tests/test_model_admission_live.py -m live -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from openai import OpenAI

from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import ModelLane, model_turn, resolve_model_binding
from turnstone.core.trajectory import Turn

if TYPE_CHECKING:
    from types import TracebackType


_LIVE_BASE_URL = os.environ.get("TURNSTONE_TEST_BASE_URL", "http://localhost:8000/v1")
_LIVE_API_KEY = os.environ.get("TURNSTONE_TEST_API_KEY", "not-needed") or "not-needed"
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
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
class _ObservedCounts:
    requests: int
    forwarded: int
    completed: int
    failed: int
    active: int
    peak: int
    forward_active: int
    forward_peak: int
    requests_by_alias: dict[str, int]
    forwarded_by_alias: dict[str, int]
    completed_by_alias: dict[str, int]
    peak_by_alias: dict[str, int]
    forward_peak_by_alias: dict[str, int]
    rendezvous_timed_out: bool


class _ConcurrencyCounter:
    """Thread-safe request counts with one bounded overlap rendezvous.

    The first ``rendezvous_size`` admitted requests wait for each other before
    any is forwarded upstream.  A short settling hold after the rendezvous
    makes an over-admission regression observable as a peak above the cap,
    independent of how quickly the live backend generates the tiny response.
    """

    def __init__(
        self,
        *,
        rendezvous_size: int,
        rendezvous_timeout: float = 3.0,
        settling_hold: float = 0.5,
    ) -> None:
        self._lock = threading.Lock()
        self._rendezvous = threading.Event()
        self._rendezvous_size = rendezvous_size
        self._rendezvous_timeout = rendezvous_timeout
        self._settling_hold = settling_hold
        self._requests = 0
        self._forwarded = 0
        self._completed = 0
        self._failed = 0
        self._active = 0
        self._peak = 0
        self._forward_active = 0
        self._forward_peak = 0
        self._active_by_alias: dict[str, int] = defaultdict(int)
        self._forward_active_by_alias: dict[str, int] = defaultdict(int)
        self._requests_by_alias: dict[str, int] = defaultdict(int)
        self._forwarded_by_alias: dict[str, int] = defaultdict(int)
        self._completed_by_alias: dict[str, int] = defaultdict(int)
        self._peak_by_alias: dict[str, int] = defaultdict(int)
        self._forward_peak_by_alias: dict[str, int] = defaultdict(int)
        self._rendezvous_timed_out = False

    def enter(self, alias: str) -> None:
        with self._lock:
            self._requests += 1
            self._requests_by_alias[alias] += 1
            self._active += 1
            self._active_by_alias[alias] += 1
            self._peak = max(self._peak, self._active)
            self._peak_by_alias[alias] = max(
                self._peak_by_alias[alias], self._active_by_alias[alias]
            )
            if self._active >= self._rendezvous_size:
                self._rendezvous.set()

        if not self._rendezvous.wait(self._rendezvous_timeout):
            # Release this and every later request so a broken cap fails by
            # count instead of leaving live-test worker threads parked.
            with self._lock:
                self._rendezvous_timed_out = True
                self._rendezvous.set()
        time.sleep(self._settling_hold)

    def begin_forward(self, alias: str) -> None:
        """Record one HTTP request entering the real upstream backend."""
        with self._lock:
            self._forwarded += 1
            self._forwarded_by_alias[alias] += 1
            self._forward_active += 1
            self._forward_active_by_alias[alias] += 1
            self._forward_peak = max(self._forward_peak, self._forward_active)
            self._forward_peak_by_alias[alias] = max(
                self._forward_peak_by_alias[alias],
                self._forward_active_by_alias[alias],
            )

    def end_forward(self, alias: str) -> None:
        with self._lock:
            self._forward_active -= 1
            self._forward_active_by_alias[alias] -= 1

    def leave(self, alias: str, *, completed: bool) -> None:
        with self._lock:
            self._active -= 1
            self._active_by_alias[alias] -= 1
            if completed:
                self._completed += 1
                self._completed_by_alias[alias] += 1
            else:
                self._failed += 1

    def snapshot(self) -> _ObservedCounts:
        with self._lock:
            return _ObservedCounts(
                requests=self._requests,
                forwarded=self._forwarded,
                completed=self._completed,
                failed=self._failed,
                active=self._active,
                peak=self._peak,
                forward_active=self._forward_active,
                forward_peak=self._forward_peak,
                requests_by_alias=dict(self._requests_by_alias),
                forwarded_by_alias=dict(self._forwarded_by_alias),
                completed_by_alias=dict(self._completed_by_alias),
                peak_by_alias=dict(self._peak_by_alias),
                forward_peak_by_alias=dict(self._forward_peak_by_alias),
                rendezvous_timed_out=self._rendezvous_timed_out,
            )


class _CountingProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, counter: _ConcurrencyCounter) -> None:
        super().__init__(("127.0.0.1", 0), _CountingProxyHandler)
        self.counter = counter
        self.upstream_base_url = _LIVE_BASE_URL.rstrip("/")
        self.upstream_authorization = f"Bearer {_LIVE_API_KEY}"
        self.alias_by_authorization: dict[str, str] = {}


class _CountingProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> _CountingProxyServer:
        if not isinstance(self.server, _CountingProxyServer):
            raise TypeError("counting handler requires _CountingProxyServer")
        return self.server

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        alias = self._proxy.alias_by_authorization.get(self.headers.get("Authorization", ""))
        if alias is None:
            self._write_json_error(401, "unknown live-test alias credential")
            return

        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError:
            self._write_json_error(400, "invalid content length")
            return
        body = self.rfile.read(content_length)
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
            # The temporary per-alias credentials are observation tags only;
            # the real backend sees exactly the configured live credential.
            headers["Authorization"] = self._proxy.upstream_authorization
            headers["Accept-Encoding"] = "identity"
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
            payload = upstream.content
            self.send_response(upstream.status_code)
            for key, value in upstream.headers.multi_items():
                if key.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            completed = upstream.status_code < 400
        except Exception:
            # The worker observes a normal 502; its exception and the failed
            # count retain the useful signal without leaking backend details.
            self._write_json_error(502, "live backend proxy failure")
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


class _CountingProxy:
    def __init__(self, counter: _ConcurrencyCounter) -> None:
        self._server = _CountingProxyServer(counter)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="model-admission-live-proxy",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        address = self._server.server_address
        host = address[0]
        port = address[1]
        if not isinstance(host, str) or not isinstance(port, int):
            raise TypeError("counting proxy did not bind an IPv4 TCP address")
        return f"http://{host}:{port}"

    def credential_for(self, alias: str) -> str:
        credential = f"turnstone-live-admission-{alias}"
        self._server.alias_by_authorization[f"Bearer {credential}"] = alias
        return credential

    def __enter__(self) -> _CountingProxy:
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
            raise AssertionError("live counting proxy did not stop")


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


def _live_lane(
    proxy: _CountingProxy,
    model: str,
    *,
    alias: str,
    limit: int,
) -> tuple[ModelRegistry, ModelLane]:
    config = ModelConfig(
        alias=alias,
        base_url=proxy.base_url,
        api_key=proxy.credential_for(alias),
        model=model,
        provider="openai-compatible",
        max_concurrency=limit,
    )
    registry = ModelRegistry({alias: config}, default=alias)
    try:
        binding = resolve_model_binding(registry, alias)
        # Bound each live call and disable SDK retries: the proxy's exact
        # request count should equal the number of model_turn invocations,
        # while model_turn still exercises the real provider and admission
        # lifecycle.
        client = binding.lane.client.with_options(timeout=60.0, max_retries=0)
        return registry, dataclasses.replace(binding.lane, client=client)
    except BaseException:
        registry.shutdown()
        raise


def _run_parallel(lanes: list[ModelLane], model: str) -> None:
    barrier = threading.Barrier(len(lanes))

    def _one_turn(lane: ModelLane) -> str:
        barrier.wait(timeout=10.0)
        result = model_turn(
            lane,
            [Turn.user("Reply with OK.")],
            max_tokens=8,
        )
        return result.serving_model

    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(_one_turn, lane) for lane in lanes]
        observed_models = [future.result(timeout=75.0) for future in futures]
    assert observed_models == [model] * len(lanes)


@pytest.mark.live
class TestLiveModelAdmissionCounts:
    """Count genuine backend requests at the per-alias admission boundary."""

    def test_alias_cap_bounds_exact_peak_and_completes_every_request(
        self, live_model_id: str
    ) -> None:
        counter = _ConcurrencyCounter(rendezvous_size=2)
        with _CountingProxy(counter) as proxy:
            registry, lane = _live_lane(proxy, live_model_id, alias="limited", limit=2)
            try:
                _run_parallel([lane, lane, lane, lane], live_model_id)
            finally:
                try:
                    lane.client.close()
                finally:
                    registry.shutdown()

        counts = counter.snapshot()
        assert counts.requests == 4
        assert counts.forwarded == 4
        assert counts.completed == 4
        assert counts.failed == 0
        assert counts.active == 0
        assert counts.peak == 2
        assert counts.forward_active == 0
        assert counts.forward_peak == 2
        assert counts.requests_by_alias == {"limited": 4}
        assert counts.forwarded_by_alias == {"limited": 4}
        assert counts.completed_by_alias == {"limited": 4}
        assert counts.peak_by_alias == {"limited": 2}
        assert counts.forward_peak_by_alias == {"limited": 2}
        assert counts.rendezvous_timed_out is False

    def test_aliases_sharing_endpoint_have_independent_caps(self, live_model_id: str) -> None:
        counter = _ConcurrencyCounter(rendezvous_size=2)
        with _CountingProxy(counter) as proxy:
            configs = {
                alias: ModelConfig(
                    alias=alias,
                    base_url=proxy.base_url,
                    api_key=proxy.credential_for(alias),
                    model=live_model_id,
                    provider="openai-compatible",
                    max_concurrency=1,
                )
                for alias in ("alpha", "beta")
            }
            # The two registry aliases deliberately name the exact same URL;
            # both requests are then forwarded to the same physical backend.
            assert configs["alpha"].base_url == configs["beta"].base_url
            registry = ModelRegistry(configs, default="alpha")
            lanes: list[ModelLane] = []
            try:
                alpha = resolve_model_binding(registry, "alpha").lane
                beta = resolve_model_binding(registry, "beta").lane
                alpha = dataclasses.replace(
                    alpha, client=alpha.client.with_options(timeout=60.0, max_retries=0)
                )
                lanes.append(alpha)
                beta = dataclasses.replace(
                    beta, client=beta.client.with_options(timeout=60.0, max_retries=0)
                )
                lanes.append(beta)
                _run_parallel(lanes, live_model_id)
            finally:
                try:
                    for lane in lanes:
                        lane.client.close()
                finally:
                    registry.shutdown()

        counts = counter.snapshot()
        assert counts.requests == 2
        assert counts.forwarded == 2
        assert counts.completed == 2
        assert counts.failed == 0
        assert counts.active == 0
        assert counts.peak == 2
        assert counts.forward_active == 0
        assert counts.forward_peak == 2
        assert counts.requests_by_alias == {"alpha": 1, "beta": 1}
        assert counts.forwarded_by_alias == {"alpha": 1, "beta": 1}
        assert counts.completed_by_alias == {"alpha": 1, "beta": 1}
        assert counts.peak_by_alias == {"alpha": 1, "beta": 1}
        assert counts.forward_peak_by_alias == {"alpha": 1, "beta": 1}
        assert counts.rendezvous_timed_out is False
