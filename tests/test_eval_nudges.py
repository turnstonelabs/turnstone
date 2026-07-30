"""Harness tests for the idle-nudge behavioral eval (no LLM calls).

The LLM loop itself is exercised by real sweeps against a live
endpoint; these pin everything deterministic around it — cell fixture
validity, stimulus shape, scoring semantics, and the stub client's
network inertness — so a sweep failure means the model, not the
harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from typing import Any

import pytest

from turnstone.console.coordinator_idle_observer import _ACTIVE_CHILD_STATES
from turnstone.core.metacognition import (
    NUDGE_IDLE_TASKS_CHILD_DOOR,
    NUDGE_IDLE_TASKS_CHILD_SLOT,
    NUDGE_IDLE_TASKS_ID_SLOT,
)
from turnstone.core.session import COORDINATOR_TOOLS
from turnstone.core.storage._registry import (
    get_storage,
    init_storage,
    is_storage_initialized,
    reset_storage,
)
from turnstone.core.workstream import WorkstreamKind
from turnstone.eval.nudges import (
    _LIVE_CHILD_STATES,
    _MUTATING_TASKS_ACTIONS,
    _NO_CAVEAT_SKIP_REASON,
    _TASKS_ACTION_KEY,
    _TASKS_SCHEMA_ACTIONS,
    ARM_BARE_CONTINUE,
    ARM_NO_CAVEAT,
    ARM_NUDGE,
    ARM_PAIR_TF,
    KNOWN_ARMS,
    _live_children,
    _seed_child_transcripts,
    _seed_tasks,
    _seed_transcript,
    _StubCoordinatorClient,
    _validate_cells,
    build_stimulus,
    render_tasks_body,
    run_nudge_response,
    score_nudge_run,
)
from turnstone.eval.scenarios.nudges import NUDGE_CELLS

_LIVE_TOOL_NAMES = {t["function"]["name"] for t in COORDINATOR_TOOLS}

# The minimum a cell must seed to render a non-empty ``idle_tasks``
# body.  Every validator fixture below that is not ABOUT the seed list
# carries it, so each refusal test trips the one refusal it names.
_OPEN_TASK: dict[str, Any] = {"title": "audit auth.py for CSRF handling", "status": "pending"}

# A child row in a LIVE state — what the ``no_caveat`` arm needs to be
# measuring anything at all.  ``running`` rather than ``idle`` only
# because it also satisfies the pair arms' narrower active predicate,
# so one constant serves both classes of fixture.  It carries its
# assignment because every child must — the hollow-child refusal binds
# any cell that seeds children, scenery included.
_LIVE_CHILD: dict[str, Any] = {
    "ws_id": "ws-c1",
    "name": "auditor",
    "state": "running",
    "transcript": [{"role": "user", "content": "audit auth.py for CSRF handling"}],
}

# The transcript rows that make a child row legal in each state class:
# every child carries its assignment; an idle child also carries the
# completion turn the wait synthesis will surface.
_ASSIGNMENT_ROW: dict[str, str] = {"role": "user", "content": "audit auth.py for CSRF handling"}
_FINDINGS_ROW: dict[str, str] = {"role": "assistant", "content": "Audit complete: 3 findings."}


def _trip_cells() -> list[dict[str, Any]]:
    """One cell per registered refusal, in registration order, each
    tripping ONLY the check it is paired with.

    Shared by the structural guard (every check is reachable) and the
    ordering guard (every refusal fires before the canary probe), so
    the two cannot drift apart or cover different sets.
    """
    return [
        {"id": "X_t", "arms": ARM_NUDGE, "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": [], "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": ["not_an_arm"], "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": [ARM_NUDGE, ARM_NUDGE], "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": [ARM_NUDGE], "children": None, "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": [ARM_NUDGE], "tasks": [{"status": "pending"}]},
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "transcript": [{"content": "no role"}],
        },
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "tool_stubs": ["not-a-mapping"],
        },
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "expect_state": {3: {"status": "done"}},
        },
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "forbid_actions": [{"args": {}}],
        },
        {"id": "X_t", "arms": [ARM_NUDGE], "tasks": [{"title": "t", "status": "done"}]},
        # Parked + open together: production parks the nudge on any
        # ``needs_user`` row, so this cell would score a body no
        # coordinator can receive.  Open task present (so the
        # open-task check passes) and no pair/caveat arm, so only the
        # park check can refuse it.
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK, {"title": "waiting on you", "status": "needs_user"}],
        },
        {"id": "X_t", "arms": [ARM_PAIR_TF], "children": [], "tasks": [_OPEN_TASK]},
        {"id": "X_t", "arms": [ARM_NO_CAVEAT], "children": [], "tasks": [_OPEN_TASK]},
        # The hollow child, in its historical shape: a bare roster row
        # with nothing inside — exactly what the round-8 void was made
        # of.  Shape-legal (so the seedable check passes) and arm-legal
        # (no pair/caveat arm), so only the hollow-child check can
        # refuse it.
        {
            "id": "X_t",
            "arms": [ARM_NUDGE],
            "children": [{"ws_id": "ws-c1", "name": "auditor", "state": "idle"}],
            "tasks": [_OPEN_TASK],
        },
    ]


def _tasks_call(action: str, turn: int = 0, **args: Any) -> dict[str, Any]:
    """One ``tasks`` tool-log entry, keyed with the tool's own primary
    key so a rename of the action argument moves these fixtures too."""
    return {
        "tool": "tasks",
        "args": {_TASKS_ACTION_KEY: action, **args},
        "result": "",
        "turn": turn,
    }


def _cell(cell_id: str) -> dict[str, Any]:
    return next(c for c in NUDGE_CELLS if c["id"] == cell_id)


def _envelope(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "tasks": list(rows)}


@pytest.fixture
def eval_storage(tmp_path):
    reset_storage()
    init_storage("sqlite", path=str(tmp_path / "eval.db"), run_migrations=False)
    storage = get_storage()
    storage.register_workstream(
        "coord-eval-1",
        user_id="eval-user",
        name="eval-coordinator",
        kind=WorkstreamKind.COORDINATOR,
        state="idle",
    )
    yield storage
    reset_storage()


class TestCellFixtures:
    def test_nudge_cells_use_live_tool_names(self):
        """A forbid/expect matcher with a misspelt tool name silently
        never matches — which reads as a false PASS.  Every name must be
        on the coordinator's real wire."""
        for cell in NUDGE_CELLS:
            named = [a["tool"] for a in cell.get("forbid_actions", [])]
            named += [a["tool"] for a in cell.get("expect_actions", {}).get("actions", [])]
            for name in named:
                assert name in _LIVE_TOOL_NAMES, f"{cell['id']}: unknown tool {name!r}"

    def test_cell_ids_unique_and_arms_known(self):
        """The arm vocabulary is imported, never re-declared: a cell's
        own list is the only route into the grid, so a literal copy here
        could drift from the runner's."""
        ids = [c["id"] for c in NUDGE_CELLS]
        assert len(ids) == len(set(ids))
        for cell in NUDGE_CELLS:
            assert set(cell["arms"]) <= KNOWN_ARMS, cell["id"]

    def test_every_cell_seed_spec_passes_real_validation(self, eval_storage):
        """Fixture drift guard: every cell's tasks seed through the REAL
        tasks_add (status vocabulary, length caps, renderability) — a
        vocabulary change that orphans a fixture fails here, not mid-
        sweep."""
        for cell in NUDGE_CELLS:
            client = _StubCoordinatorClient(
                eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
            )
            id_map = _seed_tasks(client, "coord-eval-1", cell)
            assert len(id_map) == len(cell.get("tasks", []))
            assert all(tid.startswith("tsk_") for tid in id_map.values())
            # Clean the envelope between cells (same coord ws).
            env = client.tasks_get("coord-eval-1")
            for row in env.get("tasks", []):
                client.tasks_remove("coord-eval-1", task_id=row["id"])

    def test_bad_seed_fixture_fails_loudly(self, eval_storage):
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        bad = {"id": "X", "tasks": [{"title": "t", "status": "not-a-status"}]}
        with pytest.raises(ValueError, match="rejected"):
            _seed_tasks(client, "coord-eval-1", bad)


class TestSweepValidation:
    """Every fixture error a cell can carry is refused at config time —
    before the canary probe spends a model round-trip — because each of
    them otherwise surfaces as a plausible red 0% that is
    indistinguishable in the result JSON from a real model failure.
    Three shapes of harm: a stimulus that cannot be built (an unknown
    arm), a run that dies mid-sweep on a dereference (a cell with no
    id, a child with no ws_id, ``children: None``, a seed row with no
    title, a transcript row with no role), and a run that completes but
    is filed under a label it did not measure (a pair arm with no active
    child, a caveat-ablation arm with no LIVE child, a body arm with no
    open task, an ``expect_state`` index naming a task nobody seeded, a
    repeated arm or cell id whose second batch overwrites the first).
    """

    def test_shipped_cells_pass(self):
        _validate_cells(list(NUDGE_CELLS))

    def test_unknown_arm_is_refused_at_sweep_start(self):
        """The class E2 left open when it deleted the CLI override: with
        the flag gone, a cell declaration is the ONLY way to name an arm
        that does not exist."""
        cell = {
            "id": "X_typo",
            "arms": [ARM_NUDGE, "pair_tff"],
            "children": [],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_typo" in message, message
        assert "pair_tff" in message, message

    def test_unknown_arm_is_refused_before_the_pair_arm_check(self):
        """A misspelt PAIR arm is an unknown arm, not a childless pair
        arm — the diagnostic must name the typo, not send the author
        hunting for a child to seed."""
        cell = {
            "id": "X_typo_pair",
            "arms": ["pair_ttf"],
            "children": [],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "pair_ttf" in message and "unknown" in message, message
        assert "active state" not in message, message

    def test_arms_that_are_not_a_string_list_are_refused(self):
        cell = {"id": "X_arms_scalar", "arms": ARM_NUDGE, "tasks": [_OPEN_TASK]}
        with pytest.raises(SystemExit, match="X_arms_scalar"):
            _validate_cells([cell])

    def test_an_empty_arms_list_is_refused(self):
        """``all()`` over ``[]`` is vacuously true, so the shape refusal
        passed a literal empty list and the cell swept as an empty
        result — present in the file, contributing no runs to any rate,
        read as a complete grid.  Distinct from the ABSENT key, which
        deliberately defaults to the plain nudge arm."""
        cell = {"id": "X_no_arms", "arms": [], "tasks": [_OPEN_TASK]}
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_no_arms" in message and "empty" in message, message

        # The control for the "distinct from absent" clause.
        _validate_cells([{"id": "X_default", "tasks": [_OPEN_TASK]}])

    @pytest.mark.parametrize(
        ("row", "reject_fragment"),
        [
            # The [0] escape verbatim: the sibling pending row satisfies
            # the open-task check, so only the write-path dry-run can
            # refuse the cell.
            ({"title": "b", "status": "todo"}, "invalid status"),
            ({"title": "x" * 250}, "title too long"),
            ({"title": "b", "note": "n" * 250}, "note too long"),
            # Renderability: zero-widths survive ``str.strip`` (they are
            # not whitespace), so the title-presence guard passes and
            # only ``tasks_add``'s sanitiser can reject the row.
            ({"title": chr(0x200B) * 3}, "renderable"),
        ],
    )
    def test_a_seed_row_the_write_path_rejects_is_refused(self, row, reject_fragment):
        """Validation BY CONSTRUCTION: the refusal set is ``tasks_add``'s
        own, exercised through the real ``_seed_tasks`` against a
        throwaway envelope.  A hand-written restatement covered exactly
        one of these four rows (none), and each miss was a plausible red
        0% filed as a model result after the canary round-trip was paid.
        """
        cell = {"id": "X_unseedable", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK, row]}
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_unseedable" in message, message
        assert reject_fragment in message, message

    def test_tool_stubs_that_are_not_a_mapping_are_refused(self):
        for stubs in (["not-a-mapping"], "wait_for_workstream", 7):
            cell = {
                "id": "X_stub_shape",
                "arms": [ARM_NUDGE],
                "tasks": [_OPEN_TASK],
                "tool_stubs": stubs,
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert "X_stub_shape" in message and "mapping" in message, message

    def test_a_tool_stub_queue_that_is_not_result_dicts_is_refused(self):
        for queue in ({"complete": True}, ["a string result"], [{"ok": 1}, None]):
            cell = {
                "id": "X_stub_queue",
                "arms": [ARM_NUDGE],
                "tasks": [_OPEN_TASK],
                "tool_stubs": {"wait_for_workstream": queue},
            }
            with pytest.raises(SystemExit, match="X_stub_queue"):
                _validate_cells([cell])

    def test_well_shaped_tool_stubs_pass(self):
        """The refusal keys on the run path's consumed shape, so the
        shape the run path consumes must pass."""
        _validate_cells(
            [
                {
                    "id": "X_stub_ok",
                    "arms": [ARM_NUDGE],
                    "tasks": [_OPEN_TASK],
                    "tool_stubs": {"wait_for_workstream": [{"complete": False, "results": {}}]},
                }
            ]
        )

    def test_a_repeated_arm_is_refused(self):
        """The runner keys ``cell_out`` by arm name, so the second batch
        overwrites the first: a full batch of live generations bought
        and then discarded, with the file reporting half the runs it
        paid for and no sign the rest existed."""
        cell = {"id": "X_dupe_arm", "arms": [ARM_NUDGE, ARM_NUDGE], "tasks": [_OPEN_TASK]}
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_dupe_arm" in message and ARM_NUDGE in message, message

    def test_a_repeated_cell_id_is_refused(self):
        """Same overwrite mechanism one level up: ``out["cells"]`` is
        keyed by cell id.  ``test_cell_ids_unique_and_arms_known``
        covers the SHIPPED list; this covers the sweep."""
        cells = [
            {"id": "X_twice", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
            {"id": "X_twice", "arms": [ARM_BARE_CONTINUE], "tasks": [_OPEN_TASK]},
        ]
        with pytest.raises(SystemExit, match="X_twice"):
            _validate_cells(cells)

    def test_a_cell_without_an_id_is_refused(self):
        """The runner dereferences ``case['id']`` per cell and per run,
        so this kills the sweep mid-flight — tens of minutes of live
        generation in, and before anything is written to --output."""
        cells = [
            {"id": "X_ok", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
            {"arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
        ]
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells(cells)
        assert "position 1" in str(excinfo.value), str(excinfo.value)

    def test_a_child_without_a_ws_id_is_refused(self):
        cell = {
            "id": "X_child_no_ws",
            "arms": [ARM_PAIR_TF],
            "children": [{"name": "auditor", "state": "running"}],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit, match="ws_id"):
            _validate_cells([cell])

    def test_children_declared_as_none_is_refused(self):
        """The validator's own ``or []`` used to coerce this away while
        the run seeder iterated it unguarded — the validator passing a
        cell that then dies on the first run of the sweep."""
        cell = {
            "id": "X_children_none",
            "arms": [ARM_NUDGE],
            "children": None,
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit, match="X_children_none"):
            _validate_cells([cell])

    def test_a_seed_task_without_a_title_is_refused(self):
        cell = {"id": "X_task_no_title", "arms": [ARM_NUDGE], "tasks": [{"status": "pending"}]}
        with pytest.raises(SystemExit, match="title"):
            _validate_cells([cell])

    def test_a_transcript_row_without_a_role_is_refused(self):
        cell = {
            "id": "X_no_role",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "transcript": [{"content": "hello"}],
        }
        with pytest.raises(SystemExit, match="role"):
            _validate_cells([cell])

    def test_a_seeded_tool_call_without_a_name_is_refused(self):
        cell = {
            "id": "X_no_call_name",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "transcript": [{"role": "assistant", "tool_calls": [{"args": {}}]}],
        }
        with pytest.raises(SystemExit, match="X_no_call_name"):
            _validate_cells([cell])

    def test_an_expect_state_index_with_no_seeded_task_is_refused(self):
        """The unseeded index maps to no task id, so every run scores
        ``task None missing from the final envelope`` — a full grid at
        0% whose message reads as the coordinator having deleted a task
        it was never given."""
        cell = {
            "id": "X_unseeded_index",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "expect_state": {1: {"status": "done"}},
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_unseeded_index" in message and "seed index 1" in message, message

    def test_expect_state_indices_that_are_seeded_pass(self):
        """Both spellings the fixtures use — an int key and the string a
        JSON round-trip leaves behind — must be accepted, or the refusal
        would refuse real cells."""
        for key in (0, "0"):
            cell = {
                "id": "X_seeded_index",
                "arms": [ARM_NUDGE],
                "tasks": [_OPEN_TASK],
                "expect_state": {key: {"status": "done"}},
            }
            _validate_cells([cell])

    def test_a_cell_with_no_open_task_is_refused_for_body_arms(self):
        """``format_idle_tasks_nudge`` returns '' for an empty open list
        and the runner appends the turn unconditionally, so the model
        would get fence markers around an empty body — a wire production
        never sends, with every run still filed under the nudge
        heading."""
        for status in ("done", "blocked", "needs_user"):
            cell = {
                "id": f"X_closed_{status}",
                "arms": [ARM_NUDGE],
                "tasks": [{"title": "already handled", "status": status}],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert f"X_closed_{status}" in message, message
            assert "open status" in message, message

    def test_the_body_arms_set_still_derives_by_subtraction(self):
        """The open-task refusal keys on ``_TASKS_BODY_ARMS``, which is
        ``KNOWN_ARMS`` minus the one no-body arm — so the ``nudge`` arm
        (now the counts body's own measurement) stays covered through
        the derivation, and an arm added to the vocabulary joins the
        refusal unless someone explicitly excludes it.  Pinned because
        the arm retirements shrank the set by subtraction too, and a
        hand-maintained list could have quietly dropped a survivor."""
        from turnstone.eval.nudges import _TASKS_BODY_ARMS

        assert KNOWN_ARMS - {ARM_BARE_CONTINUE} == _TASKS_BODY_ARMS
        assert ARM_NUDGE in _TASKS_BODY_ARMS

    def test_a_bare_continue_only_cell_needs_no_open_task(self):
        """The refusal is scoped to arms that RENDER a body: the
        operator-poke baseline injects no nudge at all, so an empty open
        list costs it nothing."""
        _validate_cells(
            [
                {
                    "id": "X_poke_only",
                    "arms": [ARM_BARE_CONTINUE],
                    "tasks": [{"title": "already handled", "status": "done"}],
                }
            ]
        )

    def test_a_forbid_matcher_without_a_tool_is_refused(self):
        """``_match_action`` reads ``expected['tool']`` with no default,
        and it runs in the SCORER — so this shape dies after the run's
        generations have already been bought."""
        cell = {
            "id": "X_matcher_no_tool",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "forbid_actions": [{"args_pattern": {"initial_message": "audit"}}],
        }
        with pytest.raises(SystemExit, match="X_matcher_no_tool"):
            _validate_cells([cell])

    def test_expect_actions_without_an_actions_list_is_refused(self):
        cell = {
            "id": "X_expect_shape",
            "arms": [ARM_NUDGE],
            "tasks": [_OPEN_TASK],
            "expect_actions": {"mode": "contains_any"},
        }
        with pytest.raises(SystemExit, match="X_expect_shape"):
            _validate_cells([cell])

    def test_known_arms_is_the_complete_arm_vocabulary(self):
        """The validator is only as good as this set.  Both directions:
        every ``ARM_*`` constant the module declares is in it (one added
        without registering would be refused as an unknown arm), and
        every member is an arm ``build_stimulus`` really accepts (one
        dropped from the builder would pass validation and then raise
        mid-sweep — the class the validator exists to kill)."""
        from turnstone.eval import nudges as nudges_module

        declared = {
            value
            for name, value in vars(nudges_module).items()
            if name.startswith("ARM_") and isinstance(value, str)
        }
        assert declared == set(KNOWN_ARMS)

        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        kids = [{"ws_id": "ws-c1", "name": "auditor", "state": "running"}]
        for arm in sorted(KNOWN_ARMS):
            assert build_stimulus(arm, envelope=env, children=kids), arm

    def test_every_known_arm_is_accepted(self):
        """The refusal keys on :data:`KNOWN_ARMS`, so no legal arm may
        trip it — an over-tight set would refuse real sweeps."""
        for arm in sorted(KNOWN_ARMS):
            cell = {
                "id": f"X_{arm}",
                "arms": [arm],
                "children": [_LIVE_CHILD],
                "tasks": [_OPEN_TASK],
            }
            _validate_cells([cell])

    def test_pair_arm_on_childless_cell_is_refused_at_sweep_start(self):
        # Iterates the module's own pair-arm set (one member since the
        # ordering ablation retired) so a future pair variant joins this
        # refusal coverage by registration, not by edit.
        from turnstone.eval.nudges import _PAIR_ARMS

        assert {ARM_PAIR_TF} == _PAIR_ARMS
        for arm in sorted(_PAIR_ARMS):
            cell = {
                "id": f"X_childless_{arm}",
                "arms": [arm],
                "children": [],
                "tasks": [_OPEN_TASK],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert f"X_childless_{arm}" in message, message
            assert "active state" in message, message

    def test_pair_arm_with_only_inactive_children_is_refused(self):
        """``idle`` is precisely the C6b state: a child that exists but
        that the observer would not nudge about.  The child carries a
        full transcript so the PAIR-ARM refusal is the only one this
        cell can trip — a hollow child would trip its own check too and
        the assertion would ride on registration order."""
        cell = {
            "id": "X_idle_child",
            "arms": [ARM_NUDGE, ARM_PAIR_TF],
            "children": [
                {
                    "ws_id": "ws-c1",
                    "name": "auditor",
                    "state": "idle",
                    "transcript": [_ASSIGNMENT_ROW, _FINDINGS_ROW],
                }
            ],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        assert "X_idle_child" in str(excinfo.value)

    def test_no_caveat_arm_on_a_childless_cell_is_refused(self):
        """The arm measures what the caveat buys WHERE IT PROTECTS.  On a
        childless cell it measures the childless body — which is the body
        the conditional gives the plain ``nudge`` arm there anyway — so
        the pair would read as an ablation result while being one
        stimulus under two headings.  Same honesty rule as the pair-arm
        refusal."""
        cell = {
            "id": "X_no_caveat_childless",
            "arms": [ARM_NUDGE, ARM_NO_CAVEAT],
            "children": [],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_no_caveat_childless" in message, message
        assert "live state" in message, message

    def test_no_caveat_arm_with_only_terminal_children_is_refused(self):
        """``closed`` / ``deleted`` are the strings the close and reap
        paths write, and they are NOT ``WorkstreamState`` members — a row
        in one of them is gone, so the cell is childless for the body's
        purposes however many rows it declares."""
        for state in ("closed", "deleted"):
            cell = {
                "id": f"X_no_caveat_{state}",
                "arms": [ARM_NO_CAVEAT],
                # A full transcript, so the terminal-state refusal is
                # the only one the cell can trip.
                "children": [
                    {
                        "ws_id": "ws-c1",
                        "name": "auditor",
                        "state": state,
                        "transcript": [_ASSIGNMENT_ROW, _FINDINGS_ROW],
                    }
                ],
                "tasks": [_OPEN_TASK],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            assert f"X_no_caveat_{state}" in str(excinfo.value), state

    def test_no_caveat_arm_passes_on_any_live_child_including_idle(self):
        """EXISTENCE-in-a-live-state, deliberately broader than the pair
        arms' ACTIVE predicate: C6b's idle child — a child that finished
        with results nobody collected — is precisely the row the
        stopped-child fact line protects, and it fails the pair-arm check
        (``test_pair_arm_with_only_inactive_children_is_refused``) while
        passing this one.  Both are correct."""
        for state in sorted(_LIVE_CHILD_STATES):
            cell = {
                "id": f"X_no_caveat_live_{state}",
                "arms": [ARM_NUDGE, ARM_NO_CAVEAT],
                # Each state's LEGAL transcript: assignment always, and
                # the completion turn only where the hollow-child check
                # demands one (idle).  error deliberately gets the
                # assignment alone — errored-before-output is a
                # production-reachable world, pinned separately by
                # ``test_an_error_child_is_not_bound_by_the_findings_rule``.
                "children": [
                    {
                        "ws_id": "ws-c1",
                        "name": "auditor",
                        "state": state,
                        "transcript": [_ASSIGNMENT_ROW]
                        + ([_FINDINGS_ROW] if state == "idle" else []),
                    }
                ],
                "tasks": [_OPEN_TASK],
            }
            _validate_cells([cell])

    def test_a_childless_cell_is_refused_under_an_override(self):
        """Item: an override sweep can no longer force children content
        into a childless world, STRUCTURALLY.  A childless cell renders
        the formatter's childless branch, whose literal door cut would
        silently strip a candidate that quotes the shipped
        blocked-on-a-child branch — so the sweep refuses the cell at
        config validation, naming the cell and the reason, before any
        model round-trip.  Without an override the same cell passes:
        the shipped tail is exactly what the cut is defined against.
        """
        cell = {
            "id": "X_override_childless",
            "arms": [ARM_NUDGE],
            "children": [],
            "tasks": [_OPEN_TASK],
        }
        _validate_cells([cell])  # fine against the shipped body
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell], override_active=True)
        message = str(excinfo.value)
        assert "X_override_childless" in message, message
        assert "body-override" in message, message
        assert "live state" in message, message

    def test_a_terminal_only_children_cell_is_refused_under_an_override(self):
        """The refusal keys on the LIVE derivation, not the raw list: a
        cell whose every child row is terminal is a childless world to
        the formatter, so a raw-list predicate would wave it through and
        the door cut would maul the candidate anyway."""
        cell = {
            "id": "X_override_terminal",
            "arms": [ARM_NUDGE],
            "children": [
                {
                    "ws_id": "ws-c1",
                    "name": "auditor",
                    "state": "closed",
                    "transcript": [_ASSIGNMENT_ROW, _FINDINGS_ROW],
                }
            ],
            "tasks": [_OPEN_TASK],
        }
        _validate_cells([cell])
        with pytest.raises(SystemExit, match="X_override_terminal"):
            _validate_cells([cell], override_active=True)

    def test_a_live_child_cell_passes_under_an_override(self):
        """The refusal is exactly as wide as the maul: any live-state
        child row keeps the door in play, so the cell measures the
        candidate as authored (idle included — the C6b class is a legal
        override cell)."""
        for state in ("running", "idle"):
            cell = {
                "id": f"X_override_{state}",
                "arms": [ARM_NUDGE],
                "children": [
                    {
                        "ws_id": "ws-c1",
                        "name": "auditor",
                        "state": state,
                        "transcript": [_ASSIGNMENT_ROW]
                        + ([_FINDINGS_ROW] if state == "idle" else []),
                    }
                ],
                "tasks": [_OPEN_TASK],
            }
            _validate_cells([cell], override_active=True)

    def test_the_shipped_cells_that_survive_an_override_are_the_children_ones(self):
        """The shipped grid under ``--body-override``: exactly the two
        children-bearing cells validate; every childless cell is refused
        by name.  (An override sweep therefore runs with ``--cells
        C6_co_delivery,C6b_stranded_children`` or a fixture edit — never
        with a silently mauled childless body.)"""
        with_children = [c for c in NUDGE_CELLS if _live_children(c.get("children") or [])]
        assert {c["id"] for c in with_children} == {"C6_co_delivery", "C6b_stranded_children"}
        _validate_cells(with_children, override_active=True)
        for cell in NUDGE_CELLS:
            if cell in with_children:
                continue
            with pytest.raises(SystemExit, match=cell["id"]):
                _validate_cells([cell], override_active=True)

    def test_the_cells_that_declare_no_caveat_are_the_ones_with_children(self):
        """The shipped grid, named rather than assumed.

        The subset direction is the honesty rule the validator enforces
        for any cell (an arm that cannot measure anything is refused);
        the two ids are this sweep's choice, and they are the two cells
        that seed a live child today — C6's running one and C6b's idle
        one, the pair that covers both disjuncts of the sentence.  A
        third children-bearing cell may decline the arm (it costs a full
        batch of live generations), which is why only the subset is
        asserted in that direction.
        """
        declaring = {c["id"] for c in NUDGE_CELLS if ARM_NO_CAVEAT in c.get("arms", [])}
        with_children = {c["id"] for c in NUDGE_CELLS if _live_children(c.get("children") or [])}
        assert declaring == {"C6_co_delivery", "C6b_stranded_children"}
        assert declaring <= with_children

    def test_missing_children_key_is_refused(self):
        cell = {"id": "X_no_children_key", "arms": [ARM_PAIR_TF], "tasks": [_OPEN_TASK]}
        with pytest.raises(SystemExit, match="X_no_children_key"):
            _validate_cells([cell])

    def test_cell_with_an_active_child_passes(self):
        for state in sorted(_ACTIVE_CHILD_STATES):
            cell = {
                "id": "X_active",
                "arms": [ARM_PAIR_TF, ARM_NUDGE],
                "children": [
                    {
                        "ws_id": "ws-c1",
                        "name": "auditor",
                        "state": state,
                        "transcript": [_ASSIGNMENT_ROW],
                    }
                ],
                "tasks": [_OPEN_TASK],
            }
            _validate_cells([cell])

    def test_a_stateless_child_is_seeded_active_and_accepted(self):
        """The disagreement that was a live trap: the run seeder
        registers a child with no ``state`` as running (an ACTIVE
        state), while the validator's filter read the missing key as
        ``None`` and refused the cell — a diagnostic contradicting the
        state the run would have seeded.  One named default, read
        through one accessor, is what makes them agree; the stimulus
        must render the same child too."""
        cell = {
            "id": "X_stateless_child",
            "arms": [ARM_PAIR_TF],
            "children": [{"ws_id": "ws-c1", "name": "auditor", "transcript": [_ASSIGNMENT_ROW]}],
            "tasks": [_OPEN_TASK],
        }
        _validate_cells([cell])

        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        turns = build_stimulus(ARM_PAIR_TF, envelope=env, children=cell["children"])
        assert [t.get("_source") for t in turns[1:]] == ["idle_tasks", "idle_children"]
        assert "running" in turns[2]["content"], turns[2]["content"]

    def test_a_child_transcript_that_is_not_a_list_is_refused(self):
        """The transcript seeder iterates it unguarded — the same
        dereference-mirror rule as ``children: None``."""
        cell = {
            "id": "X_transcript_scalar",
            "arms": [ARM_NUDGE],
            "children": [{"ws_id": "ws-c1", "state": "running", "transcript": "do the audit"}],
            "tasks": [_OPEN_TASK],
        }
        with pytest.raises(SystemExit) as excinfo:
            _validate_cells([cell])
        message = str(excinfo.value)
        assert "X_transcript_scalar" in message and "not a list" in message, message

    def test_a_child_transcript_row_without_role_or_content_is_refused(self):
        """The seeder dereferences both keys directly; a row missing
        either dies in the harness on every run of the cell."""
        for row in ({"content": "no role"}, {"role": "assistant"}, "not-a-row"):
            cell = {
                "id": "X_transcript_row",
                "arms": [ARM_NUDGE],
                "children": [{"ws_id": "ws-c1", "state": "running", "transcript": [row]}],
                "tasks": [_OPEN_TASK],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert "X_transcript_row" in message and "role" in message, message

    def test_a_hollow_child_is_refused(self):
        """The round-8 shape, refused at sweep start: a bare roster row
        with nothing inside.  ``inspect`` is real in every run, so any
        cell's model can look inside a child and find ``messages: []``
        contradicting the roster — a world ``spawn`` cannot produce
        (it writes the assignment before the child ever runs).  Both
        the absent key and the explicit empty list are the same
        hollowness."""
        for transcript_shape in ({}, {"transcript": []}):
            cell = {
                "id": "X_hollow_child",
                "arms": [ARM_NUDGE],
                "children": [
                    {"ws_id": "ws-c1", "name": "auditor", "state": "running", **transcript_shape}
                ],
                "tasks": [_OPEN_TASK],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert "X_hollow_child" in message, message
            assert "seeds no transcript" in message, message
            assert "assignment" in message, message

    def test_an_idle_child_without_findings_is_refused(self):
        """The C6b void, refused at sweep start: idle resolves the
        synthesized wait ``complete`` and the wait carries the child's
        last assistant message — an idle child without one tells the
        model the work finished and shows nothing was produced, so it
        correctly redoes the work and the forbidden rate measures the
        fixture.  The refusal is asked of the REAL reader, so an
        assistant row whose content is whitespace is as hollow as no
        assistant row at all."""
        for transcript in (
            [_ASSIGNMENT_ROW],
            [_ASSIGNMENT_ROW, {"role": "assistant", "content": "   "}],
        ):
            cell = {
                "id": "X_idle_no_findings",
                "arms": [ARM_NUDGE],
                "children": [
                    {
                        "ws_id": "ws-c1",
                        "name": "auditor",
                        "state": "idle",
                        "transcript": transcript,
                    }
                ],
                "tasks": [_OPEN_TASK],
            }
            with pytest.raises(SystemExit) as excinfo:
                _validate_cells([cell])
            message = str(excinfo.value)
            assert "X_idle_no_findings" in message, message
            assert "surfaces" in message and "assistant" in message, message

    def test_a_running_child_with_only_its_assignment_passes(self):
        """The mid-work world: assignment, no output yet.  The wait
        synthesis answers ``message: None`` for a running child by
        production's own rule, so nothing about this shape lies."""
        _validate_cells(
            [
                {
                    "id": "X_mid_work",
                    "arms": [ARM_NUDGE],
                    "children": [
                        {
                            "ws_id": "ws-c1",
                            "name": "auditor",
                            "state": "running",
                            "transcript": [_ASSIGNMENT_ROW],
                        }
                    ],
                    "tasks": [_OPEN_TASK],
                }
            ]
        )

    def test_an_error_child_is_not_bound_by_the_findings_rule(self):
        """``error`` is terminal like ``idle``, but errored-before-output
        is a production-REACHABLE world (``_last_assistant_text``
        documents it) and the wait surfaces the persisted ``last_error``
        or the honest sentinel — so an error child with only its
        assignment is a legal fixture, while a message-less one is still
        refused by the transcript rule."""
        _validate_cells(
            [
                {
                    "id": "X_error_child",
                    "arms": [ARM_NUDGE],
                    "children": [
                        {
                            "ws_id": "ws-c1",
                            "name": "auditor",
                            "state": "error",
                            "transcript": [_ASSIGNMENT_ROW],
                        }
                    ],
                    "tasks": [_OPEN_TASK],
                }
            ]
        )

    def test_non_pair_cells_need_no_children(self):
        _validate_cells(
            [
                {"id": "X_plain", "arms": [ARM_NUDGE, ARM_BARE_CONTINUE], "tasks": [_OPEN_TASK]},
                {"id": "X_default_arm", "tasks": [_OPEN_TASK]},
            ]
        )

    def test_a_later_bad_cell_still_refuses(self):
        """The scan covers the whole list, not just its head."""
        for bad in (
            {"arms": [ARM_PAIR_TF], "children": [], "tasks": [_OPEN_TASK]},
            {"arms": ["not_an_arm"], "tasks": [_OPEN_TASK]},
        ):
            cells = [
                {"id": "X_ok", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
                {"id": "X_bad", **bad},
            ]
            with pytest.raises(SystemExit, match="X_bad"):
                _validate_cells(cells)

    def test_no_refusal_is_reachable_only_behind_another_ones_early_out(self):
        """The structural property the check table buys.

        The pair-arm class used to sit behind ``if not declared:
        continue``, so anything appended after it did not run for cells
        declaring no pair arm — exactly the cell class a new refusal is
        most likely to be about.  Asserted on the artefact: every
        registered check fires for a cell that trips ONLY it, whatever
        its position in the table.
        """
        from turnstone.eval import nudges as nudges_module

        trips = _trip_cells()
        assert len(trips) == len(nudges_module._CELL_CHECKS), (
            "every registered check needs a cell that trips only it"
        )
        for i, (check, cell) in enumerate(zip(nudges_module._CELL_CHECKS, trips, strict=True)):
            shadowed = [c.__name__ for c in nudges_module._CELL_CHECKS[:i] if c(cell) is not None]
            assert not shadowed, f"{check.__name__}'s cell is refused earlier by {shadowed}"
            assert check(cell) is not None, check.__name__
            with pytest.raises(SystemExit, match="X_t"):
                _validate_cells([cell])


class TestRefusalsPrecedeTheCanary:
    """A bad fixture must cost ZERO model round-trips.

    ``_validate_cells`` runs before ``tool_call_canary``, and the probe
    is the first thing in the sweep that touches the endpoint.  Proved
    on the artefact rather than by reading the call order: the probe is
    replaced with a sentinel that raises on ENTRY, so a refusal that
    reached it would surface as the sentinel instead of the ``SystemExit``.

    Both directions, because "everything raises before the probe" is
    also what a validator that refuses every cell would produce: each
    bad cell must raise ``SystemExit`` WITHOUT the sentinel, and a good
    cell must raise the sentinel — i.e. really get that far.
    """

    _SENTINEL = "canary-probe-entered"

    @classmethod
    def _sweep(
        cls, monkeypatch, cells: list[dict[str, Any]], *, override: str | None = None
    ) -> None:
        from turnstone.eval import nudges as nudges_module

        def _probe_sentinel(*a: Any, **k: Any) -> bool:
            raise RuntimeError(cls._SENTINEL)

        def _never_runs(**kw: Any) -> dict[str, Any]:
            raise AssertionError("a run reached the model lane")

        monkeypatch.setattr(nudges_module, "tool_call_canary", _probe_sentinel)
        monkeypatch.setattr(nudges_module, "_run_single_nudge", _never_runs)
        run_nudge_response(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            cells=cells,
            n_runs=1,
            body_override_text=override,
        )

    def test_a_good_cell_reaches_the_probe(self, monkeypatch):
        """The control.  Without it, every assertion below would also
        pass against a validator that refused everything."""
        cell = {"id": "X_good", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]}
        with pytest.raises(RuntimeError, match=self._SENTINEL):
            self._sweep(monkeypatch, [cell])

    def test_the_shipped_cells_reach_the_probe(self, monkeypatch):
        with pytest.raises(RuntimeError, match=self._SENTINEL):
            self._sweep(monkeypatch, list(NUDGE_CELLS))

    def test_every_refusal_fires_before_the_probe(self, monkeypatch):
        bad: list[dict[str, Any]] = [
            *_trip_cells(),
            {"arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},  # no id
            # The construction-based seed refusals: an unseedable status,
            # an over-cap title, an unrenderable title.  Each must cost
            # zero model round-trips, like every other refusal.
            {
                "id": "X_t",
                "arms": [ARM_NUDGE],
                "tasks": [_OPEN_TASK, {"title": "b", "status": "todo"}],
            },
            {"id": "X_t", "arms": [ARM_NUDGE], "tasks": [{"title": "x" * 250}]},
            {"id": "X_t", "arms": [ARM_NUDGE], "tasks": [{"title": chr(0x200B) * 3}]},
        ]
        for cell in bad:
            with pytest.raises(SystemExit) as excinfo:
                self._sweep(monkeypatch, [cell])
            assert self._SENTINEL not in str(excinfo.value), cell

    def test_the_override_refusal_fires_before_the_probe(self, monkeypatch):
        """The override-only class binds at the same point as every
        other refusal: config time, zero model round-trips.  Both
        directions, like the class docstring demands — the childless
        cell refuses WITHOUT the sentinel, and the same cell without an
        override reaches the probe (so the refusal really is
        override-conditional)."""
        cell = {"id": "X_t", "arms": [ARM_NUDGE], "children": [], "tasks": [_OPEN_TASK]}
        with pytest.raises(SystemExit) as excinfo:
            self._sweep(monkeypatch, [cell], override="Candidate wording.")
        assert self._SENTINEL not in str(excinfo.value)
        with pytest.raises(RuntimeError, match=self._SENTINEL):
            self._sweep(monkeypatch, [cell])

    def test_a_duplicate_cell_id_fires_before_the_probe(self, monkeypatch):
        """The one refusal that reads the whole list rather than a
        single cell, so it needs its own two-cell fixture."""
        dupes = [
            {"id": "X_twice", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
            {"id": "X_twice", "arms": [ARM_BARE_CONTINUE], "tasks": [_OPEN_TASK]},
        ]
        with pytest.raises(SystemExit) as excinfo:
            self._sweep(monkeypatch, dupes)
        assert self._SENTINEL not in str(excinfo.value)


class TestStimulus:
    def test_arm_turn_shapes(self):
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        kids = [{"ws_id": "ws-c1", "name": "auditor", "state": "running"}]

        nudge = build_stimulus(ARM_NUDGE, envelope=env, children=kids)
        assert [t["role"] for t in nudge] == ["user", "system"]
        assert nudge[0]["content"] == "" and nudge[0]["_source"] == "system_nudge"
        assert nudge[1]["_source"] == "idle_tasks"

        # Same single-turn shape as ``nudge`` — the two arms differ by
        # one ARGUMENT to the production formatter, nothing else.
        no_caveat = build_stimulus(ARM_NO_CAVEAT, envelope=env, children=kids)
        assert [t["role"] for t in no_caveat] == ["user", "system"]
        assert no_caveat[1]["_source"] == "idle_tasks"

        tf = build_stimulus(ARM_PAIR_TF, envelope=env, children=kids)
        assert [t.get("_source") for t in tf[1:]] == ["idle_tasks", "idle_children"]

        bare = build_stimulus(ARM_BARE_CONTINUE, envelope=env, children=[])
        assert bare == [{"role": "user", "content": "continue"}]

        with pytest.raises(ValueError):
            build_stimulus("nope", envelope=env, children=[])

    def test_pair_arms_omit_the_children_turn_when_no_child_is_active(self):
        """An empty children body is a turn production never sends: the
        observer short-circuits on empty text before enqueueing, and an
        appended turn would fence open/close markers around nothing.
        "No active child" covers both no children at all and children
        whose state is outside the observer's active set."""
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})

        none_at_all = build_stimulus(ARM_PAIR_TF, envelope=env, children=[])
        assert [t.get("_source") for t in none_at_all[1:]] == ["idle_tasks"]

        idle_only = build_stimulus(
            ARM_PAIR_TF,
            envelope=env,
            children=[{"ws_id": "ws-c1", "name": "auditor", "state": "idle"}],
        )
        assert [t.get("_source") for t in idle_only[1:]] == ["idle_tasks"]

    def test_children_turn_filters_on_the_observers_own_state_set(self):
        """The active-state filter is the observer's frozenset, not a
        copy of its membership — every state IN it renders the turn."""
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        for state in sorted(_ACTIVE_CHILD_STATES):
            kids = [{"ws_id": "ws-c1", "name": "auditor", "state": state}]
            turns = build_stimulus(ARM_PAIR_TF, envelope=env, children=kids)
            assert [t.get("_source") for t in turns[1:]] == ["idle_tasks", "idle_children"], state

    def test_bodies_are_production_rendered(self):
        """The eval carries no copy of the body: the real formatter over
        the really-seeded envelope — the counts opener, the per-child
        observed fact line, the id block and the escape branch,
        populated with the seeded id and the seeded child, and NO task
        text."""
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        body = render_tasks_body(env, children=[("ws-c1", "running")])
        assert body.startswith("You still have 1 open task: 0 in_progress, 1 pending.")
        assert "Child ws-c1 is still running; check before redoing anything it owns." in body
        assert "needs_user" in body
        # The seeded id reaches the block AND the branch calls; the
        # seeded title reaches neither.
        assert chr(10) + "  - tsk_1 (pending)" in body
        assert "task_id='tsk_1'" in body
        assert "child_ws_id='ws-c1'" in body
        assert "audit auth.py" not in body

    def test_the_no_caveat_arm_is_the_formatters_other_branch(self):
        """No string surgery: the arm's body is what the production
        formatter renders for a caller that observed no children, so the
        ablation cannot drift from the body that ships.

        THE ARM ABLATES THE BODY'S WHOLE CHILDREN AWARENESS, by design.
        The ``children`` pairs govern the per-child fact lines, the
        blocked-on-a-child branch and that branch's slots, because in
        production all of them answer ONE storage read and a second
        derivation of it is a state where two answers can disagree.  So
        the arm asks a single clean question — does this body need to
        mention children at all? — rather than the narrower "what does
        the sentence buy?" it was named for.  That is a better question,
        which is why the arm is worth keeping; the name stays because
        archived sweeps report under it.

        This scope is not drift.  It is what "the formatter's other
        branch" MEANS now that one read governs every children-bearing
        element, and the alternative — a knob that stripped the fact
        lines while keeping the branch — would render a body no
        coordinator receives, which is the one thing an eval arm may
        never do.
        """
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        kept = render_tasks_body(env, children=[("ws-c1", "running")])
        ablated = render_tasks_body(env, children=[])

        assert "Child ws-c1 is still running" in kept
        assert "child_ws_id='ws-c1'" in kept
        # NOTHING about children survives the ablation — asserted as
        # absence of the topic, so a reworded branch cannot pass.
        for absent in ("child", "Child", "wait_for_workstream", "list_workstreams"):
            assert absent not in ablated, absent
        # ...and nothing else moves.  The counts opener, the id block and
        # the remaining branches survive: this arm ablates an
        # observation, not the body.
        assert ablated.startswith("You still have 1 open task")
        assert "needs_user" in ablated
        assert "task_id='tsk_1'" in ablated
        assert chr(10) + "  - tsk_1 (pending)" in ablated

        turns = build_stimulus(ARM_NO_CAVEAT, envelope=env, children=[_LIVE_CHILD])
        assert turns[1]["content"] == ablated

    def test_the_nudge_arm_derives_the_children_facts_from_the_cells_children(self):
        """The ``nudge`` arm renders what a coordinator in that cell's
        state would really receive, which since the observer's probe
        landed is two different bodies by cell class — and for the
        children-bearing class, the OBSERVED per-state fact lines.

        The predicate is EXISTENCE in a live state, the observer's own:
        an idle child keeps its stopped-with-immediate-wait line (it
        may hold results nobody collected) while a terminal row does not
        count as a child at all.  Deriving through ``_live_children``
        rather than the pair arms' active filter is what those two rows
        detect — and the STATE riding beside the id is what makes the
        idle row render as the fact it is instead of a running claim or
        a hedge.
        """
        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})

        def _body(children: list[dict[str, str]]) -> str:
            turns = build_stimulus(ARM_NUDGE, envelope=env, children=children)
            return str(turns[1]["content"])

        assert "Child " not in _body([])
        assert "Child ws-c1 is still running; check before redoing anything it owns." in _body(
            [_LIVE_CHILD]
        )
        idle_body = _body([{"ws_id": "ws-c1", "name": "auditor", "state": "idle"}])
        assert (
            "Child ws-c1 has stopped — "
            "wait_for_workstream returns immediately for it."
        ) in idle_body
        assert "Child " not in _body([{"ws_id": "ws-c1", "name": "auditor", "state": "closed"}])
        # No hedge about an observed state, in any cell class.
        for children in ([], [_LIVE_CHILD], [{"ws_id": "ws-c1", "state": "idle"}]):
            body = _body(children)
            assert "may still be running" not in body, children
            assert "while you worked" not in body, children

    def test_ragged_envelope_rows_do_not_raise(self):
        env = _envelope(
            {"id": "tsk_1", "title": None, "status": "pending", "note": 42},
            "not-a-dict-row",
        )
        body = render_tasks_body(env, children=[("ws-c1", "running")])
        assert body.startswith("You still have 1 open task")

    def test_seed_transcript_pairs_calls_with_results(self):
        wires = _seed_transcript(_cell("C6_co_delivery"))
        roles = [w["role"] for w in wires]
        assert roles == ["user", "assistant", "tool"]
        call = wires[1]["tool_calls"][0]
        assert call["function"]["name"] == "spawn_workstream"
        assert json.loads(call["function"]["arguments"])["name"] == "auditor"
        assert wires[2]["tool_call_id"] == call["id"]


_SKIPPED_ARMS = [
    (ARM_NO_CAVEAT, _NO_CAVEAT_SKIP_REASON),
]
_SKIPPED_ARM_NAMES = [arm for arm, _reason in _SKIPPED_ARMS]


class TestBodyOverrideSkip:
    """The ablation arm is defined against the body that SHIPS, and
    ``--body-override`` replaces that body with unknown text.

    ``no_caveat`` cuts a LITERAL, so the failure against candidate text
    is silent in the worst way: the likeliest candidate of all, one
    that keeps the caveat sentence verbatim while rewording another
    paragraph, still contains it, so the cut lands and the sweep files
    a caveat-stripped candidate under the tuning heading — a real
    number reported against a stimulus nobody chose.  (The retired
    ``no_provenance`` arm had the positional variant of this failure;
    it died with the paragraph it measured.)

    The runner skips the arm instead of refusing the sweep, because a
    cell's arm list is the only route into the grid — exiting would kill
    every tuning sweep over the cells that declare it.
    """

    @staticmethod
    def _grid(
        monkeypatch, *, override: str | None, arm: str = ARM_NO_CAVEAT
    ) -> tuple[dict[str, Any], list[str]]:
        from turnstone.eval import nudges as nudges_module

        ran: list[str] = []

        def _fake_run(**kw: Any) -> dict[str, Any]:
            ran.append(kw["arm"])
            return {"pass": True, "failures": [], "forbidden": [], "actions": ["tasks"]}

        monkeypatch.setattr(nudges_module, "tool_call_canary", lambda *a, **k: True)
        monkeypatch.setattr(nudges_module, "_run_single_nudge", _fake_run)
        out = run_nudge_response(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            cells=[
                {
                    "id": "X_tuning",
                    "arms": [ARM_NUDGE, arm],
                    # The live child is load-bearing for ``no_caveat``:
                    # without it the sweep-start validator refuses the
                    # cell and the SystemExit lands before any skip logic
                    # runs, which would read as a validator bug rather
                    # than as this fixture missing a row.
                    "children": [_LIVE_CHILD],
                    "tasks": [_OPEN_TASK],
                }
            ],
            n_runs=2,
            body_override_text=override,
        )
        return out["cells"]["X_tuning"], ran

    @pytest.mark.parametrize(("arm", "reason"), _SKIPPED_ARMS)
    def test_an_ablation_arm_with_body_override_is_skipped(self, monkeypatch, capsys, arm, reason):
        cell, ran = self._grid(
            monkeypatch, override="Candidate wording, no paragraph break.", arm=arm
        )

        assert ran == [ARM_NUDGE, ARM_NUDGE], "the ablation arm must not reach a model"
        assert cell[arm]["runs"] == []
        assert reason in capsys.readouterr().out

    @pytest.mark.parametrize(("arm", "reason"), _SKIPPED_ARMS)
    def test_the_skip_keeps_the_uniform_per_arm_shape(self, monkeypatch, arm, reason):
        """Every archived result file carries the same four per-arm keys
        and consumers iterate them, so the skip may ADD a key and never
        drop one.  Zero runs with null rates is what marks it as a
        non-measurement — a real arm never reports either."""
        cell, _ran = self._grid(
            monkeypatch, override="Candidate wording, no paragraph break.", arm=arm
        )

        measured, skipped = cell[ARM_NUDGE], cell[arm]
        assert set(skipped) == set(measured) | {"skipped"}
        assert skipped == {
            "n": 0,
            "pass_rate": None,
            "forbidden_rate": None,
            "runs": [],
            "skipped": reason,
        }
        assert json.loads(json.dumps(cell)) == cell  # the grid is still writable

    @pytest.mark.parametrize("arm", _SKIPPED_ARM_NAMES)
    def test_an_ablation_arm_runs_when_no_override_is_in_play(self, monkeypatch, arm):
        """The guard must not cost the production sweep its arm: with no
        override the ablation is against the shipped body, which is
        exactly what it measures."""
        cell, ran = self._grid(monkeypatch, override=None, arm=arm)

        assert ran == [ARM_NUDGE, ARM_NUDGE, arm, arm]
        assert "skipped" not in cell[arm]
        assert cell[arm]["n"] == 2

    def test_a_children_cell_measures_a_door_quoting_candidate_as_authored(self):
        """The LIKELIEST candidate shape there is: one that keeps the
        shipped blocked-on-a-child branch verbatim while rewording
        another paragraph.  On a children cell — the only cell class an
        override sweep still admits — the formatter's door cut never
        runs (children are present), so the candidate's quoted branch
        survives byte-exact and its slots are populated exactly as
        production would populate the shipped text.  The childless cell
        that would have mauled this candidate is refused at config time
        (``test_a_childless_cell_is_refused_under_an_override``), which
        is the structural half of the same protection.

        Asserted through the REAL override context manager, because the
        mechanism under test is the one that swaps the module constant.
        """
        from turnstone.eval import nudges as nudges_module

        env = _envelope({"id": "tsk_1", "title": "audit auth.py", "status": "pending"})
        candidate = "Reworded opening paragraph: reconcile your list." + NUDGE_IDLE_TASKS_CHILD_DOOR
        with nudges_module._body_override(candidate):
            rendered = render_tasks_body(env, children=[("ws-c1", "running")])
            through_the_arm = build_stimulus(ARM_NUDGE, envelope=env, children=[_LIVE_CHILD])
            # The control: the childless branch really would cut the
            # quoted door out of this candidate — the maul the refusal
            # and the skip exist to make unreachable.
            mauled = render_tasks_body(env, children=[])

        # Formatter-built facts lead; the candidate rides after them
        # with its quoted door substituted, never stripped.
        assert rendered.startswith(
            "You still have 1 open task: 0 in_progress, 1 pending."
            + chr(10)
            + "Child ws-c1 is still running; check before redoing anything it owns."
        )
        assert "record the link and wait instead of redoing its work" in rendered
        assert "child_ws_id='ws-c1'" in rendered
        assert NUDGE_IDLE_TASKS_CHILD_SLOT not in rendered
        # The candidate's own task-id slot has no open-list machinery in
        # this candidate, so it substitutes from the seeded set like the
        # shipped tail's would.
        assert "task_id='tsk_1'" in rendered
        assert NUDGE_IDLE_TASKS_ID_SLOT not in rendered
        assert through_the_arm[1]["content"] == rendered
        assert "record the link and wait instead of redoing its work" not in mauled, (
            "the control must show the cut really lands on candidate text"
        )


class TestBodyFingerprint:
    """A result file must say WHICH body produced its numbers.

    The ``nudge`` heading named one stimulus per cell only while the
    caveat was unconditional; now that it is conditioned on an observed
    fact the heading covers two, by cell class.  None of the archived
    result files records body text, hash or revision, so without this
    the break between an archived sweep and a later one is folklore.
    """

    @staticmethod
    def _sweep(
        monkeypatch, *, override: str | None, cells: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        from turnstone.eval import nudges as nudges_module

        monkeypatch.setattr(nudges_module, "tool_call_canary", lambda *a, **k: True)
        monkeypatch.setattr(
            nudges_module,
            "_run_single_nudge",
            lambda **kw: {"pass": True, "failures": [], "forbidden": [], "actions": ["tasks"]},
        )
        return run_nudge_response(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            cells=cells
            if cells is not None
            else [
                {"id": "X_a", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]},
                {
                    "id": "X_b",
                    "arms": [ARM_NUDGE],
                    "children": [_LIVE_CHILD],
                    "tasks": [_OPEN_TASK],
                },
            ],
            n_runs=1,
            body_override_text=override,
        )

    def test_the_fingerprint_names_the_shipped_tail_and_every_cell(self, monkeypatch):
        from turnstone.core import metacognition as metacog

        out = self._sweep(monkeypatch, override=None)

        assert out["body"]["override"] is False
        assert (
            out["body"]["tail_sha256"]
            == hashlib.sha256(metacog.NUDGE_IDLE_TASKS_TAIL.encode()).hexdigest()
        )
        assert set(out["body"]["cells"]) == {"X_a", "X_b"}
        # The two cell classes the one ``nudge`` heading now covers,
        # stamped by class: ``X_a`` seeds no children and ``X_b`` seeds a
        # live one, so the fact is read off the cells rather than
        # asserted flat, and a stamp that stopped tracking the stimulus
        # fails here.
        assert out["body"]["cells"] == {
            "X_a": {"children_present": False},
            "X_b": {"children_present": True},
        }
        assert json.loads(json.dumps(out["body"])) == out["body"]

    def test_the_fingerprint_is_of_the_effective_tail_under_an_override(self, monkeypatch):
        """Taken INSIDE the override context, so the hash is of the text
        the runs really saw — a fingerprint of the shipped tail under a
        candidate sweep would be a false provenance stamp, which is
        worse than none.

        An override sweep admits only children-bearing cells (the
        childless class is refused at config time), so the default
        two-cell fixture cannot serve here — both cells carry a live
        child, and both stamp the ``children_present`` fact their runs
        really rendered, DERIVED rather than forced by a flag.
        """
        from turnstone.core import metacognition as metacog

        candidate = "Candidate wording under test."
        out = self._sweep(
            monkeypatch,
            override=candidate,
            cells=[
                {
                    "id": "X_b",
                    "arms": [ARM_NUDGE],
                    "children": [_LIVE_CHILD],
                    "tasks": [_OPEN_TASK],
                },
                {
                    "id": "X_c",
                    "arms": [ARM_NUDGE],
                    "children": [
                        {
                            "ws_id": "ws-c9",
                            "name": "researcher",
                            "state": "idle",
                            "transcript": [
                                _ASSIGNMENT_ROW,
                                _FINDINGS_ROW,
                            ],
                        }
                    ],
                    "tasks": [_OPEN_TASK],
                },
            ],
        )

        assert out["body"]["override"] is True
        assert out["body"]["tail_sha256"] == hashlib.sha256(candidate.encode()).hexdigest()
        assert out["body"]["cells"] == {
            "X_b": {"children_present": True},
            "X_c": {"children_present": True},
        }
        # ...and the constant is restored afterwards, so the stamp is
        # not evidence of a leaked override.
        assert candidate != metacog.NUDGE_IDLE_TASKS_TAIL

    def test_the_fingerprint_stays_out_of_the_arm_mapping(self, monkeypatch):
        """Consumers iterate ``cells[id]`` as arms, so a non-arm key
        there would read as an arm that reported no runs."""
        out = self._sweep(monkeypatch, override=None)

        for cell in out["cells"].values():
            assert set(cell) <= KNOWN_ARMS, cell


class TestScoring:
    def test_forbidden_action_flags_without_failing_state(self):
        case = {"forbid_actions": [{"tool": "spawn_workstream"}]}
        log = [{"tool": "spawn_workstream", "args": {"name": "x"}, "result": "", "turn": 0}]
        r = score_nudge_run(log, _envelope(), case, {})
        assert not r["pass"] and r["forbidden"] and not r["failures"]

    def test_expect_state_checks_status_note_and_link(self):
        case = {
            "expect_state": {
                0: {"status": "needs_user", "note_nonempty": True},
                1: {"status": "in_progress", "child_ws_id": "ws-c1"},
            }
        }
        good = _envelope(
            {"id": "tsk_a", "status": "needs_user", "note": "need the token"},
            {"id": "tsk_b", "status": "in_progress", "child_ws_id": "ws-c1"},
        )
        r = score_nudge_run([], good, case, {0: "tsk_a", 1: "tsk_b"})
        assert r["pass"], r["failures"]

        bad = _envelope(
            {"id": "tsk_a", "status": "needs_user", "note": "   "},
            {"id": "tsk_b", "status": "in_progress", "child_ws_id": ""},
        )
        r2 = score_nudge_run([], bad, case, {0: "tsk_a", 1: "tsk_b"})
        assert not r2["pass"] and len(r2["failures"]) == 2

    def test_missing_task_is_a_state_failure(self):
        case = {"expect_state": {0: {"status": "done"}}}
        r = score_nudge_run([], _envelope(), case, {0: "tsk_gone"})
        assert not r["pass"] and "missing" in r["failures"][0]

    def test_require_stop_flags_work_after_bookkeeping(self):
        case = {"require_stop": True}
        log = [
            _tasks_call("update"),
            {"tool": "spawn_workstream", "args": {}, "result": "", "turn": 1},
        ]
        r = score_nudge_run(log, _envelope(), case, {})
        assert not r["pass"] and "stop" in r["failures"][0]

    def test_allow_after_bookkeeping_permits_escalation_surfacing(self):
        """``notify`` after the bookkeeping is the escalation reaching
        the operator's channel — the POINT of a legit stop, not a
        violation of it.  Anything outside the allowlist still fails."""
        case = {"require_stop": True, "allow_after_bookkeeping": ["notify"]}
        ok = [
            _tasks_call("update"),
            {"tool": "notify", "args": {"message": "need the token"}, "result": "", "turn": 1},
        ]
        assert score_nudge_run(ok, _envelope(), case, {})["pass"]
        stray = ok + [{"tool": "list_nodes", "args": {}, "result": "", "turn": 2}]
        assert not score_nudge_run(stray, _envelope(), case, {})["pass"]

    def test_no_bookkeeping_gets_its_own_label(self):
        """A run that never called ``tasks`` has no bookkeeping to have
        kept working AFTER.  The old fallback started the tail at index
        0 and reported read-only wandering as "kept working after
        bookkeeping" — a sequence that never happened, and a label that
        merged two different residual classes into one bucket in the
        result JSONs the residual analysis reads."""
        case = {"require_stop": True}
        log = [
            {"tool": "list_workstreams", "args": {}, "result": "", "turn": 0},
            {"tool": "list_workstreams", "args": {}, "result": "", "turn": 1},
        ]
        r = score_nudge_run(log, _envelope(), case, {})
        assert not r["pass"]
        (stop,) = [f for f in r["failures"] if f.startswith("stop:")]
        assert "never called tasks" in stop, stop
        assert "after bookkeeping" not in stop, stop

        # ...and the other class still names bookkeeping, so the two
        # aggregate apart.
        with_tasks = [_tasks_call("update")] + log
        (other,) = [
            f
            for f in score_nudge_run(with_tasks, _envelope(), case, {})["failures"]
            if f.startswith("stop:")
        ]
        assert "kept working after bookkeeping" in other, other

    def test_a_tasks_read_is_not_bookkeeping(self):
        """``action='list'`` records nothing.  Scoring on the TOOL NAME
        reported a run that listed its tasks and then dispatched work as
        "kept working after bookkeeping" — naming a step that never
        happened, the same describing-a-run-that-never-happened class
        the never-called-tasks label was split out to remove, left open
        for the read case."""
        case = {"require_stop": True}
        log = [
            _tasks_call("list"),
            {"tool": "spawn_workstream", "args": {}, "result": "", "turn": 1},
        ]
        (stop,) = [
            f for f in score_nudge_run(log, _envelope(), case, {})["failures"] if f[:5] == "stop:"
        ]
        assert "read tasks but never recorded state" in stop, stop
        assert "after bookkeeping" not in stop, stop
        assert "never called tasks" not in stop, stop

    def test_the_three_stop_labels_aggregate_apart(self):
        """One residual per label and no overlap: a bucket that matched
        two of them would merge classes back together in the result JSON
        the residual analysis reads.

        The fourth log is a REJECTED write plus the stray: its envelope
        was never written, so it joins the recorded-nothing bucket
        rather than minting a fourth label — which is what makes the
        three-way comment above the branch true for the rejected-write
        case too."""
        case = {"require_stop": True}
        stray = {"tool": "list_nodes", "args": {}, "result": "", "turn": 9}
        labels = set()
        for log in (
            [_tasks_call("update"), stray],
            [_tasks_call("list"), stray],
            [stray],
            [{**_tasks_call("update"), "ok": False}, stray],
        ):
            (stop,) = [
                f
                for f in score_nudge_run(log, _envelope(), case, {})["failures"]
                if f[:5] == "stop:"
            ]
            labels.add(stop.split(" (")[0])
        assert len(labels) == 3, labels

    def test_a_rejected_write_is_not_bookkeeping(self):
        """Effect, not intent: a schema-valid mutation whose call did
        NOT land (``ok: False`` — a hallucinated task_id, an invalid
        status, an over-cap note) left the envelope untouched, so it
        must neither anchor the stop-rule tail nor count as recorded
        state.  Both directions of the old conflation:

        * a TRAILING rejected write anchored the tail past every stray
          before it, converting a real stop violation into a full pass
          on a shipped merge-gate cell (C1's reproduction);
        * a run whose ONLY tasks call was rejected was labelled "kept
          working after bookkeeping" — a step that never happened — and
          bucketed with the recorded-then-over-reached class.
        """
        case = {"require_stop": True}
        stray = {"tool": "list_nodes", "args": {}, "result": "", "turn": 1}

        laundered = [
            _tasks_call("update"),
            stray,
            {**_tasks_call("update", turn=2, task_id="tsk_hallucinated"), "ok": False},
        ]
        r = score_nudge_run(laundered, _envelope(), case, {})
        (stop,) = [f for f in r["failures"] if f.startswith("stop:")]
        assert "kept working after bookkeeping" in stop, stop
        assert "list_nodes" in stop, stop

        only_rejected = [
            {**_tasks_call("update", task_id="tsk_hallucinated"), "ok": False},
            stray,
        ]
        r2 = score_nudge_run(only_rejected, _envelope(), case, {})
        (stop2,) = [f for f in r2["failures"] if f.startswith("stop:")]
        assert "after bookkeeping" not in stop2, stop2
        assert "read tasks but never recorded state" in stop2, stop2

    def test_a_landed_write_still_anchors_with_the_flag_present(self):
        """The flag only DEMOTES: an explicit ``ok: True`` behaves like
        the flagless fixtures every other test builds (the write site
        always stamps it; absence means a hand-built entry)."""
        case = {"require_stop": True}
        stray = {"tool": "list_nodes", "args": {}, "result": "", "turn": 1}
        log = [{**_tasks_call("update"), "ok": True}, stray]
        (stop,) = [
            f for f in score_nudge_run(log, _envelope(), case, {})["failures"] if f[:5] == "stop:"
        ]
        assert "kept working after bookkeeping" in stop, stop

    def test_a_trailing_read_does_not_launder_the_tail(self):
        """The other direction of the same conflation: anchoring on the
        last ``tasks`` call of ANY kind let a closing ``action='list'``
        move the anchor past every stray before it, so a run that
        recorded its state, kept working, and then re-read the list
        scored a clean pass."""
        case = {"require_stop": True}
        log = [
            _tasks_call("update"),
            {"tool": "list_nodes", "args": {}, "result": "", "turn": 1},
            _tasks_call("list", turn=2),
        ]
        r = score_nudge_run(log, _envelope(), case, {})
        (stop,) = [f for f in r["failures"] if f.startswith("stop:")]
        assert "kept working after bookkeeping" in stop, stop
        assert "list_nodes" in stop, stop

    def test_every_schema_action_is_classified_and_the_writes_are_bookkeeping(self):
        """The classifier is derived, not typed: the action vocabulary
        comes from the tool's own schema and the read half from
        production's own classifier, so this asserts the partition (a
        new action classified by neither would silently join the
        mutating set) and then that each mutating action really anchors
        the tail."""
        from turnstone.core.session import _TASKS_READ_ACTIONS, _TASKS_WRITE_ACTIONS

        assert _TASKS_SCHEMA_ACTIONS == _TASKS_READ_ACTIONS | _TASKS_WRITE_ACTIONS
        assert not (_TASKS_READ_ACTIONS & _TASKS_WRITE_ACTIONS)
        assert _MUTATING_TASKS_ACTIONS == _TASKS_WRITE_ACTIONS

        case = {"require_stop": True}
        stray = {"tool": "list_nodes", "args": {}, "result": "", "turn": 1}
        for action in sorted(_MUTATING_TASKS_ACTIONS):
            (stop,) = [
                f
                for f in score_nudge_run([_tasks_call(action), stray], _envelope(), case, {})[
                    "failures"
                ]
                if f.startswith("stop:")
            ]
            assert "kept working after bookkeeping" in stop, (action, stop)
        for action in sorted(_TASKS_READ_ACTIONS):
            (stop,) = [
                f
                for f in score_nudge_run([_tasks_call(action), stray], _envelope(), case, {})[
                    "failures"
                ]
                if f.startswith("stop:")
            ]
            assert "read tasks but never recorded state" in stop, (action, stop)

    def test_an_action_the_schema_does_not_declare_is_not_bookkeeping(self):
        """A hallucinated action is rejected by production, so the
        envelope is untouched — counting it as bookkeeping would credit
        the run with state it never recorded."""
        case = {"require_stop": True}
        stray = {"tool": "list_nodes", "args": {}, "result": "", "turn": 1}
        for args in ({"action": "delete"}, {}, {"action": None}, None):
            log = [{"tool": "tasks", "args": args, "result": "", "turn": 0}, stray]
            (stop,) = [
                f
                for f in score_nudge_run(log, _envelope(), case, {})["failures"]
                if f.startswith("stop:")
            ]
            assert "read tasks but never recorded state" in stop, (args, stop)

    def test_empty_tool_log_adds_no_stop_failure(self):
        """A run that called nothing is not a stop violation, and the
        new branch must not invent one."""
        r = score_nudge_run([], _envelope(), {"require_stop": True}, {})
        assert r["pass"], r["failures"]

    def test_no_bookkeeping_with_only_allowed_calls_adds_no_failure(self):
        """The stray filter is unchanged by the new label: a run with no
        ``tasks`` call whose every call is allowed still passes, whether
        the allowance comes from ``allow_after_bookkeeping`` or from the
        cell's own expected actions."""
        allowlisted = {"require_stop": True, "allow_after_bookkeeping": ["notify"]}
        log = [{"tool": "notify", "args": {"message": "need the token"}, "result": "", "turn": 0}]
        assert score_nudge_run(log, _envelope(), allowlisted, {})["pass"]

        expected = {
            "require_stop": True,
            "expect_actions": {
                "mode": "contains_any",
                "actions": [{"tool": "wait_for_workstream"}],
            },
        }
        waited = [{"tool": "wait_for_workstream", "args": {}, "result": "", "turn": 0}]
        assert score_nudge_run(waited, _envelope(), expected, {})["pass"]

    def test_a_read_only_run_is_not_a_stop_failure_on_its_own(self):
        """``tasks`` is in the allowlist, so the read itself is never a
        stray: a run that listed its tasks and then stopped adds no
        failure under any of the three branches."""
        case = {"require_stop": True}
        assert score_nudge_run([_tasks_call("list")], _envelope(), case, {})["pass"]
        assert score_nudge_run(
            [_tasks_call("update"), _tasks_call("list", turn=1)], _envelope(), case, {}
        )["pass"]

    def test_expected_actions_contains_any(self):
        case = {
            "expect_actions": {
                "mode": "contains_any",
                "actions": [{"tool": "wait_for_workstream"}],
            }
        }
        hit = [{"tool": "wait_for_workstream", "args": {}, "result": "", "turn": 0}]
        assert score_nudge_run(hit, _envelope(), case, {})["pass"]
        miss = [{"tool": "tasks", "args": {}, "result": "", "turn": 0}]
        assert not score_nudge_run(miss, _envelope(), case, {})["pass"]


class TestSessionConstruction:
    def test_coordinator_headless_session_constructs_with_coord_wire(self, eval_storage):
        """The passthrough seam: ``kind=COORDINATOR`` + ``coord_client``
        must reach ChatSession, the wire must be the coordinator tool
        list (not the CLI TOOLS constant), and the natural coordinator
        prompt must compose (no override)."""
        from openai import OpenAI

        from turnstone.eval.nudges import CoordinatorHeadlessSession

        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        session = CoordinatorHeadlessSession(
            client=OpenAI(base_url="http://eval.invalid/v1", api_key="x"),
            model="eval-model",
            coord_client=client,
            ws_id="coord-eval-1",
            user_id="eval-user",
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=32768,
        )
        wire = {t["function"]["name"] for t in session._eval_tools}
        assert "tasks" in wire and "wait_for_workstream" in wire
        assert "bash" not in wire  # the CLI tool set must NOT leak in
        assert session.auto_approve is True


def _register_child(storage, ws_id: str, name: str, state: str) -> None:
    storage.register_workstream(
        ws_id,
        user_id="eval-user",
        name=name,
        kind=WorkstreamKind.INTERACTIVE,
        parent_ws_id="coord-eval-1",
        state=state,
    )


class TestStubClient:
    def test_network_methods_are_inert(self, eval_storage):
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        out = client._post_url("http://eval.invalid/v1/api/route/x", {})
        assert "error" in out and "eval environment" in out["error"]

    def test_the_direct_storage_reads_are_not_overridden(self):
        """The stub's boundary is what CROSSES THE ROUTING PROXY, and it
        was once drawn at the class instead: ``inspect`` and
        ``list_children`` — direct-storage reads production serves off
        the same rows the run seeder registers — answered scripted
        errors and unfiltered fixture dicts.  Restoring either override
        re-manufactures forbidden-rate hits on C6b (see the C6b-shaped
        test below), so their absence is pinned structurally as well as
        behaviourally."""
        assert "inspect" not in _StubCoordinatorClient.__dict__
        assert "list_children" not in _StubCoordinatorClient.__dict__

    def test_c6b_recovery_reads_are_answered_not_refused(self, eval_storage):
        """The C6b shape: a stranded idle child, and a run that takes a
        SCORED-CORRECT recovery (``inspect_workstream`` is in the cell's
        ``expect_actions``).  The old class-wide override answered it
        with "unavailable in the eval environment", pushing the model
        toward re-running the audit — the forbidden action — so the
        harness manufactured hits on its own headline metric.  The real
        method serves the seeded row."""
        _register_child(eval_storage, "ws-child-a", "auditor", "idle")
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=[{"ws_id": "ws-child-a", "name": "auditor", "state": "idle"}],
        )
        out = client.inspect("ws-child-a")
        assert "error" not in out, out
        assert out["state"] == "idle" and out["name"] == "auditor"
        assert "messages" in out
        assert "unavailable" not in json.dumps(out)

    def test_inspect_misses_answer_with_the_production_shape(self, eval_storage):
        """An unknown id gets production's not-found payload — the
        did-you-mean/roster recovery shape — not a stub-invented
        transport error."""
        _register_child(eval_storage, "ws-child-a", "auditor", "idle")
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        miss = client.inspect("deadbeef" * 4)
        assert "error" in miss and "children" in miss
        assert "unavailable" not in miss["error"]

    def test_list_children_is_real_and_honours_the_filters(self, eval_storage):
        """Production's state filter, closed-exclusion and cross-tenant
        guard all apply — the roster the model cross-checks agrees with
        the seeded state and the nudge body, instead of echoing raw
        fixture dicts that ignore every filter the model asked for."""
        _register_child(eval_storage, "ws-child-a", "auditor", "idle")
        _register_child(eval_storage, "ws-child-b", "builder", "running")
        _register_child(eval_storage, "ws-child-c", "shipped", "closed")
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )

        default = client.list_children("coord-eval-1")
        assert sorted(c["name"] for c in default["children"]) == ["auditor", "builder"]
        # Production's row projection, not the fixture dict's keys.
        assert default["children"][0]["parent_ws_id"] == "coord-eval-1"
        assert "kind" in default["children"][0]

        running = client.list_children("coord-eval-1", state="running")
        assert [c["name"] for c in running["children"]] == ["builder"]

        assert client.list_children("some-other-coord") == {"children": [], "truncated": False}

    def test_wait_default_completes_with_stubbed_children(self, eval_storage):
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=[{"ws_id": "ws-c1", "name": "auditor", "state": "idle"}],
        )
        out = client.wait_for_workstream(["ws-c1"])
        assert out["complete"] is True
        assert out["results"]["ws-c1"]["name"] == "auditor"

    def test_wait_derives_its_answer_from_the_seeded_state(self, eval_storage):
        """The old stub returned a hardcoded terminal blob — every child
        "completed its work", including ones seeded RUNNING, so the pair
        arms measured a stub-invented completion.  Now: a running child
        is the still-running timeout shape, an idle one resolves, and
        the mode semantics are production's over the real terminal set.
        """
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=[
                {"ws_id": "ws-idle", "name": "auditor", "state": "idle"},
                {"ws_id": "ws-run", "name": "builder", "state": "running"},
            ],
        )
        running = client.wait_for_workstream(["ws-run"], timeout=120)
        assert running["complete"] is False
        assert running["results"]["ws-run"]["state"] == "running"
        assert running["results"]["ws-run"]["message"] is None
        assert running["elapsed"] == 120.0  # the budget a real wait would burn

        idle = client.wait_for_workstream(["ws-idle"])
        assert idle["complete"] is True
        assert idle["results"]["ws-idle"]["state"] == "idle"

        assert client.wait_for_workstream(["ws-idle", "ws-run"], mode="any")["complete"] is True
        assert client.wait_for_workstream(["ws-idle", "ws-run"], mode="all")["complete"] is False

    def test_wait_carries_the_seeded_childs_findings(self, eval_storage):
        """The pin the deepened world turns on: with the C6b child's
        transcript seeded through the real store, the synthesized wait's
        ``message`` is the child's completion message — the real
        ``_wait_message_for`` walks the temp DB and finds it — so "the
        audit finished" and "here is what it produced" arrive together,
        exactly as production's wait delivers them for a finished child.
        Before the fixtures deepened, this resolved ``complete`` with
        the no-recent-output sentinel, and the model correctly redid
        work the world claimed was done — the round-8 void."""
        case = _cell("C6b_stranded_children")
        child = case["children"][0]
        _register_child(eval_storage, child["ws_id"], child["name"], child["state"])
        _seed_child_transcripts(eval_storage, case)
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=case["children"],
        )
        out = client.wait_for_workstream([child["ws_id"]])
        assert out["complete"] is True
        snap = out["results"][child["ws_id"]]
        assert snap["message"] == child["transcript"][-1]["content"]
        assert "findings" in snap["message"]
        assert snap["truncated"] is False

    def test_inspect_serves_the_seeded_transcript(self, eval_storage):
        """The same world through the other reader: the real ``inspect``
        reconstructs the child's fixture transcript from the store the
        seeder wrote — assignment then findings, in order — so C6b's
        scored-correct recovery read finds the audit instead of the
        ``messages: []`` that voided the cell in round 8."""
        case = _cell("C6b_stranded_children")
        child = case["children"][0]
        _register_child(eval_storage, child["ws_id"], child["name"], child["state"])
        _seed_child_transcripts(eval_storage, case)
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        out = client.inspect(child["ws_id"])
        assert [(m["role"], m["content"]) for m in out["messages"]] == [
            (row["role"], row["content"]) for row in child["transcript"]
        ]

    def test_wait_on_an_unregistered_id_is_the_not_found_shape(self, eval_storage):
        """Production fails a wait fast on an unobservable member, with
        per-ws ``state="not_found"`` and the top-level error block.  The
        old stub fabricated success for ANY requested id, so a run that
        waited on a hallucinated id satisfied the cell's unqualified
        wait expectation."""
        _register_child(eval_storage, "ws-child-a", "auditor", "idle")
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=[{"ws_id": "ws-child-a", "name": "auditor", "state": "idle"}],
        )
        out = client.wait_for_workstream(["ws-nope"])
        assert out["complete"] is False
        assert out["results"]["ws-nope"]["state"] == "not_found"
        assert out["error"] and out["not_found"]
        # The hints are the real (storage-backed) recovery payload: the
        # roster names the child the temp DB really carries.
        assert [c["ws_id"] for c in out["children"]] == ["ws-child-a"]

    def test_wait_tolerates_a_nameless_child_row(self, eval_storage):
        """A row without ``name`` is legal everywhere else — the
        validator requires only ``ws_id`` and the seeder defaults the
        name — so the wait synthesis must not be the one consumer that
        raises on it."""
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            children=[{"ws_id": "ws-anon", "state": "idle"}],
        )
        out = client.wait_for_workstream(["ws-anon"])
        assert out["results"]["ws-anon"]["name"] == "child"

    def test_scripted_stub_takes_precedence(self, eval_storage):
        client = _StubCoordinatorClient(
            eval_storage,
            coord_ws_id="coord-eval-1",
            user_id="eval-user",
            tool_stubs={"wait_for_workstream": [{"complete": False, "results": {}}]},
        )
        assert client.wait_for_workstream(["ws-x"])["complete"] is False

    def test_tasks_stay_fully_real(self, eval_storage):
        """The point of the harness: tasks execute against the temp DB
        with production validation."""
        client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        row = client.tasks_add("coord-eval-1", title="real", status="pending")
        assert row["id"].startswith("tsk_")
        # Production validation, including the unrenderable reject —
        # the genuinely-invisible class, not angle brackets, which the
        # operator surfaces render.
        assert "error" in client.tasks_add("coord-eval-1", title=chr(0x200B) * 2)


class TestChildWorldThroughTheRunPath:
    """The C6b world end-to-end: the REAL ``_run_single_nudge`` seeds
    the shipped fixture, and the collect-first moves a model would make
    — wait, then inspect — are made from inside the scripted lane
    against the run's own coordinator client.

    This is the pin that survives a deleted call site: the validator
    refuses a hollow FIXTURE, but only a run-path test can catch the
    seeder call being dropped from ``_build_session`` — the fixture
    would stay deep while every run's world silently hollowed back to
    the round-8 shape.
    """

    def test_c6b_run_world_serves_the_findings_and_scores_the_collect(self, monkeypatch):
        from turnstone.eval import nudges as nudges_module

        case = _cell("C6b_stranded_children")
        child = case["children"][0]
        seen: dict[str, Any] = {}
        real_stub_cls = nudges_module._StubCoordinatorClient

        def _spy_coord(*a: Any, **kw: Any) -> Any:
            seen["coord_client"] = real_stub_cls(*a, **kw)
            return seen["coord_client"]

        class _CollectFirstLane(nudges_module.CoordinatorHeadlessSession):
            def _run_headless_loop(self, **kw: Any) -> list[dict[str, Any]]:
                client = seen["coord_client"]
                seen["wait"] = client.wait_for_workstream([child["ws_id"]], timeout=30)
                seen["inspect"] = client.inspect(child["ws_id"])
                # The collect-first move, as the tool loop would log it.
                return [
                    {
                        "tool": "wait_for_workstream",
                        "args": {"ws_ids": [child["ws_id"]]},
                        "result": "",
                        "ok": True,
                        "turn": 0,
                    }
                ]

        monkeypatch.setattr(nudges_module, "_StubCoordinatorClient", _spy_coord)
        monkeypatch.setattr(nudges_module, "CoordinatorHeadlessSession", _CollectFirstLane)

        result = nudges_module._run_single_nudge(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            case=case,
            arm=ARM_NUDGE,
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=32768,
            max_turns=2,
            test_timeout=30,
            verbose=False,
            log_prefix="test",
        )

        # The wait a model issues resolves at once AND says what the
        # child produced — completion and findings arrive together.
        findings = child["transcript"][-1]["content"]
        assert seen["wait"]["complete"] is True
        assert seen["wait"]["results"][child["ws_id"]]["message"] == findings
        # The inspect a model issues finds the audit inside the child.
        assert [(m["role"], m["content"]) for m in seen["inspect"]["messages"]] == [
            (row["role"], row["content"]) for row in child["transcript"]
        ]
        # And the scorer files the collect-first run as the cell's pass,
        # with nothing forbidden.
        assert result["pass"] is True, result
        assert result["forbidden"] == []


class TestToolLogEffectFlag:
    """The write site stamps ``ok`` from the executor's own error state.

    ``_is_mutating_tasks_call`` rules on that flag, so the stamp is the
    other half of the rejected-write fix: driven END-TO-END through the
    real ``_run_headless_loop`` -> ``_execute_tools`` -> ``_exec_tasks``
    chain against the temp DB, with only the model lane scripted, so a
    stamp that read intent (or nothing) instead of the executor's error
    state fails here rather than in a sweep.
    """

    @staticmethod
    def _scripted_lane(monkeypatch, tool_call_turns: list[list[dict[str, Any]]]) -> None:
        from turnstone.core.model_turn import ModelTurnResult
        from turnstone.core.trajectory import turn_from_dict as _turn_from_dict
        from turnstone.eval import core as core_module

        results = [
            ModelTurnResult(
                turn=_turn_from_dict(
                    {"role": "assistant", "content": "", "tool_calls": calls}
                    if calls
                    else {"role": "assistant", "content": "done"}
                ),
                finish_reason="tool_calls" if calls else "stop",
                usage=None,
                tool_calls=calls,
            )
            for calls in tool_call_turns
        ]
        sequence = iter(results)
        monkeypatch.setattr(core_module, "resolve_lane", lambda *a, **k: None)
        monkeypatch.setattr(core_module, "model_turn", lambda *a, **k: next(sequence))

    @staticmethod
    def _wire_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def test_a_rejected_tasks_write_is_stamped_not_ok(self, eval_storage, monkeypatch):
        from openai import OpenAI

        from turnstone.eval.nudges import CoordinatorHeadlessSession, _is_mutating_tasks_call

        coord_client = _StubCoordinatorClient(
            eval_storage, coord_ws_id="coord-eval-1", user_id="eval-user"
        )
        seeded = coord_client.tasks_add("coord-eval-1", title="real row", status="pending")
        session = CoordinatorHeadlessSession(
            client=OpenAI(base_url="http://eval.invalid/v1", api_key="x"),
            model="eval-model",
            coord_client=coord_client,
            ws_id="coord-eval-1",
            user_id="eval-user",
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=32768,
        )
        self._scripted_lane(
            monkeypatch,
            [
                [
                    self._wire_call(
                        "call_landed",
                        "tasks",
                        {"action": "update", "task_id": seeded["id"], "status": "done"},
                    ),
                    self._wire_call(
                        "call_rejected",
                        "tasks",
                        {"action": "update", "task_id": "tsk_hallucinated", "status": "done"},
                    ),
                ],
                [],
            ],
        )
        from turnstone.core.trajectory import Turn

        session.messages.append(Turn.user("do the bookkeeping"))
        session._msg_tokens.append(1)
        try:
            log = session._run_headless_loop(max_turns=3)
        finally:
            session.close()

        assert [entry["tool"] for entry in log] == ["tasks", "tasks"]
        assert [entry["ok"] for entry in log] == [True, False]
        # The flag is what the bookkeeping classifier rules on.
        assert _is_mutating_tasks_call(log[0])
        assert not _is_mutating_tasks_call(log[1])
        # And the landed write really landed while the rejected one
        # really did not — the flag mirrors the envelope, not the wish.
        envelope = coord_client.tasks_get("coord-eval-1")
        assert [t["status"] for t in envelope["tasks"]] == ["done"]


class TestRunResourceLifecycle:
    """A run must leave nothing behind — a sweep is hundreds of runs,
    and every leaked connection pool is an fd that a later run cannot
    open.  Exhaustion surfaces as
    ``failures: ["harness: …"]``: a plausible red 0% attributed to the
    body under test, the same dishonesty class the canary probe and the
    sweep-start validator exist to prevent.

    Each test drives the REAL ``_run_single_nudge`` with only the model
    lane stubbed, and asserts on the artifact (a closed transport, an
    absent directory, the process cwd) rather than on a recorded
    intention to close.
    """

    _CASE = {"id": "L_lifecycle", "arms": [ARM_NUDGE], "tasks": [{"title": "audit the config"}]}

    @staticmethod
    def _drive(
        monkeypatch,
        *,
        close_raises: type[BaseException] | None = None,
        init_raises: bool = False,
        teardown_reset_raises: bool = False,
        cwd_restore_raises: bool = False,
        loop_behavior: str | None = None,
        poison: str | None = None,
        fast_retries: bool = False,
        test_timeout: int = 30,
    ):
        """One real run with the generation lane stubbed out.

        Returns the objects the run built, keyed for assertion.  The
        session subclass replaces ONLY ``_run_headless_loop`` (the sole
        step that would reach a model), so construction, seeding, the
        retry loop, the wall clock and the whole teardown path are
        production code.

        *loop_behavior*: ``"raise_once"`` / ``"raise_always"`` model a
        transient / persistent generation failure; ``"hang"`` parks the
        worker on the session's own cancel event (so the wall clock
        fires and the worker still exits promptly once cancelled).
        *poison* corrupts the post-run ground truth from INSIDE the run,
        against the current attempt's storage: ``"read_raises"`` makes
        the raw config read blow up, ``"corrupt"`` writes an unparseable
        envelope, ``"missing"`` erases the seeded key.  *fast_retries*
        neutralizes the retry backoff sleeps.
        """
        from turnstone.eval import nudges as nudges_module

        made: dict[str, Any] = {}

        if cwd_restore_raises:
            launch_cwd = os.getcwd()
            real_chdir = os.chdir
            budget = [1]

            def _chdir_failing_restore(path: str) -> None:
                # Models the launch directory going away mid-sweep (it
                # is removed, or its mount drops): the chdir INTO the
                # workdir still works, the restore does not.  One-shot,
                # so the test can put the process back afterwards.
                if os.path.abspath(path) == launch_cwd and budget:
                    budget.pop()
                    raise OSError(2, "No such file or directory", path)
                real_chdir(path)

            monkeypatch.setattr(os, "chdir", _chdir_failing_restore)

        if fast_retries:
            monkeypatch.setattr(time, "sleep", lambda _s: None)

        real_mkdtemp = tempfile.mkdtemp

        def _spy_mkdtemp(*a: Any, **kw: Any) -> str:
            path = real_mkdtemp(*a, **kw)
            if kw.get("prefix") == "turnstone_eval_nudge_":
                made["workdir"] = path
            return path

        real_openai = nudges_module.OpenAI

        def _spy_openai(**kw: Any) -> Any:
            made["run_client"] = client = real_openai(**kw)
            made.setdefault("run_clients", []).append(client)
            return client

        real_stub_cls = nudges_module._StubCoordinatorClient

        def _spy_coord(*a: Any, **kw: Any) -> Any:
            made["coord_client"] = client = real_stub_cls(*a, **kw)
            return client

        class _StubbedLaneSession(nudges_module.CoordinatorHeadlessSession):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                made["session"] = self
                made.setdefault("sessions", []).append(self)

            def _run_headless_loop(self, **kw: Any) -> list[dict[str, Any]]:
                made["cwd_in_run"] = os.getcwd()
                made["loop_calls"] = made.get("loop_calls", 0) + 1
                # What THIS attempt's world contains — a retried attempt
                # must see a freshly seeded envelope, not the previous
                # attempt's rows plus a re-seed.
                made.setdefault("seeded_counts", []).append(
                    len(made["coord_client"].tasks_get("coord-eval-1")["tasks"])
                )
                if poison == "read_raises":
                    storage = get_storage()

                    def _read_boom(*a: Any, **k: Any) -> dict[str, Any]:
                        raise RuntimeError("storage read blew up")

                    storage.load_workstream_config = _read_boom  # type: ignore[method-assign]
                elif poison == "corrupt":
                    get_storage().save_workstream_config("coord-eval-1", {"tasks": "{not json"})
                elif poison == "missing":
                    get_storage().save_workstream_config("coord-eval-1", {"tasks": ""})
                if loop_behavior == "hang":
                    self._cancelled.wait(10)
                    return []
                if loop_behavior == "raise_always" or (
                    loop_behavior == "raise_once" and made["loop_calls"] == 1
                ):
                    raise RuntimeError("transient boom: connection reset by peer")
                return []

            def close(self) -> None:
                if close_raises is not None:
                    # Models a raise from one of ``ChatSession.close()``'s
                    # UNGUARDED steps (only the ``_coord_client`` step is
                    # exception-guarded), so nothing after it in the real
                    # close runs either — including that client's close.
                    raise close_raises("teardown blew up mid-close")
                super().close()

        monkeypatch.setattr(tempfile, "mkdtemp", _spy_mkdtemp)
        monkeypatch.setattr(nudges_module, "OpenAI", _spy_openai)
        monkeypatch.setattr(nudges_module, "_StubCoordinatorClient", _spy_coord)
        monkeypatch.setattr(nudges_module, "CoordinatorHeadlessSession", _StubbedLaneSession)
        if init_raises:

            def _boom(*a: Any, **kw: Any) -> None:
                raise RuntimeError("storage init failed")

            monkeypatch.setattr(nudges_module, "init_storage", _boom)
        if teardown_reset_raises:
            real_reset = nudges_module.reset_storage
            resets: list[int] = []

            def _reset_then_fail(*a: Any, **kw: Any) -> None:
                # The run does its setup reset first; only the ``finally``
                # one fails, so the failure lands on a run that otherwise
                # completed.
                resets.append(1)
                if len(resets) > 1:
                    raise RuntimeError("storage reset failed")
                real_reset(*a, **kw)

            monkeypatch.setattr(nudges_module, "reset_storage", _reset_then_fail)

        try:
            made["result"] = nudges_module._run_single_nudge(
                base_url="http://eval.invalid/v1",
                api_key="x",
                model="eval-model",
                case=TestRunResourceLifecycle._CASE,
                arm=ARM_NUDGE,
                temperature=0.7,
                max_tokens=1024,
                reasoning_effort="medium",
                context_window=32768,
                max_turns=2,
                test_timeout=test_timeout,
                verbose=False,
                log_prefix="test",
            )
        except BaseException as exc:  # noqa: BLE001 - the raise IS the fixture
            made["raised"] = exc
        return made

    def test_run_single_nudge_closes_its_clients(self, monkeypatch):
        """Both per-run HTTP owners are released.  Asserted on the
        transports themselves — a spy counting ``close()`` calls would
        pass against a session that swallowed the call."""
        made = self._drive(monkeypatch)

        assert "raised" not in made, made.get("raised")
        assert made["result"]["pass"] is True, made["result"]
        assert made["run_client"].is_closed(), "the per-run OpenAI client leaked its pool"
        assert made["coord_client"]._http.is_closed, "the stub coordinator client leaked its pool"
        # The session's own close ran to completion: the background-shell
        # registry is its last-but-one step and latches closed.
        assert made["session"]._background_shells._closed is True

    def test_init_storage_failure_still_removes_the_workdir(self, monkeypatch):
        """Storage setup lives INSIDE the ``try`` whose ``finally`` owns
        the directory.  Before that move a failing ``init_storage``
        stranded one temp dir per attempt, permanently."""
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, init_raises=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "workdir" in made, "the run never got as far as making one"
        assert not os.path.exists(made["workdir"])
        assert os.getcwd() == cwd_before

    def test_a_failing_storage_reset_still_removes_the_workdir(self, monkeypatch):
        """The teardown reset is nested, not suppressed.

        A storage fault is real and must surface — so the raise
        propagates and the sweep records the run as a ``harness:``
        failure — but it must not take the ``rmtree`` with it.
        Flattened, this is the SETUP-path defect reappearing on the
        teardown path: one directory stranded permanently per run, i.e.
        the defect inside the fix for it.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, teardown_reset_raises=True)

        assert isinstance(made["raised"], RuntimeError)  # the fault surfaced
        assert "result" not in made  # a finally-raise discards the return
        assert not os.path.exists(made["workdir"])  # and cost the run nothing
        assert os.getcwd() == cwd_before

        reset_storage()  # the patched reset never got to do its job

    def test_a_raising_session_close_still_completes_the_cleanup(self, monkeypatch):
        """A failing teardown costs the run NOTHING else.

        Each close is suppressed on its own, so a raise inside
        ``ChatSession.close()`` cannot take the closes after it, the
        storage reset or the temp dir with it — and cannot even reach
        the sweep as a ``harness:`` failure, since the run's result is
        already computed by then.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, close_raises=RuntimeError)

        assert "raised" not in made, made.get("raised")  # contained, not propagated
        assert made["result"]["pass"] is True
        assert made["cwd_in_run"] == made["workdir"]  # it really did chdir in
        assert os.getcwd() == cwd_before
        assert not is_storage_initialized()
        assert not os.path.exists(made["workdir"])
        # The closes after the raising one still ran — including the
        # coordinator client that the failed ``session.close()`` never
        # reached, which is why that close is kept as its own step.
        assert made["run_client"].is_closed()
        assert made["coord_client"]._http.is_closed

    def test_a_ctrl_c_during_close_still_restores_the_cwd(self, monkeypatch):
        """What the cwd-restore-FIRST ordering is worth, second case.

        A suppressed close cannot abort the block at all, so the
        ordering is not what saves an ordinary teardown failure.  It
        earns its keep on the two raises that DO leave the block early:
        the unsuppressed storage reset (the test above) and a
        ``BaseException``, which no suppression catches — realistically
        a Ctrl-C landing in ``_background_shells.close()``'s bounded
        join, the one blocking window a long sweep offers an impatient
        operator.  The residues are not equal: a leaked temp dir is
        inert and visible, while a process left chdir'd into a deleted
        directory silently breaks every subsequent run — which this
        test's own process would then demonstrate by dying at its next
        ``os.getcwd()``.  The restore goes first so an interrupt can
        only cost the inert one.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, close_raises=KeyboardInterrupt)

        assert isinstance(made["raised"], KeyboardInterrupt)
        assert made["cwd_in_run"] == made["workdir"]
        assert os.getcwd() == cwd_before  # survived the abort — it ran first
        # The honest cost of an abort mid-block, asserted rather than
        # implied: everything after the interrupt is skipped.
        assert os.path.exists(made["workdir"])
        assert is_storage_initialized()

        reset_storage()
        made["run_client"].close()
        made["coord_client"].close()
        shutil.rmtree(made["workdir"], ignore_errors=True)

    def test_a_failing_cwd_restore_still_completes_the_cleanup(self, monkeypatch):
        """The teardown's FIRST statement is guarded too.

        The restore is deliberately first, and every close after it is
        suppressed on its own — but an unguarded ``os.chdir`` raise (the
        launch directory removed, or its mount dropped mid-sweep) would
        skip all three closes, the storage reset AND the rmtree,
        reinstating the per-run leak the block exists to prevent, in the
        one situation where the operator can least afford it.  The
        block's own comment claims nothing in it can cost the rest; that
        claim must not be falsifiable by the line that implements it.

        The failure is logged, not swallowed silently, and it is NOT
        re-raised: this run's result is already computed and is still
        honest.  The cost that remains is asserted below rather than
        implied — the process is left inside a directory the rmtree then
        removes, so the NEXT run fails early and visibly.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, cwd_restore_raises=True)
        os.chdir(cwd_before)  # first: the process is standing in a deleted dir

        assert "raised" not in made, made.get("raised")  # contained, not propagated
        assert made["result"]["pass"] is True  # and the measurement survived
        assert made["cwd_in_run"] == made["workdir"]  # the restore really was the failing call
        # Everything the unguarded chdir used to skip:
        assert made["run_client"].is_closed(), "the per-run OpenAI client leaked its pool"
        assert made["coord_client"]._http.is_closed, "the stub coordinator client leaked its pool"
        assert made["session"]._background_shells._closed is True
        assert not is_storage_initialized()
        assert not os.path.exists(made["workdir"])

    def test_a_failing_storage_reset_still_surfaces(self, monkeypatch):
        """The property the chdir guard must not have bought at the cost
        of: a storage fault is real and still propagates.  Guarding the
        restore with a suppression wide enough to swallow the reset —
        or wrapping the whole block — would take this with it.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, teardown_reset_raises=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "storage reset failed" in str(made["raised"])
        assert os.getcwd() == cwd_before

        reset_storage()

    def test_a_transient_generation_failure_is_retried_with_fresh_state(self, monkeypatch):
        """The mid-sweep hiccup the canary cannot see.

        The probe samples only sweep start and sweep end, so one
        connection reset on run 4 of 10 used to file 90% as a body
        regression.  The shared lifecycle retries the attempt — and
        because scoring is STATE-FIRST, the retry rebuilds the whole
        seeded world: an attempt that inherited the failed attempt's
        envelope would score expectations the model never earned.
        """
        made = self._drive(monkeypatch, loop_behavior="raise_once", fast_retries=True)

        assert "raised" not in made, made.get("raised")
        assert made["result"]["pass"] is True
        assert made["loop_calls"] == 2  # failed once, retried once
        # Each attempt saw a FRESHLY seeded envelope: exactly the cell's
        # one row, not the previous attempt's row plus a re-seed.
        assert made["seeded_counts"] == [1, 1]
        # The failed attempt's session was closed before being replaced
        # — the worker had already raised, so the drop-unclosed rule is
        # the timeout path's alone.
        assert all(s._background_shells._closed for s in made["sessions"])
        assert all(c.is_closed() for c in made["run_clients"])

    def test_every_failed_attempts_session_is_closed(self, monkeypatch):
        """A persistent failure exhausts the 3 attempts and surfaces as
        a harness failure — and every attempt's fully-built session is
        CLOSED on the way, not abandoned with its listener
        registrations and background-shell registry."""
        made = self._drive(monkeypatch, loop_behavior="raise_always", fast_retries=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "transient boom" in str(made["raised"])
        assert made["loop_calls"] == 3  # the 3x retry really ran
        assert len(made["sessions"]) == 3
        assert all(s._background_shells._closed for s in made["sessions"])
        assert all(c.is_closed() for c in made["run_clients"])
        assert not os.path.exists(made["workdir"])
        assert not is_storage_initialized()

    def test_a_hung_generation_is_bounded_by_the_wall_clock(self, monkeypatch):
        """The per-request httpx timeout cannot bound a STREAM — a
        trickling response resets the read timeout indefinitely — so
        without the executor wall clock a hung generation occupies a run
        slot forever and is scored as a body regression when the sweep
        is finally killed.  The wall clock converts it to a loud
        ``harness:`` timeout; the hung attempt's session is DROPPED
        unclosed, deliberately (the worker is still inside the drive;
        close would trade a bounded leak for a blocked teardown).
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, loop_behavior="hang", test_timeout=1)

        assert isinstance(made["raised"], TimeoutError)
        assert made["loop_calls"] == 1  # a timeout aborts, never retries
        assert made["sessions"][0]._background_shells._closed is False  # dropped
        assert made["run_clients"][0].is_closed()  # the transport IS closed
        assert os.getcwd() == cwd_before
        assert not os.path.exists(made["workdir"])
        assert not is_storage_initialized()

        # Test hygiene, not production: reap the deliberately-dropped
        # session once its cancelled worker has exited.
        made["sessions"][0].close()

    def test_a_post_run_storage_fault_is_a_harness_failure(self, monkeypatch):
        """The post-run envelope read is scoring GROUND TRUTH, and it
        was the module's only read whose failure was attributed to the
        body under test: ``load_task_envelope`` fails open, so a
        transient storage fault came back as an empty envelope and
        scored as the model having deleted its tasks.  The probe read
        raises instead, landing in the sweep's ``harness:`` bucket."""
        made = self._drive(monkeypatch, poison="read_raises", fast_retries=True)

        assert "result" not in made  # never scored as a model result
        assert isinstance(made["raised"], RuntimeError)
        assert "storage read blew up" in str(made["raised"])

    def test_a_corrupt_post_run_envelope_refuses_to_score(self, monkeypatch):
        """The corrupt flag is honoured, not discarded: corruption used
        to read as emptiness, byte-identical in the result JSON to a
        coordinator that deleted the task it was told to escalate."""
        made = self._drive(monkeypatch, poison="corrupt", fast_retries=True)

        assert "result" not in made
        assert isinstance(made["raised"], RuntimeError)
        assert "corrupt" in str(made["raised"])

    def test_a_vanished_post_run_envelope_refuses_to_score(self, monkeypatch):
        """The seed WROTE the tasks key; a post-run read without it is a
        storage fault, not a model that emptied its list (no production
        path deletes the key)."""
        made = self._drive(monkeypatch, poison="missing", fast_retries=True)

        assert "result" not in made
        assert isinstance(made["raised"], RuntimeError)
        assert "missing from storage" in str(made["raised"])


class TestHarnessFailureRecord:
    """A run the harness lost must file a record the readers can sum.

    The failure record is shape-uniform with a success record — same
    keys, zero-valued ``elapsed`` / ``usage`` — so a consumer summing
    cost or wall-clock across a cell's ``runs`` never meets a missing
    key on exactly the sweeps whose totals it most wants to read.
    """

    def test_the_failure_record_carries_the_success_records_keys(self, monkeypatch):
        from turnstone.eval import nudges as nudges_module

        calls = {"n": 0}

        def _flaky_run(**kw: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "pass": True,
                    "failures": [],
                    "forbidden": [],
                    "actions": ["tasks"],
                    "elapsed": 1.25,
                    "usage": {"prompt": 10, "completion": 5},
                }
            raise RuntimeError("endpoint hiccup")

        monkeypatch.setattr(nudges_module, "tool_call_canary", lambda *a, **k: True)
        monkeypatch.setattr(nudges_module, "_run_single_nudge", _flaky_run)
        out = run_nudge_response(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            cells=[{"id": "X_shape", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]}],
            n_runs=2,
        )
        success, failure = out["cells"]["X_shape"][ARM_NUDGE]["runs"]
        assert set(failure) == set(success)
        assert failure["failures"] == ["harness: endpoint hiccup"]
        assert failure["elapsed"] == 0
        assert failure["usage"] == {"prompt": 0, "completion": 0}


class TestCanaryFloor:
    """The probe's budget is floored, never inherited raw.

    The canary's own docstring records the starved-probe trap (a
    thinking model burns a small budget inside its reasoning block and
    the probe reads as a dead parser), and the runner used to re-enable
    it by passing the sweep's ``max_tokens`` with no floor — an operator
    smoke-sweeping with ``--max-tokens 512`` got an ABORT telling them
    to restart a healthy container.
    """

    @staticmethod
    def _probe_budgets(monkeypatch, sweep_max_tokens: int) -> list[int]:
        from turnstone.eval import nudges as nudges_module

        seen: list[int] = []

        def _spy_canary(*a: Any, **k: Any) -> bool:
            seen.append(k["max_tokens"])
            return True

        monkeypatch.setattr(nudges_module, "tool_call_canary", _spy_canary)
        monkeypatch.setattr(
            nudges_module,
            "_run_single_nudge",
            lambda **kw: {
                "pass": True,
                "failures": [],
                "forbidden": [],
                "actions": [],
                "elapsed": 0,
                "usage": {"prompt": 0, "completion": 0},
            },
        )
        run_nudge_response(
            base_url="http://eval.invalid/v1",
            api_key="x",
            model="eval-model",
            cells=[{"id": "X_floor", "arms": [ARM_NUDGE], "tasks": [_OPEN_TASK]}],
            n_runs=1,
            max_tokens=sweep_max_tokens,
        )
        return seen

    def test_a_starved_sweep_budget_cannot_reach_the_probe(self, monkeypatch):
        # Three probes, not two: the stub returns zero tool calls on a
        # nudge-class arm, so the mid-sweep tripwire fires its own
        # canary between the bracketing pair — and it must honour the
        # same floor, or the tripwire re-opens the starved-probe trap.
        assert self._probe_budgets(monkeypatch, 512) == [8192, 8192, 8192]

    def test_a_generous_sweep_budget_widens_the_probe(self, monkeypatch):
        assert self._probe_budgets(monkeypatch, 50000) == [50000, 50000, 50000]


class TestCliNRuns:
    """``--n-runs 0`` is the validate-and-canary dry run — both run
    before any generation — and the ``or``-resolution silently replaced
    it with the full default grid against a personally-funded endpoint.
    The file's two sibling resolution sites already used ``is not
    None``; this pins the nudge path to the same rule.
    """

    @staticmethod
    def _resolved_n_runs(monkeypatch, tmp_path, n_runs_arg: int | None) -> int:
        import argparse

        from turnstone.eval import cli as cli_module
        from turnstone.eval import nudges as nudges_module

        seen: dict[str, Any] = {}

        def _fake_sweep(**kw: Any) -> dict[str, Any]:
            seen["n_runs"] = kw["n_runs"]
            return {"model": kw["model"], "cells": {}}

        monkeypatch.setattr(nudges_module, "run_nudge_response", _fake_sweep)
        args = argparse.Namespace(
            cells=None,
            body_override=None,
            base_url="http://eval.invalid/v1",
            n_runs=n_runs_arg,
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=32768,
            test_timeout=30,
            verbose=False,
            output=str(tmp_path / "out.json"),
        )
        cli_module._run_nudges_cli(args, "eval-model", "key")
        return seen["n_runs"]

    def test_an_explicit_zero_is_passed_through(self, monkeypatch, tmp_path):
        assert self._resolved_n_runs(monkeypatch, tmp_path, 0) == 0

    def test_an_omitted_flag_still_defaults_to_ten(self, monkeypatch, tmp_path):
        assert self._resolved_n_runs(monkeypatch, tmp_path, None) == 10


class TestMidSweepTripwire:
    """The dead-parser tripwire: quiet nudge-class arms re-fire the
    canary; a failed probe aborts the sweep, a healthy one stamps the
    anomaly into the fingerprint.  Born from the 2026-07-28 outage,
    where 144 of 240 runs returned zero tool calls between two green
    bracketing canaries."""

    @staticmethod
    def _cell_out(empty_runs: int, total: int = 10, arm: str = ARM_NUDGE):
        runs = [
            {
                "pass": False,
                "failures": [],
                "forbidden": [],
                "actions": [],
                "elapsed": 0,
                "usage": {},
            }
            for _ in range(empty_runs)
        ] + [
            {
                "pass": True,
                "failures": [],
                "forbidden": [],
                "actions": ["tasks"],
                "elapsed": 1,
                "usage": {},
            }
            for _ in range(total - empty_runs)
        ]
        return {arm: {"n": total, "pass_rate": 0.0, "forbidden_rate": 0.0, "runs": runs}}

    def test_dead_parser_aborts_the_sweep(self, monkeypatch):
        import turnstone.eval.nudges as nudges_mod

        monkeypatch.setattr(nudges_mod, "tool_call_canary", lambda *a, **k: False)
        out = {"body": {"cells": {}}}
        with pytest.raises(SystemExit, match="died mid-sweep"):
            nudges_mod._tripwire_check(
                "C4_finished_unmarked",
                self._cell_out(empty_runs=10),
                out,
                base_url="http://x/v1",
                api_key="k",
                model="m",
                max_tokens=8192,
            )

    def test_quiet_arms_with_healthy_canary_are_stamped_not_fatal(self, monkeypatch):
        import turnstone.eval.nudges as nudges_mod

        probes = []
        monkeypatch.setattr(
            nudges_mod, "tool_call_canary", lambda *a, **k: probes.append(1) or True
        )
        out = {"body": {"cells": {}}}
        nudges_mod._tripwire_check(
            "C4_finished_unmarked",
            self._cell_out(empty_runs=6),
            out,
            base_url="http://x/v1",
            api_key="k",
            model="m",
            max_tokens=8192,
        )
        assert probes, "the canary must be re-fired on a quiet nudge-class arm"
        assert out["body"]["cells"]["C4_finished_unmarked"]["quiet_nudge_arms"] == [ARM_NUDGE]

    def test_bare_continue_silence_is_legitimate_and_never_probes(self, monkeypatch):
        import turnstone.eval.nudges as nudges_mod

        monkeypatch.setattr(
            nudges_mod,
            "tool_call_canary",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")),
        )
        out = {"body": {"cells": {}}}
        nudges_mod._tripwire_check(
            "C4_finished_unmarked",
            self._cell_out(empty_runs=10, arm=ARM_BARE_CONTINUE),
            out,
            base_url="http://x/v1",
            api_key="k",
            model="m",
            max_tokens=8192,
        )
        assert out["body"]["cells"] == {}

    def test_minority_silence_does_not_trip(self, monkeypatch):
        import turnstone.eval.nudges as nudges_mod

        monkeypatch.setattr(
            nudges_mod,
            "tool_call_canary",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")),
        )
        out = {"body": {"cells": {}}}
        nudges_mod._tripwire_check(
            "C1_legit_stop_approval",
            self._cell_out(empty_runs=5),
            out,
            base_url="http://x/v1",
            api_key="k",
            model="m",
            max_tokens=8192,
        )
        assert out["body"]["cells"] == {}
