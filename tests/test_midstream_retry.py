"""Session-level tests for the mid-stream transport-retry loop (#937).

A wire death DURING body iteration surfaces after the request already
returned its stream handle, so neither the SDK's ``max_retries`` nor the
creation-time ``_try_stream`` ladder ever sees it.  These tests drive
``ChatSession.send()`` with scripted provider streams and pin the retry
loop's contract: bounded re-issue on the normalized retryable shape, the
dead attempt finalized client-side before the retry notice
(``stream_end``) and discarded from the server buffers once the backoff
survives the Stop window (``stream_discarded`` — never
``turn_committed``, whose semantics keep the turn buffer), cancel-in-
retry-window abort, exhaustion surfacing the stream-death wording, and the
``_assistant_pending_tokens`` reset that keeps a post-finish blip from
recycling the prior turn's token count.

Fixture note: tests zero ``_RETRY_BASE_DELAY`` per instance (else each
retry pays real exponential backoff).
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests._session_helpers import NullUI, RecordingUI, make_session
from turnstone.core.memory import load_last_error
from turnstone.core.providers import IncompleteStreamError, StreamChunk, UsageInfo
from turnstone.core.session import GenerationCancelled
from turnstone.core.streaming_text import ThinkTagSplitter
from turnstone.core.trajectory import dicts_from_turns


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

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams) as create,
            patch.object(session, "_refresh_model_from_registry", side_effect=swap_binding),
            patch.object(session, "_full_messages", return_value=[]),
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
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams) as create,
            patch.object(session, "_full_messages", return_value=[]),
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
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams),
            patch.object(session, "_full_messages", return_value=[]),
        ):
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

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream(exc=httpx.ReadError("died before first token"))],
            ),
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
            patch.object(session, "_full_messages", return_value=[]),
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
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams),
            patch.object(session, "_full_messages", return_value=[]),
        ):
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

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=[gen()]),
            patch.object(session, "_full_messages", return_value=[]),
        ):
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
        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream("Hel", exc=KeyboardInterrupt())],
            ),
            patch.object(session, "_full_messages", return_value=[]),
            caplog.at_level(logging.INFO, logger="turnstone.core.session"),
            pytest.raises(KeyboardInterrupt),
        ):
            session.send("test")

        # Ctrl-C is BaseException — the retry gate never sees it, but the
        # dead attempt still needs its client-side finalize (the CLI's
        # markdown fence resets only in on_stream_end).
        assert ui.kinds().count("stream_end") == 1
        assert ("state", "error") in ui.events

    def test_gate_consults_live_stream_provider_not_primary(self, tmp_db):
        class FlakyError(Exception):
            pass

        ui = RecordingUI()
        session = _make_session(ui)
        live = SimpleNamespace(retryable_error_names=frozenset({"FlakyError"}))
        calls = {"n": 0}

        def create(msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                # What _try_stream records at creation: the provider that
                # owns the live stream (a fallback here — its retryable
                # set differs from the primary's).
                session._active_stream_provider = live
                return _dying_stream("x", exc=FlakyError("in-band transient"))
            return _good_stream("ok")

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=create),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # FlakyError is NOT in the primary provider's retryable set — the
        # re-issue proves the gate consulted the live stream's provider.
        assert calls["n"] == 2
        assert _assistant_msgs(session)[-1]["content"] == "ok"

    def test_recreate_overflow_falls_through_to_compact_retry(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        calls = {"n": 0}

        def create(msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _dying_stream("x", exc=httpx.ReadError("wire died"))
            if calls["n"] == 2:
                # A mid-retry rebind landed on a smaller-window model: the
                # re-create overflows deterministically.
                raise RuntimeError("maximum context length exceeded")
            return _good_stream("recovered")

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=create),
            patch.object(session, "_compact_messages") as compact,
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # The overflow surfaced as ITSELF (not masked behind the stream
        # death), so send()'s compact-and-retry arm recovered the turn.
        assert calls["n"] == 3
        compact.assert_called_once()
        assert _assistant_msgs(session)[-1]["content"] == "recovered"

    def test_postcompaction_failure_surfaces_as_itself(self, tmp_db):
        ui = RecordingUI()
        session = _make_session(ui)
        calls = {"n": 0}

        def create(msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("maximum context length exceeded")
            raise ValueError("backend exploded")

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=create),
            patch.object(session, "_compact_messages"),
            patch.object(session, "_full_messages", return_value=[]),
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

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams),
            patch.object(session, "_refresh_model_from_registry", side_effect=swap_model),
            patch.object(session, "_full_messages", return_value=[]),
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

        with pytest.raises(GenerationCancelled):
            session._stream_attempt(iter(chunks()), my_generation=3)

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

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream("Hel", exc=httpx.ReadError("wire died"))],
            ),
            patch.object(session, "_backoff_or_cancelled", side_effect=supersede_then_cancel),
            patch.object(session, "_full_messages", return_value=[]),
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
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams),
            patch.object(session, "_full_messages", return_value=[]),
        ):
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
        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                side_effect=[_dying_stream(dead_text, exc=httpx.ReadError("wire died"))],
            ),
            patch.object(session, "_backoff_or_cancelled", side_effect=cancel_then_backoff),
            patch.object(session, "_full_messages", return_value=[]),
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
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=streams),
            patch.object(session, "_full_messages", return_value=[]),
        ):
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
        calls = {"n": 0}

        def create(msgs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _dying_stream(
                    "dead overflow text",
                    exc=RuntimeError("maximum context length exceeded"),
                )
            return _good_stream("recovered")

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=create),
            patch.object(session, "_compact_messages"),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        assert calls["n"] == 2
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

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=[dying_superseded()]),
            patch.object(session, "_full_messages", return_value=[]),
            pytest.raises(ValueError, match="orphan death"),
        ):
            session.send("test")

        # The orphan must not flash an error banner over the live
        # successor turn or persist a wrong last_error for the coord.
        assert ("state", "error") not in ui.events
        assert not ui.of("error")
        assert not load_last_error(session._ws_id)

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
        # The terminal arm finalizes and discards even a zero-retry death.
        assert ui.kinds().count("stream_end") == 1
        assert ui.kinds().count("stream_discarded") == 1
        assert ui.kinds().count("turn_committed") == 0

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
