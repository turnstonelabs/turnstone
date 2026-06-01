"""BM25 index — lightweight, pure-Python, zero external deps.

Extracted from tool_search.py for reuse by memory relevance scoring.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.rerank import Reranker

_SPLIT_RE = re.compile(r"[_\-./\s]+")

# Max BM25 hits handed to the reranker in one request — the recall pool whose
# top-k the reranker reorders before the caller's ``k`` slice. Deliberately a
# private copy (not shared from rerank.py) so this module stays import-light and
# httpx-free; web_search.py defines the same cap independently — keep them in sync.
_RERANK_POOL = 50


def _tokenize(text: str) -> list[str]:
    """Split text on whitespace, underscores, hyphens, dots."""
    return [t.lower() for t in _SPLIT_RE.split(text) if t]


class BM25Index:
    """Okapi BM25 ranking index over short text documents."""

    def __init__(
        self,
        documents: list[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        reranker: Reranker | None = None,
        rerank_filters: bool = False,
    ) -> None:
        self.k1 = k1
        self.b = b
        self._reranker = reranker
        self._rerank_filters = rerank_filters
        self._docs = documents
        self._doc_tokens: list[list[str]] = [_tokenize(d) for d in documents]
        self._doc_lens = [len(t) for t in self._doc_tokens]
        self._avgdl = sum(self._doc_lens) / max(len(self._doc_lens), 1)
        self._n = len(documents)
        # Document frequency per term
        self._df: Counter[str] = Counter()
        for tokens in self._doc_tokens:
            for term in set(tokens):
                self._df[term] += 1

    def _bm25_rank(self, query: str) -> list[int]:
        """Return ALL matching document indices sorted by descending BM25 score."""
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scores: list[tuple[float, int]] = []
        for idx, doc_tokens in enumerate(self._doc_tokens):
            score = self._score(q_tokens, doc_tokens, self._doc_lens[idx])
            if score > 0:
                scores.append((score, idx))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [idx for _, idx in scores]

    def search(self, query: str, k: int = 5) -> list[int]:
        """Return indices of top-k documents by relevance.

        Stage 1 is BM25. When a reranker is attached, a recall pool of the top
        ``_RERANK_POOL`` BM25 hits is reranked and the top-k spliced back.
        """
        ranked = self._bm25_rank(query)
        if self._reranker is None:
            return ranked[:k]  # no reranker: today's behavior, byte-for-byte
        pool = ranked[:_RERANK_POOL]
        if not pool:
            return []
        docs = [self._docs[i] for i in pool]
        try:
            order = list(self._reranker(query, docs))
        except Exception:
            return pool[:k]  # reranker ERROR -> BM25 order (both modes)
        seen: set[int] = set()
        out: list[int] = []
        for pos in order:
            # bool is an int subclass -- reject a stray True/False posing as 1/0.
            if (
                isinstance(pos, int)
                and not isinstance(pos, bool)
                and 0 <= pos < len(pool)
                and pos not in seen
            ):
                seen.add(pos)
                out.append(pool[pos])  # map reranker position -> original doc index
            if len(out) >= k:
                break
        if self._rerank_filters:
            # FILTER MODE (memory floor): the reranker may legitimately drop
            # sub-floor candidates, so a clean short/empty result is HONORED as-is
            # -- NO fallback to BM25 order, NO backfill. This is the deliberate
            # divergence from web_search._rerank_results and from reorder mode below;
            # do not "fix" it to fall back on empty (a test pins this).
            return out
        # REORDER MODE (reactive tool/skill search): the reranker must never drop
        # candidates. An empty result means the endpoint failed -> BM25 fallback;
        # any pool items the reranker omitted (e.g. a top_n subset) are backfilled
        # in BM25 order so results are never silently lost.
        if not out:
            return pool[:k]
        for pos in range(len(pool)):
            if len(out) >= k:
                break
            if pos not in seen:
                out.append(pool[pos])
        return out

    def _score(self, q_tokens: list[str], doc_tokens: list[str], dl: int) -> float:
        tf_map: Counter[str] = Counter(doc_tokens)
        score = 0.0
        for term in q_tokens:
            if term not in tf_map:
                continue
            tf = tf_map[term]
            df = self._df.get(term, 0)
            idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            score += idf * numerator / denominator
        return score
