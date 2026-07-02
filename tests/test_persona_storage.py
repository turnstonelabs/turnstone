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
import sqlalchemy as sa


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
        # Non-seed slug (the migration seeds a real "scribe"); the display name
        # is name.title(), so a hyphenated slug title-cases each segment.
        p = _mk(backend, "test-scribe")
        assert p["display_name"] == "Test-Scribe"
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
        _mk(backend, "test-writer", base_prompt="You write.")
        p = backend.get_persona_by_name("test-writer")
        assert p is not None
        assert p["persona_id"] == "id-test-writer"
        assert p["base_prompt"] == "You write."

    def test_duplicate_name_rejected(self, backend: Any) -> None:
        _mk(backend, "test-scribe")
        with pytest.raises(ValueError, match="already exists"):
            backend.create_persona({"persona_id": "other", "name": "test-scribe"})

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


class TestPersonaStorageHardening:
    """Serializer size caps, corrupt-row reads, the serialize-before-invariant
    ordering, and the single-default backstop — the storage edge every future
    ingress (SDK-direct, admin CLI) inherits, so it rejects rather than
    truncates or decodes garbage."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("display_name", "x" * 129),
            ("description", "x" * 1025),
            ("base_prompt", "x" * 32769),
        ],
    )
    def test_capped_field_over_limit_raises(self, backend: Any, field: str, value: str) -> None:
        # Each operator-authored text field is bounded; one char over its cap is
        # a ValueError naming the field, not a silent truncation.
        with pytest.raises(ValueError, match=field):
            _mk(backend, "capped", **{field: value})

    def test_allowlist_too_many_entries_raises(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="tool_allowlist"):
            _mk(backend, "big-list", tool_allowlist=[f"t{i}" for i in range(513)])

    def test_allowlist_entry_too_long_raises(self, backend: Any) -> None:
        with pytest.raises(ValueError, match="tool_allowlist"):
            _mk(backend, "long-entry", tool_allowlist=["x" * 257])

    def test_corrupt_allowlist_read_raises_naming_persona(self, backend: Any) -> None:
        # A row whose tool_allowlist JSON parses but is the wrong shape (an
        # object where a list-of-strings is required) must fail loudly on read,
        # naming the persona — never decode into a garbage envelope that masks a
        # broken invariant.
        _mk(backend, "corrupt-row")
        with backend._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE personas SET tool_allowlist = :bad WHERE persona_id = :pid"),
                {"bad": '{"not": "a list"}', "pid": "id-corrupt-row"},
            )
        with pytest.raises(ValueError, match="id-corrupt-row"):
            backend.get_persona("id-corrupt-row")
        with pytest.raises(ValueError, match="id-corrupt-row"):
            backend.list_personas()

    def test_update_none_kinds_raises_value_error_not_type_error(self, backend: Any) -> None:
        # applies_to_kinds=None (an explicit JSON null from an
        # UpdatePersonaRequest) reaches storage; validating BEFORE the invariant
        # checks surfaces the serializer's precise ValueError instead of a
        # TypeError escaping the route's 400 mapping as a 500.  pytest.raises on
        # ValueError alone would let a TypeError propagate and fail the test.
        _mk(backend, "upd-none")
        with pytest.raises(ValueError, match="applies_to_kinds"):
            backend.update_persona("id-upd-none", applies_to_kinds=None, is_default=True)

    def test_duplicate_name_insert_race_maps_to_value_error(self, backend: Any) -> None:
        # TOCTOU: two concurrent creates both pass the name pre-check, then one
        # loses the UNIQUE(name) INSERT.  The loser's IntegrityError must surface
        # as the same "already exists" ValueError the pre-check raises (one 400
        # shape), never an opaque 500.  Force the race window by blanking the
        # pre-check's result for a name that really exists, so the INSERT hits a
        # genuine constraint violation.
        import contextlib

        _mk(backend, "racer")  # the winner row is really present now
        real_conn = backend._conn

        class _NoRow:
            def fetchone(self) -> None:
                return None

        class _PrecheckMiss:
            # Delegates to a real connection but blanks the FIRST result
            # (create_persona's name pre-check) so the code proceeds to INSERT.
            def __init__(self, conn: Any) -> None:
                self._conn = conn
                self._blanked = False

            def execute(self, *args: Any, **kwargs: Any) -> Any:
                result = self._conn.execute(*args, **kwargs)
                if not self._blanked:
                    self._blanked = True
                    return _NoRow()
                return result

            def __getattr__(self, name: str) -> Any:
                return getattr(self._conn, name)

        @contextlib.contextmanager
        def _racing_conn() -> Any:
            with real_conn() as conn:
                yield _PrecheckMiss(conn)

        backend._conn = _racing_conn
        try:
            with pytest.raises(ValueError, match="already exists"):
                backend.create_persona({"persona_id": "racer-2", "name": "racer"})
        finally:
            backend._conn = real_conn

    def test_single_default_backstop_rolls_back(
        self, backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Manufacture two enabled interactive defaults directly (bypassing the
        # demotion the normal path enforces), then suppress the in-txn demotion
        # to model a promotion that slipped past serialization — the exact
        # concurrent state the post-promote backstop exists to catch.  Its
        # ValueError must roll the whole transaction back (the promotion must
        # NOT stick).
        now = "2026-01-01T00:00:00"
        with backend._engine.begin() as conn:
            for pid in ("mfg-d1", "mfg-d2"):
                conn.execute(
                    sa.text(
                        "INSERT INTO personas (persona_id, name, display_name, "
                        "description, base_prompt, tool_allowlist, mcp_enabled, "
                        "memory_enabled, applies_to_kinds, is_default, enabled, "
                        "org_id, created_by, created, updated) VALUES "
                        "(:pid, :pid, '', '', NULL, NULL, 1, 1, :kinds, 1, 1, "
                        "'', '', :now, :now)"
                    ),
                    {"pid": pid, "kinds": '["interactive"]', "now": now},
                )
        _mk(backend, "promotee")  # a third: enabled, interactive, non-default
        monkeypatch.setattr(
            type(backend).__module__ + "._validate_and_clear_default_persona",
            lambda *a, **k: None,
        )
        with pytest.raises(ValueError, match="concurrent default"):
            backend.update_persona("id-promotee", is_default=True)
        # The backstop rolled the txn back: the promotion did not commit, and the
        # manufactured pair still hold their (illegally duplicated) default flag.
        assert backend.get_persona("id-promotee")["is_default"] is False
        assert backend.get_persona("mfg-d1")["is_default"] is True
        assert backend.get_persona("mfg-d2")["is_default"] is True
