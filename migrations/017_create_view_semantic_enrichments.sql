-- Migration 017: SQL Architecture Correction, Phase 6, Step 6C1 --
-- create sql_view_semantic_enrichments (deep VIEW-only semantic enrichment)
--
-- REPLACES an earlier, abandoned Step 6C draft of this migration that
-- instead widened sql_semantic_enrichments' `phase` CHECK constraint (via
-- a table rebuild: create-copy-drop-rename) to also allow 'VIEW_ENRICHED'.
-- That draft was NEVER applied to any live database and is superseded by
-- this file before ever being used. Step 6C1's audit found three concrete
-- reasons to prefer a brand-new, dedicated table instead:
--   1. sql_semantic_enrichments is a LIVE, already-populated table (590
--      real ACTIVE OBJECT_CORE rows as of this audit, 2026-09-16) --
--      rebuilding a live, populated, indexed, foreign-keyed table carries
--      materially more risk than creating an empty new one.
--   2. data/db.py's get_connection() sets `PRAGMA foreign_keys = ON` for
--      every real connection, and SQLite only allows toggling that pragma
--      OUTSIDE an active transaction -- the abandoned draft's rebuild
--      sequence never accounted for this, a genuine correctness gap this
--      migration eliminates by construction (a plain CREATE TABLE IF NOT
--      EXISTS never needs to touch PRAGMA foreign_keys at all).
--   3. SQLite has no ALTER TABLE ... ALTER COLUMN / DROP CONSTRAINT of any
--      kind, so widening an existing CHECK clause is INHERENTLY a
--      multi-statement, partially-completable operation (create, copy,
--      verify, drop, rename, recreate three indexes) -- a brand-new table
--      is a SINGLE atomic CREATE TABLE IF NOT EXISTS statement that can
--      never be "partially applied": there is no intermediate state for a
--      partial-failure detector to ever need to find.
--
-- SUPERSEDED as the primary application path by data/models.py's own
-- init_db() (called on every real app startup), which creates this exact
-- table via ensure_sql_view_semantic_enrichments_table() -- itself nothing
-- more than the same CREATE TABLE IF NOT EXISTS statement below, run
-- through the SAME executescript() call every other table in that file
-- already uses. Prefer letting init_db() apply this automatically. This
-- file is kept as documentation of the exact schema and for the
-- from-scratch/other-tooling case.
--
-- NEVER touches sql_semantic_enrichments (or any other existing table) in
-- any way -- no ALTER, no rebuild, no data copy, no index change to it.
-- Existing OBJECT_CORE/COLUMN_DEFERRED rows, their ids, timestamps,
-- statuses, and indexes are completely unaffected because this migration
-- never references that table at all.
--
-- No `phase` column here at all (unlike sql_semantic_enrichments): every
-- row in THIS table is, by table identity alone, a VIEW_ENRICHED record --
-- there is nothing to discriminate. core.sqlservice.semantic_enrichment_
-- phases.PHASE_VIEW_ENRICHED remains a valid Python-level constant (used
-- for SemanticEnrichmentCacheKey identity and readiness-status reporting)
-- purely as an in-memory label; it is never written to any column here.
--
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS -- safe to run any number
-- of times against the same database, including one that already has this
-- table from a prior init_db() call.
--
-- NOT applied against any live database as part of Step 6C1 -- this file
-- is written and tested (see tests/test_sqlservice_view_semantic_
-- enrichment_migration.py, which applies it only to a temporary, on-disk
-- SQLite copy) but never run against data/toolsmith.db in this step.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/017_create_view_semantic_enrichments.sql

CREATE TABLE IF NOT EXISTS sql_view_semantic_enrichments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
    object_fqn           TEXT    NOT NULL CHECK (length(object_fqn) > 0),
    semantic_input_hash  TEXT    NOT NULL CHECK (length(semantic_input_hash) = 64),
    provider_id          TEXT    NOT NULL CHECK (length(provider_id) > 0),
    model                TEXT    NOT NULL CHECK (length(model) > 0),
    contract_version     INTEGER NOT NULL CHECK (contract_version > 0),
    payload_json         TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('ACTIVE','INVALID')) DEFAULT 'ACTIVE',
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_svse_cache_key
    ON sql_view_semantic_enrichments (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version);
CREATE INDEX IF NOT EXISTS idx_svse_source_current
    ON sql_view_semantic_enrichments (source_id, status, provider_id, model, contract_version);
CREATE INDEX IF NOT EXISTS idx_svse_source_object
    ON sql_view_semantic_enrichments (source_id, object_fqn);
