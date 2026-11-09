"""
Executive Report persistence/viewing bug fix.

Root cause: data.report_service.save_report/get_report_by_id round-trip the
full ExecutiveReportData payload correctly — persistence was never broken.
The break was in the frontend Reports detail view, which only recognized
content.sections (the legacy dataset-report shape) and showed "No report
content available" for content.executive_report (the shape
_answer_executive_report_generation actually saves). That frontend fix is
covered by frontend/src/utils/reportContent.test.js and
frontend/src/components/ExecutiveReportView.content.test.jsx.

This file proves the backend half of the contract these frontend tests
assume: save_report/get_report_by_id preserve an ExecutiveReportData
payload byte-for-byte across a real SQLite round trip (not mocked), using
the same models.init_db() schema and per-test temp-db convention as
tests/test_phase9_query_planning.py.

Run:
    python -m pytest tests/test_report_persistence_roundtrip.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-report-persistence-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-report-persistence-salt-long-enough-123")

import pytest

import data.models as models
from core.reporting.report_models import ChartDataset, ChartSeries, ExecutiveReportData, KPICard, ReportTable


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def report_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "reports.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.report_service.get_connection", lambda p=db_path: _db_conn(p))
    return db_path


def _full_executive_report() -> ExecutiveReportData:
    report = ExecutiveReportData(title="Executive Report — the reference database", source_id=1)
    report.subtitle = "Live connected database"
    report.narrative = "Client growth remained steady across the reporting period."
    report.kpi_cards = [
        KPICard(id="total_clients", title="Total Clients", value=65000),
        KPICard(id="total_payments", title="Total Payments", value=2000000.00),
        KPICard(id="avg_payment", title="Average Payment Amount", value=164.42),
        KPICard(id="total_events", title="Total Events", value=7473),
    ]
    report.charts = [
        ChartDataset(id="monthly_new_clients", title="Monthly New Clients", chart_type="bar",
                     labels=("Jan", "Feb"), series=(ChartSeries(name="New Clients", data=(10, 20)),)),
    ]
    report.tables = [
        ReportTable(id="top_clients", title="Top Clients", columns=("Name", "Total"),
                    rows=({"Name": "Acme", "Total": 100},)),
    ]
    report.warnings = ["Report is based on data at time of generation — re-run for updated results."]
    report.data_quality_notes = ["3 columns had partial null coverage."]
    report.technical_diagnostics = [{"table": "clients", "issue": "null_rate", "value": 0.02}]
    return report


class TestExecutiveReportRoundTrip:
    def test_full_payload_preserved_across_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_dict = report.to_dict()

        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report_dict}, status="completed", dataset_id=None,
        )

        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved is not None
        assert retrieved["content"]["executive_report"] == report_dict

    def test_kpi_cards_survive_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        cards = retrieved["content"]["executive_report"]["kpi_cards"]
        assert [c["title"] for c in cards] == [
            "Total Clients", "Total Payments", "Average Payment Amount", "Total Events",
        ]
        assert cards[0]["value"] == 65000
        assert cards[1]["value"] == 2000000.00

    def test_chart_data_survives_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        charts = retrieved["content"]["executive_report"]["charts"]
        assert charts[0]["title"] == "Monthly New Clients"
        assert charts[0]["series"][0]["data"] == [10, 20]

    def test_narrative_survives_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["content"]["executive_report"]["narrative"] == (
            "Client growth remained steady across the reporting period."
        )

    def test_data_quality_notes_survive_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["content"]["executive_report"]["data_quality_notes"] == [
            "3 columns had partial null coverage.",
        ]

    def test_technical_diagnostics_survive_save_and_retrieve(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["content"]["executive_report"]["technical_diagnostics"] == [
            {"table": "clients", "issue": "null_rate", "value": 0.02},
        ]

    def test_task_type_persisted_for_type_detection(self, report_db):
        """Report type must be recoverable from persisted metadata (task_type),
        never guessed from the title — the frontend's typeLabel()/
        resolveReportContentView() both key off this."""
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report.title = "My dataset report"  # deliberately misleading title
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["task_type"] == "generate_executive_report"


class TestLegacyDatasetReportUnaffected:
    """Existing dataset-report save/retrieve path (content.sections) must be
    completely untouched by the Executive Report fix."""

    def test_legacy_sections_report_round_trips_unchanged(self, report_db):
        from data.report_service import save_report, get_report_by_id

        content = {
            "sections": [
                {"type": "text", "heading": "Overview", "items": ["A dataset of 100 rows."]},
            ],
            "report_plan": {"steps": ["profile", "summarize"]},
        }
        report_id = save_report(
            user_id="user-1", title="Sales Dataset Report", task_type="generate_report",
            content=content, dataset_id=42,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["content"] == content
        assert retrieved["content"].get("executive_report") is None


class TestReportOwnership:
    def test_user_cannot_retrieve_another_users_report(self, report_db):
        from data.report_service import save_report, get_report_by_id

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )
        assert get_report_by_id(report_id, "user-2") is None
        assert get_report_by_id(report_id, "user-1") is not None


class TestOpeningSavedReportDoesNotRegenerate:
    """View Report must display the SAVED report — it must never re-run the
    executive-report generation pipeline or live KPI SQL just to open an
    already-saved report."""

    def test_get_report_by_id_never_calls_generate_executive_report(self, report_db, monkeypatch):
        from unittest.mock import MagicMock
        import core.reporting.executive_report_service as exec_report_service
        from data.report_service import save_report, get_report_by_id

        spy = MagicMock(side_effect=AssertionError("generate_executive_report must not run on retrieval"))
        monkeypatch.setattr(exec_report_service, "generate_executive_report", spy)

        report = _full_executive_report()
        report_id = save_report(
            user_id="user-1", title=report.title, task_type="generate_executive_report",
            content={"executive_report": report.to_dict()}, dataset_id=None,
        )

        retrieved = get_report_by_id(report_id, "user-1")

        spy.assert_not_called()
        assert retrieved["content"]["executive_report"]["kpi_cards"][0]["value"] == 65000


class TestLegacyContentGracefulEmptyState:
    def test_report_with_neither_shape_has_no_recognizable_content(self, report_db):
        """A pre-existing/malformed row with neither executive_report nor
        sections must not crash retrieval — the frontend's
        resolveReportContentView() maps this to the 'empty' state rather
        than throwing."""
        from data.report_service import save_report, get_report_by_id

        report_id = save_report(
            user_id="user-1", title="Old stub report", task_type="generate_report",
            content={}, dataset_id=None,
        )
        retrieved = get_report_by_id(report_id, "user-1")
        assert retrieved["content"] == {}
