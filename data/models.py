from data.db import get_connection


def init_db() -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
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
    """)
    conn.commit()
    conn.close()
