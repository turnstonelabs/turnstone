"""Calibration core — probe a fake reranker and recommend a 0-1 floor."""

from __future__ import annotations

from turnstone.core.rerank import RerankHit
from turnstone.core.rerank_calibrate import _GAP_FRACTION, _PROBE_SET, _build_result, calibrate


class _ScriptedClient:
    """RerankClient stub: scores doc 0 (the relevant one) at ``r``, the rest ``i``.

    Drives the real ``calibrate`` loop through the ``RerankClient`` seam — the
    relevant doc is always position 0 of the documents calibrate sends.
    """

    def __init__(self, r: float, i: float) -> None:
        self._r, self._i = r, i

    def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        assert top_n is None  # calibration must request every doc's score
        return [RerankHit(index=0, score=self._r)] + [
            RerankHit(index=idx, score=self._i) for idx in range(1, len(documents))
        ]


class _FlakyClient:
    """Fails the first ``cold`` calls (cold-start compile), then scores normally."""

    def __init__(self, cold: int, r: float, i: float) -> None:
        self.calls = 0
        self.cold = cold
        self._r, self._i = r, i

    def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        self.calls += 1
        if self.calls <= self.cold:
            raise RuntimeError("cold endpoint (compiling)")
        return [RerankHit(index=0, score=self._r)] + [
            RerankHit(index=idx, score=self._i) for idx in range(1, len(documents))
        ]


class TestCalibrate:
    def test_warmup_absorbs_cold_start(self):
        # First 2 calls fail (compile); warmup consumes them so the probe loop is
        # warm and calibration still succeeds.
        c = _FlakyClient(cold=2, r=0.9, i=0.1)
        res = calibrate(c, model="m")
        assert res.separated
        assert c.calls > 2  # warmup absorbed the cold calls before the probes ran

    def test_probability_scale_clean_separation(self):
        res = calibrate(_ScriptedClient(0.9, 0.1), model="m")
        assert res.raw_scale == "probability (0-1)"  # already 0-1 -> identity
        assert res.separated
        # gap (0.1, 0.9); _GAP_FRACTION in from the irrelevant edge.
        assert res.suggested_threshold == round(0.1 + _GAP_FRACTION * 0.8, 4)
        assert res.irrelevant_max < res.suggested_threshold < res.relevant_min
        assert res.n_relevant == len(_PROBE_SET)
        assert res.n_irrelevant == len(_PROBE_SET) * (len(_PROBE_SET) - 1)

    def test_logit_scale_normalized_then_separated(self):
        # Out-of-[0,1] raw scores -> sigmoid -> a 0-1 floor regardless of scale.
        res = calibrate(_ScriptedClient(5.0, -2.0), model="m")
        assert "logit" in res.raw_scale
        assert res.separated
        assert res.suggested_threshold is not None
        assert 0.0 < res.suggested_threshold < 1.0
        assert res.irrelevant_max < res.suggested_threshold < res.relevant_min
        # all reported score fields live in the normalised 0-1 space
        assert 0.0 <= res.irrelevant_min <= res.relevant_max <= 1.0

    def test_overlap_reports_no_separation(self):
        # relevant 0.4 <= irrelevant 0.6 -> not separable, no recommendation.
        res = calibrate(_ScriptedClient(0.4, 0.6), model="m")
        assert not res.separated
        assert res.suggested_threshold is None

    def test_recall_bias_floor_below_lowest_relevant(self):
        # The floor must never exceed the lowest relevant score (no false drops).
        res = calibrate(_ScriptedClient(0.55, 0.45), model="m")
        assert res.separated
        assert res.suggested_threshold is not None
        assert res.suggested_threshold < res.relevant_min

    def test_empty_scores_is_no_separation(self):
        # A broken endpoint that scores nothing -> health-check fail, no floor.
        res = _build_result("m", "unknown (no scores)", [], [])
        assert not res.separated
        assert res.suggested_threshold is None
        assert res.raw_scale == "unknown (no scores)"
