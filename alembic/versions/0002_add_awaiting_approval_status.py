"""Add AWAITING_APPROVAL and APPROVED to order_item_status enum

Revision ID: 0002_add_awaiting_approval_status
Revises: 0001_initial
Create Date: 2026-05-01
"""

from __future__ import annotations

from alembic import op

revision = "0002_add_awaiting_approval_status"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    # PostgreSQL requires ALTER TYPE … ADD VALUE outside a transaction block.
    op.execute("COMMIT")
    op.execute(
        f"ALTER TYPE \"{SCHEMA}\".order_item_status ADD VALUE IF NOT EXISTS 'AWAITING_APPROVAL'"
    )
    op.execute(
        f"ALTER TYPE \"{SCHEMA}\".order_item_status ADD VALUE IF NOT EXISTS 'APPROVED'"
    )


def downgrade() -> None:
    # Removing enum values is not supported in PostgreSQL without recreating
    # the type, so we leave downgrade as a no-op.
    pass
