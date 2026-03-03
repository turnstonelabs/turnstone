"""Tests for turnstone.console — collector and HTTP server."""

import json
import queue
import threading
from unittest.mock import MagicMock

import httpx
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
    """Build a /api/dashboard-style response dict."""
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
    """Polling /api/dashboard from nodes."""

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
    def server(self, mock_collector):
        from turnstone.console.server import (
            ConsoleHTTPHandler,
            ThreadedHTTPServer,
            _load_static,
        )

        _load_static()

        from turnstone.core.auth import AuthConfig

        httpd = ThreadedHTTPServer(("127.0.0.1", 0), ConsoleHTTPHandler)
        httpd.collector = mock_collector
        httpd.auth_config = AuthConfig()  # auth disabled by default
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()

    def _get(self, server, path):
        resp = httpx.get(f"{server}{path}", timeout=5)
        return resp.status_code, resp.json()

    def _get_raw(self, server, path):
        resp = httpx.get(f"{server}{path}", timeout=5)
        return resp.status_code, resp.text, resp.headers.get("content-type")

    def test_get_overview(self, server, mock_collector):
        status, data = self._get(server, "/api/cluster/overview")
        assert status == 200
        assert data["nodes"] == 3
        assert data["workstreams"] == 15
        assert data["states"]["running"] == 5
        mock_collector.get_overview.assert_called_once()

    def test_get_nodes(self, server, mock_collector):
        status, data = self._get(server, "/api/cluster/nodes?sort=activity&limit=10&offset=0")
        assert status == 200
        assert len(data["nodes"]) == 1
        assert data["total"] == 1
        mock_collector.get_nodes.assert_called_once_with(sort_by="activity", limit=10, offset=0)

    def test_get_workstreams(self, server, mock_collector):
        status, data = self._get(
            server, "/api/cluster/workstreams?state=running&page=1&per_page=25"
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

    def test_get_workstreams_per_page_capped(self, server, mock_collector):
        self._get(server, "/api/cluster/workstreams?per_page=999")
        call_kwargs = mock_collector.get_workstreams.call_args
        assert call_kwargs.kwargs["per_page"] == 200

    def test_get_node_detail(self, server, mock_collector):
        status, data = self._get(server, "/api/cluster/node/node-a")
        assert status == 200
        assert data["node_id"] == "node-a"
        mock_collector.get_node_detail.assert_called_once_with("node-a")

    def test_get_node_detail_not_found(self, server, mock_collector):
        mock_collector.get_node_detail.return_value = None
        status, data = self._get(server, "/api/cluster/node/nonexistent")
        assert status == 404
        assert "error" in data

    def test_health_endpoint(self, server, mock_collector):
        status, data = self._get(server, "/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["service"] == "turnstone-console"
        assert data["nodes"] == 3

    def test_index_html(self, server):
        status, body, ct = self._get_raw(server, "/")
        assert status == 200
        assert "text/html" in ct
        assert "turnstone console" in body

    def test_static_css(self, server):
        status, body, ct = self._get_raw(server, "/static/style.css")
        assert status == 200
        assert "text/css" in ct

    def test_static_js(self, server):
        status, body, ct = self._get_raw(server, "/static/app.js")
        assert status == 200
        assert "javascript" in ct

    def test_404(self, server):
        resp = httpx.get(f"{server}/nonexistent", timeout=5)
        assert resp.status_code == 404
