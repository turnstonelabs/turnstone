"""Metadata-only BM25 scoring for live memory pointers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turnstone.core.bm25 import BM25Index

if TYPE_CHECKING:
    from turnstone.core.rerank import Reranker


@dataclass
class MemoryConfig:
    """Configuration for the structured memory system."""

    relevance_k: int = 5
    index_budget_chars: int = 65_536
    max_content: int = 32768
    nudge_cooldown: int = 300
    nudges: bool = True


def score_memories(
    memories: list[dict[str, str]],
    query: str,
    k: int = 5,
    reranker: Reranker | None = None,
    rerank_filters: bool = False,
) -> list[dict[str, str]]:
    """Return the top-k memories most relevant to *query*.

    Builds a BM25 index over authored index metadata only. Memory bodies remain
    fetch-on-demand and must never influence or leak through a pointer.
    """
    if not memories:
        return []
    if not query or not query.strip():
        return []

    documents = [f"{m.get('name', '')} {m.get('description', '')}" for m in memories]
    index = BM25Index(documents, reranker=reranker, rerank_filters=rerank_filters)
    top_indices = index.search(query, k)
    return [memories[i] for i in top_indices]
