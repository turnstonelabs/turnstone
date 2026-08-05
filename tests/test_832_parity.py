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

    Every transform cites its D12 row.  Pre-fold trees (fixtures being
    captured) never reach this — capture mode writes and exits.
    """
    expected = json.loads(json.dumps(baseline))  # deep copy
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
