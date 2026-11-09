"""
View Semantic Discovery milestone.

At schema-discovery time, VIEW-type tables already get full column-level
metadata (core/connectors/relational/mssql.py already returns real columns
for views) — what's missing is their SQL definition and the base tables
they actually read from. This module discovers and persists both, reusing
existing primitives end to end:

  - data.investigation_service.inspect_targeted_values(..., investigation_type
    ="view_definition") — the existing dialect-aware (MSSQL/PostgreSQL/MySQL/
    SQLite) live-probe primitive already used defensively by the restricted-
    view-safety mechanism in data.semantic_contract_service — reused here as
    a connect-time, persisted, positive evidence source instead.
  - data.semantic_contract_service._detect_view_restriction /
    _extract_all_referenced_tables — the same generic (no table/view names
    hardcoded) regex parsing already used for restriction detection, reused
    unchanged plus one new generalization (all referenced tables, not just
    the first FROM-clause table).
  - data.query_planning_service.compute_naming_penalty — the same generic
    backup/temp/test/old/dated-copy naming heuristic _score_table_authority
    already applies, reused as the "is this view curated-quality" gate so
    a view named e.g. "vw_ClientBackup_2019" never gets promoted to
    stronger-than-raw-name-guess evidence.
  - data.relationship_service.persist_relationships — the same idempotent
    INSERT OR IGNORE persistence table_relationships already uses for
    declared FKs, reused for view -> base-table dependency edges
    (relationship_type='VIEW_DEPENDENCY').

Never raises — degrades to "discovered nothing this run" on any failure,
the same best-effort contract every other connect-time bootstrap step in
data.datasource_service already has. A view whose metadata (dictionary/
profiling context) doesn't exist yet is silently skipped this run and
naturally picked up on the next call (e.g. after structural profiling has
run, or on the next re-discovery) rather than persisted as a false "not
curated" row.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)

# Connect-time-once budget — wider than the existing per-question restriction
# probe cap (_MAX_RESTRICTION_PROBES=3 in semantic_contract_service.py) since
# this runs once per discovery, not once per question, but still bounded so a
# very wide schema can't turn connection onboarding into an unbounded scan.
_MAX_VIEWS_PER_DISCOVERY_RUN = 50

# Structural-regex-validated-against-catalog dependency, not a declared FK
# constraint — deliberately below FOREIGN_KEY's 1.0 confidence.
_VIEW_DEPENDENCY_CONFIDENCE = 0.6

# ── Phase 7, Step 6E3B4, Part 2 — complete-definition capture ──────────────
#
# Root cause of the Step 6E3B3 TYPED_FAILURE/truncation finding: the OLD
# capture path (still used below for non-mssql dialects) reused data.
# investigation_service's generic per-object "investigation" probe, whose
# result text was sliced to a value-SAMPLING-sized cap — even though the
# underlying dialect queries (OBJECT_DEFINITION/pg_get_viewdef/
# sqlite_master.sql) already returned the complete, correct text every
# time. There was never a SQL-side truncation; see data.investigation_
# service's own _MAX_VIEW_DEFINITION_CHARS docstring for the full account.
#
# capture_complete_mssql_view_definitions() below is the replacement for
# MSSQL sources: ONE bounded catalog query across sys.views/sys.schemas/
# sys.sql_modules returns every accessible view's COMPLETE NVARCHAR(MAX)
# definition in a single round trip (never N round trips), with an
# authoritative, typed capture_status per view — distinguishing an
# encrypted definition from a permission-denied one from a genuinely empty
# one, never collapsing all three into one "no text" case the way the old
# path did.
_MAX_MSSQL_VIEWS_PER_CAPTURE = 5000
_MSSQL_CAPTURE_TIMEOUT_S = 30

CAPTURE_STATUS_AVAILABLE_COMPLETE = "AVAILABLE_COMPLETE"
CAPTURE_STATUS_ENCRYPTED = "ENCRYPTED"
CAPTURE_STATUS_INACCESSIBLE = "INACCESSIBLE_PERMISSION_DENIED"
CAPTURE_STATUS_EMPTY = "EMPTY_DEFINITION"
CAPTURE_STATUS_NOT_FOUND = "NOT_FOUND"
CAPTURE_STATUS_LEGACY_UNKNOWN = "LEGACY_UNKNOWN"
CAPTURE_STATUS_CAPTURE_FAILED = "CAPTURE_FAILED"

_MSSQL_BULK_DEFINITION_SQL = (
    "SELECT s.name AS schema_name, v.name AS view_name, "
    "m.definition AS view_definition, "
    "CAST(OBJECTPROPERTY(v.object_id, 'IsEncrypted') AS INT) AS is_encrypted "
    "FROM sys.views v "
    "JOIN sys.schemas s ON v.schema_id = s.schema_id "
    "LEFT JOIN sys.sql_modules m ON m.object_id = v.object_id "
    "ORDER BY s.name, v.name"
)


def capture_complete_mssql_view_definitions(source_id: int, user_id: str) -> dict:
    """One bounded, read-only MSSQL catalog query returning EVERY
    accessible view's complete definition for this source, keyed by
    table_fqn(lower) -> {definition_sql, definition_length,
    definition_checksum, capture_status}. Never raises, never partially
    writes anything (pure read + return) — the caller decides what to do
    with the result, exactly like data.investigation_service.inspect_
    targeted_values's own contract.

    Returns {} (never raises) when: the source isn't owned by user_id, the
    source isn't an MSSQL connector, live_query_enabled/capability is
    off, or the connection/query itself fails — every one of those is a
    normal, non-fatal "capture skipped this run, caller falls back to
    whatever is already persisted" outcome, matching every other
    connect-time bootstrap step's own best-effort contract.

    definition_sql is NEVER logged or included in any warning/log message
    here — only its length and a status code are ever written to logs.
    """
    from core.live.connection_resolver import LiveConnectionResolver
    from core.live.models import ResolutionStatus

    resolution = LiveConnectionResolver().resolve(source_id, user_id, required_capability="sql_query")
    if resolution.status != ResolutionStatus.RESOLVED:
        return {}
    context = resolution.context
    if context.source_type != "mssql":
        return {}

    try:
        db_conn = context.connector_cls().open_connection(context.config)
    except Exception:  # noqa: BLE001
        logger.warning("view_semantics_service: could not open MSSQL connection for bulk definition capture, source_id=%s", source_id)
        return {}

    from data.query_execution_service import _execute_with_timeout

    try:
        description, rows, exec_error, timed_out = _execute_with_timeout(
            db_conn, _MSSQL_BULK_DEFINITION_SQL, [], _MAX_MSSQL_VIEWS_PER_CAPTURE, _MSSQL_CAPTURE_TIMEOUT_S,
        )
    finally:
        try:
            db_conn.close()
        except Exception:  # noqa: BLE001
            pass

    if timed_out:
        logger.warning("view_semantics_service: bulk definition capture timed out after %ss, source_id=%s", _MSSQL_CAPTURE_TIMEOUT_S, source_id)
        return {}
    if exec_error or description is None or rows is None:
        logger.warning("view_semantics_service: bulk definition capture query failed, source_id=%s", source_id)
        return {}

    if len(rows) > _MAX_MSSQL_VIEWS_PER_CAPTURE:
        logger.warning(
            "view_semantics_service: bulk definition capture hit the %s-view bound, source_id=%s "
            "(some views were not captured this run)", _MAX_MSSQL_VIEWS_PER_CAPTURE, source_id,
        )
    rows = rows[:_MAX_MSSQL_VIEWS_PER_CAPTURE]

    out: dict[str, dict] = {}
    for row in rows:
        schema_name, view_name, definition, is_encrypted = row[0], row[1], row[2], row[3]
        table_fqn = f"{schema_name}.{view_name}"
        if definition is not None and str(definition).strip():
            text = str(definition)
            out[table_fqn.lower()] = {
                "table_fqn": table_fqn,
                "definition_sql": text,
                "definition_length": len(text),
                "definition_checksum": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "capture_status": CAPTURE_STATUS_AVAILABLE_COMPLETE,
            }
        elif definition is not None:
            # Present but blank/whitespace-only — a genuinely pathological
            # catalog state (sys.sql_modules row exists but carries no
            # text), never conflated with an encrypted/permission-denied
            # NULL below.
            out[table_fqn.lower()] = {
                "table_fqn": table_fqn, "definition_sql": None, "definition_length": 0,
                "definition_checksum": None, "capture_status": CAPTURE_STATUS_EMPTY,
            }
        elif is_encrypted == 1:
            out[table_fqn.lower()] = {
                "table_fqn": table_fqn, "definition_sql": None, "definition_length": None,
                "definition_checksum": None, "capture_status": CAPTURE_STATUS_ENCRYPTED,
            }
        else:
            # NULL definition, not flagged encrypted — the sys.sql_modules
            # LEFT JOIN produced no row for this object, which (for a real
            # view that does exist in sys.views) means the connected
            # principal lacks VIEW DEFINITION permission on it. Never
            # guessed to be anything more specific than this.
            out[table_fqn.lower()] = {
                "table_fqn": table_fqn, "definition_sql": None, "definition_length": None,
                "definition_checksum": None, "capture_status": CAPTURE_STATUS_INACCESSIBLE,
            }
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_views_and_catalog_from_snapshot_json(snapshot_json: str) -> tuple[list[dict], set[str]]:
    """Returns (view table dicts, set of every table_fqn in the catalog) —
    mirrors relationship_service._parse_fks_from_snapshot_json's own
    snapshot_json parsing. Never raises; returns ([], set()) on parse failure."""
    try:
        data = json.loads(snapshot_json)
    except (json.JSONDecodeError, TypeError):
        return [], set()

    views: list[dict] = []
    catalog: set[str] = set()
    for schema in data.get("schemas") or []:
        schema_name = schema.get("schema_name") or ""
        for table in schema.get("tables") or []:
            table_name = table.get("table_name") or ""
            table_fqn = table.get("table_fqn") or f"{schema_name}.{table_name}"
            catalog.add(table_fqn)
            if table.get("table_type") == "VIEW":
                views.append({
                    "table_name": table_name,
                    "schema_name": schema_name,
                    "table_fqn": table_fqn,
                })
    return views, catalog


def discover_and_persist_view_semantics(snapshot_id: int, source_id: int, user_id: str) -> dict:
    """Orchestrate discovery + idempotent persistence for one snapshot.

    Returns a summary dict ({"views_examined", "views_curated",
    "dependency_edges_persisted"}); never raises — a per-view or per-step
    failure is logged and that view is simply skipped, matching every other
    best-effort bootstrap step's contract.
    """
    summary = {"views_examined": 0, "views_curated": 0, "dependency_edges_persisted": 0}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM schema_snapshots WHERE id = ? AND source_id = ?",
            (snapshot_id, source_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return summary

    views, catalog = _parse_views_and_catalog_from_snapshot_json(row["snapshot_json"])
    if not views:
        return summary

    # A wide real schema (e.g. a real production database: hundreds of views) can exceed
    # _MAX_VIEWS_PER_DISCOVERY_RUN in a single connect. Without this,
    # every re-discovery would re-probe the SAME first N views (list order
    # is deterministic from the snapshot) and the rest of a large catalog
    # would never be reached. Already-discovered views (any prior
    # snapshot_id -- views rarely disappear) are pushed to the back of this
    # run's queue so each successive discovery call makes real forward
    # progress across the whole catalog instead of looping on one slice.
    conn = get_connection()
    try:
        already_rows = conn.execute(
            "SELECT table_fqn FROM view_semantic_evidence WHERE source_id = ?", (source_id,),
        ).fetchall()
    finally:
        conn.close()
    already_discovered = {r["table_fqn"] for r in already_rows}
    views.sort(key=lambda v: v["table_fqn"] in already_discovered)

    from data.investigation_service import inspect_targeted_values
    from data.semantic_contract_service import _detect_view_restriction, _extract_all_referenced_tables
    from data.query_planning_service import compute_naming_penalty
    from data.relationship_service import persist_relationships

    # Phase 7, Step 6E3B4, Part 2 — ONE bulk, bounded catalog round trip for
    # an MSSQL source replaces up to _MAX_VIEWS_PER_DISCOVERY_RUN individual
    # per-view connections below with complete, untruncated text (also
    # closes the pre-existing "five separate connections for hundreds of views"
    # scaling defect for this capture path specifically). Never raises;
    # returns {} for a non-mssql source or any resolution/connection
    # failure, in which case every view below transparently falls back to
    # the pre-existing per-view probe, unchanged.
    try:
        complete_definitions_by_fqn_lower = capture_complete_mssql_view_definitions(source_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("view_semantics_service: bulk MSSQL definition capture failed, source_id=%s", source_id, exc_info=True)
        complete_definitions_by_fqn_lower = {}

    now = _now()
    dependency_rows: list[dict] = []

    for view in views[:_MAX_VIEWS_PER_DISCOVERY_RUN]:
        table_fqn = view["table_fqn"]
        summary["views_examined"] += 1

        complete = complete_definitions_by_fqn_lower.get(table_fqn.lower())
        if complete is not None:
            if complete["capture_status"] != CAPTURE_STATUS_AVAILABLE_COMPLETE:
                # Typed non-text outcome (encrypted/inaccessible/empty) —
                # persist the status itself (never a guessed definition),
                # then move on; naturally retried on the next discovery run
                # in case a permission grant changes this.
                _upsert_view_semantic_evidence(
                    source_id=source_id, snapshot_id=snapshot_id, table_fqn=table_fqn,
                    definition_sql=None, referenced_tables=[], restriction=None,
                    naming_penalty_score=0.0, is_curated=False, discovered_at=now,
                    definition_capture_status=complete["capture_status"], definition_length=complete["definition_length"],
                )
                continue
            definition_sql = complete["definition_sql"]
            capture_status = CAPTURE_STATUS_AVAILABLE_COMPLETE
            definition_length = complete["definition_length"]
        else:
            try:
                result = inspect_targeted_values(
                    source_id, user_id, table_fqn, None,
                    investigation_type="view_definition",
                    reason="Connect-time view semantic discovery",
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "view_semantics_service: view_definition probe failed for table_fqn=%s", table_fqn,
                    exc_info=True,
                )
                continue

            if not result.valid or not result.sample_values:
                # Metadata not ready yet (e.g. structural profiling hasn't run
                # for a brand-new connection), or no definition text for this
                # dialect — skip; naturally retried on the next discovery run.
                continue

            definition_sql = result.sample_values[0]
            if not isinstance(definition_sql, str) or not definition_sql:
                continue
            # Pre-existing path (non-mssql dialects only, now that mssql is
            # always served by the bulk capture above when it succeeds) —
            # completeness cannot be positively confirmed here, so this is
            # explicitly labeled unverified rather than silently trusted.
            capture_status = CAPTURE_STATUS_LEGACY_UNKNOWN
            definition_length = len(definition_sql)

        restriction = _detect_view_restriction(definition_sql)
        referenced_tables = _extract_all_referenced_tables(definition_sql, known_table_fqns=catalog)
        penalty, hits = compute_naming_penalty(view["table_name"])
        is_curated = bool(referenced_tables) and not hits

        _upsert_view_semantic_evidence(
            source_id=source_id, snapshot_id=snapshot_id, table_fqn=table_fqn,
            definition_sql=definition_sql, referenced_tables=referenced_tables,
            restriction=restriction, naming_penalty_score=penalty, is_curated=is_curated,
            discovered_at=now, definition_capture_status=capture_status, definition_length=definition_length,
        )
        if is_curated:
            summary["views_curated"] += 1

        for to_fqn in referenced_tables:
            to_schema, _, to_table = to_fqn.rpartition(".")
            dependency_rows.append({
                "source_id": source_id, "snapshot_id": snapshot_id,
                "from_schema": view["schema_name"], "from_table": view["table_name"],
                # Phase 6, Step 6B: "*" (never ""), matching data/view_
                # dependency_service.py's own sentinel exactly — see that
                # module's _rows_to_dependency_dicts docstring for why an
                # empty string is actively wrong here (it's falsy, so
                # core.sqlservice.retrieval_documents' relationship-row
                # loop silently drops the edge entirely; it's also not a
                # valid "schema.table.column" descriptor suffix). Using the
                # SAME sentinel as the catalog-based writer also means both
                # mechanisms now agree on the identity of a given (view,
                # target) logical edge, so persist_relationships()'s
                # evidence-precedence upgrade logic can actually compare
                # and choose between them instead of silently creating two
                # parallel rows for the same real edge.
                "from_table_fqn": table_fqn, "from_column": "*",
                "to_schema": to_schema, "to_table": to_table, "to_table_fqn": to_fqn,
                "to_column": "*",
                "relationship_name": None, "relationship_type": "VIEW_DEPENDENCY",
                "inference_method": "view_dependency",
                # SQL rebuild Phase 5, Step 6B: explicit, never left to
                # persist_relationships()'s DEFAULT-mirroring fallback
                # ('AUTO'). This is a regex/text-parse of the view's own
                # SQL definition — a heuristic, not an authoritative
                # source — so it must remain PENDING (human-reviewable via
                # data.relationship_service.approve_relationship()) exactly
                # like every other heuristic discovery path in
                # data/relationship_service.py already does, never
                # auto-confirmed by relationship_status alone.
                "relationship_status": "PENDING",
                "confidence": _VIEW_DEPENDENCY_CONFIDENCE,
                "evidence_json": json.dumps({
                    "source": "view_definition_parse", "snapshot_id": snapshot_id,
                }),
                "created_at": now,
            })

    if dependency_rows:
        conn = get_connection()
        try:
            summary["dependency_edges_persisted"] = persist_relationships(
                conn, snapshot_id, source_id, dependency_rows,
            )
        finally:
            conn.close()

    return summary


def _upsert_view_semantic_evidence(
    *, source_id: int, snapshot_id: int, table_fqn: str, definition_sql: "str | None",
    referenced_tables: list[str], restriction: dict | None, naming_penalty_score: float,
    is_curated: bool, discovered_at: str,
    definition_capture_status: str = CAPTURE_STATUS_LEGACY_UNKNOWN,
    definition_length: "int | None" = None,
) -> None:
    """Idempotent upsert, one row per (source_id, table_fqn). definition_sql
    may be None (Phase 7, Step 6E3B4, Part 2 — an encrypted/inaccessible/
    empty view carries a typed definition_capture_status instead of a
    definition/checksum). definition_length, when not explicitly supplied,
    is always just len(definition_sql) — a cheap derived fact, never a
    second source of truth to keep in sync by hand.

    The two new columns (definition_capture_status, definition_length) are
    written defensively: a database that hasn't picked up data.models'
    additive ALTER TABLE for them yet (see that module's own migration
    list, mirroring table_relationships' identical idiom) still gets the
    unchanged, pre-6E3B4 INSERT — never a crash, matching every other
    schema-evolution fallback already in this codebase."""
    checksum = hashlib.sha256(definition_sql.encode("utf-8")).hexdigest() if definition_sql else None
    if definition_length is None:
        definition_length = len(definition_sql) if definition_sql else 0

    conn = get_connection()
    try:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(view_semantic_evidence)").fetchall()}
        has_new_cols = "definition_capture_status" in existing_cols and "definition_length" in existing_cols

        if has_new_cols:
            conn.execute(
                """
                INSERT INTO view_semantic_evidence
                    (source_id, snapshot_id, table_fqn, definition_sql, definition_checksum,
                     referenced_tables_json, restriction_json, naming_penalty_score, is_curated,
                     discovered_at, definition_capture_status, definition_length)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, table_fqn) DO UPDATE SET
                    snapshot_id              = excluded.snapshot_id,
                    definition_sql           = excluded.definition_sql,
                    definition_checksum      = excluded.definition_checksum,
                    referenced_tables_json   = excluded.referenced_tables_json,
                    restriction_json         = excluded.restriction_json,
                    naming_penalty_score     = excluded.naming_penalty_score,
                    is_curated               = excluded.is_curated,
                    discovered_at            = excluded.discovered_at,
                    definition_capture_status = excluded.definition_capture_status,
                    definition_length        = excluded.definition_length
                """,
                (
                    source_id, snapshot_id, table_fqn, definition_sql, checksum,
                    json.dumps(referenced_tables), json.dumps(restriction) if restriction else None,
                    naming_penalty_score, 1 if is_curated else 0, discovered_at,
                    definition_capture_status, definition_length,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO view_semantic_evidence
                    (source_id, snapshot_id, table_fqn, definition_sql, definition_checksum,
                     referenced_tables_json, restriction_json, naming_penalty_score, is_curated,
                     discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, table_fqn) DO UPDATE SET
                    snapshot_id            = excluded.snapshot_id,
                    definition_sql         = excluded.definition_sql,
                    definition_checksum    = excluded.definition_checksum,
                    referenced_tables_json = excluded.referenced_tables_json,
                    restriction_json       = excluded.restriction_json,
                    naming_penalty_score   = excluded.naming_penalty_score,
                    is_curated             = excluded.is_curated,
                    discovered_at          = excluded.discovered_at
                """,
                (
                    source_id, snapshot_id, table_fqn, definition_sql, checksum,
                    json.dumps(referenced_tables), json.dumps(restriction) if restriction else None,
                    naming_penalty_score, 1 if is_curated else 0, discovered_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()
