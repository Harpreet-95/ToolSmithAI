from data.db import get_connection


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
        # Dictionary entries (business names, labels) always require a human
        # since they carry direct business meaning that needs domain expert sign-off.
        (
            "POLICY_REQUIRE_HUMAN_DICT_ENTRIES",
            1, 50,
            '["dict.table","dict.column"]',
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

    conn.close()
