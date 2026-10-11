"""Match the maintained slug-based character array model."""

from alembic import op

revision = "0009_character_slug_ids"
down_revision = "0008_complete_account_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE web.order_item ALTER COLUMN character_ids TYPE text[] USING character_ids::text[]")


def downgrade():
    # Slugs cannot be losslessly converted back to numeric IDs.
    pass
