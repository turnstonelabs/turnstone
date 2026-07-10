"""Tests for the watch dispatch closure built inside ``set_watch_runner``.

The closure routes watch results onto the per-session :class:`NudgeQueue`
under the unified pull-model surface.  Each test focuses on one
assertion: enqueue shape, sanitisation, soft-cap drop-oldest,
``valid_until`` predicate, and concurrent-enqueue safety.

Tests in this file replace the pre-switchover suite that pinned the
``_make_watch_dispatch`` worker-spawn / ``_watch_pending`` machinery —
the contracts those tests pinned no longer exist.  See
``tests/test_watch.py`` for the still-relevant ``WatchRunner``
mechanics tests, and ``tests/test_watch_integration.py`` for the
boundary-crossing integration test covering the chat-loop drain.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._helpers import patch_session_storage
from turnstone.core.session import _WATCH_QUEUE_SOFT_CAP, ChatSession


class _NullUI:
    """UI adapter that discards all output — local to this test module
    to avoid a cross-test-file import (mirrors the pattern in
    test_session.py / test_rewind_retry.py).
    """

    def __getattr__(self, name: str) -> Any:
        # Catch-all: any UI hook the chat loop calls becomes a no-op.
        return MagicMock()


def _make_session_for_dispatch(**kwargs: Any) -> ChatSession:
    """ChatSession built with the same minimal harness used elsewhere
    in the test suite, scoped down to what the dispatch closure needs.
    """
    client = MagicMock()
    defaults = dict(
        client=client,
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _register_runner(session: ChatSession, wake_fn: Any = None) -> tuple[Any, Any]:
    """Attach a minimal stub ``WatchRunner`` to *session* and return the
    ``(runner, dispatch_fn)`` pair captured by ``set_dispatch_fn``.
    ``wake_fn`` rides through to ``set_watch_runner`` (default ``None``
    matches the pre-wake wiring most tests here exercise).
    """
    captured: dict[str, Any] = {}

    class _StubRunner:
        def set_dispatch_fn(self, ws_id: str, fn: Any) -> None:
            captured["fn"] = fn

    runner = _StubRunner()
    session.set_watch_runner(runner, wake_fn=wake_fn)
    return runner, captured["fn"]


def _reminder(text: str, **extra: Any) -> dict[str, Any]:
    """Build a structured ``watch_triggered`` reminder dict for tests.

    Mirrors the shape produced by :func:`turnstone.core.watch.build_watch_reminder`
    — ``text`` is the formatted body, optional fields ride alongside.
    Tests that don't care about the optional fields can call with
    ``text`` only.
    """
    out: dict[str, Any] = {"type": "watch_triggered", "text": text}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Enqueue shape
# ---------------------------------------------------------------------------


class TestEnqueueShape:
    """``set_watch_runner``'s closure produces a single
    ``("watch_triggered", text, "any")`` entry per fire.
    """

    def test_dispatch_enqueues_watch_triggered_with_any_channel(self, tmp_db):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        dispatch(_reminder("watch fired body"), "watch-1")

        # One entry, "watch_triggered" type, on "any" channel.
        assert len(session._nudge_queue) == 1
        assert session._nudge_queue.pending(channel="any") == [
            ("watch_triggered", "watch fired body")
        ]
        # NOT on "user" or "tool" channels.
        assert session._nudge_queue.pending(channel="user") == []
        assert session._nudge_queue.pending(channel="tool") == []


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


class TestSanitisation:
    """``sanitize_payload`` runs producer-side over the formatted message
    before it ever reaches the queue.  The wire-boundary fence escaping
    (``fence.neutralize`` at fold time) only defangs ``[start system-reminder]``
    markers; this producer layer covers everything else.
    """

    def test_dispatch_sanitizes_payload_before_enqueue(self, tmp_db):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Build a payload with: BEL (\x07), zero-width space (U+200B),
        # bidi RTL override (U+202E), and angle-bracket tag breakers.
        raw = "before\x07middle​after‮more<thinking>tail"
        dispatch(_reminder(raw), "watch-1")

        pending = session._nudge_queue.pending(channel="any")
        assert len(pending) == 1
        sanitized = pending[0][1]
        # Control / steering chars become spaces; angle brackets vanish.
        assert "\x07" not in sanitized
        assert "​" not in sanitized
        assert "‮" not in sanitized
        assert "<" not in sanitized
        assert ">" not in sanitized
        # Real content survives.
        assert "before" in sanitized
        assert "thinking" in sanitized

    def test_dispatch_preserves_newlines_for_multiline_output(self, tmp_db):
        """Multi-line shell output must keep its layout — TAB / LF / CR
        are intentionally preserved by ``sanitize_payload`` (R8).
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        dispatch(_reminder("line1\nline2\n\tindented\n\rline3"), "watch-1")

        pending = session._nudge_queue.pending(channel="any")
        assert len(pending) == 1
        text = pending[0][1]
        # Lines stay separated; tab kept.
        assert "\n" in text
        assert "\t" in text

    def test_dispatch_drops_empty_after_sanitization(self, tmp_db):
        """A payload that's all control chars sanitises to "" — no enqueue."""
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # All-control + DEL + zero-width — strips to empty.
        dispatch(_reminder("\x07\x0b\x7f​"), "watch-1")

        assert len(session._nudge_queue) == 0


# ---------------------------------------------------------------------------
# Soft cap
# ---------------------------------------------------------------------------


class TestSoftCap:
    """When ``"watch_triggered"`` saturates at :data:`_WATCH_QUEUE_SOFT_CAP`,
    the closure drops the OLDEST entry of that type and enqueues the new
    one — so the queue stays ≤ cap with the most recent watch outputs.
    """

    def test_dispatch_drop_oldest_at_soft_cap(self, tmp_db, caplog):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Pre-fill at the cap.  Each entry has a unique body so we can
        # tell which one(s) survived a drop.
        for i in range(_WATCH_QUEUE_SOFT_CAP):
            dispatch(_reminder(f"body-{i}"), "watch-1")
        assert len(session._nudge_queue) == _WATCH_QUEUE_SOFT_CAP

        with caplog.at_level("WARNING"):
            dispatch(_reminder("overflow"), "watch-1")

        # Total stays at cap (one dropped, one added).
        assert len(session._nudge_queue) == _WATCH_QUEUE_SOFT_CAP
        bodies = [text for _t, text in session._nudge_queue.pending(channel="any")]
        # Oldest ("body-0") gone; newest ("overflow") present.
        assert "body-0" not in bodies
        assert "overflow" in bodies
        # Warning logged (the shared external-event rail owns the event now).
        assert any("external_event.queue_full" in r.message for r in caplog.records), (
            "expected an external_event.queue_full warning record"
        )

    def test_dispatch_soft_cap_does_not_evict_other_types(self, tmp_db):
        """A watch saturation drop must only target watch-typed entries.
        Other producers (idle_children, advisories) have their own
        rate limiters and must not be collateral damage.
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Mix in a few non-watch entries on the same queue.
        session._nudge_queue.enqueue("idle_children", "ic-1", "any")
        session._nudge_queue.enqueue("idle_children", "ic-2", "any")

        # Saturate watches up to cap (queue holds cap+2 total).
        for i in range(_WATCH_QUEUE_SOFT_CAP):
            dispatch(_reminder(f"body-{i}"), "watch-1")
        # One more triggers drop-oldest of a "watch_triggered" entry.
        dispatch(_reminder("overflow"), "watch-1")

        # Both idle_children entries survived — no collateral eviction.
        idle_bodies = [
            text
            for nt, text in session._nudge_queue.pending(channel="any")
            if nt == "idle_children"
        ]
        assert idle_bodies == ["ic-1", "ic-2"]


# ---------------------------------------------------------------------------
# Predicate independence
# ---------------------------------------------------------------------------


class TestPredicateIndependence:
    """The watch closure does NOT wire a ``valid_until`` predicate.

    Earlier the closure wired ``_still_active`` (re-reading
    ``is_watch_active`` at drain time).  That predicate raced
    ``WatchRunner._poll_watch``'s commit of ``active=False`` and silently
    dropped every terminal fire.  The closure now enqueues without a
    predicate; entries survive drain regardless of the row's ``active``
    column state.
    """

    def test_drain_delivers_even_when_storage_reports_inactive(self, tmp_db, monkeypatch):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Even if storage reports active=False, the entry should still
        # drain — no predicate to drop it.
        patch_session_storage(monkeypatch, active=False)

        dispatch(_reminder("body"), "watch-1")
        out = session._nudge_queue.drain({"any"})
        assert len(out) == 1
        assert out[0][0] == "watch_triggered"

    def test_dispatch_never_calls_is_watch_active(self, tmp_db, monkeypatch):
        """Pin the invariant directly: the closure must NOT consult
        ``storage.is_watch_active`` anywhere along the enqueue + drain
        path.  Without this assertion, a future change that re-wires
        an ``is_watch_active`` predicate would silently bring back the
        bug that motivates this whole module.
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        is_active_calls = patch_session_storage(monkeypatch, active=True)

        dispatch(_reminder("body"), "watch-bound-id")
        session._nudge_queue.drain({"any"})
        assert is_active_calls == [], (
            f"watch closure must not call is_watch_active; got {is_active_calls!r}"
        )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Two threads each fire 100 dispatches against the same session;
    the soft-cap read-then-mutate window stays bounded and the queue
    settles in a consistent state.

    Per the plan's risk register R2: in production only one daemon
    thread (``WatchRunner``'s ``_run``) ever calls a session's dispatch
    fn, so the 3-acquisition non-atomicity is harmless.  This test
    pins lock-correctness anyway against the broader race window.
    """

    def test_dispatch_concurrent_enqueues_thread_safe(self, tmp_db, monkeypatch):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Bypass the storage-touching valid_until predicate: count cap
        # behaviour, not storage round-trips.
        patch_session_storage(monkeypatch, active=True)

        per_thread = 100
        labels = ("a", "b")

        def fire(label: str) -> None:
            for i in range(per_thread):
                dispatch(_reminder(f"{label}-{i}"), f"watch-{label}")

        threads = [threading.Thread(target=fire, args=(label,), daemon=True) for label in labels]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        for t in threads:
            assert not t.is_alive(), "dispatch thread did not finish in time"

        # The non-atomic count-then-drop window admits at most one "slip"
        # per concurrent thread above the cap (each thread can observe a
        # sub-cap count and append before another thread's drop runs).
        depth = len(session._nudge_queue)
        assert depth <= len(threads) * per_thread
        assert depth <= _WATCH_QUEUE_SOFT_CAP + len(threads)


# ---------------------------------------------------------------------------
# Empty-input / multi-call invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "   ", "\x07\x0b"])
def test_dispatch_no_op_for_empty_payloads(tmp_db, payload: str):
    """Whitespace-only / pure-control payloads sanitise to empty and
    do not produce a queue entry — silent drop.
    """
    session = _make_session_for_dispatch()
    _runner, dispatch = _register_runner(session)

    dispatch(_reminder(payload), "watch-1")

    assert len(session._nudge_queue) == 0


# ---------------------------------------------------------------------------
# Metadata propagation
# ---------------------------------------------------------------------------


class TestMetadataPropagation:
    """The dispatch closure pulls optional fields out of the structured
    ``reminder`` dict and attaches them to the queue entry's
    ``metadata``.  Drain seams later merge ``metadata`` into the
    rendered reminder dict so the frontend can display a structured
    ``.msg.watch-result`` card.
    """

    def test_dispatch_attaches_watch_metadata_on_enqueue(self, tmp_db):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        reminder = _reminder(
            "$ ls\nfile.txt",
            watch_name="my-watch",
            command="ls",
            poll_count=2,
            max_polls=100,
            is_final=False,
        )
        dispatch(reminder, "watch-1")

        # Snapshot via ``pending_with_metadata`` to inspect the full
        # entry shape.  Exactly one entry, with the optional fields
        # carried verbatim onto ``metadata``.
        snapshot = session._nudge_queue.pending_with_metadata(channel="any")
        assert len(snapshot) == 1
        nt, _text, meta = snapshot[0]
        assert nt == "watch_triggered"
        assert meta == {
            "watch_name": "my-watch",
            "command": "ls",
            "poll_count": 2,
            "max_polls": 100,
            "is_final": False,
        }

    def test_dispatch_omits_metadata_when_optional_fields_missing(self, tmp_db):
        """A bare ``{type, text}`` reminder produces an entry with no
        metadata — the closure builds an empty dict, sees nothing to
        carry, and falls through to ``metadata=None``.
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        dispatch(_reminder("just a body"), "watch-1")

        snapshot = session._nudge_queue.pending_with_metadata(channel="any")
        assert len(snapshot) == 1
        _nt, _text, meta = snapshot[0]
        assert meta is None


# ---------------------------------------------------------------------------
# Wake trigger
# ---------------------------------------------------------------------------


class TestWakeFn:
    """``set_watch_runner``'s optional ``wake_fn`` fires once per enqueued
    dispatch — AFTER the entry lands — so a watch firing on an
    already-idle workstream (no IDLE transition for the
    ``IdleNudgeWatcher`` to observe) can spawn the wake worker that
    drains it.  Failures are contained: the enqueue must survive a
    raising ``wake_fn``, because a propagated raise would abort
    ``WatchRunner._poll_watch`` before the watch-row update commits and
    re-fire the same reminder every subsequent tick.
    """

    def test_wake_fn_called_after_enqueue(self, tmp_db):
        session = _make_session_for_dispatch()
        depth_at_wake: list[int] = []
        _runner, dispatch = _register_runner(
            session, wake_fn=lambda: depth_at_wake.append(len(session._nudge_queue))
        )

        dispatch(_reminder("watch fired body"), "watch-1")

        # Fired exactly once, and the entry was already queued when it ran
        # — the wake worker's drain must be able to see the fresh entry.
        assert depth_at_wake == [1]

    def test_wake_fn_not_called_when_payload_sanitizes_empty(self, tmp_db):
        """A fire whose payload strips to nothing enqueues nothing — and
        must not wake anything either (a wake with an empty queue would
        just spawn a worker that no-ops at the drain guard)."""
        session = _make_session_for_dispatch()
        wake = MagicMock()
        _runner, dispatch = _register_runner(session, wake_fn=wake)

        dispatch(_reminder("\x07\x0b\x7f"), "watch-1")

        assert len(session._nudge_queue) == 0
        wake.assert_not_called()

    def test_wake_fn_exception_is_contained(self, tmp_db, caplog):
        session = _make_session_for_dispatch()
        wake = MagicMock(side_effect=RuntimeError("boom"))
        _runner, dispatch = _register_runner(session, wake_fn=wake)

        with caplog.at_level("WARNING"):
            dispatch(_reminder("body"), "watch-1")  # must not raise

        # Entry survived; the failure surfaced as a warning, not a raise
        # up into the poll loop.
        assert len(session._nudge_queue) == 1
        assert any("external_event.wake_failed" in r.message for r in caplog.records), (
            "expected an external_event.wake_failed warning record"
        )


class _RecordingRunner:
    """Stub WatchRunner recording registration/removal order.  Mirrors the
    production owner-checked removal semantics — the resume tail passes
    ``owner`` and peeks ``get_dispatch_fn`` before re-registering."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.fns: dict[str, Any] = {}

    def set_dispatch_fn(self, ws_id: str, fn: Any) -> None:
        self.events.append(("set", ws_id))
        self.fns[ws_id] = fn

    def get_dispatch_fn(self, ws_id: str) -> Any:
        return self.fns.get(ws_id)

    def remove_dispatch_fn(self, ws_id: str, owner: Any = None) -> None:
        if owner is not None and self.fns.get(ws_id) is not owner:
            return
        self.events.append(("remove", ws_id))
        self.fns.pop(ws_id, None)


class TestResumeReRegistration:
    """A non-fork ``resume()`` rebinds ``_ws_id``; the dispatch
    registration must FOLLOW that identity — otherwise watches stamped
    with the adopted id never find the live session, and every fire
    takes the restore path, spawning a duplicate auto-approved session
    racing writes into the same conversation (CLI ``--resume`` and the
    ``/resume`` command both hit this)."""

    def _saved_ws(self, ws_id: str) -> None:
        from turnstone.core.memory import register_workstream, save_message

        register_workstream(ws_id)
        save_message(ws_id, "user", "hi")

    def test_nonfork_resume_moves_registration_to_adopted_id(self, tmp_db):
        self._saved_ws("resume-target")
        session = _make_session_for_dispatch()
        old_id = session._ws_id
        runner = _RecordingRunner()
        session.set_watch_runner(runner, wake_fn=None)

        assert session.resume("resume-target") is True

        # New key live BEFORE the old key is removed — a fire during the
        # transition can never observe an empty registry (which would
        # divert it to the restore path).
        assert runner.events == [
            ("set", old_id),
            ("set", "resume-target"),
            ("remove", old_id),
        ]
        assert set(runner.fns) == {"resume-target"}

    def test_fork_resume_keeps_registration(self, tmp_db):
        self._saved_ws("fork-src")
        session = _make_session_for_dispatch()
        old_id = session._ws_id
        runner = _RecordingRunner()
        session.set_watch_runner(runner, wake_fn=None)

        assert session.resume("fork-src", fork=True) is True

        # Fork keeps its own identity — registration untouched.
        assert runner.events == [("set", old_id)]

    def test_resume_without_runner_is_noop(self, tmp_db):
        # CLI --resume / restore-fn shape: resume() runs BEFORE any
        # set_watch_runner call — nothing to re-register, nothing raises.
        self._saved_ws("resume-bare")
        session = _make_session_for_dispatch()

        assert session.resume("resume-bare") is True
        assert session._watch_runner is None

    def test_reregistered_closure_keeps_wake_fn(self, tmp_db):
        # The stored wake_fn rides the re-registration: a watch firing on
        # the ADOPTED id must still wake the workstream.
        self._saved_ws("resume-wake")
        session = _make_session_for_dispatch()
        runner = _RecordingRunner()
        wake = MagicMock()
        session.set_watch_runner(runner, wake_fn=wake)

        assert session.resume("resume-wake") is True

        runner.fns["resume-wake"](_reminder("watch output"), "w1")
        assert len(session._nudge_queue) == 1
        wake.assert_called_once()

    def test_resume_does_not_steal_another_live_registration(self, tmp_db):
        # In-session /resume of a workstream that is OPEN IN ANOTHER PANE
        # (a degenerate two-live-sessions state): the original owner keeps
        # its watch fires — the adopter neither clobbers the target's
        # registration nor (on a later resume-away or close) deletes it.
        self._saved_ws("shared-A")
        self._saved_ws("other-C")
        runner = _RecordingRunner()

        pane_a = _make_session_for_dispatch()
        pane_a._ws_id = "shared-A"  # pane A opened A and registered
        pane_a.set_watch_runner(runner, wake_fn=None)
        fn_a = runner.fns["shared-A"]

        pane_b = _make_session_for_dispatch()
        pane_b.set_watch_runner(runner, wake_fn=None)

        assert pane_b.resume("shared-A") is True
        # Pane A's registration survived the adoption…
        assert runner.fns["shared-A"] is fn_a

        assert pane_b.resume("other-C") is True
        # …and the resume-away removed only pane B's own (absent) claim.
        assert runner.fns["shared-A"] is fn_a
        assert "other-C" in runner.fns

    def test_new_command_moves_registration_to_fresh_id(self, tmp_db):
        # /new is the other identity rebind: watches created AFTER it stamp
        # the fresh id and must reach this session, while the old
        # workstream's fires must stop landing in a conversation that no
        # longer shows them (they divert to the restore path instead).
        session = _make_session_for_dispatch()
        old_id = session._ws_id
        runner = _RecordingRunner()
        session.set_watch_runner(runner, wake_fn=None)

        # handle_command's return means "should exit" — /new never exits.
        assert session.handle_command("/new") is False

        assert session._ws_id != old_id
        assert set(runner.fns) == {session._ws_id}
        assert ("remove", old_id) in runner.events

    def test_close_removes_only_own_registration(self, tmp_db):
        # A watch-restore shell and a reopened pane can serve one ws_id in
        # sequence; the shell's later teardown must not unregister the pane.
        self._saved_ws("shared-W")
        runner = _RecordingRunner()

        shell = _make_session_for_dispatch()
        shell._ws_id = "shared-W"
        shell.set_watch_runner(runner, wake_fn=None)

        pane = _make_session_for_dispatch()
        pane._ws_id = "shared-W"
        pane.set_watch_runner(runner, wake_fn=None)  # pane re-registers (last writer)
        pane_fn = runner.fns["shared-W"]

        shell.close()  # shell reaped (close_idle / eviction)

        assert runner.fns.get("shared-W") is pane_fn  # pane still registered
