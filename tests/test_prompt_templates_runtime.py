"""Tests for prompt template runtime wiring into ChatSession."""

from __future__ import annotations

from unittest.mock import MagicMock

from turnstone.core.session import ChatSession, _render_template


class NullUI:
    """UI adapter that discards all output."""

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

    def on_tool_result(self, call_id, name, output):
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


def _make_session(**kwargs):
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
    return ChatSession(**defaults)


def _sys_content(session: ChatSession) -> str:
    """Extract the system message content."""
    msgs = [m for m in session.system_messages if m["role"] == "system"]
    assert msgs
    return msgs[0]["content"]


def _create_template(db, template_id, name, content, is_default=False, **kwargs):
    """Helper to create a prompt template in storage."""
    db.create_prompt_template(
        template_id=template_id,
        name=name,
        category=kwargs.get("category", "general"),
        content=content,
        variables=kwargs.get("variables", "[]"),
        is_default=is_default,
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
# Default templates in system message
# ---------------------------------------------------------------------------


class TestDefaultTemplates:
    def test_default_templates_in_system_message(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "alpha", "You are a helpful assistant.", is_default=True)
        _create_template(db, "t2", "beta", "Always be concise.", is_default=True)

        session = _make_session()
        content = _sys_content(session)
        assert "You are a helpful assistant." in content
        assert "Always be concise." in content

    def test_default_templates_ordered_by_name(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t2", "b-template", "SECOND", is_default=True)
        _create_template(db, "t1", "a-template", "FIRST", is_default=True)

        session = _make_session()
        content = _sys_content(session)
        first_pos = content.index("FIRST")
        second_pos = content.index("SECOND")
        assert first_pos < second_pos

    def test_no_default_templates(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "alpha", "Not default.", is_default=False)

        session = _make_session()
        content = _sys_content(session)
        assert "Not default." not in content

    def test_templates_before_instructions(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "tpl", "TEMPLATE_CONTENT", is_default=True)

        session = _make_session(instructions="USER_INSTRUCTIONS")
        content = _sys_content(session)
        tpl_pos = content.index("TEMPLATE_CONTENT")
        instr_pos = content.index("USER_INSTRUCTIONS")
        assert tpl_pos < instr_pos


# ---------------------------------------------------------------------------
# Explicit template selection
# ---------------------------------------------------------------------------


class TestExplicitTemplate:
    def test_explicit_template_replaces_defaults(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "default-tpl", "DEFAULT_CONTENT", is_default=True)
        _create_template(db, "t2", "specific-tpl", "SPECIFIC_CONTENT", is_default=False)

        session = _make_session(template="specific-tpl")
        content = _sys_content(session)
        assert "SPECIFIC_CONTENT" in content
        assert "DEFAULT_CONTENT" not in content

    def test_explicit_template_not_found(self, tmp_db):
        session = _make_session(template="nonexistent")
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
        _create_template(db, "t1", "vars-tpl", "Model: {{model}}, WS: {{ws_id}}", is_default=True)

        session = _make_session()
        content = _sys_content(session)
        assert "Model: test-model" in content
        assert f"WS: {session.ws_id}" in content

    def test_node_id_substituted(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "node-tpl", "Node: {{node_id}}", is_default=True)

        session = _make_session(node_id="node-42")
        content = _sys_content(session)
        assert "Node: node-42" in content

    def test_unknown_variable_preserved(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "unknown-tpl", "Val: {{unknown_var}}", is_default=True)

        session = _make_session()
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
        _create_template(db, "t1", "my-tpl", "TPL_CONTENT", is_default=False)

        session = _make_session(template="my-tpl")
        config = load_workstream_config(session.ws_id)
        assert config["template"] == "my-tpl"

    def test_template_restored_on_resume(self, tmp_db):
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "PERSISTED_TEMPLATE", is_default=False)

        # Create session with template, save a message so resume has history
        session1 = _make_session(template="my-tpl")
        ws_id = session1.ws_id
        save_message(ws_id, "user", "hello")

        # New session without template, then resume
        session2 = _make_session()
        assert session2._template_name is None
        resumed = session2.resume(ws_id)
        assert resumed
        assert session2._template_name == "my-tpl"
        content = _sys_content(session2)
        assert "PERSISTED_TEMPLATE" in content

    def test_empty_template_config_means_defaults(self, tmp_db):
        from turnstone.core.memory import load_workstream_config

        session = _make_session()
        config = load_workstream_config(session.ws_id)
        assert config["template"] == ""


# ---------------------------------------------------------------------------
# /template slash command
# ---------------------------------------------------------------------------


class TestTemplateSlashCommand:
    def test_template_set(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "SLASH_TEMPLATE", is_default=False)

        session = _make_session()
        content_before = _sys_content(session)
        assert "SLASH_TEMPLATE" not in content_before

        session.handle_command("/template my-tpl")
        assert session._template_name == "my-tpl"
        content_after = _sys_content(session)
        assert "SLASH_TEMPLATE" in content_after

    def test_template_clear(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "EXPLICIT_TEMPLATE", is_default=False)
        _create_template(db, "t2", "default-tpl", "DEFAULT_TEMPLATE", is_default=True)

        session = _make_session(template="my-tpl")
        assert "EXPLICIT_TEMPLATE" in _sys_content(session)
        assert "DEFAULT_TEMPLATE" not in _sys_content(session)

        session.handle_command("/template clear")
        assert session._template_name is None
        assert "DEFAULT_TEMPLATE" in _sys_content(session)
        assert "EXPLICIT_TEMPLATE" not in _sys_content(session)

    def test_template_not_found(self, tmp_db):
        ui = NullUI()
        ui.on_error = MagicMock()
        session = _make_session(ui=ui)
        session.handle_command("/template nonexistent")
        ui.on_error.assert_called_once()
        assert "not found" in ui.on_error.call_args[0][0].lower()

    def test_template_show_current(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "t1", "my-tpl", "content", is_default=False)

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, template="my-tpl")
        session.handle_command("/template")
        ui.on_info.assert_called_once()
        assert "my-tpl" in ui.on_info.call_args[0][0]


# ---------------------------------------------------------------------------
# MCP-origin templates
# ---------------------------------------------------------------------------


class TestMCPTemplates:
    def test_mcp_readonly_template_as_default(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(
            db,
            "t1",
            "mcp__server__prompt",
            "MCP_CONTENT",
            is_default=True,
            origin="mcp",
            mcp_server="server",
            readonly=True,
        )

        session = _make_session()
        content = _sys_content(session)
        assert "MCP_CONTENT" in content

    def test_mcp_template_selectable_explicitly(self, tmp_db):
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(
            db,
            "t1",
            "mcp__server__code",
            "MCP_EXPLICIT",
            is_default=False,
            origin="mcp",
            mcp_server="server",
            readonly=True,
        )

        session = _make_session(template="mcp__server__code")
        content = _sys_content(session)
        assert "MCP_EXPLICIT" in content
