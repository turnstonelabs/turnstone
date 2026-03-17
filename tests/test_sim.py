"""Tests for the turnstone cluster simulator."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import MagicMock

import pytest

from turnstone.mq.protocol import (
    OutboundEvent,
    SendMessage,
    StateChangeEvent,
)
from turnstone.sim.config import SimConfig
from turnstone.sim.engine import SimEngine, ToolSimulationError
from turnstone.sim.metrics import MetricsCollector
from turnstone.sim.node import SimNode, SimWorkstream


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# SimConfig
# ---------------------------------------------------------------------------


class TestSimConfig:
    def test_defaults(self):
        cfg = SimConfig()
        assert cfg.num_nodes == 10
        assert cfg.scenario == "steady"
        assert cfg.llm_latency_mean == 2.0
        assert cfg.tool_failure_rate == 0.02

    def test_frozen(self):
        cfg = SimConfig()
        with pytest.raises(AttributeError):
            cfg.num_nodes = 5  # type: ignore[misc]

    def test_custom_values(self):
        cfg = SimConfig(num_nodes=100, scenario="burst", seed=42)
        assert cfg.num_nodes == 100
        assert cfg.scenario == "burst"
        assert cfg.seed == 42


# ---------------------------------------------------------------------------
# SimEngine
# ---------------------------------------------------------------------------


class TestSimEngine:
    @pytest.fixture
    def fast_config(self):
        return SimConfig(
            llm_latency_mean=0.01,
            llm_latency_stddev=0.001,
            llm_tokens_mean=20,
            llm_tokens_stddev=5,
            tool_latency_mean=0.01,
            tool_latency_stddev=0.001,
            tool_failure_rate=0.0,
            seed=42,
        )

    @pytest.fixture
    def engine(self, fast_config):
        return SimEngine(fast_config)

    def test_llm_response_returns_content(self, engine):
        async def _test():
            content, tool_calls = await engine.simulate_llm_response(True)
            assert isinstance(content, str)
            assert len(content) > 0
            assert isinstance(tool_calls, list)

        _run(_test())

    def test_llm_response_reproducible_with_seed(self, fast_config):
        async def _test():
            e1 = SimEngine(fast_config, rng=random.Random(123))
            e2 = SimEngine(fast_config, rng=random.Random(123))
            c1, t1 = await e1.simulate_llm_response(True)
            c2, t2 = await e2.simulate_llm_response(True)
            assert c1 == c2
            assert len(t1) == len(t2)

        _run(_test())

    def test_tool_execution_success(self, engine):
        async def _test():
            result = await engine.simulate_tool_execution("bash")
            assert "bash" in result
            assert "completed" in result

        _run(_test())

    def test_tool_execution_failure(self, fast_config):
        cfg = SimConfig(
            llm_latency_mean=0.01,
            tool_latency_mean=0.01,
            tool_latency_stddev=0.001,
            tool_failure_rate=1.0,  # always fail
            seed=42,
        )
        engine = SimEngine(cfg)

        async def _test():
            with pytest.raises(ToolSimulationError, match="Simulated bash failure"):
                await engine.simulate_tool_execution("bash")

        _run(_test())

    def test_generate_content(self, engine):
        content = engine._generate_content(10)
        words = content.split()
        assert len(words) == 10


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


class TestMetricsCollector:
    def test_record_and_summary(self):
        m = MetricsCollector()
        m.record_inject()
        m.record_turn("ws1", "node-0", 1.5)
        m.record_turn("ws2", "node-0", 2.5)
        m.record_turn("ws3", "node-1", 3.0)
        m.record_error("node-0", "test error")

        report = m.summary()
        assert report["total_turns"] == 3
        assert report["total_errors"] == 1
        assert report["latency"]["p50"] == 2.5
        assert report["latency"]["max"] == 3.0
        assert report["turns_per_node"]["node-0"] == 2
        assert report["turns_per_node"]["node-1"] == 1

    def test_empty_summary(self):
        m = MetricsCollector()
        report = m.summary()
        assert report["total_turns"] == 0
        assert report["latency"]["p50"] == 0

    def test_node_kill_tracking(self):
        m = MetricsCollector()
        m.record_node_kill("node-0")
        m.record_node_kill("node-1")
        report = m.summary()
        assert report["node_kills"] == 2

    def test_utilization_snapshot(self):
        m = MetricsCollector()
        m.snapshot_utilization({"node-0": 3, "node-1": 5, "node-2": 0})
        report = m.summary()
        assert report["utilization"]["mean_ws_per_node"] == pytest.approx(8 / 3)
        assert report["utilization"]["max_ws_per_node"] == 5
        assert report["utilization"]["nodes_with_zero_ws"] == 1


# ---------------------------------------------------------------------------
# SimNode — message dispatch
# ---------------------------------------------------------------------------


class TestSimNode:
    @pytest.fixture
    def fast_config(self):
        return SimConfig(
            llm_latency_mean=0.01,
            llm_latency_stddev=0.001,
            llm_tokens_mean=10,
            llm_tokens_stddev=2,
            llm_token_rate=1000,
            tool_latency_mean=0.01,
            tool_latency_stddev=0.001,
            tool_failure_rate=0.0,
            max_tool_rounds=0,  # no tool calls — fast turn
            seed=42,
        )

    @pytest.fixture
    def mock_broker(self):
        broker = MagicMock()
        broker.list_nodes.return_value = []
        return broker

    @pytest.fixture
    def node(self, fast_config, mock_broker):
        metrics = MetricsCollector()
        return SimNode("test-node", mock_broker, fast_config, metrics)

    def test_handle_send_creates_workstream(self, node, mock_broker):
        async def _test():
            msg = SendMessage(message="hello", auto_approve=True)
            await node.handle_message(msg.to_json())

            assert node.workstream_count == 1
            mock_broker.set_ws_owner.assert_called_once()
            assert mock_broker.publish_outbound.call_count > 0

        _run(_test())

    def test_handle_send_reuses_existing_ws(self, node, mock_broker):
        async def _test():
            msg1 = SendMessage(message="hello", auto_approve=True)
            await node.handle_message(msg1.to_json())
            assert node.workstream_count == 1

            ws_id = list(node._workstreams.keys())[0]

            msg2 = SendMessage(message="world", ws_id=ws_id, auto_approve=True)
            await node.handle_message(msg2.to_json())
            assert node.workstream_count == 1

        _run(_test())

    def test_published_events_are_valid_protocol(self, node, mock_broker):
        async def _test():
            msg = SendMessage(message="test", auto_approve=True)
            await node.handle_message(msg.to_json())

            for c in mock_broker.publish_outbound.call_args_list:
                _channel, event_json = c[0]
                event = OutboundEvent.from_json(event_json)
                assert event.type != ""

        _run(_test())

    def test_state_transitions(self, node, mock_broker):
        async def _test():
            msg = SendMessage(message="test", auto_approve=True)
            await node.handle_message(msg.to_json())

            states = []
            for c in mock_broker.publish_outbound.call_args_list:
                channel, event_json = c[0]
                event = OutboundEvent.from_json(event_json)
                if isinstance(event, StateChangeEvent):
                    states.append(event.state)

            assert "thinking" in states
            assert "idle" in states
            assert states.index("thinking") < states.index("idle")

        _run(_test())

    def test_turn_complete_published(self, node, mock_broker):
        async def _test():
            msg = SendMessage(message="test", auto_approve=True)
            await node.handle_message(msg.to_json())

            turn_completes = [
                OutboundEvent.from_json(c[0][1])
                for c in mock_broker.publish_outbound.call_args_list
                if '"turn_complete"' in c[0][1]
            ]
            assert len(turn_completes) >= 1

        _run(_test())

    def test_close_workstream(self, node, mock_broker):
        async def _test():
            msg = SendMessage(message="hello", auto_approve=True)
            await node.handle_message(msg.to_json())
            ws_id = list(node._workstreams.keys())[0]

            from turnstone.mq.protocol import CloseWorkstreamMessage

            close_msg = CloseWorkstreamMessage(ws_id=ws_id)
            await node.handle_message(close_msg.to_json())

            assert node.workstream_count == 0
            mock_broker.del_ws_owner.assert_called_with(ws_id)

        _run(_test())

    def test_stop_cleans_up(self, node, mock_broker):
        # Add a fake workstream
        node._workstreams["fake"] = MagicMock()
        mock_broker.set_ws_owner("fake", "test-node")

        node.stop()
        assert not node._running
        assert node.workstream_count == 0
        mock_broker.del_ws_owner.assert_called()

    def test_heartbeat_once(self, node, mock_broker):
        node.heartbeat_once()
        mock_broker.register_node.assert_called_once()
        args = mock_broker.register_node.call_args
        assert args[0][0] == "test-node"
        assert args[0][1]["sim"] is True


# ---------------------------------------------------------------------------
# SimWorkstream — state machine
# ---------------------------------------------------------------------------


class TestSimWorkstream:
    @pytest.fixture
    def fast_config(self):
        return SimConfig(
            llm_latency_mean=0.01,
            llm_latency_stddev=0.001,
            llm_tokens_mean=10,
            llm_tokens_stddev=2,
            llm_token_rate=1000,
            tool_latency_mean=0.01,
            tool_latency_stddev=0.001,
            tool_failure_rate=0.0,
            max_tool_rounds=0,
            seed=42,
        )

    def test_turn_ends_in_idle(self, fast_config):
        async def _test():
            broker = MagicMock()
            metrics = MetricsCollector()
            node = SimNode("test", broker, fast_config, metrics)
            engine = SimEngine(fast_config)
            ws = SimWorkstream("ws1", "test-ws", node, engine, fast_config)

            await ws.process_turn("hello", "cid-123")
            assert ws.state == "idle"

        _run(_test())

    def test_turn_records_metrics(self, fast_config):
        async def _test():
            broker = MagicMock()
            metrics = MetricsCollector()
            node = SimNode("test", broker, fast_config, metrics)
            engine = SimEngine(fast_config)
            ws = SimWorkstream("ws1", "test-ws", node, engine, fast_config)

            await ws.process_turn("hello", "cid-123")
            report = metrics.summary()
            assert report["total_turns"] == 1
            assert report["turns_per_node"]["test"] == 1

        _run(_test())
