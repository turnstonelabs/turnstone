"""Pure RBAC and project-authorization policy.

Database adapters load facts and hold any required locks; this module owns the
decision so transactional memory operations, forks, ordinary authorization,
and derived index health cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def permission_set(value: str | Iterable[str] | None) -> set[str]:
    """Normalize a stored comma list or an iterable into a permission set."""
    values = value.split(",") if isinstance(value, str) else value or ()
    return {str(permission).strip() for permission in values if str(permission).strip()}


def fold_role_permissions(
    baseline: str | Iterable[str] | None,
    *,
    grants: Iterable[str] = (),
    revokes: Iterable[str] = (),
) -> set[str]:
    """Apply one role's additive grants and subtractive revokes."""
    return (permission_set(baseline) | permission_set(grants)) - permission_set(revokes)


@dataclass(frozen=True)
class ProjectAccessDecision:
    can_read: bool
    can_write: bool


def decide_project_access(
    *,
    principal_id: str,
    owner_id: str,
    visibility: str,
    state: str,
    is_member: bool,
    permissions: Iterable[str],
) -> ProjectAccessDecision:
    """Decide whether a principal may use a project at runtime.

    Archived projects remain administrable through
    :func:`decide_project_management_access`, but they are never eligible for
    session attachment, memory recall, or other runtime use.
    """
    if state != "active":
        return ProjectAccessDecision(False, False)
    return decide_project_management_access(
        principal_id=principal_id,
        owner_id=owner_id,
        visibility=visibility,
        is_member=is_member,
        permissions=permissions,
    )


def decide_project_management_access(
    *,
    principal_id: str,
    owner_id: str,
    visibility: str,
    is_member: bool,
    permissions: Iterable[str],
) -> ProjectAccessDecision:
    """Compose the ACL and RBAC facts used by project management surfaces.

    Lifecycle state is deliberately absent: an archived project must remain
    inspectable and reactivatable by the same principals who could manage it
    while active.
    """
    if not principal_id:
        return ProjectAccessDecision(False, False)
    if principal_id == owner_id:
        return ProjectAccessDecision(True, True)
    effective = permission_set(permissions)
    return ProjectAccessDecision(
        can_read="project.read" in effective and (is_member or visibility == "public"),
        can_write="project.write" in effective and is_member,
    )
