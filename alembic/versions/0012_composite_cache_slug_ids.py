"""Match the composite cache to the slug-based character model."""

from alembic import op

revision = "0012_composite_cache_slug_ids"
down_revision = "0011_funding_and_legacy_claims"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE web.composite_cache ALTER COLUMN character_ids TYPE text[] USING character_ids::text[]")


def downgrade():
    # Character slugs have no lossless numeric representation.
    pass
