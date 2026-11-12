"""Custom main-menu buttons.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "menu_buttons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("titles", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("row", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False, server_default="all"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_menu_buttons"),
    )
    op.create_index("ix_menu_buttons_row_order", "menu_buttons", ["row", "sort_order"])


def downgrade() -> None:
    op.drop_index("ix_menu_buttons_row_order", table_name="menu_buttons")
    op.drop_table("menu_buttons")
