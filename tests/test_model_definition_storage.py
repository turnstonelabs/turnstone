"""Tests for model definition storage CRUD operations."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.storage._sqlite import SQLiteBackend


def _make_id() -> str:
    return uuid.uuid4().hex


class TestModelDefinitionStorage:
    def test_create_and_get(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(
            definition_id=did,
            alias="test-model",
            model="gpt-5",
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            context_window=128000,
        )
        m = db.get_model_definition(did)
        assert m is not None
        assert m["alias"] == "test-model"
        assert m["model"] == "gpt-5"
        assert m["provider"] == "openai"
        assert m["base_url"] == "https://api.openai.com/v1"
        assert m["api_key"] == "sk-test"
        assert m["context_window"] == 128000
        assert m["capabilities"] == "{}"
        assert m["enabled"] is True

    def test_get_by_alias(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(definition_id=did, alias="by-alias", model="gpt-5")
        m = db.get_model_definition_by_alias("by-alias")
        assert m is not None
        assert m["definition_id"] == did

    def test_get_by_alias_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_model_definition_by_alias("nope") is None

    def test_get_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_model_definition("nonexistent") is None

    def test_list_empty(self, db: SQLiteBackend) -> None:
        assert db.list_model_definitions() == []

    def test_list_all(self, db: SQLiteBackend) -> None:
        db.create_model_definition(definition_id=_make_id(), alias="alpha", model="gpt-5")
        db.create_model_definition(
            definition_id=_make_id(), alias="beta", model="claude-opus-4-6", provider="anthropic"
        )
        models = db.list_model_definitions()
        assert len(models) == 2
        assert models[0]["alias"] == "alpha"  # ordered by alias
        assert models[1]["alias"] == "beta"

    def test_list_enabled_only(self, db: SQLiteBackend) -> None:
        db.create_model_definition(
            definition_id=_make_id(), alias="enabled-model", model="gpt-5", enabled=True
        )
        db.create_model_definition(
            definition_id=_make_id(), alias="disabled-model", model="gpt-5", enabled=False
        )
        enabled = db.list_model_definitions(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["alias"] == "enabled-model"

    def test_update_basic_fields(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(
            definition_id=did, alias="orig", model="gpt-5", base_url="http://old"
        )
        ok = db.update_model_definition(did, alias="renamed", base_url="http://new")
        assert ok is True
        m = db.get_model_definition(did)
        assert m is not None
        assert m["alias"] == "renamed"
        assert m["base_url"] == "http://new"

    def test_update_boolean_conversion(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(definition_id=did, alias="booltest", model="gpt-5")
        db.update_model_definition(did, enabled=False)
        m = db.get_model_definition(did)
        assert m is not None
        assert m["enabled"] is False

    def test_update_not_found(self, db: SQLiteBackend) -> None:
        ok = db.update_model_definition("nonexistent", alias="x")
        assert ok is False

    def test_update_ignores_disallowed_fields(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(
            definition_id=did, alias="guard", model="gpt-5", created_by="admin"
        )
        original = db.get_model_definition(did)
        assert original is not None
        original_created = original["created"]
        # created_by and created are not in the mutable allowlist
        db.update_model_definition(did, created_by="evil", created="2000-01-01T00:00:00")
        m = db.get_model_definition(did)
        assert m is not None
        assert m["created_by"] == "admin"  # unchanged
        assert m["created"] == original_created  # unchanged

    def test_delete(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(definition_id=did, alias="delme", model="gpt-5")
        ok = db.delete_model_definition(did)
        assert ok is True
        assert db.get_model_definition(did) is None

    def test_delete_not_found(self, db: SQLiteBackend) -> None:
        ok = db.delete_model_definition("nonexistent")
        assert ok is False

    def test_create_duplicate_alias(self, db: SQLiteBackend) -> None:
        db.create_model_definition(definition_id=_make_id(), alias="unique", model="gpt-5")
        # Second create with same alias but different ID should be no-op (OR IGNORE)
        did2 = _make_id()
        db.create_model_definition(definition_id=did2, alias="unique", model="gpt-5")
        assert db.get_model_definition(did2) is None

    def test_create_idempotent_same_id(self, db: SQLiteBackend) -> None:
        did = _make_id()
        db.create_model_definition(definition_id=did, alias="idem", model="gpt-5")
        db.create_model_definition(definition_id=did, alias="idem", model="gpt-5-mini")
        m = db.get_model_definition(did)
        assert m is not None
        assert m["model"] == "gpt-5"  # original preserved

    def test_capabilities_json(self, db: SQLiteBackend) -> None:
        did = _make_id()
        caps = '{"supports_vision": true, "supports_web_search": false}'
        db.create_model_definition(
            definition_id=did, alias="caps-test", model="gpt-5", capabilities=caps
        )
        m = db.get_model_definition(did)
        assert m is not None
        assert m["capabilities"] == caps

    def test_defaults(self, db: SQLiteBackend) -> None:
        """Verify default values for optional fields."""
        did = _make_id()
        db.create_model_definition(definition_id=did, alias="defaults", model="gpt-5")
        m = db.get_model_definition(did)
        assert m is not None
        assert m["provider"] == "openai"
        assert m["base_url"] == ""
        assert m["api_key"] == ""
        assert m["context_window"] == 32768
        assert m["capabilities"] == "{}"
        assert m["enabled"] is True
        assert m["created_by"] == ""
