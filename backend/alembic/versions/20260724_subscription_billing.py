"""Add account, entitlement, payment and audit tables.

Revision ID: 20260724_subscription_billing
Revises: 567ae56c545c
"""
from alembic import op
import sqlalchemy as sa

revision = "20260724_subscription_billing"
down_revision = "567ae56c545c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_role", "users", ["role"])
    op.create_table("subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("grace_ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_ends_at", "subscriptions", ["ends_at"])
    op.create_table("payments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128)),
        sa.Column("order_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False), sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("pay_currency", sa.String(length=32)), sa.Column("pay_amount", sa.String(length=64)), sa.Column("payment_address", sa.String(length=256)),
        sa.Column("transaction_hash", sa.String(length=256), unique=True), sa.Column("confirmations", sa.Integer(), nullable=False),
        sa.Column("raw_payload", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_id"), sa.UniqueConstraint("order_id", name="uq_payment_order_id"),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"]); op.create_index("ix_payments_subscription_id", "payments", ["subscription_id"]); op.create_index("ix_payments_status", "payments", ["status"])
    op.create_table("refresh_tokens", sa.Column("id", sa.String(length=36), primary_key=True), sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False))
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"]); op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])
    op.create_table("audit_events", sa.Column("id", sa.String(length=36), primary_key=True), sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("event_type", sa.String(length=64), nullable=False), sa.Column("ip_address", sa.String(length=64)), sa.Column("metadata_json", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False))
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"]); op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("audit_events"); op.drop_table("refresh_tokens"); op.drop_table("payments"); op.drop_table("subscriptions"); op.drop_table("users")
