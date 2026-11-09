import json
import logging
import os
import pathlib
import tempfile
from datetime import datetime, timezone

from core.config import AUDIT_LOG_DIR, AUDIT_LOG_MAX_BYTES, TOOLSMITH_DATA_DIR
from core.security.encryption import encrypt
from data.db import get_connection

logger = logging.getLogger(__name__)


def _resolve_audit_log_path(audit_log_dir: str, data_dir: str) -> pathlib.Path:
    """Precedence: AUDIT_LOG_DIR (audit-log-specific override) >
    TOOLSMITH_DATA_DIR (general runtime-data override) > historical default
    next to this file. A pure function of its arguments so path resolution
    is unit-testable without reloading this module."""
    if audit_log_dir:
        base = pathlib.Path(audit_log_dir)
    elif data_dir:
        base = pathlib.Path(data_dir)
    else:
        base = pathlib.Path(__file__).parent
    return base / "audit.log"


AUDIT_LOG_PATH = _resolve_audit_log_path(AUDIT_LOG_DIR, TOOLSMITH_DATA_DIR)


def _rotate_if_oversized() -> None:
    """Archive audit.log to a timestamped file once it reaches
    AUDIT_LOG_MAX_BYTES, before it grows large enough to risk write failures
    on cloud-synced filesystems — observed: a 131 MB audit.log inside a
    OneDrive-synced project directory reliably failed open+close with
    OSError [Errno 22] Invalid argument (reproduced in both text and binary
    mode; a fresh/small file at the same path never failed), because nothing
    ever rotated it. Best-effort: rotation failure must not block the write
    it guards, matching this module's existing non-fatal error handling.
    """
    try:
        if AUDIT_LOG_PATH.exists() and AUDIT_LOG_PATH.stat().st_size >= AUDIT_LOG_MAX_BYTES:
            archive_path = AUDIT_LOG_PATH.with_name(
                f"audit.log.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
            )
            os.replace(AUDIT_LOG_PATH, archive_path)
    except OSError as e:
        logger.error("Failed to rotate audit log: %s", e)


def log_audit_event(task_result: dict, user_id: str | None = None) -> None:
    _rotate_if_oversized()
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
