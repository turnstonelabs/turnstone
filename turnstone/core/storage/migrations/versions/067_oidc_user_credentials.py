"""Add oidc_user_credentials for single-credential MCP token minting.

One encrypted IdP refresh token per (user, issuer), captured at OIDC login
when ``[oidc] capture_user_credential`` is enabled.  Servers with
``auth_type='oauth_obo'`` redeem this single credential on demand for
short-lived per-server access tokens instead of holding a per-(user, server)
refresh token — see issue #551.

Keyed ``(user_id, issuer)`` rather than ``user_id`` alone so a future
multi-IdP login surface needs no re-keying; today's OIDCConfig is
single-issuer, so the table holds at most one row per user in practice.

Revision ID: 067
Revises: 066
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_user_credentials",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("refresh_token_ct", sa.LargeBinary, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("last_refreshed", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "issuer"),
    )


def downgrade() -> None:
    op.drop_table("oidc_user_credentials")
