"""Tests for workstream template storage CRUD operations."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from turnstone.core.storage._schema import workstreams
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture()
def db(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


def _make_template_kwargs(**overrides):
    defaults = {
        "ws_template_id": "tpl_001",
        "name": "research-agent",
        "description": "Deep research profile",
        "system_prompt": "You are a research assistant.",
        "prompt_template": "tpl-greeting",
        "model": "gpt-5",
        "auto_approve": False,
        "auto_approve_tools": "read_file,write_file",
        "temperature": 0.7,
        "reasoning_effort": "medium",
        "max_tokens": 4096,
        "token_budget": 100000,
        "agent_max_turns": 10,
        "notify_on_complete": '{"webhook":"https://example.com"}',
        "org_id": "org1",
        "created_by": "admin",
        "enabled": True,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# CRUD Operations
# ---------------------------------------------------------------------------


class TestWsTemplateCRUD:
    def test_create_ws_template(self, db):
        db.create_ws_template(**_make_template_kwargs())
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["ws_template_id"] == "tpl_001"
        assert tpl["name"] == "research-agent"

    def test_create_ws_template_fields(self, db):
        db.create_ws_template(**_make_template_kwargs())
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["description"] == "Deep research profile"
        assert tpl["system_prompt"] == "You are a research assistant."
        assert tpl["prompt_template"] == "tpl-greeting"
        assert tpl["model"] == "gpt-5"
        assert tpl["auto_approve"] is False
        assert isinstance(tpl["auto_approve"], bool)
        assert tpl["auto_approve_tools"] == "read_file,write_file"
        assert tpl["temperature"] == 0.7
        assert tpl["reasoning_effort"] == "medium"
        assert tpl["max_tokens"] == 4096
        assert tpl["token_budget"] == 100000
        assert tpl["agent_max_turns"] == 10
        assert tpl["notify_on_complete"] == '{"webhook":"https://example.com"}'
        assert tpl["org_id"] == "org1"
        assert tpl["created_by"] == "admin"
        assert tpl["enabled"] is True
        assert isinstance(tpl["enabled"], bool)
        assert tpl["version"] == 1
        assert "created" in tpl
        assert "updated" in tpl

    def test_get_ws_template_not_found(self, db):
        assert db.get_ws_template("nonexistent") is None

    def test_get_ws_template_by_name(self, db):
        db.create_ws_template(**_make_template_kwargs())
        tpl = db.get_ws_template_by_name("research-agent")
        assert tpl is not None
        assert tpl["ws_template_id"] == "tpl_001"
        assert tpl["auto_approve"] is False
        assert isinstance(tpl["auto_approve"], bool)
        assert tpl["enabled"] is True
        assert isinstance(tpl["enabled"], bool)

    def test_get_ws_template_by_name_not_found(self, db):
        assert db.get_ws_template_by_name("nope") is None

    def test_list_ws_templates(self, db):
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t2", name="beta"))
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t1", name="alpha"))
        templates = db.list_ws_templates()
        assert len(templates) == 2
        assert templates[0]["name"] == "alpha"
        assert templates[1]["name"] == "beta"

    def test_list_ws_templates_empty(self, db):
        assert db.list_ws_templates() == []

    def test_list_ws_templates_enabled_only(self, db):
        db.create_ws_template(
            **_make_template_kwargs(ws_template_id="t1", name="active", enabled=True)
        )
        db.create_ws_template(
            **_make_template_kwargs(ws_template_id="t2", name="disabled", enabled=False)
        )
        result = db.list_ws_templates(enabled_only=True)
        assert len(result) == 1
        assert result[0]["name"] == "active"

    def test_list_ws_templates_org_filter(self, db):
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t1", name="a", org_id="org1"))
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t2", name="b", org_id="org2"))
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t3", name="c", org_id="org1"))
        result = db.list_ws_templates(org_id="org1")
        assert len(result) == 2
        assert {r["ws_template_id"] for r in result} == {"t1", "t3"}

    def test_update_ws_template(self, db):
        db.create_ws_template(**_make_template_kwargs())
        ok = db.update_ws_template("tpl_001", name="updated-agent", description="New desc")
        assert ok is True
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["name"] == "updated-agent"
        assert tpl["description"] == "New desc"

    def test_update_ws_template_not_found(self, db):
        assert db.update_ws_template("missing", name="x") is False

    def test_update_ws_template_ignores_unknown_fields(self, db):
        db.create_ws_template(**_make_template_kwargs())
        ok = db.update_ws_template("tpl_001", name="new-name", org_id="hack", created_by="hack")
        assert ok is True
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["name"] == "new-name"
        # Non-mutable fields unchanged.
        assert tpl["org_id"] == "org1"
        assert tpl["created_by"] == "admin"

    def test_update_ws_template_boolean_normalization(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", auto_approve=True, enabled=False)
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["auto_approve"] is True
        assert isinstance(tpl["auto_approve"], bool)
        assert tpl["enabled"] is False
        assert isinstance(tpl["enabled"], bool)

    def test_delete_ws_template(self, db):
        db.create_ws_template(**_make_template_kwargs())
        ok = db.delete_ws_template("tpl_001")
        assert ok is True
        assert db.get_ws_template("tpl_001") is None

    def test_delete_ws_template_not_found(self, db):
        assert db.delete_ws_template("missing") is False

    def test_delete_ws_template_cascades_versions(self, db):
        db.create_ws_template(**_make_template_kwargs())
        # Create a version snapshot via update.
        db.update_ws_template("tpl_001", name="v2-name")
        versions = db.list_ws_template_versions("tpl_001")
        assert len(versions) == 1
        # Delete template — versions should be gone too.
        db.delete_ws_template("tpl_001")
        assert db.list_ws_template_versions("tpl_001") == []

    def test_create_ws_template_with_hash(self, db):
        db.create_ws_template(
            ws_template_id="tpl_hash",
            name="hashed-template",
            prompt_template="my-prompt",
            prompt_template_hash="abc123hash",
        )
        tpl = db.get_ws_template("tpl_hash")
        assert tpl["prompt_template_hash"] == "abc123hash"

    def test_update_ws_template_hash(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", prompt_template_hash="newhash456")
        tpl = db.get_ws_template("tpl_001")
        assert tpl["prompt_template_hash"] == "newhash456"

    def test_create_duplicate_name(self, db):
        db.create_ws_template(**_make_template_kwargs(ws_template_id="t1", name="unique"))
        with pytest.raises(IntegrityError):
            db.create_ws_template(**_make_template_kwargs(ws_template_id="t2", name="unique"))


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


class TestWsTemplateVersioning:
    def test_update_creates_version_snapshot(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", description="Changed")
        versions = db.list_ws_template_versions("tpl_001")
        assert len(versions) == 1
        assert versions[0]["ws_template_id"] == "tpl_001"
        assert versions[0]["version"] == 1

    def test_version_increments_on_update(self, db):
        db.create_ws_template(**_make_template_kwargs())
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["version"] == 1
        db.update_ws_template("tpl_001", description="v2")
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["version"] == 2
        db.update_ws_template("tpl_001", description="v3")
        tpl = db.get_ws_template("tpl_001")
        assert tpl is not None
        assert tpl["version"] == 3

    def test_version_snapshot_contains_json(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", description="Changed")
        versions = db.list_ws_template_versions("tpl_001")
        snapshot = json.loads(versions[0]["snapshot"])
        # Snapshot should contain the pre-update state.
        assert snapshot["description"] == "Deep research profile"
        assert snapshot["name"] == "research-agent"
        assert snapshot["version"] == 1

    def test_multiple_updates_create_versions(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", description="Second")
        db.update_ws_template("tpl_001", description="Third")
        db.update_ws_template("tpl_001", description="Fourth")
        versions = db.list_ws_template_versions("tpl_001")
        assert len(versions) == 3
        # Ordered by version DESC.
        assert versions[0]["version"] == 3
        assert versions[1]["version"] == 2
        assert versions[2]["version"] == 1

    def test_list_ws_template_versions(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.update_ws_template("tpl_001", description="v2")
        db.update_ws_template("tpl_001", description="v3")
        versions = db.list_ws_template_versions("tpl_001")
        assert len(versions) == 2
        # Ordered by version DESC.
        assert versions[0]["version"] == 2
        assert versions[1]["version"] == 1
        for v in versions:
            assert "created" in v
            assert "snapshot" in v
            assert "changed_by" in v

    def test_list_ws_template_versions_empty(self, db):
        assert db.list_ws_template_versions("nonexistent") == []

    def test_create_ws_template_version_direct(self, db):
        db.create_ws_template(**_make_template_kwargs())
        snapshot_data = json.dumps({"name": "manual-snapshot", "version": 99})
        db.create_ws_template_version(
            "tpl_001", version=99, snapshot=snapshot_data, changed_by="admin"
        )
        versions = db.list_ws_template_versions("tpl_001")
        assert len(versions) == 1
        assert versions[0]["version"] == 99
        assert versions[0]["changed_by"] == "admin"
        parsed = json.loads(versions[0]["snapshot"])
        assert parsed["name"] == "manual-snapshot"


# ---------------------------------------------------------------------------
# Workstream Integration
# ---------------------------------------------------------------------------


class TestWsTemplateWorkstreamIntegration:
    def test_register_workstream_with_template(self, db):
        db.create_ws_template(**_make_template_kwargs())
        db.register_workstream(
            ws_id="ws-001",
            node_id="node-1",
            name="test-ws",
            ws_template_id="tpl_001",
            ws_template_version=1,
        )
        # Verify via direct query — list_workstreams doesn't select template fields.
        with db._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.ws_template_id, workstreams.c.ws_template_version).where(
                    workstreams.c.ws_id == "ws-001"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "tpl_001"
        assert row[1] == 1

    def test_update_workstream_template(self, db):
        db.register_workstream(ws_id="ws-002", node_id="node-1", name="test-ws")
        # Initially defaults
        with db._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.ws_template_id, workstreams.c.ws_template_version).where(
                    workstreams.c.ws_id == "ws-002"
                )
            ).fetchone()
        assert row[0] == ""
        assert row[1] == 0
        # Update template lineage
        db.update_workstream_template("ws-002", "tpl_abc", 3)
        with db._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.ws_template_id, workstreams.c.ws_template_version).where(
                    workstreams.c.ws_id == "ws-002"
                )
            ).fetchone()
        assert row[0] == "tpl_abc"
        assert row[1] == 3
