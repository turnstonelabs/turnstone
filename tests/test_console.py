"""Tests for turnstone.console — collector and HTTP server."""

import json
import queue
from unittest.mock import MagicMock

import pytest

from turnstone.console.collector import ClusterCollector, NodeSnapshot
from turnstone.mq.protocol import (
    ClusterStateEvent,
)

# ---------------------------------------------------------------------------
# Mock broker for collector tests
# ---------------------------------------------------------------------------


class MockBroker:
    """Minimal broker mock that records calls and stores nodes."""

    def __init__(self):
        self.nodes: list[dict] = []
        self._subscriptions: dict[str, list] = {}

    def list_nodes(self) -> list[dict]:
        return list(self.nodes)

    def subscribe_outbound(self, channel, callback):
        self._subscriptions.setdefault(channel, []).append(callback)

    def publish_outbound(self, channel, event):
        for cb in self._subscriptions.get(channel, []):
            cb(event)

    def subscribe_cluster(self, callback):
        channel = "turnstone:events:cluster"
        self.subscribe_outbound(channel, callback)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_collector(broker=None, poll_interval=999, discovery_interval=999):
    """Create a collector with long intervals so threads don't auto-fire."""
    b = broker or MockBroker()
    return ClusterCollector(
        broker=b,
        poll_interval=poll_interval,
        discovery_interval=discovery_interval,
    )


def _dashboard_response(workstreams=None, aggregate=None):
    """Build a /v1/api/dashboard-style response dict."""
    return {
        "workstreams": workstreams or [],
        "aggregate": aggregate
        or {
            "total_tokens": 0,
            "total_tool_calls": 0,
            "active_count": 0,
            "total_count": 0,
            "uptime_seconds": 0,
            "node": "local",
        },
    }


# ---------------------------------------------------------------------------
# ClusterCollector — unit tests
# ---------------------------------------------------------------------------


class TestCollectorDiscovery:
    """Node discovery from heartbeat keys."""

    def test_discover_new_nodes(self):
        broker = MockBroker()
        broker.nodes = [
            {"node_id": "node-a", "server_url": "http://a:8080"},
            {"node_id": "node-b", "server_url": "http://b:8080"},
        ]
        c = _make_collector(broker)
        c._discover_nodes()

        overview = c.get_overview()
        assert overview["nodes"] == 2

    def test_discover_removes_lost_nodes(self):
        broker = MockBroker()
        broker.nodes = [{"node_id": "node-a", "server_url": "http://a:8080"}]
        c = _make_collector(broker)
        c._discover_nodes()
        assert c.get_overview()["nodes"] == 1

        # Node disappears
        broker.nodes = []
        c._discover_nodes()
        assert c.get_overview()["nodes"] == 0

    def test_discover_updates_server_url(self):
        broker = MockBroker()
        broker.nodes = [{"node_id": "node-a", "server_url": "http://a:8080"}]
        c = _make_collector(broker)
        c._discover_nodes()

        broker.nodes = [{"node_id": "node-a", "server_url": "http://a:9090"}]
        c._discover_nodes()

        detail = c.get_node_detail("node-a")
        assert detail["server_url"] == "http://a:9090"

    def test_discover_emits_node_joined_event(self):
        broker = MockBroker()
        c = _make_collector(broker)
        _events = []
        q = queue.Queue()
        c.register_listener(q)

        broker.nodes = [{"node_id": "node-a", "server_url": "http://a:8080"}]
        c._discover_nodes()

        event = q.get_nowait()
        assert event["type"] == "node_joined"
        assert event["node_id"] == "node-a"

    def test_discover_emits_node_lost_event(self):
        broker = MockBroker()
        broker.nodes = [{"node_id": "node-a", "server_url": "http://a:8080"}]
        c = _make_collector(broker)
        c._discover_nodes()

        q = queue.Queue()
        c.register_listener(q)

        broker.nodes = []
        c._discover_nodes()

        event = q.get_nowait()
        assert event["type"] == "node_lost"
        assert event["node_id"] == "node-a"


class TestCollectorPolling:
    """Polling /v1/api/dashboard from nodes."""

    def test_apply_poll_populates_workstreams(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")

        dashboard = _dashboard_response(
            workstreams=[
                {
                    "id": "ws1",
                    "name": "test",
                    "state": "running",
                    "tokens": 1000,
                    "context_ratio": 0.15,
                    "activity": "bash: ls",
                    "activity_state": "tool",
                    "tool_calls": 3,
                    "title": "My task",
                },
            ],
            aggregate={"total_tokens": 1000, "total_tool_calls": 3},
        )
        c._apply_poll("node-a", dashboard, {"status": "ok"})

        detail = c.get_node_detail("node-a")
        assert len(detail["workstreams"]) == 1
        assert detail["workstreams"][0]["name"] == "test"
        assert detail["workstreams"][0]["node"] == "node-a"
        assert detail["workstreams"][0]["server_url"] == "http://a:8080"
        assert detail["health"]["status"] == "ok"

    def test_apply_poll_replaces_stale_workstreams(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"old-ws": {"id": "old-ws", "name": "old", "state": "idle"}},
        )

        dashboard = _dashboard_response(
            workstreams=[{"id": "new-ws", "name": "new", "state": "running"}]
        )
        c._apply_poll("node-a", dashboard, {})

        detail = c.get_node_detail("node-a")
        assert len(detail["workstreams"]) == 1
        assert detail["workstreams"][0]["id"] == "new-ws"

    def test_apply_poll_ignores_unknown_node(self):
        c = _make_collector()
        # Should not raise
        c._apply_poll("unknown", _dashboard_response(), {})


class TestCollectorEvents:
    """Real-time event handling from cluster channel."""

    def test_cluster_state_event_updates_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "test", "state": "idle", "node": "node-a"}},
        )

        event = ClusterStateEvent(
            ws_id="ws1",
            state="running",
            node_id="node-a",
            tokens=5000,
            context_ratio=0.25,
            activity="bash: echo hi",
            activity_state="tool",
        )
        c._on_cluster_event(event.to_json())

        ws = c._nodes["node-a"].workstreams["ws1"]
        assert ws["state"] == "running"
        assert ws["tokens"] == 5000
        assert ws["activity"] == "bash: echo hi"

    def test_ws_created_event_adds_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")

        event_json = json.dumps(
            {
                "type": "ws_created",
                "ws_id": "ws-new",
                "name": "new-task",
                "node_id": "node-a",
                "correlation_id": "abc",
            }
        )
        c._on_cluster_event(event_json)

        assert "ws-new" in c._nodes["node-a"].workstreams
        assert c._nodes["node-a"].workstreams["ws-new"]["name"] == "new-task"
        assert c._nodes["node-a"].workstreams["ws-new"]["server_url"] == "http://a:8080"

    def test_ws_closed_event_removes_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            workstreams={"ws1": {"id": "ws1", "state": "idle"}},
        )

        event_json = json.dumps({"type": "ws_closed", "ws_id": "ws1"})
        c._on_cluster_event(event_json)

        assert "ws1" not in c._nodes["node-a"].workstreams

    def test_ws_rename_event_updates_name(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            workstreams={"ws1": {"id": "ws1", "name": "old-name", "state": "idle"}},
        )

        event_json = json.dumps({"type": "ws_rename", "ws_id": "ws1", "name": "new-name"})
        c._on_cluster_event(event_json)

        assert c._nodes["node-a"].workstreams["ws1"]["name"] == "new-name"

    def test_event_fans_out_to_listeners(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            workstreams={"ws1": {"id": "ws1", "state": "idle", "node": "node-a"}},
        )

        q = queue.Queue()
        c.register_listener(q)

        event = ClusterStateEvent(ws_id="ws1", state="running", node_id="node-a")
        c._on_cluster_event(event.to_json())

        fan_event = q.get_nowait()
        assert fan_event["type"] == "cluster_state"
        assert fan_event["ws_id"] == "ws1"

    def test_unregister_listener_stops_fanout(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a")

        q = queue.Queue()
        c.register_listener(q)
        c.unregister_listener(q)

        c._fanout({"type": "test"})
        assert q.empty()

    def test_invalid_json_event_ignored(self):
        c = _make_collector()
        # Should not raise
        c._on_cluster_event("not valid json {{{")
        c._on_cluster_event("")


class TestCollectorQueries:
    """Query methods: get_overview, get_nodes, get_workstreams, get_node_detail."""

    @pytest.fixture()
    def populated_collector(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={
                "ws1": {
                    "id": "ws1",
                    "name": "alpha",
                    "state": "running",
                    "node": "node-a",
                    "title": "Task A",
                    "tokens": 5000,
                    "context_ratio": 0.2,
                    "activity": "",
                    "activity_state": "",
                    "tool_calls": 10,
                },
                "ws2": {
                    "id": "ws2",
                    "name": "beta",
                    "state": "idle",
                    "node": "node-a",
                    "title": "Task B",
                    "tokens": 2000,
                    "context_ratio": 0.1,
                    "activity": "",
                    "activity_state": "",
                    "tool_calls": 5,
                },
            },
            aggregate={"total_tokens": 7000, "total_tool_calls": 15},
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b",
            server_url="http://b:8080",
            workstreams={
                "ws3": {
                    "id": "ws3",
                    "name": "gamma",
                    "state": "attention",
                    "node": "node-b",
                    "title": "Task C",
                    "tokens": 10000,
                    "context_ratio": 0.5,
                    "activity": "awaiting approval",
                    "activity_state": "approval",
                    "tool_calls": 20,
                },
            },
            aggregate={"total_tokens": 10000, "total_tool_calls": 20},
        )
        return c

    def test_get_overview(self, populated_collector):
        o = populated_collector.get_overview()
        assert o["nodes"] == 2
        assert o["workstreams"] == 3
        assert o["states"]["running"] == 1
        assert o["states"]["idle"] == 1
        assert o["states"]["attention"] == 1
        assert o["aggregate"]["total_tokens"] == 17000
        assert o["aggregate"]["total_tool_calls"] == 35

    def test_get_nodes_sorted_by_activity(self, populated_collector):
        nodes, total = populated_collector.get_nodes(sort_by="activity")
        assert total == 2
        # node-a has 1 running, node-b has 1 attention — both have activity=1
        # order depends on tie-breaking but both should be present
        ids = [n["node_id"] for n in nodes]
        assert "node-a" in ids
        assert "node-b" in ids

    def test_get_nodes_pagination(self, populated_collector):
        nodes, total = populated_collector.get_nodes(limit=1, offset=0)
        assert len(nodes) == 1
        assert total == 2

        nodes2, _ = populated_collector.get_nodes(limit=1, offset=1)
        assert len(nodes2) == 1
        assert nodes2[0]["node_id"] != nodes[0]["node_id"]

    def test_get_workstreams_no_filter(self, populated_collector):
        ws, total = populated_collector.get_workstreams()
        assert total == 3
        assert len(ws) == 3

    def test_get_workstreams_filter_by_state(self, populated_collector):
        ws, total = populated_collector.get_workstreams(state="running")
        assert total == 1
        assert ws[0]["name"] == "alpha"

    def test_get_workstreams_filter_by_node(self, populated_collector):
        ws, total = populated_collector.get_workstreams(node="node-b")
        assert total == 1
        assert ws[0]["name"] == "gamma"

    def test_get_workstreams_filter_by_search(self, populated_collector):
        ws, total = populated_collector.get_workstreams(search="Task C")
        assert total == 1
        assert ws[0]["name"] == "gamma"

    def test_get_workstreams_search_case_insensitive(self, populated_collector):
        ws, total = populated_collector.get_workstreams(search="task c")
        assert total == 1

    def test_get_workstreams_pagination(self, populated_collector):
        ws, total = populated_collector.get_workstreams(page=1, per_page=2)
        assert len(ws) == 2
        assert total == 3

        ws2, _ = populated_collector.get_workstreams(page=2, per_page=2)
        assert len(ws2) == 1

    def test_get_workstreams_sorted_by_state(self, populated_collector):
        ws, _ = populated_collector.get_workstreams(sort_by="state")
        states = [w["state"] for w in ws]
        # running before attention before idle
        assert states.index("running") < states.index("attention") < states.index("idle")

    def test_get_workstreams_combined_filters(self, populated_collector):
        ws, total = populated_collector.get_workstreams(state="idle", node="node-a")
        assert total == 1
        assert ws[0]["name"] == "beta"

    def test_get_node_detail_found(self, populated_collector):
        detail = populated_collector.get_node_detail("node-a")
        assert detail is not None
        assert detail["node_id"] == "node-a"
        assert len(detail["workstreams"]) == 2

    def test_get_node_detail_not_found(self, populated_collector):
        assert populated_collector.get_node_detail("nonexistent") is None


# ---------------------------------------------------------------------------
# ClusterStateEvent protocol tests
# ---------------------------------------------------------------------------


class TestClusterStateEventProtocol:
    """Ensure ClusterStateEvent round-trips through JSON correctly."""

    def test_round_trip(self):
        event = ClusterStateEvent(
            ws_id="ws1",
            state="running",
            node_id="node-a",
            tokens=5000,
            context_ratio=0.25,
            activity="bash: ls",
            activity_state="tool",
        )
        raw = event.to_json()
        data = json.loads(raw)
        assert data["type"] == "cluster_state"
        assert data["ws_id"] == "ws1"
        assert data["node_id"] == "node-a"
        assert data["tokens"] == 5000
        assert data["context_ratio"] == 0.25

    def test_from_json(self):
        from turnstone.mq.protocol import OutboundEvent

        raw = json.dumps(
            {
                "type": "cluster_state",
                "ws_id": "ws1",
                "state": "running",
                "node_id": "node-a",
                "tokens": 5000,
            }
        )
        event = OutboundEvent.from_json(raw)
        assert isinstance(event, ClusterStateEvent)
        assert event.node_id == "node-a"
        assert event.tokens == 5000


# ---------------------------------------------------------------------------
# Console HTTP server tests
# ---------------------------------------------------------------------------


class TestConsoleHTTPEndpoints:
    """Test console HTTP API endpoints with a mock collector."""

    @pytest.fixture()
    def mock_collector(self):
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 3,
            "workstreams": 15,
            "states": {
                "running": 5,
                "thinking": 2,
                "attention": 1,
                "idle": 6,
                "error": 1,
            },
            "aggregate": {"total_tokens": 50000, "total_tool_calls": 200},
        }
        collector.get_nodes.return_value = (
            [
                {
                    "node_id": "node-a",
                    "ws_total": 5,
                    "ws_running": 3,
                    "total_tokens": 20000,
                }
            ],
            1,
        )
        collector.get_workstreams.return_value = (
            [{"id": "ws1", "name": "test", "state": "running", "node": "node-a"}],
            1,
        )
        collector.get_node_detail.return_value = {
            "node_id": "node-a",
            "server_url": "http://a:8080",
            "health": {},
            "workstreams": [],
            "aggregate": {},
        }
        return collector

    @pytest.fixture()
    def client(self, mock_collector):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app

        _load_static()

        from turnstone.core.auth import AuthConfig

        app = create_app(
            collector=mock_collector,
            broker=MagicMock(),
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def _get(self, client, path):
        resp = client.get(path)
        return resp.status_code, resp.json()

    def _get_raw(self, client, path):
        resp = client.get(path)
        return resp.status_code, resp.text, resp.headers.get("content-type")

    def test_get_overview(self, client, mock_collector):
        status, data = self._get(client, "/v1/api/cluster/overview")
        assert status == 200
        assert data["nodes"] == 3
        assert data["workstreams"] == 15
        assert data["states"]["running"] == 5
        mock_collector.get_overview.assert_called_once()

    def test_get_nodes(self, client, mock_collector):
        status, data = self._get(client, "/v1/api/cluster/nodes?sort=activity&limit=10&offset=0")
        assert status == 200
        assert len(data["nodes"]) == 1
        assert data["total"] == 1
        mock_collector.get_nodes.assert_called_once_with(sort_by="activity", limit=10, offset=0)

    def test_get_workstreams(self, client, mock_collector):
        status, data = self._get(
            client, "/v1/api/cluster/workstreams?state=running&page=1&per_page=25"
        )
        assert status == 200
        assert len(data["workstreams"]) == 1
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["pages"] == 1
        mock_collector.get_workstreams.assert_called_once_with(
            state="running",
            node=None,
            search=None,
            sort_by="state",
            page=1,
            per_page=25,
        )

    def test_get_workstreams_per_page_capped(self, client, mock_collector):
        self._get(client, "/v1/api/cluster/workstreams?per_page=999")
        call_kwargs = mock_collector.get_workstreams.call_args
        assert call_kwargs.kwargs["per_page"] == 200

    def test_get_node_detail(self, client, mock_collector):
        status, data = self._get(client, "/v1/api/cluster/node/node-a")
        assert status == 200
        assert data["node_id"] == "node-a"
        mock_collector.get_node_detail.assert_called_once_with("node-a")

    def test_get_node_detail_not_found(self, client, mock_collector):
        mock_collector.get_node_detail.return_value = None
        status, data = self._get(client, "/v1/api/cluster/node/nonexistent")
        assert status == 404
        assert "error" in data

    def test_health_endpoint(self, client, mock_collector):
        status, data = self._get(client, "/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["service"] == "turnstone-console"
        assert data["nodes"] == 3

    def test_index_html(self, client):
        status, body, ct = self._get_raw(client, "/")
        assert status == 200
        assert "text/html" in ct
        assert "turnstone console" in body

    def test_static_css(self, client):
        status, body, ct = self._get_raw(client, "/static/style.css")
        assert status == 200
        assert "text/css" in ct

    def test_static_js(self, client):
        status, body, ct = self._get_raw(client, "/static/app.js")
        assert status == 200
        assert "javascript" in ct

    def test_404(self, client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404

    def test_index_has_new_ws_button(self, client):
        status, body, ct = self._get_raw(client, "/")
        assert status == 200
        assert 'id="new-ws-btn"' in body
        assert "showNewWsModal" in body

    def test_index_has_new_ws_modal(self, client):
        status, body, ct = self._get_raw(client, "/")
        assert 'id="new-ws-overlay"' in body
        assert 'id="new-ws-node"' in body


# ---------------------------------------------------------------------------
# Version tracking / drift detection
# ---------------------------------------------------------------------------


class TestCollectorVersionInfo:
    """Version extraction and drift detection."""

    def test_get_overview_no_drift(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a", health={"status": "ok", "version": "0.3.0"}
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b", health={"status": "ok", "version": "0.3.0"}
        )
        overview = c.get_overview()
        assert overview["version_drift"] is False
        assert overview["versions"] == ["0.3.0"]

    def test_get_overview_drift_detected(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a", health={"status": "ok", "version": "0.3.0"}
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b", health={"status": "ok", "version": "0.3.1"}
        )
        overview = c.get_overview()
        assert overview["version_drift"] is True
        assert sorted(overview["versions"]) == ["0.3.0", "0.3.1"]

    def test_get_overview_no_version_in_health(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", health={"status": "ok"})
        overview = c.get_overview()
        assert overview["version_drift"] is False
        assert overview["versions"] == []

    def test_get_overview_single_node_no_drift(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", health={"version": "0.3.0"})
        overview = c.get_overview()
        assert overview["version_drift"] is False
        assert overview["versions"] == ["0.3.0"]

    def test_get_nodes_includes_version(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            health={"status": "ok", "version": "0.3.0"},
        )
        nodes, _ = c.get_nodes()
        assert nodes[0]["version"] == "0.3.0"

    def test_get_nodes_version_empty_when_missing(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080", health={})
        nodes, _ = c.get_nodes()
        assert nodes[0]["version"] == ""

    def test_get_version_info(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", health={"version": "0.3.0"})
        c._nodes["node-b"] = NodeSnapshot(node_id="node-b", health={"version": "0.3.1"})
        info = c.get_version_info()
        assert info["drift"] is True
        assert info["versions"]["node-a"] == "0.3.0"
        assert info["versions"]["node-b"] == "0.3.1"
        assert sorted(info["unique_versions"]) == ["0.3.0", "0.3.1"]

    def test_get_version_info_no_drift(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", health={"version": "0.3.0"})
        c._nodes["node-b"] = NodeSnapshot(node_id="node-b", health={"version": "0.3.0"})
        info = c.get_version_info()
        assert info["drift"] is False
        assert info["unique_versions"] == ["0.3.0"]


# ---------------------------------------------------------------------------
# Workstream creation tests
# ---------------------------------------------------------------------------


class TestConsoleWorkstreamCreation:
    """Tests for POST /v1/api/cluster/workstreams/new."""

    @pytest.fixture()
    def mock_collector(self):
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 2,
            "workstreams": 5,
            "states": {"running": 1, "idle": 4, "thinking": 0, "attention": 0, "error": 0},
            "aggregate": {"total_tokens": 0, "total_tool_calls": 0},
        }
        collector.get_node_detail.return_value = {
            "node_id": "node-a",
            "server_url": "http://a:8080",
            "health": {},
            "workstreams": [],
            "aggregate": {},
            "reachable": True,
        }
        collector.get_nodes.return_value = (
            [
                {"node_id": "node-a", "reachable": True, "max_ws": 10, "ws_total": 8},
                {"node_id": "node-b", "reachable": True, "max_ws": 10, "ws_total": 3},
            ],
            2,
        )
        return collector

    @pytest.fixture()
    def client_and_broker(self, mock_collector):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import AuthConfig

        _load_static()
        mock_broker = MagicMock()
        app = create_app(
            collector=mock_collector,
            broker=mock_broker,
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client, mock_broker
        client.close()

    def test_create_with_explicit_node(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "name": "test-ws"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["target_node"] == "node-a"
        assert "correlation_id" in data
        broker.push_inbound.assert_called_once()
        # Verify the pushed message
        msg_json = broker.push_inbound.call_args[0][0]
        msg = json.loads(msg_json)
        assert msg["type"] == "create_workstream"
        assert msg["target_node"] == "node-a"
        assert msg["name"] == "test-ws"

    def test_create_with_model(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "model": "gpt-5"},
        )
        assert resp.status_code == 200
        msg_json = broker.push_inbound.call_args[0][0]
        msg = json.loads(msg_json)
        assert msg["model"] == "gpt-5"

    def test_create_with_initial_message_directed(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "initial_message": "Do the thing"},
        )
        assert resp.status_code == 200
        msg = json.loads(broker.push_inbound.call_args[0][0])
        assert msg["initial_message"] == "Do the thing"

    def test_create_with_initial_message_pool(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool", "initial_message": "Pool task"},
        )
        assert resp.status_code == 200
        msg = json.loads(broker.push_inbound.call_args[0][0])
        assert msg["initial_message"] == "Pool task"

    def test_create_auto_selects_best_node(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"name": "auto-test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # node-b has more headroom (10-3=7 vs 10-8=2)
        assert data["target_node"] == "node-b"

    def test_create_no_reachable_nodes(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        mock_collector.get_nodes.return_value = ([], 0)
        resp = client.post("/v1/api/cluster/workstreams/new", json={})
        assert resp.status_code == 503
        assert "No reachable nodes" in resp.json()["error"]

    def test_create_unknown_node(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        mock_collector.get_node_detail.return_value = None
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "nonexistent"},
        )
        assert resp.status_code == 404

    def test_create_invalid_json(self, client_and_broker):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_create_pushes_to_directed_queue(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a"},
        )
        assert resp.status_code == 200
        # Verify push_inbound called with node_id kwarg
        call_kwargs = broker.push_inbound.call_args
        assert call_kwargs[1]["node_id"] == "node-a"

    def test_create_pool_pushes_to_shared_queue(self, client_and_broker, mock_collector):
        client, broker = client_and_broker
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool", "name": "pool-task"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["target_node"] == "pool"
        broker.push_inbound.assert_called_once()
        # Shared queue: no node_id kwarg (or empty)
        call_args = broker.push_inbound.call_args
        assert call_args[1].get("node_id", "") == ""
        # Message should have no target_node
        msg = json.loads(call_args[0][0])
        assert msg["type"] == "create_workstream"
        assert msg["target_node"] == ""
        assert msg["name"] == "pool-task"

    def test_create_pool_skips_node_validation(self, client_and_broker, mock_collector):
        """Pool mode doesn't need a valid node_id — it goes to the shared queue."""
        client, broker = client_and_broker
        mock_collector.get_node_detail.return_value = None  # would 404 for directed
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool"},
        )
        assert resp.status_code == 200
        assert resp.json()["target_node"] == "pool"


# ---------------------------------------------------------------------------
# Proxy tests
# ---------------------------------------------------------------------------


class TestConsoleProxy:
    """Tests for /node/{node_id}/ reverse proxy."""

    @pytest.fixture()
    def mock_collector(self):
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 1,
            "workstreams": 2,
            "states": {"running": 0, "idle": 2, "thinking": 0, "attention": 0, "error": 0},
            "aggregate": {"total_tokens": 0, "total_tool_calls": 0},
        }
        collector.get_node_detail.return_value = {
            "node_id": "node-a",
            "server_url": "http://a:8080",
            "health": {},
            "workstreams": [],
            "aggregate": {},
            "reachable": True,
        }
        return collector

    @pytest.fixture()
    def client(self, mock_collector):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import AuthConfig

        _load_static()
        app = create_app(
            collector=mock_collector,
            broker=MagicMock(),
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_proxy_unknown_node_returns_404(self, client, mock_collector):
        mock_collector.get_node_detail.return_value = None
        resp = client.get("/node/unknown/")
        assert resp.status_code == 404

    def test_proxy_static_unknown_node_returns_404(self, client, mock_collector):
        mock_collector.get_node_detail.return_value = None
        resp = client.get("/node/unknown/static/app.js")
        assert resp.status_code == 404

    def test_proxy_api_unknown_node_returns_404(self, client, mock_collector):
        mock_collector.get_node_detail.return_value = None
        resp = client.get("/node/unknown/api/workstreams")
        assert resp.status_code == 404

    def test_proxy_api_post_unknown_node_returns_404(self, client, mock_collector):
        mock_collector.get_node_detail.return_value = None
        resp = client.post(
            "/node/unknown/api/send",
            json={"message": "hello", "ws_id": "ws1"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Proxy URL rewriting unit tests (no HTTP needed)
# ---------------------------------------------------------------------------


class TestProxyRewriting:
    """Test the JS shim and HTML rewriting logic."""

    def test_js_shim_contains_prefix_placeholder(self):
        from turnstone.console.server import _JS_PROXY_SHIM

        assert "PREFIX_PLACEHOLDER" in _JS_PROXY_SHIM
        replaced = _JS_PROXY_SHIM.replace("PREFIX_PLACEHOLDER", "/node/my-node")
        assert "/node/my-node" in replaced
        assert "PREFIX_PLACEHOLDER" not in replaced

    def test_js_shim_overrides_fetch_and_eventsource(self):
        from turnstone.console.server import _JS_PROXY_SHIM

        assert "window.fetch" in _JS_PROXY_SHIM
        assert "window.EventSource" in _JS_PROXY_SHIM

    def test_console_banner_contains_placeholder(self):
        from turnstone.console.server import _CONSOLE_BANNER_TEMPLATE

        assert "NODE_ID_PLACEHOLDER" in _CONSOLE_BANNER_TEMPLATE
        assert "Console" in _CONSOLE_BANNER_TEMPLATE

    def test_html_rewriting_changes_static_paths(self):
        """Simulate the proxy_index rewriting logic."""
        sample_html = (
            '<link rel="stylesheet" href="/static/style.css">\n'
            '<script src="/static/app.js"></script>'
        )
        prefix = "/node/test-node"
        rewritten = sample_html.replace('href="/static/', f'href="{prefix}/static/')
        rewritten = rewritten.replace('src="/static/', f'src="{prefix}/static/')
        assert "/node/test-node/static/style.css" in rewritten
        assert "/node/test-node/static/app.js" in rewritten
        # Originals should be gone
        assert 'href="/static/' not in rewritten
        assert 'src="/static/' not in rewritten

    def test_banner_injection_after_body(self):
        """Simulate the banner injection logic."""
        from turnstone.console.server import _CONSOLE_BANNER_TEMPLATE

        sample_html = "<html><body><div>content</div></body></html>"
        banner = _CONSOLE_BANNER_TEMPLATE.replace("NODE_ID_PLACEHOLDER", "node-a")
        result = sample_html.replace("<body>", "<body>" + banner, 1)
        assert "node-a" in result
        assert "Console" in result
        assert result.startswith("<html><body><div")


# ---------------------------------------------------------------------------
# _pick_best_node unit tests
# ---------------------------------------------------------------------------


class TestPickBestNode:
    """Test the _pick_best_node helper."""

    def test_picks_node_with_most_headroom(self):
        from turnstone.console.server import _pick_best_node

        collector = MagicMock(spec=ClusterCollector)
        collector.get_nodes.return_value = (
            [
                {"node_id": "busy", "reachable": True, "max_ws": 10, "ws_total": 9},
                {"node_id": "free", "reachable": True, "max_ws": 10, "ws_total": 2},
                {"node_id": "mid", "reachable": True, "max_ws": 10, "ws_total": 5},
            ],
            3,
        )
        assert _pick_best_node(collector) == "free"

    def test_skips_unreachable_nodes(self):
        from turnstone.console.server import _pick_best_node

        collector = MagicMock(spec=ClusterCollector)
        collector.get_nodes.return_value = (
            [
                {"node_id": "down", "reachable": False, "max_ws": 10, "ws_total": 0},
                {"node_id": "up", "reachable": True, "max_ws": 10, "ws_total": 5},
            ],
            2,
        )
        assert _pick_best_node(collector) == "up"

    def test_returns_empty_when_no_nodes(self):
        from turnstone.console.server import _pick_best_node

        collector = MagicMock(spec=ClusterCollector)
        collector.get_nodes.return_value = ([], 0)
        assert _pick_best_node(collector) == ""

    def test_returns_empty_when_all_unreachable(self):
        from turnstone.console.server import _pick_best_node

        collector = MagicMock(spec=ClusterCollector)
        collector.get_nodes.return_value = (
            [{"node_id": "down", "reachable": False, "max_ws": 10, "ws_total": 0}],
            1,
        )
        assert _pick_best_node(collector) == ""


# ---------------------------------------------------------------------------
# Version tracking endpoint tests
# ---------------------------------------------------------------------------


class TestConsoleVersionEndpoints:
    """HTTP endpoint tests for version drift fields."""

    @pytest.fixture()
    def mock_collector(self):
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 2,
            "workstreams": 5,
            "states": {"running": 1, "thinking": 0, "attention": 0, "idle": 4, "error": 0},
            "aggregate": {"total_tokens": 10000, "total_tool_calls": 50},
            "version_drift": True,
            "versions": ["0.3.0", "0.3.1"],
        }
        return collector

    @pytest.fixture()
    def client(self, mock_collector):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import AuthConfig

        _load_static()
        app = create_app(
            collector=mock_collector,
            broker=MagicMock(),
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def _get(self, client, path):
        resp = client.get(path)
        return resp.status_code, resp.json()

    def test_overview_includes_version_drift(self, client, mock_collector):
        status, data = self._get(client, "/v1/api/cluster/overview")
        assert status == 200
        assert data["version_drift"] is True
        assert "0.3.0" in data["versions"]
        assert "0.3.1" in data["versions"]

    def test_health_includes_version_drift(self, client, mock_collector):
        status, data = self._get(client, "/health")
        assert status == 200
        assert data["version_drift"] is True
        assert "0.3.0" in data["versions"]


# ---------------------------------------------------------------------------
# Shared static serving
# ---------------------------------------------------------------------------


class TestSharedStatic:
    """Tests for /shared/ static file serving."""

    @pytest.fixture()
    def client(self):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import AuthConfig

        _load_static()
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 0,
            "workstreams": 0,
            "states": {},
            "aggregate": {},
        }
        app = create_app(
            collector=collector,
            broker=MagicMock(),
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_shared_base_css(self, client):
        resp = client.get("/shared/base.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers.get("content-type", "")

    def test_shared_utils_js(self, client):
        resp = client.get("/shared/utils.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "")

    def test_shared_auth_js(self, client):
        resp = client.get("/shared/auth.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "")

    def test_shared_toast_js(self, client):
        resp = client.get("/shared/toast.js")
        assert resp.status_code == 200

    def test_shared_theme_js(self, client):
        resp = client.get("/shared/theme.js")
        assert resp.status_code == 200

    def test_shared_kb_js(self, client):
        resp = client.get("/shared/kb.js")
        assert resp.status_code == 200

    def test_shared_nonexistent_returns_404(self, client):
        resp = client.get("/shared/nonexistent.js")
        assert resp.status_code == 404

    def test_index_imports_shared_base_css(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert '/shared/base.css"' in resp.text

    def test_index_imports_shared_scripts(self, client):
        resp = client.get("/")
        body = resp.text
        assert "/shared/utils.js" in body
        assert "/shared/toast.js" in body
        assert "/shared/theme.js" in body
        assert "/shared/auth.js" in body
        assert "/shared/kb.js" in body

    def test_shared_scripts_load_before_app_js(self, client):
        """Shared scripts must appear before page-specific app.js."""
        body = client.get("/").text
        shared_pos = body.find("/shared/utils.js")
        app_pos = body.find("/static/app.js")
        assert shared_pos < app_pos


class TestProxySharedStatic:
    """Tests for proxy rewriting of /shared/ paths."""

    def test_html_rewriting_includes_shared_paths(self):
        """Verify proxy_index rewrites /shared/ paths like /static/ paths."""
        sample_html = (
            '<link rel="stylesheet" href="/shared/base.css">\n'
            '<link rel="stylesheet" href="/static/style.css">\n'
            '<script src="/shared/utils.js"></script>\n'
            '<script src="/static/app.js"></script>'
        )
        prefix = "/node/test-node"
        rewritten = sample_html.replace('href="/static/', f'href="{prefix}/static/')
        rewritten = rewritten.replace('src="/static/', f'src="{prefix}/static/')
        rewritten = rewritten.replace('href="/shared/', f'href="{prefix}/shared/')
        rewritten = rewritten.replace('src="/shared/', f'src="{prefix}/shared/')
        assert "/node/test-node/shared/base.css" in rewritten
        assert "/node/test-node/shared/utils.js" in rewritten
        assert "/node/test-node/static/style.css" in rewritten
        assert "/node/test-node/static/app.js" in rewritten
        assert 'href="/shared/' not in rewritten
        assert 'src="/shared/' not in rewritten

    def test_proxy_shim_injected_in_html(self):
        """Verify shim is injected as inline script in proxied HTML."""
        import json

        from turnstone.console.server import _CONSOLE_BANNER_TEMPLATE, _JS_PROXY_SHIM

        sample_html = "<html><body><div>content</div></body></html>"
        prefix = "/node/test-node"
        banner = _CONSOLE_BANNER_TEMPLATE.replace("NODE_ID_PLACEHOLDER", "test-node")
        shim = (
            "<script>"
            + _JS_PROXY_SHIM.replace('"PREFIX_PLACEHOLDER"', json.dumps(prefix))
            + "</script>"
        )
        result = sample_html.replace("<body>", "<body>" + banner + shim, 1)
        assert "<script>" in result
        assert "/node/test-node" in result
        assert "window.fetch" in result
        assert "window.EventSource" in result

    def test_proxy_shared_static_unknown_node_returns_404(self):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import AuthConfig

        _load_static()
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 0,
            "workstreams": 0,
            "states": {},
            "aggregate": {},
        }
        collector.get_node_detail.return_value = None
        app = create_app(
            collector=collector,
            broker=MagicMock(),
            auth_config=AuthConfig(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/node/unknown/shared/base.css")
        assert resp.status_code == 404
        client.close()
