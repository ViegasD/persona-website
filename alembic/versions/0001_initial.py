"""initial storefront schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-24

Creates all storefront tables in the configured `web` schema and a few
read-only views over the existing Node-backend tables (`character`,
`occasion`, `style_template`).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "web"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # ── Enums ──────────────────────────────────────────────────────────────
    personalization = postgresql.ENUM(
        "name_only", "medium", "full", name="personalization_level", schema=SCHEMA
    )
    order_status = postgresql.ENUM(
        "DRAFT", "AWAITING_PAYMENT", "PAID", "QUEUED", "GENERATING",
        "READY", "DELIVERED", "FAILED", "REFUNDED",
        name="order_status", schema=SCHEMA,
    )
    order_item_status = postgresql.ENUM(
        "PENDING", "COMPOSITING", "RENDERING", "READY", "FAILED",
        name="order_item_status", schema=SCHEMA,
    )
    batch_status = postgresql.ENUM(
        "COLLECTING", "STARTING_POD", "RUNNING", "DRAINING", "DONE", "FAILED",
        name="batch_status", schema=SCHEMA,
    )
    batch_trigger = postgresql.ENUM(
        "auto_threshold", "auto_age", "manual", "scheduled",
        name="batch_trigger", schema=SCHEMA,
    )
    payment_status = postgresql.ENUM(
        "PENDING", "APPROVED", "REJECTED", "REFUNDED", "EXPIRED",
        name="payment_status", schema=SCHEMA,
    )
    delivery_channel = postgresql.ENUM(
        "account", "whatsapp", "email", name="delivery_channel", schema=SCHEMA
    )
    delivery_status = postgresql.ENUM(
        "PENDING", "SUCCEEDED", "FAILED", name="delivery_status", schema=SCHEMA
    )
    subscription_status = postgresql.ENUM(
        "ACTIVE", "PAUSED", "CANCELED", name="subscription_status", schema=SCHEMA
    )
    billing_period = postgresql.ENUM(
        "weekly", "monthly", name="billing_period", schema=SCHEMA
    )
    for e in (
        personalization, order_status, order_item_status, batch_status,
        batch_trigger, payment_status, delivery_channel, delivery_status,
        subscription_status, billing_period,
    ):
        e.create(op.get_bind(), checkfirst=True)

    # ── user ──────────────────────────────────────────────────────────────
    op.create_table(
        "user",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.String(255), unique=True),
        sa.Column("phone", sa.String(32)),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("name", sa.String(255)),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_user_phone", "user", ["phone"], schema=SCHEMA)

    # ── plan ──────────────────────────────────────────────────────────────
    op.create_table(
        "plan",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("video_count", sa.Integer, nullable=False),
        sa.Column("max_characters_per_video", sa.Integer, nullable=False, server_default="1"),
        sa.Column("personalization_level", personalization, nullable=False, server_default="medium"),
        sa.Column("is_subscription", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("billing_period", billing_period),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("features", postgresql.ARRAY(sa.String)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )

    # ── order ─────────────────────────────────────────────────────────────
    op.create_table(
        "order",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey(f"{SCHEMA}.user.id")),
        sa.Column("guest_email", sa.String(255)),
        sa.Column("guest_phone", sa.String(32)),
        sa.Column("plan_id", sa.BigInteger, sa.ForeignKey(f"{SCHEMA}.plan.id"), nullable=False),
        sa.Column("status", order_status, nullable=False, server_default="DRAFT"),
        sa.Column("recipient_name", sa.String(128)),
        sa.Column("recipient_age", sa.String(16)),
        sa.Column("occasion_slug", sa.String(64)),
        sa.Column("total_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        schema=SCHEMA,
    )
    op.create_index("ix_order_status", "order", ["status"], schema=SCHEMA)
    op.create_index("ix_order_created_at", "order", ["created_at"], schema=SCHEMA)
    op.create_index("ix_order_guest_email", "order", ["guest_email"], schema=SCHEMA)
    op.create_index("ix_order_occasion_slug", "order", ["occasion_slug"], schema=SCHEMA)

    # ── order_item ────────────────────────────────────────────────────────
    op.create_table(
        "order_item",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "order_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("character_ids", postgresql.ARRAY(sa.Integer), nullable=False),
        sa.Column("custom_message", sa.Text),
        sa.Column("resolved_script", sa.Text),
        sa.Column("composite_image_s3_key", sa.String(512)),
        sa.Column("video_s3_key", sa.String(512)),
        sa.Column("thumbnail_s3_key", sa.String(512)),
        sa.Column("status", order_item_status, nullable=False, server_default="PENDING"),
        sa.Column("error", sa.Text),
        sa.Column("comfy_workflow_a_prompt_id", sa.String(128)),
        sa.Column("comfy_workflow_b_prompt_id", sa.String(128)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", "sequence", name="uq_order_item_sequence"),
        schema=SCHEMA,
    )
    op.create_index("ix_order_item_status", "order_item", ["status"], schema=SCHEMA)

    # ── batch ─────────────────────────────────────────────────────────────
    op.create_table(
        "batch",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("status", batch_status, nullable=False, server_default="COLLECTING"),
        sa.Column("trigger", batch_trigger),
        sa.Column("pod_id", sa.String(128)),
        sa.Column("pod_endpoint", sa.String(256)),
        sa.Column("order_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        schema=SCHEMA,
    )
    op.create_index("ix_batch_status", "batch", ["status"], schema=SCHEMA)

    # ── batch_item ────────────────────────────────────────────────────────
    op.create_table(
        "batch_item",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "batch_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.batch.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "order_item_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.order_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        sa.UniqueConstraint("batch_id", "order_item_id", name="uq_batch_item"),
        schema=SCHEMA,
    )
    op.create_index("ix_batch_item_batch_id", "batch_item", ["batch_id"], schema=SCHEMA)
    op.create_index("ix_batch_item_order_item_id", "batch_item", ["order_item_id"], schema=SCHEMA)

    # ── payment ───────────────────────────────────────────────────────────
    op.create_table(
        "payment",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "order_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False, server_default="mercadopago"),
        sa.Column("provider_id", sa.String(128)),
        sa.Column("status", payment_status, nullable=False, server_default="PENDING"),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("qr_code_payload", sa.Text),
        sa.Column("qr_code_s3_key", sa.String(512)),
        sa.Column("ticket_url", sa.String(512)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("raw_webhook", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_payment_order_id", "payment", ["order_id"], schema=SCHEMA)
    op.create_index("ix_payment_status", "payment", ["status"], schema=SCHEMA)
    op.create_index(
        "ix_payment_provider_id", "payment", ["provider", "provider_id"], unique=True, schema=SCHEMA
    )

    # ── delivery ──────────────────────────────────────────────────────────
    op.create_table(
        "delivery",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "order_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", delivery_channel, nullable=False),
        sa.Column("status", delivery_status, nullable=False, server_default="PENDING"),
        sa.Column("target", sa.String(255)),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_delivery_order_id", "delivery", ["order_id"], schema=SCHEMA)

    # ── subscription ──────────────────────────────────────────────────────
    op.create_table(
        "subscription",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.BigInteger, sa.ForeignKey(f"{SCHEMA}.plan.id"), nullable=False),
        sa.Column("status", subscription_status, nullable=False, server_default="ACTIVE"),
        sa.Column("period", billing_period, nullable=False),
        sa.Column("next_charge_at", sa.DateTime(timezone=True)),
        sa.Column("last_order_id", sa.BigInteger, sa.ForeignKey(f"{SCHEMA}.order.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_subscription_user_id", "subscription", ["user_id"], schema=SCHEMA)
    op.create_index("ix_subscription_next_charge", "subscription", ["next_charge_at"], schema=SCHEMA)

    # ── upsell_event ──────────────────────────────────────────────────────
    op.create_table(
        "upsell_event",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "order_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("accepted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_upsell_event_order_id", "upsell_event", ["order_id"], schema=SCHEMA)

    # ── composite_cache ───────────────────────────────────────────────────
    op.create_table(
        "composite_cache",
        sa.Column("sha", sa.String(64), primary_key=True),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("character_ids", postgresql.ARRAY(sa.Integer), nullable=False),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )

    # ── Read-only views over Node backend tables ──────────────────────────
    # Wrap in DO blocks so we don't fail if the source tables aren't present
    # yet (e.g. fresh dev DB). The views are recreated as `OR REPLACE`.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='Character') THEN
                EXECUTE 'CREATE OR REPLACE VIEW {SCHEMA}.character_v AS SELECT * FROM public."Character"';
            END IF;
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='Occasion') THEN
                EXECUTE 'CREATE OR REPLACE VIEW {SCHEMA}.occasion_v AS SELECT * FROM public."Occasion"';
            END IF;
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='StyleTemplate') THEN
                EXECUTE 'CREATE OR REPLACE VIEW {SCHEMA}.style_template_v AS SELECT * FROM public."StyleTemplate"';
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute(f'DROP VIEW IF EXISTS "{SCHEMA}".character_v')
    op.execute(f'DROP VIEW IF EXISTS "{SCHEMA}".occasion_v')
    op.execute(f'DROP VIEW IF EXISTS "{SCHEMA}".style_template_v')

    for tbl in (
        "composite_cache", "upsell_event", "subscription", "delivery",
        "payment", "batch_item", "batch", "order_item", "order", "plan", "user",
    ):
        op.drop_table(tbl, schema=SCHEMA)

    for enum_name in (
        "billing_period", "subscription_status", "delivery_status", "delivery_channel",
        "payment_status", "batch_trigger", "batch_status", "order_item_status",
        "order_status", "personalization_level",
    ):
        op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}"."{enum_name}"')
