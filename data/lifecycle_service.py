import json
import logging
from datetime import datetime, timezone

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo,
    SchemaInfo, SchemaSnapshot, TableInfo,
)
from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reconstruct_snapshot(d: dict, source_id: int) -> SchemaSnapshot:
    """Rebuild a SchemaSnapshot dataclass tree from a dataclasses.asdict() dict.

    Duplicated from data/dictionary_service.py / data/profiling_service.py —
    matches the existing convention of a private, module-local copy rather than
    a shared helper (already duplicated twice before this module existed).
    """
    schemas = []
    for s in d.get('schemas', []):
        tables = []
        for t in s.get('tables', []):
            tables.append(TableInfo(
                table_name=t['table_name'],
                schema_name=t['schema_name'],
                table_fqn=t['table_fqn'],
                table_type=t['table_type'],
                row_count_estimate=t.get('row_count_estimate'),
                columns=[ColumnInfo(**c) for c in t.get('columns', [])],
                primary_keys=[PrimaryKeyInfo(**pk) for pk in t.get('primary_keys', [])],
                foreign_keys=[ForeignKeyInfo(**fk) for fk in t.get('foreign_keys', [])],
            ))
        schemas.append(SchemaInfo(schema_name=s['schema_name'], tables=tables))
    return SchemaSnapshot(
        source_id=source_id,
        source_type=d.get('source_type', ''),
        discovered_at=d.get('discovered_at', ''),
        schemas=schemas,
        database_name=d.get('database_name'),
        server_name=d.get('server_name'),
        connector_version=d.get('connector_version'),
        discovery_duration_ms=d.get('discovery_duration_ms'),
        warnings=d.get('warnings', []),
    )


def get_latest_two_snapshots(
    source_id: int,
) -> tuple[tuple[int, SchemaSnapshot] | None, tuple[int, SchemaSnapshot]]:
    """Return ((prev_id, prev_snapshot) | None, (latest_id, latest_snapshot)).

    prev is None when there is no snapshot older than the latest (first-ever scan).
    Raises ValueError if no snapshot exists at all for source_id.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 2",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"No schema snapshot found for source_id={source_id}.")

    latest_row = rows[0]
    latest = (latest_row["id"], _reconstruct_snapshot(json.loads(latest_row["snapshot_json"]), source_id))

    if len(rows) < 2:
        return None, latest

    prev_row = rows[1]
    prev = (prev_row["id"], _reconstruct_snapshot(json.loads(prev_row["snapshot_json"]), source_id))
    return prev, latest


def get_pii_flags(source_id: int, table_fqns: list[str]) -> dict[tuple[str, str], bool]:
    """{(table_fqn, column_name): pii_risk} for the given tables' current dictionary columns."""
    if not table_fqns:
        return {}
    placeholders = ",".join("?" for _ in table_fqns)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT table_fqn, column_name, pii_risk FROM data_dictionary_columns "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
            (source_id, *table_fqns),
        ).fetchall()
    finally:
        conn.close()
    return {(r["table_fqn"], r["column_name"]): bool(r["pii_risk"]) for r in rows}


def get_domain_values(source_id: int, table_fqns: list[str]) -> dict[str, str]:
    """{table_fqn: domain} for the given tables' current domain assignments."""
    if not table_fqns:
        return {}
    placeholders = ",".join("?" for _ in table_fqns)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT table_fqn, domain FROM domain_assignments "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
            (source_id, *table_fqns),
        ).fetchall()
    finally:
        conn.close()
    return {r["table_fqn"]: r["domain"] for r in rows}


def get_entity_values(source_id: int, table_fqns: list[str]) -> dict[str, str]:
    """{table_fqn: entity} for the given tables' current entity assignments."""
    if not table_fqns:
        return {}
    placeholders = ",".join("?" for _ in table_fqns)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT table_fqn, entity FROM entity_assignments "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
            (source_id, *table_fqns),
        ).fetchall()
    finally:
        conn.close()
    return {r["table_fqn"]: r["entity"] for r in rows}


def record_lifecycle_run_start(
    source_id: int,
    user_id: str,
    job_id: int | None,
    trigger_event: str,
    old_snapshot_id: int | None,
    new_snapshot_id: int | None,
) -> int:
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO metadata_lifecycle_runs
               (source_id, job_id, user_id, trigger_event, status,
                old_snapshot_id, new_snapshot_id, started_at, created_at)
               VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?)""",
            (source_id, job_id, user_id, trigger_event, old_snapshot_id, new_snapshot_id, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def record_lifecycle_run_complete(run_id: int, result) -> None:
    """result is a core.lifecycle.models.LifecycleRunResult."""
    from core.lifecycle.models import ChangeSet

    cs: ChangeSet | None = result.change_set
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE metadata_lifecycle_runs
               SET status = ?,
                   tables_added_count = ?,
                   tables_removed_count = ?,
                   tables_modified_count = ?,
                   objects_changed_count = ?,
                   dictionary_refreshed_count = ?,
                   domains_refreshed_count = ?,
                   entities_refreshed_count = ?,
                   review_tasks_created_count = ?,
                   notifications_sent_count = ?,
                   steps_executed_json = ?,
                   completed_at = ?
               WHERE id = ?""",
            (
                result.status,
                len(cs.added_tables) if cs else 0,
                len(cs.removed_tables) if cs else 0,
                len(cs.modified_tables) if cs else 0,
                len(cs.affected_table_fqns) if cs else 0,
                (result.dictionary_summary or {}).get("tables_generated", 0) if result.dictionary_summary else 0,
                (result.domain_summary or {}).get("tables_assigned", 0) if result.domain_summary else 0,
                (result.entity_summary or {}).get("entities_assigned", 0) if result.entity_summary else 0,
                result.review_tasks_created,
                result.notifications_sent,
                json.dumps([s.to_dict() for s in result.steps]),
                now,
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_lifecycle_run_failed(run_id: int, error_message: str, result=None) -> None:
    now = _now()
    steps_json = json.dumps([s.to_dict() for s in result.steps]) if result is not None else "[]"
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE metadata_lifecycle_runs
               SET status = 'FAILED', error_message = ?, steps_executed_json = ?, completed_at = ?
               WHERE id = ?""",
            (error_message, steps_json, now, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def trigger_manual_lifecycle_run(source_id: int, user_id: str) -> dict | None:
    """Ownership check, then run the autonomous lifecycle synchronously with
    trigger=MANUAL. Returns the serialized LifecycleRunResult, or None if
    source_id does not belong to user_id."""
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if owns is None:
        return None

    from core.lifecycle.models import LifecycleTrigger
    from core.lifecycle.runner import run_autonomous_lifecycle

    result = run_autonomous_lifecycle(
        source_id, user_id, trigger=LifecycleTrigger.MANUAL, job_id=None,
    )
    return result.to_dict()


def list_lifecycle_runs(source_id: int, user_id: str, limit: int = 20) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM metadata_lifecycle_runs "
            "WHERE source_id = ? ORDER BY id DESC LIMIT ?",
            (source_id, limit),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["steps_executed"] = json.loads(d.get("steps_executed_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["steps_executed"] = []
        result.append(d)
    return result


def get_lifecycle_run(source_id: int, user_id: str, run_id: int) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        row = conn.execute(
            "SELECT * FROM metadata_lifecycle_runs WHERE id = ? AND source_id = ?",
            (run_id, source_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    d = dict(row)
    try:
        d["steps_executed"] = json.loads(d.get("steps_executed_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["steps_executed"] = []
    return d
