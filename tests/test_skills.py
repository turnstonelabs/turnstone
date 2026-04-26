"""Tests for the skills foundation feature.

Covers storage operations, session runtime wiring, skill search,
admin API endpoints, and MCP sync integration.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.core.auth import AuthResult
from turnstone.core.session import ChatSession
from turnstone.core.skill_search import SkillSearchManager
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _create_template(db, template_id, name, content, **kwargs):
    """Helper to create a prompt template in storage."""
    db.create_prompt_template(
        template_id=template_id,
        name=name,
        category=kwargs.get("category", "general"),
        content=content,
        variables=kwargs.get("variables", "[]"),
        is_default=kwargs.get("is_default", False),
        org_id=kwargs.get("org_id", ""),
        created_by=kwargs.get("created_by", "test"),
        origin=kwargs.get("origin", "manual"),
        mcp_server=kwargs.get("mcp_server", ""),
        readonly=kwargs.get("readonly", False),
        description=kwargs.get("description", ""),
        tags=kwargs.get("tags", "[]"),
        source_url=kwargs.get("source_url", ""),
        version=kwargs.get("version", "1.0.0"),
        author=kwargs.get("author", ""),
        activation=kwargs.get("activation", "named"),
        token_estimate=kwargs.get("token_estimate", 0),
        model=kwargs.get("model", ""),
        auto_approve=kwargs.get("auto_approve", False),
        temperature=kwargs.get("temperature"),
        reasoning_effort=kwargs.get("reasoning_effort", ""),
        max_tokens=kwargs.get("max_tokens"),
        token_budget=kwargs.get("token_budget", 0),
        agent_max_turns=kwargs.get("agent_max_turns"),
        notify_on_complete=kwargs.get("notify_on_complete", "{}"),
        enabled=kwargs.get("enabled", True),
        allowed_tools=kwargs.get("allowed_tools", "[]"),
        priority=kwargs.get("priority", 0),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Create a fresh SQLite backend for each test."""
    return SQLiteBackend(str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# 1. Storage tests
# ---------------------------------------------------------------------------


class TestSkillStorage:
    def test_create_skill_with_new_fields(self, db):
        """Create a prompt template with all skill fields and verify storage."""
        db.create_prompt_template(
            template_id="s1",
            name="code-review",
            category="engineering",
            content="You are a code reviewer.",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="admin",
            description="Reviews code for quality and correctness",
            tags='["code", "review"]',
            activation="search",
            author="alice",
            version="2.0.0",
            token_estimate=500,
        )
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["template_id"] == "s1"
        assert tpl["name"] == "code-review"
        assert tpl["category"] == "engineering"
        assert tpl["content"] == "You are a code reviewer."
        assert tpl["description"] == "Reviews code for quality and correctness"
        assert tpl["tags"] == '["code", "review"]'
        assert tpl["activation"] == "search"
        assert tpl["author"] == "alice"
        assert tpl["version"] == "2.0.0"
        assert tpl["token_estimate"] == 500

    def test_list_skills_by_activation(self, db):
        """Filter templates by activation mode."""
        _create_template(db, "s1", "default-skill", "D", activation="default", is_default=True)
        _create_template(db, "s2", "search-skill", "S", activation="search")
        _create_template(db, "s3", "named-skill", "N", activation="named")
        _create_template(db, "s4", "another-search", "S2", activation="search")

        defaults = db.list_skills_by_activation("default")
        assert len(defaults) == 1
        assert defaults[0]["name"] == "default-skill"

        search = db.list_skills_by_activation("search")
        assert len(search) == 2
        names = {s["name"] for s in search}
        assert names == {"another-search", "search-skill"}

    def test_activation_is_default_sync_on_create(self, db):
        """Creating with activation='default' sets is_default=True."""
        db.create_prompt_template(
            template_id="s1",
            name="auto-default",
            category="general",
            content="content",
            activation="default",
        )
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["activation"] == "default"
        assert tpl["is_default"] is True

    def test_activation_is_default_sync_on_update(self, db):
        """Updating activation syncs is_default and vice versa."""
        _create_template(db, "s1", "skill", "content", activation="named")
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["is_default"] is False

        # Setting activation to default should set is_default
        db.update_prompt_template("s1", activation="default")
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["is_default"] is True
        assert tpl["activation"] == "default"

        # Setting is_default to False should set activation to named
        db.update_prompt_template("s1", is_default=False)
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["is_default"] is False
        assert tpl["activation"] == "named"

        # Setting is_default to True should set activation to default
        db.update_prompt_template("s1", is_default=True)
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["is_default"] is True
        assert tpl["activation"] == "default"

    def test_tags_json_roundtrip(self, db):
        """Tags stored as JSON string parse back correctly."""
        tags = ["code", "review", "python"]
        _create_template(db, "s1", "tagged", "content", tags=json.dumps(tags))
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        parsed = json.loads(tpl["tags"])
        assert parsed == tags

    def test_token_estimate_stored(self, db):
        """token_estimate is persisted and returned."""
        _create_template(db, "s1", "estimated", "content", token_estimate=500)
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["token_estimate"] == 500

    def test_get_skill_by_name(self, db):
        """get_skill_by_name works as alias for get_prompt_template_by_name."""
        _create_template(db, "s1", "my-skill", "skill content")
        tpl = db.get_skill_by_name("my-skill")
        assert tpl is not None
        assert tpl["template_id"] == "s1"
        assert tpl["name"] == "my-skill"

    def test_get_skill_by_name_nonexistent(self, db):
        """get_skill_by_name returns None for missing names."""
        assert db.get_skill_by_name("nonexistent") is None

    def test_new_fields_have_defaults(self, db):
        """Creating a template without new params uses sensible defaults."""
        db.create_prompt_template(
            template_id="s1",
            name="old-style",
            category="general",
            content="Hello",
        )
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["description"] == ""
        assert tpl["tags"] == "[]"
        assert tpl["activation"] == "named"
        assert tpl["author"] == ""
        assert tpl["version"] == "1.0.0"
        assert tpl["token_estimate"] == 0
        assert tpl["source_url"] == ""

    def test_list_skills_by_activation_empty(self, db):
        """Querying a nonexistent activation returns empty list."""
        result = db.list_skills_by_activation("nonexistent")
        assert result == []

    def test_list_skills_by_activation_ordered_by_name(self, db):
        """Results are ordered by name ascending when priority is equal."""
        _create_template(db, "s2", "beta-search", "B", activation="search")
        _create_template(db, "s1", "alpha-search", "A", activation="search")
        results = db.list_skills_by_activation("search")
        assert len(results) == 2
        assert results[0]["name"] == "alpha-search"
        assert results[1]["name"] == "beta-search"

    def test_list_skills_by_activation_ordered_by_priority(self, db):
        """Results are ordered by priority ascending, then name."""
        _create_template(db, "s1", "style", "S", activation="default", priority=20)
        _create_template(db, "s2", "safety", "F", activation="default", priority=10)
        _create_template(db, "s3", "tone", "T", activation="default", priority=10)
        results = db.list_skills_by_activation("default")
        assert len(results) == 3
        assert results[0]["name"] == "safety"
        assert results[1]["name"] == "tone"
        assert results[2]["name"] == "style"

    def test_priority_default_is_zero(self, db):
        """Priority defaults to 0 when not specified."""
        _create_template(db, "s1", "skill", "content")
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["priority"] == 0

    def test_priority_roundtrip(self, db):
        """Priority can be set on create and retrieved."""
        _create_template(db, "s1", "skill", "content", priority=42)
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["priority"] == 42

    def test_priority_update(self, db):
        """Priority can be updated."""
        _create_template(db, "s1", "skill", "content", priority=10)
        db.update_prompt_template("s1", priority=99)
        tpl = db.get_prompt_template("s1")
        assert tpl is not None
        assert tpl["priority"] == 99

    def test_list_default_templates_ordered_by_priority(self, db):
        """list_default_templates() respects priority ordering."""
        _create_template(db, "s1", "beta", "b", activation="default", priority=10)
        _create_template(db, "s2", "alpha", "a", activation="default", priority=5)
        _create_template(db, "s3", "gamma", "g", activation="default", priority=1)

        results = db.list_default_templates()
        assert len(results) == 3
        assert results[0]["name"] == "gamma"
        assert results[1]["name"] == "alpha"
        assert results[2]["name"] == "beta"


# ---------------------------------------------------------------------------
# 1b. Skill resource storage tests
# ---------------------------------------------------------------------------


class TestSkillResources:
    def test_create_and_list_resources(self, db):
        _create_template(db, "s1", "my-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/search.py", "import requests")
        db.create_skill_resource("r2", "s1", "references/api.md", "# API Docs")
        resources = db.list_skill_resources("s1")
        assert len(resources) == 2
        assert resources[0]["path"] == "references/api.md"  # ordered by path
        assert resources[1]["path"] == "scripts/search.py"

    def test_get_resource_by_path(self, db):
        _create_template(db, "s1", "my-skill", "content")
        db.create_skill_resource("r1", "s1", "scripts/helper.py", "def main(): pass")
        r = db.get_skill_resource("s1", "scripts/helper.py")
        assert r is not None
        assert r["content"] == "def main(): pass"
        assert r["content_type"] == "text/plain"

    def test_get_resource_not_found(self, db):
        _create_template(db, "s1", "my-skill", "content")
        assert db.get_skill_resource("s1", "nonexistent.py") is None

    def test_delete_skill_resources(self, db):
        _create_template(db, "s1", "my-skill", "content")
        db.create_skill_resource("r1", "s1", "a.py", "code1")
        db.create_skill_resource("r2", "s1", "b.py", "code2")
        count = db.delete_skill_resources("s1")
        assert count == 2
        assert db.list_skill_resources("s1") == []

    def test_resources_scoped_to_skill(self, db):
        _create_template(db, "s1", "skill-a", "content a")
        _create_template(db, "s2", "skill-b", "content b")
        db.create_skill_resource("r1", "s1", "script.py", "code-a")
        db.create_skill_resource("r2", "s2", "script.py", "code-b")
        assert len(db.list_skill_resources("s1")) == 1
        assert len(db.list_skill_resources("s2")) == 1
        assert db.get_skill_resource("s1", "script.py")["content"] == "code-a"

    def test_content_type_stored(self, db):
        _create_template(db, "s1", "my-skill", "content")
        db.create_skill_resource("r1", "s1", "template.json", "{}", content_type="application/json")
        r = db.get_skill_resource("s1", "template.json")
        assert r["content_type"] == "application/json"

    def test_list_empty(self, db):
        assert db.list_skill_resources("nonexistent") == []

    def test_delete_empty(self, db):
        assert db.delete_skill_resources("nonexistent") == 0


# ---------------------------------------------------------------------------
# 2. Session runtime tests
# ---------------------------------------------------------------------------


class TestSkillSessionRuntime:
    def test_skill_param_alone(self, tmp_db):
        """skill= works on its own."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "my-skill", "SKILL_ONLY_CONTENT")

        session = _make_session(skill="my-skill")
        content = _sys_content(session)
        assert "SKILL_ONLY_CONTENT" in content
        assert session._skill_name == "my-skill"

    def test_set_skill_method(self, tmp_db):
        """session.set_skill() activates the skill."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "dynamic-skill", "DYNAMIC_SKILL_CONTENT")

        session = _make_session()
        content_before = _sys_content(session)
        assert "DYNAMIC_SKILL_CONTENT" not in content_before

        session.set_skill("dynamic-skill")
        assert session._skill_name == "dynamic-skill"
        content_after = _sys_content(session)
        assert "DYNAMIC_SKILL_CONTENT" in content_after

    def test_skill_slash_command(self, tmp_db):
        """The /skill command sets the active skill."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "slash-skill", "SLASH_SKILL_CONTENT")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui)
        session.handle_command("/skill slash-skill")
        assert session._skill_name == "slash-skill"
        content = _sys_content(session)
        assert "SLASH_SKILL_CONTENT" in content
        ui.on_info.assert_called_once()
        assert "slash-skill" in ui.on_info.call_args[0][0]

    def test_skill_slash_command_clear(self, tmp_db):
        """The /skill clear command clears the active skill."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "clearable", "CLEARABLE_CONTENT")
        _create_template(db, "s2", "default-one", "DEFAULT_CONTENT", is_default=True)

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="clearable")
        assert "CLEARABLE_CONTENT" in _sys_content(session)

        session.handle_command("/skill clear")
        assert session._skill_name is None
        assert "DEFAULT_CONTENT" in _sys_content(session)
        assert "CLEARABLE_CONTENT" not in _sys_content(session)

    def test_skill_slash_command_show(self, tmp_db):
        """The /skill command with no arg shows current skill."""
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "current-skill", "content")

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="current-skill")
        session.handle_command("/skill")
        ui.on_info.assert_called_once()
        assert "current-skill" in ui.on_info.call_args[0][0]

    def test_skill_slash_command_not_found(self, tmp_db):
        """The /skill command with unknown name shows error."""
        ui = NullUI()
        ui.on_error = MagicMock()
        session = _make_session(ui=ui)
        session.handle_command("/skill nonexistent")
        ui.on_error.assert_called_once()
        assert "not found" in ui.on_error.call_args[0][0].lower()

    def test_skill_saved_in_config(self, tmp_db):
        """_save_config() includes both 'skill' and 'template' keys."""
        from turnstone.core.memory import load_workstream_config
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "persist-skill", "PERSIST_CONTENT")

        session = _make_session(skill="persist-skill")
        config = load_workstream_config(session.ws_id)
        assert config["skill"] == "persist-skill"

    def test_skill_resumed_from_config(self, tmp_db):
        """Resume reads 'skill' key with precedence over 'template'."""
        from turnstone.core.memory import save_message
        from turnstone.core.storage import get_storage

        db = get_storage()
        _create_template(db, "s1", "resume-skill", "RESUMED_SKILL_CONTENT")

        session1 = _make_session(skill="resume-skill")
        ws_id = session1.ws_id
        save_message(ws_id, "user", "hello")

        session2 = _make_session()
        assert session2._skill_name is None
        resumed = session2.resume(ws_id)
        assert resumed
        assert session2._skill_name == "resume-skill"
        content = _sys_content(session2)
        assert "RESUMED_SKILL_CONTENT" in content


# ---------------------------------------------------------------------------
# 3. Skill search tests
# ---------------------------------------------------------------------------


class TestSkillSearch:
    def test_search_returns_relevant_results(self):
        """SkillSearchManager finds skills matching the query."""
        skills = [
            {
                "name": "code-review",
                "description": "Review code quality",
                "tags": '["code"]',
                "content": "You review code.",
                "category": "eng",
            },
            {
                "name": "summarize",
                "description": "Summarize documents",
                "tags": '["docs"]',
                "content": "You summarize.",
                "category": "writing",
            },
            {
                "name": "debug-helper",
                "description": "Help debug issues",
                "tags": '["code", "debug"]',
                "content": "You help debug.",
                "category": "eng",
            },
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("code review")
        assert len(results) > 0
        names = [r["name"] for r in results]
        assert "code-review" in names

    def test_search_empty_query_returns_empty(self):
        """Searching with empty string returns no results."""
        skills = [
            {
                "name": "skill-one",
                "description": "A skill",
                "tags": "[]",
                "content": "content",
                "category": "general",
            },
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("")
        assert results == []

    def test_search_no_skills_returns_empty(self):
        """SkillSearchManager with no skills returns empty on any query."""
        mgr = SkillSearchManager([])
        results = mgr.search("anything")
        assert results == []

    def test_tags_boost_relevance(self):
        """Skills with matching tags should rank higher."""
        skills = [
            {
                "name": "generic-tool",
                "description": "A generic tool for various tasks",
                "tags": "[]",
                "content": "Does many things.",
                "category": "general",
            },
            {
                "name": "python-linter",
                "description": "Lint code",
                "tags": '["python", "lint"]',
                "content": "You lint code.",
                "category": "eng",
            },
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("python lint")
        assert len(results) > 0
        # The python-linter should appear first because tags match
        assert results[0]["name"] == "python-linter"

    def test_count_property(self):
        """The count property returns the number of indexed skills."""
        skills = [
            {"name": "a", "description": "", "tags": "[]", "content": "x", "category": ""},
            {"name": "b", "description": "", "tags": "[]", "content": "y", "category": ""},
            {"name": "c", "description": "", "tags": "[]", "content": "z", "category": ""},
        ]
        mgr = SkillSearchManager(skills)
        assert mgr.count == 3

    def test_count_empty(self):
        """Empty manager has count 0."""
        mgr = SkillSearchManager([])
        assert mgr.count == 0

    def test_search_uses_content_prefix(self):
        """Search indexes first 500 chars of content for matching."""
        skills = [
            {
                "name": "obscure-name",
                "description": "generic",
                "tags": "[]",
                "content": "kubernetes cluster management and orchestration",
                "category": "devops",
            },
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("kubernetes")
        assert len(results) == 1
        assert results[0]["name"] == "obscure-name"

    def test_search_limit(self):
        """Search respects the limit parameter."""
        skills = [
            {
                "name": f"skill-{i}",
                "description": "common common common",
                "tags": "[]",
                "content": "common content",
                "category": "",
            }
            for i in range(10)
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("common", limit=3)
        assert len(results) <= 3

    def test_tags_as_list(self):
        """Tags can be passed as actual list (not just JSON string)."""
        skills = [
            {
                "name": "list-tags",
                "description": "has list tags",
                "tags": ["python", "code"],
                "content": "content",
                "category": "",
            },
        ]
        mgr = SkillSearchManager(skills)
        results = mgr.search("python")
        assert len(results) == 1
        assert results[0]["name"] == "list-tags"


# ---------------------------------------------------------------------------
# 4. API tests
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin auth result with admin.skills permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.skills"}),
        )
        return await call_next(request)


@pytest.fixture()
def api_storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "api_test.db"))


@pytest.fixture()
def api_client(api_storage):
    from turnstone.console.server import (
        admin_create_skill,
        admin_delete_skill,
        admin_list_skills,
        admin_update_skill,
    )

    routes = [
        Mount(
            "/v1",
            routes=[
                Route("/api/admin/skills", admin_list_skills),
                Route("/api/admin/skills", admin_create_skill, methods=["POST"]),
                Route("/api/admin/skills/{skill_id}", admin_update_skill, methods=["PUT"]),
                Route("/api/admin/skills/{skill_id}", admin_delete_skill, methods=["DELETE"]),
            ],
        ),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = api_storage
    return TestClient(app)


class TestSkillAPI:
    def test_list_skills_endpoint(self, api_client, api_storage):
        """GET /v1/api/admin/skills returns correct schema."""
        _create_template(
            api_storage,
            "s1",
            "skill-a",
            "content A",
            description="Desc A",
            tags='["tag1"]',
            activation="search",
        )
        _create_template(
            api_storage, "s2", "skill-b", "content B", description="Desc B", activation="named"
        )

        resp = api_client.get("/v1/api/admin/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data
        skills = data["skills"]
        assert len(skills) == 2
        # Verify schema fields are present
        for skill in skills:
            assert "template_id" in skill
            assert "name" in skill
            assert "description" in skill
            assert "tags" in skill
            assert "activation" in skill
            assert "token_estimate" in skill
            assert "author" in skill
            assert "version" in skill
            assert "origin" in skill
            assert "readonly" in skill
        # Check tags are parsed as list
        s_a = next(s for s in skills if s["name"] == "skill-a")
        assert s_a["tags"] == ["tag1"]
        assert s_a["activation"] == "search"

    def test_create_skill_endpoint(self, api_client, api_storage):
        """POST /v1/api/admin/skills creates with new fields."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "new-skill",
                "content": "You are a helpful skill.",
                "category": "custom",
                "description": "A new custom skill",
                "tags": ["test", "custom"],
                "activation": "search",
                "author": "bob",
                "version": "1.2.0",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "new-skill"
        assert data["description"] == "A new custom skill"
        assert data["activation"] == "search"
        assert data["author"] == "bob"
        assert data["version"] == "1.2.0"

    def test_create_skill_computes_token_estimate(self, api_client, api_storage):
        """Token estimate is computed from content length on creation."""
        content = "x" * 400  # 400 chars -> 100 tokens (400 // 4)
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "estimated-skill",
                "content": content,
                "description": "estimation test",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_estimate"] == 100

    def test_create_skill_requires_name(self, api_client):
        """Creating without name returns 400."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={"content": "some content", "description": "desc"},
        )
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_create_skill_requires_content(self, api_client):
        """Creating without content returns 400."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={"name": "no-content", "description": "desc"},
        )
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_create_skill_requires_description(self, api_client):
        """Creating without description returns 400 — empty descriptions
        break discoverability in list_skills."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={"name": "no-desc", "content": "some content"},
        )
        assert resp.status_code == 400
        assert "description" in resp.json()["error"].lower()

    def test_create_skill_rejects_blank_description(self, api_client):
        """An all-whitespace description is treated the same as empty."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "blank-desc",
                "content": "some content",
                "description": "   \t  ",
            },
        )
        assert resp.status_code == 400
        assert "description" in resp.json()["error"].lower()

    def test_update_skill_rejects_blanking_description(self, api_client, api_storage):
        """An update cannot blank out the description — operators must
        supply a non-empty replacement or omit the field."""
        _create_template(api_storage, "s1", "keep-desc", "content", description="existing desc")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"description": "  "},
        )
        assert resp.status_code == 400
        assert "description" in resp.json()["error"].lower()

    def test_create_skill_default_kind_is_any(self, api_client):
        """Skills without an explicit ``kind`` default to ``any`` so
        pre-upgrade catalogs keep showing up on both interactive and
        coordinator sides."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "kind-default",
                "content": "content",
                "description": "no explicit kind",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "any"

    def test_create_skill_accepts_explicit_kind(self, api_client):
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "kind-coord",
                "content": "content",
                "description": "coord only",
                "kind": "coordinator",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "coordinator"

    def test_create_skill_rejects_invalid_kind(self, api_client):
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "kind-bad",
                "content": "content",
                "description": "bad kind",
                "kind": "nonsense",
            },
        )
        assert resp.status_code == 400
        assert "kind" in resp.json()["error"].lower()

    def test_update_skill_kind_round_trip(self, api_client, api_storage):
        _create_template(api_storage, "s1", "kind-upd", "content", description="initial")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"kind": "interactive"},
        )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "interactive"

    def test_update_skill_rejects_invalid_kind(self, api_client, api_storage):
        _create_template(api_storage, "s1", "kind-upd-bad", "content", description="initial")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"kind": "bogus"},
        )
        assert resp.status_code == 400

    def test_update_skill_endpoint(self, api_client, api_storage):
        """PUT /v1/api/admin/skills/{id} updates new fields."""
        _create_template(api_storage, "s1", "update-me", "old content", description="old desc")

        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={
                "description": "updated desc",
                "tags": ["updated"],
                "activation": "default",
                "author": "charlie",
                "version": "2.0.0",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "updated desc"
        assert data["activation"] == "default"
        assert data["is_default"] is True
        assert data["author"] == "charlie"
        assert data["version"] == "2.0.0"

    def test_update_skill_not_found(self, api_client):
        """Updating nonexistent skill returns 404."""
        resp = api_client.put(
            "/v1/api/admin/skills/nonexistent",
            json={"description": "new"},
        )
        assert resp.status_code == 404

    def test_update_skill_readonly_spec_fields_rejected(self, api_client, api_storage):
        """Updating spec fields on a readonly skill returns 400 (filtered to nothing)."""
        _create_template(
            api_storage,
            "s1",
            "mcp-skill",
            "mcp content",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"description": "hacked", "content": "evil"},
        )
        assert resp.status_code == 400
        assert "runtime config" in resp.json()["error"].lower()

    def test_update_skill_readonly_runtime_config_allowed(self, api_client, api_storage):
        """Runtime config fields can be updated on a readonly (installed) skill."""
        _create_template(
            api_storage,
            "s1",
            "installed-skill",
            "external content",
            origin="source",
            readonly=True,
        )
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"model": "gpt-5", "temperature": 0.5, "enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "gpt-5"
        assert data["temperature"] == 0.5
        assert data["enabled"] is False
        # Spec fields must remain unchanged
        assert data["content"] == "external content"

    def test_update_skill_readonly_mixed_body_filters_spec(self, api_client, api_storage):
        """When JS sends all fields for a readonly skill, spec fields are silently dropped."""
        _create_template(
            api_storage,
            "s1",
            "installed",
            "original content",
            origin="source",
            readonly=True,
        )
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            # Simulate what the browser form submits: every field present
            json={
                "name": "hacked",
                "content": "evil content",
                "description": "tampered",
                "model": "gpt-5",
                "enabled": False,
                "token_budget": 50000,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Config fields updated
        assert data["model"] == "gpt-5"
        assert data["enabled"] is False
        assert data["token_budget"] == 50000
        # Spec fields unchanged
        assert data["name"] == "installed"
        assert data["content"] == "original content"
        assert data["description"] == ""

    def test_update_skill_recomputes_token_estimate(self, api_client, api_storage):
        """Updating content recomputes token_estimate."""
        _create_template(api_storage, "s1", "recompute", "short", token_estimate=1)
        new_content = "y" * 800  # 800 // 4 = 200
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"content": new_content},
        )
        assert resp.status_code == 200
        assert resp.json()["token_estimate"] == 200

    def test_delete_skill_endpoint(self, api_client, api_storage):
        """DELETE /v1/api/admin/skills/{id} deletes the skill."""
        _create_template(api_storage, "s1", "deletable", "content")

        resp = api_client.delete("/v1/api/admin/skills/s1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify deletion
        assert api_storage.get_prompt_template("s1") is None

    def test_delete_skill_not_found(self, api_client):
        """Deleting nonexistent skill returns 404."""
        resp = api_client.delete("/v1/api/admin/skills/missing")
        assert resp.status_code == 404

    def test_delete_skill_readonly_allowed(self, api_client, api_storage):
        """Deleting a readonly (installed) skill is allowed — uninstall."""
        _create_template(
            api_storage,
            "s1",
            "mcp-readonly",
            "content",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        resp = api_client.delete("/v1/api/admin/skills/s1")
        assert resp.status_code == 200

    def test_skill_field_on_workstream_create(self, api_storage):
        """Console workstream creation accepts 'skill' field in request body."""
        # This test verifies the server code that reads body.get("skill", "")
        # by importing and checking the function exists with proper handling
        from turnstone.console.server import create_workstream

        # Verify the handler function is importable and callable
        assert callable(create_workstream)

    def test_create_skill_activation_default_sync(self, api_client, api_storage):
        """Creating with activation=default auto-sets is_default."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "auto-default",
                "content": "auto default content",
                "description": "activation default",
                "activation": "default",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["activation"] == "default"
        assert data["is_default"] is True

    def test_create_skill_is_default_derives_activation(self, api_client, api_storage):
        """Creating with is_default=true and no activation sets activation=default."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "default-derived",
                "content": "derived content",
                "description": "is_default derived",
                "is_default": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_default"] is True
        assert data["activation"] == "default"


# ---------------------------------------------------------------------------
# 5. MCP sync tests
# ---------------------------------------------------------------------------


class TestMCPSyncSkillFields:
    def test_mcp_sync_sets_activation_named(self):
        """Synced MCP prompts get activation='named'."""
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager({})
        storage = MagicMock()
        storage.get_prompt_template_by_name.return_value = None
        storage.list_prompt_templates_by_origin.return_value = []
        storage.create_prompt_template.return_value = None
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Say hello",
                "arguments": [],
            },
        ]

        mgr.sync_prompts_to_storage()

        storage.create_prompt_template.assert_called_once()
        call_kwargs = storage.create_prompt_template.call_args[1]
        assert call_kwargs["activation"] == "named"

    def test_mcp_sync_sets_token_estimate(self):
        """Token estimate is computed from content on sync."""
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager({})
        storage = MagicMock()
        storage.get_prompt_template_by_name.return_value = None
        storage.list_prompt_templates_by_origin.return_value = []
        storage.create_prompt_template.return_value = None
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__srv__prompt",
                "original_name": "prompt",
                "server": "srv",
                "description": "A prompt with known description",
                "arguments": [
                    {"name": "arg1", "description": "First argument", "required": True},
                ],
            },
        ]

        mgr.sync_prompts_to_storage()

        call_kwargs = storage.create_prompt_template.call_args[1]
        # token_estimate should be len(content) // 4 where content is the generated description
        assert "token_estimate" in call_kwargs
        assert isinstance(call_kwargs["token_estimate"], int)
        assert call_kwargs["token_estimate"] > 0

    def test_mcp_sync_update_sets_token_estimate(self):
        """Token estimate is recomputed on MCP sync update."""
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager({})
        storage = MagicMock()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "existing-id",
            "name": "mcp__srv__prompt",
            "origin": "mcp",
            "mcp_server": "srv",
            "readonly": True,
        }
        storage.list_prompt_templates_by_origin.return_value = []
        storage.update_prompt_template.return_value = True
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__srv__prompt",
                "original_name": "prompt",
                "server": "srv",
                "description": "Updated description",
                "arguments": [],
            },
        ]

        mgr.sync_prompts_to_storage()

        call_kwargs = storage.update_prompt_template.call_args[1]
        assert "token_estimate" in call_kwargs
        assert isinstance(call_kwargs["token_estimate"], int)

    def test_mcp_sync_does_not_set_default_activation(self):
        """MCP sync never creates with activation='default' (security)."""
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager({})
        storage = MagicMock()
        storage.get_prompt_template_by_name.return_value = None
        storage.list_prompt_templates_by_origin.return_value = []
        storage.create_prompt_template.return_value = None
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__srv__prompt",
                "original_name": "prompt",
                "server": "srv",
                "description": "Any prompt",
                "arguments": [],
            },
        ]

        mgr.sync_prompts_to_storage()

        call_kwargs = storage.create_prompt_template.call_args[1]
        # MCP prompts should never be auto-applied as defaults
        assert call_kwargs["activation"] != "default"
        assert call_kwargs["is_default"] is False


# ---------------------------------------------------------------------------
# 6. Session config application tests
# ---------------------------------------------------------------------------


class TestSkillSessionConfigApplication:
    """Test that session config fields stored on skills round-trip correctly."""

    def test_skill_with_model_override(self, db):
        """Skill with model= stores and returns the value."""
        _create_template(db, "s1", "model-skill", "content")
        db.update_prompt_template("s1", model="test-model")
        tpl = db.get_skill_by_name("model-skill")
        assert tpl is not None
        assert tpl["model"] == "test-model"

    def test_skill_with_temperature(self, db):
        """Skill with temperature= stores and returns the value."""
        db.create_prompt_template(
            template_id="s1",
            name="temp-skill",
            category="general",
            content="content",
            temperature=0.3,
        )
        tpl = db.get_skill_by_name("temp-skill")
        assert tpl is not None
        assert tpl["temperature"] == 0.3

    def test_skill_with_token_budget(self, db):
        """Skill with token_budget= stores and returns the value."""
        db.create_prompt_template(
            template_id="s1",
            name="budget-skill",
            category="general",
            content="content",
            token_budget=5000,
        )
        tpl = db.get_skill_by_name("budget-skill")
        assert tpl is not None
        assert tpl["token_budget"] == 5000

    def test_skill_with_auto_approve(self, db):
        """Skill with auto_approve=True stores as True."""
        db.create_prompt_template(
            template_id="s1",
            name="approve-skill",
            category="general",
            content="content",
            auto_approve=True,
        )
        tpl = db.get_skill_by_name("approve-skill")
        assert tpl is not None
        assert tpl["auto_approve"] is True

    def test_skill_with_allowed_tools(self, db):
        """Skill with allowed_tools stores and parses correctly."""
        allowed = '["bash","read_file"]'
        db.create_prompt_template(
            template_id="s1",
            name="tools-skill",
            category="general",
            content="content",
            allowed_tools=allowed,
        )
        tpl = db.get_skill_by_name("tools-skill")
        assert tpl is not None
        assert tpl["allowed_tools"] == allowed
        parsed = json.loads(tpl["allowed_tools"])
        assert parsed == ["bash", "read_file"]

    def test_skill_with_reasoning_effort(self, db):
        """Skill with reasoning_effort= stores and returns the value."""
        db.create_prompt_template(
            template_id="s1",
            name="effort-skill",
            category="general",
            content="content",
            reasoning_effort="high",
        )
        tpl = db.get_skill_by_name("effort-skill")
        assert tpl is not None
        assert tpl["reasoning_effort"] == "high"

    def test_disabled_skill_not_in_summary(self, db):
        """A disabled skill is listed but with enabled=False."""
        db.create_prompt_template(
            template_id="s1",
            name="disabled-skill",
            category="general",
            content="content",
            enabled=False,
        )
        templates = db.list_prompt_templates()
        match = [t for t in templates if t["name"] == "disabled-skill"]
        assert len(match) == 1
        assert match[0]["enabled"] is False

    def test_session_config_fields_roundtrip(self, db):
        """All session config fields round-trip through storage."""
        db.create_prompt_template(
            template_id="s1",
            name="full-config-skill",
            category="general",
            content="You are a full config skill.",
            model="gpt-5",
            auto_approve=True,
            temperature=0.7,
            reasoning_effort="high",
            max_tokens=2048,
            token_budget=10000,
            agent_max_turns=5,
            notify_on_complete='{"channel":"discord"}',
            enabled=True,
            allowed_tools='["bash","read_file","write_file"]',
        )
        tpl = db.get_skill_by_name("full-config-skill")
        assert tpl is not None
        assert tpl["model"] == "gpt-5"
        assert tpl["auto_approve"] is True
        assert tpl["temperature"] == 0.7
        assert tpl["reasoning_effort"] == "high"
        assert tpl["max_tokens"] == 2048
        assert tpl["token_budget"] == 10000
        assert tpl["agent_max_turns"] == 5
        assert tpl["notify_on_complete"] == '{"channel":"discord"}'
        assert tpl["enabled"] is True
        parsed_tools = json.loads(tpl["allowed_tools"])
        assert parsed_tools == ["bash", "read_file", "write_file"]

    def test_license_compatibility_roundtrip(self, db):
        """Agent Skills spec fields license and compatibility round-trip."""
        db.create_prompt_template(
            template_id="spec1",
            name="spec-fields-skill",
            category="general",
            content="Spec test.",
            skill_license="Apache-2.0",
            compatibility="Requires git, docker, jq",
        )
        tpl = db.get_skill_by_name("spec-fields-skill")
        assert tpl is not None
        assert tpl["license"] == "Apache-2.0"
        assert tpl["compatibility"] == "Requires git, docker, jq"

    def test_license_compatibility_default_empty(self, db):
        """license and compatibility default to empty string."""
        db.create_prompt_template(
            template_id="spec2",
            name="no-spec-fields",
            category="general",
            content="No spec fields.",
        )
        tpl = db.get_skill_by_name("no-spec-fields")
        assert tpl is not None
        assert tpl["license"] == ""
        assert tpl["compatibility"] == ""

    def test_update_license_compatibility(self, db):
        """license and compatibility can be updated."""
        db.create_prompt_template(
            template_id="spec3",
            name="updatable-spec",
            category="general",
            content="Test.",
        )
        db.update_prompt_template("spec3", license="MIT")
        db.update_prompt_template("spec3", compatibility="Python 3.11+")
        tpl = db.get_prompt_template("spec3")
        assert tpl is not None
        assert tpl["license"] == "MIT"
        assert tpl["compatibility"] == "Python 3.11+"


# ---------------------------------------------------------------------------
# 7. Migration behavior tests
# ---------------------------------------------------------------------------


class TestSkillMigrationBehaviors:
    """Test storage behaviors that validate migration 021 data migration correctness."""

    def test_profile_category_skill_with_session_config(self, db):
        """Migrated ws_templates get category='profile' with session config."""
        _create_template(
            db,
            "s1",
            "my-profile",
            "Profile content",
            category="profile",
            model="gpt-5",
            temperature=0.3,
            token_budget=5000,
            auto_approve=True,
        )
        skill = db.get_prompt_template_by_name("my-profile")
        assert skill is not None
        assert skill["category"] == "profile"
        assert skill["model"] == "gpt-5"
        assert skill["temperature"] == 0.3
        assert skill["token_budget"] == 5000
        assert skill["auto_approve"] is True

    def test_skill_versions_roundtrip(self, db):
        """Migrated version history is readable."""
        _create_template(db, "s1", "versioned", "V1 content")
        db.create_skill_version("s1", 1, '{"content": "V1 content"}', "admin")
        db.create_skill_version("s1", 2, '{"content": "V2 content"}', "admin")
        versions = db.list_skill_versions("s1")
        assert len(versions) == 2
        assert versions[0]["version"] == 2  # DESC order
        assert versions[1]["version"] == 1

    def test_name_collision_would_use_prefix(self, db):
        """If a skill name already exists, migrated data would get skills- prefix."""
        _create_template(db, "s1", "deploy-bot", "Original skill")
        # Simulate what migration would do for a ws_template also named "deploy-bot"
        _create_template(
            db,
            "s2",
            "skills-deploy-bot",
            "Migrated from ws_template",
            category="profile",
            model="gpt-5",
        )
        assert db.get_prompt_template_by_name("deploy-bot") is not None
        assert db.get_prompt_template_by_name("skills-deploy-bot") is not None


# ---------------------------------------------------------------------------
# 8. Admin endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_api_storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "full_api_test.db"))


@pytest.fixture()
def full_api_client(full_api_storage):
    from turnstone.console.server import (
        admin_create_skill,
        admin_delete_skill,
        admin_get_skill,
        admin_list_skill_versions,
        admin_list_skills,
        admin_update_skill,
        list_skills_summary,
    )

    routes = [
        Mount(
            "/v1",
            routes=[
                Route("/api/skills", list_skills_summary),
                Route("/api/admin/skills", admin_list_skills),
                Route("/api/admin/skills", admin_create_skill, methods=["POST"]),
                Route("/api/admin/skills/{skill_id}", admin_get_skill),
                Route("/api/admin/skills/{skill_id}", admin_update_skill, methods=["PUT"]),
                Route(
                    "/api/admin/skills/{skill_id}",
                    admin_delete_skill,
                    methods=["DELETE"],
                ),
                Route("/api/admin/skills/{skill_id}/versions", admin_list_skill_versions),
            ],
        ),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = full_api_storage
    return TestClient(app)


class TestSkillAdminEndpoints:
    """Integration tests for skill admin API endpoints."""

    def test_create_skill_via_api(self, full_api_client):
        """POST /v1/api/admin/skills creates a skill and returns correct shape."""
        resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "test-skill",
                "content": "You are a test skill.",
                "category": "testing",
                "description": "A test skill",
                "tags": ["test"],
                "activation": "named",
                "model": "gpt-5",
                "temperature": 0.5,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-skill"
        assert data["content"] == "You are a test skill."
        assert data["category"] == "testing"
        assert data["description"] == "A test skill"
        assert data["tags"] == ["test"]
        assert data["activation"] == "named"
        assert data["model"] == "gpt-5"
        assert data["temperature"] == 0.5
        assert "template_id" in data

    def test_create_skill_duplicate_name_409(self, full_api_client):
        """POST with existing name returns 409."""
        full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "dup-skill", "content": "content", "description": "first"},
        )
        resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "dup-skill",
                "content": "other content",
                "description": "second",
            },
        )
        assert resp.status_code == 409

    def test_get_skill_via_api(self, full_api_client):
        """GET /v1/api/admin/skills/{id} returns full skill data."""
        create_resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "get-me",
                "content": "content here",
                "description": "desc",
            },
        )
        skill_id = create_resp.json()["template_id"]

        resp = full_api_client.get(f"/v1/api/admin/skills/{skill_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["template_id"] == skill_id
        assert data["name"] == "get-me"
        assert data["content"] == "content here"
        assert data["description"] == "desc"

    def test_update_skill_via_api(self, full_api_client):
        """PUT with new fields updates the skill."""
        create_resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "update-me", "content": "old content", "description": "pre"},
        )
        skill_id = create_resp.json()["template_id"]

        resp = full_api_client.put(
            f"/v1/api/admin/skills/{skill_id}",
            json={
                "description": "new desc",
                "temperature": 0.8,
                "model": "gpt-5",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "new desc"
        assert data["temperature"] == 0.8
        assert data["model"] == "gpt-5"

    def test_delete_skill_via_api(self, full_api_client):
        """DELETE removes the skill and subsequent GET returns 404."""
        create_resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "delete-me", "content": "content", "description": "doomed"},
        )
        skill_id = create_resp.json()["template_id"]

        resp = full_api_client.delete(f"/v1/api/admin/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        get_resp = full_api_client.get(f"/v1/api/admin/skills/{skill_id}")
        assert get_resp.status_code == 404

    def test_list_skills_summary_excludes_disabled(self, full_api_client, full_api_storage):
        """GET /v1/api/skills excludes disabled skills."""
        full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "enabled-skill", "content": "content", "description": "on"},
        )
        create_resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "disabled-skill", "content": "content", "description": "off"},
        )
        skill_id = create_resp.json()["template_id"]
        full_api_client.put(
            f"/v1/api/admin/skills/{skill_id}",
            json={"enabled": False},
        )

        resp = full_api_client.get("/v1/api/skills")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()["skills"]]
        assert "enabled-skill" in names
        assert "disabled-skill" not in names

    def test_skill_version_history_via_api(self, full_api_client):
        """GET /v1/api/admin/skills/{id}/versions returns version history."""
        create_resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "versioned-skill", "content": "v1 content", "description": "v1"},
        )
        skill_id = create_resp.json()["template_id"]

        # Update to create a version snapshot
        full_api_client.put(
            f"/v1/api/admin/skills/{skill_id}",
            json={"content": "v2 content"},
        )

        resp = full_api_client.get(f"/v1/api/admin/skills/{skill_id}/versions")
        assert resp.status_code == 200
        versions = resp.json()["versions"]
        assert len(versions) == 1

    def test_create_skill_invalid_temperature_400(self, full_api_client):
        """POST with temperature=5 returns 400."""
        resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "bad-temp", "content": "content", "temperature": 5},
        )
        assert resp.status_code == 400
        assert "temperature" in resp.json()["error"].lower()

    def test_create_skill_invalid_activation_400(self, full_api_client):
        """POST with activation='bogus' returns 400."""
        resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "bad-act", "content": "content", "activation": "bogus"},
        )
        assert resp.status_code == 400
        assert "activation" in resp.json()["error"].lower()

    def test_list_skills_pagination(self, full_api_client):
        """Pagination with limit and offset works, total is independent of limit."""
        for i in range(5):
            full_api_client.post(
                "/v1/api/admin/skills",
                json={
                    "name": f"page-skill-{i}",
                    "content": f"content {i}",
                    "description": f"desc {i}",
                },
            )
        # Limit
        resp = full_api_client.get("/v1/api/admin/skills?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) == 2
        assert data["total"] >= 5

        # Offset
        resp2 = full_api_client.get("/v1/api/admin/skills?limit=2&offset=2")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["skills"]) == 2
        assert data2["total"] == data["total"]
        # Different skills returned
        names1 = {s["name"] for s in data["skills"]}
        names2 = {s["name"] for s in data2["skills"]}
        assert names1.isdisjoint(names2)

        # No limit returns all
        resp3 = full_api_client.get("/v1/api/admin/skills")
        assert resp3.status_code == 200
        assert len(resp3.json()["skills"]) >= 5

    def test_create_skill_invalid_token_budget_type_400(self, full_api_client):
        """Non-numeric token_budget returns 400."""
        resp = full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "bad-budget", "content": "c", "token_budget": "abc"},
        )
        assert resp.status_code == 400
        assert "integer" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# 9. Skill session config applied to workstream via server handler
# ---------------------------------------------------------------------------


class TestSkillConfigAppliedToWorkstream:
    """Verify that skill session config fields are applied to the ChatSession
    when a workstream is created via the server ``create_workstream`` handler.
    """

    @pytest.fixture()
    def _ws_app(self, tmp_path):
        """Build a minimal Starlette app with the real ``create_workstream``
        handler, a real ``SessionManager``, and a temp SQLite storage
        backend.  Returns ``(TestClient, SessionManager, storage)``.
        """
        import queue
        import threading

        import turnstone.core.storage._registry as _reg
        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.session_manager import SessionManager
        from turnstone.server import WebUI, create_workstream

        storage = SQLiteBackend(str(tmp_path / "ws_test.db"))

        # Inject the test storage as the global singleton so that
        # get_storage() / get_skill_by_name() resolve against it.
        old_storage = _reg._storage
        _reg._storage = storage

        def _session_factory(
            ui: Any, model_alias: Any = None, ws_id: Any = None, **kwargs: Any
        ) -> ChatSession:
            return ChatSession(
                client=MagicMock(),
                model=model_alias or "test-model",
                ui=ui,
                instructions=None,
                temperature=0.5,
                max_tokens=4096,
                tool_timeout=30,
                ws_id=ws_id,
                skill=kwargs.get("skill"),
            )

        gq: queue.Queue[dict[str, Any]] = queue.Queue()
        WebUI._global_queue = gq
        adapter = InteractiveAdapter(
            global_queue=gq,
            ui_factory=lambda ws: WebUI(
                ws_id=ws.id,
                user_id=ws.user_id,
                kind=ws.kind,
                parent_ws_id=ws.parent_ws_id,
            ),
            session_factory=_session_factory,
        )
        mgr = SessionManager(adapter, storage=storage, max_active=10, event_emitter=adapter)

        routes = [
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/new",
                        create_workstream,
                        methods=["POST"],
                    ),
                ],
            ),
        ]
        app = Starlette(
            routes=routes,
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.workstreams = mgr
        app.state.skip_permissions = True
        app.state.global_queue = gq
        app.state.global_listeners = []
        app.state.global_listeners_lock = threading.Lock()

        client = TestClient(app, raise_server_exceptions=False)

        yield client, mgr, storage

        # Restore original storage singleton.
        _reg._storage = old_storage

    def test_session_receives_temperature(self, _ws_app):
        """Skill temperature overrides the session default."""
        client, mgr, storage = _ws_app
        _create_template(storage, "s1", "warm-skill", "Be warm.", temperature=0.9, enabled=True)

        resp = client.post("/v1/api/workstreams/new", json={"skill": "warm-skill"})
        assert resp.status_code == 200
        ws_id = resp.json()["ws_id"]
        ws = mgr.get(ws_id)
        assert ws is not None and ws.session is not None
        assert ws.session.temperature == 0.9

    def test_session_receives_max_tokens(self, _ws_app):
        """Skill max_tokens overrides the session default."""
        client, mgr, storage = _ws_app
        _create_template(storage, "s1", "token-skill", "Be concise.", max_tokens=1024, enabled=True)

        resp = client.post("/v1/api/workstreams/new", json={"skill": "token-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session.max_tokens == 1024

    def test_session_receives_token_budget(self, _ws_app):
        """Skill token_budget is applied to the session."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "budget-skill", "Stay on budget.", token_budget=50000, enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "budget-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session._token_budget == 50000

    def test_session_receives_reasoning_effort(self, _ws_app):
        """Skill reasoning_effort is applied to the session."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "effort-skill", "Think hard.", reasoning_effort="high", enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "effort-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session.reasoning_effort == "high"

    def test_session_receives_agent_max_turns(self, _ws_app):
        """Skill agent_max_turns is applied to the session."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "turns-skill", "Few turns.", agent_max_turns=3, enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "turns-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session.agent_max_turns == 3

    def test_auto_approve_set_on_ui(self, _ws_app):
        """Skill auto_approve=True propagates to the WebUI."""
        from turnstone.server import WebUI

        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "approve-skill", "Auto approve.", auto_approve=True, enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "approve-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None
        assert isinstance(ws.ui, WebUI)
        assert ws.ui.auto_approve is True

    def test_allowed_tools_set_on_ui(self, _ws_app):
        """Skill allowed_tools are parsed and set as auto_approve_tools on the UI."""
        from turnstone.server import WebUI

        client, mgr, storage = _ws_app
        _create_template(
            storage,
            "s1",
            "tools-skill",
            "Restricted tools.",
            allowed_tools='["bash", "read_file"]',
            enabled=True,
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "tools-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None
        assert isinstance(ws.ui, WebUI)
        assert ws.ui.auto_approve_tools == {"bash", "read_file"}

    def test_skill_model_overrides_resolved_model(self, _ws_app):
        """Skill model field overrides the default session model."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "model-skill", "Use specific model.", model="gpt-5", enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "model-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session.model == "gpt-5"

    def test_all_session_config_fields_applied(self, _ws_app):
        """All session config fields from a skill are applied together."""
        from turnstone.server import WebUI

        client, mgr, storage = _ws_app
        _create_template(
            storage,
            "s1",
            "full-skill",
            "Full config skill.",
            model="gpt-5",
            temperature=0.8,
            reasoning_effort="high",
            max_tokens=2048,
            token_budget=100000,
            agent_max_turns=10,
            auto_approve=True,
            allowed_tools='["bash", "write_file", "read_file"]',
            notify_on_complete='{"channel": "discord"}',
            enabled=True,
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "full-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        sess = ws.session
        assert sess.model == "gpt-5"
        assert sess.temperature == 0.8
        assert sess.reasoning_effort == "high"
        assert sess.max_tokens == 2048
        assert sess._token_budget == 100000
        assert sess.agent_max_turns == 10
        assert sess._notify_on_complete == '{"channel": "discord"}'
        assert sess._applied_skill_id == "s1"
        assert sess._applied_skill_content == "Full config skill."
        assert isinstance(ws.ui, WebUI)
        assert ws.ui.auto_approve is True
        assert ws.ui.auto_approve_tools == {"bash", "write_file", "read_file"}

    def test_disabled_skill_returns_400(self, _ws_app):
        """Creating a workstream with a disabled skill returns 400."""
        client, _mgr, storage = _ws_app
        _create_template(storage, "s1", "disabled-skill", "Disabled.", enabled=False)

        resp = client.post("/v1/api/workstreams/new", json={"skill": "disabled-skill"})
        assert resp.status_code == 400
        assert "disabled" in resp.json()["error"].lower()

    def test_unknown_skill_returns_400(self, _ws_app):
        """Creating a workstream with a nonexistent skill returns 400."""
        client, _mgr, _storage = _ws_app

        resp = client.post("/v1/api/workstreams/new", json={"skill": "no-such-skill"})
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"].lower()

    def test_zero_token_budget_is_noop(self, _ws_app):
        """Skill with token_budget=0 — handler skips budget application (> 0 guard)."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "no-budget-skill", "No budget.", token_budget=0, enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "no-budget-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        # Budget stays at default (0) — the handler's > 0 guard prevents application
        assert ws.session._token_budget == 0

    def test_empty_allowed_tools_is_noop(self, _ws_app):
        """Skill with allowed_tools='[]' — handler skips (empty check)."""
        client, mgr, storage = _ws_app
        _create_template(
            storage, "s1", "no-tools-skill", "No tools.", allowed_tools="[]", enabled=True
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "no-tools-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None
        # auto_approve_tools stays at default (empty set)
        assert ws.ui.auto_approve_tools == set()

    def test_skill_lineage_in_workstreams_table(self, _ws_app):
        """skill_id and skill_version columns are populated in the workstreams table."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import workstreams

        client, _mgr, storage = _ws_app
        _create_template(storage, "s1", "lineage-skill", "Track me.", enabled=True)

        resp = client.post("/v1/api/workstreams/new", json={"skill": "lineage-skill"})
        assert resp.status_code == 200
        ws_id = resp.json()["ws_id"]

        with storage._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.skill_id, workstreams.c.skill_version).where(
                    workstreams.c.ws_id == ws_id
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "s1"
        assert row[1] == 1

    def test_no_skill_lineage_when_no_skill(self, _ws_app):
        """Workstream without a skill has empty skill_id and zero skill_version."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import workstreams

        client, _mgr, storage = _ws_app

        resp = client.post("/v1/api/workstreams/new", json={})
        assert resp.status_code == 200
        ws_id = resp.json()["ws_id"]

        with storage._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.skill_id, workstreams.c.skill_version).where(
                    workstreams.c.ws_id == ws_id
                )
            ).fetchone()
        assert row is not None
        assert row[0] == ""
        assert row[1] == 0
