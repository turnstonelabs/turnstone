#!/usr/bin/env python3
"""Browser-level SSE recovery livepass — boots the REAL interactive.js
``InteractivePane`` against a REAL Turnstone node and drives the two
headline recovery scenarios through headless Chrome over CDP, stamping
``document.title`` verdicts (``RECOVERY-READY-*`` / ``RECOVERY-FAILED-*``)
the livepass convention.

Unlike ``scripts/livepass.py`` (which stubs ``window.authFetch`` with
canned fixtures), this page uses the REAL auth + REAL EventSource against a
REAL node: the page is served same-origin by the node itself (so cookie
auth and EventSource just work), the node runs a scripted-provider
workstream so a REST ``/send`` drives a real bash storm, and the pane's
own state machine (interactive.js) does the recovery.

Usage::

    python3 scripts/recovery_e2e.py                 # run both scenarios
    python3 scripts/recovery_e2e.py --scenario storm
    python3 scripts/recovery_e2e.py --scenario restart
    python3 scripts/recovery_e2e.py --keep-open 8971  # serve the storm page
                                                      # for manual inspection

Scenario A (storm): the page connects, POSTs ``/send`` on stream-open (so
the listener is registered first), the node runs a 4-parallel-bash
``seq 1 500`` storm plus a task_agent whose sub-tools are chatty bashes;
the page asserts the final DOM has the expected top-level tool rows, the
task_agent card nests its sub-tool rows (NO child escaped to the top
level), and the composer settles idle. Stamps ``RECOVERY-READY-STORM-<n>``.

Scenario B (hide -> restart -> show): the page runs a turn, the runner
hides the tab (interactive.js closes the stream), restarts the node on the
SAME port (fresh empty ring, storage-seeded counter), then shows the tab;
the pane reconnects to the restarted node, the committed transcript stays
intact, and the composer settles idle. Stamps ``RECOVERY-READY-RESTART``.

The specific ``replay_truncated`` envelope is NOT asserted at the browser
level here: it needs a browser cursor below the restarted node's seeded
counter, and Chrome's ``EventSource`` tracks that cursor internally only on
its native auto-reconnect (not on the close-on-hide reconnect, which opens
a fresh stream). The truncated -> lost_count -> /history-cursor rebuild is
proven deterministically at the server-contract level in Tier 1's
``test_restart_truncated_honesty`` / ``test_failed_resync_retries_via_truncation_record``.

A NOTE ON THE BROWSER OVERFLOW (server-side poison): a real listener-queue
poison needs the browser to STOP reading the socket so TCP backpressure
reaches the server. A backgrounded/CPU-throttled tab does NOT do this --
Chrome's network stack keeps draining the socket regardless of JS
throttling, and interactive.js deliberately CLOSES the stream on tab-hide
rather than starving it. So the server-side overflow -> stream_overflow ->
reconnect path is NOT reliably forcible from a real browser (which is why
that field bug was subtle); it is proven at the server-contract level in
``tests/test_sse_recovery_e2e.py::test_slow_consumer_overflow_then_lossless_reconnect``.
Scenario A here proves the OTHER half at the browser level: fix-3's
de-amplified storm renders correctly with no escaped sub-agent children.

MANUAL RUNBOOK (if Chrome/CDP is unavailable): run this with
``--keep-open PORT`` to boot the node + serve the storm page, open the
printed URL in a browser (the script prints the auth cookie to set), and
watch ``document.title``. For the restart scenario, boot with a fixed
port, load the restart page, background the tab, restart the node
(``RecoveryServer`` on the same port), foreground the tab, and watch the
title settle to ``RECOVERY-READY-RESTART``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# The recovery page — served same-origin by the node at /recovery.
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>recovery livepass</title>
    <link rel="stylesheet" href="/shared/base.css" />
    <link rel="stylesheet" href="/shared/ui-base.css" />
    <link rel="stylesheet" href="/shared/chat.css" />
    <link rel="stylesheet" href="/shared/conversation.css" />
    <link rel="stylesheet" href="/shared/cards.css" />
    <link rel="stylesheet" href="/shared/interactive.css" />
    <style>
      body { margin: 0; background: var(--bg); color: var(--ink); }
      #mount { height: 100vh; display: flex; }
      #mount > * { flex: 1; min-height: 0; }
    </style>
  </head>
  <body>
    <div id="header"><div id="status-bar"></div></div>
    <div id="mount"></div>
    <script>
      // Minimal globals interactive.js reads on the standalone path.
      window.showToast = function (m) { console.log("toast:", m); };
      window.showLogin = function () {};
    </script>
    <script type="module">
      import { InteractivePane } from "/shared/interactive.js";

      const q = new URLSearchParams(location.search);
      const wsId = q.get("ws_id");
      const scenario = q.get("scenario") || "storm";
      const expectRows = parseInt(q.get("rows") || "4", 10);

      // REAL pane against THIS origin (base=""): real authFetch (cookie) and
      // real EventSource. The default host provides all SSE seams.
      const pane = new InteractivePane(wsId, { base: "" });
      document.getElementById("mount").appendChild(pane.el);
      pane.wsId = wsId;
      window.__pane = pane;

      window.__hide = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: true });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
        document.dispatchEvent(new Event("visibilitychange"));
      };
      window.__show = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: false });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
        document.dispatchEvent(new Event("visibilitychange"));
      };

      // Count top-level tool rows and escaped sub-agent children.
      function domCounts() {
        const topRows = pane.messagesEl.querySelectorAll(
          ".conv-batch > .conv-row[data-call-id]"
        );
        let topLevel = 0;
        let escapedChildren = 0;
        topRows.forEach((r) => {
          const cid = r.dataset.callId || "";
          if (cid.includes("::")) escapedChildren += 1;  // a child at the top level
          else topLevel += 1;
        });
        const agentCard = pane.messagesEl.querySelector(".conv-agent");
        const nested = pane.messagesEl.querySelectorAll(
          ".conv-agent .conv-row[data-call-id]"
        ).length;
        return { topLevel, escapedChildren, agentCard: !!agentCard, nested };
      }

      let sent = false;
      function sendOnce(msg) {
        if (sent) return;
        sent = true;
        // The pane's SSE is open (host.onStreamOpen fired), so the listener is
        // registered before this /send -- no missed events.
        window
          .authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg }),
          })
          .catch((e) => { document.title = "RECOVERY-FAILED-send-" + e; });
      }

      // Drive /send once the stream is live (wrap the default host hook).
      const origOpen = pane._host.onStreamOpen.bind(pane._host);
      pane._host.onStreamOpen = function (p) {
        origOpen(p);
        window.__streamOpen = (window.__streamOpen || 0) + 1;
        if (scenario === "storm") sendOnce("run the storm");
        else if (scenario === "restart" && window.__streamOpen === 1) sendOnce("run a turn");
      };

      // First paint the REAL way: /history then connect SSE.
      pane._loadHistoryThenConnect(wsId);

      if (scenario === "storm") {
        const deadline = Date.now() + 40000;
        const poll = () => {
          const c = domCounts();
          const idle = !pane.busy;
          if (c.topLevel >= expectRows && c.agentCard && c.nested >= 2 && idle) {
            document.title = c.escapedChildren
              ? "RECOVERY-FAILED-escaped-" + c.escapedChildren
              : "RECOVERY-READY-STORM-" + c.topLevel + "-nested-" + c.nested;
            return;
          }
          if (Date.now() > deadline) {
            document.title =
              "RECOVERY-FAILED-STORM-top" + c.topLevel + "-agent" + (c.agentCard ? 1 : 0) +
              "-nested" + c.nested + "-escaped" + c.escapedChildren + "-busy" + (pane.busy ? 1 : 0);
            return;
          }
          setTimeout(poll, 200);
        };
        setTimeout(poll, 400);
      } else if (scenario === "restart") {
        // The runner drives hide -> (restart node) -> show via window.__hide/
        // __show. We watch for the truncated-triggered rebuild + idle settle.
        window.__truncatedSeen = 0;
        const origHandle = pane.handleEvent.bind(pane);
        pane.handleEvent = function (ev) {
          if (ev && ev.type === "replay_truncated") window.__truncatedSeen += 1;
          return origHandle(ev);
        };
        window.__verifyRestart = function () {
          // Browser-level restart RECOVERY: after the node restarts on the
          // same port, the pane reconnects (status bar not stuck
          // disconnected), the committed transcript is intact, and the
          // composer settles idle. (The specific ``replay_truncated``
          // envelope requires a browser cursor below the restarted node's
          // seeded counter; Chrome's EventSource tracks that cursor
          // internally only on its native auto-reconnect, so it is proven
          // deterministically at the server-contract level in Tier 1's
          // test_restart_truncated_honesty. ``__truncatedSeen`` is recorded
          // here for the runs where it does fire.)
          const c = domCounts();
          const idle = !pane.busy;
          const disc = document.querySelector(".ws-sb-disconnected") !== null;
          document.title =
            c.topLevel >= 1 && idle && !disc
              ? "RECOVERY-READY-RESTART-rows" + c.topLevel + "-trunc" + window.__truncatedSeen
              : "RECOVERY-FAILED-RESTART-rows" + c.topLevel +
                "-busy" + (pane.busy ? 1 : 0) + "-disc" + (disc ? 1 : 0);
        };
      }
    </script>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Minimal dependency-free CDP client (WebSocket over a raw socket).
# ---------------------------------------------------------------------------


class CDP:
    """Just enough Chrome DevTools Protocol: navigate, evaluate, set cookie."""

    def __init__(self, ws_url: str) -> None:
        from urllib.parse import urlsplit

        u = urlsplit(ws_url)
        self._sock = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self._sock.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {resp[:80]!r}")
        self._id = 0
        self._rbuf = b""

    def _send(self, payload: bytes) -> None:
        header = bytearray([0x81])  # FIN + text opcode
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("CDP socket closed")
            self._rbuf += chunk
        out, self._rbuf = self._rbuf[:n], self._rbuf[n:]
        return out

    def _recv_message(self) -> str:
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            data += self._recv_exact(length)
            if fin:
                return data.decode("utf-8", "replace")

    def cmd(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
        self._id += 1
        mid = self._id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}).encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.1, deadline - time.monotonic()))
            obj = json.loads(self._recv_message())
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError(f"{method}: {obj['error']}")
                return obj.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str) -> Any:
        r = self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return r.get("result", {}).get("value")

    def title(self) -> str:
        return str(self.evaluate("document.title") or "")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sock.close()


# ---------------------------------------------------------------------------
# Chrome launch + node boot
# ---------------------------------------------------------------------------


def _find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _launch_chrome(chrome: str, profile: Path) -> tuple[subprocess.Popen[bytes], int]:
    cdp_port = _free_port()
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            "--disable-background-timer-throttling",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, cdp_port


def _page_ws_url(cdp_port: int, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=2) as r:
                targets = json.loads(r.read())
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return str(t["webSocketDebuggerUrl"])
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError("no CDP page target")


def _page_route() -> Any:
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def recovery_page(_request: Any) -> HTMLResponse:
        return HTMLResponse(PAGE_HTML)

    return Route("/recovery", recovery_page)


def _boot_node(port: int = 0) -> Any:
    from tests._sse_recovery_server import RecoveryServer
    from turnstone.core.storage import init_storage, reset_storage

    # The page route must bypass auth on first load (the cookie is set by the
    # runner via CDP BEFORE navigation), so make it public by prefixing under
    # a public path is unavailable here; instead the runner sets the cookie so
    # /recovery passes the middleware. init storage per boot (shared singleton).
    reset_storage()
    init_storage("sqlite", path=os.path.join(_scratch(), "recovery_e2e.db"), run_migrations=True)
    return RecoveryServer(extra_routes=[_page_route()], port=port)


def _scratch() -> str:
    d = os.environ.get("RECOVERY_E2E_TMP") or "/tmp/recovery_e2e"
    os.makedirs(d, exist_ok=True)
    return d


def _set_cookie_and_navigate(cdp: CDP, base_url: str, token: str, page_url: str) -> None:
    cdp.cmd("Page.enable")
    cdp.cmd("Runtime.enable")
    cdp.cmd("Network.enable")
    cdp.cmd(
        "Network.setCookie",
        {
            "name": "turnstone_auth_server",
            "value": token,
            "url": base_url,
            "path": "/",
        },
    )
    cdp.cmd("Page.navigate", {"url": page_url})


def _poll_title(cdp: CDP, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = cdp.title()
        if last.startswith("RECOVERY-"):
            return last
        time.sleep(0.3)
    return last or "RECOVERY-FAILED-timeout"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _storm_scripts() -> tuple[Any, ...]:
    """A parallel bash storm PLUS a task_agent whose sub-tools are chatty
    bashes (so the browser proves both fix-3 batching AND sub-agent nesting
    with no escaped children)."""
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    storm = parallel_bash_script({f"call_{i}": "seq 1 500" for i in range(4)})
    task = dict(
        tool_calls=[
            {
                "id": "task1",
                "name": "task_agent",
                "arguments": json.dumps({"prompt": "sub tools"}),
            }
        ],
        finish_reason="tool_calls",
    )
    sub = dict(
        tool_calls=[
            {"id": "s_a", "name": "bash", "arguments": json.dumps({"command": ": a; seq 1 200"})},
            {"id": "s_b", "name": "bash", "arguments": json.dumps({"command": ": b; seq 1 200"})},
        ],
        finish_reason="tool_calls",
    )
    # Turn 1: the 4-bash storm; turn 2: a task_agent with 2 chatty sub-bashes.
    return (
        storm,
        final_text_script("storm done"),
        task,
        sub,
        final_text_script("sub done"),
        final_text_script("all done"),
    )


def run_storm(chrome: str) -> str:
    node = _boot_node()
    ws_id = node.create_workstream(*_storm_scripts(), name="browser-storm")
    profile = Path(_scratch()) / "chrome-storm"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        # The page POSTs the STORM turn on stream-open; the runner sends the
        # task_agent follow-up once the first turn settles so both land.
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=storm&rows=4"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # After the storm turn, trigger the task_agent turn via REST so the
        # page renders the nested sub-agent card.
        _wait_state(node, ws_id, "idle", 40)
        node.send(ws_id, "spawn the sub agent")
        return _poll_title(cdp, 45)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_restart(chrome: str) -> str:
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    port = _free_port()
    node = _boot_node(port=port)
    # A PACED turn so the tab can hide MID-turn (browser cursor below the
    # committed counter) -> the post-restart reconnect draws truncated.
    paced = parallel_bash_script({"r0": "for i in $(seq 1 40); do echo r-$i; sleep 0.05; done"})
    ws_id = node.create_workstream(paced, final_text_script("done"), name="browser-restart")
    profile = Path(_scratch()) / "chrome-restart"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=restart"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        node.wait_turn(ws_id, timeout=30)  # the turn renders into the DOM
        time.sleep(0.5)
        # Hide the tab -> interactive.js closes the stream (close-on-hide).
        cdp.evaluate("window.__hide && window.__hide()")
        # Restart the node on the SAME port (fresh empty ring, seeded counter).
        node.stop()
        node = _boot_node(port=port)
        node.open_workstream(ws_id)
        # Show the tab -> the pane reconnects to the restarted node and recovers.
        # The reconnect + any resync carries jitter, so settle before the verdict.
        cdp.evaluate("window.__show && window.__show()")
        time.sleep(9.0)
        cdp.evaluate("window.__verifyRestart && window.__verifyRestart()")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def _wait_state(node: Any, ws_id: str, state: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node.ws_state(ws_id) == state:
            return
        time.sleep(0.1)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()


def keep_open(port: int) -> None:
    """Boot the node + storm ws and serve the page for manual inspection."""
    node = _boot_node(port=port)
    ws_id = node.create_workstream(*_storm_scripts(), name="manual-storm")
    print(f"node: {node.base_url}")
    print(f"cookie: turnstone_auth_server={node.token}")
    print(f"page:  {node.base_url}/recovery?ws_id={ws_id}&scenario=storm&rows=4")
    print("set the cookie for this origin, then open the page. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", choices=["storm", "restart", "both"], default="both")
    ap.add_argument("--keep-open", type=int, metavar="PORT", help="serve the storm page, no CDP")
    args = ap.parse_args()

    if args.keep_open:
        keep_open(args.keep_open)
        return

    chrome = _find_chrome()
    if chrome is None:
        print("recovery_e2e: no chrome/chromium on PATH — see the module docstring runbook")
        raise SystemExit(2)

    failures = 0
    if args.scenario in ("storm", "both"):
        verdict = run_storm(chrome)
        print(f"scenario A (storm):   {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("restart", "both"):
        verdict = run_restart(chrome)
        print(f"scenario B (restart): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
