"""Endpoint-backed reranking (Cohere/Jina-compatible wire format).

Turnstone performs no in-process model inference — reranking is delegated to an
external rerank endpoint, exactly like every other model the platform talks to.
The endpoint must speak the de-facto-standard Cohere/Jina ``/rerank`` contract,
which is also implemented by self-hosted servers (vLLM, Text Embeddings
Inference, llama.cpp) and the hosted Cohere / Jina / Voyage APIs.

Request (POST to the configured URL)::

    {"model": "<name>", "query": "<q>", "documents": ["...", ...], "top_n": N}

Response — two shapes are accepted::

    {"results": [{"index": 0, "relevance_score": 0.91}, ...]}   # Cohere/Jina/vLLM
    [{"index": 0, "score": 0.91}, ...]                          # bare list (TEI)

Reranking is disabled unless an endpoint URL is configured; there is no bundled
default and no fall back to a local model.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from turnstone.core.deadline import DeadlineCancelledError
from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.admission import AdmissionLease, ModelAdmission

log = get_logger(__name__)

# A reranker reorders candidate documents by relevance to the query, returning
# their indices best-first. Defined here so bm25.py and web_search.py can share
# the type without importing each other (rerank.py imports neither).
Reranker = Callable[[str, list[str]], list[int]]


class RerankError(RuntimeError):
    """A rerank endpoint returned no usable scores for a non-empty input.

    A conforming reranker scores every document, so an empty result for
    non-empty input means the response was unparseable / non-conforming -- an
    endpoint failure, distinct from a relevance floor dropping every candidate.
    Callers raise this so retrieval falls back to BM25 order rather than
    treating the failure as "nothing relevant".
    """


class RerankCircuitOpenError(RerankError):
    """The endpoint circuit is open, so no rerank request was dispatched."""


class RerankRuntimeRetiredError(RerankError):
    """The resolved runtime retired before this batch could dispatch."""


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic sigmoid (maps a logit to a probability)."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def normalize_scores(scores: list[float]) -> list[float]:
    """Map a batch of raw rerank scores into a 0-1 relevance space.

    Cohere/Jina/Qwen endpoints already return 0-1 relevance; cross-encoders
    (bge, TEI) return raw logits. If ANY score in the batch falls outside
    ``[0, 1]`` the batch is treated as logits and squashed with the logistic
    sigmoid (the canonical logit -> P(relevant) map); otherwise the scores are
    already probabilities and pass through unchanged. Detection is per-batch
    because one rerank response comes from one model, and a real batch mixes
    relevant and irrelevant docs so a logit model reveals out-of-range (often
    negative) scores. Sigmoid is monotonic, so ranking ORDER is never affected
    -- only a threshold comparison gains a consistent 0-1 meaning.
    """
    if not scores:
        return []
    if all(0.0 <= s <= 1.0 for s in scores):
        return list(scores)
    return [_sigmoid(s) for s in scores]


@dataclass(frozen=True)
class RerankHit:
    """One reranked document: its position in the input list and its score."""

    index: int  # 0-based index into the documents passed to ``rerank``
    score: float  # relevance score; higher is more relevant


class RerankClient(Protocol):
    """Minimal interface for a rerank backend."""

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        timeout: float | None = None,
    ) -> list[RerankHit]:
        """Score ``documents`` against ``query``; return hits sorted best-first."""
        ...


@dataclass(frozen=True, slots=True)
class RerankCircuitSnapshot:
    """Non-sensitive instantaneous breaker state for diagnostics and tests."""

    state: str
    consecutive_failures: int


@dataclass(frozen=True, slots=True)
class RerankRuntimeSnapshot:
    """Non-sensitive instantaneous lifecycle state for diagnostics and tests."""

    alias: str
    model: str
    active_calls: int
    retired: bool
    closed: bool
    circuit: RerankCircuitSnapshot


@dataclass(frozen=True, slots=True)
class _CircuitPermit:
    epoch: int
    probe: bool


class _RerankCircuit:
    """Small thread-safe consecutive-failure circuit for one rerank runtime."""

    def __init__(
        self,
        alias: str,
        model: str,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._alias = alias
        self._model = model
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        # Invalidates completions from calls admitted before a transition.
        self._epoch = 0

    def acquire(self) -> _CircuitPermit:
        with self._lock:
            if self._state == "closed":
                return _CircuitPermit(self._epoch, False)
            if self._state == "half_open":
                raise RerankCircuitOpenError(
                    f"rerank circuit is half-open for alias {self._alias!r}"
                )
            opened_at = self._opened_at
            if opened_at is None or self._clock() - opened_at < self._cooldown_seconds:
                raise RerankCircuitOpenError(f"rerank circuit is open for alias {self._alias!r}")
            self._state = "half_open"
            log.info(
                "rerank.circuit_half_open alias=%s model=%s",
                self._alias,
                self._model,
            )
            return _CircuitPermit(self._epoch, True)

    def validate(self, permit: _CircuitPermit) -> None:
        """Refuse a permit invalidated while its caller waited for admission."""
        with self._lock:
            expected_state = "half_open" if permit.probe else "closed"
            if permit.epoch != self._epoch or self._state != expected_state:
                raise RerankCircuitOpenError(
                    f"rerank circuit changed while waiting for alias {self._alias!r}"
                )

    def succeed(self, permit: _CircuitPermit) -> None:
        recovered = False
        with self._lock:
            if permit.epoch != self._epoch:
                return
            if permit.probe:
                if self._state != "half_open":
                    return
                self._state = "closed"
                self._opened_at = None
                self._consecutive_failures = 0
                self._epoch += 1
                recovered = True
            elif self._state == "closed":
                self._consecutive_failures = 0
        if recovered:
            log.info(
                "rerank.circuit_recovered alias=%s model=%s",
                self._alias,
                self._model,
            )

    def fail(self, permit: _CircuitPermit) -> None:
        opened = False
        failures = 0
        with self._lock:
            if permit.epoch != self._epoch:
                return
            if permit.probe:
                if self._state != "half_open":
                    return
                self._state = "open"
                self._opened_at = self._clock()
                self._consecutive_failures = self._failure_threshold
                self._epoch += 1
                failures = self._consecutive_failures
                opened = True
            elif self._state == "closed":
                self._consecutive_failures += 1
                failures = self._consecutive_failures
                if failures >= self._failure_threshold:
                    self._state = "open"
                    self._opened_at = self._clock()
                    self._epoch += 1
                    opened = True
        if opened:
            log.warning(
                "rerank.circuit_open alias=%s model=%s failures=%d",
                self._alias,
                self._model,
                failures,
            )

    def abandon(self, permit: _CircuitPermit) -> None:
        """Release a half-open reservation without judging endpoint health."""
        with self._lock:
            if permit.epoch == self._epoch and permit.probe and self._state == "half_open":
                # Preserve the original opened_at. Its cooldown has already
                # elapsed, so the next caller may become the replacement probe.
                self._state = "open"

    def snapshot(self) -> RerankCircuitSnapshot:
        with self._lock:
            return RerankCircuitSnapshot(self._state, self._consecutive_failures)


class _RerankRuntimeLease:
    """One idempotently releasable active-call hold on a runtime."""

    __slots__ = ("_released", "_runtime")

    def __init__(self, runtime: RerankRuntime) -> None:
        self._runtime = runtime
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._runtime._release_call()


class RerankRuntime:
    """Lifecycle-owned backend, circuit, and active-call retirement state."""

    def __init__(
        self,
        client: RerankClient,
        *,
        alias: str,
        model: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("rerank circuit failure threshold must be positive")
        if cooldown_seconds < 0:
            raise ValueError("rerank circuit cooldown must be non-negative")
        self._client = client
        self.alias = alias
        self.model = model
        self._state_lock = threading.Lock()
        self._active_calls = 0
        self._retired = False
        self._closed = False
        self._circuit = _RerankCircuit(
            alias,
            model,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )

    @property
    def client(self) -> RerankClient:
        return self._client

    def acquire_call(self) -> _RerankRuntimeLease:
        with self._state_lock:
            if self._retired or self._closed:
                raise RerankRuntimeRetiredError(f"rerank runtime retired for alias {self.alias!r}")
            self._active_calls += 1
        return _RerankRuntimeLease(self)

    def _release_call(self) -> None:
        close_now = False
        with self._state_lock:
            if self._active_calls <= 0:
                raise RuntimeError("rerank runtime lease released without an active call")
            self._active_calls -= 1
            if self._retired and self._active_calls == 0 and not self._closed:
                self._closed = True
                close_now = True
        if close_now:
            self._close_client()

    def begin_retirement(self) -> Callable[[], None] | None:
        """Prevent new calls and return an idle close action for the caller.

        The split lets ``ModelRegistry`` mark the runtime while holding its
        short-lived registry lock, then execute a potentially blocking client
        close after releasing that lock. Active calls close from their final
        lease release instead.
        """
        with self._state_lock:
            self._retired = True
            if self._active_calls or self._closed:
                return None
            self._closed = True
        return self._close_client

    def retire(self) -> None:
        close = self.begin_retirement()
        if close is not None:
            close()

    def _close_client(self) -> None:
        close = getattr(self._client, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            log.warning(
                "rerank.runtime_close_failed alias=%s model=%s",
                self.alias,
                self.model,
                exc_info=True,
            )

    def circuit_acquire(self) -> _CircuitPermit:
        return self._circuit.acquire()

    def circuit_succeed(self, permit: _CircuitPermit) -> None:
        self._circuit.succeed(permit)

    def circuit_validate(self, permit: _CircuitPermit) -> None:
        self._circuit.validate(permit)

    def circuit_fail(self, permit: _CircuitPermit) -> None:
        self._circuit.fail(permit)

    def circuit_abandon(self, permit: _CircuitPermit) -> None:
        self._circuit.abandon(permit)

    def snapshot(self) -> RerankRuntimeSnapshot:
        with self._state_lock:
            active_calls = self._active_calls
            retired = self._retired
            closed = self._closed
        return RerankRuntimeSnapshot(
            alias=self.alias,
            model=self.model,
            active_calls=active_calls,
            retired=retired,
            closed=closed,
            circuit=self._circuit.snapshot(),
        )


@dataclass(frozen=True, slots=True)
class RerankLane:
    """One immutable, per-batch binding to a shared rerank runtime."""

    runtime: RerankRuntime
    alias: str
    model: str
    admission: ModelAdmission
    registry_generation: int
    config_version: int = 0


def _raise_if_aborted(cancel_ref: Any) -> None:
    if bool(getattr(cancel_ref, "aborted", False)):
        raise DeadlineCancelledError("rerank cancelled")


def rerank(
    lane: RerankLane,
    query: str,
    documents: list[str],
    *,
    top_n: int | None = None,
    timeout: float,
    cancel_ref: Any = None,
) -> list[RerankHit]:
    """Dispatch one rerank batch through circuit, admission, and runtime leases."""
    if not documents:
        return []
    _raise_if_aborted(cancel_ref)
    permit = lane.runtime.circuit_acquire()
    admission: AdmissionLease | None = None
    runtime_lease: _RerankRuntimeLease | None = None
    dispatched = False
    try:
        admission = lane.admission.acquire(cancel_ref=cancel_ref)
        # The circuit can open while this call is queued behind either rerank
        # or model work on the shared alias gate. Do not let an old closed-state
        # permit leak one more full-timeout request after that transition.
        lane.runtime.circuit_validate(permit)
        runtime_lease = lane.runtime.acquire_call()
        _raise_if_aborted(cancel_ref)
        mark_dispatch = getattr(cancel_ref, "mark_dispatch", None)
        if callable(mark_dispatch):
            with contextlib.suppress(Exception):
                mark_dispatch()
        dispatched = True
        hits = lane.runtime.client.rerank(
            query,
            documents,
            top_n=top_n,
            timeout=timeout,
        )
        _raise_if_aborted(cancel_ref)
        if not hits:
            raise RerankError("rerank endpoint returned no scores for non-empty input")
    except (DeadlineCancelledError, RerankCircuitOpenError, RerankRuntimeRetiredError):
        lane.runtime.circuit_abandon(permit)
        raise
    except Exception:
        try:
            # Stop/supersession owns the outcome even when the dispatched
            # transport fails while cancellation is landing. Do not charge a
            # cancelled request to endpoint health or let retrieval fallback
            # absorb it as an ordinary rerank outage.
            _raise_if_aborted(cancel_ref)
        except DeadlineCancelledError:
            lane.runtime.circuit_abandon(permit)
            raise
        if dispatched:
            lane.runtime.circuit_fail(permit)
        else:
            # Admission and lifecycle failures say nothing about endpoint
            # health; only a call that reached the backend can trip its circuit.
            lane.runtime.circuit_abandon(permit)
        raise
    except BaseException:
        lane.runtime.circuit_abandon(permit)
        raise
    else:
        lane.runtime.circuit_succeed(permit)
        return hits
    finally:
        if runtime_lease is not None:
            runtime_lease.release()
        if admission is not None:
            admission.release()


class CohereJinaRerankClient:
    """Rerank via a Cohere/Jina-compatible ``POST <url>`` endpoint.

    ``url`` is the *full* endpoint (including path), because the path differs by
    provider — ``/rerank`` (vLLM, TEI), ``/v1/rerank`` (Jina, llama.cpp),
    ``/v2/rerank`` (Cohere). The request body and the ``results`` /
    ``relevance_score`` response are shared across all of them.
    """

    def __init__(
        self,
        url: str,
        model: str = "",
        api_key: str = "",
        timeout: float = 30,
        instruction: str = "",
    ) -> None:
        self._url = url
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._instruction = instruction
        self._client = httpx.Client()
        self._close_lock = threading.Lock()
        self._closed = False

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        timeout: float | None = None,
    ) -> list[RerankHit]:
        if not documents:
            return []
        # Instruction-aware rerankers (Qwen3-Reranker) need the instruction in the
        # query; vLLM's /rerank does not inject it (a bare query can even invert
        # relevance). Wrap with the model's <Instruct>/<Query> framing — it frames
        # the <Document> side itself. Empty instruction -> bare query, which is
        # correct for Cohere/Jina/bge cross-encoders.
        q = f"<Instruct>: {self._instruction}\n<Query>: {query}" if self._instruction else query
        payload: dict[str, Any] = {"query": q, "documents": list(documents)}
        if self._model:
            payload["model"] = self._model
        if top_n is not None:
            payload["top_n"] = top_n
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        resp = self._client.post(
            self._url,
            json=payload,
            headers=headers,
            timeout=self._timeout if timeout is None else timeout,
        )
        resp.raise_for_status()
        return _parse_hits(resp.json(), len(documents))

    def close(self) -> None:
        """Close the owned HTTP connection pool exactly once."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._client.close()


def _parse_hits(data: Any, n_docs: int) -> list[RerankHit]:
    """Parse a Cohere/Jina ``{"results": [...]}`` or bare-list rerank response.

    Tolerates both ``relevance_score`` (Cohere/Jina/vLLM) and ``score`` (TEI),
    drops malformed or out-of-range entries, and returns hits sorted best-first.
    """
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    hits: list[RerankHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        score = row.get("relevance_score")
        if score is None:
            score = row.get("score")
        # bool is a subclass of int/float — reject it explicitly.
        if (
            isinstance(idx, int)
            and not isinstance(idx, bool)
            and 0 <= idx < n_docs
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
        ):
            hits.append(RerankHit(index=idx, score=float(score)))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def resolve_rerank_client(
    url: str, model: str = "", api_key: str = "", timeout: float = 30, instruction: str = ""
) -> RerankClient | None:
    """Return a rerank client, or ``None`` when no endpoint URL is configured.

    A missing URL is the "reranking disabled" state (the default) — there is no
    bundled endpoint and no local-inference fallback. ``instruction`` is the
    optional query instruction for instruction-aware rerankers (Qwen3-Reranker).
    """
    url = (url or "").strip()
    if not url:
        return None
    return CohereJinaRerankClient(
        url=url,
        model=(model or "").strip(),
        api_key=(api_key or "").strip(),
        timeout=timeout,
        instruction=(instruction or "").strip(),
    )
