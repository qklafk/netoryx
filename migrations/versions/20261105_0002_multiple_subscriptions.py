"""Independent subscriptions and payment targets.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch:
        # The plain index must exist before the unique one goes (MySQL needs an index for the user_id foreign key).
        batch.create_index("ix_subscriptions_user_id", ["user_id"])
        batch.drop_constraint("uq_subscriptions_user_id", type_="unique")
        batch.add_column(sa.Column("label", sa.String(64), nullable=False, server_default="Подписка"))
        batch.add_column(sa.Column("sub_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("traffic_limit_bytes", sa.BigInteger(), nullable=True))
        batch.create_unique_constraint("uq_subscriptions_sub_token", ["sub_token"])
    op.execute(sa.text("UPDATE subscriptions SET sub_token = (SELECT users.sub_token FROM users WHERE users.tg_id = subscriptions.user_id)"))
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("subscription_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("new_subscription", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("subscription_label", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_payments_subscription_id_subscriptions", "subscriptions", ["subscription_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_payments_subscription_id", ["subscription_id"])
    with op.batch_alter_table("purchases") as batch:
        batch.add_column(sa.Column("subscription_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_purchases_subscription_id_subscriptions", "subscriptions", ["subscription_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_purchases_subscription_id", ["subscription_id"])
    for table in ("payments", "purchases"):
        op.execute(sa.text(f"UPDATE {table} SET subscription_id = (SELECT subscriptions.id FROM subscriptions WHERE subscriptions.user_id = {table}.user_id)"))


def downgrade() -> None:
    duplicate = op.get_bind().execute(sa.text("SELECT user_id FROM subscriptions GROUP BY user_id HAVING COUNT(*) > 1")).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade while a user has multiple subscriptions")
    for table in ("payments", "purchases"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_subscription_id_subscriptions", type_="foreignkey")
            batch.drop_index(f"ix_{table}_subscription_id")
            batch.drop_column("subscription_id")
            if table == "payments":
                batch.drop_column("new_subscription")
                batch.drop_column("subscription_label")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_constraint("uq_subscriptions_sub_token", type_="unique")
        for name in ("label", "sub_token", "is_legacy", "traffic_limit_bytes"):
            batch.drop_column(name)
        batch.create_unique_constraint("uq_subscriptions_user_id", ["user_id"])
        batch.drop_index("ix_subscriptions_user_id")
