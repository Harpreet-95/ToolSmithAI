"""
CURRENT-STATE MILESTONE, Parts 2-6 — generic "currently X" semantic
representation and schema-driven candidate discovery.

A question naming a current-state modifier ("currently enrolled", "still
active", "currently open", "currently employed", "currently subscribed")
carries a MATERIAL semantic constraint that core.semantic.concept_resolver.
extract_query_intent already surfaces as current_state_required/
current_state_phrase/current_state_subject (see that function's docstring).
This module turns that flag into real, schema-grounded candidate filters —
never a hardcoded source-specific column mapping (no "CancelID"/"EndDate"
literal anywhere below). Every candidate is discovered from already-fetched
table metadata's own is_date/semantic_type/name-token signals, the same kind
of evidence data.query_planning_service's own date/status column discovery
already reads, plus that module's existing _compatible_status_filter for the
boolean/flag "currently active" case (reused, not reimplemented).

Multiple, materially different candidates surviving discovery is expected
and correct on a real schema (a live source can genuinely have a
non-cancelled definition, a not-ended definition, and an associated-entity-
active definition that all diverge) — that is GENUINE_BUSINESS_AMBIGUITY,
not a bug to resolve by picking one; the caller
(core.orchestrator.context_builder) is responsible for turning >=2
surviving candidates into a clarification turn instead of guessing, and 0
candidates into a safe refusal instead of silently dropping the modifier.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.dictionary.rule_classifier import _tokenize

_CANCEL_TOKENS = frozenset({"cancel", "cancelled", "canceled", "cancellation"})
_END_TOKENS = frozenset({
    "end", "ended", "expire", "expired", "expiration", "terminate", "terminated",
    "termination", "complete", "completed", "completion", "closed", "close",
})
# Excludes an END_TOKENS match on a column that is ALSO evidence of a start/
# creation event (e.g. "EnrollmentDate") — narrows false positives without
# hardcoding any specific column name.
_START_TOKENS = frozenset({
    "start", "started", "begin", "began", "create", "created", "insert", "inserted",
    "register", "registered", "signup", "signed", "enroll", "enrolled", "enrollment",
})


@dataclass(frozen=True)
class CurrentStateCandidate:
    table_fqn: str
    column_name: str
    kind: str  # "not_cancelled" | "not_ended" | "active_flag"
    operator: str
    value: object
    label: str  # business-facing description — never a raw column name


def _table_label(ctx: dict | None, table_fqn: str) -> str:
    if ctx:
        business_name = (ctx.get("dictionary") or {}).get("business_name")
        if business_name:
            return business_name
        entity = (ctx.get("entity") or {}).get("entity")
        if entity and entity != "Unknown":
            return entity
    return table_fqn.split(".")[-1]


def _column_is_nullable(col: dict) -> bool:
    schema = col.get("schema") or {}
    is_nullable = schema.get("is_nullable")
    # Only a CONFIRMED NOT NULL excludes a column — unknown nullability
    # (schema section absent/incomplete) is not treated as disqualifying,
    # the same "no evidence against it" discipline
    # _typed_status_filter_value already applies to unsampled columns.
    return is_nullable is not False


# Numeric SQL types where a bare "active"/"inactive" WORD as the compared
# value would be a type mismatch (e.g. an integer status-code column like
# "IsActiveHire" whose name naming-convention-matches "active" but whose
# real values are coded integers, not the literal word) — live-confirmed
# against a real production database. _compatible_status_filter's own textual-column branch
# is correct for a genuine TEXT status column (e.g. "Active"/"Inactive"
# values); this guard exists only to catch that ONE naming-convention
# false-positive class before it becomes a candidate, without touching
# _compatible_status_filter/_find_status_column themselves (shared,
# already-tested machinery used by the unrelated, working status-filter
# path elsewhere).
_NUMERIC_DATA_TYPES = frozenset({"INTEGER", "NUMERIC", "FLOAT", "DECIMAL", "BIGINT", "SMALLINT"})


def _status_filter_type_is_sound(ctx: dict, compat: tuple) -> bool:
    _, column_name, _operator, typed_value = compat
    if isinstance(typed_value, (int, float)) and not isinstance(typed_value, bool):
        return True  # a boolean column's own 0/1 mapping — always sound
    col = next((c for c in ctx.get("columns") or [] if c.get("column_name") == column_name), None)
    data_type = ((col or {}).get("schema") or {}).get("data_type")
    return data_type not in _NUMERIC_DATA_TYPES


def plan_table_fqns(query_plan: dict) -> list[str]:
    """Every table this plan's resolved SELECT/join path actually touches —
    the entity/measure/dimension table(s) plus every intermediate join hop
    (query_plan["join_plan"]["steps"], the raw data.query_planning_service.
    _plan_joins output both the AI physical planner and the legacy planner
    attach unchanged) — never a database-wide table list."""
    tables: set[str] = set(query_plan.get("tables") or [])
    join_plan = query_plan.get("join_plan") or {}
    for step in join_plan.get("steps") or []:
        for key in ("from_table", "to_table"):
            t = step.get(key)
            if t:
                tables.add(t)
    return sorted(tables)


def discover_current_state_candidates(
    source_id: int,
    table_contexts: dict[str, dict],
    candidate_table_fqns: list[str],
    *, session=None,
) -> list[CurrentStateCandidate]:
    """Bounded, deterministic pass over the tables THIS question's resolved
    plan already touches (never a database-wide scan, mirroring
    core.answering.data_quality_observations' same bounded-to-this-question
    discipline). Reads only already-fetched table metadata (nullability,
    column-name tokens) plus one call per table into
    data.query_planning_service._compatible_status_filter — the same
    generic, non-database-specific boolean/flag "active" column discovery the
    status-filter path already uses."""
    from data.query_planning_service import _compatible_status_filter

    # Keyed by (table_fqn, kind) rather than (table_fqn, column_name) — a
    # table with several differently-named "ended"-shaped columns (e.g. a
    # class's own end date AND a separate completion date) offers the SAME
    # underlying business choice ("has this table's record ended?"); asking
    # a user to pick between near-duplicate columns they have no way to
    # distinguish would violate the "useful clarification" requirement, so
    # only the single most specific match per (table, kind) survives —
    # fewest name tokens first (a tighter, more literal name match), then
    # alphabetical for a fully deterministic tie-break.
    best_by_table_kind: dict[tuple[str, str], tuple[tuple[int, str], CurrentStateCandidate]] = {}

    def _consider(table_fqn: str, kind: str, candidate: CurrentStateCandidate, sort_name: str) -> None:
        rank = (len(_tokenize(sort_name)), sort_name)
        key = (table_fqn, kind)
        existing = best_by_table_kind.get(key)
        if existing is None or rank < existing[0]:
            best_by_table_kind[key] = (rank, candidate)

    for table_fqn in candidate_table_fqns:
        ctx = table_contexts.get(table_fqn)
        if not ctx:
            continue
        table_label = _table_label(ctx, table_fqn)

        for col in ctx.get("columns") or []:
            col_name = col.get("column_name") or ""
            if not col_name or not _column_is_nullable(col):
                continue
            tok_set = set(_tokenize(col_name))
            if tok_set & _CANCEL_TOKENS:
                _consider(table_fqn, "not_cancelled", CurrentStateCandidate(
                    table_fqn=table_fqn, column_name=col_name, kind="not_cancelled",
                    operator="IS NULL", value=None,
                    label=f"{table_label} record has not been cancelled",
                ), col_name)
            elif (tok_set & _END_TOKENS) and not (tok_set & _START_TOKENS):
                _consider(table_fqn, "not_ended", CurrentStateCandidate(
                    table_fqn=table_fqn, column_name=col_name, kind="not_ended",
                    operator="IS NULL", value=None,
                    label=f"{table_label} record has not ended",
                ), col_name)

        compat = _compatible_status_filter(table_fqn, ctx, "active", source_id, session=session)
        if compat and _status_filter_type_is_sound(ctx, compat):
            _, col_name, operator, typed_value = compat
            _consider(table_fqn, "active_flag", CurrentStateCandidate(
                table_fqn=table_fqn, column_name=col_name, kind="active_flag",
                operator=operator, value=typed_value,
                label=f"associated {table_label} is currently active",
            ), col_name)

    # Stable, deterministic output order: by table_fqn, then kind.
    return [
        c for _key, (_rank, c) in sorted(best_by_table_kind.items(), key=lambda kv: kv[0])
    ]


def candidate_already_satisfied(
    candidates: list[CurrentStateCandidate], existing_filters: list[dict],
) -> bool:
    """True when one of the discovered candidates is already present among
    the plan's own resolved filters (e.g. the question ALSO said "active"
    explicitly and status resolution already added that exact column) — the
    current-state requirement is then already met, no clarification needed."""
    existing = {
        (f.get("table_fqn"), f.get("column") or f.get("column_name"))
        for f in existing_filters if f.get("resolved", True)
    }
    return any((c.table_fqn, c.column_name) in existing for c in candidates)


def as_query_plan_filter(candidate: CurrentStateCandidate) -> dict:
    """The exact dict shape data.sql_planning_service._build_where reads
    directly from query_plan["filters"] — mirrors
    core.orchestrator.ai_physical_planning._resolve_stage2_filters' own
    status-filter shape (filter_kind="status"), just filter_kind=
    "current_state"."""
    return {
        "column": candidate.column_name, "operator": candidate.operator,
        "value": candidate.value, "resolved": True, "table_fqn": candidate.table_fqn,
        "filter_kind": "current_state",
    }


def serialize_candidates(candidates: list[CurrentStateCandidate]) -> list[dict]:
    """Plain-dict form for attaching to an early-exit result dict (crosses
    into core.answering.explanation_builder) — never leak the dataclass
    itself past this module."""
    return [
        {
            "id": f"cs_{i}", "table_fqn": c.table_fqn, "column_name": c.column_name,
            "kind": c.kind, "operator": c.operator, "value": c.value, "label": c.label,
        }
        for i, c in enumerate(candidates, start=1)
    ]
