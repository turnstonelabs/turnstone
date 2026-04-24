"""Tests for turnstone.console — collector and HTTP server."""

import asyncio
import json
import queue
from unittest.mock import MagicMock

import pytest

from turnstone.console.collector import ClusterCollector, NodeSnapshot

# Shared test auth — JWT-based
_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _test_jwt() -> str:
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    return create_jwt(
        user_id="test-console",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )


_TEST_AUTH_HEADERS = {"Authorization": f"Bearer {_test_jwt()}"}

# ---------------------------------------------------------------------------
# Mock storage for collector tests
# ---------------------------------------------------------------------------

from tests._coord_test_helpers import MockStorage  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_collector(storage=None, discovery_interval=999):
    """Create a collector for tests (discovery disabled by default)."""
    s = storage or MockStorage()
    return ClusterCollector(
        storage=s,
        discovery_interval=discovery_interval,
    )


# ---------------------------------------------------------------------------
# ClusterCollector — unit tests
# ---------------------------------------------------------------------------


class TestCollectorDiscovery:
    """Node discovery from service registry."""

    def test_discover_new_nodes(self):
        storage = MockStorage()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"},
            {"service_id": "node-b", "url": "http://b:8080", "metadata": "{}"},
        ]
        c = _make_collector(storage)
        c._discover_nodes()

        overview = c.get_overview()
        assert overview["nodes"] == 2

    def test_discover_removes_lost_nodes(self):
        storage = MockStorage()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"},
        ]
        c = _make_collector(storage)
        c._discover_nodes()
        assert c.get_overview()["nodes"] == 1

        # Node disappears
        storage.services = []
        c._discover_nodes()
        assert c.get_overview()["nodes"] == 0

    def test_discover_updates_server_url(self):
        storage = MockStorage()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"},
        ]
        c = _make_collector(storage)
        c._discover_nodes()

        storage.services = [
            {"service_id": "node-a", "url": "http://a:9090", "metadata": "{}"},
        ]
        c._discover_nodes()

        detail = c.get_node_detail("node-a")
        assert detail["server_url"] == "http://a:9090"

    def test_discover_emits_node_joined_event(self):
        storage = MockStorage()
        c = _make_collector(storage)
        q = queue.Queue()
        c.register_listener(q)

        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"},
        ]
        c._discover_nodes()

        event = q.get_nowait()
        assert event["type"] == "node_joined"
        assert event["node_id"] == "node-a"

    def test_discover_emits_node_lost_event(self):
        storage = MockStorage()
        storage.services = [
            {"service_id": "node-a", "url": "http://a:8080", "metadata": "{}"},
        ]
        c = _make_collector(storage)
        c._discover_nodes()

        q = queue.Queue()
        c.register_listener(q)

        storage.services = []
        c._discover_nodes()

        event = q.get_nowait()
        assert event["type"] == "node_lost"
        assert event["node_id"] == "node-a"

    def test_discover_parses_metadata(self):
        storage = MockStorage()
        storage.services = [
            {
                "service_id": "node-a",
                "url": "http://a:8080",
                "metadata": '{"max_ws": 20, "started": 1234567890.0}',
            },
        ]
        c = _make_collector(storage)
        c._discover_nodes()

        detail = c.get_node_detail("node-a")
        assert detail is not None
        # Verify metadata was parsed into the NodeSnapshot
        assert c._nodes["node-a"].max_ws == 20
        assert c._nodes["node-a"].started == 1234567890.0


class TestCollectorSnapshot:
    """Applying node_snapshot SSE events."""

    def test_apply_snapshot_populates_workstreams(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [
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
                "health": {"status": "ok"},
                "aggregate": {"total_tokens": 1000, "total_tool_calls": 3},
            },
        )

        detail = c.get_node_detail("node-a")
        assert len(detail["workstreams"]) == 1
        assert detail["workstreams"][0]["name"] == "test"
        assert detail["workstreams"][0]["node"] == "node-a"
        assert detail["workstreams"][0]["server_url"] == "http://a:8080"
        assert detail["health"]["status"] == "ok"

    def test_apply_snapshot_replaces_stale_workstreams(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"old-ws": {"id": "old-ws", "name": "old", "state": "idle"}},
        )

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [{"id": "new-ws", "name": "new", "state": "running"}],
                "health": {},
                "aggregate": {},
            },
        )

        detail = c.get_node_detail("node-a")
        assert len(detail["workstreams"]) == 1
        assert detail["workstreams"][0]["id"] == "new-ws"

    def test_apply_snapshot_ignores_unknown_node(self):
        c = _make_collector()
        # Should not raise
        c._apply_snapshot(
            "unknown", {"type": "node_snapshot", "workstreams": [], "health": {}, "aggregate": {}}
        )

    def test_apply_snapshot_emits_ws_created_for_new_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [{"id": "ws1", "name": "new-task", "state": "idle"}],
                "health": {},
                "aggregate": {},
            },
        )

        event = q.get_nowait()
        assert event["type"] == "ws_created"
        assert event["ws_id"] == "ws1"
        assert event["name"] == "new-task"
        assert event["node_id"] == "node-a"

    def test_apply_snapshot_emits_ws_closed_for_removed_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "old", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [],
                "health": {},
                "aggregate": {},
            },
        )

        event = q.get_nowait()
        assert event["type"] == "ws_closed"
        assert event["ws_id"] == "ws1"

    def test_apply_snapshot_no_events_when_unchanged(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "same", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [{"id": "ws1", "name": "same", "state": "idle"}],
                "health": {},
                "aggregate": {},
            },
        )

        assert q.empty()

    def test_apply_snapshot_emits_state_change_as_cluster_state(self):
        """State change events must use type 'cluster_state' for the frontend."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "same", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [{"id": "ws1", "name": "same", "state": "running"}],
                "health": {},
                "aggregate": {},
            },
        )

        event = q.get_nowait()
        assert event["type"] == "cluster_state"
        assert event["ws_id"] == "ws1"
        assert event["state"] == "running"

    def test_apply_snapshot_skips_empty_id_workstream(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_snapshot(
            "node-a",
            {
                "type": "node_snapshot",
                "node_id": "node-a",
                "workstreams": [{"name": "no-id", "state": "idle"}],
                "health": {},
                "aggregate": {},
            },
        )

        assert q.empty()
        assert len(c._nodes["node-a"].workstreams) == 0


class TestCollectorDelta:
    """Applying individual SSE delta events."""

    def test_apply_delta_ws_state_fans_out_as_cluster_state(self):
        """Server emits ws_state; collector must translate to cluster_state."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "test", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_delta(
            "node-a", {"type": "ws_state", "ws_id": "ws1", "state": "running", "tokens": 500}
        )

        event = q.get_nowait()
        assert event["type"] == "cluster_state"
        assert event["state"] == "running"
        # Verify in-memory state was updated
        assert c._nodes["node-a"].workstreams["ws1"]["state"] == "running"

    def test_apply_delta_ws_created(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_delta("node-a", {"type": "ws_created", "ws_id": "ws1", "name": "new"})

        event = q.get_nowait()
        assert event["type"] == "ws_created"
        assert "ws1" in c._nodes["node-a"].workstreams

    def test_apply_delta_ws_closed(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "old", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_delta("node-a", {"type": "ws_closed", "ws_id": "ws1"})

        event = q.get_nowait()
        assert event["type"] == "ws_closed"
        assert "ws1" not in c._nodes["node-a"].workstreams

    def test_apply_delta_ws_rename(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            workstreams={"ws1": {"id": "ws1", "name": "old-name", "state": "idle"}},
        )
        q: queue.Queue[dict] = queue.Queue()
        c.register_listener(q)

        c._apply_delta("node-a", {"type": "ws_rename", "ws_id": "ws1", "name": "new-name"})

        event = q.get_nowait()
        assert event["type"] == "ws_rename"
        assert event["name"] == "new-name"
        assert c._nodes["node-a"].workstreams["ws1"]["name"] == "new-name"

    def test_apply_delta_health_changed(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            health={"status": "ok", "backend": {"status": "up"}},
        )

        c._apply_delta("node-a", {"type": "health_changed", "backend_status": "degraded"})

        health = c._nodes["node-a"].health
        assert health["backend"]["status"] == "down"
        assert health["status"] == "degraded"

    def test_apply_delta_aggregate(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")

        c._apply_delta(
            "node-a",
            {"type": "aggregate", "total_tokens": 5000, "total_tool_calls": 42, "active_count": 3},
        )

        assert c._nodes["node-a"].aggregate["total_tokens"] == 5000
        assert c._nodes["node-a"].aggregate["total_tool_calls"] == 42

    def test_mark_unreachable_preserves_workstreams(self):
        """Disconnection marks unreachable but preserves workstream data."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            reachable=True,
            workstreams={"ws1": {"id": "ws1", "name": "existing", "state": "idle"}},
        )

        c._mark_unreachable("node-a")

        assert c._nodes["node-a"].reachable is False
        assert "ws1" in c._nodes["node-a"].workstreams
        assert c._nodes["node-a"].workstreams["ws1"]["name"] == "existing"


class TestCollectorFanout:
    """SSE fan-out to registered listeners."""

    def test_unregister_listener_stops_fanout(self):
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(node_id="node-a")

        q = queue.Queue()
        c.register_listener(q)
        c.unregister_listener(q)

        c._fanout({"type": "test"})
        assert q.empty()


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

    def test_get_snapshot_empty(self):
        c = _make_collector()
        snap = c.get_snapshot()
        assert snap["nodes"] == []
        assert snap["overview"]["nodes"] == 0
        assert snap["overview"]["workstreams"] == 0
        assert snap["overview"]["states"]["running"] == 0
        assert "timestamp" in snap

    def test_get_snapshot_with_nodes(self, populated_collector):
        snap = populated_collector.get_snapshot()
        assert len(snap["nodes"]) == 2
        assert snap["overview"]["nodes"] == 2
        assert snap["overview"]["workstreams"] == 3
        assert snap["overview"]["states"]["running"] == 1
        assert snap["overview"]["states"]["attention"] == 1
        assert snap["overview"]["states"]["idle"] == 1
        assert snap["overview"]["aggregate"]["total_tokens"] == 17000
        assert snap["timestamp"] > 0
        # Each node should embed its workstreams
        node_ids = {n["node_id"] for n in snap["nodes"]}
        assert node_ids == {"node-a", "node-b"}
        for n in snap["nodes"]:
            if n["node_id"] == "node-a":
                assert len(n["workstreams"]) == 2
            elif n["node_id"] == "node-b":
                assert len(n["workstreams"]) == 1

    def test_get_snapshot_consistency(self, populated_collector):
        """Snapshot overview should match get_overview()."""
        snap = populated_collector.get_snapshot()
        overview = populated_collector.get_overview()
        assert snap["overview"]["nodes"] == overview["nodes"]
        assert snap["overview"]["workstreams"] == overview["workstreams"]
        assert snap["overview"]["states"] == overview["states"]
        assert snap["overview"]["aggregate"] == overview["aggregate"]
        assert snap["overview"]["version_drift"] == overview["version_drift"]


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
        collector.get_snapshot.return_value = {
            "nodes": [
                {
                    "node_id": "node-a",
                    "server_url": "http://a:8080",
                    "max_ws": 10,
                    "reachable": True,
                    "version": "0.5.0",
                    "health": {},
                    "aggregate": {"total_tokens": 50000, "total_tool_calls": 200},
                    "workstreams": [
                        {"id": "ws1", "name": "test", "state": "running", "node": "node-a"},
                    ],
                },
            ],
            "overview": {
                "nodes": 3,
                "workstreams": 15,
                "states": {"running": 5, "thinking": 2, "attention": 1, "idle": 6, "error": 1},
                "aggregate": {"total_tokens": 50000, "total_tool_calls": 200},
                "version_drift": False,
                "versions": ["0.5.0"],
            },
            "timestamp": 1234567890.0,
        }
        return collector

    @pytest.fixture()
    def client(self, mock_collector):
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app

        _load_static()

        app = create_app(
            collector=mock_collector,
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
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
        mock_collector.get_nodes.assert_called_once_with(
            sort_by="activity", limit=10, offset=0, node_ids=None
        )

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
            extra_rows=[],
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

    def test_get_snapshot(self, client, mock_collector):
        status, data = self._get(client, "/v1/api/cluster/snapshot")
        assert status == 200
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["node_id"] == "node-a"
        assert data["overview"]["nodes"] == 3
        assert data["overview"]["workstreams"] == 15
        assert data["timestamp"] == 1234567890.0
        mock_collector.get_snapshot.assert_called_once()

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
    """Tests for POST /v1/api/cluster/workstreams/new (HTTP dispatch)."""

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
        # get_all_nodes delegates to get_nodes (mirrors real implementation)
        collector.get_all_nodes.side_effect = lambda: collector.get_nodes.return_value[0]
        return collector

    @pytest.fixture()
    def client_and_mock(self, mock_collector):
        """Returns (TestClient, mock_proxy_post) where mock_proxy_post is the
        patched proxy_client.post that captures outgoing HTTP calls."""
        import httpx
        from starlette.testclient import TestClient

        from turnstone.console.server import _load_static, create_app

        _load_static()
        app = create_app(
            collector=mock_collector,
            jwt_secret=_TEST_JWT_SECRET,
        )

        # Set up a mock proxy_client (lifespan doesn't run in TestClient)
        async def _mock_post(*args, **kwargs):
            return httpx.Response(
                200,
                json={"ws_id": "ws_new_123", "name": "test"},
                request=httpx.Request("POST", args[0] if args else "http://test"),
            )

        mock_post = MagicMock(side_effect=_mock_post)
        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = mock_post
        app.state.proxy_client = mock_proxy

        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
        yield client, mock_post
        client.close()

    def test_create_with_explicit_node(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "name": "test-ws"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["target_node"] == "node-a"
        assert "correlation_id" in data
        mock_post.assert_called_once()
        # Verify the HTTP call was to the right node
        call_args = mock_post.call_args
        assert "http://a:8080/v1/api/workstreams/new" in call_args[0]
        body = call_args[1]["json"]
        assert body["name"] == "test-ws"

    def test_create_with_model(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "model": "gpt-5"},
        )
        assert resp.status_code == 200
        body = mock_post.call_args[1]["json"]
        assert body["model"] == "gpt-5"

    def test_create_with_initial_message_directed(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "initial_message": "Do the thing"},
        )
        assert resp.status_code == 200
        body = mock_post.call_args[1]["json"]
        assert body["initial_message"] == "Do the thing"

    def test_create_with_initial_message_pool(self, client_and_mock, mock_collector):
        """Pool mode picks the best node and dispatches via HTTP."""
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool", "initial_message": "Pool task"},
        )
        assert resp.status_code == 200
        body = mock_post.call_args[1]["json"]
        assert body["initial_message"] == "Pool task"

    def test_create_auto_selects_best_node(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"name": "auto-test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # node-b has more headroom (10-3=7 vs 10-8=2)
        assert data["target_node"] == "node-b"

    def test_create_no_reachable_nodes(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        mock_collector.get_nodes.return_value = ([], 0)
        resp = client.post("/v1/api/cluster/workstreams/new", json={})
        assert resp.status_code == 503
        assert "No reachable nodes" in resp.json()["error"]

    def test_create_unknown_node(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        mock_collector.get_node_detail.return_value = None
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "nonexistent"},
        )
        assert resp.status_code == 404

    def test_create_invalid_json(self, client_and_mock):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_create_dispatches_to_correct_node_url(self, client_and_mock, mock_collector):
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a"},
        )
        assert resp.status_code == 200
        call_args = mock_post.call_args
        assert "http://a:8080/v1/api/workstreams/new" in call_args[0]

    def test_create_pool_picks_best_node(self, client_and_mock, mock_collector):
        """Pool mode dispatches to the best available node."""
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool", "name": "pool-task"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        # Pool picks best node (node-b has most headroom)
        assert data["target_node"] == "node-b"

    def test_create_pool_no_nodes_returns_503(self, client_and_mock, mock_collector):
        """Pool mode with no reachable nodes returns 503."""
        client, mock_post = client_and_mock
        mock_collector.get_nodes.return_value = ([], 0)
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "pool"},
        )
        assert resp.status_code == 503

    def test_create_with_resume_ws_directed(self, client_and_mock, mock_collector):
        """resume_ws is forwarded in directed dispatch."""
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "resume_ws": "old-ws-id-123"},
        )
        assert resp.status_code == 200
        body = mock_post.call_args[1]["json"]
        assert body["resume_ws"] == "old-ws-id-123"

    def test_create_with_resume_ws_auto(self, client_and_mock, mock_collector):
        """resume_ws is forwarded in auto-select dispatch."""
        client, mock_post = client_and_mock
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"resume_ws": "old-ws-id-789"},
        )
        assert resp.status_code == 200
        body = mock_post.call_args[1]["json"]
        assert body["resume_ws"] == "old-ws-id-789"


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

        _load_static()
        app = create_app(
            collector=mock_collector,
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
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

    @staticmethod
    def _mock_collector(nodes: list) -> MagicMock:
        collector = MagicMock(spec=ClusterCollector)
        collector.get_nodes.return_value = (nodes, len(nodes))
        collector.get_all_nodes.side_effect = lambda: collector.get_nodes.return_value[0]
        return collector

    def test_picks_node_with_most_headroom(self):
        from turnstone.console.server import _pick_best_node

        collector = self._mock_collector(
            [
                {"node_id": "busy", "reachable": True, "max_ws": 10, "ws_total": 9},
                {"node_id": "free", "reachable": True, "max_ws": 10, "ws_total": 2},
                {"node_id": "mid", "reachable": True, "max_ws": 10, "ws_total": 5},
            ]
        )
        assert _pick_best_node(collector) == "free"

    def test_skips_unreachable_nodes(self):
        from turnstone.console.server import _pick_best_node

        collector = self._mock_collector(
            [
                {"node_id": "down", "reachable": False, "max_ws": 10, "ws_total": 0},
                {"node_id": "up", "reachable": True, "max_ws": 10, "ws_total": 5},
            ]
        )
        assert _pick_best_node(collector) == "up"

    def test_returns_empty_when_no_nodes(self):
        from turnstone.console.server import _pick_best_node

        collector = self._mock_collector([])
        assert _pick_best_node(collector) == ""

    def test_returns_empty_when_all_unreachable(self):
        from turnstone.console.server import _pick_best_node

        collector = self._mock_collector(
            [
                {"node_id": "down", "reachable": False, "max_ws": 10, "ws_total": 0},
            ]
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

        _load_static()
        app = create_app(
            collector=mock_collector,
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
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
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
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
        assert "/shared/base.css?v=" in resp.text

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

    def test_index_cache_control_no_cache(self, client):
        resp = client.get("/")
        assert resp.headers.get("cache-control") == "no-cache"

    def test_index_etag_present(self, client):
        resp = client.get("/")
        assert resp.headers.get("etag")

    def test_index_etag_304(self, client):
        resp = client.get("/")
        etag = resp.headers.get("etag")
        resp2 = client.get("/", headers={"If-None-Match": etag})
        assert resp2.status_code == 304


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
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False, headers=_TEST_AUTH_HEADERS)
        resp = client.get("/node/unknown/shared/base.css")
        assert resp.status_code == 404
        client.close()


# ---------------------------------------------------------------------------
# SSE proxy — raw byte passthrough
# ---------------------------------------------------------------------------


class TestSSEProxy:
    """Verify _proxy_sse forwards raw bytes including ping comments."""

    def test_proxy_sse_preserves_pings_and_events(self):
        """SSE proxy should forward ping comments and events verbatim."""
        from turnstone.console.server import _proxy_sse

        # Simulate an upstream SSE response with a ping comment and a real event
        sse_payload = b': ping - 2026-03-08T12:00:00Z\n\nevent: message\ndata: {"type": "test"}\n\n'

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield sse_payload

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeClient:
            def stream(self, method, url, **kwargs):
                return FakeResponse()

        class FakeRequest:
            class url:  # noqa: N801
                query = "ws_id=test123"

            class app:  # noqa: N801
                class state:  # noqa: N801
                    proxy_sse_client = FakeClient()
                    proxy_auth_token = ""

            headers = {}

            async def is_disconnected(self):
                return False

        async def _run():
            response = await _proxy_sse(
                FakeRequest(), "http://fake:8080", "events", api_prefix="v1/api"
            )
            assert response.media_type == "text/event-stream"
            # Collect the streamed bytes
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            body = b"".join(chunks)
            # Ping comment must be preserved (not filtered)
            assert b": ping" in body
            # Real event must be preserved
            assert b"event: message" in body
            assert b'"type": "test"' in body

        asyncio.run(_run())

    def test_proxy_sse_upstream_error_status(self):
        """Non-200 upstream status should yield an error event."""

        from turnstone.console.server import _proxy_sse

        class FakeResponse:
            status_code = 502

            async def aiter_bytes(self):
                return
                yield  # make it an async generator

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeClient:
            def stream(self, method, url, **kwargs):
                return FakeResponse()

        class FakeRequest:
            class url:  # noqa: N801
                query = ""

            class app:  # noqa: N801
                class state:  # noqa: N801
                    proxy_sse_client = FakeClient()
                    proxy_auth_token = ""

            headers = {}

            async def is_disconnected(self):
                return False

        async def _run():
            response = await _proxy_sse(FakeRequest(), "http://fake:8080", "events")
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            body = b"".join(chunks)
            assert b"event: error" in body
            assert b"502" in body

        asyncio.run(_run())

    def test_proxy_sse_disconnect_handling(self):
        """Proxy should stop when browser disconnects."""

        from turnstone.console.server import _proxy_sse

        class FakeResponse:
            status_code = 200

            async def aiter_bytes(self):
                yield b"data: chunk1\n\n"
                yield b"data: chunk2\n\n"  # should not be reached
                yield b"data: chunk3\n\n"

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeClient:
            def stream(self, method, url, **kwargs):
                return FakeResponse()

        call_count = 0

        class FakeRequest:
            class url:  # noqa: N801
                query = ""

            class app:  # noqa: N801
                class state:  # noqa: N801
                    proxy_sse_client = FakeClient()
                    proxy_auth_token = ""

            headers = {}

            async def is_disconnected(self):
                nonlocal call_count
                call_count += 1
                return call_count > 1  # disconnect after first chunk

        async def _run():
            response = await _proxy_sse(FakeRequest(), "http://fake:8080", "events")
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            body = b"".join(chunks)
            assert b"chunk1" in body
            # Should have stopped before chunk3
            assert b"chunk3" not in body

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Proxy auth header propagation
# ---------------------------------------------------------------------------


class TestProxyAuthHeaders:
    """Verify _proxy_auth_headers mints user-scoped JWTs for proxy requests."""

    SECRET = "test-secret-that-is-at-least-32-chars"

    def _make_request(
        self, *, auth_result=None, jwt_secret="", proxy_token_mgr=None, proxy_auth_token=""
    ):
        """Build a minimal fake request for _proxy_auth_headers."""

        class _State:
            pass

        class _AppState:
            pass

        class _App:
            state = _AppState()

        class _Request:
            state = _State()
            app = _App()

        req = _Request()
        req.state.auth_result = auth_result
        req.app.state.jwt_secret = jwt_secret
        req.app.state.proxy_token_mgr = proxy_token_mgr
        req.app.state.proxy_auth_token = proxy_auth_token
        return req

    def test_mints_user_jwt(self):
        """Real user auth_result → JWT with correct sub, scopes, src, aud, permissions."""
        import jwt as pyjwt

        from turnstone.console.server import _proxy_auth_headers
        from turnstone.core.auth import JWT_AUD_SERVER, AuthResult

        auth = AuthResult(
            user_id="alice",
            scopes=frozenset({"read", "write"}),
            token_source="jwt",
            permissions=frozenset({"admin.users"}),
        )
        req = self._make_request(auth_result=auth, jwt_secret=self.SECRET)
        headers = _proxy_auth_headers(req)

        assert "Authorization" in headers
        token = headers["Authorization"].removeprefix("Bearer ")
        payload = pyjwt.decode(token, self.SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER)
        assert payload["sub"] == "alice"
        assert set(payload["scopes"].split(",")) == {"read", "write"}
        assert payload["src"] == "console-proxy"
        assert payload["aud"] == JWT_AUD_SERVER
        assert payload["permissions"] == "admin.users"

    def test_narrows_scopes(self):
        """Read-only user → JWT carries only read scope, not full {read,write,approve}."""
        import jwt as pyjwt

        from turnstone.console.server import _proxy_auth_headers
        from turnstone.core.auth import JWT_AUD_SERVER, AuthResult

        auth = AuthResult(
            user_id="viewer",
            scopes=frozenset({"read"}),
            token_source="jwt",
        )
        req = self._make_request(auth_result=auth, jwt_secret=self.SECRET)
        headers = _proxy_auth_headers(req)

        token = headers["Authorization"].removeprefix("Bearer ")
        payload = pyjwt.decode(token, self.SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER)
        assert payload["scopes"] == "read"

    def test_short_expiry(self):
        """Minted JWT expires in 300 seconds, not hours."""
        import jwt as pyjwt

        from turnstone.console.server import _proxy_auth_headers
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="alice",
            scopes=frozenset({"read"}),
            token_source="jwt",
        )
        req = self._make_request(auth_result=auth, jwt_secret=self.SECRET)
        headers = _proxy_auth_headers(req)

        token = headers["Authorization"].removeprefix("Bearer ")
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert payload["exp"] - payload["iat"] == 300

    def test_fallback_no_user(self):
        """No auth_result → falls back to ServiceTokenManager."""
        from turnstone.console.server import _proxy_auth_headers
        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="console-proxy",
            scopes=frozenset({"read", "write", "approve"}),
            source="console",
            secret=self.SECRET,
        )
        req = self._make_request(proxy_token_mgr=mgr)
        headers = _proxy_auth_headers(req)

        assert "Authorization" in headers
        assert headers["Authorization"] == f"Bearer {mgr.token}"

    def test_fallback_no_secret(self):
        """auth_result present but empty jwt_secret → falls back to ServiceTokenManager."""
        from turnstone.console.server import _proxy_auth_headers
        from turnstone.core.auth import AuthResult, ServiceTokenManager

        auth = AuthResult(
            user_id="alice",
            scopes=frozenset({"read"}),
            token_source="jwt",
        )
        mgr = ServiceTokenManager(
            user_id="console-proxy",
            scopes=frozenset({"read", "write", "approve"}),
            source="console",
            secret=self.SECRET,
        )
        req = self._make_request(auth_result=auth, jwt_secret="", proxy_token_mgr=mgr)
        headers = _proxy_auth_headers(req)

        # Should use ServiceTokenManager, not mint a user JWT
        assert headers["Authorization"] == f"Bearer {mgr.token}"

    def test_no_mgr_no_user_returns_empty(self):
        """No auth_result, no ServiceTokenManager → empty headers."""
        from turnstone.console.server import _proxy_auth_headers

        req = self._make_request()
        headers = _proxy_auth_headers(req)

        assert headers == {}


# ---------------------------------------------------------------------------
# Server: trusted user_id forwarding on create_workstream
# ---------------------------------------------------------------------------


class TestCreateWorkstreamUserIdTrust:
    """Verify that only trusted service tokens can forward user_id in create_workstream."""

    def _extract_uid(self, body: dict, auth_result) -> str:
        """Replicate the trust check from server.py:create_workstream."""
        auth = auth_result
        uid: str = getattr(auth, "user_id", "") or ""
        trusted_sources = {"console"}
        if (
            body.get("user_id")
            and isinstance(body["user_id"], str)
            and auth is not None
            and auth.token_source in trusted_sources
        ):
            uid = body["user_id"]
        return uid

    def test_console_can_forward_user_id(self):
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="console",
            scopes=frozenset({"approve"}),
            token_source="console",
        )
        uid = self._extract_uid({"user_id": "real-user-abc"}, auth)
        assert uid == "real-user-abc"

    def test_console_service_can_forward_user_id(self):
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="console",
            scopes=frozenset({"approve"}),
            token_source="console",
        )
        uid = self._extract_uid({"user_id": "real-user-abc"}, auth)
        assert uid == "real-user-abc"

    def test_console_proxy_user_cannot_override_user_id(self):
        """End-user tokens via console-proxy must NOT override user_id."""
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="real-user-abc",
            scopes=frozenset({"read", "write"}),
            token_source="console-proxy",
        )
        uid = self._extract_uid({"user_id": "impersonated-user"}, auth)
        # Should use JWT identity, NOT the body override
        assert uid == "real-user-abc"

    def test_direct_user_cannot_override_user_id(self):
        """Direct JWT login must NOT override user_id."""
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="real-user-abc",
            scopes=frozenset({"read", "write"}),
            token_source="password",
        )
        uid = self._extract_uid({"user_id": "impersonated-user"}, auth)
        assert uid == "real-user-abc"

    def test_no_body_user_id_uses_jwt(self):
        from turnstone.core.auth import AuthResult

        auth = AuthResult(
            user_id="bridge",
            scopes=frozenset({"approve"}),
            token_source="bridge",
        )
        uid = self._extract_uid({"name": "test-ws"}, auth)
        assert uid == "bridge"


# ---------------------------------------------------------------------------
# Collector — MCP aggregation in get_overview()
# ---------------------------------------------------------------------------


class TestCollectorMCPAggregation:
    """Verify MCP server/resource/prompt aggregation in overview and snapshot."""

    def test_overview_mcp_aggregation(self):
        """Two nodes with MCP data produce correct sums in the overview."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            health={"mcp": {"servers": 2, "resources": 5, "prompts": 3}},
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b",
            server_url="http://b:8080",
            health={"mcp": {"servers": 1, "resources": 4, "prompts": 2}},
        )

        overview = c.get_overview()
        assert overview["mcp_servers"] == 3
        assert overview["mcp_resources"] == 9
        assert overview["mcp_prompts"] == 5

    def test_overview_mcp_absent_when_zero(self):
        """Nodes without MCP data produce no mcp_servers key in the overview."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            health={"status": "ok"},
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b",
            server_url="http://b:8080",
            health={},
        )

        overview = c.get_overview()
        assert "mcp_servers" not in overview
        assert "mcp_resources" not in overview
        assert "mcp_prompts" not in overview

    def test_overview_mcp_mixed_nodes(self):
        """One node with MCP, one without — only the MCP node contributes."""
        c = _make_collector()
        c._nodes["node-a"] = NodeSnapshot(
            node_id="node-a",
            server_url="http://a:8080",
            health={"mcp": {"servers": 3, "resources": 10, "prompts": 7}},
        )
        c._nodes["node-b"] = NodeSnapshot(
            node_id="node-b",
            server_url="http://b:8080",
            health={"status": "ok"},
        )

        overview = c.get_overview()
        assert overview["mcp_servers"] == 3
        assert overview["mcp_resources"] == 10
        assert overview["mcp_prompts"] == 7
