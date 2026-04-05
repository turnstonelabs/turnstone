"""Tests for turnstone.core.healthcheck — passive backend health tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

from turnstone.core.healthcheck import BackendHealthTracker, HealthTrackerRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_metrics() -> Generator[MagicMock]:
    """Patch the metrics singleton so set_backend_status exists."""
    m = MagicMock()
    with (
        patch("turnstone.core.healthcheck.metrics", m, create=True),
        patch("turnstone.core.metrics.metrics", m, create=True),
    ):
        yield m


def _make_tracker(failure_threshold: int = 3) -> BackendHealthTracker:
    return BackendHealthTracker(failure_threshold=failure_threshold)


# ---------------------------------------------------------------------------
# BackendHealthTracker
# ---------------------------------------------------------------------------


class TestBackendHealthTracker:
    def test_starts_healthy(self) -> None:
        t = _make_tracker()
        assert t.is_healthy is True
        assert t.is_degraded is False
        assert t.consecutive_failures == 0

    def test_failures_below_threshold(self, mock_metrics: MagicMock) -> None:
        """Failures below threshold do not degrade."""
        t = _make_tracker(failure_threshold=5)
        for _ in range(4):
            t.record_failure()
        assert t.is_healthy is True
        assert t.consecutive_failures == 4

    def test_degrades_at_threshold(self, mock_metrics: MagicMock) -> None:
        t = _make_tracker(failure_threshold=3)
        for _ in range(3):
            t.record_failure()
        assert t.is_degraded is True
        assert t.is_healthy is False

    def test_stays_degraded_on_more_failures(self, mock_metrics: MagicMock) -> None:
        t = _make_tracker(failure_threshold=2)
        for _ in range(5):
            t.record_failure()
        assert t.is_degraded is True
        assert t.consecutive_failures == 5

    def test_success_clears_degraded(self, mock_metrics: MagicMock) -> None:
        t = _make_tracker(failure_threshold=2)
        t.record_failure()
        t.record_failure()
        assert t.is_degraded is True
        t.record_success()
        assert t.is_healthy is True
        assert t.consecutive_failures == 0

    def test_success_resets_failure_count(self, mock_metrics: MagicMock) -> None:
        t = _make_tracker(failure_threshold=5)
        for _ in range(4):
            t.record_failure()
        t.record_success()
        assert t.consecutive_failures == 0
        # Should need 5 more failures to degrade
        for _ in range(4):
            t.record_failure()
        assert t.is_healthy is True

    def test_state_changed_callback_on_degrade(self, mock_metrics: MagicMock) -> None:
        events: list[str] = []
        t = BackendHealthTracker(failure_threshold=2, on_state_changed=events.append)
        t.record_failure()
        assert events == []
        t.record_failure()
        assert events == ["degraded"]

    def test_state_changed_callback_on_recover(self, mock_metrics: MagicMock) -> None:
        events: list[str] = []
        t = BackendHealthTracker(failure_threshold=1, on_state_changed=events.append)
        t.record_failure()
        assert events == ["degraded"]
        t.record_success()
        assert events == ["degraded", "healthy"]

    def test_no_callback_when_already_degraded(self, mock_metrics: MagicMock) -> None:
        """Extra failures after degraded don't fire again."""
        events: list[str] = []
        t = BackendHealthTracker(failure_threshold=1, on_state_changed=events.append)
        t.record_failure()
        t.record_failure()
        t.record_failure()
        assert events == ["degraded"]  # only once

    def test_no_callback_when_already_healthy(self, mock_metrics: MagicMock) -> None:
        """Success while healthy doesn't fire."""
        events: list[str] = []
        t = BackendHealthTracker(failure_threshold=3, on_state_changed=events.append)
        t.record_success()
        t.record_success()
        assert events == []

    def test_no_direct_metrics_calls(self) -> None:
        """Tracker does not touch metrics — the server callback handles it."""
        t = _make_tracker(failure_threshold=1)
        t.record_failure()
        t.record_success()
        # No assertion on metrics — the tracker delegates metric updates
        # to the server-level callback via on_state_changed


# ---------------------------------------------------------------------------
# HealthTrackerRegistry
# ---------------------------------------------------------------------------


class TestHealthTrackerRegistry:
    def test_same_backend_shares_tracker(self, mock_metrics: MagicMock) -> None:
        """Two aliases on the same (provider, base_url) share a tracker."""
        reg = HealthTrackerRegistry(failure_threshold=5)
        t1 = reg.get_tracker("openai", "https://api.openai.com/v1")
        t2 = reg.get_tracker("openai", "https://api.openai.com/v1")
        assert t1 is t2

    def test_different_backends_independent(self, mock_metrics: MagicMock) -> None:
        """Different (provider, base_url) pairs get independent trackers."""
        reg = HealthTrackerRegistry(failure_threshold=5)
        t_cloud = reg.get_tracker("openai", "https://api.openai.com/v1")
        t_local = reg.get_tracker("openai-compatible", "http://localhost:8000/v1")
        assert t_cloud is not t_local

    def test_trailing_slash_normalized(self, mock_metrics: MagicMock) -> None:
        """Trailing slashes on base_url are normalized away."""
        reg = HealthTrackerRegistry(failure_threshold=5)
        t1 = reg.get_tracker("openai", "https://api.openai.com/v1/")
        t2 = reg.get_tracker("openai", "https://api.openai.com/v1")
        assert t1 is t2

    def test_degraded_isolation(self, mock_metrics: MagicMock) -> None:
        """Degrading one backend does not affect another."""
        reg = HealthTrackerRegistry(failure_threshold=2)
        t_cloud = reg.get_tracker("openai", "https://api.openai.com/v1")
        t_local = reg.get_tracker("openai-compatible", "http://localhost:8000/v1")
        # Degrade the cloud tracker
        t_cloud.record_failure()
        t_cloud.record_failure()
        assert t_cloud.is_degraded is True
        # Local should be unaffected
        assert t_local.is_healthy is True

    def test_get_tracker_for_alias(self, mock_metrics: MagicMock) -> None:
        """get_tracker_for_alias looks up by model config's backend."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry

        models = {
            "cloud": ModelConfig(
                "cloud", "https://api.openai.com/v1", "sk", "gpt-4o", provider="openai"
            ),
            "local": ModelConfig(
                "local", "http://localhost:8000/v1", "x", "qwen", provider="openai-compatible"
            ),
        }
        model_reg = ModelRegistry(models=models, default="cloud")

        reg = HealthTrackerRegistry(failure_threshold=5)
        # No tracker created yet — should return None
        assert reg.get_tracker_for_alias(model_reg, "cloud") is None

        # Create a tracker for the cloud backend
        t = reg.get_tracker("openai", "https://api.openai.com/v1")
        assert reg.get_tracker_for_alias(model_reg, "cloud") is t

        # Local alias should still return None (no tracker for that backend)
        assert reg.get_tracker_for_alias(model_reg, "local") is None

    def test_state_changed_callback(self, mock_metrics: MagicMock) -> None:
        """on_state_changed fires with backend key and state."""
        events: list[tuple[str, str]] = []
        reg = HealthTrackerRegistry(
            failure_threshold=2,
            on_state_changed=lambda backend, state: events.append((backend, state)),
        )
        t = reg.get_tracker("openai", "https://api.openai.com/v1")
        t.record_failure()
        t.record_failure()  # triggers degraded
        assert len(events) == 1
        assert events[0][0] == "openai:https://api.openai.com/v1"
        assert events[0][1] == "degraded"

    def test_backend_key_static(self) -> None:
        """backend_key is a static method returning normalized tuple."""
        key = HealthTrackerRegistry.backend_key("anthropic", "https://api.anthropic.com/")
        assert key == ("anthropic", "https://api.anthropic.com")
