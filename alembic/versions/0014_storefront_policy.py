"""Storefront business controls without changing customer records."""

import sqlalchemy as sa
from alembic import op

revision = "0014_storefront_policy"
down_revision = "0013_google_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("storefront_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_orders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("registration_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("credit_redemption_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("require_account_checkout", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="single_storefront_policy"), schema="web")


def downgrade():
    op.drop_table("storefront_policy", schema="web")
