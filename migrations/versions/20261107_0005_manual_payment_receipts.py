"""Telegram receipts for manual transfers.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("receipt_file_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("receipt_media_type", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("receipt_media_type")
        batch.drop_column("receipt_file_id")
