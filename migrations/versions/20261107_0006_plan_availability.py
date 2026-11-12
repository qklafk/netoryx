"""Tariff audience rules.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("availability_rule", sa.String(16), nullable=False, server_default="all"))
        batch.add_column(sa.Column("allowed_user_ids", sa.JSON(), nullable=False, server_default=sa.text("('[]')")))


def downgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("allowed_user_ids")
        batch.drop_column("availability_rule")
