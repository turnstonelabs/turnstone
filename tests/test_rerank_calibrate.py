"""Calibration core — probe a fake reranker and recommend a 0-1 floor."""

from __future__ import annotations

import pytest

from turnstone.core.rerank import RerankHit
from turnstone.core.rerank_calibrate import (
    _GAP_FRACTION,
    _PROBE_SET,
    _build_result,
    calibrate,
    calibrate_model,
)


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


class TestCalibrateModelLifecycle:
    class _ClosableClient(_ScriptedClient):
        def __init__(self, *, fail: bool = False) -> None:
            super().__init__(0.9, 0.1)
            self.fail = fail
            self.close_calls = 0

        def rerank(
            self, query: str, documents: list[str], *, top_n: int | None = None
        ) -> list[RerankHit]:
            if self.fail:
                raise RuntimeError("probe failed")
            return super().rerank(query, documents, top_n=top_n)

        def close(self) -> None:
            self.close_calls += 1

    def test_one_shot_client_closes_after_success(self, monkeypatch) -> None:
        client = self._ClosableClient()
        monkeypatch.setattr("turnstone.core.rerank.resolve_rerank_client", lambda *a, **k: client)

        result = calibrate_model("http://rr/rerank", "m", "k")

        assert result.separated
        assert client.close_calls == 1

    def test_one_shot_client_closes_after_failure(self, monkeypatch) -> None:
        client = self._ClosableClient(fail=True)
        monkeypatch.setattr("turnstone.core.rerank.resolve_rerank_client", lambda *a, **k: client)

        with pytest.raises(RuntimeError, match="probe failed"):
            calibrate_model("http://rr/rerank", "m", "k")

        assert client.close_calls == 1


class TestCalibrationCapsConfinement:
    def test_calibrate_merge_confined_to_calibration_fields(self):
        """The merge touches only the three probe-derived keys and preserves
        everything else in the gated ``capabilities`` column."""
        import json

        from turnstone.core.rerank_calibrate import (
            calibration_caps_fields,
            merge_calibration_into_caps,
        )

        result = _build_result("m", "probability (0-1)", [0.9, 0.95], [0.1, 0.2])
        fields = calibration_caps_fields(result)
        assert set(fields) == {"rerank_threshold", "rerank_scale", "rerank_separated"}

        existing = {
            "server_compat": {"api_surface": "chat", "extra_body": {"x": 1}},
            "context_window": 5,
        }
        merged = json.loads(merge_calibration_into_caps(json.dumps(existing), result))
        assert merged["server_compat"] == existing["server_compat"]
        assert merged["context_window"] == 5
        assert set(merged) == set(existing) | set(fields)

    def test_confinement_refuses_type_flip_that_python_equality_masks(self):
        """Python ``!=`` conflates ``True`` with ``1``; the confinement
        compare canonicalizes per key like the write gate's comparator."""
        import json

        from turnstone.core.rerank_calibrate import (
            calibration_caps_fields,
            calibration_confinement_violations,
        )

        result = _build_result("m", "probability (0-1)", [0.9, 0.95], [0.1, 0.2])
        stored = json.dumps({"server_compat": {"stream": 1}})
        merged = json.dumps({"server_compat": {"stream": True}, **calibration_caps_fields(result)})

        assert calibration_confinement_violations(stored, merged, result) == ["server_compat"]

    def test_confinement_ignores_integral_float_spelling(self):
        """``1.0`` vs ``1`` is JSON round-trip spelling, not a value change,
        so a healthy merge is not refused over it."""
        import json

        from turnstone.core.rerank_calibrate import (
            calibration_caps_fields,
            calibration_confinement_violations,
        )

        result = _build_result("m", "probability (0-1)", [0.9, 0.95], [0.1, 0.2])
        stored = json.dumps({"server_compat": {"scale": 1.0}})
        merged = json.dumps({"server_compat": {"scale": 1}, **calibration_caps_fields(result)})

        assert calibration_confinement_violations(stored, merged, result) == []
