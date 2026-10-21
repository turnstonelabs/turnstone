"""The largest server-sent event Turnstone's own SSE clients accept."""

from __future__ import annotations

# httpx2's EventSource caps one event at 1 MiB unless told otherwise, and an event over the cap
# ends the stream with an error: an MCP tool call then fails as a closed connection (#679, S8).
# That is too small for what these streams carry, such as a screenshot or a multi-megabyte read
# in an MCP tool result, or a large tool result in a workstream's events. 16 MiB fits
# multi-megabyte events and still bounds a runaway stream. Every SSE client applies this same
# bound: the MCP client, the cluster collector, the channel adapters and the Python SDK.
MAX_SSE_EVENT_SIZE = 16 * 1024 * 1024
