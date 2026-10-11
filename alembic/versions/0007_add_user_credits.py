"""Add user credit ledger

Revision ID: 0007_add_user_credits
Revises: 0006_add_affiliates
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_add_user_credits"
down_revision = "0006_add_affiliates"
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    credit_type = postgresql.ENUM(
        "signup_bonus",
        "purchase",
        "redeem",
        "adjustment",
        "refund",
        name="credit_transaction_type",
        schema=SCHEMA,
        create_type=False,
    )
    credit_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "user",
        sa.Column("credit_balance", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )

    op.create_table(
        "credit_transaction",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", credit_type, nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], [f"{SCHEMA}.order.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("order_id", "kind", name="uq_credit_transaction_order_kind"),
        sa.CheckConstraint("balance_after >= 0", name="ck_credit_transaction_balance"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_credit_transaction_user_id",
        "credit_transaction",
        ["user_id"],
        schema=SCHEMA,
    )
    op.create_check_constraint("ck_user_credit_balance", "user", "credit_balance >= 0", schema=SCHEMA)
    op.create_index(
        "ix_credit_transaction_user_created",
        "credit_transaction",
        ["user_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_credit_transaction_order_id",
        "credit_transaction",
        ["order_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_credit_transaction_order_id", table_name="credit_transaction", schema=SCHEMA)
    op.drop_index("ix_credit_transaction_user_created", table_name="credit_transaction", schema=SCHEMA)
    op.drop_index("ix_credit_transaction_user_id", table_name="credit_transaction", schema=SCHEMA)
    op.drop_table("credit_transaction", schema=SCHEMA)
    op.drop_constraint("ck_user_credit_balance", "user", schema=SCHEMA, type_="check")
    op.drop_column("user", "credit_balance", schema=SCHEMA)
    op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}"."credit_transaction_type"')
