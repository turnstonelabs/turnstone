"""Unit tests for :class:`turnstone.core.children_registry.ChildrenRegistry`.

The registry was lifted from ``CoordinatorAdapter`` in Stage 3 Step 1.
Adapter-level coverage for the integrated behavior already lives in
``test_coordinator_adapter.py``; this file pins the data structure
invariants in isolation so the registry can be reused by future
``ChildSource`` strategies (Step 2) without re-deriving the behavior
from the adapter test surface.
"""

from __future__ import annotations

import threading

import pytest

from turnstone.core.children_registry import ChildrenRegistry


class _Sentinel:
    """Lightweight UI stand-in; identity-comparable, no behavior."""


@pytest.fixture
def registry() -> ChildrenRegistry:
    return ChildrenRegistry()


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


class TestInstallUninstall:
    def test_install_seeds_empty_child_set_and_presence(self, registry: ChildrenRegistry) -> None:
        ui = _Sentinel()
        registry.install("p1", ui)
        assert registry.children_of("p1") == []
        assert registry.ui_for("p1") is ui
        assert registry.parents() == ["p1"]

    def test_install_is_idempotent_repoints_ui_keeps_children(
        self, registry: ChildrenRegistry
    ) -> None:
        ui_a = _Sentinel()
        ui_b = _Sentinel()
        registry.install("p1", ui_a)
        registry.merge_children("p1", ["c1", "c2"])
        registry.install("p1", ui_b)
        assert registry.ui_for("p1") is ui_b
        assert set(registry.children_of("p1")) == {"c1", "c2"}

    def test_uninstall_clears_forward_reverse_and_presence(
        self, registry: ChildrenRegistry
    ) -> None:
        ui = _Sentinel()
        registry.install("p1", ui)
        registry.merge_children("p1", ["c1", "c2"])
        registry.uninstall("p1")
        assert registry.children_of("p1") == []
        assert registry.ui_for("p1") is None
        assert registry.parents() == []
        assert registry.parent_for("c1") is None
        assert registry.parent_for("c2") is None

    def test_uninstall_unknown_parent_is_noop(self, registry: ChildrenRegistry) -> None:
        registry.uninstall("never-installed")  # must not raise

    def test_uninstall_does_not_clobber_other_parents(self, registry: ChildrenRegistry) -> None:
        registry.install("p1", _Sentinel())
        registry.install("p2", _Sentinel())
        registry.merge_children("p1", ["c1"])
        registry.merge_children("p2", ["c2"])
        registry.uninstall("p1")
        assert registry.parent_for("c1") is None
        assert registry.parent_for("c2") == "p2"
        assert registry.parents() == ["p2"]


# ---------------------------------------------------------------------------
# add_child — atomic check-and-route
# ---------------------------------------------------------------------------


class TestAddChild:
    def test_add_child_returns_ui_on_success(self, registry: ChildrenRegistry) -> None:
        ui = _Sentinel()
        registry.install("p1", ui)
        assert registry.add_child("p1", "c1") is ui
        assert registry.parent_for("c1") == "p1"
        assert registry.children_of("p1") == ["c1"]

    def test_add_child_returns_none_when_parent_not_installed(
        self, registry: ChildrenRegistry
    ) -> None:
        assert registry.add_child("absent", "c1") is None
        assert registry.parent_for("c1") is None

    def test_add_child_returns_none_on_duplicate(self, registry: ChildrenRegistry) -> None:
        ui = _Sentinel()
        registry.install("p1", ui)
        assert registry.add_child("p1", "c1") is ui
        # second add for same child returns None — caller must not
        # double-dispatch.
        assert registry.add_child("p1", "c1") is None
        assert registry.children_of("p1") == ["c1"]


# ---------------------------------------------------------------------------
# merge_children — bulk seeding
# ---------------------------------------------------------------------------


class TestMergeChildren:
    def test_merge_seeds_forward_and_reverse(self, registry: ChildrenRegistry) -> None:
        registry.merge_children("p1", ["c1", "c2", "c3"])
        assert set(registry.children_of("p1")) == {"c1", "c2", "c3"}
        for cid in ("c1", "c2", "c3"):
            assert registry.parent_for(cid) == "p1"

    def test_merge_is_idempotent(self, registry: ChildrenRegistry) -> None:
        registry.merge_children("p1", ["c1"])
        registry.merge_children("p1", ["c1"])
        assert registry.children_of("p1") == ["c1"]

    def test_merge_skips_empty_or_falsy_ids(self, registry: ChildrenRegistry) -> None:
        registry.merge_children("p1", ["", "c1", "", "c2"])
        assert set(registry.children_of("p1")) == {"c1", "c2"}

    def test_merge_does_not_require_install(self, registry: ChildrenRegistry) -> None:
        # Snapshot-priming may run before the parent's install fires —
        # the merge still seeds the forward set so the install picks
        # the children up. (Storage-seeded rebuild relies on this.)
        registry.merge_children("p1", ["c1"])
        assert registry.children_of("p1") == ["c1"]
        # ui_for is still None because install hasn't run
        assert registry.ui_for("p1") is None


# ---------------------------------------------------------------------------
# Lookups — return copies, not live refs
# ---------------------------------------------------------------------------


class TestLookups:
    def test_children_of_returns_copy(self, registry: ChildrenRegistry) -> None:
        registry.install("p1", _Sentinel())
        registry.merge_children("p1", ["c1", "c2"])
        snap = registry.children_of("p1")
        snap.append("c3-injected")
        assert "c3-injected" not in registry.children_of("p1")

    def test_children_of_unknown_parent_returns_empty(self, registry: ChildrenRegistry) -> None:
        assert registry.children_of("absent") == []

    def test_parent_for_unknown_child_returns_none(self, registry: ChildrenRegistry) -> None:
        assert registry.parent_for("absent") is None

    def test_parents_returns_copy(self, registry: ChildrenRegistry) -> None:
        registry.install("p1", _Sentinel())
        snap = registry.parents()
        snap.append("p2-injected")
        assert "p2-injected" not in registry.parents()


# ---------------------------------------------------------------------------
# Concurrency — concurrent add_child must not exceed the unique-set
# invariant or leave a half-installed reverse-index entry.
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_add_child_returns_ui_exactly_once_per_unique(
        self, registry: ChildrenRegistry
    ) -> None:
        ui = _Sentinel()
        registry.install("p1", ui)
        results: list[object] = []
        results_lock = threading.Lock()

        def attempt_add(child_id: str) -> None:
            r = registry.add_child("p1", child_id)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=attempt_add, args=("c1",)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread sees the UI; the remaining 19 see None
        # (duplicate). The forward + reverse indexes carry exactly one
        # entry for c1.
        successes = [r for r in results if r is ui]
        nones = [r for r in results if r is None]
        assert len(successes) == 1
        assert len(nones) == 19
        assert registry.children_of("p1") == ["c1"]
        assert registry.parent_for("c1") == "p1"

    def test_concurrent_install_and_add_child_no_resurrect(
        self, registry: ChildrenRegistry
    ) -> None:
        # add_child racing with uninstall: either lands first (registry
        # populated) or the parent is gone (returns None). Must NOT
        # leave a forward-set entry without presence — that would be
        # the "resurrected after close" leak the locked dispatch path
        # was guarding against.
        ui = _Sentinel()
        registry.install("p1", ui)

        outcomes: list[object] = []

        def adder() -> None:
            outcomes.append(registry.add_child("p1", "c1"))

        def uninstaller() -> None:
            registry.uninstall("p1")

        threads = [
            threading.Thread(target=adder),
            threading.Thread(target=uninstaller),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # If add_child landed first: c1 is in the forward set, then
        # uninstall clears everything. End state: nothing.
        # If uninstall landed first: add_child sees no presence,
        # returns None, no entry added. End state: nothing.
        # Either way, the leak invariant holds: child set is empty or
        # parent is gone, never "child set populated but no presence".
        children = registry.children_of("p1")
        ui_present = registry.ui_for("p1") is not None
        if children:
            assert ui_present, "registry leaked: children set without presence"
