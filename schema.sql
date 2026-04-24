-- =============================================================================
-- Persona Storefront — initial schema
-- Schema: web
-- Run once against: postgresql://persona:persona_dev_123@93.127.210.42:41532/persona
-- Safe to re-run (uses IF NOT EXISTS / CREATE OR REPLACE throughout)
-- =============================================================================

-- ── Schema ────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS "web";

-- ── Enums ─────────────────────────────────────────────────────────────────────
DO $$ BEGIN
  CREATE TYPE web.personalization_level AS ENUM ('name_only','medium','full');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.order_status AS ENUM (
    'DRAFT','AWAITING_PAYMENT','PAID','QUEUED','GENERATING','READY','DELIVERED','FAILED','REFUNDED'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.order_item_status AS ENUM ('PENDING','COMPOSITING','RENDERING','READY','FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.batch_status AS ENUM ('COLLECTING','STARTING_POD','RUNNING','DRAINING','DONE','FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.batch_trigger AS ENUM ('auto_threshold','auto_age','manual','scheduled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.payment_status AS ENUM ('PENDING','APPROVED','REJECTED','REFUNDED','EXPIRED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.delivery_channel AS ENUM ('account','whatsapp','email');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.delivery_status AS ENUM ('PENDING','SUCCEEDED','FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.subscription_status AS ENUM ('ACTIVE','PAUSED','CANCELED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE web.billing_period AS ENUM ('weekly','monthly');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── user ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web."user" (
  id               BIGSERIAL PRIMARY KEY,
  email            VARCHAR(255) UNIQUE,
  phone            VARCHAR(32),
  password_hash    VARCHAR(255),
  name             VARCHAR(255),
  email_verified_at TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_phone ON web."user" (phone);

-- ── plan ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.plan (
  id                          BIGSERIAL PRIMARY KEY,
  slug                        VARCHAR(64) NOT NULL UNIQUE,
  name                        VARCHAR(128) NOT NULL,
  description                 TEXT,
  price_cents                 INTEGER NOT NULL,
  video_count                 INTEGER NOT NULL,
  max_characters_per_video    INTEGER NOT NULL DEFAULT 1,
  personalization_level       web.personalization_level NOT NULL DEFAULT 'medium',
  is_subscription             BOOLEAN NOT NULL DEFAULT FALSE,
  billing_period              web.billing_period,
  is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order                  INTEGER NOT NULL DEFAULT 0,
  features                    TEXT[],
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── order ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web."order" (
  id             BIGSERIAL PRIMARY KEY,
  user_id        BIGINT REFERENCES web."user"(id),
  guest_email    VARCHAR(255),
  guest_phone    VARCHAR(32),
  plan_id        BIGINT NOT NULL REFERENCES web.plan(id),
  status         web.order_status NOT NULL DEFAULT 'DRAFT',
  recipient_name VARCHAR(128),
  recipient_age  VARCHAR(16),
  occasion_slug  VARCHAR(64),
  total_cents    INTEGER NOT NULL DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  paid_at        TIMESTAMPTZ,
  generated_at   TIMESTAMPTZ,
  delivered_at   TIMESTAMPTZ,
  error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_order_status      ON web."order" (status);
CREATE INDEX IF NOT EXISTS ix_order_created_at  ON web."order" (created_at);
CREATE INDEX IF NOT EXISTS ix_order_guest_email ON web."order" (guest_email);
CREATE INDEX IF NOT EXISTS ix_order_occasion_slug ON web."order" (occasion_slug);

-- ── order_item ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.order_item (
  id                         BIGSERIAL PRIMARY KEY,
  order_id                   BIGINT NOT NULL REFERENCES web."order"(id) ON DELETE CASCADE,
  sequence                   INTEGER NOT NULL,
  character_ids              INTEGER[] NOT NULL,
  custom_message             TEXT,
  resolved_script            TEXT,
  composite_image_s3_key     VARCHAR(512),
  video_s3_key               VARCHAR(512),
  thumbnail_s3_key           VARCHAR(512),
  status                     web.order_item_status NOT NULL DEFAULT 'PENDING',
  error                      TEXT,
  comfy_workflow_a_prompt_id VARCHAR(128),
  comfy_workflow_b_prompt_id VARCHAR(128),
  attempts                   INTEGER NOT NULL DEFAULT 0,
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_order_item_sequence UNIQUE (order_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_order_item_status ON web.order_item (status);

-- ── batch ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.batch (
  id           BIGSERIAL PRIMARY KEY,
  status       web.batch_status NOT NULL DEFAULT 'COLLECTING',
  trigger      web.batch_trigger,
  pod_id       VARCHAR(128),
  pod_endpoint VARCHAR(256),
  order_count  INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_batch_status ON web.batch (status);

-- ── batch_item ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.batch_item (
  id            BIGSERIAL PRIMARY KEY,
  batch_id      BIGINT NOT NULL REFERENCES web.batch(id) ON DELETE CASCADE,
  order_item_id BIGINT NOT NULL REFERENCES web.order_item(id) ON DELETE CASCADE,
  attempt       INTEGER NOT NULL DEFAULT 1,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  error         TEXT,
  CONSTRAINT uq_batch_item UNIQUE (batch_id, order_item_id)
);
CREATE INDEX IF NOT EXISTS ix_batch_item_batch_id      ON web.batch_item (batch_id);
CREATE INDEX IF NOT EXISTS ix_batch_item_order_item_id ON web.batch_item (order_item_id);

-- ── payment ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.payment (
  id               BIGSERIAL PRIMARY KEY,
  order_id         BIGINT NOT NULL REFERENCES web."order"(id) ON DELETE CASCADE,
  provider         VARCHAR(32) NOT NULL DEFAULT 'mercadopago',
  provider_id      VARCHAR(128),
  status           web.payment_status NOT NULL DEFAULT 'PENDING',
  amount_cents     INTEGER NOT NULL,
  qr_code_payload  TEXT,
  qr_code_s3_key   VARCHAR(512),
  ticket_url       VARCHAR(512),
  expires_at       TIMESTAMPTZ,
  paid_at          TIMESTAMPTZ,
  raw_webhook      JSONB,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_payment_order_id   ON web.payment (order_id);
CREATE INDEX IF NOT EXISTS ix_payment_status     ON web.payment (status);
CREATE UNIQUE INDEX IF NOT EXISTS ix_payment_provider_id ON web.payment (provider, provider_id);

-- ── delivery ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.delivery (
  id           BIGSERIAL PRIMARY KEY,
  order_id     BIGINT NOT NULL REFERENCES web."order"(id) ON DELETE CASCADE,
  channel      web.delivery_channel NOT NULL,
  status       web.delivery_status NOT NULL DEFAULT 'PENDING',
  target       VARCHAR(255),
  payload      JSONB,
  attempted_at TIMESTAMPTZ,
  succeeded_at TIMESTAMPTZ,
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_delivery_order_id ON web.delivery (order_id);

-- ── subscription ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.subscription (
  id             BIGSERIAL PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES web."user"(id) ON DELETE CASCADE,
  plan_id        BIGINT NOT NULL REFERENCES web.plan(id),
  status         web.subscription_status NOT NULL DEFAULT 'ACTIVE',
  period         web.billing_period NOT NULL,
  next_charge_at TIMESTAMPTZ,
  last_order_id  BIGINT REFERENCES web."order"(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_subscription_user_id     ON web.subscription (user_id);
CREATE INDEX IF NOT EXISTS ix_subscription_next_charge ON web.subscription (next_charge_at);

-- ── upsell_event ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.upsell_event (
  id         BIGSERIAL PRIMARY KEY,
  order_id   BIGINT NOT NULL REFERENCES web."order"(id) ON DELETE CASCADE,
  kind       VARCHAR(32) NOT NULL,
  accepted   BOOLEAN NOT NULL DEFAULT FALSE,
  payload    JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_upsell_event_order_id ON web.upsell_event (order_id);

-- ── composite_cache ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.composite_cache (
  sha           VARCHAR(64) PRIMARY KEY,
  s3_key        VARCHAR(512) NOT NULL,
  character_ids INTEGER[] NOT NULL,
  payload       JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Alembic version tracking ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web.alembic_version (
  version_num VARCHAR(32) NOT NULL PRIMARY KEY
);
INSERT INTO web.alembic_version (version_num)
VALUES ('0001_initial')
ON CONFLICT DO NOTHING;

-- ── Views over Node backend tables (safe — skipped if source tables missing) ──
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'Character'
  ) THEN
    EXECUTE 'CREATE OR REPLACE VIEW web.character_v AS SELECT * FROM public."Character"';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'Occasion'
  ) THEN
    EXECUTE 'CREATE OR REPLACE VIEW web.occasion_v AS SELECT * FROM public."Occasion"';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'StyleTemplate'
  ) THEN
    EXECUTE 'CREATE OR REPLACE VIEW web.style_template_v AS SELECT * FROM public."StyleTemplate"';
  END IF;
END $$;
