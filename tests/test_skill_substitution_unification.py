"""Skill substitution unification (SKILL.md subsystem refactor, step 1).

Pins the invariant that skill-body placeholder substitution is IDENTICAL
across every invocation context.  Interactive load, default skills, and
``task_agent`` sub-agents all route through
``ChatSession._render_skill_body`` — so a skill reading ``$ARGUMENTS`` or
``${TURNSTONE_EFFORT}`` resolves the same everywhere, rather than
rendering literally on the ``task_agent`` path (which previously ran
``_render_template`` alone).

Also covers the two behaviours the unified path newly guarantees:

* ``${TURNSTONE_*}`` env vars (canonical) and their ``${CLAUDE_*}``
  back-compat aliases both resolve.
* ``${TURNSTONE_SKILL_DIR}`` resolves to the concrete materialized bundle
  path in the rendered body, because resources are materialized BEFORE
  substitution (the ordering fix).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests._session_helpers import make_session
from turnstone.core.storage._registry import get_storage

if TYPE_CHECKING:
    import pytest


def _create_skill(db: Any, skill_id: str, name: str, content: str, **kw: Any) -> None:
    db.create_prompt_template(
        template_id=skill_id,
        name=name,
        category=kw.get("category", "general"),
        content=content,
        variables="[]",
        is_default=kw.get("is_default", False),
        org_id="",
        created_by="test",
        origin="manual",
        mcp_server="",
        readonly=False,
        description="",
        tags="[]",
        source_url="",
        version="1.0.0",
        author="",
        activation=kw.get("activation", "named"),
        token_estimate=0,
        model="",
        auto_approve=False,
        temperature=None,
        reasoning_effort="",
        max_tokens=None,
        token_budget=0,
        agent_max_turns=None,
        notify_on_complete="{}",
        enabled=True,
        allowed_tools="[]",
        priority=0,
    )


class TestRenderSkillBodySharedPath:
    """``_render_skill_body`` is the single substitution path — the one
    ``task_agent`` now calls.  Assert it resolves the spec forms that the
    old ``_render_template``-only ``task_agent`` path left literal."""

    def test_env_vars_resolve(self, tmp_db: str) -> None:
        session = make_session(reasoning_effort="high")
        try:
            out = session._render_skill_body(
                "id ${TURNSTONE_SESSION_ID} effort ${TURNSTONE_EFFORT}"
            )
            assert out == f"id {session._ws_id} effort high"
        finally:
            session.close()

    def test_claude_aliases_resolve(self, tmp_db: str) -> None:
        session = make_session(reasoning_effort="low")
        try:
            out = session._render_skill_body("id ${CLAUDE_SESSION_ID} effort ${CLAUDE_EFFORT}")
            assert out == f"id {session._ws_id} effort low"
        finally:
            session.close()

    def test_bare_arguments_clears_to_empty(self, tmp_db: str) -> None:
        # A ``task_agent`` gets its task via the prompt, not invocation
        # args, so the shared path is called with ``arguments_str=""`` —
        # the bare ``$ARGUMENTS`` placeholder clears to empty (spawn-child
        # semantics), NOT the literal string it was before unification.
        session = make_session()
        try:
            assert session._render_skill_body("before $ARGUMENTS after") == "before  after"
        finally:
            session.close()

    def test_curly_and_spec_passes_both_apply(self, tmp_db: str) -> None:
        # Legacy ``{{model}}`` AND spec ``${TURNSTONE_EFFORT}`` in one body —
        # both passes run through the shared path.
        session = make_session(model="my-model", reasoning_effort="high")
        try:
            out = session._render_skill_body("model {{model}} effort ${TURNSTONE_EFFORT}")
            assert out == "model my-model effort high"
        finally:
            session.close()

    def test_positional_tokens_clear_without_args(self, tmp_db: str) -> None:
        # A sub-agent supplies no invocation args, so every positional form
        # ($N / $0 / $ARGUMENTS[N]) — like bare $ARGUMENTS — resolves to
        # empty, identical to the defaults and spawn-child paths.  Pinned so
        # this deliberate unification isn't mistaken for silent corruption:
        # the old _render_template-only path left these tokens verbatim.
        session = make_session()
        try:
            out = session._render_skill_body("step $1 / $0 / $ARGUMENTS[2] done")
            assert out == "step  /  /  done"
        finally:
            session.close()

    def test_skill_dir_literal_on_sub_agent_path(self, tmp_db: str) -> None:
        # task_agent calls _render_skill_body with no skill_dir (sub-agent
        # bundles aren't materialized yet), so ${TURNSTONE_SKILL_DIR} stays
        # literal on this path — unchanged from before, resolved in a later
        # step.  The env vars that DO have values still resolve.
        session = make_session(reasoning_effort="high")
        try:
            out = session._render_skill_body(
                "dir ${TURNSTONE_SKILL_DIR} effort ${TURNSTONE_EFFORT}"
            )
            assert out == "dir ${TURNSTONE_SKILL_DIR} effort high"
        finally:
            session.close()


class TestSkillDirResolvesInBody:
    """Materialize-before-substitute: ``${TURNSTONE_SKILL_DIR}`` in a skill
    body resolves to the concrete on-disk bundle path after a full load."""

    def test_turnstone_skill_dir_in_body(self, tmp_db: str) -> None:
        db = get_storage()
        _create_skill(db, "s1", "dir-skill", "Scripts under ${TURNSTONE_SKILL_DIR}/scripts.")
        db.create_skill_resource("r1", "s1", "scripts/go.py", "print('x')")

        session = make_session(skill="dir-skill")
        try:
            base = session._skill_resources_dir
            assert base is not None
            assert session._skill_content == f"Scripts under {base}/scripts."
        finally:
            session.close()

    def test_claude_skill_dir_alias_in_body(self, tmp_db: str) -> None:
        db = get_storage()
        _create_skill(db, "s1", "dir-alias-skill", "Bundle at ${CLAUDE_SKILL_DIR}")
        db.create_skill_resource("r1", "s1", "references/a.md", "# a")

        session = make_session(skill="dir-alias-skill")
        try:
            base = session._skill_resources_dir
            assert base is not None
            assert session._skill_content == f"Bundle at {base}"
        finally:
            session.close()

    def test_skill_dir_literal_without_resources(self, tmp_db: str) -> None:
        # No bundled resources → no dir → placeholder stays literal
        # (graceful degradation), not an empty path.
        db = get_storage()
        _create_skill(db, "s1", "no-res-skill", "Path ${TURNSTONE_SKILL_DIR} here.")

        session = make_session(skill="no-res-skill")
        try:
            assert session._skill_resources_dir is None
            assert session._skill_content == "Path ${TURNSTONE_SKILL_DIR} here."
        finally:
            session.close()


class TestSkillResourceEnvAliases:
    """Bash env exposes the materialized bundle dir under turnstone-owned
    names unconditionally, and under the foreign ``CLAUDE_SKILL_DIR`` alias
    only when the host hasn't already set it (no shadowing when turnstone
    runs as a node inside Claude Code)."""

    def test_turnstone_owned_aliases_always_present(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_SKILL_DIR", raising=False)
        db = get_storage()
        _create_skill(db, "s1", "env-alias-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/t.py", "code")

        session = make_session(skill="env-alias-skill")
        try:
            env = session._skill_resource_env()
            d = session._skill_resources_dir
            assert env["SKILL_RESOURCES_DIR"] == d
            assert env["TURNSTONE_SKILL_DIR"] == d
            # Host hasn't set it → turnstone supplies the portability alias.
            assert env["CLAUDE_SKILL_DIR"] == d
        finally:
            session.close()

    def test_host_claude_skill_dir_not_shadowed(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # turnstone as a node inside Claude Code: the host's CLAUDE_SKILL_DIR
        # must survive.  turnstone only injects its own names and leaves
        # CLAUDE_SKILL_DIR out of the extra-env so scrubbed_env's passthrough
        # keeps the host value.
        monkeypatch.setenv("CLAUDE_SKILL_DIR", "/host/claude/skill")
        db = get_storage()
        _create_skill(db, "s1", "env-host-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/t.py", "code")

        session = make_session(skill="env-host-skill")
        try:
            env = session._skill_resource_env()
            d = session._skill_resources_dir
            assert env["TURNSTONE_SKILL_DIR"] == d
            assert env["SKILL_RESOURCES_DIR"] == d
            assert "CLAUDE_SKILL_DIR" not in env
        finally:
            session.close()
