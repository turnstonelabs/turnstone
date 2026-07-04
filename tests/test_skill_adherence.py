"""Tests for turnstone.eval skill-adherence measurement mode.

Two levels, neither requires a live model:

* ``TestSkillComposition`` is the load-bearing plumbing proof — it seeds a
  named skill, builds ``HeadlessSession`` under natural composition, and
  asserts the skill body folds into ``system_messages`` for the treatment
  arm and is absent for the control arm.  This is what makes the two arms
  measure different things.
* ``TestAdherenceLift`` unit-tests ``run_skill_adherence``'s lift math with
  the per-arm runner stubbed out.
"""

import os
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest
from openai import OpenAI

from turnstone.core.storage import get_storage, init_storage, reset_storage
from turnstone.eval import core
from turnstone.eval.core import HeadlessSession, run_skill_adherence

_SKILL = {
    "name": "search-first",
    "content": (
        "# Search First\n\nBefore answering ANY question about where something "
        "lives in the codebase, you MUST call the `search` tool first. "
        "SENTINEL_SKILL_BODY_MARKER."
    ),
}


@pytest.fixture
def temp_storage() -> Iterator[None]:
    """Fresh sqlite storage in a temp dir, torn down after the test."""
    workdir = tempfile.mkdtemp(prefix="turnstone_skill_test_")
    reset_storage()
    init_storage("sqlite", path=os.path.join(workdir, ".eval.db"), run_migrations=False)
    try:
        yield
    finally:
        reset_storage()
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _seed_skill(skill: dict[str, str]) -> None:
    """Seed a named skill exactly as the runner does."""
    get_storage().create_prompt_template(
        template_id="eval-skill",
        name=skill["name"],
        category="eval",
        content=skill["content"],
        variables="[]",
        is_default=False,
        org_id="",
        created_by="eval",
        activation="named",
        enabled=True,
    )


def _system_text(session: HeadlessSession) -> str:
    return "\n".join(m["content"] for m in session.system_messages)


class TestSkillComposition:
    """Prove the treatment/control arms compose different system messages."""

    def test_treatment_folds_skill_into_system(self, temp_storage: None) -> None:
        _seed_skill(_SKILL)
        client = OpenAI(base_url="http://localhost:9/v1", api_key="dummy")
        session = HeadlessSession(client=client, model="test-model")
        try:
            # Treatment arm activates the seeded skill via the real path.
            session.set_skill(_SKILL["name"])
            assert "SENTINEL_SKILL_BODY_MARKER" in _system_text(session)
        finally:
            session.close()

    def test_control_omits_skill(self, temp_storage: None) -> None:
        # Control arm: no skill seeded, no set_skill — natural default only.
        client = OpenAI(base_url="http://localhost:9/v1", api_key="dummy")
        session = HeadlessSession(client=client, model="test-model")
        try:
            assert "SENTINEL_SKILL_BODY_MARKER" not in _system_text(session)
        finally:
            session.close()

    def test_no_system_prompt_override_in_skill_mode(self, temp_storage: None) -> None:
        # skill_mode must NOT override the base identity — a real base prompt
        # (persona / composed developer message) must survive, or we'd be
        # measuring an empty prompt instead of the identity under test.
        client = OpenAI(base_url="http://localhost:9/v1", api_key="dummy")
        session = HeadlessSession(client=client, model="test-model")
        try:
            assert _system_text(session).strip(), "expected a composed base prompt"
        finally:
            session.close()


class TestAdherenceLift:
    """Unit-test the lift math with the per-arm runner stubbed."""

    def test_lift_treatment_over_control(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub _run_iteration: treatment (skill != None) passes 3/3, control
        # (skill is None) passes 1/3.  run_skill_adherence must report the
        # difference as the lift.
        def fake_run_iteration(**kwargs: Any) -> dict[str, Any]:
            rate = 1.0 if kwargs.get("skill") is not None else 1.0 / 3.0
            return {"aggregate": {"overall_pass_rate": rate}}

        monkeypatch.setattr(core, "_run_iteration", fake_run_iteration)

        cases = [
            {
                "id": "search-first",
                "skill": _SKILL,
                "user_prompt": "where is X?",
                "expected_actions": [{"tool": "search"}],
            }
        ]
        result = run_skill_adherence(
            client=None,
            base_url="http://localhost:9/v1",
            api_key="dummy",
            model="test-model",
            cases=cases,
            n_runs=3,
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=8192,
        )

        assert len(result["cases"]) == 1
        row = result["cases"][0]
        assert row["case_id"] == "search-first"
        assert row["skill"] == "search-first"
        assert row["treatment_rate"] == pytest.approx(1.0)
        assert row["control_rate"] == pytest.approx(1.0 / 3.0)
        assert row["lift"] == pytest.approx(2.0 / 3.0)
        assert row["n_runs"] == 3
        assert result["mean_lift"] == pytest.approx(2.0 / 3.0)

    def test_rejects_malformed_skill(self) -> None:
        # A skill missing 'content' (or 'name') fails fast with a clear error,
        # not a KeyError mid-run (Copilot review). Validation raises before any
        # arm runs, so no _run_iteration stub is needed.
        cases = [
            {
                "id": "bad-skill",
                "skill": {"name": "x"},  # missing 'content'
                "user_prompt": "do x",
                "expected_actions": [{"tool": "search"}],
            }
        ]
        with pytest.raises(ValueError, match="non-empty 'name' and 'content'"):
            run_skill_adherence(
                client=None,
                base_url="http://localhost:9/v1",
                api_key="dummy",
                model="test-model",
                cases=cases,
                n_runs=1,
                temperature=0.7,
                max_tokens=1024,
                reasoning_effort="medium",
                context_window=8192,
            )

    def test_skipped_when_no_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A case with no skill is not measurable — it must be skipped, not
        # crash, and must not contribute to the mean.
        def fake_run_iteration(**kwargs: Any) -> dict[str, Any]:
            return {"aggregate": {"overall_pass_rate": 1.0}}

        monkeypatch.setattr(core, "_run_iteration", fake_run_iteration)

        cases = [{"id": "no-skill", "user_prompt": "hi", "expected_actions": []}]
        result = run_skill_adherence(
            client=None,
            base_url="http://localhost:9/v1",
            api_key="dummy",
            model="test-model",
            cases=cases,
            n_runs=3,
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=8192,
        )
        assert result["cases"] == []
        assert result["mean_lift"] == 0.0

    def test_mean_lift_averages_multiple_cases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two skill cases with different lifts average into mean_lift.
        rates = iter([1.0, 0.0, 1.0, 0.5])  # t1, c1, t2, c2 -> lifts 1.0, 0.5

        def fake_run_iteration(**kwargs: Any) -> dict[str, Any]:
            return {"aggregate": {"overall_pass_rate": next(rates)}}

        monkeypatch.setattr(core, "_run_iteration", fake_run_iteration)

        cases = [
            {"id": "a", "skill": _SKILL, "user_prompt": "q", "expected_actions": []},
            {"id": "b", "skill": _SKILL, "user_prompt": "q", "expected_actions": []},
        ]
        result = run_skill_adherence(
            client=None,
            base_url="http://localhost:9/v1",
            api_key="dummy",
            model="test-model",
            cases=cases,
            n_runs=2,
            temperature=0.7,
            max_tokens=1024,
            reasoning_effort="medium",
            context_window=8192,
        )
        assert [c["lift"] for c in result["cases"]] == pytest.approx([1.0, 0.5])
        assert result["mean_lift"] == pytest.approx(0.75)
