"""Tests for intent verdict storage operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _make_verdict_kwargs(**overrides):
    """Build default kwargs for create_intent_verdict."""
    defaults = {
        "verdict_id": "v_001",
        "ws_id": "ws-abc",
        "call_id": "tc_001",
        "func_name": "bash",
        "func_args": '{"command":"echo hello"}',
        "intent_summary": "Echo a greeting to stdout",
        "risk_level": "low",
        "confidence": 0.85,
        "recommendation": "approve",
        "reasoning": "Simple echo command with no side effects.",
        "evidence": '["The command only prints text."]',
        "tier": "heuristic",
        "judge_model": "",
        "latency_ms": 2,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# CRUD Operations
# ---------------------------------------------------------------------------


class TestIntentVerdictCRUD:
    def test_create_and_get(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs())
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["verdict_id"] == "v_001"
        assert v["ws_id"] == "ws-abc"
        assert v["call_id"] == "tc_001"
        assert v["func_name"] == "bash"
        assert v["func_args"] == '{"command":"echo hello"}'
        assert v["intent_summary"] == "Echo a greeting to stdout"
        assert v["risk_level"] == "low"
        assert v["confidence"] == 0.85
        assert v["recommendation"] == "approve"
        assert v["reasoning"] == "Simple echo command with no side effects."
        assert v["evidence"] == '["The command only prints text."]'
        assert v["tier"] == "heuristic"
        assert v["judge_model"] == ""
        assert v["latency_ms"] == 2
        assert v["user_decision"] == ""
        assert "created" in v

    def test_get_nonexistent(self, db):
        assert db.get_intent_verdict("nonexistent") is None

    def test_update_user_decision(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs())
        ok = db.update_intent_verdict("v_001", user_decision="approved")
        assert ok is True
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["user_decision"] == "approved"

    def test_update_mutable_fields(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs())
        ok = db.update_intent_verdict(
            "v_001",
            intent_summary="Updated summary",
            risk_level="high",
            confidence=0.95,
            recommendation="deny",
            reasoning="Changed reasoning",
            evidence='["new evidence"]',
            tier="llm",
            judge_model="gpt-5",
            latency_ms=500,
        )
        assert ok is True
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["intent_summary"] == "Updated summary"
        assert v["risk_level"] == "high"
        assert v["confidence"] == 0.95
        assert v["recommendation"] == "deny"
        assert v["reasoning"] == "Changed reasoning"
        assert v["evidence"] == '["new evidence"]'
        assert v["tier"] == "llm"
        assert v["judge_model"] == "gpt-5"
        assert v["latency_ms"] == 500

    def test_update_rejects_immutable_fields(self, db):
        """Non-mutable fields like ws_id, call_id, func_name are rejected."""
        db.create_intent_verdict(**_make_verdict_kwargs())
        # Only non-mutable fields passed — should return False (no valid fields).
        ok = db.update_intent_verdict(
            "v_001",
            ws_id="ws-hacked",
            call_id="tc_hacked",
            func_name="hacked",
        )
        assert ok is False
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["ws_id"] == "ws-abc"
        assert v["call_id"] == "tc_001"
        assert v["func_name"] == "bash"

    def test_update_nonexistent(self, db):
        ok = db.update_intent_verdict("missing", user_decision="approved")
        assert ok is False


# ---------------------------------------------------------------------------
# List queries
# ---------------------------------------------------------------------------


class TestIntentVerdictList:
    def test_list_by_ws_id(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1", ws_id="ws-1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2", ws_id="ws-1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3", ws_id="ws-2"))

        results = db.list_intent_verdicts(ws_id="ws-1")
        assert len(results) == 2
        assert all(r["ws_id"] == "ws-1" for r in results)

    def test_list_by_risk_level(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1", risk_level="low"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2", risk_level="high"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3", risk_level="low"))

        results = db.list_intent_verdicts(risk_level="high")
        assert len(results) == 1
        assert results[0]["verdict_id"] == "v2"

    def test_list_by_date_range(self, db):
        now = datetime.now(UTC)

        # create_intent_verdict uses datetime.now(UTC) internally, so
        # we test with since/until relative to the auto-created time.
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3"))

        # All should be within a recent window
        one_minute_ago = (now - timedelta(minutes=1)).isoformat()
        one_minute_later = (now + timedelta(minutes=1)).isoformat()
        results = db.list_intent_verdicts(since=one_minute_ago, until=one_minute_later)
        assert len(results) == 3

        # Nothing before a far-past date
        ancient = "2020-01-01T00:00:00"
        results = db.list_intent_verdicts(until=ancient)
        assert len(results) == 0

    def test_list_pagination(self, db):
        for i in range(10):
            db.create_intent_verdict(**_make_verdict_kwargs(verdict_id=f"v_{i:03d}"))

        page1 = db.list_intent_verdicts(limit=3, offset=0)
        assert len(page1) == 3

        page2 = db.list_intent_verdicts(limit=3, offset=3)
        assert len(page2) == 3

        # Pages should not overlap
        ids1 = {r["verdict_id"] for r in page1}
        ids2 = {r["verdict_id"] for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_list_ordering_desc(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v_aaa"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v_bbb"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v_ccc"))

        results = db.list_intent_verdicts()
        # Created timestamps are likely identical (fast inserts), so
        # secondary sort is by verdict_id DESC.
        ids = [r["verdict_id"] for r in results]
        assert ids == ["v_ccc", "v_bbb", "v_aaa"]

    def test_list_empty(self, db):
        assert db.list_intent_verdicts() == []

    def test_list_combined_filters(self, db):
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v1", ws_id="ws-1", risk_level="high")
        )
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v2", ws_id="ws-1", risk_level="low")
        )
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v3", ws_id="ws-2", risk_level="high")
        )

        results = db.list_intent_verdicts(ws_id="ws-1", risk_level="high")
        assert len(results) == 1
        assert results[0]["verdict_id"] == "v1"


# ---------------------------------------------------------------------------
# Count queries
# ---------------------------------------------------------------------------


class TestIntentVerdictCount:
    def test_count_basic(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3"))
        assert db.count_intent_verdicts() == 3

    def test_count_empty(self, db):
        assert db.count_intent_verdicts() == 0

    def test_count_with_ws_id(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1", ws_id="ws-1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2", ws_id="ws-1"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3", ws_id="ws-2"))
        assert db.count_intent_verdicts(ws_id="ws-1") == 2

    def test_count_with_risk_level(self, db):
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v1", risk_level="low"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v2", risk_level="high"))
        db.create_intent_verdict(**_make_verdict_kwargs(verdict_id="v3", risk_level="high"))
        assert db.count_intent_verdicts(risk_level="high") == 2

    def test_count_matches_list_length(self, db):
        """Count with filters matches the length of list with same filters."""
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v1", ws_id="ws-1", risk_level="high")
        )
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v2", ws_id="ws-1", risk_level="low")
        )
        db.create_intent_verdict(
            **_make_verdict_kwargs(verdict_id="v3", ws_id="ws-2", risk_level="high")
        )

        for ws, rl in [("ws-1", ""), ("", "high"), ("ws-1", "high"), ("ws-2", "low")]:
            count = db.count_intent_verdicts(ws_id=ws, risk_level=rl)
            listed = db.list_intent_verdicts(ws_id=ws, risk_level=rl)
            assert count == len(listed), f"Mismatch for ws_id={ws!r}, risk_level={rl!r}"
