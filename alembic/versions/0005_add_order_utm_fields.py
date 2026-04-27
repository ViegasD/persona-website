"""Add UTM tracking fields to order table

Revision ID: 0005_add_order_utm_fields
Revises: 0004_add_api_cost_log
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_add_order_utm_fields"
down_revision = "0004_add_api_cost_log"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    op.add_column("order", sa.Column("utm_source",   sa.String(128), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_medium",   sa.String(128), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_campaign", sa.String(256), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_content",  sa.String(256), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_term",     sa.String(256), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_sck",      sa.String(256), nullable=True), schema=SCHEMA)
    op.add_column("order", sa.Column("utm_src",      sa.String(256), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    for col in ("utm_src", "utm_sck", "utm_term", "utm_content", "utm_campaign", "utm_medium", "utm_source"):
        op.drop_column("order", col, schema=SCHEMA)
