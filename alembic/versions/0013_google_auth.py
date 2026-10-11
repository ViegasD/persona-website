"""Google identity binding and single-use sign-in challenges."""

import sqlalchemy as sa

from alembic import op

revision = "0013_google_auth"
down_revision = "0012_composite_cache_slug_ids"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("google_sub", sa.String(255), nullable=True), schema="web")
    op.create_unique_constraint("uq_user_google_sub", "user", ["google_sub"], schema="web")
    op.create_table("google_challenge",
        sa.Column("nonce_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)), schema="web")
    op.create_index("ix_google_challenge_expires_at", "google_challenge", ["expires_at"], schema="web")


def downgrade():
    op.drop_table("google_challenge", schema="web")
    op.drop_constraint("uq_user_google_sub", "user", schema="web", type_="unique")
    op.drop_column("user", "google_sub", schema="web")
