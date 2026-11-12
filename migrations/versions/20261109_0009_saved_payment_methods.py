"""Saved payment methods for auto-renewal.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_payment_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name="fk_saved_payment_methods_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_payment_methods"),
        sa.UniqueConstraint("provider", "token", name="uq_saved_payment_methods_provider_token"),
    )
    op.create_index("ix_saved_payment_methods_user_id", "saved_payment_methods", ["user_id"])
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("save_method", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("saved_method_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_payments_saved_method_id_saved_payment_methods",
            "saved_payment_methods",
            ["saved_method_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_payments_saved_method_id", ["saved_method_id"])


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        # The foreign key goes first: MySQL refuses to drop an index that a foreign key still relies on.
        batch.drop_constraint("fk_payments_saved_method_id_saved_payment_methods", type_="foreignkey")
        batch.drop_index("ix_payments_saved_method_id")
        batch.drop_column("saved_method_id")
        batch.drop_column("save_method")
    op.drop_index("ix_saved_payment_methods_user_id", table_name="saved_payment_methods")
    op.drop_table("saved_payment_methods")
