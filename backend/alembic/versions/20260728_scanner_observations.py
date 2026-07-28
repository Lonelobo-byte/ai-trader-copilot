"""Persist autonomous scanner evidence and publication blockers.

Revision ID: 20260728_scanner_observations
Revises: 20260728_shared_radar_snapshots
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260728_scanner_observations"
down_revision: Union[str, Sequence[str], None] = "20260728_shared_radar_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "scanner_observations" not in existing_tables:
        op.create_table(
            "scanner_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("timeframe", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("decision", sa.String(length=24), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("primary_blocker", sa.String(length=300), nullable=True),
            sa.Column("blockers", sa.JSON(), nullable=False),
            sa.Column("publication_coverage", sa.JSON(), nullable=False),
            sa.Column("institutional", sa.JSON(), nullable=False),
            sa.Column("tactical", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_scanner_observations_symbol", "scanner_observations", ["symbol"])
        op.create_index("ix_scanner_observations_timeframe", "scanner_observations", ["timeframe"])
        op.create_index("ix_scanner_observations_status", "scanner_observations", ["status"])
        op.create_index("ix_scanner_observations_primary_blocker", "scanner_observations", ["primary_blocker"])
        op.create_index("ix_scanner_observations_created_at", "scanner_observations", ["created_at"])
        op.create_index(
            "ix_scanner_observations_symbol_time_created",
            "scanner_observations", ["symbol", "timeframe", "created_at"],
        )


def downgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if context.is_offline_mode() or "scanner_observations" in existing_tables:
        op.drop_index("ix_scanner_observations_symbol_time_created", table_name="scanner_observations")
        op.drop_index("ix_scanner_observations_created_at", table_name="scanner_observations")
        op.drop_index("ix_scanner_observations_primary_blocker", table_name="scanner_observations")
        op.drop_index("ix_scanner_observations_status", table_name="scanner_observations")
        op.drop_index("ix_scanner_observations_timeframe", table_name="scanner_observations")
        op.drop_index("ix_scanner_observations_symbol", table_name="scanner_observations")
        op.drop_table("scanner_observations")
