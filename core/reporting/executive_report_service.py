"""
Executive Report Query Foundation milestone — single top-level entry point
tying Parts 7-12 together:

    report intent -> evidence -> KPIQuerySpecs (report_planner)
    -> plan/validate/execute each KPI (report_orchestrator)
    -> deterministic comparisons (comparison, inside the orchestrator)
    -> ExecutiveReportData (report_models)
    -> executive narrative over the STRUCTURED result only (narrative)

Part 18 — instrumentation: records planning/evidence time, per-KPI
plan+execute time (already summed by the orchestrator), and narrative time
separately in report.timings_ms, so a slow stage is visible without a
profiler.
"""
from __future__ import annotations

import time

from core.reporting.report_models import ExecutiveReportData
from core.reporting.report_orchestrator import execute_kpi_specs
from core.reporting.report_planner import plan_executive_report_kpis


def generate_executive_report(
    source_id: int, user_id: str, *, title: str | None = None, source_display_name: str | None = None,
    max_kpis: int = 6,
) -> ExecutiveReportData:
    t_start = time.monotonic()

    t0 = time.monotonic()
    kpi_specs, planning_warnings = plan_executive_report_kpis(source_id, user_id, max_kpis=max_kpis)
    kpi_planning_ms = (time.monotonic() - t0) * 1000

    report_title = title or f"Executive Report — {source_display_name or f'Source {source_id}'}"

    if not kpi_specs:
        report = ExecutiveReportData(title=report_title, source_id=source_id)
        report.warnings.extend(planning_warnings)
        report.timings_ms["kpi_planning_ms"] = round(kpi_planning_ms, 1)
        report.timings_ms["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)
        return report

    report = execute_kpi_specs(source_id, user_id, report_title, kpi_specs)
    report.warnings = planning_warnings + report.warnings

    t1 = time.monotonic()
    from core.reporting.narrative import generate_executive_narrative

    narrative = generate_executive_narrative(report)
    narrative_ms = (time.monotonic() - t1) * 1000
    if narrative:
        report.narrative = narrative["summary"]
        if narrative.get("highlights"):
            report.narrative += "\n" + "\n".join(f"- {h}" for h in narrative["highlights"])

    report.timings_ms["kpi_planning_ms"] = round(kpi_planning_ms, 1)
    report.timings_ms["narrative_ms"] = round(narrative_ms, 1)
    report.timings_ms["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    return report
