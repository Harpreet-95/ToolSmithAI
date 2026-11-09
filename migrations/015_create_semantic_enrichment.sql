-- Migration 015: SQL Architecture Correction, Phase 6, Step 4B —
-- sql_semantic_enrichments
--
-- Additive only: one new table (plus its indexes), no changes to any
-- existing table or row. Safe to run on any existing database.
--
-- Mirrors data/models.py's init_db() exactly, so a fresh database (via
-- init_db()) and an existing database migrated with this script end up
-- schema-identical.
--
-- Persists structurally validated, model-inferred object/column semantics
-- (core/sqlservice/semantic_enrichment_service.py's EnrichedObjectSemantics
-- + EnrichedColumnSemantics — never arbitrary raw provider JSON; see
-- core/sqlservice/semantic_enrichment_store.py, the only writer of this
-- table) by per-object semantic-input hash. NO manual approval workflow —
-- model semantics are automatically usable, but only ever as bounded
-- advisory evidence (see core/sqlservice/semantic_catalog.py's
-- build_semantic_catalog enrichment_input parameter).
--
-- payload_json stores ONLY the strictly-validated semantic payload for one
-- object plus its columns (business_name/description/object_role/
-- entity_concepts/aliases/supported_operations/confidence/provenance per
-- object, and the analogous per-column fields) — never a prompt, a
-- question, a row value, a credential, the provider's raw response, or
-- exception text.
--
-- Cache identity (source_id, object_fqn, semantic_input_hash, provider_id,
-- model, contract_version) is a UNIQUE index: historical versions are
-- retained (a schema/dictionary change that produces a NEW
-- semantic_input_hash for the same object simply adds a NEW row; it never
-- updates or deletes the prior one), and a stale hash is excluded from
-- "current" results purely by the STORE layer's own query discipline
-- (core/sqlservice/semantic_enrichment_store.py only ever asks for rows
-- matching the CALLER-SUPPLIED current hash per object) — never by
-- deleting old rows.
--
-- status ACTIVE|INVALID: `mark_enrichment_invalid_by_id()` flips a
-- corrupt/untrusted row to INVALID without deleting or altering
-- payload_json — every load function in the store excludes INVALID rows
-- from its results, so an invalid row's content is retained for audit but
-- never again surfaced to a caller ("unusable without exposing content").
--
-- Run once against your existing toolsmith.db:
--   sqlite3 data/toolsmith.db < migrations/015_create_semantic_enrichment.sql

CREATE TABLE IF NOT EXISTS sql_semantic_enrichments (
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

-- The complete cache-key identity (Phase 6 Step 4B requirement 1): at most
-- one row per (source, object, exact semantic input, provider, model,
-- contract version). A second insert attempt at this exact key is a reuse,
-- never a duplicate or an update — see insert_semantic_enrichment()'s own
-- idempotent-insert handling of the resulting IntegrityError.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sse_cache_key
    ON sql_semantic_enrichments (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version);
-- Source/current-preparation lookup: "every ACTIVE row for this source
-- under this (provider, model, contract_version)" — the exact shape
-- load_current_enrichments_for_source() queries before filtering to the
-- caller's current per-object hashes in application code.
CREATE INDEX IF NOT EXISTS idx_sse_source_current
    ON sql_semantic_enrichments (source_id, status, provider_id, model, contract_version);
CREATE INDEX IF NOT EXISTS idx_sse_source_object
    ON sql_semantic_enrichments (source_id, object_fqn);
