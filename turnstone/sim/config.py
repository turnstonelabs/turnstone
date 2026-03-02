"""Simulation configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimConfig:
    """All parameters controlling a simulation run."""

    # -- cluster --
    num_nodes: int = 10
    max_ws_per_node: int = 10

    # -- redis --
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    prefix: str = "turnstone"

    # -- heartbeat --
    heartbeat_ttl: int = 60

    # -- LLM simulation --
    llm_latency_mean: float = 2.0
    llm_latency_stddev: float = 0.5
    llm_tokens_mean: int = 200
    llm_tokens_stddev: int = 50
    llm_token_rate: float = 50.0  # tokens/sec streaming speed
    context_window: int = 131072  # for computing context ratio

    # -- tool simulation --
    tool_latency_mean: float = 0.5
    tool_latency_stddev: float = 0.2
    tool_failure_rate: float = 0.02
    tool_calls_per_turn_mean: float = 1.5
    tool_calls_per_turn_max: int = 4
    max_tool_rounds: int = 3

    # -- scenario --
    scenario: str = "steady"
    duration: int = 60
    messages_per_second: float = 5.0
    burst_size: int = 100
    node_kill_interval: float = 15.0
    node_kill_count: int = 1

    # -- metrics --
    metrics_interval: float = 5.0
    metrics_file: str = ""

    # -- reproducibility --
    seed: int | None = None
