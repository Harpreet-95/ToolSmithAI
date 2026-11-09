"""
Fast-path context builder — Phase 7, Step 6C, Part 2.

Builds the bounded context for the ONE combined GPT-6 Sol call (request
kind + business intent + schema selection + SQL generation). Reuses
core.sqlservice.schema_frontier.build_schema_frontier() and
render_frontier_candidate() ENTIRELY UNCHANGED — this module adds exactly
one new rendered line per candidate (explicit confirmed-relationship
column pairs, "local_column -> other_object.other_column") on top of the
frontier's own already-bounded, already-priority-ordered compact summary.
No second retrieval, ranking, or business-knowledge system: every field
this module reads was already computed by build_schema_frontier() (which
itself reuses core.sqlservice.vector_index/lexical_index/
business_concept_index/business_knowledge_evidence/schema_object_authority/
snapshot_classifier unchanged) for the plain schema-selection stage.

Context priority (Part 2's own requirement) is inherited from
build_schema_frontier(), never re-implemented here:
  1. Owner-confirmed business mappings — force-admitted ahead of every
     fused-score candidate (build_schema_frontier's own verified-mapping
     force-admission + sort-first behavior).
  2. Required PK/local-FK/relationship-column evidence — primary_key_
     columns/local_foreign_key_columns/confirmed_relationship_column_pairs
     are NEVER trimmed by the character-budget pass (see schema_frontier's
     own _DROP_PRIORITY and this module's own unconditional JOIN_KEYS
     append below).
  3. Question-matching columns — dimension_/measure_/date_/status_columns
     are already ordered by question-token overlap first (schema_frontier.
     _order_columns_by_relevance).
  4. Canonical/authority evidence — AUTHORITY/AUTHORITY_DETAIL lines.
  5. Date/status/measure columns — DATE_COLUMNS/STATUS_COLUMNS/
     MEASURE_COLUMNS lines.
  6. Remaining bounded columns — COLUMNS (dimension_columns), the last,
     most-aggressively-trimmed category.

No SQL Server call of any kind — only the already-prepared, already-stored
retrieval build (Part 2's own "do not query SQL Server for schema at
question time" requirement).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import (
    AI_FIRST_SQL_FAST_PATH_MAX_CHARACTERS_PER_OBJECT,
    AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS,
)
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.schema_frontier import STATUS_OK as _FRONTIER_STATUS_OK
from core.sqlservice.schema_frontier import (
    DEFAULT_MAX_ADMITTED_CANDIDATES,
    DEFAULT_FRONTIER_SIZE,
    FrontierCandidate,
    SchemaFrontierResult,
    build_schema_frontier,
    render_frontier_candidate,
)

__all__ = [
    "STATUS_OK",
    "FastPathContext",
    "render_fast_path_candidate",
    "build_fast_path_context",
]

STATUS_OK = _FRONTIER_STATUS_OK

# Rough, honest, clearly-labeled estimate only (this codebase has no
# published per-model tokenizer wired into core.sqlservice — see e.g.
# development validation material (not included here)'s own
# "no cost is estimated or fabricated" convention). ~4 characters/token is
# a standard order-of-magnitude approximation for English + identifier-
# heavy text, never presented as an exact count.
_CHARS_PER_TOKEN_ESTIMATE = 4


@dataclass(frozen=True)
class FastPathContext:
    """Result of one build_fast_path_context() call.

    status: STATUS_OK, or any of build_schema_frontier's own non-OK
        SchemaFrontierResult.status values, propagated unchanged when the
        underlying frontier itself could not be built.
    frontier: the underlying SchemaFrontierResult, kept for a caller that
        needs a candidate's own structured fields (e.g. to validate a
        selected_object_fqn is real) — never re-derived.
    object_count / total_characters / estimated_tokens: Part 2's own
        required observability fields, recorded here rather than left for
        each caller to recompute.
    """

    status: str
    build_id: int
    schema_fingerprint: str | None
    frontier: SchemaFrontierResult
    context_text: str
    object_count: int
    total_characters: int
    estimated_tokens: int
    warnings: tuple[str, ...]
    error: str | None


# Phase 7, Step 6E3B1, Part 5 — priority ranks for the FINAL bounded
# context assembly below. 1-2 (object identity, real columns) are rendered
# by render_frontier_candidate() itself and are NEVER trimmed here (that
# function already enforces its own, pre-existing per-category character
# budget — see its own module docstring; this module only adds NEW
# content on top of it). Priorities 3-9 are exactly this module's own
# additions, each independently droppable, highest-priority-first, by
# _assemble_bounded_context below — never an arbitrary byte-level cut that
# could sever a line mid-category.
_PRIORITY_JOIN_KEYS = 3
_PRIORITY_GRAIN = 4
_PRIORITY_POPULATION_RESTRICTIONS = 5
_PRIORITY_REQUIRED_DISTINCT_KEY = 6
_PRIORITY_SECRET_WARNINGS = 7
_PRIORITY_SUITABILITY = 8
_PRIORITY_DESCRIPTIVE = 9

_CATEGORY_LABELS = {
    _PRIORITY_JOIN_KEYS: "join_keys",
    _PRIORITY_GRAIN: "view_grain",
    _PRIORITY_POPULATION_RESTRICTIONS: "population_restrictions",
    _PRIORITY_REQUIRED_DISTINCT_KEY: "required_distinct_key",
    _PRIORITY_SECRET_WARNINGS: "secret_warnings",
    _PRIORITY_SUITABILITY: "suitability",
    _PRIORITY_DESCRIPTIVE: "descriptive_enrichment",
}


def _view_profile_category_lines(profile, profile_state: str | None = None) -> dict[int, str]:
    """Phase 7, Step 6E3B, Part 7 (restructured for Part 6E3B1, Part 5;
    Part 6E3B2 Part 1 adds the live `profile_state`) — compact, decision-
    critical view-suitability evidence for GPT-6 Sol, split into
    independently-droppable priority categories (see the `_PRIORITY_*`
    constants above) instead of one monolithic line. Never includes a raw
    PII/secret VALUE — only column-name-level categories, matching the
    persisted profile's own informational-only contract.

    `profile_state` (core.sqlservice.view_profiler.classify_profile_state,
    computed by the caller against THIS build's live identity -- never
    recomputed here) is folded into the highest-priority VIEW_PROFILE line
    so GPT-6 Sol knows BEFORE generating SQL that a non-COMPLETE view's
    reference will be rejected at validation -- it should pick another
    object or ask for clarification rather than spend a turn on it."""
    if profile is None:
        return {}
    s = profile.suitability
    state_text = f"profile_state={profile_state} " if profile_state is not None else ""
    lines: dict[int, str] = {
        _PRIORITY_GRAIN: (
            f"VIEW_GRAIN: {state_text}grain={profile.grain_class} dup_risk={profile.duplication_risk} "
            f"row_preservation={profile.row_preservation_risk} authority={profile.authority_class} "
            f"lineage_complete={profile.lineage_complete}"
        ),
    }
    if profile.population_restrictions or (s is not None and s.population_restriction):
        lines[_PRIORITY_POPULATION_RESTRICTIONS] = "POPULATION_RESTRICTED: true"
    if profile.required_distinct_key:
        lines[_PRIORITY_REQUIRED_DISTINCT_KEY] = f"REQUIRED_DISTINCT_KEY: {profile.required_distinct_key}"
    if profile.secret_columns:
        lines[_PRIORITY_SECRET_WARNINGS] = f"SECRET_COLUMNS: {','.join(profile.secret_columns[:5])}"
    if s is not None:
        suitable_for = "+".join(
            name for name, ok in (
                ("DETAIL", s.suitable_for_detail), ("COUNT", s.suitable_for_count),
                ("AGGREGATION", s.suitable_for_aggregation), ("JOIN", s.suitable_for_join),
            ) if ok
        )
        suitability_parts = [f"suitable_for={suitable_for or 'NONE'}"]
        if s.unsuitable_reasons:
            suitability_parts.append(f"unsuitable={';'.join(s.unsuitable_reasons[:3])}")
        lines[_PRIORITY_SUITABILITY] = "SUITABILITY: " + " ".join(suitability_parts)
    descriptive_parts = [f"framework_backed={profile.is_framework_backed}"]
    if profile.entity_keys:
        descriptive_parts.append(f"entity_keys={','.join(profile.entity_keys[:3])}")
    if profile.pii_categories:
        descriptive_parts.append(f"pii={','.join(profile.pii_categories[:5])}")
    # Phase 7, Step 6E3B1, Part 4 -- business labels/semantic role from
    # already-populated core.sqlservice.semantic_enrichment_store data,
    # lowest priority (this is exactly the "optional descriptive
    # enrichment" category Part 5's own priority list names) and only
    # ever surfaced when the enrichment is AVAILABLE (fresh) -- never a
    # STALE/FAILED business label presented as if it were current.
    descriptive_parts.append(f"enrichment={profile.semantic_enrichment_status}")
    # Step 6E3B2, Part 2: UNVERIFIED (not provably stale, but exact
    # content-hash identity could not be proven) still surfaces the
    # business label -- the status itself (rendered above) already tells
    # GPT-6 Sol it is not exactly verified. STALE/FAILED/MISSING/DISABLED
    # never do.
    if profile.semantic_enrichment_status in ("AVAILABLE", "UNVERIFIED"):
        if profile.semantic_enrichment_business_name:
            descriptive_parts.append(f"business_name={profile.semantic_enrichment_business_name}")
        if profile.semantic_enrichment_source_role:
            descriptive_parts.append(f"source_role={profile.semantic_enrichment_source_role}")
    lines[_PRIORITY_DESCRIPTIVE] = "VIEW_PROFILE_NOTES: " + " ".join(descriptive_parts)
    return lines


def _render_view_profile_line(profile) -> str:
    """Backward-compatible single-string rendering (unbounded — callers
    needing the Part 5 budget guarantee must use _candidate_prioritized_
    lines + _assemble_bounded_context instead, which build_fast_path_
    context now does)."""
    lines = _view_profile_category_lines(profile)
    if not lines:
        return ""
    return "VIEW_PROFILE: " + "; ".join(lines[p] for p in sorted(lines))


def render_fast_path_candidate(candidate: FrontierCandidate, view_profile=None) -> str:
    """Unbounded convenience rendering (used by tests and any caller that
    does not need Part 5's global character-budget guarantee). The real
    question-time path (build_fast_path_context) does NOT call this —
    it uses _candidate_prioritized_lines + _assemble_bounded_context so
    the appended JOIN_KEYS/VIEW_PROFILE content can never push the final
    prompt past max_context_characters."""
    base = render_frontier_candidate(candidate)
    if candidate.confirmed_relationship_column_pairs:
        pairs = ", ".join(candidate.confirmed_relationship_column_pairs)
        base = f"{base}\nJOIN_KEYS: {pairs}"
    if candidate.object_type == "VIEW" and view_profile is not None:
        line = _render_view_profile_line(view_profile)
        if line:
            base = f"{base}\n{line}"
    return base


def _candidate_prioritized_lines(
    candidate: FrontierCandidate, view_profile, profile_state: str | None = None,
) -> tuple[str, dict[int, str]]:
    """Returns (pinned_base_text, {priority: line_text}) for one
    candidate. `pinned_base_text` (render_frontier_candidate's own output
    — priorities 1-2) is NEVER subject to the category-level trim in
    _assemble_bounded_context; only the returned dict's entries are."""
    base = render_frontier_candidate(candidate)
    by_priority: dict[int, str] = {}
    if candidate.confirmed_relationship_column_pairs:
        pairs = ", ".join(candidate.confirmed_relationship_column_pairs)
        by_priority[_PRIORITY_JOIN_KEYS] = f"JOIN_KEYS: {pairs}"
    if candidate.object_type == "VIEW" and view_profile is not None:
        by_priority.update(_view_profile_category_lines(view_profile, profile_state))
    return base, by_priority


def _assemble_bounded_context(
    per_candidate: list[tuple[str, dict[int, str]]], max_context_characters: int,
) -> tuple[str, int, tuple[str, ...]]:
    """Phase 7, Step 6E3B1, Part 5 — ONE final bounded assembly with a
    hard configured maximum. Never appends unlimited content after
    trimming: starts from every candidate's pinned base text PLUS every
    optional category, and — only if that exceeds max_context_characters —
    drops ENTIRE categories, lowest-priority (9, "descriptive_enrichment")
    first, across EVERY candidate at once, re-checking the total after
    each drop. A category is never partially dropped (no candidate keeps
    a lower-priority line while another loses a higher-priority one), and
    decision-critical categories (3-8) are only ever dropped if priority 9
    alone was not enough — never before it. Returns (context_text,
    total_characters, warnings) — `warnings` names exactly which
    categories were dropped and how many objects lost a line, so a trim
    is always observable, never silent. The pinned base text itself
    (priorities 1-2) is NEVER trimmed here (that is schema_frontier's own,
    separate, pre-existing responsibility) — if even that alone exceeds
    the budget, this function applies one last explicit, warned, hard
    truncation so the configured maximum is a real guarantee, not merely
    a target.
    """
    warnings: list[str] = []

    def _render(dropped: frozenset[int]) -> str:
        blocks = []
        for base, by_priority in per_candidate:
            lines = [base] + [by_priority[p] for p in sorted(by_priority) if p not in dropped]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    dropped: set[int] = set()
    text = _render(frozenset(dropped))
    for priority in sorted(_CATEGORY_LABELS, reverse=True):  # 9 (lowest) down to 3
        if len(text) <= max_context_characters:
            break
        affected = sum(1 for _, by_priority in per_candidate if priority in by_priority)
        if affected == 0:
            continue
        dropped.add(priority)
        text = _render(frozenset(dropped))
        warnings.append(
            f"context_budget_trimmed:category={_CATEGORY_LABELS[priority]}(priority={priority}),"
            f"objects_affected={affected}"
        )

    if len(text) > max_context_characters:
        # Even every pinned base block alone exceeds the budget -- a hard,
        # observable last resort so the configured maximum is never
        # silently exceeded. render_frontier_candidate's own per-object
        # budget should make this unreachable in practice; this is a
        # guarantee, not an expectation.
        warnings.append(
            f"context_budget_hard_truncated:from_characters={len(text)},to_characters={max_context_characters}"
        )
        text = text[:max_context_characters]

    return text, len(text), tuple(warnings)


def build_fast_path_context(
    question: str,
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    frontier_size: int = DEFAULT_FRONTIER_SIZE,
    max_admitted_candidates: int = DEFAULT_MAX_ADMITTED_CANDIDATES,
    max_context_characters: int = AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS,
    max_characters_per_object: int = AI_FIRST_SQL_FAST_PATH_MAX_CHARACTERS_PER_OBJECT,
) -> FastPathContext:
    """
    Build the bounded prompt context for the fast path's one combined call.

    Makes exactly the SAME one embedding call build_schema_frontier()
    itself spends — zero additional AI/embedding calls, zero SQL Server
    calls, zero new retrieval/ranking/business-knowledge computation.

    A non-OK frontier status (or zero candidates) is propagated as-is,
    context_text="" — the caller treats this exactly like an unusable
    frontier anywhere else in this codebase (STATUS_RETRIEVAL_FAILED).
    """
    frontier = build_schema_frontier(
        question, build_id, source_id, user_id, provider,
        frontier_size=frontier_size, max_admitted_candidates=max_admitted_candidates,
        max_frontier_characters=max_context_characters, max_characters_per_object=max_characters_per_object,
    )
    if frontier.status != _FRONTIER_STATUS_OK or not frontier.candidates:
        return FastPathContext(
            status=frontier.status, build_id=frontier.build_id, schema_fingerprint=frontier.schema_fingerprint,
            frontier=frontier, context_text="", object_count=0, total_characters=0, estimated_tokens=0,
            warnings=frontier.warnings, error=frontier.error,
        )

    view_fqns = [c.object_fqn for c in frontier.candidates if c.object_type == "VIEW"]
    profiles_by_fqn_lower: dict = {}
    state_by_fqn_lower: dict = {}
    if view_fqns:
        try:
            from data.view_profile_service import get_view_profiles_by_fqns, get_current_definition_hashes
            from core.sqlservice.view_profiler import classify_profile_state
            from core.dictionary.pii_detector import SECRET_POLICY_VERSION
            profiles_by_fqn_lower = get_view_profiles_by_fqns(source_id, view_fqns)
            current_hashes = get_current_definition_hashes(source_id, view_fqns)
            for fqn_lower, profile in profiles_by_fqn_lower.items():
                state_by_fqn_lower[fqn_lower] = classify_profile_state(
                    profile, schema_fingerprint=frontier.schema_fingerprint,
                    current_definition_hash=current_hashes.get(fqn_lower),
                    current_secret_policy_version=SECRET_POLICY_VERSION,
                )
            # A VIEW admitted to the frontier but with no profile row at
            # all still needs its state surfaced -- MISSING, never absent.
            from core.sqlservice.view_profiler import PROFILE_STATE_MISSING
            for fqn in view_fqns:
                state_by_fqn_lower.setdefault(fqn.lower(), PROFILE_STATE_MISSING)
        except Exception:  # noqa: BLE001 -- view-profile evidence is additive, never fatal to context building
            profiles_by_fqn_lower = {}
            state_by_fqn_lower = {}

    per_candidate = [
        _candidate_prioritized_lines(
            c, profiles_by_fqn_lower.get(c.object_fqn.lower()), state_by_fqn_lower.get(c.object_fqn.lower()),
        )
        for c in frontier.candidates
    ]
    rendered_text, total_characters, trim_warnings = _assemble_bounded_context(per_candidate, max_context_characters)
    return FastPathContext(
        status=STATUS_OK, build_id=frontier.build_id, schema_fingerprint=frontier.schema_fingerprint,
        frontier=frontier, context_text=rendered_text, object_count=len(frontier.candidates),
        total_characters=total_characters,
        estimated_tokens=math.ceil(total_characters / _CHARS_PER_TOKEN_ESTIMATE),
        warnings=frontier.warnings + trim_warnings, error=None,
    )
