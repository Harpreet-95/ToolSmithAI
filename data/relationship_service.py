import json
import logging
import re
from datetime import datetime, timezone

from data.db import get_connection
from data.profiling_service import get_key_candidate_columns
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot
from core.connectors.schema import normalize_data_type

logger = logging.getLogger(__name__)

# Relationship Intelligence (Program 3 Phase 1) — inference is discarded below this score.
MIN_SUGGEST_CONFIDENCE = 30

# Guards inference cost on very wide schemas — see discover_relationship_candidates().
_MAX_CANDIDATE_PAIRS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Phase 6, Step 6B — deterministic evidence-precedence ranking, shared by
# persist_relationships()'s upgrade-on-conflict logic below. Pure function of
# the three provenance fields already used by core.sqlservice.retrieval_
# documents.classify_relationship_row() to decide confirmed vs inferred —
# this ranking is a strict refinement of that same policy (never a
# competing one): every rank >= 1 row is "confirmed" under that function,
# and rank 0 is exactly its "inferred" bucket. Higher rank always wins a
# same-identity conflict, independent of insertion order.
#   3 — a physical, schema-declared foreign key (the strongest possible
#       evidence: relationship_type='FOREIGN_KEY', or the equivalent
#       inference_method='declared_fk' marker).
#   2 — SQL Server's own compiled view-dependency catalog
#       (inference_method='catalog_view_dependency') — authoritative
#       lineage, but never assumed to carry real join columns (see
#       data/knowledge_graph_service.py's _classify_graph_edge, which
#       additionally checks for the "*"/"" sentinel before ever calling
#       an edge an executable join).
#   1 — a human-approved relationship (relationship_status='APPROVED')
#       that doesn't already qualify for rank 2 or 3 on its own.
#   0 — everything else: PENDING/REJECTED, or an AUTO-status row backed by
#       neither a declared-FK nor catalog-dependency inference_method (a
#       regex/heuristic writer that happens to use this column's own
#       'AUTO' DEFAULT rather than 'PENDING' — never trusted on
#       relationship_status alone, matching classify_relationship_row).
_RANK_PHYSICAL_FK = 3
_RANK_CATALOG_VIEW_DEPENDENCY = 2
_RANK_APPROVED = 1
_RANK_UNCONFIRMED = 0


def _evidence_rank(
    relationship_type: str | None,
    inference_method: str | None,
    relationship_status: str | None,
) -> int:
    """Higher = stronger evidence for the same logical (from, to) edge.
    Never raises; unrecognized/missing values rank lowest (fail-closed,
    the same convention classify_relationship_row already uses)."""
    if relationship_type == "FOREIGN_KEY" or inference_method == "declared_fk":
        return _RANK_PHYSICAL_FK
    if inference_method == "catalog_view_dependency":
        return _RANK_CATALOG_VIEW_DEPENDENCY
    if relationship_status == "APPROVED":
        return _RANK_APPROVED
    return _RANK_UNCONFIRMED


# The SAME ranking, expressed as a SQL CASE expression over the EXISTING
# row's own columns — evaluated only in persist_relationships()'s
# conditional UPDATE, never duplicated anywhere else. Kept textually
# adjacent to _evidence_rank() above specifically so the two can be eyeballed
# in lockstep; tests/test_relationship_evidence_precedence.py additionally
# cross-checks them against the same input matrix so the two can never
# silently drift apart.
_EXISTING_RANK_SQL = """
    CASE
        WHEN relationship_type = 'FOREIGN_KEY' THEN 3
        WHEN inference_method  = 'declared_fk' THEN 3
        WHEN inference_method  = 'catalog_view_dependency' THEN 2
        WHEN relationship_status = 'APPROVED' THEN 1
        ELSE 0
    END
"""


def _audit_discovery(source_id: int, user_id: str, result: dict) -> None:
    try:
        from data.audit import log_audit_event
        log_audit_event(
            {
                "task_type": "relationship_candidate_discovery",
                "original_input": json.dumps({"source_id": source_id, **result}),
                "status": "success",
            },
            user_id=user_id,
        )
    except Exception:
        logger.warning(
            "discover_relationship_candidates: audit logging failed for source_id=%s", source_id,
            exc_info=True,
        )


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
                    # Explicit (Step 6B) rather than left to persist_
                    # relationships()'s own DEFAULT-mirroring fallback — a
                    # schema-declared FK is unconditionally trustworthy
                    # (also confirmed via relationship_type='FOREIGN_KEY'
                    # alone), so 'AUTO' is correct and intentional here.
                    "relationship_status": "AUTO",
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
    Idempotent, evidence-precedence-aware upsert. The unique index on
    (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column) is
    the logical-edge identity — unchanged by this function (Phase 6, Step
    6B deliberately does NOT fold relationship_status/type/inference_method
    into that index, which would only create parallel duplicate rows for
    the same real edge). Returns the number of BRAND NEW rows inserted (0
    on a pure repeat run) — an in-place upgrade of an existing row is never
    counted here, matching every existing caller's "how many new edges did
    this run find" usage of the return value.

    Phase 6, Step 6B — evidence-precedence correction. Previously this was
    a plain `INSERT OR IGNORE`: whichever writer recorded a given logical
    edge FIRST won permanently, even if a later call carried strictly
    stronger evidence for the exact same edge (e.g. data/view_semantics_
    service.py's regex-derived, PENDING-status guess landing before data/
    view_dependency_service.py's authoritative SQL Server catalog
    confirmation for the same (view, target) pair). Now, for every row:
      1. INSERT OR IGNORE — creates the row if this exact identity doesn't
         exist yet (unchanged from before; first-ever sighting of an edge
         is always just inserted).
      2. A conditional UPDATE, gated on `_evidence_rank(new) >
         _evidence_rank(existing)` (computed identically in Python via
         _evidence_rank() and in SQL via _EXISTING_RANK_SQL — see both
         definitions above) — overwrites the row's provenance/confidence
         fields ONLY when the incoming evidence is STRICTLY stronger than
         what's already stored. Equal-or-weaker incoming evidence leaves
         the existing row completely untouched, which gives exactly the
         three required properties: stronger evidence upgrades a weaker
         row regardless of write order; weaker evidence can never downgrade
         a stronger row regardless of write order; and re-persisting
         identical evidence is a no-op (rank ties, nothing changes).

    approved_by/approved_at are deliberately never touched by the UPDATE —
    a human approval decision on a row that later gets structurally
    upgraded (e.g. a manually-approved heuristic guess that a subsequent
    catalog scan confirms authoritatively) is left as historical context,
    not silently cleared; this function only ever adds/strengthens
    evidence, never touches the separate human-approval workflow in
    approve_relationship()/reject_relationship().
    """
    if not relationships:
        return 0

    before = conn.execute(
        "SELECT COUNT(*) FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
        (source_id, snapshot_id),
    ).fetchone()[0]

    for r in relationships:
        # Declared FKs (every pre-existing caller) never set this key --
        # 'declared_fk' matches the column's own DEFAULT exactly, so
        # behavior is unchanged for them. View-dependency writers pass
        # 'catalog_view_dependency' / 'view_dependency' explicitly.
        inference_method = r.get("inference_method", "declared_fk")
        # Mirrors the column's own DEFAULT ('AUTO') for any caller that
        # doesn't (yet) pass this explicitly — every current caller now
        # passes it explicitly; kept only as a backstop.
        relationship_status = r.get("relationship_status", "AUTO")

        conn.execute(
            """
            INSERT OR IGNORE INTO table_relationships
                (source_id, snapshot_id, from_schema, from_table, from_table_fqn,
                 from_column, to_schema, to_table, to_table_fqn, to_column,
                 relationship_name, relationship_type, confidence, evidence_json, created_at,
                 inference_method, relationship_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["source_id"],        r["snapshot_id"],    r["from_schema"],
                r["from_table"],       r["from_table_fqn"], r["from_column"],
                r["to_schema"],        r["to_table"],       r["to_table_fqn"],
                r["to_column"],        r["relationship_name"], r["relationship_type"],
                r["confidence"],       r["evidence_json"],  r["created_at"],
                inference_method,      relationship_status,
            ),
        )

        incoming_rank = _evidence_rank(r["relationship_type"], inference_method, relationship_status)
        conn.execute(
            f"""
            UPDATE table_relationships
               SET relationship_name = ?,
                   relationship_type = ?,
                   confidence        = ?,
                   evidence_json     = ?,
                   created_at        = ?,
                   inference_method  = ?,
                   relationship_status = ?
             WHERE snapshot_id = ? AND from_table_fqn = ? AND from_column = ?
               AND to_table_fqn = ? AND to_column = ?
               AND ? > ({_EXISTING_RANK_SQL})
            """,
            (
                r["relationship_name"], r["relationship_type"], r["confidence"],
                r["evidence_json"],     r["created_at"],         inference_method,
                relationship_status,
                r["snapshot_id"], r["from_table_fqn"], r["from_column"],
                r["to_table_fqn"], r["to_column"],
                incoming_rank,
            ),
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

    Executive Report / Data Analyst Agent integration milestone — each row
    now also carries relationship_status ('AUTO'/'PENDING'/'APPROVED'/
    'REJECTED') and relationship_confidence, the SAME governance fields
    data.query_planning_service._plan_joins already trusts to decide which
    edges are safe to join across (see that module's own "trusted
    (AUTO/APPROVED)" convention). Additive columns only — every existing
    caller that ignores the new keys is unaffected.
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
            "relationship_name, relationship_type, confidence, created_at, "
            "relationship_status, relationship_confidence, cardinality"
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
    "name_match":       "INFERRED_NAME_MATCH",
    "value_overlap":    "INFERRED_VALUE_OVERLAP",
    "business_key":     "INFERRED_BUSINESS_KEY",
    "semantic_key_role": "INFERRED_SEMANTIC_KEY_ROLE",
}

_NAME_MATCH_POINTS    = 25
_DATATYPE_POINTS      = 10
_TARGET_KEY_POINTS    = 20
_DICTIONARY_POINTS    = 15
_DOMAIN_POINTS        = 10
_ENTITY_POINTS        = 10
_VALUE_OVERLAP_POINTS = 20

# Weighted higher than _VALUE_OVERLAP_POINTS: that signal is an approximate
# containment check over sampled values (profiling_value_samples, often
# empty in practice — see _value_overlap_score's own docstring); this one is
# an exact, live, single-JOIN measurement over the real column pair (see
# _measure_referential_coverage / Part 3), so full coverage is stronger
# evidence than a full sample match.
_REFERENTIAL_COVERAGE_POINTS = 30

# How high live-measured referential coverage must be before a differently-
# named key pair stops recording "names don't match" as a weakness (see
# _score_candidate's name_matched=False branch). Deliberately a high bar —
# this is what allows a Part 3 candidate to clear
# get_auto_trusted_pending_relationship_ids' "no recorded weaknesses" gate.
_STRONG_COVERAGE_THRESHOLD = 0.8

# Minimum distinct source values a referential-coverage measurement must
# span before it counts as strong evidence. Found live against a real production database:
# EDU_Student_Documents.CategoryID (only 4 distinct values) showed 100%
# coverage against EDU_Category_QueriesList.QueryID — an unrelated
# table — purely because a small value set (1-4) trivially falls inside
# almost any sequential integer PK's range. Below this bound, coverage is
# recorded as a weakness (see _score_candidate) even at 100%, and can never
# offset the name-mismatch weakness, regardless of the ratio.
_MIN_DISTINCT_FOR_STRONG_COVERAGE = 20


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


def _snake_case(name: str) -> str:
    """'CustomerID' -> 'customer_id'; underscore-and-lowercase everything else."""
    snake = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)       # 'CustomerId' -> 'Customer_Id'
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)     # 'CustomerID' -> 'Customer_ID'
    return re.sub(r"[^a-zA-Z0-9]+", "_", snake).lower()


def _stripped_tokens(name: str) -> list[str]:
    """snake_case(name) split into tokens with trailing id/key/code/guid/...
    stopword tokens removed — e.g. 'CustomerID' -> ['customer'],
    'ID' -> [] (nothing left once the generic word itself is stripped)."""
    tokens = [t for t in _snake_case(name).split("_") if t]
    while tokens and tokens[-1] in _ID_STEM_STOPWORDS:
        tokens.pop()
    return tokens


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
    tokens = _stripped_tokens(name)
    if tokens:
        return "_".join(tokens)
    if table_fqn:
        _, table = _split_fqn(table_fqn)
        table_norm = re.sub(r"[^a-zA-Z0-9]+", "_", table).lower().strip("_")
        if table_norm:
            return _singularize(table_norm)
    return _snake_case(name)


def _semantic_key_stem(column_name: str) -> str | None:
    """Non-generic stem of an FK-shaped column name for Part 2 semantic
    key-role matching — e.g. 'SessionID' -> 'session'. Returns None for a
    bare generic id-word ('ID'/'Key'/...) since there is no real term to
    look up against other tables' names. Deliberately does NOT use
    _normalize_column_name's table-name fallback (that fallback exists to
    bucket a bare PK with ITS OWN table; here we need a term to search
    OTHER tables' names with)."""
    tokens = _stripped_tokens(column_name)
    return "_".join(tokens) if tokens else None


def _table_name_tokens(table_fqn: str) -> list[str]:
    """A target table's own name reduced to singularized tokens for the
    Part 2 lexical/key-role hint — e.g. 'dbo.EDU_Sessions' -> ['edu', 'session']."""
    _, table = _split_fqn(table_fqn)
    return [_singularize(t) for t in _snake_case(table).split("_") if t]


# A token shared by more than this many tables' names is too common to be a
# meaningful lexical hint and is dropped from the index entirely — found
# live against a real production database: a shared schema-wide prefix ('EDU_' -> token
# 'edu') matched dozens of tables, and generic dimension-style words ('type',
# 'category', 'user', 'setting') each matched 10-20 more, together
# dominating the bounded candidate budget with low-quality pairs before
# genuinely specific hints (e.g. 'session') were ever reached. Same
# "too generic to trust on its own" reasoning as _is_bare_generic_pk_name,
# applied to the table-name side instead of the column-name side.
_MAX_TABLES_PER_TOKEN = 8


def _build_table_stem_index(table_fqns) -> dict[str, set[str]]:
    """token -> set(table_fqn) reverse index used to BOUND Part 2 candidate
    generation: a source column's stem is looked up here directly (O(1))
    rather than compared against every table in the schema. Indexes both
    each individual table-name token and (for multi-word table names) the
    full joined stem, so a column stem like 'class_session' can match a
    table whose full normalized name is 'class_session...' as well as one
    named just 'session'. Tokens shared by more than _MAX_TABLES_PER_TOKEN
    tables are dropped (see that constant) — too generic to be a meaningful
    hint, and would otherwise dominate the bounded candidate budget."""
    index: dict[str, set[str]] = {}
    for table_fqn in table_fqns:
        tokens = _table_name_tokens(table_fqn)
        keys = set(tokens)
        if len(tokens) > 1:
            keys.add("_".join(tokens))
        for key in keys:
            if key:
                index.setdefault(key, set()).add(table_fqn)
    return {k: v for k, v in index.items() if len(v) <= _MAX_TABLES_PER_TOKEN}


def _is_key_like_target(col: dict) -> bool:
    """Same 'looks like the parent/target side of a join' bar
    _score_candidate's target_points tiers use, reused here as the
    SELECTION gate for Part 2 candidate generation. Selection is only the
    first filter — the live referential-coverage check in Part 3 is what
    actually validates the candidate; this just decides whether a column is
    structurally plausible enough to even measure."""
    return (
        bool(col.get("is_primary_key"))
        or bool(col.get("is_identity"))
        or (col.get("uniqueness_score") or 0.0) >= 0.95
        or (col.get("guid_match_rate") or 0.0) >= 0.8
    )


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
    referential_coverage: dict | None = None,
) -> dict:
    """
    Weighted 0-100 confidence score from independent evidence signals.
    Caller guarantees from_col/to_col already passed the datatype-compatible
    hard gate before this is called.

    referential_coverage, when provided (Part 3 — live, single-JOIN
    measurement over the real column pair; see _measure_referential_coverage),
    takes priority over the approximate sample-based value_overlap for the
    "do these columns' values actually line up" evidence signal — it is
    exact, not sampled, so it is weighted higher and its own weakness message
    supersedes the generic "could not be verified" one.
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
    elif (
        referential_coverage is not None
        and referential_coverage["coverage"] >= _STRONG_COVERAGE_THRESHOLD
        and referential_coverage["source_distinct_count"] >= _MIN_DISTINCT_FOR_STRONG_COVERAGE
    ):
        # A name mismatch is ordinarily recorded as a weakness (and
        # get_auto_trusted_pending_relationship_ids refuses to auto-trust
        # ANY candidate with a recorded weakness — see that function's
        # docstring). That is the right default when the only evidence for
        # "these columns are related" IS the name. It stops being true once
        # we have live, measured proof the columns' actual values line up —
        # exactly the case discover_semantic_key_candidates exists for
        # (e.g. SessionID -> ID). Without this, no differently-named-key
        # relationship could ever clear auto-trust regardless of evidence
        # strength, defeating the point of measuring it. Only fires when
        # coverage was actually measured live (Part 3), never for the
        # sample-based value_overlap signal, which stays weaker evidence.
        evidence.append({
            "signal": "name_mismatch_offset_by_coverage",
            "detail": (
                f"Column names differ ('{from_col['column_name']}' vs "
                f"'{to_col['column_name']}'), but live referential coverage of "
                f"{referential_coverage['coverage']:.0%} confirms the relationship "
                "independently of naming."
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
    coverage_points = 0
    coverage = referential_coverage["coverage"] if referential_coverage else None
    if referential_coverage is not None:
        coverage_points = round(_REFERENTIAL_COVERAGE_POINTS * coverage)
        raw += coverage_points
        evidence.append({
            "signal": "referential_coverage", "points": coverage_points,
            "detail": (
                f"{coverage:.0%} of {referential_coverage['source_distinct_count']} distinct "
                f"'{from_col['column_name']}' values were found in "
                f"'{to_col['table_fqn']}.{to_col['column_name']}' — a live, read-only, "
                f"single-JOIN measurement over {referential_coverage['source_nonnull_count']} "
                f"non-null source rows (not a sample)."
            ),
        })
        if coverage < 0.5:
            weaknesses.append(
                f"Low referential coverage ({coverage:.0%}) — most source values have no "
                "matching target key. Likely false positive or a partially-populated table."
            )
        elif referential_coverage["source_distinct_count"] < _MIN_DISTINCT_FOR_STRONG_COVERAGE:
            # Real-the reference database false positive this guard was added for: a 4-distinct-
            # value lookup column ('CategoryID') showed 100% coverage against
            # an unrelated table's sequential integer PK purely because a
            # small, low-cardinality value set (1-4) trivially falls inside
            # almost any auto-increment ID range. High coverage over few
            # distinct values is not strong evidence — it's a coincidence
            # risk — so it stays a recorded weakness (blocking auto-trust)
            # regardless of how complete the coverage looks.
            weaknesses.append(
                f"Only {referential_coverage['source_distinct_count']} distinct source values "
                "were measured — high coverage over so few distinct values is weak statistical "
                "evidence and may be coincidental (e.g. a small lookup/category code matching an "
                "unrelated sequential ID range)."
            )
    elif value_overlap is None:
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
        if coverage is not None:
            inference_method = "semantic_key_role" if coverage >= 0.5 else "business_key"
        else:
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
        if profiling_snap_id:
            prof_snap_id = profiling_snap_id
        else:
            snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
            prof_snap_id = snapshot.id if snapshot else None

        result = {
            "schema_snapshot_id":                  snap_id,
            "profiling_snapshot_id":               prof_snap_id,
            "candidates_evaluated":                0,
            "candidates_persisted":                0,
            "candidates_discarded_low_confidence":  0,
            "candidates_skipped_existing":          0,
        }
        if snap_id is None or prof_snap_id is None:
            _audit_discovery(source_id, user_id, result)
            return result

        columns = get_key_candidate_columns(conn, source_id, prof_snap_id)
        if len(columns) < 2:
            _audit_discovery(source_id, user_id, result)
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
        _audit_discovery(source_id, user_id, result)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Structural PK-name-match relationship discovery
# (Enterprise Implementation — Structural Relationship Inference)
#
# discover_relationship_candidates() above depends on profiling statistics:
# get_key_candidate_columns() only reads profiling_column_profiles rows at
# ONE exact profiling_snapshot_id (the latest). A table profiled in an
# earlier snapshot version but never re-profiled in the latest one (e.g.
# EDU_Enrollment_Tracking in a real production database — profiled through an earlier snapshot, but
# the latest profiling snapshot is newer, which has zero rows for it) is
# silently invisible to that function regardless of how strong its column
# names are.
#
# This function proposes candidates directly from schema_snapshots —
# column names, normalized data types, and declared primary keys the schema
# connector already discovered (core.connectors.schema.TableInfo/ColumnInfo/
# PrimaryKeyInfo) — with zero dependency on profiling_column_profiles.
# Deliberately narrower than discover_relationship_candidates(): only an
# EXACT column-name match against a declared target primary key, never the
# normalized-stem/domain/entity/value-overlap signals that function uses.
# Always persisted PENDING, never AUTO/APPROVED, and MIN_SUGGEST_CONFIDENCE
# is never consulted or lowered — this is a separate, additive candidate
# source, not a relaxation of the existing one. analyze_join_quality/
# recommend_best_join_path/sql_planning_service/query_planning_service/
# semantic_retrieval_service already only ever traverse AUTO/APPROVED edges
# and are untouched by this function — a PENDING row it creates has no
# effect on any of them until a human reviewer approves it.
# ---------------------------------------------------------------------------

def _is_bare_generic_pk_name(column_name: str) -> bool:
    """
    True when column_name, lowercased, IS EXACTLY one of the generic
    id-stem stopwords (id/key/code/guid/uuid/fk/no/num/number/ref/
    reference) with no qualifying prefix — e.g. "ID", "Id", "Key". A
    prefixed name like "TrackID"/"ClassID"/"UserID" keeps a real (if
    sometimes still fairly generic, e.g. "UserID") word in front of the
    stem and is NOT bare, so it stays eligible. Reuses _ID_STEM_STOPWORDS
    (the same vocabulary _normalize_column_name already strips as suffix
    noise) rather than inventing a new list.

    Minimum-specificity gate added after the first real-the reference database run of
    discover_structural_pk_candidates() showed 89% of all persisted
    candidates (the large majority) were bare "ID"/"Id"/"id" collisions
    across the reference database's hundreds of tables — technically satisfying every one of the
    four creation criteria, but carrying essentially no semantic signal.
    """
    return (column_name or "").strip().lower() in _ID_STEM_STOPWORDS


STRUCTURAL_INFERENCE_METHOD = "STRUCTURAL_PK_NAME_MATCH"
_STRUCTURAL_RELATIONSHIP_TYPE = "INFERRED_STRUCTURAL_PK_MATCH"
# Reuses the existing name_match + datatype + target_primary_key point
# weights from _score_candidate's own scale (25 + 10 + 20 = 55) instead of
# inventing a new number — this candidate satisfies exactly those three
# signals, deterministically, every time (no partial credit, and no
# value-overlap/dictionary/domain/entity signals available to add or
# subtract without profiling data).
_STRUCTURAL_MATCH_CONFIDENCE = _NAME_MATCH_POINTS + _DATATYPE_POINTS + _TARGET_KEY_POINTS


def _parse_tables_from_snapshot_json(snapshot_json: str, snapshot_id: int) -> list[dict]:
    """
    Extract a flat list of {table_fqn, columns, primary_key_names} straight
    from the raw schema_snapshot JSON — the same structural metadata the
    connector already discovered, no profiling read at all. Returns [] on
    parse failure — never raises.
    """
    try:
        data = json.loads(snapshot_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "discover_structural_pk_candidates: failed to parse snapshot_json for snapshot_id=%s",
            snapshot_id,
        )
        return []

    tables: list[dict] = []
    for schema in data.get("schemas") or []:
        for table in schema.get("tables") or []:
            table_fqn = table.get("table_fqn") or ""
            if not table_fqn:
                continue
            pk_names = {
                pk.get("column_name") for pk in (table.get("primary_keys") or [])
                if pk.get("column_name")
            }
            tables.append({
                "table_fqn": table_fqn,
                "columns": table.get("columns") or [],
                "primary_key_names": pk_names,
            })
    return tables


def discover_structural_pk_candidates(
    source_id: int, user_id: str, schema_snap_id: int | None = None,
) -> dict | None:
    """
    Propose relationship candidates directly from schema discovery metadata
    (schema_snapshots — column names, normalized data types, declared
    primary keys), with NO dependency on profiling statistics. Complements
    (does not replace) discover_relationship_candidates().

    A candidate is created only when ALL of:
      - source column name EXACTLY matches a target table's declared PK
        column name (no stemming/normalization — deliberately stricter than
        discover_relationship_candidates()'s bucket matching);
      - normalized data types are compatible (core.connectors.schema.
        normalize_data_type on each column's raw_type);
      - source and target tables are different;
      - the target column is a confirmed primary key (present in that
        table's own schema-declared primary_keys list);
      - the target PK column name is not a BARE generic id-stem word with
        no qualifying prefix (see _is_bare_generic_pk_name) — e.g. "ID"/
        "Id"/"Key" alone are excluded, but "TrackID"/"ClassID"/"UserID" keep
        a real prefix and remain eligible. Added after a real-the reference database run
        showed bare "ID" collisions alone accounted for 89% of all
        candidates, with essentially no semantic signal.

    Always persisted with relationship_status='PENDING' — never AUTO or
    APPROVED. A pair already covered by ANY existing row (declared FK, a
    prior inferred candidate, or a prior structural candidate — checked both
    directions, and within this same run) is skipped; INSERT OR IGNORE
    against the existing unique index is the final backstop.

    Does not read or write profiling_column_profiles, and does not change
    sql_planning_service.py, query_planning_service.py,
    semantic_layer_service.py, or semantic_retrieval_service.py.

    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        snap_id = _resolve_snapshot_id(conn, source_id, schema_snap_id)
        result = {
            "schema_snapshot_id":              snap_id,
            "candidates_evaluated":            0,
            "candidates_persisted":            0,
            "candidates_rejected_type":        0,
            "candidates_rejected_generic_name": 0,
            "candidates_skipped_existing":     0,
        }
        if snap_id is None:
            _audit_discovery(source_id, user_id, result)
            return result

        snap_row = conn.execute(
            "SELECT snapshot_json FROM schema_snapshots WHERE id = ? AND source_id = ?",
            (snap_id, source_id),
        ).fetchone()
        if snap_row is None:
            _audit_discovery(source_id, user_id, result)
            return result

        tables = _parse_tables_from_snapshot_json(snap_row["snapshot_json"], snap_id)
        if len(tables) < 2:
            _audit_discovery(source_id, user_id, result)
            return result

        # target_index: column_name -> [(table_fqn, column dict), ...] for
        # every table's OWN declared primary key column only — this is the
        # "confirmed target primary key" gate, satisfied by construction
        # (a column only ever lands here because it's in that table's own
        # primary_key_names), not re-checked redundantly below.
        target_index: dict[str, list[tuple[str, dict]]] = {}
        for t in tables:
            for col in t["columns"]:
                col_name = col.get("column_name")
                if col_name not in t["primary_key_names"]:
                    continue
                if _is_bare_generic_pk_name(col_name):
                    result["candidates_rejected_generic_name"] += 1
                    continue
                target_index.setdefault(col_name, []).append((t["table_fqn"], col))

        existing_pairs = {
            (r["from_table_fqn"], r["from_column"], r["to_table_fqn"], r["to_column"])
            for r in conn.execute(
                "SELECT from_table_fqn, from_column, to_table_fqn, to_column "
                "FROM table_relationships WHERE source_id = ? AND snapshot_id = ?",
                (source_id, snap_id),
            ).fetchall()
        }

        now = _now()
        seen_this_run: set[tuple] = set()
        for t in tables:
            for col in t["columns"]:
                col_name = col.get("column_name")
                if not col_name or col_name not in target_index:
                    continue
                for target_table_fqn, target_col in target_index[col_name]:
                    if target_table_fqn == t["table_fqn"]:
                        continue  # source and target tables must differ

                    result["candidates_evaluated"] += 1

                    from_type = normalize_data_type(col.get("raw_type") or col.get("data_type") or "")
                    to_type = normalize_data_type(target_col.get("raw_type") or target_col.get("data_type") or "")
                    if from_type != to_type:
                        result["candidates_rejected_type"] += 1
                        continue

                    pair_key = (t["table_fqn"], col_name, target_table_fqn, col_name)
                    reverse_key = (target_table_fqn, col_name, t["table_fqn"], col_name)
                    if (
                        pair_key in existing_pairs or pair_key in seen_this_run
                        or reverse_key in existing_pairs or reverse_key in seen_this_run
                    ):
                        result["candidates_skipped_existing"] += 1
                        continue
                    seen_this_run.add(pair_key)

                    from_schema, from_table = _split_fqn(t["table_fqn"])
                    to_schema, to_table = _split_fqn(target_table_fqn)
                    # Cardinality: the target side is a confirmed PK (the
                    # "one" side). The source side is only known to be a PK
                    # too — and therefore also "one" — when it is its own
                    # table's declared primary key; there is no profiling
                    # uniqueness data available to say more than that.
                    cardinality = "ONE_TO_ONE" if col_name in t["primary_key_names"] else "MANY_TO_ONE"
                    evidence_json = json.dumps({
                        "inference_method": STRUCTURAL_INFERENCE_METHOD,
                        "evidence": [
                            {
                                "signal": "exact_name_match",
                                "detail": f"Column name '{col_name}' exactly matches target primary key column name.",
                            },
                            {
                                "signal": "datatype_compatible",
                                "detail": (
                                    f"'{col.get('raw_type') or col.get('data_type')}' normalizes to the same "
                                    f"type ('{from_type}') as target "
                                    f"'{target_col.get('raw_type') or target_col.get('data_type')}'."
                                ),
                            },
                            {
                                "signal": "target_primary_key",
                                "detail": (
                                    f"Target column '{target_table_fqn}.{col_name}' is a confirmed "
                                    "(schema-declared) primary key."
                                ),
                            },
                        ],
                        "weaknesses": [],
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
                            from_schema, from_table, t["table_fqn"], col_name,
                            to_schema, to_table, target_table_fqn, col_name,
                            f"structural_{STRUCTURAL_INFERENCE_METHOD.lower()}", _STRUCTURAL_RELATIONSHIP_TYPE,
                            round(_STRUCTURAL_MATCH_CONFIDENCE / 100.0, 3), evidence_json, now,
                            _STRUCTURAL_MATCH_CONFIDENCE, STRUCTURAL_INFERENCE_METHOD, "PENDING", cardinality,
                        ),
                    )
                    if conn.total_changes > before:
                        result["candidates_persisted"] += 1
                    else:
                        result["candidates_skipped_existing"] += 1

        conn.commit()
        _audit_discovery(source_id, user_id, result)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Semantic key-role relationship discovery (Enterprise Implementation —
# Automatic Relationship Discovery, Parts 2-3).
#
# The two discovery paths above both anchor candidate generation on some
# form of "the names already match": discover_relationship_candidates()
# only pairs columns whose normalized stems are IDENTICAL (Pass 1) or that
# share a pre-existing domain/entity assignment (Pass 2);
# discover_structural_pk_candidates() only pairs an EXACT column-name match
# against a declared primary key, and deliberately EXCLUDES bare generic PK
# names ("ID"/"Key"/...) as too noisy on their own (see
# _is_bare_generic_pk_name's docstring — 89% of that function's first
# real-the reference database run was bare-"ID" noise). Neither path can ever discover a real
# relationship like child.SessionID -> parent.ID when "parent" carries no
# domain/entity assignment linking it to the child table and its own PK is
# a bare generic word — exactly the documented real-the reference database gap
# (SessionID -> ID).
#
# This section closes that gap with a THIRD, additive discovery pass that
# replaces "the column names match" with two independent, bounded,
# deterministic signals instead:
#   (1) the SOURCE column's stem lexically matches the TARGET TABLE's own
#       name (Part 2) — bounding candidate generation to plausible tables
#       via a reverse token index, never a schema-wide cross product; and
#   (2) actual, live, read-only referential coverage between the two real
#       columns (Part 3) — what fraction of the source's distinct non-null
#       values are actually found in the target column, measured with one
#       bounded single-JOIN query per candidate (never a full cross-join,
#       never a per-row correlated subquery, never unbounded — see
#       _MAX_LIVE_OVERLAP_QUERIES/_MAX_LIVE_OVERLAP_ROW_BOUND below).
#
# A bare generic target PK name ("ID") is explicitly ALLOWED here (unlike
# discover_structural_pk_candidates), because the trust burden has moved
# from "the name is distinctive enough on its own" to "the table-name hint
# plus real measured evidence together justify it" — consistent with the
# task's explicit instruction that type/name coincidence alone must never
# be trusted, and that strong MULTI-SIGNAL evidence is required.
#
# Persists PENDING rows through the exact same table_relationships write
# path, unique-index dedup, and downstream trust machinery
# (get_auto_trusted_pending_relationship_ids) as the other two discovery
# functions — this function only proposes and scores; it never sets
# relationship_status to anything but 'PENDING'.
# ---------------------------------------------------------------------------

# Hard ceiling on how many lexically-bounded candidate pairs Part 2 will
# even score, independent of schema width (mirrors _MAX_CANDIDATE_PAIRS's
# role for discover_relationship_candidates).
_MAX_SEMANTIC_CANDIDATE_PAIRS = 300

# Hard ceiling on how many target tables a SINGLE source column's stem may
# pair with, applied on top of _MAX_TABLES_PER_TOKEN's schema-wide filter.
# Keeps one moderately-common token (e.g. matching 6-8 tables) from
# consuming a disproportionate share of _MAX_SEMANTIC_CANDIDATE_PAIRS
# before other, more specific columns are ever reached.
_MAX_TARGET_TABLES_PER_SOURCE_COLUMN = 5

# Hard ceiling on how many of those candidates get a live Part 3 overlap
# query in one discovery run — the expensive step, so bounded independently
# and more tightly than candidate generation itself.
_MAX_LIVE_OVERLAP_QUERIES = 40

# Skip the live overlap query (falls back to 'unverified', same handling as
# today's sample-based value_overlap when no samples exist) for a source
# table this large, rather than risk a slow scan on a column that turns out
# to carry no useful index. Table row counts come from already-persisted
# profiling stats — no new query is issued just to check this bound.
_MAX_LIVE_OVERLAP_ROW_BOUND = 50_000_000

# Per-query wall-clock bound, enforced by the same thread-based mechanism
# data.query_execution_service already uses for governed live queries
# (_execute_with_timeout) — reused here rather than inventing a second
# timeout mechanism. On timeout the connection is closed by that helper, so
# the discovery loop below stops issuing further live queries for the rest
# of the run once that happens (remaining candidates fall back to
# 'unverified', not blocked).
_LIVE_OVERLAP_TIMEOUT_S = 20

# Same naming-convention suffixes get_key_candidate_columns's own SQL
# checks (id/_id/key/_key/code/_code/guid/_guid/uuid/_uuid/fk/_fk) —
# reused here so a schema-only column (no profiling row to consult) is
# included on the same "looks FK-shaped by name" basis, not a new heuristic.
_SCHEMA_KEY_NAME_SUFFIXES = ("id", "key", "code", "guid", "uuid", "fk")


def _looks_key_shaped_by_name(column_name: str) -> bool:
    name = (column_name or "").lower()
    return any(name.endswith(suf) for suf in _SCHEMA_KEY_NAME_SUFFIXES)


def _schema_based_key_candidate_columns(
    conn, source_id: int, schema_snapshot_id: int, exclude_tables: set[str],
) -> list[dict]:
    """Key-like columns sourced directly from schema_snapshots — no
    profiling dependency — for tables ABSENT from the latest profiling
    snapshot (exclude_tables). Closes the same profiling-staleness gap
    discover_structural_pk_candidates was built for (see its module
    comment): a table profiled in an earlier snapshot version, or never
    profiled at all, is invisible to get_key_candidate_columns regardless
    of how strong its column names are.

    Reuses _parse_tables_from_snapshot_json (already used by
    discover_structural_pk_candidates) rather than a second schema parser.
    Shaped identically to get_key_candidate_columns's output so callers can
    treat both lists uniformly. Numeric profiling stats only a real
    profiling run can produce (uniqueness_score, distinct_count,
    guid_match_rate) are left None/0 rather than invented —
    _is_key_like_target and _score_candidate already treat a declared
    is_primary_key/is_identity flag as sufficient on its own. id=None (no
    profiling_column_profiles row exists for these); _value_overlap_score's
    'WHERE profiling_column_profile_id = ?' with a None parameter matches no
    rows in SQL, so it safely returns None ('unverified'), never a wrong
    answer.
    """
    snap_row = conn.execute(
        "SELECT snapshot_json FROM schema_snapshots WHERE id = ? AND source_id = ?",
        (schema_snapshot_id, source_id),
    ).fetchone()
    if snap_row is None:
        return []

    tables = _parse_tables_from_snapshot_json(snap_row["snapshot_json"], schema_snapshot_id)
    columns: list[dict] = []
    for t in tables:
        if t["table_fqn"] in exclude_tables:
            continue
        for col in t["columns"]:
            col_name = col.get("column_name")
            if not col_name:
                continue
            is_pk = col_name in t["primary_key_names"]
            is_identity = bool(col.get("is_identity"))
            if not (is_pk or is_identity or _looks_key_shaped_by_name(col_name)):
                continue
            columns.append({
                "id": None,
                "table_fqn": t["table_fqn"],
                "column_name": col_name,
                "data_type": col.get("data_type") or col.get("raw_type") or "",
                "distinct_count": None,
                "uniqueness_score": None,
                "cardinality_tier": None,
                "is_primary_key": is_pk,
                "is_identity": is_identity,
                "guid_match_rate": None,
                "semantic_type": None,
                "semantic_confidence": None,
            })
    return columns


def _get_table_row_count(
    conn, source_id: int, profiling_snapshot_id: int, table_fqn: str,
) -> int | None:
    """Best-known row count for table_fqn from already-persisted profiling
    stats (exact_row_count, falling back to estimated_row_count) — never
    issues a new query. Returns None when the table was never profiled."""
    row = conn.execute(
        "SELECT exact_row_count, estimated_row_count FROM profiling_table_profiles "
        "WHERE source_id = ? AND profiling_snapshot_id = ? AND table_fqn = ?",
        (source_id, profiling_snapshot_id, table_fqn),
    ).fetchone()
    if row is None:
        return None
    return row["exact_row_count"] if row["exact_row_count"] is not None else row["estimated_row_count"]


# Autonomous Live-SQL Foundation milestone, Part 6 — this connection is
# opened only for optional overlap-validation evidence (never Q&A itself),
# so it must never stall on a slow/unreachable source the way the default
# 30s connect timeout did (measured ~31s of a ~33s connect-time job against
# an unreachable fixture host). Bounded well below that; a source that can't
# be reached this quickly just proceeds without live validation.
_LIVE_VALIDATION_CONNECT_TIMEOUT_S = 5


def _open_live_connection(source_id: int, user_id: str):
    """Best-effort live connection + query builder for Part 3 overlap
    validation. Returns (conn, builder) or (None, None) on ANY failure
    (missing credentials, unsupported connector, connection error, or a
    connect attempt that exceeds _LIVE_VALIDATION_CONNECT_TIMEOUT_S) — a live
    connection is a nice-to-have for stronger evidence, never a hard
    requirement for candidate discovery to proceed. A timeout here never
    trusts the candidate; it just leaves it unvalidated, exactly like any
    other connection failure — callers already treat None as "unverified,"
    never as "verified with zero overlap." Reuses the exact same
    decrypt-and-connect path data.query_execution_service already uses for
    governed live queries, rather than a second implementation of it."""
    try:
        from data.query_execution_service import _load_source_connection, _registry
        db_conn, source_type = _load_source_connection(
            source_id, user_id, timeout_s=_LIVE_VALIDATION_CONNECT_TIMEOUT_S,
        )
    except Exception:
        logger.info(
            "discover_semantic_key_candidates: no live connection available for "
            "source_id=%s; proceeding without Part 3 overlap validation.", source_id,
        )
        return None, None

    connector_cls = _registry.get(source_type)
    if connector_cls is None:
        try:
            db_conn.close()
        except Exception:
            pass
        return None, None

    try:
        builder = connector_cls().get_query_builder()
    except Exception:
        try:
            db_conn.close()
        except Exception:
            pass
        return None, None

    return db_conn, builder


def _measure_referential_coverage(live_conn, builder, from_col: dict, to_col: dict) -> dict | None:
    """Run ONE bounded, read-only, single-JOIN overlap query for a single
    candidate pair. Returns {'source_nonnull_count', 'source_distinct_count',
    'matched_distinct_count', 'coverage'} or None on any failure, timeout, or
    empty source — callers must treat None as 'unverified', never as zero
    overlap.

    Returns (result, connection_still_alive) so the caller can stop issuing
    further live queries once _execute_with_timeout has closed live_conn on
    a timeout, instead of erroring on every subsequent candidate.
    """
    from data.query_execution_service import _execute_with_timeout

    try:
        sql = builder.build_referential_overlap_query(
            from_col["table_fqn"], from_col["column_name"],
            to_col["table_fqn"], to_col["column_name"],
        )
    except NotImplementedError:
        return None, True
    except Exception:
        logger.warning(
            "discover_semantic_key_candidates: failed to build overlap query for "
            "%s.%s -> %s.%s", from_col["table_fqn"], from_col["column_name"],
            to_col["table_fqn"], to_col["column_name"], exc_info=True,
        )
        return None, True

    try:
        description, rows, error, timed_out = _execute_with_timeout(
            live_conn, sql, [], 1, _LIVE_OVERLAP_TIMEOUT_S,
        )
    except Exception:
        logger.warning(
            "discover_semantic_key_candidates: overlap query failed for %s.%s -> %s.%s",
            from_col["table_fqn"], from_col["column_name"],
            to_col["table_fqn"], to_col["column_name"], exc_info=True,
        )
        return None, True

    if timed_out:
        return None, False
    if error or not rows:
        return None, True

    keys = [d[0].lower() for d in description]
    d = dict(zip(keys, rows[0]))
    source_nonnull = int(d.get("source_nonnull_count") or 0)
    source_distinct = int(d.get("source_distinct_count") or 0)
    matched_distinct = int(d.get("matched_distinct_count") or 0)
    if not source_distinct:
        return None, True

    return {
        "source_nonnull_count":  source_nonnull,
        "source_distinct_count": source_distinct,
        "matched_distinct_count": matched_distinct,
        "coverage": round(matched_distinct / source_distinct, 3),
    }, True


def discover_semantic_key_candidates(
    source_id: int,
    user_id: str,
    schema_snap_id: int | None = None,
    profiling_snap_id: int | None = None,
) -> dict | None:
    """
    Discover candidate relationships between differently-named key columns
    (e.g. child.SessionID -> parent.ID) that discover_relationship_candidates()
    and discover_structural_pk_candidates() structurally cannot reach — see
    this section's module-level comment for why.

    Part 2 (bounded candidate generation): a source column qualifies when
    its stem (_semantic_key_stem — the same id-suffix-stripping
    _normalize_column_name already uses) matches one of a candidate target
    table's own name tokens (_build_table_stem_index — an O(1) reverse
    lookup, not a schema-wide comparison) AND that table has a column that
    already looks key-like (_is_key_like_target: declared PK, identity, or
    near-unique/GUID-shaped) — bare generic target names ARE eligible here,
    unlike discover_structural_pk_candidates.

    Part 3 (live value-level validation): for up to _MAX_LIVE_OVERLAP_QUERIES
    of the bounded candidates, runs one single-JOIN, read-only query
    (_measure_referential_coverage) measuring the real fraction of distinct
    source values found in the target column. This is fed into
    _score_candidate as referential_coverage, which weighs it above the
    approximate sample-based value_overlap signal used elsewhere. Candidates
    beyond the query budget, or when no live connection is available, are
    scored exactly as before with referential_coverage=None — 'unverified',
    not rejected outright; they can still surface as a low-confidence
    PENDING candidate on other evidence (dictionary/domain/entity), but
    cannot reach the auto-trust confidence floor without it, since
    name_matched is always False on this path.

    Part 4 (trust): rows are persisted with relationship_status='PENDING'
    through the identical write path as the other two discovery functions —
    get_auto_trusted_pending_relationship_ids evaluates them with the exact
    same confidence floor, cardinality gate, fan-out guard, and contested-
    group tie-break as any other candidate. This function never sets
    relationship_status to AUTO/APPROVED and never bypasses that gate.

    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source_ownership(conn, source_id, user_id):
            return None

        snap_id = _resolve_snapshot_id(conn, source_id, schema_snap_id)
        if profiling_snap_id:
            prof_snap_id = profiling_snap_id
        else:
            snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
            prof_snap_id = snapshot.id if snapshot else None

        result = {
            "schema_snapshot_id":                 snap_id,
            "profiling_snapshot_id":              prof_snap_id,
            "candidates_evaluated":               0,
            "candidates_persisted":               0,
            "candidates_discarded_low_confidence": 0,
            "candidates_skipped_existing":         0,
            "candidates_live_validated":           0,
            "candidates_unverified":               0,
            "live_connection_available":           False,
        }
        if snap_id is None or prof_snap_id is None:
            _audit_discovery(source_id, user_id, result)
            return result

        columns = get_key_candidate_columns(conn, source_id, prof_snap_id)

        cols_by_table: dict[str, list[dict]] = {}
        for col in columns:
            cols_by_table.setdefault(col["table_fqn"], []).append(col)

        # Same profiling-staleness gap discover_structural_pk_candidates was
        # built to work around (see that function's module comment): a table
        # profiled in an earlier snapshot version, or never profiled at all,
        # is invisible to get_key_candidate_columns regardless of how strong
        # its column names are. Without this, this function would inherit
        # that exact blind spot — live-verified against a real production database:
        # dbo.LMS_Attendance.SessionID and its target dbo.LMS_CLASS_SESSIONS
        # (bare 'ID' PK) are BOTH absent from the latest profiling snapshot,
        # so the real SessionID -> ID gap this function exists to close
        # would otherwise never even reach candidate generation.
        schema_only_columns = _schema_based_key_candidate_columns(
            conn, source_id, snap_id, exclude_tables=set(cols_by_table.keys()),
        )
        for col in schema_only_columns:
            cols_by_table.setdefault(col["table_fqn"], []).append(col)
        columns = columns + schema_only_columns

        if len(columns) < 2:
            _audit_discovery(source_id, user_id, result)
            return result

        stem_index = _build_table_stem_index(cols_by_table.keys())

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

        # ---- Part 2: bounded candidate generation --------------------------
        seen_pairs: set[tuple] = set()
        pairs_to_score: list[tuple[dict, dict]] = []
        for from_col in columns:
            stem = _semantic_key_stem(from_col["column_name"])
            if stem is None:
                continue
            target_tables = stem_index.get(stem)
            if not target_tables:
                continue
            # Deterministic, bounded fan-out per source column — even a
            # token that survives the per-token _MAX_TABLES_PER_TOKEN cutoff
            # can still map to a handful of tables; capping here stops one
            # generic column from consuming a disproportionate share of the
            # run's bounded candidate budget before other columns are ever
            # considered.
            for target_table in sorted(target_tables)[:_MAX_TARGET_TABLES_PER_SOURCE_COLUMN]:
                if target_table == from_col["table_fqn"]:
                    continue
                to_candidates = [c for c in cols_by_table[target_table] if _is_key_like_target(c)]
                if not to_candidates:
                    continue
                to_col = max(to_candidates, key=_key_strength)

                if not _types_compatible(from_col.get("data_type"), to_col.get("data_type")):
                    continue

                pair = (from_col["table_fqn"], from_col["column_name"],
                        to_col["table_fqn"], to_col["column_name"])
                reverse = (to_col["table_fqn"], to_col["column_name"],
                           from_col["table_fqn"], from_col["column_name"])
                if pair in existing_pairs or reverse in existing_pairs:
                    result["candidates_skipped_existing"] += 1
                    continue
                if pair in seen_pairs or reverse in seen_pairs:
                    continue
                seen_pairs.add(pair)
                pairs_to_score.append((from_col, to_col))

                if len(pairs_to_score) >= _MAX_SEMANTIC_CANDIDATE_PAIRS:
                    break
            if len(pairs_to_score) >= _MAX_SEMANTIC_CANDIDATE_PAIRS:
                logger.warning(
                    "discover_semantic_key_candidates: truncating candidate pairs to "
                    "%d for source_id=%s", _MAX_SEMANTIC_CANDIDATE_PAIRS, source_id,
                )
                break

        if not pairs_to_score:
            _audit_discovery(source_id, user_id, result)
            return result

        # ---- Part 3: bounded live value-level validation --------------------
        live_conn, builder = _open_live_connection(source_id, user_id)
        result["live_connection_available"] = live_conn is not None
        live_queries_run = 0
        live_conn_alive = live_conn is not None

        now = _now()
        for from_col, to_col in pairs_to_score:
            result["candidates_evaluated"] += 1

            coverage_result = None
            if (
                live_conn_alive
                and live_queries_run < _MAX_LIVE_OVERLAP_QUERIES
            ):
                row_count = _get_table_row_count(conn, source_id, prof_snap_id, from_col["table_fqn"])
                if row_count is None or row_count <= _MAX_LIVE_OVERLAP_ROW_BOUND:
                    coverage_result, live_conn_alive = _measure_referential_coverage(
                        live_conn, builder, from_col, to_col,
                    )
                    live_queries_run += 1

            if coverage_result is not None:
                result["candidates_live_validated"] += 1
            else:
                result["candidates_unverified"] += 1

            value_overlap = _value_overlap_score(conn, from_col["id"], to_col["id"])
            scored = _score_candidate(
                from_col, to_col,
                name_matched=False,
                from_dict=dict_rows.get((from_col["table_fqn"], from_col["column_name"])),
                to_dict=dict_rows.get((to_col["table_fqn"], to_col["column_name"])),
                from_domain=domain_rows.get(from_col["table_fqn"]),
                to_domain=domain_rows.get(to_col["table_fqn"]),
                from_entity=entity_rows.get(from_col["table_fqn"]),
                to_entity=entity_rows.get(to_col["table_fqn"]),
                value_overlap=value_overlap,
                referential_coverage=coverage_result,
            )

            if scored["score"] < MIN_SUGGEST_CONFIDENCE:
                result["candidates_discarded_low_confidence"] += 1
                continue

            rel_type = _INFERENCE_METHOD_TO_TYPE[scored["inference_method"]]
            cardinality = _infer_cardinality(from_col, to_col)
            from_schema, from_table = _split_fqn(from_col["table_fqn"])
            to_schema, to_table = _split_fqn(to_col["table_fqn"])
            evidence_json = json.dumps({
                "evidence":   scored["evidence"],
                "weaknesses": scored["weaknesses"],
                "table_name_hint": _semantic_key_stem(from_col["column_name"]),
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
                    f"semantic_key_role_{scored['inference_method']}", rel_type,
                    round(scored["score"] / 100.0, 3), evidence_json, now,
                    scored["score"], scored["inference_method"], "PENDING", cardinality,
                ),
            )
            if conn.total_changes > before:
                result["candidates_persisted"] += 1
            else:
                result["candidates_skipped_existing"] += 1

        if live_conn is not None and live_conn_alive:
            try:
                live_conn.close()
            except Exception:
                pass

        conn.commit()
        _audit_discovery(source_id, user_id, result)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Automatic relationship trust (Part 4) — read-time evidence evaluation only.
#
# discover_relationship_candidates()/discover_structural_pk_candidates()
# above already compute rich, independent evidence for every inferred
# relationship (name match, datatype compatibility, target-key strength,
# dictionary/domain/entity corroboration, sampled value overlap, cardinality)
# and persist it as relationship_confidence/cardinality/evidence_json on the
# PENDING row. The only thing that has ever decided whether a relationship
# is USABLE for real join planning is relationship_status — every PENDING
# row, however strong its evidence, requires a human approve_relationship()
# call. This section adds an automatic trust decision that reuses that
# already-persisted evidence: it NEVER mutates relationship_status (no
# governance write, no dry-run concern, fully reversible — see this
# project's own standing caution about live writes against the a real production database
# connection), and NEVER lowers MIN_SUGGEST_CONFIDENCE or any existing
# threshold. It only widens which relationship_status values a READER
# (data.semantic_layer_service._load_edges) is willing to treat as trusted,
# for one request at a time.
# ---------------------------------------------------------------------------

# The maximum score achievable from CONFIRMED structural facts alone (exact/
# stemmed name match + compatible datatype + a DECLARED primary-key target —
# not merely near-unique/identity/guid-shaped, which score lower in
# _score_candidate) — i.e. _STRUCTURAL_MATCH_CONFIDENCE itself, reused
# verbatim as the auto-trust floor rather than a new invented number. A
# relationship using a weaker uniqueness proxy without corroborating
# value-overlap/dictionary/domain/entity evidence scores below this and does
# not qualify; one WITH such corroboration can still clear it on its own
# strength via a different evidence combination.
_AUTO_TRUST_MIN_CONFIDENCE = _STRUCTURAL_MATCH_CONFIDENCE

# Cardinality shapes that are never auto-trusted regardless of score — a
# many-to-many or unresolved cardinality is a structurally riskier join
# (unbounded fan-out potential) than the confirmed-PK-target case the score
# floor above already requires; matches the same MANY_TO_MANY-is-weaker
# reasoning data.semantic_layer_service._IDENTITY_CARDINALITIES already
# encodes for a different purpose.
_AUTO_TRUST_BLOCKED_CARDINALITIES = frozenset({"MANY_TO_MANY", "UNKNOWN", None})

# A table whose PENDING structural candidates span more than this many
# DISTINCT OUTBOUND target tables is excluded from auto-trust entirely.
# Deliberately OUTBOUND-only (from_table_fqn's own distinct to_table_fqn
# set) — NOT total degree in either direction. A popular TARGET referenced
# by many different source tables (e.g. a real production database: dbo.EDU_Instructor is
# pointed to by several different tables' InstructorID columns) is a completely
# normal, even positive, hub/dimension-table signal and must not be
# penalized; live-confirmed first draft of this guard counted both
# directions and wrongly disqualified the real Instructor relationship
# alongside the actual bad case. The actual failure signature is one SOURCE
# table's OWN columns structurally matching declared primary keys across
# many BUSINESS-UNRELATED tables at once: dbo.EDU_ClassActive's bare
# ID-shaped columns (ClassID/StudentID/TrackID/CourseID/EventID/DocumentID/
# RoleID/...) match many distinct, unrelated tables outbound — the signature
# of a wide staging/denormalized table, not a genuine relationship source.
# Promoting two of its edges (StudentID->EDU_Student, TrackID->EDU_Track,
# each individually unopposed, so the contested-group check above never
# fired) introduced a spurious 1-hop shortcut the join planner then
# PREFERRED over the correct, already-working 3-hop dbo.EDU_Student ->
# dbo.EDU_ClassSignups -> dbo.EDU_Class -> dbo.EDU_Track chain, regressing
# "How many students are in each program?" from passing to failing. The
# real Instructor bridge (dbo.LMS_CLASS_SESSION_SECTIONS) has outbound
# fan-out 4. Automatic trust requires a FOCUSED source table; a diffuse one
# is exactly the "multiple relationship paths, cannot choose safely" case
# that must stay blocked for human review regardless of any single edge's
# own score.
_AUTO_TRUST_MAX_CANDIDATE_FANOUT = 5


def _declared_relationship_degree(conn, source_id: int, snapshot_id: int) -> dict[str, int]:
    """table_fqn -> count of DECLARED (AUTO, i.e. real schema-declared FK)
    relationship rows touching it, either direction, for this snapshot.

    Graph-consistency evidence (Part 4): a table that itself participates in
    other real, declared foreign keys is far more likely to be the canonical
    entity table than an isolated, narrowly-named derived/filtered variant
    that happens to share a column name — e.g. a real production database data:
    dbo.EDU_Instructor has 3 declared FK edges; dbo.EDU_Mentoring_
    ActiveInstructors and dbo.EDU_Mentoring_Active_Tier2_Instructors (which
    also expose an InstructorID primary key and therefore tie on every other
    signal) have 0. Reads only table_relationships, already this module's
    own table — no new evidence source, no cross-module dependency.
    """
    rows = conn.execute(
        """SELECT from_table_fqn, to_table_fqn FROM table_relationships
           WHERE source_id = ? AND snapshot_id = ? AND relationship_status = 'AUTO'""",
        (source_id, snapshot_id),
    ).fetchall()
    degree: dict[str, int] = {}
    for r in rows:
        degree[r["from_table_fqn"]] = degree.get(r["from_table_fqn"], 0) + 1
        degree[r["to_table_fqn"]] = degree.get(r["to_table_fqn"], 0) + 1
    return degree


def get_auto_trusted_pending_relationship_ids(
    source_id: int, snapshot_id: int, *, conn=None,
) -> set[int]:
    """Automatic evidence-based trust decision for PENDING relationships —
    never a status mutation, never requires human approval, never a blanket
    "trust every PENDING row."

    A PENDING row qualifies only when ALL of:
      - relationship_confidence >= _AUTO_TRUST_MIN_CONFIDENCE (the ceiling
        for confirmed structural facts alone);
      - cardinality is a normal FK shape (not MANY_TO_MANY/UNKNOWN);
      - its own evidence_json records no "weaknesses" (the same field
        _score_candidate/explain_relationship already populate for low
        value-overlap or an unclear target — untouched, just consulted);
      - the SOURCE table is not diffuse/promiscuous: from_table_fqn must
        have at most _AUTO_TRUST_MAX_CANDIDATE_FANOUT distinct OUTBOUND
        target tables among its OTHERWISE-ELIGIBLE PENDING candidates only
        (confidence/cardinality/no-weaknesses already satisfied — computed
        AFTER those checks, deliberately not over the raw PENDING pool).
        Outbound-only — a popular TARGET referenced by many different
        sources is a normal hub/dimension signal, not penalized. Restricting
        the count to otherwise-eligible rows matters once more than one
        discovery function proposes candidates for the same table: a table
        with 2 genuine, strong structural matches must not lose auto-trust
        on those 2 just because a DIFFERENT, weaker discovery pass (e.g.
        discover_semantic_key_candidates's lexical table-name hints) also
        proposed several low-confidence/weakness-flagged candidates for it —
        live-confirmed regression against a real production database: dbo.EDU_Homework had 2
        auto-trusting structural candidates (ClassSignupsID, SectionID);
        adding 4 unrelated weak 'SectionID matches some other table sharing
        the "section" token' candidates (all carrying a recorded weakness,
        never auto-trust-eligible themselves) pushed its fanout from 3 to 7
        and silently demoted the 2 genuine ones. A table whose bare
        ID-shaped columns structurally match primary keys on many unrelated
        tables at once — the ORIGINAL failure this guard targets — is still
        caught: those matches are themselves confidence>=floor with no
        weaknesses, so they still count;
      - it is not CONTESTED: when multiple PENDING candidates share the same
        (from_table_fqn, from_column) — i.e. they are competing hypotheses
        for what one source column joins to, not independent facts — only
        one may auto-trust, and only if it strictly wins the declared-
        relationship-degree tie-break (_declared_relationship_degree) over
        every other candidate in the group. A genuine tie on that signal too
        stays CONTESTED and none of the group is trusted — weak/conflicting
        evidence remains blocked, exactly as instructed.

    Never raises; returns an empty set on any failure (fails closed to
    today's AUTO/APPROVED-only behavior).
    """
    own_connection = conn is None
    c = get_connection() if own_connection else conn
    try:
        rows = c.execute(
            """SELECT id, from_table_fqn, from_column, to_table_fqn,
                      relationship_confidence, cardinality, evidence_json
               FROM table_relationships
               WHERE source_id = ? AND snapshot_id = ? AND relationship_status = 'PENDING'""",
            (source_id, snapshot_id),
        ).fetchall()
        if not rows:
            return set()

        # First pass: confidence/cardinality/weaknesses only — independent
        # of any other row. Fan-out is deliberately computed from THIS
        # already-filtered set (see the docstring above), not the raw
        # PENDING pool, so a different discovery pass's low-confidence or
        # weakness-flagged noise for the same source table can never crowd
        # out an otherwise-trustworthy candidate purely by volume.
        eligible: list[dict] = []
        for r in rows:
            d = dict(r)
            if (d["relationship_confidence"] or 0) < _AUTO_TRUST_MIN_CONFIDENCE:
                continue
            if d["cardinality"] in _AUTO_TRUST_BLOCKED_CARDINALITIES:
                continue
            if _parse_evidence_json(d["evidence_json"]).get("weaknesses"):
                continue
            eligible.append(d)
        if not eligible:
            return set()

        outbound_fanout: dict[str, set] = {}
        for d in eligible:
            outbound_fanout.setdefault(d["from_table_fqn"], set()).add(d["to_table_fqn"])

        candidates = [
            d for d in eligible
            if len(outbound_fanout.get(d["from_table_fqn"], ())) <= _AUTO_TRUST_MAX_CANDIDATE_FANOUT
        ]
        if not candidates:
            return set()

        by_source: dict[tuple, list[dict]] = {}
        for d in candidates:
            by_source.setdefault((d["from_table_fqn"], d["from_column"]), []).append(d)

        declared_degree = _declared_relationship_degree(c, source_id, snapshot_id)

        trusted_ids: set[int] = set()
        for group in by_source.values():
            if len(group) == 1:
                trusted_ids.add(group[0]["id"])
                continue
            ranked = sorted(
                group, key=lambda d: declared_degree.get(d["to_table_fqn"], 0), reverse=True,
            )
            best_degree = declared_degree.get(ranked[0]["to_table_fqn"], 0)
            runner_up_degree = declared_degree.get(ranked[1]["to_table_fqn"], 0)
            if best_degree > 0 and best_degree > runner_up_degree:
                trusted_ids.add(ranked[0]["id"])
            # else: still contested among the group — none added, all stay blocked.

        return trusted_ids
    except Exception:  # noqa: BLE001
        logger.warning(
            "get_auto_trusted_pending_relationship_ids: failed for source_id=%s snapshot_id=%s",
            source_id, snapshot_id, exc_info=True,
        )
        return set()
    finally:
        if own_connection:
            c.close()


# ---------------------------------------------------------------------------
# Relationship explanation (Program 3 Phase 1, Step 4)
# ---------------------------------------------------------------------------

_INFERENCE_METHOD_LABEL = {
    "name_match":        "matching column names",
    "value_overlap":     "overlapping sampled values",
    "business_key":      "shared business domain/entity context",
    "semantic_key_role": "a differently-named key matched to the target table by name and validated by live referential coverage",
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


# ---------------------------------------------------------------------------
# Join Intelligence support (Program 3 Phase 2)
#
# pk_quality_score generalizes the PK/identity/uniqueness/GUID tiering already
# used internally by _score_candidate's target-key scoring into a standalone,
# reusable 0-100 utility for semantic_layer_service's join-quality scoring.
# _score_candidate itself is untouched — this is a new parallel function, not
# a refactor of tested Phase 1 code.
# ---------------------------------------------------------------------------

def pk_quality_score(col_profile: dict | None) -> int:
    """
    Score how strongly a column looks like a valid primary/unique join key, 0-100.

    Mirrors the target-key tiering in _score_candidate:
      declared primary key   -> 100
      identity column        -> 90
      uniqueness_score>=0.95 -> 80
      GUID-shaped values     -> 60
      none of the above      -> 0

    Returns 0 when col_profile is None (column was never profiled) — never
    invents a score for missing data.
    """
    if col_profile is None:
        return 0
    if col_profile.get("is_primary_key"):
        return 100
    if col_profile.get("is_identity"):
        return 90
    if (col_profile.get("uniqueness_score") or 0.0) >= 0.95:
        return 80
    if (col_profile.get("guid_match_rate") or 0.0) >= 0.8:
        return 60
    return 0
