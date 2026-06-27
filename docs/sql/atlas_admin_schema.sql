-- Esquema de administración del Visor geográfico (Atlas Municipal).
-- Ejecutar una vez en la base PostGIS del stack.

CREATE SCHEMA IF NOT EXISTS atlas_admin;

CREATE TABLE IF NOT EXISTS atlas_admin.users (
  id            SERIAL PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'visor_admin'
                CHECK (role IN ('visor_admin', 'viewer')),
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS atlas_admin.catalog_audit (
  id          BIGSERIAL PRIMARY KEY,
  user_id     INT REFERENCES atlas_admin.users(id),
  action      TEXT NOT NULL,
  layer_id    TEXT,
  before_json JSONB,
  after_json  JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_catalog_audit_layer
  ON atlas_admin.catalog_audit (layer_id);

CREATE INDEX IF NOT EXISTS idx_catalog_audit_created
  ON atlas_admin.catalog_audit (created_at DESC);

CREATE TABLE IF NOT EXISTS atlas_admin.layer_publications (
  id               SERIAL PRIMARY KEY,
  layer_id         TEXT NOT NULL UNIQUE,
  table_name       TEXT NOT NULL,
  published_by     INT REFERENCES atlas_admin.users(id),
  published_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  catalog_snapshot JSONB
);
