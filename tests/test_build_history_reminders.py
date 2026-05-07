"""Tests for ``turnstone.server._build_history`` reminder + source surfacing.

The replay path (``_build_history``) projects the ``_source`` and
``_reminders`` side-channels onto the wire entry the frontend
consumes.  Persisted via migration 050 (Commit 1) so multi-tab /
multi-device replay sees the same metacognitive bubble shape the
originating tab saw live.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from turnstone.server import _build_history


def _make_stub_session(messages: list[dict[str, Any]]) -> Any:
    """Minimal ChatSession-shaped stub.  ``_build_history`` only reads
    ``session.messages`` plus calls ``_load_verdict_indexes(ws_id)`` —
    the latter we patch out below.
    """
    return SimpleNamespace(messages=messages, _ws_id="ws-test")


def _build(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run ``_build_history`` against a stub session, bypassing the
    verdicts / output-assessment storage round-trip (no tool_calls in
    these tests, so the indexes are unused anyway).
    """
    session = _make_stub_session(messages)
    with patch(
        "turnstone.server._load_verdict_indexes",
        return_value=({}, {}),
    ):
        return _build_history(session)


class TestSourceSurfacing:
    def test_source_surfaces_when_set(self) -> None:
        msg = {
            "role": "user",
            "content": "",
            "_source": "system_nudge",
        }
        history = _build([msg])
        assert len(history) == 1
        assert history[0]["source"] == "system_nudge"

    def test_source_absent_when_unset(self) -> None:
        msg = {"role": "user", "content": "hello"}
        history = _build([msg])
        assert "source" not in history[0]


class TestRemindersWidening:
    def test_watch_triggered_optional_fields_propagate(self) -> None:
        """The widened payload (Commit 2) carries watch_name / command /
        poll_count / max_polls / is_final on each ``watch_triggered``
        reminder so the frontend renders ``.msg.watch-result``.
        """
        msg = {
            "role": "user",
            "content": "",
            "_source": "system_nudge",
            "_reminders": [
                {
                    "type": "watch_triggered",
                    "text": "$ ls\nfile.txt",
                    "watch_name": "w1",
                    "command": "ls",
                    "poll_count": 2,
                    "max_polls": 100,
                    "is_final": False,
                }
            ],
        }
        history = _build([msg])
        assert history[0]["source"] == "system_nudge"
        assert history[0]["reminders"] == [
            {
                "type": "watch_triggered",
                "text": "$ ls\nfile.txt",
                "watch_name": "w1",
                "command": "ls",
                "poll_count": 2,
                "max_polls": 100,
                "is_final": False,
            }
        ]

    def test_legacy_two_field_reminders_still_work(self) -> None:
        """Producers without optional fields (correction / denial /
        idle_children) keep the legacy ``{type, text}`` shape — the
        widened filter just doesn't add anything beyond that."""
        msg = {
            "role": "user",
            "content": "noted",
            "_reminders": [{"type": "correction", "text": "watch out"}],
        }
        history = _build([msg])
        assert history[0]["reminders"] == [{"type": "correction", "text": "watch out"}]

    def test_unknown_keys_are_dropped(self) -> None:
        """The wire-layer filter projects on a known set of keys so a
        future producer accidentally stuffing arbitrary fields can't
        leak them through replay.
        """
        msg = {
            "role": "user",
            "content": "x",
            "_reminders": [
                {
                    "type": "correction",
                    "text": "hi",
                    "secret": "leak-me",
                    "internal_id": 42,
                }
            ],
        }
        history = _build([msg])
        clean = history[0]["reminders"][0]
        assert "secret" not in clean
        assert "internal_id" not in clean
        assert clean == {"type": "correction", "text": "hi"}

    def test_malformed_reminder_skipped(self) -> None:
        """A non-dict / empty entry is filtered out instead of breaking
        the rest of the list (mirrors the defensive filter in
        ``_apply_reminders_for_provider``).
        """
        msg = {
            "role": "user",
            "content": "x",
            "_reminders": [
                "garbage string",
                {"type": "", "text": ""},  # empty type + text → drop
                {"type": "denial", "text": "ok"},
            ],
        }
        history = _build([msg])
        assert history[0]["reminders"] == [{"type": "denial", "text": "ok"}]
