import json
import logging
import re
from collections import Counter

from core.domains.rules import detect_table_domain
from data.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_THRESHOLD_TABLES_HIGH_COVERAGE  = 100    # absolute table count per rule
_THRESHOLD_DOMAIN_SPREAD         = 3      # distinct generic domains among matched tables
_THRESHOLD_CONFIDENCE_LOW        = 0.60   # confidence_avg below this is suspicious
_THRESHOLD_SHARE_OF_ASSIGNED     = 0.25   # rule covers > 25 % of all assigned tables

# Regex that matches the evidence string written by apply_learned_rules():
#   "learned rule [PREFIX] 'adf' → Operations"
_LEARNED_SIG = re.compile(r"learned rule \[([A-Z]+)\] '([^']+)'")


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def analyze_domain_quality(source_id: int, user_id: str) -> dict | None:
    """Identify potentially incorrect assignments caused by broad learned rules.

    Read-only — makes no writes to the database.

    Returns:
        Analysis dict, or None if source_id not owned by user_id.

    Keys returned:
        source_id, total_rules, total_assigned, healthy_rules,
        review_required_rules, rules_analysis, recommendations.
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
    finally:
        conn.close()

    # ------------------------------------------------------------------
    # Map each assignment back to the learned rule that produced it.
    # Evidence string: "learned rule [PREFIX] 'adf' → Operations"
    # ------------------------------------------------------------------

    rule_match_map: dict[tuple[str, str], list[dict]] = {}
    total_assigned = 0

    for row in assignment_rows:
        d = dict(row)
        if d["domain"] != "Unknown":
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
                break  # one assignment, one originating rule

    # ------------------------------------------------------------------
    # Compute per-rule metrics
    # ------------------------------------------------------------------

    rules_analysis: list[dict] = []

    for rule_row in rule_rows:
        r = dict(rule_row)
        key = (r["pattern_type"], r["pattern_value"])
        matches = rule_match_map.get(key, [])
        tables_matched = len(matches)

        confidence_avg = (
            round(sum(m["confidence"] for m in matches) / tables_matched, 3)
            if tables_matched > 0 else 0.0
        )

        # What would the generic engine assign to these same tables?
        # Running detect_table_domain on each matched table's profile reveals
        # whether the rule is collapsing naturally distinct domains into one.
        generic_domains: Counter = Counter()
        for m in matches:
            profile = profile_map.get(m["table_fqn"])
            if profile:
                ga = detect_table_domain(profile)
                generic_domains[ga.domain] += 1

        rules_analysis.append({
            "rule_id":               r["id"],
            "pattern_type":          r["pattern_type"],
            "pattern_value":         r["pattern_value"],
            "rule_domain":           r["domain"],
            "tables_matched":        tables_matched,
            "confidence_avg":        confidence_avg,
            "generic_domain_spread": dict(
                sorted(generic_domains.items(), key=lambda x: -x[1])
            ),
            "unique_generic_domains": len(generic_domains),
        })

    rules_analysis.sort(key=lambda ra: -ra["tables_matched"])

    # ------------------------------------------------------------------
    # Flag suspicious rules and build recommendations
    # ------------------------------------------------------------------

    recommendations: list[dict] = []
    review_ids: set[int] = set()

    for ra in rules_analysis:
        flags: list[str] = []

        if ra["tables_matched"] > _THRESHOLD_TABLES_HIGH_COVERAGE:
            flags.append(
                f"matches {ra['tables_matched']} tables "
                f"(threshold: {_THRESHOLD_TABLES_HIGH_COVERAGE})"
            )

        if ra["unique_generic_domains"] > _THRESHOLD_DOMAIN_SPREAD:
            flags.append(
                f"generic engine distributes matched tables across "
                f"{ra['unique_generic_domains']} domains "
                f"(threshold: {_THRESHOLD_DOMAIN_SPREAD})"
            )

        if ra["confidence_avg"] < _THRESHOLD_CONFIDENCE_LOW and ra["tables_matched"] > 0:
            flags.append(
                f"confidence_avg={ra['confidence_avg']} "
                f"(threshold: {_THRESHOLD_CONFIDENCE_LOW})"
            )

        if total_assigned > 0:
            share = ra["tables_matched"] / total_assigned
            if share > _THRESHOLD_SHARE_OF_ASSIGNED:
                pct = round(share * 100, 1)
                flags.append(
                    f"captures {pct}% of all assigned tables "
                    f"(threshold: {int(_THRESHOLD_SHARE_OF_ASSIGNED * 100)}%)"
                )

        if not flags:
            continue

        review_ids.add(ra["rule_id"])

        # Recommendation text based on dominant flag
        spread = ra["unique_generic_domains"]
        if spread > _THRESHOLD_DOMAIN_SPREAD:
            top3 = list(ra["generic_domain_spread"].items())[:3]
            domain_list = ", ".join(f"{d} ({n})" for d, n in top3)
            rec = (
                f"Rule may be too broad. Generic engine distributes matched tables "
                f"across {spread} domains: {domain_list}"
                f"{', ...' if spread > 3 else ''}. "
                f"Consider splitting into more specific patterns."
            )
        elif ra["tables_matched"] > _THRESHOLD_TABLES_HIGH_COVERAGE:
            pct = round(ra["tables_matched"] / total_assigned * 100, 1) if total_assigned else 0
            rec = (
                f"High-coverage rule ({ra['tables_matched']} tables, {pct}% of assigned). "
                f"Verify all matched tables belong to '{ra['rule_domain']}'."
            )
        else:
            rec = "Review this rule for accuracy."

        recommendations.append({
            "rule_id":               ra["rule_id"],
            "pattern_type":          ra["pattern_type"],
            "pattern_value":         ra["pattern_value"],
            "rule_domain":           ra["rule_domain"],
            "tables_matched":        ra["tables_matched"],
            "confidence_avg":        ra["confidence_avg"],
            "generic_domain_spread": ra["generic_domain_spread"],
            "flags":                 flags,
            "recommendation":        rec,
        })

    healthy_count = len(rules_analysis) - len(review_ids)

    return {
        "source_id":             source_id,
        "total_rules":           len(rules_analysis),
        "total_assigned":        total_assigned,
        "healthy_rules":         healthy_count,
        "review_required_rules": len(review_ids),
        "rules_analysis":        rules_analysis,
        "recommendations":       recommendations,
    }
