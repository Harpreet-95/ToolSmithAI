import json
import logging
from datetime import datetime, timezone

from core.domains.learning import LearnedDomainRule, apply_learned_rules
from core.domains.rules import detect_table_domain
from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_UPSERT = """
    INSERT INTO domain_assignments (
        source_id, profiling_snapshot_id, table_fqn, domain,
        confidence, evidence_json, competing_domains_json,
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        profiling_snapshot_id  = excluded.profiling_snapshot_id,
        domain                 = excluded.domain,
        confidence             = excluded.confidence,
        evidence_json          = excluded.evidence_json,
        competing_domains_json = excluded.competing_domains_json,
        updated_at             = excluded.updated_at
"""


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def generate_domain_assignments(source_id: int, user_id: str) -> dict | None:
    """Classify every profiled table for source_id and persist the results.

    Uses the latest profiling snapshot.  Column semantic types from
    profiling_column_profiles are passed to detect_table_domain() as
    additional signal.

    Returns:
        Summary dict, or None if source_id does not belong to user_id.

    Raises:
        ValueError: No profiling snapshot exists for this source.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        snap_row = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snap_row is None:
            raise ValueError(
                "No profiling snapshot found. "
                "Run POST /sources/{id}/profile/structural first."
            )

        snap_id = snap_row["id"]

        table_rows = conn.execute(
            "SELECT * FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ?",
            (snap_id,),
        ).fetchall()

        # Semantic types per table: table_fqn → [semantic_type, ...]
        sem_rows = conn.execute(
            "SELECT table_fqn, semantic_type FROM profiling_column_profiles "
            "WHERE profiling_snapshot_id = ? AND semantic_type IS NOT NULL",
            (snap_id,),
        ).fetchall()
    finally:
        conn.close()

    sem_map: dict[str, list[str]] = {}
    for row in sem_rows:
        sem_map.setdefault(row["table_fqn"], []).append(row["semantic_type"])

    # Load APPROVED active learned rules for this source.
    # Scoped to source_id — rules from other sources are never loaded.
    conn = get_connection()
    try:
        rule_rows = conn.execute(
            "SELECT * FROM domain_learning_rules "
            "WHERE source_id = ? AND approval_status = 'APPROVED' AND active = 1",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    learned_rules: list[LearnedDomainRule] = [
        LearnedDomainRule(
            id=r["id"],
            source_id=r["source_id"],
            pattern_type=r["pattern_type"],
            pattern_value=r["pattern_value"],
            domain=r["domain"],
            confidence=float(r["confidence"]),
            approval_status=r["approval_status"],
            created_by=r["created_by"],
            approved_by=r["approved_by"],
            created_at=r["created_at"],
            approved_at=r["approved_at"],
            active=bool(r["active"]),
        )
        for r in rule_rows
    ]

    now = _now()
    assignments = []
    learned_matches = 0
    generic_matches = 0

    for row in table_rows:
        profile   = dict(row)
        sem_types = sem_map.get(profile["table_fqn"])

        # Learned rules take priority over the generic engine.
        a = apply_learned_rules(profile, learned_rules)
        if a is not None:
            learned_matches += 1
        else:
            a = detect_table_domain(profile, sem_types)
            if a.domain != "Unknown":
                generic_matches += 1

        assignments.append(a)

    conn = get_connection()
    try:
        for a in assignments:
            competing = [
                {"domain": c.domain, "score": c.score, "evidence": c.evidence}
                for c in a.competing_domains
            ]
            conn.execute(
                _UPSERT,
                (
                    source_id,
                    snap_id,
                    a.table_fqn,
                    a.domain,
                    a.confidence,
                    json.dumps(a.evidence),
                    json.dumps(competing),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    domain_counts: dict[str, int] = {}
    for a in assignments:
        domain_counts[a.domain] = domain_counts.get(a.domain, 0) + 1

    tables_total   = len(assignments)
    tables_unknown = domain_counts.get("Unknown", 0)

    return {
        "source_id":             source_id,
        "profiling_snapshot_id": snap_id,
        "tables_total":          tables_total,
        "tables_assigned":       tables_total - tables_unknown,
        "tables_unknown":        tables_unknown,
        "domain_counts":         domain_counts,
        "learned_rule_matches":  learned_matches,
        "generic_rule_matches":  generic_matches,
        "unknown_assignments":   tables_unknown,
        "generated_at":          now,
    }


def list_domain_assignments(source_id: int, user_id: str) -> list[dict] | None:
    """Return all domain assignments for source_id, ordered by domain then confidence.

    Returns None if source_id does not belong to user_id.
    evidence_json and competing_domains_json are parsed into native Python objects.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM domain_assignments "
            "WHERE source_id = ? "
            "ORDER BY domain, confidence DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        d["evidence"]          = json.loads(d.pop("evidence_json", "[]"))
        d["competing_domains"] = json.loads(d.pop("competing_domains_json", "[]"))
        result.append(d)
    return result


def get_domain_summary(source_id: int, user_id: str) -> dict | None:
    """Return aggregate domain counts for source_id.

    Returns None if source_id does not belong to user_id.
    Returns a zeroed summary (no error) if no assignments have been generated yet.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        count_rows = conn.execute(
            "SELECT domain, COUNT(*) AS cnt "
            "FROM domain_assignments "
            "WHERE source_id = ? "
            "GROUP BY domain ORDER BY cnt DESC",
            (source_id,),
        ).fetchall()

        meta = conn.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS last_generated_at "
            "FROM domain_assignments WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    tables_total = meta["total"] if meta else 0
    if tables_total == 0:
        return {
            "source_id":         source_id,
            "tables_total":      0,
            "tables_assigned":   0,
            "tables_unknown":    0,
            "domain_counts":     {},
            "last_generated_at": None,
        }

    domain_counts  = {r["domain"]: r["cnt"] for r in count_rows}
    tables_unknown = domain_counts.get("Unknown", 0)

    return {
        "source_id":         source_id,
        "tables_total":      tables_total,
        "tables_assigned":   tables_total - tables_unknown,
        "tables_unknown":    tables_unknown,
        "domain_counts":     domain_counts,
        "last_generated_at": meta["last_generated_at"],
    }
