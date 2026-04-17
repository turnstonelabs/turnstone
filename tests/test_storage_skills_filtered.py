"""Storage-protocol tests for ``list_skills_filtered``.

Runs on both SQLite and PostgreSQL via the shared ``storage_backend``
fixture (``conftest.py``) so the tag-substring filter and column-match
filters are validated against both backends' ``LIKE`` semantics.
"""

from __future__ import annotations

import json
from typing import Any


def _create_skill(
    storage: Any,
    *,
    template_id: str,
    name: str,
    category: str = "general",
    tags: list[str] | None = None,
    scan_status: str = "",
    enabled: bool = True,
    priority: int = 0,
) -> None:
    storage.create_prompt_template(
        template_id=template_id,
        name=name,
        category=category,
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        tags=json.dumps(tags or []),
        priority=priority,
        enabled=enabled,
    )
    if scan_status:
        # scan_status is set by the scanner pipeline, not create_prompt_template;
        # patch it directly so tests can fix the value.
        with storage._conn() as conn:
            import sqlalchemy as sa

            from turnstone.core.storage._schema import prompt_templates

            conn.execute(
                sa.update(prompt_templates)
                .where(prompt_templates.c.template_id == template_id)
                .values(scan_status=scan_status)
            )
            conn.commit()


class TestListSkillsFiltered:
    def test_no_filters_returns_all_ordered_by_priority_then_name(self, storage):
        _create_skill(storage, template_id="s1", name="zebra", priority=10)
        _create_skill(storage, template_id="s2", name="alpha", priority=10)
        _create_skill(storage, template_id="s3", name="any", priority=1)
        rows = storage.list_skills_filtered()
        names = [r["name"] for r in rows]
        # priority asc (1 then 10), name asc within priority.
        assert names == ["any", "alpha", "zebra"]

    def test_category_exact_match(self, storage):
        _create_skill(storage, template_id="s1", name="a", category="ops")
        _create_skill(storage, template_id="s2", name="b", category="engineering")
        _create_skill(storage, template_id="s3", name="c", category="engineering")
        rows = storage.list_skills_filtered(category="engineering")
        assert {r["name"] for r in rows} == {"b", "c"}

    def test_tag_substring_quote_safe(self, storage):
        # Quote-bracketed pattern: `"foo"` matches `["foo", "bar"]` but not `["foobar"]`.
        _create_skill(storage, template_id="s1", name="m", tags=["foo", "bar"])
        _create_skill(storage, template_id="s2", name="m2", tags=["foobar"])
        _create_skill(storage, template_id="s3", name="m3", tags=["other"])
        rows = storage.list_skills_filtered(tag="foo")
        assert {r["name"] for r in rows} == {"m"}

    def test_tag_filter_is_case_insensitive_on_both_backends(self, storage):
        """SQLite LIKE is case-insensitive by default; PostgreSQL is not.
        Normalise at the filter site so dev and prod return the same rows."""
        _create_skill(storage, template_id="s1", name="a", tags=["GPU"])
        _create_skill(storage, template_id="s2", name="b", tags=["cpu"])
        assert {r["name"] for r in storage.list_skills_filtered(tag="gpu")} == {"a"}
        assert {r["name"] for r in storage.list_skills_filtered(tag="GPU")} == {"a"}
        assert {r["name"] for r in storage.list_skills_filtered(tag="Gpu")} == {"a"}
        assert {r["name"] for r in storage.list_skills_filtered(tag="CPU")} == {"b"}

    def test_tag_filter_escapes_like_wildcards(self, storage):
        """Literal ``%`` / ``_`` in the tag must NOT act as SQL wildcards."""
        _create_skill(storage, template_id="s1", name="literal", tags=["a%b"])
        _create_skill(storage, template_id="s2", name="underscore-tag", tags=["a_b"])
        _create_skill(storage, template_id="s3", name="decoy", tags=["axxb", "acb"])
        # Literal `%` matches only the literal tag, not arbitrary chars.
        assert {r["name"] for r in storage.list_skills_filtered(tag="a%b")} == {"literal"}
        # Literal `_` matches only the literal tag, not any single char.
        assert {r["name"] for r in storage.list_skills_filtered(tag="a_b")} == {"underscore-tag"}

    def test_scan_status_filter(self, storage):
        _create_skill(storage, template_id="s1", name="a", scan_status="clean")
        _create_skill(storage, template_id="s2", name="b", scan_status="flagged")
        _create_skill(storage, template_id="s3", name="c")
        rows = storage.list_skills_filtered(scan_status="flagged")
        assert {r["name"] for r in rows} == {"b"}

    def test_enabled_only_filter(self, storage):
        _create_skill(storage, template_id="s1", name="a", enabled=True)
        _create_skill(storage, template_id="s2", name="b", enabled=False)
        rows = storage.list_skills_filtered(enabled_only=True)
        assert {r["name"] for r in rows} == {"a"}

    def test_limit_caps_rows(self, storage):
        for i in range(5):
            _create_skill(storage, template_id=f"s{i}", name=f"sk-{i:02d}")
        rows = storage.list_skills_filtered(limit=2)
        assert len(rows) == 2

    def test_filters_combine_with_and_semantics(self, storage):
        _create_skill(storage, template_id="s1", name="a", category="ops", tags=["alpha"])
        _create_skill(storage, template_id="s2", name="b", category="ops", tags=["beta"])
        _create_skill(storage, template_id="s3", name="c", category="other", tags=["alpha"])
        rows = storage.list_skills_filtered(category="ops", tag="alpha")
        assert {r["name"] for r in rows} == {"a"}

    def test_empty_result_for_no_match(self, storage):
        _create_skill(storage, template_id="s1", name="a", category="ops")
        rows = storage.list_skills_filtered(category="nonexistent")
        assert rows == []
