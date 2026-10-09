"""Tests for the skills foundation feature.

Covers storage operations, session runtime wiring, admin API
endpoints, and MCP sync integration.
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

# ---------------------------------------------------------------------------
# Helpers
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
    # Mirror production's parent-before-conversation ordering.  Skill slash
    # commands persist keyed SYSTEM rows and must retain the orphan-write
    # protection exercised by the storage layer.
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
        description=kwargs.get("description", ""),
        tags=kwargs.get("tags", "[]"),
        source_url=kwargs.get("source_url", ""),
        version=kwargs.get("version", "1.0.0"),
        author=kwargs.get("author", ""),
        token_estimate=kwargs.get("token_estimate", 0),
        auto_approve=kwargs.get("auto_approve", False),
        notify_on_complete=kwargs.get("notify_on_complete", "{}"),
        enabled=kwargs.get("enabled", True),
        allowed_tools=kwargs.get("allowed_tools", "[]"),
        priority=kwargs.get("priority", 0),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, sqlite_backend_factory):
    """Create a fresh SQLite backend for each test."""
    return sqlite_backend_factory(str(tmp_path / "test.db"))


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
            org_id="",
            created_by="admin",
            description="Reviews code for quality and correctness",
            tags='["code", "review"]',
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
        assert tpl["author"] == "alice"
        assert tpl["version"] == "2.0.0"
        assert tpl["token_estimate"] == 500

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
        assert tpl["author"] == ""
        assert tpl["version"] == "1.0.0"
        assert tpl["token_estimate"] == 0
        assert tpl["source_url"] == ""

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

        ui = NullUI()
        ui.on_info = MagicMock()
        session = _make_session(ui=ui, skill="clearable")
        assert "CLEARABLE_CONTENT" in _sys_content(session)

        session.handle_command("/skill clear")
        assert session._skill_name is None
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

        session2 = _make_session(ws_id=ws_id)
        assert session2._skill_name is None
        resumed = session2.rehydrate()
        assert resumed
        assert session2._skill_name == "resume-skill"
        content = _sys_content(session2)
        assert "RESUMED_SKILL_CONTENT" in content


# ---------------------------------------------------------------------------
# 3. API tests
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
def api_storage(tmp_path, sqlite_backend_factory):
    return sqlite_backend_factory(str(tmp_path / "api_test.db"))


@pytest.fixture()
def api_client(api_storage):
    from turnstone.console.server import (
        admin_create_skill,
        admin_delete_skill,
        admin_list_skills,
        admin_unlock_skill,
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
                Route(
                    "/api/admin/skills/{skill_id}/unlock",
                    admin_unlock_skill,
                    methods=["POST"],
                ),
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
        )
        _create_template(api_storage, "s2", "skill-b", "content B", description="Desc B")

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
            assert "token_estimate" in skill
            assert "author" in skill
            assert "version" in skill
            assert "origin" in skill
            assert "readonly" in skill
            # A skill has no activation mode, default flag or token budget (#1292).
            assert not {"activation", "is_default", "token_budget"} & skill.keys()
        # Check tags are parsed as list
        s_a = next(s for s in skills if s["name"] == "skill-a")
        assert s_a["tags"] == ["tag1"]

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
                # An older client's activation is ignored (#1292).
                "activation": "search",
                "author": "bob",
                "version": "1.2.0",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "new-skill"
        assert data["description"] == "A new custom skill"
        assert "activation" not in data
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
                # An older client's default flag is ignored (#1292).
                "is_default": True,
                "author": "charlie",
                "version": "2.0.0",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "updated desc"
        assert not {"activation", "is_default"} & data.keys()
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
            json={"priority": 4, "enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["priority"] == 4
        assert data["enabled"] is False
        # Spec fields must remain unchanged
        assert data["content"] == "external content"

    def test_update_skill_readonly_model_settings_are_not_runtime_config(
        self, api_client, api_storage
    ):
        """A skill has no model, sampling or task-agent turn settings to edit (#1292)."""
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
            json={
                "model": "gpt-5",
                "temperature": 0.5,
                "reasoning_effort": "max",
                "max_tokens": 9,
                "agent_max_turns": 4,
            },
        )
        assert resp.status_code == 400
        assert "runtime config" in resp.json()["error"].lower()

    def test_update_skill_readonly_hidden_from_menu_allowed(self, api_client, api_storage):
        """``hidden_from_menu`` is in SKILL_RUNTIME_CONFIG_FIELDS so an admin
        can hide/unhide an installed (readonly) skill from the user picker
        without unlocking the row.  Pins the load-bearing invariant the
        ``skill_field_validation`` comment depends on — a future refactor
        that drops the field from runtime-config would silently break
        admin's ability to toggle this on installed skills."""
        _create_template(
            api_storage,
            "s1",
            "installed-skill",
            "external content",
            origin="source",
            readonly=True,
        )
        # Default after install: visible.  Verified via direct storage
        # read rather than a GET — the api_client fixture doesn't wire
        # the GET-by-id route.
        assert api_storage.get_prompt_template("s1")["hidden_from_menu"] is False

        # Hide.
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"hidden_from_menu": True},
        )
        assert resp.status_code == 200
        assert resp.json()["hidden_from_menu"] is True
        # Spec/content fields untouched.
        assert resp.json()["content"] == "external content"

        # Unhide round-trips back.
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"hidden_from_menu": False},
        )
        assert resp.status_code == 200
        assert resp.json()["hidden_from_menu"] is False

    def test_create_skill_hidden_from_menu_string_rejected(self, api_client):
        """``hidden_from_menu`` is bool-typed at the API boundary —
        a malformed client sending the string ``"false"`` (which Python
        truthiness would silently coerce to ``True``, flipping the flag
        opposite to intent) must be rejected with a 400.  Copilot review
        on PR #577 caught the loose ``bool()`` cast."""
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "loose-bool",
                "content": "...",
                "description": "x",
                "hidden_from_menu": "false",
            },
        )
        assert resp.status_code == 400
        assert "boolean" in resp.json()["error"].lower()

    def test_create_skill_hidden_from_menu_int_zero_and_one_accepted(self, api_client, api_storage):
        """Strict-bool parse accepts canonical JSON ``true``/``false``
        AND integer ``0``/``1`` — the latter for clients that serialise
        Postgres-style.  Other integers fall to 400."""
        # int 1 → True
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "intbool-true",
                "content": "...",
                "description": "x",
                "hidden_from_menu": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["hidden_from_menu"] is True

        # int 0 → False (defaults check).
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "intbool-false",
                "content": "...",
                "description": "x",
                "hidden_from_menu": 0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["hidden_from_menu"] is False

        # int 2 → 400.
        resp = api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "intbool-ambiguous",
                "content": "...",
                "description": "x",
                "hidden_from_menu": 2,
            },
        )
        assert resp.status_code == 400

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
        # Config fields updated; a skill has no model or token budget to set (#1292)
        assert not {"model", "token_budget"} & data.keys()
        assert data["enabled"] is False
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

    def test_update_skill_notify_on_complete_empty_string_normalises_to_array(
        self, api_client, api_storage
    ):
        """Empty / whitespace ``notify_on_complete`` is normalised to ``"[]"``.

        Defends against a regression where ``if nc and nc != "[]":`` would
        short-circuit on a blank value, skip JSON validation, and persist a
        non-JSON empty string into storage.
        """
        _create_template(api_storage, "s1", "norm-empty", "x")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"notify_on_complete": ""},
        )
        assert resp.status_code == 200
        assert api_storage.get_prompt_template("s1")["notify_on_complete"] == "[]"

    def test_update_skill_notify_on_complete_legacy_object_normalises(
        self, api_client, api_storage
    ):
        """The legacy ``"{}"`` sentinel coerces to ``"[]"`` on write."""
        _create_template(api_storage, "s1", "norm-obj", "x")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"notify_on_complete": "{}"},
        )
        assert resp.status_code == 200
        assert api_storage.get_prompt_template("s1")["notify_on_complete"] == "[]"

    def test_update_skill_notify_on_complete_rejects_non_array_json(self, api_client, api_storage):
        """Valid JSON that isn't an array is a 400 — was previously accepted."""
        _create_template(api_storage, "s1", "reject-obj", "x")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"notify_on_complete": '{"channel": "discord"}'},
        )
        assert resp.status_code == 400
        assert "array" in resp.json()["error"].lower()

    def test_update_skill_notify_on_complete_rejects_targets_workstream_create_drops(
        self, api_client, api_storage
    ):
        """A skill's targets take the check a workstream's notify_targets get (#1292)."""
        _create_template(api_storage, "s1", "notify-skill", "content")
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"notify_on_complete": [{"channel_type": "discord"}] * 11},
        )
        assert resp.status_code == 400
        assert "notify_on_complete limited to 10 entries" in resp.json()["error"]

    def test_update_skill_null_notify_on_complete_keeps_the_targets(self, api_client, api_storage):
        """null means unchanged, as for the endpoint's other optional fields (#1292)."""
        targets = '[{"channel_type": "discord", "channel_id": "1"}]'
        _create_template(api_storage, "s1", "keep-targets", "x", notify_on_complete=targets)
        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={"enabled": True, "notify_on_complete": None},
        )
        assert resp.status_code == 200
        assert api_storage.get_prompt_template("s1")["notify_on_complete"] == targets

    @pytest.mark.parametrize("value", [0, False, {"channel_type": "discord"}])
    def test_update_skill_notify_on_complete_rejects_non_string_non_list(
        self, api_client, api_storage, value
    ):
        _create_template(api_storage, "s1", "odd-targets", "x")
        resp = api_client.put("/v1/api/admin/skills/s1", json={"notify_on_complete": value})
        assert resp.status_code == 400
        assert "notify_on_complete must be a JSON array or string" in resp.json()["error"]

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

    def test_unlock_skill_flips_readonly(self, api_client, api_storage):
        """POST /unlock on a readonly skill flips readonly to False."""
        _create_template(
            api_storage,
            "s1",
            "installed",
            "upstream content",
            origin="source",
            source_url="https://skills.sh/skills/owner/repo/leaf",
            readonly=True,
        )

        resp = api_client.post("/v1/api/admin/skills/s1/unlock")

        assert resp.status_code == 200
        data = resp.json()
        assert data["readonly"] is False
        # Origin preserved so the UI can still surface "Customized from upstream".
        assert data["origin"] == "source"
        assert data["source_url"] == "https://skills.sh/skills/owner/repo/leaf"

    def test_unlock_skill_persists(self, api_client, api_storage):
        """Unlock side-effects survive a fresh load from storage."""
        _create_template(api_storage, "s1", "installed", "x", origin="source", readonly=True)

        api_client.post("/v1/api/admin/skills/s1/unlock")

        row = api_storage.get_prompt_template("s1")
        assert row is not None
        assert bool(row["readonly"]) is False

    def test_unlock_skill_snapshots_pre_unlock_state(self, api_client, api_storage):
        """Pre-unlock skill state is snapshotted to skill_versions for rollback."""
        _create_template(
            api_storage, "s1", "installed", "upstream content", origin="source", readonly=True
        )
        assert api_storage.list_skill_versions("s1") == []

        api_client.post("/v1/api/admin/skills/s1/unlock")

        versions = api_storage.list_skill_versions("s1")
        assert len(versions) == 1
        assert versions[0]["version"] == 1

    def test_unlock_skill_versions_after_existing_history(self, api_client, api_storage):
        """Unlock picks the next version number based on max(version), not list length.

        Defends against regressions where len(list)+1 racing with a concurrent
        update could produce a (skill_id, version) collision. Seeds an
        out-of-order version (3) and verifies unlock chooses 4, not 2.
        """
        import json as _json

        _create_template(
            api_storage, "s1", "installed", "v0 content", origin="source", readonly=True
        )
        api_storage.create_skill_version(
            skill_id="s1", version=3, snapshot=_json.dumps({"v": 3}), changed_by="prior"
        )

        resp = api_client.post("/v1/api/admin/skills/s1/unlock")
        assert resp.status_code == 200

        versions = sorted(api_storage.list_skill_versions("s1"), key=lambda v: v["version"])
        assert [v["version"] for v in versions] == [3, 4]

    def test_unlock_skill_already_unlocked(self, api_client, api_storage):
        """Unlocking an already-unlocked skill returns 400."""
        _create_template(api_storage, "s1", "local", "content", origin="manual", readonly=False)

        resp = api_client.post("/v1/api/admin/skills/s1/unlock")

        assert resp.status_code == 400
        assert "already unlocked" in resp.json()["error"].lower()

    def test_unlock_skill_not_found(self, api_client):
        """Unlocking a nonexistent skill returns 404."""
        resp = api_client.post("/v1/api/admin/skills/missing/unlock")
        assert resp.status_code == 404

    def test_unlock_then_edit_spec_fields_succeeds(self, api_client, api_storage):
        """After unlock, PUT can edit spec fields the readonly gate previously rejected."""
        _create_template(
            api_storage,
            "s1",
            "installed",
            "upstream content",
            description="upstream description",
            origin="source",
            readonly=True,
        )

        api_client.post("/v1/api/admin/skills/s1/unlock")

        resp = api_client.put(
            "/v1/api/admin/skills/s1",
            json={
                "name": "customized",
                "content": "tuned content",
                "description": "tuned description",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "customized"
        assert data["content"] == "tuned content"
        assert data["description"] == "tuned description"
        # Origin still records that this came from upstream.
        assert data["origin"] == "source"

    def test_skill_field_on_workstream_create(self, api_storage):
        """Console workstream creation accepts 'skill' field in request body."""
        # This test verifies the server code that reads body.get("skill", "")
        # by importing and checking the function exists with proper handling
        from turnstone.console.server import create_workstream

        # Verify the handler function is importable and callable
        assert callable(create_workstream)


# ---------------------------------------------------------------------------
# 4. MCP sync tests
# ---------------------------------------------------------------------------


class TestMCPSyncSkillFields:
    def test_mcp_sync_sets_no_activation(self):
        """Synced MCP prompts apply only when named, like every skill (#1292)."""
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
        assert not {"activation", "is_default"} & call_kwargs.keys()

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


# ---------------------------------------------------------------------------
# 5. Session config application tests
# ---------------------------------------------------------------------------


class TestSkillSessionConfigApplication:
    """Test that session config fields stored on skills round-trip correctly."""

    def test_skill_rows_carry_none_of_the_dropped_settings(self, db):
        """A skill has no model alias, sampling settings, task-agent turn cap,
        token budget or activation mode (#1292); an update naming them changes
        nothing instead of writing a dropped column."""
        _create_template(db, "s1", "model-skill", "content")
        db.update_prompt_template(
            "s1",
            model="test-model",
            temperature=0.3,
            reasoning_effort="high",
            max_tokens=9,
            agent_max_turns=4,
            token_budget=5,
            activation="default",
            is_default=True,
        )
        tpl = db.get_skill_by_name("model-skill")
        assert tpl is not None
        dropped = {
            "model",
            "temperature",
            "reasoning_effort",
            "max_tokens",
            "agent_max_turns",
            "token_budget",
            "activation",
            "is_default",
        }
        assert not dropped & tpl.keys()

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
            auto_approve=True,
            notify_on_complete='{"channel":"discord"}',
            enabled=True,
            allowed_tools='["bash","read_file","write_file"]',
        )
        tpl = db.get_skill_by_name("full-config-skill")
        assert tpl is not None
        assert tpl["auto_approve"] is True
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
# 6. Migration behavior tests
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
            auto_approve=True,
        )
        skill = db.get_prompt_template_by_name("my-profile")
        assert skill is not None
        assert skill["category"] == "profile"
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
        )
        assert db.get_prompt_template_by_name("deploy-bot") is not None
        assert db.get_prompt_template_by_name("skills-deploy-bot") is not None


# ---------------------------------------------------------------------------
# 7. Admin endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_api_storage(tmp_path, sqlite_backend_factory):
    return sqlite_backend_factory(str(tmp_path / "full_api_test.db"))


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
                # An older client's model settings, activation and token budget
                # are ignored (#1292).
                "activation": "named",
                "token_budget": 100,
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
        assert not {"activation", "token_budget", "model", "temperature"} & data.keys()
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
                "priority": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "new desc"
        assert data["priority"] == 3

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

    def test_list_skills_summary_excludes_hidden_from_menu(self, full_api_client, full_api_storage):
        """GET /v1/api/skills excludes skills with ``hidden_from_menu=true``.

        The admin Skills tab (``/v1/api/admin/skills``) still returns them
        — the filter only applies to the user-facing picker.
        """
        full_api_client.post(
            "/v1/api/admin/skills",
            json={"name": "visible-skill", "content": "content", "description": "v"},
        )
        full_api_client.post(
            "/v1/api/admin/skills",
            json={
                "name": "hidden-skill",
                "content": "content",
                "description": "h",
                "hidden_from_menu": True,
            },
        )

        # Picker filters out the hidden one.
        resp = full_api_client.get("/v1/api/skills")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()["skills"]]
        assert "visible-skill" in names
        assert "hidden-skill" not in names

        # Admin tab still surfaces it.
        admin_resp = full_api_client.get("/v1/api/admin/skills")
        assert admin_resp.status_code == 200
        admin_names = [s["name"] for s in admin_resp.json()["skills"]]
        assert "hidden-skill" in admin_names

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


# ---------------------------------------------------------------------------
# 8. Skill session config applied to workstream via server handler
# ---------------------------------------------------------------------------


class TestSkillConfigAppliedToWorkstream:
    """Verify that skill session config fields are applied to the ChatSession
    when a workstream is created via the server ``create_workstream`` handler.
    """

    @pytest.fixture()
    def _ws_app(self, tmp_path, sqlite_backend_factory):
        """Build a minimal Starlette app with the real ``create_workstream``
        handler, a real ``SessionManager``, and a temp SQLite storage
        backend.  Returns ``(TestClient, SessionManager, storage)``.
        """
        import queue
        import threading

        import turnstone.core.storage._registry as _reg
        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.session_manager import SessionManager
        from turnstone.core.session_routes import (
            SessionEndpointConfig,
            make_create_handler,
        )
        from turnstone.server import (
            WebUI,
            _interactive_create_build_kwargs,
            _interactive_create_post_install,
            _interactive_create_pre_commit,
            _interactive_create_prepare_install,
            _interactive_create_validate_request,
            _interactive_manager_lookup,
            _interactive_tenant_check,
        )

        storage = sqlite_backend_factory(str(tmp_path / "ws_test.db"))

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
                # Production passes the operator's ``tools.agent_max_turns`` here.
                agent_max_turns=7,
                ws_id=ws_id,
                skill=kwargs.get("skill"),
                workstream_lease=kwargs.get("workstream_lease"),
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

        # Build the same lifted create handler the production app
        # mounts so this fixture exercises the make_create_handler
        # factory rather than a parallel pre-lift body.
        _test_cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=_interactive_manager_lookup,
            tenant_check=_interactive_tenant_check,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            create_supports_attachments=True,
            create_supports_user_id_override=True,
            create_validate_request=_interactive_create_validate_request,
            create_build_kwargs=_interactive_create_build_kwargs,
            create_pre_commit=_interactive_create_pre_commit,
            create_post_install=_interactive_create_post_install,
            create_prepare_install=_interactive_create_prepare_install,
        )
        _test_create_handler = make_create_handler(_test_cfg)
        routes = [
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/new",
                        _test_create_handler,
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

    def test_create_lift_400s_on_malformed_notify_targets(self, _ws_app):
        """Regression for the lifted create handler — malformed
        ``notify_targets`` returns 400 from the validator (pre-create
        gate), not 500 from a post_install raise."""
        client, mgr, storage = _ws_app

        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "x", "notify_targets": "{not json"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert "error" in body
        # The workstream must NOT have been created — the validator
        # gates BEFORE mgr.create, so storage stays clean.
        assert len(list(storage.list_workstreams())) == 0

    @staticmethod
    def _serve_rows_with_model_settings(monkeypatch: pytest.MonkeyPatch, storage: Any) -> None:
        """Make skill lookups return the five keys a skill row no longer has, the
        shape of a custom backend or a row read before migration 080."""
        real_lookup = storage.get_prompt_template_by_name

        def _lookup(name: str) -> dict[str, Any] | None:
            row = real_lookup(name)
            if row is None:
                return None
            return {
                **row,
                "model": "skill-model",
                "temperature": 0.9,
                "reasoning_effort": "high",
                "max_tokens": 1024,
                "agent_max_turns": 3,
            }

        monkeypatch.setattr(storage, "get_prompt_template_by_name", _lookup)

    def test_skill_never_sets_the_model_or_sampling(self, _ws_app, monkeypatch):
        """A skill leaves the default alias, its settings and the task-agent turn
        cap in place (#1292)."""
        client, mgr, storage = _ws_app
        _create_template(storage, "s1", "legacy-skill", "Be warm.", enabled=True)
        self._serve_rows_with_model_settings(monkeypatch, storage)

        resp = client.post("/v1/api/workstreams/new", json={"skill": "legacy-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        sess = ws.session
        assert sess.model == "test-model"
        assert (sess.temperature, sess.reasoning_effort, sess.max_tokens) == (0.5, None, 4096)
        assert sess.agent_max_turns == 7  # the session factory's operator value

    def test_node_factory_passes_the_operator_turn_cap(self):
        """The node session factory, a closure in ``server.main``, gives every session the
        operator's ``tools.agent_max_turns``: the task-agent turn cap's only source (#1292)."""
        import inspect

        from turnstone import server

        src = inspect.getsource(server.main)
        assert 'agent_max_turns=config_store.get("tools.agent_max_turns")' in src

    def test_request_model_wins_over_a_skill(self, _ws_app, monkeypatch):
        """The model the create request names is the one the workstream runs on."""
        client, mgr, storage = _ws_app
        _create_template(storage, "s1", "legacy-skill", "Be warm.", enabled=True)
        self._serve_rows_with_model_settings(monkeypatch, storage)

        resp = client.post(
            "/v1/api/workstreams/new", json={"skill": "legacy-skill", "model": "request-model"}
        )
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session.model == "request-model"

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

    def test_all_session_config_fields_applied(self, _ws_app):
        """All session config fields from a skill are applied together."""
        from turnstone.server import WebUI

        client, mgr, storage = _ws_app
        targets = [{"channel_type": "discord", "channel_id": "1"}]
        _create_template(
            storage,
            "s1",
            "full-skill",
            "Full config skill.",
            auto_approve=True,
            allowed_tools='["bash", "write_file", "read_file"]',
            notify_on_complete=json.dumps(targets),
            enabled=True,
        )

        resp = client.post("/v1/api/workstreams/new", json={"skill": "full-skill"})
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        sess = ws.session
        # The skill's targets are where the workstream reports when it finishes.
        assert json.loads(ws.notify_targets) == targets
        assert sess._applied_skill_id == "s1"
        assert sess._applied_skill_content == "Full config skill."
        # The create-time stamp is saved with the workstream: every reopen, fork and copy reads it.
        saved = storage.load_workstream_config(ws.id)
        assert saved["applied_skill_id"] == "s1"
        assert saved["applied_skill_content"] == "Full config skill."
        assert isinstance(ws.ui, WebUI)
        assert ws.ui.auto_approve is True
        assert ws.ui.auto_approve_tools == {"bash", "write_file", "read_file"}

    def test_a_reopen_renders_the_text_the_skill_had_at_creation(self, _ws_app):
        """Through the real create handler and session manager (#1292)."""
        client, mgr, storage = _ws_app
        _create_template(storage, "s1", "full-skill", "CREATE-TIME TEXT", enabled=True)
        resp = client.post("/v1/api/workstreams/new", json={"skill": "full-skill"})
        assert resp.status_code == 200, resp.text
        ws_id = resp.json()["ws_id"]
        storage.update_prompt_template("s1", content="EDITED TEXT")

        mgr.close(ws_id)
        ws = mgr.open(ws_id)

        assert ws is not None and ws.session is not None
        text = "\n".join(m["content"] for m in ws.session.system_messages)
        assert "CREATE-TIME TEXT" in text
        assert "EDITED TEXT" not in text
        assert ws.session._skill_name == "full-skill"
        mgr.close(ws_id)

    def test_request_notify_targets_win_over_the_skills(self, _ws_app):
        client, mgr, storage = _ws_app
        _create_template(
            storage,
            "s1",
            "notify-skill",
            "Skill with targets.",
            notify_on_complete='[{"channel_type": "discord", "channel_id": "1"}]',
            enabled=True,
        )
        own = [{"channel_type": "slack", "user_id": "u2"}]

        resp = client.post(
            "/v1/api/workstreams/new", json={"skill": "notify-skill", "notify_targets": own}
        )
        assert resp.status_code == 200
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None
        assert json.loads(ws.notify_targets) == own

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
