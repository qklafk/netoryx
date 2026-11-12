"""Paid subscription add-ons.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_addon_purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_kop", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_subscription_addon_purchases_subscription_id", "subscription_addon_purchases", ["subscription_id"]
    )
    op.create_index("ix_addon_purchases_user_created", "subscription_addon_purchases", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_addon_purchases_user_created", table_name="subscription_addon_purchases")
    op.drop_index("ix_subscription_addon_purchases_subscription_id", table_name="subscription_addon_purchases")
    op.drop_table("subscription_addon_purchases")
