from __future__ import annotations

import itertools
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.orchestrator.interfaces import IContextBuilder
from core.orchestrator.models import (
    EvidenceItem,
    EvidencePackage,
    OrchestratorRequest,
    ResolvedIntent,
    ServiceCallRecord,
    ServiceCapability,
    ServiceDescriptor,
)

logger = logging.getLogger(__name__)

_Adapter = Callable[[OrchestratorRequest], Any]


# ---------------------------------------------------------------------------
# Service adapters — thin wrappers that call existing service functions.
# Each adapter accepts OrchestratorRequest and returns the raw service output.
# Returns None when required parameters (source_id, user_id) are absent.
# All underlying services manage their own DB connections via data.db.get_connection().
# ---------------------------------------------------------------------------

def _dictionary(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.dictionary_service import list_dictionary_tables
    return list_dictionary_tables(source_id=req.source_id, user_id=req.user_id)


def _domain(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.domain_service import get_domain_summary
    return get_domain_summary(source_id=req.source_id, user_id=req.user_id)


def _entity(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.entity_service import get_entity_summary
    return get_entity_summary(source_id=req.source_id, user_id=req.user_id)


def _profiling(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.profiling_service import get_latest_profile
    return get_latest_profile(source_id=req.source_id, user_id=req.user_id)


def _governance(req: OrchestratorRequest) -> Any:
    from data.governance_service import governance_readiness_summary
    return governance_readiness_summary(source_id=req.source_id)


def _relationship(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.relationship_service import get_relationship_summary
    return get_relationship_summary(source_id=req.source_id, user_id=req.user_id)


def _knowledge_graph(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.knowledge_graph_service import knowledge_graph_summary
    return knowledge_graph_summary(source_id=req.source_id, user_id=req.user_id)


def _lineage(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.lineage_service import lineage_summary
    return lineage_summary(source_id=req.source_id, user_id=req.user_id)


def _semantic_layer(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.semantic_layer_service import semantic_summary
    return semantic_summary(source_id=req.source_id, user_id=req.user_id)


def _business_knowledge(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.business_knowledge_service import get_business_summary
    return get_business_summary(source_id=req.source_id, user_id=req.user_id)


def _reports(req: OrchestratorRequest) -> Any:
    if req.user_id is None:
        return None
    from data.report_service import list_reports_for_user
    return list_reports_for_user(user_id=req.user_id)


def _workflow(req: OrchestratorRequest) -> Any:
    if req.user_id is None:
        return None
    from data.workflow_service import list_workflows
    return list_workflows(user_id=req.user_id)


def _schema(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.schema_service import get_latest_snapshot
    return get_latest_snapshot(source_id=req.source_id, user_id=req.user_id)


def _search(req: OrchestratorRequest) -> Any:
    from data.search_service import search_metadata
    return search_metadata(q=req.query, limit=20, source_id=req.source_id)


def _live_metadata(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from core.live.connection_resolver import LiveConnectionResolver
    from core.live.health_service import ConnectionHealthService
    from core.live.metadata_provider import LiveMetadataProvider
    from core.live.models import ResolutionStatus

    resolution = LiveConnectionResolver().resolve(
        req.source_id, req.user_id, required_capability="schema_discovery"
    )
    if resolution.status != ResolutionStatus.RESOLVED:
        return {"connection_state": resolution.status.value, "message": resolution.message}

    health = ConnectionHealthService().check(resolution.context)
    metadata = LiveMetadataProvider().get_metadata(resolution.context)
    metadata["connection_state"] = health.state.value
    return metadata


# ---------------------------------------------------------------------------
# Milestone Phase 6.6 — Enterprise Clarification Intelligence
#
# query_planning_service.py already refuses to auto-select a measure/
# dimension when its top-ranked candidate doesn't clear the ambiguity
# margin over the runner-up (selected=None, an "ambiguous_*" warning, a
# ranked candidates[:5] list) — the existing Authoritative Ranking signal.
# These helpers surface that signal as a clarification turn instead of
# letting the question fall through to a plain SQL-generation refusal, and
# apply a user's follow-up selection back onto query_plan before SQL
# planning/generation ever run. No ranking, scoring, or margin logic is
# touched — only query_planning_service's own already-computed output.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# EDP Day 1 — Candidate-family resolution.
#
# _extract_ambiguous_terms below surfaces every ambiguous_* candidate group
# as a clarification turn, whether the tied candidates are genuinely
# different business objects or just physical variants of the SAME object
# (a trusted view over the base table, an export/report copy, a backup/
# history/temp snapshot, a derivative/rolling view). Day 1 requires the
# former to still clarify and the latter to resolve silently ("obvious
# duplicates must not create clarification... genuine source-system
# ambiguity must still clarify").
#
# This never touches query_planning_service's own scoring (name_score/
# authority_bonus/table_object_type) — it only classifies, from each
# candidate's own table_fqn/table_object_type, whether a tied group is one
# object wearing several disguises from the SAME schema, or genuinely
# separate objects/source systems. When it's the former, the highest-ranked
# disguise is fed back through _apply_clarification_overrides exactly as a
# user's own clarification pick would be — no second "selected" mechanism.
# ---------------------------------------------------------------------------

_FAMILY_RANK: dict[str, int] = {
    "canonical":    0,
    "trusted_view": 1,
    "derivative":   2,
    "export":       3,
    "backup":       4,
}

# Order matters: first pattern that matches the bare table name wins.
_FAMILY_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("backup",     re.compile(r"(_bak|_backup|_old|_archive|_hist(?:ory)?|_temp|_tmp|_staging|_deprecated)$", re.IGNORECASE)),
    ("export",     re.compile(r"(_export|_extract|_rpt|_report(?:_copy)?)$|^rpt_", re.IGNORECASE)),
    ("derivative", re.compile(r"(_rolling|_derived|_calc|_mv|_summary)$", re.IGNORECASE)),
]
_VIEW_PREFIX_RE = re.compile(r"^(vw_|v_)", re.IGNORECASE)


def _infer_candidate_family(candidate: dict) -> tuple[int, str]:
    """Return (rank, family_label) for one candidate — lower rank wins a tie.
    table_object_type ("VIEW"/"TABLE") comes straight from the already-
    fetched schema metadata (query_planning_service._score_candidates);
    naming heuristics run only on the bare table name (schema stripped).
    """
    table_fqn = candidate.get("table_fqn") or ""
    table_name = table_fqn.split(".")[-1]
    object_type = (candidate.get("table_object_type") or "").upper()

    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(table_name):
            return _FAMILY_RANK[family], family

    if object_type == "VIEW" or _VIEW_PREFIX_RE.match(table_name):
        return _FAMILY_RANK["trusted_view"], "trusted_view"

    return _FAMILY_RANK["canonical"], "canonical"


def _candidate_base_name(candidate: dict) -> str:
    """Strip family suffixes/prefixes so 'Students', 'vw_Students', and
    'Students_export' all normalize to the same base name."""
    table_fqn = candidate.get("table_fqn") or ""
    table_name = table_fqn.split(".")[-1]
    stripped = _VIEW_PREFIX_RE.sub("", table_name)
    for _family, pattern in _FAMILY_PATTERNS:
        stripped = pattern.sub("", stripped)
    return stripped.lower()


def _candidate_source_system(candidate: dict) -> str:
    """Proxy for 'separate source system': the schema portion of table_fqn.
    Two candidates in different schemas are never collapsed automatically —
    that is exactly the genuine ambiguity Day 1 requires to still clarify.
    """
    table_fqn = candidate.get("table_fqn") or ""
    parts = table_fqn.split(".")
    return parts[0].lower() if len(parts) > 1 else ""


def _auto_resolve_duplicate_families(ambiguous_terms: list[dict]) -> list[dict]:
    """For each ambiguous term, group its candidates by (base_name,
    source_system). When every candidate collapses into a SINGLE group —
    i.e. every tied candidate is just a physical variant of the same
    business object from the same source system — return a clarification-
    style selection for the group's highest-ranked (canonical-first) member
    instead of leaving the term ambiguous. A term whose candidates span more
    than one group (a genuinely different base object, or the same object
    duplicated across source systems/schemas) is left out — still ambiguous,
    still clarified by the caller.

    Returns selections in the exact {"term", "table_fqn", "column_name"}
    shape _apply_clarification_overrides already accepts from a real user
    clarification answer — no second resolution path.
    """
    auto_selections: list[dict] = []
    for group in ambiguous_terms:
        candidates = group.get("candidates") or []
        if len(candidates) < 2:
            continue
        keys = {(_candidate_base_name(c), _candidate_source_system(c)) for c in candidates}
        if len(keys) != 1:
            continue  # more than one real object or source system — genuine ambiguity
        best = min(candidates, key=lambda c: (_infer_candidate_family(c)[0], -(c.get("score") or 0.0)))
        auto_selections.append({
            "term": group.get("term"),
            "table_fqn": best.get("table_fqn"),
            "column_name": best.get("column_name"),
        })
    return auto_selections


def _extract_ambiguous_terms(query_plan: dict) -> list[dict]:
    """Normalize query_plan's unresolved, ambiguous measures/dimensions into
    a clarification-ready shape. A term only qualifies when
    query_planning_service itself flagged it ambiguous (not just missing)
    and left >=1 ranked candidate — never invents ambiguity on its own.

    Day 2C follow-up ("material qualifier policy"): each measure/dimension
    term is now evaluated independently, regardless of whether some OTHER
    term in the same question already resolved. Previously this function
    bailed out entirely — returning [] — the moment ANY measure/dimension
    had a `selected` value, mirroring sql_planning_service.py's own "if
    select: skip unresolved ones" leniency. That leniency is appropriate for
    a genuinely decorative extra word, but verified live against real CCPP
    it silently swallowed a real one too: "How many students are currently
    enrolled?" resolves "students" confidently (aggregation_target=
    entity_count) while "currently"/"enrolled" both come back
    ambiguous_measure with zero usable signal — and the old bail-out meant
    that was NEVER offered as a clarification, silently becoming "how many
    students are there at all" instead. Now every ambiguous_{kind} entry is
    surfaced independently; a genuinely resolved OTHER term no longer
    suppresses it. data.sql_planning_service.build_sql_plan's own leniency
    is a separate, narrower backstop (see its own updated docstring) for
    exactly the case this function can't reach: a caller with no
    clarification wiring at all (execute_query_route).

    Each returned group carries "tied": True when >=2 candidates scored
    within the ambiguity margin of each other (a genuine tie — the original,
    unchanged case), or False when exactly one candidate was found but
    didn't clear the auto-select confidence threshold on its own (Phase 2 —
    query_planning_service's own _resolve_term already emits an
    "ambiguous_{kind}" warning for this single-weak-candidate case, e.g.
    "No confident measure match... below the 0.50 threshold" — offered as a
    single-guess clarification rather than silently dropped either way; the
    caller renders "tied" vs not with different wording so a genuine tie
    is never described as a single guess, or vice versa).
    """
    intent = query_plan.get("intent") or {}
    no_aggregation = intent.get("aggregation") is None and intent.get("aggregation_target") is None

    # extract_terms() puts every "before"-clause word into both `concepts`
    # and `measures` (data.query_planning_service reads `measures` as
    # candidate METRIC columns to resolve). For a bare/ranked entity
    # listing with no aggregation ("Show the 10 most recently added
    # students") that dual-classification is a false positive: "students"
    # already resolved confidently as the query's business entity via
    # query_plan["concepts"], but the SAME word, tried again as a measure
    # column name, predictably finds nothing but weak/unrelated numeric
    # columns and gets flagged "ambiguous_measure" — a byproduct of the
    # dual-classification, not real ambiguity. Left unfiltered, that
    # spurious entry used to block the Entity-Centric Planner fallback
    # below even though the entity itself was never actually ambiguous.
    resolved_concept_terms = {
        (c.get("term") or "").lower()
        for c in (query_plan.get("concepts") or [])
        if c.get("selected")
    }

    ambiguous: list[dict] = []
    for kind, entries in (
        ("measure", query_plan.get("measures") or []),
        ("dimension", query_plan.get("dimensions") or []),
    ):
        for entry in entries:
            candidates = entry.get("candidates") or []
            is_ambiguous = any(
                str(w.get("type", "")).startswith("ambiguous_") for w in (entry.get("warnings") or [])
            )
            if not (is_ambiguous and candidates):
                continue
            term = entry.get("term")
            if kind == "measure" and no_aggregation and (term or "").lower() in resolved_concept_terms:
                continue
            deduped_candidates: list[dict] = []
            seen_candidates: set[tuple] = set()
            for c in candidates:
                key = (c.get("table_fqn"), c.get("column_name"))
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                deduped_candidates.append(c)
            # Day 2C follow-up ("material qualifier policy") — a MULTI-
            # candidate list where EVERY entry scores exactly 0 is not a
            # genuine "did you mean X or Y" choice; it's a fake tie with no
            # real differentiation between the options, functionally
            # identical to missing_{kind}. Reproduced against a real
            # fixture: "Top 10 sales by amount" flagged "amount" ambiguous_
            # dimension against 5 unrelated columns that all scored 0.0.
            # Deliberately does NOT exclude a SINGLE candidate at score 0 —
            # that is the pre-existing, deliberate Phase 2 design (a bare-
            # entity-count term against the only table in the database is a
            # legitimate, if weak, single-guess clarification, not a fake
            # tie — see this function's own docstring above and
            # tests/test_composer_sql_routing.py::
            # test_returns_enterprise_answer_with_live_query_citation),
            # left completely unchanged.
            if len(deduped_candidates) >= 2 and not any((c.get("score") or 0) > 0 for c in deduped_candidates):
                continue
            ambiguous.append({
                "term": term, "kind": kind, "candidates": deduped_candidates,
                "tied": len(deduped_candidates) >= 2,
            })

    # Entity-Centric Planner (A5 Milestone 1) — a bare business-entity
    # question ("Show candidates") has no measure/dimension term at all, so
    # the loop above finds nothing to flag; the ambiguity instead lives on
    # query_plan["concepts"] (query_planning_service._resolve_concept's own
    # pre-existing table-resolution — the SAME _score_term_match/
    # _score_table_authority/_AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN
    # machinery already used above, never a second scorer). Only surfaced
    # when:
    #   - nothing else in the plan resolved OR was even flagged ambiguous
    #     (the `if not ambiguous` guard — measure/dimension ambiguity, when
    #     present, always takes priority, unchanged);
    #   - no aggregation/ranking was requested (query_plan["intent"], already
    #     computed by extract_query_intent()/_build_intent(), never
    #     re-derived here) — "Total revenue"/"Top recruiters" must never
    #     fall back to a bare entity listing just because their metric-
    #     column search happened to find nothing;
    #   - exactly one concept term was extracted — a multi-concept bare
    #     request ("show clients and invoices") stays a plain refusal,
    #     mirroring _build_intent's own existing list_entities eligibility
    #     rule exactly, rather than guessing which concept to clarify.
    if not ambiguous:
        order = intent.get("order") or {}
        # A chronological order ("most recently added", "earliest") needs no
        # measure column at all — its table_fqn/column_name are already
        # resolved directly off the date column (extract_query_intent's own
        # order.target=="date" path), independent of measures/concepts. Only
        # a ranking BY A METRIC (order present, no "date" target) still
        # requires the measure path and must keep blocking this fallback —
        # "Top recruiters by placements" must never fall back to a bare
        # entity listing just because its metric-column search found
        # nothing, unchanged.
        no_ranking = not order or order.get("target") == "date"
        concepts = query_plan.get("concepts") or []
        if no_aggregation and no_ranking and len(concepts) == 1:
            concept = concepts[0]
            candidates = concept.get("candidates") or []
            if not concept.get("selected") and candidates:
                normalized_candidates = [
                    {
                        "table_fqn":           c.get("table_fqn"),
                        "column_name":         None,
                        "business_label":      None,
                        "table_business_name": c.get("business_name"),
                        "entity_name":         c.get("entity"),
                        "domain_name":         c.get("domain"),
                        "score":               c.get("score"),
                    }
                    for c in candidates
                ]
                ambiguous.append({
                    "term": concept.get("term"), "kind": "concept",
                    "candidates": normalized_candidates,
                    "tied": len(normalized_candidates) >= 2,
                })
    return ambiguous


def _extract_ambiguous_terms_after_family_collapse(
    query_plan: dict, source_id: int, user_id: str, question: str,
) -> list[dict]:
    """EDP Day 1 — _extract_ambiguous_terms, but first silently auto-
    resolves obvious same-object candidate-family duplicates (a trusted
    view, export copy, or backup/history table of the SAME canonical
    object in the SAME schema — see _auto_resolve_duplicate_families)
    before ever surfacing a clarification turn.

    Both _live_query (below) and core.orchestrator.agent's own
    clarification check call this instead of calling
    _extract_ambiguous_terms directly, so obvious-duplicate collapsing
    behaves identically on every question-answering path — one resolution
    rule, not two copies that could drift.
    """
    ambiguous_terms = _extract_ambiguous_terms(query_plan)
    if not ambiguous_terms:
        return ambiguous_terms

    auto_selections = _auto_resolve_duplicate_families(ambiguous_terms)
    if not auto_selections:
        return ambiguous_terms

    from core.semantic.concept_resolver import extract_query_intent

    distinct_requested = extract_query_intent(question).get("aggregation_target") == "distinct_entity_count"
    _apply_clarification_overrides(
        query_plan, auto_selections, source_id, user_id,
        distinct_requested=distinct_requested,
    )
    return _extract_ambiguous_terms(query_plan)


def _apply_clarification_overrides(
    query_plan: dict, selections: list[dict], source_id: int, user_id: str, *, distinct_requested: bool,
) -> None:
    """Patch a user's clarification picks onto query_plan's own
    already-ranked candidates. Never accepts a table_fqn/column_name that
    wasn't already one of query_planning_service's own candidates for that
    term — an unmatched selection is left unresolved so it is re-asked
    rather than silently guessed.

    Skipped entirely when the plan requires a join: join_plan/fanout-risk
    were computed by plan_business_query against the ORIGINAL (unresolved)
    table selection, so patching a different table post-hoc could leave a
    stale join plan. Multi-table ambiguity resume is a documented remaining
    gap, not something this function risks getting silently wrong.
    """
    if not selections or (query_plan.get("join_plan") or {}).get("required"):
        return

    by_term = {sel.get("term"): sel for sel in selections if sel.get("term")}
    concept_resolved = False

    for kind, entries in (
        ("measure", query_plan.get("measures") or []),
        ("dimension", query_plan.get("dimensions") or []),
        # Entity-Centric Planner (A5 Milestone 1) — a concept-kind
        # ambiguous_terms group is _extract_ambiguous_terms' normalized copy
        # of query_plan["concepts"][i]["candidates"] (_resolve_concept's own
        # output); the match/select logic below is identical to measure/
        # dimension resume, since concept candidates never carry a
        # column_name either (the existing "choice.get('column_name') is
        # None" branch already covers it, unchanged).
        ("concept", query_plan.get("concepts") or []),
    ):
        for entry in entries:
            if entry.get("selected") is not None:
                continue
            choice = by_term.get(entry.get("term"))
            if not choice:
                continue
            candidates = entry.get("candidates") or []
            match = next(
                (
                    c for c in candidates
                    if c.get("table_fqn") == choice.get("table_fqn")
                    and (choice.get("column_name") is None or c.get("column_name") == choice.get("column_name"))
                ),
                None,
            )
            if match is None:
                continue  # invalid selection — leave unresolved, re-ask

            is_entity_count = kind == "measure" and all(c.get("column_name") is None for c in candidates)
            if is_entity_count:
                from data.query_planning_service import enrich_entity_count_selection
                match = enrich_entity_count_selection(
                    source_id, user_id, match, distinct_requested=distinct_requested,
                )

            entry["selected"] = match
            if kind == "concept":
                # _resolve_concept's own contract — "resolved"/"ambiguity_reason",
                # never a "warnings" list (see query_planning_service.py).
                entry["resolved"] = True
                entry["ambiguity_reason"] = None
                concept_resolved = True
            else:
                entry["warnings"] = [
                    w for w in (entry.get("warnings") or []) if not str(w.get("type", "")).startswith("ambiguous_")
                ]

    # Sprint 1.5 — Join-Aware Clarification. The join_plan attached to
    # query_plan above was computed before any of this function's overrides
    # were applied (selected_tables was empty at that point, since ambiguous
    # terms by definition had selected=None) — so it's stale for whatever
    # tables the user's picks actually resolved to. Recompute it the same
    # way plan_business_query itself does (query_planning_service.py
    # ~1159-1170), reusing _plan_joins verbatim, so build_sql_plan/
    # generate_sql see the real join requirement instead of a stale
    # "no join needed" plan.
    from data.query_planning_service import _plan_joins

    selected_tables: set[str] = set()
    primary_table = None
    for entry in query_plan.get("measures") or []:
        sel = entry.get("selected")
        if sel:
            selected_tables.add(sel["table_fqn"])
            if primary_table is None:
                primary_table = sel["table_fqn"]
    for entry in query_plan.get("dimensions") or []:
        sel = entry.get("selected")
        if sel:
            selected_tables.add(sel["table_fqn"])

    query_plan["join_plan"] = _plan_joins(source_id, user_id, primary_table, selected_tables)

    # Entity-Centric Planner (A5 Milestone 1) — a concept resolved above was,
    # by construction (see _extract_ambiguous_terms), the ONLY term in the
    # whole plan with no measure/dimension selected — exactly the condition
    # _build_intent's own "bare entity" branch already checks
    # (len(resolved_concepts)==1 and len(resolved)==1) to decide
    # intent.type=="list_entities". _build_intent ran on the first pass
    # before this concept resolved, so its output is stale here — this sets
    # the SAME field to the SAME value _build_intent would compute if
    # re-invoked now, the same "recompute after override" idiom already used
    # above for join_plan/order, never a second intent-classification rule.
    # sql_planning_service.build_sql_plan's existing (unmodified) bare-
    # entity-list routing is strictly gated on this exact field/value.
    if concept_resolved:
        query_plan["intent"]["type"] = "list_entities"
        resolved_concept = next(
            (c for c in (query_plan.get("concepts") or []) if c.get("selected")), None,
        )
        if resolved_concept is not None:
            query_plan["entity"] = resolved_concept.get("term")
            query_plan["entity_table"] = resolved_concept["selected"].get("table_fqn")
            query_plan["entity_confidence"] = resolved_concept.get("confidence")
            query_plan["entity_candidates"] = resolved_concept.get("candidates") or []

    # Enterprise Implementation — Recompute Order After Clarification. A
    # ranking question ("Which courses have the highest enrollment?") has
    # its ORDER BY target resolved by plan_business_query from the winning
    # measure's column (query_planning_service._resolve_ranking_order_column)
    # — but when that measure was itself ambiguous, plan_business_query's
    # first pass ran before any override was applied, so
    # query_plan["intent"]["order"] was left without a table_fqn/column_name
    # (a "no measure to rank by" warning) even though a measure now has been
    # selected, right above. Reuses the exact same resolution rule — never a
    # second ordering implementation — now that measures[].selected is
    # populated. A date-target order (unrelated to which measure/dimension
    # was ambiguous) and an order that already resolved are left untouched,
    # preserving non-ranking clarification behavior exactly as before.
    order = (query_plan.get("intent") or {}).get("order")
    if order and order.get("target") != "date" and not order.get("column_name"):
        from data.query_planning_service import _resolve_ranking_order_column

        resolved_order, order_warning = _resolve_ranking_order_column(order, query_plan["measures"])
        query_plan["intent"]["order"] = resolved_order
        if not order_warning:
            query_plan["warnings"] = [
                w for w in (query_plan.get("warnings") or []) if w.get("type") != "order_column_not_found"
            ]


# Sprint 1.5 — Join-Aware Clarification refusal message, returned in place
# of a clarification turn when no combination of candidates across the
# ambiguous terms can actually be joined.
NO_VALID_JOIN_MESSAGE = (
    "I found matching business fields but they cannot be combined because "
    "no relationship exists."
)


# ---------------------------------------------------------------------------
# Enterprise Implementation — CCPP Safe Enrollment Question Support.
#
# "Which courses have the highest enrollment?" has no correct answer in the
# generic semantic pipeline: enrollment (ADF_ClassSignups) only has a
# trusted relationship to ADF_Class, never to ADF_Course — every route to
# Course fans out through the shared ADF_Path parent (many courses per
# path), misattributing one class's enrollment to every course on its path.
# "Which classes have the highest enrollment?" is the question this data
# source can actually answer, via a real, declared, single-hop FK
# (ADF_ClassSignups.ClassID -> ADF_Class.ClassID).
#
# This is a hardcoded answer to one specific, verified real-CCPP business
# question — not a change to how the generic semantic/join-ranking pipeline
# resolves ambiguous questions in general. It intercepts before
# plan_business_query ever runs, so the courses phrasing can never
# accidentally produce the flawed Path-based query, and the classes
# phrasing never depends on generic candidate-table scoring picking the
# right table by name-match luck.
# ---------------------------------------------------------------------------

CLASSES_ENROLLMENT_CLARIFICATION = (
    '"Which courses have the highest enrollment?" cannot be answered correctly — '
    "enrollment is tracked per class, not per course, in this data source, and every "
    "class-to-course route requires fanning out through a shared path. "
    "Did you mean: Which classes have the highest enrollment?"
)


def _class_enrollment_sql(dialect: str) -> str:
    """
    The one correct, safe business model for "which classes have the
    highest enrollment": ADF_ClassSignups.ClassID -> ADF_Class.ClassID,
    COUNT(DISTINCT StudentID), cancelled signups excluded (CancelID IS
    NULL — confirmed by the declared FK ADF_ClassSignups.CancelID ->
    ADF_Cancel.CancelID). Never touches ADF_Enrollment_Tracking (no
    declared PK/FK, unprofiled, ~15 rows), ADF_Course, or ADF_Path.

    Reuses sql_generation_service's own identifier-quoting helpers so
    table/column rendering stays dialect-consistent with every other
    generated query, rather than hand-formatting quotes here. No user input
    is interpolated anywhere in this fixed query, so it is safe to assemble
    as a plain string (no parameters to bind).
    """
    from data.sql_dialects import get_adapter
    from data.sql_generation_service import _qcol, _qfqn

    adapter = get_adapter(dialect)
    signups, classes = "dbo.ADF_ClassSignups", "dbo.ADF_Class"
    alias = adapter.quote_identifier("enrolled_student_count")
    limit_prefix = adapter.row_limit_prefix(10)
    limit_suffix = adapter.row_limit_suffix(10)

    lines = [
        f"SELECT {limit_prefix}"
        f"{_qcol(classes, 'ClassID', adapter)}, {_qcol(classes, 'ClassName', adapter)}, "
        f"COUNT(DISTINCT {_qcol(signups, 'StudentID', adapter)}) AS {alias}",
        f"FROM {_qfqn(signups, adapter)}",
        f"JOIN {_qfqn(classes, adapter)} "
        f"ON {_qcol(signups, 'ClassID', adapter)} = {_qcol(classes, 'ClassID', adapter)}",
        f"WHERE {_qcol(signups, 'CancelID', adapter)} IS NULL",
        f"GROUP BY {_qcol(classes, 'ClassID', adapter)}, {_qcol(classes, 'ClassName', adapter)}",
        f"ORDER BY {alias} DESC",
    ]
    if limit_suffix:
        lines.append(limit_suffix)
    return "\n".join(lines)


def _class_enrollment_ranking_entity(question: str) -> str | None:
    """
    Returns "class", "course", or None. Reuses the existing wh-ranking
    analytics-intent detector (core.semantic.concept_resolver.
    derive_analytics_intent) verbatim — never a second "is this a ranking
    question" implementation — and only narrows on top of it to the exact
    entity/measure combination this hardcoded business model covers.
    """
    from core.semantic.concept_resolver import derive_analytics_intent

    analytics_intent = derive_analytics_intent(question)
    if (
        analytics_intent.get("confidence") != "high"
        or analytics_intent.get("ordering") != "DESC"
        or "enroll" not in (analytics_intent.get("measure") or "").lower()
    ):
        return None
    entity = (analytics_intent.get("entity") or "").strip().lower()
    if entity in ("class", "classes"):
        return "class"
    if entity in ("course", "courses"):
        return "course"
    return None


def _is_verified_ccpp_source(source_id: int, user_id: str) -> bool:
    """
    Gates the hardcoded CCPP enrollment override (both the "classes" SQL and
    the "courses" clarification) to only the one real data source this
    business model was verified against. Without this, the override would
    apply to ANY source's "classes/courses ... enrollment" question purely
    from question text, generating SQL against dbo.ADF_ClassSignups /
    dbo.ADF_Class tables that don't exist on other tenants' sources, or
    rejecting a legitimately answerable course-enrollment question there.

    Reuses data.datasource_service.get_connection_config — the same
    read-only, ownership-scoped lookup core.live.connection_resolver already
    calls for live connections — rather than adding a second lookup path.

    Identity check: the connection's configured "database" param (the real
    SQL Server database name, required by core.connectors.relational.mssql)
    equals "ccpp", case-insensitively. This is a stable, source-scoped
    configuration value set once when the connection was created — unlike
    display_name (a free-text user label a customer could name anything,
    explicitly excluded) or source_type/dialect (shared by every mssql
    tenant, explicitly excluded). No dedicated "verified source" flag exists
    yet in data_source_connections for this purpose; if one is added later,
    prefer it over this field.

    Fails closed on any uncertainty (lookup error, missing record, missing
    or mismatched database param) — returns False so the caller falls
    through to the generic pipeline instead of applying CCPP-specific
    behavior.
    """
    from data.datasource_service import get_connection_config

    try:
        record = get_connection_config(source_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "CCPP enrollment override: datasource lookup failed for "
            "source_id=%s — failing closed", source_id,
        )
        return False

    if record is None:
        return False

    database = (record.get("params") or {}).get("database")
    return isinstance(database, str) and database.strip().lower() == "ccpp"


def _filter_joinable_clarification(
    ambiguous_terms: list[dict], source_id: int, user_id: str,
) -> tuple[list[dict], bool]:
    """Sprint 1.5 — Join-Aware Clarification. _extract_ambiguous_terms above
    resolves each ambiguous term's candidates independently; it never checks
    whether a combination of picks across terms actually belongs to
    joinable tables. Left unchecked, a user can pick a perfectly valid
    candidate for each term individually and still hit a SQL-generation
    refusal later because the two tables can't be joined.

    This validates every combination up front (one candidate per term)
    using the existing join graph (_plan_joins, itself built on
    analyze_join_quality/recommend_best_join_path — no new relationship
    engine), keeps only candidates that appear in at least one joinable
    combination, and ranks them by semantic confidence, join distance,
    authority, and relationship confidence. Returns (filtered_terms,
    no_valid_join) — no_valid_join is True only when NO combination at all
    is joinable, in which case the caller must refuse instead of asking.

    A single ambiguous term has nothing to join against, so it is returned
    unchanged — today's single-term clarification behavior is untouched.
    """
    if len(ambiguous_terms) < 2:
        return ambiguous_terms, False

    from data.query_planning_service import _plan_joins

    term_candidates = [term.get("candidates") or [] for term in ambiguous_terms]

    ranked_combos = []
    for combo in itertools.product(*term_candidates):
        tables = {c["table_fqn"] for c in combo}
        if len(tables) <= 1:
            join_distance, relationship_confidence = 0, 100
        else:
            join_plan = _plan_joins(source_id, user_id, sorted(tables)[0], tables)
            steps = join_plan.get("steps") or []
            if not steps or not all(s.get("path_found") for s in steps):
                continue  # this combination is not joinable — excluded
            join_distance = len(steps)
            relationship_confidence = join_plan.get("confidence", 0)

        semantic_confidence = sum(c.get("score", 0.0) for c in combo) / len(combo)
        authority = sum(c.get("authority_bonus", 0.0) for c in combo) / len(combo)
        rank_key = (-semantic_confidence, join_distance, -authority, -relationship_confidence)
        ranked_combos.append((rank_key, combo))

    if not ranked_combos:
        return ambiguous_terms, True

    ranked_combos.sort(key=lambda rc: rc[0])

    # For each (term position, candidate), keep only its best (lowest) rank
    # key across every joinable combination it appeared in.
    best_for_candidate: dict[tuple, tuple] = {}
    for rank_key, combo in ranked_combos:
        for idx, candidate in enumerate(combo):
            cand_id = (idx, candidate.get("table_fqn"), candidate.get("column_name"))
            if cand_id not in best_for_candidate or rank_key < best_for_candidate[cand_id][0]:
                best_for_candidate[cand_id] = (rank_key, candidate)

    filtered_terms = []
    for idx, term in enumerate(ambiguous_terms):
        kept = sorted(
            (
                (rank_key, candidate)
                for (t_idx, _, _), (rank_key, candidate) in best_for_candidate.items()
                if t_idx == idx
            ),
            key=lambda pair: pair[0],
        )
        kept_candidates = [c for _, c in kept]
        # "tied" is recomputed against the POST-filter candidate count: join-
        # ability filtering can narrow an originally-tied term down to a
        # single joinable candidate, at which point it is no longer a real
        # tie for wording purposes (see _extract_ambiguous_terms).
        filtered_terms.append({**term, "candidates": kept_candidates, "tied": len(kept_candidates) >= 2})

    return filtered_terms, False


def _build_business_plan(question: str, query_plan: dict, sql_plan: dict) -> dict:
    """Milestone M-25 — compact bridge between the semantic/SQL planning
    stages and answer rendering. Carries only sql_plan's already-clean
    select/where/group_by/order_by rows plus a few derived business labels —
    never the raw candidate lists living on query_plan (measures[].candidates,
    concepts, etc.). Does not affect planning/SQL generation in any way; this
    runs strictly after both stages have already produced their output.
    """
    from core.semantic.concept_resolver import extract_query_intent

    intent = query_plan.get("intent") or {}
    aggregation_plan = sql_plan.get("aggregation_plan")
    select = sql_plan.get("select") or []
    where = sql_plan.get("where") or []

    def _first_business_label(entries: list) -> str | None:
        for entry in entries:
            sel = entry.get("selected")
            if sel:
                return sel.get("business_label") or sel.get("table_fqn", "").split(".")[-1]
        return None

    measures = query_plan.get("measures") or []
    dimensions = query_plan.get("dimensions") or []

    dimension_labels = {}
    for entry in dimensions:
        sel = entry.get("selected")
        if sel and sel.get("column_name"):
            dimension_labels[sel["column_name"]] = sel.get("business_label") or sel["column_name"]

    # Recover the human date-range label ("this month") and status adjective
    # ("Active") by re-deriving query intent from the question text —
    # extract_query_intent is a pure, side-effect-free function already used
    # elsewhere in this chain; this does not touch
    # query_planning_service/sql_planning_service at all.
    recovered_intent = extract_query_intent(question) or {}
    date_range = recovered_intent.get("date_range")
    date_context = None
    if date_range:
        for w in where:
            if w.get("operator") == "BETWEEN" and w.get("value") == [date_range["start"], date_range["end"]]:
                date_context = {"label": date_range["label"], "start": date_range["start"], "end": date_range["end"]}
                break

    status_value = recovered_intent.get("status_value")
    status_label = None
    if status_value:
        for w in where:
            if w.get("operator") == "=" and w.get("value") == status_value:
                status_label = status_value
                break

    return {
        "aggregation": intent.get("aggregation"),
        "aggregation_target": intent.get("aggregation_target"),
        "distinct": bool(intent.get("distinct")),
        "entity_label": (aggregation_plan or {}).get("counted_entity") or _first_business_label(measures),
        "measure_label": None if aggregation_plan else _first_business_label(measures),
        "select": select,
        "where": where,
        "group_by": sql_plan.get("group_by") or [],
        "order_by": sql_plan.get("order_by") or [],
        "order_intent": intent.get("order"),
        "dimension_labels": dimension_labels,
        "date_context": date_context,
        "status_label": status_label,
        "source_tables": sorted({r["table_fqn"] for r in select if r.get("table_fqn")}),
        # Phase 3, Step 4 — passed straight through from query_plan (already
        # deduplicated by query_planning_service.plan_business_query); pure
        # explainability, read only by citation_builder._cite_live_query,
        # never by SQL generation or planning.
        "remembered_terminology": query_plan.get("remembered_terminology") or [],
        # Enterprise Phase 4 — same pass-through pattern, one row per
        # (original_term, generated_term) pair whose generated-vocabulary
        # bonus actually decided a selected candidate (already deduplicated
        # by plan_business_query). Read only by
        # citation_builder._cite_generated_vocabulary.
        "generated_vocabulary_evidence": query_plan.get("generated_vocabulary_evidence") or [],
        # Rule G — real per-stage confidence, passed straight through (never
        # re-derived) for core.answering.explanation_builder._explain_live_query
        # to fold into the final answer confidence instead of a hardcoded
        # constant. "plan_confidence" is query_planning_service._compute_
        # confidence's 0-100 blend of measure/dimension match score, join
        # confidence, and warning penalties; "entity_confidence" is the
        # bare-concept resolution score (0-1) for a list_entities/
        # joined_detail_list question, where plan_confidence alone
        # under-represents a confident table match (no measure/dimension
        # score to average).
        "plan_confidence": query_plan.get("confidence"),
        "entity_confidence": query_plan.get("entity_confidence"),
    }


# ---------------------------------------------------------------------------
# EDP Day 1 — Modern Semantic Understanding.
#
# One structured AI pass over the raw question, run BEFORE
# plan_business_query (the deterministic planner) below. Additive only: on
# success, the AI's entities/measures/dimensions are unioned onto
# extract_terms()'s own regex-based lists, never replacing them — same
# fail-closed philosophy as every other optional AI layer in this codebase
# (see core.ai.semantic_intelligence). Disabled by default
# (ENABLE_AI_QUESTION_INTERPRETER); any failure, timeout, or schema
# violation inside core.semantic.ai_interpreter.interpret() returns None and
# this function is a no-op, so plan_business_query always still runs off the
# deterministic extract_terms() output alone.
# ---------------------------------------------------------------------------

def _run_ai_question_interpreter(source_id: int, user_id: str, question: str):
    from core.config import ENABLE_AI_QUESTION_INTERPRETER
    if not ENABLE_AI_QUESTION_INTERPRETER:
        return None
    from core.semantic.ai_interpreter import build_grounding_vocabulary, interpret

    vocabulary = build_grounding_vocabulary(source_id, user_id)
    return interpret(question, known_vocabulary=vocabulary)


def _phrase_singular_plural_equivalent(a: str, b: str) -> bool:
    """True if a and b are the same phrase modulo a simple singular/plural
    (or other short trailing-suffix) variant on each word — e.g.
    "invoice"/"invoices", "job order"/"job orders". Mirrors the narrow,
    non-stemming tolerance of data.query_planning_service._tokens_near_match
    (same length-difference cap), reimplemented locally rather than imported
    so this orchestrator-level merge doesn't reach into that module's
    private planning internals.
    """
    a_words = a.lower().split()
    b_words = b.lower().split()
    if len(a_words) != len(b_words):
        return False
    for wa, wb in zip(a_words, b_words):
        if wa == wb:
            continue
        shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
        if len(shorter) >= 3 and longer.startswith(shorter) and len(longer) - len(shorter) <= 2:
            continue
        return False
    return True


def _merge_ai_terms(existing: list[str], additions: list[str]) -> list[str]:
    """Merge AI-interpreter entities/measures/dimensions into the
    deterministically-extracted terms, treating a singular/plural variant of
    an already-present term as a duplicate rather than a new concept.

    Without this, a term like "invoice" (AI, singular per its own prompt
    rule) and "invoices" (deterministic, the literal word in the question)
    both survive the old dict.fromkeys() dedup as distinct strings, which
    silently breaks the bare_entity_term guard in
    data.query_planning_service (requires len(concepts) == 1 to keep a bare
    "<status> <entity>" question a row list instead of running it through
    measure-column resolution).
    """
    merged = list(existing)
    for term in additions:
        if term in merged:
            continue
        if any(_phrase_singular_plural_equivalent(term, seen) for seen in merged):
            continue
        merged.append(term)
    return merged


def _plan_with_autonomous_preparation(
    source_id: int,
    user_id: str,
    question: str,
    *,
    filters: list | None = None,
    allow_unconfirmed_pii: bool = False,
    on_plan_resolved: Optional[Callable[[dict], Optional[dict]]] = None,
) -> dict:
    """
    Shared planning core for BOTH production question paths — _live_query
    (POST /v1/composer/ask, via EnterpriseOrchestrator) and
    POST /v1/sources/{id}/execute-query (api/v1/routes.execute_query_route) —
    so neither route re-implements its own planner-retry policy.

    Runs plan_business_query -> build_sql_plan -> generate_sql. AI-Native.2 —
    Autonomous Preparation Contract: when generation refuses purely because
    an already-identified candidate table lacks column-level metadata, runs
    one bounded targeted-preparation cycle (data.metadata_preparation_service
    .prepare_selected_tables, scoped only to that plan's candidate tables)
    and retries the whole plan -> build -> generate sequence exactly once.
    Never retries more than once, and never retries when the plan found no
    candidate table at all (nothing to prepare) — see prepare_selected_tables
    and its own tests for the preparation step's own bounds.

    on_plan_resolved, if given, is called with each freshly-resolved
    query_plan (before SQL planning/generation) and may return an early-exit
    result dict to short-circuit the whole call — used by _live_query for
    its clarification-required / no_valid_join handling, which must never
    reach SQL generation and must never trigger preparation (a genuinely
    ambiguous question needs clarification, not more metadata). Callers that
    don't need this (execute_query_route today has no clarification
    handling of its own) simply omit it.

    Returns one of:
      {"outcome": "unowned"}                          — unknown/unowned source
      {"outcome": "early_exit", "result": <dict>}      — on_plan_resolved short-circuited
      {"outcome": "planned"|"refused",
       "query_plan":, "sql_plan":, "generated":, "preparation_trace": dict | None}
    """
    from core.semantic.concept_resolver import extract_terms
    from data.query_planning_service import plan_business_query
    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import detect_dialect, generate_sql

    concepts, measures, dimensions = extract_terms(question)

    # Day 2B, Task 6 / Day 2C, Task 5 — skip the AI interpreter's OpenAI
    # round-trip (the dominant single latency contributor observed in Day
    # 2A frontend verification, 6-30s per question) ONLY when every
    # material entity this question names is actually grounded (a verified
    # RESOLVED/PARTIAL contract with a canonical table — apply_grounding's
    # own "fully_grounded" criterion), not merely whenever any one term
    # lexically matches a target-entity name. Day 2B's original check
    # (match_entities_for_terms alone) skipped the interpreter for the
    # WHOLE question the moment ANY term matched one of the 10 target-
    # entity names, even if that entity had no contract at all yet (still
    # NO_CANDIDATE) and even if OTHER entities in the same multi-entity
    # question (e.g. Student grounded, Enrollment/Course not) were
    # completely unresolved — exactly the failure mode Day 2C's
    # verification flagged: "skipping the AI interpreter whenever any one
    # term is grounded may damage multi-entity questions". apply_grounding
    # is cache-backed (get_or_build_entity_contract does no rediscovery on
    # a fresh cache hit — tests/test_semantic_contract_service.py::
    # test_cache_hit_skips_discovery_entirely), so this second call here
    # (the real planning pass a few lines later calls apply_grounding again
    # via plan_business_query) is a cheap cache read, not a duplicated
    # discovery pass — an already-grounded Student contract is reused, not
    # re-derived. Any question naming no target entity at all keeps
    # today's behavior exactly (interpreter runs when enabled).
    from data.semantic_contract_service import apply_grounding, grounding_fully_accounted, match_entities_for_terms

    # Day 2E, Task 3 — also skip when every matched entity is fully
    # contract-ACCOUNTED-for: grounded OR confirmed-unsupported (Task 4).
    # A confirmed dead end (e.g. Recruiter/Placement) is exactly as settled
    # as a verified contract for this purpose — an AI interpretation pass
    # cannot resolve either one differently, so paying its latency (and, in
    # this environment, a real OpenAI call) for a question the pipeline
    # will fast-exit moments later is pure waste, live-confirmed at 55.7s
    # for "Show recruiters and their placements."
    from core.perf import stage_timer

    ai_interpretation = None
    if match_entities_for_terms(question, concepts, measures, dimensions):
        grounding_precheck = apply_grounding(source_id, user_id, question, concepts, measures, dimensions)
        if not grounding_fully_accounted(grounding_precheck):
            with stage_timer.measure("ai_question_interpretation"):
                ai_interpretation = _run_ai_question_interpreter(source_id, user_id, question)
    else:
        with stage_timer.measure("ai_question_interpretation"):
            ai_interpretation = _run_ai_question_interpreter(source_id, user_id, question)
    if ai_interpretation is not None:
        concepts = _merge_ai_terms(concepts, list(ai_interpretation.entities))
        measures = _merge_ai_terms(measures, list(ai_interpretation.measures))
        dimensions = _merge_ai_terms(dimensions, list(ai_interpretation.dimensions))

    request_payload = {
        "question": question,
        "concepts": concepts,
        "measures": measures,
        "dimensions": dimensions,
        "filters": filters or [],
    }
    if ai_interpretation is not None:
        # Explainability/Day-2 handoff only — plan_business_query reads only
        # question/concepts/measures/dimensions/filters from this dict
        # (data/query_planning_service.py::_plan_business_query_impl) and
        # ignores unknown keys, so attaching this never changes planning
        # behavior. date_range/sorting/filters/relationship_intent here are
        # not yet threaded into SQL planning — Day 2 scope.
        request_payload["ai_interpretation"] = {
            "entities": list(ai_interpretation.entities),
            "measures": list(ai_interpretation.measures),
            "dimensions": list(ai_interpretation.dimensions),
            "requested_attributes": list(ai_interpretation.requested_attributes),
            "relationship_intent": ai_interpretation.relationship_intent,
            "expected_result_shape": ai_interpretation.expected_result_shape,
            "date_range": ai_interpretation.date_range,
            "sorting": ai_interpretation.sorting,
            "clarification_required": ai_interpretation.clarification_required,
            "clarification_reason": ai_interpretation.clarification_reason,
        }

    preparation_trace: dict | None = None
    already_retried = False

    while True:
        query_plan = plan_business_query(source_id, user_id, request_payload)
        if query_plan is None:
            return {"outcome": "unowned"}

        # Day 2E, Task 5 — a confirmed-unsupported entity (Task 4) short-
        # circuits here, BEFORE build_sql_plan/generate_sql ever run and
        # before on_plan_resolved's own ambiguous-term/clarification-picker
        # check — there is nothing to build SQL from, and no genuine
        # ambiguity to offer a physical-table choice for: the contract
        # already positively confirmed no reliable table exists. Returns
        # the same {"outcome": "early_exit", "result": {...}} shape
        # on_plan_resolved itself returns, so core.orchestrator.agent's
        # existing early-exit dispatch handles it via one more `reason`
        # branch rather than a new outcome type.
        if query_plan.get("unsupported_entities"):
            return {"outcome": "early_exit", "result": {
                "executed": False, "reason": "unsupported_entity",
                "question": question,
                "unsupported_entities": query_plan["unsupported_entities"],
                "business_messages": query_plan.get("business_messages") or [],
                "warnings": query_plan.get("warnings") or [],
            }}

        if on_plan_resolved is not None:
            early = on_plan_resolved(query_plan)
            if early is not None:
                return {"outcome": "early_exit", "result": early}

        sql_plan = build_sql_plan(
            source_id, user_id, query_plan,
            allow_unconfirmed_pii=allow_unconfirmed_pii,
        )
        generated = generate_sql(source_id, user_id, sql_plan, dialect=detect_dialect(source_id))

        if generated.get("sql"):
            return {
                "outcome": "planned", "query_plan": query_plan, "sql_plan": sql_plan,
                "generated": generated, "preparation_trace": preparation_trace,
            }

        candidate_tables = query_plan.get("tables") or []
        if already_retried or not candidate_tables:
            # Bounded: never retry a second time, and never retry when the
            # plan found no candidate table at all (nothing to prepare).
            return {
                "outcome": "refused", "query_plan": query_plan, "sql_plan": sql_plan,
                "generated": generated, "preparation_trace": preparation_trace,
            }

        from data.metadata_preparation_service import prepare_selected_tables

        already_retried = True
        prep_result = prepare_selected_tables(source_id, user_id, candidate_tables)
        preparation_trace = {
            "candidate_tables": candidate_tables,
            "prepared_tables": prep_result.prepared,
            "skipped_tables": prep_result.skipped,
            "failed_tables": prep_result.failed,
            "retried": bool(prep_result.changed),
        }
        if not prep_result.changed:
            # Preparation added nothing new (already-thin metadata that
            # couldn't be improved, or every table failed) — no point
            # retrying planning with unchanged inputs.
            return {
                "outcome": "refused", "query_plan": query_plan, "sql_plan": sql_plan,
                "generated": generated, "preparation_trace": preparation_trace,
            }
        # Preparation added column metadata for at least one candidate table
        # — retry planning exactly once with the same request payload.


def _live_query(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.query_execution_service import execute_governed_query

    sql = req.params.get("sql")
    if sql:
        # Trusted caller already has exact SQL to run (Phase 7 bypass) —
        # unchanged behavior. No sql_plan exists for a caller-supplied raw
        # SQL string, so _governance_recheck has no columns to classify;
        # routed through execute_governed_query anyway so every live
        # execution shares one governed entry point, audit included.
        result, _gov_warnings = execute_governed_query(
            req.source_id, req.user_id, sql, {},
            params=req.params.get("sql_params"),
            row_limit=req.params.get("row_limit"),
            timeout_s=req.params.get("timeout_s"),
            page=req.params.get("page", 1),
            page_size=req.params.get("page_size"),
            max_payload_bytes=req.params.get("max_payload_bytes"),
        )
        return result.to_dict()

    # No pre-built SQL — treat this as a business question and run the full
    # existing chain: semantic resolution (query_planning_service) -> SQL
    # planning (sql_planning_service) -> SQL generation (sql_generation_service).
    # LiveQueryEngine.execute() (with its own read-only validation, ownership,
    # live_query_enabled gate, rate limits, and audit logging) is only ever
    # reached below when generation actually produced a validated SQL string.
    question = (req.params.get("question") or req.query or "").strip()
    if not question:
        return None

    from data.sql_generation_service import detect_dialect

    # Enterprise Implementation — CCPP Safe Enrollment Question Support.
    # Checked before plan_business_query ever runs: "courses" phrasing must
    # never reach the generic pipeline (which would silently produce the
    # flawed Path-based query), and "classes" phrasing is answered directly
    # from the verified business model rather than trusting generic
    # candidate-table scoring to land on the right table by name-match luck.
    ranking_entity = _class_enrollment_ranking_entity(question)
    if ranking_entity is not None and not _is_verified_ccpp_source(req.source_id, req.user_id):
        # Not the verified CCPP source (or identity is uncertain) — fail
        # closed and fall through to the generic pipeline below instead of
        # applying CCPP-specific SQL/clarification to an unrelated source.
        ranking_entity = None
    if ranking_entity == "course":
        return {
            "executed": False,
            "reason": "ambiguous_enrollment_entity",
            "question": question,
            "explanation": [CLASSES_ENROLLMENT_CLARIFICATION],
        }
    if ranking_entity == "class":
        sql = _class_enrollment_sql(detect_dialect(req.source_id))
        # Fixed, hand-verified query with no PII columns — still routed
        # through execute_governed_query (no sql_plan to classify) so every
        # branch shares the one governed entry point rather than being
        # exempted by construction.
        result, _gov_warnings = execute_governed_query(
            req.source_id, req.user_id, sql, {},
            row_limit=req.params.get("row_limit"),
            timeout_s=req.params.get("timeout_s"),
            page=req.params.get("page", 1),
            page_size=req.params.get("page_size"),
            max_payload_bytes=req.params.get("max_payload_bytes"),
        )
        data = result.to_dict()
        data["generated_sql"] = sql
        data["sql_generation_explanation"] = [
            "Answered directly from the verified CCPP enrollment business model "
            "(ADF_ClassSignups -> ADF_Class) — enrollment has no correct join path "
            "through ADF_Enrollment_Tracking, ADF_Course, or ADF_Path.",
        ]
        return data

    from core.semantic.concept_resolver import extract_query_intent

    def _on_plan_resolved(query_plan: dict) -> Optional[dict]:
        # Milestone Phase 6.6 — Enterprise Clarification Intelligence. A prior
        # clarification answer (if any) is applied to query_plan's own
        # already-ranked candidates before anything is asked/executed again.
        cancel_clarification = bool(req.params.get("cancel_clarification"))
        clarification_selection = req.params.get("clarification_selection") or []
        if clarification_selection and not cancel_clarification:
            distinct_requested = extract_query_intent(question).get("aggregation_target") == "distinct_entity_count"
            _apply_clarification_overrides(
                query_plan, clarification_selection, req.source_id, req.user_id,
                distinct_requested=distinct_requested,
            )

        if not cancel_clarification:
            # EDP Day 1 — obvious same-object candidate-family duplicates
            # are collapsed silently inside this call; only genuine
            # cross-object or cross-source-system ambiguity reaches
            # clarification below (see _extract_ambiguous_terms_after_family_collapse).
            ambiguous_terms = _extract_ambiguous_terms_after_family_collapse(
                query_plan, req.source_id, req.user_id, question,
            )
            if ambiguous_terms:
                # Sprint 1.5 — Join-Aware Clarification: don't offer (or ask
                # about) combinations that can't actually be joined together.
                ambiguous_terms, no_valid_join = _filter_joinable_clarification(
                    ambiguous_terms, req.source_id, req.user_id,
                )
                if no_valid_join:
                    return {
                        "executed": False,
                        "reason": "no_valid_join",
                        "question": question,
                        "message": NO_VALID_JOIN_MESSAGE,
                    }
                # Never generate or execute SQL while a real ambiguity remains —
                # ask instead of guessing. Never triggers preparation: a
                # genuinely ambiguous question needs clarification, not more
                # metadata.
                #
                # Phase 2 — distinguish "these tied" from "this is my best
                # weak guess" in the reason value, so the rendered message
                # (explanation_builder._explain_clarification) never
                # describes a single below-threshold candidate as if it were
                # competing against an equally-confident rival. Any group
                # still tied (>=2 candidates) keeps the original wording —
                # only when EVERY group is a lone weak match does the
                # low_confidence_match wording apply. The option-picking/
                # resume machinery (_apply_clarification_overrides) is
                # identical either way.
                reason = (
                    "clarification_required"
                    if any(t.get("tied") for t in ambiguous_terms)
                    else "low_confidence_match"
                )
                return {
                    "executed": False,
                    "reason": reason,
                    "question": question,
                    "ambiguous_terms": ambiguous_terms,
                }
        return None

    outcome = _plan_with_autonomous_preparation(
        req.source_id, req.user_id, question,
        filters=req.params.get("filters"),
        allow_unconfirmed_pii=req.params.get("allow_unconfirmed_pii", False),
        on_plan_resolved=_on_plan_resolved,
    )
    if outcome["outcome"] == "unowned":
        return None  # unknown/unowned source — same contract as the raw-SQL branch
    if outcome["outcome"] == "early_exit":
        return outcome["result"]

    query_plan = outcome["query_plan"]
    sql_plan = outcome["sql_plan"]
    generated = outcome["generated"]
    preparation_trace = outcome["preparation_trace"]

    if outcome["outcome"] == "refused":
        return {
            "executed": False,
            "reason": "sql_generation_refused",
            "explanation": generated.get("explanation") or [],
            "warnings": generated.get("warnings") or sql_plan.get("warnings") or [],
            **({"preparation": preparation_trace} if preparation_trace else {}),
        }

    result, gov_warnings = execute_governed_query(
        req.source_id, req.user_id, generated["sql"], sql_plan,
        params=generated["parameters"]["values"],
        row_limit=req.params.get("row_limit"),
        timeout_s=req.params.get("timeout_s"),
        page=req.params.get("page", 1),
        page_size=req.params.get("page_size"),
        max_payload_bytes=req.params.get("max_payload_bytes"),
    )
    data = result.to_dict()
    if gov_warnings:
        data["warnings"] = [*(data.get("warnings") or []), *[w["message"] for w in gov_warnings]]
    data["generated_sql"] = generated["sql"]
    data["sql_generation_explanation"] = generated.get("explanation") or []
    data["business_plan"] = _build_business_plan(question, query_plan, sql_plan)
    if preparation_trace:
        data["metadata_preparation"] = preparation_trace
    return data


def _semantic_query_plan(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if req.source_id is None or req.user_id is None or not question:
        return None
    from core.semantic.planner import SemanticQueryPlanner

    plan = SemanticQueryPlanner().plan(
        req.source_id, req.user_id, question,
        filters=req.params.get("filters"),
    )
    return plan.to_dict() if plan is not None else None


def _execution_planner(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if not question:
        return None
    from core.execution.planner import ExecutionPlanner

    strategy = ExecutionPlanner().plan(question, req.source_id, req.user_id)
    return strategy.to_dict()


def _enterprise_answer(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if not question:
        return None
    from core.answering.answer_planner import AnswerPlanner
    from core.execution.planner import ExecutionPlanner
    from core.orchestrator.orchestrator import EnterpriseOrchestrator

    # Reuses the existing, unmodified intent-resolver-driven process() for
    # the Evidence Package input — this is "Execution Planner ↓ Answer
    # Generation Layer" using the same evidence the normal chat flow would
    # gather, not a second, differently-selected evidence-gathering pass.
    # Safe from re-entrancy: ENTERPRISE_ANSWER is never wired into
    # intent_resolver.py, so process() can never re-select this adapter.
    package = EnterpriseOrchestrator().process(req)
    strategy = ExecutionPlanner().plan(question, req.source_id, req.user_id)
    answer = AnswerPlanner().build(strategy, package)
    return {"execution_strategy": strategy.to_dict(), "enterprise_answer": answer.to_dict()}


_ADAPTERS: Dict[str, _Adapter] = {
    "dictionary":        _dictionary,
    "domain":            _domain,
    "entity":            _entity,
    "profiling":         _profiling,
    "governance":        _governance,
    "relationship":      _relationship,
    "knowledge_graph":   _knowledge_graph,
    "lineage":           _lineage,
    "semantic_layer":    _semantic_layer,
    "business_knowledge": _business_knowledge,
    "reports":           _reports,
    "workflow":          _workflow,
    "schema":            _schema,
    "search":            _search,
    "live_metadata":     _live_metadata,
    "live_query":        _live_query,
    "semantic_query_plan": _semantic_query_plan,
    "execution_planner":   _execution_planner,
    "enterprise_answer":   _enterprise_answer,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _primary_capability(descriptor: ServiceDescriptor) -> ServiceCapability:
    return descriptor.capabilities[0] if descriptor.capabilities else ServiceCapability.SEARCH_READ


def _evidence_confidence(data: Any) -> float:
    """Confidence that the evidence item contains useful data."""
    if data is None:
        return 0.0
    if isinstance(data, (list, dict)) and not data:
        return 0.5
    return 1.0


def _extract_governance_state(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        return data.get("governance_state") or data.get("state")
    return None


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder(IContextBuilder):
    """
    Collects read-only evidence from registered enterprise services and
    packages it into an EvidencePackage.

    No AI, no summaries, no explanation generation, no writes.
    Service failures are tracked inside the package — they never raise.
    """

    def build(
        self,
        request: OrchestratorRequest,
        intent: ResolvedIntent,
        services: List[ServiceDescriptor],
    ) -> EvidencePackage:
        evidence: List[EvidenceItem] = []
        call_records: List[ServiceCallRecord] = []
        errors: List[str] = []
        succeeded = 0

        for descriptor in services:
            adapter = _ADAPTERS.get(descriptor.service_id)
            if adapter is None:
                msg = f"No adapter registered for service '{descriptor.service_id}'"
                logger.warning("ContextBuilder: %s", msg)
                errors.append(msg)
                continue

            func_name = (
                descriptor.primary_functions[0]
                if descriptor.primary_functions
                else descriptor.service_id
            )
            called_at = datetime.utcnow()
            t0 = time.perf_counter()

            try:
                data = adapter(request)
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

                call_records.append(ServiceCallRecord(
                    service_id=descriptor.service_id,
                    function_name=func_name,
                    called_at=called_at,
                    duration_ms=elapsed_ms,
                    succeeded=True,
                ))

                evidence.append(EvidenceItem(
                    evidence_id=str(uuid.uuid4()),
                    source_service=descriptor.service_id,
                    source_function=func_name,
                    capability=_primary_capability(descriptor),
                    data=data,
                    timestamp=called_at,
                    confidence=_evidence_confidence(data),
                    governance_state=_extract_governance_state(data),
                ))
                succeeded += 1

            except Exception as exc:  # noqa: BLE001
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                error_msg = f"{descriptor.service_id}/{func_name}: {type(exc).__name__}: {exc}"
                logger.warning("ContextBuilder service call failed — %s", error_msg)

                call_records.append(ServiceCallRecord(
                    service_id=descriptor.service_id,
                    function_name=func_name,
                    called_at=called_at,
                    duration_ms=elapsed_ms,
                    succeeded=False,
                    error=error_msg,
                ))
                errors.append(error_msg)

        return EvidencePackage(
            request_id=request.request_id,
            query=request.query,
            intent=intent,
            evidence=evidence,
            service_calls=call_records,
            built_at=datetime.utcnow(),
            source_id=request.source_id,
            errors=errors,
            total_evidence_items=len(evidence),
            services_attempted=len(services),
            services_succeeded=succeeded,
        )
