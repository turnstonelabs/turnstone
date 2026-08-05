"""#832 replay-parity pins: fixed chunk scripts ⇒ identical observable turn.

Baseline fixtures under ``tests/data/parity_832/`` were captured from the
pre-fold streaming path (``UPDATE_832_PARITY=1``; the capturing commit's
``session.py`` is byte-identical to main, which is what makes them THE
old-world record).  Each test replays a scenario on the current tree and
compares the full record — UI event sequence, committed-message
projection, mid-stream usage — against the baseline, after applying the
transforms the design's D12 table RULED (each transform cites its row).
A difference outside a ruled transform is a fold regression.

Do not regenerate baselines casually: they encode the old world.  A
legitimate regeneration (a ruled delta superseding capture) updates the
matching transform below in the same commit, or the pin loses its
meaning.
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


def _apply_ruled_deltas(name: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """Transform an old-world record into the post-fold expectation.

    Every transform cites its D12 row (docs/design/832-main-loop-model-turn.md).
    Pre-fold trees (fixtures being captured) never reach this — capture mode
    writes and exits.
    """
    expected = json.loads(json.dumps(baseline))  # deep copy

    if name == "info_postfinish_footer":
        # D12 row 1 (RULED adopt-drain): the trailing citations footer
        # enters the COMMITTED content (conditional fold: non-blank answer,
        # "\n\n" separator) and streams as content — post-carry-flush, so
        # displayed ordering matches committed — instead of an ephemeral
        # info bubble that never survived reload.
        footer = "Sources:\n- example.com/page"
        expected["result"]["content"] += "\n\n" + footer
        events = [e for e in expected["ui_events"] if e != ["info", footer]]
        end = events.index(["stream_end", ""])
        events[end:end] = [["content", "\n\n" + footer]]
        expected["ui_events"] = events

    elif name == "no_finish_clean_exhaust":
        # D12 row 2 (RULED adopt strict gate): a stream that exhausts with
        # no finish reason no longer commits its partial silently — it is a
        # mid-stream death: the re-issue ladder finalizes the display and
        # re-drives the turn (_MID_STREAM_RETRIES times), then the terminal
        # arm finalizes+discards and the retryable error surfaces.
        expected["raised"] = "IncompleteStreamError"
        expected["result"] = None
        retry_theater = []
        for attempt in (1, 2):
            retry_theater += [
                ["stream_end", ""],
                [
                    "info",
                    f"[stream died mid-response (IncompleteStreamError) — retrying in "
                    f"{2 ** (attempt - 1)}s ({attempt}/2)]",
                ],
                ["stream_discarded", ""],
                ["thinking_start", ""],
                ["thinking_stop", ""],
            ]
        expected["ui_events"] = (
            [["thinking_stop", ""]] + retry_theater + [["stream_end", ""], ["stream_discarded", ""]]
        )

    elif name == "think_tags_split_across_chunks":
        # D12 residue-trim row (RULED adopt; V14.1): the COMMITTED content
        # takes the drain's single edge trim when a tag was consumed; the
        # displayed stream keeps the raw residue ("\n\nAnswer") — the
        # accepted snapshot-vs-history whitespace class.
        expected["result"]["content"] = expected["result"]["content"].lstrip("\n")

    return expected


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_parity(name: str) -> None:
    record = run_scenario(name)
    if UPDATE:
        write_fixture(name, record)
        pytest.skip("captured baseline")
    assert fixture_path(name).exists(), (
        f"missing baseline {name!r}; run UPDATE_832_PARITY=1 at a pre-fold tree"
    )
    expected = _apply_ruled_deltas(name, load_fixture(name))
    assert record == expected
