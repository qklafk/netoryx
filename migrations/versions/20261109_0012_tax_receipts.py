"""Receipts of the self-employed tax service ("Мой налог").

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tax_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("service_name", sa.String(256), nullable=False),
        sa.Column("operation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_uuid", sa.String(64), nullable=True),
        sa.Column("receipt_url", sa.String(512), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("user_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tax_receipts"),
        sa.UniqueConstraint("payment_id", name="uq_tax_receipts_payment_id"),
    )
    op.create_index("ix_tax_receipts_user_id", "tax_receipts", ["user_id"])
    op.create_index("ix_tax_receipts_status_next_attempt", "tax_receipts", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_tax_receipts_status_next_attempt", table_name="tax_receipts")
    op.drop_index("ix_tax_receipts_user_id", table_name="tax_receipts")
    op.drop_table("tax_receipts")
