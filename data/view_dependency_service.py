"""
Authoritative SQL Server view-dependency discovery — SQL rebuild Phase 5,
Step 6.

Discovers same-database view -> table/view dependencies from SQL Server's
own compiled dependency catalog (sys.sql_expression_dependencies via
core.profiling.sql.mssql.build_view_dependency_query()) — never by parsing
view SQL text with regex and never by asking an LLM to infer them.

This is deliberately a SEPARATE, additive discovery mechanism, not a
rewrite of data/view_semantics_service.py: that module's regex-based
view-definition parsing (data.semantic_contract_service._extract_all_
referenced_tables) still runs for its own distinct purpose — view
RESTRICTION detection and curated-quality scoring, which this module does
not attempt — and this module's own rows persist alongside its
VIEW_DEPENDENCY rows through the exact same reused mechanism and the exact
same idempotent unique index, so a (view, target) pair either mechanism
has already recorded for this snapshot is silently skipped by the other,
never duplicated. Kept apart specifically because "catalog-authoritative"
and "regex-best-effort" are different EVIDENCE QUALITIES that deserve
distinct inference_method provenance (see _INFERENCE_METHOD below). As of
SQL rebuild Phase 5, Step 6B, core.sqlservice.retrieval_documents.
classify_relationship_row() confirms THIS module's rows via
inference_method='catalog_view_dependency' explicitly — data/view_
semantics_service.py's own regex-derived rows now persist relationship_
status='PENDING' explicitly and are never confirmed by status alone.

Reused, not duplicated:
  - data.relationship_service.persist_relationships() — the same
    idempotent INSERT OR IGNORE persistence table_relationships already
    uses for declared FKs and data/view_semantics_service.py's own
    view-definition-derived edges.
  - data.datasource_service.get_connection_config() — the same decrypted-
    connection resolution every other live-probe already uses.
  - core.connectors.registry — the same connector self-registration /
    open_connection() pattern core.sqlservice.sql_execution.py's own
    default connection factory already uses.
  - core.profiling.sql.mssql.build_view_dependency_query() — pure SQL
    text construction, no execution, matching that module's own existing
    scope/convention exactly.

Never raises: a failure at any stage (ownership, snapshot resolution,
connection, query execution, persistence) is caught, classified into one
of this module's own typed statuses, and logged with only source_id and
the exception's TYPE name — never str(exc)/repr(exc), never a traceback,
never the SQL text, never a connection string or credential. This module
never claims dependencies are ready when discovery actually failed — a
non-OK status always means zero rows were persisted this run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "STATUS_NOT_MSSQL",
    "STATUS_NO_SNAPSHOT",
    "STATUS_CONNECTION_FAILED",
    "STATUS_QUERY_FAILED",
    "ViewDependencyDiscoveryResult",
    "discover_and_persist_view_dependencies",
]

STATUS_OK = "OK"
STATUS_UNOWNED = "UNOWNED"
STATUS_NOT_MSSQL = "NOT_MSSQL"                # authoritative catalog discovery is SQL-Server-specific
STATUS_NO_SNAPSHOT = "NO_SNAPSHOT"
STATUS_CONNECTION_FAILED = "CONNECTION_FAILED"
STATUS_QUERY_FAILED = "QUERY_FAILED"

# Distinct from data/view_semantics_service.py's "view_dependency" — this
# provenance means "resolved by SQL Server's own compiled dependency
# catalog", a strictly stronger evidence class than that module's
# regex-over-view-SQL-text parsing. SQL rebuild Phase 5, Step 6B:
# core.sqlservice.retrieval_documents.classify_relationship_row() now
# confirms these rows via inference_method='catalog_view_dependency'
# explicitly (a named, reviewed, authoritative provenance) — never via
# relationship_status alone, which is set to 'AUTO' below purely because
# that is this mechanism's own genuinely correct, intentional status
# (a live, read-only, deterministic catalog query — no heuristic, no
# regex, no LLM), passed explicitly rather than left to persist_
# relationships()'s DEFAULT-mirroring fallback.
_INFERENCE_METHOD = "catalog_view_dependency"
_RELATIONSHIP_TYPE = "VIEW_DEPENDENCY"
_RELATIONSHIP_STATUS = "AUTO"
# Authoritative catalog metadata — SQL Server's own resolved compiled
# dependency graph, not a heuristic guess — so this is scored at parity
# with a declared FOREIGN_KEY's own 1.0, never at the lower confidence a
# best-effort regex/text-parse would warrant.
_CONFIDENCE = 1.0

_REF_TYPE_TABLE = "U"  # sys.objects.type: user table
_REF_TYPE_VIEW = "V"   # sys.objects.type: view


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ViewDependencyDiscoveryResult:
    """
    Result of one discover_and_persist_view_dependencies() call.

    status: STATUS_OK | STATUS_UNOWNED | STATUS_NOT_MSSQL |
        STATUS_NO_SNAPSHOT | STATUS_CONNECTION_FAILED | STATUS_QUERY_FAILED.
        Only STATUS_OK means the catalog query actually ran and its rows
        (if any) were persisted — every other status means zero rows were
        discovered or persisted this run; `error` carries a safe, generic
        reason (never a credential, connection string, or raw exception
        message).
    view_table_edges / view_view_edges: DISTINCT (view, target) pairs found
        this run, split by the target's catalog type — before persistence
        idempotency (a pair already recorded by an earlier run of either
        this mechanism or data/view_semantics_service.py is still counted
        here as "found", but persist_relationships' own INSERT OR IGNORE
        means it contributes 0 to `edges_persisted`).
    views_with_dependencies: distinct views represented in this run's
        discovered edges.
    edges_persisted: rows persist_relationships() actually inserted this
        run (0 on a pure repeat run — idempotent by design).
    """

    status: str
    source_id: int
    snapshot_id: int | None
    view_table_edges: int
    view_view_edges: int
    views_with_dependencies: int
    edges_persisted: int
    error: str | None


def _empty_result(status: str, source_id: int, snapshot_id: int | None, error: str | None) -> ViewDependencyDiscoveryResult:
    return ViewDependencyDiscoveryResult(
        status=status, source_id=source_id, snapshot_id=snapshot_id,
        view_table_edges=0, view_view_edges=0, views_with_dependencies=0,
        edges_persisted=0, error=error,
    )


def _verify_source_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _resolve_latest_snapshot_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row is not None else None


def _rows_to_dependency_dicts(rows, *, source_id: int, snapshot_id: int, now: str) -> list[dict]:
    """
    Transform catalog rows (view_schema, view_name, ref_schema, ref_name,
    ref_type) into the existing table_relationships row shape — the exact
    same dict fields data/relationship_service.persist_relationships()
    already expects from every other caller. No column-level detail is
    available from sys.sql_expression_dependencies for an ordinary view
    reference, so from_column/to_column are the generic "*" sentinel
    (never a specific, invented column name) meaning "the whole object,
    no particular column" — never an empty string. An empty string is
    falsy, and core.sqlservice.retrieval_documents.build_retrieval_
    documents()'s own relationship-row loop requires both from_column and
    to_column to be truthy before it will accept a row at all (`if not
    (from_fqn and from_col and to_col and to_table_fqn): continue`) — an
    empty-string column would make this step's own edges silently vanish
    before ever reaching a retrieval document's metadata, defeating this
    step's entire purpose. "*" is also required by the SAME module's
    confirmed_relationship_targets/sources descriptor format ("schema.
    table.column", parsed by rpartition(".") in both
    core.sqlservice.schema_context_retrieval._relationship_candidate_fqns
    and core.sqlservice.relationship_graph — an object_fqn with no column
    suffix at all would have its own last dot-segment misparsed as the
    "column", corrupting the recovered object identity).
    """
    dependency_rows: list[dict] = []
    for row in rows:
        view_schema = row["view_schema"]
        view_name = row["view_name"]
        ref_schema = row["ref_schema"]
        ref_name = row["ref_name"]
        from_table_fqn = f"{view_schema}.{view_name}"
        to_table_fqn = f"{ref_schema}.{ref_name}"
        dependency_rows.append({
            "source_id": source_id, "snapshot_id": snapshot_id,
            "from_schema": view_schema, "from_table": view_name,
            "from_table_fqn": from_table_fqn, "from_column": "*",
            "to_schema": ref_schema, "to_table": ref_name,
            "to_table_fqn": to_table_fqn, "to_column": "*",
            "relationship_name": None, "relationship_type": _RELATIONSHIP_TYPE,
            "inference_method": _INFERENCE_METHOD,
            "relationship_status": _RELATIONSHIP_STATUS,
            "confidence": _CONFIDENCE,
            "evidence_json": json.dumps({
                "source": "sys.sql_expression_dependencies", "snapshot_id": snapshot_id,
            }),
            "created_at": now,
        })
    return dependency_rows


def discover_and_persist_view_dependencies(
    source_id: int,
    user_id: str,
    snapshot_id: int | None = None,
    *,
    connect_timeout_seconds: int = 15,
    query_timeout_seconds: int = 15,
) -> ViewDependencyDiscoveryResult:
    """
    Discover this source's same-database view -> table/view dependencies
    directly from the live SQL Server's own compiled dependency catalog,
    and persist them as confirmed VIEW_DEPENDENCY relationships for the
    exact (source_id, snapshot_id) pair, idempotently.

    Order of operations (cheapest/safest checks first — a live connection
    is opened only after every check before it has passed):
      1. Ownership check.
      2. Resolve snapshot_id (the caller's, or the latest for this source)
         — STATUS_NO_SNAPSHOT if none exists yet.
      3. source_type must be 'mssql' — STATUS_NOT_MSSQL otherwise; this
         mechanism is authoritative-catalog-specific and never falls back
         to a weaker heuristic for another dialect.
      4. Open exactly one live, read-only connection via the registered
         'mssql' connector's own open_connection() (never a parallel
         connection system) and run exactly one bounded, parameter-free,
         read-only catalog query — STATUS_CONNECTION_FAILED /
         STATUS_QUERY_FAILED on any failure, with the connection closed in
         a finally block regardless of outcome.
      5. Persist via data.relationship_service.persist_relationships() —
         atomic (a single transaction, matching that function's own
         behavior), idempotent (INSERT OR IGNORE against the existing
         unique index), and additive-only: never deletes or modifies any
         existing FK row or any other relationship_type/inference_method.

    Never raises. Never logs a credential, connection string, SQL
    definition text, or raw exception message/args — only source_id, the
    exception's type name, and this module's own fixed, generic status
    labels.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return _empty_result(STATUS_UNOWNED, source_id, None, "source not found or not owned by this user")

        resolved_snapshot_id = snapshot_id if snapshot_id is not None else _resolve_latest_snapshot_id(conn, source_id)
        if resolved_snapshot_id is None:
            return _empty_result(STATUS_NO_SNAPSHOT, source_id, None, "no schema snapshot exists for this source yet")

        source_row = conn.execute(
            "SELECT source_type FROM data_source_connections WHERE id = ?", (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if source_row is None or source_row["source_type"] != "mssql":
        return _empty_result(
            STATUS_NOT_MSSQL, source_id, resolved_snapshot_id,
            "authoritative catalog view-dependency discovery is only available for SQL Server sources",
        )

    from data.datasource_service import get_connection_config

    config_dict = get_connection_config(source_id, user_id)
    if config_dict is None:
        return _empty_result(STATUS_CONNECTION_FAILED, source_id, resolved_snapshot_id, "connection configuration unavailable")

    try:
        import core.connectors.registry as registry
        from core.connectors.base import DataSourceConfig

        connector_cls = registry.get(config_dict["source_type"])
        if connector_cls is None:
            return _empty_result(STATUS_CONNECTION_FAILED, source_id, resolved_snapshot_id, "no connector registered for this source type")

        data_source_config = DataSourceConfig(source_type=config_dict["source_type"], params=config_dict["params"])
        live_connection = connector_cls().open_connection(data_source_config, timeout_s=connect_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — never leaks credentials/driver text; type name only
        logger.warning(
            "discover_and_persist_view_dependencies: connection failed for source_id=%s (exception_type=%s).",
            source_id, type(exc).__name__,
        )
        return _empty_result(STATUS_CONNECTION_FAILED, source_id, resolved_snapshot_id, "could not open a live connection")

    try:
        from core.profiling.sql.mssql import build_view_dependency_query

        cursor = live_connection.cursor()
        try:
            cursor.timeout = query_timeout_seconds
        except Exception:  # noqa: BLE001 — best-effort; not every DBAPI cursor exposes .timeout
            pass
        cursor.execute(build_view_dependency_query())
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001 — never leaks SQL text or driver exception content
        logger.warning(
            "discover_and_persist_view_dependencies: catalog query failed for source_id=%s (exception_type=%s).",
            source_id, type(exc).__name__,
        )
        return _empty_result(STATUS_QUERY_FAILED, source_id, resolved_snapshot_id, "catalog query failed")
    finally:
        try:
            live_connection.close()
        except Exception:  # noqa: BLE001 — never let a close-time driver error mask the real outcome
            pass

    # Defense-in-depth de-duplication on the exact (view, target) pair,
    # independent of the catalog query's own SELECT DISTINCT — this
    # function's reported counts and persisted rows are never inflated by
    # a duplicate regardless of what a given driver/server actually
    # returns. First occurrence wins; order is otherwise preserved.
    deduped_rows: list[dict] = []
    seen_pairs: set[tuple] = set()
    for r in rows:
        pair = (r.get("view_schema"), r.get("view_name"), r.get("ref_schema"), r.get("ref_name"))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        deduped_rows.append(r)

    view_table_rows = [r for r in deduped_rows if r.get("ref_type", "").strip() == _REF_TYPE_TABLE]
    view_view_rows = [r for r in deduped_rows if r.get("ref_type", "").strip() == _REF_TYPE_VIEW]
    distinct_views = {f"{r['view_schema']}.{r['view_name']}" for r in deduped_rows}

    now = _now()
    dependency_rows = _rows_to_dependency_dicts(deduped_rows, source_id=source_id, snapshot_id=resolved_snapshot_id, now=now)

    edges_persisted = 0
    if dependency_rows:
        from data.relationship_service import persist_relationships

        conn = get_connection()
        try:
            edges_persisted = persist_relationships(conn, resolved_snapshot_id, source_id, dependency_rows)
        finally:
            conn.close()

    return ViewDependencyDiscoveryResult(
        status=STATUS_OK, source_id=source_id, snapshot_id=resolved_snapshot_id,
        view_table_edges=len(view_table_rows), view_view_edges=len(view_view_rows),
        views_with_dependencies=len(distinct_views), edges_persisted=edges_persisted, error=None,
    )
