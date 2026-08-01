"""Store payment amounts as exact decimals.

Revision ID: 20260801_payment_amount_decimal
Revises: 20260728_scanner_observations
Create Date: 2026-08-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260801_payment_amount_decimal"
down_revision: Union[str, Sequence[str], None] = "20260728_scanner_observations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _payments_exists() -> bool:
    if context.is_offline_mode():
        return True
    return "payments" in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _payments_exists():
        return
    with op.batch_alter_table("payments") as batch_op:
        batch_op.alter_column(
            "amount",
            existing_type=sa.Float(),
            type_=sa.Numeric(precision=18, scale=8),
            existing_nullable=False,
        )


def downgrade() -> None:
    if not _payments_exists():
        return
    with op.batch_alter_table("payments") as batch_op:
        batch_op.alter_column(
            "amount",
            existing_type=sa.Numeric(precision=18, scale=8),
            type_=sa.Float(),
            existing_nullable=False,
        )
