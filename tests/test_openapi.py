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

    def test_has_coordinator_endpoints(self):
        """Phase 1-3 coordinator routes must appear in the OpenAPI catalog —
        the spec was missing every coordinator endpoint except ``/open``,
        so SDK consumers and operators couldn't discover the surface
        from /docs.  Pin the full set so a future regression that drops
        one fails loudly."""
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        paths = set(spec["paths"].keys())
        expected = {
            "/v1/api/workstreams/new",
            "/v1/api/workstreams",
            "/v1/api/workstreams/{ws_id}",
            "/v1/api/workstreams/{ws_id}/open",
            "/v1/api/workstreams/{ws_id}/send",
            "/v1/api/workstreams/{ws_id}/approve",
            "/v1/api/workstreams/{ws_id}/cancel",
            "/v1/api/workstreams/{ws_id}/close",
            "/v1/api/workstreams/{ws_id}/events",
            "/v1/api/workstreams/{ws_id}/history",
            "/v1/api/workstreams/{ws_id}/children",
            "/v1/api/workstreams/{ws_id}/tasks",
            "/v1/api/cluster/ws/{ws_id}/detail",
        }
        assert expected.issubset(paths), f"Missing: {expected - paths}"

    def test_coordinator_create_has_request_body_and_201(self):
        """Coordinator create returns 201 (not 200) and accepts a body."""
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        op = spec["paths"]["/v1/api/workstreams/new"]["post"]
        assert "requestBody" in op
        assert "application/json" in op["requestBody"]["content"]
        # Pin the 201 success code.
        assert "201" in op["responses"]

    def test_coordinator_history_has_limit_query_param(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        op = spec["paths"]["/v1/api/workstreams/{ws_id}/history"]["get"]
        param_names = [p["name"] for p in op.get("parameters", [])]
        assert "ws_id" in param_names  # auto-added from path
        assert "limit" in param_names

    def test_coordinator_endpoints_share_tag(self):
        """All coordinator endpoints (including the cluster-inspect one)
        live under the same OpenAPI tag so /docs groups them together."""
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        coord_paths = [p for p in spec["paths"] if "/coordinator" in p]
        coord_paths.append("/v1/api/cluster/ws/{ws_id}/detail")
        for path in coord_paths:
            for op in spec["paths"][path].values():
                assert "Coordinator" in op.get("tags", []), (
                    f"{path} missing Coordinator tag (tags={op.get('tags')})"
                )
