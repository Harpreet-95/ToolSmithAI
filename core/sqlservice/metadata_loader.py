"""
Read-only bulk metadata loader — SQL rebuild Phase 1, Step 2.

Adapter between ToolSmithAI's local SQLite metadata store and the pure
retrieval-document builder (core/sqlservice/retrieval_documents.py). For one
owned data source, loads the latest stored schema snapshot plus its
dictionary/vocabulary/relationship metadata with a small, fixed number of
bulk SQLite queries — never one query per table — reconstructs the existing
SchemaSnapshot dataclass tree, and calls build_retrieval_documents().

Scope: this module reads ToolSmithAI's own local SQLite metadata store
(data/db.py's get_connection()) only. It never connects to a live SQL
Server, never calls OpenAI, and never runs schema discovery — it only
reshapes what discovery already persisted.
"""

import json
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.sqlservice.retrieval_documents import RetrievalDocument, build_retrieval_documents
from data.db import get_connection

logger = logging.getLogger(__name__)


# ── Status vocabulary ─────────────────────────────────────────────────────

STATUS_READY = "READY"
STATUS_UNOWNED = "UNOWNED"
STATUS_NO_SNAPSHOT = "NO_SNAPSHOT"
STATUS_INVALID_SNAPSHOT = "INVALID_SNAPSHOT"
STATUS_DISCOVERY_FAILED = "DISCOVERY_FAILED"

_EMPTY_COUNTS: Mapping[str, int] = MappingProxyType({})


# ── Public result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalDocumentBatch:
    """
    Result of one load_retrieval_document_batch() call. `documents`,
    `counts`, and `warnings` are all immutable — a frozen dataclass alone
    does not stop a caller from mutating a plain dict/list attribute, so
    `counts` is a MappingProxyType and `documents`/`warnings` are tuples.
    """

    source_id: int
    snapshot_id: int | None
    snapshot_version: int | None
    status: str  # STATUS_READY | STATUS_UNOWNED | STATUS_NO_SNAPSHOT | STATUS_INVALID_SNAPSHOT | STATUS_DISCOVERY_FAILED
    documents: tuple[RetrievalDocument, ...]
    counts: Mapping[str, int]
    warnings: tuple[str, ...]


def _empty_batch(source_id: int, status: str, warnings: tuple[str, ...] = ()) -> RetrievalDocumentBatch:
    return RetrievalDocumentBatch(
        source_id=source_id,
        snapshot_id=None,
        snapshot_version=None,
        status=status,
        documents=(),
        counts=_EMPTY_COUNTS,
        warnings=warnings,
    )


# ── Schema snapshot reconstruction (persisted JSON only, no discovery) ──


def _ci_equal(a: str, b: str) -> bool:
    """
    Case-insensitive identifier comparison — SQL Server identifiers are
    normally case-insensitive (default collation), so a case-only
    difference between a stored table_fqn/schema_name and its expected
    value is normalized away here rather than rejected. Both sides are
    stripped of surrounding whitespace before comparing. This is the one
    documented comparison rule used everywhere object identity is checked
    below; it never rewrites which spelling is kept — callers keep using
    whatever string was actually stored.
    """
    return isinstance(a, str) and isinstance(b, str) and a.strip().lower() == b.strip().lower()


def _reconstruct_schema_snapshot(raw: object, source_id: int) -> SchemaSnapshot:
    """
    Rebuild a SchemaSnapshot dataclass tree from the persisted
    dataclasses.asdict() JSON (see data/schema_service.py's
    `json.dumps(dataclasses.asdict(snapshot))`, and the same reconstruction
    idiom in data/dictionary_service.py's `_reconstruct_snapshot`).

    Unlike that reference, every required shape is validated explicitly and
    a ValueError is raised (never a bare KeyError/TypeError/AttributeError)
    on any structurally invalid input, so a caller can turn it into
    STATUS_INVALID_SNAPSHOT instead of crashing or silently returning an
    empty result.
    """
    if not isinstance(raw, dict):
        raise ValueError("top-level snapshot JSON is not an object")

    # discovery_succeeded: missing is backward-compatible (older persisted
    # snapshots predate this field) and defaults to True, matching
    # SchemaSnapshot's own dataclass default. When the key IS present —
    # including an explicit JSON null — it must be a real boolean; a
    # string ("false"), an int (0/1), or null must never be silently
    # coerced into a boolean, since that could turn a recorded failed
    # discovery into a falsely successful one.
    if "discovery_succeeded" in raw:
        discovery_succeeded = raw["discovery_succeeded"]
        if not isinstance(discovery_succeeded, bool):
            raise ValueError(
                "'discovery_succeeded' must be a boolean when present "
                f"(got {type(discovery_succeeded).__name__})"
            )
    else:
        discovery_succeeded = True

    # warnings: missing or null becomes an empty list. A supplied value
    # must be an actual list of strings — in particular, a bare string
    # must never be accepted here, since list("some string") would
    # silently explode it into one warning per character.
    warnings_raw = raw.get("warnings")
    if warnings_raw is None:
        warnings = []
    elif isinstance(warnings_raw, list) and all(isinstance(w, str) for w in warnings_raw):
        warnings = list(warnings_raw)
    else:
        raise ValueError("snapshot 'warnings' must be a list of strings (or absent/null)")

    schemas_raw = raw.get("schemas")
    if not isinstance(schemas_raw, list):
        raise ValueError("snapshot 'schemas' is missing or not a list")

    schemas: list[SchemaInfo] = []
    for schema_raw in schemas_raw:
        if not isinstance(schema_raw, dict):
            raise ValueError("a 'schemas' entry is not an object")

        schema_name = schema_raw.get("schema_name")
        if not schema_name or not isinstance(schema_name, str):
            raise ValueError("a schema entry is missing a valid 'schema_name'")

        tables_raw = schema_raw.get("tables")
        if not isinstance(tables_raw, list):
            raise ValueError(f"schema '{schema_name}': 'tables' is missing or not a list")

        tables: list[TableInfo] = []
        for table_raw in tables_raw:
            if not isinstance(table_raw, dict):
                raise ValueError(f"schema '{schema_name}': a table entry is not an object")

            table_name = table_raw.get("table_name")
            table_fqn = table_raw.get("table_fqn")
            if not table_name or not isinstance(table_name, str):
                raise ValueError(f"schema '{schema_name}': a table is missing a valid 'table_name'")
            if not table_fqn or not isinstance(table_fqn, str):
                raise ValueError(f"table '{table_name}': missing a valid 'table_fqn'")

            # Object identity: a table's own stored schema_name (when
            # present) must agree with its containing schema — case-only
            # differences are normalized via _ci_equal, anything else is a
            # substantive mismatch and invalidates the snapshot rather than
            # silently trusting one side over the other. The table's own
            # spelling (if supplied) is preserved on the reconstructed
            # TableInfo, exactly as the stored snapshot spelled it.
            table_schema_name = table_raw.get("schema_name")
            if table_schema_name is not None:
                if not isinstance(table_schema_name, str) or not _ci_equal(table_schema_name, schema_name):
                    raise ValueError(
                        f"table '{table_name}': schema_name '{table_schema_name}' does not match "
                        f"containing schema '{schema_name}'"
                    )
                effective_schema_name = table_schema_name
            else:
                effective_schema_name = schema_name

            expected_fqn = f"{effective_schema_name}.{table_name}"
            if not _ci_equal(table_fqn, expected_fqn):
                raise ValueError(
                    f"table_fqn '{table_fqn}' does not match schema_name+table_name "
                    f"('{expected_fqn}')"
                )

            try:
                columns = [ColumnInfo(**c) for c in (table_raw.get("columns") or [])]
                primary_keys = [PrimaryKeyInfo(**pk) for pk in (table_raw.get("primary_keys") or [])]
                foreign_keys = [ForeignKeyInfo(**fk) for fk in (table_raw.get("foreign_keys") or [])]
            except TypeError as exc:
                raise ValueError(f"table '{table_fqn}': malformed column/key entry ({exc})") from exc

            tables.append(
                TableInfo(
                    table_name=table_name,
                    schema_name=effective_schema_name,
                    table_fqn=table_fqn,
                    table_type=table_raw.get("table_type") or "TABLE",
                    row_count_estimate=table_raw.get("row_count_estimate"),
                    columns=columns,
                    primary_keys=primary_keys,
                    foreign_keys=foreign_keys,
                )
            )
        schemas.append(SchemaInfo(schema_name=schema_name, tables=tables))

    return SchemaSnapshot(
        source_id=source_id,
        source_type=raw.get("source_type") or "",
        discovered_at=raw.get("discovered_at") or "",
        schemas=schemas,
        database_name=raw.get("database_name"),
        server_name=raw.get("server_name"),
        connector_version=raw.get("connector_version"),
        discovery_duration_ms=raw.get("discovery_duration_ms"),
        warnings=warnings,
        discovery_succeeded=discovery_succeeded,
    )


# ── Counts ─────────────────────────────────────────────────────────────


def _compute_counts(
    schema_snapshot: SchemaSnapshot,
    documents: list[RetrievalDocument],
    dictionary_table_rows: list,
    dictionary_column_rows: list,
    vocabulary_rows: list,
) -> dict[str, int]:
    tables_flat = [t for s in schema_snapshot.schemas for t in s.tables]

    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    confirmed_edges = 0
    inferred_edges = 0
    for doc in documents:
        tier_counts[doc.retrieval_tier] = tier_counts.get(doc.retrieval_tier, 0) + 1
        confirmed_edges += len(doc.metadata.get("confirmed_relationship_targets", ()))
        inferred_edges += len(doc.metadata.get("inferred_relationship_targets", ()))

    return {
        "tables": sum(1 for t in tables_flat if (t.table_type or "").strip().upper() == "TABLE"),
        "views": sum(1 for t in tables_flat if (t.table_type or "").strip().upper() == "VIEW"),
        "columns": sum(len(t.columns) for t in tables_flat),
        "documents": len(documents),
        "dictionary_tables": len(dictionary_table_rows),
        "dictionary_columns": len(dictionary_column_rows),
        "vocabulary_rows": len(vocabulary_rows),
        "confirmed_relationship_edges": confirmed_edges,
        "inferred_relationship_edges": inferred_edges,
        "tier_1_documents": tier_counts[1],
        "tier_2_documents": tier_counts[2],
        "tier_3_documents": tier_counts[3],
        "tier_4_documents": tier_counts[4],
    }


# ── Public entry point ────────────────────────────────────────────────────


def load_retrieval_document_batch(source_id: int, user_id: str) -> RetrievalDocumentBatch:
    """
    Load one owned source's latest schema snapshot plus its dictionary/
    vocabulary/relationship metadata from ToolSmithAI's local SQLite store
    and build retrieval documents for it.

    Exactly one SQLite connection is opened, and exactly six bulk SELECTs
    are issued regardless of how many tables/columns/relationships the
    source has: ownership, latest snapshot, dictionary tables, dictionary
    columns, vocabulary, and relationships for that snapshot — never one
    query per table (no N+1). The connection is always closed before
    returning or raising.

    A snapshot that reconstructs successfully but recorded
    discovery_succeeded=False short-circuits with STATUS_DISCOVERY_FAILED
    before any of the four dictionary/vocabulary/relationship queries run
    — that part of the six-query design only executes on the READY path.

    Never connects to a live SQL Server, never calls OpenAI, never runs
    schema discovery — only reshapes what discovery already persisted.
    """
    conn = get_connection()
    try:
        owner_row = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owner_row is None:
            return _empty_batch(source_id, STATUS_UNOWNED)

        snapshot_row = conn.execute(
            "SELECT id, snapshot_version, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snapshot_row is None:
            return _empty_batch(source_id, STATUS_NO_SNAPSHOT)

        snapshot_id = snapshot_row["id"]
        snapshot_version = snapshot_row["snapshot_version"]

        try:
            raw_snapshot = json.loads(snapshot_row["snapshot_json"])
        except (TypeError, ValueError):
            logger.warning(
                "load_retrieval_document_batch: unparseable snapshot_json "
                "for source_id=%s snapshot_id=%s", source_id, snapshot_id, exc_info=True,
            )
            return RetrievalDocumentBatch(
                source_id=source_id,
                snapshot_id=snapshot_id,
                snapshot_version=snapshot_version,
                status=STATUS_INVALID_SNAPSHOT,
                documents=(),
                counts=_EMPTY_COUNTS,
                warnings=("stored snapshot JSON could not be parsed.",),
            )

        try:
            schema_snapshot = _reconstruct_schema_snapshot(raw_snapshot, source_id)
        except ValueError as exc:
            logger.warning(
                "load_retrieval_document_batch: invalid snapshot structure "
                "for source_id=%s snapshot_id=%s: %s", source_id, snapshot_id, exc,
            )
            return RetrievalDocumentBatch(
                source_id=source_id,
                snapshot_id=snapshot_id,
                snapshot_version=snapshot_version,
                status=STATUS_INVALID_SNAPSHOT,
                documents=(),
                counts=_EMPTY_COUNTS,
                warnings=(f"stored snapshot structure is invalid: {exc}",),
            )

        if not schema_snapshot.discovery_succeeded:
            logger.warning(
                "load_retrieval_document_batch: latest snapshot recorded a "
                "failed discovery run for source_id=%s snapshot_id=%s",
                source_id, snapshot_id,
            )
            return RetrievalDocumentBatch(
                source_id=source_id,
                snapshot_id=snapshot_id,
                snapshot_version=snapshot_version,
                status=STATUS_DISCOVERY_FAILED,
                documents=(),
                counts=_EMPTY_COUNTS,
                warnings=("the stored snapshot recorded an unsuccessful discovery run.",),
            )

        # Bulk, source-level metadata — matches the existing
        # list_dictionary_tables()/get_generated_vocabulary() convention of
        # filtering by source_id only (each is upserted/regenerated across
        # snapshots, not versioned per-snapshot). Selected columns are
        # exactly what build_retrieval_documents()/classify_relationship_row()
        # read — no credentials, approval state, or profiled sample values.
        dict_table_rows = conn.execute(
            "SELECT table_fqn, business_name, description, domain "
            "FROM data_dictionary_tables WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        dict_column_rows = conn.execute(
            "SELECT table_fqn, column_name, business_label, meaning, semantic_type, "
            "is_metric, is_date "
            "FROM data_dictionary_columns WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        vocabulary_rows = conn.execute(
            "SELECT term, table_fqn, column_name, confidence_tier, confidence_score "
            "FROM generated_business_vocabulary WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        # Relationships ARE scoped to the same snapshot_id just selected —
        # never mixed with an older/unrelated snapshot's edges.
        relationship_rows = conn.execute(
            "SELECT from_schema, from_table, from_table_fqn, from_column, "
            "to_schema, to_table, to_table_fqn, to_column, relationship_name, "
            "relationship_type, confidence, relationship_status, inference_method "
            "FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
            (source_id, snapshot_id),
        ).fetchall()
    finally:
        conn.close()

    dictionary = {
        "tables": [dict(row) for row in dict_table_rows],
        "columns": [dict(row) for row in dict_column_rows],
    }

    vocabulary: dict[str, list[dict]] = {}
    for row in vocabulary_rows:
        entry = dict(row)
        vocabulary.setdefault(entry["term"], []).append(entry)

    relationships = [dict(row) for row in relationship_rows]

    documents = build_retrieval_documents(
        source_id=source_id,
        schema_snapshot=schema_snapshot,
        dictionary=dictionary,
        vocabulary=vocabulary,
        relationships=relationships,
    )

    counts = _compute_counts(
        schema_snapshot, documents, dict_table_rows, dict_column_rows, vocabulary_rows,
    )

    return RetrievalDocumentBatch(
        source_id=source_id,
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_version,
        status=STATUS_READY,
        documents=tuple(documents),
        counts=MappingProxyType(counts),
        warnings=(),
    )
