import json
import logging
import pathlib
from datetime import datetime, timezone

from core.security.encryption import encrypt
from data.db import get_connection

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = pathlib.Path(__file__).parent / "audit.log"


def log_audit_event(task_result: dict) -> None:
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
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        logger.error("Failed to write audit log: %s", e)

    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO audit_logs (timestamp, task_type, original_input, status) VALUES (?, ?, ?, ?)",
            (record["timestamp"], record["task_type"], record["original_input"], record["status"]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to write audit log to database: %s", e)
