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

    conn.close()
