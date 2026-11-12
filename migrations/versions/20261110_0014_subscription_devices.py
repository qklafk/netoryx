"""Devices seen on subscription links, for the bot-side device limit.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("hwid_hash", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("os_version", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("app", sa.String(64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name="fk_subscription_devices_subscription_id_subscriptions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscription_devices"),
        sa.UniqueConstraint("subscription_id", "hwid_hash", name="uq_subscription_devices_subscription_id_hwid_hash"),
    )
    op.create_index("ix_subscription_devices_subscription_id", "subscription_devices", ["subscription_id"])
    op.create_index("ix_subscription_devices_last_seen_at", "subscription_devices", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_subscription_devices_last_seen_at", table_name="subscription_devices")
    op.drop_index("ix_subscription_devices_subscription_id", table_name="subscription_devices")
    op.drop_table("subscription_devices")
