"""Tests for prompt template runtime wiring into ChatSession."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from turnstone.core.session import ChatSession, _render_template


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

    def record_output_assessment(
        self,
        call_id,
        assessment,
        *,
        tier="heuristic",
        reasoning="",
        judge_model="",
        latency_ms=0,
        confidence=0.0,
    ):
        pass


def _make_session(**kwargs):
    from turnstone.core.memory import register_workstream

    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    session = ChatSession(**defaults)
    # SessionManager establishes this parent row before constructing the live
    # session.  The direct factory must do the same before slash commands can
    # admit their keyed SYSTEM rows.
    register_workstream(session.ws_id, user_id=kwargs.get("user_id"))
    return session


def _sys_content(session: ChatSession) -> str:
    """Full prompt prefix: identity system message + any skill context message."""
    assert session.system_messages
    return "\n".join(m["content"] for m in session.system_messages)


def _create_template(db, template_id, name, content, **kwargs):
    """Helper to create a prompt template in storage."""
    db.create_prompt_template(
        template_id=template_id,
        name=name,
        category=kwargs.get("category", "general"),
        content=content,
        variables=kwargs.get("variables", "[]"),
        org_id=kwargs.get("org_id", ""),
        created_by=kwargs.get("created_by", "test"),
        origin=kwargs.get("origin", "manual"),
        mcp_server=kwargs.get("mcp_server", ""),
        readonly=kwargs.get("readonly", False),
    )


# ---------------------------------------------------------------------------
# _render_template unit tests
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_basic_substitution(self):
        result = _render_template("Hello {{name}}", {"name": "world"})
        assert result == "Hello world"

    def test_multiple_variables(self):
        result = _render_template(
            "Model: {{model}}, WS: {{ws_id}}", {"model": "gpt-5", "ws_id": "abc123"}
        )
        assert result == "Model: gpt-5, WS: abc123"

    def test_unresolvable_variable_kept(self):
        result = _render_template("Hello {{unknown}}", {"model": "gpt-5"})
        assert result == "Hello {{unknown}}"

    def test_empty_context(self):
        result = _render_template("No vars here", {})
        assert result == "No vars here"

    def test_duplicate_placeholder(self):
        result = _render_template("{{x}} and {{x}}", {"x": "val"})
        assert result == "val and val"

    def test_no_cross_variable_injection(self):
        # If model contains {{ws_id}}, it must NOT be expanded
        result = _render_template("Model: {{model}}", {"model": "{{ws_id}}", "ws_id": "secret"})
        assert result == "Model: {{ws_id}}"
        assert "secret" not in result


# ---------------------------------------------------------------------------
# A session that names no skill
# ---------------------------------------------------------------------------


class TestNoSkillNamed:
    def test_a_session_that_names_no_skill_carries_no_skill_text(self, tmp_db):
        """No skill applies unless one is named, and no skill list rides the
        system prompt (#1292)."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "alpha", "ALPHA_TEXT")
        _create_template(db, "t2", "beta", "BETA_TEXT")

        session = _make_session(instructions="USER_INSTRUCTIONS")
        content = _sys_content(session)
        assert "ALPHA_TEXT" not in content
        assert "BETA_TEXT" not in content
        assert "<available-skills>" not in content
        assert "USER_INSTRUCTIONS" in content
        assert all(m["role"] == "system" for m in session.system_messages)


# ---------------------------------------------------------------------------
# Explicit template selection
# ---------------------------------------------------------------------------


class TestExplicitTemplate:
    def test_named_skill_rides_its_own_message(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "other-tpl", "OTHER_CONTENT")
        _create_template(db, "t2", "specific-tpl", "SPECIFIC_CONTENT")

        session = _make_session(skill="specific-tpl")
        content = _sys_content(session)
        assert "SPECIFIC_CONTENT" in content
        assert "OTHER_CONTENT" not in content
        # Capability context, off the identity system message.
        assert "SPECIFIC_CONTENT" not in session.system_messages[0]["content"]

    def test_explicit_template_not_found(self, tmp_db):
        session = _make_session(skill="nonexistent")
        content = _sys_content(session)
        # Graceful degradation — no template content injected
        assert "nonexistent" not in content


# ---------------------------------------------------------------------------
# Variable substitution in templates
# ---------------------------------------------------------------------------


class TestTemplateVariables:
    def test_model_and_ws_id_substituted(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "vars-tpl", "Model: {{model}}, WS: {{ws_id}}")

        session = _make_session(skill="vars-tpl")
        content = _sys_content(session)
        assert "Model: test-model" in content
        assert f"WS: {session.ws_id}" in content

    def test_node_id_substituted(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "node-tpl", "Node: {{node_id}}")

        session = _make_session(node_id="node-42", skill="node-tpl")
        content = _sys_content(session)
        assert "Node: node-42" in content

    def test_unknown_variable_preserved(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "unknown-tpl", "Val: {{unknown_var}}")

        session = _make_session(skill="unknown-tpl")
        content = _sys_content(session)
        assert "Val: {{unknown_var}}" in content


# ---------------------------------------------------------------------------
# Template persistence and resume
# ---------------------------------------------------------------------------


class TestTemplatePersistence:
    def test_template_persisted_in_config(self, tmp_db):
        from turnstone.core.memory import load_workstream_config
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "TPL_CONTENT")

        session = _make_session(skill="my-tpl")
        config = load_workstream_config(session.ws_id)
        assert config["skill"] == "my-tpl"

    def test_template_restored_on_resume(self, tmp_db):
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "PERSISTED_TEMPLATE")

        # Create session with skill, save a message so resume has history
        session1 = _make_session(skill="my-tpl")
        ws_id = session1.ws_id
        save_message(ws_id, "user", "hello")

        # New session without template, then resume
        session2 = _make_session(ws_id=ws_id)
        assert session2._skill_name is None
        resumed = session2.rehydrate()
        assert resumed
        assert session2._skill_name == "my-tpl"
        content = _sys_content(session2)
        assert "PERSISTED_TEMPLATE" in content

    def test_empty_skill_config_means_no_skill(self, tmp_db):
        from turnstone.core.memory import load_workstream_config

        session = _make_session()
        config = load_workstream_config(session.ws_id)
        assert config["skill"] == ""


class TestCreateTimeSkillOnReopen:
    """A workstream created with a skill keeps that skill across a reopen (#1292).

    Its create-time text (the ``applied_skill_content`` stamp) renders under the
    saved skill name whatever happened to the skill since: edited, renamed,
    disabled, deleted, or replaced by a new skill with the same name. An explicit
    switch or clear survives a reopen.
    """

    _TEXT = "ORIGINAL {{model}} $ARGUMENTS"

    @classmethod
    def _created_with_skill(cls) -> str:
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        _create_template(get_storage(), "t1", "my-tpl", cls._TEXT)
        session = _make_session(skill="my-tpl", skill_arguments="ARGS")
        # What the node's create path records for a workstream named with a skill.
        session._applied_skill_id = "t1"
        session._applied_skill_content = cls._TEXT
        session._save_config()
        save_message(session.ws_id, "user", "hello")
        return session.ws_id

    @staticmethod
    def _reopen(ws_id: str) -> ChatSession:
        session = _make_session(ws_id=ws_id)
        assert session.rehydrate()
        return session

    def test_reopen_renders_the_create_time_text_under_the_skill_name(self, tmp_db):
        from turnstone.core.memory import load_workstream_config
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        get_storage().update_prompt_template("t1", content="EDITED")

        session = self._reopen(ws_id)

        assert session._skill_name == "my-tpl"
        content = _sys_content(session)
        assert "ORIGINAL test-model ARGS" in content
        assert "EDITED" not in content
        assert "ORIGINAL" not in session.system_messages[0]["content"]
        session._save_config()
        assert load_workstream_config(ws_id)["skill"] == "my-tpl"

    def test_a_switch_survives_a_reopen(self, tmp_db):
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        _create_template(get_storage(), "t2", "other-tpl", "OTHER_TEXT")
        self._reopen(ws_id).handle_command("/skill other-tpl")

        session = self._reopen(ws_id)

        assert session._skill_name == "other-tpl"
        content = _sys_content(session)
        assert "OTHER_TEXT" in content
        assert "ORIGINAL" not in content

    def test_a_clear_survives_a_reopen(self, tmp_db):
        ws_id = self._created_with_skill()
        self._reopen(ws_id).handle_command("/skill clear")

        session = self._reopen(ws_id)

        assert session._skill_name is None
        assert "ORIGINAL" not in _sys_content(session)

    def test_a_config_saved_without_the_skill_name_gets_it_back(self, tmp_db):
        """Before #1292 a reopened workstream saved its skill name empty while
        keeping the snapshot."""
        from turnstone.core.memory import save_workstream_config

        ws_id = self._created_with_skill()
        save_workstream_config(ws_id, {"skill": ""})

        session = self._reopen(ws_id)

        assert session._skill_name == "my-tpl"
        assert "ORIGINAL test-model ARGS" in _sys_content(session)

    def test_a_fork_keeps_the_text_the_skill_had_at_creation(self, tmp_db):
        from tests._session_helpers import make_fork_destination
        from turnstone.core.memory import save_message, save_workstream_config
        from turnstone.core.storage import get_storage

        storage = get_storage()
        _create_template(storage, "t1", "my-tpl", self._TEXT)
        storage.register_workstream(
            "fork_source", user_id="owner", state="idle", fork_reservation_token="source-token"
        )
        save_workstream_config(
            "fork_source",
            {
                "skill": "my-tpl",
                "skill_arguments": "ARGS",
                "applied_skill_id": "t1",
                "applied_skill_content": self._TEXT,
            },
        )
        save_message("fork_source", "user", "hello")
        storage.update_prompt_template("t1", content="EDITED")

        fork = make_fork_destination()
        fork.fork_from_storage(
            "fork_source", principal_id="owner", source_reservation_token="source-token"
        )
        reopened = _make_session(ws_id=fork.ws_id, user_id="owner")
        assert reopened.rehydrate()

        assert reopened._skill_name == "my-tpl"
        content = _sys_content(reopened)
        assert "ORIGINAL test-model ARGS" in content
        assert "EDITED" not in content

    def test_a_copy_keeps_the_text_the_skill_had_at_creation(self, tmp_db):
        """CLI ``/new`` copies the settings through ``adopt_settings``."""
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        copy = _make_session()
        copy.adopt_settings(self._reopen(ws_id)._config_for_save())
        save_message(copy.ws_id, "user", "hello")
        get_storage().update_prompt_template("t1", content="EDITED")

        reopened = self._reopen(copy.ws_id)

        assert reopened._skill_name == "my-tpl"
        content = _sys_content(reopened)
        assert "ORIGINAL test-model ARGS" in content
        assert "EDITED" not in content

    def test_a_renamed_and_edited_skill_keeps_its_saved_name_and_text(self, tmp_db):
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        get_storage().update_prompt_template("t1", name="renamed-tpl", content="EDITED")

        session = self._reopen(ws_id)

        assert session._skill_name == "my-tpl"
        content = _sys_content(session)
        assert "your active skill 'my-tpl'" in content
        assert "ORIGINAL test-model ARGS" in content
        assert "EDITED" not in content

    def test_a_deleted_skill_keeps_its_saved_text(self, tmp_db):
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        get_storage().delete_prompt_template("t1")

        session = self._reopen(ws_id)

        assert session._skill_name == "my-tpl"
        assert "ORIGINAL test-model ARGS" in _sys_content(session)
        assert session._skill_resources == {}

    def test_a_new_skill_with_the_old_name_does_not_take_over(self, tmp_db):
        """MCP prompt sync deletes a skill row and re-creates it under a new id."""
        from turnstone.core.storage import get_storage

        storage = get_storage()
        ws_id = self._created_with_skill()
        storage.delete_prompt_template("t1")
        _create_template(storage, "t9", "my-tpl", "REPLACEMENT")
        storage.create_skill_resource("r9", "t9", "scripts/helper.sh", "echo replacement")

        session = self._reopen(ws_id)

        content = _sys_content(session)
        assert "ORIGINAL test-model ARGS" in content
        assert "REPLACEMENT" not in content
        # Nothing of the newer row loads: its bundled files stay out too.
        assert session._skill_resources == {}
        assert "helper.sh" not in content

    def test_a_disabled_skill_keeps_its_saved_text(self, tmp_db):
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        get_storage().update_prompt_template("t1", enabled=False)

        session = self._reopen(ws_id)

        assert "ORIGINAL test-model ARGS" in _sys_content(session)

    def test_a_renamed_and_disabled_skill_still_supplies_files_and_argument_names(self, tmp_db):
        """While its row exists, found by id whatever its name or enabled flag, the saved skill
        takes its argument names and bundled files from it (#1292)."""
        import os

        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        storage = get_storage()
        text = "Greet $who; read ${TURNSTONE_SKILL_DIR}/references/guide.md"
        _create_template(storage, "t1", "my-tpl", text)
        storage.update_prompt_template("t1", arguments='["who"]')
        session = _make_session(skill="my-tpl", skill_arguments="Ann")
        session._applied_skill_id = "t1"
        session._applied_skill_content = text
        session._save_config()
        save_message(session.ws_id, "user", "hello")
        storage.create_skill_resource("r1", "t1", "references/guide.md", "GUIDE BODY")
        storage.update_prompt_template("t1", name="renamed-tpl", enabled=False)

        reopened = self._reopen(session.ws_id)
        try:
            base = reopened._skill_resources_dir
            assert base is not None
            assert f"Greet Ann; read {base}/references/guide.md" in _sys_content(reopened)
            with open(os.path.join(base, "references", "guide.md")) as f:
                assert f.read() == "GUIDE BODY"
        finally:
            reopened.close()

    def test_a_failed_lookup_still_renders_the_saved_text(self, tmp_db, monkeypatch):
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()

        def _down(template_id):
            raise RuntimeError("storage down")

        monkeypatch.setattr(get_storage(), "get_prompt_template", _down)
        session = self._reopen(ws_id)

        assert "ORIGINAL test-model ARGS" in _sys_content(session)
        assert session._skill_resources == {}

    def test_a_nameless_older_config_whose_skill_was_deleted_keeps_the_text(self, tmp_db):
        from turnstone.core.memory import save_workstream_config
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        save_workstream_config(ws_id, {"skill": ""})
        get_storage().delete_prompt_template("t1")

        session = self._reopen(ws_id)

        assert session._skill_name is None
        content = _sys_content(session)
        assert "the guidance for your active skill. Apply" in content
        assert "ORIGINAL test-model ARGS" in content

    def test_loading_the_saved_skill_again_changes_nothing(self, tmp_db):
        ws_id = self._created_with_skill()
        session = self._reopen(ws_id)

        _, msg = session._exec_skills_load({"call_id": "c1", "name": "my-tpl", "arguments": "ARGS"})

        assert msg == "Skill 'my-tpl' is already active"
        assert session._applied_skill_content == self._TEXT

    def test_loading_a_skill_missing_at_load_applies_it(self, tmp_db):
        """A session whose named skill was missing when it loaded applies the skill once a row has
        that name, rather than reporting it already active (#1292)."""
        from turnstone.core.storage import get_storage

        session = _make_session(skill="ghost")
        assert session._skill_content is None
        _create_template(get_storage(), "t9", "ghost", "GHOST_TEXT")

        _, msg = session._exec_skills_load({"call_id": "c1", "name": "ghost", "arguments": ""})

        assert msg.startswith("Loaded skill 'ghost'")
        assert "GHOST_TEXT" in _sys_content(session)

    def _reopen_noting_info(self, ws_id: str) -> list[str]:
        infos: list[str] = []
        ui = NullUI()
        ui.on_info = infos.append  # type: ignore[method-assign]
        session = _make_session(ws_id=ws_id, ui=ui)
        assert session.rehydrate()
        return [m for m in infos if "risk level" in m]

    def test_a_renamed_high_risk_skill_is_named_as_saved(self, tmp_db):
        """The risk notice names the skill whose text renders, not the row's new name."""
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        storage = get_storage()
        storage.update_prompt_template("t1", name="renamed-tpl")
        storage.update_prompt_template("t1", risk_level="high")

        notices = self._reopen_noting_info(ws_id)

        assert len(notices) == 1
        assert "'my-tpl'" in notices[0]
        assert "renamed-tpl" not in notices[0]

    def test_an_edited_skill_gives_no_risk_notice_for_its_saved_text(self, tmp_db):
        """The row's tier describes its new text, not the saved text that renders."""
        from turnstone.core.storage import get_storage

        ws_id = self._created_with_skill()
        storage = get_storage()
        storage.update_prompt_template("t1", content="EDITED")
        storage.update_prompt_template("t1", risk_level="high")

        assert self._reopen_noting_info(ws_id) == []

    def test_loading_a_new_skill_under_the_saved_name_applies_it(self, tmp_db):
        from turnstone.core.storage import get_storage

        storage = get_storage()
        ws_id = self._created_with_skill()
        storage.delete_prompt_template("t1")
        _create_template(storage, "t9", "my-tpl", "REPLACEMENT")
        session = self._reopen(ws_id)

        _, msg = session._exec_skills_load({"call_id": "c1", "name": "my-tpl", "arguments": "ARGS"})

        assert msg.startswith("Loaded skill 'my-tpl'")
        assert session._applied_skill_content == ""
        content = _sys_content(session)
        assert "REPLACEMENT" in content
        assert "ORIGINAL" not in content


# ---------------------------------------------------------------------------
# /template slash command
# ---------------------------------------------------------------------------


class TestTemplateSlashCommand:
    def test_template_set(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "SLASH_TEMPLATE")

        session = _make_session()
        content_before = _sys_content(session)
        assert "SLASH_TEMPLATE" not in content_before

        session.handle_command("/skill my-tpl")
        assert session._skill_name == "my-tpl"
        content_after = _sys_content(session)
        assert "SLASH_TEMPLATE" in content_after

    def test_template_clear(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "EXPLICIT_TEMPLATE")
        _create_template(db, "t2", "other-tpl", "OTHER_TEMPLATE")

        session = _make_session(skill="my-tpl")
        assert "EXPLICIT_TEMPLATE" in _sys_content(session)

        session.handle_command("/skill clear")
        assert session._skill_name is None
        assert "EXPLICIT_TEMPLATE" not in _sys_content(session)
        assert "OTHER_TEMPLATE" not in _sys_content(session)

    def test_template_not_found(self, tmp_db):
        ui = NullUI()
        ui.on_error = MagicMock()
        session = _make_session(ui=ui)
        session.handle_command("/skill nonexistent")
        ui.on_error.assert_called_once()
        assert "not found" in ui.on_error.call_args[0][0].lower()

    def test_template_show_current(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "content")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="my-tpl")
        session.handle_command("/skill")
        ui.on_info.assert_called_once()
        assert "my-tpl" in ui.on_info.call_args[0][0]


# ---------------------------------------------------------------------------
# MCP-origin templates
# ---------------------------------------------------------------------------


class TestMCPTemplates:
    def test_mcp_template_selectable_explicitly(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(
            db,
            "t1",
            "mcp__server__code",
            "MCP_EXPLICIT",
            origin="mcp",
            mcp_server="server",
            readonly=True,
        )

        session = _make_session(skill="mcp__server__code")
        content = _sys_content(session)
        assert "MCP_EXPLICIT" in content


# ---------------------------------------------------------------------------
# Resume with deleted template
# ---------------------------------------------------------------------------


class TestResumeDeletedTemplate:
    def test_resume_with_deleted_template_degrades_gracefully(self, tmp_db, caplog):
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "ephemeral-tpl", "EPHEMERAL_CONTENT")

        # Create session with template, save a message so resume has history
        session1 = _make_session(skill="ephemeral-tpl")
        ws_id = session1.ws_id
        save_message(ws_id, "user", "hello")
        assert "EPHEMERAL_CONTENT" in _sys_content(session1)

        # Delete the template from storage
        db.delete_prompt_template("t1")

        # Resume into a new session
        session2 = _make_session(ws_id=ws_id)
        resumed = session2.rehydrate()

        assert resumed
        assert session2._skill_name == "ephemeral-tpl"
        assert session2._skill_content is None
        # System message should not contain the deleted template content
        content = _sys_content(session2)
        assert "EPHEMERAL_CONTENT" not in content
        # Warning should be logged via structlog
        assert "not_found" in caplog.text


# ---------------------------------------------------------------------------
# Threading safety
# ---------------------------------------------------------------------------


class TestSkillFactoryPassthrough:
    def test_skill_passed_through_workstream_create(self, tmp_db):
        """SessionManager.create(skill=...) propagates to session factory."""
        import queue

        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.session_manager import SessionManager
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "factory-tpl", "FACTORY_CONTENT")

        captured_skill = None

        def factory(ui, model_alias=None, ws_id=None, *, skill=None, **_kwargs):
            nonlocal captured_skill
            captured_skill = skill
            return _make_session(
                skill=captured_skill, workstream_lease=_kwargs.get("workstream_lease")
            )

        gq: queue.Queue[dict] = queue.Queue(maxsize=1000)
        adapter = InteractiveAdapter(
            global_queue=gq,
            ui_factory=lambda ws: NullUI(),
            session_factory=factory,
        )
        mgr = SessionManager(adapter, storage=MagicMock(), max_active=10, event_emitter=adapter)
        ws = mgr.create(user_id="", name="test", skill="factory-tpl")
        assert captured_skill == "factory-tpl"
        assert ws.session is not None
        assert ws.session._skill_name == "factory-tpl"
        assert "FACTORY_CONTENT" in _sys_content(ws.session)

    def test_no_skill_passes_none(self, tmp_db):
        """SessionManager.create() without skill passes None."""
        import queue

        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.session_manager import SessionManager

        captured_skill = "sentinel"

        def factory(ui, model_alias=None, ws_id=None, *, skill=None, **_kwargs):
            nonlocal captured_skill
            captured_skill = skill
            return _make_session(skill=skill, workstream_lease=_kwargs.get("workstream_lease"))

        gq: queue.Queue[dict] = queue.Queue(maxsize=1000)
        adapter = InteractiveAdapter(
            global_queue=gq,
            ui_factory=lambda ws: NullUI(),
            session_factory=factory,
        )
        mgr = SessionManager(adapter, storage=MagicMock(), max_active=10, event_emitter=adapter)
        mgr.create(user_id="", name="test")
        assert captured_skill is None


class TestTemplateThreadSafety:
    def test_concurrent_template_and_system_message_init(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "thread-tpl", "THREAD_TEMPLATE")

        session = _make_session(skill="thread-tpl")
        errors: list[Exception] = []
        stop = threading.Event()
        iterations = 200

        def init_loop():
            """Simulate MCP callback repeatedly calling _init_system_messages."""
            try:
                for _ in range(iterations):
                    if stop.is_set():
                        break
                    session._init_system_messages()
                    # system_messages must always be a valid list
                    msgs = session.system_messages
                    assert isinstance(msgs, list)
                    assert len(msgs) > 0
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=init_loop, daemon=True)
        t.start()

        # Main thread toggles template on/off
        try:
            for i in range(iterations):
                if i % 2 == 0:
                    session.set_skill("thread-tpl")
                else:
                    session.set_skill(None)
        finally:
            stop.set()
            t.join(timeout=5)

        assert not errors, f"Thread raised: {errors}"
        # Final state: system_messages is a valid list
        msgs = session.system_messages
        assert isinstance(msgs, list)
        assert len(msgs) > 0
