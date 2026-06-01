"""Tests for turnstone.core.bm25 — tokenizer and BM25 index."""

from turnstone.core.bm25 import _RERANK_POOL, BM25Index, _tokenize


class TestTokenize:
    def test_simple_words(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_underscores(self):
        assert _tokenize("read_file") == ["read", "file"]

    def test_hyphens(self):
        assert _tokenize("web-search") == ["web", "search"]

    def test_dots(self):
        assert _tokenize("foo.bar.baz") == ["foo", "bar", "baz"]

    def test_mixed_separators(self):
        assert _tokenize("mcp__server__read_file") == ["mcp", "server", "read", "file"]

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_case_folding(self):
        assert _tokenize("Hello World") == ["hello", "world"]


class TestBM25Index:
    def test_search_returns_relevant(self):
        docs = ["read a file from disk", "search for file in directory", "execute a bash command"]
        index = BM25Index(docs)
        results = index.search("file", k=2)
        assert 0 in results
        assert 1 in results

    def test_search_empty_query(self):
        docs = ["hello world"]
        index = BM25Index(docs)
        assert index.search("") == []

    def test_search_no_match(self):
        docs = ["hello world", "foo bar"]
        index = BM25Index(docs)
        assert index.search("zzzznotfound") == []

    def test_search_respects_k(self):
        docs = [f"document {i} with common word" for i in range(20)]
        index = BM25Index(docs)
        results = index.search("common", k=3)
        assert len(results) <= 3

    def test_empty_corpus(self):
        index = BM25Index([])
        assert index.search("anything") == []

    def test_single_document(self):
        index = BM25Index(["the only document about turnstone"])
        results = index.search("turnstone")
        assert results == [0]

    def test_ordering_by_relevance(self):
        docs = [
            "unrelated content about cooking recipes",
            "python programming with file operations",
            "read file write file file operations disk io",
        ]
        index = BM25Index(docs)
        results = index.search("file operations", k=3)
        # Doc 2 has more file/operations mentions, should rank higher
        assert results[0] == 2


class TestBM25Reranking:
    """Two-stage search: BM25 recall pool reordered by an attached reranker.

    The reranker is a plain callable returning POSITIONS into the recall pool
    (best-first); ``search`` maps each position back to the original doc index
    via ``pool[pos]``.  Fakes are deterministic lambdas/closures — the rerank
    HTTP boundary is exercised separately in test_rerank.py.
    """

    # All matching docs share "alpha"; the non-matching ones (zzz) stay out of
    # the pool, so a returned value that is 1 or 3 would prove a mapping bug.
    _DOCS = ["alpha one", "zzz", "alpha two", "zzz", "alpha three"]

    def test_reorders_by_reranker_output(self):
        # Reranker dictates pool order 2, 0, 1 (positions) regardless of BM25.
        docs = ["alpha", "alpha", "alpha"]
        index = BM25Index(docs, reranker=lambda q, d: [2, 0, 1])
        # pool == [0, 1, 2] (BM25 tie-break is ascending index); positions map
        # back to original indices 2, 0, 1.
        assert index.search("alpha", k=3) == [2, 0, 1]

    def test_maps_reranker_position_back_to_original_index(self):
        # Reranker reverses the pool it is handed (positions n-1..0). Guards
        # the ``pool[pos]`` mapping line: returned values must be ORIGINAL doc
        # indices (a subset of {0, 2, 4}), never raw positions like 1 or 3.
        index = BM25Index(self._DOCS, reranker=lambda q, d: list(range(len(d)))[::-1])
        result = index.search("alpha", k=5)
        # Only the three "alpha" docs are in the pool.
        assert set(result) == {0, 2, 4}
        # Reversed pool ordering: whatever BM25 pool order was, it is reversed.
        bm25_pool = BM25Index(self._DOCS).search("alpha", k=_RERANK_POOL)
        assert result == bm25_pool[::-1]

    def test_exception_falls_back_to_bm25_order(self):
        def boom(q, d):
            raise RuntimeError("rerank endpoint down")

        index = BM25Index(self._DOCS, reranker=boom)
        # Guards the ``except`` clause -> BM25 order, top-k.
        assert index.search("alpha", k=2) == BM25Index(self._DOCS)._bm25_rank("alpha")[:2]

    def test_clean_empty_return_is_honored_no_fallback(self):
        # FILTER MODE (rerank_filters=True, the memory floor): a clean empty
        # return means "inject nothing" (the caller's relevance floor emptied
        # it) and must NOT fall back to BM25 order. Guards the filter-branch
        # ``return out`` -- adding ``if not out: return pool[:k]`` there fails.
        index = BM25Index(self._DOCS, reranker=lambda q, d: [], rerank_filters=True)
        assert index.search("alpha", k=5) == []

    def test_filter_mode_exception_still_falls_back(self):
        # FILTER MODE honors a clean empty (above), but a reranker EXCEPTION is
        # an endpoint failure, not a floor verdict -> BM25 fallback, NOT empty.
        # This is the parse-failure-vs-floor distinction at the seam: the
        # _bm25_reranker closure raises on an unparseable/empty response so
        # memory composition can't be silently suppressed by a broken endpoint.
        def boom(q, d):
            raise RuntimeError("rerank endpoint down")

        index = BM25Index(self._DOCS, reranker=boom, rerank_filters=True)
        assert index.search("alpha", k=2) == BM25Index(self._DOCS)._bm25_rank("alpha")[:2]

    def test_singleton_pool_still_reranked(self):
        # A 1-doc pool is still sent to the reranker (no len>1 short-circuit).
        # Recording reranker returns [] -> result is [] AND it WAS called once.
        # Filter mode honors the empty result (no fallback).
        calls = {"n": 0}

        def rec(q, d):
            calls["n"] += 1
            return []

        # "two" matches only docs[2] -> pool of size 1.
        index = BM25Index(self._DOCS, reranker=rec, rerank_filters=True)
        assert index.search("two", k=5) == []
        assert calls["n"] == 1

    def test_reorder_mode_empty_falls_back_to_bm25(self):
        # REORDER MODE (default rerank_filters=False, reactive tool/skill
        # search): an empty reranker result means the endpoint failed, so fall
        # back to BM25 order -- results are NEVER silently dropped. [bug-1]
        # Guards the reorder-branch ``if not out: return pool[:k]`` -- removing
        # it makes this return [] and fail.
        index = BM25Index(self._DOCS, reranker=lambda q, d: [])
        assert index.search("alpha", k=5) == BM25Index(self._DOCS)._bm25_rank("alpha")[:5]

    def test_reorder_mode_backfills_omitted_pool_items(self):
        # REORDER MODE: the reranker returns a STRICT SUBSET of pool positions
        # (only the first two). The two reranked items come FIRST, then the
        # omitted pool items are backfilled in BM25 order, capped at k. [bug-2]
        # Guards the backfill loop -- removing it drops the omitted docs.
        bm25_pool = BM25Index(self._DOCS)._bm25_rank("alpha")  # 3 matching docs
        assert len(bm25_pool) == 3
        # Reranker keeps only pool positions 0 and 1 (drops the third).
        index = BM25Index(self._DOCS, reranker=lambda q, d: [0, 1])
        result = index.search("alpha", k=5)
        # Reranked two first (pool[0], pool[1]) ...
        assert result[:2] == [bm25_pool[0], bm25_pool[1]]
        # ... then the omitted pool item backfilled in BM25 order.
        assert result == [bm25_pool[0], bm25_pool[1], bm25_pool[2]]
        # No item silently lost: the full matching set is present.
        assert set(result) == set(bm25_pool)

    def test_reorder_mode_backfill_respects_k(self):
        # Backfill must stop at k: subset rerank + a k smaller than the pool.
        bm25_pool = BM25Index(self._DOCS)._bm25_rank("alpha")  # 3 matching docs
        index = BM25Index(self._DOCS, reranker=lambda q, d: [0])
        result = index.search("alpha", k=2)
        # One reranked item, then one backfilled, capped at k=2.
        assert result == [bm25_pool[0], bm25_pool[1]]

    def test_reorder_mode_full_reorder_unchanged(self):
        # REORDER MODE (default) with a full permutation: behaves exactly like
        # the historical reorder -- every pool item present, in reranker order,
        # backfill loop adds nothing (all positions already seen).
        index = BM25Index(self._DOCS, reranker=lambda q, d: list(range(len(d)))[::-1])
        result = index.search("alpha", k=5)
        bm25_pool = BM25Index(self._DOCS).search("alpha", k=_RERANK_POOL)
        assert result == bm25_pool[::-1]

    def test_no_reranker_matches_baseline(self):
        docs = [
            "read a file from disk",
            "search for file in directory",
            "execute a bash command about files",
            "totally unrelated cooking content",
        ]
        plain = BM25Index(docs)
        attached = BM25Index(docs, reranker=lambda q, d: list(range(len(d))))
        for q in ("file", "bash command", "disk", "cooking", "file directory"):
            # reranker=identity returns the pool unchanged, but the no-reranker
            # path must be byte-for-byte the historical result regardless.
            assert plain.search(q, k=3) == BM25Index(docs).search(q, k=3)
            # And identity-rerank reproduces the BM25 top-k for these queries.
            assert attached.search(q, k=3) == plain.search(q, k=3)

    def test_bool_positions_rejected(self):
        docs = ["alpha", "alpha", "alpha"]
        # True/False are int subclasses posing as 1/0 -> rejected; "1" is not an
        # int -> rejected; only positions 2 and 0 survive. Filter mode so the
        # rejected positions are not re-added by reorder-mode backfill -- this
        # isolates the type guard.
        index = BM25Index(docs, reranker=lambda q, d: [True, "1", 2, 0], rerank_filters=True)
        assert index.search("alpha", k=5) == [2, 0]

    def test_recall_pool_capped_at_rerank_pool(self):
        # More matching docs than the recall cap: the reranker must receive
        # exactly _RERANK_POOL docs, and outputs may only reference that set.
        n = _RERANK_POOL + 10
        docs = [f"alpha doc{i}" for i in range(n)]
        seen_len = {"n": -1}

        def rec(q, d):
            seen_len["n"] = len(d)
            return list(range(len(d)))  # identity over the (capped) pool

        index = BM25Index(docs, reranker=rec)
        result = index.search("alpha", k=n)
        assert seen_len["n"] == _RERANK_POOL  # only the first 50 reached rerank
        assert len(result) == _RERANK_POOL
        # Every returned index is from the BM25 top-50 recall set.
        recall = set(BM25Index(docs)._bm25_rank("alpha")[:_RERANK_POOL])
        assert set(result) == recall

    def test_empty_query_skips_reranker(self):
        calls = {"n": 0}

        def rec(q, d):
            calls["n"] += 1
            return list(range(len(d)))

        index = BM25Index(self._DOCS, reranker=rec)
        # Empty query -> empty BM25 pool -> reranker never invoked.
        assert index.search("", k=5) == []
        assert calls["n"] == 0
