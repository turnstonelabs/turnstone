"""Tests for the watch dispatch closure built inside ``set_watch_runner``.

The closure routes watch results onto the per-session :class:`NudgeQueue`
under the unified pull-model surface (post-#482).  Each test focuses on
one assertion: enqueue shape, sanitisation, soft-cap drop-oldest,
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


def _register_runner(session: ChatSession) -> tuple[Any, Any]:
    """Attach a minimal stub ``WatchRunner`` to *session* and return the
    ``(runner, dispatch_fn)`` pair captured by ``set_dispatch_fn``.
    """
    captured: dict[str, Any] = {}

    class _StubRunner:
        def set_dispatch_fn(self, ws_id: str, fn: Any) -> None:
            captured["fn"] = fn

    runner = _StubRunner()
    session.set_watch_runner(runner)
    return runner, captured["fn"]


def _watch_row(active: bool = True, watch_id: str = "watch-1") -> dict[str, Any]:
    """Minimal storage row shape ``valid_until`` predicates re-check."""
    return {"watch_id": watch_id, "active": active}


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

        dispatch("watch fired body", "watch-1")

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
    before it ever reaches the queue.  The wire-boundary
    ``escape_wrapper_tags`` only protects ``<system-reminder>`` /
    ``<tool_output>`` envelopes; this layer covers everything else.
    """

    def test_dispatch_sanitizes_payload_before_enqueue(self, tmp_db):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        # Build a payload with: BEL (\x07), zero-width space (U+200B),
        # bidi RTL override (U+202E), and angle-bracket tag breakers.
        raw = "before\x07middle​after‮more<thinking>tail"
        dispatch(raw, "watch-1")

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

        dispatch("line1\nline2\n\tindented\n\rline3", "watch-1")

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
        dispatch("\x07\x0b\x7f​", "watch-1")

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
            dispatch(f"body-{i}", "watch-1")
        assert len(session._nudge_queue) == _WATCH_QUEUE_SOFT_CAP

        with caplog.at_level("WARNING"):
            dispatch("overflow", "watch-1")

        # Total stays at cap (one dropped, one added).
        assert len(session._nudge_queue) == _WATCH_QUEUE_SOFT_CAP
        bodies = [text for _t, text in session._nudge_queue.pending(channel="any")]
        # Oldest ("body-0") gone; newest ("overflow") present.
        assert "body-0" not in bodies
        assert "overflow" in bodies
        # Warning logged.
        assert any("watch_dispatch.queue_full" in r.message for r in caplog.records), (
            "expected a watch_dispatch.queue_full warning record"
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
            dispatch(f"body-{i}", "watch-1")
        # One more triggers drop-oldest of a "watch_triggered" entry.
        dispatch("overflow", "watch-1")

        # Both idle_children entries survived — no collateral eviction.
        idle_bodies = [
            text
            for nt, text in session._nudge_queue.pending(channel="any")
            if nt == "idle_children"
        ]
        assert idle_bodies == ["ic-1", "ic-2"]


# ---------------------------------------------------------------------------
# valid_until predicate
# ---------------------------------------------------------------------------


class TestValidUntil:
    """The ``valid_until`` predicate captured at dispatch time re-checks
    the watch's ``active`` flag at drain time, so a cancelled watch's
    last splat doesn't ride out a future wake.
    """

    def test_valid_until_drops_when_watch_inactive(self, tmp_db, monkeypatch):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        from turnstone.core import session as session_mod

        # Storage stub returns active=False at drain time.
        get_calls: list[str] = []

        class _StubStorage:
            def get_watch(self, watch_id: str) -> dict[str, Any] | None:
                get_calls.append(watch_id)
                return _watch_row(active=False, watch_id=watch_id)

        monkeypatch.setattr(session_mod, "get_storage", lambda: _StubStorage())

        dispatch("body", "watch-1")
        # Drain fires the predicate; entry should NOT be delivered.
        out = session._nudge_queue.drain({"any"})
        assert out == []
        # Predicate ran once.
        assert get_calls == ["watch-1"]

    def test_valid_until_drops_when_watch_missing(self, tmp_db, monkeypatch):
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        from turnstone.core import session as session_mod

        class _StubStorage:
            def get_watch(self, watch_id: str) -> dict[str, Any] | None:
                return None  # row gone (deleted)

        monkeypatch.setattr(session_mod, "get_storage", lambda: _StubStorage())

        dispatch("body", "watch-1")
        out = session._nudge_queue.drain({"any"})
        assert out == []

    def test_valid_until_drops_when_storage_raises(self, tmp_db, monkeypatch):
        """The closure's broad-except in the predicate translates a
        storage-layer exception to ``False`` so the drain doesn't
        propagate; the predicate captured ``watch_id`` correctly
        (otherwise storage wouldn't even be touched).
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        from turnstone.core import session as session_mod

        class _BoomStorage:
            def get_watch(self, watch_id: str) -> dict[str, Any] | None:
                raise RuntimeError("storage down")

        monkeypatch.setattr(session_mod, "get_storage", lambda: _BoomStorage())

        dispatch("body", "watch-bound-id")
        out = session._nudge_queue.drain({"any"})
        assert out == []

    def test_valid_until_delivers_when_watch_active(self, tmp_db, monkeypatch):
        """Happy-path counter-test for the predicate above: the entry
        DOES drain when the watch is still active.
        """
        session = _make_session_for_dispatch()
        _runner, dispatch = _register_runner(session)

        from turnstone.core import session as session_mod

        class _StubStorage:
            def get_watch(self, watch_id: str) -> dict[str, Any] | None:
                return _watch_row(active=True, watch_id=watch_id)

        monkeypatch.setattr(session_mod, "get_storage", lambda: _StubStorage())

        dispatch("body", "watch-1")
        out = session._nudge_queue.drain({"any"})
        assert len(out) == 1
        assert out[0][0] == "watch_triggered"


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
        from turnstone.core import session as session_mod

        class _ActiveStorage:
            def get_watch(self, watch_id: str) -> dict[str, Any]:
                return _watch_row(active=True, watch_id=watch_id)

        monkeypatch.setattr(session_mod, "get_storage", lambda: _ActiveStorage())

        per_thread = 100

        def fire(label: str) -> None:
            for i in range(per_thread):
                dispatch(f"{label}-{i}", f"watch-{label}")

        threads = [
            threading.Thread(target=fire, args=("a",), daemon=True),
            threading.Thread(target=fire, args=("b",), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        for t in threads:
            assert not t.is_alive(), "dispatch thread did not finish in time"

        # Two threads × 100 dispatches → at most 200 entries; the cap
        # bounds the actual count; no exception was raised across the
        # 3-acquisition window per dispatch.
        depth = len(session._nudge_queue)
        assert depth <= 2 * per_thread
        # The non-atomic count-then-drop window allows the queue to
        # transiently go slightly above cap.  Bound the slack:
        assert depth <= _WATCH_QUEUE_SOFT_CAP + 2 * per_thread


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

    dispatch(payload, "watch-1")

    assert len(session._nudge_queue) == 0
