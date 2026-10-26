"""Optional generation overrides; existing orders and environment defaults preserved."""
import sqlalchemy as sa
from alembic import op

revision = "0015_generation_settings"
down_revision = "0014_storefront_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("storefront_policy", sa.Column("video_duration_seconds", sa.Integer(), nullable=True), schema="web")
    op.add_column("storefront_policy", sa.Column("video_model", sa.String(64), nullable=True), schema="web")
    op.create_check_constraint("generation_duration_range", "storefront_policy", "video_duration_seconds BETWEEN 4 AND 15", schema="web")


def downgrade():
    op.drop_constraint("generation_duration_range", "storefront_policy", schema="web", type_="check")
    op.drop_column("storefront_policy", "video_model", schema="web")
    op.drop_column("storefront_policy", "video_duration_seconds", schema="web")
