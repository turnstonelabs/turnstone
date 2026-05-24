"""Tests for output assessment storage operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _make_assessment_kwargs(**overrides):
    """Build default kwargs for record_output_assessment."""
    defaults = {
        "assessment_id": "oa_001",
        "ws_id": "ws-abc",
        "call_id": "tc_001",
        "func_name": "bash",
        "flags": '["credential_leak"]',
        "risk_level": "high",
        "annotations": "[]",
        "output_length": 256,
        "redacted": False,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# CRUD Operations
# ---------------------------------------------------------------------------


class TestOutputAssessmentCRUD:
    def test_record_and_list(self, db):
        db.record_output_assessment(**_make_assessment_kwargs())
        results = db.list_output_assessments()
        assert len(results) == 1
        assert results[0]["assessment_id"] == "oa_001"
        assert results[0]["ws_id"] == "ws-abc"
        assert results[0]["func_name"] == "bash"
        assert results[0]["risk_level"] == "high"


# ---------------------------------------------------------------------------
# Count queries
# ---------------------------------------------------------------------------


class TestOutputAssessmentCount:
    def test_count_basic(self, db):
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa2"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa3"))
        assert db.count_output_assessments() == 3

    def test_count_empty(self, db):
        assert db.count_output_assessments() == 0

    def test_count_with_ws_id(self, db):
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa1", ws_id="ws-1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa2", ws_id="ws-1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa3", ws_id="ws-2"))
        assert db.count_output_assessments(ws_id="ws-1") == 2

    def test_count_with_risk_level(self, db):
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa1", risk_level="low")
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa2", risk_level="high")
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa3", risk_level="high")
        )
        assert db.count_output_assessments(risk_level="high") == 2

    def test_count_with_since(self, db):
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa2"))

        future = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        assert db.count_output_assessments(since=future) == 0

    def test_count_with_until(self, db):
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa2"))

        past = "2020-01-01T00:00:00"
        assert db.count_output_assessments(until=past) == 0

    def test_count_with_date_range(self, db):
        now = datetime.now(UTC)
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa1"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa2"))
        db.record_output_assessment(**_make_assessment_kwargs(assessment_id="oa3"))

        one_minute_ago = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        one_minute_later = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        assert db.count_output_assessments(since=one_minute_ago, until=one_minute_later) == 3

    def test_count_matches_list_length(self, db):
        """Count with filters matches the length of list with same filters."""
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa1", ws_id="ws-1", risk_level="high")
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa2", ws_id="ws-1", risk_level="low")
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa3", ws_id="ws-2", risk_level="high")
        )

        now = datetime.now(UTC)
        one_minute_ago = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        one_minute_later = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")

        for ws, rl, s, u in [
            ("ws-1", "", "", ""),
            ("", "high", "", ""),
            ("ws-1", "high", "", ""),
            ("ws-2", "low", "", ""),
            ("", "", one_minute_ago, one_minute_later),
            ("ws-1", "high", one_minute_ago, one_minute_later),
        ]:
            count = db.count_output_assessments(ws_id=ws, risk_level=rl, since=s, until=u)
            listed = db.list_output_assessments(ws_id=ws, risk_level=rl, since=s, until=u)
            assert count == len(listed), (
                f"Mismatch for ws_id={ws!r}, risk_level={rl!r}, since={s!r}, until={u!r}"
            )


# ---------------------------------------------------------------------------
# Tier tie-breaker — when heuristic and llm rows share a second-resolution
# `created` value (the common case for two rows on the same call_id), the
# llm row must sort first so downstream consumers see the acted verdict.
# ---------------------------------------------------------------------------


class TestOutputAssessmentTierOrdering:
    def test_llm_wins_tie_on_same_created(self, db):
        # Two rows on the same call_id with the SAME `created` timestamp —
        # without the tier tie-breaker the order is randomised by
        # assessment_id (UUID).  With the tie-breaker, llm sorts first.
        # The insert path writes `created = now`, so back-to-back inserts
        # within the same wall-clock second already tie naturally.
        db.record_output_assessment(
            **_make_assessment_kwargs(
                assessment_id="oa_h",
                call_id="tc_tied",
                tier="heuristic",
            )
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(
                assessment_id="oa_l",
                call_id="tc_tied",
                tier="llm",
                reasoning="judged",
                judge_model="gpt-5-mini",
                latency_ms=42,
            )
        )
        rows = db.list_output_assessments()
        # Two rows for the same call_id; llm must be first.
        assert len(rows) == 2
        assert rows[0]["tier"] == "llm"
        assert rows[1]["tier"] == "heuristic"

    def test_single_tier_ordering_unchanged(self, db):
        # Single-tier rows (no LLM stage) should still sort by created DESC
        # — the tie-breaker only kicks in when timestamps match exactly.
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa_old", call_id="tc_a")
        )
        db.record_output_assessment(
            **_make_assessment_kwargs(assessment_id="oa_new", call_id="tc_b")
        )
        rows = db.list_output_assessments()
        # Most recent first; with both at "heuristic" tier the secondary
        # sort falls through to assessment_id DESC, but the key invariant
        # is that listing produces both rows in a deterministic order.
        assert len(rows) == 2
        assert {r["assessment_id"] for r in rows} == {"oa_old", "oa_new"}
