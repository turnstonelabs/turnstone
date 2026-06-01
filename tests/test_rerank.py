"""Tests for turnstone.core.rerank — endpoint-backed reranking client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from turnstone.core.rerank import (
    CohereJinaRerankClient,
    RerankHit,
    _parse_hits,
    normalize_scores,
    resolve_rerank_client,
)
from turnstone.core.session import ChatSession


def _mock_httpx_post(handler):
    """Patch target for ``rerank.httpx.post`` that routes the call through a real
    ``httpx.MockTransport``. The request flows through genuine httpx JSON/header
    encoding and response parsing — a true boundary, not a bare MagicMock.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler))

    def _post(url, **kwargs):
        return client.post(url, **kwargs)

    return _post


# A Cohere/Jina/vLLM-shaped response: results wrapper + relevance_score, returned
# out of input order so tests prove the client sorts best-first.
RESULTS_WRAPPED = {
    "results": [
        {"index": 2, "relevance_score": 0.10},
        {"index": 0, "relevance_score": 0.95},
        {"index": 1, "relevance_score": 0.42},
    ]
}

# A TEI-shaped response: bare list + "score" key.
BARE_LIST = [
    {"index": 0, "score": 0.30},
    {"index": 1, "score": 0.80},
]


# ---------------------------------------------------------------------------
# _parse_hits — response-shape tolerance (the boundary that varies by provider)
# ---------------------------------------------------------------------------


class TestParseHits:
    def test_results_wrapped_relevance_score_sorted(self):
        hits = _parse_hits(RESULTS_WRAPPED, n_docs=3)
        assert [(h.index, h.score) for h in hits] == [(0, 0.95), (1, 0.42), (2, 0.10)]

    def test_bare_list_score_key(self):
        hits = _parse_hits(BARE_LIST, n_docs=2)
        assert [(h.index, h.score) for h in hits] == [(1, 0.80), (0, 0.30)]

    def test_relevance_score_preferred_over_score(self):
        # When both keys are present, relevance_score wins.
        hits = _parse_hits({"results": [{"index": 0, "relevance_score": 0.9, "score": 0.1}]}, 1)
        assert hits == [RerankHit(index=0, score=0.9)]

    def test_drops_out_of_range_index(self):
        hits = _parse_hits({"results": [{"index": 5, "relevance_score": 0.9}]}, n_docs=2)
        assert hits == []

    def test_drops_non_dict_and_missing_fields(self):
        data = {
            "results": [
                "not-a-dict",
                {"index": 0},  # missing score
                {"relevance_score": 0.5},  # missing index
                {"index": 1, "relevance_score": 0.7},  # valid
            ]
        }
        assert _parse_hits(data, n_docs=2) == [RerankHit(index=1, score=0.7)]

    def test_rejects_bool_index_and_score(self):
        # bool is a subclass of int/float — must not be accepted as a hit.
        assert _parse_hits({"results": [{"index": True, "relevance_score": 0.9}]}, 2) == []
        assert _parse_hits({"results": [{"index": 0, "relevance_score": True}]}, 2) == []

    def test_empty_and_garbage(self):
        assert _parse_hits({"results": []}, 3) == []
        assert _parse_hits({}, 3) == []
        assert _parse_hits({"results": "nope"}, 3) == []
        assert _parse_hits(42, 3) == []


# ---------------------------------------------------------------------------
# CohereJinaRerankClient — request construction + transport boundary
# ---------------------------------------------------------------------------


class TestCohereJinaRerankClient:
    def test_request_body_boundary(self, monkeypatch):
        """Drive a real httpx request through MockTransport and assert the URL,
        body, and auth header the client builds."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=RESULTS_WRAPPED)

        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        client = CohereJinaRerankClient(
            "http://vllm:8000/rerank", model="bge", api_key="secret", timeout=10
        )
        hits = client.rerank("q", ["a", "b", "c"], top_n=2)

        assert captured["url"] == "http://vllm:8000/rerank"
        assert captured["body"] == {
            "query": "q",
            "documents": ["a", "b", "c"],
            "model": "bge",
            "top_n": 2,
        }
        assert captured["auth"] == "Bearer secret"
        # Response is parsed + sorted best-first.
        assert [h.index for h in hits] == [0, 1, 2]

    def test_model_and_top_n_omitted_when_unset(self, monkeypatch):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"results": []})

        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        CohereJinaRerankClient("http://h/rerank").rerank("q", ["a"])

        assert captured["body"] == {"query": "q", "documents": ["a"]}
        assert captured["auth"] is None  # no Authorization header without a key

    def test_empty_documents_makes_no_request(self, monkeypatch):
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"results": []})

        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        assert CohereJinaRerankClient("http://h/rerank").rerank("q", []) == []
        assert called["n"] == 0  # short-circuits before any HTTP call

    def test_bare_list_response_parsed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=BARE_LIST)

        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        hits = CohereJinaRerankClient("http://tei/rerank").rerank("q", ["a", "b"])
        assert [h.index for h in hits] == [1, 0]

    def test_http_error_propagates(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        with pytest.raises(httpx.HTTPStatusError):
            CohereJinaRerankClient("http://h/rerank").rerank("q", ["a"])


# ---------------------------------------------------------------------------
# resolve_rerank_client
# ---------------------------------------------------------------------------


class TestResolveRerankClient:
    def test_client_when_url_set(self):
        client = resolve_rerank_client("http://h/rerank", model="m", api_key="k")
        assert isinstance(client, CohereJinaRerankClient)
        assert client._url == "http://h/rerank"
        assert client._model == "m"
        assert client._api_key == "k"

    def test_none_when_no_url(self):
        assert resolve_rerank_client("") is None
        assert resolve_rerank_client("   ") is None
        assert resolve_rerank_client(None) is None  # type: ignore[arg-type]

    def test_strips_whitespace(self):
        client = resolve_rerank_client("  http://h/rerank  ", model="  m  ", api_key="  k  ")
        assert isinstance(client, CohereJinaRerankClient)
        assert client._url == "http://h/rerank"
        assert client._model == "m"
        assert client._api_key == "k"


# ---------------------------------------------------------------------------
# ChatSession wiring — disabled by default, endpoint-gated, per-tool toggles
# ---------------------------------------------------------------------------


class _FakeConfigStore:
    """Minimal ConfigStore stand-in (explicit value vs unset via stored_keys)."""

    def __init__(self, values: dict, stored: set[str] | None = None) -> None:
        self._values = dict(values)
        self._stored = frozenset(stored if stored is not None else values.keys())

    def stored_keys(self) -> frozenset[str]:
        return self._stored

    def get(self, key: str):
        return self._values.get(key)


def _patch_getters_empty(monkeypatch):
    for name in ("get_rerank_url", "get_rerank_model", "get_rerank_api_key"):
        monkeypatch.setattr(f"turnstone.core.config.{name}", lambda: "")


class TestSessionRerankWiring:
    def test_disabled_by_default_when_no_endpoint(self, monkeypatch):
        _patch_getters_empty(monkeypatch)
        stub = SimpleNamespace(_config_store=None, tool_timeout=30)
        assert ChatSession._resolve_rerank_client(stub) is None

    def test_resolves_client_from_config_store(self, monkeypatch):
        _patch_getters_empty(monkeypatch)
        cs = _FakeConfigStore(
            {
                "tools.rerank_url": "http://h/rerank",
                "tools.rerank_model": "m",
                "tools.rerank_api_key": "k",
            }
        )
        stub = SimpleNamespace(_config_store=cs, tool_timeout=15)
        client = ChatSession._resolve_rerank_client(stub)
        assert isinstance(client, CohereJinaRerankClient)
        assert client._url == "http://h/rerank"
        assert client._model == "m"
        assert client._api_key == "k"

    def test_explicit_empty_url_stays_disabled(self, monkeypatch):
        # An admin who clears the URL (explicit "") must NOT fall back to a
        # config.toml/env value — explicit-empty means "off".
        monkeypatch.setattr("turnstone.core.config.get_rerank_url", lambda: "http://env/rerank")
        monkeypatch.setattr("turnstone.core.config.get_rerank_model", lambda: "")
        monkeypatch.setattr("turnstone.core.config.get_rerank_api_key", lambda: "")
        cs = _FakeConfigStore({"tools.rerank_url": ""}, stored={"tools.rerank_url"})
        stub = SimpleNamespace(_config_store=cs, tool_timeout=30)
        assert ChatSession._resolve_rerank_client(stub) is None

    def test_enabled_for_defaults_true_without_store(self):
        stub = SimpleNamespace(_config_store=None)
        assert ChatSession._rerank_enabled_for(stub, "web_search") is True

    def test_enabled_for_respects_per_tool_toggle(self):
        cs = _FakeConfigStore({"tools.rerank_web_search": False})
        stub = SimpleNamespace(_config_store=cs)
        assert ChatSession._rerank_enabled_for(stub, "web_search") is False

    def test_prefers_reranker_model_definition(self, monkeypatch):
        # A model definition with supports_rerank, selected via the Reranker
        # role, wins over the raw tools.rerank_url settings.
        _patch_getters_empty(monkeypatch)
        from turnstone.core.model_registry import ModelConfig

        cfg = ModelConfig(
            alias="rr",
            base_url="http://rr:8000/rerank",
            api_key="k",
            model="bge",
            capabilities={"supports_rerank": True},
        )
        cs = _FakeConfigStore({"tools.reranker_alias": "rr"})
        stub = SimpleNamespace(
            _config_store=cs,
            tool_timeout=30,
            _registry=SimpleNamespace(get_config=lambda a: cfg),
        )
        client = ChatSession._resolve_rerank_client(stub)
        assert isinstance(client, CohereJinaRerankClient)
        assert client._url == "http://rr:8000/rerank"
        assert client._model == "bge"
        assert client._api_key == "k"

    def test_ignores_alias_without_rerank_capability(self, monkeypatch):
        # A non-reranker model (no supports_rerank) must NOT be used as a reranker,
        # even if the alias is set — guards against a stale/wrong alias misrouting.
        _patch_getters_empty(monkeypatch)
        from turnstone.core.model_registry import ModelConfig

        cfg = ModelConfig(
            alias="chat", base_url="http://chat/v1", api_key="k", model="gpt", capabilities={}
        )
        cs = _FakeConfigStore({"tools.reranker_alias": "chat"})
        stub = SimpleNamespace(
            _config_store=cs,
            tool_timeout=30,
            _registry=SimpleNamespace(get_config=lambda a: cfg),
        )
        assert ChatSession._resolve_rerank_client(stub) is None  # falls through, no rerank_url


# ---------------------------------------------------------------------------
# ChatSession BM25 reranker — the closure feeding tool/skill/memory retrieval
# ---------------------------------------------------------------------------


class _FakeRerankClient:
    """In-process RerankClient stub returning fixed hits (no HTTP)."""

    def __init__(self, hits: list[RerankHit]) -> None:
        self._hits = hits

    def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        return self._hits


class TestRerankInstruction:
    """`rerank_instruction` wraps the query for instruction-aware rerankers.

    Drives through the real httpx boundary (MockTransport) and inspects the sent
    body — for endpoints that don't apply the model's own chat template; vLLM
    should use --chat-template instead (combining both double-wraps).
    """

    def _capture(self):
        sent: dict = {}

        def handler(request):
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})

        return sent, handler

    def test_no_instruction_sends_bare_query(self, monkeypatch):
        sent, handler = self._capture()
        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        CohereJinaRerankClient("http://x/rerank").rerank("capital of France", ["d0"])
        assert sent["body"]["query"] == "capital of France"

    def test_instruction_wraps_query(self, monkeypatch):
        sent, handler = self._capture()
        monkeypatch.setattr("turnstone.core.rerank.httpx.post", _mock_httpx_post(handler))
        CohereJinaRerankClient("http://x/rerank", instruction="Find relevant passages").rerank(
            "capital of France", ["d0"]
        )
        assert (
            sent["body"]["query"]
            == "<Instruct>: Find relevant passages\n<Query>: capital of France"
        )

    def test_resolve_threads_instruction(self):
        client = resolve_rerank_client("http://x/rerank", instruction="X")
        assert client is not None and client._instruction == "X"


class TestNormalizeScores:
    """`normalize_scores` — per-batch 0-1 mapping (sigmoid for logit batches)."""

    def test_empty(self):
        assert normalize_scores([]) == []

    def test_all_in_range_pass_through(self):
        # Probability-scale batch (Cohere/Jina/Qwen) -> identity, no transform.
        assert normalize_scores([0.9, 0.1, 0.5, 0.0, 1.0]) == [0.9, 0.1, 0.5, 0.0, 1.0]

    def test_negative_triggers_sigmoid(self):
        out = normalize_scores([2.0, -2.0])
        assert all(0.0 < s < 1.0 for s in out)
        assert out[0] > 0.8 and out[1] < 0.2  # sigmoid(2)=0.88, sigmoid(-2)=0.12

    def test_above_one_triggers_sigmoid(self):
        out = normalize_scores([5.0, 0.5])  # 5.0 > 1 -> the whole batch is sigmoided
        assert out != [5.0, 0.5]
        assert all(0.0 < s < 1.0 for s in out)

    def test_sigmoid_is_monotonic(self):
        # Ranking must be preserved by normalisation (only the scale changes).
        raw = [3.0, -1.0, 0.0, -5.0, 2.0]
        out = normalize_scores(raw)
        assert sorted(range(len(raw)), key=lambda i: out[i]) == sorted(
            range(len(raw)), key=lambda i: raw[i]
        )

    def test_extreme_values_do_not_overflow(self):
        out = normalize_scores([1000.0, -1000.0])
        assert out[0] == pytest.approx(1.0) and out[1] == pytest.approx(0.0)


class TestSessionBM25Reranker:
    """``_bm25_reranker`` / ``_bm25_rerank_threshold`` — the BM25 seam adapters.

    Drives the real closure through a fake ``RerankClient`` (the in-process
    callable seam), never by patching internal state. The HTTP boundary lives in
    ``TestCohereJinaRerankClient`` above.
    """

    def test_none_when_disabled_for_bm25(self):
        # Per-tool toggle off -> no reranker even with an endpoint configured.
        stub = SimpleNamespace(
            _rerank_enabled_for=lambda tool: False,
            _resolve_rerank_client=lambda: _FakeRerankClient([]),
        )
        assert ChatSession._bm25_reranker(stub) is None

    def test_none_when_no_client(self):
        # Enabled, but no endpoint resolves -> None.
        stub = SimpleNamespace(
            _rerank_enabled_for=lambda tool: True,
            _resolve_rerank_client=lambda: None,
        )
        assert ChatSession._bm25_reranker(stub) is None

    def _enabled_stub(self, hits: list[RerankHit]) -> SimpleNamespace:
        return SimpleNamespace(
            _rerank_enabled_for=lambda tool: True,
            _resolve_rerank_client=lambda: _FakeRerankClient(hits),
        )

    def test_no_threshold_returns_all_hit_indices(self):
        hits = [RerankHit(index=0, score=0.9), RerankHit(index=1, score=0.1)]
        rank = ChatSession._bm25_reranker(self._enabled_stub(hits), 0.0)
        assert rank is not None
        # threshold 0 disables the floor -> every hit index passes through.
        assert rank("q", ["d0", "d1"]) == [0, 1]

    def test_threshold_filters_below_floor(self):
        hits = [RerankHit(index=0, score=0.9), RerankHit(index=1, score=0.1)]
        rank = ChatSession._bm25_reranker(self._enabled_stub(hits), 0.5)
        assert rank is not None
        # Only the 0.9 hit clears the 0.5 floor. Guards the ``h.score >=
        # threshold`` boundary (flip to ``>`` / ``<`` and idx1 leaks or idx0
        # drops).
        assert rank("q", ["d0", "d1"]) == [0]

    def test_threshold_boundary_is_inclusive(self):
        # A hit exactly at the floor is KEPT (>= , not >).
        hits = [RerankHit(index=0, score=0.5)]
        rank = ChatSession._bm25_reranker(self._enabled_stub(hits), 0.5)
        assert rank is not None
        assert rank("q", ["d0"]) == [0]

    def test_empty_hits_raise_for_nonempty_docs(self):
        # A conforming reranker scores every doc; [] for non-empty input is an
        # endpoint/parse failure, NOT a floor result -> raise (a discrete branch
        # from the threshold) so BM25Index falls back to BM25 order in BOTH
        # modes. Holds regardless of threshold.
        from turnstone.core.rerank import RerankError

        for thr in (0.0, 0.5):
            rank = ChatSession._bm25_reranker(self._enabled_stub([]), thr)
            assert rank is not None
            with pytest.raises(RerankError):
                rank("q", ["d0", "d1"])

    def test_floor_dropping_all_returns_empty_not_raise(self):
        # Distinct from a parse failure: the reranker DID score the doc, the
        # floor just dropped it -> clean empty (honored by filter mode), no raise.
        hits = [RerankHit(index=0, score=0.1)]
        rank = ChatSession._bm25_reranker(self._enabled_stub(hits), 0.5)
        assert rank is not None
        assert rank("q", ["d0"]) == []

    def test_empty_docs_does_not_raise(self):
        # Empty input legitimately yields empty output -- nothing to signal.
        rank = ChatSession._bm25_reranker(self._enabled_stub([]), 0.0)
        assert rank is not None
        assert rank("q", []) == []

    def test_logit_scores_are_normalized_before_floor(self):
        # Batch has a score outside [0,1] -> treated as logits -> sigmoid. raw
        # 0.3 fails a 0.5 floor, but sigmoid(0.3)=0.574 clears it; the negative
        # sibling sigmoid(-2)=0.12 does not. Without normalisation BOTH raw
        # values fail 0.5 -> []; this proves the closure normalises the batch.
        hits = [RerankHit(index=0, score=0.3), RerankHit(index=1, score=-2.0)]
        rank = ChatSession._bm25_reranker(self._enabled_stub(hits), 0.5)
        assert rank is not None
        assert rank("q", ["d0", "d1"]) == [0]

    def test_threshold_reads_setting(self):
        cs = _FakeConfigStore({"tools.rerank_bm25_threshold": 0.42})
        stub = SimpleNamespace(_config_store=cs)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.42

    def test_threshold_zero_without_config_store(self):
        stub = SimpleNamespace(_config_store=None)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.0

    def test_threshold_zero_on_garbage_value(self):
        cs = _FakeConfigStore({"tools.rerank_bm25_threshold": "not-a-number"})
        stub = SimpleNamespace(_config_store=cs)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.0

    # -- per-model calibrated floor precedence (Phase 3) ---------------------

    def _floor_stub(self, *, alias: str, global_thr: float, caps: dict | None):
        """Stub with a reranker alias selected + a fake registry holding caps.

        ``caps=None`` means the alias is selected but absent from the registry
        (has_alias False) -> must fall through to the global value, no crash.
        """
        cs = _FakeConfigStore(
            {"tools.reranker_alias": alias, "tools.rerank_bm25_threshold": global_thr}
        )
        registry = SimpleNamespace(
            has_alias=lambda a: caps is not None and a == alias,
            get_config=lambda a: SimpleNamespace(capabilities=caps or {}),
        )
        return SimpleNamespace(_config_store=cs, _registry=registry)

    def test_floor_uses_per_model_when_calibrated_and_separated(self):
        # Calibrated (rerank_scale set) AND separated -> the per-model floor wins
        # over the global fallback.
        caps = {
            "rerank_scale": "probability (0-1)",
            "rerank_separated": True,
            "rerank_threshold": 0.61,
        }
        stub = self._floor_stub(alias="rr", global_thr=0.2, caps=caps)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.61

    def test_floor_zero_when_calibrated_but_not_separated(self):
        # Calibrated but NO clean separation -> floor disabled (0.0), NOT the
        # global fallback. Flip rerank_separated to True and this returns 0.61;
        # the False branch must yield 0.0.
        caps = {
            "rerank_scale": "logit (sigmoid-normalised)",
            "rerank_separated": False,
            "rerank_threshold": 0.61,
        }
        stub = self._floor_stub(alias="rr", global_thr=0.4, caps=caps)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.0

    def test_floor_falls_back_to_global_when_uncalibrated(self):
        # Reranker selected but never calibrated (no rerank_scale marker) ->
        # the global fallback applies.
        caps = {"supports_rerank": True}
        stub = self._floor_stub(alias="rr", global_thr=0.4, caps=caps)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.4

    def test_floor_global_when_no_alias_selected(self):
        # No reranker_alias -> the alias guard is False, global fallback used
        # (and the registry is never consulted).
        cs = _FakeConfigStore({"tools.reranker_alias": "", "tools.rerank_bm25_threshold": 0.33})

        def _boom(_a):  # registry must not be touched without an alias
            raise AssertionError("registry consulted despite empty alias")

        registry = SimpleNamespace(has_alias=_boom, get_config=_boom)
        stub = SimpleNamespace(_config_store=cs, _registry=registry)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.33

    def test_floor_global_when_alias_absent_from_registry(self):
        # Alias set but not in the registry (has_alias False) -> no crash, falls
        # through to the global value.
        stub = self._floor_stub(alias="ghost", global_thr=0.25, caps=None)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.25

    def test_floor_global_when_registry_is_none(self):
        # Alias set but no registry attached -> guard on registry None, global.
        cs = _FakeConfigStore({"tools.reranker_alias": "rr", "tools.rerank_bm25_threshold": 0.15})
        stub = SimpleNamespace(_config_store=cs, _registry=None)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.15

    def test_floor_zero_on_garbage_per_model_threshold(self):
        # Calibrated+separated but the stored threshold is junk -> the
        # (TypeError, ValueError) guard yields 0.0 rather than propagating.
        caps = {
            "rerank_scale": "probability (0-1)",
            "rerank_separated": True,
            "rerank_threshold": "nan-ish",
        }
        stub = self._floor_stub(alias="rr", global_thr=0.4, caps=caps)
        assert ChatSession._bm25_rerank_threshold(stub) == 0.0


class TestModelCapabilitiesRerankFields:
    """The Phase 3 reranker-calibration fields on the frozen dataclass."""

    def test_replace_accepts_calibration_fields(self):
        import dataclasses

        from turnstone.core.providers._protocol import ModelCapabilities

        caps = dataclasses.replace(
            ModelCapabilities(),
            rerank_threshold=0.33,
            rerank_scale="probability (0-1)",
            rerank_separated=True,
        )
        assert caps.rerank_threshold == 0.33
        assert caps.rerank_scale == "probability (0-1)"
        assert caps.rerank_separated is True

    def test_defaults_mark_uncalibrated(self):
        from turnstone.core.providers._protocol import ModelCapabilities

        caps = ModelCapabilities()
        # Empty rerank_scale is the "not calibrated" marker the floor logic keys
        # off of; the numeric/bool defaults are inert.
        assert caps.rerank_scale == ""
        assert caps.rerank_threshold == 0.0
        assert caps.rerank_separated is False

    def test_resolve_capabilities_merges_calibration_overrides(self):
        """The session field-filter in ``_resolve_capabilities`` now passes the
        three calibration keys through (they are real dataclass fields), so a
        per-model caps dict carrying them survives onto the runtime caps."""
        import dataclasses

        from turnstone.core.providers._protocol import ModelCapabilities
        from turnstone.core.session import ChatSession

        base = ModelCapabilities()
        provider = SimpleNamespace(get_capabilities=lambda model: base)
        cfg = SimpleNamespace(
            capabilities={
                "rerank_threshold": 0.5,
                "rerank_scale": "logit (sigmoid-normalised)",
                "rerank_separated": True,
                "not_a_field": "dropped",
            }
        )
        registry = SimpleNamespace(get_config=lambda alias: cfg)
        stub = SimpleNamespace(_registry=registry)
        caps = ChatSession._resolve_capabilities(stub, provider, "m", "rr")
        assert isinstance(caps, ModelCapabilities)
        assert caps.rerank_threshold == 0.5
        assert caps.rerank_scale == "logit (sigmoid-normalised)"
        assert caps.rerank_separated is True
        # Unknown keys are filtered out (no crash on replace).
        assert not hasattr(caps, "not_a_field")
        # Sanity: the dataclasses module is genuinely exercised.
        assert dataclasses.is_dataclass(caps)
