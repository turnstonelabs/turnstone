"""Tests for governance storage operations (SQLite backend).

Covers RBAC roles, organizations, tool policies, prompt templates,
usage events, and audit events.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class TestRoleCRUD:
    def test_create_role(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        role = db.get_role("r1")
        assert role is not None
        assert role["role_id"] == "r1"
        assert role["name"] == "editor"
        assert role["display_name"] == "Editor"
        assert role["permissions"] == "read,write"
        assert role["builtin"] is False
        assert role["org_id"] == ""
        assert "created" in role
        assert "updated" in role

    def test_create_role_idempotent(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        # Second insert with same role_id should be silently ignored.
        db.create_role("r1", "editor2", "Editor 2", "read", builtin=True, org_id="org1")
        role = db.get_role("r1")
        assert role is not None
        # Original values preserved.
        assert role["name"] == "editor"
        assert role["display_name"] == "Editor"

    def test_get_role_by_name(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        role = db.get_role_by_name("editor")
        assert role is not None
        assert role["role_id"] == "r1"

    def test_get_role_by_name_nonexistent(self, db):
        assert db.get_role_by_name("nope") is None

    def test_list_roles(self, db):
        db.create_role("r2", "beta", "Beta Role", "read", builtin=False, org_id="")
        db.create_role("r1", "alpha", "Alpha Role", "write", builtin=False, org_id="")
        roles = db.list_roles()
        assert len(roles) == 2
        # Ordered by name ascending.
        assert roles[0]["name"] == "alpha"
        assert roles[1]["name"] == "beta"

    def test_list_roles_filter_org(self, db):
        db.create_role("r1", "role_a", "A", "read", builtin=False, org_id="org1")
        db.create_role("r2", "role_b", "B", "read", builtin=False, org_id="org2")
        db.create_role("r3", "role_c", "C", "read", builtin=False, org_id="org1")
        result = db.list_roles(org_id="org1")
        assert len(result) == 2
        assert {r["role_id"] for r in result} == {"r1", "r3"}

    def test_update_role(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        ok = db.update_role("r1", permissions="read,write,approve", display_name="Senior Editor")
        assert ok is True
        role = db.get_role("r1")
        assert role is not None
        assert role["permissions"] == "read,write,approve"
        assert role["display_name"] == "Senior Editor"

    def test_update_role_nonexistent(self, db):
        assert db.update_role("missing", permissions="read") is False

    def test_delete_role(self, db):
        db.create_role("r1", "editor", "Editor", "read", builtin=False, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1")
        # Verify assignment exists.
        assert len(db.list_user_roles("u1")) == 1
        ok = db.delete_role("r1")
        assert ok is True
        assert db.get_role("r1") is None
        # Cascade: user_roles for this role should be gone.
        assert len(db.list_user_roles("u1")) == 0

    def test_delete_role_nonexistent(self, db):
        assert db.delete_role("missing") is False

    def test_assign_role(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1", assigned_by="admin")
        roles = db.list_user_roles("u1")
        assert len(roles) == 1
        assert roles[0]["role_id"] == "r1"
        assert roles[0]["assigned_by"] == "admin"

    def test_assign_role_idempotent(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1")
        # Second assign should not raise.
        db.assign_role("u1", "r1")
        roles = db.list_user_roles("u1")
        assert len(roles) == 1

    def test_unassign_role(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1")
        ok = db.unassign_role("u1", "r1")
        assert ok is True
        assert len(db.list_user_roles("u1")) == 0

    def test_unassign_role_nonexistent(self, db):
        assert db.unassign_role("u1", "r1") is False

    def test_list_user_roles(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        db.create_role("r2", "viewer", "Viewer", "read", builtin=True, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1", assigned_by="admin")
        db.assign_role("u1", "r2", assigned_by="system")
        roles = db.list_user_roles("u1")
        assert len(roles) == 2
        # Each entry should have joined role fields plus assignment metadata.
        for r in roles:
            assert "role_id" in r
            assert "name" in r
            assert "permissions" in r
            assert "assigned_by" in r
            assert "assignment_created" in r

    def test_get_user_permissions(self, db):
        db.create_role("r1", "editor", "Editor", "read,write", builtin=False, org_id="")
        db.create_role("r2", "approver", "Approver", "approve,read", builtin=False, org_id="")
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        db.assign_role("u1", "r1")
        db.assign_role("u1", "r2")
        perms = db.get_user_permissions("u1")
        assert perms == {"read", "write", "approve"}

    def test_get_user_permissions_no_roles(self, db):
        db.create_user("u1", "alice", "Alice", "$2b$hash")
        assert db.get_user_permissions("u1") == set()


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


class TestOrgCRUD:
    def test_create_org(self, db):
        db.create_org("org1", "acme", "Acme Corp", '{"plan":"pro"}')
        org = db.get_org("org1")
        assert org is not None
        assert org["org_id"] == "org1"
        assert org["name"] == "acme"
        assert org["display_name"] == "Acme Corp"
        assert org["settings"] == '{"plan":"pro"}'
        assert "created" in org
        assert "updated" in org

    def test_get_org_nonexistent(self, db):
        assert db.get_org("nope") is None

    def test_create_org_idempotent(self, db):
        db.create_org("org1", "acme", "Acme Corp")
        db.create_org("org1", "acme2", "Acme 2")
        org = db.get_org("org1")
        assert org is not None
        assert org["name"] == "acme"

    def test_list_orgs(self, db):
        db.create_org("o2", "beta", "Beta Inc")
        db.create_org("o1", "alpha", "Alpha LLC")
        orgs = db.list_orgs()
        assert len(orgs) == 2
        # Ordered by name ascending.
        assert orgs[0]["name"] == "alpha"
        assert orgs[1]["name"] == "beta"

    def test_update_org(self, db):
        db.create_org("org1", "acme", "Acme Corp")
        ok = db.update_org(
            "org1", display_name="Acme Corp Global", settings='{"plan":"enterprise"}'
        )
        assert ok is True
        org = db.get_org("org1")
        assert org is not None
        assert org["display_name"] == "Acme Corp Global"
        assert org["settings"] == '{"plan":"enterprise"}'

    def test_update_org_nonexistent(self, db):
        assert db.update_org("missing", display_name="X") is False


# ---------------------------------------------------------------------------
# Tool Policies
# ---------------------------------------------------------------------------


class TestToolPolicyCRUD:
    def test_create_tool_policy(self, db):
        db.create_tool_policy(
            "p1",
            "deny-bash",
            "bash*",
            "deny",
            priority=100,
            org_id="org1",
            enabled=True,
            created_by="admin",
        )
        pol = db.get_tool_policy("p1")
        assert pol is not None
        assert pol["policy_id"] == "p1"
        assert pol["name"] == "deny-bash"
        assert pol["tool_pattern"] == "bash*"
        assert pol["action"] == "deny"
        assert pol["priority"] == 100
        assert pol["org_id"] == "org1"
        assert pol["enabled"] is True
        assert pol["created_by"] == "admin"

    def test_get_tool_policy_nonexistent(self, db):
        assert db.get_tool_policy("missing") is None

    def test_list_tool_policies_ordered_by_priority(self, db):
        db.create_tool_policy("p1", "low", "*", "allow", priority=10)
        db.create_tool_policy("p2", "high", "*", "deny", priority=100)
        db.create_tool_policy("p3", "mid", "*", "ask", priority=50)
        policies = db.list_tool_policies()
        assert len(policies) == 3
        # DESC priority order.
        assert policies[0]["priority"] == 100
        assert policies[1]["priority"] == 50
        assert policies[2]["priority"] == 10

    def test_update_tool_policy(self, db):
        db.create_tool_policy("p1", "deny-bash", "bash*", "deny", priority=100)
        ok = db.update_tool_policy("p1", action="allow", priority=50)
        assert ok is True
        pol = db.get_tool_policy("p1")
        assert pol is not None
        assert pol["action"] == "allow"
        assert pol["priority"] == 50

    def test_update_tool_policy_nonexistent(self, db):
        assert db.update_tool_policy("missing", action="deny") is False

    def test_delete_tool_policy(self, db):
        db.create_tool_policy("p1", "deny-bash", "bash*", "deny", priority=100)
        ok = db.delete_tool_policy("p1")
        assert ok is True
        assert db.get_tool_policy("p1") is None

    def test_delete_tool_policy_nonexistent(self, db):
        assert db.delete_tool_policy("missing") is False

    def test_enabled_as_bool(self, db):
        db.create_tool_policy("p1", "on", "*", "allow", priority=0, enabled=True)
        db.create_tool_policy("p2", "off", "*", "deny", priority=0, enabled=False)
        p1 = db.get_tool_policy("p1")
        p2 = db.get_tool_policy("p2")
        assert p1 is not None
        assert p2 is not None
        assert p1["enabled"] is True
        assert isinstance(p1["enabled"], bool)
        assert p2["enabled"] is False
        assert isinstance(p2["enabled"], bool)

    def test_list_policies_filter_org(self, db):
        db.create_tool_policy("p1", "a", "*", "allow", priority=0, org_id="org1")
        db.create_tool_policy("p2", "b", "*", "deny", priority=0, org_id="org2")
        db.create_tool_policy("p3", "c", "*", "ask", priority=0, org_id="org1")
        result = db.list_tool_policies(org_id="org1")
        assert len(result) == 2
        assert {r["policy_id"] for r in result} == {"p1", "p3"}


# ---------------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------------


class TestPromptTemplateCRUD:
    def test_create_prompt_template(self, db):
        db.create_prompt_template(
            "t1",
            "greeting",
            "general",
            "Hello {{name}}!",
            variables='["name"]',
            is_default=True,
            org_id="org1",
            created_by="admin",
        )
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["template_id"] == "t1"
        assert tpl["name"] == "greeting"
        assert tpl["category"] == "general"
        assert tpl["content"] == "Hello {{name}}!"
        assert tpl["variables"] == '["name"]'
        assert tpl["is_default"] is True
        assert tpl["org_id"] == "org1"
        assert tpl["created_by"] == "admin"

    def test_get_prompt_template_nonexistent(self, db):
        assert db.get_prompt_template("missing") is None

    def test_list_prompt_templates_ordered_by_name(self, db):
        db.create_prompt_template("t2", "beta", "general", "B")
        db.create_prompt_template("t1", "alpha", "general", "A")
        templates = db.list_prompt_templates()
        assert len(templates) == 2
        assert templates[0]["name"] == "alpha"
        assert templates[1]["name"] == "beta"

    def test_list_prompt_templates_filter_org(self, db):
        db.create_prompt_template("t1", "a", "general", "A", org_id="org1")
        db.create_prompt_template("t2", "b", "general", "B", org_id="org2")
        result = db.list_prompt_templates(org_id="org1")
        assert len(result) == 1
        assert result[0]["template_id"] == "t1"

    def test_update_prompt_template(self, db):
        db.create_prompt_template("t1", "greeting", "general", "Hello!")
        ok = db.update_prompt_template("t1", content="Hi there!", category="custom")
        assert ok is True
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["content"] == "Hi there!"
        assert tpl["category"] == "custom"

    def test_update_prompt_template_nonexistent(self, db):
        assert db.update_prompt_template("missing", content="x") is False

    def test_delete_prompt_template(self, db):
        db.create_prompt_template("t1", "greeting", "general", "Hello!")
        ok = db.delete_prompt_template("t1")
        assert ok is True
        assert db.get_prompt_template("t1") is None

    def test_delete_prompt_template_nonexistent(self, db):
        assert db.delete_prompt_template("missing") is False

    def test_is_default_as_bool(self, db):
        db.create_prompt_template("t1", "default_one", "general", "D", is_default=True)
        db.create_prompt_template("t2", "not_default", "general", "N", is_default=False)
        t1 = db.get_prompt_template("t1")
        t2 = db.get_prompt_template("t2")
        assert t1 is not None
        assert t2 is not None
        assert t1["is_default"] is True
        assert isinstance(t1["is_default"], bool)
        assert t2["is_default"] is False
        assert isinstance(t2["is_default"], bool)

    def test_create_with_mcp_origin(self, db):
        db.create_prompt_template(
            "t1",
            "mcp__srv__prompt",
            "mcp",
            "content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["origin"] == "mcp"
        assert tpl["mcp_server"] == "srv"
        assert tpl["readonly"] is True
        assert isinstance(tpl["readonly"], bool)

    def test_default_origin_values(self, db):
        db.create_prompt_template("t1", "basic", "general", "Hello")
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["origin"] == "manual"
        assert tpl["mcp_server"] == ""
        assert tpl["readonly"] is False

    def test_get_prompt_template_by_name(self, db):
        db.create_prompt_template("t1", "greeting", "general", "Hello!")
        tpl = db.get_prompt_template_by_name("greeting")
        assert tpl is not None
        assert tpl["template_id"] == "t1"
        assert tpl["name"] == "greeting"

    def test_get_prompt_template_by_name_nonexistent(self, db):
        assert db.get_prompt_template_by_name("nope") is None

    def test_list_default_templates(self, db):
        db.create_prompt_template("t1", "alpha", "general", "A", is_default=True)
        db.create_prompt_template("t2", "beta", "general", "B", is_default=False)
        db.create_prompt_template("t3", "gamma", "general", "C", is_default=True)
        result = db.list_default_templates()
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "gamma"

    def test_list_default_templates_empty(self, db):
        db.create_prompt_template("t1", "alpha", "general", "A", is_default=False)
        assert db.list_default_templates() == []

    def test_list_prompt_templates_by_origin(self, db):
        db.create_prompt_template("t1", "manual_one", "general", "A", origin="manual")
        db.create_prompt_template("t2", "mcp_one", "mcp", "B", origin="mcp", mcp_server="srv1")
        db.create_prompt_template("t3", "mcp_two", "mcp", "C", origin="mcp", mcp_server="srv2")
        result = db.list_prompt_templates_by_origin("mcp")
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "mcp_one" in names
        assert "mcp_two" in names


# ---------------------------------------------------------------------------
# Usage Events
# ---------------------------------------------------------------------------


class TestUsageEvents:
    def test_record_usage_event(self, db):
        db.record_usage_event(
            "ev1",
            user_id="u1",
            ws_id="ws1",
            node_id="n1",
            model="gpt-5",
            prompt_tokens=100,
            completion_tokens=50,
            tool_calls_count=2,
        )
        # Verify via query_usage (no group_by returns summary).
        result = db.query_usage(since="2000-01-01T00:00:00")
        assert len(result) == 1
        assert result[0]["prompt_tokens"] == 100
        assert result[0]["completion_tokens"] == 50
        assert result[0]["tool_calls_count"] == 2

    def test_query_usage_summary(self, db):
        db.record_usage_event("ev1", model="gpt-5", prompt_tokens=100, completion_tokens=50)
        db.record_usage_event("ev2", model="gpt-5", prompt_tokens=200, completion_tokens=75)
        result = db.query_usage(since="2000-01-01T00:00:00")
        assert len(result) == 1
        assert result[0]["prompt_tokens"] == 300
        assert result[0]["completion_tokens"] == 125

    def test_query_usage_by_day(self, db):
        # Insert events with known timestamps by directly inserting rows.
        from turnstone.core.storage._schema import usage_events

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "e1",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5",
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                    {
                        "event_id": "e2",
                        "timestamp": "2026-03-01T14:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5",
                        "prompt_tokens": 50,
                        "completion_tokens": 25,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T14:00:00",
                    },
                    {
                        "event_id": "e3",
                        "timestamp": "2026-03-02T08:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5",
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "tool_calls_count": 0,
                        "created": "2026-03-02T08:00:00",
                    },
                ],
            )
            conn.commit()

        result = db.query_usage(since="2026-03-01T00:00:00", group_by="day")
        assert len(result) == 2
        assert result[0]["key"] == "2026-03-01"
        assert result[0]["prompt_tokens"] == 150
        assert result[1]["key"] == "2026-03-02"
        assert result[1]["prompt_tokens"] == 200

    def test_query_usage_by_model(self, db):
        from turnstone.core.storage._schema import usage_events

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "e1",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5",
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                    {
                        "event_id": "e2",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "claude-4",
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "tool_calls_count": 1,
                        "created": "2026-03-01T10:00:00",
                    },
                ],
            )
            conn.commit()

        result = db.query_usage(since="2026-03-01T00:00:00", group_by="model")
        assert len(result) == 2
        keys = [r["key"] for r in result]
        assert "gpt-5" in keys
        assert "claude-4" in keys

    def test_query_usage_by_user(self, db):
        from turnstone.core.storage._schema import usage_events

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "e1",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "u1",
                        "ws_id": "",
                        "node_id": "",
                        "model": "",
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                    {
                        "event_id": "e2",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "u2",
                        "ws_id": "",
                        "node_id": "",
                        "model": "",
                        "prompt_tokens": 300,
                        "completion_tokens": 150,
                        "tool_calls_count": 2,
                        "created": "2026-03-01T10:00:00",
                    },
                ],
            )
            conn.commit()

        result = db.query_usage(since="2026-03-01T00:00:00", group_by="user")
        assert len(result) == 2
        by_key = {r["key"]: r for r in result}
        assert by_key["u1"]["prompt_tokens"] == 100
        assert by_key["u2"]["prompt_tokens"] == 300

    def test_query_usage_filter_model(self, db):
        from turnstone.core.storage._schema import usage_events

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "e1",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5",
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                    {
                        "event_id": "e2",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "claude-4",
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "tool_calls_count": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                ],
            )
            conn.commit()

        result = db.query_usage(since="2026-03-01T00:00:00", model="gpt-5")
        assert len(result) == 1
        assert result[0]["prompt_tokens"] == 100

    def test_prune_usage_events(self, db):
        from turnstone.core.storage._schema import usage_events

        old_ts = "2020-01-01T00:00:00"
        now_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "old",
                        "timestamp": old_ts,
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "tool_calls_count": 0,
                        "created": old_ts,
                    },
                    {
                        "event_id": "new",
                        "timestamp": now_ts,
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "",
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "tool_calls_count": 0,
                        "created": now_ts,
                    },
                ],
            )
            conn.commit()

        pruned = db.prune_usage_events(retention_days=30)
        assert pruned == 1
        # Only the recent event should remain.
        result = db.query_usage(since="2000-01-01T00:00:00")
        assert result[0]["prompt_tokens"] == 20

    def test_record_and_query_cache_tokens(self, db):
        """Cache token columns are recorded and aggregated in query_usage."""
        db.record_usage_event(
            "ev1",
            model="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            cache_creation_tokens=80,
            cache_read_tokens=0,
        )
        db.record_usage_event(
            "ev2",
            model="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            cache_creation_tokens=0,
            cache_read_tokens=80,
        )
        result = db.query_usage(since="2000-01-01T00:00:00")
        assert len(result) == 1
        assert result[0]["cache_creation_tokens"] == 80
        assert result[0]["cache_read_tokens"] == 80

    def test_query_cache_tokens_grouped_by_model(self, db):
        """Cache tokens are included in grouped query results."""
        from turnstone.core.storage._schema import usage_events

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(usage_events),
                [
                    {
                        "event_id": "e1",
                        "timestamp": "2026-03-01T10:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "claude-sonnet-4-6",
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "tool_calls_count": 0,
                        "cache_creation_tokens": 90,
                        "cache_read_tokens": 0,
                        "created": "2026-03-01T10:00:00",
                    },
                    {
                        "event_id": "e2",
                        "timestamp": "2026-03-01T14:00:00",
                        "user_id": "",
                        "ws_id": "",
                        "node_id": "",
                        "model": "gpt-5.1",
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "tool_calls_count": 0,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 150,
                        "created": "2026-03-01T14:00:00",
                    },
                ],
            )
            conn.commit()

        result = db.query_usage(since="2026-03-01T00:00:00", group_by="model")
        assert len(result) == 2
        claude = next(r for r in result if r["key"] == "claude-sonnet-4-6")
        gpt = next(r for r in result if r["key"] == "gpt-5.1")
        assert claude["cache_creation_tokens"] == 90
        assert claude["cache_read_tokens"] == 0
        assert gpt["cache_creation_tokens"] == 0
        assert gpt["cache_read_tokens"] == 150


# ---------------------------------------------------------------------------
# Audit Events
# ---------------------------------------------------------------------------


class TestAuditEvents:
    def test_record_audit_event(self, db):
        db.record_audit_event(
            "a1",
            user_id="u1",
            action="role.create",
            resource_type="role",
            resource_id="r1",
            detail='{"name":"editor"}',
            ip_address="127.0.0.1",
        )
        events = db.list_audit_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["event_id"] == "a1"
        assert ev["user_id"] == "u1"
        assert ev["action"] == "role.create"
        assert ev["resource_type"] == "role"
        assert ev["resource_id"] == "r1"
        assert ev["detail"] == '{"name":"editor"}'
        assert ev["ip_address"] == "127.0.0.1"

    def test_list_audit_events(self, db):
        db.record_audit_event("a1", action="login")
        db.record_audit_event("a2", action="logout")
        events = db.list_audit_events()
        assert len(events) == 2
        # Ordered by timestamp DESC — most recent first.
        # Both created in quick succession with same-second granularity,
        # but the order should still be deterministic (DESC).
        assert {e["event_id"] for e in events} == {"a1", "a2"}

    def test_list_audit_events_filter_action(self, db):
        db.record_audit_event("a1", action="login")
        db.record_audit_event("a2", action="logout")
        db.record_audit_event("a3", action="login")
        events = db.list_audit_events(action="login")
        assert len(events) == 2
        assert all(e["action"] == "login" for e in events)

    def test_list_audit_events_filter_user(self, db):
        db.record_audit_event("a1", user_id="u1", action="login")
        db.record_audit_event("a2", user_id="u2", action="login")
        events = db.list_audit_events(user_id="u1")
        assert len(events) == 1
        assert events[0]["user_id"] == "u1"

    def test_list_audit_events_pagination(self, db):
        for i in range(5):
            db.record_audit_event(f"a{i}", action="test")
        page1 = db.list_audit_events(limit=2, offset=0)
        page2 = db.list_audit_events(limit=2, offset=2)
        page3 = db.list_audit_events(limit=2, offset=4)
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1
        # No overlap.
        ids = [e["event_id"] for e in page1 + page2 + page3]
        assert len(set(ids)) == 5

    def test_count_audit_events(self, db):
        db.record_audit_event("a1", action="login")
        db.record_audit_event("a2", action="logout")
        db.record_audit_event("a3", action="login")
        assert db.count_audit_events() == 3
        assert db.count_audit_events(action="login") == 2
        assert db.count_audit_events(action="logout") == 1

    def test_count_audit_events_filter_user(self, db):
        db.record_audit_event("a1", user_id="u1", action="login")
        db.record_audit_event("a2", user_id="u2", action="login")
        assert db.count_audit_events(user_id="u1") == 1

    def test_prune_audit_events(self, db):
        from turnstone.core.storage._schema import audit_events

        old_ts = "2020-01-01T00:00:00"
        now_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(audit_events),
                [
                    {
                        "event_id": "old",
                        "timestamp": old_ts,
                        "user_id": "",
                        "action": "test",
                        "resource_type": "",
                        "resource_id": "",
                        "detail": "{}",
                        "ip_address": "",
                        "created": old_ts,
                    },
                    {
                        "event_id": "new",
                        "timestamp": now_ts,
                        "user_id": "",
                        "action": "test",
                        "resource_type": "",
                        "resource_id": "",
                        "detail": "{}",
                        "ip_address": "",
                        "created": now_ts,
                    },
                ],
            )
            conn.commit()

        pruned = db.prune_audit_events(retention_days=30)
        assert pruned == 1
        assert db.count_audit_events() == 1
