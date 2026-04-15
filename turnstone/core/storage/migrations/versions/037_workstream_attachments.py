"""Add workstream_attachments table for user-uploaded files.

Creates a side table for images and text documents attached to a user
turn.  Lifecycle:

  pending  : message_id IS NULL  AND  reserved_for_msg_id IS NULL
  reserved : message_id IS NULL  AND  reserved_for_msg_id = <queue-msg-id>
  consumed : message_id IS NOT NULL  (reservation cleared on transition)

``message_id`` links to ``conversations.id`` once the user message is
saved.  ``reserved_for_msg_id`` is a soft-lock held by the server
between reserving attachments and dispatching a send, so an attachment
tied to a queued turn can't be re-used, deleted, or auto-consumed by
another send before the queue drains.

Revision ID: 037
Revises: 036
Create Date: 2026-04-15
"""

import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workstream_attachments",
        sa.Column("attachment_id", sa.Text, primary_key=True),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("mime_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("message_id", sa.Integer, nullable=True),
        sa.Column("reserved_for_msg_id", sa.Text, nullable=True),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index(
        "idx_ws_attachments_ws_id",
        "workstream_attachments",
        ["ws_id"],
    )
    op.create_index(
        "idx_ws_attachments_pending",
        "workstream_attachments",
        ["ws_id", "user_id", "message_id"],
    )
    op.create_index(
        "idx_ws_attachments_message",
        "workstream_attachments",
        ["message_id"],
    )
    op.create_index(
        "idx_ws_attachments_reserved",
        "workstream_attachments",
        ["ws_id", "user_id", "reserved_for_msg_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ws_attachments_reserved", table_name="workstream_attachments")
    op.drop_index("idx_ws_attachments_message", table_name="workstream_attachments")
    op.drop_index("idx_ws_attachments_pending", table_name="workstream_attachments")
    op.drop_index("idx_ws_attachments_ws_id", table_name="workstream_attachments")
    op.drop_table("workstream_attachments")
