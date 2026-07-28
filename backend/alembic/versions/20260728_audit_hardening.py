"""Add query support for the active signal ledger.

Revision ID: 20260728_audit_hardening
Revises: 20260728_research_slot_capacity
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_audit_hardening"
down_revision: Union[str, Sequence[str], None] = "20260728_research_slot_capacity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("trade_signals")}
    if "ix_trade_signals_symbol_timeframe_status" not in indexes:
        op.create_index(
            "ix_trade_signals_symbol_timeframe_status",
            "trade_signals",
            ["symbol", "timeframe", "status"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("trade_signals")}
    if "ix_trade_signals_symbol_timeframe_status" in indexes:
        op.drop_index("ix_trade_signals_symbol_timeframe_status", table_name="trade_signals")
