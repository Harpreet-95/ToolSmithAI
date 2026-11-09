"""
Tests for core.reporting.executive_report_service.generate_executive_report —
Executive Report Query Foundation milestone, top-level wiring + Part 18
timing instrumentation.

Run:
    python -m pytest tests/test_executive_report_service.py -v
"""
from __future__ import annotations

from unittest.mock import patch

from core.reporting.executive_report_service import generate_executive_report
from core.reporting.kpi_spec import KPIMeasure, KPIQuerySpec
from core.reporting.report_models import ExecutiveReportData, KPICard


def test_no_kpi_specs_returns_empty_report_with_warning():
    with patch(
        "core.reporting.executive_report_service.plan_executive_report_kpis",
        return_value=([], ["No approved metric/dimension/date columns were found for this source."]),
    ):
        report = generate_executive_report(1, "u1", source_display_name="Test DB")

    assert report.kpi_cards == []
    assert "kpi_planning_ms" in report.timings_ms
    assert "total_ms" in report.timings_ms
    assert any("No approved" in w for w in report.warnings)


def test_full_pipeline_wiring_and_narrative():
    spec = KPIQuerySpec(
        id="k1", title="Student Count", business_question="q", entity_name="student",
        measures=(KPIMeasure(term="student", aggregation="COUNT"),),
    )
    fake_report = ExecutiveReportData(title="x", source_id=1)
    fake_report.kpi_cards.append(KPICard(id="k1", title="Student Count", value=250))

    with (
        patch(
            "core.reporting.executive_report_service.plan_executive_report_kpis",
            return_value=([spec], []),
        ),
        patch(
            "core.reporting.executive_report_service.execute_kpi_specs",
            return_value=fake_report,
        ),
        patch(
            "core.reporting.narrative.generate_executive_narrative",
            return_value={"summary": "Student count is 250.", "highlights": ["250 students total."]},
        ),
    ):
        report = generate_executive_report(1, "u1", source_display_name="EXAMPLECO")

    assert report.kpi_cards[0].value == 250
    assert "Student count is 250." in report.narrative
    assert "250 students total." in report.narrative
    assert set(report.timings_ms.keys()) >= {"kpi_planning_ms", "narrative_ms", "total_ms"}


def test_narrative_failure_does_not_break_report():
    spec = KPIQuerySpec(
        id="k1", title="Student Count", business_question="q", entity_name="student",
        measures=(KPIMeasure(term="student", aggregation="COUNT"),),
    )
    fake_report = ExecutiveReportData(title="x", source_id=1)
    fake_report.kpi_cards.append(KPICard(id="k1", title="Student Count", value=250))

    with (
        patch(
            "core.reporting.executive_report_service.plan_executive_report_kpis",
            return_value=([spec], []),
        ),
        patch(
            "core.reporting.executive_report_service.execute_kpi_specs",
            return_value=fake_report,
        ),
        patch("core.reporting.narrative.generate_executive_narrative", return_value=None),
    ):
        report = generate_executive_report(1, "u1")

    assert report.narrative is None
    assert report.kpi_cards[0].value == 250
