-- Migration 009: Add tenant_id column to workflows table for tenant isolation
--
-- Safe to run on any existing database. SQLite ALTER TABLE ADD COLUMN
-- sets NULL for all existing rows — no data is modified or deleted.
--
-- NULL tenant_id means the workflow is a legacy or shared workflow and
-- is accessible to all tenants. Tenant-specific workflows will have an
-- explicit tenant_id value set at creation time.
--
-- Do not backfill existing rows. The application query layer treats
-- NULL as "global/shared" using: WHERE tenant_id = ? OR tenant_id IS NULL
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/009_add_tenant_id_to_workflows.sql

ALTER TABLE workflows ADD COLUMN tenant_id TEXT;
