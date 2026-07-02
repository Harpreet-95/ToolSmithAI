import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.connectors.schema import ColumnInfo, SchemaSnapshot, TableInfo
from core.profiling.sql.base import ProfilingQueryBuilder
from core.dictionary.pii_detector import detect_pii
from core.profiling.classification.column_typer import classify_column
from core.profiling.classification.table_classifier import classify_table
from core.profiling.execution import (
    profile_column_percentiles, profile_column_statistics, profile_sample_values,
    profile_table_statistics, profile_top_values,
)
from core.profiling.models import (
    ColumnProfile, ConfidenceScore, DataCurrency, ProfilingConfig,
    ProfilingDepth, ProfilingMode, ProfilingSnapshot, ProfilingStatus,
    RowCountTier, SemanticType, TableProfile,
)

_RULES_VERSION = '4.0.0'

# Regex to identify temporally-named columns as the primary date column for a table.
_DATE_COL_RE = re.compile(
    r'creat|modif|updat|insert|transact|event|record|log|timestamp|dt\b',
    re.IGNORECASE,
)


# ── Result container ───────────────────────────────────────────────────────────

@dataclass
class ProfilingRunResult:
    snapshot:              ProfilingSnapshot
    table_profiles:        list[TableProfile]
    tables_classified:     int
    columns_classified:    int
    pii_columns_found:     int
    classification_summary: dict[str, int]   # TableClass.value → count
    semantic_type_summary:  dict[str, int]   # SemanticType.value → count
    duration_ms:           int
    tables_selected_for_full_profile: list[dict] = field(default_factory=list)
    # each entry: {table_fqn, priority_score}


# ── Private helpers ────────────────────────────────────────────────────────────

def _build_referenced_by_index(snapshot: SchemaSnapshot) -> dict[str, int]:
    """Count inbound FK references for each table_fqn across the whole snapshot."""
    counts: dict[str, int] = {}
    for schema in snapshot.schemas:
        for table in schema.tables:
            for fk in table.foreign_keys:
                target = f"{fk.to_schema}.{fk.to_table}"
                counts[target] = counts.get(target, 0) + 1
    return counts


def _row_count_tier(count: int | None) -> RowCountTier | None:
    if count is None:
        return None
    if count == 0:
        return RowCountTier.EMPTY
    if count < 1_000:
        return RowCountTier.TINY
    if count < 100_000:
        return RowCountTier.SMALL
    if count < 1_000_000:
        return RowCountTier.MEDIUM
    if count < 10_000_000:
        return RowCountTier.LARGE
    return RowCountTier.VERY_LARGE


def _primary_date_column(columns: list[ColumnInfo]) -> str | None:
    """Return the name of the most likely primary date column, or None."""
    date_cols = [c for c in columns if c.data_type == 'DATETIME']
    if not date_cols:
        return None
    for c in date_cols:
        if _DATE_COL_RE.search(c.column_name):
            return c.column_name
    return date_cols[0].column_name


# ── Structural profiling ───────────────────────────────────────────────────────

def _profile_column_structural(
    col: ColumnInfo,
    table_fqn: str,
    source_id: int,
    snapshot_id: int,
) -> ColumnProfile:
    """Build a ColumnProfile from schema metadata alone — no SQL executed."""
    pii = detect_pii(col.column_name, col.data_type)
    return ColumnProfile(
        source_id=source_id,
        profiling_snapshot_id=snapshot_id,
        table_fqn=table_fqn,
        column_name=col.column_name,
        data_type=col.data_type,
        raw_type=col.raw_type,
        is_nullable=col.is_nullable,
        is_primary_key=col.is_primary_key,
        is_identity=col.is_identity,
        ordinal_position=col.ordinal_position,
        pii_name_heuristic=pii,
        profiling_depth=ProfilingDepth.STRUCTURAL_ONLY,
        profiling_status=ProfilingStatus.COMPLETE,
    )


def _profile_table_structural(
    table: TableInfo,
    source_id: int,
    snapshot_id: int,
    referenced_by_count: int,
) -> TableProfile:
    """Build a TableProfile from schema metadata alone — no SQL executed."""
    fk_count     = len(table.foreign_keys)
    col_count    = len(table.columns)
    pk_count     = len(table.primary_keys)
    has_identity = any(c.is_identity for c in table.columns)

    # A junction table connects two entities via exactly two FKs with minimal
    # additional columns (timestamp or synthetic PK are common extras).
    is_junction = fk_count >= 2 and col_count <= (fk_count + 2)

    date_col = _primary_date_column(table.columns)

    tp = TableProfile(
        source_id=source_id,
        profiling_snapshot_id=snapshot_id,
        table_fqn=table.table_fqn,
        table_name=table.table_name,
        schema_name=table.schema_name,
        table_type=table.table_type,
        estimated_row_count=table.row_count_estimate,
        row_count_tier=_row_count_tier(table.row_count_estimate),
        has_date_column=date_col is not None,
        date_column_name=date_col,
        column_count=col_count,
        pk_column_count=pk_count,
        fk_count=fk_count,
        referenced_by_count=referenced_by_count,
        is_junction_table=is_junction,
        is_root_table=fk_count == 0,
        is_leaf_table=referenced_by_count == 0,
        has_identity_column=has_identity,
        profiling_depth=ProfilingDepth.STRUCTURAL_ONLY,
        profiling_status=ProfilingStatus.COMPLETE,
        profiled_at=datetime.now(timezone.utc).isoformat(),
    )

    for col in table.columns:
        cp = _profile_column_structural(col, table.table_fqn, source_id, snapshot_id)
        tp.column_profiles.append(cp)

    tp.pii_column_count = sum(1 for cp in tp.column_profiles if cp.pii_name_heuristic)

    return tp


# ── Table priority scoring ────────────────────────────────────────────────────

_DEPRIORITY_PREFIXES = ('_', 'tmp_', 'temp_', 'stg_', 'bak_', 'old_', 'test_')


def _score_table_priority(
    table: TableInfo,
    referenced_by_count: int,
    tp: TableProfile,
) -> int:
    """Score a table's business importance to select which tables receive live profiling.

    Higher score = more important = profiled first when max_tables is set.
    """
    score = 0
    cls = tp.classification.classification if tp.classification else None
    tname = table.table_name.lower()

    # Classification bonuses / penalties
    if cls == 'Master':
        score += 50
    elif cls == 'Transactional':
        score += 40
    elif cls == 'Reference':
        score += 25
    elif cls == 'Audit':
        score -= 25

    # Structural connectivity signals
    if referenced_by_count >= 5:
        score += 20
    if tp.fk_count > 0:
        score += 15
    if tp.has_date_column:
        score += 10
    if tp.pk_column_count > 0:
        score += 10

    # Name-based demotion
    if any(tname.startswith(pfx) for pfx in _DEPRIORITY_PREFIXES):
        score -= 50
    if 'migration' in tname or '__migrationhistory' in tname:
        score -= 25
    if 'hookdata' in tname or 'hook_data' in tname:
        score -= 25

    return score


# ── Classification passes ──────────────────────────────────────────────────────

def _classify_table(tp: TableProfile) -> None:
    """Run table classification and attach result to the profile in-place."""
    tp.classification = classify_table(tp)


def _classify_columns(tp: TableProfile) -> None:
    """Run column semantic typing and attach results to each profile in-place."""
    for cp in tp.column_profiles:
        result: ConfidenceScore = classify_column(cp)
        cp.semantic_type       = SemanticType(result.classification)
        cp.semantic_confidence = result.confidence
        cp.semantic_evidence   = list(result.evidence)


# ── Statistical profiling pass ────────────────────────────────────────────────

def _run_statistical_pass(
    conn,
    tp: TableProfile,
    config: ProfilingConfig,
    query_builder: ProfilingQueryBuilder,
) -> None:
    """Execute live SQL queries against the source and enrich all profiles in-place.

    Order: table stats (row count, date range) → per-column stats → top values
    → sample values.  Classification is deliberately deferred to after this pass
    so classifiers receive the full evidence set.
    """
    profile_table_statistics(conn, tp, config, query_builder)

    for cp in tp.column_profiles:
        profile_column_statistics(conn, cp, config, query_builder)
        profile_column_percentiles(conn, cp, config, query_builder)
        profile_top_values(conn, cp, config, query_builder)
        profile_sample_values(conn, cp, config, query_builder)


# ── Public API ─────────────────────────────────────────────────────────────────

def run_profiling(
    source_id: int,
    schema_snapshot_id: int,
    config: ProfilingConfig,
    snapshot: SchemaSnapshot,
    connection=None,
    query_builder: ProfilingQueryBuilder | None = None,
) -> ProfilingRunResult:
    """Execute a profiling pass over a SchemaSnapshot.

    STRUCTURAL_ONLY (default / connection=None):
        Schema metadata only.  No SQL is executed against the source database.
        query_builder is not required and may be None.

    FULL / SAMPLED (connection + query_builder required):
        Structural pass → statistical pass (live SQL) → classification refresh.
        Raises ValueError if connection or query_builder is None.

    The caller (service layer) is responsible for persisting the result.
    """
    is_statistical = config.mode not in (ProfilingMode.STRUCTURAL_ONLY,)
    if is_statistical and connection is None:
        raise ValueError(
            f"A live connection is required for ProfilingMode.{config.mode.name}. "
            "Pass a DBAPI2-compatible connection or use ProfilingMode.STRUCTURAL_ONLY."
        )
    if is_statistical and query_builder is None:
        raise ValueError(
            f"A query_builder is required for ProfilingMode.{config.mode.name}. "
            "Pass a ProfilingQueryBuilder or use ProfilingMode.STRUCTURAL_ONLY."
        )
    t0         = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    referenced_by = _build_referenced_by_index(snapshot)
    all_tables    = [t for s in snapshot.schemas for t in s.tables]

    # ── Phase 1: apply exclusions, build structural profiles, initial classification
    # Table classification runs first so priority scoring has a class signal.
    candidates: list[tuple[int, TableInfo, TableProfile]] = []

    for table in all_tables:
        if table.schema_name in config.excluded_schemas:
            continue
        if any(table.table_name.lower().startswith(p.lower())
               for p in config.excluded_prefixes):
            continue
        if table.table_fqn in config.excluded_table_fqns:
            continue

        ref_count = referenced_by.get(table.table_fqn, 0)
        tp        = _profile_table_structural(table, source_id, schema_snapshot_id, ref_count)
        _classify_table(tp)

        score = _score_table_priority(table, ref_count, tp) if is_statistical else 0
        candidates.append((score, table, tp))

    # ── Phase 2: sort by priority score and determine live-profiling set ──────
    selected_for_full: list[dict] = []
    live_fqns: set[str] = set()

    if is_statistical:
        candidates.sort(key=lambda x: -x[0])   # highest priority first

        eligible = []
        for score, table, tp in candidates:
            if config.priority_tables and table.table_fqn not in config.priority_tables:
                continue
            over_col = (config.max_column_count > 0
                        and len(table.columns) > config.max_column_count)
            over_row = (config.row_limit_for_full > 0
                        and tp.estimated_row_count is not None
                        and tp.estimated_row_count > config.row_limit_for_full)
            if not over_col and not over_row:
                eligible.append((score, table, tp))

        limit = config.max_tables if config.max_tables > 0 else len(eligible)
        for score, table, tp in eligible[:limit]:
            live_fqns.add(table.table_fqn)
            selected_for_full.append({"table_fqn": table.table_fqn, "priority_score": score})

    # ── Phase 3: statistical profiling for selected tables; column classification
    table_profiles: list[TableProfile] = []

    for _score, table, tp in candidates:
        if table.table_fqn in live_fqns:
            # Enrich with live statistics; re-run table classification with richer evidence.
            _run_statistical_pass(connection, tp, config, query_builder)
            _classify_table(tp)
        # Column classification runs once, after any statistical enrichment.
        _classify_columns(tp)
        table_profiles.append(tp)

    duration_ms = int((time.monotonic() - t0) * 1000)

    # ── Summary aggregation ────────────────────────────────────────────────────

    pii_total         = sum(tp.pii_column_count for tp in table_profiles)
    tables_classified = sum(1 for tp in table_profiles if tp.classification is not None)
    cols_classified   = sum(
        sum(1 for cp in tp.column_profiles if cp.semantic_type is not None)
        for tp in table_profiles
    )

    cls_summary: dict[str, int] = {}
    for tp in table_profiles:
        if tp.classification:
            key = tp.classification.classification
            cls_summary[key] = cls_summary.get(key, 0) + 1

    sem_summary: dict[str, int] = {}
    for tp in table_profiles:
        for cp in tp.column_profiles:
            if cp.semantic_type is not None:
                key = cp.semantic_type.value
                sem_summary[key] = sem_summary.get(key, 0) + 1

    snap = ProfilingSnapshot(
        source_id=source_id,
        schema_snapshot_id=schema_snapshot_id,
        snapshot_version=1,              # service layer sets the real version on persist
        mode=config.mode,
        sample_rate=config.sample_rate,
        profiling_rules_version=_RULES_VERSION,
        status=ProfilingStatus.COMPLETE,
        tables_total=len(all_tables),
        tables_profiled=len(table_profiles),
        tables_skipped=0,
        tables_failed=0,
        columns_total=sum(tp.column_count for tp in table_profiles),
        columns_profiled=cols_classified,
        pii_columns_found=pii_total,
        classifications_complete=tables_classified,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=duration_ms // 1000,
    )

    return ProfilingRunResult(
        snapshot=snap,
        table_profiles=table_profiles,
        tables_classified=tables_classified,
        columns_classified=cols_classified,
        pii_columns_found=pii_total,
        classification_summary=cls_summary,
        semantic_type_summary=sem_summary,
        duration_ms=duration_ms,
        tables_selected_for_full_profile=selected_for_full,
    )
