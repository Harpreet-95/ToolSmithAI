-- Migration 016: SQL Architecture Correction, Phase 6, Step 4C2 --
-- add `phase` to sql_semantic_enrichments
--
-- SUPERSEDED as the primary application path by Step 4C2A: this project has
-- no migration-ledger/runner, so data/models.py's init_db() (called on
-- every real app startup) now applies this SAME change itself, idempotently
-- and safely, via ensure_sql_semantic_enrichments_phase_migration() --
-- checking PRAGMA table_info/index_info state before ever touching the
-- schema, and raising SemanticEnrichmentMigrationError instead of guessing
-- past a partially-applied state. Prefer letting init_db() apply this
-- automatically. Manual raw reapplication of the ALTER TABLE statement
-- below is UNSAFE if init_db() may already have applied it (SQLite's ALTER
-- TABLE ADD COLUMN has no "IF NOT EXISTS" form and will error with
-- "duplicate column name") -- if you must run this file by hand, verify
-- FIRST via `PRAGMA table_info(sql_semantic_enrichments)` that `phase` is
-- genuinely absent. This file is kept as documentation of the exact schema
-- change and for the from-scratch/other-tooling case; see
-- tests/test_sqlservice_semantic_enrichment_object_core_migration.py for
-- this raw-SQL path's own tests and
-- tests/test_sqlservice_semantic_enrichment_object_core_migration_runner.py
-- for init_db()'s idempotent equivalent.
--
-- Additive only: one new NOT-NULL column with a DEFAULT (so it backfills
-- every pre-existing row automatically -- SQLite's ALTER TABLE ADD COLUMN
-- computes the DEFAULT for existing rows at the time of the ALTER, it does
-- not merely apply it to future inserts), plus a replacement UNIQUE index
-- that now includes `phase`. No existing row's payload_json, status,
-- source_id, object_fqn, semantic_input_hash, provider_id, model, or
-- contract_version is touched. Safe to run on any existing database that
-- already has migrations/015_create_semantic_enrichment.sql applied.
--
-- NOT applied against any live database as part of Step 4C2 -- this file
-- is written and tested (see tests/test_sqlservice_semantic_enrichment_
-- object_core_migration.py, which applies it only to a temporary, in-
-- memory/on-disk SQLite copy) but never run against data/toolsmith.db in
-- this step.
--
-- Why a new column is needed at all: Phase 6 Step 4C2 introduces a
-- genuinely SEPARATE, compact "OBJECT_CORE" enrichment contract alongside
-- the pre-existing (Step 4A/4B/4C1) combined object+selected-columns
-- contract, now called "COLUMN_DEFERRED" (core.sqlservice.
-- semantic_enrichment_phases.PHASE_OBJECT_CORE/PHASE_COLUMN_DEFERRED). Both
-- phases persist into this SAME table (an OBJECT_CORE row simply has an
-- empty `columns` array in its payload_json), so their cache identities
-- must be kept apart at the SCHEMA level -- migration 015's own UNIQUE
-- index (source_id, object_fqn, semantic_input_hash, provider_id, model,
-- contract_version) has no way to distinguish "this object's OBJECT_CORE
-- result" from "this object's COLUMN_DEFERRED result" if they ever shared
-- the same semantic_input_hash by coincidence (they cannot in practice --
-- core.sqlservice.semantic_enrichment_service.compute_object_core_input_
-- hash and compute_object_semantic_input_hash hash structurally different
-- payloads -- but the SCHEMA itself should not rely on that as its only
-- safety net). The Phase 6 Step 4C2 contract_version bump (2 -> 3) ALSO
-- independently guarantees a pre-4C2 (v2) row can never satisfy a v3
-- lookup for either phase -- this migration's `phase` column is the
-- belt to that bump's suspenders, not a substitute for it.
--
-- Idempotency note: unlike migration 015's CREATE TABLE/INDEX IF NOT
-- EXISTS statements, SQLite's ALTER TABLE ADD COLUMN has no "IF NOT
-- EXISTS" form -- re-running this file against a database it has already
-- been applied to fails LOUDLY with a "duplicate column name" error on the
-- ALTER TABLE statement, rather than silently corrupting anything or
-- partially re-applying (the two DROP/CREATE INDEX statements below ARE
-- individually idempotent). This is the expected, tested behavior (see
-- tests/test_sqlservice_semantic_enrichment_object_core_migration.py) --
-- run this file at most once per database, exactly like 015.
--
-- Run once against your existing toolsmith.db (after migration 015):
--   sqlite3 data/toolsmith.db < migrations/016_add_semantic_enrichment_phase.sql

ALTER TABLE sql_semantic_enrichments
    ADD COLUMN phase TEXT NOT NULL DEFAULT 'COLUMN_DEFERRED'
        CHECK (phase IN ('OBJECT_CORE', 'COLUMN_DEFERRED'));

-- Replace the old (phase-less) UNIQUE index with a phase-inclusive one --
-- SQLite has no ALTER INDEX; drop and recreate is the only option, and is
-- safe (an index carries no data of its own).
DROP INDEX IF EXISTS idx_sse_cache_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sse_cache_key
    ON sql_semantic_enrichments (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase);

-- Replace the old source/current-preparation lookup index the same way --
-- load_current_enrichments_for_source()/count_enrichment_completeness() now
-- always filter by phase too (core/sqlservice/semantic_enrichment_store.py).
DROP INDEX IF EXISTS idx_sse_source_current;
CREATE INDEX IF NOT EXISTS idx_sse_source_current
    ON sql_semantic_enrichments (source_id, status, provider_id, model, contract_version, phase);
