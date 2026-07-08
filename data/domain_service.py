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
        assignment_source, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'rule', ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        profiling_snapshot_id  = excluded.profiling_snapshot_id,
        domain                 = excluded.domain,
        confidence             = excluded.confidence,
        evidence_json          = excluded.evidence_json,
        competing_domains_json = excluded.competing_domains_json,
        updated_at             = excluded.updated_at
    WHERE domain_assignments.assignment_source != 'human'
"""


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def generate_domain_assignments(
    source_id: int, user_id: str, table_fqns: list[str] | None = None
) -> dict | None:
    """Classify every profiled table for source_id and persist the results.

    Uses the latest profiling snapshot.  Column semantic types from
    profiling_column_profiles are passed to detect_table_domain() as
    additional signal.

    table_fqns: when provided, only these tables are classified/upserted (used
    by the autonomous metadata lifecycle to refresh changed/new objects only).
    None (default) preserves the original full-source behavior used by the
    manual "Generate Domains" action. Rows with assignment_source='human' are
    never overwritten regardless of this parameter (see _UPSERT guard).

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

        if table_fqns is not None:
            if not table_fqns:
                table_rows = []
                col_rows = []
            else:
                placeholders = ",".join("?" for _ in table_fqns)
                table_rows = conn.execute(
                    "SELECT * FROM profiling_table_profiles "
                    f"WHERE profiling_snapshot_id = ? AND table_fqn IN ({placeholders})",
                    (snap_id, *table_fqns),
                ).fetchall()
                col_rows = conn.execute(
                    "SELECT table_fqn, semantic_type, cardinality_tier, uniqueness_score, "
                    "null_percentage, blank_percentage, quality_score, quality_grade, "
                    "distribution_shape, pii_signals_json, dominant_pattern, pattern_coverage, "
                    "semantic_confidence "
                    f"FROM profiling_column_profiles "
                    f"WHERE profiling_snapshot_id = ? AND table_fqn IN ({placeholders})",
                    (snap_id, *table_fqns),
                ).fetchall()
        else:
            table_rows = conn.execute(
                "SELECT * FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ?",
                (snap_id,),
            ).fetchall()

            # Full column profiles per table: used for deep profiling domain intelligence.
            # Fetches all columns (not just those with semantic_type) to use cardinality,
            # quality, distribution, and PII signals.
            col_rows = conn.execute(
                "SELECT table_fqn, semantic_type, cardinality_tier, uniqueness_score, "
                "null_percentage, blank_percentage, quality_score, quality_grade, "
                "distribution_shape, pii_signals_json, dominant_pattern, pattern_coverage, "
                "semantic_confidence "
                "FROM profiling_column_profiles "
                "WHERE profiling_snapshot_id = ?",
                (snap_id,),
            ).fetchall()
    finally:
        conn.close()

    sem_map: dict[str, list[str]] = {}
    col_profiles_map: dict[str, list[dict]] = {}
    for row in col_rows:
        row_dict = dict(row)
        fqn = row_dict["table_fqn"]
        col_profiles_map.setdefault(fqn, []).append(row_dict)
        sem = row_dict.get("semantic_type")
        if sem is not None:
            sem_map.setdefault(fqn, []).append(sem)

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
            a = detect_table_domain(
                profile,
                sem_types,
                column_profiles=col_profiles_map.get(profile["table_fqn"]),
            )
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


def lock_domain_assignment(
    source_id: int, user_id: str, table_fqn: str, domain: str | None = None
) -> dict | None:
    """Mark a domain assignment as human-set so it is never overwritten by
    generate_domain_assignments() again.

    If domain is provided, the value is corrected at the same time. Otherwise
    the existing domain value is left as-is and only assignment_source flips
    to 'human'.

    Returns the updated row, or None if source_id does not belong to user_id
    or no assignment row exists yet for table_fqn.
    """
    now = _now()
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        if domain is not None:
            cursor = conn.execute(
                "UPDATE domain_assignments "
                "SET domain = ?, assignment_source = 'human', updated_at = ? "
                "WHERE source_id = ? AND table_fqn = ?",
                (domain, now, source_id, table_fqn),
            )
        else:
            cursor = conn.execute(
                "UPDATE domain_assignments "
                "SET assignment_source = 'human', updated_at = ? "
                "WHERE source_id = ? AND table_fqn = ?",
                (now, source_id, table_fqn),
            )
        conn.commit()

        if cursor.rowcount == 0:
            return None

        row = conn.execute(
            "SELECT * FROM domain_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType, log_governance_event,
        )
        log_governance_event(
            object_type_id = GovernedObjectType.DOMAIN_ASSIGNMENT,
            object_id      = f"{source_id}:{table_fqn}",
            event_type     = "HUMAN_LOCK",
            from_state     = GovernanceState.GENERATED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "domain_service",
        )
    except Exception:
        logger.warning("governance logging failed for domain.assignment %s:%s", source_id, table_fqn)

    d = dict(row)
    d["evidence"]          = json.loads(d.pop("evidence_json", "[]") or "[]")
    d["competing_domains"] = json.loads(d.pop("competing_domains_json", "[]") or "[]")
    return d


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
