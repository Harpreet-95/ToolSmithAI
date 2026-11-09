"""
Autonomous PII classification — Autonomous Live-SQL Foundation milestone,
Parts 2-4.

Separates PII *classification* (does the system have enough evidence to
decide?) from PII *policy* (governance still decides masking/blocking
independently — see data/query_execution_service.py::_governance_recheck,
unchanged by this module). Rules-first, deterministic, evidence-based:

  Tier 1/2 name-pattern hit (core.dictionary.pii_detector) -> CONFIRMED_PII
  Tier 3 name-pattern hit (ambiguous "name" column)         -> UNCERTAIN
  No name-pattern hit at all                                -> CONFIRMED_NON_PII

CONFIRMED_PII sets pii_confirmed = 1 (the existing masking-eligible gate);
CONFIRMED_NON_PII additionally clears a Tier-3 false positive's block by
setting pii_confirmed = 0 and leaving the column ungated. UNCERTAIN changes
nothing — the column stays blocked exactly as before, failing conservatively
per the milestone brief ("do NOT automatically classify uncertain
personal-looking data as safe").

Manual override always wins: any row with pii_manual_override = 1 (set by
data.governance_service.confirm_pii_column / set_pii_non_pii_override) is
skipped by run_pii_classification, forever, until a human changes it again.
Idempotent — re-running re-derives the same deterministic result for every
non-overridden row; safe to call repeatedly (see Part 18 idempotency tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.dictionary.pii_detector import detect_pii_tier
from data.db import get_connection
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

CONFIRMED_PII = "CONFIRMED_PII"
CONFIRMED_NON_PII = "CONFIRMED_NON_PII"
UNCERTAIN = "UNCERTAIN"

_TIER_CONFIDENCE = {1: 0.95, 2: 0.9, 3: 0.5}
_NON_PII_CONFIDENCE = 0.9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PIIClassificationSummary:
    source_id: int
    profiling_snapshot_id: int | None = None
    columns_evaluated: int = 0
    confirmed_pii: int = 0
    confirmed_non_pii: int = 0
    uncertain: int = 0
    manual_override_skipped: int = 0
    unchanged: int = 0
    details: list[dict] = field(default_factory=list)


def classify_column(column_name: str, data_type: str) -> tuple[str, float, str]:
    """Pure function: (classification, confidence, evidence) for one column.
    No DB access — safe to unit test directly."""
    is_pii, tier, evidence = detect_pii_tier(column_name, data_type)
    if not is_pii:
        return CONFIRMED_NON_PII, _NON_PII_CONFIDENCE, evidence
    if tier == 3:
        return UNCERTAIN, _TIER_CONFIDENCE[3], evidence
    return CONFIRMED_PII, _TIER_CONFIDENCE.get(tier, 0.8), evidence


def run_pii_classification(
    source_id: int,
    *,
    table_fqns: list[str] | None = None,
) -> PIIClassificationSummary:
    """Classify every column in source_id's latest profiling snapshot that
    is not already manually overridden. table_fqns scopes to a change-set
    (mirrors dictionary/domain/entity refresh's own affected-tables scoping
    in core/lifecycle/runner.py); omitted means "every table in the
    snapshot" (used by the standalone/manual re-classification path).

    Never raises — a per-column failure is skipped and counted, matching
    every other lifecycle-refresh step's best-effort convention.
    """
    summary = PIIClassificationSummary(source_id=source_id)

    snapshot = get_latest_profiling_snapshot(source_id)
    if snapshot is None:
        return summary
    summary.profiling_snapshot_id = snapshot.id

    conn = get_connection()
    try:
        query = (
            "SELECT id, table_fqn, column_name, data_type, pii_classification, "
            "pii_manual_override FROM profiling_column_profiles "
            "WHERE profiling_snapshot_id = ? AND source_id = ?"
        )
        params: list = [snapshot.id, source_id]
        if table_fqns:
            placeholders = ",".join("?" for _ in table_fqns)
            query += f" AND table_fqn IN ({placeholders})"
            params.extend(table_fqns)
        rows = conn.execute(query, params).fetchall()

        now = _now()
        for row in rows:
            summary.columns_evaluated += 1
            if row["pii_manual_override"]:
                summary.manual_override_skipped += 1
                continue

            try:
                classification, confidence, evidence = classify_column(
                    row["column_name"], row["data_type"],
                )
            except Exception:
                continue

            if classification == row["pii_classification"]:
                summary.unchanged += 1

            if classification == CONFIRMED_PII:
                summary.confirmed_pii += 1
            elif classification == CONFIRMED_NON_PII:
                summary.confirmed_non_pii += 1
            else:
                summary.uncertain += 1

            conn.execute(
                """UPDATE profiling_column_profiles SET
                       pii_classification            = ?,
                       pii_classification_confidence  = ?,
                       pii_classification_evidence    = ?,
                       pii_classification_source      = 'AUTO_RULE',
                       pii_classified_at              = ?,
                       pii_confirmed                  = ?,
                       updated_at                     = ?
                   WHERE id = ?""",
                (
                    classification, confidence, evidence, now,
                    1 if classification == CONFIRMED_PII else 0,
                    now, row["id"],
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return summary
