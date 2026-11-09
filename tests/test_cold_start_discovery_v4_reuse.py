"""
Executive Report V4 — coverage for the two new discovery-reuse signals added
to core.analysis.cold_start_discovery (entity-assignment reuse, generic
business-vocabulary semantic boost) and the WARM-but-poor-pick top-up gate.

Root cause this closes (live-traced against a real production database, see that module's own
docstring): the original structural-only ranking had no way to prefer a
genuinely central business table over a generically-named, highly
cross-referenced identity/reference table, and once ANY MAX_PREPARED_TABLES
tables were evidenced — regardless of whether they were the RIGHT ones — the
WARM gate froze the source's business_map at that pick forever. These tests
prove the fix generically (synthetic table names, never the reference database's own), mocking
search_metadata so cases are deterministic and never touch a real DB.

Run from the project root:
    python -m pytest tests/test_cold_start_discovery_v4_reuse.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.analysis.cold_start_discovery import (
    STATUS_TOPPED_UP,
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
        "has_entity": 0,
        "assigned_entity": None,
        "assigned_entity_confidence": None,
    }
    base.update(overrides)
    return base


_EMPTY_SEARCH = {"results": [], "total": 0, "query": "", "tokens": []}


class TestEntityAssignmentReuse:
    def test_existing_entity_assignment_boosts_score(self):
        """A table with no dictionary/domain evidence yet, but ALREADY
        entity-resolved (e.g. by normal Q&A's own prior semantic-contract
        resolution), must rank higher than an otherwise-identical table with
        no entity evidence at all — the direct fix for real EDU_Track-style
        tables that structural signal alone ranked near the bottom."""
        plain, _ = _relevance_score(_row("dbo.Widgets"))
        resolved, reasons = _relevance_score(_row(
            "dbo.Catalog", assigned_entity="Program", assigned_entity_confidence=0.75,
        ))
        assert resolved > plain
        assert any("Program" in r for r in reasons)

    def test_unknown_or_zero_confidence_entity_gives_no_bonus(self):
        baseline, _ = _relevance_score(_row("dbo.Plain"))
        unknown, _ = _relevance_score(_row(
            "dbo.StillUnknown", assigned_entity="Unknown", assigned_entity_confidence=0.9,
        ))
        zero_conf, _ = _relevance_score(_row(
            "dbo.NoConfidence", assigned_entity="Customer", assigned_entity_confidence=0.0,
        ))
        assert unknown == baseline
        assert zero_conf == baseline

    def test_higher_confidence_scores_higher(self):
        low, _ = _relevance_score(_row("dbo.A", assigned_entity="Order", assigned_entity_confidence=0.2))
        high, _ = _relevance_score(_row("dbo.B", assigned_entity="Order", assigned_entity_confidence=0.9))
        assert high > low


class TestSemanticVocabularyBoost:
    def test_search_metadata_match_boosts_ranked_table_into_selection(self):
        """A not-yet-evidenced table whose raw column names match generic
        business vocabulary (via data.search_service.search_metadata, the
        SAME engine normal Q&A's candidate retrieval already uses) must be
        selected ahead of a structurally-similar table with no such match —
        proves the semantic signal actually reaches the selection, not just
        the score function in isolation."""
        matched = _row("dbo.RealBusinessTable", table_class="Reference")
        noise = [_row(f"dbo.Noise{i}", table_class="Reference") for i in range(15)]
        signals = [matched] + noise

        search_result = {
            "results": [{"qualified_name": "dbo.RealBusinessTable", "relevance_score": 300}],
            "total": 1, "query": "x", "tokens": [],
        }
        prep_result = MagicMock()
        prep_result.failed = {}

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.search_service.search_metadata", return_value=search_result), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result) as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary"), \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=5)

        assert "dbo.RealBusinessTable" in trace.tables_prepared
        called_fqns = set(mock_prep.call_args.args[2])
        assert "dbo.RealBusinessTable" in called_fqns

    def test_search_metadata_failure_degrades_to_structural_only(self):
        """A search_metadata failure must never break discovery — same
        best-effort convention as every other signal in this module."""
        signals = [_row("dbo.Only", table_class="Master")]
        prep_result = MagicMock()
        prep_result.failed = {}
        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.search_service.search_metadata", side_effect=RuntimeError("boom")), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result), \
             patch("data.dictionary_service.generate_and_save_dictionary"), \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1")

        assert trace.tables_prepared == ["dbo.Only"]


class TestWarmButPoorPickTopUp:
    def test_thin_warm_source_gets_bounded_topup_when_better_candidate_exists(self):
        """The historical bug: 12 mediocre, generically-referenced tables
        already evidenced (meeting the raw count budget) while ONE
        genuinely strong, already entity-resolved candidate sat unused.
        Previously this stayed WARM forever; now it must run one bounded,
        additive top-up pass that picks up the better candidate WITHOUT
        touching the existing 12."""
        evidenced = [
            _row(f"dbo.Generic{i}", has_dictionary=1, has_domain=1, table_class="Reference")
            for i in range(12)
        ]
        strong_candidate = _row(
            "dbo.RealEntity", assigned_entity="Program", assigned_entity_confidence=1.0, table_class="Unknown",
        )
        signals = evidenced + [strong_candidate]

        prep_result = MagicMock()
        prep_result.failed = {}

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.search_service.search_metadata", return_value=_EMPTY_SEARCH), \
             patch("data.metadata_preparation_service.prepare_selected_tables", return_value=prep_result) as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary") as mock_dict, \
             patch("data.domain_service.generate_domain_assignments") as mock_domain, \
             patch("data.entity_service.generate_entity_assignments") as mock_entity:
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=12)

        assert trace.status == STATUS_TOPPED_UP
        assert trace.tables_prepared == ["dbo.RealEntity"]
        # Never touches the already-evidenced 12.
        called_fqns = set(mock_prep.call_args.args[2])
        assert called_fqns == {"dbo.RealEntity"}
        mock_dict.assert_called_once()
        mock_domain.assert_called_once()
        mock_entity.assert_called_once()

    def test_warm_stays_warm_when_existing_pick_already_good(self):
        """No not-yet-evidenced candidate meaningfully outranks the weakest
        already-evidenced table — must stay a true no-op, exactly like
        before this milestone, so a genuinely good first pick is never
        needlessly re-touched on every report generation."""
        evidenced = [
            _row(f"dbo.Good{i}", has_dictionary=1, has_domain=1, table_class="Master",
                 assigned_entity="Customer", assigned_entity_confidence=0.9)
            for i in range(12)
        ]
        mediocre_candidate = _row("dbo.Mediocre", table_class="Unknown")
        signals = evidenced + [mediocre_candidate]

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.search_service.search_metadata", return_value=_EMPTY_SEARCH), \
             patch("data.metadata_preparation_service.prepare_selected_tables") as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary") as mock_dict, \
             patch("data.domain_service.generate_domain_assignments") as mock_domain, \
             patch("data.entity_service.generate_entity_assignments") as mock_entity:
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=12)

        assert trace.status == STATUS_WARM
        mock_prep.assert_not_called()
        mock_dict.assert_not_called()
        mock_domain.assert_not_called()
        mock_entity.assert_not_called()

    def test_topup_bounded_by_discovery_ceiling_even_when_thin(self):
        """Once existing evidence already reached max_discovery_tables, no
        further top-up is attempted even if a better candidate exists —
        ceiling, not target: repeated report runs against a source that
        never improves must still terminate."""
        evidenced = [
            _row(f"dbo.Generic{i}", has_dictionary=1, table_class="Master") for i in range(20)
        ]
        strong_candidate = _row(
            "dbo.RealEntity", assigned_entity="Program", assigned_entity_confidence=0.9,
        )
        signals = evidenced + [strong_candidate]

        with patch("data.profiling_service.list_table_relevance_signals", return_value=signals), \
             patch("data.search_service.search_metadata", return_value=_EMPTY_SEARCH), \
             patch("data.metadata_preparation_service.prepare_selected_tables") as mock_prep, \
             patch("data.dictionary_service.generate_and_save_dictionary"), \
             patch("data.domain_service.generate_domain_assignments"), \
             patch("data.entity_service.generate_entity_assignments"):
            trace = ensure_business_evidence(1, "u1", max_discovery_tables=20, max_prepared_tables=12)

        assert trace.status == STATUS_WARM
        mock_prep.assert_not_called()
