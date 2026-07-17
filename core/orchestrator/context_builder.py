from __future__ import annotations

import itertools
import logging
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

def _extract_ambiguous_terms(query_plan: dict) -> list[dict]:
    """Normalize query_plan's unresolved, ambiguous measures/dimensions into
    a clarification-ready shape. A term only qualifies when
    query_planning_service itself flagged it ambiguous (not just missing)
    and left >=2 ranked candidates — never invents ambiguity on its own.

    Mirrors sql_planning_service.py's own existing leniency exactly: when at
    least one OTHER measure/dimension already resolved, an extra ambiguous
    term is skipped with a warning rather than blocking the whole question
    (build_sql_plan's "if select: skip unresolved ones" rule) — so this only
    ever intercepts the case that would otherwise become a hard refusal
    (nothing in the plan resolved at all), never a question that already has
    a confident answer alongside one ambiguous extra word.
    """
    all_entries = (query_plan.get("measures") or []) + (query_plan.get("dimensions") or [])
    if any(entry.get("selected") for entry in all_entries):
        return []

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
            if is_ambiguous and len(candidates) >= 2:
                ambiguous.append({"term": entry.get("term"), "kind": kind, "candidates": candidates})
    return ambiguous


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

    for kind, entries in (
        ("measure", query_plan.get("measures") or []),
        ("dimension", query_plan.get("dimensions") or []),
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
        filtered_terms.append({**term, "candidates": [c for _, c in kept]})

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
    }


def _live_query(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from core.live.query_engine import LiveQueryEngine

    sql = req.params.get("sql")
    if sql:
        # Trusted caller already has exact SQL to run (Phase 7 bypass) —
        # unchanged behavior.
        result = LiveQueryEngine().execute(
            req.source_id, req.user_id, sql,
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
        result = LiveQueryEngine().execute(
            req.source_id, req.user_id, sql,
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

    from core.semantic.concept_resolver import extract_terms, extract_query_intent
    from data.query_planning_service import plan_business_query
    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import generate_sql

    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(req.source_id, req.user_id, {
        "question": question,
        "concepts": concepts,
        "measures": measures,
        "dimensions": dimensions,
        "filters": req.params.get("filters") or [],
    })
    if query_plan is None:
        return None  # unknown/unowned source — same contract as the raw-SQL branch

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
        ambiguous_terms = _extract_ambiguous_terms(query_plan)
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
            # ask instead of guessing.
            return {
                "executed": False,
                "reason": "clarification_required",
                "question": question,
                "ambiguous_terms": ambiguous_terms,
            }

    sql_plan = build_sql_plan(
        req.source_id, req.user_id, query_plan,
        allow_unconfirmed_pii=req.params.get("allow_unconfirmed_pii", False),
    )
    generated = generate_sql(
        req.source_id, req.user_id, sql_plan,
        dialect=detect_dialect(req.source_id),
    )
    if not generated.get("sql"):
        return {
            "executed": False,
            "reason": "sql_generation_refused",
            "explanation": generated.get("explanation") or [],
            "warnings": generated.get("warnings") or sql_plan.get("warnings") or [],
        }

    result = LiveQueryEngine().execute(
        req.source_id, req.user_id, generated["sql"],
        params=generated["parameters"]["values"],
        row_limit=req.params.get("row_limit"),
        timeout_s=req.params.get("timeout_s"),
        page=req.params.get("page", 1),
        page_size=req.params.get("page_size"),
        max_payload_bytes=req.params.get("max_payload_bytes"),
    )
    data = result.to_dict()
    data["generated_sql"] = generated["sql"]
    data["sql_generation_explanation"] = generated.get("explanation") or []
    data["business_plan"] = _build_business_plan(question, query_plan, sql_plan)
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
