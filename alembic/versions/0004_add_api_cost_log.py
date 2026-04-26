"""Add api_cost_log table

Revision ID: 0004_add_api_cost_log
Revises: 0003_add_order_quality
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_add_api_cost_log"
down_revision = "0003_add_order_quality"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    op.create_table(
        "api_cost_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("order_item_id", sa.BigInteger(), sa.ForeignKey(f"{SCHEMA}.order_item.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_api_cost_log_order_item_id", "api_cost_log", ["order_item_id"], schema=SCHEMA)
    op.create_index("ix_api_cost_log_provider", "api_cost_log", ["provider"], schema=SCHEMA)
    op.create_index("ix_api_cost_log_created_at", "api_cost_log", ["created_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("api_cost_log", schema=SCHEMA)
