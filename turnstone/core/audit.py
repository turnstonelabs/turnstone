"""Audit event recording helper.

Provides a fire-and-forget ``record_audit`` function that admin handlers
call after mutations to create a persistent audit trail.

Action-name conventions (non-exhaustive — grep
``record_audit(`` in the tree for the live set):

    coordinator.*       console-side coordinator lifecycle
                        (``coordinator.create`` / ``.close`` /
                        ``.cancel``) plus governance sub-prefixes
                        (``coordinator.trust.toggled``,
                        ``coordinator.send.auto_approved``,
                        ``coordinator.restricted``,
                        ``coordinator.stopped_cascade``).

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
import re
import uuid
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.output_guard import redact_credentials

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


# C0 control chars except tab/newline, plus DEL.  Stripped after the
# credential scrub so any downstream exporter that pulls ``detail``
# and renders raw strings cannot re-surface CR/LF injection from
# model-controlled content — the audit row itself is already safe
# because ``json.dumps`` escapes the bytes, but JSON-loaded consumers
# often print the inner strings directly.
_AUDIT_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _scrub_string(value: str) -> str:
    """Redact credentials and strip stray control chars from a single string."""
    return _AUDIT_CONTROL_CHAR_RE.sub(" ", redact_credentials(value))


def _has_any_string(value: Any) -> bool:
    """Return True if ``value`` or any nested value is a string.

    Fast-path guard for :func:`_redact_detail`: audit details that carry
    only bools, ints, floats, and None (common for ``{"spawned": 5,
    "ok": True}`` shapes) skip the recursive scrub entirely.  The
    traversal mirrors the shape ``_redact_detail`` recurses into so a
    False return actually means there's nothing to scrub.
    """
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return any(isinstance(k, str) for k in value) or any(
            _has_any_string(v) for v in value.values()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_has_any_string(v) for v in value)
    return False


def _redact_detail(value: Any) -> Any:
    """Recursively scrub strings inside ``value`` before audit persistence.

    Walks dicts, lists, tuples, sets, and frozensets; applies
    :func:`redact_credentials` + a control-char scrub to every string,
    including dict keys.  Non-string scalars pass through unchanged.
    Sets and frozensets are converted to sorted lists — JSON has no
    native set type, and storing them as lists is what ``json.dumps``
    would do for any downstream reader anyway.
    """
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {
            (_scrub_string(k) if isinstance(k, str) else k): _redact_detail(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_detail(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_detail(v) for v in value)
    if isinstance(value, (set, frozenset)):
        # sorted() needs uniform item types; cast to str as a last
        # resort so a mixed set still produces a stable output.
        scrubbed = [_redact_detail(v) for v in value]
        try:
            return sorted(scrubbed)
        except TypeError:
            return sorted(scrubbed, key=repr)
    return value


def record_audit(
    storage: StorageBackend,
    user_id: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    detail: dict[str, Any] | None = None,
    ip_address: str = "",
    *,
    raw_detail: bool = False,
) -> None:
    """Record an audit event. Silently logs on failure (never raises).

    By default every string inside ``detail`` is routed through
    :func:`redact_credentials` before the row is persisted, so
    model-controlled text (task titles, send/spawn messages, close
    reasons) cannot leak credentials into the audit table.  Callers
    that need to preserve the raw payload — e.g. an operator-supplied
    field an admin explicitly asked to keep intact for an
    investigation — can pass ``raw_detail=True`` to opt out.  Every
    model→audit path MUST leave ``raw_detail`` at its default.
    """
    if detail and not raw_detail and _has_any_string(detail):
        detail = _redact_detail(detail)
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
