"""Session-level tests for the mid-stream transport-retry loop (#937).

A wire death DURING body iteration surfaces after the request already
returned its stream handle, so neither the SDK's ``max_retries`` nor the
creation-time per-lane ladder (``_model_turn_with_retry``) ever sees it.
These tests drive ``ChatSession.send()`` with scripted provider streams
and pin the retry loop's contract: bounded re-issue on the normalized
retryable shape, the dead attempt finalized client-side before the retry
notice (``stream_end``) and discarded from the server buffers once the
backoff survives the Stop window (``stream_discarded`` — never
``turn_committed``, whose semantics keep the turn buffer),
cancel-in-retry-window abort, exhaustion surfacing the stream-death
wording, and the ``_assistant_pending_tokens`` reset that keeps a
post-finish blip from recycling the prior turn's token count.

Fixture note: tests zero ``_RETRY_BASE_DELAY`` per instance (else each
retry pays real exponential backoff).
"""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests._session_helpers import NullUI, RecordingUI, arm_session, make_session
from turnstone.core.memory import load_last_error
from turnstone.core.model_turn import WirePreparationError
from turnstone.core.providers import IncompleteStreamError, StreamChunk, UsageInfo
from turnstone.core.session import BackendAuthUnavailableError, GenerationCancelled
from turnstone.core.streaming_text import ThinkTagSplitter
from turnstone.core.trajectory import Turn, dicts_from_turns


def _make_session(ui=None, **kwargs):
    """Wrap the shared session factory with this suite's two retry knobs.

    The defaults live in tests/_session_helpers.make_session — duplicating
    them here is exactly the drift its docstring warns about.
    """
    session = make_session(ui=ui or RecordingUI(), **kwargs)
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
        create = arm_session(session, *streams).create_streaming
        with (
            caplog.at_level(logging.WARNING, logger="turnstone.core.session"),
        ):
            session.send("test")

        assert create.call_count == 2
        # ONE committed turn, carrying only the successful attempt's text.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Hello world"
        # The dead attempt's tokens streamed live (minus the trailing
        # MAX_TAG_LEN chars the tag-scan buffer held back — never
        # displayed, so nothing needs to finalize them); the retried text
        # is a fresh bubble, not an append onto the dead attempt's.
        flushed = first_text[: len(first_text) - ThinkTagSplitter.MAX_TAG_LEN]
        assert ui.of("content") == [flushed, "Hello world"]
        # The dead attempt is finalized client-side before the notice, and
        # DISCARDED only after the backoff survives the Stop window (a
        # Stop during backoff must find the dead text still in the turn
        # buffer, matching what the cancel handler persists): stream_end
        # -> notice -> (backoff) -> stream_discarded -> spinner, then the
        # retried stream's own end-of-turn pair (a real commit).
        seq = [
            (k, d)
            for k, d in ui.events
            if k in ("stream_end", "stream_discarded", "turn_committed", "thinking_start")
            or (k == "info" and "stream died mid-response" in d)
        ]
        assert [k for k, _ in seq] == [
            "thinking_start",
            "stream_end",
            "info",
            "stream_discarded",
            "thinking_start",
            "stream_end",
            "turn_committed",
        ]
        notice = seq[2][1]
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
        create = arm_session(session, *streams).create_streaming
        with (
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
        # Every dead attempt is finalized AND discarded — the terminal arm
        # included (keeping its buffers bought nothing: the fatal path's
        # error-state drain wipes them anyway, and an overflow recovered by
        # compact-and-retry would otherwise concatenate dead text into the
        # idle payload).  No commit ever happens.
        assert ui.kinds().count("stream_end") == 3
        assert ui.kinds().count("stream_discarded") == 3
        assert ui.kinds().count("turn_committed") == 0

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

        create = arm_session(
            session, _dying_stream("Hel", exc=httpx.ReadError("wire died"))
        ).create_streaming
        with (
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
        ):
            session.send("test")

        assert create.call_count == 1  # no further API call after the Stop
        assert ("state", "idle") in ui.events
        assert ("state", "error") not in ui.events
        assert any("cancelled" in d.lower() for d in ui.of("info"))
        # The dead attempt's partial survives the Stop with the cancel
        # marker — same disposition a cancel DURING the attempt gets.  "Hel"
        # is shorter than the splitter's carry window, so this also pins the
        # carry-tail inclusion in the stash.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Hel\n\n[generation cancelled before completion]"

    def test_rebind_reprepares_wire_messages(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        streams = [
            _dying_stream("x", exc=httpx.ReadError("wire died")),
            _good_stream("ok"),
        ]

        def swap_binding():
            # A registry reload that rebinds mid-retry replaces the client
            # object (the identity signal the wrapper keys on).
            session.client = MagicMock()

        create = arm_session(session, *streams).create_streaming
        with (
            patch.object(session, "_refresh_model_from_registry", side_effect=swap_binding),
            patch.object(
                session, "_prepare_wire_messages", wraps=session._prepare_wire_messages
            ) as prep,
        ):
            session.send("test")

        # send() prepares once; the rebind forces exactly one re-prepare
        # (capability-sensitive wire fold) before the re-issue.
        assert prep.call_count == 2
        assert create.call_count == 2
        assert _assistant_msgs(session)[-1]["content"] == "ok"

    def test_refresh_fires_per_reissue(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        streams = [
            _dying_stream("x", exc=httpx.ReadError("wire died")),
            _dying_stream("y", exc=httpx.ReadError("wire died again")),
            _good_stream("ok"),
        ]
        create = arm_session(session, *streams).create_streaming
        with (
            patch.object(
                session,
                "_refresh_model_from_registry",
                wraps=session._refresh_model_from_registry,
            ) as refresh,
        ):
            session.send("test")

        assert create.call_count == 3
        # Once at the top of send() (the per-send driver) plus once per
        # re-issue — a regression that drops the mid-retry refresh would
        # show 1 here and stream a reload-closed client (#937 recreate
        # hardening).
        assert refresh.call_count == 3

    def test_retry_window_restarts_spinner(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        streams = [
            _dying_stream(exc=httpx.ReadError("pre-token wire death")),
            _good_stream("ok"),
        ]
        arm_session(session, *streams)
        session.send("test")

        # A pre-first-token death leaves the spinner RUNNING; the CLI's
        # on_thinking_start is idempotent at the callee (it stops a live
        # spinner before replacing it), so the retry arm restarts with a
        # single call — after the backoff-gated discard.
        notice = [i for i, (k, d) in enumerate(ui.events) if k == "info" and "stream died" in d]
        assert notice
        after = [k for k, _ in ui.events[notice[0] + 1 : notice[0] + 3]]
        assert after == ["stream_discarded", "thinking_start"]

    def test_pretoken_death_stop_persists_marker_row(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        session._RETRY_BASE_DELAY = 30.0
        real_backoff = session._backoff_or_cancelled

        def cancel_then_backoff(delay, my_generation=0):
            session.cancel()
            real_backoff(delay, my_generation)

        arm_session(session, _dying_stream(exc=httpx.ReadError("died before first token")))
        with (
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
        ):
            session.send("test")

        # No content ever streamed, but a cancelled streaming turn must
        # still persist the marker row — an absent assistant row is the
        # ambiguous shape the marker branch exists to prevent.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "[generation cancelled before completion]"
        assert ("state", "idle") in ui.events

    def test_ttft_window_stop_backfills_previous_partial(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)

        def second_stream():
            # A Stop lands after the re-create, before the first chunk:
            # cancel() closes the fresh stream and the read dies.
            session.cancel()
            raise httpx.ReadError("closed by cancel")
            yield  # pragma: no cover — generator marker

        streams = [
            _dying_stream("Hel", exc=httpx.ReadError("wire died")),
            second_stream(),
        ]
        arm_session(session, *streams)
        session.send("test")

        # The replacement attempt recorded an EMPTY partial; the wrapper
        # backfills it with the previous attempt's text — the user saw it,
        # and a Stop one second earlier (during backoff) preserves it too.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Hel\n\n[generation cancelled before completion]"

    def test_trailing_window_stop_aborts_with_marker(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)

        def gen():
            yield StreamChunk(content_delta="done-ish")
            yield StreamChunk(finish_reason="stop")
            # Stop races the trailing-metadata window; the closed stream
            # ends cleanly under transport_guarded's post-finish tolerance.
            session.cancel()

        arm_session(session, gen())
        session.send("test")

        # The post-loop cancel re-check converts the Stop — the turn must
        # NOT commit as complete (its tool calls would execute despite the
        # Stop); it aborts with the marker like any observed cancel.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "done-ish\n\n[generation cancelled before completion]"
        assert ("state", "idle") in ui.events
        assert ("state", "error") not in ui.events

    def test_keyboard_interrupt_finalizes_dead_attempt(self, tmp_db, caplog):
        ui = RecordingUI()
        session = _make_session(ui)
        arm_session(session, _dying_stream("Hel", exc=KeyboardInterrupt()))
        with (
            caplog.at_level(logging.INFO, logger="turnstone.core.session"),
            pytest.raises(KeyboardInterrupt),
        ):
            session.send("test")

        # Ctrl-C is BaseException — the retry gate never sees it, but the
        # dead attempt still needs its client-side finalize (the CLI's
        # markdown fence resets only in on_stream_end).
        assert ui.kinds().count("stream_end") == 1
        assert ("state", "error") in ui.events

    def test_gate_consults_serving_lane_provider(self, tmp_db):
        class FlakyError(Exception):
            pass

        ui = RecordingUI()
        session = _make_session(ui)
        # The retry gate reads the SERVING lane's provider.  FlakyError is
        # retryable only per THIS provider's set, so the re-issue proves
        # the gate consulted the lane that armed the stream, not a global
        # default.  (The distinct-fallback-lane variant rides the real
        # walk in test_model_registry's TestSessionFallback.)
        provider = arm_session(
            session,
            _dying_stream("x", exc=FlakyError("in-band transient")),
            _good_stream("ok"),
            retryable=frozenset({"FlakyError"}),
        )
        session.send("test")

        assert provider.create_streaming.call_count == 2
        assert _assistant_msgs(session)[-1]["content"] == "ok"

    def test_recreate_overflow_falls_through_to_compact_retry(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        provider = arm_session(
            session,
            _dying_stream("x", exc=httpx.ReadError("wire died")),
            # A mid-retry rebind landed on a smaller-window model: the
            # re-create overflows deterministically (a creation-phase
            # raise — unarmed).
            RuntimeError("maximum context length exceeded"),
            _good_stream("recovered"),
        )
        with (
            patch.object(session, "_compact_messages") as compact,
        ):
            session.send("test")

        # The overflow surfaced as ITSELF (not masked behind the stream
        # death), so send()'s compact-and-retry arm recovered the turn.
        assert provider.create_streaming.call_count == 3
        compact.assert_called_once()
        assert _assistant_msgs(session)[-1]["content"] == "recovered"

    def test_postcompaction_failure_surfaces_as_itself(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        arm_session(
            session,
            RuntimeError("maximum context length exceeded"),
            ValueError("backend exploded"),
        )
        with (
            patch.object(session, "_compact_messages"),
            pytest.raises(ValueError, match="backend exploded"),
        ):
            session.send("test")

        # Compaction already succeeded — the post-compaction failure must
        # not be re-labeled as a context overflow.
        persisted = load_last_error(session._ws_id)
        assert persisted and "ValueError" in persisted
        assert "Context window exceeded" not in persisted

    def test_model_only_rebind_reprepares(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        streams = [
            _dying_stream("x", exc=httpx.ReadError("wire died")),
            _good_stream("ok"),
        ]

        refreshes = {"n": 0}

        def swap_model():
            # reload() keeps the pooled client when only the alias's model
            # id changed — the binding triple must still trigger the
            # re-prepare.  The swap fires on the RETRY's refresh (call 2);
            # call 1 is send()'s per-send driver at the top of the turn.
            refreshes["n"] += 1
            if refreshes["n"] == 2:
                session.model = "swapped-model"

        arm_session(session, *streams)
        with (
            patch.object(session, "_refresh_model_from_registry", side_effect=swap_model),
            patch.object(
                session, "_prepare_wire_messages", wraps=session._prepare_wire_messages
            ) as prep,
        ):
            session.send("test")

        assert prep.call_count == 2

    def test_orphan_attempt_touches_neither_ui_nor_partial_slot(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        session._generation = 7  # a successor generation owns the session

        def chunks():
            yield StreamChunk(content_delta="orphan text")
            yield StreamChunk(content_delta="more")

        arm_session(session, chunks())
        with pytest.raises(GenerationCancelled):
            session._stream_response(3)

        # The superseded thread's cancel arm must touch neither the UI
        # (its stream_end would reset the successor's inflight buffers)
        # nor the shared partial slot the successor's handler consumes.
        assert "stream_end" not in ui.kinds()
        assert session._cancelled_partial_msg is None

    def test_superseded_backoff_cancel_does_not_write_partial_slot(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)

        def supersede_then_cancel(delay, my_generation=0):
            session._generation += 1  # force-cancel claimed a successor
            raise GenerationCancelled()

        arm_session(session, _dying_stream("Hel", exc=httpx.ReadError("wire died")))
        with (
            patch.object(session, "_backoff_or_cancelled", side_effect=supersede_then_cancel),
        ):
            session.send("test")  # the orphaned turn returns silently

        # An orphan waking in the backoff window must not write the
        # successor's partial slot or persist anything.
        assert session._cancelled_partial_msg is None
        assert not _assistant_msgs(session)

    def test_dead_attempt_text_absent_from_turn_buffer(self, tmp_db):
        # Real SessionUIBase buffers — a recording fake cannot see the
        # multi-segment turn buffer the IDLE payload drains, which
        # on_turn_committed deliberately does NOT clear.  on_state_change
        # is a web-fanout concern narrowed off the shared base.
        class _BufferUI(NullUI):
            def on_state_change(self, state):
                pass

        ui = _BufferUI()
        session = _make_session(ui)
        streams = [
            _dying_stream(
                "dead attempt text that must not surface",
                exc=httpx.ReadError("wire died"),
            ),
            _good_stream("final answer"),
        ]
        arm_session(session, *streams)
        session.send("test")

        # The dead segment was truncated at the watermark; only the
        # retried attempt's text reaches the IDLE payload.
        assert "".join(ui._ws_turn_content) == "final answer"

    def test_stop_in_backoff_keeps_dead_text_in_turn_buffer(self, tmp_db):
        # The discard is gated on the backoff SURVIVING: a Stop during the
        # window persists the promoted partial to history, and the idle
        # payload (drained from the turn buffer) must carry the same text
        # — discarding first rendered the cancelled turn empty on the
        # dashboard while the transcript had it.
        class _BufferUI(NullUI):
            def on_state_change(self, state):
                pass

        ui = _BufferUI()
        session = _make_session(ui)
        session._RETRY_BASE_DELAY = 30.0
        real_backoff = session._backoff_or_cancelled

        def cancel_then_backoff(delay, my_generation=0):
            session.cancel()
            real_backoff(delay, my_generation)

        dead_text = "a dead attempt long enough to flush past the carry window"
        arm_session(session, _dying_stream(dead_text, exc=httpx.ReadError("wire died")))
        with (
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
        ):
            session.send("test")

        assert "a dead attempt" in "".join(ui._ws_turn_content)
        assistant = _assistant_msgs(session)
        assert assistant and assistant[-1]["content"].startswith("a dead attempt")

    def test_pre937_ui_without_discard_hook_survives_retry(self, tmp_db):
        # A duck-typed UI predating on_stream_discarded must degrade to
        # "no server-buffer truncate", not crash the retry arm with an
        # AttributeError that replaces the stream death being handled.
        class _Pre937UI(RecordingUI):
            # property() with no getter raises AttributeError on access —
            # simulating the hook's absence on an inheriting fake.
            on_stream_discarded = property()

        ui = _Pre937UI()
        session = _make_session(ui)
        streams = [
            _dying_stream("x", exc=httpx.ReadError("wire died")),
            _good_stream("ok"),
        ]
        arm_session(session, *streams)
        session.send("test")

        assert _assistant_msgs(session)[-1]["content"] == "ok"
        assert ("state", "idle") in ui.events

    def test_overflow_recovery_discards_dead_text_from_turn_buffer(self, tmp_db):
        # A mid-consumption overflow is TERMINAL for the retry ladder but
        # RECOVERED by send()'s compact-and-retry — the dead attempt's text
        # must not concatenate with the recovered answer in the idle
        # payload (the terminal arm discards, same as the retry arm).
        class _BufferUI(NullUI):
            def on_state_change(self, state):
                pass

        ui = _BufferUI()
        session = _make_session(ui)
        provider = arm_session(
            session,
            _dying_stream(
                "dead overflow text",
                exc=RuntimeError("maximum context length exceeded"),
            ),
            _good_stream("recovered"),
        )
        with (
            patch.object(session, "_compact_messages"),
        ):
            session.send("test")

        assert provider.create_streaming.call_count == 2
        assert "".join(ui._ws_turn_content) == "recovered"

    def test_orphan_death_records_no_fatal_over_successor(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)

        def dying_superseded():
            yield StreamChunk(content_delta="Hel")
            # A force-cancel claims a successor generation before the
            # orphan's death propagates (its cancel event was replaced, so
            # cancel conversion cannot fire).
            session._generation += 1
            raise ValueError("orphan death")

        arm_session(session, dying_superseded())
        # RULED (#832, orphan-exit): a superseded generation's death
        # converts at the ladder's generation check and send() ends
        # SILENTLY as cancelled — no arbitrary exception class escapes
        # into the thread runner.
        session.send("test")

        # The orphan must not flash an error banner over the live
        # successor turn or persist a wrong last_error for the coord.
        assert ("state", "error") not in ui.events
        assert not ui.of("error")
        assert not load_last_error(session._ws_id)
        assert not _assistant_msgs(session)

    def test_non_retryable_error_is_immediately_fatal(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        create = arm_session(
            session, _dying_stream("Hel", exc=ValueError("model exploded"))
        ).create_streaming
        with (
            pytest.raises(ValueError, match="model exploded"),
        ):
            session.send("test")

        assert create.call_count == 1  # zero retries
        assert ("state", "error") in ui.events
        assert not any("stream died mid-response" in d for d in ui.of("info"))
        # The terminal arm finalizes and discards even a zero-retry death.
        assert ui.kinds().count("stream_end") == 1
        assert ui.kinds().count("stream_discarded") == 1
        assert ui.kinds().count("turn_committed") == 0

    def test_recreate_failure_surfaces_original_stream_death(self, tmp_db, caplog):
        """A failing re-create (closed client, registry chaos) must not
        replace the operator-actionable stream-death error with its own."""
        ui = RecordingUI()
        session = _make_session(ui)
        create = arm_session(
            session,
            _dying_stream("He", exc=httpx.ReadError("[SSL] record layer failure")),
            RuntimeError("Cannot send a request, as the client has been closed."),
        ).create_streaming
        with (
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

        arm_session(session, blipping())
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


class TestRecreateWindowClassification:
    """Between a mid-stream death and the next attempt's ``begin_attempt``
    there is NO live attempt — ``end_attempt`` pronounces the dead one
    dead the moment its partial is captured.  These pins hold the two
    failure modes of reading the dead attempt's armed state in that
    window (a Stop re-finalizing discarded display state; a walk-preamble
    error replacing the stream death), the two error classes the
    re-issue mask must forward verbatim, and the saw-chunk classifier
    fallback for adapters that never arm."""

    def test_stop_in_recreate_window_emits_no_stale_display_state(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        arm_session(
            session,
            _dying_stream("Half an answer", exc=httpx.ReadError("wire died")),
            _good_stream("never reached"),
        )
        # The Stop lands AFTER backoff+discard, BEFORE the next attempt
        # arms: _refresh_model_from_registry is the last step of the
        # re-create sequence, so a cancel fired there raises at the next
        # walk's loop-top _check_cancelled — the exact window.
        real_refresh = session._refresh_model_from_registry

        def cancel_in_window():
            real_refresh()
            session._cancel_event.set()

        with patch.object(session, "_refresh_model_from_registry", side_effect=cancel_in_window):
            session.send("test")

        # The dead attempt streamed only its safe-flush prefix ("Ha");
        # the splitter carry ("lf an answer") must NEVER surface as a late
        # content token behind a duplicate stream_end.
        assert ui.of("content") == ["Ha"]
        assert ui.kinds().count("stream_end") == 1
        # The full partial (flushed + carry) still reaches history via
        # the promotion path — display suppression must not cost text.
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert (
            assistant[0]["content"] == "Half an answer\n\n[generation cancelled before completion]"
        )
        assert ("state", "idle") in ui.events
        assert ("state", "error") not in ui.events

    def test_recreate_preamble_error_surfaces_original_death(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        arm_session(
            session,
            _dying_stream("text", exc=httpx.ReadError("wire died")),
            _good_stream("never reached"),
        )
        # The re-issue walk's PREAMBLE (before any begin_attempt) raises:
        # with the dead attempt's ref retired this is a creation-phase
        # failure, so the ORIGINAL stream death is the error the operator
        # sees — not the preamble's.
        real_tracker = session._get_health_tracker
        calls: list[int] = []

        def tracker_then_boom():
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError("registry blew up mid-rebind")
            return real_tracker()

        with (
            patch.object(session, "_get_health_tracker", side_effect=tracker_then_boom),
            pytest.raises(IncompleteStreamError, match="ReadError"),
        ):
            session.send("test")

        errors = ui.of("error")
        assert errors and "Backend stream died mid-response" in errors[-1]
        assert not any("registry blew up" in e for e in errors)

    def test_auth_refusal_at_reissue_surfaces_as_itself(self, tmp_db):
        # An OBO mint refusal during the re-create is a config outage
        # with its own remediation branch — masking it behind the earlier
        # transport death would misdiagnose it as a network flap.
        ui = RecordingUI()
        session = _make_session(ui)
        arm_session(
            session,
            _dying_stream("text", exc=httpx.ReadError("wire died")),
            BackendAuthUnavailableError("obo mint refused for alias 'gpt': no cached grant"),
        )
        with pytest.raises(BackendAuthUnavailableError):
            session.send("test")

        errors = ui.of("error")
        assert errors and "configured to mint a credential per call" in errors[-1]
        assert not any("Backend stream died mid-response" in e for e in errors)

    def test_never_arming_adapter_death_classifies_midstream(self, tmp_db):
        """An adapter that ignores ``cancel_ref`` forfeits the
        health/usage hook duties, but its mid-stream death must STILL
        classify as mid-stream: chunks reached the display, so a
        creation-classified death would silently re-issue the same lane
        and double-render them."""
        ui = RecordingUI()
        session = _make_session(ui)
        provider = arm_session(session)  # install the provider shell only
        scripts = [
            _dying_stream("First words from the dying wire", exc=httpx.ReadError("wire died")),
            _good_stream("Recovered"),
        ]

        def create_no_arm(**kwargs):
            assert scripts, "script exhausted"
            return scripts.pop(0)

        provider.create_streaming = MagicMock(side_effect=create_no_arm)
        session.send("test")

        # The mid-stream ladder ran its full theater — finalize, notice,
        # discard — and the retried attempt committed ALONE.
        assert any("stream died mid-response" in d for d in ui.of("info"))
        assert ui.kinds().count("stream_discarded") == 1
        assistant = _assistant_msgs(session)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Recovered"
        # The dead attempt's flushed prefix rendered exactly once.
        flushed = "First words from the dying wire"[: -ThinkTagSplitter.MAX_TAG_LEN]
        assert ui.of("content").count(flushed) == 1

    def test_wire_preparation_failure_never_touches_backend_health(self, tmp_db):
        """A lowering failure is a session-data fault: no dispatch and no
        health record on ANY lane — but it DOES walk the fallbacks, since
        prepare is lane-variant and another lane's posture may serve the
        turn.  When none does, the typed ``WirePreparationError`` rides to
        the fatal formatter's dedicated branch."""
        ui = RecordingUI()
        session = _make_session(ui)
        provider = arm_session(session, _good_stream("unreached"))
        tracker = MagicMock()
        registry = MagicMock()
        registry.fallback = ["fb1"]
        session._registry = registry
        with (
            patch.object(session, "_get_health_tracker", return_value=tracker),
            patch.object(session, "_try_fallback_lane", return_value=None) as fb_spy,
            patch.object(
                session, "_prepare_wire_messages", side_effect=ValueError("malformed turn 7")
            ),
            pytest.raises(WirePreparationError),
        ):
            session.send("test")

        tracker.record_failure.assert_not_called()
        fb_spy.assert_called_once()
        provider.create_streaming.assert_not_called()
        errors = ui.of("error")
        assert errors and "stored history" in errors[-1]
        # Reached the dedicated branch — identified by the cause's CLASS.
        # Its message is withheld all the way through the live send path:
        # it is our lowering's text over stored history, and this string
        # is persisted (see TestWirePrepFaultRedaction).
        assert "ValueError" in errors[-1]
        assert "malformed turn 7" not in errors[-1]

    def test_fallback_prep_fault_continues_walk(self, tmp_db):
        """A prep fault on one lane must not abort the walk: the next
        alias may still serve the turn (prepare is lane-variant)."""
        from tests._session_helpers import make_result

        session = _make_session(RecordingUI())
        registry = MagicMock()
        registry.fallback = ["a", "b"]
        session._registry = registry
        served = make_result(content="ok")
        tracker = MagicMock()
        with (
            patch.object(session, "_get_health_tracker", return_value=tracker),
            patch.object(
                session,
                "_model_turn_with_retry",
                side_effect=WirePreparationError("primary prep fault"),
            ),
            patch.object(session, "_try_fallback_lane", side_effect=[None, served]) as fb,
        ):
            from turnstone.core.session import _StreamTurnConsumer

            consumer = _StreamTurnConsumer(session, 0)
            result = session._model_turn_with_fallback(consumer, lambda w, lane: w, 0)

        assert result is served
        assert fb.call_count == 2
        tracker.record_failure.assert_not_called()

    def test_fallback_prep_fault_records_no_health(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        registry = MagicMock()
        registry.resolve_binding.return_value = (MagicMock(), "m", None, MagicMock(), None)
        fb_tracker = MagicMock()
        session._registry = registry
        session._health_registry = MagicMock()
        session._health_registry.get_tracker_for_alias.return_value = fb_tracker
        with (
            patch.object(session, "_build_main_lane", return_value=MagicMock()),
            patch.object(
                session,
                "_model_turn_with_retry",
                side_effect=WirePreparationError("fold blew up"),
            ),
        ):
            from turnstone.core.session import _StreamTurnConsumer

            consumer = _StreamTurnConsumer(session, 0)
            out = session._try_fallback_lane("fb", consumer, lambda w, lane: w, 0)

        assert out is None
        fb_tracker.record_failure.assert_not_called()
        assert any("Fallback fb also failed: WirePreparationError" in i for i in ui.of("info"))


class TestDebugDumpLatch:
    """The debug request dump prints once per ``_stream_response``
    invocation.  RULED (#832): send()'s overflow-recovery re-invocation
    prints the RE-PREPARED wire — the dump that diagnoses the recovery —
    where the pre-fold behavior printed only the first.  Within one
    invocation, re-issues and fallback lanes re-run the passes but never
    re-dump."""

    def test_reissue_never_redumps(self, tmp_db):
        session = _make_session(RecordingUI())
        session.debug = True
        session.messages.append(Turn.user("hi"))
        arm_session(
            session,
            _dying_stream("x" * 20, exc=httpx.ReadError("wire died")),
            _good_stream("ok"),
        )
        with patch.object(session, "_debug_print_request") as dump:
            session._stream_response(0)
        assert dump.call_count == 1

    def test_second_invocation_reprints(self, tmp_db):
        session = _make_session(RecordingUI())
        session.debug = True
        session.messages.append(Turn.user("hi"))
        arm_session(session, _good_stream("a"), _good_stream("b"))
        with patch.object(session, "_debug_print_request") as dump:
            session._stream_response(0)
            session._stream_response(0)
        assert dump.call_count == 2


class TestFallbackFailureRedaction:
    def test_fallback_ui_line_carries_class_name_only(self, tmp_db):
        """The fallback-failure info line lands in the browser transcript
        and persisted event stream — it carries the exception CLASS, never
        its text (a ConnectError's str can embed a credential-bearing
        base_url)."""
        ui = RecordingUI()
        session = _make_session(ui)
        registry = MagicMock()
        registry.resolve_binding.side_effect = httpx.ConnectError(
            "dial http://user:SECRETKEY@gw.example/v1 failed"
        )
        session._registry = registry
        from turnstone.core.session import _StreamTurnConsumer

        consumer = _StreamTurnConsumer(session, 0)
        result = session._try_fallback_lane("fb", consumer, lambda w, lane: w, 0)

        assert result is None
        infos = ui.of("info")
        assert any("Fallback fb also failed: ConnectError" in i for i in infos)
        assert not any("SECRETKEY" in i for i in infos)


class TestWirePrepFaultRedaction:
    def test_operator_text_carries_the_cause_class_only(self, tmp_db):
        """A wire-prep fault renders its cause's CLASS, never its message.

        Every other branch of the formatter tails the backend's own
        diagnostic text, which is what the operator needs.  This one is
        different in kind: ``prepare_wire`` is our lowering over the
        session's STORED HISTORY, so its exception message can quote that
        history — and this string is both shown to the operator and
        persisted to ``last_error``, which a coordinating agent reads.
        ``redact_credentials`` is a best-effort regex by its own
        docstring, so it is no floor for arbitrary conversation text.
        """
        from turnstone.core.model_turn import WirePreparationError

        session = _make_session(RecordingUI())
        cause = ValueError("malformed block in turn 4: {'text': 'the user's private notes'}")
        exc = WirePreparationError(str(cause))
        exc.__cause__ = cause

        rendered = session._format_backend_error(exc)

        assert rendered is not None
        assert "ValueError" in rendered
        assert "private notes" not in rendered
        assert "malformed block" not in rendered
        # Still actionable: the operator learns what failed and what to try.
        assert "stored history" in rendered
        assert "/compact" in rendered


class TestPrepareWireLaneCaps:
    """The per-attempt wire prep folds with the SERVING lane's
    capabilities — a fallback whose template rejects mid-conversation
    system roles gets the folded shape even when the primary keeps them
    inline."""

    def test_caps_override_controls_fold_posture(self, tmp_db):
        from turnstone.core.providers import ModelCapabilities

        session = _make_session(RecordingUI())
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
            {"role": "system", "content": "nudge", "_source": "idle_nudge"},
            {"role": "user", "content": "next"},
        ]
        native = session._prepare_wire_messages(
            list(msgs), caps=ModelCapabilities(supports_mid_conversation_system=True)
        )
        folded = session._prepare_wire_messages(
            list(msgs), caps=ModelCapabilities(supports_mid_conversation_system=False)
        )
        assert any(m["role"] == "system" for m in native[1:])
        assert not any(m["role"] == "system" for m in folded[1:])

    def test_stream_prepare_passes_serving_lane_caps(self, tmp_db):
        session = _make_session(RecordingUI())
        arm_session(session, _good_stream("ok"))
        with patch.object(
            session, "_prepare_wire_messages", wraps=session._prepare_wire_messages
        ) as prep:
            session.send("test")
        assert prep.call_args.kwargs["caps"] is session._get_capabilities()
