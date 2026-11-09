-- Migration 010: Create usage_events table for tenant-level usage metering
--
-- This table records one row per billable event (interpret call, workflow run,
-- execution step, etc.) per tenant. It is the foundation for plan enforcement,
-- usage dashboards, and billing integration.
--
-- Design notes:
--   tenant_id is NOT NULL — every usage event must belong to a tenant.
--   user_id is nullable to allow system-level events with no specific user.
--   event_type describes what happened (e.g. 'interpret', 'workflow_run').
--   source describes the trigger path (e.g. 'api', 'scheduler').
--   reference_id is nullable and links back to the originating plan_id or
--     workflow_id in execution_history for cross-table correlation.
--   This table is append-only and is NOT subject to the standard retention
--     purge policy — usage records must be preserved for billing audit trails.
--
-- Safe to run on any existing database. Uses CREATE TABLE IF NOT EXISTS
-- so re-running has no effect if the table already exists.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/010_create_usage_events_table.sql

CREATE TABLE IF NOT EXISTS usage_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT    NOT NULL,
    user_id      TEXT,
    event_type   TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    reference_id TEXT,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_id  ON usage_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_created_at ON usage_events (created_at);
