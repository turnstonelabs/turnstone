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


class TestRingInfo:
    """Ring membership and version gauges."""

    def test_defaults_zero(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "turnstone_ring_membership_size 0" in text
        assert "turnstone_ring_version 0" in text

    def test_set_ring_info(self) -> None:
        m = ConsoleMetrics()
        m.set_ring_info(3, 7)

        text = m.generate_text()
        assert "turnstone_ring_membership_size 3" in text
        assert "turnstone_ring_version 7" in text


class TestRebalance:
    """Rebalance and migration counters."""

    def test_noop(self) -> None:
        m = ConsoleMetrics()
        m.record_rebalance("noop")

        text = m.generate_text()
        assert 'turnstone_ring_rebalance_total{result="noop"} 1' in text

    def test_seeded(self) -> None:
        m = ConsoleMetrics()
        m.record_rebalance("seeded")

        text = m.generate_text()
        assert 'turnstone_ring_rebalance_total{result="seeded"} 1' in text

    def test_rebalanced(self) -> None:
        m = ConsoleMetrics()
        m.record_rebalance("rebalanced")
        m.record_rebalance("rebalanced")

        text = m.generate_text()
        assert 'turnstone_ring_rebalance_total{result="rebalanced"} 2' in text

    def test_migrations(self) -> None:
        m = ConsoleMetrics()
        m.record_migrations(5)
        m.record_migrations(3)

        text = m.generate_text()
        assert "turnstone_ring_migrations_total 8" in text

    def test_migrations_default_zero(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "turnstone_ring_migrations_total 0" in text


class TestGenerateText:
    """Output format validation."""

    def test_contains_all_metric_names(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        expected = [
            "turnstone_router_requests_total",
            "turnstone_router_request_duration_seconds",
            "turnstone_ring_membership_size",
            "turnstone_ring_version",
            "turnstone_ring_rebalance_total",
            "turnstone_ring_migrations_total",
        ]
        for name in expected:
            assert name in text, f"Missing metric: {name}"

    def test_has_help_and_type(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert "# HELP turnstone_router_requests_total" in text
        assert "# TYPE turnstone_router_requests_total counter" in text
        assert "# HELP turnstone_ring_membership_size" in text
        assert "# TYPE turnstone_ring_membership_size gauge" in text

    def test_ends_with_newline(self) -> None:
        m = ConsoleMetrics()
        text = m.generate_text()
        assert text.endswith("\n")

    def test_combined_scenario(self) -> None:
        """Full scenario: routes, ring info, rebalances, migrations."""
        m = ConsoleMetrics()
        m.record_route("create", 200, 0.1)
        m.record_route("send", 200, 0.05)
        m.record_route("send", 502, 1.2)
        m.set_ring_info(3, 12)
        m.record_rebalance("seeded")
        m.record_rebalance("noop")
        m.record_rebalance("rebalanced")
        m.record_migrations(4)

        text = m.generate_text()
        assert 'turnstone_router_requests_total{method="create",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="send",status="2xx"} 1' in text
        assert 'turnstone_router_requests_total{method="send",status="5xx"} 1' in text
        assert "turnstone_ring_membership_size 3" in text
        assert "turnstone_ring_version 12" in text
        assert 'turnstone_ring_rebalance_total{result="noop"} 1' in text
        assert 'turnstone_ring_rebalance_total{result="rebalanced"} 1' in text
        assert 'turnstone_ring_rebalance_total{result="seeded"} 1' in text
        assert "turnstone_ring_migrations_total 4" in text
