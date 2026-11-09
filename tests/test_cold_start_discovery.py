"""
Executive Report V3 — unit-level coverage for core.analysis.cold_start_discovery,
independent of the larger end-to-end fixture in
tests/test_executive_report_cold_start_fixture.py.

Covers the ranking/selection/warm-awareness logic in isolation by mocking
data.profiling_service.list_table_relevance_signals and the four existing
targeted-preparation primitives it orchestrates
(prepare_selected_tables / generate_and_save_dictionary /
generate_domain_assignments / generate_entity_assignments) — this file
proves the ORCHESTRATION contract (budgets, ranking, warm/partial/cold
status, gap-filling, best-effort failure handling), not the underlying
metadata generation itself (already covered by their own test files).

Run from the project root:
    python -m pytest tests/test_cold_start_discovery.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.analysis.cold_start_discovery import (
    MAX_PREPARED_TABLES,
    STATUS_COLD,
    STATUS_PARTIALLY_LEARNED,
    STATUS_SKIPPED_NO_SCHEMA,
    STATUS_SKIPPED_NOT_OWNED,
    STATUS_WARM,
    _relevance_score,
    ensure_business_evidence,
)


def _row(table_fqn, **overrides):
    base = {
        "table_fqn": table_fqn,
        "table_name": table_fqn.split(".")[-1],
        "schema_name": "main",
        "table_type": "TABLE",
        "estimated_row_count": 100,
        "exact_row_count": None,
        "has_date_column": 0,
        "column_count": 3,
        "pk_column_count": 1,
        "fk_count": 0,
        "referenced_by_count": 0,
        "is_junction_table": 0,
        "is_root_table": 1,
        "table_class": "Unknown",
        "has_dictionary": 0,
        "has_domain": 0,
    }
    base.update(overrides)
    return base


class TestOwnershipAndSchemaGuards:
    def test_not_owned_returns_skipped(self):
        with patch("data.profiling_service.list_table_relevance_signals", return_value=None):
            trace = ensure_business_evidence(1, "u1")
        assert trace.status == STATUS_SKIPPED_NOT_OWNED

    def test_no_schema_returns_skipped(self):
        with patch("data.profiling_service.list_table_relevance_signals", return_value=[]):
            trace = ensure_business_evidence(1, "u1")
        assert trace.status == STATUS_SKIPPED_NO_SCHEMA


class TestRelevanceScore:
    def test_master_table_scores_higher_than_reference(self):
        master_score, master_reasons = _relevance_score(_row("main.Students", table_class="Master"))
        ref_score, _ = _relevance_score(_row("main.RefCodes", table_class="Reference"))
        assert master_score > ref_score
        assert any("Master" in r for r in master_reasons)

    def test_audit_table_scores_negative(self):
        score, reasons = _relevance_score(_row("main.AuditLog", table_class="Audit"))
        assert score < 0
        assert any("Audit" in r for r in reasons)

    def test_staging_class_deprioritized_even_without_a_matching_name_prefix(self):
        """Regression test for a real bug found by the Executive Report V3
        acceptance fixture: table_class == 'Staging' used to get NO score
        penalty at all (unlike Audit's explicit -25) — a table the
        classifier correctly called Staging via a name prefix outside this
        module's own narrower _DEPRIORITY_PREFIXES list (e.g. 'stage_',
        not in the old tmp_/temp_/stg_/bak_/old_/test_ set) scored
        identically to a real, unclassified business table. Uses a name
        that deliberately does NOT match _DEPRIORITY_PREFIXES, so this only
        passes if the table_class branch itself applies the penalty."""
        baseline, _ = _relevance_score(_row("main.Plain", table_class="Unknown"))
        staging, reasons = _relevance_score(_row("main.stage_LoadBuffer", table_class="Staging"))
        assert staging < baseline
        assert any("Staging" in r for r in reasons)

    def test_staging_name_prefix_deprioritized(self):
        baseline, _ = _relevance_score(_row("main.Widgets", table_class="Unknown", pk_column_count=1))
        staged, reasons = _relevance_score(_row("tmp_Widgets", table_class="Unknown", pk_column_count=1))
        assert staged < baseline
        assert any("staging/temp" in r for r in reasons)

    def test_migration_history_table_deprioritized(self):
        score, reasons = _relevance_score(_row("main.__MigrationHistory", table_class="Unknown"))
        assert score < 0
        assert any("migration" in r.lower() for r in reasons)

    def test_connectivity_and_date_and_fk_signals_are_additive(self):
        bare_score, _ = _relevance_score(_row("main.Bare", pk_column_count=0))
        rich_score, reasons = _relevance_score(_row(
            "main.Rich", pk_column_count=1, fk_count=2, has_date_column=1, referenced_by_count=6,
        ))
        assert rich_score > bare_score
        assert len(reasons) >= 4

    def test_junction_table_penalized(self):
        plain_score, _ = _relevance_score(_row("main.StudentCourseLink", is_junction_table=0))
        junction_score, reasons = _relevance_score(_row("main.StudentCourseLink", is_junction_table=1))
        assert junction_score < plain_score
        assert any("junction" in r for r in reasons)


class TestWarmColdPartial:
    def test_warm_when_existing_evidence_meets_budget(self):
        signals = [_row(f"main.T{i}", has_dictionary=1, has_domain=1) for i in range(MAX_PREPARED_TABLES)]
        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables") as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary") as mock_dict, \
             patch("data.domain_service.generate_domain_assignments") as mock_domain, \
             patch("data.entity_service.generate_entity_assignments") as mock_entity:
            trace = ensure_business_evidence(1, "u1")

        assert trace.status == STATUS_WARM
        assert trace.existing_evidence_table_count == MAX_PREPARED_TABLES
        mock_prep.assert_not_called()
        mock_dict.assert_not_called()
        mock_domain.assert_not_called()
        mock_entity.assert_not_called()

    def test_cold_selects_top_ranked_within_budget_and_prepares(self):
        # 15 candidate tables (more than max_prepared_tables=12), scores
        # deliberately spread so ranking is unambiguous: 5 clearly strong
        # (Master), 10 weak (Audit).
        strong = [_row(f"main.Strong{i}", table_class="Master", pk_column_count=1) for i in range(5)]
        weak = [_row(f"main.Weak{i}", table_class="Audit") for i in range(10)]
        signals = strong + weak

        prep_result = MagicMock()
        prep_result.failed = {}

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result) as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary") as mock_dict, \
             patch("data.domain_service.generate_domain_assignments") as mock_domain, \
             patch("data.entity_service.generate_entity_assignments") as mock_entity:
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=12)

        assert trace.status == STATUS_COLD
        assert len(trace.tables_prepared) == 12
        # All 5 strong tables must be selected before any weak one.
        selected_fqns = set(trace.tables_prepared)
        assert {f"main.Strong{i}" for i in range(5)} <= selected_fqns
        # Only 7 of the 10 weak tables fit in the remaining budget (12-5).
        weak_selected = [f for f in selected_fqns if f.startswith("main.Weak")]
        assert len(weak_selected) == 7

        mock_prep.assert_called_once()
        called_fqns = mock_prep.call_args.args[2]
        assert set(called_fqns) == selected_fqns
        mock_dict.assert_called_once()
        mock_domain.assert_called_once()
        mock_entity.assert_called_once()

    def test_partial_only_fills_gap_never_reselects_evidenced_tables(self):
        already = [_row(f"main.Done{i}", has_dictionary=1, table_class="Master") for i in range(3)]
        gap = [_row(f"main.Gap{i}", table_class="Transactional") for i in range(4)]
        signals = already + gap

        prep_result = MagicMock()
        prep_result.failed = {}

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result) as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary"), \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=12)

        assert trace.status == STATUS_PARTIALLY_LEARNED
        assert trace.existing_evidence_table_count == 3
        called_fqns = set(mock_prep.call_args.args[2])
        assert called_fqns == {f"main.Gap{i}" for i in range(4)}
        assert not (called_fqns & {f"main.Done{i}" for i in range(3)})

    def test_discovery_considers_at_most_max_discovery_tables(self):
        signals = [_row(f"main.T{i}", table_class="Master") for i in range(30)]
        prep_result = MagicMock()
        prep_result.failed = {}
        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result), \
             patch("data.dictionary_service.generate_and_save_dictionary"), \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=8, max_prepared_tables=12)

        assert len(trace.tables_considered) == 8
        assert len(trace.tables_prepared) <= 8


class TestBestEffortFailureHandling:
    def test_generation_failure_recorded_not_fatal(self):
        signals = [_row("main.OnlyOne", table_class="Master")]
        prep_result = MagicMock()
        prep_result.failed = {}
        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result), \
             patch("data.dictionary_service.generate_and_save_dictionary", side_effect=RuntimeError("boom")), \
             patch("data.domain_service.generate_domain_assignments") as mock_domain, \
             patch("data.entity_service.generate_entity_assignments") as mock_entity:
            trace = ensure_business_evidence(1, "u1")

        assert trace.status == STATUS_COLD
        assert trace.dictionary_summary is None
        mock_domain.assert_called_once()
        mock_entity.assert_called_once()

    def test_preparation_failure_excludes_table_from_classification(self):
        signals = [_row("main.Fails", table_class="Master"), _row("main.Works", table_class="Master")]
        prep_result = MagicMock()
        prep_result.failed = {"main.Fails": "live column probe failed"}
        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result), \
             patch("data.dictionary_service.generate_and_save_dictionary") as mock_dict, \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1")

        assert trace.tables_prepared == ["main.Works"]
        assert trace.preparation_failed == {"main.Fails": "live column probe failed"}
        assert mock_dict.call_args.kwargs["table_fqns"] == ["main.Works"]
