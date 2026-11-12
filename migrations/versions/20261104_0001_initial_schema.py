"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit_log")),
    )
    op.create_index("ix_admin_audit_log_actor_created", "admin_audit_log", ["actor_id", "created_at"], unique=False)
    op.create_index(op.f("ix_admin_audit_log_created_at"), "admin_audit_log", ["created_at"], unique=False)
    op.create_index(op.f("ix_admin_audit_log_target_id"), "admin_audit_log", ["target_id"], unique=False)
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_sessions_token_hash")),
    )
    op.create_index(op.f("ix_admin_sessions_admin_id"), "admin_sessions", ["admin_id"], unique=False)
    op.create_index(op.f("ix_admin_sessions_expires_at"), "admin_sessions", ["expires_at"], unique=False)
    op.create_table(
        "admins",
        sa.Column("tg_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "support", "viewer", name="admin_role", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tg_id", name=op.f("pk_admins")),
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("buttons", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("draft", "running", "done", "canceled", name="broadcast_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Integer(), nullable=False),
        sa.Column("last_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=True),
        sa.Column("data", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_type_created", "events", ["type", "created_at"], unique=False)
    op.create_index("ix_events_user_created", "events", ["user_id", "created_at"], unique=False)
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("traffic_limit_gb", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("code", name=op.f("uq_invites_code")),
    )
    op.create_table(
        "kv",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_kv")),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("flag", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locations")),
    )
    op.create_table(
        "panels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("xui", "remnawave", "telemt", name="panel_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("api_token", sa.Text(), nullable=True),
        sa.Column("tls_verify", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panels")),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("price_kop", sa.BigInteger(), nullable=False),
        sa.Column("price_stars", sa.Integer(), nullable=True),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("traffic_limit_gb", sa.Integer(), nullable=False),
        sa.Column("includes_tg_proxy", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("duration_days > 0", name=op.f("ck_plans_duration_positive")),
        sa.CheckConstraint("price_kop >= 0", name=op.f("ck_plans_price_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_table(
        "text_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_text_overrides")),
        sa.UniqueConstraint("key", "language", name=op.f("uq_text_overrides_key_language")),
    )
    op.create_table(
        "users",
        sa.Column("tg_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=256), nullable=True),
        sa.Column("language", sa.String(length=8), server_default="ru", nullable=False),
        sa.Column("balance_kop", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("referral_balance_kop", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("referral_code", sa.String(length=32), nullable=False),
        sa.Column("referred_by", sa.BigInteger(), nullable=True),
        sa.Column("sub_token", sa.String(length=64), nullable=False),
        sa.Column("trial_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("receipt_email", sa.String(length=256), nullable=True),
        sa.Column("bot_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_banned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("balance_kop >= 0", name=op.f("ck_users_balance_non_negative")),
        sa.CheckConstraint("referral_balance_kop >= 0", name=op.f("ck_users_referral_balance_non_negative")),
        sa.ForeignKeyConstraint(
            ["referred_by"], ["users.tg_id"], name=op.f("fk_users_referred_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("tg_id", name=op.f("pk_users")),
        sa.UniqueConstraint("referral_code", name=op.f("uq_users_referral_code")),
        sa.UniqueConstraint("sub_token", name=op.f("uq_users_sub_token")),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"], unique=False)
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"], unique=False)
    op.create_index(op.f("ix_users_referred_by"), "users", ["referred_by"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=False)
    op.create_table(
        "balance_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "account",
            sa.Enum("main", "referral", name="ledger_account", native_enum=False, length=32),
            server_default="main",
            nullable=False,
        ),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "payment",
                "purchase",
                "gift_purchase",
                "referral_invitee",
                "referral_inviter",
                "welcome_bonus",
                "promo",
                "admin_adjust",
                "refund",
                "referral_commission",
                "referral_transfer",
                "withdrawal",
                name="tx_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("ref", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_balance_transactions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_transactions")),
        sa.UniqueConstraint(
            "account", "kind", "ref", "user_id", name=op.f("uq_balance_transactions_account_kind_ref_user_id")
        ),
    )
    op.create_index(
        "ix_balance_transactions_user_created", "balance_transactions", ["user_id", "created_at"], unique=False
    )
    op.create_table(
        "inbounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("remote_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("params", sa.Text(), nullable=True),
        sa.Column("params_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flow", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["location_id"], ["locations.id"], name=op.f("fk_inbounds_location_id_locations"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_inbounds_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbounds")),
        sa.UniqueConstraint("panel_id", "remote_id", name=op.f("uq_inbounds_panel_id_remote_id")),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("is_gift", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_donation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_recurring", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("provider_amount", sa.String(length=64), nullable=True),
        sa.Column("provider_currency", sa.String(length=16), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "pending",
                "succeeded",
                "canceled",
                "expired",
                "refunded",
                name="payment_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("confirmation_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name=op.f("fk_payments_plan_id_plans"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_payments_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("provider", "provider_payment_id", name=op.f("uq_payments_provider_provider_payment_id")),
    )
    op.create_index("ix_payments_credited_at", "payments", ["credited_at"], unique=False)
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"], unique=False)
    op.create_index("ix_payments_user_created", "payments", ["user_id", "created_at"], unique=False)
    op.create_index(op.f("ix_payments_user_id"), "payments", ["user_id"], unique=False)
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.Enum("promo", "gift", name="promo_kind", native_enum=False, length=32), nullable=False),
        sa.Column("bonus_kop", sa.BigInteger(), nullable=False),
        sa.Column("bonus_days", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("only_new_users", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_promo_codes_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_promo_codes_code")),
    )
    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "trial",
                "purchase",
                "auto_renew",
                "gift_redeem",
                "promo",
                "admin_grant",
                "invite",
                "referral_reward",
                name="purchase_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("price_kop", sa.BigInteger(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("expires_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_id", sa.String(length=36), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_purchases_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_purchases_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchases")),
    )
    op.create_index("ix_purchases_user_created", "purchases", ["user_id", "created_at"], unique=False)
    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("beneficiary_id", sa.BigInteger(), nullable=False),
        sa.Column("source_user_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column(
            "account",
            sa.Enum("main", "referral", name="ledger_account_rr", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["beneficiary_id"],
            ["users.tg_id"],
            name=op.f("fk_referral_rewards_beneficiary_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referral_rewards")),
        sa.UniqueConstraint("source_ref", "level", name=op.f("uq_referral_rewards_source_ref_level")),
    )
    op.create_index(
        "ix_referral_rewards_beneficiary_created", "referral_rewards", ["beneficiary_id", "created_at"], unique=False
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("client_uuid", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "expired", "disabled", name="subscription_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("traffic_limit_gb", sa.Integer(), nullable=False),
        sa.Column("auto_renew", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_trial", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_complimentary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_reminder_days", sa.Integer(), nullable=True),
        sa.Column("stars_charge_id", sa.String(length=128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("traffic_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("traffic_exhausted_epoch", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_subscriptions_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_subscriptions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint("client_uuid", name=op.f("uq_subscriptions_client_uuid")),
        sa.UniqueConstraint("user_id", name=op.f("uq_subscriptions_user_id")),
    )
    op.create_index("ix_subscriptions_status_expires", "subscriptions", ["status", "expires_at"], unique=False)
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "answered", "closed", name="ticket_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_tickets_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index("ix_tickets_status_updated", "tickets", ["status", "updated_at"], unique=False)
    op.create_index(op.f("ix_tickets_user_id"), "tickets", ["user_id"], unique=False)
    op.create_table(
        "withdrawals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_kop", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("details", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "paid", "rejected", name="withdrawal_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_comment", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_kop > 0", name=op.f("ck_withdrawals_amount_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_withdrawals_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_withdrawals")),
    )
    op.create_index("ix_withdrawals_status_created", "withdrawals", ["status", "created_at"], unique=False)
    op.create_index(op.f("ix_withdrawals_user_id"), "withdrawals", ["user_id"], unique=False)
    op.create_table(
        "endpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column(
            "kind",
            sa.Enum("direct", "relay", "cdn", name="endpoint_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "profile",
            sa.Enum("standard", "whitelist", name="endpoint_profile", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("overrides", sa.Text(), nullable=True),
        sa.Column("remark", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("auto_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["inbound_id"], ["inbounds.id"], name=op.f("fk_endpoints_inbound_id_inbounds"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["locations.id"], name=op.f("fk_endpoints_location_id_locations"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoints")),
    )
    op.create_index(op.f("ix_endpoints_inbound_id"), "endpoints", ["inbound_id"], unique=False)
    op.create_table(
        "promo_activations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("promo_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["promo_id"], ["promo_codes.id"], name=op.f("fk_promo_activations_promo_id_promo_codes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_promo_activations_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_activations")),
        sa.UniqueConstraint("promo_id", "user_id", name=op.f("uq_promo_activations_promo_id_user_id")),
    )
    op.create_index(op.f("ix_promo_activations_user_id"), "promo_activations", ["user_id"], unique=False)
    op.create_table(
        "provisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=True),
        sa.Column("inbound_key", sa.Integer(), nullable=False),
        sa.Column("remote_email", sa.String(length=128), nullable=True),
        sa.Column("remote_id", sa.String(length=64), nullable=True),
        sa.Column("remote_short_id", sa.String(length=64), nullable=True),
        sa.Column("links_cache", sa.Text(), nullable=True),
        sa.Column(
            "desired_state",
            sa.Enum("enabled", "disabled", "absent", name="provision_desired", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "actual_state",
            sa.Enum("enabled", "disabled", "absent", name="provision_actual", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("synced_revision", sa.Integer(), nullable=False),
        sa.Column("traffic_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["inbound_id"], ["inbounds.id"], name=op.f("fk_provisions_inbound_id_inbounds"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_provisions_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_provisions_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisions")),
        sa.UniqueConstraint(
            "subscription_id",
            "panel_id",
            "inbound_key",
            name=op.f("uq_provisions_subscription_id_panel_id_inbound_key"),
        ),
    )
    op.create_index(op.f("ix_provisions_panel_id"), "provisions", ["panel_id"], unique=False)
    op.create_index("ix_provisions_panel_next_attempt", "provisions", ["panel_id", "next_attempt_at"], unique=False)
    op.create_table(
        "tg_proxy_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=True),
        sa.Column("remote_name", sa.String(length=64), nullable=False),
        sa.Column("links_cache", sa.Text(), nullable=True),
        sa.Column(
            "desired_state",
            sa.Enum("enabled", "disabled", "absent", name="tgp_desired", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "actual_state",
            sa.Enum("enabled", "disabled", "absent", name="tgp_actual", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("synced_revision", sa.Integer(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["inbound_id"], ["inbounds.id"], name=op.f("fk_tg_proxy_access_inbound_id_inbounds"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["panel_id"], ["panels.id"], name=op.f("fk_tg_proxy_access_panel_id_panels"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.tg_id"], name=op.f("fk_tg_proxy_access_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tg_proxy_access")),
        sa.UniqueConstraint("user_id", "panel_id", name=op.f("uq_tg_proxy_access_user_id_panel_id")),
    )
    op.create_index(
        "ix_tg_proxy_access_panel_next_attempt", "tg_proxy_access", ["panel_id", "next_attempt_at"], unique=False
    )
    op.create_index(op.f("ix_tg_proxy_access_user_id"), "tg_proxy_access", ["user_id"], unique=False)
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_id", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_ticket_messages_ticket_id_tickets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
    )
    op.create_index(op.f("ix_ticket_messages_ticket_id"), "ticket_messages", ["ticket_id"], unique=False)
    op.create_table(
        "usage_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_usage_snapshots_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_snapshots")),
        sa.UniqueConstraint("subscription_id", "day", name=op.f("uq_usage_snapshots_subscription_id_day")),
    )


def downgrade() -> None:
    op.drop_table("usage_snapshots")
    op.drop_index(op.f("ix_ticket_messages_ticket_id"), table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index(op.f("ix_tg_proxy_access_user_id"), table_name="tg_proxy_access")
    op.drop_index("ix_tg_proxy_access_panel_next_attempt", table_name="tg_proxy_access")
    op.drop_table("tg_proxy_access")
    op.drop_index("ix_provisions_panel_next_attempt", table_name="provisions")
    op.drop_index(op.f("ix_provisions_panel_id"), table_name="provisions")
    op.drop_table("provisions")
    op.drop_index(op.f("ix_promo_activations_user_id"), table_name="promo_activations")
    op.drop_table("promo_activations")
    op.drop_index(op.f("ix_endpoints_inbound_id"), table_name="endpoints")
    op.drop_table("endpoints")
    op.drop_index(op.f("ix_withdrawals_user_id"), table_name="withdrawals")
    op.drop_index("ix_withdrawals_status_created", table_name="withdrawals")
    op.drop_table("withdrawals")
    op.drop_index(op.f("ix_tickets_user_id"), table_name="tickets")
    op.drop_index("ix_tickets_status_updated", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_subscriptions_status_expires", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_referral_rewards_beneficiary_created", table_name="referral_rewards")
    op.drop_table("referral_rewards")
    op.drop_index("ix_purchases_user_created", table_name="purchases")
    op.drop_table("purchases")
    op.drop_table("promo_codes")
    op.drop_index(op.f("ix_payments_user_id"), table_name="payments")
    op.drop_index("ix_payments_user_created", table_name="payments")
    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_index("ix_payments_credited_at", table_name="payments")
    op.drop_table("payments")
    op.drop_table("inbounds")
    op.drop_index("ix_balance_transactions_user_created", table_name="balance_transactions")
    op.drop_table("balance_transactions")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_referred_by"), table_name="users")
    op.drop_index("ix_users_last_seen_at", table_name="users")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_table("users")
    op.drop_table("text_overrides")
    op.drop_table("plans")
    op.drop_table("panels")
    op.drop_table("locations")
    op.drop_table("kv")
    op.drop_table("invites")
    op.drop_index("ix_events_user_created", table_name="events")
    op.drop_index("ix_events_type_created", table_name="events")
    op.drop_table("events")
    op.drop_table("broadcasts")
    op.drop_table("admins")
    op.drop_index(op.f("ix_admin_sessions_expires_at"), table_name="admin_sessions")
    op.drop_index(op.f("ix_admin_sessions_admin_id"), table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_index(op.f("ix_admin_audit_log_target_id"), table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_created_at"), table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_actor_created", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
