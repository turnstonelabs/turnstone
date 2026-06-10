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
        # ``user_decision`` defaults to ``"pending"`` (not the empty
        # string) so an audit reader can distinguish in-flight rows
        # from pre-convention legacy rows that carry the column's
        # server_default of ``""``.
        assert v["user_decision"] == "pending"
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


class TestIntentVerdictUpsert:
    """``upsert_intent_verdict`` — the LLM-tier-aware persistence path.

    Backs the heuristic → llm_fallback "upgrade in place" pattern.
    The async judge's fallback verdicts deliberately reuse the
    heuristic ``verdict_id``; a plain INSERT would collide on the
    PK and the upgrade would be lost to a silently-swallowed
    exception (Postgres logged ``intent_verdicts_pkey`` violations
    for every fallback delivery on stable/1.5 smoke tests).
    """

    def test_upsert_on_fresh_id_inserts(self, db):
        """No conflict — behaves like a regular INSERT."""
        db.upsert_intent_verdict(**_make_verdict_kwargs())
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["tier"] == "heuristic"
        assert v["user_decision"] == "pending"

    def test_upsert_on_conflict_upgrades_tier_reasoning_judge_model(self, db):
        """On PK conflict: tier, reasoning, judge_model update — every
        other field is preserved.  Mirrors what the judge emits when
        promoting heuristic → llm_fallback."""
        db.upsert_intent_verdict(
            **_make_verdict_kwargs(
                tier="heuristic",
                reasoning="initial heuristic reasoning",
                judge_model="",
            )
        )
        db.upsert_intent_verdict(
            **_make_verdict_kwargs(
                tier="llm_fallback",
                reasoning="initial heuristic reasoning (LLM judge did not return a verdict)",
                judge_model="gpt-5-judge",
            )
        )
        v = db.get_intent_verdict("v_001")
        assert v is not None
        # The three fields that should change.
        assert v["tier"] == "llm_fallback"
        assert "LLM judge did not return" in v["reasoning"]
        assert v["judge_model"] == "gpt-5-judge"

    def test_upsert_on_conflict_preserves_user_decision(self, db):
        """LOAD-BEARING: a manually-resolved approval (user_decision=
        ``"approved"``) or auto-approve-stamped row (user_decision=
        ``"policy"``/``"blanket"``/etc.) must NOT be clobbered back to
        ``"pending"`` when the late LLM-fallback verdict lands.
        ``IntentVerdict.to_dict()`` doesn't project user_decision, so
        the upsert's defaulted ``"pending"`` would silently overwrite
        the real value if user_decision were in the on-conflict
        SET clause."""
        db.upsert_intent_verdict(**_make_verdict_kwargs())
        ok = db.update_intent_verdict("v_001", user_decision="approved")
        assert ok is True
        # Simulate the late LLM-fallback delivery — same verdict_id,
        # default user_decision (the IntentVerdict.to_dict() shape).
        db.upsert_intent_verdict(
            **_make_verdict_kwargs(
                tier="llm_fallback",
                reasoning="extended (LLM judge did not return a verdict)",
                judge_model="gpt-5-judge",
            )
        )
        v = db.get_intent_verdict("v_001")
        assert v is not None
        assert v["user_decision"] == "approved"  # NOT clobbered to "pending"
        assert v["tier"] == "llm_fallback"  # but the upgrade did land

    def test_upsert_on_conflict_preserves_identity_and_carried_fields(self, db):
        """Identity columns (ws_id, call_id, func_name, func_args) and
        carried-verbatim columns (intent_summary, risk_level,
        confidence, recommendation, evidence, latency_ms) are
        excluded from the on-conflict SET — verify they aren't
        changed even when the second upsert passes different values
        (defensive against a future judge bug that ships divergent
        carried fields)."""
        db.upsert_intent_verdict(**_make_verdict_kwargs())
        db.upsert_intent_verdict(
            **_make_verdict_kwargs(
                # Same verdict_id (conflict trigger), divergent everything else.
                ws_id="ws-different",
                call_id="tc_different",
                func_name="bash_v2",
                func_args='{"command":"rm -rf /"}',
                intent_summary="totally different summary",
                risk_level="critical",
                confidence=0.0,
                recommendation="deny",
                evidence='["dangerous"]',
                latency_ms=99999,
                # The three fields that DO update.
                tier="llm_fallback",
                reasoning="upgraded reasoning",
                judge_model="judge-v2",
            )
        )
        v = db.get_intent_verdict("v_001")
        assert v is not None
        # All preserved from the first upsert (identity + carried).
        assert v["ws_id"] == "ws-abc"
        assert v["call_id"] == "tc_001"
        assert v["func_name"] == "bash"
        assert v["func_args"] == '{"command":"echo hello"}'
        assert v["intent_summary"] == "Echo a greeting to stdout"
        assert v["risk_level"] == "low"
        assert v["confidence"] == 0.85
        assert v["recommendation"] == "approve"
        assert v["evidence"] == '["The command only prints text."]'
        assert v["latency_ms"] == 2
        # Only the three updated.
        assert v["tier"] == "llm_fallback"
        assert v["reasoning"] == "upgraded reasoning"
        assert v["judge_model"] == "judge-v2"


# ---------------------------------------------------------------------------
# Bulk insert
# ---------------------------------------------------------------------------


class TestIntentVerdictBulkInsert:
    """Coverage for ``create_intent_verdicts_bulk`` — backs the
    ``approve_tools`` per-turn heuristic-verdict persistence path so a
    fan-out turn pays one commit instead of N.
    """

    def test_bulk_insert_creates_all_rows(self, db):
        db.create_intent_verdicts_bulk(
            [
                _make_verdict_kwargs(verdict_id="b1", call_id="c1"),
                _make_verdict_kwargs(verdict_id="b2", call_id="c2"),
                _make_verdict_kwargs(verdict_id="b3", call_id="c3"),
            ]
        )
        for vid in ("b1", "b2", "b3"):
            v = db.get_intent_verdict(vid)
            assert v is not None
            assert v["verdict_id"] == vid

    def test_bulk_insert_empty_list_is_noop(self, db):
        # Must not raise and must not commit a phantom row.
        db.create_intent_verdicts_bulk([])
        assert db.list_intent_verdicts() == []

    def test_bulk_insert_preserves_distinct_field_values(self, db):
        db.create_intent_verdicts_bulk(
            [
                _make_verdict_kwargs(
                    verdict_id="b1",
                    risk_level="low",
                    tier="heuristic",
                    confidence=0.4,
                ),
                _make_verdict_kwargs(
                    verdict_id="b2",
                    risk_level="high",
                    tier="llm",
                    confidence=0.95,
                ),
            ]
        )
        v1 = db.get_intent_verdict("b1")
        v2 = db.get_intent_verdict("b2")
        assert v1 is not None and v2 is not None
        assert v1["risk_level"] == "low" and v1["tier"] == "heuristic"
        assert v2["risk_level"] == "high" and v2["tier"] == "llm"

    def test_bulk_insert_pk_collision_skips_only_colliding_row(self, db):
        """Regression: the async judge daemon can UPSERT a fallback row —
        reusing a heuristic verdict_id from the incoming batch — BEFORE
        ``approve_tools`` runs the bulk write.  The bulk insert must skip
        just that row (keeping the daemon's tier upgrade) instead of
        aborting the whole statement and silently losing every sibling
        row in the batch."""
        # Daemon won the race: fallback row already sits on b2's PK.
        db.upsert_intent_verdict(
            **_make_verdict_kwargs(
                verdict_id="b2",
                call_id="c2",
                tier="llm_fallback",
                judge_model="judge-model",
            )
        )
        db.create_intent_verdicts_bulk(
            [
                _make_verdict_kwargs(verdict_id="b1", call_id="c1"),
                _make_verdict_kwargs(verdict_id="b2", call_id="c2"),  # collides
                _make_verdict_kwargs(verdict_id="b3", call_id="c3"),
            ]
        )
        # Siblings landed despite the mid-batch collision.
        for vid in ("b1", "b3"):
            v = db.get_intent_verdict(vid)
            assert v is not None, f"sibling row {vid} lost to the collision"
            assert v["tier"] == "heuristic"
        # The colliding row kept the daemon's upgrade, not the bulk stamp.
        v2 = db.get_intent_verdict("b2")
        assert v2 is not None
        assert v2["tier"] == "llm_fallback"
        assert v2["judge_model"] == "judge-model"


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
