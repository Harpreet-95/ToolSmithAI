"""
Reconnect Reliability milestone, Part 14 — Q&A pipeline observability.

Prior to this milestone, no structured record of a live-SQL question's
pipeline (candidates considered, join reasoning, generated-SQL validation
status, warnings) was persisted anywhere — it was all computed in-memory and
discarded once an answer was returned, making the exact question this
milestone started from ("why did this question behave differently after
reconnect?") undiagnosable after the fact without re-running it live.

build_pipeline_trace() is a pure function: given the SQL plan
data.sql_planning_service.build_sql_plan() already produced for a question
(no new reads, nothing re-derived), it extracts a compact, storage-ready
summary. Never includes raw SQL text, parameter values, or row data — this
mirrors data.audit.py's own "never log raw SQL/param values/PII" invariant;
callers that want the actual SQL text pass it in separately (already
encrypted the same way data.audit.py encrypts other sensitive fields, via
core.security.encryption.encrypt) rather than this module handling it.
"""
from __future__ import annotations

from typing import Any


def build_pipeline_trace(sql_plan: dict, *, ai_used: dict | None = None) -> dict:
    """
    sql_plan: the dict returned by data.sql_planning_service.build_sql_plan().
    ai_used: optional caller-supplied flags for AI involvement at stages this
        module cannot infer from sql_plan alone (e.g. {"question_interpreter":
        bool, "candidate_adjudicator": bool, "implicit_ranking_ai": bool}).
        Omitted keys mean "unknown", not "false" — never assume AI was NOT
        used just because a caller didn't report it.

    Returns a JSON-serializable dict with no raw SQL text, parameter values,
    or row data anywhere in it.
    """
    sql_plan = sql_plan or {}
    validation = sql_plan.get("validation") or {}
    warnings = sql_plan.get("warnings") or []
    aggregation_plan = sql_plan.get("aggregation_plan")

    tables_referenced: list[str] = []
    from_clause = sql_plan.get("from")
    if from_clause and from_clause.get("table_fqn"):
        tables_referenced.append(from_clause["table_fqn"])
    for j in sql_plan.get("joins") or []:
        fqn = j.get("table_fqn")
        if fqn and fqn not in tables_referenced:
            tables_referenced.append(fqn)

    join_summary = [
        {
            "table_fqn": j.get("table_fqn"),
            "join_type": j.get("join_type"),
            "fanout_risk": j.get("fanout_risk"),
        }
        for j in (sql_plan.get("joins") or [])
    ]

    intent = sql_plan.get("intent") or {}

    return {
        "question_understanding": {
            # sql_plan["intent"] is data.query_planning_service._build_intent's
            # output — already the one structure every downstream consumer
            # (SQL generation, answer explanation) trusts as "the resolved
            # intent," so this is a passthrough, not a second/parallel
            # structure. See data/query_planning_service.py's own
            # evidence-hierarchy module docstring for how each of these
            # fields gets resolved.
            "type": intent.get("type"),
            "aggregation": intent.get("aggregation"),
            "aggregation_target": intent.get("aggregation_target"),
            "order": intent.get("order"),
            "status_value": intent.get("status_value"),
            "status_negated": intent.get("status_negated"),
        },
        "candidate_retrieval": {
            # semantic_context is plan_business_query's own resolved-concept
            # evidence, passed through build_sql_plan unchanged — the closest
            # thing to "candidates considered" already computed upstream.
            "concepts_considered": [
                {"term": c.get("term"), "selected_table_fqn": (c.get("selected") or {}).get("table_fqn")}
                for c in (sql_plan.get("semantic_context") or [])
            ],
        },
        "relationship_resolution": {
            "tables_referenced": tables_referenced,
            "joins": join_summary,
        },
        "ai_planning": {
            "question_interpreter_used": (ai_used or {}).get("question_interpreter"),
            "candidate_adjudicator_used": (ai_used or {}).get("candidate_adjudicator"),
            "implicit_ranking_ai_used": (ai_used or {}).get("implicit_ranking_ai"),
            # RETIRE-LEGACY-FALLBACK MILESTONE, Part 17 — the fields that
            # specific milestone's observability requirement names directly.
            # legacy_semantic_planner MUST be False for every normal Data
            # Analyst Agent Q&A request; see core.orchestrator.agent's
            # AgentState.legacy_semantic_planner_competing_fallback for the
            # exact definition (True only if legacy ever ran to resolve
            # semantic ambiguity AI itself was consulted on and could not
            # safely resolve -- never for the operator kill-switch or a
            # deliberate deterministic-grounding skip, both of which are
            # transparently reported via the other fields here instead of
            # being folded into this one).
            "physical_planner_attempted": (ai_used or {}).get("physical_planner_attempted"),
            "physical_planner_valid": (ai_used or {}).get("physical_planner_valid"),
            "ai_interpretation_deliberately_skipped": (ai_used or {}).get("ai_interpretation_deliberately_skipped"),
            "legacy_semantic_planner": (ai_used or {}).get("legacy_semantic_planner"),
            "legacy_fallback_reason": (ai_used or {}).get("legacy_fallback_reason"),
        },
        "sql_generation": {
            # Confirmed elsewhere in this codebase (data.sql_generation_service
            # module docstring): SQL text generation is always deterministic
            # template rendering from a validated plan, never an LLM call.
            "method": "template",
            "aggregation_plan": aggregation_plan,
        },
        "validation": {
            "valid": validation.get("valid"),
            "checks": validation.get("checks"),
            "blocking_reasons": validation.get("blocking_reasons"),
        },
        "warnings": [{"type": w.get("type"), "severity": w.get("severity")} for w in warnings],
        "explanation": sql_plan.get("explanation") or [],
    }
