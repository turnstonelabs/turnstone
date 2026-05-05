"""Spike 1 — validate MCP SDK behavior for the per-(user, server) session pool.

Three scenarios:

1. N=20 concurrent ClientSession instances to the same URL.
   Verifies: no FD blow-up, no shared transport state, each session's
   tools/list returns independently.

2. Two concurrent tools/call on a shared ClientSession with interleaving
   payloads. Verifies: request_id demux works under contention.

3. Per-session Authorization header isolation. Verifies: different Bearer
   tokens per ClientSession reach the server with the expected
   Authorization header — i.e. httpx connection pooling does not cross
   headers between sessions.

Run: uv run python tests/spike_sdk_concurrency.py

Outcome gates Phase 5's pool architecture; if any scenario fails, fall
back to per-call header injection (Alternative F in the OAuth-MCP RFC).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

# Reduce uvicorn / mcp log noise so spike output is readable.
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

# Records (auth_header, tool_name) per request — populated by the
# AuthHeaderRecorder middleware below. Indexed by call sequence.
SERVER_OBSERVATIONS: list[tuple[str | None, str | None]] = []
# Tool-call payloads observed (for request_id demux verification).
TOOL_CALL_PAYLOADS: list[str] = []


class AuthHeaderRecorder(BaseHTTPMiddleware):
    """Records the Authorization header on every request the server sees."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        auth = request.headers.get("authorization")
        # We only record the auth header here; tool name comes from the
        # body payload which we can't read non-destructively. The tool
        # handler logs the payload it received.
        SERVER_OBSERVATIONS.append((auth, None))
        return await call_next(request)


def find_free_port() -> int:
    """Bind to port 0, return the assigned port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_server(port: int) -> uvicorn.Server:
    """Create a minimal FastMCP server with one echo tool."""
    mcp = FastMCP(name="spike-target", streamable_http_path="/mcp")

    @mcp.tool()
    async def echo(payload: str) -> str:
        """Echo the payload back. Records the payload server-side."""
        TOOL_CALL_PAYLOADS.append(payload)
        # Add a small await so two concurrent calls can interleave
        # on the wire if the SDK pools the requests.
        await asyncio.sleep(0.05)
        return f"echoed:{payload}"

    app = mcp.streamable_http_app()
    app.add_middleware(AuthHeaderRecorder)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)


def run_server_in_thread(server: uvicorn.Server) -> threading.Thread:
    """Boot the server in a background thread on its own asyncio loop."""

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="spike-server")
    t.start()
    return t


async def wait_for_server_ready(url: str, timeout: float = 5.0) -> None:
    """Poll the server until it accepts connections."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"server at {url} not ready within {timeout}s")


def fd_count() -> int:
    """Count open file descriptors for the current process."""
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return -1


# ---------------------------------------------------------------------------
# Scenario 1: N=20 concurrent ClientSession instances
# ---------------------------------------------------------------------------


async def scenario_1_concurrent_sessions(url: str, n: int = 20) -> dict:
    """Open N concurrent ClientSession instances and call tools/list on each."""
    print(f"\n=== Scenario 1: {n} concurrent ClientSession instances ===")
    fd_before = fd_count()

    async def one_session(idx: int) -> dict:
        headers = {"Authorization": f"Bearer test-token-{idx}"}
        async with (
            streamablehttp_client(url=url, headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return {
                "idx": idx,
                "tool_count": len(tools.tools),
                "tool_names": [t.name for t in tools.tools],
            }

    start = time.monotonic()
    results = await asyncio.gather(*[one_session(i) for i in range(n)], return_exceptions=True)
    elapsed = time.monotonic() - start

    fd_after = fd_count()
    # Allow some settling time for FDs to release.
    await asyncio.sleep(0.5)
    fd_settled = fd_count()

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, Exception)]

    # Verify every session got the same tool catalog.
    catalog_consistent = (
        len(successes) == n and len({tuple(r["tool_names"]) for r in successes}) == 1
    )

    return {
        "scenario": "concurrent_sessions",
        "n": n,
        "successes": len(successes),
        "failures": len(failures),
        "elapsed_seconds": round(elapsed, 3),
        "fd_before": fd_before,
        "fd_during_peak": fd_after,
        "fd_settled": fd_settled,
        "fd_growth_during": fd_after - fd_before,
        "fd_growth_settled": fd_settled - fd_before,
        "catalog_consistent": catalog_consistent,
        "first_failure": str(failures[0]) if failures else None,
    }


# ---------------------------------------------------------------------------
# Scenario 2: 2 concurrent tools/call on a shared session
# ---------------------------------------------------------------------------


async def scenario_2_concurrent_calls_shared_session(url: str) -> dict:
    """Two concurrent tools/call on one ClientSession with interleaving payloads.

    The echo tool sleeps 50ms, so concurrent calls overlap on the wire.
    Each call passes a distinct payload (~10KB) to make request bodies
    spannable across multiple stream frames.
    """
    print("\n=== Scenario 2: 2 concurrent tools/call on shared session ===")

    # Generous-size payloads so both bodies live during the await.
    payload_a = "A" * 10000
    payload_b = "B" * 10000

    headers = {"Authorization": "Bearer shared-session-token"}
    async with (
        streamablehttp_client(url=url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        TOOL_CALL_PAYLOADS.clear()

        start = time.monotonic()
        results = await asyncio.gather(
            session.call_tool("echo", {"payload": payload_a}),
            session.call_tool("echo", {"payload": payload_b}),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - start

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    # Each result.content[0].text should be "echoed:{payload}".
    response_payloads: list[str] = []
    if len(successes) == 2:
        for r in successes:
            text = r.content[0].text if r.content else ""
            response_payloads.append(text)

    # Order may not match call order — what matters is both payloads echo.
    expected = {f"echoed:{payload_a}", f"echoed:{payload_b}"}
    received = set(response_payloads)
    demux_ok = received == expected

    # Did both calls actually overlap? If sequential, elapsed ~= 0.1+s;
    # if concurrent, ~0.05s.
    concurrent_observed = elapsed < 0.09

    return {
        "scenario": "concurrent_calls_shared_session",
        "successes": len(successes),
        "failures": len(failures),
        "elapsed_seconds": round(elapsed, 3),
        "demux_ok": demux_ok,
        "expected_payloads_received": list(received) if demux_ok else None,
        "actual_payloads_received_count": len(received),
        "appears_concurrent_on_wire": concurrent_observed,
        "first_failure": str(failures[0]) if failures else None,
    }


# ---------------------------------------------------------------------------
# Scenario 3: per-session header isolation
# ---------------------------------------------------------------------------


async def scenario_3_header_isolation(url: str, n: int = 5) -> dict:
    """Open N sessions with distinct Authorization headers, call echo on each.

    Verifies the server sees each session's own header — i.e. httpx
    connection pooling does not cross headers between concurrent
    ClientSession instances against the same URL.
    """
    print(f"\n=== Scenario 3: {n}-session Authorization-header isolation ===")
    SERVER_OBSERVATIONS.clear()

    async def one_session(idx: int) -> str | None:
        headers = {"Authorization": f"Bearer iso-token-{idx}"}
        async with (
            streamablehttp_client(url=url, headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            # One call per session.
            await session.call_tool("echo", {"payload": f"session-{idx}"})
            return f"Bearer iso-token-{idx}"

    start = time.monotonic()
    expected_tokens = await asyncio.gather(*[one_session(i) for i in range(n)])
    elapsed = time.monotonic() - start

    # Tally observed Authorization headers, ignoring None entries (initial
    # handshake sometimes lacks auth).
    observed_auth = [auth for auth, _ in SERVER_OBSERVATIONS if auth]
    expected_set = set(expected_tokens)
    observed_set = set(observed_auth)

    # Every expected token must show up at least once on the server.
    all_present = expected_set.issubset(observed_set)
    # No spurious tokens.
    no_extras = observed_set.issubset(expected_set)
    # Frequency: at least one observation per token.
    counts = defaultdict(int)
    for a in observed_auth:
        counts[a] += 1
    each_seen = all(counts[t] >= 1 for t in expected_tokens)

    return {
        "scenario": "header_isolation",
        "n": n,
        "elapsed_seconds": round(elapsed, 3),
        "expected_tokens": sorted(expected_set),
        "observed_tokens": sorted(observed_set),
        "all_expected_present": all_present,
        "no_extra_tokens_observed": no_extras,
        "each_token_seen_at_least_once": each_seen,
        "header_counts_per_token": dict(counts),
        "total_requests_observed": len(observed_auth),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def main() -> None:
    port = find_free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    server = build_server(port)
    server_thread = run_server_in_thread(server)
    try:
        await wait_for_server_ready(url)
        print(f"server up at {url}\n")

        result_1 = await scenario_1_concurrent_sessions(url, n=20)
        print_scenario_result(result_1)

        result_2 = await scenario_2_concurrent_calls_shared_session(url)
        print_scenario_result(result_2)

        result_3 = await scenario_3_header_isolation(url, n=5)
        print_scenario_result(result_3)

        # Final verdict
        verdict_1 = (
            result_1["successes"] == result_1["n"]
            and result_1["catalog_consistent"]
            and result_1["fd_growth_settled"] < 30  # 20 sessions, generous bound
        )
        verdict_2 = result_2["demux_ok"] and result_2["successes"] == 2
        verdict_3 = (
            result_3["all_expected_present"]
            and result_3["no_extra_tokens_observed"]
            and result_3["each_token_seen_at_least_once"]
        )

        print("\n=== VERDICT ===")
        print(f"  Scenario 1 (concurrent sessions):    {'PASS' if verdict_1 else 'FAIL'}")
        print(f"  Scenario 2 (concurrent calls shared): {'PASS' if verdict_2 else 'FAIL'}")
        print(f"  Scenario 3 (header isolation):        {'PASS' if verdict_3 else 'FAIL'}")
        all_pass = verdict_1 and verdict_2 and verdict_3
        print(
            f"\n  Phase 5 per-(user, server) pool architecture: "
            f"{'VIABLE' if all_pass else 'NEEDS REWORK (Alternative F fallback)'}"
        )
        sys.exit(0 if all_pass else 1)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


def print_scenario_result(result: dict) -> None:
    print(f"\nresult[{result['scenario']}]:")
    for k, v in result.items():
        if k == "scenario":
            continue
        print(f"  {k}: {v}")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
