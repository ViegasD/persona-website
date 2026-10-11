"""Account credentials, credit reversals and durable delivery outbox."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_complete_account_lifecycle"
down_revision = "0007_add_user_credits"
branch_labels = None
depends_on = None
SCHEMA = "web"


def upgrade():
    for name in ("credit_debt", "token_version"):
        op.add_column("user", sa.Column(name, sa.Integer(), nullable=False, server_default="0"), schema=SCHEMA)
    op.create_check_constraint("ck_user_credit_debt", "user", "credit_debt >= 0", schema=SCHEMA)
    op.add_column("credit_transaction", sa.Column("debt_after", sa.Integer(), nullable=False, server_default="0"), schema=SCHEMA)
    op.create_table(
        "account_token",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("web.user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_account_token_user_purpose", "account_token", ["user_id", "purpose", "created_at"], schema=SCHEMA)
    op.create_table(
        "welcome_credit_claim",
        sa.Column("identity_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("web.user.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_table(
        "outbox_job",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dedup_key", sa.String(128), unique=True, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("web.order.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("web.user.id", ondelete="CASCADE")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_outbox_pending", "outbox_job", ["delivered_at", "available_at"], schema=SCHEMA)
    # Repair the commit/queue gap for older paid orders which never started.
    op.execute("""
        INSERT INTO web.outbox_job (dedup_key, kind, order_id, payload)
        SELECT 'process_item:' || i.id, 'process_item', i.order_id,
               jsonb_build_object('item_id', i.id)
        FROM web.order_item i JOIN web."order" o ON o.id = i.order_id
        WHERE i.status = 'PENDING' AND o.status IN ('PAID', 'QUEUED', 'GENERATING')
        ON CONFLICT (dedup_key) DO NOTHING
    """)


def downgrade():
    op.drop_table("outbox_job", schema=SCHEMA)
    op.drop_table("welcome_credit_claim", schema=SCHEMA)
    op.drop_table("account_token", schema=SCHEMA)
    op.drop_column("credit_transaction", "debt_after", schema=SCHEMA)
    op.drop_constraint("ck_user_credit_debt", "user", schema=SCHEMA, type_="check")
    op.drop_column("user", "token_version", schema=SCHEMA)
    op.drop_column("user", "credit_debt", schema=SCHEMA)
