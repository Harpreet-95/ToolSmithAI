import json
import logging
import os
import pathlib
import tempfile
from datetime import datetime, timezone

from core.security.encryption import encrypt
from data.db import get_connection

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = pathlib.Path(__file__).parent / "audit.log"


def log_audit_event(task_result: dict, user_id: str | None = None) -> None:
    task_type = (
        task_result.get("task_type")
        or "execution"
    )
    original_input = (
        task_result.get("original_input")
        or task_result.get("intent")
        or "N/A"
    )
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "original_input": encrypt(original_input),
        "status": task_result.get("status") or "unknown",
        "user_id": user_id,
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        logger.error("Failed to write audit log: %s", e)

    conn = None
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO audit_logs (timestamp, task_type, original_input, status, user_id) VALUES (?, ?, ?, ?, ?)",
            (record["timestamp"], record["task_type"], record["original_input"], record["status"], record["user_id"]),
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to write audit log to database: %s", e)
    finally:
        if conn:
            conn.close()


def delete_audit_log_entries(user_id: str) -> int:
    """Remove lines owned by user_id from audit.log. Returns count removed.

    Lines with no user_id field (legacy) and lines owned by other users are
    preserved. Returns 0 without raising if the file does not exist or any
    I/O step fails — DB deletions are committed before this is called.
    """
    if not AUDIT_LOG_PATH.exists():
        return 0

    keep: list[str] = []
    deleted = 0

    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    keep.append(line)
                    continue
                if entry.get("user_id") == user_id:
                    deleted += 1
                else:
                    keep.append(line)
    except OSError as e:
        logger.error("Failed to read audit log for deletion: %s", e)
        return 0

    tmp_fd, tmp_path = tempfile.mkstemp(dir=AUDIT_LOG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            for line in keep:
                tmp_f.write(line + "\n")
        os.replace(tmp_path, AUDIT_LOG_PATH)
    except OSError as e:
        logger.error("Failed to rewrite audit log: %s", e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return 0

    return deleted


def purge_old_audit_db(cutoff: str) -> int:
    """Delete audit_logs rows whose timestamp is before cutoff.

    cutoff must be an ISO 8601 string (e.g. '2026-02-03T14:00:00+00:00').
    Returns count of deleted rows.
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.execute(
            "DELETE FROM audit_logs WHERE timestamp < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception as e:
        logger.error("Failed to purge old audit_logs rows: %s", e)
        return 0
    finally:
        if conn:
            conn.close()


def purge_old_audit_log_file(cutoff: str) -> int:
    """Remove lines from audit.log whose timestamp is before cutoff.

    Lines with no parseable timestamp are preserved. Returns 0 without
    raising if the file does not exist or any I/O step fails.
    """
    if not AUDIT_LOG_PATH.exists():
        return 0

    keep: list[str] = []
    deleted = 0

    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    keep.append(line)
                    continue
                ts = entry.get("timestamp", "")
                if ts and ts < cutoff:
                    deleted += 1
                else:
                    keep.append(line)
    except OSError as e:
        logger.error("Failed to read audit log for purge: %s", e)
        return 0

    tmp_fd, tmp_path = tempfile.mkstemp(dir=AUDIT_LOG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            for line in keep:
                tmp_f.write(line + "\n")
        os.replace(tmp_path, AUDIT_LOG_PATH)
    except OSError as e:
        logger.error("Failed to rewrite audit log after purge: %s", e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return 0

    return deleted
