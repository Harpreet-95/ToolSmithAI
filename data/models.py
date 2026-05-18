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
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name   TEXT NOT NULL,
            config TEXT NOT NULL DEFAULT '{}'
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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            dataset_id  INTEGER,
            input_text  TEXT    NOT NULL,
            task_type   TEXT    NOT NULL,
            frequency   TEXT    NOT NULL,
            day_of_week TEXT,
            next_run_at TEXT    NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            last_run_at TEXT,
            last_status TEXT,
            last_error  TEXT,
            run_count   INTEGER NOT NULL DEFAULT 0
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
        ("last_run_at", "ALTER TABLE scheduled_workflows ADD COLUMN last_run_at TEXT"),
        ("last_status",  "ALTER TABLE scheduled_workflows ADD COLUMN last_status TEXT"),
        ("last_error",   "ALTER TABLE scheduled_workflows ADD COLUMN last_error TEXT"),
        ("run_count",    "ALTER TABLE scheduled_workflows ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, stmt in sw_migrations:
        if col not in sw_existing:
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

    # Idempotent migration: add date_profile_json to datasets.
    # Rows uploaded before this migration will have NULL; the report generator
    # treats NULL as "no date analysis available" and skips the new sections.
    ds_existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(datasets)").fetchall()
    }
    if "date_profile_json" not in ds_existing:
        cursor.execute("ALTER TABLE datasets ADD COLUMN date_profile_json TEXT")
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

    conn.close()
