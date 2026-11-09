"""
Deterministic join-subgraph planner — SQL Architecture Correction, Phase 6,
Step 3 (regression-corrected in Step 3B — see the "Step 3B correction"
notes throughout this file for exactly what changed and why).

Given a core.sqlservice.schema_linker.SchemaLinkingResult (already-matched
QuestionRequirements plus ranked AnchorCandidate objects) and the complete
retrieval-document set it was computed against, chooses the smallest
high-confidence CONNECTED set of tables/views that collectively covers the
question's semantic requirements, together with the exact join edges (real
column pairs, never a fabricated predicate) that connect them.

This module does NOT wire into production retrieval/orchestration, does not
generate SQL, does not execute anything, and performs no I/O of any kind —
pure, in-memory, deterministic, offline. See plan_schema_join_subgraph's own
docstring for the full contract.

Reuse (this module does not duplicate any of the following):
  - core.sqlservice.schema_linker.SchemaLinkingResult/QuestionRequirements/
    MatchedRequirement/RequirementCandidate/AnchorCandidate — the ONLY
    source of "what does this question need" and "which objects are
    plausible representatives of each need". This module never re-derives
    a requirement or re-matches a question term.
  - core.sqlservice.schema_linker.ANCHOR_AMBIGUITY_MARGIN — the SAME
    "materially plausible alternative" margin convention, reused verbatim
    for plan-level ambiguity detection (never a second, conflicting margin
    value).
  - core.sqlservice.canonical_scoring.compute_canonical_scores — the SAME
    per-build canonical-authority score, reused for the "canonical-object
    quality" scoring term (never a second/conflicting authority score).
  - core.sqlservice.retrieval_documents' confirmed_relationship_edges
    metadata shape (direction/column/other/kind), written by
    build_retrieval_documents — the ONLY source of relationship evidence.
    This module never reads document_text, never re-parses a view
    definition, and never calls a database.

Why NOT core.sqlservice.relationship_graph: that module's adjacency is
built from confirmed_relationship_targets/sources, which — by design —
includes lineage-only VIEW_DEPENDENCY "*" edges alongside real FK edges
with no capability distinction at all (it exists to answer "is there a
CONFIRMED relationship of any kind", for one-hop retrieval expansion, not
"can this become a SQL join predicate"). Requirement 1 of this Step
("classify relationship capability... do not treat every confirmed
relationship as a SQL join edge") is exactly the distinction that module
does not make and is not the right layer to retrofit it onto (it would
change behavior for schema_context_retrieval's own one-hop expansion,
which correctly wants ALL confirmed edges, lineage included, for
observability/candidate purposes). This module therefore builds its own
capability-classified, JOINABLE-only graph directly from the same
underlying confirmed_relationship_edges metadata, over the complete Tier
1/2 object set — never a second copy of relationship_graph's own
traversal for the same purpose.

Scope: pure, in-memory, no I/O of any kind — no database connection, no
embedding/LLM/provider call. Never generates SQL, never executes a query,
never wires into production retrieval/orchestration (that is a later
Step). No single customer's specific table/column name or production
constant appears anywhere in this file.

── Step 3B: requirement roles, single-object preservation, lazy search ────

A read-only verification against a real established golden-case
fixture (Phase 6, Step 3A) proved three GENERIC (never table-specific)
defects in Step 3's own design, not fixable by tuning:

  1. `max_terminal_candidates_per_requirement`/`max_requirements_considered`
     were small RANK cutoffs (5/8) applied BEFORE the search's own path/
     operation budget ever ran — a correct candidate ranked 9th or 13th
     among a modest ~15-20 total was silently excluded, misreported as a
     schema gap that never existed. Step 3B raises both to large defensive
     SAFETY CEILINGS (50/200) and makes the GLOBAL path/operation/time
     budget the only real correctness-limiting mechanism (requirement 3/4).
  2. IDENTIFIER-kind concepts (structurally FK/PK-shaped column matches,
     e.g. a "ClassID" appearing on dozens of tables) were treated as
     independently mandatory coverage, identically to an explicit entity/
     measure ask — inflating a single repeated join key into what looked
     like dozens of separate "requirements". Step 3B classifies every
     normalized requirement into one of four ROLES (see
     REQUIREMENT_ROLE_*) and uses IDENTIFIER-role concepts ONLY as join-
     path evidence, never as coverage a plan must satisfy.
  3. A single-object branch that is CORRECT but cannot grow further (its
     object has no — or only lineage-only — confirmed relationships) was
     unfairly evicted from the beam purely because unrelated multi-object
     branches kept accumulating incidental coverage of OTHER, lower-value
     concepts; the beam's own per-step width pruning was the only chance a
     valid single-object answer ever got. Step 3B evaluates every linker
     anchor as its own durable SINGLE_OBJECT plan candidate (see
     _build_single_object_baseline_states), scored by the SAME formula and
     compared FAIRLY at the very end, never dependent on surviving
     intermediate beam pruning (requirement 2's "linker preservation
     contract").
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict, defaultdict, deque
from itertools import combinations
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from core.config import (
    SQL_JOIN_PLANNING_MAX_ALTERNATIVE_PLANS,
    SQL_JOIN_PLANNING_MAX_AMBIGUITY_RECORDS,
    SQL_JOIN_PLANNING_MAX_BRIDGE_ANCHOR_PAIRS,
    SQL_JOIN_PLANNING_MAX_BRIDGE_CANDIDATES_PER_PAIR,
    SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES,
    SQL_JOIN_PLANNING_MAX_CONSIDERED_PATHS,
    SQL_JOIN_PLANNING_MAX_EXPANDED_STATES,
    SQL_JOIN_PLANNING_MAX_FRONTIER_SIGNATURES,
    SQL_JOIN_PLANNING_MAX_OPERATION_MS,
    SQL_JOIN_PLANNING_MAX_PATH_HOPS,
    SQL_JOIN_PLANNING_MAX_PATHS_PER_ANCHOR_FAMILY,
    SQL_JOIN_PLANNING_MAX_REQUIREMENTS_CONSIDERED,
    SQL_JOIN_PLANNING_MAX_SELECTED_OBJECTS,
    SQL_JOIN_PLANNING_MAX_SINGLE_OBJECT_BASELINES,
    SQL_JOIN_PLANNING_MAX_STATES_PER_SIGNATURE,
    SQL_JOIN_PLANNING_MAX_TERMINAL_CANDIDATES_PER_REQUIREMENT,
    SQL_JOIN_PLANNING_MAX_WARNINGS,
)
from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, compute_canonical_scores
from core.sqlservice.schema_linker import (
    ANCHOR_AMBIGUITY_MARGIN,
    CONCEPT_KIND_IDENTIFIER,
    MATCH_KIND_MODEL_SEMANTIC,
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    SchemaLinkingResult,
    SemanticCatalog,
    _load_linker_index,
)
from core.sqlservice.view_authority import MATCH_KIND_VIEW_AUTHORITY

# Phase 6, Step 6G, Part 3 — every match_kind that carries VALIDATED but
# ADVISORY evidence, never direct structural/dictionary/lexical proof: a
# model-inferred business_domain/alias/column-role claim (MATCH_KIND_
# MODEL_SEMANTIC, core.sqlservice.schema_linker) and a view-authority
# domain/source-role/canonical-quality boost (MATCH_KIND_VIEW_AUTHORITY,
# core.sqlservice.view_authority.augment_linking_with_view_authority).
# Both must be treated identically here — "structural evidence outranks
# model evidence" and "domain and source-role authority must not
# introduce an object into the candidate pool by themselves" apply to
# BOTH channels equally; a set (not a single constant) makes it
# impossible to update one and silently forget the other.
_ADVISORY_MATCH_KINDS = frozenset({MATCH_KIND_MODEL_SEMANTIC, MATCH_KIND_VIEW_AUTHORITY})

__all__ = [
    "JOIN_CAPABILITY_JOINABLE",
    "JOIN_CAPABILITY_LINEAGE_ONLY",
    "JOIN_CAPABILITY_UNUSABLE",
    "RELATIONSHIP_TYPE_PHYSICAL_FK",
    "RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY",
    "RELATIONSHIP_TYPE_OTHER_CONFIRMED",
    "DIRECTION_FORWARD",
    "DIRECTION_REVERSE",
    "DIRECTION_UNDIRECTED",
    "REQUIREMENT_ROLE_BUSINESS_REQUIRED",
    "REQUIREMENT_ROLE_FILTER_OR_GROUPING",
    "REQUIREMENT_ROLE_JOIN_HINT",
    "REQUIREMENT_ROLE_UNRESOLVED",
    "PLAN_KIND_SINGLE_OBJECT",
    "PLAN_KIND_JOINED_SUBGRAPH",
    "PLAN_KIND_PARTIAL",
    "PLAN_KIND_AMBIGUOUS",
    "REASON_NO_JOIN_REQUIRED",
    "REASON_NO_CONFIRMED_JOIN_PATH",
    "REASON_LINEAGE_ONLY_PATH",
    "REASON_SEARCH_BUDGET_EXHAUSTED",
    "REASON_CANDIDATE_EVIDENCE_INSUFFICIENT",
    "REASON_GENUINE_AMBIGUITY",
    "STATUS_OK",
    "STATUS_PARTIAL_COVERAGE",
    "STATUS_NO_REQUIREMENTS",
    "STATUS_NO_CANDIDATES",
    "STATUS_LIMITS_EXCEEDED",
    "STATUS_INDEX_BUILD_FAILED",
    "STATUS_NOT_PREPARED",
    "PlanningIndexPreparationResult",
    "prepare_schema_planning_indexes",
    "LIMIT_MAX_FRONTIER_SIGNATURES",
    "LIMIT_MAX_STATES_PER_SIGNATURE",
    "LIMIT_MAX_PATH_HOPS",
    "LIMIT_MAX_SELECTED_OBJECTS",
    "LIMIT_MAX_CONSIDERED_PATHS",
    "LIMIT_MAX_TERMINAL_CANDIDATES",
    "LIMIT_MAX_REQUIREMENTS_CONSIDERED",
    "LIMIT_MAX_ALTERNATIVE_PLANS",
    "LIMIT_MAX_AMBIGUITY_RECORDS",
    "LIMIT_MAX_WARNINGS",
    "LIMIT_MAX_EXPANDED_STATES",
    "LIMIT_MAX_PATHS_PER_ANCHOR_FAMILY",
    "LIMIT_MAX_BRIDGE_ANCHOR_PAIRS",
    "LIMIT_MAX_BRIDGE_CANDIDATES_PER_PAIR",
    "CONCEPT_KIND_BRIDGE",
    "PROVENANCE_BRIDGE",
    "LIMIT_OPERATION_TIME_BUDGET",
    "PlannedJoinEdge",
    "UncoveredRequirementReason",
    "SchemaSubgraphPlan",
    "PlanAmbiguityRecord",
    "JoinPlanningLimits",
    "DEFAULT_JOIN_PLANNING_LIMITS",
    "JoinPlanningResult",
    "plan_schema_join_subgraph",
    "clear_join_planner_cache",
    "join_planner_cache_size",
    "prepare_join_graph_index",
]

# ── Capability vocabulary (requirement 1) ──────────────────────────────────

JOIN_CAPABILITY_JOINABLE = "JOINABLE"
JOIN_CAPABILITY_LINEAGE_ONLY = "LINEAGE_ONLY"
JOIN_CAPABILITY_UNUSABLE = "UNUSABLE"

# Mirrors core.sqlservice.retrieval_documents' own three-bucket confirmed-
# edge provenance vocabulary verbatim (_KIND_PHYSICAL_FK/
# _KIND_CATALOG_VIEW_DEPENDENCY/_KIND_OTHER_CONFIRMED) — this module reads
# exactly that classification from confirmed_relationship_edges' own "kind"
# field and never invents a finer vocabulary than what that field already
# carries (the original relationship_service relationship_type/
# inference_method columns are not visible past that point).
RELATIONSHIP_TYPE_PHYSICAL_FK = "PHYSICAL_FK"
RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY = "CATALOG_VIEW_DEPENDENCY"
RELATIONSHIP_TYPE_OTHER_CONFIRMED = "OTHER_CONFIRMED"

DIRECTION_FORWARD = "FORWARD"      # from_object_fqn is the FK owner
DIRECTION_REVERSE = "REVERSE"      # to_object_fqn is the FK owner
DIRECTION_UNDIRECTED = "UNDIRECTED"  # no determinable FK ownership (e.g. approved non-FK edge, lineage)

# ── Requirement roles (Step 3B, requirement 1; Step 3C, requirement 6) ────
#
# Every normalized MatchedRequirement (core.sqlservice.schema_linker) is
# classified into exactly one PLANNER role, deterministically, from its own
# (concept_kind, strength) pair — never from any table/column name, never
# tuned per schema/question. Step 3B introduced the four roles from
# concept_kind alone; Step 3C corrected the root cause that made this
# insufficient (a real, large schema derives dozens of low-value single-
# word ENTITY-kind matches that are not, in fact, what the question is
# about) by having core.sqlservice.schema_linker itself additionally
# classify EVERY requirement's own `strength` (STRENGTH_CORE/CONSTRAINT/
# SUPPORTING/UNRESOLVED) — see that module's own docstring for exactly how.
# This module now maps strength (not raw concept_kind) to a planner role:
#
#   BUSINESS_REQUIRED: strength == CORE (an explicitly expressed business
#       entity/measure/dimension/date target — see schema_linker's own
#       STRENGTH_CORE docstring for the exact, generic admission rules). A
#       complete plan must cover every BUSINESS_REQUIRED concept that has
#       at least one eligible candidate.
#   FILTER_OR_GROUPING: strength == CONSTRAINT (an explicit filter/group/
#       date modifier the question actually used, associated with a core
#       concept) — contributes to scoring at a strictly lower priority tier
#       than BUSINESS_REQUIRED and never forces an otherwise-unnecessary
#       object into the plan on its own.
#   JOIN_HINT: strength == SUPPORTING (weak token/column/identifier
#       evidence), REGARDLESS of concept_kind — an IDENTIFIER-kind concept
#       is always JOIN_HINT (Step 3B's own rule, still exactly correct: an
#       identifier is never independently mandatory coverage even when
#       schema_linker judges it otherwise-strong). Used ONLY as
#       connectivity/ranking evidence; never counted as coverage, never
#       appears in a plan's covered/uncovered requirement lists.
#   UNRESOLVED: strength == UNRESOLVED (a plausible meaning the evidence
#       cannot confidently assign to one candidate) — retained for
#       ambiguity/clarification observability only, never processed by the
#       search.
REQUIREMENT_ROLE_BUSINESS_REQUIRED = "BUSINESS_REQUIRED"
REQUIREMENT_ROLE_FILTER_OR_GROUPING = "FILTER_OR_GROUPING"
REQUIREMENT_ROLE_JOIN_HINT = "JOIN_HINT"
REQUIREMENT_ROLE_UNRESOLVED = "UNRESOLVED"

_ROLE_BY_STRENGTH: Mapping[str, str] = MappingProxyType({
    STRENGTH_CORE: REQUIREMENT_ROLE_BUSINESS_REQUIRED,
    STRENGTH_CONSTRAINT: REQUIREMENT_ROLE_FILTER_OR_GROUPING,
    STRENGTH_SUPPORTING: REQUIREMENT_ROLE_JOIN_HINT,
    STRENGTH_UNRESOLVED: REQUIREMENT_ROLE_UNRESOLVED,
})


def _requirement_role(concept_kind: str, strength: str, *, promoted: bool = False) -> str:
    """(concept_kind, strength) -> planner role — requirement 6. An
    IDENTIFIER-kind concept is ALWAYS JOIN_HINT regardless of strength OR
    promotion (Step 3B's own rule preserved verbatim — "an identifier is
    never independently mandatory coverage even when schema_linker judges
    it otherwise-strong", unaffected by this function's own `promoted`
    parameter); every other kind follows `strength` alone via
    `_ROLE_BY_STRENGTH`, defaulting to JOIN_HINT for any strength value
    this module does not recognize (fail-safe: never silently promote
    unknown evidence to mandatory coverage).

    Phase 6, Step 6J, Part 1 fix — `promoted=True` (this concept_key is a
    member of core.sqlservice.schema_linker.SchemaLinkingResult.
    promoted_supporting_concept_keys, Step 6I Part 2's own strong-evidence
    anchor promotion) maps a non-IDENTIFIER STRENGTH_SUPPORTING concept to
    FILTER_OR_GROUPING instead of JOIN_HINT. A real audit proved this gap:
    schema_linker could already promote a strong, directly-evidenced
    SUPPORTING candidate all the way to becoming a real schema_linker
    anchor, but this module's OWN role mapping had no channel to see that
    decision at all — the promoted concept still earned zero mandatory-
    coverage scoring credit here, so a correctly-constructed plan
    including it could never structurally outrank a smaller, incomplete
    plan lacking it. The concept's own `strength` is never changed by
    this — it stays SUPPORTING; only which PLANNER ROLE it maps to here
    changes, and only for a concept schema_linker itself already decided
    deserves anchor-mandatory treatment (never a blanket SUPPORTING ->
    FILTER_OR_GROUPING promotion, which would silently resurrect the
    anchor-explosion defect Step 2B/3C's own strength classification
    exists to prevent)."""
    if concept_kind == CONCEPT_KIND_IDENTIFIER:
        return REQUIREMENT_ROLE_JOIN_HINT
    if promoted and strength == STRENGTH_SUPPORTING:
        return REQUIREMENT_ROLE_FILTER_OR_GROUPING
    return _ROLE_BY_STRENGTH.get(strength, REQUIREMENT_ROLE_JOIN_HINT)

_COVERAGE_ROLES = frozenset({REQUIREMENT_ROLE_BUSINESS_REQUIRED, REQUIREMENT_ROLE_FILTER_OR_GROUPING})

# ── Relationship-aware bridge candidates (Phase 6, Step 6J, Part 2) ───────
#
# A synthetic concept_kind/match-provenance pair for a graph-derived
# candidate that has ZERO lexical/semantic relevance to the question by
# construction — it exists ONLY because it sits on a real, confirmed,
# JOINABLE (never lineage-only) relationship path connecting two
# genuinely strong, DIFFERENT business requirements. Never collides with
# a real core.sqlservice.schema_linker CONCEPT_KIND_* value (those are
# ENTITY/MEASURE/DIMENSION/DATE_ROLE/IDENTIFIER; "BRIDGE" is not and never
# will be one of them), so a caller can always tell a bridge-derived
# _Requirement apart from a real matched one purely from its concept_key
# prefix, before even inspecting confidence.
CONCEPT_KIND_BRIDGE = "BRIDGE"
PROVENANCE_BRIDGE = "BRIDGE_RELATIONSHIP_PATH"

# Confidence assigned to a bridge candidate is a bounded, DECREASING
# function of hop distance from its nearest anchor endpoint — never a
# claim of real semantic relevance (Part 2: "a bridge receives BRIDGE/
# structural provenance, never fabricated semantic relevance"). Purely a
# deterministic, generic distance-based tie-break for the search's own
# ranking; never tuned against any specific schema/question.
_BRIDGE_BASE_CONFIDENCE = 0.30
_BRIDGE_CONFIDENCE_DECAY_PER_HOP = 0.05


def _strong_requirement_anchors(requirements: Sequence["_Requirement"]) -> dict[str, str]:
    """concept_key -> object_fqn_lower of the single strongest candidate
    for every BUSINESS_REQUIRED-role requirement with at least one
    candidate — bridge expansion's own notion of "a strong candidate for a
    distinct requirement" (Part 2, bullet 3), reusing each requirement's
    OWN already-established best candidate verbatim (never a second,
    conflicting re-rank)."""
    return {
        req.concept_key: req.candidates[0][0]
        for req in requirements
        if req.role == REQUIREMENT_ROLE_BUSINESS_REQUIRED and req.candidates
    }


def _generate_bridge_requirements(
    requirements: Sequence["_Requirement"], index: "_JoinGraphIndex", limits: "JoinPlanningLimits",
    limits_reached: list[str],
) -> list["_Requirement"]:
    """
    Phase 6, Step 6J, Part 2 — bounded structural expansion. For every
    DISTINCT pair of BUSINESS_REQUIRED requirements' own strongest
    candidates, finds the shortest path through `index.joinable_adjacency`
    (this module's OWN capability-classified graph — JOINABLE edges only,
    already built from confirmed_relationship_edges' real column-level
    join evidence via `_classify_raw_edge_capability`; a LINEAGE_ONLY or
    UNUSABLE edge can never appear on it at all, by construction, never by
    a runtime filter that could be forgotten) bounded by
    `limits.max_path_hops` — the SAME existing documented relationship-hop
    limit every other bounded traversal in this module already uses, never
    a new/separate bridge-specific bound. Every intermediate object on
    such a path (excluding both strong-anchor endpoints, which are already
    real candidates for their own requirements) becomes ONE synthetic
    JOIN_HINT-role `_Requirement` per connecting pair — pure connectivity
    evidence, never mandatory coverage, never credited with fabricated
    semantic relevance (its confidence is a bounded, hop-decreasing
    constant, never derived from any lexical/vector score).

    Bounded, never a global graph flood: at most `limits.
    max_bridge_anchor_pairs` DISTINCT anchor pairs are ever examined (Part
    2: "No global graph flood"), and at most `limits.
    max_bridge_candidates_per_pair` intermediate objects are kept per
    connecting path, deterministically ordered by (hop distance ascending,
    canonical object_fqn ascending) — the exact tie-break Part 2 asks for
    ("path length, edge trust, requirement relevance, and canonical FQN";
    edge trust and requirement relevance are constant within one bounded
    BFS call, so hop distance and FQN are the only two that can ever
    actually differ here).

    Reuses `_bounded_bfs` — this module's OWN existing bounded-BFS
    primitive (already used by `_extend_state_with_candidate`) — never a
    second/parallel graph-search implementation. A direct (zero-
    intermediate-node) connection between two anchors contributes nothing
    here (the existing `_extend_state_with_candidate` path-connection logic
    already handles that case with no bridge candidate needed at all).
    """
    anchors = _strong_requirement_anchors(requirements)
    distinct_anchor_objects = sorted({fqn for fqn in anchors.values() if fqn in index.eligible_fqns_lower})
    if len(distinct_anchor_objects) < 2:
        return []

    bridge_requirements: list["_Requirement"] = []
    pairs_considered = 0
    bfs_cache: dict[str, tuple["_BfsReach", bool]] = {}
    for a_lower, b_lower in combinations(distinct_anchor_objects, 2):
        if pairs_considered >= limits.max_bridge_anchor_pairs:
            limits_reached.append(LIMIT_MAX_BRIDGE_ANCHOR_PAIRS)
            break
        pairs_considered += 1

        cached = bfs_cache.get(a_lower)
        if cached is None:
            cached = _bounded_bfs(index, a_lower, limits.max_path_hops)
            bfs_cache[a_lower] = cached
        reach, _hop_ceiling_hit = cached
        hit = reach.get(b_lower)
        if hit is None:
            continue
        dist, path, _edges = hit
        intermediate = [n for n in path[1:-1] if n in index.eligible_fqns_lower]
        if not intermediate:
            continue  # direct (or lineage-filtered-out) connection -- no bridge object needed

        candidates = sorted(
            (
                (node_lower, _clip01(_BRIDGE_BASE_CONFIDENCE - hop * _BRIDGE_CONFIDENCE_DECAY_PER_HOP))
                for hop, node_lower in enumerate(intermediate, start=1)
            ),
            key=lambda c: (-c[1], c[0]),
        )
        total_count = len(candidates)
        bounded = tuple(candidates[: limits.max_bridge_candidates_per_pair])
        if len(bounded) < total_count:
            limits_reached.append(LIMIT_MAX_BRIDGE_CANDIDATES_PER_PAIR)

        pair_key = f"{CONCEPT_KIND_BRIDGE}:{a_lower}~{b_lower}"
        bridge_requirements.append(_Requirement(
            concept_key=pair_key, role=REQUIREMENT_ROLE_JOIN_HINT, candidates=bounded,
            candidate_total_count=total_count, truncated_by_ceiling=len(bounded) < total_count,
            structural_fqns_lower=frozenset(fqn for fqn, _c in bounded),
            raw_confidence_by_fqn=MappingProxyType({fqn: conf for fqn, conf in bounded}),
        ))

    return sorted(bridge_requirements, key=lambda r: r.concept_key)


# ── Plan-kind vocabulary (Step 3B, requirement 2) ──────────────────────────
PLAN_KIND_SINGLE_OBJECT = "SINGLE_OBJECT"
PLAN_KIND_JOINED_SUBGRAPH = "JOINED_SUBGRAPH"
PLAN_KIND_PARTIAL = "PARTIAL"
PLAN_KIND_AMBIGUOUS = "AMBIGUOUS"

# ── Missing/uncovered-requirement reason vocabulary (Step 3B, requirement 8)
#
# NO_JOIN_REQUIRED is deliberately never a FAILURE reason — it annotates a
# COVERED business/filter concept on a SINGLE_OBJECT plan whose object has
# no (or only lineage-only) confirmed relationships, to say explicitly "this
# is fine, no join was ever needed" (requirement 7: never mark a single-
# table plan incomplete merely because its object only has lineage
# relationships).
REASON_NO_JOIN_REQUIRED = "NO_JOIN_REQUIRED"
REASON_NO_CONFIRMED_JOIN_PATH = "NO_CONFIRMED_JOIN_PATH"
REASON_LINEAGE_ONLY_PATH = "LINEAGE_ONLY_PATH"
REASON_SEARCH_BUDGET_EXHAUSTED = "SEARCH_BUDGET_EXHAUSTED"
REASON_CANDIDATE_EVIDENCE_INSUFFICIENT = "CANDIDATE_EVIDENCE_INSUFFICIENT"
REASON_GENUINE_AMBIGUITY = "GENUINE_AMBIGUITY"

# ── Result status vocabulary ────────────────────────────────────────────────
#
# STATUS_OK: primary_plan is populated and covers every requirement
#     considered.
# STATUS_PARTIAL_COVERAGE: primary_plan is populated (the best connected
#     plan the search could find) but uncovered_requirement_keys is
#     non-empty — either because no JOINABLE path connects that
#     requirement's candidates to the rest within the configured hop/size
#     bounds (a genuine schema-connectivity gap — see this module's own
#     "lineage cannot become an ON predicate"/disconnection boundary), or
#     because a defensive limit was exhausted before the search could
#     extend to it (see `limits_reached`). Both are reported via warnings
#     so the caller can tell which applies; this module never silently
#     drops a requirement without saying why.
# STATUS_NO_REQUIREMENTS: the input SchemaLinkingResult carried zero
#     matched concepts (or was not STATUS_OK itself) — nothing to plan for.
# STATUS_NO_CANDIDATES: at least one requirement existed, but not a single
#     requirement had any candidate object present in the joinable graph —
#     no basis to select anything.
# STATUS_LIMITS_EXCEEDED: a defensive limit blocked the search from
#     finding ANY valid plan at all (distinct from STATUS_PARTIAL_COVERAGE,
#     where at least a partial plan was found before limits applied).
STATUS_OK = "OK"
STATUS_PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
STATUS_NO_REQUIREMENTS = "NO_REQUIREMENTS"
STATUS_NO_CANDIDATES = "NO_CANDIDATES"
STATUS_LIMITS_EXCEEDED = "LIMITS_EXCEEDED"
# Phase 6, Step 3E, Part 1 — typed failures for the schema-derived join-
# graph index itself (never the search that runs on top of it):
# STATUS_INDEX_BUILD_FAILED: the index could not be constructed safely
#     (an exception was raised while building/loading it) — returned
#     INSTEAD of letting the exception propagate mid-build or silently
#     continuing to search with a partially-built/truncated graph.
# STATUS_NOT_PREPARED: prepare_schema_planning_indexes() was given
#     unusable input (e.g. zero documents) and produced no index at all.
STATUS_INDEX_BUILD_FAILED = "INDEX_BUILD_FAILED"
STATUS_NOT_PREPARED = "NOT_PREPARED"

# ── limits_reached vocabulary ────────────────────────────────────────────
# Phase 6, Step 3F, Part 5 — distinct, separately-reportable bounds:
#   MAX_FRONTIER_SIGNATURES: the coverage-STATE beam limit — how many
#       DISTINCT covered-requirement patterns the frontier tracks at once.
#   MAX_STATES_PER_SIGNATURE: the per-signature state limit — how many
#       alternative (differently-connected) states survive for the SAME
#       covered-requirement pattern.
#   MAX_CONSIDERED_PATHS: the candidate-attempt limit (unchanged from
#       Step 3B) — total candidate-to-tree connection attempts.
#   MAX_PATH_HOPS: the path-expansion limit (unchanged) — BFS hop ceiling.
#   OPERATION_TIME_BUDGET: Phase 6, Step 6J, Part 3 — DEMOTED from the
#       PRACTICAL search bound to an EMERGENCY-ONLY circuit breaker. A real
#       audit proved the wall-clock check (previously evaluated once per
#       requirement round, at the top of the round loop) was load-
#       dependent: the SAME bounded amount of deterministic work could
#       legitimately take a different amount of wall-clock time under CPU
#       contention, so an identical question could silently skip a
#       different number of trailing requirement rounds across repeated
#       calls — "identical input must produce byte-identical plans
#       regardless of CPU load" cannot hold while this is the dominant
#       gate. MAX_EXPANDED_STATES (new) now does that gating job
#       deterministically; this wall-clock check still exists, with a far
#       larger default (core.config.SQL_JOIN_PLANNING_MAX_OPERATION_MS),
#       purely as a last-resort guard against a pathological input the
#       deterministic counters somehow didn't bound in time.
#   MAX_EXPANDED_STATES: Phase 6, Step 6J, Part 3 — total states ever
#       inserted/updated in one round's `new_frontier` (carry-forwards AND
#       extensions), summed across every round of one search — the
#       deterministic replacement for the wall-clock check's own
#       "should we even attempt the next round" gate.
#   MAX_PATHS_PER_ANCHOR_FAMILY: Phase 6, Step 6J, Part 3 — per round, how
#       many newly-EXTENDED states sharing the same "anchor family" (the
#       same set of chosen objects across every currently-covered
#       BUSINESS_REQUIRED concept) may be added to the frontier — bounds
#       one strongly-scored interpretation's own many minor variations
#       from consuming the shared MAX_EXPANDED_STATES/MAX_CONSIDERED_PATHS
#       budget before a genuinely different interpretation (a different
#       anchor family) gets a fair chance to be explored at all. Never
#       applied to carry-forward states (an already-frontier state is
#       always preserved into the next round regardless of family size).
LIMIT_MAX_FRONTIER_SIGNATURES = "max_frontier_signatures"
LIMIT_MAX_STATES_PER_SIGNATURE = "max_states_per_signature"
LIMIT_MAX_PATH_HOPS = "max_path_hops"
LIMIT_MAX_SELECTED_OBJECTS = "max_selected_objects"
LIMIT_MAX_CONSIDERED_PATHS = "max_considered_paths"
LIMIT_MAX_TERMINAL_CANDIDATES = "max_terminal_candidates_per_requirement"
LIMIT_MAX_REQUIREMENTS_CONSIDERED = "max_requirements_considered"
LIMIT_MAX_ALTERNATIVE_PLANS = "max_alternative_plans"
LIMIT_MAX_AMBIGUITY_RECORDS = "max_ambiguity_records"
LIMIT_MAX_WARNINGS = "max_warnings"
LIMIT_MAX_EXPANDED_STATES = "max_expanded_states"
LIMIT_MAX_PATHS_PER_ANCHOR_FAMILY = "max_paths_per_anchor_family"
LIMIT_MAX_BRIDGE_ANCHOR_PAIRS = "max_bridge_anchor_pairs"
LIMIT_MAX_BRIDGE_CANDIDATES_PER_PAIR = "max_bridge_candidates_per_pair"
LIMIT_OPERATION_TIME_BUDGET = "operation_time_budget_ms"

# Only used to key compute_canonical_scores' result identity field — the
# function itself is pure and never touches this identity for scoring math
# (mirrors core.sqlservice.schema_linker's own _CANONICAL_IDENTITY
# convention exactly).
_CANONICAL_IDENTITY = CanonicalScoringIdentity(
    build_id=0, embedding_provider="join_planner", embedding_model="none", embedding_dimensions=0,
)

_ELIGIBLE_TIERS = frozenset({1, 2})

# ── Scoring weights (Step 3B, requirement 5) ───────────────────────────────
#
# `score` is a single bounded-precision float that encodes the SEVEN-tier
# lexicographic priority order as one comparable number: each tier's raw
# component is independently clipped to [0, 1], and tiers are spaced by a
# factor of 10. Because every lower-tier component is <= 1.0, the maximum
# possible contribution from ALL tiers below tier k (at most 6 terms, each
# <= 1, spaced by <=10x) is always strictly less than one full unit of tier
# k's own weight — so a plan that is even fractionally better on a higher-
# priority tier ALWAYS outranks any combination of lower-tier advantages,
# exactly reproducing pure lexicographic comparison while still being one
# sortable float. This is a documented, generic formula — never tuned
# against any specific schema/question.
#
# Step 3B split the old single "coverage" tier into two (BUSINESS_REQUIRED
# first, FILTER_OR_GROUPING second) per requirement 5 — JOIN_HINT-role
# concepts contribute to NEITHER tier, ever, so a cluster of tables that
# only happens to share a common identifier column can never outrank a
# smaller plan that actually answers the question's real business ask.
_SCORE_WEIGHT_BUSINESS_COVERAGE = 10_000_000.0  # 1. coverage of BUSINESS_REQUIRED requirements
_SCORE_WEIGHT_FILTER_COVERAGE = 1_000_000.0     # 2. coverage of FILTER_OR_GROUPING requirements
_SCORE_WEIGHT_JOINABILITY = 100_000.0           # 3. authoritative join validity
_SCORE_WEIGHT_RELEVANCE = 10_000.0              # 4. terminal question relevance
_SCORE_WEIGHT_SIZE = 1_000.0                    # 5. fewer selected objects/shorter paths
_SCORE_WEIGHT_CANONICAL = 100.0                 # 6. canonical-object quality
_SCORE_WEIGHT_HUB = 10.0                        # 7. lower hub/ambiguity risk

# Authority weight per relationship_type, used only for the joinability
# scoring TERM (never for capability classification itself, which is
# purely structural — see _classify_capability). A schema-declared FK is
# the strongest possible evidence; a human-approved-but-otherwise-inferred
# edge (RELATIONSHIP_TYPE_OTHER_CONFIRMED) is real (relationship_status ==
# 'APPROVED' — genuine manual approval, never a default — see
# core.sqlservice.retrieval_documents' own provenance-hardening docstring)
# but conservatively scored a little below a physical constraint.
_EDGE_AUTHORITY_WEIGHT: Mapping[str, float] = MappingProxyType({
    RELATIONSHIP_TYPE_PHYSICAL_FK: 1.0,
    RELATIONSHIP_TYPE_OTHER_CONFIRMED: 0.7,
})


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Public data model (frozen, deeply immutable) ──────────────────────────


@dataclass(frozen=True)
class PlannedJoinEdge:
    """One logical join edge between two objects — ALL authoritative column
    pairs discovered between this exact (from_object_fqn, to_object_fqn)
    pair are grouped into ONE edge here (requirement 2: composite joins are
    never split into independent single-column edges). `from_object_fqn`/
    `to_object_fqn` are always ordered by case-insensitive alphabetical
    FQN — a deliberate CANONICAL orientation (never "whichever side the FK
    happens to be declared on") so the same physical relationship produces
    an identical edge regardless of which endpoint's document metadata it
    was discovered from (see this module's own "reversed relationship
    direction" test). `direction` separately records which side is the
    actual FK owner (DIRECTION_FORWARD == from_object_fqn owns the FK,
    DIRECTION_REVERSE == to_object_fqn does, DIRECTION_UNDIRECTED when no
    FK ownership is determinable — e.g. an approved-but-inferred edge, or a
    lineage-only view dependency). `column_pairs` is `(from_column,
    to_column)` tuples in that same canonical from/to orientation, always
    sorted by (from_column, to_column) for deterministic ordering."""

    from_object_fqn: str
    to_object_fqn: str
    column_pairs: tuple[tuple[str, str], ...]
    relationship_type: str  # RELATIONSHIP_TYPE_*
    inference_method: str
    direction: str  # DIRECTION_*
    join_capability: str  # JOIN_CAPABILITY_*


@dataclass(frozen=True)
class UncoveredRequirementReason:
    """Why one BUSINESS_REQUIRED/FILTER_OR_GROUPING concept is missing from
    a plan's `covered_requirement_keys` — a distinct, evidence-based REASON_*
    (Step 3B, requirement 8), never a vague "not covered". JOIN_HINT/
    UNRESOLVED-role concepts are never represented here at all — they are
    never mandatory coverage in the first place."""

    concept_key: str
    role: str  # REQUIREMENT_ROLE_* (always BUSINESS_REQUIRED or FILTER_OR_GROUPING here)
    reason: str  # REASON_*
    detail: str


@dataclass(frozen=True)
class SchemaSubgraphPlan:
    """One complete, connected schema plan. `selected_object_fqns` and
    `join_edges` together are always a single connected component (a
    spanning tree over the selected objects — no cycles, no duplicate
    paths between the same pair — requirement 7). `anchor_object_fqns` are
    the selected objects that were also schema_linker AnchorCandidate
    objects; `bridge_object_fqns` are selected objects that are NOT a
    candidate for any covered requirement — pure connective tissue, needing
    no independent question relevance of their own (requirement 4/7).
    `covered_requirement_keys`/`uncovered_requirement_keys` are scoped to
    BUSINESS_REQUIRED and FILTER_OR_GROUPING concepts ONLY (Step 3B) —
    JOIN_HINT (identifier) concepts are join-path evidence, never mandatory
    coverage, and never appear in either list. `plan_kind` is one of
    PLAN_KIND_* (Step 3B, requirement 2). `score` is this module's own
    lexicographic ranking float (see the module-level scoring-weight
    comment) — meaningful only for COMPARING plans from the same planning
    call, never across calls/schemas. `confidence` is a bounded [0, 1]
    trust estimate for this plan on its own (coverage x terminal relevance
    x joinability), safe to read in isolation.

    `score_breakdown` (Phase 6, Step 6I, Part 3) is the SAME named
    PlanScoreBreakdown `_score_plan_state` already computed for this plan's
    own state — carried through verbatim (never recomputed a second time)
    so a caller comparing/ordering already-generated plans (e.g. core.
    sqlservice.schema_plan_options' structural-vs-semantic-enhanced dual
    ordering) can do so from real, already-validated component evidence
    instead of re-deriving it from `score`/`confidence` alone. Optional and
    defaults to None ONLY for backward compatibility with a plan
    hand-constructed outside `_state_to_plan` (e.g. an existing test
    fixture) — every plan `plan_schema_join_subgraph` itself returns always
    populates it."""

    selected_object_fqns: tuple[str, ...]
    join_edges: tuple[PlannedJoinEdge, ...]
    plan_kind: str  # PLAN_KIND_*
    covered_requirement_keys: tuple[str, ...]
    uncovered_requirement_keys: tuple[str, ...]
    uncovered_requirement_reasons: tuple[UncoveredRequirementReason, ...]
    anchor_object_fqns: tuple[str, ...]
    bridge_object_fqns: tuple[str, ...]
    score: float
    confidence: float
    warnings: tuple[str, ...]
    score_breakdown: "PlanScoreBreakdown | None" = None


@dataclass(frozen=True)
class PlanAmbiguityRecord:
    """One deterministic plan-level ambiguity signal: the primary plan and
    one alternative plan cover the SAME requirement set via a materially
    different choice for at least one requirement (`concept_key`) — the two
    candidate objects (`primary_object_fqn` / `alternative_object_fqn`) are
    each other's plausible competitor within core.sqlservice.schema_linker.
    ANCHOR_AMBIGUITY_MARGIN, reused verbatim (never a second, conflicting
    margin). `description` is expressed in business/object-evidence terms
    for later clarification — never "please choose a table"."""

    concept_key: str
    description: str
    primary_object_fqn: str
    alternative_object_fqn: str
    primary_plan_score: float
    alternative_plan_score: float


@dataclass(frozen=True)
class JoinPlanningLimits:
    """Defensive, configuration-backed bounds — every one is visible via
    JoinPlanningResult.limits_reached when it actually constrained the
    search (requirement 10: "limit exhaustion must be visible and
    deterministic"). Never tuned against any specific schema/question."""

    max_terminal_candidates_per_requirement: int = SQL_JOIN_PLANNING_MAX_TERMINAL_CANDIDATES_PER_REQUIREMENT
    max_requirements_considered: int = SQL_JOIN_PLANNING_MAX_REQUIREMENTS_CONSIDERED
    max_path_hops: int = SQL_JOIN_PLANNING_MAX_PATH_HOPS
    max_selected_objects: int = SQL_JOIN_PLANNING_MAX_SELECTED_OBJECTS
    # Phase 6, Step 3F — replaces the old single global `beam_width` (a flat
    # top-N-states-by-raw-covered-count cut — see LIMIT_MAX_FRONTIER_
    # SIGNATURES' own docstring for the exact order-sensitivity defect this
    # fixes). max_frontier_signatures bounds BREADTH (distinct covered-
    # requirement patterns tracked); max_states_per_signature bounds DEPTH
    # (alternative states kept per pattern).
    max_frontier_signatures: int = SQL_JOIN_PLANNING_MAX_FRONTIER_SIGNATURES
    max_states_per_signature: int = SQL_JOIN_PLANNING_MAX_STATES_PER_SIGNATURE
    max_considered_paths: int = SQL_JOIN_PLANNING_MAX_CONSIDERED_PATHS
    max_alternative_plans: int = SQL_JOIN_PLANNING_MAX_ALTERNATIVE_PLANS
    max_ambiguity_records: int = SQL_JOIN_PLANNING_MAX_AMBIGUITY_RECORDS
    max_warnings: int = SQL_JOIN_PLANNING_MAX_WARNINGS
    # Phase 6, Step 6J, Part 3 — see LIMIT_OPERATION_TIME_BUDGET's own
    # docstring: this is now an EMERGENCY-ONLY guard (default raised
    # accordingly in core.config) — max_expanded_states is the practical,
    # deterministic replacement for the "should the search even attempt
    # another requirement round" gate this used to solely control.
    operation_time_budget_ms: float = SQL_JOIN_PLANNING_MAX_OPERATION_MS
    max_single_object_baselines: int = SQL_JOIN_PLANNING_MAX_SINGLE_OBJECT_BASELINES
    max_expanded_states: int = SQL_JOIN_PLANNING_MAX_EXPANDED_STATES
    max_paths_per_anchor_family: int = SQL_JOIN_PLANNING_MAX_PATHS_PER_ANCHOR_FAMILY
    max_bridge_anchor_pairs: int = SQL_JOIN_PLANNING_MAX_BRIDGE_ANCHOR_PAIRS
    max_bridge_candidates_per_pair: int = SQL_JOIN_PLANNING_MAX_BRIDGE_CANDIDATES_PER_PAIR


DEFAULT_JOIN_PLANNING_LIMITS = JoinPlanningLimits()


@dataclass(frozen=True)
class JoinPlanningResult:
    """Result of one plan_schema_join_subgraph() call."""

    status: str  # STATUS_*
    primary_plan: SchemaSubgraphPlan | None
    alternative_plans: tuple[SchemaSubgraphPlan, ...]
    ambiguity_records: tuple[PlanAmbiguityRecord, ...]
    considered_candidate_count: int  # total (requirement, terminal candidate) pairs admitted into the search
    considered_path_count: int  # total candidate-to-tree connection attempts actually performed
    limits_reached: tuple[str, ...]  # LIMIT_* names — which defensive bounds actually constrained this result
    error: str | None  # populated only when status != STATUS_OK
    # Phase 6, Step 3E, Part 1 — explicit timing split (requirement 2):
    # `index_load_or_build_ms` is the schema-derived join-graph index's own
    # cache lookup (warm) or construction (cold) cost; `join_search_ms` is
    # the beam search + single-object-baseline scoring cost that follows.
    # `operation_time_budget_ms` (JoinPlanningLimits) is charged ONLY
    # against `join_search_ms` — index construction never consumes the
    # per-question search budget (requirement 3), so a cold cache can never
    # produce a different STRUCTURAL decision than a warm one purely
    # because the clock started earlier (requirement 4).
    index_load_or_build_ms: float = 0.0
    join_search_ms: float = 0.0
    total_ms: float = 0.0  # == index_load_or_build_ms + join_search_ms
    # Phase 6, Step 6J, Part 5 — safe, deterministic diagnostics (counts
    # and identifiers ONLY, computed purely from schema/plan structure —
    # never a raw provider response, prompt, credential, row value, or
    # view-definition SQL; see this module's own docstring for why no
    # such thing could ever reach here in the first place).
    bridge_candidate_count: int = 0  # total graph-derived BRIDGE-provenance candidates generated (Part 2)
    expanded_states_count: int = 0  # total beam-search states expanded (Part 3's deterministic work counter)
    pareto_dominated_count: int = 0  # candidate states removed by _filter_pareto_dominated


# ── Local helpers: descriptor parsing / column validation ─────────────────


def _split_descriptor(descriptor: str) -> tuple[str, str]:
    """"schema.table.column" -> ("schema.table", "column"). "*" columns are
    preserved verbatim (never treated as a parse failure)."""
    object_fqn, sep, column = str(descriptor).rpartition(".")
    if not sep:
        return "", str(descriptor)
    return object_fqn, column


def _columns_for(metadata: Mapping[str, object]) -> frozenset[str]:
    names = metadata.get("column_names") or ()
    if names:
        return frozenset(str(n).lower() for n in names if str(n).strip())
    descriptors = metadata.get("column_descriptors") or ()
    return frozenset(
        str(d.get("column_name")).lower()
        for d in descriptors
        if isinstance(d, Mapping) and d.get("column_name")
    )


# ── Raw edge extraction + canonicalization ─────────────────────────────────


@dataclass(frozen=True)
class _RawEdge:
    from_fqn_lower: str
    from_column: str
    to_fqn_lower: str
    to_column: str
    kind: str  # RELATIONSHIP_TYPE_*


def _extract_raw_edges(documents: Sequence) -> list[_RawEdge]:
    """Every confirmed relationship edge, from EVERY document's own
    confirmed_relationship_edges metadata (both outbound and inbound
    entries) — deliberately symmetric-tolerant: a physical edge recorded on
    both endpoints (the normal case — see core.sqlservice.
    retrieval_documents' own docstring: "a matching inbound edge in the
    target object's document") produces two _RawEdge entries here that
    canonicalize to the SAME PlannedJoinEdge later (see
    _group_into_planned_edges' own dedup), never two different edges."""
    raw: list[_RawEdge] = []
    for doc in documents:
        own_fqn_lower = doc.object_fqn.lower()
        for entry in doc.metadata.get("confirmed_relationship_edges") or ():
            if not isinstance(entry, Mapping):
                continue
            direction = entry.get("direction")
            own_column = str(entry.get("column") or "")
            other_descriptor = str(entry.get("other") or "")
            kind = str(entry.get("kind") or RELATIONSHIP_TYPE_OTHER_CONFIRMED)
            other_fqn, other_column = _split_descriptor(other_descriptor)
            if not other_fqn or not own_column or not other_column:
                continue
            other_fqn_lower = other_fqn.lower()
            if direction == "outbound":
                raw.append(_RawEdge(own_fqn_lower, own_column, other_fqn_lower, other_column, kind))
            elif direction == "inbound":
                raw.append(_RawEdge(other_fqn_lower, other_column, own_fqn_lower, own_column, kind))
    return raw


_KIND_PRIORITY: Mapping[str, int] = MappingProxyType({
    RELATIONSHIP_TYPE_PHYSICAL_FK: 0,
    RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY: 1,
    RELATIONSHIP_TYPE_OTHER_CONFIRMED: 2,
})


def _classify_raw_edge_capability(
    edge: _RawEdge, fqn_by_lower: Mapping[str, str], columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> str:
    """Requirement 1/8: a self-loop or a missing endpoint object is
    UNUSABLE — malformed/out-of-scope evidence, never silently treated as
    usable and never silently dropped without being counted (see this
    module's own observability warnings) — checked FIRST, before any
    capability-specific classification, so EVERY returned capability
    (LINEAGE_ONLY included) is guaranteed to have both endpoints present
    in `fqn_by_lower`; a caller like core.sqlservice.schema_plan_options
    only ever loads Tier 1/2 documents, so a Tier 1/2 object's lineage-only
    (VIEW_DEPENDENCY/"*"-column) edge to an excluded Tier 3/4 object is a
    routine, expected shape here — never a graph-construction crash.

    A "*" column on either side (catalog VIEW_DEPENDENCY edges always use
    "*" — see core.sqlservice.retrieval_documents'/data.
    view_dependency_service's own docstrings) is LINEAGE_ONLY, never a join
    predicate — checked BEFORE column-existence validation, since "*" is a
    deliberate sentinel, not a real (and therefore checkable) column name.
    A real column absent from that object's own real column set is
    UNUSABLE."""
    if edge.from_fqn_lower == edge.to_fqn_lower:
        return JOIN_CAPABILITY_UNUSABLE
    if edge.from_fqn_lower not in fqn_by_lower or edge.to_fqn_lower not in fqn_by_lower:
        return JOIN_CAPABILITY_UNUSABLE
    if edge.kind == RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY or edge.from_column == "*" or edge.to_column == "*":
        return JOIN_CAPABILITY_LINEAGE_ONLY
    from_cols = columns_by_fqn_lower.get(edge.from_fqn_lower, frozenset())
    to_cols = columns_by_fqn_lower.get(edge.to_fqn_lower, frozenset())
    if edge.from_column.lower() not in from_cols or edge.to_column.lower() not in to_cols:
        return JOIN_CAPABILITY_UNUSABLE
    return JOIN_CAPABILITY_JOINABLE


def _group_into_planned_edges(
    raw_edges: Sequence[_RawEdge], fqn_by_lower: Mapping[str, str], columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> tuple[tuple[PlannedJoinEdge, ...], tuple[PlannedJoinEdge, ...], int]:
    """Requirement 2 (composite joins): group every DISTINCT column pair
    between the same canonical (from, to) object pair AND capability into
    ONE PlannedJoinEdge. Returns (joinable_edges, lineage_only_edges,
    unusable_raw_edge_count). Canonical orientation: from_object_fqn/
    to_object_fqn are always the case-insensitively alphabetically-earlier/
    later FQN — so an edge discovered via object A's outbound listing and
    the SAME physical edge discovered via object B's inbound listing always
    canonicalize to byte-identical PlannedJoinEdge output (requirement:
    "reversed relationship direction" produces the same edge)."""
    # group_key: (from_lower, to_lower, capability) -> {(from_col, to_col): (kind, direction)}
    groups: dict[tuple[str, str, str], dict[tuple[str, str], tuple[str, str]]] = {}
    unusable_count = 0

    for edge in raw_edges:
        capability = _classify_raw_edge_capability(edge, fqn_by_lower, columns_by_fqn_lower)
        if capability == JOIN_CAPABILITY_UNUSABLE:
            unusable_count += 1
            continue

        if edge.from_fqn_lower <= edge.to_fqn_lower:
            canon_from, canon_to = edge.from_fqn_lower, edge.to_fqn_lower
            col_pair = (edge.from_column, edge.to_column)
            fk_owner_is_canon_from = True
        else:
            canon_from, canon_to = edge.to_fqn_lower, edge.from_fqn_lower
            col_pair = (edge.to_column, edge.from_column)
            fk_owner_is_canon_from = False

        # PHYSICAL_FK evidence always means the ORIGINAL from_fqn_lower (the
        # object whose document declared the FK) owns it — regardless of
        # which side of the raw edge that ended up on after canonical
        # reordering above.
        if edge.kind == RELATIONSHIP_TYPE_PHYSICAL_FK:
            direction = DIRECTION_FORWARD if fk_owner_is_canon_from else DIRECTION_REVERSE
        else:
            direction = DIRECTION_UNDIRECTED

        group_key = (canon_from, canon_to, capability)
        groups.setdefault(group_key, {})[col_pair] = (edge.kind, direction)

    joinable: list[PlannedJoinEdge] = []
    lineage_only: list[PlannedJoinEdge] = []
    for (canon_from, canon_to, capability), pairs in groups.items():
        # Deterministic composite column-pair ordering (requirement 2).
        ordered_pairs = tuple(sorted(pairs))
        # One relationship_type/direction per grouped edge: highest-priority
        # kind wins (mirrors core.sqlservice.retrieval_documents'
        # _RELATIONSHIP_KIND_PRIORITY convention exactly); its OWN direction
        # travels with it. A composite FK's columns always share one kind,
        # so this only matters when a physical FK and a separate approved-
        # inferred edge happen to connect the exact same two objects.
        best_pair = min(ordered_pairs, key=lambda p: (_KIND_PRIORITY.get(pairs[p][0], 99), p))
        kind, direction = pairs[best_pair]
        planned = PlannedJoinEdge(
            from_object_fqn=fqn_by_lower[canon_from],
            to_object_fqn=fqn_by_lower[canon_to],
            column_pairs=ordered_pairs,
            relationship_type=kind,
            inference_method=kind,
            direction=direction,
            join_capability=capability,
        )
        (joinable if capability == JOIN_CAPABILITY_JOINABLE else lineage_only).append(planned)

    joinable.sort(key=lambda e: (e.from_object_fqn.lower(), e.to_object_fqn.lower()))
    lineage_only.sort(key=lambda e: (e.from_object_fqn.lower(), e.to_object_fqn.lower()))
    return tuple(joinable), tuple(lineage_only), unusable_count


# ── Cached, schema-derived graph index (requirement 11: cache only this) ──


@dataclass(frozen=True)
class _JoinGraphIndex:
    fqn_by_lower: Mapping[str, str]
    eligible_fqns_lower: frozenset[str]
    object_type_by_fqn_lower: Mapping[str, str]
    joinable_adjacency: Mapping[str, tuple[tuple[str, PlannedJoinEdge], ...]]  # Tier 1/2 only
    joinable_edges: tuple[PlannedJoinEdge, ...]
    lineage_edges: tuple[PlannedJoinEdge, ...]
    unusable_edge_count: int
    degree_by_fqn_lower: Mapping[str, int]
    max_degree: int
    canonical_score_by_fqn_lower: Mapping[str, float]


def _compute_schema_fingerprint(documents: Sequence) -> str:
    """A deterministic fingerprint over exactly the fields this module's
    graph derives from — never the question, never a plan. Two calls over
    the same document content (any order) always produce the same key."""
    payload = sorted(
        (
            doc.object_fqn,
            doc.object_type,
            doc.retrieval_tier,
            sorted(str(n) for n in (doc.metadata.get("column_names") or ())),
            sorted(
                (e.get("direction"), e.get("column"), e.get("other"), e.get("kind"))
                for e in (doc.metadata.get("confirmed_relationship_edges") or ())
                if isinstance(e, Mapping)
            ),
        )
        for doc in documents
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_join_graph_index(documents: Sequence) -> _JoinGraphIndex:
    fqn_by_lower: dict[str, str] = {}
    object_type_by_fqn_lower: dict[str, str] = {}
    tier_by_fqn_lower: dict[str, int] = {}
    columns_by_fqn_lower: dict[str, frozenset[str]] = {}

    for doc in documents:
        key = doc.object_fqn.lower()
        fqn_by_lower[key] = doc.object_fqn
        object_type_by_fqn_lower[key] = doc.object_type
        tier_by_fqn_lower[key] = doc.retrieval_tier
        columns_by_fqn_lower[key] = _columns_for(doc.metadata)

    eligible_fqns_lower = frozenset(k for k, tier in tier_by_fqn_lower.items() if tier in _ELIGIBLE_TIERS)

    raw_edges = _extract_raw_edges(documents)
    joinable_edges, lineage_edges, unusable_count = _group_into_planned_edges(
        raw_edges, fqn_by_lower, columns_by_fqn_lower,
    )

    # Requirement 7 / "Tier 3/4 bridge exclusion": an edge is only added to
    # the TRAVERSABLE adjacency when BOTH endpoints are Tier 1/2-eligible —
    # a Tier 3/4 object structurally can never become a graph node at all,
    # mirroring core.sqlservice.relationship_graph's own "dangling
    # reference... never added as a graph node" convention exactly.
    adjacency: dict[str, list[tuple[str, PlannedJoinEdge]]] = {k: [] for k in eligible_fqns_lower}
    for edge in joinable_edges:
        a, b = edge.from_object_fqn.lower(), edge.to_object_fqn.lower()
        if a not in eligible_fqns_lower or b not in eligible_fqns_lower:
            continue
        adjacency[a].append((b, edge))
        adjacency[b].append((a, edge))

    degree_by_fqn_lower = {k: len(v) for k, v in adjacency.items()}
    max_degree = max(degree_by_fqn_lower.values(), default=0)

    canonical_result = compute_canonical_scores(sorted(documents, key=lambda d: d.object_fqn.lower()), _CANONICAL_IDENTITY)
    canonical_score_by_fqn_lower = {
        key: score.total_score for key, score in canonical_result.scores_by_fqn_lower.items()
    }

    return _JoinGraphIndex(
        fqn_by_lower=MappingProxyType(fqn_by_lower),
        eligible_fqns_lower=eligible_fqns_lower,
        object_type_by_fqn_lower=MappingProxyType(object_type_by_fqn_lower),
        joinable_adjacency=MappingProxyType({k: tuple(sorted(v, key=lambda item: item[0])) for k, v in adjacency.items()}),
        joinable_edges=joinable_edges,
        lineage_edges=lineage_edges,
        unusable_edge_count=unusable_count,
        degree_by_fqn_lower=MappingProxyType(degree_by_fqn_lower),
        max_degree=max_degree,
        canonical_score_by_fqn_lower=MappingProxyType(canonical_score_by_fqn_lower),
    )


# Bounded in-process cache — keyed ONLY by a schema-derived fingerprint,
# NEVER by question content or by any plan (requirement 11).
_graph_index_cache: "OrderedDict[str, _JoinGraphIndex]" = OrderedDict()


def _load_join_graph_index(documents: Sequence, *, use_cache: bool = True) -> tuple[_JoinGraphIndex, str]:
    key = _compute_schema_fingerprint(documents)
    if use_cache:
        cached = _graph_index_cache.get(key)
        if cached is not None:
            _graph_index_cache.move_to_end(key)
            return cached, key

    index = _build_join_graph_index(documents)
    if use_cache:
        _graph_index_cache[key] = index
        _graph_index_cache.move_to_end(key)
        while len(_graph_index_cache) > SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES:
            _graph_index_cache.popitem(last=False)
    return index, key


def prepare_join_graph_index(documents: Sequence, *, use_cache: bool = True) -> None:
    """
    Warm this module's own schema-fingerprint-keyed `_JoinGraphIndex` cache
    for `documents` — SQL Architecture Correction, Phase 6, Step 4B, Part 6:
    "prepare the existing... join-planner indexes. No question is
    involved." A thin public wrapper over the already-existing, private
    `_load_join_graph_index` — no new caching mechanism, no change to how a
    real `plan_schema_join_subgraph()` call builds or reuses this same
    cache. Discards the built index; callers only need the side effect (a
    warm cache entry).

    Unlike core.sqlservice.schema_linker.prepare_linker_index, this
    module's own cache key (`_compute_schema_fingerprint(documents)`) is
    derived purely from STRUCTURAL document facts (object/column names,
    types, tiers, confirmed relationship edges) — this module never reads a
    SemanticCatalog or any model-inferred evidence at all, so an enrichment
    run changing catalog content correctly does NOT invalidate this
    particular cache; there is nothing enrichment-derived in it to go
    stale.
    """
    _load_join_graph_index(documents, use_cache=use_cache)


def clear_join_planner_cache() -> None:
    """Explicitly evict every cached _JoinGraphIndex — for tests, and for
    future rebuild invalidation, mirroring clear_schema_linker_cache()."""
    _graph_index_cache.clear()


def join_planner_cache_size() -> int:
    """Number of schema fingerprints currently cached — exposed for tests."""
    return len(_graph_index_cache)


# ── Bounded BFS from one terminal (requirement 5) ──────────────────────────

_BfsReach = Mapping[str, tuple[int, tuple[str, ...], tuple[PlannedJoinEdge, ...]]]


def _bounded_bfs(index: _JoinGraphIndex, start_lower: str, max_hops: int) -> tuple[_BfsReach, bool]:
    """Every node reachable from `start_lower` within `max_hops` JOINABLE
    edges, as {node_lower: (distance, path_of_node_lowers_including_both_
    ends, edges_along_path)}. Deterministic: neighbors visited in sorted
    order at every level (mirrors core.sqlservice.relationship_graph's own
    convention), so the first path recorded to any node is always the
    lexicographically-earliest shortest one.

    Also returns `hop_ceiling_hit`: True iff at least one node at the exact
    `max_hops` frontier still had an unexplored JOINABLE neighbor — i.e. the
    hop budget, not the graph's own extent, is what stopped exploration
    (requirement 10: limit exhaustion must be visible)."""
    if start_lower not in index.joinable_adjacency:
        return {}, False
    reach: dict[str, tuple[int, tuple[str, ...], tuple[PlannedJoinEdge, ...]]] = {
        start_lower: (0, (start_lower,), ()),
    }
    queue: deque[str] = deque([start_lower])
    hop_ceiling_hit = False
    while queue:
        current = queue.popleft()
        dist, path, edges = reach[current]
        if dist >= max_hops:
            if any(neighbor not in reach for neighbor, _e in index.joinable_adjacency.get(current, ())):
                hop_ceiling_hit = True
            continue
        for neighbor, edge in index.joinable_adjacency.get(current, ()):
            if neighbor in reach:
                continue
            reach[neighbor] = (dist + 1, path + (neighbor,), edges + (edge,))
            queue.append(neighbor)
    return reach, hop_ceiling_hit


# ── Requirement extraction from SchemaLinkingResult ────────────────────────


@dataclass(frozen=True)
class _Requirement:
    concept_key: str
    role: str  # REQUIREMENT_ROLE_*
    candidates: tuple[tuple[str, float], ...]  # (object_fqn_lower, confidence), sorted desc confidence
    candidate_total_count: int  # before any ceiling — for CANDIDATE_EVIDENCE_INSUFFICIENT/SEARCH_BUDGET_EXHAUSTED reporting
    truncated_by_ceiling: bool  # True iff candidate_total_count > len(candidates) (the safety ceiling actually bound it)
    # Phase 6, Step 6G, Part 3 — the subset of `candidates` (by object_fqn_
    # lower) whose candidacy for THIS concept traces to at least one
    # non-MODEL_SEMANTIC (structural/dictionary) schema_linker match.
    # `_covered_concepts` requires membership here, never mere presence in
    # `candidates`, before crediting a selected object with "covering" this
    # concept — "model concepts cannot independently satisfy a mandatory
    # requirement". An object can still appear in `candidates` (and be
    # picked as a plan's terminal choice / contribute relevance/ranking)
    # purely on model evidence; it simply never, by itself, flips this
    # concept from uncovered to covered.
    structural_fqns_lower: frozenset[str] = frozenset()
    # Phase 6, Step 6H, Part 2 — the ORIGINAL, pre-anchor-boost confidence
    # per (eligible) candidate object, before `_extract_requirements`' own
    # `max(cand.confidence, anchor_combined_score)` blend. `candidates`
    # (above) remains the BOOSTED relevance value every existing consumer
    # (terminal-choice ranking, beam-search extension) already relies on —
    # this is a SEPARATE, additive observability channel letting
    # `_score_plan_state` report "structural evidence quality" (how
    # strong is the DIRECT catalog/dictionary match itself) distinctly
    # from "direct relevance" (which also credits corroborating semantic/
    # lexical/example evidence blended in via the anchor score) — Part 3's
    # own tier 2 vs tier 3/4 distinction.
    raw_confidence_by_fqn: Mapping[str, float] = MappingProxyType({})


_ROLE_PROCESSING_ORDER: Mapping[str, int] = MappingProxyType({
    REQUIREMENT_ROLE_BUSINESS_REQUIRED: 0,
    REQUIREMENT_ROLE_FILTER_OR_GROUPING: 1,
    REQUIREMENT_ROLE_JOIN_HINT: 2,
})


def _extract_requirements(
    linking_result: SchemaLinkingResult, index: _JoinGraphIndex, limits: JoinPlanningLimits,
    warnings: list[str], limits_reached: list[str],
    *, anchor_combined_score_by_fqn_lower: Mapping[str, float] = MappingProxyType({}),
) -> tuple[list[_Requirement], int]:
    """
    Step 3B correction: `max_requirements_considered`/
    `max_terminal_candidates_per_requirement` are now large defensive safety
    CEILINGS (see core.config's own updated docstring) — NOT the mechanism
    that decides which requirement/candidate the search may consider.
    Nothing here is dropped by rank alone; a requirement or candidate is
    only ever excluded when the ceiling is genuinely, pathologically
    exceeded (reported via `limits_reached`/`warnings` exactly as before),
    never as routine behavior on an ordinary question.

    Every requirement is tagged with its REQUIREMENT_ROLE_* (see
    _requirement_role) from the SAME (concept_kind, strength) pair
    core.sqlservice.schema_linker itself already computed (Step 3C) — this
    module never re-derives strength/role from a table or column name. A
    requirement whose candidate list is empty after Tier 1/2 eligibility
    filtering keeps its own role unchanged (it is excluded from search
    participation for lack of anything to search with, but still counts
    toward its role's own coverage denominator and is still returned for
    observability — see _score_state's own docstring).
    """
    all_matched = (
        linking_result.requirements.entity_concepts
        + linking_result.requirements.measure_concepts
        + linking_result.requirements.dimension_concepts
        + linking_result.requirements.date_role_concepts
        + linking_result.requirements.identifier_concepts
    )
    ordered = sorted(all_matched, key=lambda r: (-r.confidence, r.concept_key))
    bounded = ordered[: limits.max_requirements_considered]
    requirements_considered_omitted = max(0, len(ordered) - len(bounded))
    if requirements_considered_omitted:
        warnings.append(
            f"requirements: {requirements_considered_omitted} item(s) truncated by the "
            f"max_requirements_considered={limits.max_requirements_considered} safety ceiling"
        )
        limits_reached.append(LIMIT_MAX_REQUIREMENTS_CONSIDERED)

    considered_candidate_count = 0
    requirements: list[_Requirement] = []
    for req in bounded:
        seen_fqn_lower: set[str] = set()
        structural_fqns_lower: set[str] = set()
        raw_confidence_by_fqn: dict[str, float] = {}
        eligible_candidates: list[tuple[str, float]] = []
        for cand in req.candidates:
            fqn_lower = cand.object_fqn.lower()
            if fqn_lower not in index.eligible_fqns_lower or fqn_lower in seen_fqn_lower:
                continue
            seen_fqn_lower.add(fqn_lower)
            if cand.match_kind not in _ADVISORY_MATCH_KINDS:
                structural_fqns_lower.add(fqn_lower)
            # Step 3E, Part 4 — `cand.confidence` alone is catalog-only
            # (direct exact phrase/business-name evidence); when this
            # object also cleared linker anchor admission, its AnchorCandidate.
            # combined_score additionally reflects real candidate-specific
            # lexical/semantic/example relevance. max(), never a replace: a
            # strong direct catalog match must never be diluted by a
            # moderate blended anchor score, but a genuinely strong
            # independent evidence corroboration can still raise relevance
            # above the catalog-only value.
            #
            # Phase 6, Step 6G, Part 3 — this boost is withheld for an
            # ADVISORY-origin candidate (_ADVISORY_MATCH_KINDS: model-
            # inferred OR view-authority domain/source-role evidence). An
            # object's
            # overall anchor `combined_score` is earned largely through ITS
            # OWN real lexical/semantic/structural relevance to the
            # question as a WHOLE, not through per-concept evidence — for a
            # structural candidate that borrowed strength is a genuine
            # corroboration (Step 3E's own reasoning still applies
            # unchanged). But a candidate whose ONLY reason for even being
            # present under THIS concept is validated-but-advisory model
            # inference must never additionally inherit the anchor's
            # overall (possibly much stronger, unrelated-evidence-derived)
            # score for a concept model evidence alone proposed — that is
            # exactly how a business view with strong incidental lexical
            # matches (its own name/columns) but only weak, broad model-
            # inferred concept coverage could out-relevance a genuinely
            # narrower, more directly evidenced object for a DIFFERENT
            # concept the view never had real support for at all. Model
            # evidence keeps its own already-capped, already-informativeness
            # -discounted confidence (see schema_linker's
            # MODEL_CANDIDATE_CONFIDENCE_CAP / _build_model_entity_alias_
            # index / _build_model_column_token_index) and nothing more.
            if cand.match_kind in _ADVISORY_MATCH_KINDS:
                relevance = cand.confidence
            else:
                relevance = max(cand.confidence, anchor_combined_score_by_fqn_lower.get(fqn_lower, 0.0))
            eligible_candidates.append((fqn_lower, relevance))
            raw_confidence_by_fqn[fqn_lower] = cand.confidence
        # Re-sort by the (possibly evidence-boosted) relevance value — the
        # incoming order was catalog-confidence-only; candidates[0] must be
        # the TRUE best-evidence candidate (see _requirement_weight), and
        # truncation below must keep the most relevant ones, not merely the
        # most catalog-confident ones.
        eligible_candidates.sort(key=lambda item: (-item[1], item[0]))
        candidate_total_count = len(eligible_candidates)
        candidates = eligible_candidates[: limits.max_terminal_candidates_per_requirement]
        truncated_by_ceiling = len(candidates) < candidate_total_count
        if truncated_by_ceiling:
            warnings.append(
                f"requirement {req.concept_key!r}: terminal candidates truncated by the "
                f"max_terminal_candidates_per_requirement={limits.max_terminal_candidates_per_requirement} safety ceiling"
            )
            limits_reached.append(LIMIT_MAX_TERMINAL_CANDIDATES)
        considered_candidate_count += len(candidates)
        # Step 3C, requirement 6: `role` is always the (concept_kind,
        # strength)-mapped role (see _requirement_role) — NEVER downgraded
        # when candidates is empty. A BUSINESS_REQUIRED/FILTER_OR_GROUPING
        # concept with zero eligible candidates is still counted toward its
        # role's coverage denominator (it is a real ask the question
        # expressed; it simply cannot be satisfied — see
        # REASON_CANDIDATE_EVIDENCE_INSUFFICIENT, which is exactly the
        # observable, reportable form of "no usable evidence", never a
        # silent disappearance from the count).
        role = _requirement_role(
            req.concept_kind, req.strength,
            promoted=req.concept_key in linking_result.promoted_supporting_concept_keys,
        )
        requirements.append(_Requirement(
            concept_key=req.concept_key, role=role, candidates=tuple(candidates),
            candidate_total_count=candidate_total_count, truncated_by_ceiling=truncated_by_ceiling,
            structural_fqns_lower=frozenset(structural_fqns_lower),
            raw_confidence_by_fqn=MappingProxyType(dict(raw_confidence_by_fqn)),
        ))

    # Role-ordered, then most-confident-first within each role (Step 3B,
    # requirement 1/5): BUSINESS_REQUIRED concepts are always processed
    # FIRST — the plan's tree grows OUTWARD from the question's actual
    # entity/measure ask — then FILTER_OR_GROUPING, then JOIN_HINT last, so
    # a repeated identifier column can only ever EXTEND an already-
    # established business backbone via a real join path, never seed or
    # grow its own business-unrelated cluster. Within a role, most-
    # confident-first (ties broken by fewer candidates, then concept_key)
    # keeps the Step 3A fix that a widely-shared column's own concept must
    # not starve out the question's dominant concept — this is a generic
    # ordering rule, never a schema-specific one.
    requirements.sort(key=lambda r: (
        _ROLE_PROCESSING_ORDER.get(r.role, 99),
        -max((c[1] for c in r.candidates), default=0.0),
        len(r.candidates), r.concept_key,
    ))
    return requirements, considered_candidate_count


# ── Beam-search state ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PlanState:
    selected_lower: frozenset[str]
    selected_order: tuple[str, ...]  # insertion order, for deterministic path/edge reconstruction
    edges: tuple[PlannedJoinEdge, ...]
    terminal_choice_by_concept: Mapping[str, str]  # concept_key -> object_fqn_lower actually chosen to cover it


def _covered_concepts(
    state: _PlanState, requirements: Sequence[_Requirement], roles: frozenset[str] = _COVERAGE_ROLES,
) -> frozenset[str]:
    """Concept keys `state.selected_lower` satisfies, restricted to `roles`
    (Step 3B: defaults to BUSINESS_REQUIRED + FILTER_OR_GROUPING — JOIN_HINT
    concepts are never "covered" in the sense this function/its callers
    mean; pass roles=frozenset({REQUIREMENT_ROLE_JOIN_HINT}) explicitly for
    the rare caller that needs join-hint-specific bookkeeping instead).

    Phase 6, Step 6G, Part 3 — a selected object only satisfies a concept
    here when it is also a STRUCTURAL candidate for it (`req.
    structural_fqns_lower`), never merely present in `req.candidates` at
    all. Phase 6, Step 6F's own offline evaluation traced a real
    production regression to exactly this gap: `req.candidates` also
    includes validated-but-advisory MODEL_SEMANTIC entries (Part 4's own
    "expose business_domain/status_concepts/... to question-time
    selection"), and this function previously treated ANY presence —
    however weak, however purely model-inferred — as full coverage, which
    `_weighted_coverage_ratio` then credited using the CONCEPT's own
    best-available confidence (possibly from a completely different,
    unselected, genuinely strong candidate), not the selected object's own
    evidence. A business view whose model-inferred aliases/domain merely
    happen to nominally touch several required concepts could therefore
    look "complete" on paper while covering none of them with real
    evidence — "missing mandatory requirements cannot be hidden by many
    optional matches" (Part 3) and "model concepts cannot independently
    satisfy a mandatory requirement" (Part 5) applied literally. A
    requirement with NO structural candidate at all (an ask this schema
    only ever matched via model inference) is therefore never covered by
    ANY state — a real, intended consequence of "advisory," not a bug."""
    covered = set()
    for req in requirements:
        if req.role not in roles:
            continue
        for fqn_lower, _conf in req.candidates:
            if fqn_lower in state.selected_lower and fqn_lower in req.structural_fqns_lower:
                covered.add(req.concept_key)
                break
    return covered


def _covered_business_concepts(state: _PlanState, requirements: Sequence[_Requirement]) -> frozenset[str]:
    return _covered_concepts(state, requirements, frozenset({REQUIREMENT_ROLE_BUSINESS_REQUIRED}))


def _covered_filter_concepts(state: _PlanState, requirements: Sequence[_Requirement]) -> frozenset[str]:
    return _covered_concepts(state, requirements, frozenset({REQUIREMENT_ROLE_FILTER_OR_GROUPING}))


def _extend_state_with_candidate(
    state: _PlanState, concept_key: str, candidate_lower: str, index: _JoinGraphIndex,
    bfs_cache: dict[str, tuple[_BfsReach, bool]], max_hops: int, max_selected_objects: int,
    hop_ceiling_sink: list[bool],
) -> tuple[_PlanState, int] | None:
    """Returns (new_state, paths_considered_increment) or None if no
    connection exists within bounds. `bfs_cache` memoizes one bounded BFS
    per terminal object (requirement 5: "bounded multi-source BFS").
    `hop_ceiling_sink` records (regardless of outcome) whenever a BFS
    actually hit `max_hops` before exhausting the graph — requirement 10's
    "limit exhaustion must be visible", surfaced by the caller as
    LIMIT_MAX_PATH_HOPS."""
    if candidate_lower in state.selected_lower:
        return None  # already present — a no-op continuation is handled by the caller separately

    if not state.selected_lower:
        new_state = _PlanState(
            selected_lower=frozenset({candidate_lower}), selected_order=(candidate_lower,), edges=(),
            terminal_choice_by_concept={**state.terminal_choice_by_concept, concept_key: candidate_lower},
        )
        return new_state, 0

    cached = bfs_cache.get(candidate_lower)
    if cached is None:
        cached = _bounded_bfs(index, candidate_lower, max_hops)
        bfs_cache[candidate_lower] = cached
    reach, hop_ceiling_hit = cached
    if hop_ceiling_hit:
        hop_ceiling_sink.append(True)

    best: tuple[int, str, tuple[str, ...], tuple[PlannedJoinEdge, ...]] | None = None
    for tree_node in sorted(state.selected_lower):
        hit = reach.get(tree_node)
        if hit is None:
            continue
        dist, path, edges = hit
        candidate_key = (dist, tree_node, path)
        if best is None or candidate_key < (best[0], best[1], best[2]):
            best = (dist, tree_node, path, edges)

    if best is None:
        return None

    _dist, _tree_node, path, edges = best
    new_nodes = [n for n in path if n not in state.selected_lower]
    if len(state.selected_lower) + len(new_nodes) > max_selected_objects:
        return None

    new_selected = state.selected_lower | frozenset(new_nodes)
    existing_edge_keys = {(e.from_object_fqn.lower(), e.to_object_fqn.lower()) for e in state.edges}
    added_edges = tuple(
        e for e in edges
        if (e.from_object_fqn.lower(), e.to_object_fqn.lower()) not in existing_edge_keys
    )
    new_state = _PlanState(
        selected_lower=new_selected,
        selected_order=state.selected_order + tuple(new_nodes),
        edges=state.edges + added_edges,
        terminal_choice_by_concept={**state.terminal_choice_by_concept, concept_key: candidate_lower},
    )
    return new_state, 1


def _requirement_weight(req: _Requirement) -> float:
    """A requirement's own best-candidate confidence — the SAME value
    core.sqlservice.schema_linker.MatchedRequirement.confidence already
    reports (the top eligible candidate's confidence). Used ONLY to weight
    how much one concept's coverage contributes to its role's coverage
    ratio (see _weighted_coverage_ratio) — never to change which candidate
    a requirement prefers."""
    return req.candidates[0][1] if req.candidates else 0.0


def _weighted_coverage_ratio(covered: frozenset[str], role_requirements: Sequence[_Requirement]) -> float:
    """
    Confidence-WEIGHTED coverage ratio, not a raw concept COUNT (Step 3B
    correction): a real, messy schema routinely matches several
    business-kind concepts of wildly different confidence for one question
    (e.g. the question's actual, obvious ask at 0.99 alongside a handful of
    incidental single-word entity-name fragments at 0.55-0.65) — a raw
    count ratio lets covering ONE low-confidence noise concept tie with
    covering the ONE high-confidence real concept, silently discarding the
    question's own evidence about which concept actually mattered before
    scoring ever reaches the terminal-relevance tier. Weighting by each
    requirement's own already-computed confidence (never a second/
    conflicting scoring algorithm) fixes this generically: covering the
    question's most confidently-expressed business concept always
    contributes more than covering an incidental fragment, while covering
    MORE concepts still always scores at least as well as covering fewer
    (weights are all >= 0, so the sum only grows). Vacuous 1.0 when this
    role has no requirements at all — never a false penalty.
    """
    if not role_requirements:
        return 1.0
    total = sum(_requirement_weight(r) for r in role_requirements)
    if total <= 0.0:
        return 0.0
    covered_weight = sum(_requirement_weight(r) for r in role_requirements if r.concept_key in covered)
    return _clip01(covered_weight / total)


@dataclass(frozen=True)
class PlanScoreBreakdown:
    """Phase 6, Step 6H, Part 2 — deterministic, testable decomposition of
    one plan STATE's score, computed ONLY from schema identities and
    already-validated numeric/code-authored evidence (concept keys, object
    FQNs, confidences, edge/relationship types, counts) — never a prompt,
    credential, row value, or raw provider response. Every field here is
    used to build `sort_key` (see `_score_plan_state`'s own docstring for
    the exact tier meaning of each); this dataclass exists so ranking
    decisions are independently observable/testable field-by-field,
    instead of only visible as one opaque tuple or float.
    """

    mandatory_total_count: int
    mandatory_covered_count: int
    mandatory_coverage_ratio: float  # confidence-weighted (see _weighted_coverage_ratio) — the tier-1 dominance/ranking signal
    optional_total_count: int
    optional_covered_count: int
    optional_coverage_ratio: float
    structural_covered_total: int  # count of BUSINESS_REQUIRED + FILTER_OR_GROUPING concepts covered via real structural evidence
    structural_quality: float  # avg RAW (pre-anchor-boost) structural confidence across covered mandatory concepts
    min_direct_relevance_mandatory: float  # weakest-link relevance across covered mandatory concepts
    total_direct_relevance: float  # avg relevance across every covered BUSINESS_REQUIRED/FILTER_OR_GROUPING concept
    direct_relevance_by_concept: Mapping[str, float]  # per-concept relevance, coverage-role concepts only
    strongest_evidence_kind_by_concept: Mapping[str, str]  # concept_key -> "STRUCTURAL" | "ADVISORY", coverage-role concepts only
    connectivity_quality: float  # avg join-edge authority weight (a.k.a. joinability)
    advisory_contribution: float  # avg relevance of ADVISORY-origin terminal choices (bounded, low-priority)
    join_count: int
    object_count: int
    canonical_quality: float
    hub_quality: float
    covered_concept_keys: frozenset[str]  # BUSINESS_REQUIRED + FILTER_OR_GROUPING, structurally covered
    aggregate_score: float  # decade-weighted float, OBSERVABILITY ONLY — never used to decide ranking
    confidence: float
    sort_key: tuple  # the authoritative, fully lexicographic ranking key — see _score_plan_state


def _score_plan_state(
    state: _PlanState, requirements: Sequence[_Requirement], index: _JoinGraphIndex, max_selected_objects: int,
) -> PlanScoreBreakdown:
    """
    Phase 6, Step 6H, Part 3 — relevance-first plan ordering. Replaces the
    Step 3B/6G seven-tier `sort_key` with the corrected priority order
    Step 6H's own offline evaluation was commissioned to establish:

      1. mandatory (BUSINESS_REQUIRED) requirement completeness
      1b. optional (FILTER_OR_GROUPING/constraint) completeness — preserved
          from Step 3B's own "JOIN_HINT coverage must never outweigh
          business/constraint coverage" in its original relative position
      2. structural evidence QUALITY (not just presence) across covered
         mandatory requirements — a 0.97 exact-business-name match is not
         interchangeable with a 0.31 incidental column-token match, even
         though Step 6G's structural gate already treats both as "covered"
      3. weakest-link direct relevance across covered mandatory
         requirements — one badly-supported mandatory pick can no longer
         hide behind several strongly-supported ones
      4. total direct relevance across every covered requirement (the
         Step 3E/6G `relevance_term`, unchanged in its own computation)
      5. confirmed executable connectivity / join-edge quality (the
         existing `joinability_term`, unchanged)
      6. bounded advisory (model-semantic / view-authority) evidence — a
         real, but strictly LOW-priority, tie-breaker: because Python
         tuple comparison is exactly lexicographic, this tier can only
         ever matter once 1-5 are tied, regardless of its own magnitude,
         so "bounded" here needs no extra clamping beyond the existing
         MODEL_CANDIDATE_CONFIDENCE_CAP
      7. fewer joins (join_count ascending)
      8. fewer selected objects (object_count ascending) — Step 6H's own
         diagnosed defect: plan SIZE previously entered the comparison at
         a tier where it could still decide close relevance/connectivity
         near-ties; it is now strictly subordinate to tiers 1-6, so a
         smaller plan can win ONLY once every one of those already ties
      9. canonical/hub quality (kept, unchanged computation, from Step 3E)
         then the plan's own sorted object-FQN tuple as the final,
         unconditionally deterministic tie-break — never an arbitrary
         epsilon, never tuned to any specific case: identical inputs
         always produce an identical tuple, byte-for-byte.

    `covered`/`confidence` remain scoped to BUSINESS_REQUIRED +
    FILTER_OR_GROUPING only, exactly as before (Step 3B, requirement 5).
    """
    covered_business = _covered_business_concepts(state, requirements)
    covered_filter = _covered_filter_concepts(state, requirements)
    covered = covered_business | covered_filter
    business_requirements = [r for r in requirements if r.role == REQUIREMENT_ROLE_BUSINESS_REQUIRED]
    filter_requirements = [r for r in requirements if r.role == REQUIREMENT_ROLE_FILTER_OR_GROUPING]
    business_ratio = _weighted_coverage_ratio(covered_business, business_requirements)
    filter_ratio = _weighted_coverage_ratio(covered_filter, filter_requirements)

    if state.edges:
        joinability_term = _clip01(
            sum(_EDGE_AUTHORITY_WEIGHT.get(e.relationship_type, 0.5) for e in state.edges) / len(state.edges)
        )
    else:
        joinability_term = 1.0

    # Step 3B bug fix: confidence must be looked up PER (concept, object)
    # pair, never via a single global object->confidence map — the SAME
    # object routinely appears as a candidate for several DIFFERENT
    # concepts at very different confidences (e.g. a table's own strong
    # 0.99 entity-name match alongside a weak 0.4 identifier-column match
    # elsewhere), and a flattened map silently let whichever concept was
    # iterated LAST overwrite the relevance value for that object,
    # corrupting the terminal-relevance tier for exactly the cases this
    # tier exists to get right.
    confidence_by_concept_and_fqn = {
        (req.concept_key, fqn): conf for req in requirements for fqn, conf in req.candidates
    }
    raw_confidence_by_concept_and_fqn = {
        (req.concept_key, fqn): conf for req in requirements for fqn, conf in req.raw_confidence_by_fqn.items()
    }
    structural_fqns_by_concept = {req.concept_key: req.structural_fqns_lower for req in requirements}
    # Only BUSINESS_REQUIRED/FILTER_OR_GROUPING terminal choices earn
    # relevance credit — a JOIN_HINT (identifier) pick is structural
    # bridging evidence, never "the plan is about this object" (Step 3B).
    #
    # Phase 6, Step 6G, Part 3 — a terminal choice whose OWN candidacy for
    # this concept is model-semantic-only is excluded from this average
    # UNLESS the concept has no structural candidate at all (the one case
    # Step 4B1 deliberately preserves: "model evidence may make an object
    # available as a terminal candidate" when nothing structural exists to
    # prefer instead). Otherwise a business view whose validated model
    # inference merely happens to nominally touch an ALREADY-otherwise-
    # covered concept could still inflate `relevance_term` — the SAME
    # underlying issue `_covered_concepts`' structural gate fixes for
    # `business_ratio`/`filter_ratio`, applied consistently to the
    # relevance tier too: "structural evidence outranks model evidence"
    # must hold for every scoring tier a model-only candidate could reach,
    # not only the coverage-ratio one.
    coverage_concept_keys = {r.concept_key for r in requirements if r.role in _COVERAGE_ROLES}

    def _is_structural_choice(concept_key: str, fqn: str) -> bool:
        structural_set = structural_fqns_by_concept.get(concept_key, frozenset())
        return fqn in structural_set or not structural_set

    direct_relevance_by_concept: dict[str, float] = {}
    strongest_evidence_kind_by_concept: dict[str, str] = {}
    for concept_key, fqn in state.terminal_choice_by_concept.items():
        if concept_key not in coverage_concept_keys or (concept_key, fqn) not in confidence_by_concept_and_fqn:
            continue
        is_structural = fqn in structural_fqns_by_concept.get(concept_key, frozenset())
        if not (is_structural or _is_structural_choice(concept_key, fqn)):
            continue
        direct_relevance_by_concept[concept_key] = confidence_by_concept_and_fqn[(concept_key, fqn)]
        strongest_evidence_kind_by_concept[concept_key] = "STRUCTURAL" if is_structural else "ADVISORY"

    terminal_relevances = list(direct_relevance_by_concept.values())
    relevance_term = _clip01(sum(terminal_relevances) / len(terminal_relevances)) if terminal_relevances else 0.0

    # Part 3, tier 2 — structural evidence QUALITY: the raw (pre-anchor-
    # boost) structural confidence for each COVERED mandatory concept's own
    # terminal choice (covered ⟹ that choice is already known structural —
    # _covered_concepts' own gate guarantees it). Distinct from tier 3/4's
    # relevance, which additionally credits corroborating semantic/lexical/
    # example evidence blended in via the anchor's overall combined_score.
    structural_quality_values = [
        raw_confidence_by_concept_and_fqn[(ck, state.terminal_choice_by_concept[ck])]
        for ck in covered_business
        if ck in state.terminal_choice_by_concept
        and (ck, state.terminal_choice_by_concept[ck]) in raw_confidence_by_concept_and_fqn
    ]
    # Vacuous 1.0 ONLY when this question has NO mandatory requirements at
    # all (nothing to evaluate — matches `_weighted_coverage_ratio`'s own
    # "vacuous when role has no requirements" convention exactly). When
    # mandatory requirements DO exist but this plan covers none of them
    # structurally, the honest value is 0.0, never a fake "perfect" 1.0 —
    # tier 1 (business_ratio) already reflects that same 0.0 coverage, so
    # this is not a double penalty, it is the SAME fact reported at a
    # finer grain. A fake 1.0 here would be actively harmful beyond
    # sort_key: `_dominates`' "at least as good" check would then judge a
    # plan with genuinely covered, imperfectly-confident structural
    # evidence (say 0.6) as WORSE than a plan with ZERO real coverage
    # (fake 1.0), making the zero-coverage plan artificially undominatable.
    if not business_requirements:
        structural_quality = 1.0
        min_direct_relevance_mandatory = 1.0
    else:
        structural_quality = (
            _clip01(sum(structural_quality_values) / len(structural_quality_values))
            if structural_quality_values else 0.0
        )
        mandatory_relevances = [
            direct_relevance_by_concept[ck] for ck in covered_business if ck in direct_relevance_by_concept
        ]
        min_direct_relevance_mandatory = min(mandatory_relevances) if mandatory_relevances else 0.0

    # Part 3, tier 6 — bounded advisory contribution: the average relevance
    # of terminal choices whose OWN candidacy for their specific concept is
    # NOT structural (either no structural candidate exists at all, or one
    # exists elsewhere but this choice itself is advisory-origin). Never
    # able to outrank tiers 1-5 by construction (lexicographic tuple
    # position), so no additional clamping beyond the existing per-
    # candidate MODEL_CANDIDATE_CONFIDENCE_CAP is needed.
    advisory_relevances = [
        confidence_by_concept_and_fqn[(concept_key, fqn)]
        for concept_key, fqn in state.terminal_choice_by_concept.items()
        if concept_key in coverage_concept_keys and (concept_key, fqn) in confidence_by_concept_and_fqn
        and fqn not in structural_fqns_by_concept.get(concept_key, frozenset())
    ]
    advisory_contribution = _clip01(sum(advisory_relevances) / len(advisory_relevances)) if advisory_relevances else 0.0

    join_count = len(state.edges)
    object_count = len(state.selected_lower)

    canonical_values = [index.canonical_score_by_fqn_lower.get(fqn, 0.0) for fqn in state.selected_lower]
    canonical_term = _clip01(sum(canonical_values) / len(canonical_values)) if canonical_values else 0.0

    coverage_terminal_fqns = frozenset(
        fqn for concept_key, fqn in state.terminal_choice_by_concept.items() if concept_key in coverage_concept_keys
    )
    bridge_fqns = state.selected_lower - coverage_terminal_fqns
    if bridge_fqns and index.max_degree > 0:
        hub_risk = sum(index.degree_by_fqn_lower.get(fqn, 0) / index.max_degree for fqn in bridge_fqns) / len(bridge_fqns)
    else:
        hub_risk = 0.0
    hub_term = _clip01(1.0 - hub_risk)

    # Kept UNCHANGED (Step 3E's own weighting) — observability only, never
    # used to decide ranking (see `sort_key` below and this function's own
    # docstring). Adding the three new Part 2 quantities to this legacy
    # decade-weighted sum would only ever change it by a fixed offset
    # shared by every state scored in the same call, so it is left as-is
    # rather than risk silently changing the meaning of a float external
    # callers may already log/compare across runs.
    score = (
        business_ratio * _SCORE_WEIGHT_BUSINESS_COVERAGE
        + filter_ratio * _SCORE_WEIGHT_FILTER_COVERAGE
        + joinability_term * _SCORE_WEIGHT_JOINABILITY
        + relevance_term * _SCORE_WEIGHT_RELEVANCE
        + _clip01(1.0 - max(0, object_count - 1) / max(1, max_selected_objects)) * _SCORE_WEIGHT_SIZE
        + canonical_term * _SCORE_WEIGHT_CANONICAL
        + hub_term * _SCORE_WEIGHT_HUB
    )
    confidence = _clip01(business_ratio * relevance_term * joinability_term)

    # Phase 6, Step 6H, Part 3 — the corrected, fully lexicographic
    # ordering tuple; see this function's own docstring for the exact
    # rationale of every tier. Every component is either a bounded [0, 1]
    # float (negated so ascending tuple order means "better"), a small
    # non-negative integer count (left un-negated — ascending is already
    # "fewer/smaller is better"), or, only in the final position, a plain
    # tuple of the plan's own already-canonical object FQNs — a pure
    # function of WHICH objects are selected, never a hash, timestamp, or
    # anything derived from case-specific data. No epsilon/threshold of
    # any kind appears anywhere in this tuple.
    sort_key = (
        -business_ratio,
        -filter_ratio,
        -structural_quality,
        -min_direct_relevance_mandatory,
        -relevance_term,
        -joinability_term,
        -advisory_contribution,
        join_count,
        object_count,
        -canonical_term,
        -hub_term,
        tuple(sorted(state.selected_lower)),
    )

    return PlanScoreBreakdown(
        mandatory_total_count=len(business_requirements),
        mandatory_covered_count=len(covered_business),
        mandatory_coverage_ratio=business_ratio,
        optional_total_count=len(filter_requirements),
        optional_covered_count=len(covered_filter),
        optional_coverage_ratio=filter_ratio,
        structural_covered_total=len(covered_business) + len(covered_filter),
        structural_quality=structural_quality,
        min_direct_relevance_mandatory=min_direct_relevance_mandatory,
        total_direct_relevance=relevance_term,
        direct_relevance_by_concept=MappingProxyType(dict(direct_relevance_by_concept)),
        strongest_evidence_kind_by_concept=MappingProxyType(dict(strongest_evidence_kind_by_concept)),
        connectivity_quality=joinability_term,
        advisory_contribution=advisory_contribution,
        join_count=join_count,
        object_count=object_count,
        canonical_quality=canonical_term,
        hub_quality=hub_term,
        covered_concept_keys=covered,
        aggregate_score=score,
        confidence=confidence,
        sort_key=sort_key,
    )


def _score_state(
    state: _PlanState, requirements: Sequence[_Requirement], index: _JoinGraphIndex, max_selected_objects: int,
) -> tuple[tuple, float, float, frozenset[str]]:
    """Thin backward-compatible wrapper over `_score_plan_state` — returns
    (sort_key, score, confidence, covered_concept_keys) exactly as before
    Step 6H, so every EXISTING caller/test keeps working unchanged. New
    code (and Step 6H's own new tests) should prefer `_score_plan_state`
    directly for the full, named `PlanScoreBreakdown`."""
    breakdown = _score_plan_state(state, requirements, index, max_selected_objects)
    return breakdown.sort_key, breakdown.aggregate_score, breakdown.confidence, breakdown.covered_concept_keys


def _dominates(a: PlanScoreBreakdown, b: PlanScoreBreakdown) -> bool:
    """True when plan `a` Pareto-dominates plan `b`: at least as good on
    every one of the four named dimensions Part 4 specifies (mandatory
    coverage, structural support, relevance, connectivity), and
    MATERIALLY (beyond ANCHOR_AMBIGUITY_MARGIN — the SAME pre-existing,
    already-documented margin core.sqlservice.schema_linker and this
    module's own `_detect_plan_ambiguities`/`_preserve_ambiguous_concept_
    candidates` already use for "is this a genuinely different competing
    choice or just noise", never a new/arbitrary epsilon) better on at
    least one. Without this tolerance, two candidates separated by a
    trivial, statistically-meaningless difference (e.g. 0.80 vs 0.78
    confidence for an otherwise-identical concept) would eliminate one of
    them before `_detect_plan_ambiguities` ever gets a chance to report the
    real, near-tied ambiguity between them — silently undoing this same
    module's own established "preserve multiple plausible anchors when
    ambiguity is real" guarantee. Deliberately excludes object/join COUNT
    and canonical/hub quality — Part 4's own "never remove a stronger-
    relevance plan solely because it has one additional required bridge
    object" and "preserve distinct base-table and business-view plans when
    both remain non-dominated": a bridge object a smaller plan lacks is
    exactly the kind of difference that must NEVER, by itself, cause
    removal here; size only ever acts as the tie-break of last resort in
    the final `sort_key` ordering, never as a dominance criterion.

    Phase 6, Step 6J, Part 3 — COVERAGE-SIGNATURE COMPATIBILITY GUARD: `a`
    may only dominate `b` when `b.covered_concept_keys` is a SUBSET of
    `a.covered_concept_keys` (a covers at least everything b covers, and
    possibly more). Two states covering DIFFERENT, non-overlapping-superset
    required concepts (e.g. one interpretation's mandatory concept set vs a
    genuinely distinct one) are, by definition, never comparable under
    dominance — one cannot be numerically "better" at satisfying a concept
    it never even attempted to cover. Without this guard, two states that
    happen to score identically on the four numeric dimensions (plausible
    when each covers exactly one, differently-confident concept) could
    eliminate each other — or worse, a state covering a slightly higher-
    confidence but DIFFERENT concept could dominate one covering a lower-
    confidence but genuinely REQUIRED different concept — purely by
    coincidence of scale, never because it actually satisfies more of what
    the question asked. This is exactly Part 3's "apply Pareto dominance
    only within compatible coverage signatures" and "a lower-relevance
    required bridge must not be discarded merely because it lowers average
    relevance"."""
    if not (b.covered_concept_keys <= a.covered_concept_keys):
        return False
    at_least_as_good = (
        a.mandatory_coverage_ratio >= b.mandatory_coverage_ratio - ANCHOR_AMBIGUITY_MARGIN
        and a.structural_quality >= b.structural_quality - ANCHOR_AMBIGUITY_MARGIN
        and a.total_direct_relevance >= b.total_direct_relevance - ANCHOR_AMBIGUITY_MARGIN
        and a.connectivity_quality >= b.connectivity_quality - ANCHOR_AMBIGUITY_MARGIN
    )
    materially_better = (
        a.mandatory_coverage_ratio > b.mandatory_coverage_ratio + ANCHOR_AMBIGUITY_MARGIN
        or a.structural_quality > b.structural_quality + ANCHOR_AMBIGUITY_MARGIN
        or a.total_direct_relevance > b.total_direct_relevance + ANCHOR_AMBIGUITY_MARGIN
        or a.connectivity_quality > b.connectivity_quality + ANCHOR_AMBIGUITY_MARGIN
    )
    return at_least_as_good and materially_better


def _filter_pareto_dominated(
    scored: Sequence[tuple[_PlanState, PlanScoreBreakdown]],
) -> list[tuple[_PlanState, PlanScoreBreakdown]]:
    """
    Phase 6, Step 6H, Part 4 — removes a plan ONLY when another plan in the
    SAME candidate set is at least as good on all four of (mandatory
    coverage, structural support, relevance, connectivity) and strictly
    better on at least one (see `_dominates`). Runs once, over the
    already-bounded `combined_states` this call's beam search/single-
    object-baseline stage produced — never expands that set, never adds a
    new plan, purely a removal pass, so the existing maximum option count
    and memory/time bounds are unaffected (this is an O(n^2) pass over an
    already-small, already-bounded list).

    A plan with an ADDITIONAL required bridge object, or a genuinely
    different anchor family (e.g. a consolidating business view vs. the
    equivalent base-table join), is essentially never dominated under this
    rule, because the four compared dimensions never reference object/join
    count at all — exactly Part 4's own "never remove a stronger-relevance
    plan solely because it has one additional required bridge object" and
    "preserve distinct base-table and business-view plans when both remain
    non-dominated".
    """
    survivors: list[tuple[_PlanState, PlanScoreBreakdown]] = []
    for i, (state_i, breakdown_i) in enumerate(scored):
        dominated = False
        for j, (_state_j, breakdown_j) in enumerate(scored):
            if i == j:
                continue
            if _dominates(breakdown_j, breakdown_i):
                dominated = True
                break
        if not dominated:
            survivors.append((state_i, breakdown_i))
    return survivors


# ── Coverage-state frontier search (Phase 6, Step 3F, Part 3) ─────────────
#
# Step 3E's read-only production-schema verification proved the OLD flat "top-beam_width-
# states-by-raw-covered-count" cut was order-sensitive: adding real vector
# evidence reorders a requirement's own candidates (never the requirement
# SET itself — see Step 3F Part 1), and a popular requirement with many
# well-ranked candidates could produce enough same-coverage states to fill
# every beam slot, silently evicting a state that covered a DIFFERENT,
# less-populous requirement — a real regression (a measured drop on the exact
# same 15 questions, Step 3D/3E's own established baseline) traced to
# nothing but candidate order, not any actual schema/evidence deficiency.
#
# The frontier is now keyed by COVERAGE SIGNATURE — the frozenset of
# BUSINESS_REQUIRED/FILTER_OR_GROUPING concept_keys a state currently
# covers (see _coverage_signature). Two independent bounds replace the one
# flat beam_width:
#   max_frontier_signatures: how many DISTINCT coverage signatures are
#       tracked at all (breadth) — when this must be trimmed, signatures
#       covering MORE concepts are kept first (Part 3: "preserve at least
#       one state seeded from each materially plausible CORE candidate
#       while limits allow"), tied-broken by each signature's own best
#       _score_state sort_key (the SAME authoritative ranking the final
#       plan comparison uses — never a second, conflicting ranking).
#   max_states_per_signature: how many alternative (differently-connected,
#       deduplicated by selected-object set) states are kept for the SAME
#       signature (depth) — ranked and trimmed by the same sort_key.
# A flood of candidates for one requirement can therefore only ever
# inflate its OWN signature's bucket; it structurally cannot touch a
# different requirement's bucket, which is exactly the "must not let many
# high-vector candidates for one requirement eliminate every state
# covering another requirement" guarantee.
#
# Complexity: let T = total bounded terminal candidates (<=
# max_requirements_considered * max_terminal_candidates_per_requirement),
# R = requirements considered, S = max_frontier_signatures, D =
# max_states_per_signature, H = max_path_hops. One bounded BFS per DISTINCT
# terminal object costs O(V + E) restricted to an H-hop frontier (memoized
# — each terminal is ever BFS'd at most once per call, never once per
# state). Each round considers at most (S * D) carried-forward states times
# their own candidate-extension attempts, globally capped by
# max_considered_paths regardless of S/D — the SAME hard ceiling Step 3B
# already relied on, so this remains a BOUNDED APPROXIMATION (never an
# exhaustive enumeration over subsets) to the NP-hard minimum connected
# Steiner-tree problem, not an exact solver. Step 3B's single-object
# baseline evaluation (see _build_single_object_baseline_states) is
# unchanged: every linker anchor is still scored fairly against this
# search's own output, so a valid single-object answer is never solely
# dependent on surviving this frontier's own pruning.


def _coverage_signature(state: _PlanState, requirements: Sequence[_Requirement]) -> frozenset[str]:
    """The stable bitset/signature Part 3 asks for: every CORE
    (BUSINESS_REQUIRED) and CONSTRAINT (FILTER_OR_GROUPING) concept_key
    `state` currently covers — a frozenset, hashable, order-independent,
    and a pure function of `state.selected_lower` (never of HOW the state
    was reached or in what order candidates were tried)."""
    return frozenset(_covered_business_concepts(state, requirements) | _covered_filter_concepts(state, requirements))


def _anchor_family(state: _PlanState, business_required_keys: frozenset[str]) -> frozenset[str]:
    """Phase 6, Step 6J, Part 3 — the set of chosen objects across every
    BUSINESS_REQUIRED concept this state currently covers. Two states
    agreeing on every BUSINESS_REQUIRED terminal choice represent the SAME
    underlying business interpretation, even if they differ in bridge
    objects, FILTER_OR_GROUPING/JOIN_HINT choices, or edge topology."""
    return frozenset(
        fqn for key, fqn in state.terminal_choice_by_concept.items() if key in business_required_keys
    )


def _run_beam_search(
    requirements: Sequence[_Requirement], index: _JoinGraphIndex, limits: JoinPlanningLimits, t0: float,
) -> tuple[list[_PlanState], int, list[str], int]:
    limits_reached: list[str] = []
    considered_path_count = 0
    expanded_states_count = 0
    bfs_cache: dict[str, tuple[_BfsReach, bool]] = {}
    hop_ceiling_sink: list[bool] = []

    if not requirements:
        return [], 0, limits_reached, 0

    business_required_keys = frozenset(
        r.concept_key for r in requirements if r.role == REQUIREMENT_ROLE_BUSINESS_REQUIRED
    )

    def _rank_key(state: _PlanState) -> tuple:
        return _score_state(state, requirements, index, limits.max_selected_objects)[0]

    empty_state = _PlanState(selected_lower=frozenset(), selected_order=(), edges=(), terminal_choice_by_concept={})
    frontier: dict[frozenset[str], list[_PlanState]] = {frozenset(): [empty_state]}

    for req in requirements:
        # Phase 6, Step 6J, Part 3 — the PRACTICAL, deterministic gate: has
        # this search already expanded at least max_expanded_states states
        # (checked BEFORE the emergency wall-clock guard, so under normal
        # operation this is what actually stops the search, identically
        # regardless of CPU load). See LIMIT_MAX_EXPANDED_STATES' own
        # docstring.
        if expanded_states_count >= limits.max_expanded_states:
            limits_reached.append(LIMIT_MAX_EXPANDED_STATES)
            break
        # Emergency-only wall-clock circuit breaker (Part 3: "a hard
        # elapsed-time guard may remain only as an emergency failure
        # boundary") — a far larger default than before (core.config's
        # own SQL_JOIN_PLANNING_MAX_OPERATION_MS), meant to essentially
        # never fire while max_expanded_states/max_considered_paths are
        # doing their job; a typed limit is still recorded when it does.
        if (time.perf_counter() - t0) * 1000.0 > limits.operation_time_budget_ms:
            limits_reached.append(LIMIT_OPERATION_TIME_BUDGET)
            break

        # signature -> {selected_lower -> best state for that exact object
        # set} — the inner dict is the dedup Part 3 asks for ("deduplicate
        # equivalent selected-object/edge sets"), keyed by the SAME
        # selected_lower identity _extend_state_with_candidate already uses.
        new_frontier: dict[frozenset[str], dict[frozenset[str], _PlanState]] = defaultdict(dict)
        budget_exhausted = False
        # Phase 6, Step 6J, Part 3 — per-round anchor-family counter (see
        # LIMIT_MAX_PATHS_PER_ANCHOR_FAMILY / _anchor_family). Reset every
        # round: the cap bounds how much of THIS round's own expansion
        # budget one family may consume, never a cumulative lifetime cap.
        family_counts: dict[frozenset[str], int] = defaultdict(int)

        def _consider(candidate_state: _PlanState) -> None:
            nonlocal expanded_states_count
            expanded_states_count += 1
            sig = _coverage_signature(candidate_state, requirements)
            bucket = new_frontier[sig]
            key = candidate_state.selected_lower
            existing = bucket.get(key)
            if existing is None or len(candidate_state.edges) < len(existing.edges):
                bucket[key] = candidate_state

        for states in frontier.values():
            if budget_exhausted:
                break
            for state in states:
                _consider(state)  # carry-forward: this requirement may already be free-covered, or left uncovered
                for candidate_lower, _confidence in req.candidates:
                    if considered_path_count >= limits.max_considered_paths:
                        limits_reached.append(LIMIT_MAX_CONSIDERED_PATHS)
                        budget_exhausted = True
                        break
                    if expanded_states_count >= limits.max_expanded_states:
                        limits_reached.append(LIMIT_MAX_EXPANDED_STATES)
                        budget_exhausted = True
                        break
                    result = _extend_state_with_candidate(
                        state, req.concept_key, candidate_lower, index, bfs_cache,
                        limits.max_path_hops, limits.max_selected_objects, hop_ceiling_sink,
                    )
                    considered_path_count += 1
                    if result is None:
                        continue
                    new_state, _increment = result
                    family = _anchor_family(new_state, business_required_keys)
                    if family_counts[family] >= limits.max_paths_per_anchor_family:
                        limits_reached.append(LIMIT_MAX_PATHS_PER_ANCHOR_FAMILY)
                        continue  # this family already used its round budget -- try the NEXT candidate, never abort the round
                    family_counts[family] += 1
                    _consider(new_state)
                if budget_exhausted:
                    break

        # Bound DEPTH: within each signature, keep only the best
        # max_states_per_signature states (by the authoritative sort_key).
        trimmed: dict[frozenset[str], list[_PlanState]] = {}
        depth_overflowed = False
        for sig, bucket in new_frontier.items():
            states_here = sorted(bucket.values(), key=_rank_key)
            if len(states_here) > limits.max_states_per_signature:
                depth_overflowed = True
            trimmed[sig] = states_here[: limits.max_states_per_signature]
        if depth_overflowed:
            limits_reached.append(LIMIT_MAX_STATES_PER_SIGNATURE)

        # Bound BREADTH: cap the number of DISTINCT signatures tracked.
        # Broader coverage survives first (Part 3's "preserve at least one
        # state seeded from each materially plausible CORE candidate while
        # limits allow"); among equally-broad signatures, the one whose
        # best state ranks higher (same sort_key every other comparison in
        # this module uses) survives — never which candidate/order
        # happened to produce it.
        if len(trimmed) > limits.max_frontier_signatures:
            limits_reached.append(LIMIT_MAX_FRONTIER_SIGNATURES)
            ordered_signatures = sorted(
                trimmed.keys(), key=lambda sig: (-len(sig), _rank_key(trimmed[sig][0])),
            )
            keep = set(ordered_signatures[: limits.max_frontier_signatures])
            trimmed = {sig: states for sig, states in trimmed.items() if sig in keep}

        frontier = trimmed

        if any(len(s.selected_lower) >= limits.max_selected_objects for states in frontier.values() for s in states):
            limits_reached.append(LIMIT_MAX_SELECTED_OBJECTS)

        if budget_exhausted:
            break

    if hop_ceiling_sink:
        limits_reached.append(LIMIT_MAX_PATH_HOPS)

    all_states = [state for states in frontier.values() for state in states]
    return all_states, considered_path_count, sorted(set(limits_reached)), expanded_states_count


# ── Ambiguity detection (requirement 9) ────────────────────────────────────


def _detect_plan_ambiguities(
    primary_state: _PlanState, primary_score: float, other_states: Sequence[tuple[_PlanState, float]],
    requirements: Sequence[_Requirement], index: _JoinGraphIndex, max_records: int,
) -> tuple[PlanAmbiguityRecord, ...]:
    # Step 3B bug fix: same (concept, object)-keyed lookup as _score_state —
    # never a flattened global object->confidence map (see that function's
    # own comment for why a global map silently corrupts this value).
    confidence_by_concept_and_fqn = {
        (req.concept_key, fqn): conf for req in requirements for fqn, conf in req.candidates
    }
    coverage_concept_keys = {r.concept_key for r in requirements if r.role in _COVERAGE_ROLES}
    primary_covered = _covered_concepts(primary_state, requirements)
    records: list[PlanAmbiguityRecord] = []

    for other_state, other_score in other_states:
        if len(records) >= max_records:
            break
        if _covered_concepts(other_state, requirements) != primary_covered:
            continue
        for concept_key, primary_fqn in sorted(primary_state.terminal_choice_by_concept.items()):
            if concept_key not in coverage_concept_keys:
                continue  # Step 3B: a differing JOIN_HINT pick is never plan-level ambiguity
            alt_fqn = other_state.terminal_choice_by_concept.get(concept_key)
            if alt_fqn is None or alt_fqn == primary_fqn:
                continue
            primary_conf = confidence_by_concept_and_fqn.get((concept_key, primary_fqn), 0.0)
            alt_conf = confidence_by_concept_and_fqn.get((concept_key, alt_fqn), 0.0)
            if abs(primary_conf - alt_conf) > ANCHOR_AMBIGUITY_MARGIN:
                continue
            records.append(
                PlanAmbiguityRecord(
                    concept_key=concept_key,
                    description=(
                        f"concept {concept_key!r} is plausibly satisfied by either "
                        f"{index.fqn_by_lower.get(primary_fqn, primary_fqn)!r} or "
                        f"{index.fqn_by_lower.get(alt_fqn, alt_fqn)!r} — both connect the plan with similar confidence"
                    ),
                    primary_object_fqn=index.fqn_by_lower.get(primary_fqn, primary_fqn),
                    alternative_object_fqn=index.fqn_by_lower.get(alt_fqn, alt_fqn),
                    primary_plan_score=primary_score,
                    alternative_plan_score=other_score,
                )
            )
            if len(records) >= max_records:
                break
    return tuple(records)


# ── Single-object baseline plans (Step 3B, requirement 2/6) ───────────────


def _build_single_object_baseline_states(
    linking_result: SchemaLinkingResult, requirements: Sequence[_Requirement], index: _JoinGraphIndex,
    max_baselines: int,
) -> list[_PlanState]:
    """
    "Preserve valid single-object plans" / the "linker preservation
    contract": every schema_linker anchor (already ranked, admitted,
    vetted — linking_result.anchors is sorted by combined_score descending)
    PLUS every BUSINESS_REQUIRED requirement's own candidate objects becomes
    its own durable SINGLE_OBJECT _PlanState here — scored by the exact
    same formula as every multi-object beam state (see _score_state) and
    compared FAIRLY at the very end, in plan_schema_join_subgraph, never
    dependent on surviving this module's own beam-width pruning mid-search.
    Cheap: no BFS, only candidate-list membership checks. Deterministic:
    both source orderings (anchors by combined_score, requirement
    candidates by confidence) are already stable/sorted upstream.
    """
    pool_lower: list[str] = []
    seen: set[str] = set()
    for anchor in linking_result.anchors:
        fqn_lower = anchor.object_fqn.lower()
        if fqn_lower in index.eligible_fqns_lower and fqn_lower not in seen:
            seen.add(fqn_lower)
            pool_lower.append(fqn_lower)
    for req in requirements:
        if req.role != REQUIREMENT_ROLE_BUSINESS_REQUIRED:
            continue
        for fqn_lower, _conf in req.candidates:
            if fqn_lower not in seen:
                seen.add(fqn_lower)
                pool_lower.append(fqn_lower)
    pool_lower = pool_lower[:max_baselines]

    states: list[_PlanState] = []
    for fqn_lower in pool_lower:
        terminal_choice = {
            req.concept_key: fqn_lower
            for req in requirements
            if req.role in _COVERAGE_ROLES and any(c == fqn_lower for c, _conf in req.candidates)
        }
        states.append(_PlanState(
            selected_lower=frozenset({fqn_lower}), selected_order=(fqn_lower,), edges=(),
            terminal_choice_by_concept=terminal_choice,
        ))
    return states


# ── Missing/uncovered-requirement reason classification (requirement 8) ──


def _classify_uncovered_reason(
    req: _Requirement, index: _JoinGraphIndex, search_budget_signals_present: bool, covered_by_alternative: bool,
) -> UncoveredRequirementReason:
    """One BUSINESS_REQUIRED/FILTER_OR_GROUPING requirement's evidence-based
    reason for being uncovered in the chosen plan — never a vague "missing"
    and never conflating a search cutoff with an actual schema gap
    (requirement 8's own "never describe a cutoff as missing schema
    governance")."""
    if req.candidate_total_count == 0:
        return UncoveredRequirementReason(
            concept_key=req.concept_key, role=req.role, reason=REASON_CANDIDATE_EVIDENCE_INSUFFICIENT,
            detail="no eligible (Tier 1/2 graph) candidate object matched this concept at all",
        )
    if req.truncated_by_ceiling:
        return UncoveredRequirementReason(
            concept_key=req.concept_key, role=req.role, reason=REASON_SEARCH_BUDGET_EXHAUSTED,
            detail=(
                f"{req.candidate_total_count} candidate(s) existed but the "
                f"max_terminal_candidates_per_requirement safety ceiling truncated the list before this "
                f"concept's remaining candidates could be considered"
            ),
        )
    best_fqn = req.candidates[0][0]
    has_joinable = bool(index.joinable_adjacency.get(best_fqn))
    if not has_joinable:
        has_lineage = any(
            best_fqn in (e.from_object_fqn.lower(), e.to_object_fqn.lower()) for e in index.lineage_edges
        )
        if has_lineage:
            return UncoveredRequirementReason(
                concept_key=req.concept_key, role=req.role, reason=REASON_LINEAGE_ONLY_PATH,
                detail=f"{index.fqn_by_lower.get(best_fqn, best_fqn)!r}'s only confirmed relationship(s) are "
                       f"lineage-only (e.g. a view dependency) — never usable as a SQL join predicate",
            )
        return UncoveredRequirementReason(
            concept_key=req.concept_key, role=req.role, reason=REASON_NO_CONFIRMED_JOIN_PATH,
            detail=f"{index.fqn_by_lower.get(best_fqn, best_fqn)!r} has no confirmed relationship of any kind "
                   f"to the rest of the Tier 1/2 schema graph",
        )
    if search_budget_signals_present:
        return UncoveredRequirementReason(
            concept_key=req.concept_key, role=req.role, reason=REASON_SEARCH_BUDGET_EXHAUSTED,
            detail="a real joinable candidate exists, but a global search budget (paths/beam/time) bound "
                   "before this concept's connection to the chosen plan could be confirmed",
        )
    if covered_by_alternative:
        return UncoveredRequirementReason(
            concept_key=req.concept_key, role=req.role, reason=REASON_GENUINE_AMBIGUITY,
            detail="a joinable candidate exists and an alternative plan does cover this concept — the primary "
                   "plan scored higher overall without it, a genuine tradeoff rather than a graph gap",
        )
    return UncoveredRequirementReason(
        concept_key=req.concept_key, role=req.role, reason=REASON_CANDIDATE_EVIDENCE_INSUFFICIENT,
        detail="a joinable candidate exists but did not connect within the configured hop/size bounds to any "
               "other selected object",
    )


# ── State -> SchemaSubgraphPlan ────────────────────────────────────────────


def _state_to_plan(
    state: _PlanState, score: float, confidence: float, requirements: Sequence[_Requirement],
    index: _JoinGraphIndex, linking_result: SchemaLinkingResult, warnings: tuple[str, ...],
    *, search_budget_signals_present: bool = False, alternative_states: Sequence[_PlanState] = (),
    is_ambiguous: bool = False, breakdown: "PlanScoreBreakdown | None" = None,
) -> SchemaSubgraphPlan:
    covered = _covered_concepts(state, requirements)
    coverage_requirements = [r for r in requirements if r.role in _COVERAGE_ROLES]
    all_coverage_keys = {r.concept_key for r in coverage_requirements}
    uncovered = tuple(sorted(all_coverage_keys - covered))

    uncovered_business = sorted(
        r.concept_key for r in coverage_requirements
        if r.role == REQUIREMENT_ROLE_BUSINESS_REQUIRED and r.concept_key not in covered
    )
    alt_covered_keys: set[str] = set()
    for alt_state in alternative_states:
        alt_covered_keys |= _covered_concepts(alt_state, requirements)
    reasons = tuple(
        _classify_uncovered_reason(
            req, index, search_budget_signals_present, req.concept_key in alt_covered_keys,
        )
        for req in coverage_requirements
        if req.concept_key not in covered
    )

    anchor_fqns_lower = {a.object_fqn.lower() for a in linking_result.anchors}
    coverage_terminal_fqns_lower = frozenset(
        fqn for concept_key, fqn in state.terminal_choice_by_concept.items() if concept_key in all_coverage_keys
    )

    selected_sorted = tuple(sorted(index.fqn_by_lower.get(f, f) for f in state.selected_lower))
    join_edges_sorted = tuple(sorted(state.edges, key=lambda e: (e.from_object_fqn.lower(), e.to_object_fqn.lower())))

    plan_warnings = list(warnings)
    if len(state.selected_lower) == 1:
        only_fqn = next(iter(state.selected_lower))
        if not index.joinable_adjacency.get(only_fqn):
            plan_warnings.append(
                f"{REASON_NO_JOIN_REQUIRED}: {index.fqn_by_lower.get(only_fqn, only_fqn)!r} has no confirmed "
                f"joinable relationship, but this single-object plan needs none — any lineage-only "
                f"relationship it does have is irrelevant here"
            )

    if uncovered_business:
        plan_kind = PLAN_KIND_PARTIAL
    elif is_ambiguous:
        plan_kind = PLAN_KIND_AMBIGUOUS
    elif len(state.selected_lower) == 1:
        plan_kind = PLAN_KIND_SINGLE_OBJECT
    else:
        plan_kind = PLAN_KIND_JOINED_SUBGRAPH

    return SchemaSubgraphPlan(
        selected_object_fqns=selected_sorted,
        join_edges=join_edges_sorted,
        plan_kind=plan_kind,
        covered_requirement_keys=tuple(sorted(covered)),
        uncovered_requirement_keys=uncovered,
        uncovered_requirement_reasons=reasons,
        anchor_object_fqns=tuple(sorted(index.fqn_by_lower.get(f, f) for f in state.selected_lower if f in anchor_fqns_lower)),
        bridge_object_fqns=tuple(sorted(index.fqn_by_lower.get(f, f) for f in state.selected_lower if f not in coverage_terminal_fqns_lower)),
        score=score,
        confidence=confidence,
        warnings=tuple(plan_warnings),
        score_breakdown=breakdown,
    )


@dataclass(frozen=True)
class PlanningIndexPreparationResult:
    """Result of one prepare_schema_planning_indexes() call."""

    status: str  # STATUS_OK | STATUS_NOT_PREPARED | STATUS_INDEX_BUILD_FAILED
    linker_index_ready: bool
    join_graph_index_ready: bool
    eligible_object_count: int
    joinable_edge_count: int
    index_load_or_build_ms: float
    error: str | None


def prepare_schema_planning_indexes(
    semantic_catalog: SemanticCatalog, documents: Sequence,
) -> PlanningIndexPreparationResult:
    """
    Phase 6, Step 3E, Part 1, requirement 6 — build (or reuse an already-
    cached) BOTH core.sqlservice.schema_linker's own question-linking index
    AND this module's join-graph index for one (semantic_catalog,
    documents) pair, WITHOUT a question. Intended for later connection-time
    / background warm-up wiring (not wired into any route or service by
    this Step) so the FIRST real question against a schema never pays
    either index's cold-build cost out of its own request latency or
    operation_time_budget_ms.

    Populates the SAME bounded LRU caches link_question_to_schema()/
    plan_schema_join_subgraph() already use — a subsequent real call
    against the same catalog/documents becomes a pure cache hit. Because
    both indexes are pure functions of (semantic_catalog, documents) alone,
    warming them here changes ONLY subsequent latency, never any
    structural linking/planning decision (requirement 4/7).

    Pure and local: no database, network, embedding, or LLM/provider call;
    never uses, requires, or accepts a question.

    Returns STATUS_NOT_PREPARED (no index touched) for empty `documents`;
    STATUS_INDEX_BUILD_FAILED (typed, never a raw exception) if either
    index cannot be constructed safely; STATUS_OK otherwise.
    """
    if not documents:
        return PlanningIndexPreparationResult(
            status=STATUS_NOT_PREPARED, linker_index_ready=False, join_graph_index_ready=False,
            eligible_object_count=0, joinable_edge_count=0, index_load_or_build_ms=0.0,
            error="no documents supplied — nothing to prepare",
        )

    t0 = time.perf_counter()
    try:
        linker_index = _load_linker_index(semantic_catalog, documents)
        join_graph_index, _fingerprint = _load_join_graph_index(documents)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any index-construction failure is typed, never a raw crash
        index_load_or_build_ms = (time.perf_counter() - t0) * 1000.0
        return PlanningIndexPreparationResult(
            status=STATUS_INDEX_BUILD_FAILED, linker_index_ready=False, join_graph_index_ready=False,
            eligible_object_count=0, joinable_edge_count=0, index_load_or_build_ms=index_load_or_build_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
    index_load_or_build_ms = (time.perf_counter() - t0) * 1000.0

    return PlanningIndexPreparationResult(
        status=STATUS_OK, linker_index_ready=True, join_graph_index_ready=True,
        eligible_object_count=len(linker_index.eligible_fqns_lower),
        joinable_edge_count=len(join_graph_index.joinable_edges),
        index_load_or_build_ms=index_load_or_build_ms, error=None,
    )


def _select_diverse_alternatives(
    candidates: list[tuple["_PlanState", float, float, PlanScoreBreakdown]], max_alternative_plans: int,
) -> list[tuple["_PlanState", float, float, PlanScoreBreakdown]]:
    """
    Phase 6, Step 6J, Part 3 — the final alternative-plan slice must not
    let MANY states sharing the exact same coverage signature (e.g. many
    single-object baselines, one per candidate object, for the SAME sole
    BUSINESS_REQUIRED concept) consume the entire bounded slot budget
    before a genuinely DIFFERENT required concept — covered by only ONE
    or a few competing states — ever gets a single slot. `candidates` is
    already sorted by the authoritative sort_key (Part 3's own final
    ordering); this function is a pure SELECTION pass over that existing
    order — it never re-ranks, re-scores, or adds a plan.

    Pass 1: walk `candidates` in order, taking the first (= best-ranked)
    state for each DISTINCT `breakdown.covered_concept_keys` signature —
    "keep at least one non-dominated state per qualifying coverage
    signature until the configured signature bound is reached" (Part 3),
    applied to the OFFERED alternative slots specifically, mirroring the
    same signature-diversity guarantee `_run_beam_search` already gives
    its own internal frontier.
    Pass 2: if slots remain after every distinct signature got one
    representative, fill them by walking `candidates` again in the SAME
    order, now allowing repeats — never leaves a slot empty when a valid
    candidate exists.

    A no-op (returns `candidates` unchanged) whenever it already fits
    within `max_alternative_plans` — the common case.
    """
    if len(candidates) <= max_alternative_plans:
        return candidates

    selected: list[tuple["_PlanState", float, float, PlanScoreBreakdown]] = []
    selected_ids: set[int] = set()
    seen_signatures: set[frozenset[str]] = set()

    for item in candidates:
        if len(selected) >= max_alternative_plans:
            break
        signature = frozenset(item[3].covered_concept_keys)
        if signature in seen_signatures:
            continue
        selected.append(item)
        selected_ids.add(id(item[0]))
        seen_signatures.add(signature)

    for item in candidates:
        if len(selected) >= max_alternative_plans:
            break
        if id(item[0]) in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(id(item[0]))

    return selected


# ── Public entry point ─────────────────────────────────────────────────────


def plan_schema_join_subgraph(
    linking_result: SchemaLinkingResult,
    documents: Sequence,
    *,
    limits: JoinPlanningLimits = DEFAULT_JOIN_PLANNING_LIMITS,
) -> JoinPlanningResult:
    """
    Choose the smallest high-confidence CONNECTED set of tables/views that
    collectively covers `linking_result.requirements`' BUSINESS_REQUIRED
    (and, at lower priority, FILTER_OR_GROUPING) concepts — see
    REQUIREMENT_ROLE_* — using ONLY authoritative JOINABLE relationship
    edges (requirement 1) — never a lineage-only view-dependency edge,
    never a fabricated predicate. A single object that alone already
    satisfies every BUSINESS_REQUIRED concept is always evaluated as its
    own valid SINGLE_OBJECT plan candidate, fairly scored against whatever
    the multi-object search finds (Step 3B's own "linker preservation
    contract" — see _build_single_object_baseline_states). Pure and local:
    no database, embedding, or LLM/provider call; no SQL is generated,
    executed, or wired into production retrieval/orchestration here — that
    is a later Step.

    `documents` must be the SAME (or a superset of the) retrieval-document
    sequence `linking_result` was computed against — the search considers
    the COMPLETE Tier 1/2 confirmed-joinable graph built from it, not only
    `linking_result.anchors` (requirement 4: "bridge objects need no
    independent question similarity").

    Never uses expected/ground-truth objects of any kind — this function
    has no such parameter, by construction.
    """
    warnings: list[str] = []

    if linking_result.status != "OK":
        return JoinPlanningResult(
            status=STATUS_NO_REQUIREMENTS, primary_plan=None, alternative_plans=(), ambiguity_records=(),
            considered_candidate_count=0, considered_path_count=0, limits_reached=(),
            error=f"input SchemaLinkingResult.status={linking_result.status!r} (expected OK)",
            index_load_or_build_ms=0.0, join_search_ms=0.0, total_ms=0.0,
        )

    # Phase 6, Step 3E, Part 1 — index construction/lookup is timed and
    # failure-handled SEPARATELY from the search that follows: the search's
    # own operation_time_budget_ms clock (`t_search_start` below) starts
    # only once the index is ready, so a cold build can never consume the
    # per-question search budget and never changes the STRUCTURAL decision
    # a warm cache would have produced (requirements 3/4). A construction
    # failure returns a typed STATUS_INDEX_BUILD_FAILED result rather than
    # continuing to search over a partial/broken graph (requirement 5).
    t_index_start = time.perf_counter()
    try:
        index, _fingerprint = _load_join_graph_index(documents)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any index-construction failure is typed, never a raw crash
        index_load_or_build_ms = (time.perf_counter() - t_index_start) * 1000.0
        return JoinPlanningResult(
            status=STATUS_INDEX_BUILD_FAILED, primary_plan=None, alternative_plans=(), ambiguity_records=(),
            considered_candidate_count=0, considered_path_count=0, limits_reached=(),
            error=f"join-graph index construction failed: {type(exc).__name__}: {exc}",
            index_load_or_build_ms=index_load_or_build_ms, join_search_ms=0.0, total_ms=index_load_or_build_ms,
        )
    index_load_or_build_ms = (time.perf_counter() - t_index_start) * 1000.0

    t_search_start = time.perf_counter()
    pre_search_limits_reached: list[str] = []
    # Phase 6, Step 3E, Part 4 — candidate-specific lexical/semantic/example
    # relevance: core.sqlservice.schema_linker's AnchorCandidate.
    # combined_score already fuses catalog + semantic + lexical + example +
    # canonical evidence for every ADMITTED object; a terminal choice that
    # IS an anchor uses that real, evidence-aware score for its own
    # relevance term (see _score_state) instead of the catalog-only
    # RequirementCandidate confidence, so direct question evidence (not
    # just catalog matching) can actually outrank a merely-more-central/
    # canonical competitor at equal requirement coverage.
    anchor_combined_score_by_fqn_lower = {a.object_fqn.lower(): a.combined_score for a in linking_result.anchors}
    requirements, considered_candidate_count = _extract_requirements(
        linking_result, index, limits, warnings, pre_search_limits_reached,
        anchor_combined_score_by_fqn_lower=anchor_combined_score_by_fqn_lower,
    )

    if not requirements:
        join_search_ms = (time.perf_counter() - t_search_start) * 1000.0
        return JoinPlanningResult(
            status=STATUS_NO_REQUIREMENTS, primary_plan=None, alternative_plans=(), ambiguity_records=(),
            considered_candidate_count=0, considered_path_count=0, limits_reached=(),
            error="no matched concept requirements to plan for",
            index_load_or_build_ms=index_load_or_build_ms, join_search_ms=join_search_ms,
            total_ms=index_load_or_build_ms + join_search_ms,
        )

    if all(not r.candidates for r in requirements):
        join_search_ms = (time.perf_counter() - t_search_start) * 1000.0
        return JoinPlanningResult(
            status=STATUS_NO_CANDIDATES, primary_plan=None, alternative_plans=(), ambiguity_records=(),
            considered_candidate_count=0, considered_path_count=0, limits_reached=(),
            error="no requirement has any eligible candidate object present in the joinable schema graph",
            index_load_or_build_ms=index_load_or_build_ms, join_search_ms=join_search_ms,
            total_ms=index_load_or_build_ms + join_search_ms,
        )

    # Phase 6, Step 6J, Part 2 — relationship-aware bridge candidates,
    # generated from the SAME real requirements just extracted (never a
    # second retrieval pass, never an LLM call). Appended as additional,
    # JOIN_HINT-role _Requirement entries — pure connectivity evidence the
    # beam search may use to CONNECT real requirements, never counted as
    # mandatory coverage and never contributing to considered_candidate_
    # count (which stays scoped to real, question-matched candidates).
    bridge_requirements = _generate_bridge_requirements(requirements, index, limits, pre_search_limits_reached)
    bridge_candidate_count = sum(len(r.candidates) for r in bridge_requirements)
    if bridge_requirements:
        requirements = list(requirements) + bridge_requirements

    beam, considered_path_count, beam_limits_reached, expanded_states_count = _run_beam_search(
        requirements, index, limits, t_search_start,
    )
    limits_reached = pre_search_limits_reached + beam_limits_reached

    # Step 3B, requirement 2/6: every linker anchor (plus every
    # BUSINESS_REQUIRED requirement's own candidates) ALSO becomes its own
    # durable SINGLE_OBJECT candidate here — combined with the beam's own
    # output and scored FAIRLY, together, exactly once. A valid single-
    # object answer therefore survives even when the beam search itself
    # found nothing (or found something worse) for unrelated reasons.
    single_object_states = _build_single_object_baseline_states(
        linking_result, requirements, index, limits.max_single_object_baselines,
    )

    # Phase 6, Step 6H, Part 4 — dedup key is the plan's canonical object
    # set PLUS its canonical join-edge identity (an unordered set of
    # (from, to) FQN pairs — column pairs never affect which two OBJECTS
    # are joined, so they are deliberately excluded from the identity
    # itself), never object set alone: two states reaching the SAME
    # objects via a genuinely DIFFERENT edge topology are different plans,
    # not "the same plan, keep whichever has fewer edges" as before.
    #
    # Two states CAN legitimately collide on this key while disagreeing on
    # `terminal_choice_by_concept` — the beam search explores one
    # requirement's extension at a time, so it may reach an object and
    # record it as satisfying only the ONE concept it was extending
    # toward, even when that SAME object also happens to satisfy another
    # concept it never separately seeded a branch for (`_build_single_
    # object_baseline_states` computes the FULL set for every object up
    # front, so it never has this gap). Picking "whichever happened to be
    # inserted first" on a tie would silently discard real, already-proven
    # concept credit for the exact same object set — exactly the "a
    # required object's own extra structural coverage disappears" defect
    # class this step exists to close. The fix is a plain union of both
    # states' terminal-choice maps, never a preference for one over the
    # other and never a fabrication (both mappings were independently
    # derived from this object's own real candidacy).
    combined_by_key: dict[tuple[frozenset[str], frozenset[tuple[str, str]]], _PlanState] = {}
    for state in (*beam, *single_object_states):
        edge_identity = frozenset((e.from_object_fqn.lower(), e.to_object_fqn.lower()) for e in state.edges)
        key = (state.selected_lower, edge_identity)
        existing = combined_by_key.get(key)
        if existing is None:
            combined_by_key[key] = state
            continue
        kept_state = state if len(state.edges) < len(existing.edges) else existing
        merged_terminal_choice = {**existing.terminal_choice_by_concept, **state.terminal_choice_by_concept}
        if merged_terminal_choice != kept_state.terminal_choice_by_concept:
            kept_state = _PlanState(
                selected_lower=kept_state.selected_lower, selected_order=kept_state.selected_order,
                edges=kept_state.edges, terminal_choice_by_concept=merged_terminal_choice,
            )
        combined_by_key[key] = kept_state
    combined_states = list(combined_by_key.values())

    if not combined_states:
        join_search_ms = (time.perf_counter() - t_search_start) * 1000.0
        return JoinPlanningResult(
            status=STATUS_LIMITS_EXCEEDED, primary_plan=None, alternative_plans=(), ambiguity_records=(),
            considered_candidate_count=considered_candidate_count, considered_path_count=considered_path_count,
            limits_reached=tuple(sorted(set(limits_reached))),
            error="defensive limits were exhausted before any candidate plan could be built",
            index_load_or_build_ms=index_load_or_build_ms, join_search_ms=join_search_ms,
            total_ms=index_load_or_build_ms + join_search_ms,
        )

    # Phase 6, Step 6H, Part 2/3 — score every candidate state into its
    # full, named PlanScoreBreakdown once (never a raw tuple/float alone).
    scored_breakdowns: list[tuple[_PlanState, PlanScoreBreakdown]] = [
        (state, _score_plan_state(state, requirements, index, limits.max_selected_objects))
        for state in combined_states
    ]
    # Phase 6, Step 6H, Part 4 — remove only genuinely Pareto-DOMINATED
    # plans (see _dominates/_filter_pareto_dominated's own docstrings)
    # before final ranking; never expands `combined_states`, purely a
    # removal pass over the same already-bounded set.
    surviving_breakdowns = _filter_pareto_dominated(scored_breakdowns)
    pareto_dominated_count = len(scored_breakdowns) - len(surviving_breakdowns)
    # Deterministic final ordering — Phase 6, Step 6H, Part 3: `breakdown.
    # sort_key` is now ITSELF the complete, fully-lexicographic tuple
    # (mandatory completeness, ..., object count, canonical/hub quality,
    # and finally the plan's own sorted object-FQN tuple) — no further
    # terms need appending here; doing so would only ever re-apply the
    # SAME final tiebreak a second time.
    surviving_breakdowns.sort(key=lambda item: item[1].sort_key)

    primary_state, primary_breakdown = surviving_breakdowns[0]
    primary_score, primary_confidence = primary_breakdown.aggregate_score, primary_breakdown.confidence

    other_scored = [
        (state, breakdown.aggregate_score, breakdown.confidence, breakdown)
        for state, breakdown in surviving_breakdowns[1:]
        if state.selected_lower != primary_state.selected_lower
    ]
    other_scored_bounded = _select_diverse_alternatives(other_scored, limits.max_alternative_plans)
    if len(other_scored) > limits.max_alternative_plans:
        limits_reached.append(LIMIT_MAX_ALTERNATIVE_PLANS)

    ambiguity_records = _detect_plan_ambiguities(
        primary_state, primary_score, [(s, sc) for s, sc, _c, _b in other_scored_bounded], requirements, index,
        limits.max_ambiguity_records,
    )
    if len(ambiguity_records) >= limits.max_ambiguity_records:
        limits_reached.append(LIMIT_MAX_AMBIGUITY_RECORDS)

    # A real, global search-budget signal (as opposed to the defensive
    # safety ceilings, reported separately per-requirement via
    # `truncated_by_ceiling`) — used only to distinguish
    # REASON_SEARCH_BUDGET_EXHAUSTED from a genuine graph/scoring outcome
    # (requirement 8).
    search_budget_signals_present = bool(set(beam_limits_reached) & {
        LIMIT_MAX_CONSIDERED_PATHS, LIMIT_MAX_FRONTIER_SIGNATURES, LIMIT_MAX_STATES_PER_SIGNATURE,
        LIMIT_OPERATION_TIME_BUDGET, LIMIT_MAX_SELECTED_OBJECTS, LIMIT_MAX_PATH_HOPS,
    })

    primary_plan = _state_to_plan(
        primary_state, primary_score, primary_confidence, requirements, index, linking_result, (),
        search_budget_signals_present=search_budget_signals_present,
        alternative_states=[s for s, _sc, _c, _b in other_scored_bounded],
        is_ambiguous=bool(ambiguity_records),
        breakdown=primary_breakdown,
    )

    alternative_plans = tuple(
        _state_to_plan(state, sc, conf, requirements, index, linking_result, (), breakdown=breakdown)
        for state, sc, conf, breakdown in other_scored_bounded
    )

    uncovered_business_keys = {
        r.concept_key for r in requirements
        if r.role == REQUIREMENT_ROLE_BUSINESS_REQUIRED and r.concept_key in primary_plan.uncovered_requirement_keys
    }
    status = STATUS_OK if not uncovered_business_keys else STATUS_PARTIAL_COVERAGE
    error = None if status == STATUS_OK else (
        "; ".join(f"{r.concept_key} ({r.reason}): {r.detail}" for r in primary_plan.uncovered_requirement_reasons)
        or "partial coverage"
    )

    join_search_ms = (time.perf_counter() - t_search_start) * 1000.0
    return JoinPlanningResult(
        status=status,
        primary_plan=primary_plan,
        alternative_plans=alternative_plans,
        ambiguity_records=ambiguity_records,
        considered_candidate_count=considered_candidate_count,
        considered_path_count=considered_path_count,
        limits_reached=tuple(sorted(set(limits_reached))),
        error=error,
        index_load_or_build_ms=index_load_or_build_ms,
        join_search_ms=join_search_ms,
        total_ms=index_load_or_build_ms + join_search_ms,
        bridge_candidate_count=bridge_candidate_count,
        expanded_states_count=expanded_states_count,
        pareto_dominated_count=pareto_dominated_count,
    )
