"""Audit event recording helper.

Provides a fire-and-forget ``record_audit`` function that admin handlers
call after mutations to create a persistent audit trail.

Action-name conventions (non-exhaustive — grep
``record_audit(`` in the tree for the live set):

    coordinator.*       console-side coordinator lifecycle
                        (``coordinator.create`` / ``.close`` /
                        ``.cancel``).

    route.*             multi-node routing proxy hops
                        (``route.workstream.create`` / ``.send`` /
                        ``.close`` / ``.delete``,
                        ``route.approve`` / ``.cancel`` /
                        ``.command`` / ``.plan``).  ``detail`` carries
                        ``{src, node_id, coord_ws_id?}`` so coordinator-
                        origin attribution is preserved without a
                        schema migration.

    <resource>.<verb>   per-resource CRUD on admin handlers — verbs
                        are typically ``create`` / ``update`` /
                        ``delete``.  Resource prefixes in tree today
                        include ``user``, ``role``, ``policy``,
                        ``skill``, ``skill_resource``,
                        ``oidc_identity``, ``mcp_server``,
                        ``model_definition``, ``channel``,
                        ``heuristic_rule``, ``output_guard_pattern``,
                        ``prompt_policy``, ``setting``, ``token``,
                        ``conversation``, ``memory``, ``org``.

When adding a new namespace, prefer extending an existing prefix over
inventing a synonym (e.g. ``mcp_server.refresh`` rather than
``mcp.refresh`` — ``mcp_server.*`` is already the established prefix).
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


def record_audit(
    storage: StorageBackend,
    user_id: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    detail: dict[str, Any] | None = None,
    ip_address: str = "",
) -> None:
    """Record an audit event. Silently logs on failure (never raises)."""
    try:
        storage.record_audit_event(
            event_id=uuid.uuid4().hex,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=json.dumps(detail) if detail else "{}",
            ip_address=ip_address,
        )
    except Exception:
        log.warning("Failed to record audit event: %s %s", action, resource_id, exc_info=True)
