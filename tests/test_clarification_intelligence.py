"""
Enterprise Clarification Intelligence (Phase 6.6 / EDP M-24) — tests.

Two layers:
  1. Pure unit tests (no DB) for the new context_builder helpers that
     surface query_planning_service's own already-computed ambiguity
     signal (selected=None + "ambiguous_*" warning + ranked candidates)
     as a clarification turn, and apply a resumed selection back onto
     query_plan before SQL planning/generation ever run.
  2. One end-to-end DB-backed test (mirroring tests/test_composer_sql_routing.py's
     fixture pattern) proving the full round trip through the real,
     unmodified composer_ask -> plan_business_query -> build_sql_plan ->
     generate_sql -> LiveQueryEngine chain: an ambiguous "how many clients"
     question returns CLARIFICATION_NEEDED, and resuming with the user's
     selection produces a real answer with no further clarification.

Run from the project root:
    python -m pytest tests/test_clarification_intelligence.py -v
"""
from __future__ import annotations

import os
import sqlite3
import uuid

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase6-6-clarification-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-phase6-6-salt-long-enough-value-1234567890")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import data.datasource_service as datasource_service
import data.models as models
import data.query_planning_service as query_planning_service
from auth.api_key import AuthenticatedUser
from api.v1.composer import ComposerRequest, composer_ask
from core.answering.models import AnswerType
from core.orchestrator.context_builder import (
    NO_VALID_JOIN_MESSAGE,
    _apply_clarification_overrides,
    _extract_ambiguous_terms,
    _filter_joinable_clarification,
)

_NOW = "2026-07-13T00:00:00+00:00"
_USER = "user-1"


# ---------------------------------------------------------------------------
# 1. Pure unit tests — hand-built query_plan dicts, no DB.
# ---------------------------------------------------------------------------

def _measure(term, *, selected=None, candidates=None, warning_type=None):
    warnings = [{"type": warning_type, "severity": "MEDIUM", "message": "x"}] if warning_type else []
    return {"term": term, "selected": selected, "candidates": candidates or [], "warnings": warnings}


class TestExtractAmbiguousTerms:
    def test_detects_tied_measure_candidates(self):
        query_plan = {
            "measures": [_measure(
                "clients", warning_type="ambiguous_measure",
                candidates=[
                    {"table_fqn": "dbo.active_clients", "column_name": None, "business_label": "Active Clients", "score": 0.6},
                    {"table_fqn": "dbo.legacy_clients", "column_name": None, "business_label": "Historical Clients", "score": 0.6},
                ],
            )],
            "dimensions": [],
        }
        ambiguous = _extract_ambiguous_terms(query_plan)
        assert len(ambiguous) == 1
        assert ambiguous[0]["term"] == "clients"
        assert ambiguous[0]["kind"] == "measure"
        assert len(ambiguous[0]["candidates"]) == 2

    def test_ignores_missing_term_with_no_candidates(self):
        query_plan = {"measures": [_measure("widgets", warning_type="missing_measure", candidates=[])], "dimensions": []}
        assert _extract_ambiguous_terms(query_plan) == []

    def test_ignores_already_resolved_entries(self):
        query_plan = {
            "measures": [_measure("sales", selected={"table_fqn": "dbo.sales", "column_name": "amount"})],
            "dimensions": [],
        }
        assert _extract_ambiguous_terms(query_plan) == []

    def test_single_candidate_is_not_ambiguous(self):
        # query_planning_service only warns "ambiguous_*" when there IS a
        # real tie; a lone low-confidence candidate is a different warning
        # type entirely, but this guards the >=2 rule defensively either way.
        query_plan = {
            "measures": [_measure(
                "clients", warning_type="ambiguous_measure",
                candidates=[{"table_fqn": "dbo.clients", "column_name": None, "business_label": None, "score": 0.4}],
            )],
            "dimensions": [],
        }
        assert _extract_ambiguous_terms(query_plan) == []


class TestApplyClarificationOverrides:
    def _candidates(self):
        return [
            {"table_fqn": "dbo.revenue_current", "column_name": "amount", "business_label": "Revenue", "score": 0.55},
            {"table_fqn": "dbo.revenue_legacy", "column_name": "amt", "business_label": "Legacy Revenue", "score": 0.53},
        ]

    def test_resolves_matching_selection(self):
        query_plan = {
            "measures": [_measure("revenue", warning_type="ambiguous_measure", candidates=self._candidates())],
            "dimensions": [], "join_plan": {"required": False},
        }
        _apply_clarification_overrides(
            query_plan, [{"term": "revenue", "table_fqn": "dbo.revenue_current"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )
        entry = query_plan["measures"][0]
        assert entry["selected"]["table_fqn"] == "dbo.revenue_current"
        assert entry["warnings"] == []

    def test_invalid_selection_leaves_unresolved(self):
        query_plan = {
            "measures": [_measure("revenue", warning_type="ambiguous_measure", candidates=self._candidates())],
            "dimensions": [], "join_plan": {"required": False},
        }
        _apply_clarification_overrides(
            query_plan, [{"term": "revenue", "table_fqn": "dbo.not_a_real_candidate"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )
        entry = query_plan["measures"][0]
        assert entry["selected"] is None
        assert _extract_ambiguous_terms(query_plan) != []  # still asked again

    def test_skips_when_join_required(self):
        query_plan = {
            "measures": [_measure("revenue", warning_type="ambiguous_measure", candidates=self._candidates())],
            "dimensions": [], "join_plan": {"required": True},
        }
        _apply_clarification_overrides(
            query_plan, [{"term": "revenue", "table_fqn": "dbo.revenue_current"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )
        assert query_plan["measures"][0]["selected"] is None

    def test_repeated_clarification_when_nothing_resolved_yet(self):
        # Two ambiguous measures, neither answered this turn (e.g. an empty
        # or unrelated clarification_selection) — both are still asked
        # about, since nothing in the plan resolved.
        query_plan = {
            "measures": [
                _measure("revenue", warning_type="ambiguous_measure", candidates=self._candidates()),
                _measure("clients", warning_type="ambiguous_measure", candidates=[
                    {"table_fqn": "dbo.active_clients", "column_name": None, "business_label": "Active Clients", "score": 0.6},
                    {"table_fqn": "dbo.legacy_clients", "column_name": None, "business_label": "Historical Clients", "score": 0.6},
                ]),
            ],
            "dimensions": [], "join_plan": {"required": False},
        }
        _apply_clarification_overrides(query_plan, [], source_id=1, user_id=_USER, distinct_requested=False)
        remaining = {t["term"] for t in _extract_ambiguous_terms(query_plan)}
        assert remaining == {"revenue", "clients"}

    def test_partial_resolution_does_not_reclarify_remaining_ambiguous_term(self):
        # Faithfully mirrors sql_planning_service's own existing leniency:
        # once ANY measure/dimension resolves, an extra ambiguous term is
        # left for build_sql_plan's own "if select: skip with a warning"
        # rule rather than blocked/re-asked — the same behavior a question
        # with one confident term and one unrelated extra word already gets
        # today, unchanged by this milestone.
        query_plan = {
            "measures": [_measure("revenue", warning_type="ambiguous_measure", candidates=self._candidates())],
            "dimensions": [_measure(
                "region", warning_type="ambiguous_dimension",
                candidates=[
                    {"table_fqn": "dbo.region_current", "column_name": "region", "business_label": "Region", "score": 0.5},
                    {"table_fqn": "dbo.region_legacy", "column_name": "region_code", "business_label": "Legacy Region", "score": 0.48},
                ],
            )],
            "join_plan": {"required": False},
        }
        _apply_clarification_overrides(
            query_plan, [{"term": "revenue", "table_fqn": "dbo.revenue_current"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )
        assert query_plan["measures"][0]["selected"]["table_fqn"] == "dbo.revenue_current"
        assert _extract_ambiguous_terms(query_plan) == []

    def test_entity_count_candidate_gets_enriched(self, monkeypatch):
        entity_candidates = [
            {"table_fqn": "dbo.active_clients", "column_name": None, "business_label": "Active Clients", "score": 0.6},
            {"table_fqn": "dbo.legacy_clients", "column_name": None, "business_label": "Historical Clients", "score": 0.6},
        ]
        query_plan = {
            "measures": [_measure("clients", warning_type="ambiguous_measure", candidates=entity_candidates)],
            "dimensions": [], "join_plan": {"required": False},
        }
        enriched = {**entity_candidates[0], "column_name": "id", "aggregation_target": "entity_count", "key_tier": 1}
        called = {}

        def fake_enrich(source_id, user_id, candidate, *, distinct_requested):
            called["candidate"] = candidate
            called["distinct_requested"] = distinct_requested
            return enriched

        monkeypatch.setattr(query_planning_service, "enrich_entity_count_selection", fake_enrich)
        _apply_clarification_overrides(
            query_plan, [{"term": "clients", "table_fqn": "dbo.active_clients"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )
        assert called["candidate"]["table_fqn"] == "dbo.active_clients"
        assert query_plan["measures"][0]["selected"] == enriched


class TestApplyClarificationOverridesRecomputesJoinPlan:
    """Sprint 1.5 — Step A. Before this fix, join_plan stayed whatever it
    was computed as BEFORE overrides were applied (always {"required":
    False} at this point, since ambiguous entries have selected=None) —
    stale for whatever tables the user's picks actually resolved to. Now it
    must be recomputed from the post-override selection."""

    def test_join_plan_recomputed_for_cross_table_selection(self, monkeypatch):
        query_plan = {
            "measures": [_measure(
                "enrollment", warning_type="ambiguous_measure",
                candidates=[{"table_fqn": "dbo.enrollments", "column_name": None, "business_label": "Enrollment Count", "score": 0.6}],
            )],
            "dimensions": [_measure(
                "course name", warning_type="ambiguous_dimension",
                candidates=[{"table_fqn": "dbo.courses", "column_name": "name", "business_label": "Course Name", "score": 0.5}],
            )],
            "join_plan": {"required": False, "tables": [], "steps": []},
        }
        recompute_calls = []

        def fake_plan_joins(source_id, user_id, primary_table, selected_tables):
            recompute_calls.append((primary_table, selected_tables))
            return {"required": True, "tables": sorted(selected_tables), "steps": [{"path_found": True}], "confidence": 90}

        monkeypatch.setattr(query_planning_service, "_plan_joins", fake_plan_joins)
        _apply_clarification_overrides(
            query_plan,
            [{"term": "enrollment", "table_fqn": "dbo.enrollments"}, {"term": "course name", "table_fqn": "dbo.courses"}],
            source_id=1, user_id=_USER, distinct_requested=False,
        )

        assert len(recompute_calls) == 1
        _, tables = recompute_calls[0]
        assert tables == {"dbo.enrollments", "dbo.courses"}
        assert query_plan["join_plan"] == {
            "required": True, "tables": ["dbo.courses", "dbo.enrollments"],
            "steps": [{"path_found": True}], "confidence": 90,
        }


class TestResolveRankingOrderColumn:
    """Enterprise Implementation — Recompute Order After Clarification.
    Pure unit tests for query_planning_service._resolve_ranking_order_column
    — the single ordering-resolution rule shared by plan_business_query's
    first pass and _apply_clarification_overrides' post-clarification
    recompute below (never two implementations)."""

    def test_resolves_from_selected_measure_preserving_direction_and_limit(self):
        from data.query_planning_service import _resolve_ranking_order_column

        order = {"direction": "DESC", "limit": 10}
        measures = [_measure("enrollment", selected={"table_fqn": "dbo.enrollments", "column_name": "ClassID"})]
        resolved, warning = _resolve_ranking_order_column(order, measures)

        assert resolved == {"direction": "DESC", "limit": 10, "table_fqn": "dbo.enrollments", "column_name": "ClassID"}
        assert warning is None

    def test_no_selected_measure_returns_warning_and_unmodified_order(self):
        from data.query_planning_service import _resolve_ranking_order_column

        order = {"direction": "DESC", "limit": 10}
        resolved, warning = _resolve_ranking_order_column(order, [_measure("enrollment", selected=None)])

        assert resolved == {"direction": "DESC", "limit": 10}
        assert warning["type"] == "order_column_not_found"


class TestApplyClarificationOverridesRecomputesOrder:
    """Enterprise Implementation — Recompute Order After Clarification.

    plan_business_query resolves a ranking order's target column from
    measures[].selected — but for a WH-ranking question with an ambiguous
    measure ("Which courses have the highest enrollment?"), that first pass
    runs before any clarification override, so intent["order"] was left
    without a table_fqn/column_name (an "order_column_not_found" warning),
    even after the measure resolves right above it. Reuses
    query_planning_service._resolve_ranking_order_column verbatim — the
    exact same rule plan_business_query itself uses, never a second one.
    """

    def _query_plan(self, *, order):
        return {
            "measures": [_measure(
                "enrollment", warning_type="ambiguous_measure",
                candidates=[{"table_fqn": "dbo.enrollments", "column_name": None,
                             "business_label": "Enrollment", "score": 0.6}],
            )],
            "dimensions": [_measure(
                "courses", warning_type="ambiguous_dimension",
                candidates=[{"table_fqn": "dbo.courses", "column_name": "name",
                             "business_label": "Course Name", "score": 0.5}],
            )],
            "join_plan": {"required": False, "tables": [], "steps": []},
            "intent": {"order": order},
            "warnings": (
                [{"type": "order_column_not_found", "severity": "LOW",
                  "message": "Ranking was requested but no measure/dimension was specified to rank by."}]
                if order else []
            ),
        }

    def _selections(self):
        return [
            {"term": "enrollment", "table_fqn": "dbo.enrollments"},
            {"term": "courses", "table_fqn": "dbo.courses", "column_name": "name"},
        ]

    def _fake_plan_joins(self, monkeypatch):
        monkeypatch.setattr(
            query_planning_service, "_plan_joins",
            lambda source_id, user_id, primary_table, selected_tables: {
                "required": True, "tables": sorted(selected_tables),
                "steps": [{"path_found": True}], "confidence": 90,
            },
        )

    def _fake_enrich(self, monkeypatch, *, column_name="ClassID"):
        monkeypatch.setattr(
            query_planning_service, "enrich_entity_count_selection",
            lambda source_id, user_id, candidate, *, distinct_requested: {
                **candidate, "column_name": column_name, "aggregation_target": "entity_count",
            },
        )

    def test_wh_ranking_gains_order_by_aggregate_target_after_clarification(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        self._fake_enrich(monkeypatch)
        query_plan = self._query_plan(order={"direction": "DESC", "limit": 10})

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        order = query_plan["intent"]["order"]
        assert order["table_fqn"] == "dbo.enrollments"
        assert order["column_name"] == "ClassID"
        assert order["direction"] == "DESC"
        assert not any(w.get("type") == "order_column_not_found" for w in query_plan["warnings"])

    def test_top_n_limit_preserved(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        self._fake_enrich(monkeypatch)
        query_plan = self._query_plan(order={"direction": "DESC", "limit": 10})

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        assert query_plan["intent"]["order"]["limit"] == 10

    def test_lowest_uses_ascending(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        self._fake_enrich(monkeypatch)
        query_plan = self._query_plan(order={"direction": "ASC", "limit": 10})

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        order = query_plan["intent"]["order"]
        assert order["direction"] == "ASC"
        assert order["column_name"] == "ClassID"

    def test_non_ranking_clarification_unaffected(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        query_plan = self._query_plan(order=None)

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        assert query_plan["intent"]["order"] is None

    def test_already_resolved_order_left_untouched(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        already_resolved = {"direction": "DESC", "limit": 10, "table_fqn": "dbo.other", "column_name": "amount"}
        query_plan = self._query_plan(order=dict(already_resolved))

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        assert query_plan["intent"]["order"] == already_resolved

    def test_date_target_order_left_untouched(self, monkeypatch):
        self._fake_plan_joins(monkeypatch)
        query_plan = self._query_plan(order={"direction": "DESC", "limit": 10, "target": "date"})

        _apply_clarification_overrides(
            query_plan, self._selections(), source_id=1, user_id=_USER, distinct_requested=False,
        )

        assert "table_fqn" not in query_plan["intent"]["order"]


class TestFilterJoinableClarification:
    """Sprint 1.5 — Steps 1-7. Validates candidate combinations across
    ambiguous terms against the existing join graph (_plan_joins) before
    they're ever offered to the user."""

    def _candidate(self, table_fqn, score=0.5, authority_bonus=0.0, column_name=None):
        return {
            "table_fqn": table_fqn, "column_name": column_name,
            "business_label": table_fqn, "score": score, "authority_bonus": authority_bonus,
        }

    def test_single_ambiguous_term_is_unchanged(self):
        # Nothing to join against with only one ambiguous term — same
        # behavior as today.
        terms = [{"term": "clients", "kind": "measure", "candidates": [self._candidate("dbo.a"), self._candidate("dbo.b")]}]
        filtered, no_valid_join = _filter_joinable_clarification(terms, source_id=1, user_id=_USER)
        assert filtered == terms
        assert no_valid_join is False

    def test_same_table_combo_is_trivially_valid_without_calling_plan_joins(self, monkeypatch):
        called = []
        monkeypatch.setattr(query_planning_service, "_plan_joins", lambda *a, **k: called.append(a) or {})
        terms = [
            {"term": "enrollment", "kind": "measure", "candidates": [self._candidate("dbo.enrollments")]},
            {"term": "course id", "kind": "dimension", "candidates": [self._candidate("dbo.enrollments")]},
        ]
        filtered, no_valid_join = _filter_joinable_clarification(terms, source_id=1, user_id=_USER)
        assert no_valid_join is False
        assert called == []  # single-table combo never needs a join check
        assert [c["table_fqn"] for c in filtered[0]["candidates"]] == ["dbo.enrollments"]

    def test_keeps_only_joinable_combination(self, monkeypatch):
        # "course name" has two candidates: dbo.courses (joinable to
        # dbo.enrollments) and dbo.archived_courses (not). Only the joinable
        # one should survive.
        def fake_plan_joins(source_id, user_id, primary_table, selected_tables):
            if "dbo.archived_courses" in selected_tables:
                return {"steps": [{"path_found": False}], "confidence": 0}
            return {"steps": [{"path_found": True}], "confidence": 85}

        monkeypatch.setattr(query_planning_service, "_plan_joins", fake_plan_joins)
        terms = [
            {"term": "enrollment", "kind": "measure", "candidates": [self._candidate("dbo.enrollments", score=0.6)]},
            {"term": "course name", "kind": "dimension", "candidates": [
                self._candidate("dbo.courses", score=0.5),
                self._candidate("dbo.archived_courses", score=0.55),
            ]},
        ]
        filtered, no_valid_join = _filter_joinable_clarification(terms, source_id=1, user_id=_USER)
        assert no_valid_join is False
        course_term = next(t for t in filtered if t["term"] == "course name")
        assert [c["table_fqn"] for c in course_term["candidates"]] == ["dbo.courses"]

    def test_no_joinable_combination_returns_refusal_signal(self, monkeypatch):
        monkeypatch.setattr(
            query_planning_service, "_plan_joins",
            lambda *a, **k: {"steps": [{"path_found": False}], "confidence": 0},
        )
        terms = [
            {"term": "ai agent", "kind": "dimension", "candidates": [self._candidate("dbo.ai_agents")]},
            {"term": "workflow count", "kind": "measure", "candidates": [self._candidate("dbo.unrelated_workflows")]},
        ]
        filtered, no_valid_join = _filter_joinable_clarification(terms, source_id=1, user_id=_USER)
        assert no_valid_join is True

    def test_ranks_higher_semantic_confidence_and_fewer_hops_first(self, monkeypatch):
        def fake_plan_joins(source_id, user_id, primary_table, selected_tables):
            hops = 2 if "dbo.far_table" in selected_tables else 1
            return {"steps": [{"path_found": True}] * hops, "confidence": 80}

        monkeypatch.setattr(query_planning_service, "_plan_joins", fake_plan_joins)
        terms = [
            {"term": "enrollment", "kind": "measure", "candidates": [self._candidate("dbo.enrollments", score=0.6)]},
            {"term": "course name", "kind": "dimension", "candidates": [
                self._candidate("dbo.near_table", score=0.5),
                self._candidate("dbo.far_table", score=0.5),
            ]},
        ]
        filtered, no_valid_join = _filter_joinable_clarification(terms, source_id=1, user_id=_USER)
        assert no_valid_join is False
        course_term = next(t for t in filtered if t["term"] == "course name")
        # Both combos are joinable; the fewer-hops one (near_table) must rank first.
        assert [c["table_fqn"] for c in course_term["candidates"]] == ["dbo.near_table", "dbo.far_table"]


# ---------------------------------------------------------------------------
# 2. End-to-end — real composer_ask over a fixture with two tied "clients"
#    tables (same pattern as test_composer_sql_routing.py::env), proving the
#    full ask -> clarify -> resume -> real-answer loop with no bypass.
# ---------------------------------------------------------------------------

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
    "data.schema_service",
    "data.relationship_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    """One mssql source owning two tied 'clients' tables (dbo.active_clients,
    dbo.legacy_clients) — both unapproved, no domain/entity assignment, so
    _score_table_authority gives them an identical bonus and
    _score_term_match ties them at 0.75 name-score against 'clients'
    (substring bonus, symmetric) — a genuine, deterministic tie, not a
    contrived one, well within _AMBIGUITY_MARGIN."""
    db_path = str(tmp_path / "clarification.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, live_query_enabled, created_at, updated_at) "
        "VALUES (1,?,'Prod SQL Server','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,1,?,?)",
        (_USER, _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )

    for i, (table_fqn, table_name, business_name) in enumerate((
        ("dbo.active_clients", "active_clients", "Active Clients"),
        ("dbo.legacy_clients", "legacy_clients", "Historical Clients"),
    ), start=1):
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
            " table_class, profiling_status, exact_row_count, created_at, updated_at) "
            "VALUES (?,1,1,?,?,'dbo','Transactional','COMPLETE',500,?,?)",
            (i, table_fqn, table_name, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            " business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,'dbo','TABLE',?,0,'rule_based',?,?)",
            (table_fqn, table_name, business_name, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
            " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (?,1,1,?,'id','INTEGER',1,1,1.0,0,0.0,'HIGH',0,0,?,?)",
            (100 + i, table_fqn, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,?,'id','Client ID',0,0,0,1,0,0,'rule_based',?,?)",
            (table_fqn, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return db_path


def _mssql_record(**overrides):
    base = {
        "source_type": "mssql", "source_category": "relational_db",
        "display_name": "Prod SQL Server", "is_active": True, "source_status": "ACTIVE",
        "capabilities": ["connection_test", "schema_discovery", "sql_query"],
        "live_query_enabled": True, "params": {"host": "db.internal", "database": "CCPP"},
    }
    base.update(overrides)
    return base


class _FakeCursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def execute(self, sql, params):
        pass

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _bypass_rate_limits_and_audit(monkeypatch):
    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: False)
    monkeypatch.setattr(qes, "_check_daily_limit", lambda user_id: 0)
    monkeypatch.setattr(qes, "_check_source_rate", lambda source_id: 0)
    monkeypatch.setattr(qes, "_check_repeated_query", lambda user_id, sql_hash: 0)
    monkeypatch.setattr(qes, "log_query_execution", lambda *a, **k: None)
    monkeypatch.setattr(qes, "_write_audit", lambda *a, **k: None)


def _wire_fake_connector(monkeypatch, *, rows, description):
    fake_conn = _FakeConnection(_FakeCursor(description, rows))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


def _ask(message: str, **extra) -> dict:
    body = ComposerRequest(
        session_id=str(uuid.uuid4()), message=message, selected_data_source=1, **extra,
    )
    user = AuthenticatedUser(role="user", user_id=_USER)
    return composer_ask(body, user)


class TestClarificationEndToEnd:
    def test_ambiguous_question_returns_clarification_not_sql(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        result = _ask("how many clients")

        answer = result["enterprise_answer"]
        assert answer["answer_type"] == AnswerType.CLARIFICATION_NEEDED.value
        assert answer["clarification"] is not None
        table_fqns = {o["table_fqn"] for o in answer["clarification"]["options"]}
        assert table_fqns == {"dbo.active_clients", "dbo.legacy_clients"}
        # Frontend contract guard: AIWorkspace.jsx's ClarificationCard renders
        # `label`/`description` and resubmits `{term, table_fqn, column_name}`
        # (not `id`, which is UI-only). A field rename here must fail this
        # assertion rather than silently break the frontend.
        for option in answer["clarification"]["options"]:
            assert set(option.keys()) == {
                "id", "term", "table_fqn", "column_name", "label", "description", "score",
            }
        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        assert live_evidence["data"]["executed"] is False

    def test_resume_with_selection_executes_real_query(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("cnt",)], rows=[(42,)])

        result = _ask(
            "how many clients",
            clarification_selection=[{"term": "clients", "table_fqn": "dbo.active_clients"}],
        )

        answer = result["enterprise_answer"]
        assert answer["answer_type"] == AnswerType.LIVE_QUERY.value
        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        assert live_evidence["data"]["status"] == "success"
        assert "active_clients" in live_evidence["data"]["generated_sql"]
        assert "legacy_clients" not in live_evidence["data"]["generated_sql"]

    def test_invalid_selection_asks_again(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        result = _ask(
            "how many clients",
            clarification_selection=[{"term": "clients", "table_fqn": "dbo.not_a_real_table"}],
        )

        assert result["enterprise_answer"]["answer_type"] == AnswerType.CLARIFICATION_NEEDED.value

    def test_cancel_clarification_falls_back_to_plain_refusal(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        result = _ask("how many clients", cancel_clarification=True)

        answer = result["enterprise_answer"]
        assert answer["answer_type"] == AnswerType.LIVE_QUERY.value
        assert answer["clarification"] is None
        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        assert live_evidence["data"]["executed"] is False
        assert live_evidence["data"]["reason"] == "sql_generation_refused"
