"""Safe, comparable diagnostics for the two live Entra harnesses."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any


def report_source(script: str) -> None:
    path = Path(script).resolve()
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path.parent, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=path.parent,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        source = f"revision={revision} dirty={dirty}"
    except (OSError, subprocess.CalledProcessError):
        source = "revision=unavailable"
    print(f"[SOURCE] {path.name} {source} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")


def failure_summary(status: int, body: Any) -> str:
    """Emit protocol codes only; descriptions and echoed payloads may hold secrets."""
    body = body if isinstance(body, dict) else {}
    code = body.get("error")
    known_errors = {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "consent_required",
        "login_required",
    }
    code = code if isinstance(code, str) and code in known_errors else "unknown"
    raw_codes = body.get("error_codes")
    numbers = (
        [str(value) for value in raw_codes if type(value) is int]
        if isinstance(raw_codes, list)
        else []
    )
    if not numbers:
        description = body.get("error_description")
        if isinstance(description, str):
            numbers = re.findall(r"AADSTS(\d+)", description)
    return f"http={status} error={code} aadsts={','.join(numbers) or 'none'}"
