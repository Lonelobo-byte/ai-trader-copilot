"""Persist platform scanner controls and AI-budget reservations.

Revision ID: 20260728_platform_controls
Revises: 20260727_user_ai_connections
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260728_platform_controls"
down_revision: Union[str, Sequence[str], None] = "20260727_user_ai_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The application can create model tables at startup for older local
    # installations.  Do not fail when that happened before this revision was
    # recorded; Alembic still needs to mark the revision as applied.
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "scanner_configuration" not in existing_tables:
        op.create_table(
            "scanner_configuration",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("discovery", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("watchlist", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "platform_ai_usage" not in existing_tables:
        op.create_table(
            "platform_ai_usage",
            sa.Column("period_key", sa.String(length=7), nullable=False),
            sa.Column("reserved_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reserved_cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("period_key"),
        )


def downgrade() -> None:
    existing_tables = set()
    if not context.is_offline_mode():
        existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    if context.is_offline_mode() or "platform_ai_usage" in existing_tables:
        op.drop_table("platform_ai_usage")
    if context.is_offline_mode() or "scanner_configuration" in existing_tables:
        op.drop_table("scanner_configuration")
