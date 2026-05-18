import json
from datetime import datetime, timezone

from data.db import get_connection


def create_dataset_summary(
    user_id: str,
    filename: str,
    row_count: int,
    column_count: int,
    columns: list,
    numeric_profile: dict,
    missing_values: dict,
    categorical_profile: dict,
    date_profile: dict | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO datasets
              (user_id, filename, uploaded_at, row_count, column_count,
               columns_json, numeric_profile_json, missing_values_json,
               categorical_profile_json, date_profile_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                filename,
                now,
                row_count,
                column_count,
                json.dumps(columns),
                json.dumps(numeric_profile),
                json.dumps(missing_values),
                json.dumps(categorical_profile),
                json.dumps(date_profile) if date_profile is not None else None,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_latest_dataset_for_user(user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_dataset_by_id(dataset_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_datasets_for_user(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, filename, uploaded_at, row_count, column_count"
            " FROM datasets WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rename_dataset(dataset_id: int, user_id: str, new_filename: str) -> bool:
    """Rename a dataset's display filename. Returns True if updated, False if not found or not owned."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE datasets SET filename = ? WHERE id = ? AND user_id = ?",
            (new_filename, dataset_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_dataset(dataset_id: int, user_id: str) -> bool:
    """Delete a dataset owned by user_id. Returns True if deleted, False if not found or not owned."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_user_email(user_id: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return row["email"] if row and row["email"] else None
    finally:
        conn.close()
