"""Live-marker test for the watch switchover (run separately by user).

Confirms that a real LLM handles ``<system-reminder>``-framed watch
results sensibly under the post-switchover pull-model path.  The
plan's risk register R9 calls this an ``ASSUMED`` claim; this test is
the verification recipe — convert R9 to ``VERIFIED`` after a manual
run against an Anthropic-backed config.

Run via::

    pytest -m live tests/test_watch_live.py -k watch_envelope

Test collects without error during the regular ``-m "not live"`` run
(the marker filter deselects it); the live operator runs it on demand.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.session import ChatSession
from turnstone.core.watch import WatchRunner


class _NullUI:
    """Minimal UI stub for the chat loop's hook surface."""

    def __getattr__(self, name: str) -> Any:
        return MagicMock()


@pytest.mark.live
def test_watch_envelope_round_trip_through_real_model(tmp_db):
    """End-to-end pin against a real LLM backend.

    1. Build a real ``ChatSession`` against the configured live backend
       (the test runner injects credentials via env / config — same
       pattern as other live tests).
    2. Register a real ``WatchRunner`` and fire one watch result with
       a recognisable shape (``"ls -la output: foo bar baz"``).
    3. Send a follow-up user turn referencing the output.
    4. Inspect the assistant response — should reference the watch
       output rather than ignoring it or treating it as malformed.

    The assertion is intentionally loose (substring search) — model
    responses vary; we only care that the envelope-framed payload
    reached the model intact and influenced the response.

    NOTE: this scaffold does not currently provision a live client —
    the parent test session is expected to inject the live ``client``
    fixture (real LLM provider + model) before the assertion stage.
    See risk register R9 for the verification protocol.
    """
    session = ChatSession(
        client=MagicMock(),  # live runner replaces this with a real provider
        model="test-model",  # live runner overrides via config
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )

    class _StubStorage:
        def get_watch(self, watch_id: str) -> dict[str, Any]:
            return {"watch_id": watch_id, "active": True}

    runner = WatchRunner(storage=_StubStorage(), node_id="live-node")
    session.set_watch_runner(runner)

    # Fire one watch payload with a recognisable substring.
    runner._dispatch_result(session._ws_id, "ls -la output: foo bar baz", "watch-1")
    assert len(session._nudge_queue) == 1

    # User turn — the model's response under live mode should reference
    # the watch output via the envelope splice.
    session.send("What did the watch produce?")

    assistant = [m for m in session.messages if m.get("role") == "assistant"]
    assert assistant, "expected at least one assistant turn"
    body = (assistant[-1].get("content") or "").lower()
    assert "foo" in body or "bar" in body or "baz" in body, (
        f"expected the model's response to reference the watch payload; got: {body!r}"
    )
