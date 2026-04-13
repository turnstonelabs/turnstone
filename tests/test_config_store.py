"""Tests for ConfigStore database-backed configuration."""

from __future__ import annotations

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
