import json
import logging
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_snapshot(report_content: dict) -> dict:
    """Extract lightweight aggregated metrics from a report for historical storage.

    Never stores the full report, raw dataset rows, or unaggregated values.
    Unknown section types are silently skipped.
    All fields default to None or empty counts when absent or malformed.
    """
    snapshot: dict = {
        "report_version":                    report_content.get("version"),
        "row_count":                         None,
        "column_count":                      None,
        "readiness_score":                   None,
        "readiness_level":                   None,
        "kpi_values":                        {},
        "anomaly_counts_by_severity":        {"high": 0, "medium": 0, "low": 0},
        "recommendation_counts_by_priority": {"high": 0, "medium": 0, "low": 0},
        "trend_counts_by_direction":         {"up": 0, "down": 0, "stable": 0, "volatile": 0},
        "created_at":                        _now(),
    }

    for section in report_content.get("sections", []):
        sec_type = section.get("type", "text")
        try:
            if sec_type == "kpi":
                for kpi in section.get("kpis", []):
                    label = kpi.get("label", "")
                    value = kpi.get("value")
                    if label and value is not None:
                        snapshot["kpi_values"][label] = value
                    if label == "Total Records" and value is not None:
                        snapshot["row_count"] = value
                    if label == "Total Features" and value is not None:
                        snapshot["column_count"] = value

            elif sec_type == "predictive_readiness":
                snapshot["readiness_score"] = section.get("readiness_score")
                snapshot["readiness_level"] = section.get("readiness_level")

            elif sec_type == "anomaly":
                counts: dict = {"high": 0, "medium": 0, "low": 0}
                for a in section.get("anomalies", []):
                    sev = str(a.get("severity", "")).lower()
                    if sev in counts:
                        counts[sev] += 1
                snapshot["anomaly_counts_by_severity"] = counts

            elif sec_type == "recommendation":
                counts = {"high": 0, "medium": 0, "low": 0}
                for r in section.get("recommendations", []):
                    pri = str(r.get("priority", "")).lower()
                    if pri in counts:
                        counts[pri] += 1
                snapshot["recommendation_counts_by_priority"] = counts

            elif sec_type == "trend":
                counts = {"up": 0, "down": 0, "stable": 0, "volatile": 0}
                for t in section.get("trends", []):
                    d = str(t.get("direction", "")).lower()
                    if d in counts:
                        counts[d] += 1
                snapshot["trend_counts_by_direction"] = counts

        except Exception:
            continue

    return snapshot


def save_report_metric_snapshot(
    user_id: str,
    report_id: int | None,
    dataset_id: int | None,
    task_type: str,
    report_content: dict,
) -> int:
    """Extract lightweight metrics from report_content and persist a snapshot row.

    Fails safely — callers must wrap in try/except so a snapshot failure never
    prevents the parent report from being returned to the user.
    Returns the new snapshot id.
    """
    snapshot = _extract_snapshot(report_content)
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO report_metric_snapshots
              (user_id, report_id, dataset_id, task_type, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                report_id,
                dataset_id,
                task_type,
                json.dumps(snapshot),
                now,
            ),
        )
        conn.commit()
        logger.debug(
            "[snapshot] saved id=%d user=%s report=%s dataset=%s",
            cursor.lastrowid, user_id, report_id, dataset_id,
        )
        return cursor.lastrowid
    finally:
        conn.close()


def get_previous_snapshot_for_dataset(
    user_id: str,
    dataset_id: int,
    before_created_at: str | None = None,
) -> dict | None:
    """Return the most recent snapshot for a dataset, optionally before a timestamp.

    If before_created_at is provided, only snapshots strictly older than that
    timestamp are considered — this is the timing-safety guard that prevents a
    report from comparing itself against its own snapshot.
    Returns None if no qualifying snapshot exists.
    Ownership is enforced; only the owning user's rows are returned.
    """
    conn = get_connection()
    try:
        if before_created_at:
            row = conn.execute(
                """
                SELECT id, user_id, report_id, dataset_id, task_type,
                       snapshot_json, created_at
                FROM report_metric_snapshots
                WHERE user_id = ? AND dataset_id = ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, dataset_id, before_created_at),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, user_id, report_id, dataset_id, task_type,
                       snapshot_json, created_at
                FROM report_metric_snapshots
                WHERE user_id = ? AND dataset_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, dataset_id),
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        try:
            rec["snapshot"] = json.loads(rec.pop("snapshot_json"))
        except Exception:
            rec["snapshot"] = {}
        return rec
    finally:
        conn.close()


def get_snapshot_baseline_for_dataset(
    user_id: str,
    dataset_id: int,
    limit: int = 10,
) -> list[dict]:
    """Return up to `limit` recent snapshots, ordered oldest → newest.

    Malformed snapshot_json entries are excluded so callers can safely
    iterate without additional error handling.
    Ownership enforced via user_id filter.
    Used by drift detection to compute rolling baseline averages.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, report_id, dataset_id, task_type,
                   snapshot_json, created_at
            FROM report_metric_snapshots
            WHERE user_id = ? AND dataset_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, dataset_id, max(1, limit)),
        ).fetchall()
        result = []
        for row in rows:
            rec = dict(row)
            try:
                rec["snapshot"] = json.loads(rec.pop("snapshot_json"))
                result.append(rec)
            except Exception:
                continue
        result.reverse()  # oldest → newest
        return result
    finally:
        conn.close()


def list_snapshots_for_dataset(
    user_id: str,
    dataset_id: int,
    limit: int = 20,
) -> list[dict]:
    """Return up to `limit` snapshots for one dataset, newest first.

    Enforces user ownership — only rows whose user_id matches are returned.
    snapshot_json is decoded back to a dict in each row.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, report_id, dataset_id, task_type,
                   snapshot_json, created_at
            FROM report_metric_snapshots
            WHERE user_id = ? AND dataset_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, dataset_id, max(1, limit)),
        ).fetchall()
        result = []
        for row in rows:
            rec = dict(row)
            try:
                rec["snapshot"] = json.loads(rec.pop("snapshot_json"))
            except Exception:
                rec["snapshot"] = {}
            result.append(rec)
        return result
    finally:
        conn.close()


def list_recent_snapshots_for_user(
    user_id: str,
    limit: int = 50,
) -> list[dict]:
    """Return up to `limit` recent snapshots for a user across all datasets.

    Enforces user ownership. snapshot_json is decoded back to a dict.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, report_id, dataset_id, task_type,
                   snapshot_json, created_at
            FROM report_metric_snapshots
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, max(1, limit)),
        ).fetchall()
        result = []
        for row in rows:
            rec = dict(row)
            try:
                rec["snapshot"] = json.loads(rec.pop("snapshot_json"))
            except Exception:
                rec["snapshot"] = {}
            result.append(rec)
        return result
    finally:
        conn.close()
