"""
Portable Business Learning Layer milestone — controller unit tests for
core.orchestrator.business_learning.run_connect_time_business_learning.

Offline/mocked (no real LLM, no real DB) — exercises the two-path control
flow (portable-match vs. bounded bootstrap) and the flag gate, mirroring the
same technique test_investigation_agent.py already uses for
investigate_and_replan.
"""
from __future__ import annotations

import core.orchestrator.business_learning as bl


def test_no_op_when_flag_disabled(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER", False)
    trace = bl.run_connect_time_business_learning(1, "u1")
    assert trace.fingerprint_computed is False
    assert trace.portable_match == "NONE"
    assert trace.ai_calls == 0


def test_no_op_when_no_schema_fingerprint_yet(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER", True)
    monkeypatch.setattr("data.schema_fingerprint_service.compute_fingerprint", lambda *a, **k: None)
    trace = bl.run_connect_time_business_learning(1, "u1")
    assert trace.fingerprint_computed is False
    assert trace.error == "no_schema_fingerprint"


def test_activates_matched_portable_package_without_any_ai_call(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER", True)
    fp = {"fingerprint_version": 1, "fingerprint_hash": "h", "table_set_hash": "t",
          "table_fqns": ["dbo.Student"], "structural_summary": {"dbo.Student": {"columns": []}}}
    monkeypatch.setattr("data.schema_fingerprint_service.compute_fingerprint", lambda *a, **k: fp)

    package = {"database_family_identifier": "reference_v1", "knowledge_package_version": 1,
               "schema_fingerprint": fp, "vocabulary": [{"term": "student", "table_fqn": "dbo.Student",
               "column_name": "", "confidence_tier": "HIGH", "confidence_score": 0.9, "evidence": [],
               "required_schema_objects": [{"table_fqn": "dbo.Student", "column_name": None}]}],
               "entity_assignments": [], "entity_contracts": [], "trusted_relationships": []}
    validation = {"match": "EXACT", "sections": {
        "vocabulary": [{"row": package["vocabulary"][0], "status": "VALID"}],
        "entity_assignments": [], "entity_contracts": [], "trusted_relationships": [],
    }}

    import data.portable_knowledge_service as pks
    monkeypatch.setattr(pks, "find_matching_package",
                         lambda *a, **k: {"package": package, "comparison": {"match": "EXACT"}})
    monkeypatch.setattr(pks, "validate_package_against_live_schema", lambda *a, **k: validation)
    activated = {}
    monkeypatch.setattr(pks, "activate_package", lambda *a, **k: activated.setdefault("called", True))

    called_ai = {"count": 0}
    monkeypatch.setattr(bl, "_infer_preliminary_business_map", lambda *a, **k: called_ai.__setitem__("count", called_ai["count"] + 1))

    trace = bl.run_connect_time_business_learning(1, "u1")
    assert trace.portable_match == "EXACT"
    assert trace.matched_package_family == "reference_v1"
    assert trace.assertions_loaded == 1
    assert trace.assertions_validated == 1
    assert activated.get("called") is True
    assert called_ai["count"] == 0  # no LLM call on the portable-match path


def test_runs_bounded_bootstrap_when_no_package_matches(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER", True)
    monkeypatch.setattr("core.config.BUSINESS_LEARNING_MAX_TABLES", 12)
    fp = {"fingerprint_version": 1, "fingerprint_hash": "h", "table_set_hash": "t",
          "table_fqns": ["dbo.Widget"], "structural_summary": {"dbo.Widget": {"columns": [
              {"column_name": "id", "data_type": "INTEGER", "is_nullable": False, "is_primary_key": True}]}}}
    monkeypatch.setattr("data.schema_fingerprint_service.compute_fingerprint", lambda *a, **k: fp)

    import data.portable_knowledge_service as pks
    monkeypatch.setattr(pks, "find_matching_package", lambda *a, **k: None)

    class _FakeDiscoveryTrace:
        tables_prepared = ["dbo.Widget"]

    monkeypatch.setattr(
        "core.analysis.cold_start_discovery.ensure_business_evidence",
        lambda *a, **k: _FakeDiscoveryTrace(),
    )
    monkeypatch.setattr("data.entity_service.list_entity_assignments", lambda *a, **k: [])

    fake_map = {
        "likely_domain": "widgets", "major_entities": ["Widget"],
        "fact_tables": [], "dimension_tables": ["dbo.Widget"], "bridge_tables": [],
        "candidate_measures": [], "candidate_dates": [], "notes": None,
    }
    monkeypatch.setattr(bl, "_infer_preliminary_business_map", lambda *a, **k: fake_map)

    trace = bl.run_connect_time_business_learning(1, "u1")
    assert trace.portable_match == "NONE"
    assert trace.bootstrap_activated is True
    assert trace.tables_structurally_examined == 1
    assert trace.ai_calls == 1
    assert trace.preliminary_domain == "widgets"
    assert trace.preliminary_entities == ["Widget"]


def test_never_raises_on_internal_failure(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER", True)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("data.schema_fingerprint_service.compute_fingerprint", _boom)
    trace = bl.run_connect_time_business_learning(1, "u1")
    assert trace.error is not None
    assert "boom" in trace.error


def test_preliminary_map_never_references_a_table_not_in_the_input(monkeypatch):
    """Fail-closed identifier guard, same discipline as investigation_agent's
    _validate_decision — the model must never be trusted to only mention
    tables it was given."""
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(bl, "_OPENAI_AVAILABLE", True)

    class _FakeChoice:
        class _Msg:
            content = (
                '{"likely_domain": "test", "major_entities": ["Widget"], '
                '"fact_tables": ["dbo.Ghost"], "dimension_tables": ["dbo.Widget"], '
                '"bridge_tables": [], "candidate_measures": [{"table_fqn": "dbo.Ghost", "column_name": "x"}], '
                '"candidate_dates": [], "notes": null}'
            )
        message = _Msg()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(bl._openai, "OpenAI", lambda **k: _FakeClient())

    structural_summary = {"dbo.Widget": {"columns": [{"column_name": "id", "data_type": "INTEGER"}]}}
    result = bl._infer_preliminary_business_map(structural_summary, ["dbo.Widget"], {})
    assert result is not None
    assert result["fact_tables"] == []  # "dbo.Ghost" was never in the input — dropped
    assert result["candidate_measures"] == []  # references dbo.Ghost — dropped
    assert result["dimension_tables"] == ["dbo.Widget"]
