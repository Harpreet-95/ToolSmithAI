import json
import logging
import re
from datetime import datetime, timezone

from data.db import get_connection
from data.profiling_service import get_key_candidate_columns

logger = logging.getLogger(__name__)

# Relationship Intelligence (Program 3 Phase 1) — inference is discarded below this score.
MIN_SUGGEST_CONFIDENCE = 30

# Guards inference cost on very wide schemas — see discover_relationship_candidates().
_MAX_CANDIDATE_PAIRS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_profiling_snap_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM profiling_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


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


# ---------------------------------------------------------------------------
# Relationship Intelligence (Program 3 Phase 1) — candidate discovery,
# confidence scoring, and evidence capture for relationships beyond declared
# foreign keys. Declared-FK extraction above this point is untouched.
#
# Discovered candidates are persisted with relationship_status='PENDING' and
# are NOT consumed by lineage/semantic-layer/knowledge-graph reasoning until
# a later governance phase approves them — this module only discovers and
# scores, it never marks a candidate as trusted.
# ---------------------------------------------------------------------------

_ID_STEM_STOPWORDS = {
    "id", "key", "code", "guid", "uuid", "fk", "no", "num", "number",
    "ref", "reference",
}

_TYPE_GROUPS = [
    {"int", "bigint", "smallint", "tinyint", "integer", "int4", "int8", "serial", "bigserial"},
    {"varchar", "nvarchar", "char", "nchar", "text", "string", "ntext", "varchar2"},
    {"uniqueidentifier", "guid", "uuid"},
    {"decimal", "numeric", "money", "smallmoney", "float", "real", "double", "double precision"},
]

_INFERENCE_METHOD_TO_TYPE = {
    "name_match":    "INFERRED_NAME_MATCH",
    "value_overlap": "INFERRED_VALUE_OVERLAP",
    "business_key":  "INFERRED_BUSINESS_KEY",
}

_NAME_MATCH_POINTS    = 25
_DATATYPE_POINTS      = 10
_TARGET_KEY_POINTS    = 20
_DICTIONARY_POINTS    = 15
_DOMAIN_POINTS        = 10
_ENTITY_POINTS        = 10
_VALUE_OVERLAP_POINTS = 20


def _split_fqn(table_fqn: str) -> tuple[str, str]:
    """Split 'schema.table' into (schema, table); schema is '' when absent."""
    if "." in table_fqn:
        schema, _, table = table_fqn.partition(".")
        return schema, table
    return "", table_fqn


def _singularize(word: str) -> str:
    """Naive plural -> singular for table-name fallback stems (best-effort only)."""
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _normalize_column_name(name: str, table_fqn: str | None = None) -> str:
    """
    Reduce a column name to a normalized business-term stem for bucket
    matching: 'customer_id' -> 'customer', 'CustomerID' -> 'customer',
    'order_no' -> 'order'. Trailing id/key/code/guid/... tokens are stripped;
    everything else is kept so unrelated columns don't collapse together.

    When stripping leaves nothing — a bare 'id'/'key'/'guid' column, the most
    common primary-key naming convention — falls back to the table's own
    (singularized) name so 'customers.id' buckets with 'orders.customer_id'
    instead of bucketing with every other bare 'id' column in the schema.
    """
    snake = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)       # 'CustomerId' -> 'Customer_Id'
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)     # 'CustomerID' -> 'Customer_ID'
    snake = re.sub(r"[^a-zA-Z0-9]+", "_", snake).lower()
    tokens = [t for t in snake.split("_") if t]
    while tokens and tokens[-1] in _ID_STEM_STOPWORDS:
        tokens.pop()
    if tokens:
        return "_".join(tokens)
    if table_fqn:
        _, table = _split_fqn(table_fqn)
        table_norm = re.sub(r"[^a-zA-Z0-9]+", "_", table).lower().strip("_")
        if table_norm:
            return _singularize(table_norm)
    return snake


def _types_compatible(type_a: str | None, type_b: str | None) -> bool:
    a = (type_a or "").strip().lower()
    b = (type_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return any(a in group and b in group for group in _TYPE_GROUPS)


def _key_strength(col: dict) -> float:
    """Higher = looks more like the 'one' (parent/target) side of a join."""
    score = float(col.get("uniqueness_score") or 0.0)
    if col.get("is_primary_key"):
        score += 3.0
    if col.get("is_identity"):
        score += 2.0
    return score


def _pick_direction(a: dict, b: dict) -> tuple[dict, dict]:
    """Choose (from_col, to_col) so to_col is the more PK-like side."""
    if _key_strength(b) >= _key_strength(a):
        return a, b
    return b, a


def _infer_cardinality(from_col: dict, to_col: dict) -> str:
    """1:1 / 1:N / N:1 / N:N from each side's uniqueness, post-direction-pick."""
    def _is_unique_side(c: dict) -> bool:
        return (
            bool(c.get("is_primary_key"))
            or bool(c.get("is_identity"))
            or (c.get("uniqueness_score") or 0.0) >= 0.95
        )

    to_unique = _is_unique_side(to_col)
    from_unique = _is_unique_side(from_col)
    if to_unique and from_unique:
        return "ONE_TO_ONE"
    if to_unique and not from_unique:
        return "MANY_TO_ONE"
    if from_unique and not to_unique:
        return "ONE_TO_MANY"
    if not from_unique and not to_unique:
        return "MANY_TO_MANY"
    return "UNKNOWN"


def _load_sample_values(conn, col_profile_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT value FROM profiling_value_samples "
        "WHERE profiling_column_profile_id = ? AND value IS NOT NULL",
        (col_profile_id,),
    ).fetchall()
    return {str(r["value"]).strip().lower() for r in rows if r["value"] is not None}


def _value_overlap_score(conn, from_col_id: int, to_col_id: int) -> float | None:
    """
    Approximate containment of from-column sample values within to-column
    sample values, using already-persisted profiling_value_samples rows only
    — no query is issued against the customer's source database here.

    Returns None when either side has no sampled values yet (today this is
    common — profiling_value_samples has no populating writer wired in this
    phase). Callers must treat None as 'unverified', not as zero overlap.
    """
    from_values = _load_sample_values(conn, from_col_id)
    to_values = _load_sample_values(conn, to_col_id)
    if not from_values or not to_values:
        return None
    overlap = from_values & to_values
    return round(len(overlap) / len(from_values), 3)


def _score_candidate(
    from_col: dict,
    to_col: dict,
    *,
    name_matched: bool,
    from_dict: dict | None,
    to_dict: dict | None,
    from_domain: str | None,
    to_domain: str | None,
    from_entity: str | None,
    to_entity: str | None,
    value_overlap: float | None,
) -> dict:
    """
    Weighted 0-100 confidence score from independent evidence signals.
    Caller guarantees from_col/to_col already passed the datatype-compatible
    hard gate before this is called.
    """
    evidence: list[dict] = []
    weaknesses: list[str] = []
    raw = 0.0

    if name_matched:
        raw += _NAME_MATCH_POINTS
        evidence.append({
            "signal": "name_match", "points": _NAME_MATCH_POINTS,
            "detail": (
                f"Column names normalize to the same term "
                f"('{from_col['column_name']}' ~ '{to_col['column_name']}')."
            ),
        })
    else:
        weaknesses.append(
            "Column names do not match; relationship inferred from shared "
            "business context only."
        )

    raw += _DATATYPE_POINTS
    evidence.append({
        "signal": "datatype_compatible", "points": _DATATYPE_POINTS,
        "detail": f"'{from_col['data_type']}' is compatible with '{to_col['data_type']}'.",
    })

    target_points = 0
    if to_col.get("is_primary_key"):
        target_points = _TARGET_KEY_POINTS
        evidence.append({
            "signal": "target_primary_key", "points": target_points,
            "detail": f"Target column '{to_col['column_name']}' is a declared primary key.",
        })
    elif to_col.get("is_identity"):
        target_points = round(_TARGET_KEY_POINTS * 0.9)
        evidence.append({
            "signal": "target_identity", "points": target_points,
            "detail": f"Target column '{to_col['column_name']}' is an identity column.",
        })
    elif (to_col.get("uniqueness_score") or 0.0) >= 0.95:
        target_points = round(_TARGET_KEY_POINTS * 0.8)
        evidence.append({
            "signal": "target_near_unique", "points": target_points,
            "detail": f"Target column uniqueness score is {to_col.get('uniqueness_score'):.2f}.",
        })
    elif (to_col.get("guid_match_rate") or 0.0) >= 0.8:
        target_points = round(_TARGET_KEY_POINTS * 0.6)
        evidence.append({
            "signal": "target_guid_shaped", "points": target_points,
            "detail": "Target column values are GUID-shaped.",
        })
    else:
        weaknesses.append(
            f"Target column '{to_col['column_name']}' is not clearly unique "
            "(no primary key/identity flag, uniqueness below threshold) — "
            "may not be a valid join target."
        )
    raw += target_points

    dict_points = 0
    if from_dict and to_dict and from_dict.get("is_id") and to_dict.get("is_id"):
        dict_points = _DICTIONARY_POINTS
        evidence.append({
            "signal": "dictionary_id_match", "points": dict_points,
            "detail": "Both columns are marked as identifier columns in the business dictionary.",
        })
    elif (from_dict and from_dict.get("is_id")) or (to_dict and to_dict.get("is_id")):
        dict_points = round(_DICTIONARY_POINTS * 0.5)
        evidence.append({
            "signal": "dictionary_id_partial", "points": dict_points,
            "detail": "One column is marked as an identifier column in the business dictionary.",
        })
    raw += dict_points

    domain_points = 0
    if from_domain and to_domain and from_domain != "Unknown" and from_domain == to_domain:
        domain_points = _DOMAIN_POINTS
        evidence.append({
            "signal": "same_domain", "points": domain_points,
            "detail": f"Both tables are assigned to the '{from_domain}' business domain.",
        })
    raw += domain_points

    entity_points = 0
    if from_entity and to_entity and from_entity != "Unknown" and from_entity == to_entity:
        entity_points = _ENTITY_POINTS
        evidence.append({
            "signal": "same_entity", "points": entity_points,
            "detail": f"Both tables are assigned to the '{from_entity}' business entity.",
        })
    raw += entity_points

    overlap_points = 0
    if value_overlap is None:
        weaknesses.append(
            "Sampled value overlap could not be verified (no profiling "
            "samples available for one or both columns)."
        )
    else:
        overlap_points = round(_VALUE_OVERLAP_POINTS * value_overlap)
        raw += overlap_points
        evidence.append({
            "signal": "value_overlap", "points": overlap_points,
            "detail": (
                f"{value_overlap:.0%} of sampled values in "
                f"'{from_col['column_name']}' also appear in "
                f"'{to_col['column_name']}' (approximate, sample-based)."
            ),
        })
        if value_overlap < 0.3:
            weaknesses.append(
                f"Low sampled value overlap ({value_overlap:.0%}) — possible false positive."
            )

    score = int(min(100, round(raw)))

    if not name_matched:
        inference_method = "value_overlap" if (value_overlap or 0.0) >= 0.7 else "business_key"
    elif value_overlap is not None and overlap_points > _NAME_MATCH_POINTS:
        inference_method = "value_overlap"
    else:
        inference_method = "name_match"

    return {
        "score": score,
        "evidence": evidence,
        "weaknesses": weaknesses,
        "inference_method": inference_method,
    }


def _pair_key(a: dict, b: dict) -> frozenset:
    return frozenset({(a["table_fqn"], a["column_name"]), (b["table_fqn"], b["column_name"])})


def discover_relationship_candidates(
    source_id: int,
    user_id: str,
    schema_snap_id: int | None = None,
    profiling_snap_id: int | None = None,
) -> dict | None:
    """
    Infer candidate relationships beyond declared foreign keys, using
    column-name matching, profiling statistics, business dictionary labels,
    domain/entity assignments, and sampled value overlap.

    Candidates are scored 0-100 and persisted to table_relationships with
    relationship_status='PENDING' — never auto-trusted. A pair already
    covered by ANY existing row (declared FK or a prior candidate) is
    skipped via INSERT OR IGNORE against the existing unique index, so
    declared FK rows are never touched.

    Only reads ToolSmithAI's own metadata store (profiling, dictionary,
    domain, entity tables already populated by prior phases) — no query is
    issued against the customer's source database here.

    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        snap_id = _resolve_snapshot_id(conn, source_id, schema_snap_id)
        prof_snap_id = profiling_snap_id or _latest_profiling_snap_id(conn, source_id)

        result = {
            "schema_snapshot_id":                  snap_id,
            "profiling_snapshot_id":               prof_snap_id,
            "candidates_evaluated":                0,
            "candidates_persisted":                0,
            "candidates_discarded_low_confidence":  0,
            "candidates_skipped_existing":          0,
        }
        if snap_id is None or prof_snap_id is None:
            return result

        columns = get_key_candidate_columns(conn, source_id, prof_snap_id)
        if len(columns) < 2:
            return result

        existing_pairs = {
            (r["from_table_fqn"], r["from_column"], r["to_table_fqn"], r["to_column"])
            for r in conn.execute(
                "SELECT from_table_fqn, from_column, to_table_fqn, to_column "
                "FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
                (source_id, snap_id),
            ).fetchall()
        }

        dict_rows = {
            (r["table_fqn"], r["column_name"]): dict(r)
            for r in conn.execute(
                "SELECT table_fqn, column_name, business_label, meaning, is_id "
                "FROM data_dictionary_columns WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        }
        domain_rows = {
            r["table_fqn"]: r["domain"]
            for r in conn.execute(
                "SELECT table_fqn, domain FROM domain_assignments WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        }
        entity_rows = {
            r["table_fqn"]: r["entity"]
            for r in conn.execute(
                "SELECT table_fqn, entity FROM entity_assignments WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        }

        cols_by_table: dict[str, list[dict]] = {}
        for col in columns:
            cols_by_table.setdefault(col["table_fqn"], []).append(col)

        buckets: dict[str, list[dict]] = {}
        for col in columns:
            buckets.setdefault(
                _normalize_column_name(col["column_name"], col["table_fqn"]), []
            ).append(col)

        seen_unordered: set[frozenset] = set()
        pairs_to_score: list[tuple[dict, dict, bool]] = []  # (col_a, col_b, name_matched)

        # Pass 1 — within-bucket pairs (column names normalize to the same stem).
        for cols in buckets.values():
            if len(cols) < 2:
                continue
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    a, b = cols[i], cols[j]
                    if a["table_fqn"] == b["table_fqn"]:
                        continue
                    key = _pair_key(a, b)
                    if key in seen_unordered:
                        continue
                    seen_unordered.add(key)
                    pairs_to_score.append((a, b, True))

        # Pass 2 — cross-bucket pairs justified by a shared domain or entity
        # assignment. Bounded by cluster size (tables sharing a domain/entity),
        # not by total schema width — never a full cross product of all columns.
        cluster_table_pairs: set[tuple[str, str]] = set()
        for assignment_map in (domain_rows, entity_rows):
            groups: dict[str, list[str]] = {}
            for table_fqn, label in assignment_map.items():
                if label and label != "Unknown" and table_fqn in cols_by_table:
                    groups.setdefault(label, []).append(table_fqn)
            for tables in groups.values():
                if len(tables) < 2:
                    continue
                for i in range(len(tables)):
                    for j in range(i + 1, len(tables)):
                        cluster_table_pairs.add((tables[i], tables[j]))

        for t_a, t_b in cluster_table_pairs:
            for a in cols_by_table[t_a]:
                for b in cols_by_table[t_b]:
                    if _normalize_column_name(a["column_name"], a["table_fqn"]) == \
                            _normalize_column_name(b["column_name"], b["table_fqn"]):
                        continue  # already covered by Pass 1
                    key = _pair_key(a, b)
                    if key in seen_unordered:
                        continue
                    seen_unordered.add(key)
                    pairs_to_score.append((a, b, False))

        if len(pairs_to_score) > _MAX_CANDIDATE_PAIRS:
            logger.warning(
                "discover_relationship_candidates: truncating %d candidate pairs to %d for source_id=%s",
                len(pairs_to_score), _MAX_CANDIDATE_PAIRS, source_id,
            )
            pairs_to_score = pairs_to_score[:_MAX_CANDIDATE_PAIRS]

        now = _now()
        for col_a, col_b, name_matched in pairs_to_score:
            from_col, to_col = _pick_direction(col_a, col_b)

            if (from_col["table_fqn"], from_col["column_name"],
                    to_col["table_fqn"], to_col["column_name"]) in existing_pairs:
                result["candidates_skipped_existing"] += 1
                continue
            if (to_col["table_fqn"], to_col["column_name"],
                    from_col["table_fqn"], from_col["column_name"]) in existing_pairs:
                result["candidates_skipped_existing"] += 1
                continue

            if not _types_compatible(from_col.get("data_type"), to_col.get("data_type")):
                continue

            result["candidates_evaluated"] += 1

            value_overlap = _value_overlap_score(conn, from_col["id"], to_col["id"])
            scored = _score_candidate(
                from_col, to_col,
                name_matched=name_matched,
                from_dict=dict_rows.get((from_col["table_fqn"], from_col["column_name"])),
                to_dict=dict_rows.get((to_col["table_fqn"], to_col["column_name"])),
                from_domain=domain_rows.get(from_col["table_fqn"]),
                to_domain=domain_rows.get(to_col["table_fqn"]),
                from_entity=entity_rows.get(from_col["table_fqn"]),
                to_entity=entity_rows.get(to_col["table_fqn"]),
                value_overlap=value_overlap,
            )

            if scored["score"] < MIN_SUGGEST_CONFIDENCE:
                result["candidates_discarded_low_confidence"] += 1
                continue

            rel_type = _INFERENCE_METHOD_TO_TYPE[scored["inference_method"]]
            cardinality = _infer_cardinality(from_col, to_col)
            from_schema, from_table = _split_fqn(from_col["table_fqn"])
            to_schema, to_table = _split_fqn(to_col["table_fqn"])
            evidence_json = json.dumps({
                "evidence":    scored["evidence"],
                "weaknesses":  scored["weaknesses"],
            })

            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO table_relationships
                    (source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column,
                     to_schema, to_table, to_table_fqn, to_column,
                     relationship_name, relationship_type, confidence, evidence_json, created_at,
                     relationship_confidence, inference_method, relationship_status, cardinality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, snap_id,
                    from_schema, from_table, from_col["table_fqn"], from_col["column_name"],
                    to_schema, to_table, to_col["table_fqn"], to_col["column_name"],
                    f"inferred_{scored['inference_method']}", rel_type,
                    round(scored["score"] / 100.0, 3), evidence_json, now,
                    scored["score"], scored["inference_method"], "PENDING", cardinality,
                ),
            )
            if conn.total_changes > before:
                result["candidates_persisted"] += 1
            else:
                result["candidates_skipped_existing"] += 1

        conn.commit()
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Relationship explanation (Program 3 Phase 1, Step 4)
# ---------------------------------------------------------------------------

_INFERENCE_METHOD_LABEL = {
    "name_match":    "matching column names",
    "value_overlap": "overlapping sampled values",
    "business_key":  "shared business domain/entity context",
}


def _parse_evidence_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _recommend_action(confidence_tier: str | None, relationship_status: str | None) -> str:
    if relationship_status == "APPROVED":
        return "Already approved — no action needed."
    if relationship_status == "REJECTED":
        return "Already rejected — no action needed."
    if confidence_tier in ("VERY_HIGH", "HIGH"):
        return "Evidence is strong — approve."
    if confidence_tier == "MEDIUM":
        return "Review the evidence before approving."
    return "Weak evidence — verify manually before approving."


def explain_relationship(source_id: int, user_id: str, relationship_id: int) -> dict | None:
    """
    Return a structured explanation for one table_relationships row: why it
    exists, what evidence supports it, known weaknesses, and a recommended
    next action. Covers both declared FK rows and inferred candidates.

    Returns None when the source does not exist or is not owned by user_id,
    or when no relationship with relationship_id exists for that source.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        row = conn.execute(
            "SELECT * FROM table_relationships WHERE id = ? AND source_id = ?",
            (relationship_id, source_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    rel = dict(row)
    inference_method = rel.get("inference_method") or "declared_fk"

    relationship_confidence = rel.get("relationship_confidence")
    if relationship_confidence is None:
        relationship_confidence = int(round(float(rel.get("confidence") or 0.0) * 100))

    # Reuses governance_service's existing 0-1 tier breakpoints (VERY_HIGH/HIGH/
    # MEDIUM/LOW) rather than defining a second tier function for the 0-100 scale.
    from data.governance_service import _confidence_tier
    confidence_tier = _confidence_tier(relationship_confidence / 100.0)

    parsed = _parse_evidence_json(rel.get("evidence_json"))
    evidence = parsed.get("evidence") or []
    weaknesses = parsed.get("weaknesses") or []

    if inference_method == "declared_fk":
        why = (
            f"'{rel['from_table_fqn']}.{rel['from_column']}' declares a foreign key to "
            f"'{rel['to_table_fqn']}.{rel['to_column']}' in the source database schema."
        )
        if not evidence:
            evidence = [{
                "signal": "declared_fk",
                "detail": "Extracted directly from the source database's declared foreign key constraint.",
            }]
        recommended_action = (
            "No review needed — this relationship is schema-declared and trusted automatically."
        )
    else:
        method_label = _INFERENCE_METHOD_LABEL.get(inference_method, inference_method)
        why = (
            f"Suggested because '{rel['from_table_fqn']}.{rel['from_column']}' and "
            f"'{rel['to_table_fqn']}.{rel['to_column']}' show {method_label}, "
            f"scored {relationship_confidence}/100 ({confidence_tier})."
        )
        recommended_action = _recommend_action(confidence_tier, rel.get("relationship_status"))

    return {
        "relationship_id":     rel["id"],
        "relationship_type":   rel["relationship_type"],
        "inference_method":    inference_method,
        "relationship_status": rel.get("relationship_status") or "AUTO",
        "confidence":          relationship_confidence,
        "confidence_tier":     confidence_tier,
        "cardinality":         rel.get("cardinality") or "UNKNOWN",
        "from_table_fqn":      rel["from_table_fqn"],
        "from_column":         rel["from_column"],
        "to_table_fqn":        rel["to_table_fqn"],
        "to_column":           rel["to_column"],
        "why":                 why,
        "evidence":            evidence,
        "weaknesses":          weaknesses,
        "recommended_action":  recommended_action,
    }


# ---------------------------------------------------------------------------
# Governance integration (Program 3 Phase 1, Step 5)
#
# Reuses the existing governance lifecycle in governance_service.py — no new
# approval engine. These two functions are the authoritative write path for
# relationship_status, mirroring domain_learning_service.approve_domain_rule/
# reject_domain_rule exactly (status check, then ownership check, then update,
# then best-effort governance event logging).
# ---------------------------------------------------------------------------

def approve_relationship(relationship_id: int, user_id: str) -> dict | None:
    """
    Approve a PENDING inferred relationship — sets relationship_status='APPROVED'.

    Declared FK rows (relationship_status='AUTO') and already-decided rows
    cannot be approved through this path.

    Returns:
        Updated relationship dict, or None if the relationship does not exist
        or user_id does not own its source.

    Raises:
        ValueError: relationship_status is not 'PENDING'.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM table_relationships WHERE id = ?", (relationship_id,)
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["relationship_status"] != "PENDING":
            raise ValueError(
                f"Relationship {relationship_id} is '{d['relationship_status']}' "
                "and cannot be approved again."
            )
        if not _verify_source_ownership(conn, d["source_id"], user_id):
            return None

        conn.execute(
            """
            UPDATE table_relationships
               SET relationship_status = 'APPROVED',
                   approved_by         = ?,
                   approved_at         = ?
             WHERE id = ?
            """,
            (user_id, now, relationship_id),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM table_relationships WHERE id = ?", (relationship_id,)
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        _confidence = (dict(updated).get("relationship_confidence") or 0) / 100.0
        log_governance_event(
            object_type_id = GovernedObjectType.RELATIONSHIP_SUGGESTION,
            object_id      = str(relationship_id),
            event_type     = "APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "relationship_service",
        )
        upsert_governance_state(
            object_type_id   = GovernedObjectType.RELATIONSHIP_SUGGESTION,
            object_id        = str(relationship_id),
            approval_state   = GovernanceState.HUMAN_APPROVED,
            confidence_score = _confidence,
            reviewer_id      = user_id,
            reviewed_at      = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for relationship.suggestion id=%s", relationship_id
        )

    return dict(updated)


def reject_relationship(relationship_id: int, user_id: str) -> dict | None:
    """
    Reject a PENDING inferred relationship — sets relationship_status='REJECTED'.

    Declared FK rows (relationship_status='AUTO') and already-decided rows
    cannot be rejected through this path.

    Returns:
        Updated relationship dict, or None if the relationship does not exist
        or user_id does not own its source.

    Raises:
        ValueError: relationship_status is not 'PENDING'.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM table_relationships WHERE id = ?", (relationship_id,)
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["relationship_status"] != "PENDING":
            raise ValueError(
                f"Relationship {relationship_id} is '{d['relationship_status']}' "
                "and cannot be rejected again."
            )
        if not _verify_source_ownership(conn, d["source_id"], user_id):
            return None

        conn.execute(
            """
            UPDATE table_relationships
               SET relationship_status = 'REJECTED',
                   approved_by         = ?,
                   approved_at         = ?
             WHERE id = ?
            """,
            (user_id, now, relationship_id),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM table_relationships WHERE id = ?", (relationship_id,)
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        log_governance_event(
            object_type_id = GovernedObjectType.RELATIONSHIP_SUGGESTION,
            object_id      = str(relationship_id),
            event_type     = "REJECTED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.REJECTED,
            actor_id       = user_id,
            source_service = "relationship_service",
        )
        upsert_governance_state(
            object_type_id = GovernedObjectType.RELATIONSHIP_SUGGESTION,
            object_id      = str(relationship_id),
            approval_state = GovernanceState.REJECTED,
            reviewer_id    = user_id,
            reviewed_at    = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for relationship.suggestion id=%s", relationship_id
        )

    return dict(updated)
