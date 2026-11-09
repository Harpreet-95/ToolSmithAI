-- Migration 014: SQL rebuild Phase 5, Step 9 — sql_examples / sql_example_embeddings
--
-- Additive only: two new tables (plus their indexes), no changes to any
-- existing table or row. Safe to run on any existing database.
--
-- Mirrors data/models.py's init_db() exactly, so a fresh database (via
-- init_db()) and an existing database migrated with this script end up
-- schema-identical.
--
-- sql_examples stores automatically-trusted (see core/sqlservice/
-- sql_example_model.py's classify_trust_level — NO manual approval
-- workflow anywhere) natural-language-question/SQL-template pairs, never
-- credentials, result rows, raw prompts/errors, or unnormalized literal
-- data. sql_example_embeddings is a content-addressed cache for their
-- search-document vectors, structurally identical in shape to
-- sql_retrieval_embeddings (migration 013) but a SEPARATE table so example
-- vectors are never conflated with schema-document vectors even though a
-- hash collision between the two is cryptographically negligible.
--
-- This migration creates empty storage only — it does not populate any
-- example, call an embedding provider, or run vector search.
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/014_create_sql_examples.sql

CREATE TABLE IF NOT EXISTS sql_examples (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id               INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
    schema_fingerprint      TEXT    NOT NULL,
    question_pattern        TEXT    NOT NULL CHECK (length(question_pattern) > 0),
    sql_template             TEXT    NOT NULL CHECK (length(sql_template) > 0),
    canonical_object_fqns_json TEXT NOT NULL,
    canonical_columns_json  TEXT    NOT NULL DEFAULT '[]',
    join_path_json          TEXT    NOT NULL DEFAULT '[]',
    result_shape            TEXT    NOT NULL CHECK (result_shape IN ('COUNT','LIST','GROUPED_AGGREGATION','TREND','ATTRIBUTE')),
    provenance               TEXT    NOT NULL CHECK (provenance IN ('SCHEMA_GENERATED_TEMPLATE','DEVELOPER_VERIFICATION_SET','VALIDATED_EXECUTION','OBSERVED_UNVERIFIED')),
    trust_level              TEXT    NOT NULL CHECK (trust_level IN ('TRUSTED','UNTRUSTED')),
    search_document_text    TEXT    NOT NULL,
    search_document_hash    TEXT    NOT NULL CHECK (length(search_document_hash) = 64),
    content_hash             TEXT    NOT NULL CHECK (length(content_hash) = 64),
    status                   TEXT    NOT NULL CHECK (status IN ('ACTIVE','STALE')) DEFAULT 'ACTIVE',
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);

-- Idempotent insert identity: the same logical example (by its own
-- immutable content_hash) for the same source is never duplicated.
CREATE UNIQUE INDEX IF NOT EXISTS idx_se_source_content_hash
    ON sql_examples (source_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_se_source_status_trust
    ON sql_examples (source_id, status, trust_level);
CREATE INDEX IF NOT EXISTS idx_se_search_document_hash
    ON sql_examples (search_document_hash);

CREATE TABLE IF NOT EXISTS sql_example_embeddings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding_provider    TEXT    NOT NULL CHECK (length(embedding_provider) > 0),
    embedding_model       TEXT    NOT NULL CHECK (length(embedding_model) > 0),
    embedding_dimensions  INTEGER NOT NULL CHECK (embedding_dimensions > 0),
    search_document_hash  TEXT    NOT NULL CHECK (length(search_document_hash) = 64),
    vector_blob           BLOB    NOT NULL,
    vector_byte_length    INTEGER NOT NULL CHECK (vector_byte_length = embedding_dimensions * 4),
    created_at            TEXT    NOT NULL,
    CHECK (length(vector_blob) = vector_byte_length)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_seem_provider_model_dims_hash
    ON sql_example_embeddings (embedding_provider, embedding_model, embedding_dimensions, search_document_hash);
CREATE INDEX IF NOT EXISTS idx_seem_hash
    ON sql_example_embeddings (search_document_hash);
