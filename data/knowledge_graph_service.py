"""
Knowledge Graph Reasoning Engine — Phase 4.

Reasons over existing enterprise metadata to produce graph-aware insights:
related table discovery, business asset search, table explanation,
join-path tracing, and graph-level coverage summaries.

NO new storage. NO graph database. NO AI. NO LLM.
Pure structural reasoning over the metadata already produced by
Schema Discovery, Profiling, Dictionary, Domain, Entity,
and Relationship services.

This layer is the future foundation for Join Intelligence, AI Workspace
SQL generation, and executive reporting.
"""
import logging
from collections import deque

from data.db import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _verify_source(conn, source_id: int, user_id: str):
    """Return source row if owned by user_id, else None."""
    return conn.execute(
        "SELECT id, display_name, source_type, source_category "
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


def _latest_profiling_snap_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM profiling_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


def _compute_importance_score(table_profile: dict | None, dict_row=None) -> float:
    """
    Derive business importance (0–1) from structural metadata signals.
    Higher score = more central to the business graph.
    No LLM required — score is entirely rule-based over stored metadata.
    """
    if table_profile is None:
        return 0.1

    score = 0.0

    # Tables that other tables point to are foundational (demand signal)
    ref_count = table_profile.get("referenced_by_count") or 0
    if ref_count > 0:
        score += min(0.30, 0.06 * ref_count)

    # Root table = primary entity with no incoming FKs
    if table_profile.get("is_root_table"):
        score += 0.20

    # Technical classification signal
    cls = table_profile.get("table_class") or ""
    if cls == "Master":
        score += 0.20
    elif cls == "Reference":
        score += 0.12
    elif cls == "Transactional":
        score += 0.08

    # PII presence = compliance-critical asset
    if (table_profile.get("pii_column_count") or 0) > 0:
        score += 0.10

    # Human dictionary approval = trusted, governed asset
    if dict_row and dict_row.get("is_approved"):
        score += 0.10

    return round(min(1.0, score), 3)


def _build_fk_graph(conn, source_id: int, schema_snap_id: int) -> dict[str, list[dict]]:
    """
    Build a bidirectional adjacency list from table_relationships.
    Bidirectional so path-tracing can traverse FKs in either direction —
    both 'A references B' and 'B is referenced by A' are valid business
    connections.
    """
    rows = conn.execute(
        """SELECT from_table_fqn, to_table_fqn,
                  from_column, to_column, relationship_name, confidence
           FROM table_relationships
           WHERE source_id = ? AND snapshot_id = ?
             AND relationship_status IN ('AUTO', 'APPROVED')""",
        (source_id, schema_snap_id),
    ).fetchall()

    adj: dict[str, list[dict]] = {}
    for r in rows:
        f, t = r["from_table_fqn"], r["to_table_fqn"]
        adj.setdefault(f, []).append({
            "table_fqn":         t,
            "from_column":       r["from_column"],
            "to_column":         r["to_column"],
            "relationship_name": r["relationship_name"],
            "direction":         "outbound",
            "confidence":        float(r["confidence"]),
        })
        adj.setdefault(t, []).append({
            "table_fqn":         f,
            "from_column":       r["to_column"],
            "to_column":         r["from_column"],
            "relationship_name": r["relationship_name"],
            "direction":         "inbound",
            "confidence":        float(r["confidence"]),
        })
    return adj


def _empty_related(fqn: str) -> dict:
    return {
        "table_fqn":         fqn,
        "relationship_types": [],
        "confidence":        0.0,
        "evidence":          [],
    }


def _safe_rate(num: int, den: int) -> float:
    return round(num / den, 3) if den > 0 else 0.0


# ---------------------------------------------------------------------------
# Public reasoning functions
# ---------------------------------------------------------------------------

def get_related_tables(
    source_id: int,
    user_id: str,
    table_fqn: str,
) -> dict | None:
    """
    Return tables related to the given table by FK edges, shared domain,
    or shared entity — ranked by confidence, FK-direct first.

    Confidence propagation:
      FK_OUTBOUND / FK_INBOUND : stored FK confidence  (≈ 1.0)
      SAME_DOMAIN              : domain_confidence × 0.5
      SAME_ENTITY              : entity_confidence × 0.4

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        schema_snap_id = _latest_schema_snap_id(conn, source_id)

        # ── FK relationships ──────────────────────────────────────────────
        fk_outbound: list = []
        fk_inbound:  list = []
        if schema_snap_id:
            fk_outbound = conn.execute(
                """SELECT to_table_fqn, from_column, to_column,
                          relationship_name, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ?
                     AND relationship_status IN ('AUTO', 'APPROVED')""",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()
            fk_inbound = conn.execute(
                """SELECT from_table_fqn, from_column, to_column,
                          relationship_name, confidence
                   FROM table_relationships
                   WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ?
                     AND relationship_status IN ('AUTO', 'APPROVED')""",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()

        # ── Domain peers ──────────────────────────────────────────────────
        domain_row = conn.execute(
            "SELECT domain, confidence FROM domain_assignments "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
        this_domain: str | None = None
        domain_peers: list = []
        if domain_row and domain_row["domain"] not in (None, "Unknown"):
            this_domain = domain_row["domain"]
            domain_peers = conn.execute(
                "SELECT table_fqn, confidence FROM domain_assignments "
                "WHERE source_id = ? AND domain = ? AND table_fqn != ? "
                "ORDER BY confidence DESC LIMIT 20",
                (source_id, this_domain, table_fqn),
            ).fetchall()

        # ── Entity peers ──────────────────────────────────────────────────
        entity_row = conn.execute(
            "SELECT entity, confidence FROM entity_assignments "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
        this_entity: str | None = None
        entity_peers: list = []
        if entity_row and entity_row["entity"] not in (None, "Unknown"):
            this_entity = entity_row["entity"]
            entity_peers = conn.execute(
                "SELECT table_fqn, confidence FROM entity_assignments "
                "WHERE source_id = ? AND entity = ? AND table_fqn != ? "
                "ORDER BY confidence DESC LIMIT 20",
                (source_id, this_entity, table_fqn),
            ).fetchall()

    finally:
        conn.close()

    # ── Merge into keyed dict, propagating confidence ─────────────────────
    related: dict[str, dict] = {}

    for r in fk_outbound:
        fqn = r["to_table_fqn"]
        entry = related.setdefault(fqn, _empty_related(fqn))
        entry["relationship_types"].append("FK_OUTBOUND")
        entry["confidence"] = max(entry["confidence"], float(r["confidence"]))
        entry["evidence"].append(
            f"Foreign key: {table_fqn}.{r['from_column']} → {fqn}.{r['to_column']}"
        )

    for r in fk_inbound:
        fqn = r["from_table_fqn"]
        entry = related.setdefault(fqn, _empty_related(fqn))
        entry["relationship_types"].append("FK_INBOUND")
        entry["confidence"] = max(entry["confidence"], float(r["confidence"]))
        entry["evidence"].append(
            f"Foreign key: {fqn}.{r['from_column']} → {table_fqn}.{r['to_column']}"
        )

    for r in domain_peers:
        fqn = r["table_fqn"]
        entry = related.setdefault(fqn, _empty_related(fqn))
        if "SAME_DOMAIN" not in entry["relationship_types"]:
            entry["relationship_types"].append("SAME_DOMAIN")
            entry["evidence"].append(f"Shared business domain: {this_domain}")
        entry["confidence"] = max(entry["confidence"], float(r["confidence"]) * 0.5)

    for r in entity_peers:
        fqn = r["table_fqn"]
        entry = related.setdefault(fqn, _empty_related(fqn))
        if "SAME_ENTITY" not in entry["relationship_types"]:
            entry["relationship_types"].append("SAME_ENTITY")
            entry["evidence"].append(f"Shared business entity: {this_entity}")
        entry["confidence"] = max(entry["confidence"], float(r["confidence"]) * 0.4)

    # ── Rank: FK-direct first, then by confidence desc ────────────────────
    _FK_TYPES = frozenset(("FK_OUTBOUND", "FK_INBOUND"))
    sorted_items = sorted(
        related.values(),
        key=lambda x: (
            0 if any(t in _FK_TYPES for t in x["relationship_types"]) else 1,
            -x["confidence"],
        ),
    )

    return {
        "source_id":      source_id,
        "table_fqn":      table_fqn,
        "domain":         this_domain,
        "entity":         this_entity,
        "related_tables": sorted_items,
        "total_related":  len(sorted_items),
        "fk_direct":      sum(1 for x in sorted_items if any(t in _FK_TYPES for t in x["relationship_types"])),
        "domain_related": sum(1 for x in sorted_items if "SAME_DOMAIN" in x["relationship_types"]),
        "entity_related": sum(1 for x in sorted_items if "SAME_ENTITY" in x["relationship_types"]),
    }


def find_business_assets(
    source_id: int,
    user_id: str,
    *,
    domain: str | None = None,
    entity: str | None = None,
    term: str | None = None,
) -> dict | None:
    """
    Discover all business assets (tables, columns, FK relationships,
    PII columns, business definitions) matching domain, entity, or a
    free-text term searched against dictionary business names/descriptions.

    At least one filter must be non-empty.
    Returns None when source not found or not owned by user_id.
    """
    if not any([domain, entity, term]):
        return {"error": "At least one filter (domain, entity, or term) is required."}

    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        schema_snap_id = _latest_schema_snap_id(conn, source_id)
        prof_snap_id   = _latest_profiling_snap_id(conn, source_id)

        # ── Collect qualifying table FQNs ─────────────────────────────────
        qualified: set[str] = set()

        if domain:
            rows = conn.execute(
                "SELECT table_fqn FROM domain_assignments "
                "WHERE source_id = ? AND domain = ?",
                (source_id, domain),
            ).fetchall()
            qualified.update(r["table_fqn"] for r in rows)

        if entity:
            rows = conn.execute(
                "SELECT table_fqn FROM entity_assignments "
                "WHERE source_id = ? AND entity = ?",
                (source_id, entity),
            ).fetchall()
            qualified.update(r["table_fqn"] for r in rows)

        if term:
            like = f"%{term}%"
            rows = conn.execute(
                "SELECT table_fqn FROM data_dictionary_tables "
                "WHERE source_id = ? AND (business_name LIKE ? OR description LIKE ? OR grain LIKE ?)",
                (source_id, like, like, like),
            ).fetchall()
            qualified.update(r["table_fqn"] for r in rows)

            col_rows = conn.execute(
                "SELECT DISTINCT table_fqn FROM data_dictionary_columns "
                "WHERE source_id = ? AND (business_label LIKE ? OR meaning LIKE ?)",
                (source_id, like, like),
            ).fetchall()
            qualified.update(r["table_fqn"] for r in col_rows)

        if not qualified:
            return {
                "source_id": source_id,
                "filters": {"domain": domain, "entity": entity, "term": term},
                "tables": [], "columns": [], "relationships": [],
                "pii_assets": [], "business_definitions": [],
                "total_assets": 0, "total_tables": 0, "total_columns": 0,
                "total_relationships": 0, "total_pii_columns": 0,
            }

        fqn_list   = list(qualified)
        ph         = ",".join("?" * len(fqn_list))

        # ── Tables (with domain + entity context) ─────────────────────────
        tbl_rows = conn.execute(
            f"""SELECT ddt.table_fqn, ddt.table_name, ddt.schema_name,
                       ddt.business_name, ddt.description, ddt.is_approved,
                       ddt.generation_method,
                       da.domain     AS assigned_domain,
                       da.confidence AS domain_confidence,
                       ea.entity     AS assigned_entity,
                       ea.confidence AS entity_confidence
                FROM data_dictionary_tables ddt
                LEFT JOIN domain_assignments da
                  ON da.source_id = ddt.source_id AND da.table_fqn = ddt.table_fqn
                LEFT JOIN entity_assignments ea
                  ON ea.source_id = ddt.source_id AND ea.table_fqn = ddt.table_fqn
                WHERE ddt.source_id = ? AND ddt.table_fqn IN ({ph})
                ORDER BY ddt.schema_name, ddt.table_name""",
            (source_id, *fqn_list),
        ).fetchall()
        dict_fqns = {r["table_fqn"] for r in tbl_rows}
        tables = [dict(r) for r in tbl_rows]

        # Tables in scope that have no dictionary entry yet
        for fqn in qualified - dict_fqns:
            tables.append({
                "table_fqn": fqn, "business_name": None, "description": None,
                "is_approved": False, "assigned_domain": None, "assigned_entity": None,
            })

        # ── Columns ───────────────────────────────────────────────────────
        col_rows = conn.execute(
            f"""SELECT table_fqn, column_name, business_label, meaning,
                       semantic_type, pii_risk, is_approved
                FROM data_dictionary_columns
                WHERE source_id = ? AND table_fqn IN ({ph})
                ORDER BY table_fqn, column_name""",
            (source_id, *fqn_list),
        ).fetchall()
        columns = [dict(r) for r in col_rows]

        # ── FK Relationships touching these tables ────────────────────────
        relationships: list[dict] = []
        if schema_snap_id:
            rel_rows = conn.execute(
                f"""SELECT from_table_fqn, to_table_fqn, from_column, to_column,
                           relationship_name, relationship_type, confidence
                    FROM table_relationships
                    WHERE source_id = ? AND snapshot_id = ?
                      AND (from_table_fqn IN ({ph}) OR to_table_fqn IN ({ph}))
                      AND relationship_status IN ('AUTO', 'APPROVED')""",
                (source_id, schema_snap_id, *fqn_list, *fqn_list),
            ).fetchall()
            relationships = [dict(r) for r in rel_rows]

        # ── PII assets ────────────────────────────────────────────────────
        pii_assets: list[dict] = []
        if prof_snap_id:
            pii_rows = conn.execute(
                f"""SELECT table_fqn, column_name, semantic_type,
                           pii_name_heuristic, pii_confirmed
                    FROM profiling_column_profiles
                    WHERE source_id = ? AND profiling_snapshot_id = ?
                      AND pii_name_heuristic = 1
                      AND table_fqn IN ({ph})
                    ORDER BY table_fqn, column_name""",
                (source_id, prof_snap_id, *fqn_list),
            ).fetchall()
            pii_assets = [dict(r) for r in pii_rows]

        # ── Business definitions (tables with a business name) ────────────
        def_rows = conn.execute(
            f"""SELECT table_fqn, business_name, description, grain
                FROM data_dictionary_tables
                WHERE source_id = ? AND table_fqn IN ({ph})
                  AND business_name IS NOT NULL
                ORDER BY table_fqn""",
            (source_id, *fqn_list),
        ).fetchall()
        business_definitions = [dict(r) for r in def_rows]

    finally:
        conn.close()

    return {
        "source_id": source_id,
        "filters": {"domain": domain, "entity": entity, "term": term},
        "tables":               tables,
        "columns":              columns,
        "relationships":        relationships,
        "pii_assets":           pii_assets,
        "business_definitions": business_definitions,
        "total_assets":         len(tables) + len(columns),
        "total_tables":         len(tables),
        "total_columns":        len(columns),
        "total_relationships":  len(relationships),
        "total_pii_columns":    len(pii_assets),
    }


def explain_table(
    source_id: int,
    user_id: str,
    table_fqn: str,
) -> dict | None:
    """
    Produce a structured, evidence-backed explanation of a table by composing
    all available enterprise metadata layers. No LLM — pure structural reasoning.

    Produces:
      business_purpose     — best available human-readable description
      business_importance  — derived score + label (CRITICAL/HIGH/MEDIUM/LOW)
      evidence             — list of supporting signals from stored metadata
      gaps                 — missing metadata layers that would improve coverage
      governance           — per-dimension governance status flags
      governance_score     — fraction of governance dimensions satisfied (0–1)

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if not source_row:
            return None

        schema_snap_id = _latest_schema_snap_id(conn, source_id)
        prof_snap_id   = _latest_profiling_snap_id(conn, source_id)

        dict_row = conn.execute(
            "SELECT business_name, description, grain, is_approved, "
            "approved_by, generation_method FROM data_dictionary_tables "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()

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

        table_profile = None
        if prof_snap_id:
            table_profile = conn.execute(
                """SELECT table_name, schema_name, table_type,
                          exact_row_count, estimated_row_count, row_count_tier,
                          fk_count, referenced_by_count,
                          is_root_table, is_leaf_table, is_junction_table,
                          table_class, classification_confidence,
                          pii_column_count, confirmed_pii_count,
                          completeness_score, profiling_depth, profiling_status
                   FROM profiling_table_profiles
                   WHERE profiling_snapshot_id = ? AND table_fqn = ?""",
                (prof_snap_id, table_fqn),
            ).fetchone()

        outbound_rels: list = []
        inbound_rels:  list = []
        if schema_snap_id:
            outbound_rels = conn.execute(
                "SELECT to_table_fqn, from_column, to_column, relationship_name "
                "FROM table_relationships "
                "WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ? "
                "AND relationship_status IN ('AUTO', 'APPROVED')",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()
            inbound_rels = conn.execute(
                "SELECT from_table_fqn, from_column, to_column, relationship_name "
                "FROM table_relationships "
                "WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ? "
                "AND relationship_status IN ('AUTO', 'APPROVED')",
                (source_id, schema_snap_id, table_fqn),
            ).fetchall()

    finally:
        conn.close()

    # ── Derive importance score ───────────────────────────────────────────
    tp = dict(table_profile) if table_profile else None
    dr = dict(dict_row)      if dict_row      else None
    importance_score = _compute_importance_score(tp, dr)
    if importance_score >= 0.7:
        importance_label = "CRITICAL"
    elif importance_score >= 0.4:
        importance_label = "HIGH"
    elif importance_score >= 0.2:
        importance_label = "MEDIUM"
    else:
        importance_label = "LOW"

    # ── Derive business purpose (best available source) ───────────────────
    if dict_row and dict_row["description"]:
        business_purpose = dict_row["description"]
    elif dict_row and dict_row["business_name"]:
        business_purpose = f"Business asset: {dict_row['business_name']}."
    elif entity_row and entity_row["entity"] not in (None, "Unknown"):
        domain_part = (
            f" in the {domain_row['domain']} domain"
            if domain_row and domain_row["domain"] not in (None, "Unknown")
            else ""
        )
        business_purpose = f"{entity_row['entity']} data{domain_part}."
    else:
        leaf = table_fqn.split(".")[-1] if "." in table_fqn else table_fqn
        business_purpose = f"Table '{leaf}' — no business description available yet."

    # ── Compile evidence signals ──────────────────────────────────────────
    evidence: list[str] = []
    if dict_row and dict_row["business_name"]:
        evidence.append(f"Dictionary business name: {dict_row['business_name']}")
    if dict_row and dict_row["is_approved"]:
        evidence.append("Dictionary entry approved by human reviewer")
    if domain_row and domain_row["domain"] not in (None, "Unknown"):
        evidence.append(
            f"Domain: {domain_row['domain']} "
            f"({round(float(domain_row['confidence']) * 100)}% confidence)"
        )
    if entity_row and entity_row["entity"] not in (None, "Unknown"):
        evidence.append(
            f"Entity: {entity_row['entity']} "
            f"({round(float(entity_row['confidence']) * 100)}% confidence)"
        )
    if table_profile:
        if table_profile["table_class"]:
            conf = round(float(table_profile["classification_confidence"] or 0) * 100)
            evidence.append(f"Classification: {table_profile['table_class']} ({conf}%)")
        if table_profile["is_root_table"]:
            evidence.append("Root table: no incoming FKs — primary business entity")
        ref = table_profile["referenced_by_count"] or 0
        if ref > 0:
            evidence.append(f"Referenced by {ref} other table(s) via FK")
        pii = table_profile["pii_column_count"] or 0
        if pii > 0:
            evidence.append(f"Contains {pii} PII-flagged column(s)")
    if outbound_rels:
        targets = ", ".join(r["to_table_fqn"] for r in outbound_rels[:3])
        suffix = f" (+{len(outbound_rels)-3} more)" if len(outbound_rels) > 3 else ""
        evidence.append(f"Outbound FK(s) to: {targets}{suffix}")
    if inbound_rels:
        sources = ", ".join(r["from_table_fqn"] for r in inbound_rels[:3])
        suffix = f" (+{len(inbound_rels)-3} more)" if len(inbound_rels) > 3 else ""
        evidence.append(f"Referenced by FK from: {sources}{suffix}")

    # ── Identify metadata gaps ────────────────────────────────────────────
    gaps: list[str] = []
    if not dict_row:
        gaps.append("No dictionary entry — run dictionary generation")
    elif not dict_row["is_approved"]:
        gaps.append("Dictionary entry not yet human-approved")
    if not domain_row or domain_row["domain"] in (None, "Unknown"):
        gaps.append("No domain assignment — run domain generation")
    if not entity_row or entity_row["entity"] in (None, "Unknown"):
        gaps.append("No entity assignment — run entity generation")
    if not table_profile:
        gaps.append("Not profiled — run structural profiling")
    if not outbound_rels and not inbound_rels:
        gaps.append("No FK relationships extracted — re-run schema discovery")

    # ── Governance dimensions ─────────────────────────────────────────────
    governance = {
        "dictionary_approved": bool(dict_row and dict_row["is_approved"]),
        "domain_assigned":     bool(domain_row and domain_row["domain"] not in (None, "Unknown")),
        "entity_assigned":     bool(entity_row and entity_row["entity"] not in (None, "Unknown")),
        "profiling_complete":  bool(table_profile and table_profile["profiling_status"] == "COMPLETE"),
        "pii_review_needed":   bool(
            table_profile and
            (table_profile["pii_column_count"] or 0) > (table_profile["confirmed_pii_count"] or 0)
        ),
    }
    _governable = ["dictionary_approved", "domain_assigned", "entity_assigned", "profiling_complete"]
    governance_score = round(sum(1 for k in _governable if governance[k]) / len(_governable), 2)

    return {
        "source_id":   source_id,
        "table_fqn":   table_fqn,
        "table_name":  (table_profile["table_name"] if table_profile else table_fqn.split(".")[-1]),
        "schema_name": (
            table_profile["schema_name"] if table_profile
            else (table_fqn.split(".")[0] if "." in table_fqn else None)
        ),
        "business_purpose":  business_purpose,
        "business_domain":   (domain_row["domain"]        if domain_row  else None),
        "business_entity":   (entity_row["entity"]        if entity_row  else None),
        "business_name":     (dict_row["business_name"]   if dict_row    else None),
        "grain":             (dict_row["grain"]           if dict_row    else None),
        "classification":    (table_profile["table_class"] if table_profile else None),
        "classification_confidence": (
            table_profile["classification_confidence"] if table_profile else None
        ),
        "profiling": {
            "row_count":         (
                (table_profile["exact_row_count"] or table_profile["estimated_row_count"])
                if table_profile else None
            ),
            "row_count_tier":    (table_profile["row_count_tier"]    if table_profile else None),
            "fk_count":          (table_profile["fk_count"]          if table_profile else None),
            "referenced_by":     (table_profile["referenced_by_count"] if table_profile else None),
            "is_root_table":     (bool(table_profile["is_root_table"])     if table_profile else None),
            "is_leaf_table":     (bool(table_profile["is_leaf_table"])     if table_profile else None),
            "is_junction_table": (bool(table_profile["is_junction_table"]) if table_profile else None),
            "completeness_score": (table_profile["completeness_score"] if table_profile else None),
            "profiling_depth":   (table_profile["profiling_depth"]    if table_profile else None),
        },
        "pii": {
            "pii_column_count":    ((table_profile["pii_column_count"]    or 0) if table_profile else 0),
            "confirmed_pii_count": ((table_profile["confirmed_pii_count"] or 0) if table_profile else 0),
            "review_needed":       governance["pii_review_needed"],
        },
        "relationships": {
            "outbound": [dict(r) for r in outbound_rels],
            "inbound":  [dict(r) for r in inbound_rels],
        },
        "business_importance": {
            "score": importance_score,
            "label": importance_label,
        },
        "governance":       governance,
        "governance_score": governance_score,
        "evidence":         evidence,
        "gaps":             gaps,
        "domain_confidence": (float(domain_row["confidence"]) if domain_row else None),
        "entity_confidence": (float(entity_row["confidence"]) if entity_row else None),
    }


def trace_business_path(
    source_id: int,
    user_id: str,
    from_fqn: str,
    to_fqn: str,
) -> dict | None:
    """
    Find the shortest business path between two tables using FK relationships
    (BFS over a bidirectional FK graph). Each hop in the path includes the
    relationship detail (column names, direction, FK name).

    Returns None when source not found or not owned by user_id.
    Returns {"found": False, ...} when no path exists.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        schema_snap_id = _latest_schema_snap_id(conn, source_id)
        if not schema_snap_id:
            return {
                "found": False, "from_table": from_fqn, "to_table": to_fqn,
                "path": [], "hops": -1,
                "message": "No schema snapshot available — run schema discovery first.",
            }

        adj = _build_fk_graph(conn, source_id, schema_snap_id)
    finally:
        conn.close()

    # Trivial case
    if from_fqn == to_fqn:
        return {
            "found": True, "from_table": from_fqn, "to_table": to_fqn,
            "path": [{"table_fqn": from_fqn, "hop": 0, "via": None}],
            "hops": 0, "message": "Source and target are the same table.",
        }

    # Neither table participates in any FK relationship
    if from_fqn not in adj and to_fqn not in adj:
        return {
            "found": False, "from_table": from_fqn, "to_table": to_fqn,
            "path": [], "hops": -1,
            "message": "Neither table has FK relationships in the knowledge graph.",
        }

    # BFS — each queue entry: (current_node, path_built_so_far)
    queue: deque = deque([
        (from_fqn, [{"table_fqn": from_fqn, "hop": 0, "via": None}])
    ])
    visited: set[str] = {from_fqn}

    while queue:
        current, path_so_far = queue.popleft()
        for edge in adj.get(current, []):
            neighbor = edge["table_fqn"]
            if neighbor in visited:
                continue
            hop_num  = len(path_so_far)
            new_node = {
                "table_fqn": neighbor,
                "hop": hop_num,
                "via": {
                    "from_column":     edge["from_column"],
                    "to_column":       edge["to_column"],
                    "relationship_name": edge["relationship_name"],
                    "direction":       edge["direction"],
                },
            }
            new_path = path_so_far + [new_node]
            if neighbor == to_fqn:
                return {
                    "found":      True,
                    "from_table": from_fqn,
                    "to_table":   to_fqn,
                    "path":       new_path,
                    "hops":       hop_num,
                    "message":    f"Path found in {hop_num} hop(s).",
                }
            visited.add(neighbor)
            queue.append((neighbor, new_path))

    return {
        "found":      False,
        "from_table": from_fqn,
        "to_table":   to_fqn,
        "path":       [],
        "hops":       -1,
        "message":    "No FK path found between these tables.",
    }


def knowledge_graph_summary(source_id: int, user_id: str) -> dict | None:
    """
    Return graph-level aggregate statistics for a source: node/edge counts,
    coverage rates per metadata dimension, and confidence distribution.
    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if not source_row:
            return None

        schema_snap_id = _latest_schema_snap_id(conn, source_id)
        prof_snap_id   = _latest_profiling_snap_id(conn, source_id)

        # Schema counts
        snap_row = conn.execute(
            "SELECT table_count, view_count, column_count FROM schema_snapshots WHERE id = ?",
            (schema_snap_id,),
        ).fetchone() if schema_snap_id else None

        total_tables  = (snap_row["table_count"]  or 0) if snap_row else 0
        total_views   = (snap_row["view_count"]   or 0) if snap_row else 0
        total_columns = (snap_row["column_count"] or 0) if snap_row else 0

        # FK edges
        rel_row = conn.execute(
            """SELECT COUNT(*) AS cnt,
                      COUNT(DISTINCT from_table_fqn) AS froms
               FROM table_relationships
               WHERE source_id = ? AND snapshot_id = ?
                 AND relationship_status IN ('AUTO', 'APPROVED')""",
            (source_id, schema_snap_id),
        ).fetchone() if schema_snap_id else None

        total_relationships = (rel_row["cnt"]   or 0) if rel_row else 0
        tables_with_fks     = (rel_row["froms"] or 0) if rel_row else 0

        # Domain coverage
        dom_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(DISTINCT domain) AS unique_domains,
                      SUM(CASE WHEN domain != 'Unknown' THEN 1 ELSE 0 END) AS assigned
               FROM domain_assignments WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

        # Entity coverage
        ent_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(DISTINCT entity) AS unique_entities,
                      SUM(CASE WHEN entity != 'Unknown' THEN 1 ELSE 0 END) AS assigned
               FROM entity_assignments WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

        # Dictionary coverage
        dct_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(is_approved), 0) AS approved
               FROM data_dictionary_tables WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

        # Profiling coverage
        prf_row = conn.execute(
            "SELECT tables_profiled, columns_profiled, pii_columns_found "
            "FROM profiling_snapshots WHERE id = ?",
            (prof_snap_id,),
        ).fetchone() if prof_snap_id else None

        # Domain confidence distribution
        conf_row = conn.execute(
            """SELECT
                 SUM(CASE WHEN confidence >= 0.8 THEN 1 ELSE 0 END) AS high,
                 SUM(CASE WHEN confidence >= 0.5 AND confidence < 0.8 THEN 1 ELSE 0 END) AS medium,
                 SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END) AS low
               FROM domain_assignments WHERE source_id = ?""",
            (source_id,),
        ).fetchone()

    finally:
        conn.close()

    dom_assigned = int(dom_row["assigned"] or 0) if dom_row else 0
    ent_assigned = int(ent_row["assigned"] or 0) if ent_row else 0
    dct_total    = int(dct_row["total"]    or 0) if dct_row else 0
    dct_approved = int(dct_row["approved"] or 0) if dct_row else 0
    prf_tables   = int(prf_row["tables_profiled"]  or 0) if prf_row else 0
    prf_columns  = int(prf_row["columns_profiled"] or 0) if prf_row else 0
    prf_pii      = int(prf_row["pii_columns_found"] or 0) if prf_row else 0

    return {
        "source_id": source_id,
        "source": {
            "display_name": source_row["display_name"],
            "source_type":  source_row["source_type"],
        },
        "nodes": {
            "total_tables":  total_tables,
            "total_views":   total_views,
            "total_columns": total_columns,
        },
        "edges": {
            "total_relationships":  total_relationships,
            "tables_with_fks":      tables_with_fks,
            "relationship_density": _safe_rate(total_relationships, total_tables),
        },
        "domain_coverage": {
            "tables_assigned":  dom_assigned,
            "tables_total":     int(dom_row["total"] or 0) if dom_row else 0,
            "unique_domains":   int(dom_row["unique_domains"] or 0) if dom_row else 0,
            "coverage_rate":    _safe_rate(dom_assigned, total_tables),
        },
        "entity_coverage": {
            "tables_assigned":  ent_assigned,
            "tables_total":     int(ent_row["total"] or 0) if ent_row else 0,
            "unique_entities":  int(ent_row["unique_entities"] or 0) if ent_row else 0,
            "coverage_rate":    _safe_rate(ent_assigned, total_tables),
        },
        "dictionary_coverage": {
            "tables_with_definitions": dct_total,
            "tables_approved":         dct_approved,
            "coverage_rate":           _safe_rate(dct_total, total_tables),
        },
        "profiling_coverage": {
            "tables_profiled":   prf_tables,
            "columns_profiled":  prf_columns,
            "pii_columns_found": prf_pii,
            "coverage_rate":     _safe_rate(prf_tables, total_tables),
        },
        "relationship_coverage": {
            "tables_with_fks": tables_with_fks,
            "coverage_rate":   _safe_rate(tables_with_fks, total_tables),
        },
        "confidence_distribution": {
            "high":   int(conf_row["high"]   or 0) if conf_row else 0,
            "medium": int(conf_row["medium"] or 0) if conf_row else 0,
            "low":    int(conf_row["low"]    or 0) if conf_row else 0,
        },
        "governance_coverage": {
            "approved_definitions": dct_approved,
            "total_definitions":    dct_total,
            "approval_rate":        _safe_rate(dct_approved, dct_total),
        },
    }
