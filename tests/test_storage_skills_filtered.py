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
    risk_level: str = "",
    enabled: bool = True,
    priority: int = 0,
    kind: str = "any",
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
        kind=kind,
    )
    if risk_level:
        # risk_level is set by the scanner pipeline, not create_prompt_template;
        # patch it directly so tests can fix the value.
        with storage._conn() as conn:
            import sqlalchemy as sa

            from turnstone.core.storage._schema import prompt_templates

            conn.execute(
                sa.update(prompt_templates)
                .where(prompt_templates.c.template_id == template_id)
                .values(risk_level=risk_level)
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
        """Literal ``%`` / ``_`` in the tag must NOT act as SQL wildcards.

        The current implementation uses JSON containment (``json_each`` on
        SQLite, ``jsonb_array_elements_text`` on PostgreSQL) so SQL
        wildcards never participate at all — but the contract still holds
        and is worth pinning.
        """
        _create_skill(storage, template_id="s1", name="literal", tags=["a%b"])
        _create_skill(storage, template_id="s2", name="underscore-tag", tags=["a_b"])
        _create_skill(storage, template_id="s3", name="decoy", tags=["axxb", "acb"])
        # Literal `%` matches only the literal tag, not arbitrary chars.
        assert {r["name"] for r in storage.list_skills_filtered(tag="a%b")} == {"literal"}
        # Literal `_` matches only the literal tag, not any single char.
        assert {r["name"] for r in storage.list_skills_filtered(tag="a_b")} == {"underscore-tag"}

    def test_tag_filter_handles_quote_in_tag_value(self, storage):
        """Tag values containing ``"`` must match correctly.  The earlier
        ``%"<tag>"%`` LIKE pattern depended on the absence of quotes in
        the value — a tag like ``foo"bar`` would have been encoded as
        ``"foo\\"bar"`` in the JSON column and either matched the wrong
        thing or nothing at all.  JSON containment decodes element-by-
        element so the literal value matches as written."""
        _create_skill(storage, template_id="s1", name="quoted", tags=['foo"bar'])
        _create_skill(storage, template_id="s2", name="other", tags=["foobar"])
        rows = storage.list_skills_filtered(tag='foo"bar')
        assert {r["name"] for r in rows} == {"quoted"}
        # And the unrelated row doesn't false-positive.
        rows2 = storage.list_skills_filtered(tag="foobar")
        assert {r["name"] for r in rows2} == {"other"}

    def test_tag_filter_handles_backslash_in_tag_value(self, storage):
        """A backslash in the tag would have been doubled in the stored
        JSON text (``\\\\``); the substring LIKE pattern would have
        searched for ``\\`` in the input and missed the doubled form."""
        _create_skill(storage, template_id="s1", name="bs", tags=["a\\b"])
        _create_skill(storage, template_id="s2", name="other", tags=["ab"])
        rows = storage.list_skills_filtered(tag="a\\b")
        assert {r["name"] for r in rows} == {"bs"}

    def test_tag_filter_handles_unicode_in_tag_value(self, storage):
        """Multi-byte UTF-8 tag values round-trip through JSON
        containment.  A previous regression would have hit if the JSON
        encoder escaped non-ASCII to ``\\uXXXX`` and the substring
        pattern was supplied as the raw character."""
        _create_skill(storage, template_id="s1", name="cjk", tags=["\u6f22\u5b57"])
        _create_skill(storage, template_id="s2", name="other", tags=["ab"])
        rows = storage.list_skills_filtered(tag="\u6f22\u5b57")
        assert {r["name"] for r in rows} == {"cjk"}

    def test_risk_level_filter(self, storage):
        _create_skill(storage, template_id="s1", name="a", risk_level="clean")
        _create_skill(storage, template_id="s2", name="b", risk_level="flagged")
        _create_skill(storage, template_id="s3", name="c")
        rows = storage.list_skills_filtered(risk_level="flagged")
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

    def test_kinds_filter_narrows_to_listed_buckets(self, storage):
        """The ``kinds`` filter narrows the result to rows whose ``kind``
        column is in the supplied list — used by the coordinator client
        to hide interactive-only skills and by any future interactive
        lister to hide coordinator-only skills."""
        _create_skill(storage, template_id="s1", name="interactive-only", kind="interactive")
        _create_skill(storage, template_id="s2", name="coord-only", kind="coordinator")
        _create_skill(storage, template_id="s3", name="universal", kind="any")

        coord_view = storage.list_skills_filtered(kinds=["coordinator", "any"])
        assert {r["name"] for r in coord_view} == {"coord-only", "universal"}

        interactive_view = storage.list_skills_filtered(kinds=["interactive", "any"])
        assert {r["name"] for r in interactive_view} == {"interactive-only", "universal"}

    def test_kinds_none_returns_all_kinds(self, storage):
        """``kinds=None`` (the default) applies no kind filter — admin
        surfaces that want the full catalog leave it unset."""
        _create_skill(storage, template_id="s1", name="interactive-only", kind="interactive")
        _create_skill(storage, template_id="s2", name="coord-only", kind="coordinator")
        _create_skill(storage, template_id="s3", name="universal", kind="any")
        assert len(storage.list_skills_filtered()) == 3

    def test_kinds_empty_list_behaves_like_none(self, storage):
        """An empty ``kinds`` list is treated the same as None (no
        filter).  Prevents an accidental empty-result from a caller
        that defensively materialises a set / list."""
        _create_skill(storage, template_id="s1", name="interactive", kind="interactive")
        _create_skill(storage, template_id="s2", name="coord", kind="coordinator")
        assert len(storage.list_skills_filtered(kinds=[])) == 2
