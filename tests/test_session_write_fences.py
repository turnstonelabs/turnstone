"""Every kind of write a leased session makes presents its owner-lease fence.

A session hosted by a manager holds the live lease on its own row, so a write
that forgot its fence goes out unfenced and storage refuses it (the lease is
live): the write never lands, or the journal stops the session as if another
process owned the workstream. Each test drives one real write path and checks
that the write landed and the session kept running.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests._session_helpers import arm_session, replace_session_lane, scripted_provider
from tests.test_lease_bookkeeping import _host
from turnstone.core.memory import LAST_ERROR_CONFIG_KEY
from turnstone.core.providers import StreamChunk, ToolCallDelta
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.session import GenerationCancelled
from turnstone.core.trajectory import turns_from_dicts


def _hosted(storage: Any) -> Any:
    mgr, _ = _host(storage)
    return mgr.create(user_id="u1").session


def _assert_running(session: Any) -> None:
    assert not session._workstream_lease_lost
    assert not session.is_workstream_gone()


def test_tool_rows_land(storage_backend: Any) -> None:
    session = _hosted(storage_backend)

    def stream_with_tool() -> Any:
        yield StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="tc_1", name="bash")])
        yield StreamChunk(
            tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"command":"echo hi"}')],
            finish_reason="tool_calls",
        )

    def cancel_before_execute(tool_calls: Any, **_kwargs: Any) -> Any:
        session.cancel()
        raise GenerationCancelled()

    arm_session(session, stream_with_tool())
    with patch.object(session, "_execute_tools", side_effect=cancel_before_execute):
        session.send("run something")

    roles = [row["role"] for row in storage_backend.load_messages(session._ws_id)]
    assert "tool" in roles
    _assert_running(session)


def test_a_generated_title_lands(storage_backend: Any) -> None:
    session = _hosted(storage_backend)
    session.messages = turns_from_dicts(
        [{"role": "user", "content": "Fix the build"}, {"role": "assistant", "content": "On it"}]
    )
    replace_session_lane(
        session,
        provider=scripted_provider([StreamChunk(content_delta="Build Fix", finish_reason="stop")]),
        capabilities=ModelCapabilities(),
    )

    session._generate_title()

    assert storage_backend.get_workstream(session._ws_id)["title"] == "Build Fix"
    _assert_running(session)


def test_a_name_lands(storage_backend: Any) -> None:
    session = _hosted(storage_backend)

    session.handle_command("/name fenced-alias")

    assert storage_backend.get_workstream(session._ws_id)["alias"] == "fenced-alias"
    _assert_running(session)


def test_a_compaction_marker_lands(storage_backend: Any) -> None:
    session = _hosted(storage_backend)
    arm_session(session, iter([StreamChunk(content_delta="did the thing", finish_reason="stop")]))
    session.send("do the thing")
    summary = SimpleNamespace(content="dense", finish_reason="stop", producer="summary-producer")

    with patch.object(session, "_utility_completion", return_value=summary):
        assert session.compact_now() is True

    assert storage_backend.get_compaction_floor(session._ws_id) > 0
    _assert_running(session)


def test_a_rewind_truncates_durably(storage_backend: Any) -> None:
    session = _hosted(storage_backend)
    arm_session(
        session,
        iter([StreamChunk(content_delta="first", finish_reason="stop")]),
        iter([StreamChunk(content_delta="second", finish_reason="stop")]),
    )
    session.send("one")
    session.send("two")
    before = len(storage_backend.load_messages(session._ws_id))

    assert session.rewind(1) == 2

    assert len(storage_backend.load_messages(session._ws_id)) == before - 2
    _assert_running(session)


def test_a_fatal_error_and_its_recovery_land(storage_backend: Any) -> None:
    session = _hosted(storage_backend)

    session._record_fatal_error(RuntimeError("the provider fell over"))
    config = storage_backend.load_workstream_config(session._ws_id)
    assert config.get(LAST_ERROR_CONFIG_KEY)

    arm_session(session, iter([StreamChunk(content_delta="back", finish_reason="stop")]))
    session.send("try again")

    assert not storage_backend.load_workstream_config(session._ws_id).get(LAST_ERROR_CONFIG_KEY)
    _assert_running(session)


def test_a_user_row_with_an_attachment_lands(storage_backend: Any) -> None:
    from turnstone.core.attachments import Attachment

    session = _hosted(storage_backend)
    arm_session(session, iter([StreamChunk(content_delta="read it", finish_reason="stop")]))

    session.send(
        "look at this",
        attachments=[Attachment("a1", "note.md", "text/markdown", "text", b"# notes")],
    )

    rows = storage_backend.load_messages(session._ws_id)
    assert [row["role"] for row in rows][:2] == ["user", "assistant"]
    _assert_running(session)


def test_a_tool_row_with_an_attachment_lands(storage_backend: Any) -> None:
    from turnstone.core.storage import AttachmentWrite

    session = _hosted(storage_backend)
    persist = session._tool_row_persist_closure(
        ws_id=session._ws_id,
        lease=session.write_fence(),
        text="here is the chart",
        tool_name="render",
        call_id="tc_1",
        is_error=False,
        meta_json=None,
        attachments=(
            AttachmentWrite(
                attachment_id="att-1",
                filename="chart.txt",
                mime_type="text/plain",
                size_bytes=5,
                kind="text",
                content=b"chart",
            ),
        ),
        commit_key="tool-row-1",
        event_id_ref=[None],
    )

    assert persist() > 0
    assert [row["role"] for row in storage_backend.load_messages(session._ws_id)] == ["tool"]
    _assert_running(session)


def test_a_cancelled_reply_lands(storage_backend: Any) -> None:
    session = _hosted(storage_backend)

    def stream() -> Any:
        yield StreamChunk(content_delta="partial answer")
        yield StreamChunk(finish_reason="stop")
        session.cancel()

    arm_session(session, stream())
    session.send("start something")

    contents = [row["content"] for row in storage_backend.load_messages(session._ws_id)]
    assert any("[generation cancelled before completion]" in (c or "") for c in contents)
    _assert_running(session)
