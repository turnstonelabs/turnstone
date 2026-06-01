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

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from turnstone.core.log import get_logger

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
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        """Score ``documents`` against ``query``; return hits sorted best-first."""
        ...


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

    def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
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
        resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
        resp.raise_for_status()
        return _parse_hits(resp.json(), len(documents))


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
