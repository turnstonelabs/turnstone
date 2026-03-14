"""BM25 index — lightweight, pure-Python, zero external deps.

Extracted from tool_search.py for reuse by memory relevance scoring.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_SPLIT_RE = re.compile(r"[_\-./\s]+")


def _tokenize(text: str) -> list[str]:
    """Split text on whitespace, underscores, hyphens, dots."""
    return [t.lower() for t in _SPLIT_RE.split(text) if t]


class BM25Index:
    """Okapi BM25 ranking index over short text documents."""

    def __init__(self, documents: list[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
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

    def search(self, query: str, k: int = 5) -> list[int]:
        """Return indices of top-k documents sorted by descending BM25 score."""
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scores: list[tuple[float, int]] = []
        for idx, doc_tokens in enumerate(self._doc_tokens):
            score = self._score(q_tokens, doc_tokens, self._doc_lens[idx])
            if score > 0:
                scores.append((score, idx))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [idx for _, idx in scores[:k]]

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
