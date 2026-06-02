"""Tests for the ``_source`` storage column round-tripping through both backends.

``_source`` mirrors the in-memory side channel — which producer synthesised the
row (a wake ``system_nudge`` or an operator-context kind on a ``system`` turn).
(The sibling ``_reminders`` column was dropped in migration 060; operator
context lives in first-class ``system`` turns now.)
"""

from __future__ import annotations


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

    def test_nul_bytes_stripped_from_source(self, backend):
        """NUL bytes must be stripped from ``_source`` at the storage layer.

        Producers strip NUL today, but the layer is the tripwire if a future
        producer forgets — and PostgreSQL TEXT columns reject NUL outright.
        """
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "", source="system_nudge\x00")
        msgs = backend.load_messages("s1")
        assert msgs[0].get("_source") == "system_nudge"
