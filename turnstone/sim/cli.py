"""CLI entry point for turnstone-sim."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from turnstone.sim.cluster import SimCluster
from turnstone.sim.config import SimConfig
from turnstone.sim.scenario import SCENARIOS

log = logging.getLogger("turnstone.sim")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="turnstone-sim",
        description="Turnstone multi-node cluster simulator",
    )
    parser.add_argument(
        "--nodes",
        type=int,
        default=10,
        help="Number of simulated nodes (default: 10)",
    )
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="steady",
        help="Scenario to run (default: steady)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Scenario duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--mps",
        type=float,
        default=5.0,
        help="Messages per second for steady scenario (default: 5.0)",
    )
    parser.add_argument(
        "--burst-size",
        type=int,
        default=100,
        help="Message count for burst scenario (default: 100)",
    )
    parser.add_argument(
        "--llm-latency",
        type=float,
        default=2.0,
        help="Mean LLM response latency in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--tool-latency",
        type=float,
        default=0.5,
        help="Mean tool execution latency in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--tool-failure-rate",
        type=float,
        default=0.02,
        help="Tool failure probability 0.0-1.0 (default: 0.02)",
    )
    parser.add_argument(
        "--node-kill-interval",
        type=float,
        default=15.0,
        help="Seconds between node kills for node_failure scenario (default: 15)",
    )
    parser.add_argument(
        "--node-kill-count",
        type=int,
        default=1,
        help="Nodes to kill per interval (default: 1)",
    )
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-password", default=None)
    parser.add_argument("--redis-db", type=int, default=0)
    parser.add_argument("--prefix", default="turnstone")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--metrics-file", default="", help="Write JSON metrics to file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    config = SimConfig(
        num_nodes=args.nodes,
        scenario=args.scenario,
        duration=args.duration,
        messages_per_second=args.mps,
        burst_size=args.burst_size,
        llm_latency_mean=args.llm_latency,
        tool_latency_mean=args.tool_latency,
        tool_failure_rate=args.tool_failure_rate,
        node_kill_interval=args.node_kill_interval,
        node_kill_count=args.node_kill_count,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_password=args.redis_password,
        redis_db=args.redis_db,
        prefix=args.prefix,
        seed=args.seed,
        metrics_file=args.metrics_file,
    )

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        log.info("Interrupted")
        sys.exit(0)


async def _run(config: SimConfig) -> None:
    cluster = SimCluster(config)
    try:
        await cluster.start()
        log.info(
            "Running scenario=%s nodes=%d duration=%ds",
            config.scenario,
            config.num_nodes,
            config.duration,
        )
        await cluster.run_scenario()

        report = cluster.report()
        _print_report(report, config)

        if config.metrics_file:
            with open(config.metrics_file, "w") as f:
                json.dump(report, f, indent=2)
            log.info("Metrics written to %s", config.metrics_file)
    finally:
        await cluster.stop()


def _print_report(report: dict[str, Any], config: SimConfig) -> None:
    lat = report.get("latency", {})
    tp = report.get("throughput", {})
    util = report.get("utilization", {})

    print("\n" + "=" * 60)
    print("  SIMULATION REPORT")
    print("=" * 60)
    print(f"  Scenario:       {config.scenario}")
    print(f"  Nodes:          {config.num_nodes}")
    print(f"  Duration:       {report['duration_seconds']}s")
    print(f"  Total turns:    {report['total_turns']}")
    print(f"  Total errors:   {report['total_errors']}")
    print(f"  Node kills:     {report['node_kills']}")
    print("-" * 60)
    print("  THROUGHPUT")
    print(f"    Messages/sec: {tp.get('messages_per_sec', 0)}")
    print(f"    Turns/sec:    {tp.get('turns_per_sec', 0)}")
    print("-" * 60)
    print("  LATENCY (seconds)")
    print(f"    p50:          {lat.get('p50', 0)}")
    print(f"    p90:          {lat.get('p90', 0)}")
    print(f"    p99:          {lat.get('p99', 0)}")
    print(f"    mean:         {lat.get('mean', 0)}")
    print(f"    max:          {lat.get('max', 0)}")
    if util:
        print("-" * 60)
        print("  UTILIZATION")
        print(f"    Mean ws/node: {util.get('mean_ws_per_node', 0):.1f}")
        print(f"    Max ws/node:  {util.get('max_ws_per_node', 0)}")
        print(f"    Idle nodes:   {util.get('nodes_with_zero_ws', 0)}")
    print("=" * 60 + "\n")
