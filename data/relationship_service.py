import json
import logging
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_snapshot_id(conn, source_id: int, snapshot_id: int | None) -> int | None:
    """Return the provided snapshot_id or the latest snapshot for this source."""
    if snapshot_id is not None:
        return snapshot_id
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


def _verify_source_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _parse_fks_from_snapshot_json(
    snapshot_json: str,
    snapshot_id: int,
    source_id: int,
) -> list[dict]:
    """
    Extract FK relationship dicts from a raw snapshot_json string.
    Returns [] on parse failure or when no FKs exist — never raises.
    from_schema and from_table are derived from the enclosing TableInfo context
    because ForeignKeyInfo only carries the to_* side.
    """
    now = _now()
    try:
        data = json.loads(snapshot_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "relationship_service: failed to parse snapshot_json for snapshot_id=%s",
            snapshot_id,
        )
        return []

    relationships: list[dict] = []
    for schema in data.get("schemas") or []:
        schema_name = schema.get("schema_name") or ""
        for table in schema.get("tables") or []:
            table_name = table.get("table_name") or ""
            from_table_fqn = table.get("table_fqn") or f"{schema_name}.{table_name}"
            for fk in table.get("foreign_keys") or []:
                from_column = fk.get("from_column") or ""
                to_schema = fk.get("to_schema") or ""
                to_table = fk.get("to_table") or ""
                to_column = fk.get("to_column") or ""
                fk_name = fk.get("fk_name") or ""

                if not (from_column and to_table and to_column):
                    continue

                to_table_fqn = f"{to_schema}.{to_table}" if to_schema else to_table
                relationships.append({
                    "source_id":          source_id,
                    "snapshot_id":        snapshot_id,
                    "from_schema":        schema_name,
                    "from_table":         table_name,
                    "from_table_fqn":     from_table_fqn,
                    "from_column":        from_column,
                    "to_schema":          to_schema,
                    "to_table":           to_table,
                    "to_table_fqn":       to_table_fqn,
                    "to_column":          to_column,
                    "relationship_name":  fk_name,
                    "relationship_type":  "FOREIGN_KEY",
                    "confidence":         1.0,
                    "evidence_json":      json.dumps({
                        "source":      "schema_snapshot",
                        "snapshot_id": snapshot_id,
                        "fk_name":     fk_name,
                    }),
                    "created_at":         now,
                })
    return relationships


def extract_relationships(snapshot_id: int, source_id: int) -> list[dict]:
    """Load snapshot_json from the DB and return extracted FK relationship dicts."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM schema_snapshots WHERE id = ? AND source_id = ?",
            (snapshot_id, source_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return []
    return _parse_fks_from_snapshot_json(row["snapshot_json"], snapshot_id, source_id)


def persist_relationships(
    conn,
    snapshot_id: int,
    source_id: int,
    relationships: list[dict],
) -> int:
    """
    Idempotent batch insert via INSERT OR IGNORE. The unique index on
    (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column)
    silently drops duplicates on re-run so re-discovery is always safe.
    Returns the number of rows actually inserted (0 on a repeat run).
    """
    if not relationships:
        return 0

    before = conn.execute(
        "SELECT COUNT(*) FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
        (source_id, snapshot_id),
    ).fetchone()[0]

    conn.executemany(
        """
        INSERT OR IGNORE INTO table_relationships
            (source_id, snapshot_id, from_schema, from_table, from_table_fqn,
             from_column, to_schema, to_table, to_table_fqn, to_column,
             relationship_name, relationship_type, confidence, evidence_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["source_id"],        r["snapshot_id"],    r["from_schema"],
                r["from_table"],       r["from_table_fqn"], r["from_column"],
                r["to_schema"],        r["to_table"],       r["to_table_fqn"],
                r["to_column"],        r["relationship_name"], r["relationship_type"],
                r["confidence"],       r["evidence_json"],  r["created_at"],
            )
            for r in relationships
        ],
    )
    conn.commit()

    after = conn.execute(
        "SELECT COUNT(*) FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
        (source_id, snapshot_id),
    ).fetchone()[0]

    return after - before


def extract_and_persist_relationships(snapshot_id: int, source_id: int) -> dict:
    """
    Orchestrate extraction from snapshot_json and idempotent persistence.
    Called automatically after every schema discovery save.
    A non-fatal wrapper: errors are logged and surfaced in the return dict.
    """
    relationships = extract_relationships(snapshot_id, source_id)

    if not relationships:
        return {"relationships_found": 0, "relationships_inserted": 0}

    conn = get_connection()
    try:
        inserted = persist_relationships(conn, snapshot_id, source_id, relationships)
    finally:
        conn.close()

    return {
        "relationships_found":    len(relationships),
        "relationships_inserted": inserted,
    }


def get_relationships_for_source(
    source_id: int,
    user_id: str,
    snapshot_id: int | None = None,
) -> list[dict] | None:
    """
    List all FK relationship rows for a source (latest snapshot by default).
    Returns None when the source does not exist or is not owned by user_id.
    Returns [] when the source exists but has no snapshot or no FKs yet.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        sid = _resolve_snapshot_id(conn, source_id, snapshot_id)
        if sid is None:
            return []

        rows = conn.execute(
            """
            SELECT id, source_id, snapshot_id,
                   from_schema, from_table, from_table_fqn, from_column,
                   to_schema, to_table, to_table_fqn, to_column,
                   relationship_name, relationship_type, confidence,
                   evidence_json, created_at
            FROM table_relationships
            WHERE source_id = ? AND snapshot_id = ?
            ORDER BY from_table_fqn, from_column
            """,
            (source_id, sid),
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def get_relationships_for_table(
    source_id: int,
    user_id: str,
    table_fqn: str,
    snapshot_id: int | None = None,
) -> dict | None:
    """
    Return {"outbound": [...], "inbound": [...]} for a single table.
    Outbound = FK relationships this table declares (from_table_fqn = table_fqn).
    Inbound  = FK relationships pointing at this table (to_table_fqn = table_fqn).
    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        sid = _resolve_snapshot_id(conn, source_id, snapshot_id)
        if sid is None:
            return {"outbound": [], "inbound": []}

        _COLS = (
            "id, from_schema, from_table, from_table_fqn, from_column, "
            "to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence, created_at"
        )

        outbound = conn.execute(
            f"SELECT {_COLS} FROM table_relationships "
            "WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ? "
            "ORDER BY from_column",
            (source_id, sid, table_fqn),
        ).fetchall()

        inbound = conn.execute(
            f"SELECT {_COLS} FROM table_relationships "
            "WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ? "
            "ORDER BY from_table_fqn, from_column",
            (source_id, sid, table_fqn),
        ).fetchall()
    finally:
        conn.close()

    return {
        "outbound": [dict(r) for r in outbound],
        "inbound":  [dict(r) for r in inbound],
    }


def get_relationship_summary(
    source_id: int,
    user_id: str,
    snapshot_id: int | None = None,
) -> dict | None:
    """
    Aggregate relationship counts for a source.
    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        sid = _resolve_snapshot_id(conn, source_id, snapshot_id)
        if sid is None:
            return {
                "snapshot_id":              None,
                "total_relationships":      0,
                "tables_with_outbound_fks": 0,
                "tables_referenced_by_fk":  0,
                "most_referenced":          [],
            }

        totals = conn.execute(
            """
            SELECT
                COUNT(*)                       AS total_relationships,
                COUNT(DISTINCT from_table_fqn) AS tables_with_outbound_fks,
                COUNT(DISTINCT to_table_fqn)   AS tables_referenced_by_fk
            FROM table_relationships
            WHERE source_id = ? AND snapshot_id = ?
            """,
            (source_id, sid),
        ).fetchone()

        most_referenced = conn.execute(
            """
            SELECT to_table_fqn, COUNT(*) AS inbound_count
            FROM table_relationships
            WHERE source_id = ? AND snapshot_id = ?
            GROUP BY to_table_fqn
            ORDER BY inbound_count DESC
            LIMIT 10
            """,
            (source_id, sid),
        ).fetchall()
    finally:
        conn.close()

    return {
        "snapshot_id":              sid,
        "total_relationships":      totals["total_relationships"],
        "tables_with_outbound_fks": totals["tables_with_outbound_fks"],
        "tables_referenced_by_fk":  totals["tables_referenced_by_fk"],
        "most_referenced": [
            {"table_fqn": r["to_table_fqn"], "inbound_count": r["inbound_count"]}
            for r in most_referenced
        ],
    }
