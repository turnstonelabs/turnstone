"""Tool policy evaluation engine.

Evaluates tool calls against admin-defined policies to determine whether
a tool should be auto-allowed, denied, or require human approval.

Policies are read through a small in-process TTL cache so the per-turn
``approve_tools`` path doesn't hit storage on every assistant turn —
admin-edited policies propagate to new lookups within ``_POLICY_CACHE_TTL``
seconds. Mutation handlers (``storage.create_tool_policy`` /
``update_tool_policy`` / ``delete_tool_policy``) call
:func:`invalidate_policy_cache` for synchronous propagation.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


# Cache window for ``storage.list_tool_policies`` reads. Admin-edited
# config — 60s is short enough that a one-off edit lands quickly without
# manual invalidation, and long enough that a tool-heavy autonomous turn
# (coord + interactive) doesn't hit storage on every assistant turn.
_POLICY_CACHE_TTL: float = 60.0


class _PolicyCache:
    """TTL-keyed snapshot of ``storage.list_tool_policies`` per org_id.

    Reads check the cache under ``self._lock`` (briefly held just long
    enough to copy the policies reference and TTL stamp); on miss the
    caller fetches outside the lock and writes back under it.
    Concurrent misses on the same org_id can produce two SELECTs but
    only one cache slot — last-writer wins, both writers see the same
    data within a tight window so the benign double-fetch is acceptable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def get(
        self,
        storage: StorageBackend,
        org_id: str,
    ) -> list[dict[str, Any]] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(org_id)
            if entry is not None:
                ts, policies = entry
                if now - ts < _POLICY_CACHE_TTL:
                    return policies
        try:
            policies = storage.list_tool_policies(org_id=org_id)
        except Exception:
            log.warning("Failed to load tool policies", exc_info=True)
            return None
        with self._lock:
            self._entries[org_id] = (time.monotonic(), policies)
        return policies

    def invalidate(self, org_id: str | None = None) -> None:
        with self._lock:
            if org_id is None:
                self._entries.clear()
            else:
                self._entries.pop(org_id, None)


_cache = _PolicyCache()

# What the approval gates tell a call needing approval when the policies could
# not be read: no approval, automatic or a person's, may run a call that a deny
# rule might cover.
POLICIES_UNREADABLE_DENIAL = (
    "Blocked: the tool policies could not be read, so this call was not checked "
    "against them and did not run. Try again shortly."
)


def invalidate_policy_cache(org_id: str | None = None) -> None:
    """Drop the cached policy snapshot for ``org_id`` (or all orgs).

    Call after every ``create_tool_policy`` / ``update_tool_policy`` /
    ``delete_tool_policy`` so the next ``evaluate_*`` reads fresh data.
    Pass ``None`` for global invalidation (e.g. test teardown).
    """
    _cache.invalidate(org_id)


def evaluate_loaded_tool_policies(
    storage: StorageBackend,
    tool_names: list[str],
    org_id: str = "",
) -> dict[str, str | None] | None:
    """Evaluate the tool policies for each name, from one cached read.

    Policies are evaluated in priority order (highest first) and the first
    matching one wins: each name maps to ``"allow"``, ``"deny"`` or ``"ask"``,
    or to ``None`` when no policy matches (the default approval flow applies).
    Returns ``None`` when the policies could not be read: the approval gates
    then refuse every call needing approval (:data:`POLICIES_UNREADABLE_DENIAL`).
    """
    policies = _cache.get(storage, org_id)
    if policies is None:
        return None

    results: dict[str, str | None] = {}
    for name in tool_names:
        result = None
        for policy in policies:
            if not policy.get("enabled", True):
                continue
            if fnmatch.fnmatch(name, policy.get("tool_pattern", "")):
                action = policy.get("action", "ask")
                result = action if action in ("allow", "deny", "ask") else "ask"
                break
        results[name] = result
    return results
