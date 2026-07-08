import json
import logging
from datetime import datetime, timezone

import core.connectors.registry as registry
from core.connectors.base import DataSourceConfig
from core.secrets.manager import get_secret_manager
from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_data_source(user_id: str, payload: dict) -> dict:
    source_type = payload["source_type"]
    connector_cls = registry.get(source_type)
    if connector_cls is None:
        raise ValueError(
            f"Unknown source_type '{source_type}'. "
            f"Supported: {registry.list_supported()}"
        )

    config = payload.get("config", {})
    encrypted = get_secret_manager().encrypt_secret(json.dumps(config))
    now = _now()

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO data_source_connections (
                user_id, display_name, source_type, source_category,
                encrypted_config_json, config_schema_version,
                capabilities_json, metadata_json, source_status, is_active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?)
            """,
            (
                user_id,
                payload["display_name"],
                source_type,
                connector_cls.source_category,
                encrypted,
                connector_cls.config_schema_version,
                json.dumps(sorted(connector_cls.supported_capabilities)),
                json.dumps(payload.get("metadata", {})),
                now,
                now,
            ),
        )
        conn.commit()
        source_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM data_source_connections WHERE id = ?",
            (source_id,),
        ).fetchone()
        record = _to_public_record(row)
    finally:
        conn.close()

    job_id = _create_metadata_job(source_id, user_id)
    record["metadata_job_id"] = job_id
    return record


def _create_metadata_job(source_id: int, user_id: str) -> int:
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO metadata_jobs (
                source_id, user_id, job_type, status, created_at, updated_at
            ) VALUES (?, ?, 'initial_metadata', 'QUEUED', ?, ?)
            """,
            (source_id, user_id, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE metadata_jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def get_metadata_job(job_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM metadata_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    d.pop("user_id", None)
    return d


def run_metadata_job(job_id: int) -> None:
    from data.schema_service import run_discovery
    from data.profiling_service import run_structural_profiling

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_id, user_id FROM metadata_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        logger.error("run_metadata_job: job id=%s not found", job_id)
        return

    source_id = row["source_id"]
    user_id = row["user_id"]

    _update_job(job_id, status="RUNNING", current_step="DISCOVERY",
                progress_message="Discovering schema...", started_at=_now())

    try:
        run_discovery(source_id, user_id)
    except Exception:
        logger.exception("Metadata job id=%s: discovery failed for source id=%s", job_id, source_id)
        _update_job(job_id, status="FAILED", error_message="Schema discovery failed.",
                    completed_at=_now())
        return

    _update_job(job_id, status="RUNNING", current_step="STRUCTURAL_PROFILING",
                progress_message="Running structural profiling...")

    try:
        run_structural_profiling(source_id, user_id)
    except Exception:
        logger.exception("Metadata job id=%s: structural profiling failed for source id=%s", job_id, source_id)
        _update_job(job_id, status="FAILED", error_message="Structural profiling failed.",
                    completed_at=_now())
        return

    _update_job(job_id, status="RUNNING", current_step="LIFECYCLE",
                progress_message="Running autonomous metadata lifecycle...")

    # The autonomous metadata lifecycle (dictionary/domain/entity refresh, review
    # tasks, notifications) is best-effort on top of the core scan promise: a
    # lifecycle failure does not fail the job, since discovery + profiling — the
    # data this job exists to produce — already succeeded. The failure is still
    # fully recorded via metadata_lifecycle_runs, the audit log, and a failure
    # notification inside run_autonomous_lifecycle() itself.
    try:
        from core.lifecycle.models import LifecycleTrigger
        from core.lifecycle.runner import run_autonomous_lifecycle

        run_autonomous_lifecycle(
            source_id, user_id, trigger=LifecycleTrigger.SCAN_COMPLETE, job_id=job_id,
        )
    except Exception:
        logger.exception(
            "Metadata job id=%s: autonomous metadata lifecycle failed for source id=%s",
            job_id, source_id,
        )

    _update_job(job_id, status="COMPLETE", current_step="READY",
                progress_message="Metadata ready.", completed_at=_now())


def list_data_sources(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM data_source_connections "
            "WHERE user_id = ? AND is_active = 1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [_to_public_record(row) for row in rows]
    finally:
        conn.close()


def get_data_source_by_id(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        return _to_public_record(row) if row is not None else None
    finally:
        conn.close()


def record_connectivity_test(source_id: int, user_id: str, status: str, message: str) -> None:
    source_status = "ACTIVE" if status == "success" else "ERROR"
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE data_source_connections
            SET last_tested_at    = ?,
                last_test_status  = ?,
                last_test_message = ?,
                source_status     = ?,
                updated_at        = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, status, message, source_status, now, source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_data_source(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_type, encrypted_config_json "
            "FROM data_source_connections "
            "WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    try:
        params = json.loads(get_secret_manager().decrypt_secret(row["encrypted_config_json"]))
    except Exception:
        logger.error("Failed to decrypt config for source id=%s", source_id)
        return {"status": "failed", "message": "Failed to load connection configuration.", "latency_ms": None}

    connector_cls = registry.get(row["source_type"])
    if connector_cls is None:
        return {
            "status": "failed",
            "message": f"No connector registered for source_type '{row['source_type']}'.",
            "latency_ms": None,
        }

    result = connector_cls().test_connectivity(
        DataSourceConfig(source_type=row["source_type"], params=params)
    )

    try:
        record_connectivity_test(
            source_id=source_id,
            user_id=user_id,
            status="success" if result.success else "failed",
            message=result.message,
        )
    except Exception:
        logger.warning(
            "record_connectivity_test failed for source id=%s — "
            "local SQLite may be locked; connectivity result still returned",
            source_id,
        )

    return {
        "status": "success" if result.success else "failed",
        "message": result.message,
        "latency_ms": result.latency_ms,
    }


def get_connection_config(source_id: int, user_id: str) -> dict | None:
    """
    Read-only lookup for live connection resolution. Returns decrypted
    connection params alongside source status/capabilities, scoped to the
    owning user. Never includes encrypted_config_json.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_type, source_category, display_name, is_active, "
            "source_status, capabilities_json, encrypted_config_json "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    try:
        params = json.loads(get_secret_manager().decrypt_secret(row["encrypted_config_json"]))
    except Exception:
        logger.error("get_connection_config: failed to decrypt config for source id=%s", source_id)
        return None

    return {
        "source_type": row["source_type"],
        "source_category": row["source_category"],
        "display_name": row["display_name"],
        "is_active": bool(row["is_active"]),
        "source_status": row["source_status"],
        "capabilities": json.loads(row["capabilities_json"] or "[]"),
        "params": params,
    }


def delete_data_source(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, display_name FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if row is None:
            return None

        display_name = row["display_name"]

        active_job = conn.execute(
            "SELECT id FROM metadata_jobs WHERE source_id = ? AND status IN ('QUEUED', 'RUNNING') LIMIT 1",
            (source_id,),
        ).fetchone()
        if active_job is not None:
            raise ValueError(
                "Metadata job is still running. Please wait or cancel it before deleting this source."
            )

        conn.execute(
            "DELETE FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": source_id, "display_name": display_name, "deleted": True}


def _to_public_record(row) -> dict:
    d = dict(row)

    config_summary = None
    try:
        params = json.loads(get_secret_manager().decrypt_secret(d["encrypted_config_json"]))
        connector_cls = registry.get(d["source_type"])
        if connector_cls is not None:
            config_summary = connector_cls().get_config_summary(params)
    except Exception:
        logger.error("config_summary build failed for source id=%s", d.get("id"))

    d.pop("encrypted_config_json", None)
    d["capabilities"] = json.loads(d.pop("capabilities_json", "[]"))
    d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
    d["config_summary"] = config_summary

    return d
