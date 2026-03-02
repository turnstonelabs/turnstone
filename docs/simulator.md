# Cluster Simulator

The simulator (`turnstone-sim`) creates lightweight simulated nodes that talk to a real Redis instance using the standard turnstone protocol. External observers — `TurnstoneClient`, `turnstone-console`, real bridges — see identical behavior. No LLM backend is needed.

## Quick Start

```bash
pip install turnstone[sim]

# 10 nodes, steady load, 60 seconds
turnstone-sim --nodes 10 --scenario steady --duration 60 --mps 5

# 100 nodes via Docker
docker compose --profile sim up redis console sim
```

## How It Works

Each simulated node is an asyncio coroutine (not a thread or process), so 1000 nodes run efficiently on a single event loop. The simulator:

1. Registers nodes via Redis heartbeats (same keys as real bridges)
2. Accepts messages from per-node and shared inbound queues
3. Simulates LLM responses with configurable latency and token generation
4. Simulates tool execution with configurable latency and failure rates
5. Publishes real protocol events (`ContentEvent`, `StateChangeEvent`, `TurnCompleteEvent`, etc.)
6. Reports latency, throughput, and utilization metrics at completion

```
TurnstoneClient → Redis Queue → SimNode → Redis Pub/Sub → TurnstoneClient
                                  ↓
                            turnstone-console (cluster dashboard)
```

## Scenarios

| Scenario | Description |
|----------|-------------|
| `steady` | Inject messages at a constant rate (`--mps`) for `--duration` seconds |
| `burst` | Push `--burst-size` messages instantly, then wait for completion |
| `node_failure` | Steady load + periodically kill nodes to test redistribution |
| `directed` | Send messages to specific nodes via `target_node` routing |
| `lifecycle` | Create, use, and close workstreams across nodes |

## CLI Reference

```
turnstone-sim [options]
```

### Cluster

| Flag | Default | Description |
|------|---------|-------------|
| `--nodes` | `10` | Number of simulated nodes |

### Scenario

| Flag | Default | Description |
|------|---------|-------------|
| `--scenario` | `steady` | Scenario name |
| `--duration` | `60` | Duration in seconds |
| `--mps` | `5.0` | Messages per second (steady) |
| `--burst-size` | `100` | Messages to send (burst) |
| `--node-kill-interval` | `15` | Seconds between kills (node_failure) |
| `--node-kill-count` | `1` | Nodes per kill cycle |

### Simulation

| Flag | Default | Description |
|------|---------|-------------|
| `--llm-latency` | `2.0` | Mean LLM response latency (seconds) |
| `--tool-latency` | `0.5` | Mean tool execution latency (seconds) |
| `--tool-failure-rate` | `0.02` | Tool failure probability (0.0–1.0) |
| `--seed` | — | Random seed for reproducibility |

### Redis

| Flag | Default | Description |
|------|---------|-------------|
| `--redis-host` | `localhost` | Redis host |
| `--redis-port` | `6379` | Redis port |
| `--redis-password` | — | Redis password |
| `--prefix` | `turnstone` | Redis key prefix |

### Output

| Flag | Default | Description |
|------|---------|-------------|
| `--metrics-file` | — | Write JSON report to file |
| `--log-level` | `INFO` | Log verbosity |

## Example: Load Testing

```bash
# 100 nodes, high throughput, 2 minutes
turnstone-sim --nodes 100 --scenario steady --duration 120 --mps 50

# Burst of 500 messages across 50 nodes
turnstone-sim --nodes 50 --scenario burst --burst-size 500 --duration 60

# Node failure resilience (kill 2 nodes every 10 seconds)
turnstone-sim --nodes 20 --scenario node_failure --duration 120 \
  --node-kill-interval 10 --node-kill-count 2

# Fast simulation (low latency, no failures)
turnstone-sim --nodes 10 --scenario steady --duration 30 \
  --llm-latency 0.1 --tool-latency 0.05 --tool-failure-rate 0 --mps 10
```

## Metrics Report

The simulator prints a summary at completion:

```
============================================================
  SIMULATION REPORT
============================================================
  Scenario:       steady
  Nodes:          100
  Duration:       60.2s
  Total turns:    295
  Total errors:   5
  Node kills:     0
------------------------------------------------------------
  THROUGHPUT
    Messages/sec: 4.97
    Turns/sec:    4.89
------------------------------------------------------------
  LATENCY (seconds)
    p50:          3.21
    p90:          5.44
    p99:          8.12
    mean:         3.56
    max:          12.1
------------------------------------------------------------
  UTILIZATION
    Mean ws/node: 2.3
    Max ws/node:  8
    Idle nodes:   12
============================================================
```

Use `--metrics-file report.json` to write the full report as JSON.

## Console Integration

The simulator's nodes appear in `turnstone-console` exactly like real nodes. Run them together to see the dashboard populate with simulated workstreams:

```bash
# Terminal 1: start Redis and console
docker compose up redis console

# Terminal 2: run simulator
docker compose --profile sim up sim
```

Or all at once:

```bash
SIM_NODES=50 SIM_DURATION=120 docker compose --profile sim up redis console sim
```

Open http://localhost:8090 to see simulated nodes, workstream states, token counts, and load bars updating in real time.

## Architecture

```
turnstone/sim/
├── __init__.py     # Public API: SimCluster, SimConfig
├── config.py       # SimConfig — all simulation parameters
├── engine.py       # SimEngine — LLM + tool execution simulation
├── node.py         # SimNode + SimWorkstream — protocol-compatible node
├── cluster.py      # SimCluster + InboundDispatcher + PooledBroker
├── scenario.py     # 5 scenario classes
├── metrics.py      # MetricsCollector — latency, throughput, utilization
└── cli.py          # CLI entry point
```

**Key design:** The `InboundDispatcher` batches ~50 node queues into a single Redis `BLPOP` call, keeping connection count bounded at ~20 regardless of node count. All nodes share a single `ConnectionPool(max_connections=64)`.

## Programmatic Use

```python
import asyncio
from turnstone.sim import SimCluster, SimConfig

async def main():
    config = SimConfig(
        num_nodes=10,
        scenario="steady",
        duration=30,
        messages_per_second=2.0,
        llm_latency_mean=0.5,
    )
    cluster = SimCluster(config)
    await cluster.start()
    await cluster.run_scenario()
    print(cluster.report())
    await cluster.stop()

asyncio.run(main())
```
