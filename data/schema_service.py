import dataclasses
import json
import logging
from datetime import datetime, timezone

import core.connectors.registry as registry
from core.connectors.base import DataSourceConfig
from core.connectors.schema import SchemaSnapshot
from core.secrets.manager import get_secret_manager
from data.db import get_connection
import data.relationship_service as _rel_svc

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_discovery(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT encrypted_config_json, source_type, capabilities_json "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    source_type = row["source_type"]
    capabilities = json.loads(row["capabilities_json"] or "[]")

    if "schema_discovery" not in capabilities:
        raise ValueError(
            f"source_type '{source_type}' does not support schema_discovery."
        )

    connector_cls = registry.get(source_type)
    if connector_cls is None:
        raise ValueError(
            f"No connector registered for source_type '{source_type}'."
        )

    try:
        params = json.loads(
            get_secret_manager().decrypt_secret(row["encrypted_config_json"])
        )
    except Exception:
        logger.error("Failed to decrypt config for source id=%s", source_id)
        raise RuntimeError("Failed to load connection configuration.")

    params["_source_id"] = source_id
    snapshot = connector_cls().discover_schema(
        DataSourceConfig(source_type=source_type, params=params)
    )

    saved = _save_snapshot(source_id, snapshot)

    try:
        _rel_svc.extract_and_persist_relationships(saved["snapshot_id"], source_id)
    except Exception:
        logger.warning(
            "Relationship extraction failed for snapshot_id=%s; discovery result unaffected",
            saved["snapshot_id"],
        )

    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE data_source_connections "
            "SET last_discovered_at = ?, last_snapshot_id = ?, "
            "source_status = 'ACTIVE', updated_at = ? "
            "WHERE id = ?",
            (now, saved["snapshot_id"], now, source_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "snapshot_id":      saved["snapshot_id"],
        "snapshot_version": saved["snapshot_version"],
        "source_id":        source_id,
        "source_type":      source_type,
        "table_count":      snapshot.table_count,
        "view_count":       snapshot.view_count,
        "column_count":     snapshot.column_count,
        "discovered_at":    snapshot.discovered_at,
        "warnings":         snapshot.warnings,
    }


def get_latest_snapshot(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        row = conn.execute(
            "SELECT * FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return {
        **_to_public_summary(row),
        **json.loads(row["snapshot_json"]),
    }


def list_snapshot_versions(source_id: int, user_id: str) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT id, source_id, snapshot_version, source_type, "
            "table_count, view_count, column_count, discovered_at, created_at "
            "FROM schema_snapshots WHERE source_id = ? "
            "ORDER BY snapshot_version DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    return [_to_public_summary(r) for r in rows]


def _save_snapshot(source_id: int, snapshot: SchemaSnapshot) -> dict:
    now = _now()
    snapshot_json = json.dumps(dataclasses.asdict(snapshot))

    conn = get_connection()
    try:
        version_row = conn.execute(
            "SELECT COALESCE(MAX(snapshot_version), 0) + 1 "
            "FROM schema_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        version = version_row[0]

        cursor = conn.execute(
            "INSERT INTO schema_snapshots "
            "(source_id, snapshot_version, source_type, table_count, view_count, "
            "column_count, snapshot_json, discovered_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                version,
                snapshot.source_type,
                snapshot.table_count,
                snapshot.view_count,
                snapshot.column_count,
                snapshot_json,
                snapshot.discovered_at,
                now,
            ),
        )
        conn.commit()
        return {"snapshot_id": cursor.lastrowid, "snapshot_version": version}
    finally:
        conn.close()


def _to_public_summary(row) -> dict:
    d = dict(row)
    d.pop("snapshot_json", None)
    return d
