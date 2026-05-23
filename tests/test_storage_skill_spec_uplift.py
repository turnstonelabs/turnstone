"""Storage round-trip for the Anthropic spec-uplift columns (migration 056).

Each column is parsed/stored/editable in PR1 (#569); the consumers
(autoload filter / menu hide / argument substitution) land in
follow-up PRs.  These tests cover only the persistence layer — that
the four new fields survive create + read + update without loss.
"""

from __future__ import annotations

import json
from typing import Any


def _create(storage: Any, **kw: Any) -> str:
    template_id = kw.pop("template_id", "spec1")
    storage.create_prompt_template(
        template_id=template_id,
        name=kw.pop("name", "skill-one"),
        category="general",
        content="",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        **kw,
    )
    return template_id


class TestPathsRoundTrip:
    def test_default_empty_array(self, storage: Any) -> None:
        _create(storage)
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["paths"] == "[]"

    def test_create_with_paths(self, storage: Any) -> None:
        _create(storage, paths=json.dumps(["**/*.py", "packages/api/**"]))
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert json.loads(row["paths"]) == ["**/*.py", "packages/api/**"]

    def test_update_paths(self, storage: Any) -> None:
        _create(storage)
        ok = storage.update_prompt_template("spec1", paths=json.dumps(["docs/**"]))
        assert ok is True
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert json.loads(row["paths"]) == ["docs/**"]


class TestHiddenFromMenu:
    def test_default_false(self, storage: Any) -> None:
        _create(storage)
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["hidden_from_menu"] is False

    def test_create_hidden(self, storage: Any) -> None:
        _create(storage, hidden_from_menu=True)
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["hidden_from_menu"] is True

    def test_update_hidden(self, storage: Any) -> None:
        _create(storage)
        ok = storage.update_prompt_template("spec1", hidden_from_menu=1)
        assert ok is True
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["hidden_from_menu"] is True


class TestArguments:
    def test_default_empty_array(self, storage: Any) -> None:
        _create(storage)
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["arguments"] == "[]"

    def test_create_with_arguments(self, storage: Any) -> None:
        _create(storage, arguments=json.dumps(["issue", "branch"]))
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert json.loads(row["arguments"]) == ["issue", "branch"]


class TestArgumentHint:
    def test_default_empty_string(self, storage: Any) -> None:
        _create(storage)
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["argument_hint"] == ""

    def test_create_with_argument_hint(self, storage: Any) -> None:
        _create(storage, argument_hint="[issue-number]")
        row = storage.get_prompt_template("spec1")
        assert row is not None
        assert row["argument_hint"] == "[issue-number]"
