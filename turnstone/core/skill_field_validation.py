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

from turnstone.core.notify_targets import validate_notify_targets

# Fields that may be updated on installed (``readonly=true``) skills.
# These are local runtime configuration — not part of the SKILL.md spec —
# so they don't compromise the fidelity of an externally-sourced skill.
# Shared between the admin HTTP path (``console/server.py``) and the
# model-tool path (``ChatSession._exec_skills_update``); both consume
# this single source of truth to avoid drift on what counts as a
# runtime field.
SKILL_RUNTIME_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "auto_approve",
        "allowed_tools",
        "enabled",
        "notify_on_complete",
        "priority",
        # ``hidden_from_menu`` is technically a SKILL.md spec field
        # (mapped from ``user-invocable: false``) but admin override
        # is a local UX preference — operators should be able to
        # hide / unhide an installed skill in the picker without
        # unlocking the row.
        "hidden_from_menu",
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

    - ``auto_approve`` / ``enabled``: bool
    - ``notify_on_complete``: the targets a workstream's ``notify_targets`` accept, normalized to
      a JSON array string ("[]" if blank or the legacy ``{}`` sentinel from migrations 011/021);
      ``null`` counts as absent, so a model that sends null for an omitted argument keeps the
      stored targets
    - ``allowed_tools``: JSON array string (accepts list, JSON string,
      or comma-separated CSV string → canonicalized to JSON array)

    A skill carries no model alias, temperature, reasoning effort, max tokens or task-agent turn
    cap: the workstream's alias and the operator's ``tools.agent_max_turns`` setting supply those
    (#1292).  Nor does it carry a token budget or an activation mode.  A body that names any of
    these has them ignored like any unknown key.
    """
    fields: dict[str, Any] = {}

    if "auto_approve" in body:
        fields["auto_approve"] = bool(body.get("auto_approve", False))

    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled", True))

    if body.get("notify_on_complete") is not None:
        nc_raw = body["notify_on_complete"]
        if not isinstance(nc_raw, (str, list)):
            return {}, "notify_on_complete must be a JSON array or string"
        # A blank value and the legacy ``"{}"`` sentinel (migrations 011/021's server_default; rows
        # migration 051 never touched may still carry it) mean no targets.  Anything else passes
        # the check a workstream's ``notify_targets`` get, so a skill never stores targets that
        # workstream create would later drop.
        if isinstance(nc_raw, str) and nc_raw.strip() in ("", "{}"):
            nc_raw = "[]"
        nc, nc_err = validate_notify_targets(nc_raw, field="notify_on_complete")
        if nc_err:
            return {}, nc_err
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
