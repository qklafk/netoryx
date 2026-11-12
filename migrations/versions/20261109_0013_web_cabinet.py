"""Web cabinet: customer sessions and confirmed e-mail addresses.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cabinet_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name="fk_cabinet_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cabinet_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_cabinet_sessions_token_hash"),
    )
    op.create_index("ix_cabinet_sessions_user_id", "cabinet_sessions", ["user_id"])
    op.create_index("ix_cabinet_sessions_expires_at", "cabinet_sessions", ["expires_at"])
    op.create_table(
        "user_emails",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.tg_id"], name="fk_user_emails_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_emails"),
        sa.UniqueConstraint("email", name="uq_user_emails_email"),
    )


def downgrade() -> None:
    op.drop_table("user_emails")
    op.drop_index("ix_cabinet_sessions_expires_at", table_name="cabinet_sessions")
    op.drop_index("ix_cabinet_sessions_user_id", table_name="cabinet_sessions")
    op.drop_table("cabinet_sessions")
