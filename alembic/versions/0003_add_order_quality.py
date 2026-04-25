"""Add quality column to order table

Revision ID: 0003_add_order_quality
Revises: 0002_add_awaiting_approval_status
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_add_order_quality"
down_revision = "0002_add_awaiting_approval_status"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    op.add_column(
        "order",
        sa.Column("quality", sa.String(8), nullable=False, server_default="sd"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("order", "quality", schema=SCHEMA)
