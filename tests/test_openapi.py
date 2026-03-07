"""Tests for OpenAPI spec generation."""

import json


class TestServerSpec:
    """Validate the generated server OpenAPI spec."""

    def test_valid_openapi_version(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        assert spec["openapi"] == "3.1.0"

    def test_has_info(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        assert "title" in spec["info"]
        assert "version" in spec["info"]

    def test_has_all_api_endpoints(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        paths = set(spec["paths"].keys())
        expected = {
            "/v1/api/workstreams",
            "/v1/api/dashboard",
            "/v1/api/workstreams/saved",
            "/v1/api/send",
            "/v1/api/approve",
            "/v1/api/plan",
            "/v1/api/command",
            "/v1/api/events",
            "/v1/api/events/global",
            "/v1/api/workstreams/new",
            "/v1/api/workstreams/close",
            "/v1/api/auth/login",
            "/v1/api/auth/logout",
            "/health",
        }
        assert expected.issubset(paths), f"Missing: {expected - paths}"

    def test_schemas_not_empty(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        assert len(spec["components"]["schemas"]) > 0

    def test_json_serializable(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        result = json.dumps(spec)
        assert len(result) > 100

    def test_send_endpoint_has_request_body(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        send = spec["paths"]["/v1/api/send"]["post"]
        assert "requestBody" in send
        assert "application/json" in send["requestBody"]["content"]

    def test_health_endpoint_not_versioned(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        assert "/health" in spec["paths"]
        assert "/v1/health" not in spec["paths"]


class TestConsoleSpec:
    """Validate the generated console OpenAPI spec."""

    def test_valid_openapi_version(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        assert spec["openapi"] == "3.1.0"

    def test_has_cluster_endpoints(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        paths = set(spec["paths"].keys())
        expected = {
            "/v1/api/cluster/overview",
            "/v1/api/cluster/nodes",
            "/v1/api/cluster/workstreams",
            "/v1/api/cluster/node/{node_id}",
            "/v1/api/cluster/workstreams/new",
            "/v1/api/cluster/events",
        }
        assert expected.issubset(paths), f"Missing: {expected - paths}"

    def test_json_serializable(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        result = json.dumps(spec)
        assert len(result) > 100

    def test_nodes_endpoint_has_query_params(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        nodes = spec["paths"]["/v1/api/cluster/nodes"]["get"]
        assert "parameters" in nodes
        param_names = [p["name"] for p in nodes["parameters"]]
        assert "sort" in param_names
        assert "limit" in param_names
