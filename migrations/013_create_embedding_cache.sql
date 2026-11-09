-- Migration 013: SQL rebuild Phase 2, Step 1 — sql_retrieval_embeddings
--
-- Additive only: one new table (plus its indexes), no changes to any
-- existing table or row. Safe to run on any existing database.
--
-- Mirrors data/models.py's init_db() exactly, so a fresh database (via
-- init_db()) and an existing database migrated with this script end up
-- schema-identical.
--
-- This is a content-addressed cache, deliberately independent of any
-- specific sql_retrieval_builds/sql_retrieval_documents row: the cache key
-- is (embedding_provider, embedding_model, embedding_dimensions,
-- document_hash), NOT build_id or document id. An unchanged document
-- (byte-identical document_text, hence the same document_hash) reuses its
-- embedding across every retrieval build and source that happens to
-- produce that same hash — no recomputation, no duplicate vectors. No
-- OpenAI/API calls, no real embeddings, and no vector similarity search
-- are introduced by this migration; it only creates local, empty storage.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/013_create_embedding_cache.sql

CREATE TABLE IF NOT EXISTS sql_retrieval_embeddings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding_provider   TEXT    NOT NULL CHECK (length(embedding_provider) > 0),
    embedding_model      TEXT    NOT NULL CHECK (length(embedding_model) > 0),
    embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
    document_hash        TEXT    NOT NULL CHECK (length(document_hash) = 64),
    vector_blob          BLOB    NOT NULL,
    vector_byte_length   INTEGER NOT NULL CHECK (vector_byte_length = embedding_dimensions * 4),
    created_at           TEXT    NOT NULL,
    CHECK (length(vector_blob) = vector_byte_length)
);

-- The cache key: at most one vector per (provider, model, dimensions,
-- document_hash) combination. A second insert attempt for the same key
-- with different vector bytes is a conflict the application layer
-- (core/sqlservice/embedding_store.py) rejects before this index would
-- ever be asked to enforce it; an identical byte-for-byte reinsert is a
-- reuse, not a write.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sre_provider_model_dims_hash
    ON sql_retrieval_embeddings (embedding_provider, embedding_model, embedding_dimensions, document_hash);
CREATE INDEX IF NOT EXISTS idx_sre_hash
    ON sql_retrieval_embeddings (document_hash);
