"""Personal SOCKS5 proxy accounts for Telegram.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tg_socks_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_enc", sa.Text(), nullable=False),
        sa.Column(
            "desired_state",
            sa.Enum("enabled", "disabled", "absent", name="tgs_desired", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "actual_state",
            sa.Enum("enabled", "disabled", "absent", name="tgs_actual", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("synced_revision", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["inbound_id"], ["inbounds.id"], name=op.f("fk_tg_socks_access_inbound_id_inbounds"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_tg_socks_access_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_tg_socks_access_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tg_socks_access")),
        sa.UniqueConstraint("user_id", "panel_id", name=op.f("uq_tg_socks_access_user_id_panel_id")),
    )
    op.create_index(
        "ix_tg_socks_access_panel_next_attempt", "tg_socks_access", ["panel_id", "next_attempt_at"], unique=False
    )
    op.create_index(op.f("ix_tg_socks_access_user_id"), "tg_socks_access", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tg_socks_access_user_id"), table_name="tg_socks_access")
    op.drop_index("ix_tg_socks_access_panel_next_attempt", table_name="tg_socks_access")
    op.drop_table("tg_socks_access")
