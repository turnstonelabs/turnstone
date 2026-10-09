"""Notify-target validation shared by workstream create, schedules and skills."""

from __future__ import annotations

import json
from typing import Any

MAX_NOTIFY_TARGETS = 10


def validate_notify_targets(raw: Any, field: str = "notify_targets") -> tuple[str, str]:
    """Validate and normalize a notify-target list.

    Returns ``(json_string, error_message)``; the error is empty on success. *field* names the
    input in error messages.
    """
    if not raw:
        return "[]", ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return "[]", f"{field} must be valid JSON"
    elif isinstance(raw, list):
        parsed = raw
    else:
        return "[]", f"{field} must be a JSON array or string"

    if not isinstance(parsed, list):
        return "[]", f"{field} must be a JSON array"

    if len(parsed) > MAX_NOTIFY_TARGETS:
        return "[]", f"{field} limited to {MAX_NOTIFY_TARGETS} entries"

    normalized: list[dict[str, str]] = []
    for i, t in enumerate(parsed):
        if not isinstance(t, dict):
            return "[]", f"{field}[{i}] must be an object"
        if t.get("channel_type") is None:
            return "[]", f"{field}[{i}] missing channel_type"

        has_channel_id = "channel_id" in t and t.get("channel_id") is not None
        has_user_id = "user_id" in t and t.get("user_id") is not None
        if has_channel_id and has_user_id:
            return "[]", f"{field}[{i}] must specify only one of channel_id or user_id"
        if not has_channel_id and not has_user_id:
            return "[]", f"{field}[{i}] requires channel_id or user_id"

        normalized_target: dict[str, str] = {}
        for key in ("channel_type", "channel_id", "user_id"):
            val = t.get(key)
            if val is None:
                continue
            if not isinstance(val, str):
                return "[]", f"{field}[{i}].{key} must be a non-empty string <= 256 chars"
            stripped = val.strip()
            if not stripped:
                return "[]", f"{field}[{i}].{key} must be a non-empty string <= 256 chars"
            if len(stripped) > 256:
                return "[]", f"{field}[{i}].{key} must be a non-empty string <= 256 chars"
            normalized_target[key] = stripped

        normalized.append(normalized_target)

    return json.dumps(normalized), ""
