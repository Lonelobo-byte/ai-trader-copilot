"""Persist shared demand-aware Radar snapshots.

Revision ID: 20260728_shared_radar_snapshots
Revises: 20260728_audit_hardening
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260728_shared_radar_snapshots"
down_revision: Union[str, Sequence[str], None] = "20260728_audit_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "radar_snapshots" not in existing_tables:
        op.create_table(
            "radar_snapshots",
            sa.Column("key", sa.String(length=32), primary_key=True),
            sa.Column("ltf", sa.String(length=16), nullable=False),
            sa.Column("htf", sa.String(length=16), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("demand_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("refreshing_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(length=500), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_radar_snapshots_captured_at", "radar_snapshots", ["captured_at"])
        op.create_index("ix_radar_snapshots_last_requested_at", "radar_snapshots", ["last_requested_at"])
        op.create_index("ix_radar_snapshots_refreshing_until", "radar_snapshots", ["refreshing_until"])


def downgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if context.is_offline_mode() or "radar_snapshots" in existing_tables:
        op.drop_index("ix_radar_snapshots_refreshing_until", table_name="radar_snapshots")
        op.drop_index("ix_radar_snapshots_last_requested_at", table_name="radar_snapshots")
        op.drop_index("ix_radar_snapshots_captured_at", table_name="radar_snapshots")
        op.drop_table("radar_snapshots")
