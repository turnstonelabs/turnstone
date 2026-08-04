"""Session-level tests for the mid-stream transport-retry loop (#937).

A wire death DURING body iteration surfaces after the request already
returned its stream handle, so neither the SDK's ``max_retries`` nor the
creation-time ``_try_stream`` ladder ever sees it.  These tests drive
``ChatSession.send()`` with scripted provider streams and pin the retry
loop's contract: bounded re-issue on the normalized retryable shape, the
dead attempt finalized in every UI consumer (``stream_end`` +
``turn_committed`` BEFORE the retry notice), cancel-during-backoff
abort, exhaustion surfacing the stream-death wording, and the
``_assistant_pending_tokens`` reset that keeps a post-finish blip from
recycling the prior turn's token count.

Fixture note: tests zero ``_RETRY_BASE_DELAY`` per instance (else each
retry pays real exponential backoff).
"""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from turnstone.core.memory import load_last_error
from turnstone.core.providers import StreamChunk, UsageInfo
from turnstone.core.providers._protocol import IncompleteStreamError
from turnstone.core.session import ChatSession
from turnstone.core.trajectory import dicts_from_turns


class RecordingUI:
    """UI adapter recording the ordered event stream ``send()`` emits."""

    def __init__(self):
        self.events = []

    def _rec(self, kind, detail=""):
        self.events.append((kind, detail))

    def on_turn_start(self):
        self._rec("turn_start")

    def on_turn_committed(self):
        self._rec("turn_committed")

    def on_thinking_start(self):
        self._rec("thinking_start")

    def on_thinking_stop(self):
        self._rec("thinking_stop")

    def on_reasoning_token(self, text):
        self._rec("reasoning", text)

    def on_content_token(self, text):
        self._rec("content", text)

    def on_stream_end(self):
        self._rec("stream_end")

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output, **kwargs):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_info(self, message):
        self._rec("info", message)

    def on_error(self, message):
        self._rec("error", message)

    def on_state_change(self, state):
        self._rec("state", state)

    def on_rename(self, name):
        pass

    def on_output_warning(self, call_id, assessment):
        pass

    def record_output_assessment(
        self,
        call_id,
        assessment,
        *,
        tier="heuristic",
        reasoning="",
        judge_model="",
        latency_ms=0,
        confidence=0.0,
    ):
        pass

    def kinds(self):
        return [k for k, _ in self.events]

    def of(self, kind):
        return [d for k, d in self.events if k == kind]


def _make_session(ui=None, **kwargs):
    """Helper to construct a ChatSession with minimal setup."""
    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=ui or RecordingUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    session = ChatSession(**defaults)
    session._RETRY_BASE_DELAY = 0
    # Latch the auto-title trigger: its background thread would drive a
    # second model call through the MagicMock client (drain retries, real
    # backoff) alongside the send under test.
    session._title_generated = True
    return session


def _dying_stream(*texts, exc):
    """Content chunks, then a mid-body death — no finish reason seen."""

    def gen():
        for t in texts:
            yield StreamChunk(content_delta=t)
        raise exc

    return gen()


def _good_stream(text):
    return iter(
        [
            StreamChunk(content_delta=text),
            StreamChunk(
                finish_reason="stop",
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            ),
        ]
    )


def _assistant_msgs(session):
    return [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]


class TestMidStreamRetry:
    def test_death_retries_and_commits_one_clean_turn(self, tmp_db, caplog):
        ui = RecordingUI()
        session = _make_session(ui)
        first_text = "streaming from the dead attempt"
        streams = [
            _dying_stream(first_text, exc=httpx.ReadError("[SSL] record layer failure")),
            _good_stream("Hello world"),
        ]
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams) as create,
            patch.object(session, "_full_messages", return_value=[]),
            caplog.at_level(logging.WARNING, logger="turnstone.core.session"),
        ):
            session.send("test")

        assert create.call_count == 2
        # ONE committed turn, carrying only the successful attempt's text.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Hello world"
        # The dead attempt's tokens streamed live (minus the trailing
        # _MAX_TAG_LEN chars the tag-scan buffer held back — never
        # displayed, so nothing needs to finalize them); the retried text
        # is a fresh bubble, not an append onto the dead attempt's.
        flushed = first_text[: len(first_text) - ChatSession._MAX_TAG_LEN]
        assert ui.of("content") == [flushed, "Hello world"]
        # The dead attempt is finalized EVERYWHERE before re-streaming:
        # stream_end (client finalize) -> turn_committed (server buffer
        # reset) -> notice -> spinner, then the retried stream's own
        # end-of-turn pair.
        seq = [
            (k, d)
            for k, d in ui.events
            if k in ("stream_end", "turn_committed", "thinking_start")
            or (k == "info" and "stream died mid-response" in d)
        ]
        assert [k for k, _ in seq] == [
            "thinking_start",
            "stream_end",
            "turn_committed",
            "info",
            "thinking_start",
            "stream_end",
            "turn_committed",
        ]
        notice = seq[3][1]
        assert "(ReadError)" in notice
        assert "(1/2)" in notice
        assert ("state", "idle") in ui.events
        assert not ui.of("error")
        assert any("stream.retry" in r.message for r in caplog.records)

    def test_exhaustion_surfaces_stream_death_wording(self, tmp_db, caplog):
        ui = RecordingUI()
        session = _make_session(ui)
        streams = [
            _dying_stream("a", exc=httpx.ReadError("[SSL] record layer failure")) for _ in range(3)
        ]
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams) as create,
            patch.object(session, "_full_messages", return_value=[]),
            caplog.at_level(logging.INFO, logger="turnstone.core.session"),
            pytest.raises(IncompleteStreamError, match="ReadError"),
        ):
            session.send("test")

        # Initial attempt + _MID_STREAM_RETRIES re-creates, then fatal.
        assert create.call_count == 1 + session._MID_STREAM_RETRIES
        assert ("state", "error") in ui.events
        errors = ui.of("error")
        assert errors and "Backend stream died mid-response" in errors[-1]
        assert "retries did not recover it" in errors[-1]
        persisted = load_last_error(session._ws_id)
        assert persisted and "Backend stream died mid-response" in persisted
        fatal = [r for r in caplog.records if "session.fatal.recorded" in r.message]
        assert fatal and any(r.levelno == logging.ERROR for r in fatal)

    def test_cancel_during_backoff_stops_without_recreate(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        session._RETRY_BASE_DELAY = 30.0
        real_backoff = session._backoff_or_cancelled

        def cancel_then_backoff(delay, my_generation=0):
            # A Stop landing during the backoff window: the event-wait
            # returns immediately instead of burning the 30s delay.
            session.cancel()
            real_backoff(delay, my_generation)

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream("Hel", exc=httpx.ReadError("wire died"))],
            ) as create,
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        assert create.call_count == 1  # no further API call after the Stop
        assert ("state", "idle") in ui.events
        assert ("state", "error") not in ui.events
        assert any("cancelled" in d.lower() for d in ui.of("info"))

    def test_non_retryable_error_is_immediately_fatal(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream("Hel", exc=ValueError("model exploded"))],
            ) as create,
            patch.object(session, "_full_messages", return_value=[]),
            pytest.raises(ValueError, match="model exploded"),
        ):
            session.send("test")

        assert create.call_count == 1  # zero retries
        assert ("state", "error") in ui.events
        assert not any("stream died mid-response" in d for d in ui.of("info"))

    def test_recreate_failure_surfaces_original_stream_death(self, tmp_db, caplog):
        """A failing re-create (closed client, registry chaos) must not
        replace the operator-actionable stream-death error with its own."""
        ui = RecordingUI()
        session = _make_session(ui)
        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[
                    _dying_stream("He", exc=httpx.ReadError("[SSL] record layer failure")),
                    RuntimeError("Cannot send a request, as the client has been closed."),
                ],
            ) as create,
            patch.object(session, "_full_messages", return_value=[]),
            caplog.at_level(logging.WARNING, logger="turnstone.core.session"),
            pytest.raises(IncompleteStreamError, match="ReadError"),
        ):
            session.send("test")

        assert create.call_count == 2
        assert any("stream.retry.recreate_failed" in r.message for r in caplog.records)
        errors = ui.of("error")
        assert errors and "Backend stream died mid-response" in errors[-1]
        assert "client has been closed" not in errors[-1]

    def test_post_finish_blip_appends_fresh_token_estimate(self, tmp_db):
        """The post-finish tolerance can end a turn with the trailing usage
        chunk lost; the estimate appended for that turn must be a fresh
        char-based one, never the PREVIOUS turn's completion count."""
        ui = RecordingUI()
        session = _make_session(ui)
        session._assistant_pending_tokens = 777  # stale prior-turn count

        def blipping():
            yield StreamChunk(content_delta="Hello world")
            yield StreamChunk(finish_reason="stop")
            raise httpx.ReadError("late blip")  # the usage chunk is lost

        with (
            patch.object(session, "_create_stream_with_retry", return_value=blipping()),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Hello world"
        # Clean end, no retry — the finish reason had already passed.
        assert not any("stream died mid-response" in d for d in ui.of("info"))
        assert ("state", "idle") in ui.events
        expected = max(1, int(session._msg_char_count(assistant[0]) / session._chars_per_token))
        assert session._msg_tokens[-1] == expected
        assert session._msg_tokens[-1] != 777
