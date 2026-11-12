"""Personal discount groups and locked invoice plan prices.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("plan_price_kop", sa.BigInteger(), nullable=True))
    op.create_table(
        "promo_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("discount_percent > 0 AND discount_percent < 100", name="discount_percent_range"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title"),
    )
    op.create_table(
        "promo_group_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["promo_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.tg_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id"),
    )
    op.create_index("ix_promo_group_members_user", "promo_group_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_promo_group_members_user", table_name="promo_group_members")
    op.drop_table("promo_group_members")
    op.drop_table("promo_groups")
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("plan_price_kop")
