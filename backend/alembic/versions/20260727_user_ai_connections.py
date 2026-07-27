"""Add encrypted user-owned AI connections.

Revision ID: 20260727_user_ai_connections
Revises: 20260724_payment_reconciliation
"""
from alembic import op
import sqlalchemy as sa


revision = "20260727_user_ai_connections"
down_revision = "20260724_payment_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_ai_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_api_key", sa.String(length=4096), nullable=False),
        sa.Column("key_suffix", sa.String(length=12), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_user_ai_connections_user_id"),
    )
    op.create_index("ix_user_ai_connections_user_id", "user_ai_connections", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_ai_connections_user_id", table_name="user_ai_connections")
    op.drop_table("user_ai_connections")
