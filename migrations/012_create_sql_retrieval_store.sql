-- Migration 012: SQL rebuild Phase 1, Step 5 — sql_retrieval_builds / sql_retrieval_documents
--
-- Additive only: two new tables (plus their indexes), no changes to any
-- existing table or row. Safe to run on any existing database.
--
-- Mirrors data/models.py's init_db() exactly, so a fresh database (via
-- init_db()) and an existing database migrated with this script end up
-- schema-identical.
--
-- Updated in Step 5A: idx_srb_source_fingerprint_docset (a plain index)
-- was replaced with idx_srb_unique_build (a UNIQUE index) covering
-- (source_id, fingerprint_version, schema_fingerprint, document_set_hash)
-- — the database-enforced idempotency/concurrency guarantee. No prior
-- release ever ran the original non-unique version of this migration
-- against a real database, so this is an in-place edit, not a new
-- migration file.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/012_create_sql_retrieval_store.sql

CREATE TABLE IF NOT EXISTS sql_retrieval_builds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
    snapshot_id         INTEGER NOT NULL REFERENCES schema_snapshots(id),
    snapshot_version    INTEGER NOT NULL,
    fingerprint_version INTEGER NOT NULL,
    schema_fingerprint  TEXT    NOT NULL,
    table_set_hash      TEXT    NOT NULL,
    document_set_hash   TEXT    NOT NULL,
    status              TEXT    NOT NULL CHECK (status IN ('BUILDING','READY','FAILED')),
    document_count      INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT    CHECK (error_message IS NULL OR length(error_message) <= 2000),
    created_at          TEXT    NOT NULL,
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_srb_source_status
    ON sql_retrieval_builds (source_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_srb_unique_build
    ON sql_retrieval_builds (source_id, fingerprint_version, schema_fingerprint, document_set_hash);

CREATE TABLE IF NOT EXISTS sql_retrieval_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id        INTEGER NOT NULL REFERENCES sql_retrieval_builds(id) ON DELETE CASCADE,
    source_id       INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
    object_fqn      TEXT    NOT NULL,
    object_type     TEXT    NOT NULL CHECK (object_type IN ('TABLE','VIEW')),
    retrieval_tier  INTEGER NOT NULL CHECK (retrieval_tier BETWEEN 1 AND 4),
    document_hash   TEXT    NOT NULL,
    document_text   TEXT    NOT NULL,
    metadata_json   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_srd_build_fqn
    ON sql_retrieval_documents (build_id, object_fqn);
CREATE INDEX IF NOT EXISTS idx_srd_build_id
    ON sql_retrieval_documents (build_id);
CREATE INDEX IF NOT EXISTS idx_srd_build_tier
    ON sql_retrieval_documents (build_id, retrieval_tier);
CREATE INDEX IF NOT EXISTS idx_srd_hash
    ON sql_retrieval_documents (document_hash);
