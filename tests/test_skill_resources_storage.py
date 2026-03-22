"""Tests for skill resource storage operations."""

from __future__ import annotations

import uuid


class TestDeleteSkillResourceByPath:
    def test_delete_existing(self, storage):
        skill_id = uuid.uuid4().hex
        rid = uuid.uuid4().hex
        storage.create_skill_resource(rid, skill_id, "scripts/a.sh", "#!/bin/bash")
        assert storage.delete_skill_resource_by_path(skill_id, "scripts/a.sh") is True
        assert storage.get_skill_resource(skill_id, "scripts/a.sh") is None

    def test_delete_not_found(self, storage):
        assert storage.delete_skill_resource_by_path("nonexistent", "scripts/a.sh") is False

    def test_delete_wrong_path(self, storage):
        skill_id = uuid.uuid4().hex
        rid = uuid.uuid4().hex
        storage.create_skill_resource(rid, skill_id, "scripts/a.sh", "content")
        assert storage.delete_skill_resource_by_path(skill_id, "scripts/b.sh") is False
        # Original still exists
        assert storage.get_skill_resource(skill_id, "scripts/a.sh") is not None

    def test_delete_only_target(self, storage):
        """Deleting one resource doesn't affect others for the same skill."""
        skill_id = uuid.uuid4().hex
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "a")
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/b.sh", "b")
        assert storage.delete_skill_resource_by_path(skill_id, "scripts/a.sh") is True
        assert storage.get_skill_resource(skill_id, "scripts/b.sh") is not None
        assert len(storage.list_skill_resources(skill_id)) == 1


class TestListSkillResources:
    def test_ordering(self, storage):
        skill_id = uuid.uuid4().hex
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/z.sh", "z")
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "assets/a.txt", "a")
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "references/m.md", "m")
        rows = storage.list_skill_resources(skill_id)
        paths = [r["path"] for r in rows]
        assert paths == sorted(paths)

    def test_empty(self, storage):
        assert storage.list_skill_resources("nonexistent") == []

    def test_size_from_content(self, storage):
        skill_id = uuid.uuid4().hex
        content = "x" * 500
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", content)
        rows = storage.list_skill_resources(skill_id)
        assert len(rows) == 1
        assert len(rows[0]["content"]) == 500
