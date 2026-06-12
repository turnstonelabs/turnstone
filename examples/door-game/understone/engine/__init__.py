"""Game engine — pure stdlib mechanics with injectable clock and RNG.

This package has no knowledge of MCP, persistence, or rendering. Every
function takes its inputs explicitly (world, player, rng, clock) so the
mechanics are deterministic under test.
"""
