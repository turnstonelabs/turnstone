"""Backend-parity tests for the atomic workstream clone primitive.

The shared ``storage_backend`` fixture runs these against SQLite by default and
against PostgreSQL under ``--storage-backend=postgresql``. The contract lives at
the storage boundary: authorization, snapshot reads, destination writes, and
attachment retention either commit together or all roll back.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from turnstone.core.storage import (
    ForkCloneExpectation,
    ForkDestinationConflictError,
    ForkSourceUnavailableError,
)
from turnstone.core.storage._protocol import FORK_RESERVATION_CONFIG_KEY
from turnstone.core.storage._schema import conversations, workstream_config, workstreams
from turnstone.core.trajectory import AttachmentRef, Role


def _register(
    backend,
    ws_id: str,
    user_id: str,
    *,
    project_id: str | None = None,
    fork_reservation_token: str = "",
    state: str | None = None,
) -> None:
    backend.register_workstream(
        ws_id,
        user_id=user_id,
        project_id=project_id,
        state=state or ("creating" if fork_reservation_token else "idle"),
        kind="interactive",
        fork_reservation_token=fork_reservation_token,
    )


def _raw_conversation_rows(backend, ws_id: str) -> list[tuple[str, str | None, str | None]]:
    with backend._conn() as conn:
        rows = conn.execute(
            sa.select(
                conversations.c.content,
                conversations.c.attachments,
                conversations.c.meta,
            )
            .where(conversations.c.ws_id == ws_id)
            .order_by(conversations.c.id)
        ).all()
    return [(str(content or ""), attachments, meta) for content, attachments, meta in rows]


def _raw_workstream_config(backend, ws_id: str) -> dict[str, str]:
    with backend._conn() as conn:
        rows = conn.execute(
            sa.select(workstream_config.c.key, workstream_config.c.value).where(
                workstream_config.c.ws_id == ws_id
            )
        ).all()
    return {str(key): str(value) for key, value in rows}


def _grant_project_read(backend, user_id: str) -> None:
    backend.create_role(
        "fork-project-reader",
        "fork-project-reader",
        "Fork Project Reader",
        "project.read",
        False,
    )
    backend.assign_role(user_id, "fork-project-reader")


def test_clone_accepts_empty_source_and_replaces_config_and_project(storage_backend) -> None:
    backend = storage_backend
    backend.create_project("shared", "Shared", "owner", visibility="public")
    _grant_project_read(backend, "alice")
    _register(backend, "source", "owner", project_id="shared")
    _register(backend, "destination", "alice", project_id="shared", state="creating")
    backend.save_workstream_config(
        "source",
        {"model_alias": "fast", "temperature": "0.25"},
    )
    backend.save_workstream_config("destination", {"stale": "yes"})

    snapshot = backend.clone_workstream(
        "source",
        "destination",
        principal_id="alice",
    )

    assert snapshot.turns == ()
    assert snapshot.config == {"model_alias": "fast", "temperature": "0.25"}
    assert snapshot.project_id == "shared"
    assert backend.load_message_turns("destination") == []
    assert backend.load_workstream_config("destination") == snapshot.config
    destination = backend.get_workstream("destination")
    assert destination is not None
    assert destination["project_id"] == "shared"


@pytest.mark.parametrize("authorization_change", ["membership_revoked", "public_to_private"])
def test_clone_rechecks_current_project_authorization(
    storage_backend,
    authorization_change: str,
) -> None:
    backend = storage_backend
    visibility = "private" if authorization_change == "membership_revoked" else "public"
    backend.create_project("project", "Project", "owner", visibility=visibility)
    if authorization_change == "membership_revoked":
        backend.add_project_member("project", "alice")
    _grant_project_read(backend, "alice")
    _register(backend, "source", "owner", project_id="project")
    _register(backend, "destination", "alice", project_id="project", state="creating")
    backend.save_message("source", "user", "private history")
    backend.save_workstream_config("destination", {"keep": "unchanged"})

    if authorization_change == "membership_revoked":
        assert backend.remove_project_member("project", "alice") is True
    else:
        assert backend.update_project("project", visibility="private") is True

    with pytest.raises(ForkSourceUnavailableError, match="source is no longer available"):
        backend.clone_workstream("source", "destination", principal_id="alice")

    assert backend.load_message_turns("destination") == []
    assert backend.load_workstream_config("destination") == {"keep": "unchanged"}


@pytest.mark.parametrize("project_change", ["deleted", "rebound"])
def test_clone_refuses_source_project_change_after_destination_preflight(
    storage_backend,
    project_change: str,
) -> None:
    backend = storage_backend
    backend.create_project("original", "Original", "alice", visibility="public")
    backend.create_project("replacement", "Replacement", "alice", visibility="public")
    _register(backend, "source", "alice", project_id="original")
    _register(backend, "destination", "alice", project_id="original", state="creating")
    backend.save_message("source", "user", "must not copy")
    backend.save_workstream_config("destination", {"keep": "unchanged"})

    if project_change == "deleted":
        assert backend.delete_project("original") is True
    else:
        with backend._conn() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == "source")
                .values(project_id="replacement")
            )
            conn.commit()

    with pytest.raises(ForkSourceUnavailableError, match="source project changed"):
        backend.clone_workstream("source", "destination", principal_id="alice")

    assert backend.load_message_turns("destination") == []
    assert backend.load_workstream_config("destination") == {"keep": "unchanged"}
    destination = backend.get_workstream("destination")
    assert destination is not None
    assert destination["project_id"] == "original"


def test_clone_rechecks_source_existence(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    _register(backend, "destination", "alice", state="creating")
    backend.save_message("source", "user", "soon deleted")
    assert backend.delete_workstream("source") is True

    with pytest.raises(ForkSourceUnavailableError):
        backend.clone_workstream("source", "destination", principal_id="alice")

    assert backend.load_message_turns("destination") == []


def test_incarnation_snapshot_claims_legacy_token_without_public_exposure(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "legacy", "alice")

    first = backend.ensure_workstream_incarnation_snapshot("legacy")
    second = backend.ensure_workstream_incarnation_snapshot("legacy")

    assert first is not None and second is not None
    token = first["fork_reservation_token"]
    assert isinstance(token, str) and token
    assert second["fork_reservation_token"] == token
    backend.save_workstream_config(
        "legacy",
        {
            "visible": "kept",
            FORK_RESERVATION_CONFIG_KEY: "must-not-overwrite",
        },
    )
    public_row = backend.get_workstream("legacy")
    assert public_row is not None
    assert "fork_reservation_token" not in public_row
    assert backend.load_workstream_config("legacy") == {"visible": "kept"}
    assert _raw_workstream_config(backend, "legacy") == {
        FORK_RESERVATION_CONFIG_KEY: token,
        "visible": "kept",
    }


def test_clone_rejects_hidden_creating_source(storage_backend) -> None:
    backend = storage_backend
    _register(
        backend,
        "source",
        "alice",
        state="creating",
        fork_reservation_token="source-incarnation",
    )
    _register(backend, "destination", "alice", state="creating")
    backend.save_message("source", "user", "not yet published")

    with pytest.raises(ForkSourceUnavailableError, match="source is no longer available"):
        backend.clone_workstream("source", "destination", principal_id="alice")

    assert backend.load_message_turns("destination") == []


def test_clone_refuses_consumed_source_id_after_preflight(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    backend.save_message("source", "user", "authorized predecessor")
    source_snapshot = backend.ensure_workstream_incarnation_snapshot("source")
    assert source_snapshot is not None
    predecessor_token = source_snapshot["fork_reservation_token"]

    assert backend.delete_workstream("source") is True
    assert (
        backend.register_workstream(
            "source",
            user_id="alice",
            state="idle",
            kind="interactive",
            fork_reservation_token="replacement-incarnation",
        )
        is False
    )
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="destination-incarnation",
    )
    expectation = ForkCloneExpectation(
        persona_config=(),
        project_id="",
        project_name="",
        project_writable=False,
        destination_reservation_token="destination-incarnation",
        source_reservation_token=predecessor_token,
    )

    with pytest.raises(ForkSourceUnavailableError, match="source is no longer available"):
        backend.clone_workstream(
            "source",
            "destination",
            principal_id="alice",
            expected_session=expectation,
        )

    assert backend.load_message_turns("destination") == []
    assert backend.load_message_turns("source") == []
    assert backend.get_workstream_reservation_token("source") == ""


def test_clone_refuses_nonempty_destination_without_mutation(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    _register(backend, "destination", "alice", state="creating")
    backend.save_message("source", "user", "source")
    backend.save_message("destination", "user", "existing")
    backend.save_workstream_config("destination", {"keep": "yes"})

    with pytest.raises(ForkDestinationConflictError, match="already has history"):
        backend.clone_workstream("source", "destination", principal_id="alice")

    assert [turn.text for turn in backend.load_message_turns("destination")] == ["existing"]
    assert backend.load_workstream_config("destination") == {"keep": "yes"}


def test_clone_retains_matching_destination_reservation_privately(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="destination-incarnation",
    )
    backend.save_message("source", "user", "copy me")
    backend.save_workstream_config("source", {"source": "adopted"})
    source_snapshot = backend.ensure_workstream_incarnation_snapshot("source")
    assert source_snapshot is not None

    snapshot = backend.clone_workstream(
        "source",
        "destination",
        principal_id="alice",
        expected_session=ForkCloneExpectation(
            persona_config=(),
            project_id="",
            project_name="",
            project_writable=False,
            destination_reservation_token="destination-incarnation",
            source_reservation_token=source_snapshot["fork_reservation_token"],
        ),
    )

    assert [turn.text for turn in snapshot.turns] == ["copy me"]
    assert snapshot.config == {"source": "adopted"}
    assert backend.load_workstream_config("destination") == snapshot.config
    assert _raw_workstream_config(backend, "destination") == {
        FORK_RESERVATION_CONFIG_KEY: "destination-incarnation",
        "source": "adopted",
    }
    assert (
        backend.delete_workstream_if_fork_reserved(
            "destination",
            "destination-incarnation",
        )
        is True
    )
    assert backend.get_workstream("destination") is None


def test_duplicate_registration_cannot_steal_destination_reservation(storage_backend) -> None:
    backend = storage_backend
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="incumbent",
    )

    inserted = backend.register_workstream(
        "destination",
        user_id="alice",
        kind="interactive",
        fork_reservation_token="challenger",
    )

    assert inserted is False
    assert _raw_workstream_config(backend, "destination") == {
        FORK_RESERVATION_CONFIG_KEY: "incumbent",
    }


@pytest.mark.parametrize("new_token", ["", "fresh-incarnation"])
def test_registration_cannot_inherit_orphaned_reservation(
    storage_backend,
    new_token: str,
) -> None:
    backend = storage_backend
    with backend._conn() as conn:
        conn.execute(
            sa.insert(workstream_config),
            {
                "ws_id": "destination",
                "key": FORK_RESERVATION_CONFIG_KEY,
                "value": "orphaned-incarnation",
            },
        )
        conn.commit()

    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token=new_token,
    )

    expected = {FORK_RESERVATION_CONFIG_KEY: new_token} if new_token else {}
    assert _raw_workstream_config(backend, "destination") == expected
    assert (
        backend.delete_workstream_if_fork_reserved(
            "destination",
            "orphaned-incarnation",
        )
        is False
    )
    assert backend.get_workstream("destination") is not None


def test_finalize_deferred_create_applies_all_writes_atomically(storage_backend) -> None:
    backend = storage_backend
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="destination-incarnation",
    )
    backend.save_workstream_config("destination", {"existing": "preserved"})

    finalized = backend.finalize_deferred_create(
        "destination",
        "destination-incarnation",
        alias="friendly-name",
        config={
            "new-setting": "installed",
            FORK_RESERVATION_CONFIG_KEY: "must-not-overwrite",
        },
        node_id="node-a",
        override_reason="local",
    )

    assert finalized is True
    row = backend.get_workstream("destination")
    assert row is not None
    assert row["alias"] == "friendly-name"
    assert backend.load_workstream_config("destination") == {
        "existing": "preserved",
        "new-setting": "installed",
    }
    assert _raw_workstream_config(backend, "destination") == {
        FORK_RESERVATION_CONFIG_KEY: "destination-incarnation",
        "existing": "preserved",
        "new-setting": "installed",
    }
    overrides = backend.list_workstream_overrides()
    assert len(overrides) == 1
    assert overrides[0]["ws_id"] == "destination"
    assert overrides[0]["node_id"] == "node-a"
    assert overrides[0]["reason"] == "local"


def test_finalize_deferred_create_refuses_replaced_reservation(storage_backend) -> None:
    backend = storage_backend
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="first-incarnation",
    )
    assert backend.delete_workstream("destination") is True
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="replacement-incarnation",
    )
    assert backend.set_workstream_alias("destination", "replacement-name") is True
    backend.save_workstream_config("destination", {"replacement": "untouched"})
    backend.set_workstream_override("destination", "node-b", reason="replacement")

    finalized = backend.finalize_deferred_create(
        "destination",
        "first-incarnation",
        alias="stale-name",
        config={"stale": "must-not-land"},
        node_id="node-a",
        override_reason="local",
    )

    assert finalized is False
    row = backend.get_workstream("destination")
    assert row is not None
    assert row["alias"] == "replacement-name"
    assert backend.load_workstream_config("destination") == {"replacement": "untouched"}
    assert _raw_workstream_config(backend, "destination") == {
        FORK_RESERVATION_CONFIG_KEY: "replacement-incarnation",
        "replacement": "untouched",
    }
    overrides = backend.list_workstream_overrides()
    assert len(overrides) == 1
    assert overrides[0]["ws_id"] == "destination"
    assert overrides[0]["node_id"] == "node-b"
    assert overrides[0]["reason"] == "replacement"


def test_finalize_deferred_create_alias_conflict_rolls_back_other_writes(
    storage_backend,
) -> None:
    backend = storage_backend
    _register(backend, "incumbent", "alice")
    assert backend.set_workstream_alias("incumbent", "taken-name") is True
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="destination-incarnation",
    )
    backend.save_workstream_config("destination", {"existing": "preserved"})
    backend.set_workstream_override("destination", "node-before", reason="existing")

    finalized = backend.finalize_deferred_create(
        "destination",
        "destination-incarnation",
        alias="taken-name",
        config={"stale": "must-not-land"},
        node_id="node-after",
        override_reason="local",
    )

    assert finalized is False
    row = backend.get_workstream("destination")
    assert row is not None
    assert row["alias"] is None
    assert backend.load_workstream_config("destination") == {"existing": "preserved"}
    overrides = backend.list_workstream_overrides()
    destination_override = next(row for row in overrides if row["ws_id"] == "destination")
    assert destination_override["node_id"] == "node-before"
    assert destination_override["reason"] == "existing"


def test_clone_refuses_replaced_destination_reservation(storage_backend) -> None:
    """A same-id replacement cannot inherit an earlier create's clone."""
    backend = storage_backend
    _register(backend, "source", "alice")
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="first-incarnation",
    )
    backend.save_message("source", "user", "must not copy")
    backend.save_workstream_config("source", {"source": "unchanged"})
    source_snapshot = backend.ensure_workstream_incarnation_snapshot("source")
    assert source_snapshot is not None

    assert backend.delete_workstream("destination") is True
    _register(
        backend,
        "destination",
        "alice",
        fork_reservation_token="replacement-incarnation",
    )
    backend.save_workstream_config("destination", {"replacement": "untouched"})

    expectation = ForkCloneExpectation(
        persona_config=(),
        project_id="",
        project_name="",
        project_writable=False,
        destination_reservation_token="first-incarnation",
        source_reservation_token=source_snapshot["fork_reservation_token"],
    )
    with pytest.raises(ForkDestinationConflictError, match="destination is not available"):
        backend.clone_workstream(
            "source",
            "destination",
            principal_id="alice",
            expected_session=expectation,
        )

    assert backend.load_message_turns("destination") == []
    assert backend.load_workstream_config("destination") == {
        "replacement": "untouched",
    }
    assert _raw_workstream_config(backend, "destination") == {
        FORK_RESERVATION_CONFIG_KEY: "replacement-incarnation",
        "replacement": "untouched",
    }
    assert (
        backend.delete_workstream_if_fork_reserved(
            "destination",
            "first-incarnation",
        )
        is False
    )
    assert [turn.text for turn in backend.load_message_turns("source")] == ["must not copy"]
    assert backend.load_workstream_config("source") == {"source": "unchanged"}


def test_clone_preserves_raw_attachment_refs_and_balances_refcounts(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    destination_token = "attachment-clone-incarnation"
    _register(
        backend,
        "destination",
        "alice",
        state="creating",
        fork_reservation_token=destination_token,
    )

    document_id = "a" * 64
    preview_id = "b" * 64
    user_row = backend.save_message("source", "user", "read this")
    backend.save_attachment(
        document_id,
        "notes.txt",
        "text/plain",
        5,
        "text",
        b"notes",
    )
    backend.set_message_attachments("source", user_row, [document_id])

    tool_calls = json.dumps(
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "render", "arguments": "{}"},
            }
        ]
    )
    backend.save_message("source", "assistant", None, tool_calls=tool_calls)
    preview_meta = {
        "effect_status": "committed",
        "preview": {"attachment_id": preview_id, "title": "Rendered output"},
    }
    tool_row = backend.save_message(
        "source",
        "tool",
        "rendered",
        tool_call_id="call-1",
        meta=json.dumps(preview_meta),
    )
    backend.save_attachment(
        preview_id,
        "preview.html",
        "text/html",
        7,
        "preview",
        b"preview",
        origin="tool",
    )
    backend.set_message_attachments("source", tool_row, [preview_id])
    backend.save_message("source", "assistant", "done")
    backend.save_workstream_config("destination", {"stale": "remove-me"})

    snapshot = backend.clone_workstream(
        "source",
        "destination",
        principal_id="alice",
    )

    assert [turn.text for turn in snapshot.turns] == ["read this", "", "rendered", "done"]
    user_turn = snapshot.turns[0]
    assert user_turn.role is Role.USER
    assert [
        block.attachment_id for block in user_turn.content if isinstance(block, AttachmentRef)
    ] == [document_id]
    tool_turn = snapshot.turns[2]
    assert tool_turn.meta.extra["preview"]["attachment_id"] == preview_id
    assert tool_turn.meta.extra["storage_attachment_ids"] == [preview_id]
    assert backend.load_workstream_config("destination") == {}

    raw_rows = _raw_conversation_rows(backend, "destination")
    assert json.loads(raw_rows[0][1] or "[]") == [document_id]
    assert json.loads(raw_rows[2][1] or "[]") == [preview_id]
    assert json.loads(raw_rows[2][2] or "{}") == preview_meta
    # The clone transaction commits before lifecycle publication, but ordinary
    # recall must not expose that provisional transcript.  Exact publication
    # flips the same durable incarnation to visible.
    assert not any(row[1] == "destination" for row in backend.search_history("rendered"))
    assert backend.publish_deferred_create("destination", destination_token) is True
    assert any(row[1] == "destination" for row in backend.search_history("rendered"))
    for attachment_id in (document_id, preview_id):
        attachment = backend.get_attachment(attachment_id)
        assert attachment is not None
        assert attachment["refcount"] == 2

    assert backend.delete_workstream("source") is True
    for attachment_id in (document_id, preview_id):
        attachment = backend.get_attachment(attachment_id)
        assert attachment is not None
        assert attachment["refcount"] == 1


def test_missing_attachment_rolls_back_refs_config_history_and_binding(storage_backend) -> None:
    backend = storage_backend
    backend.create_project("old-project", "Old", "alice", visibility="private")
    _register(backend, "source", "alice", project_id="old-project")
    _register(
        backend,
        "destination",
        "alice",
        project_id="old-project",
        state="creating",
    )
    existing_id = "c" * 64
    missing_id = "d" * 64
    source_row = backend.save_message("source", "user", "two refs")
    backend.save_attachment(
        existing_id,
        "exists.txt",
        "text/plain",
        6,
        "text",
        b"exists",
    )
    backend.set_message_attachments("source", source_row, [existing_id, missing_id])
    backend.save_workstream_config("source", {"source": "value"})
    backend.save_workstream_config("destination", {"keep": "value"})

    with pytest.raises(ForkSourceUnavailableError, match="attachments are no longer available"):
        backend.clone_workstream("source", "destination", principal_id="alice")

    existing = backend.get_attachment(existing_id)
    assert existing is not None
    assert existing["refcount"] == 1
    assert backend.load_message_turns("destination") == []
    assert backend.load_workstream_config("destination") == {"keep": "value"}
    destination = backend.get_workstream("destination")
    assert destination is not None
    assert destination["project_id"] == "old-project"


def test_compacted_clone_rewrites_marker_to_destination_id_space(storage_backend) -> None:
    backend = storage_backend
    _register(backend, "source", "alice")
    _register(backend, "destination", "alice", state="creating")
    backend.save_message("source", "user", "old question")
    backend.save_message("source", "assistant", "old answer")
    source_watermark = backend.get_compaction_watermark("source")
    assert source_watermark is not None
    backend.save_message(
        "source",
        "assistant",
        "SUMMARY",
        source="compaction",
        meta=json.dumps({"watermark": source_watermark, "input_tokens": 321}),
    )
    backend.save_message("source", "user", "new question")
    backend.save_message("source", "assistant", "new answer")

    snapshot = backend.clone_workstream(
        "source",
        "destination",
        principal_id="alice",
    )

    expected = ["[Conversation summary]", "SUMMARY", "new question", "new answer"]
    assert [turn.text for turn in snapshot.turns] == expected
    assert [turn.text for turn in backend.load_message_turns("destination")] == expected
    with backend._conn() as conn:
        marker = conn.execute(
            sa.select(conversations.c.id, conversations.c.meta).where(
                conversations.c.ws_id == "destination",
                conversations.c._source == "compaction",
            )
        ).one()
    marker_id = int(marker[0])
    marker_meta = json.loads(marker[1])
    assert marker_meta == {"watermark": marker_id, "input_tokens": 321}
    assert marker_id != source_watermark
    assert backend.get_compaction_checkpoint("destination") == marker_id
    assert backend.count_messages("destination") == 3  # marker + two live tail rows
