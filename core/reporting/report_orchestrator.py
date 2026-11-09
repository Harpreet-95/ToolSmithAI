"""
Executive Report Query Foundation milestone, Part 9 — the multi-KPI report
orchestrator.

Responsibilities (and ONLY these):
  1. receive a bounded list of KPIQuerySpecs (core.reporting.report_planner
     builds these from evidence; a caller may also hand-author them)
  2. plan each KPI via core.orchestrator.ai_physical_planning.
     attempt_structured_kpi_plan — the SAME AI physical planner/validator
     ordinary SQL Q&A uses, never a second SQL planner
  3. execute only plans that validated, via data.query_execution_service.
     execute_governed_query — the SAME governed execution path (PII
     masking, row caps, audit log) ordinary SQL Q&A uses
  4. collect structured results, perform deterministic period-over-period
     comparisons (core.reporting.comparison — never LLM arithmetic)
  5. build core.reporting.report_models.ExecutiveReportData

It NEVER generates raw SQL itself, NEVER chooses an unverified table/join
(every table/join comes from attempt_structured_kpi_plan's own validated
plan), and NEVER falls back to the legacy semantic planner to rescue a KPI
that failed AI physical planning — a KPI that cannot be safely planned is
skipped, recorded in `warnings`/`trace`, and the report continues with
whatever else validated (Part 16).
"""
from __future__ import annotations

import time
from dataclasses import replace

from core.reporting.chart_metadata import build_visual_from_result
from core.reporting.comparison import build_comparison_plan, compute_comparison
from core.reporting.kpi_spec import KPIQuerySpec
from core.reporting.report_models import ChartDataset, ExecutiveReportData, KPICard, KPITrace, ReportTable

_COMPARISON_LABELS = {
    "month_over_month": "vs last month",
    "quarter_over_quarter": "vs last quarter",
    "year_over_year": "vs last year",
    "custom": "vs comparison period",
}


def _execute_single_spec(
    source_id: int, user_id: str, spec: KPIQuerySpec, *, session=None, allow_unconfirmed_pii: bool = False,
) -> tuple[list[KPICard | ChartDataset | ReportTable], list[KPITrace], list[str], float, float, list]:
    """Plans and executes ONE (non-comparison) KPIQuerySpec. Returns
    (visuals, traces, warnings, planning_ms, execution_ms, dq_observations).
    A spec that fails planning produces zero visuals and one
    "skipped_unsafe" trace entry — never a guessed/partial substitute
    metric."""
    from core.orchestrator.ai_physical_planning import attempt_structured_kpi_plan

    t0 = time.monotonic()
    plan_result = attempt_structured_kpi_plan(
        source_id, user_id, session=session, allow_unconfirmed_pii=allow_unconfirmed_pii, **spec.as_plan_kwargs(),
    )
    planning_ms = (time.monotonic() - t0) * 1000

    visuals: list[KPICard | ChartDataset | ReportTable] = []
    traces: list[KPITrace] = []
    warnings: list[str] = []
    execution_ms = 0.0
    dq_observations: list = []

    if not plan_result["valid"]:
        traces.append(KPITrace(
            kpi_id=spec.id, business_question=spec.business_question,
            selected_tables=tuple(plan_result.get("selected_table_fqns") or []),
            ai_plan_valid=False, legacy_fallback_used=False, sql=None,
            execution_status="skipped_unsafe", skip_reason=plan_result.get("reason"),
        ))
        warnings.append(f"KPI '{spec.title}' skipped: {plan_result.get('reason')}")
        return visuals, traces, warnings, planning_ms, execution_ms, dq_observations

    for excluded in plan_result.get("excluded_measures", []):
        warnings.append(f"KPI '{spec.title}' measure '{excluded['term']}' excluded: {excluded['reason']}")

    from data.query_execution_service import execute_governed_query

    plans = plan_result["plans"]
    multi = len(plans) > 1
    for i, p in enumerate(plans):
        visual_id = spec.id if not multi else f"{spec.id}_{p['aggregation'].lower()}_{i}"
        sql = p["generated"].get("sql")
        te0 = time.monotonic()
        query_result, _gov_warnings = execute_governed_query(
            source_id, user_id, sql, p["sql_plan"],
            params=(p["generated"].get("parameters") or {}).get("values"),
            execution_kind="report_kpi",
        )
        execution_ms += (time.monotonic() - te0) * 1000

        status_value = getattr(query_result.status, "value", query_result.status)
        if status_value != "success":
            traces.append(KPITrace(
                kpi_id=visual_id, business_question=spec.business_question,
                selected_tables=tuple(plan_result["selected_table_fqns"]),
                ai_plan_valid=True, legacy_fallback_used=False, sql=sql,
                execution_status="failed", skip_reason=query_result.error,
            ))
            warnings.append(f"KPI '{spec.title}' execution failed: {query_result.error}")
            continue

        visual = build_visual_from_result(
            visual_id, spec.title, p["sql_plan"]["select"], query_result.rows, ai_hint=spec.visualization_hint,
        )

        # FINAL CORRECTNESS CLEANUP MILESTONE, Parts 1-3/6 — post-execution
        # title semantic-consistency check, against the REAL resolved
        # sql_plan (not the AI's own proposal text) now that it's known.
        # Never changes the numeric value/SQL, only the displayed title —
        # see report_planner.find_unsupported_title_modifiers's own
        # docstring for why this must run here rather than at proposal time.
        from core.reporting.report_planner import find_unsupported_title_modifiers, repair_unsupported_title

        unsupported = find_unsupported_title_modifiers(visual.title, p["sql_plan"])
        if unsupported:
            fallback = f"Total {spec.entity_name}".strip() or visual.title
            corrected_title = repair_unsupported_title(visual.title, unsupported, fallback)
            warnings.append(
                f"KPI/chart title '{visual.title}' renamed to '{corrected_title}': its own "
                f"executed plan has no filter/column evidence for "
                f"{'the word' if len(unsupported) == 1 else 'the words'} "
                f"{', '.join(repr(w) for w in unsupported)} — the original title claimed a "
                "condition the query does not actually prove.",
            )
            visual = replace(visual, title=corrected_title)

        visuals.append(visual)
        traces.append(KPITrace(
            kpi_id=visual_id, business_question=spec.business_question,
            selected_tables=tuple(plan_result["selected_table_fqns"]),
            ai_plan_valid=True, legacy_fallback_used=False, sql=sql, execution_status="executed",
        ))

        # FINAL CORRECTNESS CLEANUP MILESTONE, Part 9 — reuses core.answering.
        # data_quality_observations verbatim (same bounded-to-this-answer's-
        # own-columns discipline as the live Q&A wiring in core.orchestrator.
        # agent), adapted from p["sql_plan"]'s own select/where/group_by —
        # already in the exact shape that module's business_plan adapter
        # reads, so no new framework/duplicate logic is needed. Best-effort:
        # never lets a data-quality lookup failure break a valid KPI/chart.
        try:
            from core.answering.data_quality_observations import (
                business_columns_from_plan, detect_data_quality_observations,
            )
            from data.business_knowledge_service import get_table_business_contexts_batch

            pseudo_business_plan = {
                "select": p["sql_plan"].get("select") or [],
                "where": p["sql_plan"].get("where") or [],
                "group_by": p["sql_plan"].get("group_by") or [],
                "dimension_labels": {},
            }
            dq_columns = business_columns_from_plan(pseudo_business_plan)
            dq_tables = sorted({ref.table_fqn for ref in dq_columns})
            if dq_tables:
                dq_table_contexts = get_table_business_contexts_batch(source_id, user_id, dq_tables, session=session)
                dq_observations.extend(detect_data_quality_observations(dq_table_contexts, dq_columns))
        except Exception:  # noqa: BLE001
            pass

    return visuals, traces, warnings, planning_ms, execution_ms, dq_observations


def execute_kpi_specs(
    source_id: int, user_id: str, title: str, kpi_specs: list[KPIQuerySpec], *,
    subtitle: str = "", session=None, allow_unconfirmed_pii: bool = False,
) -> ExecutiveReportData:
    """Plans, validates, and executes every KPIQuerySpec, returning a fully
    populated ExecutiveReportData (narrative field left None — Part 12's
    caller sets it separately, after this structured data exists)."""
    report = ExecutiveReportData(title=title, source_id=source_id, subtitle=subtitle)
    total_planning_ms = 0.0
    total_execution_ms = 0.0
    all_dq_observations: list = []
    t_start = time.monotonic()

    for spec in kpi_specs:
        # A calendar-grain trend groups into multiple periods (a chart
        # series); a comparison collapses to ONE scalar value per period
        # (current vs previous). These are structurally incompatible, not a
        # business-judgment call: a spec carrying both would always fail the
        # cur_card/prev_card scalar check below and silently lose its chart
        # (core.reporting.report_planner is the primary fix — it never lets
        # an AI proposal keep both — this is defense-in-depth for any other
        # caller, including a hand-authored KPIQuerySpec per this module's
        # own docstring).
        if spec.comparison is not None and spec.calendar_grain is not None:
            report.warnings.append(
                f"KPI '{spec.title}': comparison ignored — a calendar-grain trend renders "
                "as a chart series, not a single comparison scalar.",
            )
            spec = replace(spec, comparison=None)

        if spec.comparison is not None:
            comparison_plan = build_comparison_plan(spec)

            cur_visuals, cur_traces, cur_warnings, p_ms, e_ms, cur_dq = _execute_single_spec(
                source_id, user_id, comparison_plan.current_spec,
                session=session, allow_unconfirmed_pii=allow_unconfirmed_pii,
            )
            total_planning_ms += p_ms
            total_execution_ms += e_ms
            prev_visuals, prev_traces, prev_warnings, p_ms2, e_ms2, prev_dq = _execute_single_spec(
                source_id, user_id, comparison_plan.previous_spec,
                session=session, allow_unconfirmed_pii=allow_unconfirmed_pii,
            )
            total_planning_ms += p_ms2
            total_execution_ms += e_ms2

            report.trace.extend(cur_traces)
            report.trace.extend(prev_traces)
            report.warnings.extend(cur_warnings)
            report.warnings.extend(prev_warnings)
            all_dq_observations.extend(cur_dq)
            all_dq_observations.extend(prev_dq)

            cur_card = cur_visuals[0] if len(cur_visuals) == 1 and isinstance(cur_visuals[0], KPICard) else None
            prev_card = prev_visuals[0] if len(prev_visuals) == 1 and isinstance(prev_visuals[0], KPICard) else None
            if cur_card is None or prev_card is None:
                report.warnings.append(
                    f"KPI '{spec.title}' comparison skipped: incompatible result grain "
                    f"(expected a single scalar value for both periods).",
                )
                if cur_card is not None:
                    report.kpi_cards.append(cur_card)
                continue

            comparison_result = compute_comparison(
                spec.title, cur_card.value, prev_card.value,
                comparison_plan.current_period, comparison_plan.previous_period,
            )
            if comparison_result.warning:
                report.warnings.append(f"KPI '{spec.title}': {comparison_result.warning}")
            report.kpi_cards.append(KPICard(
                id=spec.id, title=spec.title, value=comparison_result.current_value,
                previous_value=comparison_result.previous_value,
                absolute_change=comparison_result.absolute_change,
                percent_change=comparison_result.percentage_change,
                direction=comparison_result.direction,
                comparison_label=_COMPARISON_LABELS.get(comparison_plan.comparison_type, "vs comparison period"),
            ))
            continue

        visuals, traces, warnings, p_ms, e_ms, dq = _execute_single_spec(
            source_id, user_id, spec, session=session, allow_unconfirmed_pii=allow_unconfirmed_pii,
        )
        total_planning_ms += p_ms
        total_execution_ms += e_ms
        report.trace.extend(traces)
        report.warnings.extend(warnings)
        all_dq_observations.extend(dq)
        for visual in visuals:
            if isinstance(visual, KPICard):
                report.kpi_cards.append(visual)
            elif isinstance(visual, ChartDataset):
                report.charts.append(visual)
            elif isinstance(visual, ReportTable):
                report.tables.append(visual)

    # FINAL CORRECTNESS CLEANUP MILESTONE, Part 9 — dedupe across every KPI/
    # chart before surfacing (the same underlying table/column commonly
    # backs more than one KPI in one report; Part 13's "do not overwarn"
    # discipline applies here exactly as it does for live Q&A).
    from core.answering.data_quality_observations import executive_data_notes, technical_diagnostics

    seen_keys: set[tuple] = set()
    deduped_observations = []
    for obs in all_dq_observations:
        key = (obs.issue_type, obs.table_fqn, obs.column_name)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_observations.append(obs)
    report.data_quality_notes = executive_data_notes(deduped_observations)
    report.technical_diagnostics = technical_diagnostics(deduped_observations)

    report.timings_ms["planning_ms"] = round(total_planning_ms, 1)
    report.timings_ms["execution_ms"] = round(total_execution_ms, 1)
    report.timings_ms["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    return report
