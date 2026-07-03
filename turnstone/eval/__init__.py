"""turnstone.eval — headless measurement substrate and its measure-only CLI.

The measurement substrate lives in :mod:`turnstone.eval.core`; the measure-only
``turnstone-eval`` command lives in :mod:`turnstone.eval.cli`. The prompt
optimizer that consumes this substrate lives in :mod:`turnstone.optimizer`.

The core public API is re-exported here for back-compatibility with existing
importers (e.g. ``from turnstone.eval import score_run, _match_action``).
"""

from turnstone.eval.core import (
    HeadlessSession,
    NullUI,
    _match_action,
    _run_iteration,
    _run_single_test,
    score_run,
)

__all__ = [
    "HeadlessSession",
    "NullUI",
    "_match_action",
    "_run_iteration",
    "_run_single_test",
    "score_run",
]
