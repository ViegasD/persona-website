"""Identify funding payments and protect legacy welcome-credit identities."""

import sqlalchemy as sa

from alembic import op

revision = "0011_funding_and_legacy_claims"
down_revision = "0010_shared_catalog_views"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment", sa.Column("funds_order", sa.Boolean(), server_default=sa.false(), nullable=False), schema="web")
    # Historical data lacks the marker: the earliest approved payment funded it.
    op.execute('''
        UPDATE web.payment SET funds_order = true WHERE id IN (
          SELECT DISTINCT ON (order_id) id FROM web.payment
          WHERE status = 'APPROVED' ORDER BY order_id, paid_at NULLS LAST, id
        )
    ''')
    op.execute('''
        INSERT INTO web.welcome_credit_claim (identity_hash, user_id)
        SELECT DISTINCT ON (identity_hash) identity_hash, user_id FROM (
          SELECT encode(sha256(convert_to(
            CASE WHEN split_part(lower(u.email), '@', 2) IN ('gmail.com', 'googlemail.com')
              THEN replace(split_part(split_part(lower(u.email), '@', 1), '+', 1), '.', '') || '@gmail.com'
              ELSE lower(u.email) END, 'UTF8')), 'hex') AS identity_hash, u.id AS user_id
          FROM web."user" u WHERE u.email IS NOT NULL AND EXISTS (
            SELECT 1 FROM web.credit_transaction t WHERE t.user_id = u.id AND t.kind = 'signup_bonus'
          )
        ) identities ORDER BY identity_hash, user_id ON CONFLICT DO NOTHING
    ''')


def downgrade():
    op.drop_column("payment", "funds_order", schema="web")
