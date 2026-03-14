"""Tests for turnstone.core.bm25 — tokenizer and BM25 index."""

from turnstone.core.bm25 import BM25Index, _tokenize


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
