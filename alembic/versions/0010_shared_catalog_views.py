"""Create storefront views using the actual mapped Node table names."""

from alembic import op

revision = "0010_shared_catalog_views"
down_revision = "0009_character_slug_ids"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$ BEGIN
          IF to_regclass('public.characters') IS NOT NULL THEN
            DROP VIEW IF EXISTS web.character_v;
            EXECUTE 'CREATE OR REPLACE VIEW web.character_v AS SELECT * FROM public.characters';
          END IF;
          IF to_regclass('public.occasions') IS NOT NULL THEN
            DROP VIEW IF EXISTS web.occasion_v;
            EXECUTE 'CREATE OR REPLACE VIEW web.occasion_v AS SELECT * FROM public.occasions';
          END IF;
          IF to_regclass('public.style_templates') IS NOT NULL THEN
            DROP VIEW IF EXISTS web.style_template_v;
            EXECUTE 'CREATE OR REPLACE VIEW web.style_template_v AS SELECT * FROM public.style_templates';
          END IF;
        END $$
    """)


def downgrade():
    pass
