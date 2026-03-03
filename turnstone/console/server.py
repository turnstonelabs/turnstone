"""Cluster dashboard HTTP server for turnstone.

Serves the cluster-level dashboard UI and provides REST/SSE APIs
backed by the ClusterCollector.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import textwrap
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import ParseResult, parse_qs, urlparse

from turnstone.console.collector import ClusterCollector
from turnstone.mq.broker import RedisBroker

log = logging.getLogger("turnstone.console.server")

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_HTML = ""
_CSS = ""
_JS = ""


def _load_static() -> None:
    global _HTML, _CSS, _JS
    _HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    _CSS = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
    _JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class ConsoleHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for the cluster dashboard."""

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass  # suppress default logging

    def _set_headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        self._set_headers(status, "application/json")
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _check_auth(self, method: str, path: str) -> bool:
        """Return True if authorized.  Sends 401/403 and returns False otherwise."""
        from turnstone.core.auth import check_request

        auth_config = self.server.auth_config  # type: ignore[attr-defined]
        auth_header = self.headers.get("Authorization")
        cookie_header = self.headers.get("Cookie")
        allowed, status, msg = check_request(auth_config, method, path, auth_header, cookie_header)
        if not allowed:
            self._send_json({"error": msg}, status)
        return allowed

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}

    def do_POST(self) -> None:
        # Login/logout pass through _check_auth because they are in PUBLIC_PATHS.
        if not self._check_auth("POST", self.path):
            return
        if self.path == "/api/auth/login":
            from turnstone.core.auth import make_set_cookie

            body = self._read_body()
            token = body.get("token", "")
            auth_config = self.server.auth_config  # type: ignore[attr-defined]
            role = auth_config.check(token)
            if role:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", make_set_cookie(token))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "role": role}).encode("utf-8"))
            else:
                self._send_json({"error": "Invalid token"}, 401)

        elif self.path == "/api/auth/logout":
            from turnstone.core.auth import make_clear_cookie

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", make_clear_cookie())
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._check_auth("GET", parsed.path):
                return
            self._do_GET(parsed)
        except Exception:
            log.exception("Error handling GET %s", self.path)
            self._send_json({"error": "Internal server error"}, 500)

    @staticmethod
    def _parse_int(
        qs: dict[str, list[str]], name: str, default: int, minimum: int = 0, maximum: int = 10000
    ) -> int:
        try:
            val = int(qs.get(name, [str(default)])[0])
        except (ValueError, IndexError):
            val = default
        return max(minimum, min(val, maximum))

    def _do_GET(self, parsed: ParseResult) -> None:  # noqa: N802
        collector: ClusterCollector = self.server.collector  # type: ignore[attr-defined]

        if parsed.path == "/":
            self._set_headers(200, "text/html; charset=utf-8")
            self.wfile.write(_HTML.encode("utf-8"))

        elif parsed.path == "/static/style.css":
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(_CSS.encode("utf-8"))

        elif parsed.path == "/static/app.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(_JS.encode("utf-8"))

        elif parsed.path == "/api/cluster/overview":
            self._send_json(collector.get_overview())

        elif parsed.path == "/api/cluster/nodes":
            qs = parse_qs(parsed.query)
            sort_by = qs.get("sort", ["activity"])[0]
            limit = self._parse_int(qs, "limit", 100, minimum=1, maximum=1000)
            offset = self._parse_int(qs, "offset", 0)
            nodes, total = collector.get_nodes(sort_by=sort_by, limit=limit, offset=offset)
            self._send_json({"nodes": nodes, "total": total})

        elif parsed.path == "/api/cluster/workstreams":
            qs = parse_qs(parsed.query)
            state = qs.get("state", [None])[0]
            node = qs.get("node", [None])[0]
            search = qs.get("search", [None])[0]
            sort_by = qs.get("sort", ["state"])[0]
            page = self._parse_int(qs, "page", 1, minimum=1)
            per_page = self._parse_int(qs, "per_page", 50, minimum=1, maximum=200)
            ws_list, total = collector.get_workstreams(
                state=state,
                node=node,
                search=search,
                sort_by=sort_by,
                page=page,
                per_page=per_page,
            )
            pages = math.ceil(total / per_page) if per_page > 0 else 0
            self._send_json(
                {
                    "workstreams": ws_list,
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": pages,
                }
            )

        elif parsed.path.startswith("/api/cluster/node/"):
            node_id = parsed.path[len("/api/cluster/node/") :]
            if not node_id or "/" in node_id or len(node_id) > 256:
                self._send_json({"error": "Invalid node ID"}, 400)
            else:
                detail = collector.get_node_detail(node_id)
                if detail:
                    self._send_json(detail)
                else:
                    self._send_json({"error": "Node not found"}, 404)

        elif parsed.path == "/api/cluster/events":
            self._handle_sse(collector)

        elif parsed.path == "/health":
            overview = collector.get_overview()
            self._send_json(
                {
                    "status": "ok",
                    "service": "turnstone-console",
                    "nodes": overview["nodes"],
                    "workstreams": overview["workstreams"],
                }
            )

        else:
            self._set_headers(404, "text/plain")
            self.wfile.write(b"Not found")

    def _handle_sse(self, collector: ClusterCollector) -> None:
        """Server-Sent Events stream for cluster updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        collector.register_listener(client_queue)
        try:
            while True:
                try:
                    event = client_queue.get(timeout=5)
                    data = json.dumps(event)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            collector.unregister_listener(client_queue)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


# ---------------------------------------------------------------------------
# Threaded HTTP server
# ---------------------------------------------------------------------------


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="turnstone console — cluster dashboard service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              turnstone-console                              # default Redis on localhost
              turnstone-console --port 9090                  # custom port
              turnstone-console --redis-host redis.internal   # remote Redis
        """),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Port to listen on (default: 8090)",
    )
    parser.add_argument(
        "--redis-host",
        default="localhost",
        help="Redis host (default: localhost)",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=6379,
        help="Redis port (default: 6379)",
    )
    parser.add_argument(
        "--redis-password",
        default=os.environ.get("REDIS_PASSWORD"),
        help="Redis password (default: $REDIS_PASSWORD)",
    )
    parser.add_argument(
        "--redis-db",
        type=int,
        default=0,
        help="Redis DB number (default: 0)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Node polling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for polling turnstone-server nodes (default: $TURNSTONE_AUTH_TOKEN)",
    )

    from turnstone.core.config import apply_config

    apply_config(parser, ["console", "redis", "auth"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    broker = RedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
    )

    collector = ClusterCollector(
        broker=broker,
        poll_interval=args.poll_interval,
        auth_token=args.auth_token,
    )
    collector.start()

    _load_static()

    from turnstone.core.auth import load_auth_config

    auth_config = load_auth_config()

    server = ThreadedHTTPServer((args.host, args.port), ConsoleHTTPHandler)
    server.collector = collector  # type: ignore[attr-defined]
    server.auth_config = auth_config  # type: ignore[attr-defined]

    print(f"turnstone console running on http://{args.host}:{args.port}")
    if auth_config.enabled:
        print(f"Auth: enabled ({len(auth_config.tokens)} token(s) configured)")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        collector.stop()
        broker.close()
        server.shutdown()


if __name__ == "__main__":
    main()
