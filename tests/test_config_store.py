"""Tests for ConfigStore database-backed configuration."""

from __future__ import annotations

import threading

import pytest

from turnstone.core.config_store import ConfigStore
from turnstone.core.settings_registry import SETTINGS
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def store(storage):
    return ConfigStore(storage)


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestGet:
    def test_returns_registry_default_when_nothing_stored(self, store):
        defn = SETTINGS["tools.timeout"]
        assert store.get("tools.timeout") == defn.default

    def test_returns_stored_value_after_set(self, store):
        store.set("tools.timeout", 60)
        assert store.get("tools.timeout") == 60

    def test_explicit_default_for_unknown_key(self, store):
        # Unknown keys fall back to explicit default
        assert store.get("nonexistent.key", 42) == 42

    def test_none_for_unknown_key_without_default(self, store):
        assert store.get("nonexistent.key") is None


# ---------------------------------------------------------------------------
# set() — validation
# ---------------------------------------------------------------------------


class TestSet:
    def test_rejects_unknown_key(self, store):
        with pytest.raises(ValueError, match="Unknown setting"):
            store.set("bogus.key", "value")

    def test_rejects_out_of_range(self, store):
        with pytest.raises(ValueError, match="minimum"):
            store.set("tools.timeout", 0)

    def test_rejects_above_max(self, store):
        with pytest.raises(ValueError, match="maximum"):
            store.set("tools.timeout", 9999)

    def test_stored_value_above_a_tightened_maximum_is_clamped_on_load(self, storage):
        """A bound tightened after the value was written clamps it with a
        warning; it never silently reverts to the default."""
        from turnstone.core.settings_registry import TOOL_TRUNCATION_MAX_CHARS

        storage.upsert_system_setting("tools.truncation", str(TOOL_TRUNCATION_MAX_CHARS + 1))

        store = ConfigStore(storage)

        assert store.get("tools.truncation") == TOOL_TRUNCATION_MAX_CHARS


# ---------------------------------------------------------------------------
# set() + get() round-trips
# ---------------------------------------------------------------------------


class TestSetGetRoundTrip:
    def test_int(self, store):
        store.set("tools.timeout", 30)
        assert store.get("tools.timeout") == 30
        assert isinstance(store.get("tools.timeout"), int)

    def test_float(self, store):
        store.set("model.temperature", 0.42)
        assert store.get("model.temperature") == 0.42
        assert isinstance(store.get("model.temperature"), float)

    def test_bool(self, store):
        store.set("tools.skip_permissions", True)
        assert store.get("tools.skip_permissions") is True
        store.set("tools.skip_permissions", False)
        assert store.get("tools.skip_permissions") is False

    def test_str(self, store):
        store.set("model.default_alias", "gpt5-prod")
        assert store.get("model.default_alias") == "gpt5-prod"

    def test_task_alias(self, store):
        store.set("model.task_alias", "fast")
        assert store.get("model.task_alias") == "fast"

    def test_task_effort(self, store):
        store.set("model.task_effort", "low")
        assert store.get("model.task_effort") == "low"


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    def test_reverts_to_default(self, store):
        store.set("tools.timeout", 30)
        assert store.get("tools.timeout") == 30
        store.delete("tools.timeout")
        defn = SETTINGS["tools.timeout"]
        assert store.get("tools.timeout") == defn.default

    def test_returns_false_for_non_existent(self, store):
        result = store.delete("tools.timeout")
        assert result is False

    def test_rejects_unknown_key(self, store):
        with pytest.raises(ValueError, match="Unknown setting"):
            store.delete("nonexistent.key")


# ---------------------------------------------------------------------------
# reload()
# ---------------------------------------------------------------------------


class TestReload:
    def test_picks_up_external_storage_changes(self, storage, store):
        # Write directly to storage, bypassing ConfigStore
        from turnstone.core.settings_registry import serialize_value

        storage.upsert_system_setting(
            key="tools.timeout",
            value=serialize_value(99),
            node_id="",
            is_secret=False,
            changed_by="external",
        )
        # Not visible yet (cached)
        defn = SETTINGS["tools.timeout"]
        assert store.get("tools.timeout") == defn.default
        # Reload and verify
        store.reload()
        assert store.get("tools.timeout") == 99

    def test_reload_cannot_overwrite_a_concurrent_set(self, storage, store, monkeypatch):
        store.set("tools.timeout", 30)
        reload_captured = threading.Event()
        release_reload = threading.Event()
        setter_waiting = threading.Event()
        setter_done = threading.Event()
        errors: list[BaseException] = []

        real_bulk = storage.get_system_settings_bulk

        def blocked_bulk(*, node_id=""):
            raw = real_bulk(node_id=node_id)
            if threading.current_thread().name == "stale-reload":
                reload_captured.set()
                if not release_reload.wait(2):
                    raise TimeoutError("reload/set test did not release the stale read")
            return raw

        monkeypatch.setattr(storage, "get_system_settings_bulk", blocked_bulk)
        real_mutation_lock = store._mutation_lock

        class _ObservedMutationLock:
            def __enter__(self):
                if threading.current_thread().name == "new-set":
                    setter_waiting.set()
                real_mutation_lock.acquire()
                return self

            def __exit__(self, *_exc_info):
                real_mutation_lock.release()

        store._mutation_lock = _ObservedMutationLock()

        def reload_worker() -> None:
            try:
                store.reload()
            except BaseException as exc:
                errors.append(exc)

        def set_worker() -> None:
            try:
                store.set("tools.timeout", 60)
            except BaseException as exc:
                errors.append(exc)
            finally:
                setter_done.set()

        reload_thread = threading.Thread(target=reload_worker, name="stale-reload")
        setter_thread = threading.Thread(target=set_worker, name="new-set")
        reload_thread.start()
        assert reload_captured.wait(2)
        setter_thread.start()
        assert setter_waiting.wait(2)
        assert not setter_done.is_set()
        release_reload.set()
        reload_thread.join(timeout=2)
        setter_thread.join(timeout=2)

        assert not reload_thread.is_alive()
        assert not setter_thread.is_alive()
        assert errors == []
        assert store.get("tools.timeout") == 60
        assert ConfigStore(storage).get("tools.timeout") == 60

    @pytest.mark.parametrize("later_operation", ["set", "delete"])
    def test_mutations_publish_in_storage_order(
        self,
        storage,
        store,
        monkeypatch,
        later_operation,
    ):
        first_committed = threading.Event()
        release_first = threading.Event()
        later_waiting = threading.Event()
        later_done = threading.Event()
        errors: list[BaseException] = []

        real_upsert = storage.upsert_system_setting

        def blocked_upsert(**kwargs):
            real_upsert(**kwargs)
            if threading.current_thread().name == "first-set":
                first_committed.set()
                if not release_first.wait(2):
                    raise TimeoutError("mutation-order test did not release the first write")

        monkeypatch.setattr(storage, "upsert_system_setting", blocked_upsert)
        real_mutation_lock = store._mutation_lock

        class _ObservedMutationLock:
            def __enter__(self):
                if threading.current_thread().name == "later-mutation":
                    later_waiting.set()
                real_mutation_lock.acquire()
                return self

            def __exit__(self, *_exc_info):
                real_mutation_lock.release()

        store._mutation_lock = _ObservedMutationLock()

        def first_worker() -> None:
            try:
                store.set("tools.timeout", 30)
            except BaseException as exc:
                errors.append(exc)

        def later_worker() -> None:
            try:
                if later_operation == "set":
                    store.set("tools.timeout", 60)
                else:
                    store.delete("tools.timeout")
            except BaseException as exc:
                errors.append(exc)
            finally:
                later_done.set()

        first_thread = threading.Thread(target=first_worker, name="first-set")
        later_thread = threading.Thread(target=later_worker, name="later-mutation")
        first_thread.start()
        assert first_committed.wait(2)
        later_thread.start()
        assert later_waiting.wait(2)
        assert not later_done.is_set()
        release_first.set()
        first_thread.join(timeout=2)
        later_thread.join(timeout=2)

        assert not first_thread.is_alive()
        assert not later_thread.is_alive()
        assert errors == []
        expected = 60 if later_operation == "set" else SETTINGS["tools.timeout"].default
        assert store.get("tools.timeout") == expected
        assert ConfigStore(storage).get("tools.timeout") == expected


# ---------------------------------------------------------------------------
# all_effective()
# ---------------------------------------------------------------------------


class TestAllEffective:
    def test_merges_stored_with_defaults(self, store):
        store.set("tools.timeout", 30)
        effective = store.all_effective()
        # Stored value
        assert effective["tools.timeout"] == 30
        # Default for unstored
        assert effective["memory.relevance_k"] == SETTINGS["memory.relevance_k"].default
        # All registry keys present
        assert set(effective.keys()) == set(SETTINGS.keys())

    def test_effective_snapshot_closes_cache_swap_version_window(self, store):
        store.set("judge.smart_approvals", False)
        store.set("judge.confidence_threshold", 0.95)
        old_version = store.version
        old_first_key = store.get("judge.smart_approvals")
        new_cache = {
            **store._cache,
            "judge.smart_approvals": True,
            "judge.confidence_threshold": 0.4,
        }
        swapped = threading.Event()
        release = threading.Event()
        snapshot_waiting = threading.Event()
        errors: list[BaseException] = []
        real_lock = store._lock

        class _ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "snapshot-reader":
                    snapshot_waiting.set()
                real_lock.acquire()
                return self

            def __exit__(self, *_exc_info):
                real_lock.release()

        store._lock = _ObservedLock()

        def writer() -> None:
            try:
                with store._lock:
                    store._cache = new_cache
                    swapped.set()
                    if not release.wait(2):
                        raise TimeoutError("snapshot test did not release writer")
                    store._version += 1
            except BaseException as exc:
                errors.append(exc)

        writer_thread = threading.Thread(target=writer, name="snapshot-writer")
        writer_thread.start()
        assert swapped.wait(2)

        # This is the exact impossible pair the old per-key/version bracket
        # accepted while a writer paused between its two assignments.
        assert store._version == old_version
        new_second_key = store.get("judge.confidence_threshold")
        assert (old_first_key, new_second_key) == (False, 0.4)

        result: list[tuple[int, dict[str, object]]] = []

        def reader() -> None:
            try:
                result.append(store.effective_snapshot())
            except BaseException as exc:
                errors.append(exc)

        reader_thread = threading.Thread(target=reader, name="snapshot-reader")
        reader_thread.start()
        assert snapshot_waiting.wait(2)
        assert result == []
        release.set()
        writer_thread.join(timeout=2)
        reader_thread.join(timeout=2)

        assert not writer_thread.is_alive()
        assert not reader_thread.is_alive()
        assert errors == []
        version, values = result[0]
        assert version == old_version + 1
        assert values["judge.smart_approvals"] is True
        assert values["judge.confidence_threshold"] == 0.4


# ---------------------------------------------------------------------------
# stored_keys()
# ---------------------------------------------------------------------------


class TestStoredKeys:
    def test_returns_correct_set(self, store):
        assert store.stored_keys() == frozenset()
        store.set("tools.timeout", 30)
        assert store.stored_keys() == frozenset({"tools.timeout"})
        store.set("model.default_alias", "gpt5-prod")
        assert store.stored_keys() == frozenset({"tools.timeout", "model.default_alias"})
        store.delete("tools.timeout")
        assert store.stored_keys() == frozenset({"model.default_alias"})


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_waits_for_in_progress_cache_publication(self, store):
        old_version = store.version
        new_cache = {**store._cache, "tools.timeout": 31}
        cache_swapped = threading.Event()
        release_writer = threading.Event()
        reader_observed = threading.Event()
        reader_lock_attempted = threading.Event()
        errors: list[BaseException] = []
        result: list[int] = []
        real_lock = store._lock

        class _ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "version-reader":
                    reader_lock_attempted.set()
                    reader_observed.set()
                real_lock.acquire()
                return self

            def __exit__(self, *_exc_info):
                real_lock.release()

        store._lock = _ObservedLock()

        def writer() -> None:
            try:
                with store._lock:
                    store._cache = new_cache
                    cache_swapped.set()
                    if not release_writer.wait(2):
                        raise TimeoutError("version test did not release the writer")
                    store._version += 1
            except BaseException as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                result.append(store.version)
            except BaseException as exc:
                errors.append(exc)
            finally:
                reader_observed.set()

        writer_thread = threading.Thread(target=writer, name="version-writer")
        reader_thread = threading.Thread(target=reader, name="version-reader")
        writer_thread.start()
        cache_swapped_seen = cache_swapped.wait(2)
        reader_thread.start()
        reader_reached_accessor = reader_observed.wait(2)
        result_before_release = list(result)
        release_writer.set()
        writer_thread.join(timeout=2)
        reader_thread.join(timeout=2)

        assert cache_swapped_seen
        assert reader_reached_accessor
        assert not writer_thread.is_alive()
        assert not reader_thread.is_alive()
        assert errors == []
        assert reader_lock_attempted.is_set()
        assert result_before_release == []
        assert result == [old_version + 1]
        assert store.get("tools.timeout") == 31

    def test_increments_on_set(self, store):
        v0 = store.version
        store.set("tools.timeout", 30)
        assert store.version == v0 + 1

    def test_increments_on_delete(self, store):
        store.set("tools.timeout", 30)
        v0 = store.version
        store.delete("tools.timeout")
        assert store.version == v0 + 1

    def test_increments_on_reload(self, store):
        v0 = store.version
        store.reload()
        assert store.version == v0 + 1
