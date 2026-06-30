"""
Business Lineage & Impact Analysis — Phase 5.

Reasons over existing FK relationships, profiling, dictionary, domain,
entity, and governance metadata to produce enterprise-grade lineage traces,
impact assessments, and critical-asset rankings.

NO new storage. NO graph database. NO AI. NO LLM.
Pure structural reasoning over the metadata already produced by all prior
phases of the Business Knowledge Graph.

Terminology used throughout this module:
  upstream   — tables this table DEPENDS ON (follows outbound FK edges)
  downstream — tables that DEPEND ON this table (follows inbound FK edges)
  foundation — table that depends on no other table (no outbound FKs)
  terminal   — table that no other table depends on (no inbound FKs)
  hub        — table that is both depended-on and depends on others
  disconnected — table with zero FK connections
"""
import logging
from collections import deque

from data.db import get_connection

logger = logging.getLogger(__name__)

_MAX_DEPTH = 15   # guard against very deep or cyclic graphs


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _verify_source(conn, source_id: int, user_id: str):
    return conn.execute(
        "SELECT id, display_name, source_type FROM data_source_connections "
        "WHERE id = ? AND user_id = ?",
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


def _load_fk_edges(conn, source_id: int, schema_snap_id: int) -> list[dict]:
    """
    Return trusted relationship edges for the latest schema snapshot.

    Trusted = relationship_status IN ('AUTO', 'APPROVED') — declared FKs and
    steward-approved inferred relationships. PENDING/REJECTED candidates are
    never auto-trusted into lineage reasoning.
    """
    rows = conn.execute(
        """SELECT from_table_fqn, to_table_fqn,
                  from_column, to_column, relationship_name, confidence
           FROM table_relationships
           WHERE source_id = ? AND snapshot_id = ?
             AND relationship_status IN ('AUTO', 'APPROVED')""",
        (source_id, schema_snap_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_directed_adj(edges: list[dict]) -> tuple[dict, dict]:
    """
    Build two directed adjacency lists from FK edges.

    upstream_adj[F]   = list of edges where F is the referencing table
                        (F has FK pointing TO T — T is upstream of F)
    downstream_adj[T] = list of edges where T is the referenced table
                        (F has FK pointing TO T — F is downstream of T)
    """
    upstream_adj:   dict[str, list[dict]] = {}
    downstream_adj: dict[str, list[dict]] = {}

    for e in edges:
        f, t = e["from_table_fqn"], e["to_table_fqn"]
        upstream_adj.setdefault(f, []).append(e)
        downstream_adj.setdefault(t, []).append(e)

    return upstream_adj, downstream_adj


def _enrich_nodes(
    conn,
    source_id: int,
    fqns: set[str],
    prof_snap_id: int | None,
) -> dict[str, dict]:
    """
    Bulk-fetch domain, entity, dictionary, and profiling metadata for a set
    of table FQNs. Returns a mapping: fqn → enrichment dict.
    Missing metadata layers produce None values rather than errors.
    """
    if not fqns:
        return {}

    result: dict[str, dict] = {fqn: {"table_fqn": fqn} for fqn in fqns}
    ph = ",".join("?" * len(fqns))
    fqn_list = list(fqns)

    for r in conn.execute(
        f"SELECT table_fqn, domain, confidence FROM domain_assignments "
        f"WHERE source_id = ? AND table_fqn IN ({ph})",
        (source_id, *fqn_list),
    ).fetchall():
        result[r["table_fqn"]]["domain"] = r["domain"]
        result[r["table_fqn"]]["domain_confidence"] = float(r["confidence"])

    for r in conn.execute(
        f"SELECT table_fqn, entity, confidence FROM entity_assignments "
        f"WHERE source_id = ? AND table_fqn IN ({ph})",
        (source_id, *fqn_list),
    ).fetchall():
        result[r["table_fqn"]]["entity"] = r["entity"]
        result[r["table_fqn"]]["entity_confidence"] = float(r["confidence"])

    for r in conn.execute(
        f"SELECT table_fqn, business_name, description, is_approved "
        f"FROM data_dictionary_tables WHERE source_id = ? AND table_fqn IN ({ph})",
        (source_id, *fqn_list),
    ).fetchall():
        result[r["table_fqn"]]["business_name"] = r["business_name"]
        result[r["table_fqn"]]["description"] = r["description"]
        result[r["table_fqn"]]["dict_approved"] = bool(r["is_approved"])

    if prof_snap_id:
        for r in conn.execute(
            f"""SELECT table_fqn, table_class, classification_confidence,
                       pii_column_count, confirmed_pii_count,
                       referenced_by_count, fk_count
                FROM profiling_table_profiles
                WHERE profiling_snapshot_id = ? AND source_id = ? AND table_fqn IN ({ph})""",
            (prof_snap_id, source_id, *fqn_list),
        ).fetchall():
            t = r["table_fqn"]
            result[t]["table_class"]               = r["table_class"]
            result[t]["classification_confidence"] = r["classification_confidence"]
            result[t]["pii_column_count"]          = r["pii_column_count"] or 0
            result[t]["confirmed_pii_count"]       = r["confirmed_pii_count"] or 0
            result[t]["referenced_by_count"]       = r["referenced_by_count"] or 0
            result[t]["fk_count"]                  = r["fk_count"] or 0

    return result


def _bfs_lineage(
    start: str,
    adj: dict[str, list[dict]],
    direction: str,
    max_depth: int = _MAX_DEPTH,
) -> tuple[list[dict], bool]:
    """
    BFS traversal over a directed adjacency list.

    direction = 'upstream'   → follow upstream_adj (outbound FK edges)
    direction = 'downstream' → follow downstream_adj (inbound FK edges)

    Returns (nodes, has_cycle) where each node dict contains:
      table_fqn, distance, relationship_type, via_column, parent_column,
      relationship_name, confidence
    """
    visited:   set[str]    = {start}
    queue:     deque       = deque([(start, 0)])
    nodes:     list[dict]  = []
    has_cycle: bool        = False

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue

        for edge in adj.get(current, []):
            neighbor = (
                edge["to_table_fqn"]   if direction == "upstream"
                else edge["from_table_fqn"]
            )
            if neighbor == start:
                has_cycle = True
                continue
            if neighbor in visited:
                has_cycle = True
                continue

            rel_type = (
                "FK_PARENT"   if direction == "upstream" and depth == 0
                else "FK_ANCESTOR" if direction == "upstream"
                else "FK_CHILD"    if depth == 0
                else "FK_DESCENDANT"
            )

            via_col    = edge["from_column"] if direction == "upstream" else edge["to_column"]
            parent_col = edge["to_column"]   if direction == "upstream" else edge["from_column"]

            nodes.append({
                "table_fqn":         neighbor,
                "distance":          depth + 1,
                "relationship_type": rel_type,
                "via_column":        via_col,
                "parent_column":     parent_col,
                "relationship_name": edge["relationship_name"],
                "confidence":        edge["confidence"],
            })
            visited.add(neighbor)
            queue.append((neighbor, depth + 1))

    return nodes, has_cycle


def _compute_impact_score(affected: list[dict]) -> float:
    """
    Compute a 0–1 impact score from the enriched downstream node list.
    Score components: breadth, PII presence, governance, domain spread,
    and presence of highly-referenced (Master/Reference) downstream tables.
    """
    if not affected:
        return 0.0

    n = len(affected)
    breadth   = min(0.35, 0.07 * n)
    pii       = 0.25 if any((t.get("pii_column_count") or 0) > 0 for t in affected) else 0.0
    governed  = min(0.15, 0.05 * sum(1 for t in affected if t.get("dict_approved")))
    domains   = {t.get("domain") for t in affected if t.get("domain") and t["domain"] != "Unknown"}
    dom_score = min(0.15, 0.05 * len(domains))
    cls_score = 0.10 if any(
        t.get("table_class") in ("Master", "Reference") for t in affected
    ) else 0.0

    return round(min(1.0, breadth + pii + governed + dom_score + cls_score), 3)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def get_upstream_lineage(
    source_id: int,
    user_id: str,
    table_fqn: str,
    max_depth: int = _MAX_DEPTH,
) -> dict | None:
    """
    Return all upstream dependencies of a table: the tables this table
    depends on, directly or transitively, via FK references.

    upstream = follow the outbound FK edge direction:
      orders.customer_id → customers.id  ⟹  customers is upstream of orders

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        if not snap_id:
            return _empty_lineage(source_id, table_fqn, "upstream")

        edges = _load_fk_edges(conn, source_id, snap_id)
        upstream_adj, _ = _build_directed_adj(edges)

        nodes, has_cycle = _bfs_lineage(table_fqn, upstream_adj, "upstream", max_depth)

        fqns = {n["table_fqn"] for n in nodes}
        enrichment = _enrich_nodes(conn, source_id, fqns, prof_snap_id)
    finally:
        conn.close()

    for node in nodes:
        node.update(enrichment.get(node["table_fqn"], {}))

    return {
        "source_id":       source_id,
        "table_fqn":       table_fqn,
        "upstream":        sorted(nodes, key=lambda x: (x["distance"], x["table_fqn"])),
        "total_upstream":  len(nodes),
        "max_depth":       max(n["distance"] for n in nodes) if nodes else 0,
        "has_cycle":       has_cycle,
    }


def get_downstream_lineage(
    source_id: int,
    user_id: str,
    table_fqn: str,
    max_depth: int = _MAX_DEPTH,
) -> dict | None:
    """
    Return all downstream dependents of a table: tables that depend on this
    table, directly or transitively, via FK references.

    downstream = follow the inbound FK edge direction:
      orders.customer_id → customers.id  ⟹  orders is downstream of customers

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        if not snap_id:
            return _empty_lineage(source_id, table_fqn, "downstream")

        edges = _load_fk_edges(conn, source_id, snap_id)
        _, downstream_adj = _build_directed_adj(edges)

        nodes, has_cycle = _bfs_lineage(table_fqn, downstream_adj, "downstream", max_depth)

        fqns = {n["table_fqn"] for n in nodes}
        enrichment = _enrich_nodes(conn, source_id, fqns, prof_snap_id)
    finally:
        conn.close()

    for node in nodes:
        node.update(enrichment.get(node["table_fqn"], {}))

    return {
        "source_id":        source_id,
        "table_fqn":        table_fqn,
        "downstream":       sorted(nodes, key=lambda x: (x["distance"], x["table_fqn"])),
        "total_downstream": len(nodes),
        "max_depth":        max(n["distance"] for n in nodes) if nodes else 0,
        "has_cycle":        has_cycle,
    }


def _empty_lineage(source_id: int, table_fqn: str, direction: str) -> dict:
    key = direction  # "upstream" or "downstream"
    return {
        "source_id": source_id,
        "table_fqn": table_fqn,
        direction:   [],
        f"total_{key}": 0,
        "max_depth":    0,
        "has_cycle":    False,
    }


def impact_analysis(
    source_id: int,
    user_id: str,
    table_fqn: str,
    column_name: str | None = None,
) -> dict | None:
    """
    Assess the blast radius of a change to the given table (or column).

    Traverses all downstream dependents, then enriches each with domain,
    entity, PII, and governance metadata to compute an overall impact score.

    When column_name is provided, also identifies FK edges that propagate
    through that specific column so column-level impact paths are visible.

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        if not snap_id:
            return _empty_impact(source_id, table_fqn, column_name)

        edges = _load_fk_edges(conn, source_id, snap_id)
        _, downstream_adj = _build_directed_adj(edges)

        nodes, _ = _bfs_lineage(table_fqn, downstream_adj, "downstream")

        fqns = {n["table_fqn"] for n in nodes}
        enrichment = _enrich_nodes(conn, source_id, fqns, prof_snap_id)

        # PII columns in affected tables
        pii_assets: list[dict] = []
        business_definitions: list[dict] = []
        governed_assets: list[dict] = []

        if fqns and prof_snap_id:
            ph = ",".join("?" * len(fqns))
            fqn_list = list(fqns)
            pii_rows = conn.execute(
                f"""SELECT table_fqn, column_name, pii_name_heuristic, pii_confirmed
                    FROM profiling_column_profiles
                    WHERE source_id = ? AND profiling_snapshot_id = ?
                      AND pii_name_heuristic = 1 AND table_fqn IN ({ph})
                    ORDER BY table_fqn, column_name""",
                (source_id, prof_snap_id, *fqn_list),
            ).fetchall()
            pii_assets = [dict(r) for r in pii_rows]

        if fqns:
            ph = ",".join("?" * len(fqns))
            fqn_list = list(fqns)
            def_rows = conn.execute(
                f"""SELECT table_fqn, business_name, description, is_approved
                    FROM data_dictionary_tables
                    WHERE source_id = ? AND table_fqn IN ({ph}) AND business_name IS NOT NULL
                    ORDER BY table_fqn""",
                (source_id, *fqn_list),
            ).fetchall()
            business_definitions = [dict(r) for r in def_rows]
            governed_assets = [dict(r) for r in def_rows if r["is_approved"]]

        # Column-level propagation paths (which FK edges carry the specified column)
        column_paths: list[dict] = []
        if column_name:
            for e in edges:
                if (e["from_table_fqn"] == table_fqn and e["from_column"] == column_name) or \
                   (e["to_table_fqn"]   == table_fqn and e["to_column"]   == column_name):
                    column_paths.append({
                        "from_table":  e["from_table_fqn"],
                        "from_column": e["from_column"],
                        "to_table":    e["to_table_fqn"],
                        "to_column":   e["to_column"],
                        "fk_name":     e["relationship_name"],
                    })
    finally:
        conn.close()

    # Merge enrichment into nodes
    affected: list[dict] = []
    for node in sorted(nodes, key=lambda x: (x["distance"], x["table_fqn"])):
        entry = dict(node)
        entry.update(enrichment.get(node["table_fqn"], {}))
        affected.append(entry)

    affected_domains  = sorted({a.get("domain")  for a in affected if a.get("domain")  and a["domain"]  != "Unknown"})
    affected_entities = sorted({a.get("entity")  for a in affected if a.get("entity")  and a["entity"]  != "Unknown"})

    impact_score = _compute_impact_score(affected)
    if impact_score >= 0.7:
        impact_label = "CRITICAL"
    elif impact_score >= 0.4:
        impact_label = "HIGH"
    elif impact_score >= 0.2:
        impact_label = "MEDIUM"
    else:
        impact_label = "LOW"

    return {
        "source_id":                 source_id,
        "table_fqn":                 table_fqn,
        "column_name":               column_name,
        "affected_tables":           affected,
        "affected_domains":          affected_domains,
        "affected_entities":         affected_entities,
        "affected_business_definitions": business_definitions,
        "affected_pii_assets":       pii_assets,
        "affected_governed_assets":  governed_assets,
        "affected_reports":          [],   # placeholder for future report lineage
        "column_propagation_paths":  column_paths,
        "impact_score":              impact_score,
        "impact_label":              impact_label,
        "total_affected_tables":     len(affected),
        "total_pii_columns":         len(pii_assets),
        "total_governed_assets":     len(governed_assets),
    }


def _empty_impact(source_id: int, table_fqn: str, column_name: str | None) -> dict:
    return {
        "source_id": source_id, "table_fqn": table_fqn, "column_name": column_name,
        "affected_tables": [], "affected_domains": [], "affected_entities": [],
        "affected_business_definitions": [], "affected_pii_assets": [],
        "affected_governed_assets": [], "affected_reports": [],
        "column_propagation_paths": [],
        "impact_score": 0.0, "impact_label": "LOW",
        "total_affected_tables": 0, "total_pii_columns": 0, "total_governed_assets": 0,
    }


def critical_asset_analysis(source_id: int, user_id: str) -> dict | None:
    """
    Identify and rank business-critical assets across six dimensions:

    foundation_tables  — no outbound FKs (depend on nothing)
    terminal_tables    — no inbound FKs (nothing depends on them)
    hub_tables         — both inbound AND outbound FK connections
    highly_referenced  — most referenced by FK (top 10 by referenced_by_count)
    business_critical  — fully governed: approved dict + domain + entity
    governance_critical — need attention: PII unconfirmed or missing metadata

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        if not snap_id:
            return {
                "source_id": source_id,
                "foundation_tables": [], "terminal_tables": [], "hub_tables": [],
                "highly_referenced": [], "business_critical": [], "governance_critical": [],
                "summary": {"total_foundation": 0, "total_terminal": 0,
                            "total_hub": 0, "total_disconnected": 0},
            }

        edges = _load_fk_edges(conn, source_id, snap_id)
        from_fqns = {e["from_table_fqn"] for e in edges}   # tables with outbound FKs
        to_fqns   = {e["to_table_fqn"]   for e in edges}   # tables with inbound FKs
        all_fk_fqns = from_fqns | to_fqns

        # Categorise by FK connectivity
        foundation_fqns   = to_fqns  - from_fqns        # referenced but don't reference others
        terminal_fqns     = from_fqns - to_fqns          # reference others but aren't referenced
        hub_fqns          = from_fqns & to_fqns          # both directions
        disconnected_fqns: set[str] = set()

        # Also identify disconnected from profiling (have no FK connections at all)
        if prof_snap_id:
            prof_rows = conn.execute(
                "SELECT table_fqn FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ? AND source_id = ? "
                "AND fk_count = 0 AND referenced_by_count = 0",
                (prof_snap_id, source_id),
            ).fetchall()
            disconnected_fqns = {r["table_fqn"] for r in prof_rows} - all_fk_fqns

        # Highly referenced: top 10 by referenced_by_count from profiling
        highly_ref_rows: list = []
        if prof_snap_id:
            highly_ref_rows = conn.execute(
                """SELECT table_fqn, referenced_by_count, fk_count,
                          table_class, classification_confidence, pii_column_count
                   FROM profiling_table_profiles
                   WHERE profiling_snapshot_id = ? AND source_id = ?
                     AND referenced_by_count > 0
                   ORDER BY referenced_by_count DESC LIMIT 10""",
                (prof_snap_id, source_id),
            ).fetchall()

        # Business critical: approved dict + known domain + known entity
        biz_crit_rows = conn.execute(
            """SELECT ddt.table_fqn
               FROM data_dictionary_tables ddt
               JOIN domain_assignments da ON da.source_id = ddt.source_id
                    AND da.table_fqn = ddt.table_fqn AND da.domain != 'Unknown'
               JOIN entity_assignments ea ON ea.source_id = ddt.source_id
                    AND ea.table_fqn = ddt.table_fqn AND ea.entity != 'Unknown'
               WHERE ddt.source_id = ? AND ddt.is_approved = 1""",
            (source_id,),
        ).fetchall()
        biz_crit_fqns = {r["table_fqn"] for r in biz_crit_rows}

        # Governance critical: PII pending review or missing key metadata
        gov_crit_rows: list = []
        if prof_snap_id:
            gov_crit_rows = conn.execute(
                """SELECT ptp.table_fqn,
                          ptp.pii_column_count,
                          ptp.confirmed_pii_count,
                          (CASE WHEN da.table_fqn IS NULL THEN 1 ELSE 0 END) AS missing_domain,
                          (CASE WHEN ea.table_fqn IS NULL THEN 1 ELSE 0 END) AS missing_entity,
                          (CASE WHEN ddt.table_fqn IS NULL THEN 1 ELSE 0 END) AS missing_dict
                   FROM profiling_table_profiles ptp
                   LEFT JOIN domain_assignments da
                     ON da.source_id = ptp.source_id AND da.table_fqn = ptp.table_fqn
                        AND da.domain != 'Unknown'
                   LEFT JOIN entity_assignments ea
                     ON ea.source_id = ptp.source_id AND ea.table_fqn = ptp.table_fqn
                        AND ea.entity != 'Unknown'
                   LEFT JOIN data_dictionary_tables ddt
                     ON ddt.source_id = ptp.source_id AND ddt.table_fqn = ptp.table_fqn
                        AND ddt.is_approved = 1
                   WHERE ptp.profiling_snapshot_id = ? AND ptp.source_id = ?
                     AND (
                       (ptp.pii_column_count > 0 AND ptp.confirmed_pii_count < ptp.pii_column_count)
                       OR da.table_fqn IS NULL
                       OR ea.table_fqn IS NULL
                       OR ddt.table_fqn IS NULL
                     )
                   ORDER BY ptp.pii_column_count DESC, ptp.referenced_by_count DESC
                   LIMIT 20""",
                (prof_snap_id, source_id),
            ).fetchall()

        # Enrich foundation, terminal, hub sets
        all_enrichment_fqns = foundation_fqns | terminal_fqns | hub_fqns | biz_crit_fqns
        enrichment = _enrich_nodes(conn, source_id, all_enrichment_fqns, prof_snap_id)

    finally:
        conn.close()

    def _enrich_list(fqns_set: set[str]) -> list[dict]:
        return sorted(
            [enrichment.get(fqn, {"table_fqn": fqn}) for fqn in fqns_set],
            key=lambda x: x.get("referenced_by_count") or 0,
            reverse=True,
        )

    return {
        "source_id":          source_id,
        "foundation_tables":  _enrich_list(foundation_fqns),
        "terminal_tables":    _enrich_list(terminal_fqns),
        "hub_tables":         _enrich_list(hub_fqns),
        "highly_referenced": [dict(r) for r in highly_ref_rows],
        "business_critical": sorted(
            [enrichment.get(fqn, {"table_fqn": fqn}) for fqn in biz_crit_fqns],
            key=lambda x: x.get("domain_confidence") or 0,
            reverse=True,
        ),
        "governance_critical": [dict(r) for r in gov_crit_rows],
        "summary": {
            "total_foundation":   len(foundation_fqns),
            "total_terminal":     len(terminal_fqns),
            "total_hub":          len(hub_fqns),
            "total_disconnected": len(disconnected_fqns),
        },
    }


def lineage_summary(source_id: int, user_id: str) -> dict | None:
    """
    Return graph-level lineage statistics: root/leaf/hub/disconnected assets,
    the longest dependency chain, average depth, and coverage metrics.

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if not source_row:
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        # Total schema table count
        snap_meta = conn.execute(
            "SELECT table_count FROM schema_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone() if snap_id else None
        total_schema_tables = (snap_meta["table_count"] or 0) if snap_meta else 0

        if not snap_id:
            return _empty_lineage_summary(source_id, source_row, total_schema_tables)

        edges = _load_fk_edges(conn, source_id, snap_id)
        from_fqns = {e["from_table_fqn"] for e in edges}
        to_fqns   = {e["to_table_fqn"]   for e in edges}
        all_fk_fqns = from_fqns | to_fqns

        # Lineage categories derived from edge sets
        foundation_fqns = to_fqns  - from_fqns   # no outbound FKs
        terminal_fqns   = from_fqns - to_fqns     # no inbound FKs
        hub_fqns        = from_fqns & to_fqns     # both

        # Disconnected: in profiling but not in any FK edge
        disconnected_fqns: set[str] = set()
        if prof_snap_id:
            prof_fqns_rows = conn.execute(
                "SELECT table_fqn FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ? AND source_id = ?",
                (prof_snap_id, source_id),
            ).fetchall()
            all_profiled = {r["table_fqn"] for r in prof_fqns_rows}
            disconnected_fqns = all_profiled - all_fk_fqns

        # Longest chain: BFS from each foundation table following downstream adj
        _, downstream_adj = _build_directed_adj(edges)
        longest_len, longest_path = _find_longest_chain(foundation_fqns, downstream_adj)

        # Average depth: for each table in the graph, find its BFS depth from
        # nearest foundation; average across all reachable tables
        avg_depth = _compute_average_depth(foundation_fqns, downstream_adj)

        # Enrich summary node lists (top 5 each to keep payload small)
        all_summary_fqns = (
            set(list(foundation_fqns)[:5]) |
            set(list(terminal_fqns)[:5])   |
            set(list(hub_fqns)[:5])        |
            set(list(disconnected_fqns)[:5])
        )
        enrichment = _enrich_nodes(conn, source_id, all_summary_fqns, prof_snap_id)

    finally:
        conn.close()

    def _top5(fqns: set[str]) -> list[dict]:
        items = [enrichment.get(fqn, {"table_fqn": fqn}) for fqn in list(fqns)[:5]]
        return sorted(items, key=lambda x: x["table_fqn"])

    tables_in_graph = len(all_fk_fqns)

    return {
        "source_id": source_id,
        "source": {
            "display_name": source_row["display_name"],
            "source_type":  source_row["source_type"],
        },
        "root_assets":         _top5(foundation_fqns),
        "leaf_assets":         _top5(terminal_fqns),
        "hub_assets":          _top5(hub_fqns),
        "disconnected_assets": _top5(disconnected_fqns),
        "longest_chain": {
            "length": longest_len,
            "path":   longest_path,
        },
        "average_depth": avg_depth,
        "coverage": {
            "total_schema_tables":  total_schema_tables,
            "tables_in_graph":      tables_in_graph,
            "tables_disconnected":  len(disconnected_fqns),
            "foundation_count":     len(foundation_fqns),
            "terminal_count":       len(terminal_fqns),
            "hub_count":            len(hub_fqns),
            "relationship_coverage": round(tables_in_graph / total_schema_tables, 3)
                                     if total_schema_tables else 0.0,
        },
    }


def _find_longest_chain(
    foundation_fqns: set[str],
    downstream_adj: dict[str, list[dict]],
) -> tuple[int, list[str]]:
    """
    Find the longest path from any foundation table to any reachable table
    using BFS from each foundation, tracking the maximum distance reached.
    Cycles are broken by the BFS visited set.
    """
    global_max  = 0
    global_path: list[str] = []

    for root in foundation_fqns:
        dist: dict[str, int]          = {root: 0}
        prev: dict[str, str | None]   = {root: None}
        queue: deque = deque([root])
        local_max  = 0
        local_end  = root

        while queue:
            node = queue.popleft()
            d    = dist[node]
            if d > local_max:
                local_max = d
                local_end = node
            for edge in downstream_adj.get(node, []):
                neighbor = edge["from_table_fqn"]
                if neighbor not in dist:
                    dist[neighbor] = d + 1
                    prev[neighbor] = node
                    queue.append(neighbor)

        if local_max > global_max:
            global_max = local_max
            path: list[str] = []
            n: str | None   = local_end
            while n is not None:
                path.append(n)
                n = prev.get(n)
            path.reverse()
            global_path = path

    return global_max, global_path


def _compute_average_depth(
    foundation_fqns: set[str],
    downstream_adj: dict[str, list[dict]],
) -> float:
    """
    Compute the average BFS depth of all reachable tables from any foundation.
    Tables reachable from multiple foundations take their minimum depth.
    """
    all_depths: dict[str, int] = {}

    for root in foundation_fqns:
        queue: deque = deque([(root, 0)])
        visited: set[str] = {root}
        while queue:
            node, d = queue.popleft()
            if node not in all_depths or d < all_depths[node]:
                all_depths[node] = d
            for edge in downstream_adj.get(node, []):
                neighbor = edge["from_table_fqn"]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, d + 1))

    if not all_depths:
        return 0.0
    return round(sum(all_depths.values()) / len(all_depths), 2)


def _empty_lineage_summary(source_id: int, source_row, total_tables: int) -> dict:
    return {
        "source_id": source_id,
        "source": {"display_name": source_row["display_name"], "source_type": source_row["source_type"]},
        "root_assets": [], "leaf_assets": [], "hub_assets": [], "disconnected_assets": [],
        "longest_chain": {"length": 0, "path": []},
        "average_depth": 0.0,
        "coverage": {
            "total_schema_tables": total_tables, "tables_in_graph": 0,
            "tables_disconnected": 0, "foundation_count": 0,
            "terminal_count": 0, "hub_count": 0, "relationship_coverage": 0.0,
        },
    }
