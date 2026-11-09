"""
Bounded VIEW_ENRICHED input construction — SQL Architecture Correction,
Phase 6, Step 6C, Part 3.

Pure and in-memory: no I/O, no provider call, no database access. Every
piece of evidence (columns, dictionary fields, raw view-definition text,
dependency edges, confirmed-relationship summary lines) is passed in
ALREADY LOADED by the caller — this module never reads
data/view_semantics_service.py, data/view_dependency_service.py, or
data/relationship_service.py itself (avoiding a circular/heavy dependency
and keeping this module trivially testable with fake data, matching Part 9's
"no real provider, no database" requirement for the majority of this
milestone's tests). The Part 10 dry-run script is the one caller that loads
real evidence and passes it through this module's pure functions.

SQL-definition handling (Part 3's own "SQL definition handling" section):
  - Untrusted metadata: never executed, never rewritten, never parsed for
    control flow — only sliced (bounded/truncated) and carried as opaque
    text. The actual "explicit data delimiters" and "evidence only, never
    instructions" framing is applied downstream, once, when the prompt
    itself is rendered (core.sqlservice.semantic_enrichment_service.
    _render_view_block) — this module never touches prompt text.
  - Per-view AND per-batch character limits: `build_view_semantic_input`
    enforces the PER-VIEW limit (truncating deterministically and
    reporting it via `definition_truncated`); the PER-BATCH limit is
    enforced by core.sqlservice.semantic_enrichment_service's own batch
    planner (`_plan_view_batches`), which already bounds a whole batch's
    total rendered character count — two independent, composable bounds,
    never one substituting for the other.
  - Truncation is always deterministic: a fixed character-count prefix,
    exactly like core.sqlservice.semantic_enrichment_selection's own
    existing truncation conventions elsewhere in this codebase.
  - Never includes credentials, connection strings, or result rows — this
    module's only text input is the view's own DDL string; nothing else is
    ever concatenated into `definition_evidence`.

Dependency-trust classification (Part 3's own "Dependency handling"
section, and Part 6B's already-decided trust/classification policy) is
reproduced here EXACTLY as data/relationship_service.py's own
`_evidence_rank` function already ranks it (physical FK > confirmed catalog
view dependency > human-APPROVED > everything else, i.e. PENDING/regex-
derived) — see `classify_dependency_trust`'s own docstring for the precise
mapping. This is a documented, small, intentional duplication (the same
"small enough to duplicate across a clean module boundary" precedent
core.sqlservice.semantic_enrichment_object_core_selection already applies
to core.sqlservice.semantic_enrichment_selection's own `_is_opaque_name`),
not a second, independently-evolving trust policy: this module reads the
SAME three raw signals (`relationship_type`, `inference_method`,
`relationship_status`) `_evidence_rank` reads, and produces the SAME
relative ordering, never a different one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEFINITION_CHARACTERS_PER_VIEW,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEPENDENCY_EDGES_PER_VIEW,
)
from core.sqlservice.semantic_enrichment_service import (
    ColumnSemanticInput,
    DEPENDENCY_TRUST_APPROVED,
    DEPENDENCY_TRUST_CATALOG_CONFIRMED,
    DEPENDENCY_TRUST_PHYSICAL_FK,
    DEPENDENCY_TRUST_UNCONFIRMED_REGEX,
    ViewDependencyEdgeInput,
    ViewSemanticInput,
)
from core.sqlservice.view_semantic_enrichment_selection import ViewEnrichmentCandidate

__all__ = [
    "TRUNCATION_MARKER",
    "RawDependencyEdge",
    "ViewInputBuildResult",
    "classify_dependency_trust",
    "build_view_semantic_input",
]

TRUNCATION_MARKER = "\n-- [TRUNCATED: definition exceeded the configured character limit] --"


@dataclass(frozen=True)
class RawDependencyEdge:
    """One raw dependency-edge row, exactly as read from data/
    relationship_service.py / data/view_dependency_service.py — the three
    fields `classify_dependency_trust` needs, before this module reduces
    them to one DEPENDENCY_TRUST_* label."""

    referenced_object_fqn: str
    relationship_type: str | None
    inference_method: str | None
    relationship_status: str | None


def classify_dependency_trust(edge: RawDependencyEdge) -> str:
    """
    Reproduces data/relationship_service.py's `_evidence_rank` precedence
    EXACTLY (rank 3/2/1/0), mapped to core.sqlservice.
    semantic_enrichment_service's DEPENDENCY_TRUST_* labels:

      rank 3 (relationship_type == "FOREIGN_KEY" or
              inference_method == "declared_fk")        -> PHYSICAL_FK
      rank 2 (inference_method == "catalog_view_dependency")
                                                          -> CATALOG_CONFIRMED
      rank 1 (relationship_status == "APPROVED")         -> APPROVED
      rank 0 (everything else -- PENDING/REJECTED, or AUTO
              not backed by declared_fk/catalog dependency)
                                                          -> UNCONFIRMED_REGEX

    Never returns anything else — the four DEPENDENCY_TRUST_* labels are
    the complete, exhaustive output range, matching `_evidence_rank`'s own
    complete 0-3 range one-to-one.
    """
    if edge.relationship_type == "FOREIGN_KEY" or edge.inference_method == "declared_fk":
        return DEPENDENCY_TRUST_PHYSICAL_FK
    if edge.inference_method == "catalog_view_dependency":
        return DEPENDENCY_TRUST_CATALOG_CONFIRMED
    if edge.relationship_status == "APPROVED":
        return DEPENDENCY_TRUST_APPROVED
    return DEPENDENCY_TRUST_UNCONFIRMED_REGEX


@dataclass(frozen=True)
class ViewInputBuildResult:
    """Immutable result of one build_view_semantic_input() call."""

    view_input: ViewSemanticInput
    definition_truncated: bool
    dependency_edges_truncated: bool
    warnings: tuple[str, ...]


def build_view_semantic_input(
    candidate: ViewEnrichmentCandidate,
    *,
    columns: Sequence[ColumnSemanticInput],
    primary_key_columns: Sequence[str],
    dictionary_business_name: str | None,
    dictionary_description: str | None,
    dictionary_synonyms: Sequence[str],
    raw_definition_sql: str | None,
    dependency_edges: Sequence[RawDependencyEdge],
    confirmed_relationship_summary: Sequence[str],
    max_definition_characters: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEFINITION_CHARACTERS_PER_VIEW,
    max_dependency_edges: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEPENDENCY_EDGES_PER_VIEW,
) -> ViewInputBuildResult:
    """
    Build one view's complete, bounded ViewSemanticInput from already-loaded
    evidence. Every column/PK/dictionary field is carried through verbatim
    in CONTENT (no bounding needed here — those are already bounded
    upstream by existing, unchanged code, e.g.
    core.sqlservice.retrieval_documents), but never verbatim in SEQUENCE:
    `columns` and `dictionary_synonyms` are independently re-sorted here
    (by column name / synonym text, case-insensitively) regardless of the
    order the caller happened to supply them in. This is deliberate --
    Phase 6, Step 6G, Part 1 traced a real semantic_input_hash instability
    to exactly this gap (this function used to trust caller ordering for
    both fields), and a caller that reads them from an unordered SQL
    query or an unordered set has no ordering guarantee to give. The
    resulting ViewSemanticInput's field order is now a pure function of
    CONTENT alone, never of read/iteration order.

    `raw_definition_sql` (None when no view_semantic_evidence row exists
    yet for this view) is deterministically truncated to
    `max_definition_characters` (a fixed-length prefix plus TRUNCATION_MARKER
    when truncation occurs — never a re-summarization, never mid-word-aware,
    always the exact same output for the exact same input length) — this IS
    the "deterministic truncation" Part 3 requires, reported via
    `definition_truncated`.

    `dependency_edges` is classified via `classify_dependency_trust` and
    deterministically ordered by (trust rank descending, referenced_object_fqn
    ascending, inference_method ascending) before being bounded to
    `max_dependency_edges` — the trailing `inference_method` tiebreak
    (Step 6G, Part 1) makes the order total even when two edges to the
    SAME referenced object carry the SAME trust label, so a caller's
    unordered read of two such tied rows can never flip their relative
    position in the final tuple. The HIGHEST-trust edges are always kept
    first when truncation is necessary, never an arbitrary subset.
    Truncation here is reported via `dependency_edges_truncated` and a
    warning, never silently.

    Raises ValueError for `max_definition_characters <= 0` or
    `max_dependency_edges <= 0`.
    """
    if not isinstance(max_definition_characters, int) or max_definition_characters <= 0:
        raise ValueError(f"max_definition_characters must be a positive integer, got {max_definition_characters!r}")
    if not isinstance(max_dependency_edges, int) or max_dependency_edges <= 0:
        raise ValueError(f"max_dependency_edges must be a positive integer, got {max_dependency_edges!r}")

    warnings: list[str] = []

    definition_text = raw_definition_sql or ""
    definition_truncated = len(definition_text) > max_definition_characters
    if definition_truncated:
        definition_text = definition_text[:max_definition_characters] + TRUNCATION_MARKER
        warnings.append(
            f"view {candidate.object_fqn}: definition truncated to {max_definition_characters} characters"
        )

    _trust_rank = {
        DEPENDENCY_TRUST_PHYSICAL_FK: 3,
        DEPENDENCY_TRUST_CATALOG_CONFIRMED: 2,
        DEPENDENCY_TRUST_APPROVED: 1,
        DEPENDENCY_TRUST_UNCONFIRMED_REGEX: 0,
    }
    classified = tuple(
        ViewDependencyEdgeInput(
            referenced_object_fqn=edge.referenced_object_fqn,
            trust_label=classify_dependency_trust(edge),
            inference_method=str(edge.inference_method or "unknown"),
        )
        for edge in dependency_edges
    )
    classified_sorted = tuple(
        sorted(
            classified,
            key=lambda e: (-_trust_rank[e.trust_label], e.referenced_object_fqn.lower(), e.inference_method),
        )
    )
    dependency_edges_truncated = len(classified_sorted) > max_dependency_edges
    kept_edges = classified_sorted[:max_dependency_edges]
    if dependency_edges_truncated:
        warnings.append(
            f"view {candidate.object_fqn}: dependency edges truncated to {max_dependency_edges} "
            f"(kept the {max_dependency_edges} highest-trust edge(s))"
        )

    view_input = ViewSemanticInput(
        object_fqn=candidate.object_fqn,
        schema_name=candidate.schema_name,
        view_name=candidate.view_name,
        retrieval_tier=candidate.retrieval_tier,
        columns=tuple(sorted(columns, key=lambda c: c.column_name.lower())),
        primary_key_columns=tuple(sorted(set(primary_key_columns))),
        dictionary_business_name=dictionary_business_name,
        dictionary_description=dictionary_description,
        dictionary_synonyms=tuple(sorted(set(dictionary_synonyms), key=str.lower)),
        definition_evidence=definition_text,
        definition_truncated=definition_truncated,
        dependency_edges=kept_edges,
        confirmed_relationship_summary=tuple(sorted(set(confirmed_relationship_summary))),
    )
    return ViewInputBuildResult(
        view_input=view_input,
        definition_truncated=definition_truncated,
        dependency_edges_truncated=dependency_edges_truncated,
        warnings=tuple(warnings),
    )
