"""Tests for turnstone.console.metrics."""

from __future__ import annotations

from turnstone.console.metrics import ConsoleMetrics


class TestRecordRoute:
    """Recording routed requests."""

    def test_single_request(self) -> None:
        m = ConsoleMetrics()
        m.record_route("send", 200, 0.05)

        text = m.generate_text()
        assert 'turnstone_router_requests_total{method="send",status="2xx"} 1' in text

    def test_multiple_methods(self) -> None:
        m = ConsoleMetrics()
        m.record_route("send", 200, 0.01)
        m.record_route("create", 200, 0.02)
        m.record_route("send", 502, 0.5)

        text = m.generate_text()
        assert 'turnstone_router_requests_total{method="send",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="create",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="send",status="5xx"} 1' in text

    def test_duration_recorded(self) -> None:
        m = ConsoleMetrics()
        m.record_route("send", 200, 0.123)
        m.record_route("send", 200, 0.456)

        text = m.generate_text()
        assert 'turnstone_router_request_duration_seconds_count{method="send"} 2' in text
        # Sum should be 0.579
        assert "turnstone_router_request_duration_seconds_sum" in text


class TestRecordJudgeVerdict:
    """Coord-side intent-judge verdict counter."""

    def test_single_verdict(self) -> None:
        m = ConsoleMetrics()
        m.record_judge_verdict("heuristic", "high", 12)

        text = m.generate_text()
        assert 'turnstone_judge_verdicts_total{tier="heuristic",risk_level="high"} 1' in text

    def test_aggregates_by_tier_and_risk(self) -> None:
        m = ConsoleMetrics()
        m.record_judge_verdict("heuristic", "low", 5)
        m.record_judge_verdict("heuristic", "low", 7)
        m.record_judge_verdict("llm", "high", 250)

        text = m.generate_text()
        assert 'turnstone_judge_verdicts_total{tier="heuristic",risk_level="low"} 2' in text
        assert 'turnstone_judge_verdicts_total{tier="llm",risk_level="high"} 1' in text

    def test_section_omitted_when_empty(self) -> None:
        """No verdicts recorded → don't emit the empty header block."""
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "turnstone_judge_verdicts_total" not in text


class TestRouterInfo:
    """Live-membership gauge + refresh counter."""

    def test_defaults_zero(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "turnstone_router_membership_size 0" in text
        assert "turnstone_router_refresh_total 0" in text

    def test_set_router_info(self) -> None:
        m = ConsoleMetrics()
        m.set_router_info(3, 7)

        text = m.generate_text()
        assert "turnstone_router_membership_size 3" in text
        assert "turnstone_router_refresh_total 7" in text


class TestGenerateText:
    """Output format validation."""

    def test_contains_all_metric_names(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        expected = [
            "turnstone_router_requests_total",
            "turnstone_router_request_duration_seconds",
            "turnstone_router_membership_size",
            "turnstone_router_refresh_total",
        ]
        for name in expected:
            assert name in text, f"Missing metric: {name}"

    def test_has_help_and_type(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "# HELP turnstone_router_requests_total" in text
        assert "# TYPE turnstone_router_requests_total counter" in text
        assert "# HELP turnstone_router_membership_size" in text
        assert "# TYPE turnstone_router_membership_size gauge" in text

    def test_ends_with_newline(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert text.endswith("\n")

    def test_combined_scenario(self) -> None:
        """Full scenario: routes + router info."""
        m = ConsoleMetrics()
        m.record_route("create", 200, 0.1)
        m.record_route("send", 200, 0.05)
        m.record_route("send", 502, 1.2)
        m.set_router_info(3, 12)

        text = m.generate_text()
        assert 'turnstone_router_requests_total{method="create",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="send",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="send",status="5xx"} 1' in text
        assert "turnstone_router_membership_size 3" in text
        assert "turnstone_router_refresh_total 12" in text
