"""
Business Knowledge Service — Phase 2.

Aggregates existing enterprise metadata (schema, profiling, dictionary,
domains, entities, relationships) into one connected business view.

NO new storage is created here. Every field is read from the tables that
already exist. This is a pure composition layer.
"""
import json
import logging

from data.db import get_connection
from data.profiling_snapshot_resolver import (
    get_latest_profiling_snapshot,
    get_latest_profiling_snapshot_detail,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _verify_source(conn, source_id: int, user_id: str):
    """Return source row if owned by user_id, else None."""
    return conn.execute(
        "SELECT id, display_name, source_type, source_category, last_discovered_at "
        "FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()


def _latest_schema_snap_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


def _safe_json(raw: str | None) -> list | dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _build_search_keywords(
    table_fqn: str,
    dict_table,
    domain_row,
    entity_row,
    columns: list[dict],
) -> list[str]:
    """Derive search keywords from existing metadata — no new data stored."""
    parts: set[str] = set()
    for segment in table_fqn.replace(".", "_").split("_"):
        if len(segment) > 2:
            parts.add(segment.lower())
    if dict_table and dict_table["business_name"]:
        for word in dict_table["business_name"].lower().split():
            if len(word) > 2:
                parts.add(word)
    if domain_row and domain_row["domain"] and domain_row["domain"] != "Unknown":
        parts.add(domain_row["domain"].lower())
    if entity_row and entity_row["entity"] and entity_row["entity"] != "Unknown":
        parts.add(entity_row["entity"].lower())
    for col in columns:
        dic = col.get("dictionary")
        if dic and dic.get("business_label"):
            for word in dic["business_label"].lower().split():
                if len(word) > 2:
                    parts.add(word)
    return sorted(parts)


def _compute_table_confidence(dict_table, domain_row, entity_row, table_profile) -> float | None:
    """Average available confidence signals from existing metadata."""
    scores: list[float] = []
    if domain_row and domain_row["confidence"] is not None:
        scores.append(float(domain_row["confidence"]))
    if entity_row and entity_row["confidence"] is not None:
        scores.append(float(entity_row["confidence"]))
    if table_profile and table_profile["classification_confidence"] is not None:
        scores.append(float(table_profile["classification_confidence"]))
    return round(sum(scores) / len(scores), 4) if scores else None


def _build_column_evidence(dict_col, col_profile) -> list[str]:
    """Compile evidence strings from existing column metadata."""
    evidence: list[str] = []
    if dict_col:
        if dict_col["business_label"]:
            evidence.append(f"Dictionary label: {dict_col['business_label']}")
        if dict_col["is_approved"]:
            evidence.append("Dictionary entry approved by human reviewer")
        if dict_col["semantic_type"]:
            evidence.append(f"Dictionary semantic type: {dict_col['semantic_type']}")
    if col_profile:
        if col_profile["semantic_type"]:
            conf = col_profile["semantic_confidence"]
            conf_str = f" ({conf:.0%})" if conf is not None else ""
            evidence.append(f"Profiling semantic type: {col_profile['semantic_type']}{conf_str}")
        if col_profile["pii_name_heuristic"]:
            evidence.append("PII signals detected by heuristic classifier")
        if col_profile["pii_confirmed"]:
            evidence.append("PII confirmed by human reviewer")
    return evidence


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def get_table_business_context(
    source_id: int,
    user_id: str,
    table_fqn: str,
    *,
    session=None,
) -> dict | None:
    """
    Return a complete business context for one table, assembled from all
    existing metadata stores. No new data is created or stored.

    Sections that have not been populated yet are returned as None so the
    caller can distinguish "no data" from "data with zero values".

    Returns None if source_id does not exist or is not owned by user_id.

    Phase 3.2A: with a session, called once per candidate table this is
    still N queries per category (see get_table_business_contexts_batch for
    the batched equivalent used by query_planning_service's per-candidate
    loop) — session here only buys connection reuse and the shared
    schema/profiling-snapshot-id constants, not query batching.
    """
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if source_row is None:
            return None

        if session is not None:
            schema_snap_id = session.get_or_compute(
                f"latest_schema_snapshot_id:{source_id}",
                lambda: _latest_schema_snap_id(conn, source_id),
            )
            prof_snap_id = session.get_or_compute(
                f"latest_profiling_snapshot_id:{source_id}",
                lambda: (lambda s: s.id if s else None)(
                    get_latest_profiling_snapshot(source_id, conn=conn)
                ),
            )
        else:
            schema_snap_id = _latest_schema_snap_id(conn, source_id)
            snapshot       = get_latest_profiling_snapshot(source_id)
            prof_snap_id   = snapshot.id if snapshot else None

        # ── Dictionary ────────────────────────────────────────────────────
        dict_table = conn.execute(
            """SELECT business_name, description, domain, grain,
                      is_approved, approved_by, approved_at,
                      generation_method, updated_at
               FROM data_dictionary_tables
               WHERE source_id = ? AND table_fqn = ?""",
            (source_id, table_fqn),
        ).fetchone()

        dict_cols = conn.execute(
            """SELECT column_name, business_label, meaning, semantic_type,
                      is_metric, is_dimension, is_date, is_id, pii_risk,
                      is_approved, approved_by, generation_method
               FROM data_dictionary_columns
               WHERE source_id = ? AND table_fqn = ?
               ORDER BY column_name""",
            (source_id, table_fqn),
        ).fetchall()

        # ── Domain ────────────────────────────────────────────────────────
        domain_row = conn.execute(
            """SELECT domain, confidence, evidence_json
               FROM domain_assignments WHERE source_id = ? AND table_fqn = ?""",
            (source_id, table_fqn),
        ).fetchone()

        # ── Entity ────────────────────────────────────────────────────────
        entity_row = conn.execute(
            """SELECT entity, confidence, evidence_json
               FROM entity_assignments WHERE source_id = ? AND table_fqn = ?""",
            (source_id, table_fqn),
        ).fetchone()

        # ── Profiling ─────────────────────────────────────────────────────
        table_profile = None
        col_profiles: list = []
        if prof_snap_id is not None:
            table_profile = conn.execute(
                """SELECT table_name, schema_name, table_type,
                          exact_row_count, estimated_row_count, row_count_tier,
                          has_date_column, date_column_name, data_currency,
                          column_count, pk_column_count, fk_count, referenced_by_count,
                          is_junction_table, is_root_table, is_leaf_table, has_identity_column,
                          avg_null_percentage, completeness_score,
                          table_class, classification_confidence, classification_evidence_json,
                          pii_column_count, confirmed_pii_count,
                          profiling_depth, profiling_status, profiled_at
                   FROM profiling_table_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn = ?""",
                (prof_snap_id, table_fqn),
            ).fetchone()

            col_profiles = conn.execute(
                """SELECT column_name, data_type, raw_type,
                          is_nullable, is_primary_key, is_identity, ordinal_position,
                          null_percentage, distinct_count, distinct_percentage,
                          uniqueness_score, cardinality_tier,
                          min_value, max_value, avg_length,
                          semantic_type, semantic_confidence, semantic_evidence_json,
                          pii_name_heuristic, pii_confirmed, pii_signals_json,
                          profiling_depth, profiling_status
                   FROM profiling_column_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn = ?
                   ORDER BY ordinal_position""",
                (prof_snap_id, table_fqn),
            ).fetchall()

        # ── Relationships ─────────────────────────────────────────────────
        outbound_rels: list = []
        inbound_rels: list = []
        if schema_snap_id is not None:
            outbound_rels = conn.execute(
                """SELECT from_column, to_schema, to_table, to_table_fqn,
                          to_column, relationship_name, relationship_type, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ?
                     AND relationship_status IN ('AUTO', 'APPROVED')
                   ORDER BY from_column""",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()

            inbound_rels = conn.execute(
                """SELECT from_table_fqn, from_schema, from_table, from_column,
                          to_column, relationship_name, relationship_type, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ?
                     AND relationship_status IN ('AUTO', 'APPROVED')
                   ORDER BY from_table_fqn, from_column""",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()

            # Sprint 2, Signal #2 — Weighted All-Status Relationship
            # Centrality: a SEPARATE, ranking-only read of ALL
            # relationship_status values (not just AUTO/APPROVED above).
            # `relationships` stays the trusted join-planning contract,
            # unchanged; this is bounded to the fields canonicality ranking
            # needs and is never used to build a join.
            relationship_evidence_rows = conn.execute(
                """SELECT 'outbound' AS direction, id AS relationship_id,
                          to_table_fqn AS related_table,
                          relationship_status, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ?
                   UNION ALL
                   SELECT 'inbound' AS direction, id AS relationship_id,
                          from_table_fqn AS related_table,
                          relationship_status, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ?""",
                (source_id, schema_snap_id, table_fqn,
                 source_id, schema_snap_id, table_fqn),
            ).fetchall()
        else:
            relationship_evidence_rows = []

    finally:
        if own_connection:
            conn.close()

    return _assemble_table_business_context(
        source_id, table_fqn, source_row, dict_table, dict_cols, domain_row, entity_row,
        table_profile, col_profiles, outbound_rels, inbound_rels, relationship_evidence_rows,
    )


def _assemble_table_business_context(
    source_id, table_fqn, source_row, dict_table, dict_cols, domain_row, entity_row,
    table_profile, col_profiles, outbound_rels, inbound_rels, relationship_evidence_rows,
) -> dict:
    """Pure assembly of get_table_business_context's return shape from
    already-fetched rows — shared by the single-table fetch above and
    get_table_business_contexts_batch below, so both paths produce
    byte-identical output for the same underlying data. Row objects may
    carry extra columns (e.g. table_fqn, added for batch grouping) beyond
    what a single-table fetch selects — harmless, since only named keys
    used below are read."""
    # ── Merge columns from profiling + dictionary ─────────────────────────
    prof_col_map = {r["column_name"]: dict(r) for r in col_profiles}
    dict_col_map = {r["column_name"]: dict(r) for r in dict_cols}
    all_col_names = sorted(set(prof_col_map) | set(dict_col_map))

    columns: list[dict] = []
    for col_name in all_col_names:
        prof = prof_col_map.get(col_name)
        dic  = dict_col_map.get(col_name)
        columns.append({
            "column_name": col_name,
            "schema": {
                "data_type":      prof["data_type"],
                "raw_type":       prof["raw_type"],
                "is_nullable":    bool(prof["is_nullable"]),
                "is_primary_key": bool(prof["is_primary_key"]),
                "is_identity":    bool(prof["is_identity"]),
            } if prof else None,
            "dictionary": {
                "business_label":    dic["business_label"],
                "meaning":           dic["meaning"],
                "semantic_type":     dic["semantic_type"],
                "is_metric":         bool(dic["is_metric"]),
                "is_dimension":      bool(dic["is_dimension"]),
                "is_date":           bool(dic["is_date"]),
                "is_id":             bool(dic["is_id"]),
                "pii_risk":          bool(dic["pii_risk"]),
                "is_approved":       bool(dic["is_approved"]),
                "approved_by":       dic["approved_by"],
                "generation_method": dic["generation_method"],
            } if dic else None,
            "profiling": {
                "semantic_type":       prof["semantic_type"],
                "semantic_confidence": prof["semantic_confidence"],
                "pii_name_heuristic":  bool(prof["pii_name_heuristic"]),
                "pii_confirmed":       bool(prof["pii_confirmed"]),
                "null_percentage":     prof["null_percentage"],
                "distinct_count":      prof["distinct_count"],
                "distinct_percentage": prof["distinct_percentage"],
                "cardinality_tier":    prof["cardinality_tier"],
                "uniqueness_score":    prof["uniqueness_score"],
                "min_value":           prof["min_value"],
                "max_value":           prof["max_value"],
                "profiling_depth":     prof["profiling_depth"],
            } if prof else None,
        })

    # ── Assemble sections ─────────────────────────────────────────────────
    dictionary_section = {
        "business_name":     dict_table["business_name"],
        "description":       dict_table["description"],
        "grain":             dict_table["grain"],
        "is_approved":       bool(dict_table["is_approved"]),
        "approved_by":       dict_table["approved_by"],
        "approved_at":       dict_table["approved_at"],
        "generation_method": dict_table["generation_method"],
        "last_updated":      dict_table["updated_at"],
    } if dict_table else None

    domain_section = {
        "domain":     domain_row["domain"],
        "confidence": domain_row["confidence"],
        "evidence":   _safe_json(domain_row["evidence_json"]),
    } if domain_row else None

    entity_section = {
        "entity":     entity_row["entity"],
        "confidence": entity_row["confidence"],
        "evidence":   _safe_json(entity_row["evidence_json"]),
    } if entity_row else None

    profiling_section = {
        "table_class":               table_profile["table_class"],
        "classification_confidence": table_profile["classification_confidence"],
        "classification_evidence":   _safe_json(table_profile["classification_evidence_json"]),
        "exact_row_count":           table_profile["exact_row_count"],
        "estimated_row_count":       table_profile["estimated_row_count"],
        "row_count_tier":            table_profile["row_count_tier"],
        "fk_count":                  table_profile["fk_count"],
        "referenced_by_count":       table_profile["referenced_by_count"],
        "is_junction_table":         bool(table_profile["is_junction_table"]),
        "is_root_table":             bool(table_profile["is_root_table"]),
        "is_leaf_table":             bool(table_profile["is_leaf_table"]),
        "pii_column_count":          table_profile["pii_column_count"],
        "confirmed_pii_count":       table_profile["confirmed_pii_count"],
        "completeness_score":        table_profile["completeness_score"],
        "has_date_column":           bool(table_profile["has_date_column"]),
        "data_currency":             table_profile["data_currency"],
        "profiling_depth":           table_profile["profiling_depth"],
        "profiling_status":          table_profile["profiling_status"],
        "profiled_at":               table_profile["profiled_at"],
    } if table_profile else None

    # ── Governance summary (derived from existing flags) ──────────────────
    cols_approved = sum(
        1 for c in columns
        if c.get("dictionary") and c["dictionary"]["is_approved"]
    )
    pii_pending = sum(
        1 for c in columns
        if c.get("profiling")
        and c["profiling"]["pii_name_heuristic"]
        and not c["profiling"]["pii_confirmed"]
    )
    governance_section = {
        "dictionary_approved":         bool(dict_table and dict_table["is_approved"]),
        "dictionary_columns_approved": cols_approved,
        "dictionary_columns_total":    len(columns),
        "domain_assigned":             bool(domain_row and domain_row["domain"] != "Unknown"),
        "entity_assigned":             bool(entity_row and entity_row["entity"] != "Unknown"),
        "pii_columns_pending_review":  pii_pending,
    }

    # ── Metadata completeness (derived flags) ─────────────────────────────
    completeness = {
        "has_dictionary":    dict_table is not None,
        "has_domain":        domain_row is not None,
        "has_entity":        entity_row is not None,
        "has_profiling":     table_profile is not None,
        "has_relationships": bool(outbound_rels or inbound_rels),
    }
    filled = sum(1 for v in completeness.values() if v)
    completeness["completeness_score"] = round(filled / len(completeness), 2)

    # ── Search keywords ───────────────────────────────────────────────────
    keywords = _build_search_keywords(table_fqn, dict_table, domain_row, entity_row, columns)

    return {
        "source": {
            "id":              source_id,
            "display_name":    source_row["display_name"],
            "source_type":     source_row["source_type"],
            "source_category": source_row["source_category"],
        },
        "table": {
            "table_fqn":  table_fqn,
            "table_name": (
                table_profile["table_name"] if table_profile
                else table_fqn.split(".")[-1]
            ),
            "schema_name": (
                table_profile["schema_name"] if table_profile
                else (table_fqn.split(".")[0] if "." in table_fqn else None)
            ),
            "table_type": table_profile["table_type"] if table_profile else None,
        },
        "dictionary":            dictionary_section,
        "domain":                domain_section,
        "entity":                entity_section,
        "profiling":             profiling_section,
        "columns":               columns,
        "relationships": {
            "outbound": [dict(r) for r in outbound_rels],
            "inbound":  [dict(r) for r in inbound_rels],
        },
        "relationship_evidence": [dict(r) for r in relationship_evidence_rows],
        "governance":            governance_section,
        "search_keywords":       keywords,
        "overall_confidence":    _compute_table_confidence(dict_table, domain_row, entity_row, table_profile),
        "metadata_completeness": completeness,
    }


def get_table_business_contexts_batch(
    source_id: int,
    user_id: str,
    table_fqns: "list[str] | set[str]",
    *,
    session=None,
) -> dict[str, dict]:
    """Phase 3.2A / Task 4 — batched equivalent of calling
    get_table_business_context() once per table_fqn. One IN-clause query per
    metadata category (dictionary, columns, domain, entity, table profile,
    column profiles, outbound/inbound relationships, relationship evidence)
    instead of one query per category PER TABLE — the per-candidate N+1
    pattern query_planning_service.plan_business_query used to run (16
    connections/~9 queries each on a real 16-candidate question).

    Shares _assemble_table_business_context with the single-table function
    above, so output is byte-identical to calling get_table_business_context
    once per table_fqn — this function only changes how rows are fetched,
    never scoring, ordering, or content. Every requested table_fqn gets an
    entry, even one with no matching metadata anywhere (an empty-shell
    dict — every section None/empty — exactly like calling
    get_table_business_context() for it directly; None is reserved for
    "source not owned", not "no data for this table").

    Returns {} if source_id is not owned by user_id, or table_fqns is empty.
    """
    table_fqns = list(dict.fromkeys(table_fqns))
    if not table_fqns:
        return {}

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if source_row is None:
            return {}

        if session is not None:
            schema_snap_id = session.get_or_compute(
                f"latest_schema_snapshot_id:{source_id}",
                lambda: _latest_schema_snap_id(conn, source_id),
            )
            prof_snap_id = session.get_or_compute(
                f"latest_profiling_snapshot_id:{source_id}",
                lambda: (lambda s: s.id if s else None)(
                    get_latest_profiling_snapshot(source_id, conn=conn)
                ),
            )
        else:
            schema_snap_id = _latest_schema_snap_id(conn, source_id)
            snapshot       = get_latest_profiling_snapshot(source_id, conn=conn)
            prof_snap_id   = snapshot.id if snapshot else None

        ph = ",".join("?" * len(table_fqns))

        dict_table_rows = conn.execute(
            f"""SELECT table_fqn, business_name, description, domain, grain,
                      is_approved, approved_by, approved_at, generation_method, updated_at
               FROM data_dictionary_tables
               WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        dict_table_by_fqn = {r["table_fqn"]: r for r in dict_table_rows}

        dict_col_rows = conn.execute(
            f"""SELECT table_fqn, column_name, business_label, meaning, semantic_type,
                      is_metric, is_dimension, is_date, is_id, pii_risk,
                      is_approved, approved_by, generation_method
               FROM data_dictionary_columns
               WHERE source_id = ? AND table_fqn IN ({ph})
               ORDER BY table_fqn, column_name""",
            (source_id, *table_fqns),
        ).fetchall()
        dict_cols_by_fqn: dict[str, list] = {}
        for r in dict_col_rows:
            dict_cols_by_fqn.setdefault(r["table_fqn"], []).append(r)

        domain_rows = conn.execute(
            f"""SELECT table_fqn, domain, confidence, evidence_json
               FROM domain_assignments WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        domain_by_fqn = {r["table_fqn"]: r for r in domain_rows}

        entity_rows = conn.execute(
            f"""SELECT table_fqn, entity, confidence, evidence_json
               FROM entity_assignments WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        entity_by_fqn = {r["table_fqn"]: r for r in entity_rows}

        table_profile_by_fqn: dict[str, object] = {}
        col_profiles_by_fqn: dict[str, list] = {}
        if prof_snap_id is not None:
            table_profile_rows = conn.execute(
                f"""SELECT table_fqn, table_name, schema_name, table_type,
                          exact_row_count, estimated_row_count, row_count_tier,
                          has_date_column, date_column_name, data_currency,
                          column_count, pk_column_count, fk_count, referenced_by_count,
                          is_junction_table, is_root_table, is_leaf_table, has_identity_column,
                          avg_null_percentage, completeness_score,
                          table_class, classification_confidence, classification_evidence_json,
                          pii_column_count, confirmed_pii_count,
                          profiling_depth, profiling_status, profiled_at
                   FROM profiling_table_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn IN ({ph})""",
                (prof_snap_id, *table_fqns),
            ).fetchall()
            table_profile_by_fqn = {r["table_fqn"]: r for r in table_profile_rows}

            col_profile_rows = conn.execute(
                f"""SELECT table_fqn, column_name, data_type, raw_type,
                          is_nullable, is_primary_key, is_identity, ordinal_position,
                          null_percentage, distinct_count, distinct_percentage,
                          uniqueness_score, cardinality_tier,
                          min_value, max_value, avg_length,
                          semantic_type, semantic_confidence, semantic_evidence_json,
                          pii_name_heuristic, pii_confirmed, pii_signals_json,
                          profiling_depth, profiling_status
                   FROM profiling_column_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn IN ({ph})
                   ORDER BY table_fqn, ordinal_position""",
                (prof_snap_id, *table_fqns),
            ).fetchall()
            for r in col_profile_rows:
                col_profiles_by_fqn.setdefault(r["table_fqn"], []).append(r)

        outbound_by_fqn: dict[str, list] = {}
        inbound_by_fqn: dict[str, list] = {}
        rel_evidence_by_fqn: dict[str, list] = {}
        if schema_snap_id is not None:
            outbound_rows = conn.execute(
                f"""SELECT from_table_fqn, from_column, to_schema, to_table, to_table_fqn,
                          to_column, relationship_name, relationship_type, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn IN ({ph})
                     AND relationship_status IN ('AUTO', 'APPROVED')
                   ORDER BY from_table_fqn, from_column""",
                (source_id, schema_snap_id, *table_fqns),
            ).fetchall()
            for r in outbound_rows:
                d = dict(r)
                key = d.pop("from_table_fqn")  # grouping-only column, not part of the per-table shape
                outbound_by_fqn.setdefault(key, []).append(d)

            inbound_rows = conn.execute(
                f"""SELECT to_table_fqn, from_table_fqn, from_schema, from_table, from_column,
                          to_column, relationship_name, relationship_type, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn IN ({ph})
                     AND relationship_status IN ('AUTO', 'APPROVED')
                   ORDER BY to_table_fqn, from_table_fqn, from_column""",
                (source_id, schema_snap_id, *table_fqns),
            ).fetchall()
            for r in inbound_rows:
                d = dict(r)
                key = d.pop("to_table_fqn")  # grouping-only column, not part of the per-table shape
                inbound_by_fqn.setdefault(key, []).append(d)

            rel_evidence_rows = conn.execute(
                f"""SELECT 'outbound' AS direction, id AS relationship_id,
                          to_table_fqn AS related_table, relationship_status, confidence,
                          from_table_fqn AS anchor_fqn
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn IN ({ph})
                   UNION ALL
                   SELECT 'inbound' AS direction, id AS relationship_id,
                          from_table_fqn AS related_table, relationship_status, confidence,
                          to_table_fqn AS anchor_fqn
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn IN ({ph})""",
                (source_id, schema_snap_id, *table_fqns,
                 source_id, schema_snap_id, *table_fqns),
            ).fetchall()
            for r in rel_evidence_rows:
                d = dict(r)
                key = d.pop("anchor_fqn")  # grouping-only column, not part of the per-table shape
                rel_evidence_by_fqn.setdefault(key, []).append(d)
    finally:
        if own_connection:
            conn.close()

    results: dict[str, dict] = {}
    for fqn in table_fqns:
        results[fqn] = _assemble_table_business_context(
            source_id, fqn, source_row,
            dict_table_by_fqn.get(fqn), dict_cols_by_fqn.get(fqn, []),
            domain_by_fqn.get(fqn), entity_by_fqn.get(fqn),
            table_profile_by_fqn.get(fqn), col_profiles_by_fqn.get(fqn, []),
            outbound_by_fqn.get(fqn, []), inbound_by_fqn.get(fqn, []),
            rel_evidence_by_fqn.get(fqn, []),
        )
    return results


def _assemble_column_business_context(
    source_id: int,
    table_fqn: str,
    column_name: str,
    dict_col,
    col_profile,
    domain_row,
    entity_row,
) -> dict:
    """Shared assembly for get_column_business_context and
    get_column_business_contexts_batch (Day 4, Capability 6, Task 3) — the
    two only differ in how dict_col/col_profile/domain_row/entity_row are
    fetched (one row at a time vs. one IN-clause query per category across
    many columns/tables), never in how they're assembled. Mirrors
    _assemble_table_business_context's own single-source-of-truth role for
    the table-level batch function above."""
    schema_section = {
        "data_type":        col_profile["data_type"],
        "raw_type":         col_profile["raw_type"],
        "is_nullable":      bool(col_profile["is_nullable"]),
        "is_primary_key":   bool(col_profile["is_primary_key"]),
        "is_identity":      bool(col_profile["is_identity"]),
        "ordinal_position": col_profile["ordinal_position"],
    } if col_profile else None

    dictionary_section = {
        "business_label":    dict_col["business_label"],
        "meaning":           dict_col["meaning"],
        "semantic_type":     dict_col["semantic_type"],
        "is_metric":         bool(dict_col["is_metric"]),
        "is_dimension":      bool(dict_col["is_dimension"]),
        "is_date":           bool(dict_col["is_date"]),
        "is_id":             bool(dict_col["is_id"]),
        "pii_risk":          bool(dict_col["pii_risk"]),
        "is_approved":       bool(dict_col["is_approved"]),
        "approved_by":       dict_col["approved_by"],
        "approved_at":       dict_col["approved_at"],
        "generation_method": dict_col["generation_method"],
    } if dict_col else None

    profiling_section = {
        "semantic_type":        col_profile["semantic_type"],
        "semantic_confidence":  col_profile["semantic_confidence"],
        "semantic_evidence":    _safe_json(col_profile["semantic_evidence_json"]),
        "pii_name_heuristic":   bool(col_profile["pii_name_heuristic"]),
        "pii_confirmed":        bool(col_profile["pii_confirmed"]),
        "pii_signals":          _safe_json(col_profile["pii_signals_json"]),
        "null_percentage":      col_profile["null_percentage"],
        "distinct_count":       col_profile["distinct_count"],
        "distinct_percentage":  col_profile["distinct_percentage"],
        "cardinality_tier":     col_profile["cardinality_tier"],
        "uniqueness_score":     col_profile["uniqueness_score"],
        "min_value":            col_profile["min_value"],
        "max_value":            col_profile["max_value"],
        "mean_value":           col_profile["mean_value"],
        "std_deviation":        col_profile["std_deviation"],
        "dominant_pattern":     col_profile["dominant_pattern"],
        "profiling_depth":      col_profile["profiling_depth"],
    } if col_profile else None

    # Confidence: use semantic_confidence from profiling when available
    confidence: float | None = None
    if col_profile and col_profile["semantic_confidence"] is not None:
        confidence = col_profile["semantic_confidence"]

    return {
        "source_id":   source_id,
        "table_fqn":   table_fqn,
        "column_name": column_name,
        "schema":      schema_section,
        "dictionary":  dictionary_section,
        "profiling":   profiling_section,
        "table_context": {
            "domain":            domain_row["domain"]      if domain_row  else None,
            "domain_confidence": domain_row["confidence"]  if domain_row  else None,
            "entity":            entity_row["entity"]      if entity_row  else None,
            "entity_confidence": entity_row["confidence"]  if entity_row  else None,
        },
        "confidence": confidence,
        "evidence":   _build_column_evidence(dict_col, col_profile),
    }


def get_column_business_context(
    source_id: int,
    user_id: str,
    table_fqn: str,
    column_name: str,
) -> dict | None:
    """
    Return a complete business context for one column by composing its
    dictionary entry, profiling statistics, and parent-table domain/entity.

    Returns None if source_id does not exist or is not owned by user_id.
    Sections that have not been populated yet are returned as None.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if source_row is None:
            return None

        snapshot     = get_latest_profiling_snapshot(source_id)
        prof_snap_id = snapshot.id if snapshot else None

        # ── Dictionary ────────────────────────────────────────────────────
        dict_col = conn.execute(
            """SELECT business_label, meaning, semantic_type,
                      is_metric, is_dimension, is_date, is_id, pii_risk,
                      is_approved, approved_by, approved_at, generation_method
               FROM data_dictionary_columns
               WHERE source_id = ? AND table_fqn = ? AND column_name = ?""",
            (source_id, table_fqn, column_name),
        ).fetchone()

        # ── Profiling ─────────────────────────────────────────────────────
        col_profile = None
        if prof_snap_id is not None:
            col_profile = conn.execute(
                """SELECT data_type, raw_type, is_nullable, is_primary_key,
                          is_identity, ordinal_position,
                          null_count, null_percentage, populated_count, populated_percentage,
                          distinct_count, distinct_percentage, uniqueness_score, cardinality_tier,
                          min_value, max_value, avg_length,
                          mean_value, std_deviation, p5_value, p95_value,
                          dominant_pattern, pattern_coverage,
                          semantic_type, semantic_confidence, semantic_evidence_json,
                          pii_name_heuristic, pii_confirmed, pii_signals_json,
                          profiling_depth, profiling_status
                   FROM profiling_column_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn = ? AND column_name = ?""",
                (prof_snap_id, table_fqn, column_name),
            ).fetchone()

        # ── Parent-table domain and entity (for context) ──────────────────
        domain_row = conn.execute(
            "SELECT domain, confidence FROM domain_assignments "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()

        entity_row = conn.execute(
            "SELECT entity, confidence FROM entity_assignments "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()

    finally:
        conn.close()

    return _assemble_column_business_context(
        source_id, table_fqn, column_name, dict_col, col_profile, domain_row, entity_row,
    )


def get_column_business_contexts_batch(
    source_id: int,
    user_id: str,
    columns: "list[tuple[str, str]]",
) -> "dict[tuple[str, str], dict]":
    """Day 4, Capability 6 (Task 3) — batched equivalent of calling
    get_column_business_context() once per (table_fqn, column_name) pair.
    Mirrors get_table_business_contexts_batch's approach: one IN-clause
    query per metadata category across the distinct set of tables involved,
    instead of one dictionary/profiling/domain/entity/source-verify/
    snapshot-lookup round trip — and one fresh SQLite connection — PER
    COLUMN. This was the N+1 pattern data.query_execution_service.
    _governance_recheck used to run (measured: 1817 SQLite statements across
    18 columns on a real 3-table join question, ~140-200ms of governance
    recheck time vs 3-10ms for a single-table question).

    Shares _assemble_column_business_context with the single-column function
    above, so output for each pair is byte-identical to calling
    get_column_business_context() for it directly — only how rows are
    fetched changes, never the assembled content.

    Returns {} if source_id is not owned by user_id, or columns is empty.
    Every requested pair still gets an entry — value None only for "no
    metadata anywhere for this exact column", the same signal
    get_column_business_context returns for it directly (not for "source
    not owned", which instead makes the whole return value {}).
    """
    columns = list(dict.fromkeys(columns))
    if not columns:
        return {}

    table_fqns = list(dict.fromkeys(t for t, _c in columns))

    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if source_row is None:
            return {}

        snapshot     = get_latest_profiling_snapshot(source_id)
        prof_snap_id = snapshot.id if snapshot else None

        ph = ",".join("?" * len(table_fqns))

        dict_col_rows = conn.execute(
            f"""SELECT table_fqn, column_name, business_label, meaning, semantic_type,
                      is_metric, is_dimension, is_date, is_id, pii_risk,
                      is_approved, approved_by, approved_at, generation_method
               FROM data_dictionary_columns
               WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        dict_col_by_key = {(r["table_fqn"], r["column_name"]): r for r in dict_col_rows}

        col_profile_by_key: dict = {}
        if prof_snap_id is not None:
            col_profile_rows = conn.execute(
                f"""SELECT table_fqn, column_name, data_type, raw_type,
                          is_nullable, is_primary_key, is_identity, ordinal_position,
                          null_count, null_percentage, populated_count, populated_percentage,
                          distinct_count, distinct_percentage, uniqueness_score, cardinality_tier,
                          min_value, max_value, avg_length,
                          mean_value, std_deviation, p5_value, p95_value,
                          dominant_pattern, pattern_coverage,
                          semantic_type, semantic_confidence, semantic_evidence_json,
                          pii_name_heuristic, pii_confirmed, pii_signals_json,
                          profiling_depth, profiling_status
                   FROM profiling_column_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn IN ({ph})""",
                (prof_snap_id, *table_fqns),
            ).fetchall()
            col_profile_by_key = {(r["table_fqn"], r["column_name"]): r for r in col_profile_rows}

        domain_rows = conn.execute(
            f"""SELECT table_fqn, domain, confidence FROM domain_assignments
               WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        domain_by_fqn = {r["table_fqn"]: r for r in domain_rows}

        entity_rows = conn.execute(
            f"""SELECT table_fqn, entity, confidence FROM entity_assignments
               WHERE source_id = ? AND table_fqn IN ({ph})""",
            (source_id, *table_fqns),
        ).fetchall()
        entity_by_fqn = {r["table_fqn"]: r for r in entity_rows}
    finally:
        conn.close()

    results: dict = {}
    for table_fqn, column_name in columns:
        results[(table_fqn, column_name)] = _assemble_column_business_context(
            source_id, table_fqn, column_name,
            dict_col_by_key.get((table_fqn, column_name)),
            col_profile_by_key.get((table_fqn, column_name)),
            domain_by_fqn.get(table_fqn),
            entity_by_fqn.get(table_fqn),
        )
    return results


def get_business_summary(source_id: int, user_id: str) -> dict | None:
    """
    Return enterprise-level aggregate metrics for a source by reading from
    all existing metadata stores. No new data is created or stored.

    Returns None if source_id does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if source_row is None:
            return None

        # ── Schema stats (from latest schema snapshot) ────────────────────
        schema_snap = conn.execute(
            """SELECT id, table_count, view_count, column_count, discovered_at
               FROM schema_snapshots WHERE source_id = ?
               ORDER BY snapshot_version DESC LIMIT 1""",
            (source_id,),
        ).fetchone()

        # ── Profiling stats ───────────────────────────────────────────────
        # Phase 2A.2 migration: snapshot selection now owned by
        # data.profiling_snapshot_resolver.get_latest_profiling_snapshot_detail,
        # which reproduces the exact query this used to run inline (highest
        # snapshot_version for source_id, status-independent) — no behavior
        # change, just removing the duplicated SQL from this call site.
        detail = get_latest_profiling_snapshot_detail(source_id)
        prof_snap_id   = detail.id if detail else None
        schema_snap_id = schema_snap["id"] if schema_snap else None

        # ── Dictionary stats ──────────────────────────────────────────────
        dict_t_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(is_approved), 0) AS approved
               FROM data_dictionary_tables WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

        dict_c_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(is_approved), 0) AS approved
               FROM data_dictionary_columns WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

        # ── Domain stats ──────────────────────────────────────────────────
        domain_dist = conn.execute(
            """SELECT domain, COUNT(*) AS cnt
               FROM domain_assignments WHERE source_id = ?
               GROUP BY domain ORDER BY cnt DESC""",
            (source_id,),
        ).fetchall()
        domain_total_assigned = conn.execute(
            """SELECT COUNT(*) FROM domain_assignments
               WHERE source_id = ? AND domain != 'Unknown'""",
            (source_id,),
        ).fetchone()[0]
        domain_total = conn.execute(
            "SELECT COUNT(*) FROM domain_assignments WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]

        # ── Entity stats ──────────────────────────────────────────────────
        entity_dist = conn.execute(
            """SELECT entity, COUNT(*) AS cnt
               FROM entity_assignments WHERE source_id = ?
               GROUP BY entity ORDER BY cnt DESC""",
            (source_id,),
        ).fetchall()
        entity_total_assigned = conn.execute(
            """SELECT COUNT(*) FROM entity_assignments
               WHERE source_id = ? AND entity != 'Unknown'""",
            (source_id,),
        ).fetchone()[0]
        entity_total = conn.execute(
            "SELECT COUNT(*) FROM entity_assignments WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]

        # ── Relationship stats ────────────────────────────────────────────
        rel_row = None
        if schema_snap_id is not None:
            rel_row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(DISTINCT from_table_fqn) AS tables_with_fks,
                          COUNT(DISTINCT to_table_fqn) AS tables_referenced
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ?
                     AND relationship_status IN ('AUTO', 'APPROVED')""",
                (source_id, schema_snap_id),
            ).fetchone()

        # ── PII stats (from latest profiling) ─────────────────────────────
        pii_row = None
        if prof_snap_id is not None:
            pii_row = conn.execute(
                """SELECT
                     COALESCE(SUM(pii_name_heuristic), 0)                   AS flagged,
                     COALESCE(SUM(pii_confirmed), 0)                         AS confirmed,
                     COALESCE(SUM(
                       CASE WHEN pii_name_heuristic = 1
                            AND  pii_confirmed = 0 THEN 1 ELSE 0 END), 0)   AS pending
                   FROM profiling_column_profiles
                   WHERE source_id = ? AND profiling_snapshot_id = ?""",
                (source_id, prof_snap_id),
            ).fetchone()

        # ── Governance: approved dict + domain + entity per table ─────────
        tables_approved_dict = conn.execute(
            "SELECT COUNT(*) FROM data_dictionary_tables "
            "WHERE source_id = ? AND is_approved = 1",
            (source_id,),
        ).fetchone()[0]

    finally:
        conn.close()

    # ── Compute coverage ratios ───────────────────────────────────────────
    schema_table_count = schema_snap["table_count"] if schema_snap else 0

    def _safe_rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator > 0 else 0.0

    profile_coverage  = _safe_rate(detail.tables_profiled if detail else 0, schema_table_count)
    dict_coverage     = _safe_rate(dict_t_row["total"],                                schema_table_count)
    domain_coverage   = _safe_rate(domain_total_assigned,                              schema_table_count)
    entity_coverage   = _safe_rate(entity_total_assigned,                              schema_table_count)
    rel_coverage      = _safe_rate(rel_row["tables_with_fks"]    if rel_row else 0,    schema_table_count)

    # Readiness = average of all five coverage scores
    readiness_score = round(
        (profile_coverage + dict_coverage + domain_coverage + entity_coverage + rel_coverage) / 5,
        4,
    )

    return {
        "source": {
            "id":                  source_id,
            "display_name":        source_row["display_name"],
            "source_type":         source_row["source_type"],
            "source_category":     source_row["source_category"],
            "last_discovered_at":  source_row["last_discovered_at"],
        },
        "schema": {
            "table_count":      schema_snap["table_count"]    if schema_snap else 0,
            "view_count":       schema_snap["view_count"]     if schema_snap else 0,
            "column_count":     schema_snap["column_count"]   if schema_snap else 0,
            "last_discovered_at": schema_snap["discovered_at"] if schema_snap else None,
        },
        "profiling": {
            "snapshot_id":       detail.id               if detail else None,
            "tables_profiled":   detail.tables_profiled  if detail else 0,
            "columns_profiled":  detail.columns_profiled if detail else 0,
            "pii_columns_found": detail.pii_columns_found if detail else 0,
            "status":            detail.status            if detail else None,
            "completed_at":      detail.completed_at      if detail else None,
        },
        "dictionary": {
            "tables_with_definitions": int(dict_t_row["total"]),
            "tables_approved":         int(dict_t_row["approved"]),
            "columns_with_labels":     int(dict_c_row["total"]),
            "columns_approved":        int(dict_c_row["approved"]),
        },
        "domains": {
            "total_unique_domains": len({r["domain"] for r in domain_dist if r["domain"] != "Unknown"}),
            "tables_assigned":      domain_total_assigned,
            "tables_unknown":       domain_total - domain_total_assigned,
            "distribution":         {r["domain"]: r["cnt"] for r in domain_dist},
        },
        "entities": {
            "total_unique_entities": len({r["entity"] for r in entity_dist if r["entity"] != "Unknown"}),
            "tables_assigned":       entity_total_assigned,
            "tables_unknown":        entity_total - entity_total_assigned,
            "distribution":          {r["entity"]: r["cnt"] for r in entity_dist},
        },
        "relationships": {
            "total_relationships":    rel_row["total"]          if rel_row else 0,
            "tables_with_fks":        rel_row["tables_with_fks"] if rel_row else 0,
            "tables_referenced":      rel_row["tables_referenced"] if rel_row else 0,
        },
        "pii": {
            "columns_flagged":         int(pii_row["flagged"])   if pii_row else 0,
            "columns_confirmed":       int(pii_row["confirmed"]) if pii_row else 0,
            "columns_pending_review":  int(pii_row["pending"])   if pii_row else 0,
        },
        "governance": {
            "tables_with_approved_dictionary": tables_approved_dict,
            "tables_with_domain":              domain_total_assigned,
            "tables_with_entity":              entity_total_assigned,
            "readiness_score":                 readiness_score,
        },
        "coverage": {
            "profile_coverage":  profile_coverage,
            "dictionary_coverage": dict_coverage,
            "domain_coverage":   domain_coverage,
            "entity_coverage":   entity_coverage,
            "relationship_coverage": rel_coverage,
            "readiness_score":   readiness_score,
        },
    }
