import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from core.domains.rules import detect_table_domain
from data.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — mirrors domain_quality_service to identify the same flagged rules
# ---------------------------------------------------------------------------

_FLAG_TABLES_HIGH_COVERAGE  = 100
_FLAG_DOMAIN_SPREAD         = 3
_FLAG_CONFIDENCE_LOW        = 0.60
_FLAG_SHARE_OF_ASSIGNED     = 0.25

# Minimum tables in a sub-group to generate a refinement candidate
_MIN_REFINEMENT_SUPPORT = 3

# Minimum fraction of a sub-group that must agree on a domain
_MIN_REFINEMENT_CONFIDENCE = 0.60

# Evidence regex — matches strings written by apply_learned_rules()
_LEARNED_SIG = re.compile(r"learned rule \[([A-Z]+)\] '([^']+)'")

_DOMAIN_UNKNOWN = "Unknown"

_UPSERT_SUGGESTION = """
    INSERT INTO domain_rule_refinement_suggestions (
        source_id, parent_rule_id, pattern_type, pattern_value,
        suggested_domain, support_count, confidence,
        approval_status, created_at, active
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 0)
    ON CONFLICT(parent_rule_id, pattern_type, pattern_value) DO NOTHING
"""

_PROMOTE_INSERT = """
    INSERT INTO domain_learning_rules (
        source_id, pattern_type, pattern_value, domain, confidence,
        approval_status, created_by, approved_by, created_at, approved_at, active
    ) VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, 1)
    ON CONFLICT(source_id, pattern_type, pattern_value) DO NOTHING
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(name: str) -> list[str]:
    name = re.sub(r"[\.\-\s]+", "_", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [t.lower() for t in name.split("_") if t]


def _get_discriminator(
    table_name: str,
    rule_ptype: str,
    rule_pvalue: str,
) -> str | None:
    """Return the sub-token that discriminates within a broad rule's match set.

    For PREFIX / TOKEN rules the discriminator is the token that immediately
    follows the one that triggered the rule.  For SCHEMA rules it is the first
    token of the table name (the schema-level match gives no positional info).
    """
    toks = _tokenize(table_name)
    if not toks:
        return None

    if rule_ptype == "SCHEMA":
        return toks[0]

    # PREFIX or TOKEN — find the matching token and return the next one
    val = rule_pvalue.lower()
    for i, tok in enumerate(toks):
        if tok == val or tok.startswith(val):
            return toks[i + 1] if i + 1 < len(toks) else None
    return None


def _is_flagged(
    tables_matched: int,
    unique_generic_domains: int,
    confidence_avg: float,
    total_assigned: int,
) -> bool:
    if tables_matched > _FLAG_TABLES_HIGH_COVERAGE:
        return True
    if unique_generic_domains > _FLAG_DOMAIN_SPREAD:
        return True
    if confidence_avg < _FLAG_CONFIDENCE_LOW and tables_matched > 0:
        return True
    if total_assigned > 0 and tables_matched / total_assigned > _FLAG_SHARE_OF_ASSIGNED:
        return True
    return False


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def analyze_rule_refinement(source_id: int, user_id: str) -> dict | None:
    """Suggest specific sub-rules that would improve accuracy of broad learned rules.

    Read-only — makes no writes to the database.

    Returns:
        Refinement analysis dict, or None if source not owned by user_id.

    Keys returned:
        source_id, flagged_rules, candidate_refinements,
        projected_accuracy_improvement.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rule_rows = conn.execute(
            "SELECT * FROM domain_learning_rules "
            "WHERE source_id = ? AND approval_status = 'APPROVED' AND active = 1 "
            "ORDER BY id",
            (source_id,),
        ).fetchall()

        assignment_rows = conn.execute(
            "SELECT table_fqn, domain, confidence, evidence_json "
            "FROM domain_assignments WHERE source_id = ?",
            (source_id,),
        ).fetchall()

        snap_row = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()

        profile_map: dict[str, dict] = {}
        if snap_row:
            for r in conn.execute(
                "SELECT table_fqn, table_name, schema_name, table_class, "
                "pii_column_count, confirmed_pii_count, fk_count, referenced_by_count "
                "FROM profiling_table_profiles WHERE profiling_snapshot_id = ?",
                (snap_row["id"],),
            ).fetchall():
                profile_map[r["table_fqn"]] = dict(r)

        # Existing approved rules — used to skip duplicate suggestions
        existing_approved: set[tuple[str, str]] = {
            (r["pattern_type"], r["pattern_value"])
            for r in rule_rows
        }
    finally:
        conn.close()

    # ------------------------------------------------------------------
    # Map each assignment back to its originating learned rule
    # ------------------------------------------------------------------

    rule_match_map: dict[tuple[str, str], list[dict]] = {}
    total_assigned = 0

    for row in assignment_rows:
        d = dict(row)
        if d["domain"] != _DOMAIN_UNKNOWN:
            total_assigned += 1
        try:
            evidence = json.loads(d["evidence_json"])
        except (json.JSONDecodeError, TypeError):
            evidence = []
        for ev_str in evidence:
            m = _LEARNED_SIG.search(ev_str)
            if m:
                key = (m.group(1), m.group(2))
                rule_match_map.setdefault(key, []).append(d)
                break

    # ------------------------------------------------------------------
    # Identify flagged rules (same criteria as domain_quality_service)
    # ------------------------------------------------------------------

    flagged: list[dict] = []
    for rule_row in rule_rows:
        r = dict(rule_row)
        key = (r["pattern_type"], r["pattern_value"])
        matches = rule_match_map.get(key, [])
        tables_matched = len(matches)

        # Quick generic-domain spread to assess flag criteria
        generic_domains: Counter = Counter()
        for m in matches:
            profile = profile_map.get(m["table_fqn"])
            if profile:
                generic_domains[detect_table_domain(profile).domain] += 1

        confidence_avg = (
            round(sum(m["confidence"] for m in matches) / tables_matched, 3)
            if tables_matched > 0 else 0.0
        )

        if _is_flagged(
            tables_matched,
            len(generic_domains),
            confidence_avg,
            total_assigned,
        ):
            flagged.append({
                "rule_id":       r["id"],
                "pattern_type":  r["pattern_type"],
                "pattern_value": r["pattern_value"],
                "rule_domain":   r["domain"],
                "matches":       matches,
            })

    if not flagged:
        return {
            "source_id":                    source_id,
            "flagged_rules":                0,
            "candidate_refinements":        [],
            "projected_accuracy_improvement": 0.0,
        }

    # ------------------------------------------------------------------
    # Generate sub-rule candidates for each flagged rule
    # ------------------------------------------------------------------

    all_candidates: list[dict] = []
    total_flagged_tables = 0
    total_refineable_tables = 0

    for flag in flagged:
        total_flagged_tables += len(flag["matches"])

        # Group matched tables by their sub-pattern discriminator token
        sub_groups: dict[str, list[tuple[dict, dict]]] = {}  # disc → [(assignment, profile)]
        for m in flag["matches"]:
            profile = profile_map.get(m["table_fqn"])
            if not profile:
                continue
            disc = _get_discriminator(
                profile["table_name"],
                flag["pattern_type"],
                flag["pattern_value"],
            )
            if disc:
                sub_groups.setdefault(disc, []).append((m, profile))

        # Evaluate each sub-group
        for disc, items in sub_groups.items():
            if len(items) < _MIN_REFINEMENT_SUPPORT:
                continue

            # Generic engine domain distribution for this sub-group
            generic_dist: Counter = Counter()
            for _, profile in items:
                ga = detect_table_domain(profile)
                generic_dist[ga.domain] += 1

            if not generic_dist:
                continue

            top_domain, top_count = generic_dist.most_common(1)[0]
            conf = top_count / len(items)

            # Only suggest when:
            # 1. Consensus is strong enough
            # 2. Suggested domain is not Unknown (not actionable)
            # 3. Suggested domain differs from what the broad rule assigns
            #    (same domain = broad rule is already correct for this sub-group)
            # 4. Not a duplicate of an already-approved rule
            if conf < _MIN_REFINEMENT_CONFIDENCE:
                continue
            if top_domain == _DOMAIN_UNKNOWN:
                continue
            if top_domain == flag["rule_domain"]:
                continue
            if ("TOKEN", disc) in existing_approved:
                continue

            total_refineable_tables += len(items)

            all_candidates.append({
                "parent_rule_id":   flag["rule_id"],
                "parent_pattern":   f"{flag['pattern_type']} \"{flag['pattern_value']}\"",
                "parent_domain":    flag["rule_domain"],
                "pattern_type":     "TOKEN",
                "pattern_value":    disc,
                "suggested_domain": top_domain,
                "support_count":    len(items),
                "confidence":       round(conf, 3),
                "domain_spread":    dict(generic_dist.most_common()),
                "example_tables":   [item[0]["table_fqn"] for item in items[:5]],
            })

    # Deduplicate: if the same TOKEN suggestion appears under multiple parent rules,
    # keep the entry with the highest support_count.
    seen_tokens: dict[str, dict] = {}
    for cand in all_candidates:
        key = cand["pattern_value"]
        if key not in seen_tokens or cand["support_count"] > seen_tokens[key]["support_count"]:
            seen_tokens[key] = cand

    deduped = sorted(seen_tokens.values(), key=lambda c: -c["support_count"])

    projected_improvement = (
        round(total_refineable_tables / total_flagged_tables * 100, 1)
        if total_flagged_tables > 0 else 0.0
    )

    # Persist candidates as PENDING — idempotent via ON CONFLICT DO NOTHING
    now = _now()
    new_count = skipped_count = 0
    conn = get_connection()
    try:
        for cand in deduped:
            cursor = conn.execute(
                _UPSERT_SUGGESTION,
                (
                    source_id,
                    cand["parent_rule_id"],
                    cand["pattern_type"],
                    cand["pattern_value"],
                    cand["suggested_domain"],
                    cand["support_count"],
                    cand["confidence"],
                    now,
                ),
            )
            if cursor.rowcount > 0:
                new_count += 1
            else:
                skipped_count += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "source_id":                      source_id,
        "flagged_rules":                  len(flagged),
        "total_flagged_tables":           total_flagged_tables,
        "total_refineable_tables":        total_refineable_tables,
        "candidate_refinements":          deduped,
        "projected_accuracy_improvement": projected_improvement,
        "suggestions_new":                new_count,
        "suggestions_skipped":            skipped_count,
    }


# ---------------------------------------------------------------------------
# Persistence service functions
# ---------------------------------------------------------------------------

def list_refinement_suggestions(source_id: int, user_id: str) -> list[dict] | None:
    """Return all refinement suggestions for source_id ordered by confidence desc.

    Returns None if source not owned by user_id.
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
            "SELECT * FROM domain_rule_refinement_suggestions "
            "WHERE source_id = ? "
            "ORDER BY approval_status, confidence DESC, support_count DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def approve_refinement_suggestion(suggestion_id: int, user_id: str) -> dict | None:
    """Approve a PENDING refinement suggestion — sets approval_status=APPROVED, active=1.

    Returns:
        Updated row dict, or None if not found or wrong user.

    Raises:
        ValueError: Suggestion is not in PENDING state.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM domain_rule_refinement_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["approval_status"] != "PENDING":
            raise ValueError(
                f"Suggestion {suggestion_id} is already '{d['approval_status']}' "
                "and cannot be approved again."
            )

        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (d["source_id"], user_id),
        ).fetchone()
        if owns is None:
            return None

        conn.execute(
            """UPDATE domain_rule_refinement_suggestions
                  SET approval_status = 'APPROVED',
                      active          = 1,
                      approved_by     = ?,
                      approved_at     = ?
                WHERE id = ?""",
            (user_id, now, suggestion_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM domain_rule_refinement_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        _confidence = float(dict(updated).get("confidence") or 0.0)
        log_governance_event(
            object_type_id = GovernedObjectType.DOMAIN_REFINEMENT,
            object_id      = str(suggestion_id),
            event_type     = "APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "domain_refinement_service",
        )
        upsert_governance_state(
            object_type_id   = GovernedObjectType.DOMAIN_REFINEMENT,
            object_id        = str(suggestion_id),
            approval_state   = GovernanceState.HUMAN_APPROVED,
            confidence_score = _confidence,
            reviewer_id      = user_id,
            reviewed_at      = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for domain.refinement id=%s", suggestion_id
        )

    return dict(updated)


def reject_refinement_suggestion(suggestion_id: int, user_id: str) -> dict | None:
    """Reject a PENDING refinement suggestion — sets approval_status=REJECTED, active=0.

    Returns:
        Updated row dict, or None if not found or wrong user.

    Raises:
        ValueError: Suggestion is not in PENDING state.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM domain_rule_refinement_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["approval_status"] != "PENDING":
            raise ValueError(
                f"Suggestion {suggestion_id} is already '{d['approval_status']}' "
                "and cannot be rejected again."
            )

        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (d["source_id"], user_id),
        ).fetchone()
        if owns is None:
            return None

        conn.execute(
            """UPDATE domain_rule_refinement_suggestions
                  SET approval_status = 'REJECTED',
                      active          = 0,
                      approved_by     = ?,
                      approved_at     = ?
                WHERE id = ?""",
            (user_id, now, suggestion_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM domain_rule_refinement_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        log_governance_event(
            object_type_id = GovernedObjectType.DOMAIN_REFINEMENT,
            object_id      = str(suggestion_id),
            event_type     = "REJECTED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.REJECTED,
            actor_id       = user_id,
            source_service = "domain_refinement_service",
        )
        upsert_governance_state(
            object_type_id = GovernedObjectType.DOMAIN_REFINEMENT,
            object_id      = str(suggestion_id),
            approval_state = GovernanceState.REJECTED,
            reviewer_id    = user_id,
            reviewed_at    = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for domain.refinement id=%s", suggestion_id
        )

    return dict(updated)


def promote_approved_refinements(source_id: int, user_id: str) -> dict | None:
    """Promote approved refinement suggestions into active learned rules.

    Each APPROVED+active refinement suggestion is inserted into
    domain_learning_rules as an APPROVED active rule.  Uses
    ON CONFLICT(source_id, pattern_type, pattern_value) DO NOTHING so
    re-running is idempotent — already-promoted rules are silently skipped.

    Does NOT trigger domain regeneration.  Caller decides when to re-run
    generate_domain_assignments() to apply the new rules.

    Returns:
        Metrics dict, or None if source not owned by user_id.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        refinements = conn.execute(
            "SELECT * FROM domain_rule_refinement_suggestions "
            "WHERE source_id = ? AND approval_status = 'APPROVED' AND active = 1",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    approved_count = len(refinements)
    now = _now()
    promoted = 0
    skipped = 0

    conn = get_connection()
    try:
        for row in refinements:
            r = dict(row)
            cursor = conn.execute(
                _PROMOTE_INSERT,
                (
                    source_id,
                    r["pattern_type"],
                    r["pattern_value"],
                    r["suggested_domain"],
                    r["confidence"],
                    user_id,
                    r.get("approved_by") or user_id,
                    now,
                    r.get("approved_at") or now,
                ),
            )
            if cursor.rowcount > 0:
                promoted += 1
            else:
                skipped += 1
        conn.commit()

        active_rules = conn.execute(
            "SELECT COUNT(*) FROM domain_learning_rules "
            "WHERE source_id = ? AND approval_status = 'APPROVED' AND active = 1",
            (source_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "source_id":            source_id,
        "approved_refinements": approved_count,
        "promoted_count":       promoted,
        "skipped_existing":     skipped,
        "active_learned_rules": active_rules,
    }
