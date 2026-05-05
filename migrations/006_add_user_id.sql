-- Migration 006: Add user_id column for GDPR user identity tracking
--
-- Safe to run on any existing database. SQLite ALTER TABLE ADD COLUMN
-- sets NULL for all existing rows — no data is modified or deleted.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/006_add_user_id.sql

ALTER TABLE audit_logs        ADD COLUMN user_id TEXT;
ALTER TABLE execution_history ADD COLUMN user_id TEXT;
