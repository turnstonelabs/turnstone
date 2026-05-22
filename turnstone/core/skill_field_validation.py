"""Skill field validation — shared between the admin HTTP path and the
model-facing ``skills`` tool exec path.

Single source of truth for what shape each field on a skill row may
take.  The HTTP path wraps the string error into a 400 JSONResponse;
the model-tool path surfaces it via ``_coord_tool_error``.  Either
caller can trust that validation cannot drift between layers because
both go through this function.
"""

from __future__ import annotations

import json
from typing import Any

_VALID_ACTIVATIONS: frozenset[str] = frozenset({"named", "default", "search"})

# Fields that may be updated on installed (``readonly=true``) skills.
# These are local runtime configuration — not part of the SKILL.md spec —
# so they don't compromise the fidelity of an externally-sourced skill.
# Shared between the admin HTTP path (``console/server.py``) and the
# model-tool path (``ChatSession._exec_skills_update``); both consume
# this single source of truth to avoid drift on what counts as a
# runtime field.
SKILL_RUNTIME_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "temperature",
        "reasoning_effort",
        "max_tokens",
        "token_budget",
        "agent_max_turns",
        "auto_approve",
        "allowed_tools",
        "enabled",
        "notify_on_complete",
        "priority",
    }
)


def parse_skill_session_config(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate session-config fields on a skill create/update body.

    Returns ``(fields, error)``.  ``fields`` contains only the keys
    present in ``body`` (partial-update friendly), with values
    normalized to storage shape.  ``error`` is ``None`` on success or
    a human-readable message on failure — never a JSONResponse, never
    a raise.  Callers wrap into their own transport-shaped error.

    Field rules:

    - ``temperature``: float in [0.0, 2.0] or None / "" → None.
      Non-numeric input (string that doesn't parse, dict, list) errors
      out — matches ``max_tokens`` / ``token_budget`` for numeric-field
      consistency.
    - ``max_tokens``: int >= 1 or None / "" → None
    - ``token_budget``: int >= 0 (defaults to 0 if missing-but-empty)
    - ``agent_max_turns``: int >= 1 or None / "" → None
    - ``reasoning_effort``: string (stripped)
    - ``auto_approve`` / ``enabled``: bool
    - ``activation``: one of ``_VALID_ACTIVATIONS``
    - ``notify_on_complete``: JSON array string ("[]" if blank or
      legacy ``{}`` sentinel from migrations 011/021)
    - ``allowed_tools``: JSON array string (accepts list, JSON string,
      or comma-separated CSV string → canonicalized to JSON array)
    - ``model``: string (stripped)
    """
    fields: dict[str, Any] = {}

    if "model" in body:
        fields["model"] = str(body["model"] or "").strip()

    if "temperature" in body:
        temp = body["temperature"]
        if temp is None or temp == "":
            fields["temperature"] = None
        else:
            try:
                temp = float(temp)
            except (ValueError, TypeError):
                return {}, "temperature must be a number between 0 and 2"
            if not (0.0 <= temp <= 2.0):
                return {}, "temperature must be between 0 and 2"
            fields["temperature"] = temp

    if "token_budget" in body:
        try:
            tb = int(body.get("token_budget", 0) or 0)
        except (ValueError, TypeError):
            return {}, "token_budget must be an integer"
        if tb < 0:
            return {}, "token_budget must be non-negative"
        fields["token_budget"] = tb

    if "max_tokens" in body:
        mt = body["max_tokens"]
        if mt is not None and mt != "":
            try:
                mt = int(mt)
            except (ValueError, TypeError):
                return {}, "max_tokens must be an integer"
            if mt < 1:
                return {}, "max_tokens must be positive"
            fields["max_tokens"] = mt
        else:
            fields["max_tokens"] = None

    if "agent_max_turns" in body:
        amt = body["agent_max_turns"]
        if amt is not None and amt != "":
            try:
                amt = int(amt)
            except (ValueError, TypeError):
                return {}, "agent_max_turns must be an integer"
            if amt < 1:
                return {}, "agent_max_turns must be positive"
            fields["agent_max_turns"] = amt
        else:
            fields["agent_max_turns"] = None

    if "reasoning_effort" in body:
        fields["reasoning_effort"] = str(body["reasoning_effort"] or "").strip()

    if "auto_approve" in body:
        fields["auto_approve"] = bool(body.get("auto_approve", False))

    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled", True))

    if "activation" in body:
        activation = str(body["activation"] or "named").strip()
        if activation not in _VALID_ACTIVATIONS:
            return {}, (f"activation must be one of: {', '.join(sorted(_VALID_ACTIVATIONS))}")
        fields["activation"] = activation

    if "notify_on_complete" in body:
        nc_raw = body.get("notify_on_complete", "[]")
        # Accept list input from the model-tool path (the JSON-schema
        # declares this field as ``type: array``).  The HTTP path may
        # still send a JSON-encoded string body, so a str-input
        # fallback stays for the other branch.  ``str(list)`` produces
        # Python repr with single quotes and breaks ``json.loads`` —
        # don't go through that path on a list input.
        nc = json.dumps(nc_raw) if isinstance(nc_raw, list) else str(nc_raw).strip()
        # Normalise empty/whitespace and the legacy ``"{}"`` sentinel
        # (inherited from migrations 011/021's server_default — older
        # rows that haven't been touched by migration 051 may still
        # carry it) to the canonical empty-array literal so a blank
        # field can never bypass validation and persist a non-JSON
        # value.
        if not nc or nc == "{}":
            nc = "[]"
        if nc != "[]":
            try:
                parsed = json.loads(nc)
            except (json.JSONDecodeError, TypeError):
                return {}, "notify_on_complete must be valid JSON"
            if not isinstance(parsed, list):
                return {}, "notify_on_complete must be a JSON array"
        fields["notify_on_complete"] = nc

    if "allowed_tools" in body:
        at_raw = body.get("allowed_tools", "[]")
        if isinstance(at_raw, list):
            fields["allowed_tools"] = json.dumps(at_raw)
        else:
            at_str = str(at_raw).strip()
            if at_str and not at_str.startswith("["):
                at_str = json.dumps([t.strip() for t in at_str.split(",") if t.strip()])
            try:
                json.loads(at_str or "[]")
            except (ValueError, TypeError):
                at_str = "[]"
            fields["allowed_tools"] = at_str or "[]"

    return fields, None
