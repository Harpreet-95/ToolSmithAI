-- Migration 007: Add tenant_id column for multi-tenant isolation
--
-- Safe to run on any existing database. SQLite ALTER TABLE ADD COLUMN
-- sets NULL for all existing rows — no data is modified or deleted.
-- Rows written before this migration have no tenant assignment (NULL),
-- which the application treats as belonging to the "default" tenant.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/007_add_tenant_id.sql

ALTER TABLE audit_logs        ADD COLUMN tenant_id TEXT;
ALTER TABLE execution_history ADD COLUMN tenant_id TEXT;
