"""TLS certificate storage for lacme ACME integration.

Three tables for the lacme Store protocol:
- tls_account_keys: ACME account private keys
- tls_ca: CA root certificate and key
- tls_certificates: Issued service certificates

Revision ID: 026
Revises: 025
Create Date: 2026-03-25
"""

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tls_account_keys",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("key_pem", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
    )

    op.create_table(
        "tls_ca",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("cert_pem", sa.Text, nullable=False),
        sa.Column("key_pem", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
    )

    op.create_table(
        "tls_certificates",
        sa.Column("domain", sa.Text, primary_key=True),
        sa.Column("cert_pem", sa.Text, nullable=False),
        sa.Column("fullchain_pem", sa.Text, nullable=False),
        sa.Column("key_pem", sa.Text, nullable=False),
        sa.Column("issued_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("meta", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("tls_certificates")
    op.drop_table("tls_ca")
    op.drop_table("tls_account_keys")
