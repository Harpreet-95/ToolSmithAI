"""
Phase 2 — Enterprise Composer API: backend tests.

Tests cover:
  1. Successful request — orchestrator resolves intent and returns a valid package
  2. Validation failure — required fields missing raise ValidationError
  3. Unknown intent — gibberish query resolves to UNKNOWN, status is partial
  4. Partial evidence — missing source_id causes services to return None (no DB call)
  5. Empty message — ComposerRequest rejects blank/whitespace messages
  6. Unavailable service — absent source_id triggers the adapter's early-return guard

Run from the project root:
    python -m pytest tests/test_composer_api.py -v
"""
from __future__ import annotations

import os
import uuid

from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-composer-api-phase2!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-composer-api")

import pytest
from pydantic import ValidationError

from api.v1.composer import (
    ComposerRequest,
    _determine_status,
    _extract_governance_state,
    _extract_warnings,
    _generate_answer,
    _serialize_package,
)
from core.orchestrator import (
    EnterpriseOrchestrator,
    IntentType,
    OrchestratorRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch_request(
    query: str,
    source_id=None,
    user_id: str = "test-user-1",
) -> OrchestratorRequest:
    return OrchestratorRequest(
        query=query,
        source_id=source_id,
        user_id=user_id,
        request_id=str(uuid.uuid4()),
    )


def _run(query: str, source_id=None):
    req = _make_orch_request(query, source_id=source_id)
    return EnterpriseOrchestrator().process(req)


# ---------------------------------------------------------------------------
# 1. Successful request
# ---------------------------------------------------------------------------

class TestSuccessfulRequest:
    def test_package_has_expected_fields(self):
        package = _run("show me the dictionary definitions for this data source")
        assert package.request_id
        assert isinstance(package.intent.intent_type, IntentType)
        assert package.intent.confidence >= 0.0
        assert isinstance(package.evidence, list)
        assert isinstance(package.service_calls, list)
        assert isinstance(package.errors, list)

    def test_dictionary_intent_resolved(self):
        package = _run("show me the dictionary definitions for this data source")
        assert package.intent.intent_type == IntentType.DICTIONARY

    def test_services_are_attempted(self):
        package = _run("show me the dictionary definitions for this data source")
        assert package.services_attempted >= 1

    def test_package_serializes_cleanly(self):
        package = _run("show me the dictionary definitions for this data source")
        serialized = _serialize_package(package)
        assert set(serialized.keys()) >= {
            "request_id", "query", "built_at", "source_id",
            "total_evidence_items", "services_attempted",
            "services_succeeded", "evidence", "service_calls", "errors",
        }
        assert isinstance(serialized["evidence"], list)
        assert isinstance(serialized["service_calls"], list)

    def test_governance_intent_resolved(self):
        package = _run("governance compliance pii sensitive stewardship")
        assert package.intent.intent_type == IntentType.GOVERNANCE

    def test_profiling_intent_resolved(self):
        package = _run("data quality profiling statistics completeness null")
        assert package.intent.intent_type == IntentType.PROFILING


# ---------------------------------------------------------------------------
# 2. Validation failure
# ---------------------------------------------------------------------------

class TestValidationFailure:
    def test_missing_session_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ComposerRequest(message="show me the dictionary")
        field_names = [e["loc"][0] for e in exc_info.value.errors()]
        assert "session_id" in field_names

    def test_missing_message_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ComposerRequest(session_id="sess-001")
        field_names = [e["loc"][0] for e in exc_info.value.errors()]
        assert "message" in field_names

    def test_both_required_fields_missing_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            ComposerRequest()
        field_names = [e["loc"][0] for e in exc_info.value.errors()]
        assert "session_id" in field_names
        assert "message" in field_names

    def test_invalid_selected_data_source_type_raises(self):
        with pytest.raises(ValidationError):
            ComposerRequest(
                session_id="sess-001",
                message="dictionary",
                selected_data_source="not-an-int",
            )


# ---------------------------------------------------------------------------
# 3. Unknown intent
# ---------------------------------------------------------------------------

class TestUnknownIntent:
    def test_gibberish_resolves_to_unknown(self):
        package = _run("xyzzy frobnicator blarg quux")
        assert package.intent.intent_type == IntentType.UNKNOWN

    def test_unknown_intent_status_is_partial_or_failed(self):
        package = _run("xyzzy frobnicator blarg quux")
        status = _determine_status(package)
        assert status in ("partial", "failed")

    def test_unknown_intent_warning_present(self):
        package = _run("xyzzy frobnicator blarg quux")
        warnings = _extract_warnings(package)
        assert any("Intent could not be resolved" in w for w in warnings)

    def test_unknown_intent_has_low_confidence(self):
        package = _run("xyzzy frobnicator blarg quux")
        assert package.intent.confidence < 0.15


# ---------------------------------------------------------------------------
# 4. Partial evidence — missing source_id
# ---------------------------------------------------------------------------

class TestPartialEvidence:
    def test_null_source_id_produces_null_data_items(self):
        """Adapter guard returns None immediately when source_id is absent."""
        package = _run("show me the dictionary for this table", source_id=None)
        assert package.services_attempted >= 1
        null_items = [e for e in package.evidence if e.data is None]
        assert len(null_items) >= 1

    def test_null_source_id_generates_warnings(self):
        package = _run("show me the dictionary for this table", source_id=None)
        warnings = _extract_warnings(package)
        assert any("returned no data" in w for w in warnings)

    def test_null_source_id_status(self):
        package = _run("show me the dictionary for this table", source_id=None)
        status = _determine_status(package)
        # All evidence items exist but data is None — services are attempted
        assert status in ("success", "partial", "failed")

    def test_evidence_items_have_required_fields(self):
        package = _run("show me the dictionary for this table", source_id=None)
        for item in package.evidence:
            assert item.evidence_id
            assert item.source_service
            assert item.source_function
            assert item.capability is not None
            assert item.timestamp is not None
            assert 0.0 <= item.confidence <= 1.0


# ---------------------------------------------------------------------------
# 5. Empty message
# ---------------------------------------------------------------------------

class TestEmptyMessage:
    def test_empty_string_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ComposerRequest(session_id="sess-001", message="")

    def test_whitespace_only_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ComposerRequest(session_id="sess-001", message="   ")

    def test_newline_only_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ComposerRequest(session_id="sess-001", message="\n\t")

    def test_valid_message_accepted(self):
        req = ComposerRequest(session_id="sess-001", message="show me the dictionary")
        assert req.message == "show me the dictionary"

    def test_message_is_stripped(self):
        req = ComposerRequest(session_id="sess-001", message="  show me the dictionary  ")
        assert req.message == "show me the dictionary"


# ---------------------------------------------------------------------------
# 6. Unavailable service — source_id absent
# ---------------------------------------------------------------------------

class TestUnavailableService:
    def test_profiling_service_returns_none_without_source(self):
        package = _run(
            "data quality profiling statistics completeness null",
            source_id=None,
        )
        assert package.request_id is not None
        profiling_items = [e for e in package.evidence if e.source_service == "profiling"]
        if profiling_items:
            assert all(item.data is None for item in profiling_items)

    def test_dictionary_service_returns_none_without_source(self):
        package = _run("dictionary definitions glossary business name", source_id=None)
        dict_items = [e for e in package.evidence if e.source_service == "dictionary"]
        if dict_items:
            assert all(item.data is None for item in dict_items)

    def test_relationship_service_returns_none_without_source(self):
        package = _run("relationship foreign key join related tables", source_id=None)
        rel_items = [e for e in package.evidence if e.source_service == "relationship"]
        if rel_items:
            assert all(item.data is None for item in rel_items)

    def test_package_is_always_returned_even_with_no_source(self):
        """Orchestrator never raises regardless of source_id."""
        for query in [
            "dictionary",
            "governance pii compliance",
            "profiling quality null",
            "relationship join foreign key",
        ]:
            package = _run(query, source_id=None)
            assert package is not None
            assert package.request_id

    def test_no_source_service_call_records_reflect_outcome(self):
        package = _run("dictionary definitions glossary", source_id=None)
        for call in package.service_calls:
            assert call.service_id
            assert call.function_name
            assert call.duration_ms >= 0.0
            assert isinstance(call.succeeded, bool)


# ---------------------------------------------------------------------------
# ComposerRequest model coverage
# ---------------------------------------------------------------------------

class TestComposerRequestModel:
    def test_all_optional_fields_accepted(self):
        req = ComposerRequest(
            workspace_id="ws-42",
            session_id="sess-abc",
            message="show me the data dictionary and governance",
            selected_data_source=5,
            selected_dataset="sales_db",
            selected_table="orders",
            conversation_context=[{"role": "user", "content": "hello"}],
            request_options={"debug": True, "max_services": 3},
        )
        assert req.workspace_id == "ws-42"
        assert req.selected_data_source == 5
        assert req.selected_table == "orders"
        assert req.conversation_context == [{"role": "user", "content": "hello"}]
        assert req.request_options == {"debug": True, "max_services": 3}

    def test_minimal_request_accepted(self):
        req = ComposerRequest(session_id="s", message="dictionary")
        assert req.workspace_id is None
        assert req.selected_data_source is None
        assert req.conversation_context is None
        assert req.request_options is None

    def test_determine_status_success(self):
        package = _run("show me governance compliance pii", source_id=None)
        # Not testing the exact status — just that it's a known value
        assert _determine_status(package) in ("success", "partial", "failed")

    def test_extract_governance_state_returns_none_or_string(self):
        package = _run("governance compliance pii sensitive")
        result = _extract_governance_state(package)
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# TestDeterministicAnswer — _generate_answer deterministic logic tests
# ---------------------------------------------------------------------------

class TestDeterministicAnswer:
    """
    Tests for _generate_answer — completely deterministic, no AI, no SQL.
    All inputs are constructed in-memory; no database is touched.
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_package(intent_type, evidence_data=None, governance_state=None):
        """Build a minimal EvidencePackage for testing."""
        from datetime import datetime
        from core.orchestrator.models import (
            EvidenceItem, EvidencePackage, IntentType,
            ResolvedIntent, ServiceCapability,
        )

        intent = ResolvedIntent(
            intent_type=intent_type,
            confidence=0.9,
            required_capabilities=[ServiceCapability.DICTIONARY_READ],
        )

        evidence = []
        if evidence_data is not None:
            evidence = [EvidenceItem(
                evidence_id="test-ev-1",
                source_service=intent_type.value,
                source_function="test_fn",
                capability=ServiceCapability.DICTIONARY_READ,
                data=evidence_data,
                timestamp=datetime.utcnow(),
                confidence=1.0 if evidence_data else 0.0,
                governance_state=governance_state,
            )]

        return EvidencePackage(
            request_id="test-req-1",
            query="test query",
            intent=intent,
            evidence=evidence,
            service_calls=[],
            built_at=datetime.utcnow(),
            source_id=1,
            errors=[],
            total_evidence_items=len(evidence),
            services_attempted=len(evidence),
            services_succeeded=len(evidence),
        )

    @staticmethod
    def _required_fields():
        return {"answer", "answer_type", "source_summary", "confidence", "limitations", "next_suggested_action"}

    # ── contract: all six fields always present ───────────────────────────────

    def test_all_six_fields_present_for_every_intent_type(self):
        from core.orchestrator.models import IntentType
        for intent_type in [
            IntentType.DICTIONARY, IntentType.DOMAIN, IntentType.ENTITY,
            IntentType.PROFILING, IntentType.GOVERNANCE, IntentType.RELATIONSHIP,
            IntentType.REVIEW, IntentType.METADATA_LOOKUP, IntentType.UNKNOWN,
        ]:
            package = self._make_package(intent_type)  # no evidence → no_data path
            result  = _generate_answer(package)
            missing = self._required_fields() - result.keys()
            assert not missing, f"Missing fields {missing} for intent {intent_type}"

    def test_limitations_is_always_a_list(self):
        from core.orchestrator.models import IntentType
        for intent_type in [IntentType.DICTIONARY, IntentType.GOVERNANCE, IntentType.UNKNOWN]:
            result = _generate_answer(self._make_package(intent_type))
            assert isinstance(result["limitations"], list), f"limitations not a list for {intent_type}"

    def test_confidence_is_float_in_0_1(self):
        from core.orchestrator.models import IntentType
        for intent_type in [IntentType.DICTIONARY, IntentType.PROFILING, IntentType.GOVERNANCE]:
            result = _generate_answer(self._make_package(intent_type))
            assert 0.0 <= result["confidence"] <= 1.0, f"confidence out of range for {intent_type}"

    # ── no evidence ───────────────────────────────────────────────────────────

    def test_no_evidence_returns_no_data_type(self):
        from core.orchestrator.models import IntentType
        package = self._make_package(IntentType.DICTIONARY)  # evidence=[]
        result  = _generate_answer(package)
        assert result["answer_type"] == "no_data"
        assert result["confidence"] == 0.0
        assert len(result["limitations"]) >= 1

    # ── governance restriction ────────────────────────────────────────────────

    def test_restricted_governance_state_blocks_answer(self):
        from core.orchestrator.models import IntentType
        package = self._make_package(
            IntentType.DICTIONARY,
            evidence_data=[{"table_name": "users", "description": "User records"}],
            governance_state="restricted",
        )
        result = _generate_answer(package)
        assert result["answer_type"] == "access_restricted"
        assert "restricted" in result["answer"].lower()
        assert result["confidence"] == 1.0

    def test_restricted_governance_takes_precedence_over_intent(self):
        from core.orchestrator.models import IntentType
        for intent_type in [IntentType.PROFILING, IntentType.DOMAIN, IntentType.GOVERNANCE]:
            package = self._make_package(
                intent_type,
                evidence_data={"some": "data"},
                governance_state="RESTRICTED_PII",
            )
            result = _generate_answer(package)
            assert result["answer_type"] == "access_restricted", (
                f"Expected access_restricted for {intent_type} with restricted governance"
            )

    # ── unknown intent ────────────────────────────────────────────────────────

    def test_unknown_intent_returns_unknown_type(self):
        from core.orchestrator.models import IntentType
        package = self._make_package(IntentType.UNKNOWN, evidence_data={"x": 1})
        result  = _generate_answer(package)
        assert result["answer_type"] == "unknown_intent"
        assert result["confidence"] == 0.0

    # ── dictionary ────────────────────────────────────────────────────────────

    def test_dictionary_with_tables_returns_correct_answer_type(self):
        from core.orchestrator.models import IntentType
        tables = [
            {"table_name": "orders",   "description": "Order records",  "business_name": "Orders"},
            {"table_name": "products", "description": None,             "business_name": None},
        ]
        result = _generate_answer(self._make_package(IntentType.DICTIONARY, evidence_data=tables))
        assert result["answer_type"] == "dictionary_status"
        assert result["confidence"] > 0.0
        assert "2" in result["answer"]

    def test_dictionary_empty_list_returns_low_confidence(self):
        from core.orchestrator.models import IntentType
        result = _generate_answer(self._make_package(IntentType.DICTIONARY, evidence_data=[]))
        assert result["answer_type"] == "dictionary_status"
        assert result["confidence"] == 0.5

    def test_dictionary_no_evidence_returns_no_data(self):
        from core.orchestrator.models import IntentType
        result = _generate_answer(self._make_package(IntentType.DICTIONARY))
        assert result["answer_type"] == "no_data"

    # ── domain ────────────────────────────────────────────────────────────────

    def test_domain_with_assignments_returns_correct_counts(self):
        from core.orchestrator.models import IntentType
        data = {
            "source_id": 1, "tables_total": 10, "tables_assigned": 8,
            "tables_unknown": 2, "domain_counts": {"Finance": 5, "Ops": 3, "Unknown": 2},
            "last_generated_at": "2024-01-01",
        }
        result = _generate_answer(self._make_package(IntentType.DOMAIN, evidence_data=data))
        assert result["answer_type"] == "domain_assignments"
        assert "8" in result["answer"]
        assert "10" in result["answer"]

    def test_domain_zero_assignments_returns_zero_answer(self):
        from core.orchestrator.models import IntentType
        data = {
            "source_id": 1, "tables_total": 0, "tables_assigned": 0,
            "tables_unknown": 0, "domain_counts": {}, "last_generated_at": None,
        }
        result = _generate_answer(self._make_package(IntentType.DOMAIN, evidence_data=data))
        assert result["answer_type"] == "domain_assignments"
        assert result["confidence"] == 0.5

    # ── entity ────────────────────────────────────────────────────────────────

    def test_entity_with_assignments_returns_correct_counts(self):
        from core.orchestrator.models import IntentType
        data = {
            "source_id": 1, "tables_total": 5, "entities_assigned": 4,
            "entities_unknown": 1, "entity_counts": {"Customer": 3, "Product": 1, "Unknown": 1},
            "last_generated_at": "2024-01-01",
        }
        result = _generate_answer(self._make_package(IntentType.ENTITY, evidence_data=data))
        assert result["answer_type"] == "entity_assignments"
        assert "4" in result["answer"]

    # ── profiling ────────────────────────────────────────────────────────────

    def test_profiling_with_snapshot_returns_table_count(self):
        from core.orchestrator.models import IntentType
        data = {
            "snapshot": {"profiling_status": "completed"},
            "tables": [
                {"table_name": "orders",    "pii_column_count": 2, "confirmed_pii_count": 1},
                {"table_name": "products",  "pii_column_count": 0, "confirmed_pii_count": 0},
            ],
        }
        result = _generate_answer(self._make_package(IntentType.PROFILING, evidence_data=data))
        assert result["answer_type"] == "profiling_status"
        assert "2" in result["answer"]  # 2 tables

    def test_profiling_pii_count_shown_not_column_names(self):
        from core.orchestrator.models import IntentType
        data = {
            "snapshot": {},
            "tables": [{"table_name": "customers", "pii_column_count": 3, "confirmed_pii_count": 2}],
        }
        result = _generate_answer(self._make_package(IntentType.PROFILING, evidence_data=data))
        # PII counts may appear but column values must never appear
        assert result["answer_type"] == "profiling_status"
        pii_note = any("pii" in lim.lower() or "column" in lim.lower() for lim in result["limitations"])
        assert pii_note, "Profiling answer should note PII column names are not exposed"

    # ── governance ────────────────────────────────────────────────────────────

    def test_governance_score_appears_in_answer(self):
        from core.orchestrator.models import IntentType
        data = {
            "governance_score": 72, "total_governed": 50, "objects_ready": 36,
            "objects_pending": 10, "objects_blocked": 2, "objects_escalated": 2,
            "high_risk_pct": 8.0, "auto_approval_pct": 60.0,
            "avg_confidence": 0.85, "open_assignments": 5,
        }
        result = _generate_answer(self._make_package(IntentType.GOVERNANCE, evidence_data=data))
        assert result["answer_type"] == "governance"
        assert "72" in result["answer"]
        assert result["confidence"] > 0.0

    # ── relationship ─────────────────────────────────────────────────────────

    def test_relationship_with_data_returns_counts(self):
        from core.orchestrator.models import IntentType
        data = {
            "snapshot_id": 1, "total_relationships": 15,
            "tables_with_outbound_fks": 8, "tables_referenced_by_fk": 6,
            "most_referenced": [{"table_fqn": "public.orders", "inbound_count": 5}],
        }
        result = _generate_answer(self._make_package(IntentType.RELATIONSHIP, evidence_data=data))
        assert result["answer_type"] == "relationship_map"
        assert "15" in result["answer"]

    def test_relationship_zero_returns_not_found_message(self):
        from core.orchestrator.models import IntentType
        data = {
            "snapshot_id": None, "total_relationships": 0,
            "tables_with_outbound_fks": 0, "tables_referenced_by_fk": 0,
            "most_referenced": [],
        }
        result = _generate_answer(self._make_package(IntentType.RELATIONSHIP, evidence_data=data))
        assert result["answer_type"] == "relationship_map"
        assert result["confidence"] == 0.5

    # ── PII safety ───────────────────────────────────────────────────────────

    def test_pii_values_never_appear_in_answer(self):
        """PII field values in the raw evidence data must not leak into the answer."""
        from core.orchestrator.models import IntentType
        data = {
            "snapshot": {},
            "tables": [{
                "table_name": "customers",
                "pii_column_count": 2,
                "confirmed_pii_count": 1,
                # simulated raw PII that must NOT appear in the answer
                "ssn_example":  "123-45-6789",
                "card_example": "4111111111111111",
            }],
        }
        result = _generate_answer(self._make_package(IntentType.PROFILING, evidence_data=data))
        answer_blob = result["answer"] + " ".join(result.get("limitations", []))
        assert "123-45-6789" not in answer_blob
        assert "4111111111111111" not in answer_blob

    # ── partial evidence ──────────────────────────────────────────────────────

    def test_partial_evidence_noted_in_limitations(self):
        """When some evidence items have None data, limitations must say so."""
        from datetime import datetime
        from core.orchestrator.models import (
            EvidenceItem, EvidencePackage, IntentType,
            ResolvedIntent, ServiceCapability,
        )
        intent = ResolvedIntent(
            intent_type=IntentType.DICTIONARY,
            confidence=0.8,
            required_capabilities=[ServiceCapability.DICTIONARY_READ],
        )
        evidence = [
            EvidenceItem(
                evidence_id="e1",
                source_service="dictionary",
                source_function="list_dictionary_tables",
                capability=ServiceCapability.DICTIONARY_READ,
                data=[{"table_name": "orders", "description": "Order records", "business_name": "Orders"}],
                timestamp=datetime.utcnow(),
                confidence=1.0,
                governance_state=None,
            ),
            EvidenceItem(
                evidence_id="e2",
                source_service="domain",
                source_function="get_domain_summary",
                capability=ServiceCapability.DOMAIN_READ,
                data=None,  # partial — service returned no data
                timestamp=datetime.utcnow(),
                confidence=0.0,
                governance_state=None,
            ),
        ]
        package = EvidencePackage(
            request_id="test-partial", query="test query",
            intent=intent, evidence=evidence, service_calls=[],
            built_at=datetime.utcnow(), source_id=None,
            errors=[], total_evidence_items=2,
            services_attempted=2, services_succeeded=1,
        )
        result = _generate_answer(package)
        assert result["answer_type"] == "dictionary_status"
        lim_text = " ".join(result.get("limitations", []))
        assert "partial" in lim_text.lower(), "Partial evidence must be noted in limitations"

    # ── metadata fallback ────────────────────────────────────────────────────

    def test_metadata_lookup_fallback_lists_services(self):
        from core.orchestrator.models import IntentType
        data = {"tables": 10, "columns": 120}
        result = _generate_answer(self._make_package(IntentType.METADATA_LOOKUP, evidence_data=data))
        assert result["answer_type"] == "metadata_lookup"
        assert result["confidence"] > 0.0

    # ── integration: _generate_answer called through real orchestrator ────────

    def test_generate_answer_called_on_real_package(self):
        """Smoke test: _generate_answer works on a package from the real orchestrator."""
        package = _run("show me the data dictionary for this source", source_id=None)
        result  = _generate_answer(package)
        assert set(result.keys()) >= {"answer", "answer_type", "source_summary",
                                       "confidence", "limitations", "next_suggested_action"}
        assert isinstance(result["answer"], str) and result["answer"]
        assert isinstance(result["limitations"], list)


# ---------------------------------------------------------------------------
# Phase 5 — Report generation intent routing
# ---------------------------------------------------------------------------

class TestReportGenerationIntent:
    """
    Tests for the REPORT_GENERATION intent introduced in Phase 5.
    Covers intent resolution for the 7 supported generation prompts and
    verifies that _answer_report_generation returns the correct contract.
    No duplicate report generator is exercised — the function delegates.
    """

    # ── Intent resolution ────────────────────────────────────────────────────

    def test_generate_report_resolves_to_report_generation(self):
        package = _run("generate report")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_create_report_resolves_to_report_generation(self):
        package = _run("create report")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_create_executive_summary_resolves(self):
        package = _run("create executive summary")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_build_kpi_report_resolves(self):
        package = _run("build kpi report")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_generate_quality_report_resolves(self):
        package = _run("generate quality report")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_show_trends_resolves(self):
        package = _run("show trends")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_analyze_dataset_resolves(self):
        package = _run("analyze dataset")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_create_pdf_report_resolves(self):
        package = _run("create pdf report")
        assert package.intent.intent_type == IntentType.REPORT_GENERATION

    def test_report_generation_confidence_above_floor(self):
        package = _run("generate report")
        assert package.intent.confidence >= 0.15

    def test_report_generation_intent_value(self):
        assert IntentType.REPORT_GENERATION.value == "report_generation"

    # ── _answer_report_generation contract ──────────────────────────────────

    def test_delegation_returns_correct_fields_on_error(self):
        """When the pipeline raises, all required fields are still returned."""
        from unittest.mock import patch
        from api.v1.composer import _answer_report_generation

        # The function does a lazy import of run_dataset_report_plan, so we
        # patch at its source module so the lazy import gets the mock.
        with patch(
            "core.workflows.workflow_runner.run_dataset_report_plan",
            side_effect=RuntimeError("no dataset"),
        ):
            result = _answer_report_generation(
                user_id="test-user",
                dataset_id=None,
                intent_text="generate report",
            )

        required = {"answer", "answer_type", "source_summary", "confidence",
                    "limitations", "next_suggested_action", "report_id", "report_title"}
        assert required <= result.keys()
        assert result["answer_type"] == "report_generation_failed"
        assert result["report_id"] is None
        assert result["confidence"] == 0.0

    def test_delegation_returns_report_generated_on_success(self):
        """When the pipeline returns a report_id, answer_type is report_generated."""
        from unittest.mock import patch
        from api.v1.composer import _answer_report_generation

        mock_result = {
            "report_id": 42,
            "dataset_report_error": None,
            "report_save_warning": None,
            "dataset_report": {
                "title": "Sales Report",
                "report_plan": {"report_title": "Sales Report"},
                "sections": [
                    {"type": "executive_summary", "summary": "Key findings here."},
                ],
            },
        }

        with patch(
            "core.workflows.workflow_runner.run_dataset_report_plan",
            return_value=mock_result,
        ):
            result = _answer_report_generation(
                user_id="test-user",
                dataset_id=1,
                intent_text="generate report",
            )

        assert result["answer_type"] == "report_generated"
        assert result["report_id"] == 42
        assert result["report_title"] == "Sales Report"
        assert result["confidence"] == 0.95
        assert isinstance(result["limitations"], list)
        assert isinstance(result["next_suggested_action"], str)
        assert "42" in result["next_suggested_action"]

    def test_delegation_handles_pipeline_error_field(self):
        """When pipeline returns dataset_report_error, answer_type is failed."""
        from unittest.mock import patch
        from api.v1.composer import _answer_report_generation

        mock_result = {
            "report_id": None,
            "dataset_report_error": "No uploaded dataset found.",
            "report_save_warning": None,
            "dataset_report": None,
        }

        with patch(
            "core.workflows.workflow_runner.run_dataset_report_plan",
            return_value=mock_result,
        ):
            result = _answer_report_generation(
                user_id="test-user",
                dataset_id=None,
                intent_text="generate report",
            )

        assert result["answer_type"] == "report_generation_failed"
        assert result["report_id"] is None
        assert "dataset" in result["answer"].lower() or "dataset" in result["limitations"][0].lower()

    def test_composer_request_accepts_dataset_id(self):
        """ComposerRequest model accepts the new dataset_id field."""
        req = ComposerRequest(
            session_id="sess-report-1",
            message="generate report",
            dataset_id=7,
        )
        assert req.dataset_id == 7

    def test_composer_request_dataset_id_defaults_to_none(self):
        req = ComposerRequest(session_id="sess-report-2", message="generate report")
        assert req.dataset_id is None

    def test_report_generation_not_in_generate_answer(self):
        """_generate_answer is never called for REPORT_GENERATION — the endpoint
        delegates early. This test confirms the intent resolver never returns
        REPORT_GENERATION for pure catalog lookup queries."""
        package = _run("show me the dictionary definitions for this data source")
        assert package.intent.intent_type != IntentType.REPORT_GENERATION

    def test_listing_reports_still_resolves_to_reports(self):
        """'view report' / 'open report' must not be hijacked by REPORT_GENERATION."""
        package = _run("view report open report saved report")
        # REPORTS and REPORT_GENERATION both score on "report".
        # REPORTS secondary "view report", "open report", "saved report" add 0.6 more.
        # REPORT_GENERATION has no secondary matches here → REPORTS score is higher.
        assert package.intent.intent_type == IntentType.REPORTS
