"""Tests for the REST ``/history`` projection helpers.

``project_history_messages`` does the structural projection — collapse
multipart user content, surface the ``_source`` / ``_reminders``
side-channels, flatten tool_calls, derive ``denied`` / ``is_error`` /
``pending`` — that the interactive ``replayHistory`` renderer and the
coordinator dashboard both consume directly.  ``extract_reasoning_for_history``
surfaces stored reasoning text and strips the internal ``_provider_content``
lane.  Together they compose the ``make_history_handler`` pipeline.

Persisted via migration 050 (source / reminders) and migration 052
(reasoning) so multi-tab / multi-device replay sees the same metacognitive
bubble shape the originating tab saw live.
"""

from __future__ import annotations

from typing import Any

from turnstone.core.history_decoration import (
    extract_reasoning_for_history,
    project_history_messages,
)


class TestSourceSurfacing:
    def test_source_surfaces_when_set(self) -> None:
        history = project_history_messages(
            [{"role": "user", "content": "", "_source": "system_nudge"}]
        )
        assert len(history) == 1
        assert history[0]["source"] == "system_nudge"

    def test_source_absent_when_unset(self) -> None:
        history = project_history_messages([{"role": "user", "content": "hello"}])
        assert "source" not in history[0]


class TestRemindersWidening:
    def test_watch_triggered_optional_fields_propagate(self) -> None:
        """The widened payload carries watch_name / command / poll_count /
        max_polls / is_final on each ``watch_triggered`` reminder so the
        frontend renders ``.msg.watch-result``.
        """
        history = project_history_messages(
            [
                {
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
            ]
        )
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
        idle_children) keep the legacy ``{type, text}`` shape."""
        history = project_history_messages(
            [
                {
                    "role": "user",
                    "content": "noted",
                    "_reminders": [{"type": "correction", "text": "watch out"}],
                }
            ]
        )
        assert history[0]["reminders"] == [{"type": "correction", "text": "watch out"}]

    def test_unknown_keys_are_dropped(self) -> None:
        """The wire-layer filter projects on a known set of keys so a
        future producer accidentally stuffing arbitrary fields can't leak
        them through replay."""
        history = project_history_messages(
            [
                {
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
            ]
        )
        clean = history[0]["reminders"][0]
        assert "secret" not in clean
        assert "internal_id" not in clean
        assert clean == {"type": "correction", "text": "hi"}

    def test_malformed_reminder_skipped(self) -> None:
        """A non-dict / empty entry is filtered out instead of breaking the
        rest of the list (mirrors the defensive filter in
        ``_apply_reminders_for_provider``)."""
        history = project_history_messages(
            [
                {
                    "role": "user",
                    "content": "x",
                    "_reminders": [
                        "garbage string",
                        {"type": "", "text": ""},  # empty type + text → drop
                        {"type": "denial", "text": "ok"},
                    ],
                }
            ]
        )
        assert history[0]["reminders"] == [{"type": "denial", "text": "ok"}]


class TestReasoningSurfacing:
    """``extract_reasoning_for_history`` surfaces stored Anthropic thinking
    blocks on the assistant message (so refresh-the-page rehydrates the
    reasoning bubble) and strips the internal ``_provider_content`` lane.
    Drives through the real ``AnthropicProvider`` extractor — only the
    surface flag is a parameter (the active-model flag resolution lives in
    ``make_history_handler``, covered by its REST tests).
    """

    def test_reasoning_surfaces_for_anthropic_thinking_msg(self) -> None:
        msgs: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {"type": "thinking", "thinking": "let me think", "signature": "s"},
                    {"type": "text", "text": "Final answer."},
                ],
            }
        ]
        extract_reasoning_for_history(msgs, surface_persisted_reasoning_flag=True)
        assert msgs[0]["reasoning"] == "let me think"

    def test_reasoning_empty_when_persist_flag_false(self) -> None:
        msgs: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {"type": "thinking", "thinking": "hidden", "signature": "s"},
                ],
            }
        ]
        extract_reasoning_for_history(msgs, surface_persisted_reasoning_flag=False)
        assert "reasoning" not in msgs[0]

    def test_provider_content_always_stripped(self) -> None:
        # The internal lane is stripped regardless of the flag — the wire
        # payload never carries it.
        for flag in (True, False):
            msgs: list[dict[str, Any]] = [
                {
                    "role": "assistant",
                    "content": "Final answer.",
                    "_provider_content": [
                        {"type": "thinking", "thinking": "x", "signature": "s"},
                    ],
                }
            ]
            extract_reasoning_for_history(msgs, surface_persisted_reasoning_flag=flag)
            assert "_provider_content" not in msgs[0]

    def test_no_reasoning_field_when_provider_content_missing(self) -> None:
        msgs: list[dict[str, Any]] = [{"role": "assistant", "content": "plain answer"}]
        extract_reasoning_for_history(msgs, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in msgs[0]

    def test_no_reasoning_field_for_non_assistant_messages(self) -> None:
        # Defensive — user/tool messages are skipped entirely; a stray
        # _provider_content on them never gets a reasoning field stamped.
        msgs: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "out",
                "_provider_content": [{"type": "thinking", "thinking": "leak", "signature": "s"}],
            },
        ]
        extract_reasoning_for_history(msgs, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in msgs[0]
        assert "reasoning" not in msgs[1]


class TestProjectHistoryMessages:
    """End-to-end shape test — the Python port of the retired client-side
    ``normalizeHistoryMessages`` node test.  Feeds the provider-native
    ``reconstruct_messages`` storage shape (nested tool_calls,
    ``_source`` / ``_reminders`` / ``_attachments_meta`` side-channels,
    multipart content, no derived flags) and asserts the canonical
    projected wire shape both UIs consume.
    """

    def test_projects_storage_shape_to_wire_shape(self) -> None:
        raw: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {}},
                ],
                "_source": "system_nudge",
                "_reminders": [
                    {"type": "correction", "text": "fix", "secret": "x"},
                    {"type": "", "text": ""},
                ],
                "_attachments_meta": [
                    {"kind": "image", "filename": "p.png", "mime_type": "image/png"}
                ],
            },
            {
                "role": "assistant",
                "content": "ok",
                "reasoning": "think",  # already stamped by extract_reasoning_for_history
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"q":1}'},
                        "verdict": {"tier": "judge"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "res"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c2", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "Denied by user: no"},
            {"role": "tool", "tool_call_id": "cx", "content": "Error: boom"},
            # mid-conversation orphan: tool_call with no result that is NOT
            # the last tool turn → must still render (not vanish), NOT pending.
            {
                "role": "assistant",
                "tool_calls": [{"id": "c_mid", "function": {"name": "g", "arguments": "{}"}}],
            },
            # trailing orphan: last tool turn with no result → pending (awaiting).
            {
                "role": "assistant",
                "tool_calls": [{"id": "c3", "function": {"name": "f", "arguments": "{}"}}],
            },
        ]
        out = project_history_messages(raw)

        # tool_calls flattened: name / arguments / verdict top-level
        assert out[1]["tool_calls"][0]["name"] == "web_search"
        assert out[1]["tool_calls"][0]["arguments"] == '{"q":1}'
        assert out[1]["tool_calls"][0]["verdict"]["tier"] == "judge"
        # multipart user content collapsed; side-channels surfaced top-level
        assert out[0]["content"] == "hi"
        assert out[0]["attachments"][0]["filename"] == "p.png"  # _attachments_meta wins
        assert out[0]["source"] == "system_nudge"
        assert [r["type"] for r in out[0]["reminders"]] == ["correction"]  # empty filtered
        assert "secret" not in out[0]["reminders"][0]  # unknown key stripped
        # reasoning passes through (already stamped upstream)
        assert out[1]["reasoning"] == "think"
        # derived + propagated flags (the storage shape pre-sets none)
        assert out[4]["denied"] is True  # tool deny derived from content prefix
        assert out[3]["denied"] is True  # propagated to the parent assistant turn
        assert out[5]["is_error"] is True  # tool error derived from content prefix
        # ``pending`` is a LIVE-state decision gated on ``awaiting_approval``
        # (default False here) — NOT orphan-detection.  So even the trailing
        # orphan renders its tool block by default; see
        # ``test_pending_gated_on_awaiting_approval`` for the gate.
        assert out[7].get("pending") is not True  # trailing orphan c3 — not awaiting → renders
        assert out[6].get("pending") is not True  # mid-conversation orphan c_mid renders
        assert out[1].get("pending") is not True  # resolved c1

    def test_pending_gated_on_awaiting_approval(self) -> None:
        """``pending`` marks the LAST orphan tool-call turn only when the
        caller passes ``awaiting_approval=True`` (the live ``_pending_approval``
        read).  This is the regression guard for the fresh-connect bug: an
        orphan tool call mid-execution is NOT awaiting approval, so it must
        render its tool block (``pending`` absent) rather than vanish until a
        reconnect replays the buffered events.
        """
        raw: list[dict[str, Any]] = [
            # resolved turn (has a tool result)
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "res"},
            # mid-conversation orphan (NOT the last tool turn)
            {
                "role": "assistant",
                "tool_calls": [{"id": "c_mid", "function": {"name": "g", "arguments": "{}"}}],
            },
            {"role": "user", "content": "carry on"},
            # trailing orphan (last tool turn, no result)
            {
                "role": "assistant",
                "tool_calls": [{"id": "c_last", "function": {"name": "h", "arguments": "{}"}}],
            },
        ]

        # Awaiting approval: ONLY the trailing orphan turn is pending.
        awaiting = project_history_messages(raw, awaiting_approval=True)
        assert awaiting[4].get("pending") is True  # trailing orphan → skip static, live prompt
        assert awaiting[2].get("pending") is not True  # mid-conversation orphan still renders
        assert awaiting[0].get("pending") is not True  # resolved turn

        # Executing / not awaiting: NOTHING is pending — the trailing orphan
        # (a tool mid-execution) renders its tool block on a fresh connect.
        executing = project_history_messages(raw, awaiting_approval=False)
        executing_pending = [entry.get("pending") for entry in executing]
        assert executing_pending == [None, None, None, None, None]
        # Default matches awaiting_approval=False.
        default_pending = [entry.get("pending") for entry in project_history_messages(raw)]
        assert default_pending == [None, None, None, None, None]
