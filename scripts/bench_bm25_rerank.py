#!/usr/bin/env python3
"""Manual BM25-vs-BM25→rerank benchmark for the retrieval surfaces.

Run this by hand on a host that has a Cohere/Jina-compatible rerank endpoint
configured (vLLM, TEI, llama.cpp, or a hosted Cohere/Jina/Voyage key). It is NOT
a pytest test and is never collected by the test suite — it needs a live endpoint
to do anything useful.

It compares plain BM25 top-k against the two-stage BM25→rerank path on two small
in-file labeled corpora (tool-like docs and synthetic memory dicts), printing
precision@k, MRR, a ranking diff, and the relevant-vs-irrelevant score
distribution so you can pick a sensible ``tools.rerank_bm25_threshold`` default.

The endpoint bearer token (for hosted providers) is read from the
``$TURNSTONE_RERANK_API_KEY`` environment variable, never a flag, so it does not
land in shell history or the process listing.

Example::

    TURNSTONE_RERANK_API_KEY=... \
        .venv/bin/python scripts/bench_bm25_rerank.py \
        --rerank-url http://localhost:8000/rerank \
        --rerank-model BAAI/bge-reranker-v2-m3 --k 3 --threshold 0.0
"""

from __future__ import annotations

import argparse
import os
import statistics
from typing import TYPE_CHECKING

from turnstone.core.bm25 import BM25Index
from turnstone.core.rerank import resolve_rerank_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.rerank import RerankClient

# (query, relevant-doc-name) labels over a handful of tool-like docs.
TOOL_DOCS: list[dict[str, str]] = [
    {"name": "read_file", "description": "Read the contents of a file from disk"},
    {"name": "write_file", "description": "Write or overwrite a file on disk"},
    {"name": "list_dir", "description": "List the entries in a directory"},
    {"name": "web_search", "description": "Search the web and return result snippets"},
    {"name": "send_email", "description": "Send an email message to a recipient"},
    {"name": "run_bash", "description": "Execute a shell command and capture output"},
    {"name": "create_issue", "description": "Open a new issue in the bug tracker"},
    {"name": "query_db", "description": "Run a SQL query against the database"},
]
TOOL_LABELS: list[tuple[str, str]] = [
    ("open a file and show me what's inside", "read_file"),
    ("look something up on the internet", "web_search"),
    ("send a message by email", "send_email"),
    ("run a terminal command", "run_bash"),
    ("file a bug report", "create_issue"),
    ("fetch rows from the database", "query_db"),
]

# Synthetic memory dicts (name + description + content) with labeled queries.
MEMORY_DOCS: list[dict[str, str]] = [
    {
        "name": "postgres_conn",
        "description": "database connection settings",
        "content": "host=db.internal port=5432 user=app sslmode=require",
    },
    {
        "name": "redis_cache",
        "description": "cache server config",
        "content": "host=redis port=6379 maxmemory 2gb eviction allkeys-lru",
    },
    {
        "name": "deploy_runbook",
        "description": "production deploy steps",
        "content": "build image, push to registry, roll nodes one at a time",
    },
    {
        "name": "oncall_rotation",
        "description": "who is on call",
        "content": "primary alice, secondary bob, escalate to carol after 30m",
    },
    {
        "name": "tls_certs",
        "description": "certificate renewal",
        "content": "acme renews every 60 days; caddy reloads the SSLContext in place",
    },
    {
        "name": "api_ratelimits",
        "description": "rate limit policy",
        "content": "100 req/min per token, burst 20, 429 with retry-after header",
    },
    {
        "name": "backup_schedule",
        "description": "nightly backups",
        "content": "pg_dump at 02:00 UTC, retained 14 days, offsite copy weekly",
    },
    {
        "name": "feature_flags",
        "description": "rollout toggles",
        "content": "rerank_bm25 default on, voice_io default off, smart_approvals off",
    },
    {
        "name": "smtp_settings",
        "description": "outbound email",
        "content": "relay smtp.internal port 587 starttls from noreply@example.com",
    },
    {
        "name": "log_retention",
        "description": "log storage policy",
        "content": "structured logs to loki, 30 day retention, audit logs 1 year",
    },
    {
        "name": "node_placement",
        "description": "routing config",
        "content": "rendezvous hashing fnv-1a, ~100 node ceiling, hrw weights",
    },
    {
        "name": "jwt_secret_rotation",
        "description": "auth secret",
        "content": "TURNSTONE_JWT_SECRET in config.toml, rotate quarterly, hs256",
    },
]
MEMORY_LABELS: list[tuple[str, str]] = [
    ("what is the postgres database host and port", "postgres_conn"),
    ("how often do we rotate the jwt signing secret", "jwt_secret_rotation"),
    ("when do nightly database backups run", "backup_schedule"),
    ("who do I escalate an incident to", "oncall_rotation"),
    ("how are nodes placed for routing", "node_placement"),
    ("what is the per token api rate limit", "api_ratelimits"),
]


def _doc_text(d: dict[str, str]) -> str:
    return " ".join(
        filter(None, (d.get("name", ""), d.get("description", ""), d.get("content", "")))
    )


def _make_rank(client: RerankClient, threshold: float) -> Callable[[str, list[str]], list[int]]:
    def _rank(query: str, docs: list[str]) -> list[int]:
        return [
            h.index for h in client.rerank(query, docs) if threshold <= 0 or h.score >= threshold
        ]

    return _rank


def _precision_at_k(result_names: list[str], relevant: str, k: int) -> float:
    return 1.0 / k if relevant in result_names[:k] else 0.0


def _reciprocal_rank(result_names: list[str], relevant: str) -> float:
    for i, name in enumerate(result_names, 1):
        if name == relevant:
            return 1.0 / i
    return 0.0


def _bench_corpus(
    title: str,
    docs: list[dict[str, str]],
    labels: list[tuple[str, str]],
    client: RerankClient,
    threshold: float,
    k: int,
) -> None:
    print(f"\n=== {title} ({len(docs)} docs, {len(labels)} queries, k={k}) ===")
    texts = [_doc_text(d) for d in docs]
    names = [d["name"] for d in docs]
    plain = BM25Index(texts)
    # Filter mode when a floor is set (mirrors the memory composition call site)
    # so --threshold actually suppresses below-floor hits rather than being
    # masked by reorder-mode backfill; reorder mode at threshold 0.
    reranked = BM25Index(
        texts, reranker=_make_rank(client, threshold), rerank_filters=threshold > 0
    )

    bm25_p = bm25_mrr = rr_p = rr_mrr = 0.0
    for query, relevant in labels:
        b_names = [names[i] for i in plain.search(query, k=k)]
        r_names = [names[i] for i in reranked.search(query, k=k)]
        bm25_p += _precision_at_k(b_names, relevant, k)
        rr_p += _precision_at_k(r_names, relevant, k)
        bm25_mrr += _reciprocal_rank([names[i] for i in plain.search(query, k=len(docs))], relevant)
        rr_mrr += _reciprocal_rank(
            [names[i] for i in reranked.search(query, k=len(docs))], relevant
        )
        flag = "" if b_names[:k] == r_names[:k] else "  <-- reordered"
        print(f"  q: {query!r}")
        print(f"     want={relevant}  bm25={b_names[:k]}  rerank={r_names[:k]}{flag}")

    n = len(labels)
    print(f"  -- precision@{k}: bm25={bm25_p / n:.3f}  rerank={rr_p / n:.3f}")
    print(f"  -- MRR:          bm25={bm25_mrr / n:.3f}  rerank={rr_mrr / n:.3f}")


def _score_distribution(
    docs: list[dict[str, str]],
    labels: list[tuple[str, str]],
    client: RerankClient,
) -> None:
    """Print rerank-score stats for labeled relevant vs irrelevant pairs.

    A threshold default should sit above the irrelevant max / below the relevant
    min where those separate; this prints both so you can eyeball the gap.
    """
    texts = [_doc_text(d) for d in docs]
    names = [d["name"] for d in docs]
    relevant_scores: list[float] = []
    irrelevant_scores: list[float] = []
    for query, relevant in labels:
        for hit in client.rerank(query, texts):
            bucket = relevant_scores if names[hit.index] == relevant else irrelevant_scores
            bucket.append(hit.score)

    print("\n=== rerank score distribution (memory corpus) ===")
    for label, scores in (("relevant", relevant_scores), ("irrelevant", irrelevant_scores)):
        if not scores:
            print(f"  {label}: (no scores)")
            continue
        print(
            f"  {label:<10} n={len(scores):>3}  "
            f"min={min(scores):.4f}  median={statistics.median(scores):.4f}  "
            f"max={max(scores):.4f}"
        )
    if relevant_scores and irrelevant_scores:
        suggested = (min(relevant_scores) + max(irrelevant_scores)) / 2
        sep = "separable" if min(relevant_scores) > max(irrelevant_scores) else "overlapping"
        print(f"  -> classes are {sep}; midpoint threshold candidate ~= {suggested:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rerank-url", required=True, help="full Cohere/Jina-compatible /rerank URL")
    ap.add_argument("--rerank-model", default="", help="model name sent in the request body")
    ap.add_argument("--threshold", type=float, default=0.0, help="relevance floor (0 disables)")
    ap.add_argument("--k", type=int, default=3, help="top-k to score precision@k over")
    args = ap.parse_args()

    # Bearer token comes from the environment, not a flag, to keep it out of
    # shell history and the process listing.
    api_key = os.environ.get("TURNSTONE_RERANK_API_KEY", "")
    client = resolve_rerank_client(args.rerank_url, model=args.rerank_model, api_key=api_key)
    if client is None:
        print("No rerank endpoint resolved (empty --rerank-url?). Nothing to do.")
        return 1

    print(
        f"reranker: url={args.rerank_url} model={args.rerank_model or '(default)'} "
        f"threshold={args.threshold}"
    )
    _bench_corpus("tool search", TOOL_DOCS, TOOL_LABELS, client, args.threshold, args.k)
    _bench_corpus("memory composition", MEMORY_DOCS, MEMORY_LABELS, client, args.threshold, args.k)
    _score_distribution(MEMORY_DOCS, MEMORY_LABELS, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
