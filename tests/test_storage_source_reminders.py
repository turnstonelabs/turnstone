"""Tests for ``_source`` / ``_reminders`` round-tripping through both
storage backends.

Persisting the in-memory side-channels lets multi-tab / multi-device
replay show the same metacognitive bubble shape the originating tab
saw live — see ``docs/design/watch-card-ux.md`` §1.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from turnstone.core.storage._schema import conversations


class TestSourceRoundtrip:
    def test_source_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "", source="system_nudge")
        msgs = backend.load_messages("s1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == ""
        assert msgs[0].get("_source") == "system_nudge"

    def test_source_absent_when_not_set(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello")
        msgs = backend.load_messages("s1")
        assert "_source" not in msgs[0]


class TestRemindersRoundtrip:
    def test_reminders_roundtrip(self, backend):
        backend.register_workstream("s1")
        payload = [
            {
                "type": "watch_triggered",
                "text": "$ ls\nfile.txt\n",
                "watch_name": "w1",
                "command": "ls",
                "poll_count": 2,
                "max_polls": 100,
                "is_final": False,
            }
        ]
        backend.save_message(
            "s1",
            "user",
            "",
            source="system_nudge",
            reminders=json.dumps(payload, separators=(",", ":")),
        )
        msgs = backend.load_messages("s1")
        assert msgs[0].get("_reminders") == payload
        # Optional fields preserved verbatim.
        rem = msgs[0]["_reminders"][0]
        assert rem["watch_name"] == "w1"
        assert rem["command"] == "ls"
        assert rem["poll_count"] == 2
        assert rem["max_polls"] == 100
        assert rem["is_final"] is False

    def test_reminders_null_renders_as_no_key(self, backend):
        """Absent vs. empty-list should map to the same shape on the
        load side: ``_reminders`` simply not present in the dict.
        Mirrors the ``_attachments_meta`` precedent in
        ``reconstruct_messages``.
        """
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello")
        msgs = backend.load_messages("s1")
        assert "_reminders" not in msgs[0]

    def test_tool_reminders_roundtrip(self, backend):
        backend.register_workstream("s1")
        # Build a minimal valid history: assistant turn with one
        # tool_call followed by the tool result that carries the
        # tool-channel reminder.  Without the assistant turn the
        # tool row would be orphaned and stripped by the repair pass.
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ]
        )
        backend.save_message("s1", "user", "go")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        payload = [{"type": "tool_error", "text": "command failed"}]
        backend.save_message(
            "s1",
            "tool",
            "boom",
            tool_call_id="c1",
            reminders=json.dumps(payload, separators=(",", ":")),
        )
        msgs = backend.load_messages("s1")
        # Find the tool message and assert reminders survived load.
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].get("_reminders") == payload

    def test_nul_bytes_stripped_from_source_and_reminders(self, backend):
        """NUL bytes must be stripped at the storage layer.

        Producers (``sanitize_payload`` on the watch dispatch path,
        constants for non-watch nudges) already strip NUL today so
        nothing in production reaches this clamp — but the layer is
        the tripwire if a future producer forgets, mirroring how
        ``content`` and ``provider_data`` are sanitized.  PostgreSQL
        TEXT columns reject NUL outright, so the sanitization is also
        a hard correctness invariant on that backend.

        ``json.dumps`` already escapes NUL inside string values to
        ``\\u0000`` so a real NUL byte can't enter ``_reminders`` via
        the normal encode path — the test feeds a raw NUL directly to
        cover the bypass case (a future producer that hand-builds the
        column string).
        """
        backend.register_workstream("s1")
        backend.save_message(
            "s1",
            "user",
            "",
            source="system_nudge\x00",
            reminders='[{"type":"watch_triggered","text":"ok\x00bad"}]',
        )
        msgs = backend.load_messages("s1")
        assert msgs[0].get("_source") == "system_nudge"
        assert msgs[0].get("_reminders") == [{"type": "watch_triggered", "text": "okbad"}]

    def test_malformed_reminders_json_does_not_crash_load(self, backend):
        """A garbage string in the column must not abort the whole
        load — mirrors the ``provider_data`` JSON-decode-suppress
        pattern.  Concretely: write a row with valid columns BUT a
        corrupted ``_reminders`` value via raw SQL, then verify the
        load returns the message with no ``_reminders`` key (rather
        than raising or surfacing the garbage).
        """
        backend.register_workstream("s1")
        msg_id = backend.save_message("s1", "user", "hello")
        with backend._engine.connect() as conn:
            conn.execute(
                sa.update(conversations)
                .where(conversations.c.id == msg_id)
                .values(_reminders="this is not json {{")
            )
            conn.commit()
        msgs = backend.load_messages("s1")
        assert len(msgs) == 1
        # Garbage suppressed silently — key absent, content intact.
        assert "_reminders" not in msgs[0]
        assert msgs[0]["content"] == "hello"
