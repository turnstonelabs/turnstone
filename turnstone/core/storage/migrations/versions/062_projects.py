"""Projects: shared resource containers + memory ``type`` rename ``project`` → ``general``.

Adds the **Projects** feature (1.7): a governed, shareable resource container that groups
workstreams and owns a ``('project', project_id)`` memory recall rung.

Schema:

- ``projects`` — the container (``owner_id``, ``visibility`` ``private|public``, ``state``
  ``active|archived``, reserved nullable ``parent_project_id``).  No FK constraints — matches
  the rest of this schema family (see migration 058's note).
- ``project_members`` — the per-project ACL whitelist (composite PK ``(project_id, user_id)``).
- ``workstreams.project_id`` — nullable; the project a workstream is attached to (mirrors the
  ``parent_ws_id`` shape from migration 039).
- ``project.{create,read,write,delete}`` granted to ``builtin-admin`` (admin-default; opt others in
  via ``role_permission_overrides``), following the 040 pattern.

Data:

- Rename the memory ``type`` value ``'project'`` → ``'general'``.  ``'project'`` was the
  *default* type — i.e. "no particular flavour" — and the word now denotes the new project
  *scope*/entity, so the catch-all flavour is renamed to free it.  Clean break (1.7
  experimental main); no value alias is kept.

The ``structured_memories.type`` server_default is **not** altered on existing DBs: every
application write specifies ``type`` explicitly (default ``'general'`` in code), so the
DB-level default is inert, and altering it would force a full ``structured_memories`` table
rebuild on SQLite for no behavioural gain.  Fresh DBs created from ``_schema.py`` already
carry ``server_default='general'``.

``downgrade()`` drops the projects schema + perms and relabels ``'general'`` → ``'project'``.
Both are the catch-all flavour, so the remap is semantically clean and restores the pre-062
default world that pre-062 code expects.

Revision ID: 062
Revises: 061
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None

_PROJECT_PERMS = ("project.create", "project.read", "project.write", "project.delete")


def _append_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || :sep "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE :needle"
        ),
        {"sep": "," + perm, "needle": "%" + perm + "%"},
    )


def _remove_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, :needle, '') "
            "WHERE role_id = 'builtin-admin'"
        ),
        {"needle": "," + perm},
    )


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("visibility", sa.Text, nullable=False, server_default="private"),
        sa.Column("state", sa.Text, nullable=False, server_default="active"),
        sa.Column("parent_project_id", sa.Text, nullable=True),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_projects_owner", "projects", ["owner_id"])
    op.create_index("idx_projects_visibility", "projects", ["visibility"])

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index("idx_project_members_user", "project_members", ["user_id"])

    op.add_column("workstreams", sa.Column("project_id", sa.Text, nullable=True))
    op.create_index("idx_workstreams_project", "workstreams", ["project_id"])

    conn = op.get_bind()
    # Rename the catch-all memory type flavour to free the word "project" for the scope/entity.
    conn.execute(sa.text("UPDATE structured_memories SET type = 'general' WHERE type = 'project'"))
    # Grant the new perms to builtin-admin (admin-default).
    for perm in _PROJECT_PERMS:
        _append_permission(conn, perm)


def downgrade() -> None:
    conn = op.get_bind()
    for perm in reversed(_PROJECT_PERMS):
        _remove_permission(conn, perm)
    conn.execute(sa.text("UPDATE structured_memories SET type = 'project' WHERE type = 'general'"))

    op.drop_index("idx_workstreams_project", table_name="workstreams")
    op.drop_column("workstreams", "project_id")

    op.drop_index("idx_project_members_user", table_name="project_members")
    op.drop_table("project_members")

    op.drop_index("idx_projects_visibility", table_name="projects")
    op.drop_index("idx_projects_owner", table_name="projects")
    op.drop_table("projects")
