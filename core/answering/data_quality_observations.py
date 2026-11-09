"""
NEXT MILESTONE, Part 12/13/14 — Source Data Quality Observation model.

A lightweight, deterministic structure for distinguishing "ToolSmithAI got
the question right, but the underlying data itself is incomplete/
duplicated/orphaned" (SOURCE DATA QUALITY) from "ToolSmithAI got it wrong"
(SYSTEM defect) — see this module's own DataQualityObservation.issue_type
values, which mirror the milestone's own vocabulary.

NOT a new AI agent, NOT a full-database audit (Part 16: "Do NOT perform a
huge database-wide audit"). detect_data_quality_observations() only ever
inspects the columns a caller explicitly names as business-significant for
THE CURRENT QUESTION (business_columns) — every metric it reads
(null_percentage, uniqueness_score, populated_count) is ALREADY-COMPUTED
profiling evidence (core.profiling.quality/statistics), never a new live
query. This is what keeps Part 13's rule ("a nullable optional field is not
automatically bad data") true by construction: a column never scanned here
in the first place can never produce a false-positive warning — only a
column the caller says IS actually driving the answer (a label, a join/
identity key, a filter/dimension value) is ever inspected.

Evidence-based only (Part 21: never blame the source without proof) — every
observation's `evidence` field states the literal profiling number that
triggered it, so a caller/reader can verify the claim against the same
metadata this module read, never a subjective judgment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

IssueType = Literal[
    "NULL_BUSINESS_LABEL",
    "DUPLICATE_BUSINESS_KEY",
    "ORPHAN_FOREIGN_KEY",
    "HIGH_NULL_RATE",
]
Severity = Literal["LOW", "MEDIUM", "HIGH"]
ColumnRole = Literal["label", "key", "dimension", "filter", "measure"]


@dataclass(frozen=True)
class DataQualityObservation:
    issue_type: IssueType
    severity: Severity
    table_fqn: str
    column_name: str | None
    business_concept: str | None
    evidence: str
    affected_rows: int | None
    message: str


@dataclass(frozen=True)
class BusinessColumnRef:
    """One column the caller has determined is actually driving the current
    answer — the only kind of column detect_data_quality_observations() will
    ever inspect. `role` decides which checks apply (see that function's own
    docstring): a "label"/"dimension" column is checked for NULL_BUSINESS_
    LABEL, a "key" column for DUPLICATE_BUSINESS_KEY, and every role gets the
    generic (much higher-bar) HIGH_NULL_RATE check."""
    table_fqn: str
    column_name: str
    role: ColumnRole
    business_concept: str | None = None


# Thresholds are deliberately conservative (Part 13: "a nullable optional
# field is not automatically bad data") — chosen to flag only genuinely
# material gaps in a column already known to matter for this answer, not
# every column with any nulls at all.
_NULL_LABEL_THRESHOLD = 5.0      # % — a display label/dimension is expected to be ~always populated
_NULL_GENERIC_THRESHOLD = 30.0   # % — any business-significant column, higher bar
_DUPLICATE_KEY_THRESHOLD = 0.999  # uniqueness_score below this on a "key" role is material


def _column_profile(table_contexts: dict[str, dict], table_fqn: str, column_name: str) -> dict | None:
    ctx = table_contexts.get(table_fqn)
    if not ctx:
        return None
    for col in ctx.get("columns") or []:
        if col.get("column_name") == column_name:
            return col
    return None


def detect_data_quality_observations(
    table_contexts: dict[str, dict],
    business_columns: list[BusinessColumnRef],
) -> list[DataQualityObservation]:
    """Bounded, deterministic pass over ONLY the columns named in
    `business_columns`. Never raises, never queries a live database — reads
    exclusively from the already-fetched profiling/dictionary metadata
    already present in `table_contexts` (the same
    {table_fqn: {"columns": [{"column_name", "dictionary", "profiling"}]}}
    shape data.query_planning_service already builds via
    get_table_business_context)."""
    observations: list[DataQualityObservation] = []

    for ref in business_columns:
        col = _column_profile(table_contexts, ref.table_fqn, ref.column_name)
        if col is None:
            continue
        profiling = col.get("profiling") or {}
        null_pct = profiling.get("null_percentage")
        populated_count = profiling.get("populated_count")
        uniqueness = profiling.get("uniqueness_score")

        if ref.role in ("label", "dimension") and null_pct is not None and null_pct > _NULL_LABEL_THRESHOLD:
            affected = round(populated_count * null_pct / 100.0) if populated_count else None
            observations.append(DataQualityObservation(
                issue_type="NULL_BUSINESS_LABEL",
                severity="HIGH" if null_pct > 25.0 else "MEDIUM",
                table_fqn=ref.table_fqn, column_name=ref.column_name,
                business_concept=ref.business_concept,
                evidence=f"{ref.table_fqn}.{ref.column_name} null_percentage={null_pct:.1f}% (profiled)",
                affected_rows=affected,
                message=(
                    f"{ref.business_concept or ref.column_name} is missing for "
                    f"{null_pct:.1f}% of rows in the source data — some results may show "
                    "an unavailable label rather than a fabricated one."
                ),
            ))
        elif null_pct is not None and null_pct > _NULL_GENERIC_THRESHOLD:
            affected = round(populated_count * null_pct / 100.0) if populated_count else None
            observations.append(DataQualityObservation(
                issue_type="HIGH_NULL_RATE",
                severity="MEDIUM",
                table_fqn=ref.table_fqn, column_name=ref.column_name,
                business_concept=ref.business_concept,
                evidence=f"{ref.table_fqn}.{ref.column_name} null_percentage={null_pct:.1f}% (profiled)",
                affected_rows=affected,
                message=(
                    f"{ref.business_concept or ref.column_name} is missing for "
                    f"{null_pct:.1f}% of rows in the source data."
                ),
            ))

        if ref.role == "key" and uniqueness is not None and uniqueness < _DUPLICATE_KEY_THRESHOLD:
            affected = (
                round(populated_count * (1.0 - uniqueness)) if populated_count is not None else None
            )
            observations.append(DataQualityObservation(
                issue_type="DUPLICATE_BUSINESS_KEY",
                severity="HIGH" if uniqueness < 0.95 else "MEDIUM",
                table_fqn=ref.table_fqn, column_name=ref.column_name,
                business_concept=ref.business_concept,
                evidence=f"{ref.table_fqn}.{ref.column_name} uniqueness_score={uniqueness:.4f} (profiled)",
                affected_rows=affected,
                message=(
                    f"{ref.business_concept or ref.column_name} contains duplicate values "
                    f"in the source data (uniqueness {uniqueness:.1%}) — counts against this "
                    "key may not correspond 1:1 with distinct business entities."
                ),
            ))

    return observations


def business_columns_from_plan(business_plan: dict) -> list[BusinessColumnRef]:
    """NEXT MILESTONE, Part 9 — adapts core.orchestrator.context_builder.
    _build_business_plan's already-built select/where/group_by rows (the
    SAME compact bridge structure result_formatter/citation_builder already
    read from) into the bounded business_columns list
    detect_data_quality_observations() accepts. Invents no new
    classification: a column already grouped by (group_by) is the display
    label driving this answer's grain (role="label"); a plain COUNT's own
    counted key (role="key") is checked for duplication; every other
    selected column is a plain business value (role="measure", generic
    null-rate check only); a WHERE column is a filter value (role="filter",
    same generic check)."""
    group_by_columns = {(row.get("table_fqn"), row.get("column_name")) for row in (business_plan.get("group_by") or [])}
    dimension_labels = business_plan.get("dimension_labels") or {}
    refs: list[BusinessColumnRef] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(table_fqn: str | None, column_name: str | None, role: ColumnRole, concept: str | None) -> None:
        if not table_fqn or not column_name:
            return
        key = (table_fqn, column_name, role)
        if key in seen:
            return
        seen.add(key)
        refs.append(BusinessColumnRef(table_fqn=table_fqn, column_name=column_name, role=role, business_concept=concept))

    for row in business_plan.get("select") or []:
        table_fqn, column_name = row.get("table_fqn"), row.get("column_name")
        if (table_fqn, column_name) in group_by_columns:
            _add(table_fqn, column_name, "label", dimension_labels.get(column_name))
        elif row.get("aggregation") == "COUNT":
            _add(table_fqn, column_name, "key", None)
        else:
            _add(table_fqn, column_name, "measure", None)

    for row in business_plan.get("where") or []:
        _add(row.get("table_fqn"), row.get("column_name"), "filter", None)

    return refs


def orphan_foreign_key_observation(
    *, table_fqn: str, column_name: str, related_table_fqn: str,
    unmatched_count: int, total_count: int, business_concept: str | None = None,
) -> DataQualityObservation | None:
    """Builds ONE ORPHAN_FOREIGN_KEY observation from an already-computed,
    bounded FK-coverage check (Part 10's live relationship verification —
    unmatched key count vs total, the same read-only evidence that check
    already gathers). This function performs no query itself; it only
    shapes an already-computed result into the observation contract. A zero
    unmatched_count is not an issue (returns None) — a key with perfect
    coverage is a strength, not something to report."""
    if total_count <= 0 or unmatched_count <= 0:
        return None
    pct = round((unmatched_count / total_count) * 100.0, 2)
    return DataQualityObservation(
        issue_type="ORPHAN_FOREIGN_KEY",
        severity="HIGH" if pct > 10.0 else ("MEDIUM" if pct > 1.0 else "LOW"),
        table_fqn=table_fqn, column_name=column_name,
        business_concept=business_concept,
        evidence=(
            f"{table_fqn}.{column_name} -> {related_table_fqn}: {unmatched_count}/{total_count} "
            f"rows ({pct:.2f}%) have no matching row on the related table (bounded read-only check)"
        ),
        affected_rows=unmatched_count,
        message=(
            f"{pct:.2f}% of {table_fqn} rows reference a {related_table_fqn} value that "
            "does not exist in the source data."
        ),
    )


# ---------------------------------------------------------------------------
# Part 20 — Executive vs technical presentation.
#
# An executive-facing reader must never see a raw evidence string (table/
# column identifiers, percentages to 4 decimal places) — only the plain-
# language `message`. A developer/admin view gets the full observation
# (including `evidence`) for actual debugging. This is a pure, stateless
# split of an already-built observation list — it invents no new data.
# ---------------------------------------------------------------------------

def executive_data_notes(observations: list[DataQualityObservation]) -> list[str]:
    """Plain-language notes only (Part 20) — never a reason code, never a
    table/column identifier, never a raw percentage-to-4-decimals. Only
    MEDIUM/HIGH severity observations surface here; a LOW-severity finding
    (e.g. a trivial 0.5% orphan rate) is real but not worth an executive's
    attention (Part 22: "do not clutter every ordinary answer")."""
    return [o.message for o in observations if o.severity in ("MEDIUM", "HIGH")]


def technical_diagnostics(observations: list[DataQualityObservation]) -> list[dict[str, Any]]:
    """Full detail for a developer/admin view — every field, every
    severity. Collapsed/hidden by default in the UI (Part 20); this
    function only shapes the data, it does not decide visibility."""
    return [
        {
            "issue_type": o.issue_type, "severity": o.severity,
            "table_fqn": o.table_fqn, "column_name": o.column_name,
            "business_concept": o.business_concept, "evidence": o.evidence,
            "affected_rows": o.affected_rows, "message": o.message,
        }
        for o in observations
    ]
