"""
Full AI Query-Plan Control milestone — Stage 1 (single-table candidate
selection) + Stage 2 (date-range/status filters, calendar-grain GROUP BY,
date-target ORDER BY/Top-N) + Stage 3 (bare multi-table "show <entity> and
their <entity>" relationship lists, 2-3 entities): AI physical-plan model +
candidate selection, optionally filtered/grouped/ranked (single table) or
joined (relationship list).

attempt_ai_physical_plan() is the control-transfer entry point (routes to
_attempt_relationship_list_plan for relationship_intent=="list_with_related",
otherwise handles the single-entity Stage 1/2 shape itself). Neither path
resolves a table/column/filter/group/order/join itself: they reuse,
completely unchanged —

  - core.semantic.candidate_adjudicator.adjudicate_candidates  (Part 4's
    bounded "AI selects from real candidates" mechanism, already built) —
    called once per named entity (1 for Stage 1/2, 2-3 for Stage 3)
  - data.semantic_contract_service.gather_candidate_evidence   (Part 3's
    bounded real-schema evidence retrieval, already built)
  - data.semantic_contract_service.validate_ai_entity_hypotheses (rejects an
    AI entity name contradicted by the question's own deterministic terms)
  - core.semantic.concept_resolver.extract_query_intent        (deterministic
    COUNT/SUM/AVG/MIN/MAX/distinct/date_range/status_value/order detection)
  - data.query_planning_service.enrich_entity_count_selection,
    _find_date_column_with_hint, _date_order_hint_root,
    _compatible_status_filter, _resolve_calendar_grain_dimension,
    _CALENDAR_GRAIN_TERMS, _plan_joins                          (deterministic
    entity-key/date-column/status-column/grain-dimension/join-path
    resolution, already built — reused here for AI-forced tables exactly
    like their existing callers)
  - data.sql_planning_service.build_sql_plan                   (Part 6's
    deterministic validator — unchanged, not re-implemented — including its
    existing intent.type=="relationship_list" SELECT carve-out and
    all_joins_trusted join validation, Stage 3 populates their inputs only)
  - data.sql_generation_service.generate_sql                   (Part 7's
    pure SQL translator — unchanged, not re-implemented)

The only new code is the adapter that assembles AI-selected table(s) (and
their filters/grouping/ordering/joins) into the exact query_plan dict shape
data.query_planning_service.plan_business_query would have produced, so
build_sql_plan/generate_sql need no changes to consume it.

Scope (deliberately conservative, see Part 12/13). Single-entity path: no
requested_attributes; date/status filters and date-target ordering are
handled via the question's own deterministic extract_query_intent detection
(never the AI's raw sorting field); the AI's own filters list (business
attribute/operator/value terms, core.semantic.ai_interpreter's contract) is
additionally resolved via _resolve_ai_filters — but only against a real
BOOLEAN-typed column matched by name/label token overlap (AI Physical
Planner Coverage Expansion, Part 2) — any filter that isn't a genuine,
unambiguous boolean-column match still safely disqualifies rather than
guessing; at most one calendar-grain (year/month/quarter/week/day)
dimension, and an aggregation that is either absent (bare list/show
question) or COUNT (entity-count question, optionally grouped).
Relationship-list path: exactly 2-3 entities, no requested_attributes/
filters, and no aggregation/date/order/status signal at all (a filtered or
grouped join is Stage 4+ territory). Anything wider is out of scope for now
and disqualifies the attempt so the untouched legacy planner handles it — later
stages extend this SAME function and core.semantic.physical_plan.
PhysicalQueryPlan, they do not replace it.

Always returns a dict (never None) so the caller can log full Part 9
traceability regardless of outcome:
  {"attempted": bool, "valid": bool, "reason": str | None,
   "adjudication_call_count": int, "selected_table_fqns": list[str],
   "physical_plan": PhysicalQueryPlan | None,
   "query_plan": dict | None, "sql_plan": dict | None, "generated": dict | None}
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from core.semantic.physical_plan import PhysicalQueryPlan, PlanColumn, PlanFilter, PlanJoinEdge, PlanTable

logger = logging.getLogger(__name__)

# Live-calibrated against a real production database (see the check's own comment below for
# the full incident): every confirmed-correct Stage 1 table selection had
# name_score >= 0.5 (a real direct table-name token match); the one
# confirmed-wrong selection observed had name_score == 0.333 on a table with
# zero lexical relation to the entity term. Not the same mechanism/value as
# query_planning_service._NAME_RELEVANCE_GATE_FLOOR (0.25) -- that floor
# suppresses a generic authority BONUS for an otherwise-scored legacy
# candidate; this is a harder pass/fail gate on trusting an AI's autonomous,
# uncrosschecked table selection, so it is deliberately stricter.
_MIN_TRUSTED_NAME_SCORE = 0.5


def _vocabulary_trusts_candidate(term: str, candidate: dict) -> bool:
    """NEXT MILESTONE — REAL BUSINESS Q&A COMPLETION, Part 4 (event/domain
    noun grounding). True when data.vocabulary_bootstrap_service's own
    auto-generated, per-source vocabulary (real table-name/column-name-
    token/dictionary/entity-assignment evidence, computed once at bootstrap
    time -- never question-specific, never database-specific) already
    associates THIS exact term with THIS exact candidate table at
    MEDIUM+ confidence -- the same signal `_generated_vocabulary_bonus`
    (data.query_planning_service) already folded into the candidate's own
    `score` and surfaces in its `ranking_reasons` as "Generated vocabulary:
    '<term>' -> this table", promoted here into a trust signal in its own
    right for the STRICTER `_MIN_TRUSTED_NAME_SCORE` gate below.

    Live-confirmed against a real production database: "enrollment" shares zero lexical
    token with dbo.EDU_ClassSignups (name_score=0.3333, the table's real
    name has no "enroll..." root at all), so the plain lexical gate always
    refused it even though core.semantic.candidate_adjudicator's own AI
    adjudication already independently, confidently (0.8189) selected that
    EXACT table from real evidence (domain=Operations, 76 linked tables,
    thousands of rows, and -- this function's own signal -- a real auto-derived
    vocabulary row already connecting 'enrollment' to it). This function
    never changes WHICH table gets selected (adjudication has already run
    by the time it is consulted) -- it only asks whether a genuinely
    independent evidence source corroborates a selection already made,
    exactly like `_corroborate_weak_entity_candidate`'s relationship-hop
    corroboration does via a different evidence source.

    Deliberately NOT safe to use as a trust signal for a bare MEASURE term
    substituted in as a table-discovery driver (see attempt_ai_physical_
    plan's bare-measure-ownership fallback): live-confirmed against real
    the reference database that a short, common measure noun ("fee", "amount") collides with
    an unrelated table's own column vocabulary just as confidently and
    just as wrongly (e.g. 'fee' -> dnnuser.Roles, 'amount' -> dbo.
    ACHTransaction both carry a real, matching generated_vocabulary_term
    despite being the WRONG table) -- callers must restrict this signal to
    genuine entity/dimension/event-noun resolution (a term describing
    "what is this row"), never a bare aggregation-target column term."""
    # 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — a candidate this
    # module's own gather_candidate_evidence pipeline added because a
    # restricted VIEW's definition names it as that view's structural
    # FROM-clause source (data.semantic_contract_service.
    # _extract_lineage_root_table, re-verified against the real catalog
    # before ever reaching here) is corroborated exactly as genuinely as a
    # generated-vocabulary hit — a direct, code-verified structural fact
    # about the schema, not a guess. candidate["lineage_source_view"] is
    # only ever set by that one code path.
    if candidate.get("lineage_source_view"):
        return True

    generated_term = (candidate.get("generated_vocabulary_term") or "").strip().lower()
    if not generated_term:
        return False
    from data.query_planning_service import _tokens_near_match

    return _tokens_near_match(generated_term, (term or "").strip().lower())


def _vocabulary_trust_is_contested(selected_fqn: str, evidence: list[dict]) -> bool:
    """MULTI-HOP GROUPING GAP FIX — a vocabulary-corroboration hit
    (_vocabulary_trusts_candidate) is real evidence, but it is not
    SELECTIVE: it only proves the term relates to this table, never that
    it relates MORE than it relates to a competing table in the SAME
    evidence pool. Live-confirmed against a real production database: entity_name="student"
    adjudicated to dbo.EDU_Assessment_Student (a disconnected DISC
    personality-assessment table, deterministic_rank=2, zero schema
    relationships) over dbo.EDU_Student (deterministic_rank=1, score
    0.899, 4 real FK relationships incl. the join chain to EDU_Track/
    "program" -- the system's own persisted canonical Student entity)
    purely because BOTH tables' names happen to contain "student", so both
    carry a real generated_vocabulary_term hit and the lexical gate never
    told them apart.

    True only when the SELECTED candidate has NO schema relationships of
    its own AND a materially better-ranked (lower deterministic_rank),
    genuinely connected competitor already sits unused in the same
    evidence pool -- never invents or picks a table itself, it only
    refuses to let an unselective vocabulary hit silently outrank a
    strictly stronger, connected alternative. Callers fall through to the
    existing _corroborate_weak_entity_candidate retry (independent
    relationship-hop verification) instead of blind trust, exactly the
    same safe-refusal-or-real-recovery discipline already used everywhere
    else in this module.
    """
    selected = next((e for e in evidence if e.get("table_fqn") == selected_fqn), None)
    if selected is None or selected.get("relationships"):
        return False
    selected_rank = selected.get("deterministic_rank")
    if selected_rank is None:
        return False
    return any(
        item.get("table_fqn") != selected_fqn
        and item.get("deterministic_rank") is not None
        and item["deterministic_rank"] < selected_rank
        and item.get("relationships")
        for item in evidence
    )


# AI Schema Retrieval Gap fix, D4b-class relationship-corroboration retry
# (see _corroborate_weak_entity_candidate). Same BFS primitive/cap as
# data/semantic_retrieval_service.py's own _RELATIONSHIP_DISTANCE_MAX_HOPS --
# MULTI-HOP GROUPING GAP FIX widened this from 2 to 3 to actually MATCH that
# module's own bound instead of silently narrowing it. Live-confirmed against
# a real production database the previous cap of 2 excluded dbo.EDU_Track from "student"'s
# corroboration distance map entirely: the real, trusted join chain is
# Student -> ClassSignups -> Class -> Path, a verified 3 hops, the exact
# chain semantic_retrieval_service.py's own docstring already sizes 3 hops
# for. Still a bounded walk, not an unbounded graph traversal.
_CORROBORATION_MAX_HOPS = 3

# Compound Query Coverage, Part 8 — Multi-Dimension Grouping. A bounded cap,
# not a new limitation: data.sql_planning_service/data.sql_generation_service
# already iterate query_plan["dimensions"] generically (no hardcoded length),
# so this exists only to keep the join graph a question can force AI-side
# small and provable, matching this milestone's own "keep a reasonable
# bounded complexity limit" instruction -- not because more would be unsafe
# by construction, just unverified.
_MAX_MULTI_DIMENSIONS = 3

# AI Physical Planner Coverage Expansion, Part C — non-COUNT aggregation.
# Only these four: extract_query_intent's own closed aggregation vocabulary
# (core.semantic.concept_resolver) never returns anything outside COUNT plus
# these, so this set is exhaustive of what "aggregation is not None and not
# COUNT" can actually mean.
_NON_COUNT_AGGREGATIONS = frozenset({"SUM", "AVG", "MIN", "MAX"})

# Datatypes a SUM/AVG/MIN/MAX measure column is unambiguously safe to apply
# to. A MIN/MAX over a DATE/DATETIME column is a plausible future extension
# (schema evidence would fully support it) but is not attempted here --
# every live target for this stage (e.g. CourseFee) is numeric, and widening
# past what's evidenced is exactly what this milestone's own "do not force
# an answer" principle disallows.
_NUMERIC_DATA_TYPES = frozenset({"INTEGER", "DECIMAL"})


# Data Analyst Agent V1.5, Part 6/7 — semantic modifier synonym
# normalization. A business measure phrase spelled out in full ("maximum",
# "minimum", "average", ...) commonly names a physical column that
# ABBREVIATES the same modifier in its own CamelCase name (maxPanelCount,
# minRowCount, AvgRating, ...), while that column's OWN humanized
# business_label spells the modifier back out in full -- so a token set
# built from name+label ends up containing BOTH forms (e.g.
# {"max","maximum","panel","count"}), which a literal search-term token set
# ({"maximum","panel","count"}) can never exactly equal, AND which
# incorrectly overlaps with unrelated sibling columns using the alternate
# abbreviation ("minPanelCount" shares {"panel","count"} too). Live-
# confirmed against a real production database: "Maximum Panel Count"/"Maximum Row Count"/
# "Maximum Size" could not bind to dbo.survey_questions.maxPanelCount/
# maxRowCount/maxSize for exactly this reason, even though each column and a
# matching business_label both genuinely exist. Collapsing each side of a
# closed, standard SQL/business abbreviation pair to ONE canonical token
# before comparison fixes the general class of bug (not these three
# columns specifically) -- and makes the modifier itself the disambiguating
# signal against min/plain siblings, a real evidence gain rather than a
# compatibility shim. Deliberately a small, closed, generic vocabulary of
# ordinary abbreviation conventions -- never a table/column-specific
# mapping, and never touches any token outside this list.
_MODIFIER_SYNONYMS: dict[str, str] = {}
for _canon, _aliases in (
    ("max", ("maximum",)),
    ("min", ("minimum",)),
    ("avg", ("average",)),
    ("amt", ("amount",)),
    ("cnt", ("count",)),
    ("qty", ("quantity",)),
    ("num", ("number",)),
    ("pct", ("percent", "percentage")),
):
    _MODIFIER_SYNONYMS[_canon] = _canon
    for _alias in _aliases:
        _MODIFIER_SYNONYMS[_alias] = _canon
del _canon, _aliases, _alias


def _normalize_modifier_tokens(tokens: set[str]) -> set[str]:
    return {_MODIFIER_SYNONYMS.get(t, t) for t in tokens}


# ---------------------------------------------------------------------------
# Safe AI-Only Control milestone, Part 1 — Unsafe Legacy Takeover Prevention.
#
# Every disqualify `reason` this module can produce, classified so
# core.orchestrator.context_builder can decide whether legacy is safe to
# fall back to:
#
#   A — capability not yet implemented. A pure scope boundary (attempted is
#       always False for these -- no real evidence/adjudication ever ran),
#       not a judgment about THIS question's evidence. Legacy fallback may
#       still run here, exactly as it does today, until this stage is
#       extended to cover the shape.
#   B — semantic ambiguity / insufficient real evidence for a genuinely
#       in-scope shape. The AI's interpretation WAS tested against real
#       schema evidence and could not be safely resolved from it.
#   C — hallucinated or internally-contradictory AI interpretation. The
#       AI's own stated entities/terms don't hold together, or don't ground
#       in the question's own text -- live-confirmed root cause of a real
#       wrong-answer regression (REG-R-02: AI safely disqualified here,
#       legacy then silently took over and produced a materially wrong
#       answer from an uncoordinated guess).
#   D — deterministic validation failure. A plan WAS built but the
#       existing, unchanged validator/SQL generator rejected it.
#   E — technical/transient failure partway through AI-assisted resolution
#       (an adjudication call itself failed, or an internal consistency
#       backstop tripped) -- distinct from the AI simply never having been
#       consulted for this question at all (see "no_ai_interpretation"
#       below, classified A: that is normal, expected operation for any
#       question the upstream interpreter gate never routes to the AI, not
#       a failure this policy needs to guard against).
#   F — retrieval genuinely found no candidate evidence at all.
#
# B/C/D/E/F all mean the AI's own interpretation was actually tested against
# real evidence (or a real internal step) and something about it is unsafe
# to trust -- legacy must NOT silently take over and generate SQL from a
# different, uncoordinated guess in these cases. Only A (and the caller's
# own "ai_physical_planner_disabled" operational state, which never reaches
# this table at all) still permits legacy to run.
_REASON_CATEGORY: dict[str, str] = {
    # -- A: capability not yet implemented / AI not consulted at all --
    "no_ai_interpretation": "A",
    "not_single_entity": "A",
    "not_exactly_one_entity": "A",
    "requested_attributes_out_of_stage3_scope": "A",
    "requested_attributes_with_aggregation_out_of_scope": "A",
    "relationship_entity_count_out_of_stage3_scope": "A",
    "filters_out_of_stage3_scope": "A",
    "relationship_with_filters_or_aggregation_out_of_stage3_scope": "A",
    "non_count_aggregation_out_of_stage1_scope": "A",
    "dimensions_out_of_stage2_scope": "A",
    "grouping_without_count_aggregation_out_of_stage2_scope": "A",
    "non_date_ranking_out_of_stage2_scope": "A",
    "measure_term_required_for_aggregation_out_of_stage1_scope": "A",
    # 2026-08-24 "autonomous business-concept expansion" milestone —
    # corrected from "C" (hallucinated/contradictory interpretation) to "A".
    # This reason's own call site (attempt_ai_physical_plan's bare-list
    # leftover-term safety net, ~3521) is explicit in its surrounding
    # comment that a leftover attribute-shaped term here ("student contact
    # information", "instructor names", ...) is a genuine Stage 1 SCOPE
    # BOUNDARY — "data.query_planning_service's own D3 fix already handles
    # exactly this 'entity-relative attribute' shape... reached only via
    # the legacy fallback, never re-implemented here" — not a detected
    # hallucination. The "C" classification silently contradicted that
    # comment: it blocked the exact legacy fallback the code was written to
    # rely on, so this shape only ever worked when the AI interpreter
    # happened to populate requested_attributes on a given (non-
    # deterministic) call, and refused outright otherwise. Live-confirmed
    # against a real production database: "Show student contact information" refused with
    # this exact reason even though data.query_planning_service's own
    # generic business-concept expansion (this same milestone) resolves it
    # correctly when reached. relationship_extra_term_implies_attribute_
    # request (the relationship-list sibling, still "C" below) is
    # deliberately different — its own call site explicitly treats that
    # shape as a genuine hard refusal, not a scope boundary — left
    # unchanged.
    "extra_term_implies_attribute_request_out_of_stage1_scope": "A",
    # Part 7C -- Stage 1 has no implicit-activity-ranking capability of its
    # own yet; data.query_planning_service._try_build_implicit_ranking_plan
    # (legacy) already does, for this exact narrow shape only. See the call
    # site's own comment (measure_column_unresolvable branch above).
    "implicit_activity_measure_deferred_to_legacy_out_of_stage1_scope": "A",
    # -- B: semantic ambiguity / insufficient evidence --
    "clarification_required": "B",
    "no_safe_table_selection": "B",
    "dimension_unresolvable_out_of_stage2_scope": "B",
    "measure_column_unresolvable_out_of_stage1_scope": "B",
    "filters_out_of_stage1_scope": "B",
    "requested_attribute_unresolvable_out_of_stage1_scope": "B",
    "ranking_measure_unresolvable_out_of_stage2c_scope": "B",
    "cross_table_join_path_unresolvable_out_of_stage2c_scope": "B",
    "count_fanout_safety_refused_out_of_stage2e_scope": "B",
    "aggregate_fanout_safety_refused_out_of_stage2f_scope": "B",
    "relationship_entity_hypothesis_rejected": "B",
    "ai_entity_hypothesis_rejected": "B",
    "relationship_entities_collapsed_to_same_table": "B",
    "requested_attribute_unresolvable_out_of_stage3_scope": "B",
    "null_check_qualifier_unresolvable_out_of_stage2_scope": "B",
    "null_check_qualifier_ambiguous_subject_out_of_stage2_scope": "B",
    # -- C: hallucinated / internally-contradictory interpretation --
    "relationship_entity_not_grounded_in_question_text": "C",
    "relationship_extra_term_implies_attribute_request": "C",
    # -- D: deterministic validation failure --
    "sql_generation_produced_no_sql": "D",
    # -- E: technical/transient mid-process failure --
    "adjudication_unavailable_or_failed": "E",
    "selected_table_missing_from_evidence": "E",
    # -- F: retrieval insufficient --
    "no_candidate_evidence": "F",
}

# Reasons carrying dynamic detail (an f-string suffix) can't be exact-matched
# against the table above -- matched by prefix instead.
_REASON_PREFIX_CATEGORY: tuple[tuple[str, str], ...] = (
    ("deterministic_validation_failed", "D"),
    ("weak_name_relevance_evidence", "B"),
)


def classify_ai_failure(reason: str | None) -> str | None:
    """Maps a disqualify `reason` string (attempt_ai_physical_plan's own
    "reason" output field) to its A-F category. Returns None for reason=None
    (the plan was valid -- nothing to classify).

    An unrecognized reason -- e.g. a new disqualify check added later
    without updating the table above -- defaults to "B" (semantic
    ambiguity): the conservative choice, since this function exists
    specifically to decide whether legacy may safely take over, and
    silently permitting that for an unrecognized reason would reopen
    exactly the gap this policy closes. Only reasons explicitly known to be
    pure scope boundaries are classified "A"."""
    if reason is None:
        return None
    if reason in _REASON_CATEGORY:
        return _REASON_CATEGORY[reason]
    for prefix, category in _REASON_PREFIX_CATEGORY:
        if reason.startswith(prefix):
            return category
    return "B"


# ---------------------------------------------------------------------------
# INVESTIGATION AGENT HARDENING MILESTONE (Category-A Removal), Parts 1-3.
#
# classify_ai_failure's A-F letters answer "did AI physical planning even
# get consulted, and was its result trustworthy" (kept unchanged — retire-
# legacy-fallback policy still depends on it). This is a SEPARATE, orthogonal
# question: "regardless of letter, could BOUNDED DATABASE-EVIDENCE
# INVESTIGATION plausibly change this specific outcome?" A reason can be
# letter "A" (AI not fully consulted / a scope boundary) and STILL be
# INVESTIGATABLE_UNCERTAINTY (the boundary is about SHAPE-BUILDING capability
# for one narrow piece, e.g. "which column is the activity measure", not a
# blanket "this question type is unbuildable").
#
# Five dispositions (per this milestone's Part 2):
#   INVESTIGATABLE_UNCERTAINTY — the database MAY contain enough evidence;
#     planning simply hasn't gathered/adjudicated it yet. Eligible for the
#     Investigation Agent (core.orchestrator.investigation_agent).
#   GENUINE_CLARIFICATION — the user's own words are ambiguous; handled by
#     this module's/context_builder's existing clarification mechanisms,
#     never silently auto-picked.
#   GENUINE_UNSUPPORTED — a genuine SQL-construction/shape capability gap
#     (attempt_ai_physical_plan's Stage 1/2/3 literally has no code path to
#     BUILD this shape yet, e.g. a 5+-entity join, a non-COUNT aggregation
#     combination not yet implemented) or a hallucinated/self-contradictory
#     AI interpretation, or a technical/transient failure. No amount of
#     schema/relationship/value evidence changes any of these — they need a
#     future capability-expansion milestone (new SQL-shape code), not
#     investigation.
#   GOVERNANCE_SAFETY — PII/approval/fanout-safety/deterministic-validation
#     refusals. MUST NEVER be routed to investigation — see this milestone's
#     own strict "do NOT weaken governance/PII/trusted relationships" rule.
#   DATA_UNAVAILABLE — required data is confirmed empty/inaccessible
#     (handled entirely outside this reason table, by context_builder's own
#     deterministic pre-checks — see that module's _find_zero_row_entity_
#     term / current-state gate).
#
# Audited every reason string this module can produce (Investigation Agent
# Hardening milestone, Part 1) — see the milestone's own final-report audit
# table for the full file:function/condition/question-shapes-affected
# breakdown behind each entry below.
# ---------------------------------------------------------------------------
_INVESTIGATABLE_UNCERTAINTY = "INVESTIGATABLE_UNCERTAINTY"
_GENUINE_CLARIFICATION = "GENUINE_CLARIFICATION"
_GENUINE_UNSUPPORTED = "GENUINE_UNSUPPORTED"
_GOVERNANCE_SAFETY = "GOVERNANCE_SAFETY"
_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

_INVESTIGATION_DISPOSITION: dict[str, str] = {
    # -- promoted OUT of a dead-end category-A scope boundary: these two are
    # the milestone's own named proof cases. Both already attempt ONE bounded
    # evidence-based check inline (see _attempt_leftover_attribute_list_plan
    # and the implicit-activity-measure call site) before ever reaching this
    # reason — reaching it means that single attempt found nothing, not that
    # NO evidence-gathering strategy could ever help. genuinely uncertain,
    # not shape-incapable.
    "extra_term_implies_attribute_request_out_of_stage1_scope": _INVESTIGATABLE_UNCERTAINTY,
    "implicit_activity_measure_deferred_to_legacy_out_of_stage1_scope": _INVESTIGATABLE_UNCERTAINTY,
    # -- retrieval found zero candidates for the term as searched; a
    # different search strategy (synonym expansion, broader term, checking
    # generated vocabulary) may still find one. Not "no data exists" — just
    # "this one narrow retrieval attempt found nothing yet".
    "no_candidate_evidence": _INVESTIGATABLE_UNCERTAINTY,
    # -- genuine SQL-shape/capability gaps: attempt_ai_physical_plan's
    # Stage 1/2/3 code has no branch that BUILDS this shape yet, regardless
    # of what the database contains. No schema/relationship/value evidence
    # changes whether this module's SQL-plan-construction code can emit a
    # 5-entity join or a non-COUNT aggregation combination it was never
    # written to handle.
    "not_single_entity": _GENUINE_UNSUPPORTED,
    "not_exactly_one_entity": _GENUINE_UNSUPPORTED,
    "requested_attributes_out_of_stage3_scope": _GENUINE_UNSUPPORTED,
    "requested_attributes_with_aggregation_out_of_scope": _GENUINE_UNSUPPORTED,
    "relationship_entity_count_out_of_stage3_scope": _GENUINE_UNSUPPORTED,
    "filters_out_of_stage3_scope": _GENUINE_UNSUPPORTED,
    "relationship_with_filters_or_aggregation_out_of_stage3_scope": _GENUINE_UNSUPPORTED,
    "non_count_aggregation_out_of_stage1_scope": _GENUINE_UNSUPPORTED,
    "dimensions_out_of_stage2_scope": _GENUINE_UNSUPPORTED,
    "grouping_without_count_aggregation_out_of_stage2_scope": _GENUINE_UNSUPPORTED,
    "non_date_ranking_out_of_stage2_scope": _GENUINE_UNSUPPORTED,
    # -- the AI named no measure term at all for a SUM/AVG/MIN/MAX request —
    # there is no concrete term for investigation to search evidence for
    # (never "which table/column", but "the model supplied nothing to look
    # up"). Genuinely nothing for evidence-gathering to act on.
    "measure_term_required_for_aggregation_out_of_stage1_scope": _GENUINE_UNSUPPORTED,
    # -- the interpreter itself never produced a usable interpretation
    # (unavailable/timed out/malformed) — a technical/transient condition,
    # not a database-evidence gap.
    "no_ai_interpretation": _GENUINE_UNSUPPORTED,
    # -- hallucinated/self-contradictory AI interpretation (category C) —
    # investigation cannot adjudicate a claim that contradicts the
    # question's own text; there is nothing evidence-based to resolve.
    "relationship_entity_not_grounded_in_question_text": _GENUINE_UNSUPPORTED,
    "relationship_extra_term_implies_attribute_request": _GENUINE_UNSUPPORTED,
    # -- technical/transient (category E) — a retry-policy concern, out of
    # this milestone's scope; not an evidence gap investigation closes.
    "adjudication_unavailable_or_failed": _GENUINE_UNSUPPORTED,
    "selected_table_missing_from_evidence": _GENUINE_UNSUPPORTED,
    # -- deliberate SAFETY refusals (fanout risk, deterministic SQL/PII
    # validation) — MUST NEVER be converted into investigation merely to
    # try to route around a safeguard. Left exactly as governance already
    # decided.
    "count_fanout_safety_refused_out_of_stage2e_scope": _GOVERNANCE_SAFETY,
    "aggregate_fanout_safety_refused_out_of_stage2f_scope": _GOVERNANCE_SAFETY,
    "sql_generation_produced_no_sql": _GOVERNANCE_SAFETY,
}
_INVESTIGATION_DISPOSITION_PREFIX: tuple[tuple[str, str], ...] = (
    ("deterministic_validation_failed", _GOVERNANCE_SAFETY),
    ("weak_name_relevance_evidence", _INVESTIGATABLE_UNCERTAINTY),
)
# Every remaining category-B reason not listed above (dimension_
# unresolvable_out_of_stage2_scope, measure_column_unresolvable_out_of_
# stage1_scope, filters_out_of_stage1_scope, requested_attribute_
# unresolvable_out_of_stage1_scope/_stage3_scope, ranking_measure_
# unresolvable_out_of_stage2c_scope, cross_table_join_path_unresolvable_
# out_of_stage2c_scope, relationship_entity_hypothesis_rejected, ai_entity_
# hypothesis_rejected, relationship_entities_collapsed_to_same_table,
# null_check_qualifier_unresolvable_out_of_stage2_scope, null_check_
# qualifier_ambiguous_subject_out_of_stage2_scope, clarification_required)
# was ALREADY reachable by the Investigation Agent before this milestone —
# left unchanged (falls through to the default below) rather than re-
# litigated, to keep this pass scoped to genuinely removing dead-end
# category-A boundaries. clarification_required in particular stays
# investigatable-first rather than an immediate hard GENUINE_CLARIFICATION:
# the Investigation Agent's own NEEDS_CLARIFICATION outcome already covers
# "tried, still genuinely ambiguous" — strictly better than never trying.


def classify_investigation_eligibility(reason: str | None) -> str | None:
    """Maps a disqualify `reason` string to one of the five Part-2
    dispositions. Returns None for reason=None (nothing to classify).
    Unrecognized/unlisted reasons default to INVESTIGATABLE_UNCERTAINTY —
    the SAME conservative-toward-trying-harder default this module already
    uses elsewhere (classify_ai_failure defaults an unknown reason to "B",
    not "A") — but see the STRICT do-not list: this default is safe only
    because GOVERNANCE_SAFETY reasons are explicitly, exhaustively listed
    above rather than left to fall through a default."""
    if reason is None:
        return None
    if reason in _INVESTIGATION_DISPOSITION:
        return _INVESTIGATION_DISPOSITION[reason]
    for prefix, disposition in _INVESTIGATION_DISPOSITION_PREFIX:
        if reason.startswith(prefix):
            return disposition
    return _INVESTIGATABLE_UNCERTAINTY


def _disqualified(reason: str, *, attempted: bool = False, call_count: int = 0) -> dict:
    return {
        "attempted": attempted, "valid": False, "reason": reason,
        "adjudication_call_count": call_count, "selected_table_fqns": [],
        "physical_plan": None, "query_plan": None, "sql_plan": None, "generated": None,
    }


def _resolve_stage2_filters(
    source_id: int, question: str, fqn: str, ctx: dict, query_intent: dict, *,
    user_id: str | None = None, session=None,
) -> tuple[list[dict], list[dict], list[PlanFilter]]:
    """Date-range and status filters for the single AI-selected table, via
    the SAME deterministic primitives data.query_planning_service's own
    date/status filter step already uses (_find_date_column_with_hint,
    _date_order_hint_root, _compatible_status_filter) — never a fresh
    resolution mechanism. Mirrors that step's exact filter-entry shape and
    its exact "couldn't find a column the question demanded" warning types
    (date_column_not_found/status_column_not_found), so build_sql_plan's
    existing Step 6a hard-block (never silently drop a requested date/status
    qualifier) applies to this plan completely unchanged.

    Returns (resolved_filters, warnings, plan_filters) — resolved_filters
    and warnings feed query_plan["filters"]/["warnings"] directly;
    plan_filters is the same information as PhysicalQueryPlan.PlanFilter
    records for traceability.
    """
    from data.query_planning_service import (
        _compatible_status_filter, _date_order_hint_root, _find_date_column_with_hint,
    )

    graph_table_contexts = {fqn: ctx}
    resolved_filters: list[dict] = []
    warnings: list[dict] = []
    plan_filters: list[PlanFilter] = []

    date_range = query_intent.get("date_range")
    if date_range:
        date_col = _find_date_column_with_hint(graph_table_contexts, fqn, _date_order_hint_root(question))
        if date_col:
            date_fqn, col_name = date_col
            resolved_filters.append({
                "column": col_name, "operator": "BETWEEN",
                "value": [date_range["start"], date_range["end"]],
                "resolved": True, "table_fqn": date_fqn,
            })
            plan_filters.append(PlanFilter(
                table_fqn=date_fqn, column_name=col_name, operator="BETWEEN",
                value=[date_range["start"], date_range["end"]],
            ))
        else:
            warnings.append({
                "type": "date_column_not_found", "severity": "LOW",
                "message": (
                    f"A '{date_range['label']}' date range was requested but no date column "
                    f"was found on {fqn} — no date filter applied."
                ),
            })

    status_value = query_intent.get("status_value")
    if status_value:
        # NEXT MILESTONE, Part 1 — "not active"/"isn't open"/... must flip
        # the emitted operator/value here too: this is the AI physical
        # planner's own single-table filter-assembly path, live-confirmed
        # (see this module's own docstring) to be the path most real
        # questions actually take, so a fix only in the legacy
        # data.query_planning_service path would leave this exact bug live.
        # _compatible_status_filter is the shared choke point both planners
        # call — passing negated through here is the whole fix.
        #
        # LIVE the reference database VERIFICATION MILESTONE, Part 1 — the operator/value MUST
        # both come from _compatible_status_filter's own return, never be
        # re-derived independently from `status_negated` here: live-
        # confirmed against a real production database that re-deriving "!=" whenever negated
        # double-negates a boolean/FLAG column (IsActive already flipped
        # 1->0 by _typed_status_filter_value; ALSO flipping the operator to
        # "!=" produced "IsActive != 0", which SQL Server satisfies only for
        # IsActive = 1 — the opposite of "not active"). _compatible_status_
        # filter now returns the operator it actually decided on.
        status_negated = bool(query_intent.get("status_negated"))
        compat = _compatible_status_filter(
            fqn, ctx, status_value, source_id, negated=status_negated, user_id=user_id, session=session,
        )
        if compat:
            status_fqn, col_name, status_operator, typed_value = compat
            resolved_filters.append({
                "column": col_name, "operator": status_operator, "value": typed_value,
                "resolved": True, "table_fqn": status_fqn,
                "filter_kind": "status",
                "polarity": "negative" if status_negated else "positive",
            })
            plan_filters.append(PlanFilter(
                table_fqn=status_fqn, column_name=col_name, operator=status_operator, value=typed_value,
            ))
        else:
            warnings.append({
                "type": "status_column_not_found", "severity": "LOW",
                "message": (
                    f"A status filter ('{status_value}') was requested but no status column "
                    f"was found on {fqn} — no status filter applied."
                ),
            })

    return resolved_filters, warnings, plan_filters


_DATE_SHAPED_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DATE_COMPARISON_OPERATORS = frozenset({">=", "<=", ">", "<", "=", "between"})


def _is_redundant_date_filter(f: dict, query_intent: dict) -> bool:
    """True when an AI-supplied filter is a redundant restatement of a date
    range the deterministic core.semantic.concept_resolver.extract_query_intent
    path has ALREADY independently detected from the question's own text and
    will resolve on its own via _resolve_stage2_filters. Live-confirmed
    against a real production database, REG-L-05 "List students who started this year":
    extract_query_intent("this year") correctly resolves date_range on its
    own, but the AI interpreter ALSO, separately, redundantly restates the
    same "this year" signal as filters=[{"attribute": "start_date",
    "operator": ">=", "value": "2023-01-01"}] -- a date COMPARISON, a
    fundamentally different filter type than _resolve_ai_filters' deliberate
    BOOLEAN-column-only scope. Without this check, that redundant restatement
    alone hard-disqualifies a question the deterministic date-range path
    would otherwise already answer correctly -- a general defect for ANY
    question where the model duplicates an already-detected date signal this
    way, not specific to one question's wording. Only skips a filter when
    query_intent already carries a real date_range (i.e. the deterministic
    layer independently corroborates it) AND the filter's own value is
    date-shaped with a comparison operator -- a filter that doesn't meet
    both conditions is untouched and still resolved (or disqualified)
    exactly as before.

    The value may be a single date string (">=", "<=", ">", "<", "=") OR a
    two-element [start, end] list/tuple (operator "between") -- live-
    confirmed against a real production database, REG-C-02 "How many students started last
    year?": the model restates "last year" as filters=[{"attribute":
    "start_date", "operator": "between", "value": ["2022-01-01",
    "2022-12-31"]}], a list value the original single-string-only check
    didn't recognize as redundant at all (letting it fall through to the
    boolean-only resolver and hard-disqualify, same failure class as the
    single-value case). Deliberately does not compare the AI's restated
    dates against extract_query_intent's own computed range -- they can
    genuinely disagree (live-confirmed: the AI's ["2022-01-01","2022-12-31"]
    was wrong for "last year" relative to the current date, while
    extract_query_intent's own 2025-01-01..2025-12-31 was correct) and the
    deterministic range is always the one actually used downstream
    (_resolve_stage2_filters) -- this check only asks "is the AI's filter a
    restatement of the SAME kind of signal deterministic date-range
    handling already covers", never "is the AI's restatement accurate"."""
    if not query_intent.get("date_range"):
        return False
    if f.get("operator") not in _DATE_COMPARISON_OPERATORS:
        return False
    value = f.get("value")
    if isinstance(value, str):
        return bool(_DATE_SHAPED_VALUE_RE.match(value.strip()))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return all(isinstance(v, str) and _DATE_SHAPED_VALUE_RE.match(v.strip()) for v in value)
    return False


def _is_redundant_status_filter(f: dict, query_intent: dict) -> bool:
    """Sibling fix to _is_redundant_date_filter above, for the exact same
    class of bug against a different deterministic signal. Live-confirmed
    while verifying this milestone's other changes ("Total amount for
    active sales"): core.semantic.concept_resolver.extract_query_intent
    already independently detects and will resolve "active" as
    query_intent["status_value"] (via _resolve_stage2_filters/
    data.query_planning_service._compatible_status_filter, which correctly
    matches a real TEXT-typed status column), but the AI interpreter
    ALSO, separately, restates the identical signal as filters=
    [{"attribute": "status", "operator": "=", "value": "active"}] -- a
    BOOLEAN-flag-shaped filter _resolve_ai_filters' own deliberately
    conservative boolean-only scope can never match against a TEXT status
    column. Without this check, that redundant restatement alone hard-
    disqualifies (filters_out_of_stage1_scope, category B -- legacy
    blocked too, Part 1's Unsafe Legacy Takeover Prevention) a question
    the deterministic status path would otherwise already answer
    correctly -- a general defect for ANY question where the model
    duplicates an already-detected status signal this way, not specific to
    one question's wording. Only skips a filter when query_intent already
    carries a real status_value/status_values (i.e. the deterministic
    layer independently corroborates it) AND the filter's own value
    textually matches one of them -- a filter that doesn't meet both
    conditions is untouched and still resolved (or disqualified) exactly
    as before, and a GENUINELY boolean flag filter (e.g. "on a trial
    subscription", never detected by extract_query_intent's own fixed
    status vocabulary) is completely unaffected."""
    known_values = {
        v.lower() for v in (
            ([query_intent["status_value"]] if query_intent.get("status_value") else [])
            + list(query_intent.get("status_values") or [])
        )
    }
    if not known_values:
        return False
    value = f.get("value")
    return isinstance(value, str) and value.strip().lower() in known_values


def _boolean_column_matches(term_tokens: set[str], columns: list[dict]) -> list[dict]:
    """Shared predicate: real BOOLEAN-typed columns whose own name/label
    shares a token with term_tokens. Extracted so _resolve_ai_filters can
    apply the exact same matching rule to the primary table's own columns
    and (via _resolve_ai_filter_cross_table) a related table's columns,
    with no duplicated logic between the two."""
    from data.query_planning_service import _tokenize

    matches = []
    for col in columns:
        data_type = (col.get("schema") or {}).get("data_type")
        if data_type != "BOOLEAN":
            continue
        dic = col.get("dictionary") or {}
        label = dic.get("business_label") or ""
        col_tokens = set(_tokenize(col["column_name"])) | set(_tokenize(label))
        if term_tokens & col_tokens:
            matches.append(col)
    return matches


def _match_filter_columns(attribute_tokens: set[str], value_tokens: set[str], columns: list[dict]) -> list[dict]:
    """Two-tier boolean-column match: prefers the filter's VALUE tokens
    alone when they resolve to exactly one column -- a filter's value
    names the specific state being asked about ("trial"), while its
    attribute is often a broader category word ("subscription_type") that
    can lexically overlap several related boolean flags on the same table
    (live-confirmed against a real production database: dbo.EDU_ClassSignups carries
    IsTrial, IsSubscriptionPlan, AND IsSubscriptionInGoodStanding --
    matching on "subscription" alone is genuinely ambiguous there, while
    "trial" alone is not). Falls back to the combined attribute+value
    token set (this module's original matching rule) whenever the value
    alone matches zero columns -- so an attribute-only filter (no
    string value, e.g. value is a number/date) or a value too generic to
    match anything on its own still gets the original, broader search."""
    if value_tokens:
        value_matches = _boolean_column_matches(value_tokens, columns)
        if len(value_matches) == 1:
            return value_matches
    return _boolean_column_matches(attribute_tokens | value_tokens, columns)


def _fqn_exposed_to_fanout(fqn: str, steps: list[dict]) -> bool:
    """True when fqn is on the structural "one" side of any join step --
    meaning each fqn row could match MULTIPLE rows on the other side of
    that join, duplicating fqn's own rows in the joined result.

    data.query_planning_service._plan_one_join's own cardinality/
    fanout_risk fields are computed relative to the EDGE's intrinsically-
    declared FK direction (whichever table's column physically holds the
    foreign key is always "from_table") -- NOT relative to which table a
    caller is actually counting. Live-confirmed against a real production database,
    REG-S-02: the Student<->ClassSignups edge is declared
    from_table=ClassSignups (many side, the FK lives there), to_table=
    Student (one side), cardinality=MANY_TO_ONE, so
    data.semantic_layer_service._assess_fanout_risk reports LOW ("each
    ClassSignups row matches at most one Student row") -- true, but the
    AI physical planner's actual query is FROM Student JOIN ClassSignups,
    i.e. exactly the opposite direction: a given Student row CAN match
    several ClassSignups rows, which duplicates the COUNTED entity
    (Student) even though the edge-relative label says LOW. This helper
    re-derives the risk relative to fqn specifically, from the same
    cardinality/from_table/to_table fields _plan_joins already returns,
    rather than trusting the edge-relative fanout_risk label when fqn is
    the table actually being counted."""
    for step in steps:
        cardinality = step.get("cardinality")
        if cardinality == "MANY_TO_MANY":
            return True
        if cardinality == "ONE_TO_MANY" and step.get("from_table") == fqn:
            return True
        if cardinality == "MANY_TO_ONE" and step.get("to_table") == fqn:
            return True
    return False


def _is_non_authoritative_related_table(table_fqn: str) -> bool:
    """True when table_fqn's own name carries a temp/backup/archive/dated-
    copy naming signal -- reuses data.query_planning_service's own
    _NEGATIVE_NAME_TOKENS/_NEGATIVE_NAME_SUBSTRINGS/_DATED_COPY_RE (the same
    naming-authority vocabulary that module's own _score_table_authority and
    data.vocabulary_bootstrap_service already apply elsewhere) verbatim, not
    reinvented. A cross-table filter candidate must be a genuine, current
    table -- a dated snapshot/backup copy of the SAME schema (live-confirmed
    against a real production database: dbo.EDU_ClassSignups_03_19_2018 sitting alongside
    dbo.EDU_ClassSignups, both carrying IsTrial) is never a valid
    filter-evidence source, and including it would turn a genuinely
    unambiguous case into a false "ambiguous across related tables"
    refusal."""
    from data.query_planning_service import (
        _DATED_COPY_RE, _NEGATIVE_NAME_SUBSTRINGS, _NEGATIVE_NAME_TOKENS, _tokenize,
    )

    table_name = table_fqn.split(".")[-1]
    name_lower = table_name.lower()
    if set(_tokenize(table_name)) & _NEGATIVE_NAME_TOKENS:
        return True
    if any(kw in name_lower for kw in _NEGATIVE_NAME_SUBSTRINGS):
        return True
    return bool(_DATED_COPY_RE.search(table_name))


def _trusted_related_tables(
    source_id: int, user_id: str, primary_fqn: str, primary_ctx: dict, *, session=None,
) -> list[str]:
    """Bounded candidate related tables for primary_fqn: excludes non-
    authoritative naming variants (_is_non_authoritative_related_table) and
    requires a real, trusted (AUTO/APPROVED) join path
    (data.query_planning_service._plan_joins) from primary_fqn to each one.
    Shared by every cross-table column resolver in this module (filters,
    requested attributes) -- one implementation of "which related tables
    are safe to consider at all", reused for whatever column-matching
    predicate the caller applies to them.

    session, when given, memoizes THIS function's own return value per
    (source_id, primary_fqn) for the life of the request -- filter
    resolution, requested-attribute resolution, and compound-attribute
    resolution can each ask the same primary table's trusted related tables
    more than once in one question, and each related_fqn candidate here
    costs a full _plan_joins call (itself several sqlite connections via
    data.semantic_layer_service.analyze_join_quality/recommend_best_join_
    path, which take no session of their own). This does not change what
    counts as "trusted" -- _plan_joins/_related_table_names are still called
    exactly as before on a cache miss; a hit just reuses the same trusted
    set the first caller in this request already computed for this table.
    """
    from data.query_planning_service import _plan_joins, _related_table_names

    def _compute() -> list[str]:
        related_fqns = [
            t for t in _related_table_names(primary_ctx, primary_fqn)
            if not _is_non_authoritative_related_table(t)
        ]
        trusted: list[str] = []
        for related_fqn in related_fqns:
            join_check = _plan_joins(source_id, user_id, primary_fqn, {primary_fqn, related_fqn})
            steps = join_check.get("steps") or []
            if steps and all(step.get("path_found") for step in steps):
                trusted.append(related_fqn)
        return trusted

    from core.perf import stage_timer
    with stage_timer.measure("trusted_related_tables_lookup"):
        if session is None:
            return _compute()
        return session.get_or_compute(f"trusted_related_tables:{source_id}:{primary_fqn}", _compute)


def _resolve_ai_filter_cross_table(
    source_id: int, user_id: str, primary_fqn: str, primary_ctx: dict,
    attribute_tokens: set[str], value_tokens: set[str], *, session=None,
) -> dict | None:
    """Fallback when a filter's business term matches no boolean column on
    the primary/entity table -- e.g. "on a trial subscription" for a
    Student-grounded question, where the flag column (IsTrial) lives on a
    related enrollment/signup table, not Student itself (live-confirmed
    against a real production database: IsTrial lives on dbo.EDU_ClassSignups, a table
    related to, not the same as, dbo.EDU_Student).

    Never joins an arbitrary table: only considers tables primary_fqn
    already has a REAL, direct relationship to, restricted to ones with a
    trusted join path (_trusted_related_tables, shared with every other
    cross-table resolver in this module -- reused, not reinvented).

    Returns {"table_fqn", "ctx", "column"} for the SINGLE unambiguous
    boolean-column match across every trusted related table's columns
    combined, or None when there is no match, or the match is ambiguous
    across candidates -- never guesses (matches _resolve_ai_filters' own
    "exactly one, or refuse" discipline for the primary-table case)."""
    from data.business_knowledge_service import get_table_business_contexts_batch

    trusted_related = _trusted_related_tables(source_id, user_id, primary_fqn, primary_ctx, session=session)
    if not trusted_related:
        return None

    related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)

    candidates: list[dict] = []
    for related_fqn in trusted_related:
        related_ctx = related_contexts.get(related_fqn)
        if not related_ctx:
            continue
        for col in _match_filter_columns(attribute_tokens, value_tokens, related_ctx.get("columns") or []):
            candidates.append({"table_fqn": related_fqn, "ctx": related_ctx, "column": col})

    if len(candidates) != 1:
        return None  # no match, or ambiguous across related tables -- never guess

    return candidates[0]


# ---------------------------------------------------------------------------
# FINAL PRE-37 MILESTONE, Part 6/7/8/9/10/11 — Related-Value Filter
# resolution ("Show classes at the Richardson location"). A categorical
# VALUE filter is a fundamentally different shape than the BOOLEAN-flag
# filters _resolve_ai_filters/_resolve_ai_filter_cross_table already cover:
# the attribute term ("location") names a related DIMENSION/ENTITY, not a
# flag column, and the value ("Richardson") must be independently confirmed
# to actually exist — metadata alone (a column being TEXT and dimension-
# eligible) can never prove that on its own. See data.semantic_contract_
# service.validate_categorical_value_exists for the bounded, read-only,
# parameterized existence-check primitive and its own safety-boundary
# documentation (Part 8).
# ---------------------------------------------------------------------------


def _resolve_candidate_value_columns(
    source_id: int, user_id: str, primary_fqn: str, primary_ctx: dict,
    attribute_tokens: set[str], *, session=None,
) -> list[dict]:
    """Bounded candidate TEXT/categorical columns a filter VALUE could
    plausibly live in, narrowed by the filter's own ATTRIBUTE term
    ("location") via the SAME name/label token-overlap evidence class
    _match_filter_columns already uses for boolean filters — scoped to
    genuinely dimension-eligible TEXT columns instead of BOOLEAN ones, and
    searched across the primary table itself plus its trusted related
    tables (_trusted_related_tables — the SAME bounded, join-verified
    candidate set every other cross-table resolver in this module already
    uses) — never every TEXT column in the database (Part 9's own explicit
    "do not search every TEXT column" requirement).

    Returns [{"table_fqn","ctx","column"}, ...] — narrowing only; the
    caller must still independently VALIDATE the literal value actually
    exists in at least one of these before trusting any of them."""
    from data.query_planning_service import _is_dimension_column, _tokenize

    def _col_tokens(col: dict) -> set[str]:
        dic = col.get("dictionary") or {}
        return set(_tokenize(col["column_name"])) | set(_tokenize(dic.get("business_label") or ""))

    def _text_dimension_matches(ctx: dict) -> list[dict]:
        return [
            c for c in (ctx.get("columns") or [])
            if _is_dimension_column(c)
            and (c.get("schema") or {}).get("data_type") == "TEXT"
            and (attribute_tokens & _col_tokens(c))
        ]

    candidates: list[dict] = [
        {"table_fqn": primary_fqn, "ctx": primary_ctx, "column": c}
        for c in _text_dimension_matches(primary_ctx)
    ]

    trusted_related = _trusted_related_tables(source_id, user_id, primary_fqn, primary_ctx, session=session)
    if trusted_related:
        from data.business_knowledge_service import get_table_business_contexts_batch

        related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)
        for related_fqn in trusted_related:
            related_ctx = related_contexts.get(related_fqn)
            if not related_ctx:
                continue
            column_matches = _text_dimension_matches(related_ctx)
            matched_column_names = set()
            for c in column_matches:
                candidates.append({"table_fqn": related_fqn, "ctx": related_ctx, "column": c})
                matched_column_names.add(c["column_name"])

            # Live-confirmed against a real production database: "location" (the attribute
            # term) shares no token at all with any of dbo.EDU_Location's
            # OWN column names (City/State/Country/Location_1/Location_2)
            # -- "location" names the related TABLE's own business
            # identity, not any one column of it, a completely ordinary
            # phrasing ("classes at the Richardson LOCATION") this
            # column-name-only match can never see. When the attribute
            # term instead matches the related TABLE's own name (the same
            # table-identity evidence _is_non_authoritative_related_table's
            # sibling functions already tokenize table names for
            # elsewhere in this module), fall back to considering every
            # TEXT dimension column on that ONE already-trust-verified
            # table -- still never "every TEXT column in the database"
            # (Part 9), just this one join-verified table's own columns,
            # each still independently value-validated by the caller
            # before being trusted.
            table_name = related_fqn.split(".")[-1]
            if attribute_tokens & set(_tokenize(table_name)):
                for c in (related_ctx.get("columns") or []):
                    if (
                        c["column_name"] not in matched_column_names
                        and _is_dimension_column(c)
                        and (c.get("schema") or {}).get("data_type") == "TEXT"
                    ):
                        candidates.append({"table_fqn": related_fqn, "ctx": related_ctx, "column": c})

    return candidates


def _primary_table_text_dimension_fallback_candidates(
    primary_fqn: str, primary_ctx: dict, already_tried: set[tuple[str, str]],
) -> list[dict]:
    """AI-First System-Wide Intelligence milestone, Part 7D -- last-resort
    candidate widening for _resolve_ai_categorical_value_filter, used ONLY
    when the attribute-name-token-overlap candidates from
    _resolve_candidate_value_columns fail to live-confirm (empty, or every
    one of them fails the value-existence probe).

    Live-confirmed against a generic (non-the reference database) synthetic fixture: "Show
    enterprise accounts" gets filter attribute="type" from the AI
    interpreter (a plausible, generic near-synonym for a categorical
    classification column), but the real column is "AccountTier" --
    "type" and "tier" share no token at all. Worse, "type" DOES token-
    overlap a same-database but semantically UNRELATED column purely by
    coincidence (main.ActivityEvents.EventType tokenizes to {"event",
    "type"}), which wrongly satisfies _resolve_candidate_value_columns and
    then correctly fails its own live-value check -- so this fallback must
    trigger on "the confirmed set didn't come out to exactly one", not
    merely "candidates was empty", or a single coincidental false-positive
    match silently shadows the real column.

    Rather than hardcode a "type"/"tier" synonym (or any other closed
    word-pair -- the next question just uses a different near-synonym),
    widen to every remaining TEXT dimension column on the PRIMARY table
    itself only (never a related table here -- this only relaxes the
    ATTRIBUTE-NAME match, not the "which table" trust decision, which the
    caller already scoped correctly). The caller still independently
    live-confirms every one of these via the exact same
    validate_categorical_value_exists probe and the exact same "exactly
    one confirmed match, else refuse" discipline -- this never trusts a
    column by name alone."""
    from data.query_planning_service import _is_dimension_column

    return [
        {"table_fqn": primary_fqn, "ctx": primary_ctx, "column": c}
        for c in (primary_ctx.get("columns") or [])
        if _is_dimension_column(c)
        and (c.get("schema") or {}).get("data_type") == "TEXT"
        and (primary_fqn, c["column_name"]) not in already_tried
    ]


_MAX_VALUE_FILTER_CANDIDATES_CHECKED = 8


def _resolve_ai_categorical_value_filter(
    source_id: int, user_id: str, primary_fqn: str, primary_ctx: dict,
    attribute: str, value: str, *, session=None,
) -> dict | None:
    """Resolves one categorical VALUE filter (attribute="location",
    value="Richardson") to a real (table, column) pair whose literal
    contents were independently, live-confirmed to contain that exact
    value — never a guess from metadata/naming alone.

    Flow (Part 7/9/10): narrow candidate columns from the attribute term
    (_resolve_candidate_value_columns) -> validate the literal value's
    existence in each, bounded to _MAX_VALUE_FILTER_CANDIDATES_CHECKED
    probes (a plausible attribute-term match is already a small set in
    practice; this is a hard ceiling against a pathological table with many
    same-named columns across many related tables, not a normal case) ->
    require EXACTLY ONE confirmed match. Zero confirmed matches means the
    value genuinely isn't present anywhere plausible (safely disqualify,
    never invent). MORE than one confirmed match is genuine ambiguity
    (Part 10: e.g. a value that is simultaneously a real city name AND a
    real customer name) — also safely disqualified here rather than
    guessed; a future milestone could route this specific case to the
    existing CLARIFICATION_NEEDED flow instead of a bare refusal.

    Returns {"table_fqn","ctx","column_name","value"} on success, None
    otherwise. Never raises — every live-value probe
    (validate_categorical_value_exists) already fails closed (None) on any
    connection/query error, treated here as "not confirmed", same as a
    genuine non-match."""
    from data.query_planning_service import _tokenize

    attribute_tokens = set(_tokenize(attribute))
    if not attribute_tokens:
        return None

    candidates = _resolve_candidate_value_columns(
        source_id, user_id, primary_fqn, primary_ctx, attribute_tokens, session=session,
    )
    if not candidates:
        candidates = _primary_table_text_dimension_fallback_candidates(primary_fqn, primary_ctx, set())

    from data.semantic_contract_service import validate_categorical_value_exists

    def _check(cands: list[dict]) -> list[tuple[dict, str]]:
        hits: list[tuple[dict, str]] = []
        for cand in cands[:_MAX_VALUE_FILTER_CANDIDATES_CHECKED]:
            canonical = validate_categorical_value_exists(
                source_id, user_id, cand["table_fqn"], cand["column"]["column_name"], value, session=session,
            )
            if canonical is not None:
                hits.append((cand, canonical))
        return hits

    confirmed = _check(candidates)
    if not confirmed:
        # Part 7D -- the attribute-name-matched candidate(s) either don't
        # exist (already handled above) or failed live-value confirmation
        # (a coincidental token-overlap false positive, e.g. "type" ~
        # EventType on an unrelated table) -- widen to the primary table's
        # remaining TEXT dimension columns before giving up. Never widens
        # when the FIRST tier already found exactly one confirmed match
        # (the common, unambiguous case stays exactly as fast as before).
        already_tried = {(c["table_fqn"], c["column"]["column_name"]) for c in candidates}
        fallback_candidates = _primary_table_text_dimension_fallback_candidates(
            primary_fqn, primary_ctx, already_tried,
        )
        confirmed = _check(fallback_candidates)

    if len(confirmed) != 1:
        return None  # no match anywhere, or genuinely ambiguous -- never guess

    winner, canonical_value = confirmed[0]
    return {
        "table_fqn": winner["table_fqn"], "ctx": winner["ctx"],
        "column_name": winner["column"]["column_name"], "value": canonical_value,
    }


def _columns_matching_tokens(term_tokens: set[str], columns: list[dict]) -> list[dict]:
    """Generic (any-datatype) column-name/label token-overlap match -- the
    SAME evidence class _boolean_column_matches uses, without a datatype
    restriction, for resolving a requested OUTPUT attribute (Safe AI-Only
    Control milestone, Part 2): a requested attribute can be any real
    column (text, date, numeric, boolean...), unlike a measure (numeric
    only) or a boolean filter."""
    from data.query_planning_service import _tokenize

    matches = []
    for col in columns:
        dic = col.get("dictionary") or {}
        label = dic.get("business_label") or ""
        col_tokens = set(_tokenize(col["column_name"])) | set(_tokenize(label))
        if term_tokens & col_tokens:
            matches.append(col)
    return matches


def _resolve_requested_attribute(
    source_id: int, user_id: str, fqn: str, ctx: dict, attribute_term: str, *, session=None,
) -> dict | None:
    """Resolve one AI-named requested output attribute (core.semantic.
    ai_interpreter's `requested_attributes` -- a specific business FIELD
    the user wants shown, e.g. "email", "phone", "location", never a
    physical identifier by that module's own contract) to a real column on
    fqn or, failing that, one trusted related table (_trusted_related_
    tables -- the SAME bounded, join-verified candidate set
    _resolve_ai_filter_cross_table already uses for filters).

    Returns {"table_fqn", "column_name", "business_label", "score", "ctx"}
    (the "ctx" entry is the resolved table's own business-context dict,
    consumed by the caller for extra_tables bookkeeping -- popped before
    the rest of the dict is used as query_plan["dimensions"]' "selected"
    shape) when the term resolves to EXACTLY ONE real column anywhere in
    scope, or None otherwise -- callers must safely disqualify rather than
    guess (never a partial application of some attributes and a silent
    drop of others)."""
    from data.query_planning_service import _tokenize

    def _col_tokens(col: dict) -> set[str]:
        label = (col.get("dictionary") or {}).get("business_label") or ""
        return set(_tokenize(col["column_name"])) | set(_tokenize(label))

    term_tokens = set(_tokenize(attribute_term))
    if not term_tokens:
        return None

    local_matches = _columns_matching_tokens(term_tokens, ctx.get("columns") or [])
    if len(local_matches) > 1:
        # Live-confirmed: "class name" broadly token-overlaps ClassID/
        # ClassSignupCount/ClassTime/NextClass too (all share the "class"
        # token) as well as the real, exact answer ClassName -- same
        # exact-match-preferred discipline as _resolve_ai_measure_column/
        # _resolve_dimension_display_column/_resolve_relationship_
        # requested_attributes, applied here too so a genuinely
        # unambiguous requested attribute isn't refused as a false tie.
        exact = [c for c in local_matches if term_tokens == _col_tokens(c)]
        local_matches = exact if len(exact) == 1 else []
    if local_matches:
        if len(local_matches) != 1:
            return None  # ambiguous on the primary table itself -- never guess
        col = local_matches[0]
        dic = col.get("dictionary") or {}
        return {
            "table_fqn": fqn, "column_name": col["column_name"],
            "business_label": dic.get("business_label"), "score": 1.0, "ctx": ctx,
        }

    trusted_related = _trusted_related_tables(source_id, user_id, fqn, ctx, session=session)
    candidates: list[dict] = []
    if trusted_related:
        from data.business_knowledge_service import get_table_business_contexts_batch

        related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)

        for related_fqn in trusted_related:
            related_ctx = related_contexts.get(related_fqn)
            if not related_ctx:
                continue
            for col in _columns_matching_tokens(term_tokens, related_ctx.get("columns") or []):
                candidates.append({"table_fqn": related_fqn, "ctx": related_ctx, "column": col})

        if len(candidates) > 1:
            # NEXT MILESTONE, Part 14 — same exact-match-preferred discipline
            # as the local-table branch above, extended to the cross-table
            # pool (previously missing here). Live-confirmed against real
            # the reference database, REG-P-05 "Show instructor emails": dnnuser.Users.Email
            # (an EXACT {"email"} token match) and dbo.EDU_Mentoring_Program.
            # LastStatusUpdateEmail (a broader {"last","status","update",
            # "email"} superset match -- an unrelated status-tracking column
            # that merely contains the word "email") both matched via plain
            # overlap, hard-disqualifying a genuinely unambiguous requested
            # attribute as if it were a real tie. Only auto-resolves when
            # exactly one candidate has the strictly stronger exact-match
            # signal; a real cross-table tie (zero or more than one exact
            # match) still safely disqualifies exactly as before.
            exact = [c for c in candidates if term_tokens == set(_tokenize(c["column"]["column_name"])) | set(
                _tokenize((c["column"].get("dictionary") or {}).get("business_label") or "")
            )]
            candidates = exact if len(exact) == 1 else candidates

    if len(candidates) == 1:
        cand = candidates[0]
        dic = cand["column"].get("dictionary") or {}
        return {
            "table_fqn": cand["table_fqn"], "column_name": cand["column"]["column_name"],
            "business_label": dic.get("business_label"), "score": 1.0, "ctx": cand["ctx"],
        }
    if candidates:
        return None  # ambiguous across related tables -- never guess

    # Investigation Agent Hardening milestone (Category-A Removal), Part 7/9
    # — a THIRD, lowest-priority tier: a term the Investigation Agent (or an
    # earlier question on this SAME source) already validated via
    # data.vocabulary_bootstrap_service.record_investigated_term. This is
    # the one mechanism that lets investigation-learned evidence actually
    # change a requested-ATTRIBUTE resolution on replan — entity resolution
    # already consulted generated_business_vocabulary via _resolve_concept;
    # attribute resolution never did until now. Every candidate row here was
    # already schema-validated at write time (record_investigated_term only
    # ever stores a table_fqn/column_name pulled from evidence literally
    # gathered that session — see its own docstring) — this never trusts a
    # fresh, unverified identifier the way inventing one from scratch would.
    from data.vocabulary_bootstrap_service import get_generated_vocabulary
    from data.vocabulary_service import normalize_term

    vocab_rows = get_generated_vocabulary(source_id, session=session).get(normalize_term(attribute_term) or "", [])
    known_local_columns = {c["column_name"]: c for c in (ctx.get("columns") or [])}
    scoped_rows = [
        r for r in vocab_rows
        if r.get("column_name") and (
            (r.get("table_fqn") == fqn and r["column_name"] in known_local_columns)
            or (r.get("table_fqn") in (trusted_related or []))
        )
    ]
    if len(scoped_rows) == 1:
        row = scoped_rows[0]
        row_ctx = ctx
        if row["table_fqn"] != fqn:
            from data.business_knowledge_service import get_table_business_context as _get_ctx
            row_ctx = _get_ctx(source_id, user_id, row["table_fqn"]) or ctx
        return {
            "table_fqn": row["table_fqn"], "column_name": row["column_name"],
            "business_label": None, "score": float(row.get("confidence_score") or 0.6), "ctx": row_ctx,
        }
    return None  # no match anywhere, ambiguous, or no investigation-learned mapping -- never guess


_SINGLE_LETTER_ABBREVIATION_RE_CACHE: dict[str, "re.Pattern[str]"] = {}

# LOCATION/STUDENT-NAME/ENROLLMENT/DATA-QUALITY MILESTONE, Part 5/6 — root
# cause of "Show student names" refusing before any table selection:
# _compound_attribute_column_group only ever matched the single-letter-
# abbreviation shape (InstructorFname/InstructorLname -- CamelCase
# tokenization can't split "Fname" at all, so it stays one opaque token
# sharing no tokens with "name"). It never matched the PROPER-CamelCase
# shape (dnnuser.Users.FirstName/LastName) even though this exact function's
# own docstring claims to cover it -- FirstName/LastName tokenize cleanly to
# ["first","name"]/["last","name"], two separate tokens, which the single-
# letter regex (anchored to ONE opaque token) can never match. Live-
# confirmed: dnnuser.Users has FirstName/LastName (and DisplayName) but
# _compound_attribute_column_group('name', ...) returned None regardless.
#
# Fix: a second, independent, still-narrow signal — a column whose
# tokenized name is EXACTLY [qualifier, term] or [term, qualifier], where
# qualifier is drawn from a small, generic, closed set of ORDINAL/
# POSITIONAL part-words (first/last/middle) that name a PART of a compound
# concept in ANY domain (a name, an address, a phone number) — never a
# business-entity/domain word like "location" (which is what the reverted
# broader "any overlap" signal above was correctly rejected for: Location-
# Name/LocationCode are competing ALTERNATIVES, not complementary parts).
# "first"/"last"/"middle" carry that COMPOSITIONAL meaning independent of
# which concept follows them — first/last NAME, first/last ADDRESS LINE,
# first/last PAYMENT — never hardcoded to "name" specifically, so this
# generalizes to "customer name", "employee name", "driver name", etc.
# whenever a schema uses the same First/Last convention, with no table/
# column name anywhere in this list.
_COMPOUND_PART_QUALIFIERS = frozenset({"first", "last", "middle"})


def _matches_qualified_compound_part(term: str, column_name: str) -> bool:
    from data.query_planning_service import _tokenize

    tokens = _tokenize(column_name)
    if len(tokens) != 2:
        return False
    a, b = tokens
    return (
        (a in _COMPOUND_PART_QUALIFIERS and b == term)
        or (b in _COMPOUND_PART_QUALIFIERS and a == term)
    )


def _compound_attribute_column_group(term: str, columns: list[dict]) -> list[dict] | None:
    """The actual evidence rule behind Part 11/12/13's one-term-to-multiple-
    columns resolution, applied to one table's own column list: a column-
    name token that is the term prefixed by exactly one extra letter (e.g.
    "fname"/"lname" for term "name") -- for columns like InstructorFname/
    InstructorLname that CamelCase tokenization cannot connect to the bare
    term at all ("Fname" has no internal lowercase->uppercase boundary, so
    it tokenizes as ONE opaque token sharing zero tokens with "name").

    A broader "any 2+ columns whose tokens merely OVERLAP the term" signal
    was tried and deliberately reverted: live-confirmed against this
    module's own existing test fixture (test_requested_attribute_ambiguous_
    falls_back) that it produces a real false positive -- LocationName/
    LocationCode both overlap "location" exactly the same shape FirstName/
    LastName overlap "name", but they are competing ALTERNATIVE
    interpretations of "location" (pick one), not complementary sub-parts
    of one compound concept the way First/Last are sub-parts of "name".
    Nothing in the available column-level evidence (name, label, type)
    distinguishes these two cases without a curated qualifier vocabulary
    (exactly the hardcoding this milestone forbids), so the broader signal
    was removed rather than risk silently merging a genuine ambiguity into
    a wrong multi-column answer -- per Part 12's own explicit instruction
    ("do NOT assume every Fname + Lname pair means name unless evidence/
    entity context supports it"), the single-letter-abbreviation signal is
    the only one specific enough to trust unsupervised.

    Requires TWO OR MORE independently-matching columns (never one) as the
    real evidence bar -- a lone one-off abbreviation match is exactly as
    weak as any other single-token coincidence and is left to the existing
    single-column resolver's own "no match" refusal instead. Returns the
    matched column list (2+, table's own order) or None.

    LOCATION/STUDENT-NAME/ENROLLMENT/DATA-QUALITY MILESTONE, Part 5/6 — a
    second, independent evidence signal now also qualifies a column: its
    tokenized name is exactly [qualifier, term] or [term, qualifier] where
    qualifier is one of the small, generic, closed compositional part-words
    (first/last/middle — see _matches_qualified_compound_part's own
    docstring for why this doesn't reopen the LocationName/LocationCode
    false-positive the abbreviation-only design deliberately avoided)."""
    from data.query_planning_service import _tokenize

    pattern = _SINGLE_LETTER_ABBREVIATION_RE_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(r"^[a-z]" + re.escape(term) + r"$")
        _SINGLE_LETTER_ABBREVIATION_RE_CACHE[term] = pattern

    matches = [
        col for col in columns
        if any(pattern.match(tok) for tok in _tokenize(col["column_name"]))
        or _matches_qualified_compound_part(term, col["column_name"])
    ]
    return matches if len(matches) >= 2 else None


def _resolve_compound_requested_attribute(
    source_id: int, user_id: str, fqn: str, ctx: dict, attribute_term: str, *, session=None,
) -> list[dict] | None:
    """FINISH INTERACTIVE AI Q&A WIRING milestone, Part 11/12/13 — bounded
    one-business-term-to-MULTIPLE-real-columns resolution. Confirmed live
    against a real production database as the exact root cause of REG-P-02/P-03/REG-R-04-
    adjacent failures: "name" resolves to InstructorFname/InstructorLname
    (single-letter-abbreviation signal, on the entity's own table) or to
    FirstName/LastName on a related table (dnnuser.Users, joined from
    dbo.EDU_Student -- the broad-tie signal, cross-table). Tries the
    entity's own table first (_compound_attribute_column_group), then
    falls back to one trusted related table (_trusted_related_tables --
    the SAME bounded, join-verified candidate set _resolve_requested_
    attribute's own cross-table fallback already uses), mirroring that
    function's exact reuse discipline. A compound group is only ever drawn
    from ONE table (never columns scattered across different related
    tables) -- each column belongs to the SAME validated entity/table,
    which is what makes it a coherent group rather than an arbitrary
    collection.

    Returns a list of 2+ {"table_fqn","column_name","business_label",
    "score","ctx"} dicts or None when no table has a qualifying group --
    callers must safely refuse rather than guess a partial or single-
    column stand-in for a genuinely compound request."""
    term = (attribute_term or "").strip().lower()
    if len(term) < 3:
        return None

    def _as_dicts(table_fqn: str, table_ctx: dict, columns: list[dict]) -> list[dict]:
        return [
            {
                "table_fqn": table_fqn, "column_name": col["column_name"],
                "business_label": (col.get("dictionary") or {}).get("business_label"),
                "score": 1.0, "ctx": table_ctx,
            }
            for col in columns
        ]

    local_group = _compound_attribute_column_group(term, ctx.get("columns") or [])
    if local_group is not None:
        return _as_dicts(fqn, ctx, local_group)

    trusted_related = _trusted_related_tables(source_id, user_id, fqn, ctx, session=session)
    if not trusted_related:
        return None

    from data.business_knowledge_service import get_table_business_contexts_batch

    related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)
    qualifying: list[tuple[str, dict, list[dict]]] = []
    for related_fqn in trusted_related:
        related_ctx = related_contexts.get(related_fqn)
        if not related_ctx:
            continue
        group = _compound_attribute_column_group(term, related_ctx.get("columns") or [])
        if group is not None:
            qualifying.append((related_fqn, related_ctx, group))

    if len(qualifying) != 1:
        return None  # no related table qualifies, or more than one does -- never guess which

    return _as_dicts(*qualifying[0])


def _resolve_category_requested_attribute(
    source_id: int, user_id: str, fqn: str, ctx: dict, attribute_term: str, *, session=None,
) -> list[dict] | None:
    """2026-08-24 "autonomous business-concept expansion" milestone — a
    THIRD fallback tier alongside _resolve_requested_attribute (single
    lexical match) and _resolve_compound_requested_attribute (name-token
    abbreviation/qualifier compounds like "name" -> Fname/Lname): a
    business CATEGORY word like "contact" that never lexically matches any
    column name at all. Reuses data.query_planning_service's
    _BUSINESS_CONCEPT_CATEGORIES/_column_matches_concept_category verbatim
    — the SAME evidence (semantic_type + a small generic name-token
    fallback, restricted-category columns always excluded) the legacy
    planning path uses, so this module never grows a second, divergent
    business-concept vocabulary. Same entity-bound discipline as
    _resolve_compound_requested_attribute: the entity's own table is tried
    first; a related table is only consulted, via the SAME trusted-join set
    (_trusted_related_tables), when the entity's own table has zero
    matches. Returns None (never invented evidence) when the term isn't a
    recognized category or nothing safe is found anywhere reachable."""
    from data.query_planning_service import _BUSINESS_CONCEPT_CATEGORIES, _column_matches_concept_category
    from data.vocabulary_service import normalize_term

    # The AI interpreter names requested attributes as free-text phrases,
    # not single deterministic-extractor words (live-confirmed against real
    # the reference database: "contact information", not "contact") — check each word of the
    # phrase against the same category vocabulary the legacy deterministic
    # path (data.query_planning_service, which only ever sees single-word
    # terms) uses, first match wins. Never invents a category for a phrase
    # that names none of the recognized words.
    category = None
    for word in (attribute_term or "").split():
        category = _BUSINESS_CONCEPT_CATEGORIES.get(normalize_term(word))
        if category is not None:
            break
    if category is None:
        return None

    def _matches(table_ctx: dict) -> list[dict]:
        return [c for c in (table_ctx.get("columns") or []) if _column_matches_concept_category(c, category)]

    def _as_dicts(table_fqn: str, table_ctx: dict, columns: list[dict]) -> list[dict]:
        return [
            {
                "table_fqn": table_fqn, "column_name": col["column_name"],
                "business_label": (col.get("dictionary") or {}).get("business_label"),
                "score": 1.0, "ctx": table_ctx,
            }
            for col in columns
        ]

    local_matches = _matches(ctx)
    if local_matches:
        return _as_dicts(fqn, ctx, local_matches)

    trusted_related = _trusted_related_tables(source_id, user_id, fqn, ctx, session=session)
    if not trusted_related:
        return None

    from data.business_knowledge_service import get_table_business_contexts_batch

    related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)
    qualifying: list[tuple[str, dict, list[dict]]] = []
    for related_fqn in trusted_related:
        related_ctx = related_contexts.get(related_fqn)
        if not related_ctx:
            continue
        matches = _matches(related_ctx)
        if matches:
            qualifying.append((related_fqn, related_ctx, matches))

    if len(qualifying) != 1:
        return None  # no related table qualifies, or more than one does -- never guess which

    return _as_dicts(*qualifying[0])


def _resolve_requested_attributes(
    source_id: int, user_id: str, fqn: str, ctx: dict, requested_attributes: tuple[str, ...], *, session=None,
) -> tuple[list[dict], dict[str, dict]] | None:
    """Resolves each AI-named requested output attribute, in order, via
    _resolve_requested_attribute, falling back to
    _resolve_compound_requested_attribute (Part 11/12/13) when the single-
    column path finds nothing. Returns (resolved, extra_tables): resolved
    is a list of query_plan["dimensions"]-shaped entries ({"term",
    "selected", "candidates", "warnings"} -- the exact shape
    _select_entries/build_sql_plan already consumes to build a plain,
    non-aggregated SELECT column, so a requested attribute becomes just
    another dimension-shaped column, no GROUP BY added since no aggregated
    measure is ever present alongside a bare requested-attributes
    question) -- a compound attribute simply contributes MULTIPLE entries
    sharing the same `term` label, reusing PhysicalQueryPlan.columns'
    already-existing multi-column support unchanged rather than inventing
    a new "compound column" shape. extra_tables maps table_fqn -> ctx for
    every related table an attribute actually resolved onto (empty when
    every attribute matched fqn itself) -- the caller folds these into its
    own unified cross-table join computation.

    Returns None when ANY attribute cannot be resolved to at least one
    real column -- callers must safely disqualify rather than partially
    answer (never silently drop a specifically-requested field)."""
    resolved: list[dict] = []
    extra_tables: dict[str, dict] = {}
    for term in requested_attributes:
        selected = _resolve_requested_attribute(source_id, user_id, fqn, ctx, term, session=session)
        if selected is not None:
            col_ctx = selected.pop("ctx")
            resolved.append({"term": term, "selected": selected, "candidates": [], "warnings": []})
            if selected["table_fqn"] != fqn:
                extra_tables.setdefault(selected["table_fqn"], col_ctx)
            continue

        compound = _resolve_compound_requested_attribute(source_id, user_id, fqn, ctx, term, session=session)
        if compound is None:
            compound = _resolve_category_requested_attribute(source_id, user_id, fqn, ctx, term, session=session)
        if compound is None:
            return None
        for entry in compound:
            col_ctx = entry.pop("ctx")
            resolved.append({"term": term, "selected": entry, "candidates": [], "warnings": []})
            if entry["table_fqn"] != fqn:
                extra_tables.setdefault(entry["table_fqn"], col_ctx)
    return resolved, extra_tables


_EXISTENCE_POSITIVE_OPERATORS = frozenset({
    "exists", "exist", "has", "have", "is not null", "is_not_null", "not null", "not_null",
})
_EXISTENCE_NEGATIVE_OPERATORS = frozenset({
    "not_exists", "not exists", "missing", "is null", "is_null", "null",
})


def _ai_filter_existence_polarity(f: dict) -> str | None:
    """Classifies an AI-supplied filter (core.semantic.ai_interpreter's
    {"attribute", "operator", "value"} shape) as an EXISTENCE qualifier
    ("has an email", "missing a phone number") rather than a genuine
    attribute=value comparison. The AI interpreter's own Day-1 prompt
    vocabulary has no dedicated existence operator (its filters schema only
    documents <=, !=, >, <, contains, between) -- live-confirmed against
    a real production database, REG-P-04 "How many students have an email on file?": the
    model's own improvised way of expressing "has a value" was {"attribute":
    "email", "operator": "!=", "value": ""}, an inequality against an empty
    string, not a real comparison. "How many students are without an
    email?" produced a second improvised idiom live: {"attribute": "email",
    "operator": "=", "value": "null"} -- the LITERAL STRING "null", not a
    real None/empty value, still meaning the same "no value present" idea
    -- recognized here as an empty-equivalent value alongside None/"" so
    this class of model phrasing isn't missed just because the model wrote
    the word out. Recognized here alongside the explicit exists/missing
    vocabulary in case a future prompt revision (or a different provider)
    emits it directly. Returns "not_null", "null", or None (not existence-
    shaped at all -- the existing boolean-equality path below handles it
    exactly as before)."""
    operator = (f.get("operator") or "").strip().lower()
    value = f.get("value")
    value_is_empty = value is None or (
        isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a")
    )
    if operator in _EXISTENCE_POSITIVE_OPERATORS:
        return "not_null"
    if operator in _EXISTENCE_NEGATIVE_OPERATORS:
        return "null"
    if operator in {"!=", "<>"} and value_is_empty:
        return "not_null"
    if operator in {"=", "=="} and value_is_empty:
        return "null"
    return None


def _resolve_ai_existence_filter(
    source_id: int, user_id: str, fqn: str, ctx: dict, attribute: str, polarity: str, *, session=None,
) -> tuple[dict, PlanFilter, dict[str, dict]] | None:
    """Resolves one existence-shaped filter ("email" + polarity "not_null"
    for "has an email"/"email on file") to a real column and an IS NOT
    NULL/IS NULL PlanFilter -- on fqn itself or, failing that, one trusted
    related table (NEXT MILESTONE, Part 3: "if the attribute lives on a
    related table, resolve the real relationship path"). Reuses
    _resolve_requested_attribute completely unchanged -- an existence check
    needs the exact same evidence class a requested output attribute does
    (any real column, by name/label token overlap, never datatype-
    restricted the way the boolean-flag filter path above is), so this is
    the "smallest adapter", not a second column resolver. Returns None when
    the attribute cannot be resolved to exactly one real column anywhere in
    scope -- callers must safely disqualify rather than guess, same
    contract as every other resolver in this module."""
    selected = _resolve_requested_attribute(source_id, user_id, fqn, ctx, attribute, session=session)
    if selected is None:
        return None
    col_ctx = selected.pop("ctx")
    operator = "IS NOT NULL" if polarity == "not_null" else "IS NULL"
    resolved = {
        "column": selected["column_name"], "operator": operator, "value": None,
        "resolved": True, "table_fqn": selected["table_fqn"],
    }
    plan_filter = PlanFilter(
        table_fqn=selected["table_fqn"], column_name=selected["column_name"],
        operator=operator, value=None,
    )
    extra_tables = {} if selected["table_fqn"] == fqn else {selected["table_fqn"]: col_ctx}
    return resolved, plan_filter, extra_tables


def _resolve_ai_filters(
    source_id: int, user_id: str, fqn: str, ctx: dict, ai_filters: tuple[dict, ...], *, session=None,
) -> tuple[list[dict], list[PlanFilter], dict[str, dict]] | None:
    """Resolve each AI-identified business filter (core.semantic.
    ai_interpreter's own filters shape -- {"attribute": <business term>,
    "operator": <business term>, "value": <business term>}, never a
    physical identifier by that module's own contract) against fqn's REAL
    columns, falling back to a directly-related table's columns
    (_resolve_ai_filter_cross_table) when fqn itself has no match --
    AI Physical Planner Coverage Expansion, Part E: the filter's evidence
    column is never assumed to live on the entity's own table (live-
    confirmed against a real production database, REG-S-02 "How many students are on a
    trial subscription?": IsTrial lives on dbo.EDU_ClassSignups, not
    dbo.EDU_Student).

    Generalizes data.query_planning_service._find_status_column's own
    "a boolean-typed column whose own name/label carries the requested
    keyword" evidence class -- but keyed off whatever business term the AI
    actually named for THIS question, not that function's fixed status-word
    vocabulary (_STATUS_KEYWORDS/_STATUS_BOOLEAN_VALUES only knows "active"/
    "inactive"/"open"/"closed"/...), so it is never limited to a hardcoded
    business-word list (e.g. "trial", which that fixed vocabulary has no
    entry for at all). That fixed-vocabulary path is untouched and still
    runs separately via _resolve_stage2_filters/extract_query_intent above
    -- this only covers filters that path cannot reach.

    Deliberately scoped to boolean/flag columns only (mirrors data.
    query_planning_service._typed_status_filter_value's own identical
    "bit\\boolean column: only active/inactive map" scoping) -- a business
    term matched against a real BOOLEAN-typed column via its own name/label
    is genuine, unambiguous schema evidence; matching an arbitrary business
    term against a numeric/text column's VALUES would require guessing
    without a concrete evidence source, so that stays out of scope here
    rather than forcing an answer. Comparison/IN/NULL operators remain
    supported unchanged at the data.sql_planning_service.build_sql_plan/
    data.sql_generation_service.generate_sql layer this adapter feeds --
    only THIS resolver's boolean-equality scope is conservative, not the
    downstream validator/generator's.

    Returns None when ANY filter cannot be resolved to EXACTLY ONE real
    boolean column (on fqn OR, failing that, one trusted related table) via
    a genuine token match -- callers must safely disqualify rather than
    guess (never a partial application of some filters and a silent drop
    of others). Returns (resolved_filters, plan_filters, extra_tables) on
    success, where extra_tables maps table_fqn -> ctx for every related
    table a filter actually resolved onto (empty when every filter matched
    fqn itself) -- the caller folds these into its own cross-table join
    computation alongside any measure/dimension tables.
    """
    from data.query_planning_service import _tokenize

    columns = ctx.get("columns") or []
    resolved_filters: list[dict] = []
    plan_filters: list[PlanFilter] = []
    extra_tables: dict[str, dict] = {}

    for f in ai_filters:
        attribute = f.get("attribute")
        if not isinstance(attribute, str) or not attribute.strip():
            return None

        # NEXT MILESTONE, Part 3 — an existence/null qualifier ("has an
        # email", "email on file", "missing a phone number") is a
        # fundamentally different filter shape than a boolean-flag
        # equality check below: it can match ANY real column (text, date,
        # numeric...), never just a BOOLEAN-typed one, and resolves to IS
        # NOT NULL/IS NULL rather than "= 1"/"= 0". Checked and resolved
        # first, via the SAME any-datatype, cross-table-capable resolver
        # _resolve_requested_attribute already provides — routed away from
        # the boolean-only matching below entirely, never partially applied
        # through both paths.
        polarity = _ai_filter_existence_polarity(f)
        if polarity is not None:
            existence = _resolve_ai_existence_filter(
                source_id, user_id, fqn, ctx, attribute, polarity, session=session,
            )
            if existence is None:
                return None  # no unambiguous real column anywhere -- never guess
            resolved, plan_filter, existence_extra_tables = existence
            resolved_filters.append(resolved)
            plan_filters.append(plan_filter)
            extra_tables.update(existence_extra_tables)
            continue

        value = f.get("value")
        operator = f.get("operator")

        attribute_tokens = set(_tokenize(attribute))
        value_tokens = set(_tokenize(value)) if isinstance(value, str) else set()
        if not (attribute_tokens or value_tokens):
            return None

        matches = _match_filter_columns(attribute_tokens, value_tokens, columns)
        column_table_fqn = fqn
        if not matches:
            # No match at all on the primary table -- try a trusted related
            # table. Deliberately NOT taken when matches has more than one
            # entry: an ambiguous match on the primary table itself is real
            # evidence of a naming collision the AI's term can't
            # disambiguate, and must stay a hard disqualify rather than
            # being silently routed around via a different table.
            cross = _resolve_ai_filter_cross_table(
                source_id, user_id, fqn, ctx, attribute_tokens, value_tokens, session=session,
            )
            if cross is None:
                # FINAL PRE-37 MILESTONE, Part 6/7/9/11 — no BOOLEAN-flag
                # column matched anywhere, but a categorical VALUE filter
                # ("classes AT the RICHARDSON location") is a genuinely
                # different, non-boolean shape this resolver's own scope
                # never covered before: the attribute names a related
                # dimension/entity ("location"), and the value is a real
                # business VALUE on it ("Richardson"), not a flag state.
                # Live-confirmed against a real production database: "Show classes at the
                # Richardson location" -- dbo.EDU_Location has no BOOLEAN
                # column at all, so both boolean-matching attempts above
                # correctly find nothing; this is the intentionally
                # separate, live-value-validated path for that case.
                if isinstance(value, str) and value.strip():
                    categorical = _resolve_ai_categorical_value_filter(
                        source_id, user_id, fqn, ctx, attribute, value, session=session,
                    )
                    if categorical is not None:
                        resolved_filters.append({
                            "column": categorical["column_name"], "operator": "=",
                            "value": categorical["value"], "resolved": True,
                            "table_fqn": categorical["table_fqn"],
                        })
                        plan_filters.append(PlanFilter(
                            table_fqn=categorical["table_fqn"], column_name=categorical["column_name"],
                            operator="=", value=categorical["value"],
                        ))
                        if categorical["table_fqn"] != fqn:
                            extra_tables[categorical["table_fqn"]] = categorical["ctx"]
                        continue
                return None  # no match anywhere -- never guess
            column_table_fqn = cross["table_fqn"]
            matches = [cross["column"]]
            extra_tables[column_table_fqn] = cross["ctx"]
        elif len(matches) != 1:
            return None  # ambiguous on the primary table itself -- never guess

        column_name = matches[0]["column_name"]
        # A bare business-state term ("trial", "active") matched against its
        # own flag column is an affirmative request unless the AI itself
        # supplied a negating operator or an explicitly negative value word
        # -- never a hardcoded business-word list, just the AI's own signal
        # on THIS filter.
        negated = operator == "!=" or (
            isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}
        )
        typed_value = 0 if negated else 1
        resolved_filters.append({
            "column": column_name, "operator": "=", "value": typed_value,
            "resolved": True, "table_fqn": column_table_fqn,
        })
        plan_filters.append(PlanFilter(
            table_fqn=column_table_fqn, column_name=column_name, operator="=", value=typed_value,
        ))

    return resolved_filters, plan_filters, extra_tables


def _resolve_ai_measure_column(
    fqn: str, ctx: dict, measure_term: str, *, entity_hint: str | None = None, require_exact: bool = False,
) -> dict | None:
    """Resolve the AI's own measure business term (core.semantic.
    ai_interpreter's `measures`, never a physical identifier) against fqn's
    REAL columns for a SUM/AVG/MIN/MAX aggregation -- same generic
    token-overlap-against-name/business_label evidence class
    _resolve_ai_filters already uses for boolean filters, scoped here to a
    genuinely NUMERIC (INTEGER/DECIMAL) column, the only datatype these
    four aggregation functions are unambiguously safe to apply to.

    entity_hint -- REG-G-03 regression fix. The AI interpreter commonly
    splits a single compound business phrase ("course fee") into a
    separately-named entity ("course", peeled off because it matches the
    closed known-vocabulary hint) plus a bare measure term ("fee") --
    live-confirmed against a real production database: once split, "fee" alone token-overlaps
    BOTH CourseFee {course, fee} and CourseFeeEarlyBird {course, fee, early,
    bird}, and neither is an EXACT match for the bare term {fee} alone, so
    the exact-match tie-break below (correctly) still refuses as ambiguous
    even though a human reading "course fee" would not consider this
    ambiguous at all. entity_hint, when given, is the AI's own separately-
    named entity for THIS question -- reconstructing "<entity> <measure>"
    and re-trying the SAME exact-match discipline against that combined
    token set recovers the lost qualifier without weakening the "exactly
    one exact match, or refuse" rule itself (still never guesses among a
    genuine tie either way).

    require_exact -- Part 5/10 widened cross-table fallback (see
    _resolve_ai_measure_cross_table). When True, always applies the exact-
    match discipline below regardless of how many candidates merely
    overlap -- used only by the caller's second-tier fallback, which trusts
    a candidate found OUTSIDE the entity table's own relationship
    neighborhood and therefore needs the strongest evidence bar this
    resolver has, not the "unique overlap is good enough" shortcut below.

    Returns data.query_planning_service._resolve_term's own measure
    "selected" candidate shape ({"table_fqn", "column_name",
    "business_label", "score"}) so it plugs into query_plan["measures"]
    unchanged, or None when the term cannot be resolved to EXACTLY ONE real
    numeric column -- callers must safely disqualify rather than guess.
    """
    from data.query_planning_service import _tokenize

    term_tokens = _normalize_modifier_tokens(set(_tokenize(measure_term)))
    if not term_tokens:
        return None

    def _col_tokens(col: dict) -> set[str]:
        label = (col.get("dictionary") or {}).get("business_label") or ""
        raw = set(_tokenize(col["column_name"])) | set(_tokenize(label))
        return _normalize_modifier_tokens(raw)

    matches = []
    for col in ctx.get("columns") or []:
        data_type = (col.get("schema") or {}).get("data_type")
        if data_type not in _NUMERIC_DATA_TYPES:
            continue
        if term_tokens & _col_tokens(col):
            matches.append(col)

    if require_exact or len(matches) != 1:
        # Live-confirmed against a real production database ("course fee" -> both CourseFee
        # AND CourseFeeEarlyBird share the {course, fee} token overlap):
        # a plain non-empty-intersection match treats an EXACT column-name
        # match and a strict-SUPERSET column-name match as equally good
        # evidence, so a genuinely unambiguous term gets refused as if it
        # were a real tie. An exact term-tokens == column-tokens match is
        # strictly stronger evidence than mere overlap -- general to any
        # measure term, not specific to CourseFee. Only auto-resolves when
        # exactly one candidate has that stronger signal; a real tie (zero
        # or more than one exact match) still safely disqualifies.
        exact = [col for col in matches if term_tokens == _col_tokens(col)]
        if len(exact) != 1 and entity_hint:
            hint_tokens = term_tokens | _normalize_modifier_tokens(set(_tokenize(entity_hint)))
            if hint_tokens != term_tokens:
                exact = [col for col in matches if hint_tokens == _col_tokens(col)]
        if len(exact) != 1:
            return None  # no match, or genuinely ambiguous -- never guess
        matches = exact

    col = matches[0]
    dic = col.get("dictionary") or {}
    return {
        "table_fqn": fqn, "column_name": col["column_name"],
        "business_label": dic.get("business_label"), "score": 1.0,
    }


def _corroborate_weak_entity_candidate(
    source_id: int, user_id: str, entity_name: str, evidence_bundle: dict,
    anchor_term: str | None, requested_attributes: list[str] | None, *, session=None,
) -> Any | None:
    """AI Schema Retrieval Gap fix, D4b-class relationship-corroboration
    retry: a bounded, general recovery attempt for an entity/dimension
    resolution that just failed _MIN_TRUSTED_NAME_SCORE.

    Live-confirmed against a real production database: "program" has ZERO lexical relation
    to its true canonical table dbo.EDU_Track, so the AI adjudicator
    reliably prefers a lexically-strong-but-wrong alternative (e.g.
    dbo.EDU_Mentoring_Program, whose own name literally contains
    "Program") even once dbo.EDU_Track is present in its evidence (the
    role-aware column-evidence promotion above already fixed it being
    ABSENT). A lexical name-match heuristic cannot see the evidence that
    actually settles this: dbo.EDU_Track sits a verified 1 hop from
    dbo.EDU_Class, which is a real top-scored candidate for this SAME
    question's other named business term ("course fee") -- confirmed via
    get_relationship_distance_map. dbo.EDU_Mentoring_Program has no such
    connection at all. This is general schema-relationship evidence, not
    specific to "program"/EDU_Track/EDU_Class: any question where one named
    role is weakly lexically evidenced but genuinely, verifiably connected
    to another role's real evidence benefits the same way.

    Reuses only already-built primitives -- gather_candidate_evidence,
    get_relationship_distance_map (the same bounded, trusted-relationship-
    only BFS data/semantic_retrieval_service.py already uses elsewhere),
    and adjudicate_candidates -- and never invents a table: every annotated
    candidate was already real evidence in `evidence_bundle`. AI-path only;
    does not touch _resolve_concept or any legacy scoring.

    Returns a fresh AdjudicationResult (from ONE additional adjudication
    call, re-run against the SAME evidence annotated with real hop-distance
    facts) only when that retry both selects a real candidate AND clears
    the trust bar -- lexical name_score >= _MIN_TRUSTED_NAME_SCORE (as
    before) OR a genuine verified-relationship corroboration to the anchor
    term's own real evidence. Returns None when no anchor term was
    available, no candidate had any corroborating relationship, or the
    retry still doesn't clear the trust bar -- callers must fall through to
    the existing safe refusal exactly as before, never guess.
    """
    if not anchor_term or anchor_term.strip().lower() == entity_name.strip().lower():
        return None

    from data.semantic_contract_service import gather_candidate_evidence
    from data.semantic_retrieval_service import get_relationship_distance_map
    from core.semantic.candidate_adjudicator import ROLE_CORROBORATION_RETRY, adjudicate_candidates

    # The anchor must be the term's own genuinely ADJUDICATED answer, not a
    # raw top-N-by-score slice of its evidence pool -- live-confirmed that
    # slice reproduces the EXACT SAME lexical-name-dominance problem this
    # retry exists to correct (e.g. "course fee"'s own top-3 by raw score
    # is dbo.EDU_Track/EDU_Course/LMS_ACL_COURSE_ACCESS -- none of which is
    # the real answer dbo.EDU_Class -- purely because those score higher on
    # table-NAME lexical grounds despite having no fee column at all). One
    # bounded extra adjudication call, using the exact same real evidence
    # this module always uses for that term elsewhere.
    anchor_bundle = gather_candidate_evidence(source_id, user_id, anchor_term, session=session)
    anchor_evidence = anchor_bundle.get("evidence") or []
    if not anchor_evidence:
        return None
    anchor_adjudication = adjudicate_candidates(
        anchor_term, anchor_evidence, None, session=session, simple_selection_only=True,
        role_context=ROLE_CORROBORATION_RETRY,
    )
    if (
        anchor_adjudication is None or anchor_adjudication.no_safe_selection
        or not anchor_adjudication.selected_table_fqn
    ):
        return None
    anchor_fqns = {anchor_adjudication.selected_table_fqn}

    distance_map = get_relationship_distance_map(
        source_id, anchor_fqns, max_hops=_CORROBORATION_MAX_HOPS, session=session,
    )

    annotated_evidence = []
    corroborated_fqns: set[str] = set()
    for item in evidence_bundle.get("evidence") or []:
        fqn = item.get("table_fqn")
        hop = distance_map.get(fqn)
        if fqn in anchor_fqns:
            annotated_evidence.append(item)
            continue
        if hop is not None:
            corroborated_fqns.add(fqn)
            item = {
                **item,
                "corroborating_relationship": (
                    f"Verified schema relationship ({hop} hop(s) via trusted foreign keys) to a real "
                    f"top-evidenced candidate table for this question's other business term "
                    f"'{anchor_term}'."
                ),
            }
        annotated_evidence.append(item)
    if not corroborated_fqns:
        return None

    adjudication = adjudicate_candidates(
        entity_name, annotated_evidence, requested_attributes, session=session, simple_selection_only=True,
        role_context=ROLE_CORROBORATION_RETRY,
    )
    if adjudication is None or adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return None

    fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle.get("candidates_by_fqn", {}).get(fqn)
    if cand_info is None:
        return None
    candidate = cand_info["candidate"]
    trusted_by_name = (candidate.get("name_score") or 0.0) >= _MIN_TRUSTED_NAME_SCORE
    trusted_by_relationship = fqn in corroborated_fqns
    if not (trusted_by_name or trusted_by_relationship):
        return None
    return adjudication


def _relationship_connected_candidates(
    source_id: int, primary_fqn: str, evidence_bundle: dict, *, session=None,
) -> dict[str, int]:
    """{table_fqn: hop_distance} for every OTHER real candidate already
    present in evidence_bundle that has a verified schema-relationship path
    to primary_fqn within _CORROBORATION_MAX_HOPS (get_relationship_
    distance_map, trusted AUTO/APPROVED edges only -- the same bounded BFS
    primitive _corroborate_weak_entity_candidate already uses). Shared
    signal for both _resolve_non_calendar_dimension and
    _resolve_ai_measure_cross_table: a bare business term resolved via a
    single global gather_candidate_evidence/adjudicate_candidates call has
    no awareness of the entity table this question already confidently
    resolved (primary_fqn) -- live-confirmed against a real production database, two
    independent cases: "program" -> dbo.EDU_Mentoring_Program (a strong
    lexical/generated-vocabulary match with literally zero schema
    relationship to the entity table, beating dbo.EDU_Track, 1 hop away and
    present in the SAME evidence bundle, but with no lexical relation to
    "program" at all); "fee" -> dnnuser.Roles (an unrelated identity-portal
    table, over dbo.EDU_Class, also present in the same evidence bundle,
    2 hops away via dbo.EDU_Track). Never invents a table: only ever
    surfaces candidates the evidence-gathering step already retrieved.
    """
    from data.semantic_retrieval_service import get_relationship_distance_map

    distance_map = get_relationship_distance_map(
        source_id, {primary_fqn}, max_hops=_CORROBORATION_MAX_HOPS, session=session,
    )
    candidates_by_fqn = evidence_bundle.get("candidates_by_fqn") or {}
    return {
        fqn: hop for fqn, hop in distance_map.items()
        if fqn != primary_fqn and fqn in candidates_by_fqn
    }


def _resolve_ai_measure_cross_table(
    source_id: int, user_id: str, measure_term: str, primary_fqn: str, primary_ctx: dict, *,
    entity_hint: str | None = None, session=None,
) -> dict | None:
    """Fallback when _resolve_ai_measure_column finds no match on the
    entity's own table (primary_fqn) -- e.g. "Average course fee by
    program": the AI correctly names "program" as the entity (it's also the
    grouping dimension), but the measure (CourseFee) lives on a genuinely
    different, related table. The physical planner must never assume the
    grouping/entity table also owns the measure -- this runs the SAME
    independent evidence-gathering + adjudication mechanism
    _resolve_non_calendar_dimension already uses for a cross-table
    dimension, then re-applies _resolve_ai_measure_column's own unchanged
    numeric-column matcher against the newly-found table.

    Deliberately does NOT apply _MIN_TRUSTED_NAME_SCORE here (unlike
    _resolve_non_calendar_dimension/the entity-selection gate): that gate
    tests whether the TABLE's name lexically relates to the term, which is
    the right question for an entity/dimension term (it IS the table's
    business identity) but the wrong one for a measure term -- "course fee"
    has no reason to lexically resemble "EDU_Class" or "clients", it names
    a COLUMN, not the table. The real crosscheck here is downstream:
    requiring an exact, unambiguous NUMERIC column match via
    _resolve_ai_measure_column, which is a stronger, evidence-grounded
    guard for this specific shape than a table-name heuristic would be.

    Returns {"measure_fqn", "measure_ctx", "measure_selected", "candidate"}
    on success, or None on any failure -- caller must safely disqualify
    rather than guess. Join-path validation from primary_fqn to
    measure_fqn is the caller's responsibility (shared with dimension
    resolution, so both can be validated together as one combined join
    plan rather than two independently-anchored ones)."""
    from data.semantic_contract_service import gather_candidate_evidence
    from core.semantic.candidate_adjudicator import ROLE_MEASURE, adjudicate_candidates

    evidence_bundle = gather_candidate_evidence(source_id, user_id, measure_term, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return None

    adjudication = adjudicate_candidates(
        measure_term, evidence, None, session=session, simple_selection_only=True,
        role_context=ROLE_MEASURE,
    )
    if adjudication is None:
        return None
    if adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return None

    measure_fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(measure_fqn)
    if cand_info is None:
        return None

    candidate = cand_info["candidate"]
    measure_ctx = cand_info["ctx"]
    measure_selected = _resolve_ai_measure_column(measure_fqn, measure_ctx, measure_term, entity_hint=entity_hint)
    if measure_selected is None:
        # AI Physical Planner Coverage Expansion, Part C follow-up --
        # REG-G-03 regression fix. The adjudicator's own top pick for a
        # BARE measure term (e.g. "fee", the AI's own split of "course fee"
        # once "course" is peeled off as the separately-named entity) is
        # exactly the false-positive shape _vocabulary_trusts_candidate's
        # docstring already documents: a short measure noun collides with
        # an unrelated table's own column vocabulary just as confidently as
        # the real answer. Rather than trust that one ungrounded whole-
        # schema pick, check every OTHER real candidate this SAME gather
        # already retrieved that also has a verified schema-relationship
        # path to the entity's own table (_relationship_connected_
        # candidates) for a genuine, unambiguous NUMERIC column match --
        # the same "exactly one, or refuse" discipline every other cross-
        # table resolver in this module already applies to its own bounded
        # candidate pool. Restricted to relationship-connected candidates
        # (not the full raw evidence bundle) so this never promotes a
        # coincidentally-numeric-matching but schema-unrelated table.
        connected = _relationship_connected_candidates(source_id, primary_fqn, evidence_bundle, session=session)
        numeric_matches = []
        for alt_fqn, alt_hop in connected.items():
            if alt_fqn == measure_fqn:
                continue
            alt_info = evidence_bundle["candidates_by_fqn"][alt_fqn]
            alt_selected = _resolve_ai_measure_column(alt_fqn, alt_info["ctx"], measure_term, entity_hint=entity_hint)
            if alt_selected is not None:
                numeric_matches.append((alt_fqn, alt_info, alt_selected))
        if len(numeric_matches) == 1:
            measure_fqn, cand_info, measure_selected = numeric_matches[0]
        else:
            # Part 4/5/10 — relationship-connectivity to the ENTITY table is
            # the wrong bar when the entity itself is genuinely unrelated to
            # the measure (live-confirmed against a real production database: "Event Count"'s
            # AI-named entity "campaign" resolves to dbo.RetoolCampaigns,
            # which has no schema relationship to dbo.EDU_Section at all,
            # yet EDU_Section.EventCount is the exact, unambiguous, already
            # business-evidenced answer this SAME measure term's own
            # gather_candidate_evidence call already retrieved). Widen the
            # search to every OTHER candidate THIS bounded, business-
            # evidenced measure-evidence pool already contains — never a
            # wildcard schema scan, still exactly the same pool used above —
            # but raise the evidence bar to compensate for dropping the
            # relationship requirement: only an EXACT column/label token
            # match (require_exact=True, the strongest tier this resolver
            # has) is trusted here, and only when it is the SOLE exact match
            # across the whole bundle.
            exact_matches = []
            for alt_fqn, alt_info in evidence_bundle["candidates_by_fqn"].items():
                if alt_fqn == measure_fqn:
                    continue
                alt_selected = _resolve_ai_measure_column(
                    alt_fqn, alt_info["ctx"], measure_term, entity_hint=entity_hint, require_exact=True,
                )
                if alt_selected is not None:
                    exact_matches.append((alt_fqn, alt_info, alt_selected))
            if len(exact_matches) != 1:
                return None  # no genuine exact match anywhere in evidence, or a real tie -- never guess
            measure_fqn, cand_info, measure_selected = exact_matches[0]
        candidate = cand_info["candidate"]
        measure_ctx = cand_info["ctx"]

    return {
        "measure_fqn": measure_fqn, "measure_ctx": measure_ctx,
        "measure_selected": measure_selected, "candidate": candidate,
    }


def _resolve_dimension_display_column(dimension_term: str, table_fqn: str, ctx: dict) -> dict | None:
    """Term-specific dimension/attribute column match on an already-
    resolved table -- same generic token-overlap-against-name/business_label
    evidence class, and same exact-match-preferred discipline, as
    _resolve_ai_measure_column (scoped to genuinely dimension-eligible
    columns instead of numeric ones). Returns None when the term matches no
    column, or matches more than one with no single exact winner --
    callers fall back to the table's generic display label/key rather than
    guess."""
    from data.query_planning_service import _is_dimension_column, _tokenize

    term_tokens = set(_tokenize(dimension_term))
    if not term_tokens:
        return None

    def _col_tokens(col: dict) -> set[str]:
        label = (col.get("dictionary") or {}).get("business_label") or ""
        return set(_tokenize(col["column_name"])) | set(_tokenize(label))

    matches = [
        col for col in (ctx.get("columns") or [])
        if _is_dimension_column(col) and (term_tokens & _col_tokens(col))
    ]
    if len(matches) > 1:
        exact = [col for col in matches if term_tokens == _col_tokens(col)]
        if len(exact) != 1:
            return None  # genuine tie -- never guess
        matches = exact
    if len(matches) != 1:
        return None

    col = matches[0]
    dic = col.get("dictionary") or {}
    return {
        "table_fqn": table_fqn, "column_name": col["column_name"],
        "business_label": dic.get("business_label"), "score": 1.0,
    }


def _resolve_non_calendar_dimension(
    source_id: int, user_id: str, dimension_term: str, primary_fqn: str, *, session=None,
) -> dict | None:
    """Resolve a non-calendar GROUP BY dimension term (e.g. "program") to a
    REAL table via the SAME candidate-adjudication mechanism
    _attempt_relationship_list_plan already uses for a second/third named
    entity -- necessary because, unlike Part 2's boolean-filter resolution,
    there is often NO lexical relation at all between the business term and
    the real table/column that answers it (live-confirmed against real
    the reference database, golden-set defect D4b: "program" has no naming relation to
    dbo.EDU_Track/TrackName whatsoever -- a pure token-match resolver could
    never find it; this needs genuine AI/evidence-grounded adjudication,
    not string matching).

    On success, resolves a display/label column for the dimension table via
    data.query_planning_service's own _resolve_entity_label_column /
    _select_entity_key -- the SAME fallback chain
    data.query_planning_service._try_build_relationship_aggregate_plan
    already uses for its own "bare per/by <entity>" grouped-aggregate shape
    (dimension and measure on different tables) -- reused here unchanged,
    not reinvented.

    A dimension resolving to the SAME table as primary_fqn is a legitimate
    outcome (e.g. "Average course fee by program" where "program" is both
    the AI's named entity and its own grouping dimension) -- no longer
    rejected here. Join-path validation from primary_fqn to the dimension's
    table (when they differ) is the CALLER's responsibility, combined with
    the measure's own possible cross-table need into one unified join plan
    via data.query_planning_service._plan_joins, rather than computed
    independently here -- avoids two independently-anchored join plans that
    could disagree when both the measure and the dimension are cross-table.

    Returns None on ANY failure (no evidence, adjudication unavailable/no
    safe selection, weak name-relevance, no display column resolvable) --
    callers must safely disqualify rather than guess. Returns {"dim_fqn",
    "dim_ctx", "group_selected", "candidate"} on success.
    """
    from data.semantic_contract_service import gather_candidate_evidence
    from core.semantic.candidate_adjudicator import ROLE_DIMENSION, adjudicate_candidates

    evidence_bundle = gather_candidate_evidence(source_id, user_id, dimension_term, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return None

    adjudication = adjudicate_candidates(
        dimension_term, evidence, None, session=session, simple_selection_only=True,
        role_context=ROLE_DIMENSION,
    )
    if adjudication is None:
        return None
    if adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return None

    dim_fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(dim_fqn)
    if cand_info is None:
        return None

    candidate = cand_info["candidate"]

    # REG-G-03 regression fix — relationship-connectivity override. The
    # adjudicator's own top pick can be a lexically-strong-but-schema-
    # disconnected table (live-confirmed: "program" -> dbo.EDU_Mentoring_
    # Program, name_score 0.3333 but ALSO trusted via generated vocabulary
    # since the table's own name literally contains "Program" -- circular,
    # non-independent evidence for this exact term) while the real answer
    # (dbo.EDU_Track, name_score 0.0, no vocabulary term at all) sits right
    # there in the SAME evidence bundle, 1 hop from the entity's own
    # already-confirmed table. Only ever switches to a DIFFERENT candidate
    # already present in evidence_bundle, and only when it is the UNIQUE
    # one with a verified schema-relationship path to primary_fqn (never
    # guesses among two-or-more connected alternatives -- a genuine tie is
    # left to the existing trust gate below instead).
    # A dimension resolving to primary_fqn ITSELF (the entity's own table)
    # is always trivially valid by construction -- no relationship check
    # applies (there is nothing to be "connected" to; it IS the anchor) --
    # _relationship_connected_candidates deliberately never includes
    # primary_fqn in its own return value, so skip the override entirely
    # rather than have that absence misread as "not connected".
    connected: dict[str, int] = {}
    if dim_fqn != primary_fqn:
        connected = _relationship_connected_candidates(source_id, primary_fqn, evidence_bundle, session=session)
        if dim_fqn not in connected and len(connected) == 1:
            dim_fqn = next(iter(connected))
            cand_info = evidence_bundle["candidates_by_fqn"][dim_fqn]
            candidate = cand_info["candidate"]

    if (
        (candidate.get("name_score") or 0.0) < _MIN_TRUSTED_NAME_SCORE
        and not _vocabulary_trusts_candidate(dimension_term, candidate)
        and dim_fqn not in connected
        and dim_fqn != primary_fqn
    ):
        # NEXT MILESTONE, Part 4 — same vocabulary-corroboration extension
        # as attempt_ai_physical_plan's own entity-selection gate (see
        # _vocabulary_trusts_candidate's docstring): a dimension term is
        # the same "what is this row" kind of business noun an entity term
        # is, so the same real auto-derived-vocabulary evidence source is
        # safe to trust here too. A verified schema-relationship path to
        # the entity's own table (dim_fqn in connected, checked above) is
        # an independent third trust signal, sufficient on its own -- the
        # same "trusted_by_name or trusted_by_relationship" discipline
        # _corroborate_weak_entity_candidate already applies.
        return None

    dim_ctx = cand_info["ctx"]

    from data.query_planning_service import _resolve_entity_label_column, _select_entity_key

    # Compound Query Coverage, Part 8 — a term-specific column match is
    # tried FIRST, before the table's generic display label. Live-confirmed
    # this matters once more than one dimension term can resolve to the
    # SAME table (e.g. "class name" AND "class type" both landing on
    # dbo.EDU_Class): _resolve_entity_label_column is table-scoped, not
    # term-aware, so unconditionally calling it for every dimension_term
    # silently collapsed distinct terms onto the SAME generic label column
    # (ClassName for both) -- correct for a term like "program" that truly
    # has no matching column of its own (falls through to the label/key
    # below unchanged), wrong for a term that names a REAL, different
    # column on the resolved table. General fix, same exact-match-preferred
    # discipline as _resolve_ai_measure_column: never guesses among a
    # genuine multi-way tie.
    group_selected = _resolve_dimension_display_column(dimension_term, dim_fqn, dim_ctx)
    if group_selected is None:
        label_hit = _resolve_entity_label_column(dim_fqn, dim_ctx)
        if label_hit is not None:
            group_selected = label_hit
        else:
            key = _select_entity_key(dim_ctx)
            if key is None:
                return None
            group_selected = {
                "table_fqn": dim_fqn, "column_name": key["column_name"],
                "business_label": None, "score": 1.0,
            }

    return {
        "dim_fqn": dim_fqn, "dim_ctx": dim_ctx, "group_selected": group_selected, "candidate": candidate,
    }


def _resolve_relationship_requested_attributes(
    source_id: int, user_id: str,
    validated_entities: list[str], resolved: dict[str, dict], requested_attributes: tuple[str, ...],
    *, session=None,
) -> tuple[list[dict], dict[str, dict]] | None:
    """Compound Query Coverage, Part 6 — resolve each requested output
    attribute ("Show students and their enrolled classes with class
    location") against whichever of the relationship's OWN already-
    resolved participating entity tables has a genuine, unambiguous column
    match. Deliberately narrower (and cheaper) than the single-entity
    _resolve_requested_attribute's own "one trusted related table" 1-hop
    search: the join plan already spans exactly these 2-4 tables, so there
    is no need to search beyond them, and restricting to them is itself a
    real disambiguation signal (a term matching a column on exactly one
    already-named entity's table is much stronger evidence than a bare
    1-hop neighborhood search would be).

    2026-08-24 "autonomous business-concept expansion" milestone —
    source_id/user_id/session added so the category-expansion fallback
    below can widen one hop to each participating entity's own trusted
    related tables (mirrors _resolve_category_requested_attribute's
    identical widening for the single-entity path) — a lexical/token match
    still never searches beyond the entity's own table, only the NEW
    category fallback does, and only when the entity's own table has zero
    matches. Return type changed from list[dict] | None to
    tuple[list[dict], dict[str, dict]] | None to match _resolve_requested_
    attributes' own established (resolved, extra_tables) contract — a
    category hit on a related table introduces a table that isn't yet part
    of the relationship's join graph, and the caller needs to know about it
    to extend that graph (_plan_joins) before generating SQL; the original
    list-only return had no way to communicate that.

    Returns (resolved, extra_tables): resolved is query_plan["dimensions"]-
    shaped entries ({"term", "selected", "candidates", "warnings"} -- the
    exact shape build_sql_plan already consumes for a plain SELECT column,
    reused unchanged); extra_tables maps table_fqn -> ctx for every related
    table a category hit actually resolved onto (empty when every attribute
    matched a participating entity's own table). Returns None when ANY
    attribute can't be resolved to exactly one real column on exactly one
    participating entity's table (or, for a recognized business-concept
    category, its own trusted related tables) -- callers must safely
    disqualify rather than guess or silently drop a specifically-requested
    field.
    """
    from data.business_knowledge_service import get_table_business_contexts_batch
    from data.query_planning_service import _tokenize

    def _col_tokens(col: dict) -> set[str]:
        label = (col.get("dictionary") or {}).get("business_label") or ""
        return set(_tokenize(col["column_name"])) | set(_tokenize(label))

    resolved_attrs: list[dict] = []
    extra_tables: dict[str, dict] = {}
    for term in requested_attributes:
        term_tokens = set(_tokenize(term))
        if not term_tokens:
            return None
        hits = []
        for entity_name in validated_entities:
            ctx = resolved[entity_name]["ctx"]
            matches = _columns_matching_tokens(term_tokens, ctx.get("columns") or [])
            if len(matches) > 1:
                # Same exact-match-preferred discipline as
                # _resolve_ai_measure_column/_resolve_dimension_display_
                # column: a broad token-overlap match (e.g. "class name"
                # also loosely overlapping ClassID/ClassSignupCount purely
                # via the shared "class" token) must not out-rank a column
                # whose own tokens equal the term's EXACTLY (ClassName) --
                # never guessed among a genuine tie with no exact winner.
                exact = [c for c in matches if term_tokens == _col_tokens(c)]
                matches = exact if len(exact) == 1 else []
            if len(matches) == 1:
                hits.append((resolved[entity_name]["fqn"], matches[0]))
        if len(hits) == 1:
            table_fqn, col = hits[0]
            dic = col.get("dictionary") or {}
            resolved_attrs.append({
                "term": term,
                "selected": {
                    "table_fqn": table_fqn, "column_name": col["column_name"],
                    "business_label": dic.get("business_label"), "score": 1.0,
                },
                "candidates": [], "warnings": [],
            })
            continue
        if len(hits) > 1:
            return None  # ambiguous across participating entities — never guess

        # Autonomous business-concept expansion (2026-08-24 milestone) — a
        # category word like "contact information" never lexically matches
        # any column name (hits stays empty above), so try the same
        # semantic_type/name-token category evidence data.query_planning_
        # service's legacy path and this module's own single-entity path
        # (_resolve_category_requested_attribute) already use. Tried first
        # against each participating entity's OWN already-resolved table
        # (no wider search — this function's normal discipline); when NONE
        # of them has a direct match (live-confirmed against a real production database:
        # "Show students with their programs and contact information" —
        # Email lives on dnnuser.Users, related to Student, not on either
        # participating entity's own table), widen ONE hop to each entity's
        # OWN trusted (AUTO/APPROVED-join) related tables — the SAME bounded
        # widening _resolve_category_requested_attribute already does for
        # the single-entity path, just repeated per participating entity
        # here rather than for a single anchor. Requires exactly one
        # qualifying table overall (own table or 1-hop related, across every
        # entity) — never guesses among more than one. Multiple matching
        # columns on that ONE table are complementary, not ambiguous (Email
        # + Phone both belong to "contact information").
        from data.query_planning_service import _BUSINESS_CONCEPT_CATEGORIES, _column_matches_concept_category
        from data.vocabulary_service import normalize_term

        category = None
        for word in term.split():
            category = _BUSINESS_CONCEPT_CATEGORIES.get(normalize_term(word))
            if category is not None:
                break
        if category is None:
            return None

        def _cat_matches(table_ctx: dict) -> list[dict]:
            return [c for c in (table_ctx.get("columns") or []) if _column_matches_concept_category(c, category)]

        category_hits: list[tuple[str, list[dict], dict | None]] = []
        for entity_name in validated_entities:
            entity_fqn = resolved[entity_name]["fqn"]
            entity_ctx = resolved[entity_name]["ctx"]
            own_matches = _cat_matches(entity_ctx)
            if own_matches:
                category_hits.append((entity_fqn, own_matches, None))  # own table — already in the join graph
                continue
            trusted_related = _trusted_related_tables(source_id, user_id, entity_fqn, entity_ctx, session=session)
            if not trusted_related:
                continue
            related_contexts = get_table_business_contexts_batch(source_id, user_id, trusted_related, session=session)
            related_qualifying = [
                (related_fqn, _cat_matches(related_contexts[related_fqn]), related_contexts[related_fqn])
                for related_fqn in trusted_related
                if related_contexts.get(related_fqn) and _cat_matches(related_contexts[related_fqn])
            ]
            if len(related_qualifying) == 1:
                category_hits.append(related_qualifying[0])
            # 0 or 2+ qualifying related tables for this one entity is its
            # own local ambiguity/no-evidence outcome — contributes nothing,
            # never guessed — the overall "exactly one across everything"
            # check below still applies on top of this.
        if len(category_hits) != 1:
            return None  # no participating entity (or its related tables) qualifies, or more than one does

        table_fqn, cols, related_ctx = category_hits[0]
        if related_ctx is not None:
            extra_tables.setdefault(table_fqn, related_ctx)
        for col in cols:
            dic = col.get("dictionary") or {}
            resolved_attrs.append({
                "term": term,
                "selected": {
                    "table_fqn": table_fqn, "column_name": col["column_name"],
                    "business_label": dic.get("business_label"), "score": 1.0,
                },
                "candidates": [], "warnings": [],
            })
    return resolved_attrs, extra_tables


def _attempt_relationship_list_plan(
    source_id: int,
    user_id: str,
    question: str,
    ai_interpretation: Any,
    *,
    allow_unconfirmed_pii: bool = False,
    session=None,
) -> dict:
    """Stage 3 — bare "show <entity> and their <entity>[, and <entity>]"
    relationship-list questions (2-3 AI-named entities, no measure/
    dimension/filter). Reuses, completely unchanged:

      - core.semantic.candidate_adjudicator.adjudicate_candidates and
        data.semantic_contract_service.gather_candidate_evidence, looped
        once per named entity — exactly Stage 1's single-entity mechanism,
        just repeated (one AI call per entity, never a combined call).
      - data.semantic_contract_service.validate_ai_entity_hypotheses, with
        single_entity_question=False (that function's own documented mode
        for a genuinely multi-entity relationship question — a second named
        entity legitimately grounding to a different table than terms
        already present is not a hijack here, unlike the single-entity
        case).
      - data.query_planning_service._plan_joins — the SAME join-path finder
        data.query_planning_service._try_build_relationship_plan uses,
        including its own star-topology anchor-selection loop (try each
        entity's table as the FROM anchor, keep whichever produces the
        fewest total join steps) — reproduced here unchanged rather than
        calling _try_build_relationship_plan itself, since that function
        requires a full grounding-derived entity_table_map this module
        doesn't build.
      - data.sql_planning_service.build_sql_plan's existing
        intent.type == "relationship_list" carve-out (Day 3, Task 1) for
        SELECT construction, and its existing all_joins_trusted check
        (fed by join_plan["steps"]) for join validation — neither is
        touched here.

    Never attempts a join together with a filter/date/grouping/ranking
    combination (Stage 4+ territory, matching data.query_planning_service.
    _try_build_relationship_plan's own identical gating) or a fan-out
    safety override — a plain relationship list's duplication under a
    one-to-many join is treated as a structural, informational characteristic
    exactly like the legacy planner treats it (LOW-severity warning only,
    never blocked, never silently deduplicated).
    """
    entities = list(ai_interpretation.entities)
    # Compound Query Coverage, Part 10 — bumped from (2, 3) to include 4:
    # data.query_planning_service._plan_joins' star-topology anchor-
    # selection (try each entity's table as FROM, keep the fewest total
    # join steps) is already N-table-generic -- confirmed live via the
    # Part 8 multi-dimension path joining 3 unrelated tables (entity +
    # 2 dimension tables) through the SAME function. A single, cautious
    # step past the prior cap, not "arbitrary graph complexity": every join
    # edge is still individually schema-verified below (all_joins_trusted),
    # so an unprovable/untrusted 4-entity path still safely disqualifies
    # rather than guessing, exactly like a 2-3 entity one always has.
    if len(entities) not in (2, 3, 4):
        return _disqualified("relationship_entity_count_out_of_stage3_scope")
    if ai_interpretation.filters:
        return _disqualified("filters_out_of_stage3_scope")

    from core.semantic.concept_resolver import extract_query_intent, extract_terms

    query_intent = extract_query_intent(question)
    # Mirrors data.query_planning_service._try_build_relationship_plan's own
    # gating exactly — a relationship list is a bare "show X and their Y"
    # shape; any aggregation/date/order/status signal means a different,
    # unhandled combination, never guessed at here.
    if any(query_intent.get(k) for k in ("aggregation", "date_range", "order", "status_value")):
        return _disqualified("relationship_with_filters_or_aggregation_out_of_stage3_scope")

    from data.semantic_contract_service import validate_ai_entity_hypotheses

    concepts, measures, raw_dimension_terms = extract_terms(question)
    validated_entities = validate_ai_entity_hypotheses(
        source_id, entities, concepts + measures + raw_dimension_terms,
        single_entity_question=False, session=session,
    )
    if len(validated_entities) != len(entities):
        return _disqualified("relationship_entity_hypothesis_rejected", attempted=True)

    # Live-confirmed against a real production database: "Show classes and their instructor"
    # got entities=("event","user") from the AI interpreter -- the same
    # closed-known-vocabulary-hint substitution already seen on the
    # single-entity path ("instructor"->"user"), except here BOTH entities
    # were substituted and validate_ai_entity_hypotheses's own anti-hijack
    # contradiction-check is deliberately disabled for multi-entity
    # questions (its own docstring: a genuinely additional second entity
    # must not be rejected just because some other term grounds
    # differently). Both substituted entities are real, well-evidenced
    # tables (dbo.EDU_Event, dnnuser.Users) with a real, well-formed 3-hop
    # join path between them -- every check up to this point passes, and
    # the result was a confidently WRONG answer (valid=True), not a safe
    # refusal. Closed with a simpler, absolute check the single-entity path
    # doesn't need: every entity must lexically ground in the question's
    # OWN deterministic terms (near-match tolerant of plurals via the same
    # _tokens_near_match legacy already uses for this purpose) -- "event"
    # and "user" appear nowhere in extract_terms("Show classes and their
    # instructor") == (['classes','instructor'], ...), so this rejects them
    # outright regardless of how well-evidenced they are as real tables.
    from data.query_planning_service import _tokens_near_match

    deterministic_terms_lower = [t.lower() for t in concepts + measures + raw_dimension_terms]
    ungrounded_indices = [
        i for i, entity_name in enumerate(validated_entities)
        if not any(_tokens_near_match(entity_name.lower(), t) for t in deterministic_terms_lower)
    ]
    if ungrounded_indices:
        # Safe AI-Only Control milestone, Part 5 — ONE bounded repair
        # attempt for a SINGLE ungrounded entity, never a loop and never a
        # second AI interpretation call. Live-confirmed against a real production database:
        # "Show classes and their instructor" -> entities=("event","user"),
        # neither of which grounds in the question's own text at all — the
        # grounding check above (unchanged) would simply refuse this. But
        # when exactly ONE entity is ungrounded and exactly ONE of the
        # question's own deterministic terms is left unclaimed by any
        # GROUNDED entity or the AI's own relationship_semantics word, that
        # leftover term is a strong, unambiguous substitute: it comes
        # directly from the question's own text (never invented) and is
        # re-validated from scratch through the SAME real-evidence
        # mechanism (validate_ai_entity_hypotheses) every entity already
        # goes through here, never trusted blindly. Two or more ungrounded
        # entities, or zero/multiple leftover terms, means the
        # interpretation is too unreliable to safely guess a repair for --
        # disqualifies exactly as before.
        if len(ungrounded_indices) != 1:
            return _disqualified("relationship_entity_not_grounded_in_question_text", attempted=True)

        grounded_lower = [
            validated_entities[i].lower() for i in range(len(validated_entities)) if i not in ungrounded_indices
        ]
        relationship_semantics_lower = [t.lower() for t in ai_interpretation.relationship_semantics]
        leftover_terms = [
            t for t in dict.fromkeys(deterministic_terms_lower)  # de-dup, preserve order
            if not any(_tokens_near_match(t, g) for g in grounded_lower)
            and not any(_tokens_near_match(t, rs) for rs in relationship_semantics_lower)
        ]
        if len(leftover_terms) != 1:
            return _disqualified("relationship_entity_not_grounded_in_question_text", attempted=True)

        repair_idx = ungrounded_indices[0]
        repaired_entities = list(validated_entities)
        repaired_entities[repair_idx] = leftover_terms[0]

        repaired_validated = validate_ai_entity_hypotheses(
            source_id, repaired_entities, concepts + measures + raw_dimension_terms,
            single_entity_question=False, session=session,
        )
        if len(repaired_validated) != len(repaired_entities) or not all(
            any(_tokens_near_match(e.lower(), t) for t in deterministic_terms_lower) for e in repaired_validated
        ):
            # The repaired name came directly from deterministic_terms, so
            # this should be unreachable in practice -- kept as a fail-
            # closed backstop, never trust the repair without re-checking
            # both gates it must still clear.
            return _disqualified("relationship_entity_not_grounded_in_question_text", attempted=True)

        validated_entities = repaired_validated

    # Live-confirmed against a real production database Golden Set (source of a real wrong-
    # shaped-answer regression, same class as the single-entity "List
    # instructor names" bug fixed above): "Show students and their user
    # accounts" -> entities=("student","user"), requested_attributes=()
    # (the AI interpreter doesn't reliably populate that field here either).
    # extract_terms finds a THIRD term, "accounts", that neither entity
    # accounts for -- exactly the milestone brief's own worked example of a
    # question that must SAFELY REFUSE (Part 10: "If Student resolves but
    # User Account remains genuinely ambiguous: SAFE REFUSAL /
    # CLARIFICATION"), not confidently return a relationship_list whose
    # PII-safe auto-selected columns silently omit the specifically-
    # requested "accounts"/Username attribute. Inverse of the grounding
    # check above (every ENTITY must have a matching term); this checks
    # every TERM has a matching entity -- a leftover, unaccounted-for term
    # is the same reliable signal the single-entity path already uses.
    # AI Physical Planner Coverage Expansion, Part F — a leftover term that
    # matches one of the AI's own relationship_semantics words (e.g.
    # "enrolled" in "students and their enrolled classes") is evidence for
    # CHOOSING a relationship path between the named entities, not a
    # requested output column -- consumed here, not treated as the same
    # "unaccounted-for attribute request" signal a genuinely different
    # leftover term (e.g. "accounts" in "students and their user accounts",
    # still correctly disqualified below) is.
    #
    # Compound Query Coverage, Part 6 — a leftover term the AI itself
    # already named as a requested_attributes entry (e.g. "location" in
    # "students and their enrolled classes with class location") is a
    # genuine, explicit output-column request, not an unaccounted-for term
    # -- exempted here so _resolve_relationship_requested_attributes below
    # gets a chance to resolve it against a real column, same "explicit
    # request, never silently guessed or dropped" contract the single-
    # entity path's ai_interpretation.requested_attributes already has.
    relationship_semantics_lower = [t.lower() for t in ai_interpretation.relationship_semantics]
    # A deterministic leftover term is always a single word (extract_terms'
    # own contract), but a requested_attributes entry can be a multi-word
    # phrase (e.g. "class name") -- matched word-by-word, not as one whole
    # near-match string, so each individual word of the phrase is exempted.
    requested_attribute_words = {
        w for ra in ai_interpretation.requested_attributes for w in ra.lower().split()
    }
    entities_lower = [e.lower() for e in validated_entities]
    for term in deterministic_terms_lower:
        if any(_tokens_near_match(term, e) for e in entities_lower):
            continue
        if any(_tokens_near_match(term, rs) for rs in relationship_semantics_lower):
            continue
        if any(_tokens_near_match(term, w) for w in requested_attribute_words):
            continue
        return _disqualified("relationship_extra_term_implies_attribute_request", attempted=True)

    from core.semantic.candidate_adjudicator import ROLE_RELATIONSHIP_ENTITY, adjudicate_candidates
    from data.semantic_contract_service import gather_candidate_evidence

    resolved: dict[str, dict] = {}
    adjudication_calls = 0
    for entity_name in validated_entities:
        evidence_bundle = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
        evidence = evidence_bundle.get("evidence") or []
        if not evidence:
            return _disqualified("no_candidate_evidence", attempted=True, call_count=adjudication_calls)

        adjudication = adjudicate_candidates(
            entity_name, evidence, None, session=session, simple_selection_only=True,
            role_context=ROLE_RELATIONSHIP_ENTITY,
        )
        adjudication_calls += 1
        if adjudication is None:
            return _disqualified("adjudication_unavailable_or_failed", attempted=True, call_count=adjudication_calls)
        if adjudication.no_safe_selection or not adjudication.selected_table_fqn:
            return _disqualified("no_safe_table_selection", attempted=True, call_count=adjudication_calls)

        fqn = adjudication.selected_table_fqn
        cand_info = evidence_bundle["candidates_by_fqn"].get(fqn)
        if cand_info is None:
            return _disqualified(
                "selected_table_missing_from_evidence", attempted=True, call_count=adjudication_calls,
            )

        candidate = cand_info["candidate"]
        if (
            (candidate.get("name_score") or 0.0) < _MIN_TRUSTED_NAME_SCORE
            and (
                not _vocabulary_trusts_candidate(entity_name, candidate)
                or _vocabulary_trust_is_contested(fqn, evidence)
            )
        ):
            # NEXT MILESTONE, Part 4 — same vocabulary-corroboration
            # extension as the single-entity path (see
            # _vocabulary_trusts_candidate's docstring). MULTI-HOP GROUPING
            # GAP FIX also refuses here (this path has no retry mechanism)
            # when the vocabulary hit is contested by a stronger, connected
            # competitor already in the same evidence pool
            # (_vocabulary_trust_is_contested) — safe refusal, not a guess.
            return {
                "attempted": True, "valid": False,
                "reason": (
                    f"weak_name_relevance_evidence: name_score={candidate.get('name_score')!r} "
                    f"for {fqn} (entity={entity_name})"
                ),
                "adjudication_call_count": adjudication_calls, "selected_table_fqns": [fqn],
                "physical_plan": None, "query_plan": None, "sql_plan": None, "generated": None,
            }
        resolved[entity_name] = {
            "fqn": fqn, "candidate": candidate, "ctx": cand_info["ctx"],
            "all_candidates": [info["candidate"] for info in evidence_bundle["candidates_by_fqn"].values()],
        }

    selected_fqns = [resolved[e]["fqn"] for e in validated_entities]
    if len(set(selected_fqns)) != len(selected_fqns):
        # Two named entities collapsed to the same physical table -- not a
        # genuine relationship (mirrors _try_build_relationship_plan's own
        # same-table-collision check).
        return _disqualified(
            "relationship_entities_collapsed_to_same_table", attempted=True, call_count=adjudication_calls,
        )

    concepts_out = [
        {"term": e, "selected": resolved[e]["candidate"], "candidates": resolved[e]["all_candidates"]}
        for e in validated_entities
    ]
    columns = {
        resolved[e]["fqn"]: [c["column_name"] for c in (resolved[e]["ctx"].get("columns") or [])]
        for e in validated_entities
    }
    confidence = min(resolved[e]["candidate"].get("score") or 0.0 for e in validated_entities)

    # Compound Query Coverage, Part 6 — requested_attributes resolved
    # AFTER every entity is grounded and the leftover-term check above has
    # already exempted them: query_plan["dimensions"] becomes just another
    # plain SELECT column on top of the relationship_list intent's existing
    # SELECT carve-out, the SAME shape the single-entity bare-list branch
    # already produces for this field -- reused unchanged, no new SQL shape.
    #
    # 2026-08-24 "autonomous business-concept expansion" milestone — moved
    # BEFORE the join-plan computation below (was after): a category hit
    # (e.g. "contact information" -> dnnuser.Users.Email) can resolve onto a
    # table that is NOT one of the participating entities' own tables --
    # extra_tables must be folded into selected_fqns/columns before
    # _plan_joins ever runs, or the join graph never includes it and
    # build_sql_plan's own deterministic validation correctly refuses
    # ("Reference(s) to table(s) outside the FROM/JOIN graph") — live-
    # confirmed against a real production database as the exact failure this reordering
    # fixes for "Show students with their programs and contact information".
    relationship_dimensions: list[dict] = []
    if ai_interpretation.requested_attributes:
        attrs_resolved = _resolve_relationship_requested_attributes(
            source_id, user_id, validated_entities, resolved, ai_interpretation.requested_attributes,
            session=session,
        )
        if attrs_resolved is None:
            return _disqualified(
                "requested_attribute_unresolvable_out_of_stage3_scope",
                attempted=True, call_count=adjudication_calls,
            )
        relationship_dimensions, extra_tables = attrs_resolved
        for extra_fqn, extra_ctx in extra_tables.items():
            if extra_fqn not in selected_fqns:
                selected_fqns.append(extra_fqn)
                columns[extra_fqn] = [c["column_name"] for c in (extra_ctx.get("columns") or [])]

    from data.query_planning_service import _plan_joins

    join_plan: dict | None = None
    primary_table: str | None = None
    for candidate_entity in validated_entities:
        candidate_primary = resolved[candidate_entity]["fqn"]
        candidate_plan = _plan_joins(source_id, user_id, candidate_primary, set(selected_fqns))
        if join_plan is None or len(candidate_plan["steps"]) < len(join_plan["steps"]):
            primary_table, join_plan = candidate_primary, candidate_plan

    query_plan = {
        "source_id": source_id,
        "intent": {**query_intent, "type": "relationship_list"},
        "tables": selected_fqns,
        "columns": columns,
        "measures": [],
        "dimensions": relationship_dimensions,
        "concepts": concepts_out,
        "filters": [],
        "join_plan": join_plan,
        "warnings": [],
        "confidence": confidence,
        "explanation": [
            f"AI physical planner selected {resolved[e]['fqn']} for entity '{e}'." for e in validated_entities
        ],
        "candidate_ranking": [],
        "remembered_terminology": [],
        "generated_vocabulary_evidence": [],
        "entity": None,
        "entity_table": None,
        "entity_confidence": None,
        "entity_candidates": [],
        "metadata_search_failed": False,
        "metadata_search_failed_reason": None,
        "grounding": None,
    }

    plan_tables = tuple(
        PlanTable(
            candidate_id=resolved[e]["fqn"], table_fqn=resolved[e]["fqn"],
            role="primary" if resolved[e]["fqn"] == primary_table else "related",
        )
        for e in validated_entities
    )
    plan_joins = tuple(
        PlanJoinEdge(
            left_table=step["from_table"], left_column=step["from_column"],
            right_table=step["to_table"], right_column=step["to_column"],
        )
        for step in (join_plan.get("steps") or []) if step.get("path_found")
    )
    physical_plan = PhysicalQueryPlan(
        intent="relationship_list",
        source_id=source_id,
        tables=plan_tables,
        joins=plan_joins,
        expected_result_shape=ai_interpretation.expected_result_shape,
        confidence=confidence,
        candidate_provenance={resolved[e]["fqn"]: resolved[e]["fqn"] for e in validated_entities},
    )

    from data.sql_generation_service import detect_dialect, generate_sql
    from data.sql_planning_service import build_sql_plan
    from core.perf import stage_timer

    with stage_timer.measure("sql_plan_construction"):
        sql_plan = build_sql_plan(source_id, user_id, query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii)
    if not sql_plan["validation"]["valid"]:
        return {
            "attempted": True, "valid": False,
            "reason": "deterministic_validation_failed: " + "; ".join(sql_plan["validation"]["blocking_reasons"]),
            "adjudication_call_count": adjudication_calls, "selected_table_fqns": selected_fqns,
            "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": None,
        }

    with stage_timer.measure("sql_generation"):
        generated = generate_sql(source_id, user_id, sql_plan, dialect=detect_dialect(source_id))
    if not generated.get("sql"):
        return {
            "attempted": True, "valid": False, "reason": "sql_generation_produced_no_sql",
            "adjudication_call_count": adjudication_calls, "selected_table_fqns": selected_fqns,
            "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": generated,
        }

    return {
        "attempted": True, "valid": True, "reason": None,
        "adjudication_call_count": adjudication_calls, "selected_table_fqns": selected_fqns,
        "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": generated,
    }


# ---------------------------------------------------------------------------
# Executive Report Query Foundation milestone — structured KPI physical
# planning. Unlike attempt_ai_physical_plan (which parses one raw NL
# question through core.semantic.ai_interpreter), this entry point takes an
# already-structured KPI request (core.reporting.kpi_spec.KPIQuerySpec,
# adapted by the caller) — the report orchestrator (Part 9) resolves report
# intent into these bounded, explicit terms BEFORE calling here, so no NL
# entity/aggregation/dimension DETECTION happens in this function at all.
# Every table/column reference is still resolved only via the SAME
# real-evidence primitives Stage 1-3 above use
# (gather_candidate_evidence/adjudicate_candidates/_resolve_ai_measure_column/
# _resolve_ai_measure_cross_table/_resolve_non_calendar_dimension/
# _resolve_calendar_grain_dimension/_plan_joins) — nothing here is invented,
# and data.sql_planning_service.build_sql_plan / data.sql_generation_service.
# generate_sql remain completely unchanged and are the only things that ever
# produce a validated plan/SQL. attempt_ai_physical_plan's own interactive
# NL path above is untouched by everything below — this is a new, additive
# entry point, not a modification of it.
#
# Multi-measure handling (Part 2): measures sharing the same aggregation
# verb are combined into ONE SELECT (query_plan["measures"] already accepts
# a list — data.sql_planning_service._select_entries/_build_group_by and
# data.sql_generation_service already iterate it generically); measures
# using DIFFERENT aggregation verbs cannot share one SELECT (_select_entries
# applies its `aggregation` parameter uniformly to a whole entries list — a
# genuine SQL-generation constraint, not an AI-side scope restriction), so
# they are grouped by aggregation verb and returned as separate `plans`
# entries, each independently built/validated/SQL-generated through the
# unchanged validator — never a fabricated combined query. Fan-out
# coexistence (Part 2's "prove the measures can coexist on the selected join
# grain, or split") is proven per measure against the SAME cross-table-join
# grain every other measure/dimension in this call shares: a measure whose
# own table sits on the "one" side of that combined join
# (_fqn_exposed_to_fanout) is excluded from every plan this call returns
# (`excluded_measures`) rather than silently accepting a possibly-inflated
# SUM/AVG/MIN/MAX/COUNT — the caller may retry an excluded measure alone (no
# competing dimension/measure forcing the risky join) as its own follow-up
# call, which is exactly "split into multiple validated PhysicalQueryPlans".
#
# Calendar + non-calendar dimensions together (Part 4) and non-COUNT
# calendar-grain aggregation (Part 3) are both in scope here — this function
# does not re-impose attempt_ai_physical_plan's own deliberately-conservative
# Stage 2 restriction (a calendar grain mixed with anything else, or paired
# with a non-COUNT aggregation, both disqualify there): the calendar
# dimension and every non-calendar dimension are resolved independently via
# the SAME two resolvers, unconditionally allowed to coexist, still bounded
# to at most one calendar grain and _MAX_MULTI_DIMENSIONS non-calendar terms.
# ---------------------------------------------------------------------------

_MAX_KPI_MEASURES = 5


def attempt_structured_kpi_plan(
    source_id: int, user_id: str, *,
    entity_name: str,
    business_question: str,
    measures: list[dict],
    calendar_grain: str | None = None,
    dimension_terms: list[str] | None = None,
    date_range: dict | None = None,
    session=None,
    allow_unconfirmed_pii: bool = False,
) -> dict:
    """Build one or more validated PhysicalQueryPlans for a structured KPI
    request.

    `measures` is a non-empty list of {"term": str, "aggregation":
    "COUNT"|"DISTINCT_COUNT"|"SUM"|"AVG"|"MIN"|"MAX"}, bounded to
    _MAX_KPI_MEASURES. `dimension_terms` is a bounded list of non-calendar
    business terms (e.g. "program"); `calendar_grain` is an optional single
    "year"|"month"|"quarter"|"week"|"day" term — both may be supplied
    together (Part 4). `date_range` is an optional {"start", "end"} pair
    filtered against the entity's own best date column.

    Returns:
        {"attempted": bool, "valid": bool, "reason": str | None,
         "adjudication_call_count": int, "selected_table_fqns": list[str],
         "plans": [{"measure_terms": [...], "aggregation": str,
                     "physical_plan": PhysicalQueryPlan, "query_plan": dict,
                     "sql_plan": dict, "generated": dict}, ...],
         "excluded_measures": [{"term": str, "reason": str}]}

    `valid` is True only when at least one plan was built; `plans` may
    legitimately be a partial list (fewer than requested) when some measures
    were excluded for fan-out safety or failed validation — the caller
    (report orchestrator) decides whether a partial KPI result is
    acceptable, and never silently treats an excluded measure as answered.
    """
    from data.query_planning_service import _CALENDAR_GRAIN_TERMS

    if not measures or len(measures) > _MAX_KPI_MEASURES:
        return _disqualified("kpi_measure_count_out_of_scope")
    dimension_terms = [d.lower() for d in (dimension_terms or [])][:_MAX_MULTI_DIMENSIONS]
    if calendar_grain is not None and calendar_grain.lower() not in _CALENDAR_GRAIN_TERMS:
        return _disqualified("invalid_calendar_grain")

    from data.semantic_contract_service import gather_candidate_evidence, specialize_entity_term
    from core.semantic.candidate_adjudicator import ROLE_ENTITY, adjudicate_candidates

    # Specialized Concept Principle (Final Demo Hardening, Part 2/3) — same
    # widening as attempt_ai_physical_plan's single-entity path above: a
    # caller-supplied entity_name (e.g. a report/KPI opportunity generator
    # that itself only names the generic concept) must not silently absorb
    # a business_question that names a more specific one. A no-op whenever
    # business_question doesn't add anything entity_name doesn't already
    # cover — see specialize_entity_term's own docstring.
    from core.semantic.concept_resolver import extract_terms as _extract_terms_for_kpi
    _kpi_concepts, _kpi_measures, _kpi_dims = _extract_terms_for_kpi(business_question)
    entity_name = specialize_entity_term(
        entity_name, business_question, _kpi_concepts + _kpi_measures + _kpi_dims,
    )

    evidence_bundle = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return _disqualified("no_candidate_evidence", attempted=True)

    adjudication = adjudicate_candidates(
        entity_name, evidence, None, session=session, simple_selection_only=True,
        role_context=ROLE_ENTITY,
    )
    if adjudication is None:
        return _disqualified("adjudication_unavailable_or_failed", attempted=True, call_count=1)
    if adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return _disqualified("no_safe_table_selection", attempted=True, call_count=1)

    fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(fqn)
    if cand_info is None:
        return _disqualified("selected_table_missing_from_evidence", attempted=True, call_count=1)

    candidate = cand_info["candidate"]
    ctx = cand_info["ctx"]
    adjudication_call_count = 1
    if (
        (candidate.get("name_score") or 0.0) < _MIN_TRUSTED_NAME_SCORE
        and (
            not _vocabulary_trusts_candidate(entity_name, candidate)
            or _vocabulary_trust_is_contested(fqn, evidence)
        )
    ):
        # NEXT MILESTONE, Part 4 — same vocabulary-corroboration extension
        # as the single-entity Q&A path (see _vocabulary_trusts_candidate's
        # docstring); entity_name here is a KPI domain/table term, the same
        # "what is this row" class of business noun. MULTI-HOP GROUPING GAP
        # FIX also routes here (instead of silently trusting) when the
        # vocabulary hit is contested by a stronger, connected competitor
        # already in the same evidence pool (_vocabulary_trust_is_contested).
        anchor_term = measures[0]["term"] if measures else (dimension_terms[0] if dimension_terms else None)
        retry_adjudication = _corroborate_weak_entity_candidate(
            source_id, user_id, entity_name, evidence_bundle, anchor_term, None, session=session,
        )
        retry_cand_info = (
            evidence_bundle["candidates_by_fqn"].get(retry_adjudication.selected_table_fqn)
            if retry_adjudication is not None else None
        )
        if retry_adjudication is None or retry_cand_info is None:
            return {
                "attempted": True, "valid": False,
                "reason": f"weak_name_relevance_evidence: name_score={candidate.get('name_score')!r} for {fqn}",
                "adjudication_call_count": 2 if anchor_term else 1, "selected_table_fqns": [fqn],
                "plans": [], "excluded_measures": [],
            }
        adjudication = retry_adjudication
        fqn = retry_adjudication.selected_table_fqn
        cand_info = retry_cand_info
        candidate = cand_info["candidate"]
        ctx = cand_info["ctx"]
        adjudication_call_count = 2

    from data.query_planning_service import _date_order_hint_root

    other_table_ctxs: dict[str, dict] = {}
    hint_root = _date_order_hint_root(business_question)

    # --- calendar-grain dimension (at most one) -----------------------
    grain_plan_entry: dict | None = None
    if calendar_grain is not None:
        from data.query_planning_service import _resolve_calendar_grain_dimension

        grain_dimension = _resolve_calendar_grain_dimension(
            calendar_grain.lower(), fqn, ctx, [hint_root] if hint_root else [],
        )
        if not grain_dimension["selected"]:
            return _disqualified(
                "calendar_dimension_unresolvable", attempted=True, call_count=adjudication_call_count,
            )
        grain_plan_entry = grain_dimension

    # --- non-calendar dimensions (bounded list) ------------------------
    non_calendar_entries: list[dict] = []
    for term in dimension_terms:
        dim_resolution = _resolve_non_calendar_dimension(source_id, user_id, term, fqn, session=session)
        if dim_resolution is None:
            return _disqualified(
                "dimension_unresolvable_out_of_scope", attempted=True, call_count=adjudication_call_count + 1,
            )
        adjudication_call_count += 1
        non_calendar_entries.append(dim_resolution)
        if dim_resolution["dim_fqn"] != fqn:
            other_table_ctxs[dim_resolution["dim_fqn"]] = dim_resolution["dim_ctx"]

    # --- measures --------------------------------------------------------
    resolved_measures: list[dict] = []
    for m in measures:
        term = m["term"]
        agg = (m.get("aggregation") or "").upper()
        if agg not in ({"COUNT", "DISTINCT_COUNT"} | _NON_COUNT_AGGREGATIONS):
            return _disqualified(
                "kpi_measure_aggregation_out_of_scope", attempted=True, call_count=adjudication_call_count,
            )
        if agg in ("COUNT", "DISTINCT_COUNT"):
            from data.query_planning_service import enrich_entity_count_selection

            selected = enrich_entity_count_selection(
                source_id, user_id, {"table_fqn": fqn}, distinct_requested=(agg == "DISTINCT_COUNT"),
            )
            resolved_measures.append({
                "term": term, "aggregation": "COUNT", "measure_fqn": fqn, "measure_ctx": ctx,
                "selected": selected,
            })
        else:
            selected = _resolve_ai_measure_column(fqn, ctx, term)
            measure_fqn, measure_ctx = fqn, ctx
            if selected is None:
                cross = _resolve_ai_measure_cross_table(source_id, user_id, term, fqn, ctx, session=session)
                if cross is None:
                    return _disqualified(
                        "kpi_measure_column_unresolvable", attempted=True, call_count=adjudication_call_count,
                    )
                adjudication_call_count += 1
                measure_fqn = cross["measure_fqn"]
                measure_ctx = cross["measure_ctx"]
                selected = cross["measure_selected"]
            resolved_measures.append({
                "term": term, "aggregation": agg, "measure_fqn": measure_fqn, "measure_ctx": measure_ctx,
                "selected": selected,
            })
            if measure_fqn != fqn:
                other_table_ctxs[measure_fqn] = measure_ctx

    # --- optional date range filter ---------------------------------------
    plan_filters: list[PlanFilter] = []
    resolved_filters: list[dict] = []
    filter_warnings: list[dict] = []
    if date_range:
        from data.query_planning_service import _find_date_column_with_hint

        date_col = _find_date_column_with_hint({fqn: ctx}, fqn, hint_root)
        if date_col:
            date_fqn, col_name = date_col
            resolved_filters.append({
                "column": col_name, "operator": "BETWEEN",
                "value": [date_range["start"], date_range["end"]],
                "resolved": True, "table_fqn": date_fqn,
            })
            plan_filters.append(PlanFilter(
                table_fqn=date_fqn, column_name=col_name, operator="BETWEEN",
                value=[date_range["start"], date_range["end"]],
            ))
        else:
            return _disqualified(
                "kpi_date_range_column_unresolvable", attempted=True, call_count=adjudication_call_count,
            )

    # --- unified cross-table join, computed once --------------------------
    combined_steps: list[dict] = []
    combined_join_plan: dict | None = None
    if other_table_ctxs:
        from data.query_planning_service import _plan_joins

        full_table_set = {fqn, *other_table_ctxs.keys()}
        combined_join_plan = _plan_joins(source_id, user_id, fqn, full_table_set)
        combined_steps = combined_join_plan.get("steps") or []
        if not combined_steps or not all(step.get("path_found") for step in combined_steps):
            return _disqualified(
                "kpi_cross_table_join_path_unresolvable", attempted=True, call_count=adjudication_call_count,
            )

    # --- per-measure fan-out safety, Part 2 coexistence proof --------------
    excluded_measures: list[dict] = []
    safe_measures: list[dict] = []
    if combined_steps:
        from data.query_planning_service import _apply_join_fanout_safety

        for rm in resolved_measures:
            if rm["aggregation"] == "COUNT":
                probe = [{"term": rm["term"], "selected": dict(rm["selected"]), "warnings": []}]
                fanout_plan = combined_join_plan
                if (
                    (fanout_plan or {}).get("fanout_risk") not in ("MEDIUM", "HIGH")
                    and _fqn_exposed_to_fanout(rm["measure_fqn"], combined_steps)
                ):
                    fanout_plan = dict(combined_join_plan)
                    fanout_plan["fanout_risk"] = "MEDIUM"
                    fanout_plan["required"] = True
                _apply_join_fanout_safety(probe, fanout_plan or {}, [])
                if probe[0]["selected"]:
                    safe_measures.append({**rm, "selected": probe[0]["selected"]})
                else:
                    excluded_measures.append({"term": rm["term"], "reason": "count_fanout_safety"})
            else:
                if _fqn_exposed_to_fanout(rm["measure_fqn"], combined_steps):
                    excluded_measures.append({"term": rm["term"], "reason": "aggregate_fanout_safety"})
                else:
                    safe_measures.append(rm)
    else:
        safe_measures = resolved_measures

    if not safe_measures:
        return {
            "attempted": True, "valid": False, "reason": "all_kpi_measures_fanout_unsafe",
            "adjudication_call_count": adjudication_call_count,
            "selected_table_fqns": [fqn] + list(other_table_ctxs.keys()),
            "plans": [], "excluded_measures": excluded_measures,
        }

    # --- shared dimensions (both calendar and non-calendar, Part 4) -------
    plan_group_by: tuple[str, ...] = ()
    plan_dim_columns: list[PlanColumn] = []
    query_plan_dimensions: list[dict] = []
    if grain_plan_entry is not None:
        gsel = grain_plan_entry["selected"]
        plan_group_by = plan_group_by + (gsel["column_name"],)
        plan_dim_columns.append(PlanColumn(
            table_fqn=fqn, column_name=gsel["column_name"], role="dimension",
            time_grain=calendar_grain.lower(),
        ))
        query_plan_dimensions.append(grain_plan_entry)
    for dim_resolution in non_calendar_entries:
        gsel = dim_resolution["group_selected"]
        plan_group_by = plan_group_by + (gsel["column_name"],)
        plan_dim_columns.append(PlanColumn(
            table_fqn=gsel["table_fqn"], column_name=gsel["column_name"], role="dimension",
        ))
        query_plan_dimensions.append({
            "term": gsel.get("column_name"), "selected": gsel, "candidates": [gsel], "warnings": [],
        })

    has_dimensions = bool(plan_group_by)
    chronological_order: dict | None = None
    if grain_plan_entry is not None:
        gsel = grain_plan_entry["selected"]
        chronological_order = {
            "table_fqn": gsel["table_fqn"], "column_name": gsel["column_name"], "direction": "ASC",
        }

    # --- group surviving measures by aggregation verb, one plan per group -
    # (Part 2: never fabricate one SELECT across incompatible aggregation
    # verbs — see module-level comment above this function).
    groups: dict[str, list[dict]] = {}
    for rm in safe_measures:
        groups.setdefault(rm["aggregation"], []).append(rm)

    all_tables = [fqn] + list(other_table_ctxs.keys())
    join_plan_dict = combined_join_plan or {
        "tables": all_tables, "primary_table": fqn, "joins": [], "required": False, "fanout_risk": None,
    }
    plan_joins = tuple(
        PlanJoinEdge(
            left_table=step["from_table"], left_column=step["from_column"],
            right_table=step["to_table"], right_column=step["to_column"],
        )
        for step in combined_steps if step.get("path_found")
    )
    extra_plan_tables = tuple(
        PlanTable(candidate_id=t, table_fqn=t, role="related") for t in other_table_ctxs.keys()
    )
    all_columns = {
        fqn: [c["column_name"] for c in (ctx.get("columns") or [])],
        **{t: [c["column_name"] for c in (other_table_ctxs[t].get("columns") or [])] for t in other_table_ctxs},
    }

    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import detect_dialect, generate_sql

    plans: list[dict] = []
    for agg_key, group_measures in groups.items():
        measure_entries = [
            {"term": m["term"], "selected": m["selected"], "candidates": [], "warnings": []}
            for m in group_measures
        ]
        plan_columns: list[PlanColumn] = list(plan_dim_columns)
        for m in group_measures:
            plan_columns.append(PlanColumn(
                table_fqn=m["measure_fqn"], column_name=m["selected"]["column_name"], role="measure",
                aggregation=agg_key, distinct=bool(m["selected"].get("distinct")),
            ))

        intent: dict[str, Any] = {
            "aggregation": agg_key,
            "type": "aggregate_by_dimension" if has_dimensions else "aggregate_overall",
        }
        # Deliberately NOT forwarded into intent["order"]: chronological_order
        # names the RAW grain date column (e.g. TransactionDate), but once a
        # calendar grain is grouped the SELECT/GROUP BY project under the
        # grain's alias (e.g. MONTH(TransactionDate) AS transaction_month) —
        # ordering by the raw column is then a column that is neither
        # aggregated nor in GROUP BY, which every real SQL engine (live-
        # confirmed against SQL Server: "Column ... is invalid in the ORDER
        # BY clause because it is not contained in either an aggregate
        # function or the GROUP BY clause") rejects.
        # data.sql_planning_service.build_sql_plan already has its own
        # generic default-chronological-ordering fallback for exactly this
        # shape (any time-grain GROUP BY, on any table) that correctly
        # orders by the grain's own SELECT alias — reused here by simply
        # leaving intent["order"] unset, rather than overriding it with this
        # construction's own invalid raw-column form. chronological_order is
        # still recorded on the PhysicalQueryPlan below for traceability.

        query_plan = {
            "source_id": source_id, "intent": intent, "tables": all_tables, "columns": all_columns,
            "measures": measure_entries, "dimensions": query_plan_dimensions,
            "concepts": [{"term": entity_name, "selected": candidate, "candidates": []}],
            "filters": resolved_filters, "join_plan": join_plan_dict, "warnings": filter_warnings,
            "confidence": adjudication.confidence,
            "explanation": [
                f"AI report planner selected {fqn} for KPI entity '{entity_name}' "
                f"(confidence {adjudication.confidence:.2f}).",
            ],
            "candidate_ranking": [], "remembered_terminology": [], "generated_vocabulary_evidence": [],
            "entity": None, "entity_table": None, "entity_confidence": adjudication.confidence,
            "entity_candidates": [], "metadata_search_failed": False,
            "metadata_search_failed_reason": None, "grounding": None,
        }

        physical_plan = PhysicalQueryPlan(
            intent="aggregate", source_id=source_id,
            tables=(PlanTable(candidate_id=fqn, table_fqn=fqn, role="primary"),) + extra_plan_tables,
            columns=tuple(plan_columns), filters=tuple(plan_filters), group_by=plan_group_by,
            order_by=chronological_order, joins=plan_joins, expected_result_shape="tabular",
            confidence=adjudication.confidence,
            candidate_provenance={t: t for t in all_tables},
            evidence_references=adjudication.evidence_references,
        )

        from core.perf import stage_timer as _stage_timer_kpi
        with _stage_timer_kpi.measure("sql_plan_construction"):
            sql_plan = build_sql_plan(source_id, user_id, query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii)
        if not sql_plan["validation"]["valid"]:
            reason = "deterministic_validation_failed: " + "; ".join(sql_plan["validation"]["blocking_reasons"])
            excluded_measures.extend({"term": m["term"], "reason": reason} for m in group_measures)
            continue

        with _stage_timer_kpi.measure("sql_generation"):
            generated = generate_sql(source_id, user_id, sql_plan, dialect=detect_dialect(source_id))
        if not generated.get("sql"):
            excluded_measures.extend(
                {"term": m["term"], "reason": "sql_generation_produced_no_sql"} for m in group_measures
            )
            continue

        plans.append({
            "measure_terms": [m["term"] for m in group_measures], "aggregation": agg_key,
            "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": generated,
        })

    if not plans:
        return {
            "attempted": True, "valid": False, "reason": "no_kpi_plan_survived_validation",
            "adjudication_call_count": adjudication_call_count, "selected_table_fqns": all_tables,
            "plans": [], "excluded_measures": excluded_measures,
        }

    return {
        "attempted": True, "valid": True, "reason": None,
        "adjudication_call_count": adjudication_call_count, "selected_table_fqns": all_tables,
        "plans": plans, "excluded_measures": excluded_measures,
    }


# ---------------------------------------------------------------------------
# FINISH INTERACTIVE AI Q&A WIRING milestone, Part 1/2 — Multi-Entity
# Aggregation.
#
# Live-confirmed against a real production database: "How many students are in each program?"
# and "How many enrollments per instructor?" both get TWO AI-named entities
# (relationship_intent list_with_related or scoped_count) even though the
# question's own grammar makes the roles unambiguous -- one entity is being
# COUNTED, the other is the thing it's being GROUPED BY. That is exactly the
# single-entity-plus-non-calendar-dimension shape the prior milestone's
# Part 11 already built and proved (_resolve_non_calendar_dimension already
# does full gather_candidate_evidence/adjudicate_candidates resolution for a
# dimension term against a genuinely different table, with the SAME
# _MIN_TRUSTED_NAME_SCORE trust gate and the same display-column/label/key
# fallback chain a second relationship entity would get) -- there is no
# missing capability here, only a missing RECLASSIFICATION step routing this
# specific two-entity shape into the single-entity pipeline instead of
# Stage 3's plain relationship-list path (which has no aggregation support)
# or the bare not_exactly_one_entity gate. Reusing that existing pipeline
# means every downstream capability (fan-out safety, filters, existence
# qualifiers, ranking, date ranges) already applies unchanged -- this is
# deliberately NOT a new relationship-aggregate planner.
# ---------------------------------------------------------------------------

_GROUPING_MARKERS = (
    "per ", "for each ", "in each ", "grouped by ", "broken down by ", "split by ", "each ", "by ",
)
# AI-First System-Wide Intelligence milestone, Part 7 -- plain "by " added
# above. Live-confirmed against a generic (non-the reference database) synthetic fixture:
# "Average invoice amount BY account" carries two AI-named entities
# (invoice, account) and a real AVG aggregation, but none of the more
# specific markers above ("per "/"each "/"grouped by "/...) match "by
# account" -- _detect_relationship_grouping_entity returned None, so this
# reclassification no-opped, the question fell through to Stage 3's plain
# relationship-list path, was disqualified there for carrying an aggregation
# (out of that stage's scope, a category-A disqualification), and legacy's
# own dimension resolution then silently answered with the UNGROUPED overall
# average -- a wrong answer, not a refusal (exactly the failure mode this
# milestone's Part 3 calls out by name). "by " is the single most common
# English breakdown preposition ("sales BY region", "revenue BY product")
# and is safe to add here specifically (unlike in _MEASURED_ENTITY_MARKERS
# below, where "top 5 clients BY revenue" must NOT be read as an implicit
# entity count): _entity_matches only fires when the word(s) immediately
# after "by "/"by the " are literally the OTHER AI-named entity's own head
# term, so a named MEASURE word standing between "by" and anything (e.g.
# "top 5 accounts by total orders") never accidentally matches, and this
# whole function only runs once query_intent.get("aggregation") is already
# confirmed truthy by the caller -- a bare ranking phrase with no
# aggregation/counting language ("Which accounts have the most orders?")
# never reaches this marker set at all (it is handled entirely by
# _reclassify_wh_ranking_as_single_entity's separate wh_ranking detector).

# FINISH INTERACTIVE AI Q&A WIRING milestone, Part 5 — "Top 5 locations by
# NUMBER OF classes"/"Top instructors by COUNT of enrollments": the marker
# phrase sits directly before the MEASURED entity here (the thing being
# counted), not the grouping one -- the opposite role from _GROUPING_MARKERS
# above. "by classes"/"by class" alone is deliberately NOT included (same
# false-positive risk "with a/an X" was excluded for in the prior milestone
# -- "top 5 clients by revenue" is a real, different, already-supported
# ranking-by-named-measure shape, not an implicit entity count).
_MEASURED_ENTITY_MARKERS = ("number of ", "count of ", "total number of ")


def _detect_relationship_grouping_entity(question: str, entities: tuple[str, ...]) -> int | None:
    """Returns the index (into `entities`) of the entity the question's own
    text marks as the GROUPING role ("students PER program", "enrollments
    PER instructor", "students in EACH program", "Top 5 locations by NUMBER
    OF classes") via a closed, general set of grouping-preposition/implicit-
    count markers -- never inferred from entity order alone, since nothing
    in AIQuestionInterpretation's schema states which of two named entities
    is measured vs. grouped-by. Only the entity's OWN first word is checked
    (multi-word entity names are rare here and a full-phrase match would be
    brittle against articles/plurals) -- matches the same "first word is
    enough, never over-match" discipline already used elsewhere in this
    module. Returns None when the two marker sets don't agree on exactly
    one grouping index -- ambiguous or absent, callers must not guess which
    role is which."""
    q = (question or "").lower()

    def _entity_matches(idx: int, markers: tuple[str, ...]) -> bool:
        head = (entities[idx].lower().split() or [""])[0]
        if not head:
            return False
        return any(re.search(re.escape(marker) + r"(?:the\s+)?" + re.escape(head), q) for marker in markers)

    grouping_matches = [idx for idx in range(len(entities)) if _entity_matches(idx, _GROUPING_MARKERS)]
    if len(grouping_matches) == 1:
        return grouping_matches[0]

    # Inverse role: a MEASURED-entity marker on entity idx implies the
    # OTHER entity is the grouping one -- only safe with exactly 2 entities
    # (this module's only caller shape), and only when the grouping-marker
    # search above found nothing (a direct grouping marker is always
    # stronger, more specific evidence than the inverse inference).
    if len(entities) == 2:
        measured_matches = [idx for idx in range(len(entities)) if _entity_matches(idx, _MEASURED_ENTITY_MARKERS)]
        if len(measured_matches) == 1:
            return 1 - measured_matches[0]

    return None


def _reclassify_single_entity_attribute_request_as_single_entity(ai_interpretation: Any) -> Any:
    """RETIRE-LEGACY-FALLBACK MILESTONE — live-confirmed root-cause fix.
    "Show users and their emails."/"Show student contact information and
    their programs." style phrasing ("<entity> and their <attribute>")
    reliably makes the real AI interpreter set relationship_intent=
    "list_with_related" even when only ONE entity is actually named and the
    "related" part is a genuine ATTRIBUTE of that same entity (correctly
    captured in requested_attributes), not a second entity at all --
    confirmed directly: AIQuestionInterpretation(entities=('user',),
    requested_attributes=('email',), relationship_intent='list_with_related')
    for "Show users and their emails." Before this reclassification, Stage
    3's relationship-list dispatch (_attempt_relationship_list_plan) hard-
    disqualified this with relationship_entity_count_out_of_stage3_scope
    (len(entities)==1 not in (2,3,4)) -- a genuine capability-gap A-reason
    that used to silently permit legacy to answer instead (legacy's own
    single-entity bare-list-with-attribute path handles this shape fine);
    now correctly blocked, so this reclassification is what lets AI-first
    actually answer instead of hard-refusing a plain single-entity
    attribute request. Reclassifying to "single_entity" here routes the
    question into attempt_ai_physical_plan's existing single-entity +
    requested_attributes handling (the SAME path "Show student contact
    information." already uses) instead of inventing a new plan shape.
    Returns ai_interpretation UNCHANGED for every other case (more than one
    entity, or no requested_attributes to justify the reclassification) --
    a genuine multi-entity relationship list is untouched."""
    if ai_interpretation.relationship_intent != "list_with_related":
        return ai_interpretation
    if len(ai_interpretation.entities) != 1:
        return ai_interpretation
    if not ai_interpretation.requested_attributes:
        return ai_interpretation
    return replace(ai_interpretation, relationship_intent="single_entity")


def _reclassify_relationship_aggregate_as_single_entity(question: str, ai_interpretation: Any) -> Any:
    """When a question names exactly two entities AND uses explicit
    aggregation/counting language AND the question's own text marks one of
    them as the grouping role (_detect_relationship_grouping_entity),
    reinterpret it as attempt_ai_physical_plan's existing single-entity
    shape: entities becomes just the MEASURED entity, and the grouping
    entity's business term is folded into dimensions (Part 11's already-
    validated non-calendar-dimension mechanism resolves it from there,
    cross-table, via real evidence -- never a lexical guess). Returns
    ai_interpretation UNCHANGED when the two-entity+aggregation+marker
    pattern doesn't hold -- every existing single/multi-entity code path
    (Stage 3 relationship lists, the not_exactly_one_entity refusal) is
    reached exactly as before for every other question shape."""
    if len(ai_interpretation.entities) != 2:
        return ai_interpretation

    from core.semantic.concept_resolver import extract_query_intent

    query_intent = extract_query_intent(question)
    if not query_intent.get("aggregation"):
        return ai_interpretation

    relationship_intent = ai_interpretation.relationship_intent
    if relationship_intent not in ("scoped_count", "scoped_aggregate"):
        relationship_intent = "scoped_count" if query_intent["aggregation"] == "COUNT" else "scoped_aggregate"

    grouping_idx = _detect_relationship_grouping_entity(question, ai_interpretation.entities)
    if grouping_idx is not None:
        measured_entity = ai_interpretation.entities[1 - grouping_idx]
        grouping_entity = ai_interpretation.entities[grouping_idx]
        new_dimensions = ai_interpretation.dimensions
        if not any(d.lower() == grouping_entity.lower() for d in new_dimensions):
            new_dimensions = new_dimensions + (grouping_entity,)
        return replace(
            ai_interpretation,
            entities=(measured_entity,),
            dimensions=new_dimensions,
            relationship_intent=relationship_intent,
        )

    # Part 4 — a two-entity aggregation question with NO grouping marker
    # ("How many students ended their program last quarter?") is not a
    # grouped breakdown at all: the answer is one number, and the second
    # entity ("program") names what the date/filter conceptually relates
    # to, not a GROUP BY target. Live-confirmed against a real production database:
    # EDU_Student already carries its own EndDate column, and
    # _date_order_hint_root already independently derives "ended" -> "end"
    # from the question's own text (no entity name needed for that
    # disambiguation) -- so dropping the second, ungrounded-for-aggregation
    # entity and resolving purely as single-entity-plus-date-filter is safe
    # either way: correct when the measured entity's own table genuinely
    # carries the relevant date/status column, and a safe refusal (the
    # existing date_column_not_found hard-block from the prior milestone's
    # material-qualifier-policy work) when it doesn't -- never a silently
    # invented cross-table join to satisfy the second entity's name. Only
    # applies when a date/status signal is actually present; a bare two-
    # entity aggregation with neither a grouping marker nor a date/status
    # signal is too ambiguous to safely simplify and is left untouched
    # (falls through to the existing not_exactly_one_entity/relationship-
    # list refusal exactly as before).
    if query_intent.get("date_range") or query_intent.get("status_value"):
        return replace(
            ai_interpretation,
            entities=(ai_interpretation.entities[0],),
            relationship_intent=relationship_intent,
        )

    return ai_interpretation


# FINISH INTERACTIVE AI Q&A WIRING milestone, Part 6/7 — Interactive Time-
# Series. "Show enrollment trend by month"/"Show class trend by month" carry
# no "how many"/"count"/"number of" language at all, so extract_query_intent's
# own aggregation detection stays None -- but the business meaning is
# unambiguous once "trend" (now stopworded out of term search, see
# core.semantic.concept_resolver._STOPWORDS' own Part 7 comment) is
# recognized as an analytical-shape word, not a requested column: a bare
# entity "trend" over a calendar dimension means COUNT of that entity per
# period. This is the SAME entity_count measure Stage 1 already knows how to
# build (enrich_entity_count_selection) and the SAME calendar-grain grouping
# Compound Query Coverage already built (_resolve_calendar_grain_dimension,
# grain_term) -- reused completely unchanged; this function only supplies
# the missing aggregation SIGNAL, never a new SQL shape.
_TIME_SERIES_TREND_RE = re.compile(r"\btrends?\b", re.IGNORECASE)


def _apply_implicit_time_series_count(question: str, ai_interpretation: Any, query_intent: dict) -> dict:
    """Returns query_intent unchanged unless ALL of: no aggregation already
    detected (never overrides a real SUM/AVG/MIN/MAX/COUNT signal -- "show
    average fee trend by month" already gets AVG from extract_query_intent's
    own "average" detection and is left untouched), no explicit measure was
    named (a genuine measure trend already has a real aggregation target;
    this only fills the gap for a BARE entity trend, where "count of rows
    over time" is the only sensible reading), the question's own text
    literally uses "trend"/"trends" (never inferred from dimensions alone),
    and a calendar-grain dimension is actually present (a bare "trend" word
    with no time dimension at all is a different, unhandled shape, not
    guessed at here)."""
    if query_intent.get("aggregation") is not None:
        return query_intent
    if ai_interpretation.measures:
        return query_intent
    if not _TIME_SERIES_TREND_RE.search(question or ""):
        return query_intent

    from data.query_planning_service import _CALENDAR_GRAIN_TERMS

    if not any(d.lower() in _CALENDAR_GRAIN_TERMS for d in ai_interpretation.dimensions):
        return query_intent

    return {**query_intent, "aggregation": "COUNT", "aggregation_target": "entity_count"}


# ---------------------------------------------------------------------------
# FINAL PRE-37 MILESTONE, Part 2/3/4 — Ranking Intent / Two-Entity Ranking
# Reclassification.
#
# Live-confirmed against a real production database: "Which location has the most classes?"
# gets entities=("location","class") from the AI interpreter (relationship_
# intent="single_entity", inconsistent with 2 named entities) -- the
# not_exactly_one_entity gate below refuses it outright, and even if it
# didn't, core.semantic.concept_resolver.extract_query_intent's own
# aggregation scan matches "most" as a bogus scalar MAX(class-identifier)
# reading, not "COUNT classes GROUP BY location, ranked DESC". This is the
# EXACT same "two AI entities, one measured one grouped" shape
# _reclassify_relationship_aggregate_as_single_entity already handles for
# explicit "per"/"each"/"number of" phrasing -- reusing that SAME single-
# entity-plus-dimension-plus-COUNT pipeline (fan-out safety, join planning,
# SQL generation all unchanged) via a second, narrower detector for the
# grammatically different "Which A has the most/fewest B?" superlative
# phrasing, rather than a new ranking planner.
# ---------------------------------------------------------------------------


def _reclassify_wh_ranking_as_single_entity(question: str, ai_interpretation: Any) -> tuple[Any, dict | None]:
    """Detects the "Which <A> has/have/... the most/fewest/highest/lowest
    <B>?" superlative-ranking shape via core.semantic.concept_resolver.
    derive_analytics_intent (reused verbatim -- its own wh_ranking detector,
    already independently tested, now also covers "fewest"/"least"/ASC
    ranking as of this milestone) and, only when it names exactly the
    module's own two AI entities (one as the grouping role, one as the
    measured role -- matched via the same _tokens_near_match near-match
    discipline every other role-matching check in this module already
    uses), reclassifies into the single-entity-plus-dimension shape
    _reclassify_relationship_aggregate_as_single_entity already builds.

    Returns (ai_interpretation, order_override). ai_interpretation is
    returned UNCHANGED, and order_override is None, whenever: there are not
    exactly 2 AI-named entities, the question doesn't match the wh_ranking
    shape, or the WH-captured entity/measure text can't be matched 1:1 to
    the two AI entities (never a guess at which is which -- falls through
    to the existing not_exactly_one_entity refusal exactly as before).
    order_override, when not None, is the {"direction","limit"} dict the
    caller must merge into query_intent["order"] (query_intent's own
    aggregation must ALSO be forced to COUNT/entity_count by the caller --
    derive_analytics_intent's ranking default is always "COUNT of the
    measured entity's own rows", never a scalar MAX/MIN of its identifier,
    which is exactly the bug this reclassification exists to correct)."""
    if len(ai_interpretation.entities) != 2:
        return ai_interpretation, None

    from core.semantic.concept_resolver import derive_analytics_intent

    analytics = derive_analytics_intent(question)
    if analytics["question_type"] != "ranking" or analytics["shape_source"] != "wh_ranking":
        return ai_interpretation, None

    grouping_term = (analytics["entity"] or "").lower()
    measured_term = (analytics["measure"] or "").lower()
    if not grouping_term or not measured_term:
        return ai_interpretation, None

    from data.query_planning_service import _tokens_near_match

    entities_lower = [e.lower() for e in ai_interpretation.entities]
    grouping_idx = next(
        (i for i, e in enumerate(entities_lower) if _tokens_near_match(e, grouping_term)), None,
    )
    measured_idx = next(
        (i for i, e in enumerate(entities_lower) if _tokens_near_match(e, measured_term)), None,
    )
    if grouping_idx is None or measured_idx is None or grouping_idx == measured_idx:
        # No clean, unambiguous 1:1 role match to the two AI-named entities
        # -- never guess which one is being grouped vs. measured.
        return ai_interpretation, None

    grouping_entity = ai_interpretation.entities[grouping_idx]
    measured_entity = ai_interpretation.entities[measured_idx]
    new_dimensions = ai_interpretation.dimensions
    if not any(d.lower() == grouping_entity.lower() for d in new_dimensions):
        new_dimensions = new_dimensions + (grouping_entity,)

    # Aggregation Semantics / Composed Ranking milestone, Part 1/2 -- the
    # EXACT SAME "ranking word silently overwrites an explicit aggregate"
    # bug this reclassification exists to fix (see this function's own
    # docstring above) ALSO afflicted this path's own caller, which used to
    # hardcode "aggregation": "COUNT" unconditionally whenever this
    # reclassification fired at all. Live-confirmed against Fixture A/B/C:
    # "Which account has the highest total order amount?" -> AI names
    # entities=("account","order"), measures=("amount",) -- a genuine SUM
    # request ("total") -- but the old caller always forced COUNT,
    # discarding "total" and answering with an order COUNT instead of a
    # dollar SUM. analytics["aggregation"]/["aggregation_target"] (computed
    # above via derive_analytics_intent, already qualifier-aware through its
    # own measure_phrase-based SUM/AVG/COUNT priority -- see that function's
    # "ranking" branch) is always a real SUM/AVG/COUNT for this shape, never
    # None, so it always safely replaces the old hardcoded default -- the
    # caller now trusts THIS value instead of hardcoding COUNT itself.
    # relationship_intent is set to match: scoped_aggregate for a genuine
    # SUM/AVG measure (routes to the SUM/AVG/MIN/MAX branch below, which
    # already knows how to resolve `measures` + `dimensions` -- both
    # already populated on `ai_interpretation`/`new_dimensions` here),
    # scoped_count unchanged for the COUNT-of-rows case this reclassification
    # originally targeted (e.g. "Which location has the most classes?").
    final_aggregation = analytics["aggregation"]
    reclassified = replace(
        ai_interpretation,
        entities=(measured_entity,),
        dimensions=new_dimensions,
        relationship_intent="scoped_aggregate" if final_aggregation in ("SUM", "AVG") else "scoped_count",
    )
    order_override = {
        "direction": analytics["ordering"] or "DESC", "limit": analytics["top_n"],
        "aggregation": final_aggregation, "aggregation_target": analytics["aggregation_target"],
    }
    return reclassified, order_override


def _gather_trusted_related_table_contexts(
    source_id: int, user_id: str, primary_fqn: str, primary_ctx: dict, *, session=None,
) -> dict[str, dict]:
    """RETIRE-LEGACY-FALLBACK MILESTONE, Part 5 -- bounded evidence gathering
    for the implicit-activity-ranking capability below (`_try_build_
    implicit_ranking_plan`), which needs full business-context dicts for
    every table AUTO/APPROVED-trusted-related to `primary_fqn`, not just
    `primary_fqn`'s own. Deliberately deterministic and narrow: only the
    already-trusted relationship graph (data.relationship_service, same
    AUTO/APPROVED filter every other join in this module honors) is walked,
    never a fresh schema-wide search -- no new candidate-selection decision
    is made here, only evidence assembly for a table set the trust layer
    already vouches for."""
    from data.business_knowledge_service import get_table_business_context
    from data.relationship_service import get_relationships_for_table

    table_contexts = {primary_fqn: primary_ctx}
    rels = get_relationships_for_table(source_id, user_id, primary_fqn)
    if not rels:
        return table_contexts
    for r in rels.get("inbound", []) or []:
        if r.get("relationship_status") not in ("AUTO", "APPROVED"):
            continue
        related_fqn = r.get("from_table_fqn")
        if not related_fqn or related_fqn in table_contexts:
            continue
        related_ctx = get_table_business_context(source_id, user_id, related_fqn, session=session)
        if related_ctx is not None:
            table_contexts[related_fqn] = related_ctx
    return table_contexts


def _same_entity_term(term: str, entity_name: str | None) -> bool:
    """Cheap, generic singular/plural-insensitive comparison used ONLY to
    decide whether a leftover term IS the already-grounded entity itself
    (e.g. "students" vs entity_name "student") rather than a genuinely
    separate business concept ("status") the entity resolution never
    consumed. Deliberately no stemming/synonym logic beyond a trailing "s"
    — anything more would risk silently treating a real second concept as
    the entity and skipping the derived-concept attempt below."""
    if not entity_name:
        return False
    a, b = term.lower().strip(), entity_name.lower().strip()
    return a == b or a.rstrip("s") == b.rstrip("s")


def _attempt_derived_concept_dimension_plan(
    source_id: int, user_id: str, question: str, entity_name: str,
    concept_term: str, ai_interpretation: Any, *, allow_unconfirmed_pii: bool = False, session=None,
) -> dict | None:
    """AI-FIRST CAPABILITY HARDENING milestone, Part 3/4 — resolves
    `concept_term` (a leftover term the bare "<entity> by <concept>" leftover
    -term safety net would otherwise unconditionally disqualify) via
    core.semantic.derived_concept_adjudicator's bounded, evidence-locked AI
    pass, and — only on a SUPPORTED verdict — builds a complete, self-
    contained COUNT(*) GROUP BY <derived category> query_plan/sql_plan/SQL,
    exactly the same "hand-build the plan, then validate/generate through
    the SAME deterministic data.sql_planning_service/data.sql_generation_
    service every other plan in this module uses" pattern as the implicit-
    activity-measure branch below. Returns None (never partial output) on
    AMBIGUOUS/UNSUPPORTED, a failed AI call, or a validation/generation
    failure — the caller falls through to its existing safe disqualify.

    Called BEFORE this module's own main entity-table adjudication (this
    safety net fires early, to fail fast on question shapes it already
    knows it cannot handle) — so this function does its OWN table
    resolution via the SAME gather_candidate_evidence/adjudicate_candidates
    pair the rest of this module uses, rather than assuming an fqn/ctx the
    caller has not resolved yet. This means up to one extra adjudication
    call versus a plain disqualify, spent ONLY in this narrow window where
    it can turn a safe refusal into a real, evidence-backed answer."""
    from data.semantic_contract_service import gather_candidate_evidence
    from core.semantic.candidate_adjudicator import ROLE_ENTITY, adjudicate_candidates
    from core.semantic.derived_concept_adjudicator import infer_derived_concept

    evidence_bundle = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return None
    adjudication = adjudicate_candidates(
        entity_name, evidence, list(ai_interpretation.requested_attributes) or None,
        session=session, simple_selection_only=True, role_context=ROLE_ENTITY,
    )
    if adjudication is None or adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return None
    fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(fqn)
    if cand_info is None:
        return None
    candidate = cand_info["candidate"]
    ctx = cand_info["ctx"]

    # ZERO WRONG ANSWER guard, live-confirmed necessary against a real production database:
    # this function has no second named term to corroborate a weak pick
    # against (unlike _corroborate_weak_entity_candidate's relationship-hop
    # retry elsewhere in this module, which needs an anchor_term) — a bare
    # "<entity> by <concept>" question genuinely has only one entity term.
    # Reproducibly confirmed the AI's own "catalog entity" semantic
    # reasoning can override the strongest deterministic lexical match with
    # a worse one for an ambiguous term (entity_name="student" on a real production database
    # repeatedly selected dbo.EDU_Assessment_Student — name_score 0.25 —
    # over dbo.EDU_Student — name_score 0.5, the real answer, excluded by
    # the model as "not a catalog entity"). With no corroborating anchor
    # available, only trust a selection that does NOT override the
    # strongest deterministic lexical match in its own evidence pool, and
    # only when that top score itself clears the same trust bar
    # (_MIN_TRUSTED_NAME_SCORE) used everywhere else in this module. Every
    # other case fails closed (returns None -> caller's existing safe
    # disqualify) rather than trust an unverifiable semantic override.
    max_name_score = max((e.get("name_score") or 0.0) for e in evidence)
    if max_name_score < _MIN_TRUSTED_NAME_SCORE or (candidate.get("name_score") or 0.0) < max_name_score:
        return None

    # Try the cheap, deterministic path first: `concept_term` may simply
    # BE a real column on this entity (e.g. Fixture A's Accounts.Status —
    # no derivation needed at all), in which case a literal GROUP BY on
    # that column is strictly more precise than any derived-category
    # guess and needs no AI adjudication call. Only when NO literal column
    # matches does this fall through to the derived-concept adjudicator —
    # exactly the same "deterministic evidence first, AI only for the
    # genuine gap" discipline this module uses everywhere else.
    literal = _resolve_requested_attribute(source_id, user_id, fqn, ctx, concept_term, session=session)
    if literal is not None:
        literal.pop("ctx", None)
        dimension_entry = {"term": concept_term, "selected": literal, "candidates": [], "warnings": []}
        plan_confidence = literal.get("score") if isinstance(literal.get("score"), (int, float)) else 0.9
        extra_warning = None
        plan_note = f"'{concept_term}' resolved to the real column {fqn}.{literal['column_name']}."
    else:
        result = infer_derived_concept(fqn, ctx, concept_term, session=session)
        if result is None or result.verdict != "SUPPORTED" or not result.categories:
            return None

        known_columns = {c["column_name"] for c in (ctx.get("columns") or [])}
        referenced: list[str] = []
        categories: list[dict] = []
        for cat in result.categories:
            conditions = []
            for cond in cat.conditions:
                if cond.column_name not in known_columns:
                    return None  # defensive re-check; the adjudicator already identifier-locks this
                conditions.append({"column_name": cond.column_name, "is_null": cond.is_null})
                if cond.column_name not in referenced:
                    referenced.append(cond.column_name)
            categories.append({"label": cat.label, "conditions": conditions})

        derived_selected = {
            "table_fqn": fqn, "column_name": referenced[0],
            "derived_case": {"categories": categories, "else_label": result.else_label, "referenced_columns": referenced},
            "score": result.confidence,
        }
        dimension_entry = {"term": concept_term, "selected": derived_selected, "candidates": [], "warnings": []}
        plan_confidence = result.confidence
        extra_warning = {
            "type": "derived_concept_used", "severity": "LOW",
            "message": (
                f"'{concept_term}' has no literal column on {fqn}; derived from "
                f"{', '.join(referenced)}" + (f" ({result.evidence})" if result.evidence else "") + "."
            ),
        }
        plan_note = f"'{concept_term}' was derived from real column evidence on {fqn} ({', '.join(referenced)})."
    measure_entry = {
        "term": entity_name,
        "selected": {
            "table_fqn": fqn, "column_name": None, "aggregation_target": "entity_count",
            "distinct": False, "score": 1.0,
        },
        "candidates": [], "warnings": [],
    }

    derived_query_plan = {
        "source_id": source_id,
        "intent": {
            "raw_question": question, "type": "aggregate_by_dimension",
            "aggregation": "COUNT", "aggregation_target": "entity_count", "distinct": False,
            "order": None, "status_value": None, "status_negated": False,
        },
        "tables": [fqn],
        "columns": {fqn: [c["column_name"] for c in (ctx.get("columns") or [])]},
        "measures": [measure_entry],
        "dimensions": [dimension_entry],
        "concepts": [], "filters": [],
        "join_plan": {"tables": [fqn], "steps": [], "required": False, "confidence": int(plan_confidence * 100)},
        "warnings": [extra_warning] if extra_warning else [],
        "confidence": int(plan_confidence * 100),
        "explanation": plan_note,
        "candidate_ranking": [], "remembered_terminology": [], "generated_vocabulary_evidence": [],
        "entity": None, "entity_table": None, "entity_confidence": None, "entity_candidates": [],
        "metadata_search_failed": False, "metadata_search_failed_reason": None, "grounding": None,
    }

    from data.sql_planning_service import build_sql_plan as _build_sql_plan
    from data.sql_generation_service import detect_dialect as _detect_dialect, generate_sql as _generate_sql

    derived_sql_plan = _build_sql_plan(source_id, user_id, derived_query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii)
    if not derived_sql_plan["validation"]["valid"]:
        return None
    derived_generated = _generate_sql(source_id, user_id, derived_sql_plan, dialect=_detect_dialect(source_id))
    if not derived_generated.get("sql"):
        return None
    return {
        "attempted": True, "valid": True, "reason": None,
        "adjudication_call_count": 1,
        "selected_table_fqns": [fqn],
        "physical_plan": None, "query_plan": derived_query_plan,
        "sql_plan": derived_sql_plan, "generated": derived_generated,
    }


def _attempt_leftover_attribute_list_plan(
    source_id: int, user_id: str, question: str, entity_name: str,
    attribute_term: str, ai_interpretation: Any, *, allow_unconfirmed_pii: bool = False, session=None,
) -> dict | None:
    """Investigation Agent Hardening milestone (Category-A Removal), Part 9
    — sibling of _attempt_derived_concept_dimension_plan above, for the
    BARE ATTRIBUTE-LIST shape ("Show equipment names and their type") rather
    than the GROUP-BY-by-category shape ("Show equipment by status") that
    function already covers. Root cause this closes: core.semantic.
    ai_interpreter sometimes places a term that is really a requested
    OUTPUT ATTRIBUTE into `entities` instead of `requested_attributes` (a
    role-classification uncertainty, not a database-evidence gap) — live-
    confirmed for "type" in "Show equipment names and their type" against
    the industrial-ops synthetic fixture. When that happens, attempt_ai_
    physical_plan's own already-working requested_attributes branch
    (_resolve_requested_attributes, several hundred lines below) never
    even runs, because the term never reached that list. This function
    re-routes the leftover term into the EXACT SAME resolution primitive
    (_resolve_requested_attribute — singular) that branch already uses,
    from this earlier "leftover term" recovery point instead.

    Deliberately narrower than the derived-concept sibling: only the
    literal/vocabulary-backed column resolution is tried here — never
    infer_derived_concept. A bare attribute-list question asks to SEE the
    real per-row value, not a derived category; guessing a derivation for
    an output column is a materially different (and unproven-safe) claim
    from guessing one for a GROUP BY key.

    Returns None (never partial output) whenever the term does not resolve
    to exactly one real/investigation-learned column, or plan validation
    fails — the caller falls through to its existing safe disqualify
    (which the Investigation Agent may now pick up — see classify_
    investigation_eligibility)."""
    from data.semantic_contract_service import gather_candidate_evidence
    from core.semantic.candidate_adjudicator import ROLE_ENTITY, adjudicate_candidates

    evidence_bundle = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return None
    adjudication = adjudicate_candidates(
        entity_name, evidence, list(ai_interpretation.requested_attributes) or None,
        session=session, simple_selection_only=True, role_context=ROLE_ENTITY,
    )
    if adjudication is None or adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return None
    fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(fqn)
    if cand_info is None:
        return None
    candidate = cand_info["candidate"]
    ctx = cand_info["ctx"]

    # Same ZERO WRONG ANSWER guard as the derived-concept sibling — see its
    # own docstring for the live-confirmed reasoning behind it.
    max_name_score = max((e.get("name_score") or 0.0) for e in evidence)
    if max_name_score < _MIN_TRUSTED_NAME_SCORE or (candidate.get("name_score") or 0.0) < max_name_score:
        return None

    resolved = _resolve_requested_attribute(source_id, user_id, fqn, ctx, attribute_term, session=session)
    if resolved is None:
        return None
    resolved.pop("ctx", None)
    dimension_entry = {"term": attribute_term, "selected": resolved, "candidates": [], "warnings": []}
    plan_confidence = resolved.get("score") if isinstance(resolved.get("score"), (int, float)) else 0.9

    list_query_plan = {
        "source_id": source_id,
        "intent": {
            "raw_question": question, "type": "list_entities",
            "aggregation": None, "aggregation_target": None, "distinct": False,
            "order": None, "status_value": None, "status_negated": False,
        },
        "tables": [fqn],
        "columns": {fqn: [c["column_name"] for c in (ctx.get("columns") or [])]},
        "measures": [],
        "dimensions": [dimension_entry],
        "concepts": [], "filters": [],
        "join_plan": {"tables": [fqn], "steps": [], "required": False, "confidence": int(plan_confidence * 100)},
        "warnings": [],
        "confidence": int(plan_confidence * 100),
        "explanation": f"'{attribute_term}' resolved to {resolved['table_fqn']}.{resolved['column_name']}.",
        "candidate_ranking": [], "remembered_terminology": [], "generated_vocabulary_evidence": [],
        "entity": None, "entity_table": None, "entity_confidence": None, "entity_candidates": [],
        "metadata_search_failed": False, "metadata_search_failed_reason": None, "grounding": None,
    }

    from data.sql_planning_service import build_sql_plan as _build_sql_plan
    from data.sql_generation_service import detect_dialect as _detect_dialect, generate_sql as _generate_sql

    list_sql_plan = _build_sql_plan(source_id, user_id, list_query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii)
    if not list_sql_plan["validation"]["valid"]:
        return None
    list_generated = _generate_sql(source_id, user_id, list_sql_plan, dialect=_detect_dialect(source_id))
    if not list_generated.get("sql"):
        return None
    return {
        "attempted": True, "valid": True, "reason": None,
        "adjudication_call_count": 1,
        "selected_table_fqns": [fqn],
        "physical_plan": None, "query_plan": list_query_plan,
        "sql_plan": list_sql_plan, "generated": list_generated,
    }


def attempt_ai_physical_plan(
    source_id: int,
    user_id: str,
    question: str,
    ai_interpretation: Any,
    *,
    allow_unconfirmed_pii: bool = False,
    session=None,
) -> dict:
    if ai_interpretation is None:
        return _disqualified("no_ai_interpretation")
    if ai_interpretation.clarification_required:
        return _disqualified("clarification_required")
    # RETIRE-LEGACY-FALLBACK MILESTONE — must run BEFORE the list_with_related
    # dispatch just below: a single-entity attribute request mislabeled
    # list_with_related (see this function's own docstring) must never reach
    # Stage 3's 2-4-entity relationship-list gate at all. A no-op for every
    # genuine multi-entity relationship list.
    ai_interpretation = _reclassify_single_entity_attribute_request_as_single_entity(ai_interpretation)
    # Part 1/2 — must run BEFORE the list_with_related dispatch and the
    # not_exactly_one_entity gate below: a two-entity "count/aggregate of A
    # grouped by B" question can arrive with either relationship_intent
    # depending on how the model classified it (live-confirmed both
    # list_with_related and scoped_count occur for this identical shape) --
    # reclassifying first means both dispatch paths see the SAME corrected,
    # single-entity-plus-dimension interpretation. A no-op for every other
    # question shape (single entity, genuine bare relationship list, etc.).
    ai_interpretation = _reclassify_relationship_aggregate_as_single_entity(question, ai_interpretation)
    # Part 2/3/4 — tried second, only ever fires on whatever the first
    # reclassification left untouched (a genuine "X per Y" question is
    # already resolved by the time this runs; wh_ranking_order_override
    # stays None for it since derive_analytics_intent's own question_type
    # would be "distribution", not "ranking", once grouping language like
    # "per"/"each" is present).
    ai_interpretation, wh_ranking_order_override = _reclassify_wh_ranking_as_single_entity(
        question, ai_interpretation,
    )
    if ai_interpretation.relationship_intent == "list_with_related":
        return _attempt_relationship_list_plan(
            source_id, user_id, question, ai_interpretation,
            allow_unconfirmed_pii=allow_unconfirmed_pii, session=session,
        )
    if ai_interpretation.relationship_intent not in ("single_entity", "scoped_count", "scoped_aggregate"):
        # core.semantic.ai_interpreter's own _enforce_count_language_rule
        # already guarantees "scoped_count" only stands for a question that
        # genuinely uses explicit counting language ("how many"/"count"/...)
        # -- exactly the single-table entity-count shape this stage covers,
        # not a second, wider category. scoped_aggregate is let through here
        # provisionally -- it normally implies a real SUM/AVG measure column
        # (out of scope, re-disqualified below once query_intent/comparison
        # detection has run) but core.semantic.ai_interpreter's own
        # relationship_intent alias table also maps "comparison" to
        # scoped_aggregate (line ~84), which IS Stage 4's target shape.
        return _disqualified("not_single_entity")
    if len(ai_interpretation.entities) != 1:
        return _disqualified("not_exactly_one_entity")

    from core.semantic.concept_resolver import extract_query_intent, extract_terms

    query_intent = extract_query_intent(question)
    query_intent = _apply_implicit_time_series_count(question, ai_interpretation, query_intent)
    if wh_ranking_order_override is not None:
        # Part 2/3/4 — force the ranking reading: derive_analytics_intent
        # already proved this is a real ranking shape (COUNT of the measured
        # entity's own rows BY DEFAULT, or SUM/AVG when the question also
        # carried an explicit "total"/"average" qualifier -- Aggregation
        # Semantics / Composed Ranking milestone, Part 1/2, see
        # _reclassify_wh_ranking_as_single_entity's own updated docstring),
        # never extract_query_intent's own bogus scalar MAX/MIN
        # (entity_identifier) reading of the same superlative word (e.g.
        # "most"/"fewest"/"highest" with no qualifier).
        query_intent = {
            **query_intent,
            "aggregation": wh_ranking_order_override["aggregation"],
            "aggregation_target": wh_ranking_order_override["aggregation_target"],
            "order": {
                "direction": wh_ranking_order_override["direction"],
                "limit": wh_ranking_order_override["limit"],
            },
        }
    aggregation = query_intent.get("aggregation")

    # Safe AI-Only Control milestone, Part 2 — requested output attributes
    # ("Show student email", "List customer phone numbers") are resolved
    # below, in the bare-list branch only (aggregation is None) --
    # _resolve_requested_attributes. Combined with a COUNT ("how many
    # students, show their email") is a different, still-unsupported shape
    # -- a bare count already answers the whole question, and "count grouped
    # by email" is not what that phrasing means -- disqualified exactly as
    # before.
    #
    # AI Schema Retrieval + Compound Query Coverage milestone, Part 7 —
    # requested attribute + a genuine SUM/AVG/MIN/MAX measure ("Show program
    # name and average course fee") is a different, legitimate shape: in
    # ordinary business-question usage "X and average Y" means "average Y
    # PER X", i.e. the requested attribute names the GROUPING DIMENSION for
    # the aggregate -- the exact same role dimension_term already plays
    # below (AI Physical Planner Coverage Expansion Part C / REG-G-03).
    # Reuses that unchanged mechanism (measure resolution + dimension
    # resolution + join planning + SQL generation) rather than a second,
    # parallel code path. Only auto-folded when unambiguous: exactly ONE
    # requested attribute, and no dimension ALSO separately named (two
    # independent candidate group-by terms is a genuine ambiguity, not
    # guessed -- disqualifies below exactly as before instead).
    requested_attribute_as_dimension: str | None = None
    if aggregation is not None and ai_interpretation.requested_attributes:
        if (
            aggregation in _NON_COUNT_AGGREGATIONS
            and len(ai_interpretation.requested_attributes) == 1
            and not ai_interpretation.dimensions
        ):
            requested_attribute_as_dimension = ai_interpretation.requested_attributes[0]
        else:
            return _disqualified("requested_attributes_with_aggregation_out_of_scope")

    # Stage 4 — period comparison ("Compare enrollments this year vs last
    # year"). No new SQL shape exists anywhere on the live path for a
    # genuine two-value comparison ANSWER (confirmed: data.insight_service.
    # compute_period_comparison_insight only ever clones and re-executes an
    # ALREADY-answered single-period scalar-aggregate plan as a post-hoc
    # annotation, and core.orchestrator.agent.py's own comment states
    # outright "this codebase has no multi-period comparison SQL shape to
    # reuse") -- building one would be genuinely new infrastructure, not
    # "straightforward to add" as this stage requires. What IS straightforward:
    # "Compare X this year vs last year" has no "how many"/"count" wording,
    # so extract_query_intent's aggregation comes back None and this module
    # would otherwise silently take the bare list_entities branch below --
    # answering with a raw row list instead of a count, and (more
    # importantly) never producing the scalar result core.orchestrator.
    # context_builder._build_business_plan/data.insight_service's EXISTING,
    # unrelated, already-approved comparison-insight hook needs to fire.
    # That hook re-derives aggregation/date_context purely from THIS
    # module's own query_plan/sql_plan output (context_builder.py's
    # _build_business_plan, confirmed by direct read: "aggregation":
    # intent.get("aggregation"), date_context matched against sql_plan's
    # own where rows) -- so treating a detected comparison request as an
    # implicit entity-count (reusing Stage 1's entity_count measure
    # unchanged) is the ENTIRE fix needed; the actual comparison value is
    # computed entirely by existing code this module never touches.
    comparison_intent = None
    if aggregation is None and query_intent.get("date_range"):
        from core.semantic.concept_resolver import detect_comparison_intent

        comparison_intent = detect_comparison_intent(question)
        if comparison_intent and comparison_intent.get("requested"):
            aggregation = "COUNT"
            query_intent = {**query_intent, "aggregation": aggregation}

    if (
        ai_interpretation.relationship_intent == "scoped_aggregate"
        and comparison_intent is None
        and aggregation not in _NON_COUNT_AGGREGATIONS
    ):
        # scoped_aggregate implies either Stage 4's specific period-
        # comparison shape (comparison_intent, handled above) or a genuine
        # SUM/AVG/MIN/MAX measure column (AI Physical Planner Coverage
        # Expansion, Part C -- handled below via aggregation/measures) --
        # anything else reaching scoped_aggregate with neither signal is a
        # shape this stage doesn't know how to answer.
        return _disqualified("not_single_entity")

    if aggregation is not None and aggregation not in ({"COUNT"} | _NON_COUNT_AGGREGATIONS):
        return _disqualified("non_count_aggregation_out_of_stage1_scope")

    if aggregation in _NON_COUNT_AGGREGATIONS and not ai_interpretation.measures:
        # A SUM/AVG/MIN/MAX request needs the AI to have named WHICH
        # business quantity to aggregate -- never guessed from the entity
        # name alone (unlike COUNT, which counts rows, not a value).
        return _disqualified("measure_term_required_for_aggregation_out_of_stage1_scope")

    # Stage 2c — ORDER BY / Top-N ranking. Same reuse principle as filters/
    # grouping above: ai_interpretation.sorting is ignored in favor of the
    # deterministic query_intent["order"] extract_query_intent already
    # detects (Top N/Bottom N/latest/earliest regexes). The target=="date"
    # shape (e.g. "10 most recently added students") is handled below,
    # unchanged, as before.
    #
    # Safe AI-Only Control milestone, Part 3 — measure-value ranking ("top
    # 10 clients by revenue", "bottom 5 X by Y", "highest X", "lowest X")
    # for a bare (non-aggregated) ranking question where the AI named
    # EXACTLY ONE measure to rank by (ai_interpretation.measures) --
    # resolved in the bare-list branch below via the SAME bounded, real-
    # evidence measure resolver SUM/AVG/MIN/MAX already uses
    # (_resolve_ai_measure_column/_resolve_ai_measure_cross_table), never
    # guessed from the entity's identity key.
    #
    # Compound Query Coverage, Part 9 — ranking BY the aggregate result
    # itself ("Top 10 programs by average fee", "Bottom 5 regions by
    # student count") reuses the aggregate branch's OWN measure/GROUP BY
    # construction below completely unchanged -- data.sql_planning_service.
    # _build_order_by already resolves an ORDER BY that matches one of the
    # query's own aggregated SELECT rows to that row's alias when a
    # GROUP BY is present (confirmed: "Milestone Phase 2 — Analytics Intent
    # Layer" in that function), so no new SQL-generation logic is needed --
    # only in scope when there's a dimension to rank ACROSS (a bare scalar
    # aggregate has nothing to rank). Ranking by a DIFFERENT, separately-
    # named raw measure while ALSO aggregating a different one is a wider,
    # still-untested compound shape -- not attempted, disqualifies exactly
    # as before.
    order_intent = query_intent.get("order")

    # Aggregation Semantics / Composed Ranking milestone, Part 1/2 — a
    # composed "which <entity> has the highest/lowest TOTAL/AVERAGE <measure>"
    # question carries a real ranking direction (highest/largest/most/
    # lowest/smallest/fewest/...) that extract_query_intent's own `order`
    # field never captures for these words (by design -- see
    # detect_ranking_direction's own docstring: those same words ALSO drive
    # aggregation-FUNCTION selection above, and widening `order` itself to
    # cover them would wrongly force a ranking attempt onto a genuine bare
    # scalar superlative with no grouping entity at all, e.g. "largest
    # single order" -- Part 7 of this milestone). Live-confirmed root cause
    # (Fixture E, "Which depot has the highest total maintenance cost?"):
    # aggregation correctly resolves to SUM now (extract_query_intent fix,
    # same milestone), but order_intent stayed None, so the aggregate query
    # returned every depot's SUM with no ORDER BY/LIMIT at all instead of
    # ranking to the single highest one.
    #
    # NOT synthesized here yet: at this point in the function
    # ai_interpretation.dimensions is often EMPTY even for a genuinely
    # grouped question ("Which depot has the highest total maintenance
    # cost?" -- live-confirmed the AI interpreter names entities=("depot",),
    # measures=("maintenance cost",), dimensions=() for this exact shape;
    # "depot" IS the grouping term but the AI expresses it as the entity
    # being asked about, not a separate declared dimension). The REAL
    # grouping-dimension recovery (raw_dimension_terms fallback, below at
    # `effective_dimensions`/`dimension_terms`) hasn't run yet -- so whether
    # this question will actually end up grouped is not yet knowable here.
    # Synthesis is deferred to just before `plan_order` is built (below,
    # near `rank_by_aggregate`'s consumption), once `plan_group_by` is
    # definitely known either way -- see the comment there for the actual
    # gate.
    ranking_measure_term: str | None = None
    rank_by_aggregate = False
    if order_intent and order_intent.get("target") != "date":
        if aggregation is not None:
            if not (ai_interpretation.dimensions or requested_attribute_as_dimension):
                return _disqualified("non_date_ranking_out_of_stage2_scope")
            rank_by_aggregate = True
        elif len(ai_interpretation.measures) != 1:
            return _disqualified("non_date_ranking_out_of_stage2_scope")
        else:
            ranking_measure_term = ai_interpretation.measures[0]

    # Live-confirmed against a real production database: "List instructors" -> AI interpreter
    # names entities=("user",) -- the model's closed known-vocabulary hint
    # has no Instructor entry, so it substitutes the nearest generic
    # neighbor it knows, and "user" is a genuinely strong, real table match
    # (dnnuser.Users) that the name-relevance check above cannot catch (it
    # only catches a WEAK match, not a wrong-but-strong one). This exact
    # failure mode is already documented and guarded against for the legacy
    # merge path by data.semantic_contract_service.
    # validate_ai_entity_hypotheses (see its own docstring, which names this
    # precise "instructor" -> "user" case) -- reused here unchanged rather
    # than re-implemented. single_entity_question=True unconditionally:
    # unlike that function's other caller (core.orchestrator.context_builder,
    # which also passes scoped_count/scoped_aggregate through as "multi-
    # entity" for ITS different purpose of not rejecting a genuinely
    # additional relationship-question entity), every question that reaches
    # this point is already known to be single_entity/scoped_count -- always
    # exactly one entity, per the disqualification checks above -- so the
    # "does another term in the question contradict this one AI entity"
    # check is always the correct one to apply here.
    from data.semantic_contract_service import validate_ai_entity_hypotheses

    concepts, measures, raw_dimension_terms = extract_terms(question)
    validated_entities = validate_ai_entity_hypotheses(
        source_id, list(ai_interpretation.entities), concepts + measures + raw_dimension_terms,
        single_entity_question=True, session=session,
    )
    bare_measure_fallback_numeric = False
    if not validated_entities:
        # NEXT MILESTONE — REAL BUSINESS Q&A COMPLETION, Part 2 (bare-
        # measure ownership) / Part 4 (event/domain noun grounding, COUNT
        # case). The AI's own named entity was rejected -- either
        # unsupported (no real vocabulary evidence at all) or contradicted
        # by another deterministic term in the question -- but the AI may
        # still have named exactly ONE real business measure/event term
        # (ai_interpretation.measures) that legitimately has no separate
        # "owning entity" word in the question at all. Live-confirmed
        # against a real production database: "What is the total amount of fees paid?" ->
        # entities=("payment",) [rejected -- "fees" grounds to a different
        # table] measures=("amount",); "Show enrollment trend by month" ->
        # entities=("student",) [rejected] measures=("enrollment",) -- the
        # AI puts the question's real subject noun into `measures` when it
        # has no distinct separate entity to name. Reuses the EXACT SAME
        # evidence-gathering/adjudication/trust-gate pipeline below, just
        # substituting this term in as entity_name -- never a new
        # resolution mechanism. Only attempted for EXACTLY one measure
        # term (never guessed among several); the weak-name-score gate
        # below still applies, with vocabulary corroboration deliberately
        # withheld whenever this substitution feeds a genuine numeric
        # (SUM/AVG/MIN/MAX) aggregation (see _vocabulary_trusts_candidate's
        # own docstring for the live-confirmed false-positive risk a bare
        # measure-column term carries that a bare event/entity noun does
        # not) -- so a genuinely ambiguous bare measure term (e.g. "fee",
        # "amount", both of which collide with unrelated tables' own
        # column vocabulary in a real production database) still safely disqualifies below
        # rather than guessing, while a bare event/entity noun used for
        # implicit-COUNT purposes (below) gets the full trust chain.
        if len(ai_interpretation.measures) != 1:
            return _disqualified("ai_entity_hypothesis_rejected", attempted=True)
        entity_name = ai_interpretation.measures[0]
        bare_measure_fallback_numeric = aggregation in _NON_COUNT_AGGREGATIONS

        # Part 5 — a bare event/entity "trend by month" question with no
        # genuine separate measure (the substituted term above IS the
        # subject noun, not a real quantity to aggregate) still needs an
        # implicit COUNT, exactly like _apply_implicit_time_series_count
        # already provides when ai_interpretation.measures was empty from
        # the start -- that function's own "not ai_interpretation.measures"
        # guard is what stops it from firing here, since the AI DID
        # populate measures (with what turned out to be the entity noun,
        # not a real measure). Re-run the SAME check with that one guard
        # lifted (via a throwaway copy with measures cleared) rather than a
        # second, divergent trend-detection implementation.
        if aggregation is None:
            probe_interpretation = replace(ai_interpretation, measures=())
            query_intent = _apply_implicit_time_series_count(question, probe_interpretation, query_intent)
            aggregation = query_intent.get("aggregation")
    else:
        entity_name = validated_entities[0]

    # Live-confirmed against a real production database Golden Set (source of a real
    # PASS -> wrong-shaped-answer regression): "List instructor names" can
    # get an AI interpretation with requested_attributes=() -- the model
    # doesn't reliably populate that field every call (the same "mostly but
    # not bit-exact deterministic" temperature=0 caveat noted elsewhere in
    # this codebase) -- so the requested_attributes disqualifier above does
    # not always catch it. When that happens and aggregation is None, this
    # module's bare "list_entities" branch below would auto-select the
    # table's own safe (non-PII) columns via data.sql_planning_service's
    # existing carve-out -- silently omitting the exact name/email columns
    # the question asked for, since they're PII-flagged and excluded from
    # AUTO-selection (never explicitly requested, from this module's point
    # of view). data.query_planning_service's own D3 fix already handles
    # exactly this "entity-relative attribute" shape (e.g. "student
    # emails") deterministically -- reached only via the legacy fallback,
    # never re-implemented here. Detected independently of
    # requested_attributes: extract_terms(question) splitting into MORE
    # than one distinct term (e.g. concepts=['instructor','names']) is a
    # reliable, deterministic signal a bare list_entities answer would be
    # incomplete, even when the AI's own requested_attributes came back
    # empty for this call. Only applies to the bare-list shape (aggregation
    # is None) -- an entity-count answer never selects business columns, so
    # an extra leftover term there carries no such risk.
    distinct_terms = {t.lower() for t in concepts + measures + raw_dimension_terms}
    # Safe AI-Only Control milestone, Parts 2/3 — NOT triggered when the AI
    # itself already declared requested_attributes, or this is a measure-
    # value ranking question (ranking_measure_term set above): neither case
    # is a silently-dropped attribute request anymore, both are handled
    # explicitly below (_resolve_requested_attributes / the ranking-measure
    # resolution in the bare-list branch) instead of guessed at via this
    # leftover-term heuristic. Still fires exactly as before when neither
    # applies -- the original scenario this safety net targets.
    #
    # NEXT MILESTONE, Part 2/3 — also NOT triggered when query_intent's own
    # null_check_polarity is set: that leftover term is not a silently-
    # dropped requested attribute, it's an existence-idiom SUBJECT ("email"
    # in "students who have an email on file") this function resolves later
    # (the deterministic existence-idiom backstop, below the filter
    # resolution once fqn/ctx are known) via a real IS NOT NULL/IS NULL
    # filter -- disqualifying THIS early, before that resolution ever gets
    # a chance to run, would turn a materially answerable question into a
    # false refusal. That later backstop applies its own "exactly one
    # leftover term, else refuse" discipline, so this early check simply
    # defers to it rather than duplicating (and potentially disagreeing
    # with) the same decision.
    #
    # FINAL PRE-37 MILESTONE, Part 6/7 — also NOT triggered when the AI
    # itself already extracted a structured filter (ai_interpretation.
    # filters). Live-confirmed against a real production database: "Show classes at the
    # Richardson location" gets filters=({"attribute":"location","operator":
    # "=","value":"Richardson"},) from the AI interpreter -- a real,
    # structured filter this module already resolves later (_resolve_ai_
    # filters, extended this milestone with a categorical-value-on-related-
    # table fallback -- see _resolve_ai_categorical_value_filter) -- but
    # this early heuristic ran BEFORE that resolution ever got a chance,
    # refusing a materially answerable question on the same "leftover term"
    # signal that is actually the filter's own attribute/value words. Same
    # defer-to-the-real-resolver discipline as the requested_attributes/
    # null_check_polarity exemptions above: if the later, more specific
    # filter resolution still can't resolve it, THAT step's own disqualify
    # (filters_out_of_stage1_scope) still applies -- this exemption never
    # weakens the "never guess" contract, only stops a false early refusal.
    if (
        aggregation is None and len(distinct_terms) > 1
        and not ai_interpretation.requested_attributes and ranking_measure_term is None
        and not query_intent.get("null_check_polarity")
        and not ai_interpretation.filters
    ):
        # AI-FIRST CAPABILITY HARDENING milestone, Part 3/4 — before
        # disqualifying outright, try resolving the single leftover term as
        # a DERIVED business concept GROUP BY dimension (e.g. "status" on an
        # entity with no literal Status column, but real evidence like a
        # nullable CancelID/EndDate pair — live-confirmed against a real production database,
        # "Show students by status": dbo.EDU_Student has no Status column at
        # all). Only attempted when EXACTLY ONE leftover term remains (never
        # guessed among several) — same discipline as every other exemption
        # in this block. core.semantic.derived_concept_adjudicator never
        # invents a business rule: it returns SUPPORTED only when real
        # column-nullability evidence justifies it, else AMBIGUOUS/
        # UNSUPPORTED, which falls through to the same safe disqualify as
        # before. This is a bare "<entity> by <concept>" COUNT/GROUP BY
        # shape only — a derived concept requested as a FILTER or a named
        # OUTPUT ATTRIBUTE is a different shape, not attempted here.
        # ZERO WRONG ANSWER guard, live-confirmed necessary by test_ai_
        # physical_planner.py::test_bare_list_with_leftover_attribute_term_
        # falls_back: "List instructor names" has the IDENTICAL structural
        # signature (aggregation None, one leftover term "names", no
        # requested_attributes) as "Show accounts by status", but it means
        # something completely different — "names" is a per-row OUTPUT
        # ATTRIBUTE the AI failed to declare, never a GROUP BY category to
        # count by. Building a COUNT(*) GROUP BY "names" plan there would be
        # actively wrong (or, if "names" doesn't resolve, an unnecessary
        # extra evidence-gathering/adjudication call for a shape this
        # capability was never meant to touch). A "by"/"per" word is the
        # same kind of deterministic lexical signal this module already
        # uses elsewhere (superlative/Top-N/date-range regexes) to
        # distinguish a genuine breakdown-by-category question from a bare
        # attribute-list one — every fixture/live-the reference database case this capability
        # targets ("... by status") naturally contains it; "List instructor
        # names" does not.
        leftover_terms = [t for t in distinct_terms if not _same_entity_term(t, entity_name)]
        if len(leftover_terms) == 1 and re.search(r"\b(by|per)\b", question, re.IGNORECASE):
            derived_plan_result = _attempt_derived_concept_dimension_plan(
                source_id, user_id, question, entity_name, leftover_terms[0], ai_interpretation,
                allow_unconfirmed_pii=allow_unconfirmed_pii, session=session,
            )
            if derived_plan_result is not None:
                return derived_plan_result
        elif len(leftover_terms) == 1:
            # Investigation Agent Hardening milestone (Category-A Removal),
            # Part 9 — the no-"by"/"per" sibling: the leftover term may be a
            # requested OUTPUT ATTRIBUTE the AI interpreter misclassified as
            # a second entity, rather than a GROUP-BY category. Tried BEFORE
            # disqualifying so a literal/investigation-learned column match
            # answers deterministically with zero investigation overhead —
            # never a guessed derivation for an output column (see that
            # function's own docstring for why it's deliberately narrower
            # than the derived-concept sibling above).
            attribute_plan_result = _attempt_leftover_attribute_list_plan(
                source_id, user_id, question, entity_name, leftover_terms[0], ai_interpretation,
                allow_unconfirmed_pii=allow_unconfirmed_pii, session=session,
            )
            if attribute_plan_result is not None:
                return attribute_plan_result
        return _disqualified("extra_term_implies_attribute_request_out_of_stage1_scope")

    # Stage 2b — GROUP BY. ai_interpretation.dimensions is the AI's own
    # business-level grouping request (e.g. dimensions=("year",) for "how
    # many students are there by year?"). A calendar-grain word (year/
    # month/quarter/week/day) pairs with a COUNT (grain_term, same-table,
    # still capped at exactly one -- mixing multiple calendar grains, or a
    # calendar grain with a non-calendar dimension, is a wider shape not yet
    # extended, Compound Query Coverage Part 8). A non-calendar word (e.g.
    # "program") pairs with a SUM/AVG/MIN/MAX measure -- Compound Query
    # Coverage, Part 8 (Multi-Dimension Grouping) extends this from exactly
    # one term to a bounded list (dimension_terms, up to
    # _MAX_MULTI_DIMENSIONS), each independently resolved -- possibly
    # cross-table -- via _resolve_non_calendar_dimension once the primary
    # table is known below. data.sql_planning_service's own
    # _build_group_by/_select_entries and data.sql_generation_service's own
    # _build_group_by_clause already iterate query_plan["dimensions"]
    # generically (confirmed: "Results will be grouped by {len(group_by)}
    # dimension(s)" -- never hardcoded to one) -- this was purely an AI-side
    # scope restriction, not a validator/SQL-generator limitation, so no
    # downstream change is needed. A non-calendar word (or words) paired
    # with COUNT (or no aggregation at all) safely disqualifies rather than
    # guessing, same as before.
    # FINAL G-03 CORRECTNESS FIX — the AI interpreter's JSON call is not
    # bit-exact at temperature=0 (already documented above, Safe AI-Only
    # Control milestone, Part 2's "List instructor names" note): live-
    # confirmed for THIS exact question, "Average course fee by program"
    # returns dimensions=("program",) on one call and dimensions=() (with
    # "program" folded into entities instead) on another. When dimensions
    # comes back empty, ai_interpretation.dimensions alone silently loses
    # "by program" -- extract_terms(question) (already computed above as
    # raw_dimension_terms) is a purely lexical, deterministic "words after
    # by/each/per" split that is NOT subject to this non-determinism, so it
    # is used here as a fallback source of truth, never a replacement: only
    # consulted when the AI's own dimensions list is empty, and only a term
    # not already claimed by a role the AI DID name (a measure or a
    # requested attribute) is trusted -- e.g. "top 5 clients by revenue"
    # must never treat "revenue" as a dimension merely because it is the
    # word after "by"; it is already the ranking measure (ranking_measure_
    # term, resolved separately below) and is filtered out here via the
    # ai_interpretation.measures check. Applies uniformly to every
    # aggregation-by-dimension shape (COUNT and SUM/AVG/MIN/MAX alike, by
    # location/department/customer/category/month/region/program/...) --
    # not a G-03/CourseFee/Program-specific patch.
    grain_term: str | None = None
    dimension_terms: list[str] = []
    effective_dimensions = ai_interpretation.dimensions
    if aggregation is not None and not effective_dimensions and raw_dimension_terms:
        already_named_lower = {t.lower() for t in ai_interpretation.measures} | {
            t.lower() for t in ai_interpretation.requested_attributes
        }
        recovered = [t for t in raw_dimension_terms if t.lower() not in already_named_lower]
        if recovered:
            effective_dimensions = tuple(recovered)
        else:
            # Every deterministically-detected "by X" term collided with a
            # role the AI already claimed (measure/requested attribute) --
            # a genuine ambiguity between "this word is the dimension" and
            # "this word is the measure/attribute", not a safe guess either
            # way. Refuse rather than silently answering the un-grouped
            # aggregate the question never asked for (Part 7's semantic-
            # completeness requirement: a plan that drops a requested
            # grouping must not execute).
            return _disqualified(
                "dimension_term_ambiguous_with_measure_out_of_stage2_scope", attempted=True,
            )
    if effective_dimensions:
        from data.query_planning_service import _CALENDAR_GRAIN_TERMS

        lowered = [d.lower() for d in effective_dimensions]
        has_calendar = any(d in _CALENDAR_GRAIN_TERMS for d in lowered)
        if has_calendar:
            # A calendar grain mixed with a non-calendar dimension, or more
            # than one calendar-grain term, is a wider shape (Compound
            # Query Coverage Part 8's "calendar + non-calendar combination
            # where safe") not yet extended -- only a single BARE
            # calendar-grain term, exactly as before, is in scope here.
            if len(lowered) != 1:
                return _disqualified("dimensions_out_of_stage2_scope")
            grain_term = lowered[0]
            if aggregation != "COUNT":
                # A calendar-grain GROUP BY only has a well-defined shape
                # here paired with the entity-count measure this stage
                # already knows how to build (e.g. "students PER YEAR") — a
                # grain word without counting language is a different,
                # unhandled shape, not a safe guess. Checked before any
                # evidence/adjudication call, since it depends only on
                # already-known deterministic signal.
                return _disqualified("grouping_without_count_aggregation_out_of_stage2_scope")
        elif aggregation in ({"COUNT"} | _NON_COUNT_AGGREGATIONS) and len(lowered) <= _MAX_MULTI_DIMENSIONS:
            # NEXT MILESTONE — INTERACTIVE AI Q&A COVERAGE COMPLETION +
            # SAFETY, Part 11 (Grouped Results) — "How many students are in
            # each program?"/"How many enrollments per instructor?" are a
            # COUNT paired with a non-calendar dimension, previously only
            # allowed for SUM/AVG/MIN/MAX (Compound Query Coverage Part 8).
            # data.sql_planning_service._build_group_by/_select_entries and
            # data.sql_generation_service._build_group_by_clause already
            # iterate query_plan["dimensions"]/["measures"] generically
            # regardless of which aggregation verb is present (confirmed by
            # the SAME "never hardcoded to one" comment already cited above
            # for the SUM/AVG case) -- this was purely an AI-side scope
            # restriction on COUNT specifically, not a validator/SQL-
            # generator limitation, so no downstream change is needed here
            # either. Resolved below via the exact same _resolve_non_
            # calendar_dimension mechanism the SUM/AVG branch already uses.
            dimension_terms = lowered
        else:
            return _disqualified("dimensions_out_of_stage2_scope")
    elif requested_attribute_as_dimension is not None:
        dimension_terms = [requested_attribute_as_dimension.lower()]

    from data.semantic_contract_service import gather_candidate_evidence, specialize_entity_term

    # Specialized Concept Principle (Final Demo Hardening, Part 2/3) — the AI
    # question interpreter's own entity extraction is not guaranteed to
    # preserve a specializing modifier (live-confirmed: "How many mentorship
    # programs are there?" -> AIQuestionInterpretation(entities=('program',))
    # for a real source, silently dropping "mentorship"). Widens
    # entity_name to include an adjacent, unconsumed content word from the
    # RAW question text before evidence-gathering ever runs, so the search
    # itself targets the specific concept instead of the generic one the AI
    # happened to normalize to. A no-op (entity_name unchanged) whenever the
    # AI's label already accounts for every relevant word in the question —
    # see specialize_entity_term's own docstring for the exact, narrow rule.
    entity_name = specialize_entity_term(entity_name, question, list(distinct_terms))

    evidence_bundle = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    evidence = evidence_bundle.get("evidence") or []
    if not evidence:
        return _disqualified("no_candidate_evidence", attempted=True)

    from core.semantic.candidate_adjudicator import ROLE_ENTITY, adjudicate_candidates

    adjudication = adjudicate_candidates(
        entity_name, evidence, list(ai_interpretation.requested_attributes) or None,
        session=session, simple_selection_only=True, role_context=ROLE_ENTITY,
    )
    if adjudication is None:
        return _disqualified("adjudication_unavailable_or_failed", attempted=True, call_count=1)
    if adjudication.no_safe_selection or not adjudication.selected_table_fqn:
        return _disqualified("no_safe_table_selection", attempted=True, call_count=1)

    fqn = adjudication.selected_table_fqn
    cand_info = evidence_bundle["candidates_by_fqn"].get(fqn)
    if cand_info is None:
        # adjudicate_candidates already hard-validates selected_table_fqn
        # against the literal evidence it was given, so this should be
        # unreachable — kept as a fail-closed backstop, never trust a
        # selection this module cannot itself re-locate in its own evidence.
        return _disqualified("selected_table_missing_from_evidence", attempted=True, call_count=1)

    candidate = cand_info["candidate"]
    ctx = cand_info["ctx"]
    all_candidates = [info["candidate"] for info in evidence_bundle["candidates_by_fqn"].values()]
    known_column_names = [c["column_name"] for c in (ctx.get("columns") or [])]

    corroboration_used = False
    anchor_attempts = 1  # default for the vocabulary-trust branch below (no anchor-term retry loop there)
    if (
        (candidate.get("name_score") or 0.0) < _MIN_TRUSTED_NAME_SCORE
        and not bare_measure_fallback_numeric
        and _vocabulary_trusts_candidate(entity_name, candidate)
        and not _vocabulary_trust_is_contested(fqn, evidence)
    ):
        # NEXT MILESTONE, Part 4 — the adjudicator already independently
        # selected `fqn` from real evidence; a real auto-derived vocabulary
        # row already connects `entity_name` to this SAME table (see
        # _vocabulary_trusts_candidate's own docstring) -- corroborating
        # evidence from a genuinely different source than lexical name
        # overlap, so the weak lexical name_score alone no longer needs to
        # block trust. Deliberately skipped when entity_name is a bare
        # numeric-measure term substituted in by the ownership-discovery
        # fallback below (bare_measure_fallback_numeric) -- that signal is
        # calibrated for entity/dimension/event-noun resolution only, see
        # the same docstring for the live-confirmed false-positive risk
        # ("fee" -> dnnuser.Roles, "amount" -> dbo.ACHTransaction) for a
        # bare measure-column term.
        corroboration_used = True
    elif (candidate.get("name_score") or 0.0) < _MIN_TRUSTED_NAME_SCORE:
        # Live-confirmed against a real production database: "How many programs are there?"
        # had gather_candidate_evidence's pool contain only Mentorship/
        # Internship-Program tables (name_score 0.333, the entity's true
        # canonical table dbo.EDU_Track shares no lexical token with
        # "program" at all and was absent from evidence entirely) -- the AI
        # adjudicator confidently (0.93) picked the best of a genuinely
        # incomplete/wrong pool. Every live-confirmed CORRECT Stage 1 pick
        # (student/instructor/class/client) had name_score >= 0.5 (a real
        # direct table-name token match). adjudicate_candidates' own
        # identifier validation cannot catch this class of error -- the
        # selected table IS real evidence, just not the RIGHT one.
        #
        # D4b-class recovery: role-aware column-evidence promotion (above)
        # now gets dbo.EDU_Track INTO this evidence bundle for "program", but
        # a lexical name-match gate alone still can't trust it over
        # dbo.EDU_Mentoring_Program's stronger (but wrong) name match --
        # confirmed the AI adjudicator reliably picks the latter on lexical
        # grounds alone. _corroborate_weak_entity_candidate tries ONE bounded
        # retry using real schema-relationship evidence to another business
        # term this same question already named, before giving up. Still a
        # deterministic evidence-quality backstop, not a re-decision of
        # which table is correct: this never invents or forces a selection,
        # it only gives the AI a genuine second, better-evidenced look, and
        # falls through to the exact same safe refusal/legacy-fallback
        # policy as before when no corroboration is available or the retry
        # still can't clear the trust bar.
        # MULTI-HOP GROUPING GAP FIX — for a COUNT question,
        # ai_interpretation.measures is NEVER genuinely consumed as a real
        # measure term (see the identical, already-documented invariant a
        # few hundred lines below, at the null_check_polarity leftover-terms
        # backstop: "for a COUNT question, ai_interpretation.measures is
        # NEVER actually read anywhere in this function"). Live-confirmed
        # this same unreliable value ALSO broke anchor-term selection here:
        # "How many students are in each program?" has dimension_terms=
        # ['program'] (the real, deterministically-extracted grouping term)
        # but ai_interpretation.measures=('count',) — a spurious COUNT-verb
        # placeholder, not a real business term — and the old ordering
        # picked that placeholder first, so _corroborate_weak_entity_
        # candidate searched for evidence of a business concept called
        # "count" (none exists) and always returned None. Preferring
        # dimension_terms whenever aggregation is not a genuine NON_COUNT
        # aggregate (the one case _NON_COUNT_AGGREGATIONS below actually
        # resolves ai_interpretation.measures[0] into a real measure column)
        # matches that exact existing invariant instead of contradicting it.
        primary_anchor_term = (
            ai_interpretation.measures[0]
            if (aggregation in _NON_COUNT_AGGREGATIONS and ai_interpretation.measures)
            else (dimension_terms[0] if dimension_terms else None)
        )
        # Aggregation Semantics / Composed Ranking milestone, Part 1/2 --
        # once wh_ranking reclassification (above) can produce a genuine
        # SUM/AVG shape instead of always forcing COUNT, this corroboration
        # retry is now reached for compositions it never used to see, e.g.
        # "Which provider has the highest total treatment amount?" ->
        # entity_name="treatment" (weak name match to main.Appointments),
        # anchor_term="amount" (the measure). Live-confirmed: "amount" is
        # too generic a word to gather any real evidence of its own
        # (gather_candidate_evidence returns empty), so the retry returned
        # None immediately even though the OTHER already-known role for
        # this SAME question -- the dimension term "provider" -- has strong,
        # real evidence and WOULD have corroborated main.Appointments (the
        # same table _corroborate_weak_entity_candidate would already
        # accept for the COUNT-shaped version of this exact question, which
        # anchors on "provider" instead). Trying the alternate real anchor
        # (the term the primary choice above did NOT use) as a second,
        # bounded attempt closes this gap generically -- for ANY entity/
        # measure/dimension combination, not this domain's specific nouns --
        # without lowering the trust bar itself (_corroborate_weak_entity_
        # candidate's own corroboration/lexical-trust requirement is
        # unchanged); it only tries a second real, already-available anchor
        # term before giving up. Only attempted when the primary anchor
        # exists and differs from the fallback, and only ever consumes the
        # extra adjudication call when the primary attempt actually failed.
        fallback_anchor_term = (
            dimension_terms[0] if dimension_terms
            else (ai_interpretation.measures[0] if ai_interpretation.measures else None)
        )
        anchor_candidates = [primary_anchor_term]
        if fallback_anchor_term and fallback_anchor_term != primary_anchor_term:
            anchor_candidates.append(fallback_anchor_term)

        retry_adjudication = None
        anchor_attempts = 0
        for anchor_term in anchor_candidates:
            anchor_attempts += 1
            retry_adjudication = _corroborate_weak_entity_candidate(
                source_id, user_id, entity_name, evidence_bundle, anchor_term,
                list(ai_interpretation.requested_attributes) or None, session=session,
            )
            if retry_adjudication is not None:
                break
        retry_cand_info = (
            evidence_bundle["candidates_by_fqn"].get(retry_adjudication.selected_table_fqn)
            if retry_adjudication is not None else None
        )
        if retry_adjudication is None or retry_cand_info is None:
            return {
                "attempted": True, "valid": False,
                "reason": f"weak_name_relevance_evidence: name_score={candidate.get('name_score')!r} for {fqn}",
                "adjudication_call_count": 1 + anchor_attempts if primary_anchor_term else 1,
                "selected_table_fqns": [fqn],
                "physical_plan": None, "query_plan": None, "sql_plan": None, "generated": None,
            }
        adjudication = retry_adjudication
        fqn = adjudication.selected_table_fqn
        cand_info = retry_cand_info
        candidate = cand_info["candidate"]
        ctx = cand_info["ctx"]
        all_candidates = [info["candidate"] for info in evidence_bundle["candidates_by_fqn"].values()]
        known_column_names = [c["column_name"] for c in (ctx.get("columns") or [])]
        corroboration_used = True

    from data.query_planning_service import (
        _date_order_hint_root, _find_date_column_with_hint, enrich_entity_count_selection,
    )

    plan_columns: list[PlanColumn] = []
    plan_group_by: tuple[str, ...] = ()
    plan_joins: tuple[PlanJoinEdge, ...] = ()
    extra_plan_tables: tuple[PlanTable, ...] = ()
    extra_tables_columns: dict[str, list[str]] = {}
    adjudication_call_count = (1 + anchor_attempts) if corroboration_used else 1
    combined_confidence = adjudication.confidence
    # Every table a measure/dimension/filter resolves onto beyond the
    # entity table fqn, collected here and joined ONCE at the end (after
    # filters are resolved too) rather than per-role -- see the unified
    # join computation below.
    other_table_ctxs: dict[str, dict] = {}
    # Set inside the bare-list branch only, when Part 3's measure-value
    # ranking resolves a real column -- declared here so the ORDER BY
    # construction below (which runs after the if/elif/else chain, for
    # every branch) can always safely check it regardless of which branch
    # executed.
    ranking_selected: dict | None = None
    if aggregation == "COUNT":
        distinct_requested = bool(query_intent.get("distinct"))
        measure_selected = enrich_entity_count_selection(
            source_id, user_id, {"table_fqn": fqn}, distinct_requested=distinct_requested,
        )
        measures = [{"term": entity_name, "selected": measure_selected, "candidates": [], "warnings": []}]
        dimensions: list[dict] = []
        if grain_term is not None:
            from data.query_planning_service import _resolve_calendar_grain_dimension

            hint_root = _date_order_hint_root(question)
            grain_dimension = _resolve_calendar_grain_dimension(
                grain_term, fqn, ctx, [hint_root] if hint_root else [],
            )
            dimensions = [grain_dimension]
            if grain_dimension["selected"]:
                plan_group_by = (grain_dimension["selected"]["column_name"],)
                plan_columns.append(PlanColumn(
                    table_fqn=fqn, column_name=grain_dimension["selected"]["column_name"],
                    role="dimension", time_grain=grain_term,
                ))
        elif dimension_terms:
            # Part 11 — non-calendar COUNT grouping ("students PER
            # program"). Identical resolution loop to the SUM/AVG/MIN/MAX
            # branch below (_resolve_non_calendar_dimension, never assumed
            # to share a table with the entity; multiple terms join into
            # the shared other_table_ctxs pool exactly the same way) --
            # kept as its own copy rather than a shared helper because the
            # COUNT branch's `dimensions`/`plan_group_by`/`plan_columns`
            # locals are already live at this point in the function (the
            # measure PlanColumn for COUNT itself is appended later, after
            # the unified join/fan-out-safety section, same as always).
            for term in dimension_terms:
                dim_resolution = _resolve_non_calendar_dimension(
                    source_id, user_id, term, fqn, session=session,
                )
                if dim_resolution is None:
                    return _disqualified(
                        "dimension_unresolvable_out_of_stage2_scope",
                        attempted=True, call_count=adjudication_call_count + 1,
                    )
                adjudication_call_count += 1
                dim_fqn = dim_resolution["dim_fqn"]
                dim_ctx = dim_resolution["dim_ctx"]
                group_selected = dim_resolution["group_selected"]
                dimensions.append({
                    "term": term, "selected": group_selected,
                    "candidates": [group_selected], "warnings": [],
                })
                plan_group_by = plan_group_by + (group_selected["column_name"],)
                plan_columns.append(PlanColumn(
                    table_fqn=group_selected["table_fqn"], column_name=group_selected["column_name"],
                    role="dimension",
                ))
                combined_confidence = min(combined_confidence, dim_resolution["candidate"].get("score") or 0.0)
                if dim_fqn != fqn:
                    other_table_ctxs[dim_fqn] = dim_ctx
        intent_type = "aggregate_by_dimension" if dimension_terms else None
        plan_intent = "distinct_entity_count" if distinct_requested else "entity_count"
        # The measure PlanColumn is appended after the unified cross-table
        # join/fan-out-safety section below -- a cross-table filter's join
        # may still force measure_selected["distinct"] to True there, and
        # this way there is exactly one place that builds this PlanColumn
        # instead of two that could disagree.
    elif aggregation in _NON_COUNT_AGGREGATIONS:
        # AI Physical Planner Coverage Expansion, Part C — SUM/AVG/MIN/MAX
        # on an AI-named measure column, optionally grouped by a (possibly
        # cross-table) dimension. Every identifier is still resolved
        # against real, supplied evidence only (_resolve_ai_measure_column/
        # _resolve_ai_measure_cross_table/_resolve_non_calendar_dimension)
        # -- this never invents a column, it only decides whether the
        # aggregation this stage now knows how to build is answerable for
        # THIS question's evidence.
        #
        # The grouping/entity table is NEVER assumed to also own the
        # measure (AI Physical Planner Coverage Expansion, Part D — REG-G-03
        # "Average course fee by program": the AI's one named entity is
        # "program", which is ALSO the grouping dimension, but the measure
        # (CourseFee) lives on a different, related table). Measure
        # resolution tries the entity's own table first (cheap, matches
        # every previously-working case unchanged), then falls back to an
        # independent evidence-gathering + adjudication pass
        # (_resolve_ai_measure_cross_table) exactly like the dimension
        # already does. Once both roles are resolved, ONE combined join
        # plan is computed across every table actually in play (entity +
        # measure's table if different + dimension's table if different),
        # rather than two independently-anchored plans that could disagree.
        measure_term = ai_interpretation.measures[0]
        measure_fqn, measure_ctx = fqn, ctx
        measure_selected = _resolve_ai_measure_column(fqn, ctx, measure_term, entity_hint=entity_name)
        measure_candidate: dict | None = None
        if measure_selected is None:
            cross = _resolve_ai_measure_cross_table(
                source_id, user_id, measure_term, fqn, ctx, entity_hint=entity_name, session=session,
            )
            if cross is None:
                # AI-First System-Wide Intelligence milestone, Part 7C --
                # "Show the most active accounts" (or any "most/least
                # <status word>" phrasing) reaches here with aggregation
                # "MAX"/"MIN" (query_intent["aggregation_target"] ==
                # "measure_max"/"measure_min") and a real status_value
                # (e.g. "Active"), but no numeric column exists for
                # "active"/"activity" -- correctly, since none should. This
                # is NOT a dead end: data.query_planning_service already
                # has a dedicated, already-tested capability for exactly
                # this shape (_try_build_implicit_ranking_plan, gated on
                # this SAME status_col-resolved + aggregation_target==
                # "measure_max" + no-measure precondition), wired to
                # core.semantic.implicit_measure_adjudicator.
                # infer_activity_measure -- a bounded, identifier-locked AI
                # pass that proposes ranking by COUNT of a trusted related
                # table's rows (or the entity's own recency date) as an
                # activity proxy. That capability is unreachable from here
                # because this reason previously categorized as "B"
                # (semantic ambiguity -- blocks legacy fallback entirely),
                # so a real, already-built, already-safe answer was refused
                # instead of computed. Reclassified to a distinct, narrow
                # category-A reason (Stage 1 capability not yet
                # implemented, not evidence-insufficient) ONLY when
                # query_intent's own shape matches the legacy function's
                # exact trigger precondition -- every other
                # measure-unresolvable case (a genuinely missing/ambiguous
                # named measure with no status/superlative signal) still
                # correctly refuses via category B, unchanged.
                if query_intent.get("aggregation_target") == "measure_max" and query_intent.get("status_value"):
                    # RETIRE-LEGACY-FALLBACK MILESTONE, Part 5 -- "most
                    # active" must be answered by AI-first's own reasoning,
                    # never by falling through to plan_business_query's
                    # independent table-selection/orchestration (a second,
                    # uncoordinated semantic decision on the same question).
                    # Calls the SAME deterministic plan-building helper
                    # (data.query_planning_service._try_build_implicit_
                    # ranking_plan) and the SAME bounded AI measure inference
                    # (core.semantic.implicit_measure_adjudicator.
                    # infer_activity_measure) legacy already wires, but
                    # directly against the table THIS stage already selected
                    # via real evidence/adjudication (`fqn`) -- never a fresh
                    # table search. Falls closed to the original disqualify
                    # reason (still category A -- safely refused, never
                    # legacy) if the implicit plan can't be built or fails
                    # deterministic SQL validation.
                    from data.query_planning_service import _try_build_implicit_ranking_plan
                    from core.semantic.implicit_measure_adjudicator import (
                        infer_activity_measure as _infer_activity_measure,
                    )

                    def _implicit_measure_resolver(entity_table_fqn, table_context, relationships):
                        return _infer_activity_measure(
                            entity_table_fqn, table_context, relationships, session=session,
                        )

                    related_table_contexts = _gather_trusted_related_table_contexts(
                        source_id, user_id, fqn, ctx, session=session,
                    )
                    implicit_query_plan = _try_build_implicit_ranking_plan(
                        source_id, user_id, question, fqn, related_table_contexts, query_intent,
                        [], [], [], [], [], [],
                        implicit_measure_resolver=_implicit_measure_resolver,
                    )
                    if implicit_query_plan is not None:
                        from data.sql_planning_service import build_sql_plan as _build_sql_plan
                        from data.sql_generation_service import (
                            detect_dialect as _detect_dialect, generate_sql as _generate_sql,
                        )

                        implicit_sql_plan = _build_sql_plan(
                            source_id, user_id, implicit_query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii,
                        )
                        if implicit_sql_plan["validation"]["valid"]:
                            implicit_generated = _generate_sql(
                                source_id, user_id, implicit_sql_plan, dialect=_detect_dialect(source_id),
                            )
                            if implicit_generated.get("sql"):
                                return {
                                    "attempted": True, "valid": True, "reason": None,
                                    "adjudication_call_count": adjudication_call_count + 1,
                                    "selected_table_fqns": [fqn],
                                    "physical_plan": None, "query_plan": implicit_query_plan,
                                    "sql_plan": implicit_sql_plan, "generated": implicit_generated,
                                }
                    return _disqualified(
                        "implicit_activity_measure_deferred_to_legacy_out_of_stage1_scope",
                        attempted=True, call_count=adjudication_call_count,
                    )
                return _disqualified(
                    "measure_column_unresolvable_out_of_stage1_scope",
                    attempted=True, call_count=adjudication_call_count,
                )
            adjudication_call_count += 1
            measure_fqn = cross["measure_fqn"]
            measure_ctx = cross["measure_ctx"]
            measure_selected = cross["measure_selected"]
            measure_candidate = cross["candidate"]
            combined_confidence = min(combined_confidence, measure_candidate.get("score") or 0.0)

        measures = [{"term": measure_term, "selected": measure_selected, "candidates": [], "warnings": []}]
        plan_columns.append(PlanColumn(
            table_fqn=measure_fqn, column_name=measure_selected["column_name"], role="measure",
            aggregation=aggregation,
        ))

        # Compound Query Coverage, Part 8 — each dimension term independently
        # resolved via the SAME real-evidence mechanism a single dimension
        # already used (never assumed to share a table with the measure, the
        # entity, or each other): dimension_terms naming the SAME table is a
        # legitimate outcome (two attributes of one entity), and dimension_
        # terms naming DIFFERENT tables all join into the shared
        # other_table_ctxs pool below, resolved as ONE combined star-topology
        # join plan alongside the measure (and any filter) rather than N
        # independently-anchored ones that could disagree.
        dimensions = []
        for term in dimension_terms:
            dim_resolution = _resolve_non_calendar_dimension(
                source_id, user_id, term, fqn, session=session,
            )
            if dim_resolution is None:
                return _disqualified(
                    "dimension_unresolvable_out_of_stage2_scope",
                    attempted=True, call_count=adjudication_call_count + 1,
                )
            adjudication_call_count += 1
            dim_fqn = dim_resolution["dim_fqn"]
            dim_ctx = dim_resolution["dim_ctx"]
            group_selected = dim_resolution["group_selected"]
            dimensions.append({
                "term": term, "selected": group_selected,
                "candidates": [group_selected], "warnings": [],
            })
            plan_group_by = plan_group_by + (group_selected["column_name"],)
            plan_columns.append(PlanColumn(
                table_fqn=group_selected["table_fqn"], column_name=group_selected["column_name"],
                role="dimension",
            ))
            combined_confidence = min(combined_confidence, dim_resolution["candidate"].get("score") or 0.0)
            if dim_fqn != fqn:
                other_table_ctxs[dim_fqn] = dim_ctx

        # The measure's table (when it genuinely differs from the entity
        # table fqn) joins the shared other_table_ctxs pool -- each
        # dimension's table already added inside the loop above -- resolved
        # into ONE combined join plan below (alongside any cross-table
        # filter) rather than computed independently here.
        if measure_fqn != fqn:
            other_table_ctxs[measure_fqn] = measure_ctx

        intent_type = "aggregate_by_dimension" if dimensions else "aggregate_overall"
        plan_intent = "aggregate"
    else:
        measures = []
        dimensions = []
        intent_type = "list_entities"
        plan_intent = "list_entities"
        if ai_interpretation.requested_attributes:
            # Safe AI-Only Control milestone, Part 2 — a bare requested-
            # attributes question ("Show student email", "List customer
            # phone numbers"). Each attribute is resolved to a real column
            # on the entity's own table or, failing that, one trusted
            # related table (_resolve_requested_attributes) -- never an
            # invented identifier. The resolved columns become
            # query_plan["dimensions"] entries: the SAME shape
            # data.sql_planning_service._select_entries/_build_group_by
            # already use for a plain, non-aggregated SELECT column (no
            # GROUP BY is added, since no aggregated measure is ever
            # present alongside this shape) -- reused, not reinvented.
            attrs_resolved = _resolve_requested_attributes(
                source_id, user_id, fqn, ctx, ai_interpretation.requested_attributes, session=session,
            )
            if attrs_resolved is None:
                return _disqualified(
                    "requested_attribute_unresolvable_out_of_stage1_scope",
                    attempted=True, call_count=adjudication_call_count,
                )
            dimensions, attr_extra_tables = attrs_resolved
            for other_fqn, other_ctx in attr_extra_tables.items():
                other_table_ctxs.setdefault(other_fqn, other_ctx)
            for entry in dimensions:
                sel = entry["selected"]
                plan_columns.append(PlanColumn(
                    table_fqn=sel["table_fqn"], column_name=sel["column_name"], role="dimension",
                ))

        if ranking_measure_term is not None:
            # Safe AI-Only Control milestone, Part 3 — resolve the ranking
            # measure via the SAME bounded resolver SUM/AVG/MIN/MAX already
            # uses: the entity's own table first, then one trusted related
            # table (_resolve_ai_measure_cross_table) -- never guessed.
            # Added as a plain (non-aggregated) dimension column, exactly
            # like a requested attribute, so it's both visible in the
            # output (showing WHY rows are ranked as they are) and inside
            # the validated join graph the ORDER BY column reference below
            # requires.
            ranking_selected = _resolve_ai_measure_column(fqn, ctx, ranking_measure_term, entity_hint=entity_name)
            ranking_fqn, ranking_ctx = fqn, ctx
            if ranking_selected is None:
                cross = _resolve_ai_measure_cross_table(
                    source_id, user_id, ranking_measure_term, fqn, ctx, entity_hint=entity_name, session=session,
                )
                if cross is None:
                    return _disqualified(
                        "ranking_measure_unresolvable_out_of_stage2c_scope",
                        attempted=True, call_count=adjudication_call_count,
                    )
                adjudication_call_count += 1
                ranking_fqn = cross["measure_fqn"]
                ranking_ctx = cross["measure_ctx"]
                ranking_selected = cross["measure_selected"]
                combined_confidence = min(combined_confidence, cross["candidate"].get("score") or 0.0)
            if ranking_fqn != fqn:
                other_table_ctxs.setdefault(ranking_fqn, ranking_ctx)
            dimensions = dimensions + [{
                "term": ranking_measure_term, "selected": ranking_selected, "candidates": [], "warnings": [],
            }]
            plan_columns.append(PlanColumn(
                table_fqn=ranking_selected["table_fqn"], column_name=ranking_selected["column_name"],
                role="dimension",
            ))

    # Aggregation Semantics / Composed Ranking milestone, Part 1/2 (continued
    # from the `ranking_measure_term`/`rank_by_aggregate` block above) — now
    # that `plan_group_by` reflects the FULLY recovered grouping dimension
    # (ai_interpretation.dimensions when the AI named one, OR the
    # raw_dimension_terms fallback a few hundred lines above when it didn't
    # -- "Which depot has the highest total maintenance cost?" is exactly
    # the latter case: dimensions=() from the AI, "depot" recovered from
    # raw_dimension_terms instead), it is finally knowable whether this
    # question is genuinely grouped. Synthesize a ranking order ONLY when:
    # (a) no explicit order signal fired at all (top N/bottom N/latest/
    # earliest/bare top-best-worst still always take priority, unchanged --
    # this never overrides a real, already-resolved order_intent);
    # (b) a real aggregation function was chosen; (c) the question actually
    # ended up grouped (plan_group_by non-empty) -- a bare scalar aggregate
    # with no grouping (e.g. "largest single order", "highest single order
    # value" -- Part 7) has plan_group_by == () and is deliberately left
    # alone, falling through to the existing bare aggregate_overall shape
    # exactly as before; and (d) the question's own wording actually carries
    # a ranking-direction word (detect_ranking_direction) -- a plain "Total
    # maintenance cost by equipment type" with no superlative at all must
    # stay an unranked GROUP BY listing, never forced into a top-1 answer it
    # never asked for.
    if order_intent is None and aggregation in ({"COUNT"} | _NON_COUNT_AGGREGATIONS) and plan_group_by:
        from core.semantic.concept_resolver import detect_ranking_direction

        synthesized_direction = detect_ranking_direction(question)
        if synthesized_direction is not None:
            order_intent = {"direction": synthesized_direction, "limit": 1}
            rank_by_aggregate = True

    intent = dict(query_intent)
    if intent_type:
        intent["type"] = intent_type
    # Keep the traced/debuggable intent dict in sync with the actual order
    # applied -- query_intent["order"] alone would otherwise still show the
    # pre-synthesis None even when a ranking was synthesized just above,
    # misrepresenting the resolved plan to anything inspecting query_plan
    # (Part 2: aggregation function and ranking direction must be visible
    # and correct as two distinct fields, not silently out of sync).
    intent["order"] = order_intent

    plan_order: dict | None = None
    if order_intent:
        resolved_order = dict(order_intent)
        if rank_by_aggregate:
            # Compound Query Coverage, Part 9 — order by the query's own
            # aggregated measure (table_fqn/column_name identical to the
            # measure PlanColumn already appended above, whether COUNT or a
            # named SUM/AVG/MIN/MAX): data.sql_planning_service._build_
            # order_by matches this against its own select rows and orders
            # by that row's SELECT alias instead of the raw column whenever
            # a GROUP BY is present (already-built, unchanged mechanism --
            # see that function's own "Milestone Phase 2 — Analytics Intent
            # Layer" docstring). measures is always non-empty here: this
            # branch only runs from inside the aggregation arm below.
            agg_selected = measures[0]["selected"]
            resolved_order["table_fqn"] = agg_selected["table_fqn"]
            resolved_order["column_name"] = agg_selected["column_name"]
            plan_order = {
                "column_name": agg_selected["column_name"],
                "direction": order_intent.get("direction"),
                "limit": order_intent.get("limit"),
            }
        elif ranking_selected is not None:
            # Safe AI-Only Control milestone, Part 3 — measure-value
            # ranking: order by the already-resolved, real measure column
            # (never the entity's identity key, which would be a
            # meaningless order).
            resolved_order["table_fqn"] = ranking_selected["table_fqn"]
            resolved_order["column_name"] = ranking_selected["column_name"]
            plan_order = {
                "column_name": ranking_selected["column_name"],
                "direction": order_intent.get("direction"),
                "limit": order_intent.get("limit"),
            }
        else:
            # target=="date" only, per the disqualification check above.
            # Degrades gracefully (limit still applies, just unsorted)
            # exactly like data.query_planning_service's own resolution --
            # an unresolved SORT preference is never promoted to a hard
            # refusal, unlike an unresolved FILTER (dropping a filter
            # changes which rows appear at all; dropping a sort only
            # changes their order).
            date_col = _find_date_column_with_hint({fqn: ctx}, fqn, _date_order_hint_root(question))
            if date_col:
                order_fqn, order_col = date_col
                resolved_order["table_fqn"], resolved_order["column_name"] = order_fqn, order_col
                plan_order = {
                    "column_name": order_col, "direction": order_intent.get("direction"),
                    "limit": order_intent.get("limit"),
                }
        intent["order"] = resolved_order

    resolved_filters, filter_warnings, plan_filters = _resolve_stage2_filters(
        source_id, question, fqn, ctx, query_intent, user_id=user_id, session=session,
    )

    ai_filters = tuple(
        f for f in ai_interpretation.filters
        if not _is_redundant_date_filter(f, query_intent)
        and not _is_redundant_status_filter(f, query_intent)
    )
    if ai_filters:
        ai_resolved = _resolve_ai_filters(
            source_id, user_id, fqn, ctx, ai_filters, session=session,
        )
        if ai_resolved is None:
            return _disqualified(
                "filters_out_of_stage1_scope", attempted=True, call_count=adjudication_call_count,
            )
        ai_resolved_filters, ai_plan_filters, filter_other_tables = ai_resolved
        resolved_filters = resolved_filters + ai_resolved_filters
        plan_filters = plan_filters + ai_plan_filters
        for other_fqn, other_ctx in filter_other_tables.items():
            other_table_ctxs.setdefault(other_fqn, other_ctx)

    # NEXT MILESTONE — INTERACTIVE AI Q&A COVERAGE COMPLETION + SAFETY,
    # Part 2/3 — deterministic existence-idiom backstop. ai_interpretation.
    # filters (resolved above, through _resolve_ai_filters' existence
    # branch) is the PRIMARY signal for "has an email"/"missing a phone
    # number" -- but the AI's own extraction is not bit-exact deterministic
    # (documented elsewhere in this module: the same temperature=0 call can
    # vary run to run), so a question whose idiom the model fails to
    # surface as a filter on some call must not silently fall back to
    # answering a materially broader question (live-confirmed root cause of
    # REG-P-04: "How many students have an email on file?" counted every
    # student). core.semantic.concept_resolver.extract_query_intent's own
    # null_check_polarity (already computed into query_intent above) is the
    # SAME deterministic idiom detector data.query_planning_service's
    # legacy path already trusts for this exact idiom class -- reused here,
    # not reimplemented, generalized with real cross-table resolution
    # (_resolve_ai_existence_filter, backed by _resolve_requested_attribute)
    # instead of that path's same-table-only "already-resolved measure/
    # dimension" lookup. Only auto-applied when EXACTLY ONE deterministic
    # term is left over after every other role (entity, dimensions,
    # ranking measure, requested attributes, the AI's own filter
    # attributes) has claimed its own terms -- entity_consumed_terms is the
    # SAME "which raw terms does this entity's name account for" helper
    # data.query_planning_service's own leftover-term refusal logic already
    # uses, reused here rather than a second near-match implementation.
    # Zero leftover terms means the idiom's subject was already accounted
    # for elsewhere (nothing to attach); MORE than one is a genuine
    # ambiguity this stage must not guess through -- both a real idiom was
    # deterministically detected AND it cannot be safely represented, so
    # Part 2's "attempt bounded repair, otherwise refuse" applies: refuse.
    already_has_existence_filter = any(
        pf.operator in ("IS NOT NULL", "IS NULL") for pf in plan_filters
    )
    null_check_polarity = query_intent.get("null_check_polarity")
    if null_check_polarity and not already_has_existence_filter:
        from data.query_planning_service import _tokenize as _nc_tokenize
        from data.semantic_contract_service import entity_consumed_terms

        already_claimed_terms = set(entity_consumed_terms(entity_name, list(distinct_terms)))
        already_claimed_tokens: set[str] = set()
        # Live-confirmed bug in an earlier version of this exact block: for
        # a COUNT question, ai_interpretation.measures is NEVER actually
        # read anywhere in this function (the COUNT branch above builds its
        # own measure PlanColumn from entity_name's key column, not from
        # any AI-named measure term) -- so excluding its tokens here
        # unconditionally treated a term the model misfiled into `measures`
        # as "already accounted for" when nothing downstream ever consumed
        # it. "How many students have a phone number?" reproduced this
        # exactly: the model returned measures=("phone number",) instead
        # of a filter, this block wrongly treated "phone"/"number" as
        # claimed, leftover_terms came back empty, and the plan silently
        # answered a bare COUNT with no phone-number check at all -- the
        # SAME silently-dropped-qualifier failure class as REG-P-04 itself.
        # ai_interpretation.measures is only genuinely consumed when this
        # question is a real SUM/AVG/MIN/MAX aggregate (the _NON_COUNT_
        # AGGREGATIONS branch actually resolves ai_interpretation.
        # measures[0] into a measure column) -- excluded from the "already
        # claimed" set otherwise, so a COUNT question's stray measures
        # term stays a genuine leftover this backstop can still catch.
        already_claimed_source_terms = (
            list(dimension_terms)
            + ([ranking_measure_term] if ranking_measure_term else [])
            + [f.get("attribute") for f in ai_interpretation.filters if isinstance(f.get("attribute"), str)]
        )
        if aggregation in _NON_COUNT_AGGREGATIONS:
            already_claimed_source_terms += list(ai_interpretation.measures)
        # requested_attributes has the identical COUNT-branch gap: only the
        # bare-list branch (aggregation is None) actually resolves it
        # (_resolve_requested_attributes); a NON_COUNT aggregation either
        # already folded its one requested attribute into dimension_terms
        # above (requested_attribute_as_dimension) or the function already
        # disqualified before reaching here (requested_attributes_with_
        # aggregation_out_of_scope) -- either way safe to treat as claimed.
        # A COUNT question's requested_attributes is read nowhere at all,
        # so it must stay a genuine leftover candidate here too.
        if aggregation != "COUNT":
            already_claimed_source_terms += list(ai_interpretation.requested_attributes)
        for term in already_claimed_source_terms:
            already_claimed_tokens |= set(_nc_tokenize(term))

        leftover_terms = [
            t for t in distinct_terms
            if t not in already_claimed_terms and not (set(_nc_tokenize(t)) & already_claimed_tokens)
        ]
        if len(leftover_terms) == 1:
            existence = _resolve_ai_existence_filter(
                source_id, user_id, fqn, ctx, leftover_terms[0], null_check_polarity, session=session,
            )
            if existence is None:
                return _disqualified(
                    "null_check_qualifier_unresolvable_out_of_stage2_scope",
                    attempted=True, call_count=adjudication_call_count,
                )
            existence_resolved, existence_plan_filter, existence_extra_tables = existence
            resolved_filters = resolved_filters + [existence_resolved]
            plan_filters = plan_filters + [existence_plan_filter]
            for other_fqn, other_ctx in existence_extra_tables.items():
                other_table_ctxs.setdefault(other_fqn, other_ctx)
        elif len(leftover_terms) > 1:
            return _disqualified(
                "null_check_qualifier_ambiguous_subject_out_of_stage2_scope",
                attempted=True, call_count=adjudication_call_count,
            )

    # Unified cross-table join, computed ONCE here -- after measure,
    # dimension, AND filter resolution are all known -- across every table
    # actually in play beyond the entity table fqn (AI Physical Planner
    # Coverage Expansion, Parts D/E). Always anchored at fqn itself, never
    # a "try every table as anchor" search: an INNER JOIN's result set is
    # identical regardless of which side is written as FROM vs JOIN, so
    # fixing the anchor loses no correctness, while keeping it consistent
    # with every other fqn-anchored resolution in this function (filters/
    # ordering/provenance) avoids a combined measure+filter (or
    # dimension+filter) join ever disagreeing on direction. Reuses
    # data.query_planning_service._plan_joins' own native N-table
    # star-topology support -- never a new join optimizer.
    #
    # REG-G-03 fan-out fix. Exception to "always anchored at fqn": for a
    # SUM/AVG/MIN/MAX question, when fqn (the AI's named entity, "course"
    # for "Average course fee by program") ends up referenced by NEITHER
    # the measure nor any dimension/filter -- i.e. the measure and grouping
    # dimension both resolved onto OTHER, genuinely related tables (REG-G-03:
    # measure on dbo.EDU_Class, dimension on dbo.EDU_Track, entity fqn=
    # dbo.EDU_Course used by neither) -- forcing fqn into the join anyway is
    # not merely redundant, it is a live fan-out risk _fqn_exposed_to_fanout
    # below cannot see: that check only inspects edges touching measure_fqn
    # directly, not duplication introduced by a SIBLING branch (fqn and
    # measure_fqn both MANY_TO_ONE into the same shared dimension table) --
    # live-confirmed against a real production database: dbo.EDU_Course and dbo.EDU_Class are
    # both MANY_TO_ONE into dbo.EDU_Track, so including the entirely-unused
    # dbo.EDU_Course in the join multiplies every dbo.EDU_Class row by
    # however many Course rows share its Path, silently corrupting AVG the
    # moment any Path has more than one Course (today's a real production database data
    # happens not to expose this, verified by an independent 2-table
    # comparison query, but the join shape itself does not depend on that).
    # Dropping fqn and re-anchoring at measure_fqn (the actual grain owner,
    # matching data.query_planning_service._plan_joins' own docstring
    # recommendation to anchor at "the table holding the primary measure")
    # removes the risk structurally instead of trying to detect it after
    # the fact -- never invents a join, only omits one proven unnecessary.
    needed_fqns = {pc.table_fqn for pc in plan_columns} | {pf.table_fqn for pf in plan_filters}
    drop_unused_entity_anchor = bool(
        other_table_ctxs and aggregation in _NON_COUNT_AGGREGATIONS and fqn not in needed_fqns
    )
    combined_join_plan: dict | None = None
    if other_table_ctxs:
        from data.query_planning_service import _plan_joins

        if drop_unused_entity_anchor:
            full_table_set = set(other_table_ctxs.keys())
            join_anchor = measure_fqn
        else:
            full_table_set = {fqn, *other_table_ctxs.keys()}
            join_anchor = fqn
        combined_join_plan = _plan_joins(source_id, user_id, join_anchor, full_table_set)
        combined_steps = combined_join_plan.get("steps") or []
        # drop_unused_entity_anchor can legitimately collapse full_table_set
        # to a single table (measure and every dimension/filter all resolved
        # onto the SAME table once the unused entity fqn is excluded --
        # REG-G-03's "by class" shape) -- _plan_joins itself already
        # reports that correctly as required=False with zero steps, never a
        # failure; only a genuinely multi-table set with no steps (or an
        # unfound path) is the real disqualifying condition.
        if combined_join_plan.get("required") and (
            not combined_steps or not all(step.get("path_found") for step in combined_steps)
        ):
            return _disqualified(
                "cross_table_join_path_unresolvable_out_of_stage2c_scope",
                attempted=True, call_count=adjudication_call_count,
            )

        for other_fqn, other_ctx in other_table_ctxs.items():
            extra_plan_tables = extra_plan_tables + (
                PlanTable(candidate_id=other_fqn, table_fqn=other_fqn, role="related"),
            )
            extra_tables_columns[other_fqn] = [c["column_name"] for c in (other_ctx.get("columns") or [])]

        plan_joins = tuple(
            PlanJoinEdge(
                left_table=step["from_table"], left_column=step["from_column"],
                right_table=step["to_table"], right_column=step["to_column"],
            )
            for step in combined_steps if step.get("path_found")
        )

        if aggregation == "COUNT":
            # AI Physical Planner Coverage Expansion, Part E — a one-to-many
            # join introduced by a cross-table filter (e.g. Student joined
            # to a ClassSignups filter table, where one student can have
            # several signup rows) can multiply each counted entity across
            # duplicate rows. Reuses data.query_planning_service's own
            # existing fan-out guard (already proven on the legacy path),
            # wired into the AI physical planner path for the first time:
            # forces COUNT(DISTINCT key) when a trusted key is available,
            # or refuses (clears the selection, caught below) rather than
            # risk returning an inflated count.
            #
            # _plan_joins' own fanout_risk is edge-direction-relative, not
            # fqn-relative (see _fqn_exposed_to_fanout) -- escalate to at
            # least MEDIUM when fqn itself is exposed, so the guard below
            # engages even when the edge's own label reads LOW.
            from data.query_planning_service import _apply_join_fanout_safety

            effective_join_plan = combined_join_plan
            if (
                combined_join_plan.get("fanout_risk") not in ("MEDIUM", "HIGH")
                and _fqn_exposed_to_fanout(fqn, combined_steps)
            ):
                effective_join_plan = dict(combined_join_plan)
                effective_join_plan["fanout_risk"] = "MEDIUM"

            _apply_join_fanout_safety(measures, effective_join_plan, filter_warnings)
            if not (measures and measures[0].get("selected")):
                return _disqualified(
                    "count_fanout_safety_refused_out_of_stage2e_scope",
                    attempted=True, call_count=adjudication_call_count,
                )
        elif aggregation in _NON_COUNT_AGGREGATIONS:
            # Safe AI-Only Control milestone, Part 6 — a one-to-many join
            # (introduced by a cross-table measure/dimension, or a filter)
            # can duplicate the MEASURE's own rows, inflating SUM/AVG (and
            # corrupting MIN/MAX's candidate pool with repeated values).
            # Unlike COUNT, DISTINCT is NOT a valid fix here -- SUM(DISTINCT
            # revenue) would incorrectly collapse genuinely-equal values
            # from DIFFERENT rows (two different $100 orders are not the
            # same value to deduplicate) -- never blindly applied. The
            # existing architecture has no grain-restructuring mechanism (a
            # pre-aggregated subquery) to fall back to, so per this
            # milestone's own instruction ("restructure at the proper grain
            # if existing architecture supports it OR safely refuse"): any
            # join that exposes the measure's own table (measure_fqn,
            # checked via the SAME fqn-relative fan-out re-derivation the
            # COUNT path above uses -- never the edge-direction-relative
            # label alone) to duplication safely disqualifies rather than
            # risk a knowingly-inflated aggregate.
            if _fqn_exposed_to_fanout(measure_fqn, combined_steps):
                return _disqualified(
                    "aggregate_fanout_safety_refused_out_of_stage2f_scope",
                    attempted=True, call_count=adjudication_call_count,
                )

    if aggregation == "COUNT":
        count_selected = measures[0]["selected"] if measures else None
        if count_selected and count_selected.get("column_name"):
            plan_columns.append(PlanColumn(
                table_fqn=fqn, column_name=count_selected["column_name"], role="measure",
                aggregation="COUNT", distinct=bool(count_selected.get("distinct")),
            ))

    all_tables = [fqn] + list(extra_tables_columns.keys())
    all_columns = {fqn: known_column_names, **extra_tables_columns}
    selected_table_fqns = all_tables
    if combined_join_plan is not None:
        # The RAW data.query_planning_service._plan_joins output, unchanged
        # -- same convention _attempt_relationship_list_plan already uses.
        # Its "steps" rows carry cardinality/fanout_risk/confidence per hop,
        # which data.sql_planning_service.build_sql_plan's all_joins_trusted
        # check reads; a hand-rebuilt dict here would silently drop that
        # evidence.
        join_plan_dict = combined_join_plan
    else:
        join_plan_dict = {"tables": all_tables, "primary_table": fqn, "joins": [], "required": False, "fanout_risk": None}

    query_plan = {
        "source_id": source_id,
        "intent": intent,
        "tables": all_tables,
        "columns": all_columns,
        "measures": measures,
        "dimensions": dimensions,
        "concepts": [{"term": entity_name, "selected": candidate, "candidates": all_candidates}],
        "filters": resolved_filters,
        "join_plan": join_plan_dict,
        "warnings": filter_warnings,
        "confidence": combined_confidence,
        "explanation": [
            f"AI physical planner selected {fqn} for entity '{entity_name}' "
            f"(confidence {adjudication.confidence:.2f}).",
        ],
        "candidate_ranking": [],
        "remembered_terminology": [],
        "generated_vocabulary_evidence": [],
        "entity": entity_name if not measures else None,
        "entity_table": fqn if not measures else None,
        "entity_confidence": adjudication.confidence,
        "entity_candidates": all_candidates,
        "metadata_search_failed": False,
        "metadata_search_failed_reason": None,
        "grounding": None,
    }

    physical_plan = PhysicalQueryPlan(
        intent=plan_intent,
        source_id=source_id,
        tables=(PlanTable(candidate_id=fqn, table_fqn=fqn, role="primary"),) + extra_plan_tables,
        columns=tuple(plan_columns),
        filters=tuple(plan_filters),
        group_by=plan_group_by,
        order_by=plan_order,
        joins=plan_joins,
        comparison=comparison_intent,
        expected_result_shape=ai_interpretation.expected_result_shape,
        confidence=combined_confidence,
        candidate_provenance={t: t for t in all_tables},
        evidence_references=adjudication.evidence_references,
    )

    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import detect_dialect, generate_sql
    from core.perf import stage_timer

    with stage_timer.measure("sql_plan_construction"):
        sql_plan = build_sql_plan(source_id, user_id, query_plan, allow_unconfirmed_pii=allow_unconfirmed_pii)
    if not sql_plan["validation"]["valid"]:
        return {
            "attempted": True, "valid": False,
            "reason": "deterministic_validation_failed: " + "; ".join(sql_plan["validation"]["blocking_reasons"]),
            "adjudication_call_count": adjudication_call_count, "selected_table_fqns": selected_table_fqns,
            "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": None,
        }

    with stage_timer.measure("sql_generation"):
        generated = generate_sql(source_id, user_id, sql_plan, dialect=detect_dialect(source_id))
    if not generated.get("sql"):
        return {
            "attempted": True, "valid": False, "reason": "sql_generation_produced_no_sql",
            "adjudication_call_count": adjudication_call_count, "selected_table_fqns": selected_table_fqns,
            "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": generated,
        }

    return {
        "attempted": True, "valid": True, "reason": None,
        "adjudication_call_count": adjudication_call_count, "selected_table_fqns": selected_table_fqns,
        "physical_plan": physical_plan, "query_plan": query_plan, "sql_plan": sql_plan, "generated": generated,
    }
