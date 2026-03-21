"""Tool policy evaluation engine.

Evaluates tool calls against admin-defined policies to determine whether
a tool should be auto-allowed, denied, or require human approval.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


def evaluate_tool_policy(
    storage: StorageBackend,
    tool_name: str,
    org_id: str = "",
) -> str | None:
    """Check tool policies for *tool_name*.

    Policies are evaluated in priority order (highest first).  The first
    matching policy wins.

    Returns ``"allow"``, ``"deny"``, or ``"ask"`` if a policy matches,
    or ``None`` if no policy matches (caller should fall through to the
    default approval behaviour).
    """
    try:
        policies = storage.list_tool_policies(org_id=org_id)
    except Exception:
        log.warning("Failed to load tool policies", exc_info=True)
        return None

    for policy in policies:
        if not policy.get("enabled", True):
            continue
        pattern = policy.get("tool_pattern", "")
        if fnmatch.fnmatch(tool_name, pattern):
            action: str = policy.get("action", "ask")
            if action in ("allow", "deny", "ask"):
                return action
            log.warning("Unknown policy action %r for policy %s", action, policy.get("policy_id"))
            return "ask"

    return None


def evaluate_tool_policies_batch(
    storage: StorageBackend,
    tool_names: list[str],
    org_id: str = "",
) -> dict[str, str | None]:
    """Evaluate policies for multiple tools at once (single DB query).

    Returns a dict mapping each tool name to its policy result.
    """
    try:
        policies = storage.list_tool_policies(org_id=org_id)
    except Exception:
        log.warning("Failed to load tool policies", exc_info=True)
        return {name: None for name in tool_names}

    results: dict[str, str | None] = {}
    for name in tool_names:
        result = None
        for policy in policies:
            if not policy.get("enabled", True):
                continue
            pattern = policy.get("tool_pattern", "")
            if fnmatch.fnmatch(name, pattern):
                action = policy.get("action", "ask")
                result = action if action in ("allow", "deny", "ask") else "ask"
                break
        results[name] = result
    return results
