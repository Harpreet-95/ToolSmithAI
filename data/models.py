import json
import logging

from data.db import get_connection

_SSE_TABLE = "sql_semantic_enrichments"
_SSE_PHASE_COLUMN = "phase"
_SSE_CACHE_KEY_INDEX = "idx_sse_cache_key"
_SSE_SOURCE_CURRENT_INDEX = "idx_sse_source_current"


class SemanticEnrichmentMigrationError(RuntimeError):
    """Raised by ensure_sql_semantic_enrichments_phase_migration() when
    sql_semantic_enrichments is in a state it cannot safely reconcile
    automatically -- a PARTIALLY applied migration 016 (the `phase` column
    present without the phase-inclusive unique index, or vice versa),
    almost certainly meaning an earlier migration attempt was interrupted
    or the schema was manually edited outside this codebase's own
    migration path. Never silently tolerated, guessed past, or auto-
    repaired -- a human must inspect the database before init_db() can
    proceed."""


def _sse_table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_SSE_TABLE,)
    ).fetchone() is not None


def _sse_columns(conn) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({_SSE_TABLE})").fetchall()}


def _sse_index_columns(conn, index_name: str) -> tuple:
    return tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall())


def ensure_sql_semantic_enrichments_phase_migration(conn) -> str:
    """
    Idempotent, schema-STATE-checking equivalent of
    migrations/016_add_semantic_enrichment_phase.sql's ALTER TABLE ADD
    COLUMN -- SQL Architecture Correction, Phase 6, Step 4C2A, Problem 2.

    Why this exists: SQLite's ALTER TABLE ADD COLUMN has no "IF NOT EXISTS"
    form (unlike CREATE TABLE/INDEX), so migration 016's raw SQL cannot be
    re-run safely the way every other statement in init_db() already can
    be. This project has NO separate migration-ledger/runner mechanism
    (verified: no migrations table, no "applied migrations" tracking of
    any kind exists anywhere in this codebase) -- init_db() ITSELF, called
    on every real app startup (see api/app.py), IS the project's own
    established idempotent-migration mechanism for exactly this situation;
    see this same function's neighboring PRAGMA table_info(...)-guarded
    ALTER TABLE blocks for profiling_column_profiles (PII classification /
    Phase 1A-C columns) for the identical, already-production-proven
    pattern this function follows.

    Returns "APPLIED" (the table existed and predated Step 4C2 -- this call
    just added the column/indexes) or "ALREADY_CURRENT" (nothing to do --
    either a fresh Step-4C2+ CREATE TABLE already has `phase` from the
    start, the table doesn't exist yet at all, or a prior call already
    migrated it). Idempotent and cheap to call on every init_db(): when
    already current, this is a handful of read-only PRAGMA queries, never a
    write.

    Never touches a ROW of sql_semantic_enrichments, and never rebuilds or
    copies the table: ALTER TABLE ADD COLUMN in SQLite is schema-metadata-
    only (existing rows read back the DEFAULT lazily; the table file itself
    is not rewritten), and the index changes below are DROP/CREATE INDEX
    only (index-only, no row data touched) -- safe regardless of how large
    the real table has grown.

    Raises SemanticEnrichmentMigrationError -- never silently applied,
    ignored, or guessed past -- for a genuinely PARTIALLY migrated state:
    the `phase` column present without the phase-inclusive unique index, or
    vice versa. This is structurally impossible to reach via THIS function
    alone (both are always applied together, in the block below) but is
    still checked explicitly, defensively, in case of manual tampering or
    an incomplete external migration attempt.
    """
    if not _sse_table_exists(conn):
        return "ALREADY_CURRENT"

    has_phase_column = _SSE_PHASE_COLUMN in _sse_columns(conn)
    index_has_phase = _SSE_PHASE_COLUMN in _sse_index_columns(conn, _SSE_CACHE_KEY_INDEX)

    if has_phase_column and index_has_phase:
        return "ALREADY_CURRENT"

    if has_phase_column != index_has_phase:
        raise SemanticEnrichmentMigrationError(
            f"{_SSE_TABLE} is in a partially migrated state: phase column present={has_phase_column}, "
            f"phase-inclusive unique index present={index_has_phase} -- resolve this manually "
            "(inspect PRAGMA table_info/index_info) before init_db() can proceed safely"
        )

    conn.execute(
        f"ALTER TABLE {_SSE_TABLE} ADD COLUMN {_SSE_PHASE_COLUMN} TEXT NOT NULL "
        "DEFAULT 'COLUMN_DEFERRED' CHECK (phase IN ('OBJECT_CORE','COLUMN_DEFERRED'))"
    )
    # SQLite has no ALTER INDEX -- drop and recreate is the only option, and
    # is safe (an index carries no data of its own, only derived pointers).
    conn.execute(f"DROP INDEX IF EXISTS {_SSE_CACHE_KEY_INDEX}")
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_SSE_CACHE_KEY_INDEX} ON {_SSE_TABLE} "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase)"
    )
    conn.execute(f"DROP INDEX IF EXISTS {_SSE_SOURCE_CURRENT_INDEX}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_SSE_SOURCE_CURRENT_INDEX} ON {_SSE_TABLE} "
        "(source_id, status, provider_id, model, contract_version, phase)"
    )
    conn.commit()
    return "APPLIED"


_SVSE_TABLE = "sql_view_semantic_enrichments"


def _svse_table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_SVSE_TABLE,)
    ).fetchone() is not None


def ensure_sql_view_semantic_enrichments_table(conn) -> str:
    """
    Idempotent creation of `sql_view_semantic_enrichments` -- Phase 6 Step
    6C1's replacement for the Step 6C design (which widened
    sql_semantic_enrichments' `phase` CHECK constraint via a table rebuild;
    see migrations/017_create_view_semantic_enrichments.sql's own docstring
    for the full audit and why that rebuild was abandoned in favor of this
    brand-new, dedicated table).

    Deliberately NOT a rebuild, an ALTER TABLE, or any other schema
    SURGERY on an existing table: this is a plain `CREATE TABLE IF NOT
    EXISTS`, exactly like every other table in this file (see the
    `sql_semantic_enrichments` block just above, as it stood BEFORE Step
    6C ever touched it -- entirely unmodified by this function or by
    anything in Step 6C1). A `CREATE TABLE IF NOT EXISTS` statement is
    trivially idempotent and cannot be "partially applied" the way a
    multi-statement rebuild can -- there is no intermediate state for a
    partial-failure detector to find, and nothing here ever reads or
    writes a single row of `sql_semantic_enrichments` or any other table.

    Returns "APPLIED" (the table did not exist yet -- this call created it)
    or "ALREADY_CURRENT" (a prior call, or a fresh CREATE TABLE IF NOT
    EXISTS from this SAME function running again, already created it).
    """
    existed_before = _svse_table_exists(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {_SVSE_TABLE} (
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
            ON {_SVSE_TABLE} (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version);
        CREATE INDEX IF NOT EXISTS idx_svse_source_current
            ON {_SVSE_TABLE} (source_id, status, provider_id, model, contract_version);
        CREATE INDEX IF NOT EXISTS idx_svse_source_object
            ON {_SVSE_TABLE} (source_id, object_fqn);
        """
    )
    conn.commit()
    return "ALREADY_CURRENT" if existed_before else "APPLIED"


_SVP_TABLE = "sql_view_profiles"


def _svp_table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_SVP_TABLE,)
    ).fetchone() is not None


def ensure_sql_view_profiles_table(conn) -> str:
    """
    Idempotent creation of `sql_view_profiles` -- Phase 7, Step 6E3B:
    persisted, deterministic, per-view structural/lineage/suitability
    profile. One row per (source_id, object_fqn), upserted on
    recomputation (definition change, schema-fingerprint change, or
    profiling_version bump) -- mirrors view_semantic_evidence's own
    idx_vse_source_table_uniq one-row-per-view convention exactly.

    Plain `CREATE TABLE IF NOT EXISTS`, exactly like every other table in
    this file -- purely additive, no ALTER/rebuild, nothing here reads or
    writes any existing table (view_semantic_evidence, schema_snapshots,
    sql_retrieval_documents, ... are all read-only inputs to the module
    that populates this table, never touched by this function).

    Most of the rich per-view structural facts this milestone requires
    (output-column lineage, join kinds, WHERE/HAVING restrictions,
    literals, GROUP BY/aggregates, UNION branches, window functions,
    TOP/LIMIT, candidate grain + confidence, entity keys, duplication/
    row-preservation risk, population restrictions, purpose suitability,
    PII categories (informational only, never values), secret/credential
    column names, snapshot/cross-source status, evidence provenance) live
    in `payload_json` -- the same one-blob-per-row convention
    sql_view_semantic_enrichments/sql_semantic_enrichments already use for
    similarly-shaped rich per-object facts. A handful of the fields a
    validator needs to filter/gate on cheaply are ALSO promoted to real
    columns (authority_class, the four suitable_for_* flags,
    lineage_complete, is_framework_backed, duplication_risk,
    row_preservation_risk, required_distinct_key) so
    core.sqlservice.sql_validation can query them directly without parsing
    JSON for every candidate object.

    No raw row values or PII/secret VALUES are ever stored here -- only
    column NAMES, categories, and structural facts about the view
    definition itself (see core.sqlservice.view_profiler's own module
    docstring for the enforcement of that boundary).

    Returns "APPLIED" (first call) or "ALREADY_CURRENT" (already existed).

    Rollback: `DROP TABLE IF EXISTS sql_view_profiles;` -- purely additive,
    dropping it removes only this milestone's own cached profiles and
    cannot affect view_semantic_evidence, schema_object_authority, or any
    other existing table/row. Safe to run against a test database at any
    time; profiles are fully recomputable (deterministic, no live
    connection, no AI call) by re-running the connect-time bootstrap's
    VIEW_PROFILING step.
    """
    existed_before = _svp_table_exists(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {_SVP_TABLE} (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id               INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            build_id                INTEGER,
            object_fqn              TEXT    NOT NULL CHECK (length(object_fqn) > 0),
            schema_fingerprint      TEXT,
            definition_hash         TEXT,
            profiling_version       INTEGER NOT NULL CHECK (profiling_version > 0),
            completion_status       TEXT    NOT NULL CHECK (completion_status IN ('COMPLETE','INCOMPLETE','FAILED')),
            failure_reason          TEXT,
            authority_class         TEXT,
            lineage_complete        INTEGER NOT NULL DEFAULT 0,
            is_framework_backed     INTEGER NOT NULL DEFAULT 0,
            suitable_for_detail     INTEGER NOT NULL DEFAULT 0,
            suitable_for_count      INTEGER NOT NULL DEFAULT 0,
            suitable_for_aggregation INTEGER NOT NULL DEFAULT 0,
            suitable_for_join       INTEGER NOT NULL DEFAULT 0,
            duplication_risk        TEXT    NOT NULL DEFAULT 'UNKNOWN',
            row_preservation_risk   TEXT    NOT NULL DEFAULT 'UNKNOWN',
            required_distinct_key   TEXT,
            payload_json            TEXT    NOT NULL,
            created_at              TEXT    NOT NULL,
            updated_at              TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_svp_source_object_uniq
            ON {_SVP_TABLE} (source_id, object_fqn);
        CREATE INDEX IF NOT EXISTS idx_svp_source_status
            ON {_SVP_TABLE} (source_id, completion_status);
        CREATE INDEX IF NOT EXISTS idx_svp_source_authority
            ON {_SVP_TABLE} (source_id, authority_class);
        """
    )
    conn.commit()
    return "ALREADY_CURRENT" if existed_before else "APPLIED"


def ensure_business_knowledge_tables(conn) -> str:
    """
    Idempotent creation of the Phase 7, Step 3J business-knowledge-package
    tables -- a source-scoped, versioned store for owner-confirmed business
    facts (concepts/relationships/facts) plus the AI-proposed and
    deterministically-verified schema mappings derived from them
    (business_knowledge_mappings), and a bookkeeping table
    (business_knowledge_mapping_runs) that makes an unchanged source/schema/
    package rerun a zero-provider-call no-op (unique on package_id,
    schema_fingerprint, content_hash -- see
    core.sqlservice.business_knowledge_mapper).

    Plain `CREATE TABLE IF NOT EXISTS`, exactly like every other table in
    this file -- purely additive, no ALTER/rebuild, nothing here reads or
    writes an existing table. Authoritative (owner-supplied) rows are
    distinguished from AI-inferred ones purely by the `origin` column and by
    which service function is allowed to write them
    (data.business_knowledge_package_service never lets the AI mapping path
    touch a row with origin='AUTHORITATIVE') -- never by a separate schema.

    Returns "APPLIED" (first call) or "ALREADY_CURRENT" (already existed).
    """
    existed_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='business_knowledge_packages'"
    ).fetchone() is not None

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS business_knowledge_packages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id       INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            user_id         TEXT    NOT NULL,
            package_version INTEGER NOT NULL CHECK (package_version > 0),
            status          TEXT    NOT NULL CHECK (status IN ('DRAFT','ACTIVE','SUPERSEDED')) DEFAULT 'DRAFT',
            created_by      TEXT    NOT NULL,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkp_source_version
            ON business_knowledge_packages (source_id, package_version);
        CREATE INDEX IF NOT EXISTS idx_bkp_source_status
            ON business_knowledge_packages (source_id, status);

        CREATE TABLE IF NOT EXISTS business_knowledge_concepts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id      INTEGER NOT NULL REFERENCES business_knowledge_packages(id) ON DELETE CASCADE,
            concept_id      TEXT    NOT NULL CHECK (length(concept_id) > 0),
            concept_type    TEXT    NOT NULL CHECK (concept_type IN (
                                'DOMAIN','ENTITY','PROGRAM','LIFECYCLE_STAGE','MEASURE',
                                'STATUS','PAYMENT_CONCEPT','ACTIVITY_CONCEPT'
                            )),
            display_name    TEXT    NOT NULL CHECK (length(display_name) > 0),
            description     TEXT,
            aliases_json    TEXT    NOT NULL DEFAULT '[]',
            origin          TEXT    NOT NULL CHECK (origin IN ('AUTHORITATIVE','AI_INFERRED')),
            confidence      REAL    NOT NULL DEFAULT 1.0,
            evidence_json   TEXT    NOT NULL DEFAULT '[]',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkc_package_concept
            ON business_knowledge_concepts (package_id, concept_id);
        CREATE INDEX IF NOT EXISTS idx_bkc_package_type
            ON business_knowledge_concepts (package_id, concept_type);

        CREATE TABLE IF NOT EXISTS business_knowledge_relationships (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id          INTEGER NOT NULL REFERENCES business_knowledge_packages(id) ON DELETE CASCADE,
            from_concept_id     TEXT    NOT NULL CHECK (length(from_concept_id) > 0),
            relationship_type   TEXT    NOT NULL CHECK (length(relationship_type) > 0),
            to_concept_id       TEXT    NOT NULL CHECK (length(to_concept_id) > 0),
            origin              TEXT    NOT NULL CHECK (origin IN ('AUTHORITATIVE','AI_INFERRED')),
            confidence          REAL    NOT NULL DEFAULT 1.0,
            evidence_json       TEXT    NOT NULL DEFAULT '[]',
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkr_package_edge
            ON business_knowledge_relationships (package_id, from_concept_id, relationship_type, to_concept_id);
        CREATE INDEX IF NOT EXISTS idx_bkr_package_from
            ON business_knowledge_relationships (package_id, from_concept_id);

        CREATE TABLE IF NOT EXISTS business_knowledge_facts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id          INTEGER NOT NULL REFERENCES business_knowledge_packages(id) ON DELETE CASCADE,
            fact_key            TEXT    NOT NULL CHECK (length(fact_key) > 0),
            fact_text           TEXT    NOT NULL CHECK (length(fact_text) > 0),
            related_concept_id  TEXT,
            created_by          TEXT    NOT NULL,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkf_package_key
            ON business_knowledge_facts (package_id, fact_key);

        CREATE TABLE IF NOT EXISTS business_knowledge_mappings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id          INTEGER NOT NULL REFERENCES business_knowledge_packages(id) ON DELETE CASCADE,
            concept_id          TEXT    NOT NULL CHECK (length(concept_id) > 0),
            object_fqn          TEXT    NOT NULL CHECK (length(object_fqn) > 0),
            mapping_role        TEXT    NOT NULL CHECK (mapping_role IN (
                                    'CANONICAL_ENTITY_SOURCE','PREFERRED_BUSINESS_VIEW',
                                    'DETAIL_ACTIVITY_SOURCE','ENROLLMENT_SOURCE','FEE_SOURCE',
                                    'FULL_PAYMENT_SOURCE','INSTALLMENT_PAYMENT_SOURCE',
                                    'STATUS_SOURCE','OUTCOME_SOURCE','CONNECTOR_ONLY',
                                    'UNSUPPORTED_OR_CONFLICTING',
                                    'FEE_DEFINITION_SOURCE','FEE_PAYMENT_SOURCE'
                                )),
            columns_json        TEXT    NOT NULL DEFAULT '[]',
            mapping_kind        TEXT    NOT NULL CHECK (mapping_kind IN (
                                    'DIRECT','DEPENDENT','EQUIVALENT','CONDITIONAL','UNRESOLVED'
                                )),
            status              TEXT    NOT NULL CHECK (status IN (
                                    'PROPOSED','VERIFIED','UNRESOLVED','REJECTED'
                                )) DEFAULT 'PROPOSED',
            confidence          REAL    NOT NULL DEFAULT 0.0,
            evidence_json       TEXT    NOT NULL DEFAULT '[]',
            schema_fingerprint  TEXT    NOT NULL CHECK (length(schema_fingerprint) > 0),
            provider_id         TEXT,
            model               TEXT,
            contract_version    INTEGER NOT NULL DEFAULT 1,
            content_hash        TEXT    NOT NULL CHECK (length(content_hash) = 64),
            rejection_reason    TEXT,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkm_package_concept_object_role
            ON business_knowledge_mappings (package_id, concept_id, object_fqn, mapping_role);
        CREATE INDEX IF NOT EXISTS idx_bkm_package_object
            ON business_knowledge_mappings (package_id, object_fqn);
        CREATE INDEX IF NOT EXISTS idx_bkm_package_fingerprint
            ON business_knowledge_mappings (package_id, schema_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_bkm_package_status
            ON business_knowledge_mappings (package_id, status);

        CREATE TABLE IF NOT EXISTS business_knowledge_mapping_runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id           INTEGER NOT NULL REFERENCES business_knowledge_packages(id) ON DELETE CASCADE,
            schema_fingerprint   TEXT    NOT NULL CHECK (length(schema_fingerprint) > 0),
            content_hash         TEXT    NOT NULL CHECK (length(content_hash) = 64),
            status               TEXT    NOT NULL CHECK (status IN ('RUNNING','COMPLETE','FAILED')) DEFAULT 'RUNNING',
            batches_total        INTEGER NOT NULL DEFAULT 0,
            batches_completed    INTEGER NOT NULL DEFAULT 0,
            provider_calls_made  INTEGER NOT NULL DEFAULT 0,
            error_json           TEXT,
            started_at           TEXT    NOT NULL,
            completed_at         TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bkmr_package_fingerprint_hash
            ON business_knowledge_mapping_runs (package_id, schema_fingerprint, content_hash);
        """
    )
    conn.commit()
    return "ALREADY_CURRENT" if existed_before else "APPLIED"


def init_db() -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            email         TEXT,
            password_hash TEXT,
            is_active                  INTEGER NOT NULL DEFAULT 1,
            created_at                 TEXT,
            last_login                 TEXT,
            is_verified                INTEGER NOT NULL DEFAULT 0,
            verification_token_hash    TEXT,
            verification_token_expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tools (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            slug        TEXT,
            config      TEXT    NOT NULL DEFAULT '{}',
            config_json TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            approved    INTEGER NOT NULL DEFAULT 0,
            approved_by TEXT,
            approved_at TEXT,
            created_by  TEXT,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS workflows (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            definition TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT NOT NULL,
            task_type      TEXT,
            original_input TEXT,
            status         TEXT NOT NULL,
            user_id        TEXT
        );

        CREATE TABLE IF NOT EXISTS execution_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id        TEXT NOT NULL,
            workflow_id    INTEGER,
            trigger_source TEXT NOT NULL,
            task_type      TEXT,
            intent         TEXT,
            status         TEXT NOT NULL,
            started_at     TEXT NOT NULL,
            finished_at    TEXT NOT NULL,
            duration_ms    INTEGER,
            step_count     INTEGER NOT NULL,
            failed_step_id TEXT,
            failed_tool    TEXT,
            error_message  TEXT,
            user_id        TEXT
        );

        CREATE TABLE IF NOT EXISTS datasets (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                  TEXT NOT NULL,
            filename                 TEXT NOT NULL,
            uploaded_at              TEXT NOT NULL,
            row_count                INTEGER NOT NULL,
            column_count             INTEGER NOT NULL,
            columns_json             TEXT NOT NULL,
            numeric_profile_json     TEXT NOT NULL,
            missing_values_json      TEXT NOT NULL,
            categorical_profile_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled_workflows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL,
            dataset_id     INTEGER,
            input_text     TEXT    NOT NULL,
            task_type      TEXT    NOT NULL,
            frequency      TEXT    NOT NULL,
            day_of_week    TEXT,
            next_run_at    TEXT    NOT NULL,
            enabled        INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL,
            last_run_at    TEXT,
            last_status    TEXT,
            last_error     TEXT,
            run_count      INTEGER NOT NULL DEFAULT 0,
            engine_tool_id     TEXT,
            cron               TEXT,
            human_label        TEXT,
            refresh_before_run INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_sw_enabled_next_run
            ON scheduled_workflows (enabled, next_run_at);

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

        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            task_type   TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'completed',
            dataset_id  INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
            exec_id     INTEGER REFERENCES execution_history(id) ON DELETE SET NULL,
            workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
            schedule_id INTEGER REFERENCES scheduled_workflows(id) ON DELETE SET NULL,
            content_json TEXT   NOT NULL,
            summary_text TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            expires_at  TEXT,
            share_token TEXT    UNIQUE
        );

        CREATE INDEX IF NOT EXISTS idx_reports_user_id    ON reports (user_id);
        CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports (created_at);
        CREATE INDEX IF NOT EXISTS idx_reports_exec_id    ON reports (exec_id);

        CREATE TABLE IF NOT EXISTS notifications (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              TEXT    NOT NULL,
            type                 TEXT    NOT NULL DEFAULT 'info',
            title                TEXT    NOT NULL,
            message              TEXT    NOT NULL,
            status               TEXT    NOT NULL DEFAULT 'info',
            read                 INTEGER NOT NULL DEFAULT 0,
            related_report_id    INTEGER REFERENCES reports(id) ON DELETE SET NULL,
            related_execution_id INTEGER REFERENCES execution_history(id) ON DELETE SET NULL,
            created_at           TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notifications_user_id    ON notifications (user_id);
        CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications (created_at);
        CREATE INDEX IF NOT EXISTS idx_notifications_related_execution_id
            ON notifications (related_execution_id);

        CREATE TABLE IF NOT EXISTS scheduled_workflow_runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id          INTEGER NOT NULL REFERENCES scheduled_workflows(id) ON DELETE CASCADE,
            user_id              TEXT    NOT NULL,
            status               TEXT    NOT NULL,
            started_at           TEXT    NOT NULL,
            finished_at          TEXT,
            duration_ms          INTEGER,
            trigger_type         TEXT    NOT NULL DEFAULT 'scheduled',
            error_message        TEXT,
            related_execution_id  INTEGER REFERENCES execution_history(id) ON DELETE SET NULL,
            related_report_id     INTEGER REFERENCES reports(id) ON DELETE SET NULL,
            reprofile_status      TEXT,
            reprofile_duration_ms INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule_id ON scheduled_workflow_runs (schedule_id);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_user_id     ON scheduled_workflow_runs (user_id);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_started_at  ON scheduled_workflow_runs (started_at);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_related_execution_id
            ON scheduled_workflow_runs (related_execution_id);

        CREATE TABLE IF NOT EXISTS report_metric_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT    NOT NULL,
            report_id     INTEGER REFERENCES reports(id) ON DELETE SET NULL,
            dataset_id    INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
            task_type     TEXT    NOT NULL,
            snapshot_json TEXT    NOT NULL,
            created_at    TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_report_metric_snapshots_user_id    ON report_metric_snapshots (user_id);
        CREATE INDEX IF NOT EXISTS idx_report_metric_snapshots_dataset_id ON report_metric_snapshots (dataset_id);
        CREATE INDEX IF NOT EXISTS idx_report_metric_snapshots_created_at ON report_metric_snapshots (created_at);

        CREATE TABLE IF NOT EXISTS ai_workspaces (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT    NOT NULL,
            title           TEXT    NOT NULL DEFAULT 'Untitled Workspace',
            status          TEXT    NOT NULL DEFAULT 'draft',
            intent_text     TEXT,
            dataset_id      INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
            proposal_json   TEXT,
            proposal_source TEXT,
            proposed_at     TEXT,
            workflow_id     INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ai_workspaces_user_id    ON ai_workspaces (user_id);
        CREATE INDEX IF NOT EXISTS idx_ai_workspaces_status     ON ai_workspaces (status);
        CREATE INDEX IF NOT EXISTS idx_ai_workspaces_created_at ON ai_workspaces (created_at);

        CREATE TABLE IF NOT EXISTS admin_invites (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT    NOT NULL,
            invite_token_hash TEXT    NOT NULL UNIQUE,
            role              TEXT    NOT NULL DEFAULT 'admin',
            used              INTEGER NOT NULL DEFAULT 0,
            expires_at        TEXT    NOT NULL,
            created_by        TEXT,
            created_at        TEXT    NOT NULL,
            used_at           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_admin_invites_email      ON admin_invites (email);
        CREATE INDEX IF NOT EXISTS idx_admin_invites_token_hash ON admin_invites (invite_token_hash);

        CREATE TABLE IF NOT EXISTS email_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT,
            report_id       INTEGER REFERENCES reports(id) ON DELETE SET NULL,
            recipient_email TEXT    NOT NULL,
            subject         TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            error_reason    TEXT,
            sent_at         TEXT,
            created_at      TEXT    NOT NULL,
            email_type      TEXT    NOT NULL DEFAULT 'report'
        );

        CREATE INDEX IF NOT EXISTS idx_email_logs_user_id    ON email_logs (user_id);
        CREATE INDEX IF NOT EXISTS idx_email_logs_report_id  ON email_logs (report_id);
        CREATE INDEX IF NOT EXISTS idx_email_logs_status     ON email_logs (status);
        CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs (created_at);

        CREATE TABLE IF NOT EXISTS export_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT    NOT NULL,
            report_id       INTEGER REFERENCES reports(id) ON DELETE SET NULL,
            export_format   TEXT    NOT NULL,
            filename        TEXT,
            file_size_bytes INTEGER,
            status          TEXT    NOT NULL DEFAULT 'success',
            error_reason    TEXT,
            ip_address      TEXT,
            user_agent      TEXT,
            exported_at     TEXT,
            created_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_export_logs_user_id       ON export_logs (user_id);
        CREATE INDEX IF NOT EXISTS idx_export_logs_report_id     ON export_logs (report_id);
        CREATE INDEX IF NOT EXISTS idx_export_logs_export_format ON export_logs (export_format);
        CREATE INDEX IF NOT EXISTS idx_export_logs_exported_at   ON export_logs (exported_at);
    """)
    conn.commit()

    # Idempotent column migrations for existing databases that pre-date these columns.
    # SQLite does not support IF NOT EXISTS on ALTER TABLE, so we check the schema first.
    existing_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(users)").fetchall()
    }
    migrations = [
        ("email",         "ALTER TABLE users ADD COLUMN email TEXT"),
        ("password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
        ("is_active",     "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"),
        ("created_at",                    "ALTER TABLE users ADD COLUMN created_at TEXT"),
        ("last_login",                    "ALTER TABLE users ADD COLUMN last_login TEXT"),
        ("is_verified",                   "ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0"),
        ("verification_token_hash",       "ALTER TABLE users ADD COLUMN verification_token_hash TEXT"),
        ("verification_token_expires_at", "ALTER TABLE users ADD COLUMN verification_token_expires_at TEXT"),
    ]
    for column, statement in migrations:
        if column not in existing_columns:
            cursor.execute(statement)
    conn.commit()

    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"
    )
    conn.commit()

    # Idempotent migrations for scheduled_workflows status tracking columns.
    # PRAGMA table_info returns empty for non-existent tables, so this is safe
    # even when the table was just created above with all columns present.
    sw_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(scheduled_workflows)").fetchall()
    }
    sw_migrations = [
        ("last_run_at",    "ALTER TABLE scheduled_workflows ADD COLUMN last_run_at TEXT"),
        ("last_status",    "ALTER TABLE scheduled_workflows ADD COLUMN last_status TEXT"),
        ("last_error",     "ALTER TABLE scheduled_workflows ADD COLUMN last_error TEXT"),
        ("run_count",      "ALTER TABLE scheduled_workflows ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0"),
        ("engine_tool_id",    "ALTER TABLE scheduled_workflows ADD COLUMN engine_tool_id TEXT"),
        ("cron",              "ALTER TABLE scheduled_workflows ADD COLUMN cron TEXT"),
        ("human_label",       "ALTER TABLE scheduled_workflows ADD COLUMN human_label TEXT"),
        ("refresh_before_run", "ALTER TABLE scheduled_workflows ADD COLUMN refresh_before_run INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, stmt in sw_migrations:
        if col not in sw_existing:
            cursor.execute(stmt)
    conn.commit()

    # Idempotent migrations for scheduled_workflow_runs: reprofile tracking columns.
    swr_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(scheduled_workflow_runs)").fetchall()
    }
    swr_migrations = [
        ("reprofile_status",      "ALTER TABLE scheduled_workflow_runs ADD COLUMN reprofile_status TEXT"),
        ("reprofile_duration_ms", "ALTER TABLE scheduled_workflow_runs ADD COLUMN reprofile_duration_ms INTEGER"),
    ]
    for col, stmt in swr_migrations:
        if col not in swr_existing:
            cursor.execute(stmt)
    conn.commit()

    # Idempotent migration: add user_id to workflows for per-user ownership.
    # Existing NULL rows become orphaned but are not deleted.
    wf_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(workflows)").fetchall()
    }
    if "user_id" not in wf_existing:
        cursor.execute("ALTER TABLE workflows ADD COLUMN user_id TEXT")
    conn.commit()

    # Idempotent migrations for datasets table.
    # All new columns are nullable TEXT so existing rows remain valid.
    ds_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(datasets)").fetchall()
    }
    ds_migrations = [
        ("date_profile_json",           "ALTER TABLE datasets ADD COLUMN date_profile_json TEXT"),
        ("correlation_profile_json",    "ALTER TABLE datasets ADD COLUMN correlation_profile_json TEXT"),
        ("categorical_meta_json",       "ALTER TABLE datasets ADD COLUMN categorical_meta_json TEXT"),
        ("semantic_profile_json",       "ALTER TABLE datasets ADD COLUMN semantic_profile_json TEXT"),
        ("segmentation_profile_json",   "ALTER TABLE datasets ADD COLUMN segmentation_profile_json TEXT"),
        ("file_path",                   "ALTER TABLE datasets ADD COLUMN file_path TEXT"),
    ]
    for col, stmt in ds_migrations:
        if col not in ds_existing:
            cursor.execute(stmt)
    conn.commit()

    # Idempotent migration: add dataset_id to execution_history so each
    # execution row records which dataset was active at run time.
    eh_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(execution_history)").fetchall()
    }
    if "dataset_id" not in eh_existing:
        cursor.execute(
            "ALTER TABLE execution_history ADD COLUMN dataset_id INTEGER REFERENCES datasets(id)"
        )
    conn.commit()

    # Idempotent migrations for tools table: extend the minimal original schema
    # (id, name, config) with the Dynamic Tool Composer foundation columns.
    tools_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(tools)").fetchall()
    }
    tools_migrations = [
        ("slug",         "ALTER TABLE tools ADD COLUMN slug TEXT"),
        ("config_json",  "ALTER TABLE tools ADD COLUMN config_json TEXT"),
        ("enabled",      "ALTER TABLE tools ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
        ("approved",     "ALTER TABLE tools ADD COLUMN approved INTEGER NOT NULL DEFAULT 0"),
        ("approved_by",  "ALTER TABLE tools ADD COLUMN approved_by TEXT"),
        ("approved_at",  "ALTER TABLE tools ADD COLUMN approved_at TEXT"),
        ("created_by",   "ALTER TABLE tools ADD COLUMN created_by TEXT"),
        ("created_at",   "ALTER TABLE tools ADD COLUMN created_at TEXT"),
    ]
    for col, stmt in tools_migrations:
        if col not in tools_existing:
            cursor.execute(stmt)
    conn.commit()

    # Idempotent migrations for ai_workspaces: execution persistence columns.
    aw_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(ai_workspaces)").fetchall()
    }
    aw_migrations = [
        ("report_id",              "ALTER TABLE ai_workspaces ADD COLUMN report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL"),
        ("execution_summary_json", "ALTER TABLE ai_workspaces ADD COLUMN execution_summary_json TEXT"),
        ("selected_sections_json", "ALTER TABLE ai_workspaces ADD COLUMN selected_sections_json TEXT"),
        ("executed_at",            "ALTER TABLE ai_workspaces ADD COLUMN executed_at TEXT"),
        ("saved_at",               "ALTER TABLE ai_workspaces ADD COLUMN saved_at TEXT"),
        ("workflow_id",            "ALTER TABLE ai_workspaces ADD COLUMN workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL"),
    ]
    for col, stmt in aw_migrations:
        if col not in aw_existing:
            cursor.execute(stmt)
    conn.commit()

    # dataset_source_replacements — audit trail for every replace-source operation.
    # Each row records one attempt: pending → success | failed.
    # ON DELETE CASCADE keeps history only as long as the dataset exists.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS dataset_source_replacements (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id        INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            user_id           TEXT    NOT NULL,
            old_file_path     TEXT,
            new_file_path     TEXT    NOT NULL,
            original_filename TEXT    NOT NULL,
            status            TEXT    NOT NULL DEFAULT 'pending',
            error             TEXT,
            replaced_at       TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ds_replacements_dataset_id
            ON dataset_source_replacements (dataset_id);
        CREATE INDEX IF NOT EXISTS idx_ds_replacements_user_id
            ON dataset_source_replacements (user_id);
        CREATE INDEX IF NOT EXISTS idx_ds_replacements_replaced_at
            ON dataset_source_replacements (replaced_at);
    """)
    conn.commit()

    # Seed the three static built-in tools so the DB reflects the registry.
    # Uses name as the stable identity key. Existing rows get their slug and
    # approved flag backfilled; new rows are inserted with full metadata.
    _BUILTIN_SEEDS = [
        ("email_sender", "email_sender"),
        ("data_fetcher", "data_fetcher"),
        ("notifier",     "notifier"),
    ]
    for name, slug in _BUILTIN_SEEDS:
        existing = cursor.execute(
            "SELECT id, slug FROM tools WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            cursor.execute(
                "INSERT INTO tools (name, slug, config, enabled, approved) "
                "VALUES (?, ?, '{}', 1, 1)",
                (name, slug),
            )
        elif existing[1] is None or existing[1] == "":
            # Backfill slug and mark approved for pre-migration rows
            cursor.execute(
                "UPDATE tools SET slug = ?, enabled = 1, approved = 1 WHERE name = ?",
                (slug, name),
            )
    conn.commit()

    # Idempotent migration: status index on export_logs for filter-by-status queries.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_export_logs_status ON export_logs (status)"
    )
    conn.commit()

    # data_source_connections — external data sources; credentials stored in encrypted_config_json.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS data_source_connections (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               TEXT    NOT NULL,
            display_name          TEXT    NOT NULL,
            source_type           TEXT    NOT NULL,
            source_category       TEXT    NOT NULL,
            encrypted_config_json TEXT    NOT NULL,
            config_schema_version INTEGER NOT NULL DEFAULT 1,
            capabilities_json     TEXT    NOT NULL DEFAULT '[]',
            metadata_json         TEXT    NOT NULL DEFAULT '{}',
            source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
            is_active             INTEGER NOT NULL DEFAULT 1,
            live_query_enabled    INTEGER NOT NULL DEFAULT 0,
            last_tested_at        TEXT,
            last_test_status      TEXT,
            last_test_message     TEXT,
            created_at            TEXT    NOT NULL,
            updated_at            TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_dsc_user_id
            ON data_source_connections (user_id);
        CREATE INDEX IF NOT EXISTS idx_dsc_source_type
            ON data_source_connections (source_type);
        CREATE INDEX IF NOT EXISTS idx_dsc_source_category
            ON data_source_connections (source_category);
        CREATE INDEX IF NOT EXISTS idx_dsc_is_active
            ON data_source_connections (is_active);
        CREATE INDEX IF NOT EXISTS idx_dsc_source_status
            ON data_source_connections (source_status);
    """)
    conn.commit()

    # Product decision: live query execution is enabled by default for every
    # SQL data source (any connector whose registered capabilities include
    # 'sql_query'), not gated behind a manual post-connect flag flip.
    # Governed read-only enforcement, SQL validation, and ownership
    # isolation are unchanged — this only lifts the previous default-off
    # gate in core.live.connection_resolver.LiveConnectionResolver, it does
    # not touch what SQL is allowed to run. Idempotent: re-running only
    # affects rows not already enabled, and only ones with 'sql_query'
    # capability, so non-SQL sources are untouched.
    for row in cursor.execute(
        "SELECT id, capabilities_json FROM data_source_connections WHERE live_query_enabled = 0"
    ).fetchall():
        try:
            capabilities = json.loads(row["capabilities_json"] or "[]")
        except (TypeError, ValueError):
            capabilities = []
        if "sql_query" in capabilities:
            cursor.execute(
                "UPDATE data_source_connections SET live_query_enabled = 1 WHERE id = ?",
                (row["id"],),
            )
    conn.commit()

    # Phase 7, Step 6H, Part 2/3 — stable datasource identity + lifecycle
    # state. Additive only: two nullable/defaulted columns, idempotent via
    # the same PRAGMA table_info guard used throughout this file.
    # `connection_state` is deliberately separate from the pre-existing
    # `source_status` (connectivity-test result: ACTIVE/ERROR) and
    # `is_active` (soft-hide flag) columns — neither of those express the
    # new disconnect/credentials-removed/reconnect lifecycle, and changing
    # their existing meaning would be a silent behavior change for every
    # current caller of list_data_sources/is_ready_to_ask.
    dsc_existing = {
        row[1] for row in cursor.execute("PRAGMA table_info(data_source_connections)").fetchall()
    }
    dsc_migrations = [
        ("datasource_identity_key", "ALTER TABLE data_source_connections ADD COLUMN datasource_identity_key TEXT"),
        ("connection_state", "ALTER TABLE data_source_connections ADD COLUMN connection_state TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ]
    for column, statement in dsc_migrations:
        if column not in dsc_existing:
            cursor.execute(statement)
    conn.commit()

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsc_identity_key ON data_source_connections (datasource_identity_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsc_connection_state ON data_source_connections (connection_state)"
    )
    conn.commit()

    # One-time best-effort backfill for pre-existing rows with no identity
    # key yet. A row whose config fails to decrypt (corrupt row, rotated
    # ENCRYPTION_KEY with no migration) is skipped and logged — never fatal
    # to init_db(), exactly like every other best-effort step in this
    # codebase's connect/bootstrap path.
    backfill_rows = cursor.execute(
        "SELECT id, user_id, source_type, encrypted_config_json FROM data_source_connections "
        "WHERE datasource_identity_key IS NULL"
    ).fetchall()
    if backfill_rows:
        from core.secrets.datasource_identity import compute_datasource_identity_key
        from core.secrets.manager import get_secret_manager

        secret_manager = get_secret_manager()
        for row in backfill_rows:
            try:
                params = json.loads(secret_manager.decrypt_secret(row["encrypted_config_json"]))
                identity_key = compute_datasource_identity_key(row["user_id"], row["source_type"], params)
                cursor.execute(
                    "UPDATE data_source_connections SET datasource_identity_key = ? WHERE id = ?",
                    (identity_key, row["id"]),
                )
            except Exception:  # noqa: BLE001 — best-effort backfill, never fatal
                logging.getLogger(__name__).warning(
                    "init_db: datasource identity backfill failed for source_id=%s", row["id"],
                )
        conn.commit()

    # datasource_lifecycle_audit — Phase 7, Step 6H, Part 3/9. One row per
    # explicit lifecycle action (disconnect/credentials-removed/reconnect/
    # permanent delete) — never a row for routine connectivity tests or
    # automatic metadata refreshes, which already have their own tracking
    # (last_tested_at / metadata_jobs). No FK to data_source_connections(id):
    # a permanent-delete audit record must outlive the row it describes.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS datasource_lifecycle_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id       INTEGER NOT NULL,
            user_id         TEXT    NOT NULL,
            action          TEXT    NOT NULL,
            reason          TEXT,
            created_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_dsla_source_id ON datasource_lifecycle_audit (source_id);
        CREATE INDEX IF NOT EXISTS idx_dsla_user_id   ON datasource_lifecycle_audit (user_id);
        CREATE INDEX IF NOT EXISTS idx_dsla_action     ON datasource_lifecycle_audit (action);
    """)
    conn.commit()

    # fast_path_durable_plans — Phase 7, Step 6H, Part 4. Persistent
    # counterpart to core.sqlservice.fast_path_plan_cache's in-memory
    # OrderedDict, keyed by datasource_identity_key (NOT source_id) so a
    # reconnect of the same physical database can still hit — mirrors the
    # sql_retrieval_embeddings/sql_example_embeddings content-addressed,
    # non-source-scoped keying idiom identified as reusable by the Step 6H0
    # audit. Deliberately NOT FK'd to data_source_connections(id), for the
    # same reason those two tables aren't: it must survive both a reconnect
    # (new or reused source_id) and must not be silently resurrected by a
    # later, unrelated source reusing the same identity after a genuine
    # permanent delete — see this table's own module docstring in
    # core.sqlservice.durable_plan_store for how plan_format_version /
    # schema_fingerprint / business-knowledge version naturally invalidate a
    # stale row instead of a hard FK being required to do so.
    #
    # Never stores: raw question text, resolved_standalone_question, result
    # rows, PII, credentials, embeddings, or raw model prompts/responses —
    # only normalized_question_hash (a one-way hash) plus the same bounded,
    # already-audited-safe structural plan fields
    # core.sqlservice.fast_path_plan_cache.FastPathCachedPlan already stores
    # in memory (intent/selected objects/reasons/sql/decision evidence/
    # confidence/model), serialized into plan_payload_json.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS fast_path_durable_plans (
            id                              INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key_hash                  TEXT    NOT NULL UNIQUE,
            datasource_identity_key         TEXT    NOT NULL,
            user_id                         TEXT    NOT NULL,
            normalized_question_hash        TEXT    NOT NULL,
            schema_fingerprint              TEXT    NOT NULL,
            retrieval_build_id              INTEGER,
            business_knowledge_package_id   INTEGER,
            business_knowledge_package_version INTEGER,
            metrics_contract_version        TEXT,
            view_profile_state              TEXT,
            relationship_state_digest       TEXT,
            secret_policy_version           INTEGER NOT NULL DEFAULT 0,
            fast_path_model                 TEXT    NOT NULL,
            fast_path_reasoning_effort      TEXT,
            fast_path_prompt_version        TEXT    NOT NULL,
            plan_format_version             INTEGER NOT NULL,
            plan_payload_json               TEXT    NOT NULL,
            created_at                      TEXT    NOT NULL,
            last_used_at                    TEXT    NOT NULL,
            expires_at                      TEXT,
            use_count                       INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_fpdp_identity_key ON fast_path_durable_plans (datasource_identity_key);
        CREATE INDEX IF NOT EXISTS idx_fpdp_user_id       ON fast_path_durable_plans (user_id);
        CREATE INDEX IF NOT EXISTS idx_fpdp_expires_at    ON fast_path_durable_plans (expires_at);
    """)
    conn.commit()

    # schema_snapshots — versioned schema discovery results per data source connection.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS schema_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            snapshot_version INTEGER NOT NULL DEFAULT 1,
            source_type      TEXT    NOT NULL,
            table_count      INTEGER NOT NULL DEFAULT 0,
            view_count       INTEGER NOT NULL DEFAULT 0,
            column_count     INTEGER NOT NULL DEFAULT 0,
            snapshot_json    TEXT    NOT NULL,
            discovered_at    TEXT    NOT NULL,
            created_at       TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ss_source_id
            ON schema_snapshots (source_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ss_source_version
            ON schema_snapshots (source_id, snapshot_version);
    """)
    conn.commit()

    # Idempotent migrations for data_source_connections: Phase 2 discovery columns.
    dsc_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(data_source_connections)").fetchall()
    }
    dsc_migrations = [
        ("last_discovered_at", "ALTER TABLE data_source_connections ADD COLUMN last_discovered_at TEXT"),
        ("last_snapshot_id",   "ALTER TABLE data_source_connections ADD COLUMN last_snapshot_id INTEGER"),
        ("live_query_enabled", "ALTER TABLE data_source_connections ADD COLUMN live_query_enabled INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, stmt in dsc_migrations:
        if col not in dsc_existing:
            cursor.execute(stmt)
    conn.commit()

    # data_dictionary_tables — AI/rule-generated and human-validated table descriptions.
    # UNIQUE (source_id, table_fqn) enables upsert semantics on re-generation;
    # human-edited rows are protected in the service layer, not here.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS data_dictionary_tables (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            snapshot_id       INTEGER NOT NULL REFERENCES schema_snapshots(id),
            table_fqn         TEXT    NOT NULL,
            table_name        TEXT    NOT NULL,
            schema_name       TEXT    NOT NULL,
            table_type        TEXT    NOT NULL,
            business_name     TEXT,
            description       TEXT,
            domain            TEXT,
            grain             TEXT,
            is_approved       INTEGER NOT NULL DEFAULT 0,
            approved_by       TEXT,
            approved_at       TEXT,
            generation_method TEXT    NOT NULL DEFAULT 'rule_based',
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ddt_source_fqn
            ON data_dictionary_tables (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_ddt_source_id
            ON data_dictionary_tables (source_id);
        CREATE INDEX IF NOT EXISTS idx_ddt_source_approved
            ON data_dictionary_tables (source_id, is_approved);
        CREATE INDEX IF NOT EXISTS idx_ddt_source_domain
            ON data_dictionary_tables (source_id, domain);
    """)
    conn.commit()

    # data_dictionary_columns — AI/rule-generated and human-validated column descriptions.
    # UNIQUE (source_id, table_fqn, column_name) enables upsert semantics on re-generation.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS data_dictionary_columns (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            snapshot_id       INTEGER NOT NULL REFERENCES schema_snapshots(id),
            table_fqn         TEXT    NOT NULL,
            column_name       TEXT    NOT NULL,
            business_label    TEXT,
            meaning           TEXT,
            semantic_type     TEXT,
            is_metric         INTEGER NOT NULL DEFAULT 0,
            is_dimension      INTEGER NOT NULL DEFAULT 0,
            is_date           INTEGER NOT NULL DEFAULT 0,
            is_id             INTEGER NOT NULL DEFAULT 0,
            pii_risk          INTEGER NOT NULL DEFAULT 0,
            is_approved       INTEGER NOT NULL DEFAULT 0,
            approved_by       TEXT,
            approved_at       TEXT,
            generation_method TEXT    NOT NULL DEFAULT 'rule_based',
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ddc_source_fqn_col
            ON data_dictionary_columns (source_id, table_fqn, column_name);
        CREATE INDEX IF NOT EXISTS idx_ddc_source_fqn
            ON data_dictionary_columns (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_ddc_source_approved
            ON data_dictionary_columns (source_id, is_approved);
        CREATE INDEX IF NOT EXISTS idx_ddc_source_pii
            ON data_dictionary_columns (source_id, pii_risk);
        CREATE INDEX IF NOT EXISTS idx_ddc_source_semantic
            ON data_dictionary_columns (source_id, semantic_type);
    """)
    conn.commit()

    # profiling_snapshots — one record per profiling run per data source.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS profiling_snapshots (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            schema_snapshot_id       INTEGER NOT NULL REFERENCES schema_snapshots(id),
            snapshot_version         INTEGER NOT NULL DEFAULT 1,
            mode                     TEXT    NOT NULL DEFAULT 'full',
            sample_rate              REAL    NOT NULL DEFAULT 1.0,
            profiling_rules_version  TEXT    NOT NULL DEFAULT '1.0.0',
            status                   TEXT    NOT NULL DEFAULT 'PENDING',
            tables_total             INTEGER NOT NULL DEFAULT 0,
            tables_profiled          INTEGER NOT NULL DEFAULT 0,
            tables_skipped           INTEGER NOT NULL DEFAULT 0,
            tables_failed            INTEGER NOT NULL DEFAULT 0,
            tables_timed_out         INTEGER NOT NULL DEFAULT 0,
            columns_total            INTEGER NOT NULL DEFAULT 0,
            columns_profiled         INTEGER NOT NULL DEFAULT 0,
            columns_skipped          INTEGER NOT NULL DEFAULT 0,
            total_rows_profiled      INTEGER NOT NULL DEFAULT 0,
            pii_columns_found        INTEGER NOT NULL DEFAULT 0,
            classifications_complete INTEGER NOT NULL DEFAULT 0,
            started_at               TEXT,
            completed_at             TEXT,
            duration_seconds         INTEGER,
            resumable_state_json     TEXT,
            created_at               TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_prsnap_source_version
            ON profiling_snapshots (source_id, snapshot_version);
        CREATE INDEX IF NOT EXISTS idx_prsnap_source_id
            ON profiling_snapshots (source_id);
        CREATE INDEX IF NOT EXISTS idx_prsnap_status
            ON profiling_snapshots (status);
    """)
    conn.commit()

    # Idempotent migrations for profiling_snapshots: Phase 4D batch profiling columns.
    # Placed here — after CREATE TABLE profiling_snapshots — so fresh-DB init succeeds.
    psnap_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(profiling_snapshots)").fetchall()
    }
    psnap_migrations = [
        ("batch_size",        "ALTER TABLE profiling_snapshots ADD COLUMN batch_size INTEGER NOT NULL DEFAULT 50"),
        ("next_table_index",  "ALTER TABLE profiling_snapshots ADD COLUMN next_table_index INTEGER NOT NULL DEFAULT 0"),
        ("cancel_requested",  "ALTER TABLE profiling_snapshots ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, stmt in psnap_migrations:
        if col not in psnap_existing:
            cursor.execute(stmt)
    conn.commit()

    # profiling_table_profiles — per-table statistical and classification results.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS profiling_table_profiles (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            profiling_snapshot_id       INTEGER NOT NULL REFERENCES profiling_snapshots(id) ON DELETE CASCADE,
            source_id                   INTEGER NOT NULL,
            table_fqn                   TEXT    NOT NULL,
            table_name                  TEXT    NOT NULL,
            schema_name                 TEXT    NOT NULL,
            table_type                  TEXT    NOT NULL DEFAULT 'TABLE',
            exact_row_count             INTEGER,
            estimated_row_count         INTEGER,
            row_count_tier              TEXT,
            has_date_column             INTEGER NOT NULL DEFAULT 0,
            date_column_name            TEXT,
            earliest_record             TEXT,
            latest_record               TEXT,
            data_span_days              INTEGER,
            data_currency               TEXT    NOT NULL DEFAULT 'UNKNOWN',
            column_count                INTEGER NOT NULL DEFAULT 0,
            pk_column_count             INTEGER NOT NULL DEFAULT 0,
            fk_count                    INTEGER NOT NULL DEFAULT 0,
            referenced_by_count         INTEGER NOT NULL DEFAULT 0,
            is_junction_table           INTEGER NOT NULL DEFAULT 0,
            is_root_table               INTEGER NOT NULL DEFAULT 0,
            is_leaf_table               INTEGER NOT NULL DEFAULT 0,
            has_identity_column         INTEGER NOT NULL DEFAULT 0,
            avg_null_percentage         REAL,
            completeness_score          REAL,
            table_class                 TEXT,
            classification_confidence   REAL,
            classification_evidence_json TEXT,
            competing_classes_json      TEXT,
            classification_rule_version TEXT,
            pii_column_count            INTEGER NOT NULL DEFAULT 0,
            confirmed_pii_count         INTEGER NOT NULL DEFAULT 0,
            profiling_depth             TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
            profiling_duration_ms       INTEGER,
            profiling_status            TEXT    NOT NULL DEFAULT 'PENDING',
            skip_reason                 TEXT,
            profiled_at                 TEXT,
            created_at                  TEXT    NOT NULL,
            updated_at                  TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_prtp_snapshot_fqn
            ON profiling_table_profiles (profiling_snapshot_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_prtp_source_fqn
            ON profiling_table_profiles (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_prtp_source_class
            ON profiling_table_profiles (source_id, table_class);
        CREATE INDEX IF NOT EXISTS idx_prtp_status
            ON profiling_table_profiles (profiling_status);
    """)
    conn.commit()

    # One-time repair: back-fill columns_total for profiling_snapshots rows that
    # were created by the pre-fix batch profiling INSERT (which omitted the field,
    # leaving the DB DEFAULT of 0).  Only rows that already have stored
    # profiling_table_profiles data are touched; snapshots with no table profiles
    # keep columns_total = 0 because there is nothing to derive from.
    # Safe to run on every init: the WHERE columns_total = 0 guard makes it a
    # no-op once any affected row has been repaired.
    cursor.execute("""
        UPDATE profiling_snapshots
        SET columns_total = (
            SELECT COALESCE(SUM(column_count), 0)
            FROM profiling_table_profiles
            WHERE profiling_table_profiles.profiling_snapshot_id = profiling_snapshots.id
        )
        WHERE columns_total = 0
        AND EXISTS (
            SELECT 1
            FROM profiling_table_profiles
            WHERE profiling_table_profiles.profiling_snapshot_id = profiling_snapshots.id
        )
    """)
    conn.commit()

    # profiling_column_profiles — per-column statistical and semantic-type results.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS profiling_column_profiles (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            profiling_snapshot_id   INTEGER NOT NULL REFERENCES profiling_snapshots(id) ON DELETE CASCADE,
            source_id               INTEGER NOT NULL,
            table_fqn               TEXT    NOT NULL,
            column_name             TEXT    NOT NULL,
            data_type               TEXT    NOT NULL,
            raw_type                TEXT,
            is_nullable             INTEGER NOT NULL DEFAULT 1,
            is_primary_key          INTEGER NOT NULL DEFAULT 0,
            is_identity             INTEGER NOT NULL DEFAULT 0,
            ordinal_position        INTEGER NOT NULL DEFAULT 0,
            null_count              INTEGER,
            null_percentage         REAL,
            populated_count         INTEGER,
            populated_percentage    REAL,
            empty_string_count      INTEGER,
            zero_count              INTEGER,
            distinct_count          INTEGER,
            distinct_percentage     REAL,
            uniqueness_score        REAL,
            cardinality_tier        TEXT,
            min_value               TEXT,
            max_value               TEXT,
            min_length              INTEGER,
            max_length_observed     INTEGER,
            avg_length              REAL,
            mean_value              REAL,
            std_deviation           REAL,
            p5_value                TEXT,
            p95_value               TEXT,
            dominant_pattern        TEXT,
            pattern_coverage        REAL,
            email_match_rate        REAL,
            phone_match_rate        REAL,
            guid_match_rate         REAL,
            date_string_rate        REAL,
            numeric_string_rate     REAL,
            masked_value_rate       REAL,
            semantic_type           TEXT,
            semantic_confidence     REAL,
            semantic_evidence_json  TEXT,
            semantic_rule_version   TEXT,
            pii_name_heuristic      INTEGER NOT NULL DEFAULT 0,
            pii_confirmed           INTEGER NOT NULL DEFAULT 0,
            pii_signals_json        TEXT,
            top_values_coverage     REAL,
            profiling_depth         TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
            profiling_duration_ms   INTEGER,
            profiling_status        TEXT    NOT NULL DEFAULT 'PENDING',
            created_at              TEXT    NOT NULL,
            updated_at              TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_prcp_snapshot_col
            ON profiling_column_profiles (profiling_snapshot_id, table_fqn, column_name);
        CREATE INDEX IF NOT EXISTS idx_prcp_source_fqn
            ON profiling_column_profiles (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_prcp_source_semantic
            ON profiling_column_profiles (source_id, semantic_type);
        CREATE INDEX IF NOT EXISTS idx_prcp_source_pii
            ON profiling_column_profiles (source_id, pii_confirmed);
    """)
    conn.commit()

    # Idempotent migrations for autonomous PII classification (see
    # data/pii_classification_service.py). pii_confirmed stays the existing
    # boolean gate query_execution_service._governance_recheck already reads
    # (True only for pii_classification == CONFIRMED_PII); these columns add
    # the tri-state decision plus evidence/source/manual-override precedence
    # on top of it without changing pii_confirmed's existing semantics.
    prcp_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(profiling_column_profiles)").fetchall()
    }
    prcp_migrations = [
        ("pii_classification",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_classification TEXT"),
        ("pii_classification_confidence",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_classification_confidence REAL"),
        ("pii_classification_evidence",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_classification_evidence TEXT"),
        ("pii_classification_source",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_classification_source TEXT"),
        ("pii_classified_at",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_classified_at TEXT"),
        ("pii_manual_override",
         "ALTER TABLE profiling_column_profiles ADD COLUMN pii_manual_override INTEGER NOT NULL DEFAULT 0"),
    ]
    for column, statement in prcp_migrations:
        if column not in prcp_existing:
            cursor.execute(statement)
    conn.commit()

    # Idempotent migrations for profiling_column_profiles: Phase 1A deep profiling.
    # Adds percentile quartile columns and blank_percentage derived metric.
    _pcp_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(profiling_column_profiles)").fetchall()
    }
    _pcp_migrations = [
        # Phase 1A — deep statistical profiling (percentile quartiles + blank rate)
        ("p25_value",        "ALTER TABLE profiling_column_profiles ADD COLUMN p25_value TEXT"),
        ("p50_value",        "ALTER TABLE profiling_column_profiles ADD COLUMN p50_value TEXT"),
        ("p75_value",        "ALTER TABLE profiling_column_profiles ADD COLUMN p75_value TEXT"),
        ("blank_percentage", "ALTER TABLE profiling_column_profiles ADD COLUMN blank_percentage REAL"),
        # Phase 1B — distribution intelligence (histogram + shape classification)
        ("histogram_json",    "ALTER TABLE profiling_column_profiles ADD COLUMN histogram_json TEXT"),
        ("distribution_shape","ALTER TABLE profiling_column_profiles ADD COLUMN distribution_shape TEXT"),
        # Phase 1C — data quality intelligence (completeness, consistency, validity, quality score)
        ("completeness_score",       "ALTER TABLE profiling_column_profiles ADD COLUMN completeness_score REAL"),
        ("format_consistency_score", "ALTER TABLE profiling_column_profiles ADD COLUMN format_consistency_score REAL"),
        ("valid_count",              "ALTER TABLE profiling_column_profiles ADD COLUMN valid_count INTEGER"),
        ("invalid_count",            "ALTER TABLE profiling_column_profiles ADD COLUMN invalid_count INTEGER"),
        ("invalid_percentage",       "ALTER TABLE profiling_column_profiles ADD COLUMN invalid_percentage REAL"),
        ("validation_status",        "ALTER TABLE profiling_column_profiles ADD COLUMN validation_status TEXT"),
        ("quality_score",            "ALTER TABLE profiling_column_profiles ADD COLUMN quality_score REAL"),
        ("quality_grade",            "ALTER TABLE profiling_column_profiles ADD COLUMN quality_grade TEXT"),
        ("quality_summary_json",     "ALTER TABLE profiling_column_profiles ADD COLUMN quality_summary_json TEXT"),
    ]
    for _col, _stmt in _pcp_migrations:
        if _col not in _pcp_existing:
            cursor.execute(_stmt)
    conn.commit()

    # profiling_value_samples — top-N and random sample values per column.
    # value is NULL for PII columns; never stores actual sensitive data.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS profiling_value_samples (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            profiling_column_profile_id INTEGER NOT NULL REFERENCES profiling_column_profiles(id) ON DELETE CASCADE,
            sample_type                 TEXT    NOT NULL,
            value                       TEXT,
            row_count                   INTEGER,
            percentage                  REAL,
            rank                        INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_prvs_col_profile
            ON profiling_value_samples (profiling_column_profile_id);
    """)
    conn.commit()

    # metadata_jobs — background metadata collection lifecycle per data source.
    # Statuses: QUEUED → RUNNING → COMPLETE | FAILED
    # Steps:    DISCOVERY → STRUCTURAL_PROFILING → READY
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS metadata_jobs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            user_id          TEXT    NOT NULL,
            job_type         TEXT    NOT NULL DEFAULT 'initial_metadata',
            status           TEXT    NOT NULL DEFAULT 'QUEUED',
            current_step     TEXT,
            progress_message TEXT,
            error_message    TEXT,
            started_at       TEXT,
            completed_at     TEXT,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mj_source_id ON metadata_jobs (source_id);
        CREATE INDEX IF NOT EXISTS idx_mj_user_id   ON metadata_jobs (user_id);
        CREATE INDEX IF NOT EXISTS idx_mj_status    ON metadata_jobs (status);
    """)
    conn.commit()

    # domain_assignments — one row per (source, table), upserted on each generation run.
    # Cascades from both data_source_connections and profiling_snapshots so orphan rows
    # are never left behind when either parent is deleted.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS domain_assignments (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id              INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            profiling_snapshot_id  INTEGER NOT NULL REFERENCES profiling_snapshots(id) ON DELETE CASCADE,
            table_fqn              TEXT    NOT NULL,
            domain                 TEXT    NOT NULL,
            confidence             REAL    NOT NULL DEFAULT 0.0,
            evidence_json          TEXT    NOT NULL DEFAULT '[]',
            competing_domains_json TEXT    NOT NULL DEFAULT '[]',
            created_at             TEXT    NOT NULL,
            updated_at             TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_da_source_fqn
            ON domain_assignments (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_da_source_id
            ON domain_assignments (source_id);
        CREATE INDEX IF NOT EXISTS idx_da_source_domain
            ON domain_assignments (source_id, domain);
        CREATE INDEX IF NOT EXISTS idx_da_snapshot_id
            ON domain_assignments (profiling_snapshot_id);
    """)
    conn.commit()

    # concept_table_mappings — Phase 2 scoped semantic memory: remembers that
    # a business concept term (e.g. "clients") resolves to a specific
    # table/column on a given source, with confidence/evidence/governance
    # state, mirroring the domain_assignments pattern above but keyed for
    # MULTIPLE competing candidate rows per (source_id, concept_term) rather
    # than domain_assignments' one-row-per-table shape — a concept can have
    # several candidate tables before one is ever approved. column_name uses
    # '' (not NULL) as the table-level sentinel: SQLite unique indexes treat
    # NULL as non-colliding, so a nullable column_name would silently defeat
    # this uniqueness constraint for every table-level (concept-only) row.
    # "Only one approved mapping active per term" is an application-level
    # invariant (see data/concept_mapping_service.py's sibling-demotion on
    # approval), not a DB constraint — this repo doesn't use partial unique
    # indexes/triggers elsewhere, so this matches existing style.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS concept_table_mappings (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                 INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            concept_term              TEXT    NOT NULL,
            table_fqn                 TEXT    NOT NULL,
            column_name               TEXT    NOT NULL DEFAULT '',
            confidence                REAL    NOT NULL DEFAULT 0.0,
            evidence_json             TEXT    NOT NULL DEFAULT '[]',
            competing_candidates_json TEXT    NOT NULL DEFAULT '[]',
            assignment_source         TEXT    NOT NULL DEFAULT 'human'
                                       CHECK (assignment_source IN ('human','auto_governance','inferred_retrieval')),
            governance_state          TEXT    NOT NULL DEFAULT 'HUMAN_APPROVED',
            created_by                TEXT,
            created_at                TEXT    NOT NULL,
            updated_at                TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ctm_source_term_table_column
            ON concept_table_mappings (source_id, concept_term, table_fqn, column_name);
        CREATE INDEX IF NOT EXISTS idx_ctm_source_term
            ON concept_table_mappings (source_id, concept_term);
        CREATE INDEX IF NOT EXISTS idx_ctm_source_id
            ON concept_table_mappings (source_id);
    """)
    conn.commit()

    # concept_term_synonyms — Phase 3 natural-language learning: records that
    # a concept term (synonym_term) has been taught by a human, via the
    # clarification "remember this choice" action, to mean the same thing as
    # another term (canonical_term) that already has an approved mapping in
    # concept_table_mappings, scoped per source. One row per synonym_term —
    # a term means one thing at a time; teaching it a new meaning requires
    # explicit user confirmation to replace the row (see
    # concept_mapping_service.remember_synonym), never a silent or automatic
    # overwrite. Deliberately no confidence/approval-state lifecycle: this is
    # conversational memory ("the AI learned your terminology"), not a
    # governed metadata object like concept_table_mappings. Distinct from the
    # existing static data/synonyms.json (global, curated) — this is
    # per-source and learned from what a human actually taught it.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS concept_term_synonyms (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            synonym_term      TEXT    NOT NULL,
            canonical_term    TEXT    NOT NULL,
            created_by        TEXT,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_cts_source_synonym
            ON concept_term_synonyms (source_id, synonym_term);
        CREATE INDEX IF NOT EXISTS idx_cts_source_id
            ON concept_term_synonyms (source_id);
    """)
    conn.commit()

    # generated_business_vocabulary — Enterprise Phase 4 autonomous vocabulary
    # bootstrap: business terms (organization, candidate, placement, ...)
    # derived deterministically from schema/table/column names, dictionary
    # descriptions, and relationship/domain/entity evidence for a source,
    # WITHOUT any human approval step. Deliberately a separate table from
    # concept_table_mappings (human/governance-approved) and
    # concept_term_synonyms (human-taught conversational memory) — a
    # generated row is never promoted into either of those tables and never
    # outranks them; retrieval always consults explicit/approved vocabulary
    # first (see data/vocabulary_bootstrap_service.py). Idempotent and
    # replaceable: a rerun for a source deletes and re-derives every row for
    # that source_id in one transaction, so rediscovery never accumulates
    # stale rows. Multiple competing rows per (source_id, term) are expected
    # and intentional when a term is genuinely ambiguous across tables — this
    # table never silently collapses ambiguity to one winner; that decision
    # is left to the existing retrieval/clarification gates that already
    # consume concept_table_mappings.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS generated_business_vocabulary (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id          INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            schema_snapshot_id INTEGER NOT NULL REFERENCES schema_snapshots(id) ON DELETE CASCADE,
            term               TEXT    NOT NULL,
            table_fqn          TEXT    NOT NULL,
            column_name        TEXT    NOT NULL DEFAULT '',
            confidence_tier    TEXT    NOT NULL CHECK (confidence_tier IN ('HIGH','MEDIUM','LOW')),
            confidence_score   REAL    NOT NULL DEFAULT 0.0,
            evidence_json      TEXT    NOT NULL DEFAULT '[]',
            created_at         TEXT    NOT NULL,
            updated_at         TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_gbv_source_term_table_column
            ON generated_business_vocabulary (source_id, term, table_fqn, column_name);
        CREATE INDEX IF NOT EXISTS idx_gbv_source_term
            ON generated_business_vocabulary (source_id, term);
        CREATE INDEX IF NOT EXISTS idx_gbv_source_id
            ON generated_business_vocabulary (source_id);
    """)
    conn.commit()

    # semantic_entity_contracts — Day 2B (Automatic Business Semantic
    # Grounding). One versioned, auto-generated "contract" per fixed
    # business-entity name (Student, Invoice, ...) per source, resolving
    # entity name -> ONE preferred table/view + curated business semantics
    # (status/date/measure columns, verified status values, trusted
    # relationships, excluded derivative objects). Never overwritten —
    # always insert a new contract_version row, same idiom as
    # schema_snapshots.snapshot_version; readers take MAX(contract_version).
    # No governance/approval columns: contracts are fully automatic
    # (data.semantic_contract_service), never routed through
    # governance_state_map.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_entity_contracts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id          INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            entity_name        TEXT    NOT NULL,
            contract_version   INTEGER NOT NULL DEFAULT 1,
            canonical_table_fqn TEXT,
            preferred_view_fqn TEXT,
            resolution_status  TEXT    NOT NULL DEFAULT 'RESOLVED',
            confidence         REAL    NOT NULL DEFAULT 0.0,
            contract_json      TEXT    NOT NULL DEFAULT '{}',
            metadata_revision  TEXT    NOT NULL,
            generation_method  TEXT    NOT NULL DEFAULT 'auto_grounding',
            created_at         TEXT    NOT NULL,
            updated_at         TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sec_source_entity_version
            ON semantic_entity_contracts (source_id, entity_name, contract_version);
        CREATE INDEX IF NOT EXISTS idx_sec_source_entity
            ON semantic_entity_contracts (source_id, entity_name);
    """)
    conn.commit()

    # domain_learning_rules — per-source learned naming conventions.
    # UNIQUE(source_id, pattern_type, pattern_value) lets re-runs skip
    # already-suggested patterns via ON CONFLICT DO NOTHING.
    # active=1 only when approval_status='APPROVED'.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS domain_learning_rules (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            pattern_type     TEXT    NOT NULL,
            pattern_value    TEXT    NOT NULL,
            domain           TEXT    NOT NULL,
            confidence       REAL    NOT NULL DEFAULT 0.8,
            approval_status  TEXT    NOT NULL DEFAULT 'PENDING',
            created_by       TEXT    NOT NULL,
            approved_by      TEXT,
            created_at       TEXT    NOT NULL,
            approved_at      TEXT,
            active           INTEGER NOT NULL DEFAULT 0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_dlr_source_type_val
            ON domain_learning_rules (source_id, pattern_type, pattern_value);
        CREATE INDEX IF NOT EXISTS idx_dlr_source_id
            ON domain_learning_rules (source_id);
        CREATE INDEX IF NOT EXISTS idx_dlr_source_status
            ON domain_learning_rules (source_id, approval_status);
        CREATE INDEX IF NOT EXISTS idx_dlr_source_active
            ON domain_learning_rules (source_id, active);
    """)
    conn.commit()

    # domain_rule_refinement_suggestions — sub-rule candidates produced by
    # analyze_rule_refinement().  Unique on (parent_rule_id, pattern_type,
    # pattern_value) so re-runs are idempotent via ON CONFLICT DO NOTHING.
    # Cascades from data_source_connections and domain_learning_rules.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS domain_rule_refinement_suggestions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            parent_rule_id   INTEGER NOT NULL REFERENCES domain_learning_rules(id) ON DELETE CASCADE,
            pattern_type     TEXT    NOT NULL,
            pattern_value    TEXT    NOT NULL,
            suggested_domain TEXT    NOT NULL,
            support_count    INTEGER NOT NULL DEFAULT 0,
            confidence       REAL    NOT NULL DEFAULT 0.0,
            approval_status  TEXT    NOT NULL DEFAULT 'PENDING',
            created_at       TEXT    NOT NULL,
            approved_at      TEXT,
            approved_by      TEXT,
            active           INTEGER NOT NULL DEFAULT 0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_drrs_parent_type_val
            ON domain_rule_refinement_suggestions (parent_rule_id, pattern_type, pattern_value);
        CREATE INDEX IF NOT EXISTS idx_drrs_source_id
            ON domain_rule_refinement_suggestions (source_id);
        CREATE INDEX IF NOT EXISTS idx_drrs_parent_rule_id
            ON domain_rule_refinement_suggestions (parent_rule_id);
        CREATE INDEX IF NOT EXISTS idx_drrs_source_status
            ON domain_rule_refinement_suggestions (source_id, approval_status);
        CREATE INDEX IF NOT EXISTS idx_drrs_source_active
            ON domain_rule_refinement_suggestions (source_id, active);
    """)
    conn.commit()

    # entity_assignments — one row per (source, table), upserted on each generation run.
    # Mirrors domain_assignments but classifies the primary business entity a table represents.
    # Cascades from both data_source_connections and profiling_snapshots.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS entity_assignments (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            profiling_snapshot_id    INTEGER NOT NULL REFERENCES profiling_snapshots(id) ON DELETE CASCADE,
            table_fqn                TEXT    NOT NULL,
            entity                   TEXT    NOT NULL,
            confidence               REAL    NOT NULL DEFAULT 0.0,
            evidence_json            TEXT    NOT NULL DEFAULT '[]',
            competing_entities_json  TEXT    NOT NULL DEFAULT '[]',
            created_at               TEXT    NOT NULL,
            updated_at               TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ea_source_fqn
            ON entity_assignments (source_id, table_fqn);
        CREATE INDEX IF NOT EXISTS idx_ea_source_id
            ON entity_assignments (source_id);
        CREATE INDEX IF NOT EXISTS idx_ea_source_entity
            ON entity_assignments (source_id, entity);
        CREATE INDEX IF NOT EXISTS idx_ea_snapshot_id
            ON entity_assignments (profiling_snapshot_id);
    """)
    conn.commit()

    # entity_learning_rules — per-source learned entity naming conventions.
    # Mirrors domain_learning_rules but classifies the primary business entity.
    # UNIQUE(source_id, pattern_type, pattern_value) lets re-runs skip
    # already-suggested patterns via ON CONFLICT DO NOTHING.
    # active=1 only when approval_status='APPROVED'.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS entity_learning_rules (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            pattern_type     TEXT    NOT NULL,
            pattern_value    TEXT    NOT NULL,
            entity           TEXT    NOT NULL,
            confidence       REAL    NOT NULL DEFAULT 0.8,
            approval_status  TEXT    NOT NULL DEFAULT 'PENDING',
            created_by       TEXT    NOT NULL,
            approved_by      TEXT,
            created_at       TEXT    NOT NULL,
            approved_at      TEXT,
            active           INTEGER NOT NULL DEFAULT 0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_elr_source_type_val
            ON entity_learning_rules (source_id, pattern_type, pattern_value);
        CREATE INDEX IF NOT EXISTS idx_elr_source_id
            ON entity_learning_rules (source_id);
        CREATE INDEX IF NOT EXISTS idx_elr_source_status
            ON entity_learning_rules (source_id, approval_status);
        CREATE INDEX IF NOT EXISTS idx_elr_source_active
            ON entity_learning_rules (source_id, active);
    """)
    conn.commit()

    # table_relationships — first-class FK rows extracted from schema_snapshots.
    # Makes join paths, lineage, and impact analysis queryable without parsing JSON.
    # INSERT OR IGNORE + unique index provides idempotent re-extraction per snapshot.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS table_relationships (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            snapshot_id       INTEGER NOT NULL REFERENCES schema_snapshots(id) ON DELETE CASCADE,
            from_schema       TEXT    NOT NULL,
            from_table        TEXT    NOT NULL,
            from_table_fqn    TEXT    NOT NULL,
            from_column       TEXT    NOT NULL,
            to_schema         TEXT    NOT NULL,
            to_table          TEXT    NOT NULL,
            to_table_fqn      TEXT    NOT NULL,
            to_column         TEXT    NOT NULL,
            relationship_name TEXT,
            relationship_type TEXT    NOT NULL DEFAULT 'FOREIGN_KEY',
            confidence        REAL    NOT NULL DEFAULT 1.0,
            evidence_json     TEXT,
            created_at        TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_snapshot_uniq
            ON table_relationships (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);

        CREATE INDEX IF NOT EXISTS idx_tr_source_id
            ON table_relationships (source_id);

        CREATE INDEX IF NOT EXISTS idx_tr_from_table
            ON table_relationships (source_id, from_table_fqn);

        CREATE INDEX IF NOT EXISTS idx_tr_to_table
            ON table_relationships (source_id, to_table_fqn);
    """)
    conn.commit()

    # table_relationships — Relationship Intelligence extension (Program 3 Phase 1).
    # Existing declared-FK rows backfill via column DEFAULT, so persist_relationships()
    # (which does not list these columns in its INSERT) needs no changes.
    _tr_existing_cols = {
        row["name"]
        for row in cursor.execute("PRAGMA table_info(table_relationships)").fetchall()
    }
    _tr_migrations = [
        ("relationship_confidence", "ALTER TABLE table_relationships ADD COLUMN relationship_confidence INTEGER NOT NULL DEFAULT 100"),
        ("inference_method",        "ALTER TABLE table_relationships ADD COLUMN inference_method TEXT NOT NULL DEFAULT 'declared_fk'"),
        ("relationship_status",     "ALTER TABLE table_relationships ADD COLUMN relationship_status TEXT NOT NULL DEFAULT 'AUTO'"),
        ("cardinality",             "ALTER TABLE table_relationships ADD COLUMN cardinality TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("approved_by",             "ALTER TABLE table_relationships ADD COLUMN approved_by TEXT"),
        ("approved_at",             "ALTER TABLE table_relationships ADD COLUMN approved_at TEXT"),
    ]
    for _col_name, _ddl in _tr_migrations:
        if _col_name not in _tr_existing_cols:
            cursor.execute(_ddl)
    conn.commit()

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tr_source_status "
        "ON table_relationships (source_id, relationship_status)"
    )
    conn.commit()

    # -------------------------------------------------------------------------
    # View Semantic Discovery milestone — connect-time-discovered VIEW
    # definitions/dependencies, persisted so the live restriction-detection
    # probe in semantic_contract_service.py and candidate discovery in
    # semantic_retrieval_service.py don't need a live round-trip on every
    # question. One row per view per source; upserted on re-discovery.
    # -------------------------------------------------------------------------
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS view_semantic_evidence (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id               INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            snapshot_id             INTEGER NOT NULL REFERENCES schema_snapshots(id) ON DELETE CASCADE,
            table_fqn               TEXT    NOT NULL,
            definition_sql          TEXT,
            definition_checksum     TEXT,
            referenced_tables_json  TEXT,
            restriction_json        TEXT,
            naming_penalty_score    REAL    NOT NULL DEFAULT 0.0,
            is_curated              INTEGER NOT NULL DEFAULT 0,
            discovered_at           TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_vse_source_table_uniq
            ON view_semantic_evidence (source_id, table_fqn);

        CREATE INDEX IF NOT EXISTS idx_vse_source_curated
            ON view_semantic_evidence (source_id, is_curated);
    """)
    conn.commit()

    # view_semantic_evidence — Phase 7, Step 6E3B4, Part 2: additive columns
    # for the complete-definition capture fix. Existing rows (captured via
    # the OLD, truncating value-sampling path) backfill via column DEFAULT
    # to 'LEGACY_UNKNOWN' — an honest "captured before this fix existed,
    # completeness unverified" label, never silently assumed complete.
    # definition_length is always a cheap derived fact
    # (len(definition_sql)), persisted as a real column anyway so a caller
    # can filter/sort on it without loading/parsing definition_sql itself.
    # Mirrors table_relationships' own ALTER-guarded migration idiom
    # immediately above — purely additive, PRAGMA table_info-guarded, safe
    # to run against an already-populated table.
    _vse_existing_cols = {
        row["name"]
        for row in cursor.execute("PRAGMA table_info(view_semantic_evidence)").fetchall()
    }
    _vse_migrations = [
        ("definition_capture_status", "ALTER TABLE view_semantic_evidence ADD COLUMN definition_capture_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'"),
        ("definition_length",         "ALTER TABLE view_semantic_evidence ADD COLUMN definition_length INTEGER"),
    ]
    for _col_name, _ddl in _vse_migrations:
        if _col_name not in _vse_existing_cols:
            cursor.execute(_ddl)
    conn.commit()

    # -------------------------------------------------------------------------
    # Unified Governance Engine — Phase 1 Foundation
    # -------------------------------------------------------------------------

    # governance_approval_events: append-only audit trail for all governed objects.
    # Mirrors the engine_approval_events pattern but covers every governed type:
    #   dict.table, dict.column, domain.rule, domain.refinement,
    #   entity.rule, tool.engine, pii.confirmation
    # Rows are never updated or deleted.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS governance_approval_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            object_type_id TEXT    NOT NULL,
            object_id      TEXT    NOT NULL,
            event_type     TEXT    NOT NULL,
            from_state     TEXT,
            to_state       TEXT    NOT NULL,
            actor_id       TEXT    NOT NULL,
            notes          TEXT,
            source_service TEXT,
            created_at     TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gov_events_type_id
            ON governance_approval_events (object_type_id, object_id);
        CREATE INDEX IF NOT EXISTS idx_gov_events_actor
            ON governance_approval_events (actor_id);
        CREATE INDEX IF NOT EXISTS idx_gov_events_created_at
            ON governance_approval_events (created_at);
    """)
    conn.commit()

    # governance_state_map: current unified state projection for any governed object.
    # The source table is always authoritative; this projection enables fast
    # cross-type dashboard queries and review-queue aggregations without joining
    # seven domain-specific tables.
    # Upserted on every state transition by governance_service.upsert_governance_state().
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS governance_state_map (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            object_type_id   TEXT    NOT NULL,
            object_id        TEXT    NOT NULL,
            approval_state   TEXT    NOT NULL DEFAULT 'GENERATED',
            confidence_score REAL,
            confidence_tier  TEXT,
            reviewer_id      TEXT,
            reviewed_at      TEXT,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_gov_state_type_id
            ON governance_state_map (object_type_id, object_id);
        CREATE INDEX IF NOT EXISTS idx_gov_state_approval
            ON governance_state_map (approval_state);
        CREATE INDEX IF NOT EXISTS idx_gov_state_type_state
            ON governance_state_map (object_type_id, approval_state);
    """)
    conn.commit()

    # governance_policies: user-configurable auto-approval and escalation policies.
    # Evaluated in ascending priority order (lower number = evaluated first).
    # Hard-coded safety policies in governance_service.py always take precedence.
    # object_types_json: JSON array of GovernedObjectType ids; [] means all types.
    # condition_json: JSON object with matching criteria (confidence_min, domains, etc.).
    # action: REQUIRE_HUMAN | AUTO_APPROVE | ESCALATE | NO_ACTION
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS governance_policies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_name       TEXT    NOT NULL UNIQUE,
            enabled           INTEGER NOT NULL DEFAULT 1,
            priority          INTEGER NOT NULL DEFAULT 100,
            object_types_json TEXT    NOT NULL DEFAULT '[]',
            condition_json    TEXT    NOT NULL DEFAULT '{}',
            action            TEXT    NOT NULL,
            created_by        TEXT    NOT NULL DEFAULT 'system',
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gov_policies_enabled_priority
            ON governance_policies (enabled, priority);
    """)
    conn.commit()

    # Seed sensible enterprise default policies.
    # INSERT OR IGNORE ensures idempotency on repeated init_db() calls.
    _now_str = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    _policy_seeds = [
        # Domain / entity rules with ≥ 99% confidence are safe to auto-approve.
        (
            "POLICY_AUTO_APPROVE_VERY_HIGH_CONFIDENCE",
            1, 10,
            '["domain.rule","entity.rule","domain.refinement"]',
            '{"confidence_min": 0.99}',
            "AUTO_APPROVE",
        ),
        # Domain / entity rules with ≥ 95% confidence are auto-approve eligible.
        (
            "POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_RULES",
            1, 20,
            '["domain.rule","entity.rule"]',
            '{"confidence_min": 0.95}',
            "AUTO_APPROVE",
        ),
        # Milestone M-5, Part 4: dictionary entries with very strong,
        # already-computed evidence (domain + entity assignment confidence,
        # profiling classification confidence — see
        # governance_service._compute_dict_table_evidence_confidence /
        # _compute_dict_column_evidence_confidence) are auto-approve
        # eligible. Priority 30 — evaluated before the priority-50 catch-all
        # below, so only high-confidence dict.table/dict.column objects ever
        # reach AUTO_APPROVE; everything else still falls through to
        # REQUIRE_HUMAN unchanged. This policy alone is not sufficient for
        # auto-approval — data/dictionary_curation_service.py additionally
        # requires review-group "A" classification and no ambiguity with a
        # sibling candidate before ever acting on it.
        (
            "POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_DICTIONARY",
            1, 30,
            '["dict.table","dict.column"]',
            '{"confidence_min": 0.90}',
            "AUTO_APPROVE",
        ),
        # Milestone M-23 (Phase 6.5): domain/entity assignments with very
        # strong, already-computed rule-engine confidence (the assignment's
        # own `confidence` column — see governance_service._build_assignment_profile)
        # are auto-approve eligible. Priority 35 — evaluated after the dict
        # policy (30, a different object_type, no collision) and before the
        # priority-55 catch-all below. Live the reference database data clears this bar for only
        # a small minority of rows (a few percent for each of domain and entity), so this
        # is deliberately conservative, matching the dict policy's own shape.
        # Not sufficient alone — data/semantic_governance_rollout_service.py
        # additionally requires no ambiguous same-entity sibling and a
        # non-high-risk domain (the existing hard policy, unchanged) before
        # ever acting on it.
        (
            "POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_ASSIGNMENTS",
            1, 35,
            '["domain.assignment","entity.assignment"]',
            '{"confidence_min": 0.90}',
            "AUTO_APPROVE",
        ),
        # Phase 2: concept-to-table mappings with very strong stored
        # confidence are auto-approve eligible. Priority 40 — after the
        # assignment policy (35) and before the priority-50 catch-alls.
        # Not sufficient alone in practice: this phase's only implemented
        # write path is the human clarification-resume fast path
        # (concept_mapping_service.promote_clarification_selection), which
        # writes governance_state='HUMAN_APPROVED' directly since a human
        # already decided — an auto_governance batch curator that would
        # actually exercise this policy is deliberately not built yet (see
        # concept_mapping_service.py's own module docstring).
        (
            "POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_CONCEPT_MAPPING",
            1, 40,
            '["concept.mapping"]',
            '{"confidence_min": 0.90}',
            "AUTO_APPROVE",
        ),
        # Dictionary entries (business names, labels) always require a human
        # since they carry direct business meaning that needs domain expert sign-off.
        (
            "POLICY_REQUIRE_HUMAN_DICT_ENTRIES",
            1, 50,
            '["dict.table","dict.column"]',
            '{}',
            "REQUIRE_HUMAN",
        ),
        # Milestone M-23: explainability catch-all for domain/entity
        # assignments that don't clear the 0.90 confidence bar above — these
        # are already review_required=True by the policy engine's default
        # branch with or without this row, but this gives `matched_policy` a
        # real name instead of None, which the maturity classifier
        # (data/semantic_governance_rollout_service.py) surfaces as the
        # asset's "exact reason." Priority 55 — deliberately after the
        # existing priority-50 catch-alls so it never intercepts them.
        (
            "POLICY_REQUIRE_HUMAN_ASSIGNMENTS",
            1, 55,
            '["domain.assignment","entity.assignment"]',
            '{}',
            "REQUIRE_HUMAN",
        ),
        # Engine tools always require explicit human approval before execution.
        (
            "POLICY_REQUIRE_HUMAN_ENGINE_TOOLS",
            1, 50,
            '["tool.engine"]',
            '{}',
            "REQUIRE_HUMAN",
        ),
    ]
    for _seed in _policy_seeds:
        cursor.execute(
            """INSERT OR IGNORE INTO governance_policies
                   (policy_name, enabled, priority,
                    object_types_json, condition_json, action,
                    created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'system', ?, ?)""",
            (*_seed, _now_str, _now_str),
        )
    conn.commit()

    # governance_bulk_ops: immutable record of every bulk approve / reject run.
    # blocked_items_json: JSON array of {object_id, object_type_id, blocking_policy, reason}.
    # status:  COMPLETED | UNDONE
    # undone_at / undone_by: set if the operation was reversed (Phase 4).
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS governance_bulk_ops (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id           TEXT    NOT NULL,
            action             TEXT    NOT NULL,
            filter_json        TEXT    NOT NULL,
            affected_count     INTEGER NOT NULL DEFAULT 0,
            blocked_count      INTEGER NOT NULL DEFAULT 0,
            blocked_items_json TEXT    NOT NULL DEFAULT '[]',
            status             TEXT    NOT NULL DEFAULT 'COMPLETED',
            executed_at        TEXT    NOT NULL,
            undone_at          TEXT,
            undone_by          TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_gov_bulk_actor
            ON governance_bulk_ops (actor_id);
        CREATE INDEX IF NOT EXISTS idx_gov_bulk_executed_at
            ON governance_bulk_ops (executed_at);
        CREATE INDEX IF NOT EXISTS idx_gov_bulk_action
            ON governance_bulk_ops (action);
    """)
    conn.commit()

    # governance_assignments: stewardship work items — one row per governed object
    # assignment.  Multiple assignments can exist for the same object (e.g., assigned
    # to different stewards at different times).
    # status:  OPEN | COMPLETED
    # priority: CRITICAL | HIGH | MEDIUM | LOW  (auto-calculated from governance profile)
    # due_date: ISO date (YYYY-MM-DD), computed from SLA threshold if not provided.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS governance_assignments (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            object_type      TEXT    NOT NULL,
            object_id        TEXT    NOT NULL,
            source_id        INTEGER,
            assigned_to      TEXT    NOT NULL,
            assigned_by      TEXT    NOT NULL,
            assignment_group TEXT,
            priority         TEXT    NOT NULL DEFAULT 'MEDIUM',
            status           TEXT    NOT NULL DEFAULT 'OPEN',
            due_date         TEXT,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL,
            completed_at     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ga_assigned_to
            ON governance_assignments (assigned_to);
        CREATE INDEX IF NOT EXISTS idx_ga_status
            ON governance_assignments (status);
        CREATE INDEX IF NOT EXISTS idx_ga_priority
            ON governance_assignments (priority);
        CREATE INDEX IF NOT EXISTS idx_ga_source_id
            ON governance_assignments (source_id);
        CREATE INDEX IF NOT EXISTS idx_ga_assigned_to_status
            ON governance_assignments (assigned_to, status);
        CREATE INDEX IF NOT EXISTS idx_ga_object
            ON governance_assignments (object_type, object_id);
    """)
    conn.commit()

    # query_execution_log — immutable audit record for every AI-generated query execution.
    # SQL is never stored; only its SHA-256 hash. Parameter values and row values are
    # never stored.  user_id ownership check enforced in service layer.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS query_execution_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id         TEXT    NOT NULL,
            user_id              TEXT    NOT NULL,
            source_id            INTEGER NOT NULL,
            sql_hash             TEXT,
            tables_accessed_json TEXT,
            param_count          INTEGER NOT NULL DEFAULT 0,
            row_count            INTEGER NOT NULL DEFAULT 0,
            truncated            INTEGER NOT NULL DEFAULT 0,
            duration_ms          INTEGER NOT NULL DEFAULT 0,
            status               TEXT    NOT NULL,
            error_code           TEXT,
            executed_at          TEXT    NOT NULL,
            created_at           TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_qel_execution_id ON query_execution_log(execution_id);
        CREATE INDEX IF NOT EXISTS idx_qel_user_id       ON query_execution_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_qel_source_id     ON query_execution_log(source_id);
        CREATE INDEX IF NOT EXISTS idx_qel_status        ON query_execution_log(status);
        CREATE INDEX IF NOT EXISTS idx_qel_executed_at   ON query_execution_log(executed_at);
    """)
    conn.commit()

    # Phase 1 Stabilization, Task 3 — discriminates a real user-visible
    # execution from the agent's own internal investigation probes
    # (data.investigation_service), which write to this same table for
    # audit/analytics but must not count toward the per-user rate limit.
    existing_qel_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(query_execution_log)").fetchall()
    }
    if "execution_kind" not in existing_qel_columns:
        cursor.execute(
            "ALTER TABLE query_execution_log ADD COLUMN execution_kind "
            "TEXT NOT NULL DEFAULT 'user_query'"
        )
    # Reconnect Reliability milestone, Part 14 — pipeline observability trace.
    # Nullable, additive: existing rows and every pre-existing caller of
    # log_query_execution() that doesn't pass pipeline_trace are unaffected.
    # Stores an encrypted JSON blob (core.security.encryption.encrypt, same
    # pattern data/audit.py already uses) — the trace itself
    # (core.observability.pipeline_trace.build_pipeline_trace) never
    # includes raw SQL text/parameter values/row data, but is still
    # encrypted at rest since it can reveal table/column names.
    if "pipeline_trace_encrypted" not in existing_qel_columns:
        cursor.execute(
            "ALTER TABLE query_execution_log ADD COLUMN pipeline_trace_encrypted TEXT"
        )
    conn.commit()

    # ai_semantic_suggestions — pending review queue for AI-generated dictionary suggestions.
    # AI never writes directly to data_dictionary_columns; all output lands here first.
    # status: PENDING | ACCEPTED | REJECTED
    # Deduplication key: (source_id, object_type, table_fqn, column_name, status=PENDING)
    # enforced in the service layer (not by a DB unique index) so re-runs are safe.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS ai_semantic_suggestions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id               INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            object_type             TEXT    NOT NULL DEFAULT 'dict.column',
            table_fqn               TEXT    NOT NULL,
            column_name             TEXT    NOT NULL,
            suggested_business_name TEXT,
            suggested_description   TEXT,
            suggested_domain        TEXT,
            suggested_entity        TEXT,
            ai_confidence           REAL,
            ai_reasoning_json       TEXT    NOT NULL DEFAULT '[]',
            review_required         INTEGER NOT NULL DEFAULT 1,
            status                  TEXT    NOT NULL DEFAULT 'PENDING',
            provider                TEXT,
            model                   TEXT,
            prompt_version          TEXT,
            created_by              TEXT,
            created_at              TEXT    NOT NULL,
            reviewed_by             TEXT,
            reviewed_at             TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ais_source_status
            ON ai_semantic_suggestions (source_id, status);
        CREATE INDEX IF NOT EXISTS idx_ais_source_fqn_col
            ON ai_semantic_suggestions (source_id, table_fqn, column_name);
        CREATE INDEX IF NOT EXISTS idx_ais_created_at
            ON ai_semantic_suggestions (created_at);
    """)
    conn.commit()

    # Idempotent migration: assignment_source distinguishes machine-generated
    # domain/entity assignments from human-locked ones. Rows with
    # assignment_source='human' are protected from overwrite by the
    # autonomous metadata lifecycle (Phase 11).
    da_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(domain_assignments)").fetchall()
    }
    if "assignment_source" not in da_existing:
        cursor.execute(
            "ALTER TABLE domain_assignments ADD COLUMN assignment_source "
            "TEXT NOT NULL DEFAULT 'rule'"
        )
    conn.commit()

    ea_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(entity_assignments)").fetchall()
    }
    if "assignment_source" not in ea_existing:
        cursor.execute(
            "ALTER TABLE entity_assignments ADD COLUMN assignment_source "
            "TEXT NOT NULL DEFAULT 'rule'"
        )
    conn.commit()

    # metadata_lifecycle_runs — one row per autonomous metadata lifecycle run
    # (Phase 11). Records the diff summary, refresh counts, review tasks, and
    # notifications produced by a single trigger event, for audit/history.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS metadata_lifecycle_runs (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                   INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            job_id                      INTEGER REFERENCES metadata_jobs(id) ON DELETE SET NULL,
            user_id                     TEXT    NOT NULL,
            trigger_event               TEXT    NOT NULL,
            status                      TEXT    NOT NULL DEFAULT 'RUNNING',
            old_snapshot_id             INTEGER REFERENCES schema_snapshots(id),
            new_snapshot_id             INTEGER REFERENCES schema_snapshots(id),
            tables_added_count          INTEGER NOT NULL DEFAULT 0,
            tables_removed_count        INTEGER NOT NULL DEFAULT 0,
            tables_modified_count       INTEGER NOT NULL DEFAULT 0,
            objects_changed_count       INTEGER NOT NULL DEFAULT 0,
            dictionary_refreshed_count  INTEGER NOT NULL DEFAULT 0,
            domains_refreshed_count     INTEGER NOT NULL DEFAULT 0,
            entities_refreshed_count    INTEGER NOT NULL DEFAULT 0,
            review_tasks_created_count  INTEGER NOT NULL DEFAULT 0,
            notifications_sent_count    INTEGER NOT NULL DEFAULT 0,
            steps_executed_json         TEXT    NOT NULL DEFAULT '[]',
            error_message               TEXT,
            duration_ms                 INTEGER,
            started_at                  TEXT    NOT NULL,
            completed_at                TEXT,
            created_at                  TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mlr_source_id ON metadata_lifecycle_runs (source_id);
        CREATE INDEX IF NOT EXISTS idx_mlr_job_id    ON metadata_lifecycle_runs (job_id);
        CREATE INDEX IF NOT EXISTS idx_mlr_status    ON metadata_lifecycle_runs (status);
    """)
    conn.commit()

    # Idempotent migration for the Part 16 duplicate-run guard
    # (data.lifecycle_service.find_duplicate_lifecycle_run). old/new_snapshot_id
    # alone identify the SCHEMA snapshot pair, which is not fine-grained
    # enough on its own — two runs can share an unchanged schema pair while
    # differing in PROFILING evidence (see
    # tests/test_lifecycle_profiling_trigger.py::
    # test_idempotent_no_duplicate_vocabulary_rows). These two extra columns
    # let the guard key on both pairs together.
    mlr_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(metadata_lifecycle_runs)").fetchall()
    }
    mlr_migrations = [
        ("prof_old_snapshot_id",
         "ALTER TABLE metadata_lifecycle_runs ADD COLUMN prof_old_snapshot_id INTEGER"),
        ("prof_new_snapshot_id",
         "ALTER TABLE metadata_lifecycle_runs ADD COLUMN prof_new_snapshot_id INTEGER"),
    ]
    for column, statement in mlr_migrations:
        if column not in mlr_existing:
            cursor.execute(statement)
    conn.commit()

    # SQL rebuild Phase 1, Step 5 — sql_retrieval_builds / sql_retrieval_documents:
    # transactional, versioned persistence for core.sqlservice's retrieval-document
    # builder (core/sqlservice/retrieval_documents.py) and its bulk metadata loader
    # (core/sqlservice/metadata_loader.py). One build row per persisted attempt,
    # keyed on the structural schema fingerprint (data/schema_fingerprint_service.py)
    # plus a deterministic document_set_hash, so an unchanged source reuses its
    # existing READY build instead of duplicating rows (see
    # core/sqlservice/retrieval_store.py). error_message exists for forward
    # compatibility with a future non-transactional/async builder that might record
    # a FAILED row directly — the synchronous persist_retrieval_build() in this step
    # always rolls back to zero new rows on failure instead, so it never writes
    # status='FAILED' itself. No embeddings column here by design — that belongs to
    # the later vector-store step.
    cursor.executescript("""
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
        -- Step 5A: UNIQUE, not just an index — this is the database-enforced
        -- idempotency/concurrency guarantee. Two racing callers persisting the
        -- same logical build (same source, fingerprint_version,
        -- schema_fingerprint, document_set_hash) cannot both succeed: SQLite
        -- serializes writers, so the loser's INSERT raises IntegrityError,
        -- which core.sqlservice.retrieval_store.persist_retrieval_build()
        -- catches and turns into a REUSED result pointing at the winner's
        -- build (see that function's docstring for the full sequence).
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
    """)
    conn.commit()

    # SQL rebuild Phase 2, Step 1 — sql_retrieval_embeddings: a
    # content-addressed embedding cache for core.sqlservice's retrieval
    # documents (core/sqlservice/embedding_store.py). Keyed on
    # (embedding_provider, embedding_model, embedding_dimensions,
    # document_hash) — deliberately NOT build_id or document id — so an
    # unchanged document (same document_hash) reuses its cached vector
    # across every retrieval build and source that produces that hash,
    # instead of recomputing or duplicating it. Vectors are stored as
    # compact float32 little-endian binary (vector_blob), never JSON. This
    # step never calls OpenAI, never generates a real embedding, and never
    # performs vector similarity search — it only creates local, empty
    # storage plus the validated read/write functions in
    # core/sqlservice/embedding_store.py.
    cursor.executescript("""
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

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sre_provider_model_dims_hash
            ON sql_retrieval_embeddings (embedding_provider, embedding_model, embedding_dimensions, document_hash);
        CREATE INDEX IF NOT EXISTS idx_sre_hash
            ON sql_retrieval_embeddings (document_hash);
    """)
    conn.commit()

    # SQL rebuild Phase 5, Step 9 — sql_examples / sql_example_embeddings:
    # automatically-trusted (core/sqlservice/sql_example_model.py's
    # classify_trust_level -- NO manual approval workflow) natural-
    # language-question/SQL-template pairs plus their content-addressed
    # search-document embedding cache. See migrations/014_create_sql_examples.sql
    # for the full design rationale (identical schema, kept in sync here).
    cursor.executescript("""
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
    """)
    conn.commit()

    # SQL Architecture Correction, Phase 6, Step 4B — sql_semantic_enrichments:
    # structurally validated, model-inferred object/column semantics
    # (core/sqlservice/semantic_enrichment_service.py's EnrichedObjectSemantics
    # + EnrichedColumnSemantics, never arbitrary raw provider JSON), persisted
    # by per-object semantic-input hash. NO manual approval workflow. See
    # migrations/015_create_semantic_enrichment.sql for the full design
    # rationale (identical schema, kept in sync here) and
    # migrations/016_add_semantic_enrichment_phase.sql (Phase 6 Step 4C2) for
    # the `phase` column added below — a fresh database created via
    # init_db() already has both migrations' end-state schema in ONE
    # CREATE TABLE, so it never needs migration 016's ALTER TABLE applied.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS sql_semantic_enrichments (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id            INTEGER NOT NULL REFERENCES data_source_connections(id) ON DELETE CASCADE,
            object_fqn           TEXT    NOT NULL CHECK (length(object_fqn) > 0),
            semantic_input_hash  TEXT    NOT NULL CHECK (length(semantic_input_hash) = 64),
            provider_id          TEXT    NOT NULL CHECK (length(provider_id) > 0),
            model                TEXT    NOT NULL CHECK (length(model) > 0),
            contract_version     INTEGER NOT NULL CHECK (contract_version > 0),
            phase                TEXT    NOT NULL CHECK (phase IN ('OBJECT_CORE','COLUMN_DEFERRED')) DEFAULT 'COLUMN_DEFERRED',
            payload_json         TEXT    NOT NULL,
            status               TEXT    NOT NULL CHECK (status IN ('ACTIVE','INVALID')) DEFAULT 'ACTIVE',
            created_at           TEXT    NOT NULL,
            updated_at           TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sse_cache_key
            ON sql_semantic_enrichments (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase);
        CREATE INDEX IF NOT EXISTS idx_sse_source_current
            ON sql_semantic_enrichments (source_id, status, provider_id, model, contract_version, phase);
        CREATE INDEX IF NOT EXISTS idx_sse_source_object
            ON sql_semantic_enrichments (source_id, object_fqn);
    """)
    conn.commit()

    # Phase 6 Step 4C2A: handles the ONE case the CREATE TABLE IF NOT EXISTS
    # above cannot -- an existing, pre-Step-4C2 sql_semantic_enrichments
    # table that predates the `phase` column. See this function's own
    # docstring for why this can't just be more idempotent raw SQL.
    ensure_sql_semantic_enrichments_phase_migration(conn)

    # Phase 6 Step 6C1: a brand-new, dedicated table for deep VIEW_ENRICHED
    # semantics -- NOT an extension of sql_semantic_enrichments (Step 6C's
    # original design widened that table's `phase` CHECK via a table
    # rebuild; Step 6C1's audit replaced it with this safer, purely
    # additive table so the live, already-populated sql_semantic_enrichments
    # table (hundreds of real OBJECT_CORE rows as of this audit) is never rebuilt,
    # renamed, or otherwise touched by view-enrichment work at all). See
    # ensure_sql_view_semantic_enrichments_table's own docstring.
    ensure_sql_view_semantic_enrichments_table(conn)

    # Phase 7 Step 6E3B: persisted, deterministic per-view structural/
    # lineage/suitability profile -- purely additive, see
    # ensure_sql_view_profiles_table's own docstring for the rollback
    # procedure and exactly what this does/does not touch.
    ensure_sql_view_profiles_table(conn)

    # Phase 7, Step 3J: persistent, source-scoped business-knowledge package
    # (concepts/relationships/facts) plus its AI-proposed/verified schema
    # mappings -- see ensure_business_knowledge_tables's own docstring.
    ensure_business_knowledge_tables(conn)

    conn.close()
