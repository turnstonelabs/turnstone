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
    ``task_agent`` now calls.  With ``substitute_args=True`` (arg-capable
    invocations: interactive /skill, skills(load)) the spec arg forms
    resolve; with ``substitute_args=False`` (capability contexts: defaults,
    task_agent) literal ``$N``/``$ARGUMENTS`` are left untouched.  Env vars
    resolve either way."""

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

    def test_arg_capable_path_clears_bare_arguments_when_no_args(self, tmp_db: str) -> None:
        # Arg-capable invocation (interactive /skill, skills(load)) with no
        # args → bare $ARGUMENTS clears to empty, per the SKILL.md spec.
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

    def test_arg_capable_path_clears_positional_tokens_when_no_args(self, tmp_db: str) -> None:
        # Arg-capable path with no args → positional forms clear to empty (spec).
        session = make_session()
        try:
            out = session._render_skill_body("step $1 / $0 / $ARGUMENTS[2] done")
            assert out == "step  /  /  done"
        finally:
            session.close()

    def test_capability_context_preserves_literal_arg_tokens(self, tmp_db: str) -> None:
        # Capability contexts (task_agent, defaults) never receive invocation
        # args, so substitute_args=False leaves literal $ARGUMENTS/$N/$name
        # untouched (they are prose/shell text) while env vars still resolve.
        # Pins the review fix that stopped blanking such tokens for sub-agents.
        session = make_session(reasoning_effort="high")
        try:
            out = session._render_skill_body(
                "run ./deploy.sh $1 $2 at ${TURNSTONE_EFFORT}; process $ARGUMENTS",
                substitute_args=False,
            )
            assert out == "run ./deploy.sh $1 $2 at high; process $ARGUMENTS"
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

    def test_claude_skill_dir_not_aliased_in_body(self, tmp_db: str) -> None:
        # CLAUDE_SKILL_DIR is NOT a turnstone-owned alias: it stays a literal
        # placeholder even with a materialized bundle (that name belongs to the
        # host in bash; turnstone claims neither surface).  The canonical
        # TURNSTONE_SKILL_DIR does resolve.
        db = get_storage()
        _create_skill(
            db,
            "s1",
            "dir-alias-skill",
            "Bundle at ${CLAUDE_SKILL_DIR} vs ${TURNSTONE_SKILL_DIR}",
        )
        db.create_skill_resource("r1", "s1", "references/a.md", "# a")

        session = make_session(skill="dir-alias-skill")
        try:
            base = session._skill_resources_dir
            assert base is not None
            assert session._skill_content == f"Bundle at ${{CLAUDE_SKILL_DIR}} vs {base}"
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
    names (``TURNSTONE_SKILL_DIR`` / ``SKILL_RESOURCES_DIR``) unconditionally,
    and never under the foreign ``CLAUDE_SKILL_DIR`` — that name is the host's,
    so turnstone leaves it untouched whether or not the host has set it."""

    def test_turnstone_owned_names_present_claude_absent(
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
            # turnstone never supplies CLAUDE_SKILL_DIR (the host's namespace),
            # even when the host hasn't set it.
            assert "CLAUDE_SKILL_DIR" not in env
        finally:
            session.close()

    def test_host_claude_skill_dir_untouched(
        self, tmp_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # turnstone as a node inside Claude Code: the host's CLAUDE_SKILL_DIR
        # must survive.  turnstone never injects CLAUDE_SKILL_DIR into the
        # extra-env, so scrubbed_env's passthrough keeps the host value.
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


class TestSkillContextPlacement:
    """Step 3: an applied skill's body rides its own capability context
    message (user role), separate from the identity system message — so it
    never occupies the cached identity prefix or reads as identity, and it
    does not leak into the task_agent base."""

    def test_skill_body_in_context_message_not_identity(self, tmp_db: str) -> None:
        db = get_storage()
        _create_skill(db, "s1", "place-skill", "PLACEMENT_MARKER body text")

        session = make_session(skill="place-skill")
        try:
            msgs = session.system_messages
            # Identity system message is first, role=system, and skill-free.
            assert msgs[0]["role"] == "system"
            assert "PLACEMENT_MARKER" not in msgs[0]["content"]
            # Skill rides exactly one separate user-role capability message.
            skill_msgs = [m for m in msgs if m["role"] == "user"]
            assert len(skill_msgs) == 1
            assert "PLACEMENT_MARKER" in skill_msgs[0]["content"]
            # The intro names the active skill so the model knows what it is.
            assert "place-skill" in skill_msgs[0]["content"]
        finally:
            session.close()

    def test_no_skill_no_context_message(self, tmp_db: str) -> None:
        # No applied skill and no defaults → only the identity system message.
        session = make_session()
        try:
            assert all(m["role"] == "system" for m in session.system_messages)
        finally:
            session.close()

    def test_agent_prefix_excludes_skill_context(self, tmp_db: str) -> None:
        # task_agent base = the identity system block only; the parent's
        # applied skill does NOT leak into the sub-agent prefix.
        db = get_storage()
        _create_skill(db, "s1", "leak-skill", "SHOULD_NOT_LEAK body")

        session = make_session(skill="leak-skill")
        try:
            assert len(session._agent_system_messages) == 1
            assert session._agent_system_messages[0]["role"] == "system"
            assert "SHOULD_NOT_LEAK" not in session._agent_system_messages[0]["content"]
        finally:
            session.close()
