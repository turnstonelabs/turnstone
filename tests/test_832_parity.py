"""#832 replay-parity pins: fixed chunk scripts ⇒ identical observable turn.

Baseline fixtures under ``tests/data/parity_832/`` were captured from the
pre-fold streaming path (``UPDATE_832_PARITY=1`` at a tree whose
``session.py`` is byte-identical to pre-fold main), which is what makes
them THE old-world record.  Each test replays a scenario on the current
tree and compares the full record — UI event sequence, committed-message
projection, mid-stream usage — against the baseline after applying the
transforms below.  Each transform IS a ruled #832 behavior change,
restated in full where it is applied, so the pin is auditable from this
file alone; a difference outside one is a regression.

Do not regenerate baselines casually: they encode the old world.  A
legitimate regeneration updates the matching transform below in the same
commit, or the pin loses its meaning.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests._parity_832 import (
    SCENARIOS,
    UPDATE,
    fixture_path,
    load_fixture,
    run_scenario,
    write_fixture,
)
from tests._session_helpers import (
    RecordingUI,
    make_session,
    replace_session_lane,
    scripted_provider,
)
from turnstone.core.providers._protocol import StreamChunk, UsageInfo
from turnstone.core.trajectory import Turn


def _apply_ruled_deltas(name: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """Transform an old-world record into the post-fold expectation.

    Every transform restates its ruling in full — the transforms are the
    committed registry of #832's deliberate behavior changes.  Pre-fold
    trees (fixtures being captured) never reach this — capture mode
    writes and exits.
    """
    expected = json.loads(json.dumps(baseline))  # deep copy

    if name == "info_postfinish_footer":
        # RULED (#832): the trailing citations footer enters the COMMITTED
        # content (conditional fold: non-blank answer, "\n\n" separator —
        # the shared drain-side spelling) and streams as content
        # post-carry-flush, so displayed ordering matches committed,
        # instead of an ephemeral info bubble that never survived reload.
        # Consequence accepted with the ruling: web-controlled footer text
        # reaches storage/search/context/export (release-noted).
        footer = "Sources:\n- example.com/page"
        expected["result"]["content"] += "\n\n" + footer
        events = [e for e in expected["ui_events"] if e != ["info", footer]]
        end = events.index(["stream_end", ""])
        events[end:end] = [["content", "\n\n" + footer]]
        expected["ui_events"] = events

    elif name == "no_finish_clean_exhaust":
        # RULED (#832): a stream that exhausts with no finish reason no
        # longer commits its partial silently — it is a mid-stream death.
        # The re-issue ladder finalizes the display and re-drives the turn
        # (_MID_STREAM_RETRIES times), then the terminal arm
        # finalizes+discards and the retryable error surfaces.
        expected["raised"] = "IncompleteStreamError"
        expected["result"] = None
        retry_theater = []
        for attempt in (1, 2):
            retry_theater += [
                ["stream_end", ""],
                [
                    "info",
                    f"[stream died mid-response (IncompleteStreamError) — retrying in "
                    f"0s ({attempt}/2)]",
                ],
                ["stream_discarded", ""],
                ["thinking_start", ""],
                ["thinking_stop", ""],
            ]
        expected["ui_events"] = (
            [["thinking_stop", ""]] + retry_theater + [["stream_end", ""], ["stream_discarded", ""]]
        )

    elif name == "think_tags_split_across_chunks":
        # RULED (#832): the COMMITTED content takes the drain's single
        # blank-edge trim when an inline think tag was consumed; the
        # DISPLAYED stream keeps the raw residue ("\n\nAnswer") — an
        # accepted member of the existing snapshot-vs-history whitespace
        # class, not a new divergence mechanism.
        expected["result"]["content"] = expected["result"]["content"].lstrip("\n")

    return expected


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_parity(name: str) -> None:
    record = run_scenario(name)
    if UPDATE:
        write_fixture(name, record)
        pytest.skip("captured baseline")
    assert fixture_path(name).exists(), (
        f"missing baseline {name!r}; capture with UPDATE_832_PARITY=1 at a pre-fold "
        f"tree (run_scenario adapts to the old seam signature by inspection)"
    )
    expected = _apply_ruled_deltas(name, load_fixture(name))
    assert record == expected


class TestDisplayCommitMirror:
    """The mirror LAW (no old-world baselines): with one chunk script,
    the DISPLAYED content stream and the COMMITTED content must agree.

    The drain assembles the committed turn while the consumer drives the
    display; these scenarios interleave provider-parsed
    ``reasoning_delta`` with buffered content, where the two lanes can
    disagree (display dropping or relabeling the buffered tail the commit
    kept, or showing nothing while the commit carries answer + sources).
    The consumer's ``close_run`` at the reasoning boundary and
    ``partial_tag_tail``'s proper-prefix contract hold them together.
    """

    _USAGE = UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    def _mirror(self, chunks: list[StreamChunk]) -> tuple[str, str]:
        ui = RecordingUI()
        session = make_session(ui=ui)
        session._RETRY_BASE_DELAY = 0
        replace_session_lane(session, provider=scripted_provider(chunks))
        session.messages.append(Turn.user("hi"))
        result = session._stream_response(0)
        displayed = "".join(d for k, d in ui.events if k == "content")
        return displayed, result.content

    @pytest.mark.parametrize(
        ("name", "chunks"),
        [
            (
                "short_content_then_reasoning",
                [
                    StreamChunk(content_delta="Short"),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "short_content_then_reasoning_with_footer",
                [
                    StreamChunk(content_delta="Short"),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources:\n- example.com"),
                ],
            ),
            (
                "long_content_then_reasoning",
                [
                    StreamChunk(content_delta="A much longer content run here"),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "content_reasoning_content",
                [
                    StreamChunk(content_delta="Before "),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(content_delta="after"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "partial_tag_spans_reasoning_boundary",
                [
                    StreamChunk(content_delta="Ans<thi"),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(content_delta="nk>hidden</think>done"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "complete_tag_before_reasoning_boundary",
                [
                    StreamChunk(content_delta="Answer<reasoning>"),
                    StreamChunk(reasoning_delta="(r)"),
                    StreamChunk(content_delta=" resumed"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            # The boundary close must run while an INLINE think block is
            # open, and the cross-boundary carry must survive state flips.
            (
                "open_inline_think_at_reasoning_boundary",
                [
                    StreamChunk(content_delta="pre<think>secret"),
                    StreamChunk(reasoning_delta="R"),
                    StreamChunk(content_delta="answer"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "split_close_tag_across_reasoning_boundary",
                [
                    StreamChunk(content_delta="pre<think>body</thi"),
                    StreamChunk(reasoning_delta="R"),
                    StreamChunk(content_delta="nk>post"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "unresolved_partial_tag_at_finish",
                [
                    StreamChunk(content_delta="Ans<thi"),
                    StreamChunk(reasoning_delta="R"),
                    StreamChunk(finish_reason="stop"),
                ],
            ),
            (
                "partial_tag_only_with_footer",
                [
                    StreamChunk(content_delta="<thi"),
                    StreamChunk(reasoning_delta="R"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources:\n- example.com"),
                ],
            ),
            # Lax-gateway shape: content AFTER a post-finish footer —
            # the fold must run once at stream end over the FULL answer
            # (the drain's post-loop fold), never at footer arrival.
            (
                "late_content_after_footer",
                [
                    StreamChunk(content_delta="ans"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources: s"),
                    StreamChunk(content_delta="LATE"),
                ],
            ),
            (
                "footer_arrives_before_any_content",
                [
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources: s"),
                    StreamChunk(content_delta="LATE"),
                ],
            ),
        ],
    )
    def test_mirror(self, name: str, chunks: list[StreamChunk]) -> None:
        stamped = [*chunks]
        # Ride usage on the finish chunk so the strict gate passes.
        for i, c in enumerate(stamped):
            if c.finish_reason:
                stamped[i] = StreamChunk(finish_reason=c.finish_reason, usage=self._USAGE)
        displayed, committed = self._mirror(stamped)
        assert displayed == committed
