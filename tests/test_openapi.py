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
            "/v1/api/workstreams/{ws_id}",
            "/v1/api/workstreams/{ws_id}/history",
            "/v1/api/workstreams/{ws_id}/send",
            "/v1/api/workstreams/{ws_id}/approve",
            "/v1/api/workstreams/{ws_id}/cancel",
            "/v1/api/workstreams/{ws_id}/rewind",
            "/v1/api/workstreams/{ws_id}/retry",
            "/v1/api/workstreams/{ws_id}/close",
            "/v1/api/workstreams/{ws_id}/events",
            "/v1/api/dashboard",
            "/v1/api/workstreams/saved",
            "/v1/api/command",
            "/v1/api/events/global",
            "/v1/api/workstreams/new",
            "/v1/api/workstreams/{ws_id}/speech-to-text",
            "/v1/api/tts",
            "/v1/api/auth/login",
            "/v1/api/auth/logout",
            "/health",
        }
        assert expected.issubset(paths), f"Missing: {expected - paths}"

    def test_voice_endpoints_documented(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        stt = spec["paths"]["/v1/api/workstreams/{ws_id}/speech-to-text"]["post"]
        tts = spec["paths"]["/v1/api/tts"]["post"]
        assert "responses" in stt
        assert "requestBody" in tts
        assert "application/json" in tts["requestBody"]["content"]
        schemas = spec["components"]["schemas"]
        assert "capabilities" in schemas["AvailableModelInfo"]["properties"]
        models_props = schemas["ListAvailableModelsResponse"]["properties"]
        assert "stt_default_alias" in models_props
        assert "tts_default_alias" in models_props

    def test_workstream_history_has_limit_query_param(self):
        """Mirror of the coord-side history limit param test — server now
        exposes the same endpoint via the lifted factory."""
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        op = spec["paths"]["/v1/api/workstreams/{ws_id}/history"]["get"]
        param_names = [p["name"] for p in op.get("parameters", [])]
        assert "ws_id" in param_names
        assert "limit" in param_names

    def test_history_handoff_and_failure_contract_is_public(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        history = spec["paths"]["/v1/api/workstreams/{ws_id}/history"]["get"]
        events = spec["paths"]["/v1/api/workstreams/{ws_id}/events"]["get"]
        close = spec["paths"]["/v1/api/workstreams/{ws_id}/close"]["post"]
        history_schema = spec["components"]["schemas"]["WorkstreamHistoryResponse"]

        assert "authoritative total accepted conversation-row prefix" in history["description"]
        assert "History temporarily unavailable" in history["description"]
        assert "history_resync" in events["description"]
        assert "numeric event replay is not a substitute" in events["description"]
        assert "accepted live conversation row" in close["description"]
        assert "handoff_token" in history_schema["properties"]
        assert (
            "Admission of a later row changes the token"
            in history_schema["properties"]["handoff_token"]["description"]
        )

    def test_schemas_not_empty(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        assert len(spec["components"]["schemas"]) > 0

    def test_operator_workstream_schemas_publish_sanitized_persistence_state(self):
        from turnstone.api.server_spec import build_server_spec

        schemas = build_server_spec()["components"]["schemas"]
        expected = ["healthy", "pending", "retrying", "conflict"]
        for name in ("WorkstreamInfo", "WorkstreamDetailResponse", "DashboardWorkstream"):
            field = schemas[name]["properties"]["persistence_state"]
            assert field["enum"] == expected
            assert field["default"] == "healthy"

    def test_json_serializable(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        result = json.dumps(spec)
        assert len(result) > 100

    def test_send_endpoint_has_request_body(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        send = spec["paths"]["/v1/api/workstreams/{ws_id}/send"]["post"]
        assert "requestBody" in send
        assert "application/json" in send["requestBody"]["content"]

    def test_memory_name_contract_is_published_on_body_and_path(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        expected_pattern = "^[a-z0-9]+(?:_[a-z0-9]+)*$"
        save_name = spec["components"]["schemas"]["SaveMemoryRequest"]["properties"]["name"]
        assert save_name["pattern"] == expected_pattern
        assert save_name["maxLength"] == 256
        for method in ("get", "delete"):
            operation = spec["paths"]["/v1/api/memories/{name}"][method]
            name = next(param for param in operation["parameters"] if param["name"] == "name")
            assert name["schema"]["pattern"] == expected_pattern
            assert name["schema"]["maxLength"] == 256

    def test_admin_verdict_contract_exposes_approval_principals(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        verdict = spec["components"]["schemas"]["VerdictInfo"]["properties"]
        assert verdict["resolver_principal_id"]["type"] == "string"
        assert verdict["execution_principal_id"]["type"] == "string"

    def test_approval_and_cancel_preserve_extended_response_contracts(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        approve = spec["paths"]["/v1/api/workstreams/{ws_id}/approve"]["post"]
        cancel = spec["paths"]["/v1/api/workstreams/{ws_id}/cancel"]["post"]
        assert approve["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApproveResponse"
        }
        assert cancel["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CancelResponse"
        }
        assert cancel["requestBody"]["required"] is False
        assert "cycle_id" in spec["components"]["schemas"]["ApproveResponse"]["properties"]
        assert "dropped" in spec["components"]["schemas"]["CancelResponse"]["properties"]

    def test_create_status_is_optional_but_never_advertised_as_null(self):
        from turnstone.api.server_spec import build_server_spec

        spec = build_server_spec()
        schema = spec["components"]["schemas"]["CreateWorkstreamResponse"]
        status = schema["properties"]["initial_message_status"]
        assert status["enum"] == ["queue_full", "refused_closed"]
        assert "initial_message_status" not in schema.get("required", [])

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

    def test_cluster_workstream_schema_publishes_sanitized_persistence_state(self):
        from turnstone.api.console_spec import build_console_spec

        field = build_console_spec()["components"]["schemas"]["ClusterWorkstreamInfo"][
            "properties"
        ]["persistence_state"]
        assert field["enum"] == ["healthy", "pending", "retrying", "conflict"]
        assert field["default"] == "healthy"

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
            "/v1/api/workstreams/{ws_id}/rewind",
            "/v1/api/workstreams/{ws_id}/retry",
            "/v1/api/workstreams/{ws_id}/close",
            "/v1/api/workstreams/{ws_id}/events",
            "/v1/api/workstreams/{ws_id}/history",
            "/v1/api/workstreams/{ws_id}/children",
            "/v1/api/workstreams/{ws_id}/tasks",
            "/v1/api/cluster/ws/{ws_id}/detail",
        }
        assert expected.issubset(paths), f"Missing: {expected - paths}"

    def test_coordinator_history_handoff_and_failure_contract_is_public(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        history = spec["paths"]["/v1/api/workstreams/{ws_id}/history"]["get"]
        events = spec["paths"]["/v1/api/workstreams/{ws_id}/events"]["get"]
        close = spec["paths"]["/v1/api/workstreams/{ws_id}/close"]["post"]

        assert "authoritative total accepted conversation-row prefix" in history["description"]
        assert "History temporarily unavailable" in history["description"]
        assert "history_resync" in events["description"]
        assert "numeric replay is not a substitute" in events["description"]
        assert "accepted live conversation row" in close["description"]

    def test_routing_paths_and_extended_response_contracts(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        paths = spec["paths"]
        for suffix in ("send", "approve", "cancel", "rewind", "retry", "close"):
            assert f"/v1/api/route/workstreams/{{ws_id}}/{suffix}" in paths
        assert "/v1/api/route/send" not in paths
        assert "/v1/api/route/approve" not in paths
        assert "/v1/api/route/cancel" not in paths
        assert "/v1/api/route/workstreams/close" not in paths

        coordinator_approve = paths["/v1/api/workstreams/{ws_id}/approve"]["post"]
        coordinator_cancel = paths["/v1/api/workstreams/{ws_id}/cancel"]["post"]
        assert coordinator_approve["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApproveResponse"
        }
        assert coordinator_cancel["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CancelResponse"
        }
        assert coordinator_cancel["requestBody"]["required"] is False

    def test_route_create_and_live_contracts(self):
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        route_create = spec["paths"]["/v1/api/route/workstreams/new"]["post"]
        assert route_create["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/RouteCreateRequest"
        }
        ws_id_param = next(p for p in route_create["parameters"] if p["name"] == "ws_id")
        assert ws_id_param["required"] is False
        assert "multipart" in ws_id_param["description"]
        assert route_create["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/RouteCreateResponse"
        }
        route_response = spec["components"]["schemas"]["RouteCreateResponse"]
        assert "routing_strategy" in route_response["properties"]
        assert {"node_url", "node_id", "routing_strategy"}.issubset(set(route_response["required"]))
        assert route_response["properties"]["routing_strategy"]["enum"] == [
            "rendezvous",
            "target_node",
            "resume",
        ]
        assert set(route_create["responses"]) == {
            "200",
            "400",
            "403",
            "404",
            "409",
            "413",
            "429",
            "500",
            "502",
            "503",
        }

        live = spec["paths"]["/v1/api/route/workstreams/{ws_id}/live"]["get"]
        assert set(live["responses"]) == {"200", "400", "502", "503"}

    def test_coordinator_create_has_request_body_and_200(self):
        """Coordinator create returns 200 and accepts a body.

        Pre-1.5.0 this returned 201 (REST-strict for create); the lifted
        ``make_create_handler`` factory converges on 200 across both
        kinds for response-shape parity with every other shared verb.
        """
        from turnstone.api.console_spec import build_console_spec

        spec = build_console_spec()
        op = spec["paths"]["/v1/api/workstreams/new"]["post"]
        assert "requestBody" in op
        assert "application/json" in op["requestBody"]["content"]
        assert "200" in op["responses"]

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


def test_model_max_concurrency_schema_is_strict_non_nullable_integer() -> None:
    from turnstone.api.console_spec import build_console_spec

    schemas = build_console_spec()["components"]["schemas"]
    for name in ("ModelDefinitionInfo", "CreateModelDefinitionRequest"):
        prop = schemas[name]["properties"]["max_concurrency"]
        assert prop["type"] == "integer"
        assert prop["default"] == 0
        assert prop["minimum"] == 0
        assert prop["maximum"] == 2_147_483_647
        assert "anyOf" not in prop

    update_prop = schemas["UpdateModelDefinitionRequest"]["properties"]["max_concurrency"]
    assert update_prop["type"] == "integer"
    assert update_prop["minimum"] == 0
    assert update_prop["maximum"] == 2_147_483_647
    assert "anyOf" not in update_prop
    assert "default" not in update_prop


class TestCheckedInArtifactFreshness:
    """The checked-in `sdk/typescript/*.json` specs must match their source.

    ``info.version`` is normalised out on purpose: it tracks
    ``turnstone.__version__``, so comparing it would fail every release bump
    with a misdiagnosing "spec is stale" message.
    """

    @staticmethod
    def _schema_only(spec: dict) -> dict:
        """The spec with the release-coupled version stripped."""
        pruned = dict(spec)
        pruned["info"] = {k: v for k, v in spec.get("info", {}).items() if k != "version"}
        return pruned

    def _checked_in(self, name: str) -> dict:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        return json.loads((root / "sdk" / "typescript" / name).read_text())

    def test_server_spec_matches_checked_in_artifact(self):
        from turnstone.api.server_spec import build_server_spec

        assert self._schema_only(build_server_spec()) == self._schema_only(
            self._checked_in("openapi-server.json")
        ), (
            "openapi-server.json is stale; regenerate with "
            "`uv run python scripts/generate-types.py` in sdk/typescript/"
        )

    def test_console_spec_matches_checked_in_artifact(self):
        from turnstone.api.console_spec import build_console_spec

        assert self._schema_only(build_console_spec()) == self._schema_only(
            self._checked_in("openapi-console.json")
        ), (
            "openapi-console.json is stale; regenerate with "
            "`uv run python scripts/generate-types.py` in sdk/typescript/"
        )
