"""Store the provider invoice ID for hosted-checkout reconciliation.

Revision ID: 20260724_payment_reconciliation
Revises: 20260724_subscription_billing
"""
from alembic import op
import sqlalchemy as sa

revision = "20260724_payment_reconciliation"
down_revision = "20260724_subscription_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("provider_invoice_id", sa.String(length=128), nullable=True))
    op.create_index("ix_payments_provider_invoice_id", "payments", ["provider_invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_provider_invoice_id", table_name="payments")
    op.drop_column("payments", "provider_invoice_id")
