#!/usr/bin/env python3
"""Health check for turnstone containers.

Usage: healthcheck.py <url>
Exit 0 if the endpoint returns {"status": "ok"}, exit 1 otherwise.
Uses only stdlib — no pip dependencies required.
"""

import json
import sys
import urllib.request


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: healthcheck.py <url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    try:
        import ssl

        ctx = ssl.create_default_context()
        if url.startswith("https://"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") in ("ok", "degraded"):
                sys.exit(0)
            print(f"Unhealthy: {data}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Health check failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
