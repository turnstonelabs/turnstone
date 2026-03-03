"""LLM and tool execution simulation engine."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.sim.config import SimConfig

_WORD_POOL = [
    "the",
    "result",
    "shows",
    "that",
    "this",
    "file",
    "contains",
    "function",
    "data",
    "analysis",
    "implementation",
    "code",
    "completed",
    "successfully",
    "reviewed",
    "output",
    "processing",
    "module",
    "system",
    "request",
    "response",
    "value",
    "config",
    "status",
    "running",
    "checked",
    "verified",
    "found",
    "done",
]

_TOOL_NAMES = [
    "bash",
    "read_file",
    "search",
    "edit_file",
    "write_file",
    "math",
    "web_fetch",
]


class ToolSimulationError(Exception):
    """Raised when a simulated tool execution fails."""


class SimEngine:
    """Simulates LLM responses and tool execution with configurable distributions.

    Stateless — safe to share across workstreams on the same node.
    """

    def __init__(self, config: SimConfig, rng: random.Random | None = None):
        self._config = config
        self._rng = rng or random.Random(config.seed)

    async def simulate_llm_response(
        self, first_round: bool, turn_number: int
    ) -> tuple[str, list[dict[str, Any]]]:
        """Simulate an LLM response.

        Returns ``(content_text, tool_calls)`` where *tool_calls* may be
        empty (final answer) or a list of ``{"name": ..., "arguments": ...}``
        dicts.
        """
        latency = max(
            0.05,
            self._rng.gauss(
                self._config.llm_latency_mean,
                self._config.llm_latency_stddev,
            ),
        )
        await asyncio.sleep(latency)

        num_tokens = max(
            10,
            int(
                self._rng.gauss(
                    self._config.llm_tokens_mean,
                    self._config.llm_tokens_stddev,
                )
            ),
        )
        content = self._generate_content(num_tokens)

        # First round has a higher chance of tool calls; decreasing per round
        tool_prob = 0.6 if first_round else 0.3
        if self._rng.random() < tool_prob:
            num_calls = min(
                max(
                    1,
                    int(
                        self._rng.expovariate(
                            1.0 / self._config.tool_calls_per_turn_mean,
                        )
                    ),
                ),
                self._config.tool_calls_per_turn_max,
            )
            calls = [
                {
                    "name": self._rng.choice(_TOOL_NAMES),
                    "arguments": '{"simulated": true}',
                }
                for _ in range(num_calls)
            ]
            return content, calls

        return content, []

    async def simulate_tool_execution(self, tool_name: str) -> str:
        """Simulate tool execution with latency and possible failure."""
        latency = max(
            0.01,
            self._rng.gauss(
                self._config.tool_latency_mean,
                self._config.tool_latency_stddev,
            ),
        )
        await asyncio.sleep(latency)

        if self._rng.random() < self._config.tool_failure_rate:
            raise ToolSimulationError(f"Simulated {tool_name} failure")

        return f"[sim] {tool_name} completed successfully"

    def _generate_content(self, num_tokens: int) -> str:
        """Generate placeholder content of approximately *num_tokens* tokens."""
        return " ".join(self._rng.choices(_WORD_POOL, k=num_tokens))
