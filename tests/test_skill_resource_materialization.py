"""Tests for skill resource materialization to disk.

Verifies that skill-bundled resources (scripts, references, assets) stored
in the ``skill_resources`` table are written to a temp directory when a
skill is loaded, exposed via ``SKILL_RESOURCES_DIR`` env var and ``PATH``,
and cleaned up on skill change or session close.
"""

from __future__ import annotations

import os
import stat
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.session import ChatSession
from turnstone.core.storage._registry import get_storage

# ---------------------------------------------------------------------------
# Helpers (mirrors test_skills.py)
# ---------------------------------------------------------------------------


class NullUI:
    """UI adapter that discards all output."""

    def on_turn_start(self):
        pass

    def on_turn_committed(self):
        pass

    def on_thinking_start(self):
        pass

    def on_thinking_stop(self):
        pass

    def on_reasoning_token(self, text):
        pass

    def on_content_token(self, text):
        pass

    def on_stream_end(self):
        pass

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output, **kwargs):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass

    def on_state_change(self, state):
        pass

    def on_rename(self, name):
        pass

    def on_output_warning(self, call_id, assessment):
        pass


def _make_session(**kwargs: Any) -> ChatSession:
    defaults: dict[str, Any] = dict(
        client=MagicMock(),
        model="test-model",
        ui=NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _create_skill(db: Any, skill_id: str, name: str, content: str, **kw: Any) -> None:
    db.create_prompt_template(
        template_id=skill_id,
        name=name,
        category=kw.get("category", "general"),
        content=content,
        variables=kw.get("variables", "[]"),
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


def _sys_content(session: ChatSession) -> str:
    msgs = [m for m in session.system_messages if m["role"] == "system"]
    assert msgs
    return msgs[0]["content"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMaterializeResources:
    def test_materialize_creates_files(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "test-skill", "Use the scripts.")
        db.create_skill_resource("r1", "s1", "scripts/helper.py", "print('hello')")
        db.create_skill_resource("r2", "s1", "references/api.md", "# API")

        session = _make_session(skill="test-skill")
        assert session._skill_resources_dir is not None
        base = session._skill_resources_dir
        assert os.path.isdir(base)

        helper = os.path.join(base, "scripts", "helper.py")
        assert os.path.isfile(helper)
        with open(helper) as f:
            assert f.read() == "print('hello')"

        api_md = os.path.join(base, "references", "api.md")
        assert os.path.isfile(api_md)
        with open(api_md) as f:
            assert f.read() == "# API"

        session.close()

    def test_scripts_executable(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "exec-skill", "Run scripts/run.sh")
        db.create_skill_resource("r1", "s1", "scripts/run.sh", "#!/bin/bash\necho hi")

        session = _make_session(skill="exec-skill")
        base = session._skill_resources_dir
        run_sh = os.path.join(base, "scripts", "run.sh")
        mode = os.stat(run_sh).st_mode
        assert mode & stat.S_IXUSR  # owner execute
        session.close()

    def test_non_scripts_not_executable(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "ref-skill", "Read references/guide.md")
        db.create_skill_resource("r1", "s1", "references/guide.md", "# Guide")

        session = _make_session(skill="ref-skill")
        base = session._skill_resources_dir
        guide = os.path.join(base, "references", "guide.md")
        mode = os.stat(guide).st_mode
        assert not (mode & stat.S_IXUSR)  # not executable
        session.close()

    def test_cleanup_on_close(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "cleanup-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/a.py", "code")

        session = _make_session(skill="cleanup-skill")
        base = session._skill_resources_dir
        assert os.path.isdir(base)

        session.close()
        assert not os.path.exists(base)
        assert session._skill_resources_dir is None

    def test_cleanup_on_skill_switch(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "skill-a", "Skill A")
        db.create_skill_resource("r1", "s1", "scripts/a.py", "code_a")
        _create_skill(db, "s2", "skill-b", "Skill B")
        db.create_skill_resource("r2", "s2", "scripts/b.py", "code_b")

        session = _make_session(skill="skill-a")
        dir_a = session._skill_resources_dir
        assert os.path.isfile(os.path.join(dir_a, "scripts", "a.py"))

        session.set_skill("skill-b")
        dir_b = session._skill_resources_dir
        assert dir_b != dir_a
        assert not os.path.exists(dir_a)
        assert os.path.isfile(os.path.join(dir_b, "scripts", "b.py"))

        session.close()

    def test_cleanup_on_skill_clear(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "clear-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/x.py", "code")

        session = _make_session(skill="clear-skill")
        base = session._skill_resources_dir
        assert os.path.isdir(base)

        session.set_skill(None)
        assert not os.path.exists(base)
        assert session._skill_resources_dir is None

        session.close()

    def test_empty_resources_no_dir(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "no-res-skill", "content")
        # No resources added

        session = _make_session(skill="no-res-skill")
        assert session._skill_resources_dir is None
        session.close()

    def test_no_skill_no_dir(self, tmp_db):
        session = _make_session()
        assert session._skill_resources_dir is None
        session.close()

    def test_path_traversal_rejected(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "traversal-skill", "content")
        # Inject a malicious path directly into storage
        db.create_skill_resource("r1", "s1", "../etc/passwd", "bad content")
        db.create_skill_resource("r2", "s1", "scripts/good.py", "good content")

        session = _make_session(skill="traversal-skill")
        base = session._skill_resources_dir
        # The traversal path must not be written inside the resources dir
        assert not os.path.exists(os.path.join(base, "etc"))
        # The good resource should still be materialized
        assert os.path.isfile(os.path.join(base, "scripts", "good.py"))
        session.close()


class TestSkillResourceEnv:
    def test_env_with_resources(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "env-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/tool.py", "code")

        session = _make_session(skill="env-skill")
        env = session._skill_resource_env()
        assert env["SKILL_RESOURCES_DIR"] == session._skill_resources_dir
        assert "PATH" in env
        scripts_dir = os.path.join(session._skill_resources_dir, "scripts")
        assert env["PATH"].startswith(scripts_dir + ":")
        session.close()

    def test_env_without_scripts_dir(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "no-scripts-skill", "content")
        db.create_skill_resource("r1", "s1", "references/doc.md", "# Doc")

        session = _make_session(skill="no-scripts-skill")
        env = session._skill_resource_env()
        assert "SKILL_RESOURCES_DIR" in env
        # No scripts/ subdir so PATH should not be overridden
        assert "PATH" not in env
        session.close()

    def test_env_empty_when_no_resources(self, tmp_db):
        session = _make_session()
        assert session._skill_resource_env() == {}
        session.close()


class TestSystemMessageHint:
    def test_hint_present_when_resources_exist(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "hint-skill", "Use the bundled scripts.")
        db.create_skill_resource("r1", "s1", "scripts/run.py", "code")

        session = _make_session(skill="hint-skill")
        content = _sys_content(session)
        assert "$SKILL_RESOURCES_DIR" in content
        assert "scripts/ are on PATH" in content
        session.close()

    def test_no_hint_when_no_resources(self, tmp_db):
        db = get_storage()
        _create_skill(db, "s1", "plain-skill", "No resources here.")

        session = _make_session(skill="plain-skill")
        content = _sys_content(session)
        assert "SKILL_RESOURCES_DIR" not in content
        session.close()


class TestMaterializeEdgeCases:
    def test_all_resources_rejected_no_dir(self, tmp_db):
        """When every resource fails path validation, no temp dir is left."""
        db = get_storage()
        _create_skill(db, "s1", "all-bad", "content")
        db.create_skill_resource("r1", "s1", "../escape", "bad")
        db.create_skill_resource("r2", "s1", "/absolute", "bad")

        session = _make_session(skill="all-bad")
        assert session._skill_resources_dir is None
        session.close()

    def test_dot_path_rejected(self, tmp_db):
        """A bare '.' path is rejected rather than crashing."""
        db = get_storage()
        _create_skill(db, "s1", "dot-skill", "content")
        db.create_skill_resource("r1", "s1", ".", "bad")
        db.create_skill_resource("r2", "s1", "scripts/ok.py", "good")

        session = _make_session(skill="dot-skill")
        base = session._skill_resources_dir
        assert os.path.isfile(os.path.join(base, "scripts", "ok.py"))
        session.close()

    def test_empty_path_rejected(self, tmp_db):
        """An empty string path is rejected."""
        db = get_storage()
        _create_skill(db, "s1", "empty-skill", "content")
        db.create_skill_resource("r1", "s1", "", "bad")
        db.create_skill_resource("r2", "s1", "scripts/ok.py", "good")

        session = _make_session(skill="empty-skill")
        assert session._skill_resources_dir is not None
        session.close()

    def test_nested_traversal_rejected(self, tmp_db):
        """Traversal hidden inside a valid prefix is still caught."""
        db = get_storage()
        _create_skill(db, "s1", "nested-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/../../../etc/passwd", "bad")
        db.create_skill_resource("r2", "s1", "scripts/ok.py", "good")

        session = _make_session(skill="nested-skill")
        base = session._skill_resources_dir
        assert not os.path.exists(os.path.join(base, "etc"))
        assert os.path.isfile(os.path.join(base, "scripts", "ok.py"))
        session.close()

    def test_double_close_idempotent(self, tmp_db):
        """Calling close() twice does not raise."""
        db = get_storage()
        _create_skill(db, "s1", "double-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/x.py", "code")

        session = _make_session(skill="double-skill")
        session.close()
        session.close()  # must not raise


class TestPreflightValidation:
    def test_missing_resource_warns(self, tmp_db):
        """Skill content references a script not in resources."""
        db = get_storage()
        _create_skill(db, "s1", "warn-skill", "Run scripts/missing.py to start.")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="warn-skill")
        ui.on_info.assert_called_once()
        msg = ui.on_info.call_args[0][0]
        assert "scripts/missing.py" in msg
        assert "warn-skill" in msg
        session.close()

    def test_all_resources_present_no_warn(self, tmp_db):
        """No warning when all referenced paths are bundled."""
        db = get_storage()
        _create_skill(db, "s1", "ok-skill", "Run scripts/helper.py for help.")
        db.create_skill_resource("r1", "s1", "scripts/helper.py", "print('hi')")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="ok-skill")
        ui.on_info.assert_not_called()
        session.close()

    def test_no_references_no_warn(self, tmp_db):
        """Skill content with no resource paths triggers no validation warning."""
        db = get_storage()
        _create_skill(db, "s1", "plain-skill", "Just a plain skill with no paths.")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="plain-skill")
        ui.on_info.assert_not_called()
        session.close()

    def test_multiple_missing_warns_once(self, tmp_db):
        """Multiple missing resources produce a single warning listing all."""
        db = get_storage()
        _create_skill(
            db,
            "s1",
            "multi-skill",
            "Use scripts/a.py and scripts/b.sh to process references/guide.md",
        )

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="multi-skill")
        ui.on_info.assert_called_once()
        msg = ui.on_info.call_args[0][0]
        assert "3 resource(s)" in msg
        assert "scripts/a.py" in msg
        assert "scripts/b.sh" in msg
        assert "references/guide.md" in msg
        session.close()

    def test_validation_skipped_no_skill(self, tmp_db):
        """No crash or warning when no skill is active."""
        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui)
        ui.on_info.assert_not_called()
        session.close()

    def test_json_extension_not_truncated(self, tmp_db):
        """assets/config.json should match as .json, not .js."""
        db = get_storage()
        _create_skill(db, "s1", "json-skill", "Load assets/config.json for settings.")
        db.create_skill_resource("r1", "s1", "assets/config.json", "{}")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="json-skill")
        ui.on_info.assert_not_called()
        session.close()

    def test_compound_prefix_not_matched(self, tmp_db):
        """'myscripts/tool.py' should not match as 'scripts/tool.py'."""
        db = get_storage()
        _create_skill(
            db,
            "s1",
            "compound-skill",
            "The myscripts/tool.py file is unrelated.",
        )

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="compound-skill")
        ui.on_info.assert_not_called()
        session.close()

    def test_extension_suffix_not_matched(self, tmp_db):
        """'scripts/tool.python' should not match as 'scripts/tool.py'."""
        db = get_storage()
        _create_skill(
            db,
            "s1",
            "suffix-skill",
            "Run scripts/tool.python to start.",
        )

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="suffix-skill")
        ui.on_info.assert_not_called()
        session.close()
