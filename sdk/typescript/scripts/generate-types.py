#!/usr/bin/env python3
"""Export OpenAPI specs to JSON files for TypeScript type reference.

Usage:
    python scripts/generate-types.py

Writes:
    openapi-server.json   — Server API OpenAPI 3.1 spec
    openapi-console.json  — Console API OpenAPI 3.1 spec
"""

import json
import sys
from pathlib import Path

# Ensure the turnstone package is importable (repo root is 3 levels up)
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from turnstone.api.console_spec import build_console_spec
from turnstone.api.server_spec import build_server_spec

output_dir = Path(__file__).resolve().parent.parent


def main() -> None:
    server_spec = build_server_spec()
    console_spec = build_console_spec()

    server_path = output_dir / "openapi-server.json"
    console_path = output_dir / "openapi-console.json"

    server_path.write_text(json.dumps(server_spec, indent=2) + "\n")
    console_path.write_text(json.dumps(console_spec, indent=2) + "\n")

    print(f"Wrote {server_path} ({len(server_spec['paths'])} paths)")
    print(f"Wrote {console_path} ({len(console_spec['paths'])} paths)")


if __name__ == "__main__":
    main()
