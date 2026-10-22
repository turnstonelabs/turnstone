"""Heuristic-rule writes preserve the pattern types consumed by the registry."""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import (
    admin_create_heuristic_rule,
    admin_create_output_guard_pattern,
    admin_delete_heuristic_rule,
    admin_delete_output_guard_pattern,
    admin_judge_reload,
    admin_update_heuristic_rule,
    admin_update_output_guard_pattern,
)
from turnstone.core.config_store import ConfigStore
from turnstone.core.rule_registry import RuleRegistry


@pytest.fixture
def client(storage_backend):
    storage_backend.create_heuristic_rule(
        rule_id="existing",
        name="existing",
        risk_level="low",
        confidence=1.0,
        recommendation="approve",
        tool_pattern="bash",
        arg_patterns='["^safe$"]',
    )
    storage_backend.create_output_guard_pattern(
        pattern_id="existing-pattern",
        name="existing-pattern",
        category="info_disclosure",
        risk_level="medium",
        pattern="secret",
        flag_name="test_secret",
        annotation="test",
    )
    app = Starlette(
        routes=[
            Route("/rules", admin_create_heuristic_rule, methods=["POST"]),
            Route("/rules/{rule_id}", admin_update_heuristic_rule, methods=["PUT"]),
            Route("/rules/{rule_id}", admin_delete_heuristic_rule, methods=["DELETE"]),
            Route("/patterns", admin_create_output_guard_pattern, methods=["POST"]),
            Route("/patterns/{pattern_id}", admin_update_output_guard_pattern, methods=["PUT"]),
            Route("/patterns/{pattern_id}", admin_delete_output_guard_pattern, methods=["DELETE"]),
            Route("/reload", admin_judge_reload, methods=["POST"]),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage_backend
    app.state.config_store = ConfigStore(storage_backend)
    app.state.rule_registry = RuleRegistry(storage_backend)
    with TestClient(app) as client:
        client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.judge"})
        yield client


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("patterns", [[1], [True], [None], [{}]])
@pytest.mark.parametrize("encoded", [False, True], ids=["array", "json-string"])
def test_nonstring_patterns_are_refused_before_write(
    client, storage_backend, operation, patterns, encoded
):
    body = {"name": "new-rule", "arg_patterns": json.dumps(patterns) if encoded else patterns}
    if operation == "create":
        response = client.post("/rules", json=body)
    else:
        response = client.put("/rules/existing", json=body)

    assert response.status_code == 400
    assert response.json()["error"] == "arg_patterns[0] must be a string"
    assert storage_backend.get_heuristic_rule_by_name("new-rule") is None
    assert storage_backend.get_heuristic_rule("existing")["arg_patterns"] == '["^safe$"]'


def test_valid_pattern_arrays_can_be_saved_and_loaded(client, storage_backend):
    response = client.post("/rules", json={"name": "new-rule", "arg_patterns": ["^safe$"]})
    assert response.status_code == 201
    rule_id = response.json()["rule_id"]
    response = client.put(f"/rules/{rule_id}", json={"arg_patterns": '["^updated$"]'})
    assert response.status_code == 200
    registry = RuleRegistry(storage_backend)
    rule = next(rule for rule in registry.heuristic_rules if rule.name == "new-rule")
    assert rule.arg_patterns == ["^updated$"]


def _corrupt_rule(storage, name="corrupt-rule"):
    storage.create_heuristic_rule(
        rule_id=name,
        name=name,
        risk_level="low",
        confidence=1.0,
        recommendation="approve",
        tool_pattern="bash",
        arg_patterns="[broken",
    )


@pytest.mark.parametrize("kind", ["rule", "pattern"])
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_saved_edits_warn_and_publish_when_another_rule_is_corrupt(
    client, storage_backend, monkeypatch, kind, operation
):
    published = AsyncMock()
    monkeypatch.setattr("turnstone.console.server._publish_config_change", published)
    registry = client.app.state.rule_registry
    before, version = registry.heuristic_rules, registry.version
    _corrupt_rule(storage_backend)
    prefix = "/rules" if kind == "rule" else "/patterns"
    row_id = "existing" if kind == "rule" else "existing-pattern"
    get_row = (
        storage_backend.get_heuristic_rule
        if kind == "rule"
        else storage_backend.get_output_guard_pattern
    )
    if operation == "create":
        response = client.post(
            prefix,
            json={
                "name": "new-entry",
                "category": "info_disclosure",
                "pattern": "new-secret",
                "flag_name": "new_secret",
            },
        )
        assert response.status_code == 201
        row_id = response.json()["rule_id" if kind == "rule" else "pattern_id"]
        assert get_row(row_id)["name"] == "new-entry"
    elif operation == "update":
        response = client.put(f"{prefix}/{row_id}", json={"name": "updated-entry"})
        assert response.status_code == 200
        assert get_row(row_id)["name"] == "updated-entry"
    else:
        response = client.delete(f"{prefix}/{row_id}")
        assert response.status_code == 200
        assert get_row(row_id) is None

    assert "Change saved" in response.json()["warning"]
    assert "corrupt-rule" in response.json()["warning"]
    assert registry.heuristic_rules is before
    assert registry.version == version
    published.assert_awaited_once()


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "startup-failed"])
def test_corrupt_rules_can_be_repaired_one_at_a_time(client, storage_backend, monkeypatch, cached):
    published = AsyncMock()
    monkeypatch.setattr("turnstone.console.server._publish_config_change", published)
    _corrupt_rule(storage_backend, "broken-first")
    _corrupt_rule(storage_backend, "broken-second")
    if not cached:
        client.app.state.rule_registry = None

    first = client.put("/rules/broken-first", json={"arg_patterns": []})
    assert first.status_code == 200
    assert "broken-second" in first.json()["warning"]
    second = client.put("/rules/broken-second", json={"arg_patterns": []})
    assert second.status_code == 200
    assert "warning" not in second.json()
    registry = client.app.state.rule_registry
    assert {rule.name for rule in registry.heuristic_rules} >= {"broken-first", "broken-second"}
    assert published.await_count == 2


def test_explicit_reload_reports_invalid_configuration(client, storage_backend):
    _corrupt_rule(storage_backend)
    response = client.post("/reload")
    assert response.status_code == 409
    assert "corrupt-rule" in response.json()["error"]
