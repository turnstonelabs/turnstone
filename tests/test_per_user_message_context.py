"""Per-user message context (shared-workstream attribution).

The context-identity layer atop upstream's acting-user credential fix: the model
must be TOLD who sent each user turn on a multi-user workstream, and that must
survive a worker rehydrating history from the DB. The sender is sourced from the
acting user (``_mcp_effective_user_id`` = the ``bind_acting_user`` initiator,
owner fallback); persistence rides ``conversations.meta`` (no migration).

Covers: the ``_sender`` side-channel round-trip; DB replay routing; append-time
stamping from the acting user (and synthetic-turn exclusion); the wire-time
label injection gated on >1 distinct sender; and the shared-state detection +
one-time "has joined" note.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests._session_helpers import make_session
from turnstone.core.session import _prefix_sender_label
from turnstone.core.storage._utils import reconstruct_turns
from turnstone.core.trajectory import Role, turn_from_dict, turn_to_dict

# -- side-channel round-trip --------------------------------------------------


def test_sender_round_trips_through_turn_dict():
    turn = turn_from_dict({"role": "user", "content": "hi", "_sender": "alice"})
    assert turn.meta.extra.get("sender") == "alice"
    assert turn_to_dict(turn)["_sender"] == "alice"


def test_no_sender_leaves_no_key():
    turn = turn_from_dict({"role": "user", "content": "hi"})
    assert "sender" not in turn.meta.extra
    assert "_sender" not in turn_to_dict(turn)


# -- reconstruct (DB replay) --------------------------------------------------


def _user_row(row_id: int, content: str, meta: str | None):
    # (id, role, content, tool_name, tc_id, provider_data, tool_calls, source,
    #  event_id, is_error, meta)
    return (row_id, "user", content, None, None, None, None, None, None, False, meta)


def test_reconstruct_restores_user_sender_to_its_own_key():
    turns = reconstruct_turns([_user_row(1, "hello", json.dumps({"sender": "alice"}))], ws_id="ws1")
    assert turns[0].meta.extra.get("sender") == "alice"
    # Must NOT be misrouted into source_meta (that channel rides SYSTEM turns).
    assert "source_meta" not in turns[0].meta.extra


def test_reconstruct_user_row_without_meta_has_no_sender():
    turns = reconstruct_turns([_user_row(1, "hello", None)], ws_id="ws1")
    assert "sender" not in turns[0].meta.extra


# -- append stamps the sender from the ACTING user ----------------------------


def test_append_stamps_and_persists_acting_user():
    s = make_session(user_id="owner")
    s._acting_user_id = "alice"  # a member drives this turn (bind_acting_user result)
    with patch("turnstone.core.session.save_message", return_value=1) as sm:
        s._append_user_turn("hello", ())
    assert sm.call_args.kwargs["meta"] == json.dumps({"sender": "alice"})
    assert s.messages[-1].meta.extra.get("sender") == "alice"


def test_append_owner_turn_stamps_owner():
    s = make_session(user_id="owner")  # acting id empty -> effective = owner
    with patch("turnstone.core.session.save_message", return_value=1) as sm:
        s._append_user_turn("hello", ())
    assert sm.call_args.kwargs["meta"] == json.dumps({"sender": "owner"})


def test_append_synthetic_turn_is_unstamped():
    s = make_session(user_id="owner")
    s._acting_user_id = "alice"
    with patch("turnstone.core.session.save_message", return_value=1) as sm:
        s._append_user_turn("resuming", (), source="compaction_resume")
    assert sm.call_args.kwargs["meta"] is None
    assert "sender" not in s.messages[-1].meta.extra


# -- label injection (the model-visible half) ---------------------------------


def test_prefix_sender_label_string():
    assert _prefix_sender_label("do it", "alice") == "[message from alice]\ndo it"


def test_prefix_sender_label_multipart_folds_into_first_text():
    parts = [{"type": "text", "text": "look"}, {"type": "image", "attachment_id": "a1"}]
    out = _prefix_sender_label(parts, "alice")
    assert out[0]["text"] == "[message from alice]\nlook"
    assert out[1] == {"type": "image", "attachment_id": "a1"}  # untouched
    assert parts[0]["text"] == "look"  # input not mutated


def test_prefix_sender_label_attachment_only_inserts_leading_text():
    out = _prefix_sender_label([{"type": "image", "attachment_id": "a1"}], "alice")
    assert out[0] == {"type": "text", "text": "[message from alice]"}
    assert out[1] == {"type": "image", "attachment_id": "a1"}


def test_single_sender_not_labeled_same_ref():
    s = make_session(user_id="owner")
    msgs = [
        {"role": "user", "content": "a", "_sender": "alice"},
        {"role": "user", "content": "b", "_sender": "alice"},
    ]
    assert s._inject_sender_labels(msgs) is msgs  # allocation-free common case


def test_shared_state_labels_even_when_slice_has_single_sender():
    # Compaction can narrow the wire slice to one participant's turns. On a
    # known-shared workstream we must still label (the >1-sender count heuristic
    # alone would skip and let the model misattribute to the owner).
    s = make_session(user_id="owner")
    s._shared_workstream = True
    msgs = [{"role": "user", "content": "only alice remains", "_sender": "alice"}]
    with patch("turnstone.core.session.get_storage", return_value=None):
        out = s._inject_sender_labels(msgs)
    assert out is not msgs
    assert out[0]["content"] == "[message from alice]\nonly alice remains"


def test_shared_labels_every_sender_turn():
    # No storage -> _resolve_display_name falls back to the raw id, so labels
    # carry the id here (username resolution is covered separately below).
    s = make_session(user_id="owner")
    msgs = [
        {"role": "user", "content": "from owner", "_sender": "owner"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "from member", "_sender": "alice"},
    ]
    with patch("turnstone.core.session.get_storage", return_value=None):
        out = s._inject_sender_labels(msgs)
    assert out is not msgs
    assert out[0]["content"] == "[message from owner]\nfrom owner"
    assert out[2]["content"] == "[message from alice]\nfrom member"
    assert out[1]["content"] == "hi"  # assistant untouched
    assert msgs[0]["content"] == "from owner"  # canonical input untouched


def test_shared_leaves_synthetic_unlabeled():
    s = make_session(user_id="owner")
    msgs = [
        {"role": "user", "content": "hi", "_sender": "owner"},
        {"role": "user", "content": "hey", "_sender": "alice"},
        {"role": "user", "content": "", "_source": "wake"},  # synthetic: no _sender
    ]
    with patch("turnstone.core.session.get_storage", return_value=None):
        out = s._inject_sender_labels(msgs)
    assert out[2]["content"] == ""  # untouched -> still drops as an empty wire turn


# -- display-name resolution (senders read as usernames, not id hashes) -------


def test_resolve_display_name_owner_uses_session_username():
    s = make_session(user_id="owner", username="owner@example")
    assert s._resolve_display_name("owner") == "owner@example"


def test_resolve_display_name_others_via_storage_and_caches():
    s = make_session(user_id="owner")
    fake = MagicMock()
    fake.get_user.return_value = {"username": "alice@example", "display_name": "Alice"}
    with patch("turnstone.core.session.get_storage", return_value=fake):
        assert s._resolve_display_name("alice-id") == "alice@example"
        assert s._resolve_display_name("alice-id") == "alice@example"  # cache hit
    fake.get_user.assert_called_once()  # second lookup served from cache


def test_resolve_display_name_falls_back_to_id_when_unknown():
    s = make_session(user_id="owner")
    fake = MagicMock()
    fake.get_user.return_value = None
    with patch("turnstone.core.session.get_storage", return_value=fake):
        assert s._resolve_display_name("ghost-id") == "ghost-id"


def test_resolve_display_name_retries_after_transient_storage_error():
    # A storage error must NOT be cached: it falls back to the raw id for this
    # call but a later call retries and resolves, rather than pinning the id.
    s = make_session(user_id="owner")
    fake = MagicMock()
    fake.get_user.side_effect = [RuntimeError("storage down"), {"username": "alice@example"}]
    with patch("turnstone.core.session.get_storage", return_value=fake):
        assert s._resolve_display_name("alice-id") == "alice-id"  # error -> raw id, uncached
        assert s._resolve_display_name("alice-id") == "alice@example"  # retried, resolved
    assert fake.get_user.call_count == 2


def test_labels_render_resolved_usernames():
    s = make_session(user_id="owner")
    fake = MagicMock()
    fake.get_user.side_effect = lambda uid: {
        "owner": {"username": "owner@example"},
        "alice-id": {"username": "alice@example"},
    }.get(uid)
    msgs = [
        {"role": "user", "content": "a", "_sender": "owner"},
        {"role": "user", "content": "b", "_sender": "alice-id"},
    ]
    with patch("turnstone.core.session.get_storage", return_value=fake):
        out = s._inject_sender_labels(msgs)
    assert out[0]["content"] == "[message from owner@example]\na"
    assert out[1]["content"] == "[message from alice@example]\nb"


# -- shared-state detection + join note ---------------------------------------


def test_recompute_shared_state_from_history():
    s = make_session(user_id="owner")
    with patch("turnstone.core.session.get_storage", return_value=None):
        s.messages.append(turn_from_dict({"role": "user", "content": "a", "_sender": "owner"}))
        s._invalidate_shared_state()  # what _append_user_turn does for stamped turns
        s._recompute_shared_state()
        assert s._shared_workstream is False  # owner alone is not shared
        s.messages.append(turn_from_dict({"role": "user", "content": "b", "_sender": "alice"}))
        s._invalidate_shared_state()
        s._recompute_shared_state()
    assert s._shared_workstream is True
    assert s._known_senders == {"owner", "alice"}


def test_shared_state_latches_and_senders_never_shrink():
    # Compaction narrows self.messages to [summary]+[tail]; a participant whose
    # turns were summarized away must stay known (no duplicate join note) and
    # the workstream must stay shared (no banner flip, no prefix-cache churn).
    s = make_session(user_id="owner")
    with patch("turnstone.core.session.get_storage", return_value=None):
        s.messages.append(turn_from_dict({"role": "user", "content": "a", "_sender": "alice"}))
        s._invalidate_shared_state()
        s._recompute_shared_state()
        assert s._shared_workstream is True
        # compaction-style narrowing: alice's turns vanish from the slice
        s.messages = [turn_from_dict({"role": "user", "content": "s", "_sender": "owner"})]
        s._invalidate_shared_state()
        s._recompute_shared_state()
        assert s._shared_workstream is True  # latched
        assert "alice" in s._known_senders  # union, never overwrite
        # ...so the returning participant does not re-fire the join note
        n = len(s.messages)
        s._maybe_note_new_participant("alice")
        assert len(s.messages) == n


def test_recompute_unions_persisted_senders_once():
    # A rehydrating worker sees only the checkpointed slice; the one-time
    # full-history read recovers participants summarized out of it.
    s = make_session(user_id="owner")
    s._reset_shared_state()  # the state resume() leaves behind
    fake = MagicMock()
    fake.list_message_senders.return_value = ["alice"]
    with patch("turnstone.core.session.get_storage", return_value=fake):
        s._recompute_shared_state()
        assert s._shared_workstream is True
        assert "alice" in s._known_senders
        s._invalidate_shared_state()
        s._recompute_shared_state()  # second turn: no second full-history read
    fake.list_message_senders.assert_called_once()


def test_persisted_sender_read_retries_after_storage_error():
    # A transient storage error must not pin an incomplete participant set:
    # the next recompute (next user turn) retries the full-history read.
    s = make_session(user_id="owner")
    s._reset_shared_state()
    fake = MagicMock()
    fake.list_message_senders.side_effect = [RuntimeError("storage down"), ["alice"]]
    with patch("turnstone.core.session.get_storage", return_value=fake):
        s._recompute_shared_state()  # error -> degraded this turn, not cached
        assert s._shared_workstream is False
        s._invalidate_shared_state()  # next user turn
        s._recompute_shared_state()  # retried, recovered
    assert s._shared_workstream is True
    assert fake.list_message_senders.call_count == 2


def test_recompute_is_memoized_per_turn():
    # _init_system_messages fires many times within a turn; between user-turn
    # appends the recompute is a no-op flag check, not an O(n) rescan.
    s = make_session(user_id="owner")
    with patch("turnstone.core.session.get_storage", return_value=None):
        s._reset_shared_state()
        s._recompute_shared_state()
        s.messages.append(turn_from_dict({"role": "user", "content": "b", "_sender": "alice"}))
        s._recompute_shared_state()  # memoized: append not yet visible
        assert s._shared_workstream is False
        s._invalidate_shared_state()  # what _append_user_turn does
        s._recompute_shared_state()
        assert s._shared_workstream is True


def test_append_user_turn_invalidates_shared_state():
    s = make_session(user_id="owner")
    s._acting_user_id = "alice"
    with patch("turnstone.core.session.save_message", return_value=1):
        s._senders_dirty = False
        s._append_user_turn("hello", ())
    assert s._senders_dirty is True


def test_new_participant_flips_shared_and_emits_join_note_once():
    s = make_session(user_id="owner")
    s._known_senders = {"owner"}
    with (
        patch.object(s, "_init_system_messages") as recompose,
        patch("turnstone.core.session.get_storage", return_value=None),
    ):
        s._maybe_note_new_participant("alice")
    assert s._shared_workstream is True
    recompose.assert_called_once()  # banner recomposed on the shared transition
    assert s.messages[-1].role is Role.SYSTEM
    assert s.messages[-1].source == "participant_joined"
    n = len(s.messages)
    # owner and a repeat participant are no-ops (no duplicate join note)
    s._maybe_note_new_participant("owner")
    s._maybe_note_new_participant("alice")
    assert len(s.messages) == n


def test_owner_only_never_shared():
    s = make_session(user_id="owner")
    with patch.object(s, "_init_system_messages") as recompose:
        s._maybe_note_new_participant("owner")
    assert s._shared_workstream is False
    recompose.assert_not_called()


# -- resume / fork carry attribution across the DB round-trip -----------------


def test_resume_resets_shared_state():
    # resume() can point this session object at a different workstream's
    # history; the monotonic shared-state guarantees are per workstream.
    s = make_session(user_id="owner")
    s._known_senders = {"alice"}
    s._shared_workstream = True
    turns = [turn_from_dict({"role": "user", "content": "x", "_sender": "owner"})]
    with (
        patch("turnstone.core.session.load_message_turns", return_value=turns),
        patch("turnstone.core.session.get_storage", return_value=None),
        patch.object(s, "_reset_shared_state", wraps=s._reset_shared_state) as rst,
        patch.object(s, "_save_config"),
        patch.object(s, "_init_system_messages"),
    ):
        assert s.resume("ws-other") is True
    rst.assert_called_once()


def test_fork_persists_sender_meta():
    # The fork bulk-persist must carry the user-turn sender stamp into the
    # fork's rows (mirroring _append_user_turn), or the fork loses per-user
    # attribution the first time it is reopened from the DB.
    s = make_session(user_id="owner")
    turns = [
        turn_from_dict({"role": "user", "content": "hi", "_sender": "alice"}),
        turn_from_dict({"role": "user", "content": "wake", "_source": "wake"}),
        turn_from_dict({"role": "assistant", "content": "yo"}),
    ]
    with (
        patch("turnstone.core.session.load_message_turns", return_value=turns),
        patch("turnstone.core.session.save_messages_bulk") as bulk,
        patch("turnstone.core.session.get_storage", return_value=None),
        patch.object(s, "_save_config"),
        patch.object(s, "_init_system_messages"),
    ):
        assert s.resume("src-ws", fork=True) is True
    rows = bulk.call_args.args[0]
    by_content = {r["content"]: r for r in rows}
    assert json.loads(by_content["hi"]["meta"]) == {"sender": "alice"}
    assert by_content["wake"]["meta"] is None  # synthetic: no sender stamped
    assert by_content["yo"]["meta"] is None  # assistant rows carry no sender


# -- Session Context banner (shared vs single-user) ---------------------------


def test_shared_banner_declares_participants_and_tool_credentials():
    from turnstone.prompts import SessionContext, WorkstreamKind, _build_context

    shared = _build_context(
        SessionContext(current_datetime="t", timezone="UTC", username="owner@x", shared=True),
        WorkstreamKind.INTERACTIVE,
    )
    solo = _build_context(
        SessionContext(current_datetime="t", timezone="UTC", username="owner@x", shared=False),
        WorkstreamKind.INTERACTIVE,
    )
    # shared: multi-user framing + per-message tags + the tool-credential rule
    assert "SHARED workstream" in shared
    assert "message from" in shared
    assert "credentials of the participant" in shared
    assert "different results" in shared.lower() or "DIFFERENT results" in shared
    # single-user: unchanged simple owner line, no shared framing
    assert "- **User:** owner@x" in solo
    assert "SHARED" not in solo


# -- workstream / project identifiers in context ------------------------------


def test_context_surfaces_workstream_and_project_ids():
    from turnstone.prompts import SessionContext, WorkstreamKind, _build_context

    out = _build_context(
        SessionContext(
            current_datetime="t",
            timezone="UTC",
            username="owner@x",
            project="My Project",
            project_id="proj-123",
            ws_id="ws-abc",
        ),
        WorkstreamKind.INTERACTIVE,
    )
    assert "- **Workstream ID:** ws-abc" in out
    # project renders both its display name and its stable id
    assert "My Project" in out
    assert "proj-123" in out


def test_context_omits_ids_when_absent():
    from turnstone.prompts import SessionContext, WorkstreamKind, _build_context

    out = _build_context(
        SessionContext(current_datetime="t", timezone="UTC", username="owner@x"),
        WorkstreamKind.INTERACTIVE,
    )
    # no ws_id line and no project line at all when neither is set
    assert "Workstream ID" not in out
    assert "**Project:**" not in out
