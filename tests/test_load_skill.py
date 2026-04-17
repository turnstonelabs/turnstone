"""Tests for the skill built-in tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from turnstone.core.tools import BUILTIN_TOOL_NAMES, PRIMARY_KEY_MAP


class TestToolRegistration:
    """Verify skill is registered correctly."""

    def test_in_builtin_tool_names(self) -> None:
        assert "skill" in BUILTIN_TOOL_NAMES

    def test_not_agent_tool(self) -> None:
        from turnstone.core.tools import AGENT_TOOLS

        names = {t["function"]["name"] for t in AGENT_TOOLS}
        assert "skill" not in names

    def test_not_task_agent_tool(self) -> None:
        from turnstone.core.tools import TASK_AGENT_TOOLS

        names = {t["function"]["name"] for t in TASK_AGENT_TOOLS}
        assert "skill" not in names

    def test_has_primary_key(self) -> None:
        assert PRIMARY_KEY_MAP.get("skill") == "name"


# ---------------------------------------------------------------------------
# Helpers — minimal ChatSession mock
# ---------------------------------------------------------------------------


def _make_session(skills: list[dict[str, Any]] | None = None):
    """Build a minimal ChatSession with stubbed storage."""
    from turnstone.core.session import ChatSession

    ui = MagicMock()
    session = ChatSession.__new__(ChatSession)

    # Minimal state required by the methods under test
    session.ui = ui
    session.model = "test-model"
    session._ws_id = "ws-test"
    session._node_id = "node-1"
    session._skill_name = None
    session._skill_content = None
    session._applied_skill_content = None
    session.context_window = 128000
    session._notify_on_complete = "{}"
    session.messages = []
    session._config = {}
    session._tool_error_flags = {}

    # Stub set_skill to just record the call
    session._set_skill_called: list[str | None] = []

    def fake_set_skill(name):
        session._set_skill_called.append(name)
        session._skill_name = name

    session.set_skill = fake_set_skill

    # Storage mock
    _skills = skills or []

    def fake_get_skill_by_name(name):
        for s in _skills:
            if s.get("name") == name:
                return s
        return None

    return session, _skills, fake_get_skill_by_name


# ---------------------------------------------------------------------------
# Tests: Preparer
# ---------------------------------------------------------------------------


class TestPrepareLoadSkill:
    """Test _prepare_skill validation and item dict shape."""

    def test_load_valid(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "load", "name": "code-review"})
        assert item["func_name"] == "skill"
        assert item["action"] == "load"
        assert item["name"] == "code-review"
        assert item["needs_approval"] is True
        assert "execute" in item
        assert "error" not in item

    def test_load_missing_name(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "load"})
        assert "error" in item
        assert "name" in item["error"].lower()
        assert item["needs_approval"] is False

    def test_load_empty_name(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "load", "name": ""})
        assert "error" in item

    def test_search_with_query(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "code review"})
        assert item["action"] == "search"
        assert item["query"] == "code review"
        assert item["needs_approval"] is False
        assert "execute" in item

    def test_search_without_query(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search"})
        assert item["action"] == "search"
        assert item["query"] == ""
        assert item["needs_approval"] is False

    def test_invalid_action(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "delete"})
        assert "error" in item
        assert "delete" in item["error"]

    def test_empty_action(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": ""})
        assert "error" in item

    def test_header_for_load(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "load", "name": "my-skill"})
        assert "my-skill" in item["header"]

    def test_header_for_search(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "testing"})
        assert "testing" in item["header"]


# ---------------------------------------------------------------------------
# Tests: Executor
# ---------------------------------------------------------------------------


class TestExecLoadSkill:
    """Test _exec_skill execution logic."""

    def test_load_existing_skill(self) -> None:
        skills = [
            {
                "name": "code-review",
                "description": "Reviews code for quality",
                "content": "# Code Review\nReview all code.",
                "scan_status": "safe",
                "category": "engineering",
            }
        ]
        session, _, fake_get = _make_session(skills)

        with patch("turnstone.core.session.get_skill_by_name", side_effect=fake_get):
            item = session._prepare_skill("call-1", {"action": "load", "name": "code-review"})
            call_id, result = session._exec_skill(item)

        assert call_id == "call-1"
        assert "code-review" in result
        assert "Reviews code" in result
        assert "safe" in result
        assert session._set_skill_called == ["code-review"]

    def test_load_nonexistent_skill(self) -> None:
        session, _, fake_get = _make_session([])

        with patch("turnstone.core.session.get_skill_by_name", side_effect=fake_get):
            item = session._prepare_skill("call-1", {"action": "load", "name": "nope"})
            call_id, result = session._exec_skill(item)

        assert "not found" in result.lower()
        assert session._set_skill_called == []

    def test_load_calls_ui_on_tool_result(self) -> None:
        skills = [{"name": "test", "content": "content", "description": "", "scan_status": ""}]
        session, _, fake_get = _make_session(skills)

        with patch("turnstone.core.session.get_skill_by_name", side_effect=fake_get):
            item = session._prepare_skill("call-1", {"action": "load", "name": "test"})
            session._exec_skill(item)

        session.ui.on_tool_result.assert_called_once()

    def test_search_returns_results(self) -> None:
        skills = [
            {
                "name": "code-review",
                "description": "Reviews code",
                "category": "eng",
                "scan_status": "safe",
                "tags": "[]",
                "activation": "named",
            },
            {
                "name": "docs-writer",
                "description": "Writes docs",
                "category": "general",
                "scan_status": "low",
                "tags": "[]",
                "activation": "named",
            },
        ]
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = skills

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "code"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        assert "code-review" in result
        # docs-writer shouldn't match "code" query
        assert "docs-writer" not in result

    def test_search_empty_query_returns_all(self) -> None:
        skills = [
            {
                "name": f"skill-{i}",
                "description": f"Desc {i}",
                "category": "general",
                "scan_status": "",
                "tags": "[]",
                "activation": "named",
            }
            for i in range(15)
        ]
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = skills

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        # Should be limited to 10
        assert result.count("skill-") == 10

    def test_search_no_results(self) -> None:
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = []

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "nonexistent"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        assert "no skills found" in result.lower()

    def test_search_includes_scan_status(self) -> None:
        skills = [
            {
                "name": "risky",
                "description": "Risky skill",
                "category": "ops",
                "scan_status": "high",
                "tags": "[]",
                "activation": "named",
            },
        ]
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = skills

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "risky"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        assert "high" in result

    def test_search_storage_failure_returns_empty(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "test"})

        with patch(
            "turnstone.core.storage._registry.get_storage", side_effect=RuntimeError("no storage")
        ):
            call_id, result = session._exec_skill(item)

        assert "no skills found" in result.lower()

    def test_load_disabled_skill_returns_not_found(self) -> None:
        skills = [
            {
                "name": "disabled-skill",
                "content": "x",
                "description": "",
                "scan_status": "",
                "enabled": False,
            }
        ]
        session, _, fake_get = _make_session(skills)

        with patch("turnstone.core.session.get_skill_by_name", side_effect=fake_get):
            item = session._prepare_skill("call-1", {"action": "load", "name": "disabled-skill"})
            call_id, result = session._exec_skill(item)

        assert "not found" in result.lower()
        assert session._set_skill_called == []

    def test_load_already_active_skill(self) -> None:
        skills = [{"name": "active", "content": "x", "description": "", "scan_status": "safe"}]
        session, _, fake_get = _make_session(skills)
        session._skill_name = "active"

        with patch("turnstone.core.session.get_skill_by_name", side_effect=fake_get):
            item = session._prepare_skill("call-1", {"action": "load", "name": "active"})
            call_id, result = session._exec_skill(item)

        assert "already active" in result.lower()
        assert session._set_skill_called == []

    def test_search_filters_disabled(self) -> None:
        skills = [
            {
                "name": "enabled-skill",
                "description": "Good",
                "category": "gen",
                "scan_status": "",
                "tags": "[]",
                "activation": "named",
                "enabled": True,
            },
            {
                "name": "disabled-skill",
                "description": "Bad",
                "category": "gen",
                "scan_status": "",
                "tags": "[]",
                "activation": "named",
                "enabled": False,
            },
        ]
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = skills

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        assert "enabled-skill" in result
        assert "disabled-skill" not in result

    def test_search_multi_word_query(self) -> None:
        skills = [
            {
                "name": "code-review",
                "description": "Reviews code for quality",
                "category": "eng",
                "scan_status": "",
                "tags": "[]",
                "activation": "named",
            },
        ]
        mock_storage = MagicMock()
        mock_storage.list_prompt_templates.return_value = skills

        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "search", "query": "code review"})

        with patch("turnstone.core.storage._registry.get_storage", return_value=mock_storage):
            call_id, result = session._exec_skill(item)

        assert "code-review" in result

    def test_preparer_load_has_approval_label(self) -> None:
        session, _, _ = _make_session()
        item = session._prepare_skill("call-1", {"action": "load", "name": "my-skill"})
        assert item["approval_label"] == "skill__my-skill"


# ---------------------------------------------------------------------------
# Tests: Skill Catalog Disclosure (Agent Skills standard compliance)
# ---------------------------------------------------------------------------


class TestSkillCatalogDisclosure:
    """Verify <available-skills> catalog appears in system messages."""

    def _build_session_with_system_messages(
        self,
        search_skills: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Build a session and call _init_system_messages to get dev_parts."""
        from turnstone.core.session import ChatSession

        session = ChatSession.__new__(ChatSession)
        ui = MagicMock()
        session.ui = ui
        session.model = "test-model"
        session._ws_id = "ws-test"
        session._node_id = "node-1"
        session._skill_name = None
        session._skill_content = None
        session._skill_resources = {}
        session._applied_skill_content = None
        session.context_window = 128000
        session.messages = []
        session._config = {}
        session.creative_mode = False
        session.instructions = ""
        session.system_messages = []
        session._agent_system_messages = []
        session.reasoning_effort = "medium"
        session._pending_nudge = []
        session._tool_search = None
        session._mcp_client = None
        session._notify_on_complete = "{}"
        session._tool_error_flags = {}
        from turnstone.prompts import ClientType

        session._tools = []
        session._client_type = ClientType.CLI
        session._username = ""
        session._kind = "interactive"

        # Memory stubs
        session._memory_config = MagicMock()
        session._memory_config.fetch_limit = 0
        session._user_id = "test-user"

        with (
            patch(
                "turnstone.core.session.list_skills_by_activation",
                return_value=search_skills or [],
            ),
            patch.object(session, "_list_visible_memories", return_value=[]),
        ):
            session._init_system_messages()

        return session

    def test_catalog_present_with_search_skills(self) -> None:
        skills = [
            {"name": "pdf-processing", "description": "Extract PDF text and forms."},
            {"name": "data-analysis", "description": "Analyze datasets."},
        ]
        session = self._build_session_with_system_messages(search_skills=skills)
        content = session.system_messages[0]["content"]
        assert "<available-skills>" in content
        assert "pdf-processing" in content
        assert "data-analysis" in content
        assert "</available-skills>" in content

    def test_catalog_omitted_when_no_search_skills(self) -> None:
        session = self._build_session_with_system_messages(search_skills=[])
        content = session.system_messages[0]["content"]
        assert "<available-skills>" not in content

    def test_catalog_capped_at_30(self) -> None:
        skills = [{"name": f"skill-{i:03d}", "description": f"Desc {i}"} for i in range(50)]
        session = self._build_session_with_system_messages(search_skills=skills)
        content = session.system_messages[0]["content"]
        # Should include first 30, not all 50
        assert "skill-029" in content
        assert "skill-030" not in content

    def test_catalog_escapes_html(self) -> None:
        skills = [
            {"name": "xss-test", "description": "Handle <script> & 'quotes'."},
        ]
        session = self._build_session_with_system_messages(search_skills=skills)
        content = session.system_messages[0]["content"]
        assert "&lt;script&gt;" in content
        assert "<script>" not in content.replace("<available-skills>", "").replace(
            "</available-skills>", ""
        ).replace("<skill>", "").replace("</skill>", "").replace("<name>", "").replace(
            "</name>", ""
        ).replace("<description>", "").replace("</description>", "")

    def test_catalog_includes_hint(self) -> None:
        skills = [{"name": "test", "description": "Test skill."}]
        session = self._build_session_with_system_messages(search_skills=skills)
        content = session.system_messages[0]["content"]
        assert "/skill" in content
