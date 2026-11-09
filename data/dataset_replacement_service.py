from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_replacement_record(
    dataset_id: int,
    user_id: str,
    old_file_path: str | None,
    new_file_path: str,
    original_filename: str,
) -> int:
    """Insert a pending replacement record. Returns the new record id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO dataset_source_replacements
              (dataset_id, user_id, old_file_path, new_file_path,
               original_filename, status, replaced_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (dataset_id, user_id, old_file_path, new_file_path,
             original_filename, _now()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def complete_replacement(record_id: int) -> None:
    """Mark a replacement record as succeeded."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dataset_source_replacements SET status = 'success' WHERE id = ?",
            (record_id,),
        )
        conn.commit()
    finally:
        conn.close()


def fail_replacement(record_id: int, error: str) -> None:
    """Mark a replacement record as failed and store the error reason."""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE dataset_source_replacements
               SET status = 'failed', error = ?
               WHERE id = ?""",
            (str(error)[:1000], record_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_replacements_for_dataset(
    dataset_id: int,
    user_id: str,
    limit: int = 25,
) -> list[dict]:
    """Return replacement history for one dataset, newest first, ownership enforced."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, dataset_id, user_id, old_file_path, new_file_path,
                   original_filename, status, error, replaced_at
            FROM dataset_source_replacements
            WHERE dataset_id = ? AND user_id = ?
            ORDER BY replaced_at DESC
            LIMIT ?
            """,
            (dataset_id, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
