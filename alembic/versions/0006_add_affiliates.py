"""Add affiliates table and affiliate columns to order

Revision ID: 0006_add_affiliates
Revises: 0005_add_order_utm_fields
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_add_affiliates"
down_revision = "0005_add_order_utm_fields"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    op.create_table(
        "affiliate",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("commission_pct", sa.Numeric(5, 2), nullable=False, server_default="20.00"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_affiliate_code"),
        schema=SCHEMA,
    )

    op.add_column(
        "order",
        sa.Column("affiliate_id", sa.BigInteger, nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_order_affiliate_id",
        "order",
        "affiliate",
        ["affiliate_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.add_column(
        "order",
        sa.Column("commission_paid_out", sa.Boolean, nullable=False, server_default="false"),
        schema=SCHEMA,
    )
    op.add_column(
        "order",
        sa.Column("commission_paid_out_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("fk_order_affiliate_id", "order", schema=SCHEMA, type_="foreignkey")
    op.drop_column("order", "commission_paid_out_at", schema=SCHEMA)
    op.drop_column("order", "commission_paid_out", schema=SCHEMA)
    op.drop_column("order", "affiliate_id", schema=SCHEMA)
    op.drop_table("affiliate", schema=SCHEMA)
