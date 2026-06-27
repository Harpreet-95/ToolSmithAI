import json
import logging
from datetime import datetime, timezone

from core.connectors.base import DataSourceConfig
from core.connectors.registry import get as get_connector
from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo,
    SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.profiling.engine import (
    ProfilingRunResult, run_profiling,
    _build_referenced_by_index, _classify_columns, _classify_table,
    _profile_table_structural, _run_statistical_pass, _score_table_priority,
)
from core.profiling.models import (
    ProfilingBatchState, ProfilingConfig, ProfilingMode, ProfilingStatus,
)
from core.secrets.manager import get_secret_manager
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


# ── SQL templates ──────────────────────────────────────────────────────────────

_SNAP_INSERT = """
    INSERT INTO profiling_snapshots (
        source_id, schema_snapshot_id, snapshot_version, mode, sample_rate,
        profiling_rules_version, status,
        tables_total, tables_profiled, tables_skipped, tables_failed, tables_timed_out,
        columns_total, columns_profiled, columns_skipped,
        total_rows_profiled, pii_columns_found, classifications_complete,
        started_at, completed_at, duration_seconds, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_TABLE_INSERT = """
    INSERT INTO profiling_table_profiles (
        profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_type,
        exact_row_count, estimated_row_count, row_count_tier,
        has_date_column, date_column_name, earliest_record, latest_record,
        data_span_days, data_currency,
        column_count, pk_column_count, fk_count, referenced_by_count,
        is_junction_table, is_root_table, is_leaf_table, has_identity_column,
        avg_null_percentage, completeness_score,
        table_class, classification_confidence,
        classification_evidence_json, competing_classes_json, classification_rule_version,
        pii_column_count, confirmed_pii_count,
        profiling_depth, profiling_duration_ms, profiling_status,
        skip_reason, profiled_at, created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_COL_INSERT = """
    INSERT INTO profiling_column_profiles (
        profiling_snapshot_id, source_id, table_fqn, column_name,
        data_type, raw_type, is_nullable, is_primary_key, is_identity, ordinal_position,
        null_count, null_percentage, populated_count, populated_percentage,
        empty_string_count, zero_count,
        distinct_count, distinct_percentage, uniqueness_score, cardinality_tier,
        min_value, max_value, min_length, max_length_observed, avg_length,
        mean_value, std_deviation, p5_value, p95_value,
        dominant_pattern, pattern_coverage,
        email_match_rate, phone_match_rate, guid_match_rate,
        date_string_rate, numeric_string_rate, masked_value_rate,
        semantic_type, semantic_confidence, semantic_evidence_json, semantic_rule_version,
        pii_name_heuristic, pii_confirmed, pii_signals_json,
        top_values_coverage,
        profiling_depth, profiling_duration_ms, profiling_status,
        created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


# ── Parameter builders ─────────────────────────────────────────────────────────

def _snap_params(result: ProfilingRunResult, snap_version: int, now: str) -> tuple:
    s = result.snapshot
    return (
        s.source_id, s.schema_snapshot_id, snap_version,
        s.mode.value if hasattr(s.mode, 'value') else s.mode,
        s.sample_rate, s.profiling_rules_version,
        s.status.value if hasattr(s.status, 'value') else s.status,
        s.tables_total, s.tables_profiled, s.tables_skipped,
        s.tables_failed, s.tables_timed_out,
        s.columns_total, s.columns_profiled, s.columns_skipped,
        s.total_rows_profiled, s.pii_columns_found, s.classifications_complete,
        s.started_at, s.completed_at, s.duration_seconds, now,
    )


def _table_row_params(tp, snap_id: int, rule_version: str, now: str) -> tuple:
    cls = tp.classification
    rct = tp.row_count_tier.value if tp.row_count_tier else None
    dc  = tp.data_currency.value  if tp.data_currency  else None
    pd  = tp.profiling_depth.value
    ps  = tp.profiling_status.value
    return (
        snap_id, tp.source_id, tp.table_fqn, tp.table_name, tp.schema_name, tp.table_type,
        tp.exact_row_count, tp.estimated_row_count, rct,
        int(tp.has_date_column), tp.date_column_name,
        tp.earliest_record, tp.latest_record, tp.data_span_days, dc,
        tp.column_count, tp.pk_column_count, tp.fk_count, tp.referenced_by_count,
        int(tp.is_junction_table), int(tp.is_root_table),
        int(tp.is_leaf_table), int(tp.has_identity_column),
        tp.avg_null_percentage, tp.completeness_score,
        cls.classification if cls else None,
        cls.confidence    if cls else None,
        json.dumps(list(cls.evidence))   if cls else None,
        json.dumps(list(cls.competing))  if cls else None,
        cls.rule_version  if cls else None,
        tp.pii_column_count, tp.confirmed_pii_count,
        pd, tp.profiling_duration_ms, ps,
        tp.skip_reason, tp.profiled_at, now, now,
    )


def _col_row_params(cp, snap_id: int, rule_version: str, now: str) -> tuple:
    ct = cp.cardinality_tier.value if cp.cardinality_tier else None
    st = cp.semantic_type.value    if cp.semantic_type    else None
    pd = cp.profiling_depth.value
    ps = cp.profiling_status.value
    return (
        snap_id, cp.source_id, cp.table_fqn, cp.column_name,
        cp.data_type, cp.raw_type,
        int(cp.is_nullable), int(cp.is_primary_key), int(cp.is_identity), cp.ordinal_position,
        cp.null_count, cp.null_percentage, cp.populated_count, cp.populated_percentage,
        cp.empty_string_count, cp.zero_count,
        cp.distinct_count, cp.distinct_percentage, cp.uniqueness_score, ct,
        cp.min_value, cp.max_value, cp.min_length, cp.max_length_observed, cp.avg_length,
        cp.mean_value, cp.std_deviation, cp.p5_value, cp.p95_value,
        cp.dominant_pattern, cp.pattern_coverage,
        cp.email_match_rate, cp.phone_match_rate, cp.guid_match_rate,
        cp.date_string_rate, cp.numeric_string_rate, cp.masked_value_rate,
        st, cp.semantic_confidence,
        json.dumps(cp.semantic_evidence) if cp.semantic_evidence else None,
        rule_version,
        int(cp.pii_name_heuristic), int(cp.pii_confirmed),
        json.dumps(cp.pii_signals) if cp.pii_signals else None,
        cp.top_values_coverage,
        pd, cp.profiling_duration_ms, ps,
        now, now,
    )


# ── Public service functions ───────────────────────────────────────────────────

def run_structural_profiling(source_id: int, user_id: str) -> dict | None:
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

    schema_snapshot_id = snap_row["id"]
    snapshot = _reconstruct_snapshot(
        json.loads(snap_row["snapshot_json"]), source_id
    )

    config = ProfilingConfig(mode=ProfilingMode.STRUCTURAL_ONLY)
    result = run_profiling(source_id, schema_snapshot_id, config, snapshot)

    profiling_snapshot_id = _save_profiling_result(result)

    return {
        "profiling_snapshot_id":  profiling_snapshot_id,
        "schema_snapshot_id":     schema_snapshot_id,
        "tables_profiled":        result.snapshot.tables_profiled,
        "columns_profiled":       result.snapshot.columns_profiled,
        "tables_classified":      result.tables_classified,
        "columns_classified":     result.columns_classified,
        "pii_columns_found":      result.pii_columns_found,
        "classification_summary": result.classification_summary,
        "semantic_type_summary":  result.semantic_type_summary,
        "duration_ms":            result.duration_ms,
        "mode":                   result.snapshot.mode,
        "status":                 result.snapshot.status,
    }


def run_full_profiling(source_id: int, user_id: str) -> dict | None:
    # ── Ownership + snapshot ───────────────────────────────────────────────────
    iconn = get_connection()
    try:
        source_row = iconn.execute(
            "SELECT id, source_type, encrypted_config_json "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        iconn.close()

    if source_row is None:
        return None

    iconn = get_connection()
    try:
        snap_row = iconn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        iconn.close()

    if snap_row is None:
        raise ValueError(
            "No schema snapshot found. Run POST /sources/{id}/discover first."
        )

    # ── Decrypt config and open connector ─────────────────────────────────────
    schema_snapshot_id = snap_row["id"]
    snapshot = _reconstruct_snapshot(
        json.loads(snap_row["snapshot_json"]), source_id
    )

    source_type = source_row["source_type"]
    connector_cls = get_connector(source_type)
    if connector_cls is None:
        raise ValueError(
            f"No connector registered for source_type '{source_type}'."
        )
    if "sql_query" not in connector_cls.supported_capabilities:
        raise ValueError(
            f"source_type '{source_type}' does not support sql_query."
        )

    try:
        params = json.loads(
            get_secret_manager().decrypt_secret(source_row["encrypted_config_json"])
        )
    except Exception:
        logger.error("Failed to decrypt config for source id=%s", source_id)
        raise RuntimeError("Failed to load connection configuration.")

    # ── Execute full profiling with live connection ───────────────────────────
    config = ProfilingConfig(
        mode=ProfilingMode.FULL,
        max_tables=10,
        max_column_count=300,
        excluded_prefixes=['stg_', 'tmp_', 'temp_', 'bak_', 'backup_', 'arc_', 'old_'],
    )
    builder = connector_cls().get_query_builder()
    live_conn = None
    try:
        live_conn = connector_cls().open_connection(
            DataSourceConfig(source_type=source_type, params=params)
        )
        result = run_profiling(
            source_id, schema_snapshot_id, config, snapshot,
            connection=live_conn,
            query_builder=builder,
        )
    finally:
        if live_conn is not None:
            live_conn.close()

    profiling_snapshot_id = _save_profiling_result(result)

    return {
        "profiling_snapshot_id":  profiling_snapshot_id,
        "schema_snapshot_id":     schema_snapshot_id,
        "tables_profiled":        result.snapshot.tables_profiled,
        "columns_profiled":       result.snapshot.columns_profiled,
        "tables_classified":      result.tables_classified,
        "columns_classified":     result.columns_classified,
        "pii_columns_found":      result.pii_columns_found,
        "classification_summary": result.classification_summary,
        "semantic_type_summary":  result.semantic_type_summary,
        "duration_ms":            result.duration_ms,
        "mode":                   result.snapshot.mode,
        "status":                 result.snapshot.status,
        "full_profile_candidates": result.tables_selected_for_full_profile,
    }


def get_latest_profile(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        snap = conn.execute(
            "SELECT * FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snap is None:
            return None

        tables = conn.execute(
            "SELECT table_fqn, table_name, schema_name, table_type, "
            "exact_row_count, estimated_row_count, row_count_tier, "
            "table_class, classification_confidence, "
            "pii_column_count, confirmed_pii_count, "
            "fk_count, referenced_by_count, is_junction_table, "
            "profiling_depth, profiling_status "
            "FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ? "
            "ORDER BY schema_name, table_name",
            (snap["id"],),
        ).fetchall()
    finally:
        conn.close()

    return {
        "snapshot": _to_public_profile_summary(snap),
        "tables":   [dict(r) for r in tables],
    }


def list_profile_history(source_id: int, user_id: str) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    return [_to_public_profile_summary(r) for r in rows]


def get_column_profiles(
    source_id: int,
    user_id: str,
    *,
    table_fqn: str | None = None,
    semantic_type: str | None = None,
    pii_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict | None:
    """Return paginated column profiles from the latest profiling snapshot.

    Returns None when the source does not exist or does not belong to user_id.
    Returns an empty columns list when profiling has not yet run.
    All filters are applied server-side via parameterised SQL — no injection risk.
    """
    limit  = max(1, min(limit, 500))
    offset = max(0, offset)

    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        snap = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snap is None:
            return {
                "snapshot_id": None,
                "source_id":   source_id,
                "total":       0,
                "limit":       limit,
                "offset":      offset,
                "columns":     [],
            }

        snap_id = snap["id"]

        filters: list[str] = ["profiling_snapshot_id = ?"]
        params:  list      = [snap_id]

        if table_fqn:
            filters.append("table_fqn = ?")
            params.append(table_fqn)
        if semantic_type:
            filters.append("semantic_type = ?")
            params.append(semantic_type)
        if pii_only:
            filters.append("pii_name_heuristic = 1")

        where = " AND ".join(filters)

        total = conn.execute(
            f"SELECT COUNT(*) FROM profiling_column_profiles WHERE {where}",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT
                table_fqn, column_name, data_type, raw_type,
                is_nullable, is_primary_key, is_identity,
                null_percentage, distinct_percentage, uniqueness_score, cardinality_tier,
                min_value, max_value, avg_length,
                semantic_type, semantic_confidence,
                pii_name_heuristic, pii_confirmed, pii_signals_json,
                dominant_pattern, pattern_coverage, top_values_coverage,
                profiling_depth, profiling_status
            FROM profiling_column_profiles
            WHERE {where}
            ORDER BY table_fqn, ordinal_position
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return {
        "snapshot_id": snap_id,
        "source_id":   source_id,
        "total":       total,
        "limit":       limit,
        "offset":      offset,
        "columns":     [dict(r) for r in rows],
    }


def get_table_profile_detail(
    source_id: int,
    user_id: str,
    table_fqn: str,
) -> dict | None:
    """Return the latest table profile and all its column profiles.

    Returns None when the source does not belong to user_id.
    Returns {"table": None, "columns": []} when no profiling snapshot exists.
    Returns {"table": None, "columns": []} when the specific table was not profiled.
    Columns are ordered by ordinal_position.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        snap = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snap is None:
            return {"table": None, "columns": []}

        snap_id = snap["id"]

        table_row = conn.execute(
            """
            SELECT
                table_fqn, table_name, schema_name, table_type,
                exact_row_count, estimated_row_count, row_count_tier,
                has_date_column, date_column_name,
                earliest_record, latest_record, data_span_days, data_currency,
                column_count, pk_column_count, fk_count, referenced_by_count,
                is_junction_table, is_root_table, is_leaf_table, has_identity_column,
                avg_null_percentage, completeness_score,
                table_class, classification_confidence,
                classification_evidence_json, competing_classes_json,
                pii_column_count, confirmed_pii_count,
                profiling_depth, profiling_status, profiled_at
            FROM profiling_table_profiles
            WHERE profiling_snapshot_id = ? AND table_fqn = ?
            """,
            (snap_id, table_fqn),
        ).fetchone()

        col_rows = conn.execute(
            """
            SELECT
                table_fqn, column_name, data_type, raw_type,
                is_nullable, is_primary_key, is_identity,
                null_percentage, distinct_percentage, uniqueness_score, cardinality_tier,
                min_value, max_value, avg_length,
                semantic_type, semantic_confidence,
                pii_name_heuristic, pii_confirmed, pii_signals_json,
                dominant_pattern, pattern_coverage, top_values_coverage,
                profiling_depth, profiling_status
            FROM profiling_column_profiles
            WHERE profiling_snapshot_id = ? AND table_fqn = ?
            ORDER BY ordinal_position
            """,
            (snap_id, table_fqn),
        ).fetchall()
    finally:
        conn.close()

    return {
        "table":   dict(table_row) if table_row else None,
        "columns": [dict(r) for r in col_rows],
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _save_profiling_result(result: ProfilingRunResult) -> int:
    """Persist a ProfilingRunResult to the three profiling tables.

    Returns the newly-inserted profiling_snapshots.id.
    Writes are committed in per-table chunks so the write lock is not held
    for the full duration of a large executemany, which prevents blocking
    concurrent frontend writes (e.g. record_connectivity_test).
    """
    now = _now()
    rule_version = result.snapshot.profiling_rules_version
    source_id = result.snapshot.source_id

    # ── 1. Insert snapshot row and commit immediately to release write lock ────
    conn = get_connection()
    try:
        snap_version = conn.execute(
            "SELECT COALESCE(MAX(snapshot_version), 0) + 1 "
            "FROM profiling_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]

        snap_cursor = conn.execute(_SNAP_INSERT, _snap_params(result, snap_version, now))
        snap_id = snap_cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    # ── 2. Insert each table's profile + its columns in a separate transaction ─
    # Each commit releases the write lock so other connections are not blocked.
    for tp in result.table_profiles:
        conn = get_connection()
        try:
            conn.execute(_TABLE_INSERT, _table_row_params(tp, snap_id, rule_version, now))
            col_params = [
                _col_row_params(cp, snap_id, rule_version, now)
                for cp in tp.column_profiles
            ]
            if col_params:
                conn.executemany(_COL_INSERT, col_params)
            conn.commit()
        finally:
            conn.close()

    return snap_id


def _to_public_profile_summary(row) -> dict:
    d = dict(row)
    return d


# ── Batch INSERT templates (INSERT OR IGNORE for idempotent batch saves) ──────

_TABLE_INSERT_BATCH = _TABLE_INSERT.replace("INSERT INTO", "INSERT OR IGNORE INTO")
_COL_INSERT_BATCH   = _COL_INSERT.replace("INSERT INTO",   "INSERT OR IGNORE INTO")


# ── Batch profiling helpers ────────────────────────────────────────────────────

def _compute_sorted_table_order(snapshot: SchemaSnapshot, config: ProfilingConfig) -> list[str]:
    """Return table FQNs sorted by business priority score (descending).

    Applies exclusion rules from config.  No live SQL executed.
    """
    referenced_by = _build_referenced_by_index(snapshot)
    scored: list[tuple[int, str]] = []

    for schema in snapshot.schemas:
        for table in schema.tables:
            if table.schema_name in config.excluded_schemas:
                continue
            if any(table.table_name.lower().startswith(p.lower())
                   for p in config.excluded_prefixes):
                continue
            if table.table_fqn in config.excluded_table_fqns:
                continue

            ref_count = referenced_by.get(table.table_fqn, 0)
            tp = _profile_table_structural(table, 0, 0, ref_count)
            _classify_table(tp)
            score = _score_table_priority(table, ref_count, tp)
            scored.append((score, table.table_fqn))

    scored.sort(key=lambda x: -x[0])
    return [fqn for _, fqn in scored]


def _save_batch_profiles(profiling_snapshot_id: int, batch_profiles: list) -> tuple[int, int, int]:
    """Persist a batch of TableProfiles.

    Returns (tables_saved, columns_saved, pii_found).
    Uses INSERT OR IGNORE so a repeated batch call is safely idempotent.
    """
    now = _now()
    rule_version = '4.0.0'

    table_params = [
        _table_row_params(tp, profiling_snapshot_id, rule_version, now)
        for tp in batch_profiles
    ]
    col_params = [
        _col_row_params(cp, profiling_snapshot_id, rule_version, now)
        for tp in batch_profiles
        for cp in tp.column_profiles
    ]

    conn = get_connection()
    try:
        conn.executemany(_TABLE_INSERT_BATCH, table_params)
        conn.executemany(_COL_INSERT_BATCH,   col_params)
        conn.commit()
    finally:
        conn.close()

    pii = sum(tp.pii_column_count for tp in batch_profiles)
    return len(batch_profiles), len(col_params), pii


# ── Public batch functions ─────────────────────────────────────────────────────

def start_batch_profiling(
    source_id: int,
    user_id: str,
    batch_size: int = 50,
) -> ProfilingBatchState | None:
    """Create a new profiling snapshot and compute the priority-sorted table order.

    Returns a ProfilingBatchState with status=RUNNING and next_table_index=0.
    Call continue_batch_profiling repeatedly until status=COMPLETE.
    """
    iconn = get_connection()
    try:
        source_row = iconn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        iconn.close()

    if source_row is None:
        return None

    iconn = get_connection()
    try:
        snap_row = iconn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        iconn.close()

    if snap_row is None:
        raise ValueError(
            "No schema snapshot found. Run POST /sources/{id}/discover first."
        )

    schema_snapshot_id = snap_row["id"]
    snapshot = _reconstruct_snapshot(json.loads(snap_row["snapshot_json"]), source_id)

    config = ProfilingConfig(
        mode=ProfilingMode.FULL,
        max_tables=10,
        max_column_count=300,
        excluded_prefixes=['stg_', 'tmp_', 'temp_', 'bak_', 'backup_', 'arc_', 'old_'],
    )

    sorted_fqns     = _compute_sorted_table_order(snapshot, config)
    statistical_fqns = sorted_fqns[:config.max_tables]

    plan = json.dumps({
        "sorted_fqns":      sorted_fqns,
        "statistical_fqns": statistical_fqns,
        "config_max_col":   config.max_column_count,
        "config_row_limit": config.row_limit_for_full,
    })

    now = _now()
    iconn = get_connection()
    try:
        snap_version = iconn.execute(
            "SELECT COALESCE(MAX(snapshot_version), 0) + 1 "
            "FROM profiling_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]

        cursor = iconn.execute(
            """INSERT INTO profiling_snapshots (
                source_id, schema_snapshot_id, snapshot_version,
                mode, sample_rate, profiling_rules_version, status,
                tables_total, batch_size, next_table_index,
                resumable_state_json, started_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_id, schema_snapshot_id, snap_version,
                ProfilingMode.FULL.value, config.sample_rate,
                '4.0.0', ProfilingStatus.RUNNING.value,
                len(sorted_fqns), batch_size, 0,
                plan, now, now,
            ),
        )
        iconn.commit()
        profiling_snapshot_id = cursor.lastrowid
    finally:
        iconn.close()

    return ProfilingBatchState(
        profiling_snapshot_id=profiling_snapshot_id,
        next_table_index=0,
        total_tables=len(sorted_fqns),
        completed_tables=0,
        statistical_tables_completed=0,
        structural_tables_completed=0,
        status=ProfilingStatus.RUNNING,
    )


def continue_batch_profiling(
    source_id: int,
    user_id: str,
    profiling_snapshot_id: int,
) -> ProfilingBatchState | None:
    """Process the next batch of tables for an in-progress profiling snapshot.

    Returns the updated ProfilingBatchState.  Call repeatedly until status=COMPLETE.
    Returns None if the snapshot is not found or not owned by user.
    """
    # ── Load snapshot state ────────────────────────────────────────────────────
    iconn = get_connection()
    try:
        owns = iconn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        psnap = iconn.execute(
            "SELECT * FROM profiling_snapshots WHERE id = ? AND source_id = ?",
            (profiling_snapshot_id, source_id),
        ).fetchone()
    finally:
        iconn.close()

    if psnap is None:
        return None

    # Already complete — return final state immediately (idempotent)
    if psnap["status"] == ProfilingStatus.COMPLETE.value:
        return _batch_state_from_row(psnap)

    plan            = json.loads(psnap["resumable_state_json"] or "{}")
    sorted_fqns     = plan.get("sorted_fqns", [])
    statistical_fqns = set(plan.get("statistical_fqns", []))
    max_col         = plan.get("config_max_col", 300)
    row_limit       = plan.get("config_row_limit", 1_000_000)
    next_idx        = psnap["next_table_index"]
    batch_sz        = psnap["batch_size"] or 50
    schema_snap_id  = psnap["schema_snapshot_id"]

    batch_fqns = sorted_fqns[next_idx : next_idx + batch_sz]
    if not batch_fqns:
        return _batch_state_from_row(psnap)

    # ── Load schema snapshot ───────────────────────────────────────────────────
    iconn = get_connection()
    try:
        schema_row = iconn.execute(
            "SELECT snapshot_json FROM schema_snapshots WHERE id = ?",
            (schema_snap_id,),
        ).fetchone()
    finally:
        iconn.close()

    snapshot     = _reconstruct_snapshot(json.loads(schema_row["snapshot_json"]), source_id)
    table_map    = {t.table_fqn: t for s in snapshot.schemas for t in s.tables}
    ref_idx      = _build_referenced_by_index(snapshot)

    # ── Open live connection only if this batch contains statistical tables ─────
    needs_live = any(fqn in statistical_fqns for fqn in batch_fqns)
    live_conn  = None

    if needs_live:
        iconn2 = get_connection()
        try:
            source_row = iconn2.execute(
                "SELECT source_type, encrypted_config_json "
                "FROM data_source_connections WHERE id = ?",
                (source_id,),
            ).fetchone()
        finally:
            iconn2.close()

        try:
            params = json.loads(
                get_secret_manager().decrypt_secret(source_row["encrypted_config_json"])
            )
        except Exception:
            logger.error("Failed to decrypt config for source id=%s", source_id)
            raise RuntimeError("Failed to load connection configuration.")

        batch_source_type = source_row["source_type"]
        batch_connector_cls = get_connector(batch_source_type)
        if batch_connector_cls is None:
            raise ValueError(
                f"No connector registered for source_type '{batch_source_type}'."
            )
        builder = batch_connector_cls().get_query_builder()
        live_conn = batch_connector_cls().open_connection(
            DataSourceConfig(source_type=batch_source_type, params=params)
        )

    # ── Profile the batch ──────────────────────────────────────────────────────
    batch_profiles = []
    stat_count = 0
    struct_count = 0

    try:
        for fqn in batch_fqns:
            table = table_map.get(fqn)
            if table is None:
                continue   # table no longer in schema — skip

            ref_count = ref_idx.get(fqn, 0)
            tp        = _profile_table_structural(table, source_id, profiling_snapshot_id, ref_count)

            is_stat = (
                fqn in statistical_fqns
                and (max_col == 0 or len(table.columns) <= max_col)
                and (row_limit == 0
                     or tp.estimated_row_count is None
                     or tp.estimated_row_count <= row_limit)
            )

            if is_stat and live_conn is not None:
                _run_statistical_pass(live_conn, tp, ProfilingConfig(mode=ProfilingMode.FULL), builder)
                _classify_table(tp)
                stat_count += 1
            else:
                struct_count += 1

            _classify_columns(tp)
            batch_profiles.append(tp)
    finally:
        if live_conn is not None:
            live_conn.close()

    # ── Persist batch + update snapshot ───────────────────────────────────────
    tables_saved, cols_saved, pii_saved = _save_batch_profiles(profiling_snapshot_id, batch_profiles)

    new_next   = next_idx + len(batch_fqns)
    is_done    = new_next >= len(sorted_fqns)
    new_status = ProfilingStatus.COMPLETE.value if is_done else ProfilingStatus.RUNNING.value
    now        = _now()

    iconn = get_connection()
    try:
        iconn.execute(
            """UPDATE profiling_snapshots SET
                next_table_index   = ?,
                tables_profiled    = COALESCE(tables_profiled, 0) + ?,
                columns_profiled   = COALESCE(columns_profiled, 0) + ?,
                pii_columns_found  = COALESCE(pii_columns_found, 0) + ?,
                classifications_complete = COALESCE(classifications_complete, 0) + ?,
                status             = ?,
                completed_at       = CASE WHEN ? = 'COMPLETE' THEN ? ELSE completed_at END
            WHERE id = ?""",
            (
                new_next, tables_saved, cols_saved, pii_saved, tables_saved,
                new_status, new_status, now,
                profiling_snapshot_id,
            ),
        )
        iconn.commit()

        # Read back final counts for the state object
        updated = iconn.execute(
            "SELECT * FROM profiling_snapshots WHERE id = ?",
            (profiling_snapshot_id,),
        ).fetchone()
    finally:
        iconn.close()

    # Count statistical vs structural from DB for accurate cumulative totals
    iconn = get_connection()
    try:
        depth_row = iconn.execute(
            """SELECT
                SUM(CASE WHEN profiling_depth = 'STATISTICAL' THEN 1 ELSE 0 END) as stat_c,
                SUM(CASE WHEN profiling_depth = 'STRUCTURAL_ONLY' THEN 1 ELSE 0 END) as struct_c
               FROM profiling_table_profiles
               WHERE profiling_snapshot_id = ?""",
            (profiling_snapshot_id,),
        ).fetchone()
    finally:
        iconn.close()

    return ProfilingBatchState(
        profiling_snapshot_id=profiling_snapshot_id,
        next_table_index=new_next,
        total_tables=len(sorted_fqns),
        completed_tables=updated["tables_profiled"] or 0,
        statistical_tables_completed=depth_row[0] or 0,
        structural_tables_completed=depth_row[1] or 0,
        status=ProfilingStatus(new_status),
    )


def _batch_state_from_row(psnap) -> ProfilingBatchState:
    """Build ProfilingBatchState from a profiling_snapshots DB row."""
    plan = json.loads(psnap["resumable_state_json"] or "{}")
    return ProfilingBatchState(
        profiling_snapshot_id=psnap["id"],
        next_table_index=psnap["next_table_index"] or 0,
        total_tables=psnap["tables_total"] or 0,
        completed_tables=psnap["tables_profiled"] or 0,
        status=ProfilingStatus(psnap["status"]),
    )
