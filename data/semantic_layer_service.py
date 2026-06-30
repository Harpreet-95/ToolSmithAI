"""
Enterprise Semantic Layer & Join Intelligence — Phase 6.

Teaches ToolSmithAI the semantic meaning of joins: how tables should be
combined, what each table's business role is, where ambiguity exists, and
how to compute join confidence from existing structural metadata.

NO SQL generation. NO LLM. NO graph database. NO duplicate persistence.
Reads from: table_relationships, profiling_table_profiles, data_dictionary_tables,
            domain_assignments, entity_assignments.

Semantic roles:
  Fact      — Transactional table; has outbound FKs to dimensions
  Dimension — Master entity table; is referenced by many facts
  Lookup    — Small reference table; is referenced but rarely changes
  Bridge    — Junction/many-to-many table; is_junction_table = True
  Hub       — Highly connected table with many FK edges in both directions
"""
import logging
from collections import deque

from data.db import get_connection

logger = logging.getLogger(__name__)

_MAX_PATH_DEPTH  = 5   # max hops in join path search
_MAX_PATHS       = 8   # max distinct paths returned
_LOW_CONF_THRESH = 0.7  # confidence below this triggers ambiguity warning


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


def _load_edges(conn, source_id: int, snap_id: int) -> list[dict]:
    """Trusted edges only — relationship_status IN ('AUTO', 'APPROVED')."""
    rows = conn.execute(
        """SELECT from_table_fqn, to_table_fqn,
                  from_column, to_column, relationship_name, confidence
           FROM table_relationships
           WHERE source_id = ? AND snapshot_id = ?
             AND relationship_status IN ('AUTO', 'APPROVED')""",
        (source_id, snap_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_join_adj(edges: list[dict]) -> dict[str, list[dict]]:
    """
    Build a bidirectional join adjacency list.
    adj[table] = list of neighbour join-descriptors, one per FK edge.
    direction='references'    — this table has the FK (child → parent)
    direction='referenced_by' — this table is the FK target (parent ← child)
    """
    adj: dict[str, list[dict]] = {}
    for e in edges:
        f, t = e["from_table_fqn"], e["to_table_fqn"]
        adj.setdefault(f, []).append({
            "table_fqn":      t,
            "left_column":    e["from_column"],
            "right_column":   e["to_column"],
            "fk_name":        e["relationship_name"],
            "confidence":     float(e["confidence"]),
            "direction":      "references",
        })
        adj.setdefault(t, []).append({
            "table_fqn":      f,
            "left_column":    e["to_column"],
            "right_column":   e["from_column"],
            "fk_name":        e["relationship_name"],
            "confidence":     float(e["confidence"]),
            "direction":      "referenced_by",
        })
    return adj


def _enrich_pair(conn, source_id: int, fqn_a: str, fqn_b: str, prof_snap_id: int | None) -> tuple[dict, dict]:
    """Return (enrichment_a, enrichment_b) with dict/domain/entity/profiling data."""
    def _fetch(fqn: str) -> dict:
        result: dict = {"table_fqn": fqn}
        d = conn.execute(
            "SELECT business_name, is_approved FROM data_dictionary_tables "
            "WHERE source_id = ? AND table_fqn = ?", (source_id, fqn),
        ).fetchone()
        if d:
            result["business_name"] = d["business_name"]
            result["dict_approved"] = bool(d["is_approved"])
        da = conn.execute(
            "SELECT domain FROM domain_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, fqn),
        ).fetchone()
        if da:
            result["domain"] = da["domain"]
        ea = conn.execute(
            "SELECT entity FROM entity_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, fqn),
        ).fetchone()
        if ea:
            result["entity"] = ea["entity"]
        if prof_snap_id:
            p = conn.execute(
                "SELECT table_class, classification_confidence, fk_count, "
                "referenced_by_count, pii_column_count, is_junction_table "
                "FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ? AND source_id = ? AND table_fqn = ?",
                (prof_snap_id, source_id, fqn),
            ).fetchone()
            if p:
                result.update({
                    "table_class":               p["table_class"],
                    "classification_confidence": p["classification_confidence"],
                    "fk_count":                  p["fk_count"] or 0,
                    "referenced_by_count":       p["referenced_by_count"] or 0,
                    "pii_column_count":          p["pii_column_count"] or 0,
                    "is_junction_table":         bool(p["is_junction_table"]),
                })
        return result
    return _fetch(fqn_a), _fetch(fqn_b)


def _join_confidence(edge: dict, enrich_a: dict, enrich_b: dict) -> float:
    """Adjust raw FK confidence with business-metadata quality signals."""
    score = edge["confidence"]
    if enrich_a.get("dict_approved") and enrich_b.get("dict_approved"):
        score = min(1.0, score + 0.05)
    if enrich_a.get("domain") and enrich_b.get("domain"):
        score = min(1.0, score + 0.03)
    return round(score, 4)


def _business_explanation(
    edge: dict,
    enrich_a: dict,
    enrich_b: dict,
    fk_direction: str,
) -> str:
    """
    Synthesise a one-sentence business explanation for a join.
    fk_direction = 'a_references_b' or 'b_references_a'
    """
    name_a = enrich_a.get("business_name") or edge["from_table_fqn"].split(".")[-1]
    name_b = enrich_b.get("business_name") or edge["to_table_fqn"].split(".")[-1]

    if fk_direction == "a_references_b":
        child, parent      = name_a, name_b
        child_col, par_col = edge["from_column"], edge["to_column"]
        entity_b           = enrich_b.get("entity")
    else:
        child, parent      = name_b, name_a
        child_col, par_col = edge["from_column"], edge["to_column"]
        entity_b           = enrich_a.get("entity")

    base = f"Join {child} to {parent} on {child_col} = {par_col}"
    if entity_b and entity_b not in (None, "Unknown"):
        return f"{base} to access {entity_b} context for each {child} record."
    return f"{base}."


def _recommended_join_direction(edge: dict, enrich_from: dict, enrich_to: dict) -> str:
    """
    Determine the recommended starting table for the join query.
    Prefer the more granular (Fact/Transactional) table as the left side.
    """
    cls_from = enrich_from.get("table_class") or ""
    cls_to   = enrich_to.get("table_class")   or ""
    if cls_from == "Transactional":
        return edge["from_table_fqn"]
    if cls_to == "Transactional":
        return edge["to_table_fqn"]
    # Default: the child (referencing) table is the starting point
    return edge["from_table_fqn"]


def _find_all_join_paths(
    join_adj: dict,
    start: str,
    end: str,
    max_depth: int = _MAX_PATH_DEPTH,
    max_paths: int = _MAX_PATHS,
) -> list[dict]:
    """
    BFS over the bidirectional join adjacency to find all distinct join paths
    from start to end up to max_depth hops.
    Each path carries cumulative confidence (product of edge confidences).
    Cycles are prevented via the per-path visited set.
    """
    results: list[dict] = []
    # queue: (current, path_nodes, path_edges, cumulative_confidence)
    queue: deque = deque([(start, [start], [], 1.0)])

    while queue and len(results) < max_paths:
        current, node_path, edge_path, cum_conf = queue.popleft()

        if len(node_path) > max_depth + 1:
            continue

        for edge in join_adj.get(current, []):
            nbr = edge["table_fqn"]
            if nbr in node_path:      # avoid revisiting within this path
                continue
            new_conf     = round(cum_conf * edge["confidence"], 4)
            new_nodes    = node_path + [nbr]
            new_edges    = edge_path + [edge]
            if nbr == end:
                results.append({
                    "path":       new_nodes,
                    "joins":      new_edges,
                    "hops":       len(new_edges),
                    "confidence": new_conf,
                })
            else:
                queue.append((nbr, new_nodes, new_edges, new_conf))

    return results


def _classify_semantic_role(profile: dict | None, fk_adj_degree: int, ref_adj_degree: int) -> tuple[str, float]:
    """
    Classify a table into a semantic role using profiling signals and FK degree.
    Returns (role, confidence).
    """
    if profile is None:
        if fk_adj_degree > 0 and ref_adj_degree > 0:
            return "Hub", 0.35
        if fk_adj_degree > 0:
            return "Fact", 0.30
        if ref_adj_degree > 0:
            return "Dimension", 0.30
        return "Unknown", 0.10

    cls   = profile.get("table_class") or ""
    fkc   = profile.get("fk_count")          or 0
    rbc   = profile.get("referenced_by_count") or 0
    junc  = bool(profile.get("is_junction_table"))
    cc    = float(profile.get("classification_confidence") or 0.5)

    if junc:
        return "Bridge", max(0.80, cc)
    if cls == "Reference":
        return "Lookup", max(0.75, cc)
    if cls == "Master" or (rbc >= 2 and fkc == 0):
        return "Dimension", max(0.75, cc)
    if cls == "Transactional" or fkc >= 1:
        return "Fact", max(0.60, cc)
    if fkc + rbc >= 4:
        return "Hub", 0.50
    return "Unknown", 0.20


def _infer_business_processes(domain: str | None, entity: str | None) -> list[str]:
    """Derive related business process labels from stored domain and entity metadata."""
    processes: list[str] = []
    if domain and domain != "Unknown":
        processes.append(f"{domain} operations")
    if entity and entity != "Unknown":
        processes.append(f"{entity} management")
    if domain and entity and domain not in (None, "Unknown") and entity not in (None, "Unknown"):
        processes.append(f"{domain} {entity.lower()} processing")
    return processes if processes else ["General data management"]


def _importance_score(profile: dict | None, dict_approved: bool = False) -> float:
    """Derive business importance (0–1) — mirrors knowledge_graph_service logic."""
    if profile is None:
        return 0.1
    score = 0.0
    rbc = profile.get("referenced_by_count") or 0
    if rbc > 0:
        score += min(0.30, 0.06 * rbc)
    cls = profile.get("table_class") or ""
    if cls == "Master":
        score += 0.20
    elif cls == "Reference":
        score += 0.12
    elif cls == "Transactional":
        score += 0.08
    if (profile.get("pii_column_count") or 0) > 0:
        score += 0.10
    if dict_approved:
        score += 0.10
    return round(min(1.0, score), 3)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def discover_business_joins(
    source_id: int,
    user_id: str,
    table_a: str,
    table_b: str,
) -> dict | None:
    """
    Discover all direct FK join options between two tables.

    For each FK edge found, returns:
      join_columns    — the matching column pair
      confidence      — boosted by dictionary/domain approval signals
      fk_direction    — which table holds the FK
      join_type       — recommended SQL join type (INNER / LEFT)
      recommended_starting_table — more granular table (for FROM clause)
      business_explanation — human-readable sentence derived from metadata

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        if not snap_id:
            return _no_join_result(source_id, table_a, table_b, "No schema snapshot available.")

        edges        = _load_edges(conn, source_id, snap_id)
        enrich_a, enrich_b = _enrich_pair(conn, source_id, table_a, table_b, prof_snap_id)
    finally:
        conn.close()

    direct_joins: list[dict] = []
    for e in edges:
        fk_dir = None
        if e["from_table_fqn"] == table_a and e["to_table_fqn"] == table_b:
            fk_dir = "a_references_b"
        elif e["from_table_fqn"] == table_b and e["to_table_fqn"] == table_a:
            fk_dir = "b_references_a"
        if fk_dir is None:
            continue

        conf = _join_confidence(e, enrich_a, enrich_b)
        rec_start = _recommended_join_direction(e, enrich_a, enrich_b)

        direct_joins.append({
            "join_columns": {
                "left_table":   e["from_table_fqn"],
                "left_column":  e["from_column"],
                "right_table":  e["to_table_fqn"],
                "right_column": e["to_column"],
            },
            "fk_direction":            fk_dir,
            "fk_name":                 e["relationship_name"],
            "confidence":              conf,
            "join_type":               "INNER",
            "recommended_starting_table": rec_start,
            "business_explanation":    _business_explanation(e, enrich_a, enrich_b, fk_dir),
        })

    # Sort by confidence desc
    direct_joins.sort(key=lambda x: -x["confidence"])

    return {
        "source_id":        source_id,
        "table_a":          table_a,
        "table_b":          table_b,
        "has_direct_join":  len(direct_joins) > 0,
        "join_count":       len(direct_joins),
        "recommended_join": direct_joins[0] if direct_joins else None,
        "alternative_joins": direct_joins[1:],
        "table_a_metadata": enrich_a,
        "table_b_metadata": enrich_b,
        "message": (
            None if direct_joins
            else f"No direct FK relationship found between {table_a} and {table_b}. "
                 f"Use /semantic/join-paths to find an indirect route."
        ),
    }


def _no_join_result(source_id: int, ta: str, tb: str, msg: str) -> dict:
    return {
        "source_id": source_id, "table_a": ta, "table_b": tb,
        "has_direct_join": False, "join_count": 0,
        "recommended_join": None, "alternative_joins": [],
        "table_a_metadata": {}, "table_b_metadata": {},
        "message": msg,
    }


def discover_join_paths(
    source_id: int,
    user_id: str,
    start_table: str,
    target_table: str,
    max_depth: int = _MAX_PATH_DEPTH,
) -> dict | None:
    """
    Find all valid join paths between two tables through the FK graph.

    Returns:
      all_paths          — every distinct route up to max_depth hops
      shortest_path      — fewest hops
      highest_confidence — product of edge confidences maximised
      recommended_path   — shortest among the high-confidence paths

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id = _latest_schema_snap_id(conn, source_id)
        if not snap_id:
            return _no_path_result(source_id, start_table, target_table, "No schema snapshot.")

        edges    = _load_edges(conn, source_id, snap_id)
        join_adj = _build_join_adj(edges)
    finally:
        conn.close()

    # Same-table edge case
    if start_table == target_table:
        trivial = {"path": [start_table], "joins": [], "hops": 0, "confidence": 1.0}
        return {
            "source_id": source_id, "start_table": start_table,
            "target_table": target_table,
            "all_paths": [trivial],
            "shortest_path": trivial, "highest_confidence_path": trivial,
            "recommended_path": trivial, "total_paths_found": 1,
            "message": "Source and target are the same table.",
        }

    all_paths = _find_all_join_paths(join_adj, start_table, target_table, max_depth)

    if not all_paths:
        return _no_path_result(
            source_id, start_table, target_table,
            f"No join path found within {max_depth} hops. "
            "The tables may be in disconnected graph components.",
        )

    shortest     = min(all_paths, key=lambda p: p["hops"])
    highest_conf = max(all_paths, key=lambda p: p["confidence"])
    # Recommended = shortest among paths with confidence >= 0.8, else just shortest
    high_conf_paths = [p for p in all_paths if p["confidence"] >= 0.80]
    recommended = min(high_conf_paths, key=lambda p: p["hops"]) if high_conf_paths else shortest

    return {
        "source_id":               source_id,
        "start_table":             start_table,
        "target_table":            target_table,
        "all_paths":               all_paths,
        "shortest_path":           shortest,
        "highest_confidence_path": highest_conf,
        "recommended_path":        recommended,
        "total_paths_found":       len(all_paths),
        "message":                 f"{len(all_paths)} join path(s) found.",
    }


def _no_path_result(source_id, start, target, msg):
    return {
        "source_id": source_id, "start_table": start, "target_table": target,
        "all_paths": [], "shortest_path": None, "highest_confidence_path": None,
        "recommended_path": None, "total_paths_found": 0, "message": msg,
    }


def detect_join_ambiguity(
    source_id: int,
    user_id: str,
    table_a: str,
    table_b: str | None = None,
) -> dict | None:
    """
    Detect join ambiguity for a table or a pair of tables.

    Ambiguity types detected:
      MULTIPLE_DIRECT_FKS   — more than one FK between the same two tables
      MULTIPLE_JOIN_PATHS   — more than one route through intermediate tables
      CIRCULAR_JOIN         — join path cycles back to the starting table
      LOW_CONFIDENCE_JOIN   — any FK edge has confidence below threshold
      MISSING_JOIN          — same-domain/entity tables with no FK path

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
                "source_id": source_id, "table_a": table_a, "table_b": table_b,
                "ambiguities": [], "total_ambiguities": 0,
                "max_severity": None, "recommendations": [],
                "message": "No schema snapshot available.",
            }

        edges    = _load_edges(conn, source_id, snap_id)
        join_adj = _build_join_adj(edges)

        # Domain peers of table_a (for MISSING_JOIN detection)
        domain_row = conn.execute(
            "SELECT domain FROM domain_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_a),
        ).fetchone()
        domain_peers: list[str] = []
        if domain_row and domain_row["domain"] not in (None, "Unknown"):
            peer_rows = conn.execute(
                "SELECT table_fqn FROM domain_assignments "
                "WHERE source_id = ? AND domain = ? AND table_fqn != ? LIMIT 10",
                (source_id, domain_row["domain"], table_a),
            ).fetchall()
            domain_peers = [r["table_fqn"] for r in peer_rows]
    finally:
        conn.close()

    ambiguities: list[dict] = []

    if table_b:
        # ── Pair-mode: analyse ambiguity between table_a and table_b ───────
        direct = [
            e for e in edges
            if (e["from_table_fqn"] == table_a and e["to_table_fqn"] == table_b) or
               (e["from_table_fqn"] == table_b and e["to_table_fqn"] == table_a)
        ]

        if len(direct) > 1:
            ambiguities.append({
                "type":        "MULTIPLE_DIRECT_FKS",
                "severity":    "MEDIUM",
                "description": (
                    f"{len(direct)} direct FK relationships exist between "
                    f"{table_a} and {table_b}."
                ),
                "edges": [{"fk_name": e["relationship_name"],
                            "from": e["from_table_fqn"], "from_col": e["from_column"],
                            "to":   e["to_table_fqn"],   "to_col":   e["to_column"]}
                           for e in direct],
                "recommendation": "Qualify the join explicitly by column name to avoid ambiguity.",
            })

        all_paths = _find_all_join_paths(join_adj, table_a, table_b, _MAX_PATH_DEPTH)
        if len(all_paths) > 1:
            ambiguities.append({
                "type":        "MULTIPLE_JOIN_PATHS",
                "severity":    "HIGH",
                "description": (
                    f"{len(all_paths)} distinct join paths exist between "
                    f"{table_a} and {table_b}."
                ),
                "path_count": len(all_paths),
                "paths":       [{"hops": p["hops"], "path": p["path"],
                                  "confidence": p["confidence"]} for p in all_paths],
                "recommendation": "Choose the path with the highest confidence or the fewest hops.",
            })

        if not all_paths:
            ambiguities.append({
                "type":        "MISSING_JOIN",
                "severity":    "LOW",
                "description": (
                    f"No FK path found between {table_a} and {table_b} "
                    f"within {_MAX_PATH_DEPTH} hops."
                ),
                "recommendation": "Verify these tables should be joined, or add a FK relationship.",
            })

        low_conf = [e for e in direct if float(e["confidence"]) < _LOW_CONF_THRESH]
        if low_conf:
            ambiguities.append({
                "type":        "LOW_CONFIDENCE_JOIN",
                "severity":    "LOW",
                "description": (
                    f"FK(s) with confidence below {_LOW_CONF_THRESH:.0%}: "
                    + ", ".join(e["relationship_name"] or "unnamed" for e in low_conf)
                ),
                "recommendation": "Review and validate these low-confidence relationships.",
            })

    else:
        # ── Single-table mode: scan all joins involving table_a ─────────────
        involved = [
            e for e in edges
            if e["from_table_fqn"] == table_a or e["to_table_fqn"] == table_a
        ]

        low_conf = [e for e in involved if float(e["confidence"]) < _LOW_CONF_THRESH]
        if low_conf:
            ambiguities.append({
                "type":        "LOW_CONFIDENCE_JOIN",
                "severity":    "LOW",
                "description": (
                    f"{len(low_conf)} FK edge(s) involving {table_a} "
                    f"have confidence below {_LOW_CONF_THRESH:.0%}."
                ),
                "recommendation": "Review and validate these low-confidence relationships.",
            })

        # Circular join detection: BFS to see if table_a can reach itself
        visited: set[str] = set()
        queue: deque = deque([(table_a, 0)])
        found_cycle = False
        while queue and not found_cycle:
            node, depth = queue.popleft()
            if depth >= _MAX_PATH_DEPTH:
                continue
            for edge in join_adj.get(node, []):
                nbr = edge["table_fqn"]
                if nbr == table_a and depth > 0:
                    found_cycle = True
                    break
                if nbr not in visited:
                    visited.add(nbr)
                    queue.append((nbr, depth + 1))
        if found_cycle:
            ambiguities.append({
                "type":        "CIRCULAR_JOIN",
                "severity":    "HIGH",
                "description": f"A circular FK path leads back to {table_a}.",
                "recommendation": "Circular FK references may cause infinite loops in recursive queries.",
            })

        # Missing join: domain peers with no FK path
        missing_peers: list[str] = []
        for peer in domain_peers[:5]:
            paths = _find_all_join_paths(join_adj, table_a, peer, max_depth=3, max_paths=1)
            if not paths:
                missing_peers.append(peer)
        if missing_peers:
            ambiguities.append({
                "type":        "MISSING_DOMAIN_JOINS",
                "severity":    "LOW",
                "description": (
                    f"{table_a} shares a domain with {len(missing_peers)} table(s) "
                    "but has no FK path to them within 3 hops."
                ),
                "tables": missing_peers,
                "recommendation": "Confirm whether these tables should be join-able.",
            })

    _SEV_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    max_sev = (
        min(ambiguities, key=lambda x: _SEV_RANK.get(x["severity"], 9))["severity"]
        if ambiguities else None
    )

    return {
        "source_id":        source_id,
        "table_a":          table_a,
        "table_b":          table_b,
        "ambiguities":      ambiguities,
        "total_ambiguities": len(ambiguities),
        "max_severity":     max_sev,
        "is_clean":         len(ambiguities) == 0,
    }


def semantic_table_profile(
    source_id: int,
    user_id: str,
    table_fqn: str,
) -> dict | None:
    """
    Return the full semantic profile of a table:
      semantic_role        — Fact / Dimension / Lookup / Bridge / Hub / Unknown
      business_importance  — score + label derived from structural signals
      typical_joins        — outbound FK targets (what this table joins to)
      typical_consumers    — inbound FK sources (what joins to this table)
      related_processes    — business process labels from domain + entity metadata
      trusted              — True when dict approved + domain + entity all present
      governance_score     — 0–1 fraction of governance dimensions satisfied

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        # Dictionary
        dict_row = conn.execute(
            "SELECT business_name, description, grain, is_approved, generation_method "
            "FROM data_dictionary_tables WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()

        # Domain + entity
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

        # Profiling
        profile = None
        if prof_snap_id:
            p = conn.execute(
                """SELECT table_name, schema_name, table_class, classification_confidence,
                          fk_count, referenced_by_count, pii_column_count, confirmed_pii_count,
                          is_junction_table, is_root_table, is_leaf_table, row_count_tier,
                          estimated_row_count, exact_row_count, profiling_status
                   FROM profiling_table_profiles
                   WHERE profiling_snapshot_id = ? AND source_id = ? AND table_fqn = ?""",
                (prof_snap_id, source_id, table_fqn),
            ).fetchone()
            if p:
                profile = dict(p)

        # FK edges (typical joins and consumers)
        typical_joins:     list[dict] = []
        typical_consumers: list[dict] = []
        if snap_id:
            out_rows = conn.execute(
                "SELECT to_table_fqn, from_column, to_column, relationship_name, confidence "
                "FROM table_relationships "
                "WHERE source_id = ? AND snapshot_id = ? AND from_table_fqn = ? "
                "AND relationship_status IN ('AUTO', 'APPROVED') "
                "ORDER BY confidence DESC LIMIT 10",
                (source_id, snap_id, table_fqn),
            ).fetchall()
            typical_joins = [dict(r) for r in out_rows]

            in_rows = conn.execute(
                "SELECT from_table_fqn, from_column, to_column, relationship_name, confidence "
                "FROM table_relationships "
                "WHERE source_id = ? AND snapshot_id = ? AND to_table_fqn = ? "
                "AND relationship_status IN ('AUTO', 'APPROVED') "
                "ORDER BY confidence DESC LIMIT 10",
                (source_id, snap_id, table_fqn),
            ).fetchall()
            typical_consumers = [dict(r) for r in in_rows]
    finally:
        conn.close()

    # Semantic role from profiling + FK degree
    fk_degree  = len(typical_joins)
    ref_degree = len(typical_consumers)
    role, role_conf = _classify_semantic_role(profile, fk_degree, ref_degree)

    # Business importance
    dict_approved = bool(dict_row and dict_row["is_approved"])
    imp_score = _importance_score(profile, dict_approved)
    if imp_score >= 0.7:
        imp_label = "CRITICAL"
    elif imp_score >= 0.4:
        imp_label = "HIGH"
    elif imp_score >= 0.2:
        imp_label = "MEDIUM"
    else:
        imp_label = "LOW"

    # Derived values
    domain = domain_row["domain"] if domain_row else None
    entity = entity_row["entity"] if entity_row else None
    domain_ok = bool(domain and domain != "Unknown")
    entity_ok = bool(entity and entity != "Unknown")
    trusted   = dict_approved and domain_ok and entity_ok

    # Governance score (0–1)
    gov_keys = ["dictionary_approved", "domain_assigned", "entity_assigned", "profiling_complete"]
    governance = {
        "dictionary_approved": dict_approved,
        "domain_assigned":     domain_ok,
        "entity_assigned":     entity_ok,
        "profiling_complete":  bool(profile and profile.get("profiling_status") == "COMPLETE"),
    }
    gov_score = round(sum(1 for v in governance.values() if v) / len(gov_keys), 2)

    return {
        "source_id":              source_id,
        "table_fqn":              table_fqn,
        "table_name":             (profile["table_name"] if profile else table_fqn.split(".")[-1]),
        "schema_name":            (profile["schema_name"] if profile else (table_fqn.split(".")[0] if "." in table_fqn else None)),
        "semantic_role":          role,
        "semantic_role_confidence": round(role_conf, 3),
        "business_importance": {
            "score": imp_score,
            "label": imp_label,
        },
        "business_name":          (dict_row["business_name"] if dict_row else None),
        "description":            (dict_row["description"]   if dict_row else None),
        "grain":                  (dict_row["grain"]         if dict_row else None),
        "business_domain":        domain,
        "business_entity":        entity,
        "profiling": {
            "table_class":          (profile["table_class"] if profile else None),
            "row_count_tier":       (profile["row_count_tier"] if profile else None),
            "fk_count":             fk_degree,
            "referenced_by_count":  ref_degree,
            "pii_column_count":     (profile["pii_column_count"] if profile else 0),
        },
        "typical_joins":           typical_joins,
        "typical_consumers":       typical_consumers,
        "related_business_processes": _infer_business_processes(domain, entity),
        "trusted":                 trusted,
        "governance":              governance,
        "governance_score":        gov_score,
    }


def semantic_summary(source_id: int, user_id: str) -> dict | None:
    """
    Return source-level semantic intelligence:
      role distribution   — count of Fact/Dimension/Lookup/Bridge/Hub tables
      sample per role     — top 5 table names per category
      relationship_density — avg FK edges per table
      semantic_coverage   — % of profiled tables with a known role
      avg_join_confidence  — average FK edge confidence
      business_completeness — % with approved dict + domain + entity

    Returns None when source not found or not owned by user_id.
    """
    conn = get_connection()
    try:
        source_row = _verify_source(conn, source_id, user_id)
        if not source_row:
            return None

        snap_id      = _latest_schema_snap_id(conn, source_id)
        prof_snap_id = _latest_profiling_snap_id(conn, source_id)

        # Aggregate profiling class counts
        class_rows = conn.execute(
            """SELECT table_class,
                      COUNT(*) AS cnt,
                      AVG(classification_confidence) AS avg_conf
               FROM profiling_table_profiles
               WHERE profiling_snapshot_id = ? AND source_id = ?
               GROUP BY table_class""",
            (prof_snap_id, source_id),
        ).fetchall() if prof_snap_id else []

        junction_count = conn.execute(
            "SELECT COUNT(*) FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ? AND source_id = ? AND is_junction_table = 1",
            (prof_snap_id, source_id),
        ).fetchone()[0] if prof_snap_id else 0

        total_profiled = conn.execute(
            "SELECT COUNT(*) FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ? AND source_id = ?",
            (prof_snap_id, source_id),
        ).fetchone()[0] if prof_snap_id else 0

        # FK edge stats
        rel_stats = conn.execute(
            """SELECT COUNT(*) AS total_rels,
                      AVG(confidence) AS avg_conf,
                      COUNT(DISTINCT from_table_fqn) AS tables_with_fks
               FROM table_relationships
               WHERE source_id = ? AND snapshot_id = ?
                 AND relationship_status IN ('AUTO', 'APPROVED')""",
            (source_id, snap_id),
        ).fetchone() if snap_id else None

        # Business completeness
        biz_complete = conn.execute(
            """SELECT COUNT(*) FROM data_dictionary_tables ddt
               JOIN domain_assignments da ON da.source_id = ddt.source_id
                    AND da.table_fqn = ddt.table_fqn AND da.domain != 'Unknown'
               JOIN entity_assignments ea ON ea.source_id = ddt.source_id
                    AND ea.table_fqn = ddt.table_fqn AND ea.entity != 'Unknown'
               WHERE ddt.source_id = ? AND ddt.is_approved = 1""",
            (source_id,),
        ).fetchone()[0]

        # Sample tables per role (top 5 per class, ordered by referenced_by_count)
        def _sample(class_val: str, limit: int = 5) -> list[str]:
            rows = conn.execute(
                "SELECT table_fqn FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ? AND source_id = ? AND table_class = ? "
                "ORDER BY referenced_by_count DESC, table_fqn LIMIT ?",
                (prof_snap_id, source_id, class_val, limit),
            ).fetchall() if prof_snap_id else []
            return [r["table_fqn"] for r in rows]

        def _sample_junc(limit: int = 5) -> list[str]:
            rows = conn.execute(
                "SELECT table_fqn FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ? AND source_id = ? AND is_junction_table = 1 "
                "LIMIT ?",
                (prof_snap_id, source_id, limit),
            ).fetchall() if prof_snap_id else []
            return [r["table_fqn"] for r in rows]

        schema_meta = conn.execute(
            "SELECT table_count FROM schema_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone() if snap_id else None

        total_tables = (schema_meta["table_count"] or 0) if schema_meta else 0

        # Collect samples while connection is still open
        sample_fact      = _sample("Transactional")
        sample_dimension = _sample("Master")
        sample_lookup    = _sample("Reference")
        sample_bridge    = _sample_junc()
        sample_audit     = _sample("Audit")
    finally:
        conn.close()

    class_map: dict[str, int] = {r["table_class"]: r["cnt"] for r in class_rows if r["table_class"]}
    fact_count      = class_map.get("Transactional", 0)
    dimension_count = class_map.get("Master", 0)
    lookup_count    = class_map.get("Reference", 0)
    bridge_count    = junction_count
    audit_count     = class_map.get("Audit", 0)

    total_rels = (rel_stats["total_rels"] or 0) if rel_stats else 0
    avg_conf   = round(float(rel_stats["avg_conf"] or 0), 3) if rel_stats else 0.0
    tbls_w_fks = (rel_stats["tables_with_fks"] or 0) if rel_stats else 0

    classified_count = fact_count + dimension_count + lookup_count + bridge_count + audit_count
    semantic_coverage = round(classified_count / total_profiled, 3) if total_profiled else 0.0
    biz_completeness  = round(biz_complete / total_tables, 3) if total_tables else 0.0
    rel_density       = round(total_rels / total_tables, 3) if total_tables else 0.0

    return {
        "source_id": source_id,
        "source": {
            "display_name": source_row["display_name"],
            "source_type":  source_row["source_type"],
        },
        "semantic_roles": {
            "fact_tables": {
                "count": fact_count,
                "sample": sample_fact,
            },
            "dimension_tables": {
                "count": dimension_count,
                "sample": sample_dimension,
            },
            "lookup_tables": {
                "count": lookup_count,
                "sample": sample_lookup,
            },
            "bridge_tables": {
                "count": bridge_count,
                "sample": sample_bridge,
            },
            "audit_tables": {
                "count": audit_count,
                "sample": sample_audit,
            },
            "unclassified_count": total_profiled - classified_count,
        },
        "metrics": {
            "total_schema_tables":   total_tables,
            "total_profiled_tables": total_profiled,
            "total_relationships":   total_rels,
            "tables_with_fks":       tbls_w_fks,
            "relationship_density":  rel_density,
            "average_join_confidence": avg_conf,
            "semantic_coverage":     semantic_coverage,
            "business_completeness": biz_completeness,
        },
    }
