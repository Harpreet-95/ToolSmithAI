-- Migration 018: Phase 7, Step 6E3B -- deterministic, persisted VIEW
-- profiling (sql_view_profiles).
--
-- Purely additive: a brand-new CREATE TABLE IF NOT EXISTS, exactly like
-- migrations/017_create_view_semantic_enrichments.sql. Never ALTERs,
-- rebuilds, or touches view_semantic_evidence, sql_view_semantic_
-- enrichments, schema_object_authority's own in-memory cache, or any
-- other existing table/row.
--
-- SUPERSEDED as the primary application path by data/models.py's own
-- init_db() (called on every real app startup), which creates this exact
-- table via ensure_sql_view_profiles_table() -- itself nothing more than
-- the same CREATE TABLE IF NOT EXISTS statement below. Prefer letting
-- init_db() apply this automatically. This file is kept as documentation
-- of the exact schema and for the from-scratch/other-tooling case.
--
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS -- safe to run any number
-- of times against the same database.
--
-- NOT applied against any live database as part of Step 6E3B -- this
-- file is written and tested (see tests/test_sqlservice_view_profiler.py,
-- which applies it only to a temporary, on-disk SQLite copy) but never
-- run against data/toolsmith.db in this step. Source-10004 live metadata
-- is untouched by this step.
--
-- ROLLBACK: this migration is safe to roll back at any time, on any
-- database, because every row in sql_view_profiles is a deterministically
-- recomputable cache of already-persisted inputs (view_semantic_evidence,
-- schema_object_authority, sql_retrieval_documents) -- nothing else in
-- the codebase treats a sql_view_profiles row as a source of truth for
-- anything that cannot be recomputed from those other tables.
--   DROP INDEX IF EXISTS idx_svp_source_authority;
--   DROP INDEX IF EXISTS idx_svp_source_status;
--   DROP INDEX IF EXISTS idx_svp_source_object_uniq;
--   DROP TABLE IF EXISTS sql_view_profiles;
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/018_create_view_profiles.sql

CREATE TABLE IF NOT EXISTS sql_view_profiles (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id                INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
    build_id                 INTEGER,
    object_fqn               TEXT    NOT NULL CHECK (length(object_fqn) > 0),
    schema_fingerprint       TEXT,
    definition_hash          TEXT,
    profiling_version        INTEGER NOT NULL CHECK (profiling_version > 0),
    completion_status        TEXT    NOT NULL CHECK (completion_status IN ('COMPLETE','INCOMPLETE','FAILED')),
    failure_reason           TEXT,
    authority_class          TEXT,
    lineage_complete         INTEGER NOT NULL DEFAULT 0,
    is_framework_backed      INTEGER NOT NULL DEFAULT 0,
    suitable_for_detail      INTEGER NOT NULL DEFAULT 0,
    suitable_for_count       INTEGER NOT NULL DEFAULT 0,
    suitable_for_aggregation INTEGER NOT NULL DEFAULT 0,
    suitable_for_join        INTEGER NOT NULL DEFAULT 0,
    duplication_risk         TEXT    NOT NULL DEFAULT 'UNKNOWN',
    row_preservation_risk    TEXT    NOT NULL DEFAULT 'UNKNOWN',
    required_distinct_key    TEXT,
    payload_json             TEXT    NOT NULL,
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_svp_source_object_uniq
    ON sql_view_profiles (source_id, object_fqn);
CREATE INDEX IF NOT EXISTS idx_svp_source_status
    ON sql_view_profiles (source_id, completion_status);
CREATE INDEX IF NOT EXISTS idx_svp_source_authority
    ON sql_view_profiles (source_id, authority_class);
