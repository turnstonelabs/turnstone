"""Capture Entra `oid`/`tid` on oidc_identities.

Adds the stable, cross-application user key to OIDC identity rows. The existing
`subject` column holds the OIDC `sub`, which on Microsoft Entra is a PAIRWISE
identifier — a different value in every application — so it cannot correlate a
user across services. `oid` (directory object id) + `tid` (tenant id) are stable
across all apps in the tenant and are what external services should match on.

Both columns are nullable-free with a "" server default so existing rows migrate
cleanly; they are populated on each user's next login (see
`turnstone.core.oidc.provision_oidc_user`). Additive and reversible.

Revision ID: 065
Revises: 064
Create Date: 2026-07-04
"""

import sqlalchemy as sa
from alembic import op

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_identities") as batch_op:
        batch_op.add_column(sa.Column("oid", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("tid", sa.Text, nullable=False, server_default=""))
    op.create_index("idx_oidc_identities_oid", "oidc_identities", ["oid"])


def downgrade() -> None:
    op.drop_index("idx_oidc_identities_oid", table_name="oidc_identities")
    with op.batch_alter_table("oidc_identities") as batch_op:
        batch_op.drop_column("tid")
        batch_op.drop_column("oid")
