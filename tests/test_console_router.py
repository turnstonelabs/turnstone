"""Tests for turnstone.console.router (rendezvous routing)."""

from __future__ import annotations

import secrets
import threading
from typing import Any

import pytest

from turnstone.console.router import ConsoleRouter, NodeRef
from turnstone.core.node_affinity import NodeAffinityError
from turnstone.core.rendezvous import NoAvailableNodeError


class FakeStorage:
    """Minimal storage mock for router tests."""

    def __init__(self) -> None:
        self.services: list[dict[str, str]] = []
        self.overrides: list[dict[str, str]] = []
        self.workstreams: dict[str, dict[str, str]] = {}

    def get_workstream(self, ws_id: str) -> dict[str, str] | None:
        return self.workstreams.get(ws_id)

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        return list(self.services)

    def list_workstream_overrides(self) -> list[dict[str, str]]:
        return list(self.overrides)


NODE_A = {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"}
NODE_B = {"service_id": "node-b", "url": "http://b:8080", "metadata": "{}"}
NODE_C = {"service_id": "node-c", "url": "http://c:8080", "metadata": "{}"}


def _make_router(storage: FakeStorage | None = None) -> tuple[ConsoleRouter, FakeStorage]:
    s = storage or FakeStorage()
    return ConsoleRouter(s), s  # type: ignore[arg-type]


def _random_ws_id() -> str:
    return secrets.token_hex(16)


class TestRouteBasic:
    def test_route_returns_a_live_node(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()

        ref = router.route(_random_ws_id())
        assert ref.node_id in {"node-a", "node-b", "node-c"}

    def test_route_is_deterministic_for_same_ws_id(self) -> None:
        """Same ws_id + same membership → same target every time."""
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()

        ws_id = _random_ws_id()
        first = router.route(ws_id)
        for _ in range(50):
            assert router.route(ws_id) == first

    def test_route_override_priority(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        ws_id = _random_ws_id()
        storage.overrides = [{"ws_id": ws_id, "node_id": "node-b"}]
        router.refresh_cache()

        # Override wins regardless of HRW score.
        assert router.route(ws_id) == NodeRef("node-b", "http://b:8080")

    def test_route_empty_membership_raises(self) -> None:
        router, _ = _make_router()
        with pytest.raises(NoAvailableNodeError):
            router.route(_random_ws_id())

    def test_route_empty_ws_id_raises(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.refresh_cache()
        with pytest.raises(NoAvailableNodeError, match="empty"):
            router.route("")

    def test_route_url_convenience(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.refresh_cache()
        assert router.route_url(_random_ws_id()) == "http://a:8080"


class TestMembershipConvergence:
    """Rendezvous gives the minimal-moves property; pin it."""

    def test_node_join_only_steals_some_keys(self) -> None:
        """Adding a 4th node moves ~1/4 of keys to it; the other 3
        nodes' kept keys are unchanged."""
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()

        sample = [_random_ws_id() for _ in range(2000)]
        before = {ws: router.route(ws).node_id for ws in sample}

        storage.services = [
            NODE_A,
            NODE_B,
            NODE_C,
            {"service_id": "node-d", "url": "http://d:8080", "metadata": "{}"},
        ]
        router.refresh_cache()
        after = {ws: router.route(ws).node_id for ws in sample}

        moved = sum(1 for ws in sample if before[ws] != after[ws])
        moved_to_new = sum(1 for ws in sample if after[ws] == "node-d")
        # Every move must be onto the new node — no churn between
        # existing nodes.
        assert moved == moved_to_new
        # Should be roughly 1/4 of keys; allow a wide band for variance.
        assert 0.15 < moved / len(sample) < 0.35

    def test_node_leave_only_redistributes_dead_node_keys(self) -> None:
        """Removing node-a sends node-a's keys to b/c only; keys that
        were on b/c stay put."""
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()

        sample = [_random_ws_id() for _ in range(2000)]
        before = {ws: router.route(ws).node_id for ws in sample}

        storage.services = [NODE_B, NODE_C]
        router.refresh_cache()
        after = {ws: router.route(ws).node_id for ws in sample}

        for ws in sample:
            if before[ws] in ("node-b", "node-c"):
                assert after[ws] == before[ws], (
                    f"key {ws} moved from {before[ws]} to {after[ws]} "
                    "even though its old owner is still live"
                )
            else:  # was on node-a
                assert after[ws] in ("node-b", "node-c")


class TestWeights:
    def test_weight_2_node_gets_more_keys_than_weight_1(self) -> None:
        router, storage = _make_router()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": '{"weight": 2}'},
            {"service_id": "node-b", "url": "http://b:8080", "metadata": '{"weight": 1}'},
        ]
        router.refresh_cache()

        sample = [_random_ws_id() for _ in range(5000)]
        on_a = sum(1 for ws in sample if router.route(ws).node_id == "node-a")
        # Heavier node should win clearly more than half; exact ratio
        # depends on the simple hash×weight formulation but a/b > 1.4
        # for weight 2:1 across 5k samples is reliable.
        assert on_a / len(sample) > 0.55

    def test_invalid_metadata_falls_back_to_weight_1(self) -> None:
        router, storage = _make_router()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "not json"},
        ]
        router.refresh_cache()
        # Just confirms it doesn't blow up.
        router.route(_random_ws_id())


class TestRefreshLifecycle:
    def test_refresh_cache_publishes_new_membership_immediately(self) -> None:
        """refresh_cache() reloads on the calling thread — the next
        route() sees the new membership without any further trigger."""
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.refresh_cache()
        assert router.node_count() == 1

        storage.services = [NODE_A, NODE_B]
        router.refresh_cache()
        assert router.node_count() == 2

    def test_concurrent_refresh_returns_false_on_lock_contention(self) -> None:
        """refresh_cache uses a non-blocking lock acquire — if another
        thread is already refreshing, the second caller bails so the
        in-flight refresh's result is the one that publishes."""

        router, storage = _make_router()
        storage.services = [NODE_A]

        with router._refresh_lock:
            # Lock held by this thread → the call below can't acquire.
            assert router.refresh_cache() is False

    def test_force_refresh_blocks_until_in_flight_refresh_releases(self) -> None:
        """force_refresh acquires the refresh lock blocking — used by the
        404-retry path to guarantee a fresh view even under contention."""
        import threading

        router, storage = _make_router()
        storage.services = [NODE_A]

        # Hold the refresh lock from another thread.
        lock_held = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with router._refresh_lock:
                lock_held.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        assert lock_held.wait(timeout=1)

        # force_refresh should block, not bail.
        result_box: list[bool] = []

        def call_force() -> None:
            result_box.append(router.force_refresh())

        caller = threading.Thread(target=call_force, daemon=True)
        caller.start()
        caller.join(timeout=0.2)
        assert caller.is_alive(), "force_refresh returned without acquiring lock"

        release.set()
        holder.join(timeout=1)
        caller.join(timeout=1)
        assert not caller.is_alive()
        # Membership changed from empty → 1 live node.
        assert result_box == [True]
        assert router.node_count() == 1

    def test_force_refresh_always_reloads(self) -> None:
        """force_refresh skips the non-blocking-lock bail and always
        publishes a fresh view — back-to-back calls each pick up the
        latest storage state."""
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.force_refresh()
        assert router.node_count() == 1

        storage.services = [NODE_A, NODE_B]
        router.force_refresh()
        assert router.node_count() == 2

    def test_force_refresh_shares_a_refresh_that_started_after_its_evidence(self) -> None:
        """A burst of 404s scans storage once, not once per 404."""
        router, storage = _make_router()
        calls = 0
        list_services = storage.list_services

        def counting_list_services(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return list_services(*args, **kwargs)

        storage.list_services = counting_list_services  # type: ignore[method-assign]
        seen = router.refresh_generation  # all three 404s arrive here
        router.force_refresh(since=seen)
        assert router.force_refresh(since=seen) is False
        assert router.force_refresh(since=seen) is False
        assert calls == 1
        # Evidence that arrives after that refresh started gets its own.
        router.force_refresh(since=router.refresh_generation)
        assert calls == 2

    def test_a_refresh_already_reading_when_the_evidence_arrives_is_not_shared(self) -> None:
        """It may have read storage before the change the 404 reports."""
        router, storage = _make_router()
        started, release = threading.Event(), threading.Event()
        calls = 0
        list_services = storage.list_services

        def slow_first(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                release.wait(2)
            return list_services(*args, **kwargs)

        storage.list_services = slow_first  # type: ignore[method-assign]
        in_flight = threading.Thread(target=router.refresh_cache)
        in_flight.start()
        assert started.wait(2)
        seen = router.refresh_generation  # the 404 arrives while that refresh reads
        release.set()
        in_flight.join(2)

        router.force_refresh(since=seen)

        assert calls == 2

    def test_a_failed_refresh_is_never_shared(self) -> None:
        router, storage = _make_router()
        seen = router.refresh_generation
        list_services = storage.list_services

        def down(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("down")

        storage.list_services = down  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            router.force_refresh(since=seen)
        storage.list_services = list_services  # type: ignore[method-assign]
        storage.services = [NODE_A]

        router.force_refresh(since=seen)

        assert router.node_count() == 1

    def test_remember_override_cannot_be_erased_by_stale_inflight_refresh(self) -> None:
        """A pre-commit refresh snapshot publishes before the create hint."""

        class _BlockingStorage(FakeStorage):
            def __init__(self) -> None:
                super().__init__()
                self.override_snapshot_taken = threading.Event()
                self.release_override_snapshot = threading.Event()

            def list_workstream_overrides(self) -> list[dict[str, str]]:
                snapshot = list(self.overrides)
                self.override_snapshot_taken.set()
                assert self.release_override_snapshot.wait(timeout=2)
                return snapshot

        storage = _BlockingStorage()
        storage.services = [NODE_A, NODE_B]
        router, _ = _make_router(storage)
        ws_id = "a" * 32
        owner = NodeRef("node-a", "http://a:8080")
        refresh_done = threading.Event()
        remember_done = threading.Event()

        def refresh() -> None:
            router.force_refresh()
            refresh_done.set()

        def remember() -> None:
            router.remember_override(ws_id, owner)
            remember_done.set()

        refresher = threading.Thread(target=refresh)
        publisher = threading.Thread(target=remember)
        refresher.start()
        assert storage.override_snapshot_taken.wait(timeout=1)
        publisher.start()
        assert not remember_done.wait(timeout=0.1), "create hint overtook stale refresh"
        storage.release_override_snapshot.set()
        refresher.join(timeout=2)
        publisher.join(timeout=2)

        assert refresh_done.is_set()
        assert remember_done.is_set()
        assert router.route(ws_id) == owner

    def test_version_is_monotonic_across_refreshes(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.refresh_cache()
        v1 = router.version
        router.refresh_cache()
        v2 = router.version
        assert v2 > v1
        router.force_refresh()
        assert router.version > v2


class TestIsReady:
    def test_false_when_empty(self) -> None:
        router, _ = _make_router()
        assert router.is_ready() is False

    def test_true_after_membership_loads(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A]
        router.refresh_cache()
        assert router.is_ready() is True


class TestNodeCount:
    def test_count_matches_live_services(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()
        assert router.node_count() == 3


class TestDurableRequirement:
    def test_requirement_precedes_overrides_and_survives_membership_loss(self):
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        storage.workstreams["pinned"] = {"required_node_id": "node-a"}
        storage.overrides = [{"ws_id": "pinned", "node_id": "node-b"}]
        router.refresh_cache()
        assert router.route("pinned").node_id == "node-a"
        for services in ([NODE_B], []):
            storage.services = services
            router.refresh_cache()
            with pytest.raises(NodeAffinityError) as error:
                router.route("pinned")
            assert error.value.status_code == 503
            assert error.value.required_node_id == "node-a"
        storage.services = [{**NODE_A, "url": "http://returned:8080"}, NODE_B]
        router.refresh_cache()
        assert router.route("pinned") == NodeRef("node-a", "http://returned:8080")

    def test_policy_is_read_after_creation_and_after_authorized_policy_change(self):
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        router.refresh_cache()
        router.route("new-id")  # A cached miss must not release a later requirement.
        storage.workstreams["new-id"] = {"required_node_id": "node-a"}
        assert router.route("new-id").node_id == "node-a"
        # Future fenced migration can change the row without invalidating a
        # separate, indefinitely cached affinity map.
        storage.workstreams["new-id"]["required_node_id"] = "node-b"
        assert router.route("new-id").node_id == "node-b"

    def test_private_requirement_does_not_disclose_missing_node(self):
        router, storage = _make_router()
        storage.services = [NODE_B]
        storage.workstreams["private"] = {"required_node_id": "secret-node"}
        router.refresh_cache()
        assert router.route("private", can_read=lambda row: False) == router.route("unknown")
        with pytest.raises(NodeAffinityError):
            router.route("private", can_read=lambda row: True)

    def test_storage_failure_never_means_flexible_placement(self, monkeypatch):
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        router.refresh_cache()

        def broken(ws_id):
            raise RuntimeError("storage unavailable")

        monkeypatch.setattr(storage, "get_workstream", broken)
        with pytest.raises(NoAvailableNodeError):
            router.route("pinned")


class TestOwnerLeaseRouting:
    """The node holding a workstream's owner lease is the one that can serve it."""

    def test_live_lease_holder_precedes_overrides_and_rendezvous(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        storage.overrides = [{"ws_id": "moved", "node_id": "node-a"}]
        storage.workstreams["moved"] = {"lease_node_id": "node-c"}
        router.refresh_cache()

        assert router.route("moved") == NodeRef("node-c", "http://c:8080")

    def test_requirement_still_precedes_the_lease_holder(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        storage.workstreams["pinned"] = {"required_node_id": "node-a", "lease_node_id": "node-b"}
        router.refresh_cache()

        assert router.route("pinned").node_id == "node-a"

    def test_holder_that_is_not_a_live_node_falls_through(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        ws_id = _random_ws_id()
        storage.overrides = [{"ws_id": ws_id, "node_id": "node-b"}]
        router.refresh_cache()
        # A crashed node, the console pseudo-node, or a CLI holder (no node).
        for holder in ("node-gone", "console", "", None):
            storage.workstreams[ws_id] = {"lease_node_id": holder}  # type: ignore[dict-item]
            assert router.route(ws_id).node_id == "node-b"

    def test_a_row_the_caller_already_read_is_not_read_again(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B, NODE_C]
        router.refresh_cache()
        storage.workstreams["read-once"] = {"lease_node_id": "node-a"}

        ref = router.route("read-once", known_row={"lease_node_id": "node-c"})

        assert ref.node_id == "node-c"

    def test_lease_holder_is_read_on_every_call(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        router.refresh_cache()
        storage.workstreams["roaming"] = {"lease_node_id": "node-a"}
        assert router.route("roaming").node_id == "node-a"
        storage.workstreams["roaming"]["lease_node_id"] = "node-b"
        assert router.route("roaming").node_id == "node-b"

    def test_invisible_rows_do_not_disclose_their_holder(self) -> None:
        router, storage = _make_router()
        storage.services = [NODE_A, NODE_B]
        router.refresh_cache()
        # Pick an id whose rendezvous placement differs from its holder.
        ws_id = next(
            candidate
            for candidate in (_random_ws_id() for _ in range(200))
            if router.rendezvous_node(candidate).node_id == "node-a"
        )
        storage.workstreams[ws_id] = {"lease_node_id": "node-b"}

        # An unreadable row gets the placement of an unknown id: plain HRW.
        assert router.route(ws_id, can_read=lambda row: False).node_id == "node-a"
        assert router.route(ws_id, can_read=lambda row: True).node_id == "node-b"
