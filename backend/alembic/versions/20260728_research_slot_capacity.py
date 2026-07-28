"""Add server-enforced concurrent research slots.

Revision ID: 20260728_research_slot_capacity
Revises: 20260728_platform_controls
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260728_research_slot_capacity"
down_revision: Union[str, Sequence[str], None] = "20260728_platform_controls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "research_slots" not in existing_tables:
        op.create_table(
            "research_slots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("timeframe", sa.String(length=16), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_research_slots_user_id", "research_slots", ["user_id"])
        op.create_index("ix_research_slots_expires_at", "research_slots", ["expires_at"])


def downgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if context.is_offline_mode() or "research_slots" in existing_tables:
        op.drop_index("ix_research_slots_expires_at", table_name="research_slots")
        op.drop_index("ix_research_slots_user_id", table_name="research_slots")
        op.drop_table("research_slots")
