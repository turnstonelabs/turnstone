"""Create OIDC identity and pending state tables.

Revision ID: 018
Revises: 017
Create Date: 2026-03-15
"""

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_identities",
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("last_login", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("issuer", "subject"),
    )
    op.create_index("idx_oidc_identities_user_id", "oidc_identities", ["user_id"])

    op.create_table(
        "oidc_pending_states",
        sa.Column("state", sa.Text, primary_key=True),
        sa.Column("nonce", sa.Text, nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        sa.Column("audience", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oidc_pending_states")
    op.drop_table("oidc_identities")
