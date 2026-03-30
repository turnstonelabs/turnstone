"""Tests for turnstone.console.router."""

from __future__ import annotations

from typing import Any

import pytest

from turnstone.console.router import ConsoleRouter, NodeRef
from turnstone.core.hash_ring import RING_SIZE, NoAvailableNodeError

# ---------------------------------------------------------------------------
# Fake storage
# ---------------------------------------------------------------------------


class FakeStorage:
    """Minimal storage mock for router tests."""

    def __init__(self) -> None:
        self.services: list[dict[str, str]] = []
        self.buckets: list[dict[str, Any]] = []
        self.overrides: list[dict[str, str]] = []
        self.settings: dict[str, dict[str, Any]] = {}

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        return list(self.services)

    def list_ring_buckets(self) -> list[dict[str, Any]]:
        return list(self.buckets)

    def list_workstream_overrides(self) -> list[dict[str, str]]:
        return list(self.overrides)

    def get_system_setting(self, key: str, node_id: str = "") -> dict[str, Any] | None:
        return self.settings.get(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NODE_A = {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"}
NODE_B = {"service_id": "node-b", "url": "http://b:8080", "metadata": "{}"}
NODE_C = {"service_id": "node-c", "url": "http://c:8080", "metadata": "{}"}


def _make_router(storage: FakeStorage | None = None) -> tuple[ConsoleRouter, FakeStorage]:
    s = storage or FakeStorage()
    return ConsoleRouter(s), s  # type: ignore[arg-type]


def _ws_id_for_bucket(bucket: int) -> str:
    """Build a 32-char hex ws_id whose first 4 chars encode *bucket*."""
    return f"{bucket:04x}" + "0" * 28


# ---------------------------------------------------------------------------
# TestRouteBasic
# ---------------------------------------------------------------------------


class TestRouteBasic:
    """Basic routing through the bucket cache."""

    def test_route_returns_correct_node(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        storage.buckets = [
            {"bucket": 0x0000, "node_id": "node-a"},
            {"bucket": 0x0001, "node_id": "node-b"},
            {"bucket": 0x0002, "node_id": "node-c"},
        ]
        router.refresh_cache()

        assert router.route(_ws_id_for_bucket(0x0000)) == NodeRef("node-a", "http://a:8080")
        assert router.route(_ws_id_for_bucket(0x0001)) == NodeRef("node-b", "http://b:8080")
        assert router.route(_ws_id_for_bucket(0x0002)) == NodeRef("node-c", "http://c:8080")

    def test_route_override_priority(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        storage.buckets = [{"bucket": 0x0000, "node_id": "node-a"}]
        ws_id = _ws_id_for_bucket(0x0000)
        storage.overrides = [{"ws_id": ws_id, "node_id": "node-b"}]
        router.refresh_cache()

        # Override wins over bucket assignment
        assert router.route(ws_id) == NodeRef("node-b", "http://b:8080")

    def test_route_empty_cache_raises(self) -> None:
        router, _ = _make_router()

        with pytest.raises(NoAvailableNodeError, match="not assigned"):
            router.route(_ws_id_for_bucket(0x0000))

    def test_route_url_convenience(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0x0010, "node_id": "node-a"}]
        router.refresh_cache()

        assert router.route_url(_ws_id_for_bucket(0x0010)) == "http://a:8080"


# ---------------------------------------------------------------------------
# TestRefreshCache
# ---------------------------------------------------------------------------


class TestRefreshCache:
    """Cache loading from storage."""

    def test_refresh_loads_from_storage(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 100, "node_id": "node-a"}]
        router.refresh_cache()

        ref = router.route(_ws_id_for_bucket(100))
        assert ref.node_id == "node-a"

    def test_refresh_handles_dead_nodes(self) -> None:
        router, storage = _make_router()
        # node-b is in buckets but not in services (dead/expired)
        storage.services = [NODE_A]
        storage.buckets = [
            {"bucket": 0x0000, "node_id": "node-a"},
            {"bucket": 0x0001, "node_id": "node-b"},
        ]
        router.refresh_cache()

        assert router.route(_ws_id_for_bucket(0x0000)).node_id == "node-a"
        with pytest.raises(NoAvailableNodeError):
            router.route(_ws_id_for_bucket(0x0001))

    def test_refresh_returns_true_on_change(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]

        assert router.refresh_cache() is True

    def test_refresh_returns_false_on_no_change(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]

        router.refresh_cache()
        assert router.refresh_cache() is False


# ---------------------------------------------------------------------------
# TestCheckVersion
# ---------------------------------------------------------------------------


class TestCheckVersion:
    """Version-gated refresh."""

    def test_version_change_triggers_refresh(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]
        storage.settings["rebalancer_version"] = {"value": "1"}

        assert router.check_version() is True
        assert router.is_ready()

    def test_same_version_skips(self) -> None:
        router, storage = _make_router()
        # Default version is 0; setting absent also means 0
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]

        # First call: version=0 matches self._version=0 -> no refresh
        assert router.check_version() is False
        assert not router.is_ready()  # cache was never loaded

    def test_version_none_treated_as_zero(self) -> None:
        router, storage = _make_router()
        # settings dict is empty -> get_system_setting returns None
        assert router.check_version() is False


# ---------------------------------------------------------------------------
# TestGenerateWsId
# ---------------------------------------------------------------------------


class TestGenerateWsId:
    """Workstream ID generation targeting a specific node."""

    def test_generates_routable_id(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        storage.buckets = [
            {"bucket": 0x00FF, "node_id": "node-a"},
            {"bucket": 0x0100, "node_id": "node-b"},
        ]
        router.refresh_cache()

        ws_id = router.generate_ws_id_for_node("node-a")
        assert len(ws_id) == 32
        assert router.route(ws_id).node_id == "node-a"

    def test_unknown_node_raises(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]
        router.refresh_cache()

        with pytest.raises(NoAvailableNodeError, match="node-z"):
            router.generate_ws_id_for_node("node-z")


# ---------------------------------------------------------------------------
# TestIsReady
# ---------------------------------------------------------------------------


class TestIsReady:
    """Readiness checks."""

    def test_false_when_empty(self) -> None:
        router, _ = _make_router()
        assert router.is_ready() is False

    def test_true_after_refresh(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        storage.buckets = [{"bucket": 0, "node_id": "node-a"}]
        router.refresh_cache()

        assert router.is_ready() is True


# ---------------------------------------------------------------------------
# TestNodeCount
# ---------------------------------------------------------------------------


class TestNodeCount:
    """Distinct node counting."""

    def test_count_distinct_nodes(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        # Spread all 65536 buckets across 3 nodes
        storage.buckets = [
            {"bucket": b, "node_id": f"node-{['a', 'b', 'c'][b % 3]}"} for b in range(RING_SIZE)
        ]
        router.refresh_cache()

        assert router.node_count() == 3
