import json
import logging
from datetime import datetime, timezone

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo,
    SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.dictionary.generator import DictionaryResult, generate_dictionary
from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reconstruct_snapshot(d: dict, source_id: int) -> SchemaSnapshot:
    """Rebuild a SchemaSnapshot dataclass tree from a dataclasses.asdict() dict."""
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


# ── Upsert SQL (module-level to avoid re-parsing on every call) ───────────────

_TABLE_UPSERT = """
    INSERT INTO data_dictionary_tables
        (source_id, snapshot_id, table_fqn, table_name, schema_name, table_type,
         business_name, description, domain, grain,
         is_approved, generation_method, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        snapshot_id       = excluded.snapshot_id,
        business_name     = excluded.business_name,
        description       = excluded.description,
        domain            = excluded.domain,
        grain             = excluded.grain,
        generation_method = excluded.generation_method,
        updated_at        = excluded.updated_at
    WHERE data_dictionary_tables.generation_method != 'human'
"""

_COL_UPSERT = """
    INSERT INTO data_dictionary_columns
        (source_id, snapshot_id, table_fqn, column_name,
         business_label, meaning, semantic_type,
         is_metric, is_dimension, is_date, is_id, pii_risk,
         is_approved, generation_method, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn, column_name) DO UPDATE SET
        snapshot_id       = excluded.snapshot_id,
        business_label    = excluded.business_label,
        meaning           = excluded.meaning,
        semantic_type     = excluded.semantic_type,
        is_metric         = excluded.is_metric,
        is_dimension      = excluded.is_dimension,
        is_date           = excluded.is_date,
        is_id             = excluded.is_id,
        pii_risk          = excluded.pii_risk,
        generation_method = excluded.generation_method,
        updated_at        = excluded.updated_at
    WHERE data_dictionary_columns.generation_method != 'human'
"""


# ── Public service functions ───────────────────────────────────────────────────

def generate_and_save_dictionary(source_id: int, user_id: str) -> dict | None:
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

    conn = get_connection()
    try:
        snap_row = conn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if snap_row is None:
        raise ValueError(
            "No schema snapshot found. Run POST /sources/{id}/discover first."
        )

    snapshot_id = snap_row["id"]
    snapshot = _reconstruct_snapshot(json.loads(snap_row["snapshot_json"]), source_id)

    result = generate_dictionary(snapshot, snapshot_id)
    _upsert_dictionary(result)

    return {
        "snapshot_id":       snapshot_id,
        "tables_generated":  len(result.table_entries),
        "columns_generated": len(result.column_entries),
        "pii_column_count":  result.pii_column_count,
        "generation_method": result.generation_method,
        "generated_at":      result.generated_at,
        "coverage":          _coverage(source_id),
    }


def list_dictionary_tables(source_id: int, user_id: str) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM data_dictionary_tables "
            "WHERE source_id = ? ORDER BY schema_name, table_name",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def get_table_dictionary(source_id: int, user_id: str, table_fqn: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        table_row = conn.execute(
            "SELECT * FROM data_dictionary_tables "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
        if table_row is None:
            return None

        col_rows = conn.execute(
            "SELECT * FROM data_dictionary_columns "
            "WHERE source_id = ? AND table_fqn = ? "
            "ORDER BY column_name",
            (source_id, table_fqn),
        ).fetchall()
    finally:
        conn.close()

    return {
        "table":   dict(table_row),
        "columns": [dict(r) for r in col_rows],
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _upsert_dictionary(result: DictionaryResult) -> None:
    now = _now()

    table_params = [
        (
            e.source_id, e.snapshot_id, e.table_fqn, e.table_name,
            e.schema_name, e.table_type,
            e.business_name, e.description, e.domain, e.grain,
            e.generation_method, now, now,
        )
        for e in result.table_entries
    ]

    col_params = [
        (
            e.source_id, e.snapshot_id, e.table_fqn, e.column_name,
            e.business_label, e.meaning, e.semantic_type,
            int(e.is_metric), int(e.is_dimension), int(e.is_date),
            int(e.is_id), int(e.pii_risk),
            e.generation_method, now, now,
        )
        for e in result.column_entries
    ]

    conn = get_connection()
    try:
        conn.executemany(_TABLE_UPSERT, table_params)
        conn.executemany(_COL_UPSERT, col_params)
        conn.commit()
    finally:
        conn.close()


def approve_table_dictionary(source_id: int, user_id: str, table_fqn: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        now = _now()
        cursor = conn.execute(
            "UPDATE data_dictionary_tables "
            "SET is_approved = 1, approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE source_id = ? AND table_fqn = ?",
            (user_id, now, now, source_id, table_fqn),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        return None  # no matching entry
    return {"approved": True, "coverage": _coverage(source_id)}


def approve_column_dictionary(
    source_id: int, user_id: str, table_fqn: str, column_name: str
) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        now = _now()
        cursor = conn.execute(
            "UPDATE data_dictionary_columns "
            "SET is_approved = 1, approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE source_id = ? AND table_fqn = ? AND column_name = ?",
            (user_id, now, now, source_id, table_fqn, column_name),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        return None  # no matching entry
    return {"approved": True, "coverage": _coverage(source_id)}


def _coverage(source_id: int) -> dict:
    conn = get_connection()
    try:
        t = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_approved), 0) AS approved "
            "FROM data_dictionary_tables WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        c = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_approved), 0) AS approved "
            "FROM data_dictionary_columns WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    return {
        "tables_approved":  int(t["approved"]),
        "tables_total":     int(t["total"]),
        "columns_approved": int(c["approved"]),
        "columns_total":    int(c["total"]),
    }
