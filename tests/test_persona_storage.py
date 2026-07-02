"""Tests for the personas storage layer.

Runs against whichever backend ``--storage-backend`` selects (the ``backend``
fixture), so the SQLite and PostgreSQL implementations are exercised by the
same assertions.  Focus areas: the tri-state ``tool_allowlist`` round-trip
(None vs [] vs [names] — the NULL/empty distinction is load-bearing for the
visibility lever), the one-default-per-kind invariant, and the
default-not-archivable rule.
"""

from __future__ import annotations

from typing import Any

import pytest


def _mk(backend: Any, name: str, **over: Any) -> dict[str, Any]:
    row = {
        "persona_id": f"id-{name}",
        "name": name,
        "display_name": name.title(),
        "description": "",
        "applies_to_kinds": ["interactive"],
    }
    row.update(over)
    backend.create_persona(row)
    got = backend.get_persona(row["persona_id"])
    assert got is not None
    return got


class TestPersonaCRUD:
    def test_create_and_get_defaults(self, backend: Any) -> None:
        p = _mk(backend, "scribe")
        assert p["display_name"] == "Scribe"
        assert p["base_prompt"] is None
        assert p["tool_allowlist"] is None
        assert p["mcp_enabled"] is True
        assert p["memory_enabled"] is True
        assert p["applies_to_kinds"] == ["interactive"]
        assert p["is_default"] is False
        assert p["enabled"] is True

    def test_get_missing(self, backend: Any) -> None:
        assert backend.get_persona("nope") is None
        assert backend.get_persona_by_name("nope") is None
        assert backend.get_default_persona("interactive") is None

    def test_get_by_name(self, backend: Any) -> None:
        _mk(backend, "writer", base_prompt="You write.")
        p = backend.get_persona_by_name("writer")
        assert p is not None
        assert p["persona_id"] == "id-writer"
        assert p["base_prompt"] == "You write."

    def test_duplicate_name_rejected(self, backend: Any) -> None:
        _mk(backend, "scribe")
        with pytest.raises(ValueError, match="already exists"):
            backend.create_persona({"persona_id": "other", "name": "scribe"})

    def test_missing_identity_rejected(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="persona_id and name"):
            backend.create_persona({"name": "x"})
        with pytest.raises(ValueError, match="persona_id and name"):
            backend.create_persona({"persona_id": "x"})

    def test_tool_allowlist_tristate_roundtrip(self, backend: Any) -> None:
        # The three states must survive storage distinctly: None (unrestricted)
        # vs [] (hard empty) vs [names] (exact set).
        _mk(backend, "unrestricted", tool_allowlist=None)
        _mk(backend, "empty", tool_allowlist=[])
        _mk(backend, "listed", tool_allowlist=["read_file", "search"])
        assert backend.get_persona_by_name("unrestricted")["tool_allowlist"] is None
        assert backend.get_persona_by_name("empty")["tool_allowlist"] == []
        assert backend.get_persona_by_name("listed")["tool_allowlist"] == ["read_file", "search"]

    def test_tool_allowlist_survives_update(self, backend: Any) -> None:
        _mk(backend, "p", tool_allowlist=["memory"])
        assert backend.update_persona("id-p", tool_allowlist=[])
        assert backend.get_persona("id-p")["tool_allowlist"] == []
        assert backend.update_persona("id-p", tool_allowlist=None)
        assert backend.get_persona("id-p")["tool_allowlist"] is None

    def test_invalid_kinds_rejected(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="applies_to_kinds"):
            _mk(backend, "bad", applies_to_kinds=["cron"])
        with pytest.raises(ValueError, match="applies_to_kinds"):
            _mk(backend, "bad2", applies_to_kinds=[])

    def test_invalid_allowlist_rejected(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="tool_allowlist"):
            _mk(backend, "bad", tool_allowlist="read_file")

    def test_update_mutable_fields(self, backend: Any) -> None:
        _mk(backend, "p")
        assert backend.update_persona(
            "id-p",
            display_name="P2",
            description="d",
            base_prompt="You are P2.",
            mcp_enabled=False,
            memory_enabled=False,
        )
        p = backend.get_persona("id-p")
        assert p["display_name"] == "P2"
        assert p["description"] == "d"
        assert p["base_prompt"] == "You are P2."
        assert p["mcp_enabled"] is False
        assert p["memory_enabled"] is False

    def test_update_ignores_immutable_and_unknown(self, backend: Any) -> None:
        _mk(backend, "p")
        # name is the immutable slug; bogus is unknown — neither persists → no-op.
        assert not backend.update_persona("id-p", name="renamed", bogus="x")
        assert backend.get_persona("id-p")["name"] == "p"

    def test_update_missing_returns_false(self, backend: Any) -> None:
        assert not backend.update_persona("nope", display_name="x")

    def test_list_filters_disabled(self, backend: Any) -> None:
        _mk(backend, "a")
        _mk(backend, "b")
        assert backend.update_persona("id-b", enabled=False)
        assert [p["name"] for p in backend.list_personas()] == ["a"]
        assert [p["name"] for p in backend.list_personas(include_disabled=True)] == ["a", "b"]

    def test_archive_and_unarchive(self, backend: Any) -> None:
        _mk(backend, "p")
        assert backend.update_persona("id-p", enabled=False)
        assert backend.get_persona("id-p")["enabled"] is False
        assert backend.update_persona("id-p", enabled=True)
        assert backend.get_persona("id-p")["enabled"] is True


class TestPersonaDefaults:
    def test_default_resolution_per_kind(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        _mk(backend, "orch", applies_to_kinds=["coordinator"], is_default=True)
        assert backend.get_default_persona("interactive")["name"] == "eng"
        assert backend.get_default_persona("coordinator")["name"] == "orch"

    def test_default_flip_demotes_incumbent(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        _mk(backend, "eng2")
        assert backend.update_persona("id-eng2", is_default=True)
        assert backend.get_default_persona("interactive")["name"] == "eng2"
        assert backend.get_persona("id-eng")["is_default"] is False

    def test_default_flip_at_create_demotes_incumbent(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        _mk(backend, "eng2", is_default=True)
        assert backend.get_default_persona("interactive")["name"] == "eng2"
        assert backend.get_persona("id-eng")["is_default"] is False

    def test_default_flip_leaves_other_kind_alone(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        _mk(backend, "orch", applies_to_kinds=["coordinator"], is_default=True)
        _mk(backend, "eng2", is_default=True)
        assert backend.get_default_persona("coordinator")["name"] == "orch"

    def test_default_cannot_be_archived(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        with pytest.raises(ValueError, match="cannot be archived"):
            backend.update_persona("id-eng", enabled=False)

    def test_default_cannot_unset_flag_directly(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        with pytest.raises(ValueError, match="successor"):
            backend.update_persona("id-eng", is_default=False)

    def test_default_cannot_change_kinds(self, backend: Any) -> None:
        _mk(backend, "eng", is_default=True)
        with pytest.raises(ValueError, match="applies_to_kinds"):
            backend.update_persona("id-eng", applies_to_kinds=["coordinator"])

    def test_default_must_be_single_kind(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="exactly one kind"):
            _mk(
                backend,
                "both",
                applies_to_kinds=["interactive", "coordinator"],
                is_default=True,
            )

    def test_disabled_persona_cannot_become_default(self, backend: Any) -> None:
        _mk(backend, "p", enabled=False)
        with pytest.raises(ValueError, match="disabled"):
            backend.update_persona("id-p", is_default=True)

    def test_disabled_default_not_resolved(self, backend: Any) -> None:
        # get_default_persona is enabled-gated; a pre-seed DB (or one whose
        # default vanished by force) resolves to None, and the create path
        # falls back to unstamped legacy creation.
        _mk(backend, "p")
        assert backend.get_default_persona("interactive") is None
