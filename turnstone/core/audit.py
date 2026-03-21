"""Audit event recording helper.

Provides a fire-and-forget ``record_audit`` function that admin handlers
call after mutations to create a persistent audit trail.
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
