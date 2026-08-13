#!/usr/bin/env python3
"""Browser regression for the two-layer frontend cache contract.

This harness serves a versioned entry module which imports the real,
unversioned ``shared/interactive.js`` module.  It loads an old pane build in a
real Chrome profile, switches the server to a same-version replacement whose
pane has the same byte length and mtime, then performs a normal browser reload
without clearing or disabling the HTTP cache.

The old fixture advertises ``user_turn=0&tool_turn=0`` in its EventSource URL;
the current source advertises both capabilities as ``1``.  A passing run
therefore proves both layers of the contract:

* the package-versioned entry URL revalidates and may return 304; and
* its unversioned transitive pane import revalidates by content and returns the
  current bytes rather than surviving from the prior build.

The page also loads representative KaTeX, Highlight.js, and HLS.js assets.
Their installed version directories are discovered from ``shared_static`` at
runtime, so a normal vendor-version bump requires no harness edit; reload must
reuse them under the immutable policy.

Usage::

    uv run python scripts/asset_cache_e2e.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recovery_e2e import CDP, _find_chrome, _launch_chrome, _page_ws_url  # noqa: E402

from turnstone import __version__  # noqa: E402
from turnstone.core.web_helpers import (  # noqa: E402
    RevalidatingStaticFiles,
    version_html,
)

_PAGE_HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>ASSET-CACHE-PENDING</title>
    <!-- VENDORED_ASSET_TAGS -->
    <script>
      window.__assetCacheUrls = [];
      class RecordingEventSource {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSED = 2;
        constructor(url) {
          this.url = String(url);
          this.readyState = RecordingEventSource.CONNECTING;
          window.__assetCacheUrls.push(this.url);
        }
        close() {
          this.readyState = RecordingEventSource.CLOSED;
        }
      }
      window.EventSource = RecordingEventSource;
      window.addEventListener("error", (event) => {
        document.title = "ASSET-CACHE-FAILED-" + String(event.message || "script").slice(0, 80);
      });
      window.addEventListener("unhandledrejection", (event) => {
        document.title = "ASSET-CACHE-FAILED-" + String(event.reason || "promise").slice(0, 80);
      });
    </script>
  </head>
  <body>
    <main id="pane"></main>
    <script type="module" src="/static/asset_cache_boot.js"
            onerror="document.title='ASSET-CACHE-FAILED-module-load'"></script>
  </body>
</html>
"""

_BOOT_JS = """import { InteractivePane } from "/shared/interactive.js";

const fixtureWorkstreamId = "00000000-0000-0000-0000-000000000001";
const pane = new InteractivePane(fixtureWorkstreamId, { base: "" });
document.getElementById("pane").appendChild(pane.el);
pane.connectSSE(fixtureWorkstreamId);
const url = window.__assetCacheUrls.at(-1) || "";
const generation = url.includes("user_turn=1") && url.includes("tool_turn=1")
  ? "CURRENT"
  : url.includes("user_turn=0") && url.includes("tool_turn=0")
    ? "OLD"
    : "FAILED-CAPABILITIES";
window.__assetCacheResult = { generation, url };
document.title = "ASSET-CACHE-" + generation;
"""


@dataclass
class CacheState:
    phase: str = "old"
    requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, item: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(item)

    def matching(self, phase: str, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [
                item for item in self.requests if item["phase"] == phase and item["path"] == path
            ]


class SwitchingStaticFiles:
    """Select one immutable build snapshot at request dispatch time."""

    def __init__(self, state: CacheState, old_dir: Path, current_dir: Path) -> None:
        self._state = state
        self._apps = {
            "old": RevalidatingStaticFiles(directory=str(old_dir)),
            "current": RevalidatingStaticFiles(directory=str(current_dir)),
        }

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._apps[self._state.phase](scope, receive, send)


class RecordingApp:
    """Record static HTTP validators and status without perturbing streaming."""

    def __init__(self, app: Any, state: CacheState) -> None:
        self._app = app
        self._state = state

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith(("/static/", "/shared/")):
            await self._app(scope, receive, send)
            return

        phase = self._state.phase
        request_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        async def record_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
                self._state.record(
                    {
                        "phase": phase,
                        "path": path,
                        "query": scope.get("query_string", b"").decode("latin-1"),
                        "if_none_match": request_headers.get("if-none-match"),
                        "status": message["status"],
                        "etag": response_headers.get("etag"),
                        "cache_control": response_headers.get("cache-control"),
                    }
                )
            await send(message)

        await self._app(scope, receive, record_send)


def _old_interactive(current: bytes) -> bytes:
    old = current
    for needle, replacement in (
        (b'"user_turn=1"', b'"user_turn=0"'),
        (b'"&tool_turn=1"', b'"&tool_turn=0"'),
    ):
        if old.count(needle) != 1:
            raise RuntimeError(f"expected exactly one {needle.decode()} capability literal")
        old = old.replace(needle, replacement, 1)
    if len(old) != len(current):
        raise AssertionError("old and current pane fixtures must have equal byte length")
    return old


def _discover_vendor_assets(source_shared: Path) -> tuple[str, ...]:
    selected = (
        ("katex", "katex.min.css"),
        ("katex", "katex.min.js"),
        ("hljs", "highlight.min.js"),
        ("hls", "hls.min.js"),
    )
    paths = []
    for library, filename in selected:
        matches = sorted(source_shared.glob(f"{library}-*/{filename}"))
        if not matches:
            raise RuntimeError(f"no vendored {library} asset named {filename} was found")
        paths.extend(f"/shared/{match.relative_to(source_shared).as_posix()}" for match in matches)
    return tuple(paths)


def _vendor_tags(vendor_paths: tuple[str, ...]) -> str:
    tags = []
    for path in vendor_paths:
        if path.endswith(".css"):
            tags.append(f'<link rel="stylesheet" href="{path}">')
        else:
            tags.append(f'<script src="{path}"></script>')
    return "\n    ".join(tags)


def _prepare_builds(scratch: Path) -> tuple[Path, Path, Path, tuple[str, ...]]:
    import turnstone

    package_dir = Path(turnstone.__file__).resolve().parent
    source_shared = package_dir / "shared_static"
    vendor_paths = _discover_vendor_assets(source_shared)
    old_shared = scratch / "old" / "shared"
    current_shared = scratch / "current" / "shared"
    static_dir = scratch / "static"
    shutil.copytree(source_shared, old_shared)
    shutil.copytree(source_shared, current_shared)
    static_dir.mkdir()
    (static_dir / "asset_cache_boot.js").write_text(_BOOT_JS, encoding="utf-8")

    current_asset = current_shared / "interactive.js"
    old_asset = old_shared / "interactive.js"
    current = current_asset.read_bytes()
    old_asset.write_bytes(_old_interactive(current))

    # Reproduce the metadata collision which defeated Starlette's default ETag.
    fixed_mtime_ns = 1_700_000_000_123_456_789
    for asset in (old_asset, current_asset):
        os.utime(asset, ns=(fixed_mtime_ns, fixed_mtime_ns))
    old_stat = old_asset.stat()
    current_stat = current_asset.stat()
    if (old_stat.st_size, old_stat.st_mtime_ns) != (
        current_stat.st_size,
        current_stat.st_mtime_ns,
    ):
        raise AssertionError("pane fixture size/mtime collision was not preserved")
    return old_shared, current_shared, static_dir, vendor_paths


def _make_app(
    state: CacheState,
    old_dir: Path,
    current_dir: Path,
    static_dir: Path,
    vendor_paths: tuple[str, ...],
) -> Any:
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route

    async def page(_request: Any) -> HTMLResponse:
        page_html = _PAGE_HTML.replace("<!-- VENDORED_ASSET_TAGS -->", _vendor_tags(vendor_paths))
        return HTMLResponse(
            version_html(page_html),
            headers={"Cache-Control": "no-store"},
        )

    app = Starlette(
        routes=[
            Route("/asset-cache-e2e", page),
            Mount(
                "/static",
                app=RevalidatingStaticFiles(directory=str(static_dir)),
            ),
            Mount(
                "/shared",
                app=SwitchingStaticFiles(state, old_dir, current_dir),
            ),
        ]
    )

    return RecordingApp(app, state)


def _start_server(app: Any) -> tuple[Any, threading.Thread, socket.socket, str]:
    import uvicorn

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="asset-cache-e2e-server",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        sock.close()
        raise RuntimeError("asset cache test server did not start")
    return server, thread, sock, f"http://127.0.0.1:{port}"


def _wait_for_generation(cdp: CDP, expected: str, timeout: float = 20) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    last_title = ""
    while time.monotonic() < deadline:
        last_title = cdp.title()
        result = cdp.evaluate("window.__assetCacheResult || null")
        if isinstance(result, dict) and result.get("generation") == expected:
            return {"generation": str(result["generation"]), "url": str(result["url"])}
        if last_title.startswith("ASSET-CACHE-FAILED"):
            raise RuntimeError(last_title)
        time.sleep(0.1)
    raise TimeoutError(f"expected {expected}, last title was {last_title!r}")


def _one(state: CacheState, phase: str, path: str) -> dict[str, Any]:
    requests = state.matching(phase, path)
    if len(requests) != 1:
        raise AssertionError(f"expected one {phase} request for {path}, got {requests!r}")
    return requests[0]


def _verify_trace(state: CacheState, vendor_paths: tuple[str, ...]) -> tuple[str, list[str]]:
    entry_path = "/static/asset_cache_boot.js"
    pane_path = "/shared/interactive.js"
    old_entry = _one(state, "old", entry_path)
    current_entry = _one(state, "current", entry_path)
    old_pane = _one(state, "old", pane_path)
    current_pane = _one(state, "current", pane_path)

    expected_query = f"v={__version__}"
    if old_entry["query"] != expected_query or current_entry["query"] != expected_query:
        raise AssertionError("entry URL did not retain the same package version across builds")
    if old_entry["status"] != 200 or old_pane["status"] != 200:
        raise AssertionError("old build did not populate the browser cache")
    if current_entry["status"] != 304 or not current_entry["if_none_match"]:
        raise AssertionError(f"versioned entry did not revalidate to 304: {current_entry!r}")
    if current_pane["status"] != 200 or not current_pane["if_none_match"]:
        raise AssertionError(
            f"transitive pane did not revalidate to current bytes: {current_pane!r}"
        )
    if old_pane["etag"] == current_pane["etag"]:
        raise AssertionError("content-derived pane validators did not change")
    if current_pane["cache_control"] != "no-cache":
        raise AssertionError("transitive pane lost its revalidation policy")

    vendor_trace = []
    immutable = "public, max-age=31536000, immutable"
    for path in vendor_paths:
        old_vendor = _one(state, "old", path)
        if old_vendor["query"] or old_vendor["status"] != 200:
            raise AssertionError(f"versioned vendor URL was rewritten or failed: {old_vendor!r}")
        if old_vendor["cache_control"] != immutable:
            raise AssertionError(f"versioned vendor asset was not immutable: {old_vendor!r}")
        revisits = state.matching("current", path)
        if revisits:
            if len(revisits) != 1 or revisits[0]["status"] not in (200, 304):
                raise AssertionError(f"unexpected vendor reload trace: {revisits!r}")
            if revisits[0]["cache_control"] != immutable:
                raise AssertionError(f"vendor reload lost immutable policy: {revisits[0]!r}")
            vendor_trace.append(f"{path}: revisited-{revisits[0]['status']}")
        else:
            vendor_trace.append(f"{path}: cache-hit")
    verdict = f"ASSET-CACHE-READY-entry304-pane200-user1-tool1-vendor{len(vendor_paths)}"
    return verdict, vendor_trace


def _stop_process(proc: Any) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def main() -> int:
    chrome = _find_chrome()
    if not chrome:
        print("ASSET-CACHE-FAILED-no-chrome")
        return 2

    with tempfile.TemporaryDirectory(prefix="turnstone-asset-cache-e2e-") as raw_scratch:
        scratch = Path(raw_scratch)
        old_dir, current_dir, static_dir, vendor_paths = _prepare_builds(scratch)
        state = CacheState()
        server, server_thread, sock, base_url = _start_server(
            _make_app(state, old_dir, current_dir, static_dir, vendor_paths)
        )
        chrome_proc = None
        cdp = None
        try:
            chrome_proc, cdp_port = _launch_chrome(chrome, scratch / "chrome-profile")
            cdp = CDP(_page_ws_url(cdp_port))
            cdp.cmd("Page.enable")
            cdp.cmd("Runtime.enable")
            cdp.cmd("Network.enable")
            cdp.cmd("Page.navigate", {"url": f"{base_url}/asset-cache-e2e"})
            old_result = _wait_for_generation(cdp, "OLD")

            state.phase = "current"
            cdp.cmd("Page.reload", {"ignoreCache": False})
            current_result = _wait_for_generation(cdp, "CURRENT")

            if "user_turn=0" not in old_result["url"] or "tool_turn=0" not in old_result["url"]:
                raise AssertionError(f"old pane did not expose old capabilities: {old_result!r}")
            if (
                "user_turn=1" not in current_result["url"]
                or "tool_turn=1" not in current_result["url"]
            ):
                raise AssertionError(
                    f"reloaded pane did not expose current capabilities: {current_result!r}"
                )

            verdict, vendor_trace = _verify_trace(state, vendor_paths)
            cdp.evaluate(f"document.title = {json.dumps(verdict)}")
            print(verdict)
            print(f"  old EventSource:     {old_result['url']}")
            print(f"  current EventSource: {current_result['url']}")
            for item in vendor_trace:
                print(f"  vendor: {item}")
            return 0
        except Exception as exc:
            print(f"ASSET-CACHE-FAILED-{type(exc).__name__}: {exc}")
            return 1
        finally:
            if cdp is not None:
                cdp.close()
            if chrome_proc is not None:
                _stop_process(chrome_proc)
            server.should_exit = True
            server_thread.join(timeout=10)
            with contextlib.suppress(OSError):
                sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
