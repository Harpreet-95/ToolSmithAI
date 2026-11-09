"""
Focused unit tests -- SQL Architecture Correction, Phase 6, Step 6C, Parts
4/5/6/8: the deep VIEW_ENRICHED contract itself (structured output,
strict validation, cache identity/invalidation, batching, bounded
concurrency, per-batch failure isolation, resumability) --
core/sqlservice/semantic_enrichment_service.py's own VIEW_ENRICHED section.

Pure/offline: no database connection, no real OpenAI call, no live SQL
Server, no network of any kind. Every test injects a FakeSemanticEnrichment
Provider defined in this file.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from core.sqlservice.semantic_enrichment_providers import (
    ProviderSemanticEnrichmentResponse,
    SemanticEnrichmentProvider,
)
from core.sqlservice.semantic_enrichment_service import (
    DIAGNOSTIC_ACTION_BATCH_REJECTED,
    DIAGNOSTIC_ACTION_CLAIM_DROPPED,
    DIAGNOSTIC_ACTION_NORMALIZED,
    DIAGNOSTIC_ACTION_VIEW_REJECTED,
    MODEL_INFERRED_CONFIDENCE_CAP,
    PHASE_VIEW_ENRICHED,
    SKIP_REASON_BATCH_VALIDATION_FAILED,
    SKIP_REASON_INPUT_TOO_LARGE,
    SKIP_REASON_OUTPUT_BUDGET_EXCEEDED,
    SKIP_REASON_PARTIAL_COVERAGE,
    SKIP_REASON_TRUNCATED_BY_MAX_TOTAL_OBJECTS,
    SOURCE_ROLE_AUTHORITATIVE,
    SOURCE_ROLE_CURRENT_STATE,
    STATUS_OK,
    VIEW_ENRICHMENT_CONTRACT_VERSION,
    ColumnSemanticInput,
    EnrichedColumnSemantics,
    EnrichedViewObjectSemantics,
    SemanticEnrichmentBatchLimitError,
    SemanticEnrichmentCacheKey,
    SemanticEnrichmentUnsupportedMetadataError,
    SemanticEnrichmentValidationError,
    ViewEnrichmentBatchItem,
    ViewSemanticInput,
    compute_view_cache_key,
    view_batch_estimated_output_tokens,
    compute_view_semantic_input_hash,
    generate_view_semantic_enrichment,
)


class FakeSemanticEnrichmentProvider(SemanticEnrichmentProvider):
    """Test double -- never calls a real API. `responses` is either one raw
    JSON-shaped mapping (reused for every batch) or a list consumed one
    response per enrich_batch() call, in order."""

    def __init__(self, responses, *, provider_id: str = "fake", model: str = "fake-model-1"):
        self._provider_id = provider_id
        self._model = model
        self._queue = list(responses) if isinstance(responses, list) else None
        self._single = None if self._queue is not None else responses
        self.calls: list[tuple[str, str]] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model(self) -> str:
        return self._model

    def enrich_batch(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append((system_prompt, user_prompt))
        raw = self._queue.pop(0) if self._queue is not None else self._single
        if isinstance(raw, Exception):
            raise raw
        return ProviderSemanticEnrichmentResponse(raw=raw, model=self._model)


def _make_view_input(fqn: str = "dbo.ActiveStudents", **overrides) -> ViewSemanticInput:
    kwargs = dict(
        object_fqn=fqn,
        schema_name="dbo",
        view_name=fqn.split(".")[-1],
        retrieval_tier=1,
        columns=(
            ColumnSemanticInput("StudentId", "INTEGER", True, False),
            ColumnSemanticInput("EnrollmentStatus", "TEXT", False, False),
            ColumnSemanticInput("EnrolledDate", "DATETIME", False, False),
        ),
        primary_key_columns=("StudentId",),
        dictionary_business_name=None,
        dictionary_description=None,
        dictionary_synonyms=(),
        definition_evidence="SELECT StudentId, EnrollmentStatus, EnrolledDate FROM dbo.Students WHERE Active = 1",
        definition_truncated=False,
        dependency_edges=(),
        confirmed_relationship_summary=(),
    )
    kwargs.update(overrides)
    return ViewSemanticInput(**kwargs)


def _view_item(view_id: str, **overrides) -> dict:
    return {
        "id": view_id,
        "business_domain": overrides.get("business_domain", "Enrollment"),
        "business_name": overrides.get("business_name", "Active Students"),
        "description": overrides.get("description", "Students currently enrolled and active."),
        "source_role": overrides.get("source_role", SOURCE_ROLE_CURRENT_STATE),
        "entity_concepts": overrides.get("entity_concepts", ["student"]),
        "status_concepts": overrides.get("status_concepts", [{"column": "EnrollmentStatus", "label": "Active"}]),
        "aliases": overrides.get("aliases", ["current students"]),
        "operations": overrides.get("operations", ["count active students"]),
        "confidence": overrides.get("confidence", 0.8),
        "columns": overrides.get("columns", [
            {
                "column": "EnrollmentStatus", "label": "Enrollment status", "semantic_role": "STATUS",
                "date_role": None, "measure_role": None, "dimension_role": "STATUS", "identifier_role": None,
                "confidence": 0.75,
            },
            {
                "column": "EnrolledDate", "label": "Enrollment date", "semantic_role": "DATE",
                "date_role": "STARTED", "measure_role": None, "dimension_role": None, "identifier_role": None,
                "confidence": 0.7,
            },
        ]),
    }


def _run(view_inputs, response, **kwargs):
    provider = FakeSemanticEnrichmentProvider(response)
    result = generate_view_semantic_enrichment(list(view_inputs), lambda: provider, **kwargs)
    return result, provider


# ── Part 4/5: complete valid profile, validation, trust policy ────────────


def test_complete_valid_semantic_profile_persists_all_fields():
    view_input = _make_view_input()
    response = {"views": [_view_item("v1")]}
    result, _provider = _run([view_input], response)

    assert result.status == STATUS_OK
    assert result.generated_count == 1
    assert len(result.enriched_views) == 1
    view = result.enriched_views[0]
    assert isinstance(view, EnrichedViewObjectSemantics)
    assert view.object_fqn == "dbo.ActiveStudents"
    assert view.business_domain == "Enrollment"
    assert view.source_role == SOURCE_ROLE_CURRENT_STATE
    assert view.confidence <= MODEL_INFERRED_CONFIDENCE_CAP
    assert view.status_concepts[0].column_name == "EnrollmentStatus"
    assert len(result.enriched_columns) == 2
    assert {c.column_name for c in result.enriched_columns} == {"EnrollmentStatus", "EnrolledDate"}
    assert all(isinstance(c, EnrichedColumnSemantics) for c in result.enriched_columns)


def test_unknown_column_reference_is_dropped_not_fatal():
    view_input = _make_view_input()
    response = {
        "views": [
            _view_item(
                "v1",
                status_concepts=[{"column": "NotARealColumn", "label": "bogus"}],
                columns=[
                    {
                        "column": "AlsoFake", "label": "x", "semantic_role": "OTHER",
                        "date_role": None, "measure_role": None, "dimension_role": None, "identifier_role": None,
                        "confidence": 0.5,
                    },
                ],
            )
        ]
    }
    result, _provider = _run([view_input], response)
    assert result.status == STATUS_OK
    assert result.generated_count == 1  # the view profile itself still persists
    assert result.enriched_views[0].status_concepts == ()
    assert result.enriched_columns == ()
    codes = {d.code for d in result.diagnostics}
    assert "UNKNOWN_STATUS_COLUMN_REFERENCE" in codes
    assert "UNKNOWN_COLUMN_REFERENCE" in codes
    assert all(d.action == DIAGNOSTIC_ACTION_CLAIM_DROPPED for d in result.diagnostics)


def test_invented_object_unknown_local_id_rejects_whole_batch():
    view_input = _make_view_input()
    response = {"views": [_view_item("v_does_not_exist")]}
    result, _provider = _run([view_input], response)
    assert result.generated_count == 0
    assert result.failed_batch_count == 1
    assert result.skipped_objects[0].reason == SKIP_REASON_BATCH_VALIDATION_FAILED


def test_duplicate_concepts_and_aliases_deduplicated_case_insensitively():
    view_input = _make_view_input()
    response = {"views": [_view_item("v1", entity_concepts=["Student", "student", "STUDENT", "learner"])]}
    result, _provider = _run([view_input], response)
    assert result.enriched_views[0].entity_concepts == ("Student", "learner")


def test_duplicate_status_concepts_same_column_keep_first():
    view_input = _make_view_input()
    response = {
        "views": [
            _view_item(
                "v1",
                status_concepts=[
                    {"column": "EnrollmentStatus", "label": "First"},
                    {"column": "enrollmentstatus", "label": "Second"},
                ],
            )
        ]
    }
    result, _provider = _run([view_input], response)
    assert len(result.enriched_views[0].status_concepts) == 1
    assert result.enriched_views[0].status_concepts[0].label == "First"


def test_all_semantic_field_limits_enforced():
    """Phase 6 Step 6F, Part 3: an invalid REQUIRED view-level field now
    rejects only that one view, never the whole batch."""
    view_input = _make_view_input()
    too_many_concepts = [f"concept{i}" for i in range(50)]
    response = {"views": [_view_item("v1", entity_concepts=too_many_concepts)]}
    result, _provider = _run([view_input], response)
    assert result.failed_batch_count == 0
    assert result.successful_batch_count == 1
    assert result.generated_count == 0
    assert result.skipped_objects[0].reason == SKIP_REASON_PARTIAL_COVERAGE
    diag = next(d for d in result.diagnostics if d.code == "INVALID_ENTITY_CONCEPTS")
    assert diag.action == DIAGNOSTIC_ACTION_VIEW_REJECTED
    assert diag.local_id == "v1"
    assert diag.object_fqn == "dbo.ActiveStudents"


def test_non_finite_confidence_rejected():
    view_input = _make_view_input()
    response = {"views": [_view_item("v1", confidence=float("nan"))]}
    result, _provider = _run([view_input], response)
    assert result.failed_batch_count == 0
    assert result.generated_count == 0
    diag = next(d for d in result.diagnostics if d.code == "INVALID_CONFIDENCE")
    assert diag.action == DIAGNOSTIC_ACTION_VIEW_REJECTED


def test_invalid_source_role_rejected():
    """A single view's invalid source_role rejects only that view -- a
    valid sibling view in the same batch/response still persists."""
    v1 = _make_view_input("dbo.Alpha")
    v2 = _make_view_input("dbo.Beta")
    response = {"views": [
        _view_item("v1", source_role="NOT_A_REAL_ROLE"),
        _view_item("v2"),
    ]}
    result, _provider = _run([v1, v2], response)
    assert result.failed_batch_count == 0
    assert result.generated_count == 1
    assert result.enriched_views[0].object_fqn == "dbo.Beta"
    diagnostic = next(d for d in result.diagnostics if d.code == "INVALID_SOURCE_ROLE")
    assert diagnostic.action == DIAGNOSTIC_ACTION_VIEW_REJECTED
    assert diagnostic.field_path == "$.views[0].source_role"
    assert diagnostic.local_id == "v1"
    assert diagnostic.object_fqn == "dbo.Alpha"


def test_field_set_diagnostic_is_safe_and_does_not_echo_unexpected_key(caplog):
    view_input = _make_view_input()
    item = _view_item("v1")
    secret_key = "sk-fake-secret-unexpected-field"
    item[secret_key] = "fake-password"

    with caplog.at_level(logging.INFO, logger="core.sqlservice.semantic_enrichment_service"):
        result, _provider = _run([view_input], {"views": [item]})

    assert result.failed_batch_count == 0  # a known, valid id -> view-level only
    diagnostic = next(d for d in result.diagnostics if d.code == "VIEW_FIELD_SET_MISMATCH")
    assert diagnostic.action == DIAGNOSTIC_ACTION_VIEW_REJECTED
    assert diagnostic.unexpected_field_count == 1
    assert secret_key not in repr(diagnostic)
    assert secret_key not in result.warnings
    assert "fake-password" not in caplog.text
    assert secret_key not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None
        assert record.exc_text is None


def test_successful_batch_has_no_validation_diagnostic():
    result, _provider = _run([_make_view_input()], {"views": [_view_item("v1")]})
    assert result.batch_outcomes[0].diagnostics == ()
    assert result.diagnostics == ()


def test_percentage_measure_role_is_canonicalized_to_rate():
    columns = _view_item("v1")["columns"]
    columns[0] = {**columns[0], "semantic_role": "MEASURE", "measure_role": "PERCENTAGE"}
    result, _provider = _run(
        [_make_view_input()], {"views": [_view_item("v1", columns=columns)]},
    )

    assert result.failed_batch_count == 0
    assert result.generated_count == 1
    enrollment_status = next(c for c in result.enriched_columns if c.column_name == "EnrollmentStatus")
    assert enrollment_status.measure_role == "RATE"


def test_unknown_measure_role_remains_rejected():
    """Phase 6 Step 6F, Part 3: an invalid column-level enum value drops
    only that ONE claim -- the view still persists without it."""
    columns = _view_item("v1")["columns"]
    columns[0] = {**columns[0], "semantic_role": "MEASURE", "measure_role": "NOT_A_MEASURE_ROLE"}
    result, _provider = _run(
        [_make_view_input()], {"views": [_view_item("v1", columns=columns)]},
    )

    assert result.failed_batch_count == 0
    assert result.generated_count == 1
    diag = next(d for d in result.diagnostics if d.code == "INVALID_MEASURE_ROLE")
    assert diag.action == DIAGNOSTIC_ACTION_CLAIM_DROPPED
    assert columns[0]["column"] not in {c.column_name for c in result.enriched_columns}


# ── Part 2: controlled vocabulary normalization ──────────────────────────


@pytest.mark.parametrize("alias_value,canonical", [
    ("PERCENTAGE", "RATE"), ("PERCENT", "RATE"),
    ("REGION", "LOCATION"), ("GEOGRAPHY", "LOCATION"), ("GEOGRAPHIC", "LOCATION"),
    ("BOOLEAN", "FLAG"), ("YES_NO", "FLAG"), ("BINARY_FLAG", "FLAG"),
    ("CLASSIFICATION", "CATEGORY"), ("CLASS", "CATEGORY"),
])
def test_documented_aliases_canonicalize_to_toolsmith_vocabulary(alias_value, canonical):
    columns = _view_item("v1")["columns"]
    role_field = "measure_role" if canonical == "RATE" else "dimension_role"
    columns[0] = {**columns[0], "semantic_role": "MEASURE" if canonical == "RATE" else "DIMENSION", role_field: alias_value}
    result, _provider = _run([_make_view_input()], {"views": [_view_item("v1", columns=columns)]})

    assert result.failed_batch_count == 0
    assert result.generated_count == 1
    stored_col = next(c for c in result.enriched_columns if c.column_name == columns[0]["column"])
    assert getattr(stored_col, role_field) == canonical
    norm_diag = next(d for d in result.diagnostics if d.action == DIAGNOSTIC_ACTION_NORMALIZED)
    assert norm_diag.alias_applied == f"{alias_value}->{canonical}"


@pytest.mark.parametrize("ambiguous_value", ["VALUE", "LEVEL", "GROUP", "STATE", "RESULT"])
def test_ambiguous_terms_are_never_auto_mapped_and_remain_rejected(ambiguous_value):
    columns = _view_item("v1")["columns"]
    columns[0] = {**columns[0], "semantic_role": "DIMENSION", "dimension_role": ambiguous_value}
    result, _provider = _run([_make_view_input()], {"views": [_view_item("v1", columns=columns)]})

    assert result.failed_batch_count == 0
    assert result.generated_count == 1  # the view still persists -- only this one claim is dropped
    diag = next(d for d in result.diagnostics if d.code == "INVALID_DIMENSION_ROLE")
    assert diag.action == DIAGNOSTIC_ACTION_CLAIM_DROPPED
    assert diag.alias_applied is None
    assert columns[0]["column"] not in {c.column_name for c in result.enriched_columns}


@pytest.mark.parametrize("value", [" RATE ", "rate", "Rate", "rAtE"])
def test_casing_and_whitespace_normalized_before_enum_validation(value):
    columns = _view_item("v1")["columns"]
    columns[0] = {**columns[0], "semantic_role": "MEASURE", "measure_role": value}
    result, _provider = _run([_make_view_input()], {"views": [_view_item("v1", columns=columns)]})

    assert result.failed_batch_count == 0
    assert result.generated_count == 1
    stored_col = next(c for c in result.enriched_columns if c.column_name == columns[0]["column"])
    assert stored_col.measure_role == "RATE"


def test_required_enum_fields_also_normalized_for_casing():
    """source_role has no documented alias, but STILL receives casing/
    whitespace normalization (Part 2 applies to every enum field, not just
    ones with a synonym table)."""
    response = {"views": [_view_item("v1", source_role=" current_state_view ")]}
    result, _provider = _run([_make_view_input()], response)
    assert result.generated_count == 1
    assert result.enriched_views[0].source_role == SOURCE_ROLE_CURRENT_STATE


def test_diagnostics_collection_is_bounded():
    """Part 3: 'Warnings and diagnostic collections must remain bounded.'"""
    from core.config import SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_WARNINGS

    columns = _view_item("v1")["columns"]
    columns[0] = {**columns[0], "semantic_role": "MEASURE", "measure_role": "NOT_VALID"}
    many_views = []
    for i in range(SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_WARNINGS + 20):
        many_views.append(_make_view_input(f"dbo.V{i}"))
    response_items = [
        {"views": [_view_item(f"v1", columns=columns)]} for _ in many_views
    ]
    result, _provider = _run(many_views, response_items, max_objects_per_batch=1)
    assert len(result.diagnostics) <= SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_WARNINGS


def test_confidence_capped_at_model_inferred_ceiling():
    view_input = _make_view_input()
    response = {"views": [_view_item("v1", confidence=0.99)]}
    result, _provider = _run([view_input], response)
    assert result.enriched_views[0].confidence == MODEL_INFERRED_CONFIDENCE_CAP


def test_result_and_view_object_are_immutable():
    view_input = _make_view_input()
    response = {"views": [_view_item("v1")]}
    result, _provider = _run([view_input], response)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.generated_count = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.enriched_views[0].confidence = 0.1  # type: ignore[misc]


def test_direct_construction_of_enriched_view_object_semantics_is_rejected():
    from core.sqlservice.semantic_enrichment_service import SemanticEnrichmentConstructionRejected
    with pytest.raises(SemanticEnrichmentConstructionRejected):
        EnrichedViewObjectSemantics(
            object_fqn="dbo.X", business_domain="d", business_name="n", description="d",
            source_role=SOURCE_ROLE_AUTHORITATIVE, entity_concepts=(), status_concepts=(),
            aliases=(), supported_operations=(), confidence=0.1, provenance=(),
            _gate_guard=object(),  # type: ignore[arg-type]
        )


def test_deterministic_ordering_of_results():
    inputs = [_make_view_input("dbo.Zeta"), _make_view_input("dbo.Alpha")]
    response = [
        {"views": [_view_item("v1", business_name="Zeta view")]},
        {"views": [_view_item("v1", business_name="Alpha view")]},
    ]
    result, _provider = _run(inputs, response, max_objects_per_batch=1)
    assert [v.object_fqn for v in result.enriched_views] == ["dbo.Alpha", "dbo.Zeta"]


def test_no_reference_specific_tokens_in_new_view_enrichment_code():
    """Scoped to the NEW code this milestone adds (the VIEW_ENRICHED section
    of semantic_enrichment_service.py plus the three new view_semantic_
    enrichment_* modules) -- not the whole shared file, which already
    contains a pre-existing, out-of-scope docstring reference to a prior
    step's own real-database audit (unrelated to this milestone; not
    touched here per the "no unrelated cleanup" restriction)."""
    import inspect
    import core.sqlservice.semantic_enrichment_service as service_mod
    import core.sqlservice.view_semantic_enrichment_selection as selection_mod
    import core.sqlservice.view_semantic_enrichment_input as input_mod
    import core.sqlservice.view_semantic_enrichment_catalog_adapter as adapter_mod

    service_source = inspect.getsource(service_mod)
    marker = "PHASE_VIEW_ENRICHED: deep, VIEW-ONLY business semantics"
    view_section = service_source[service_source.index(marker):]
    assert "exampleco" not in view_section.lower()
    for mod in (selection_mod, input_mod, adapter_mod):
        assert "exampleco" not in inspect.getsource(mod).lower()


# ── Part 6: cache identity / invalidation proofs ───────────────────────────


def test_unchanged_view_rerun_makes_zero_provider_calls():
    view_input = _make_view_input()
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    first = generate_view_semantic_enrichment([view_input], lambda: provider)
    assert first.generated_count == 1
    assert len(provider.calls) == 1

    already_cached = frozenset(first.cache_keys_by_object_fqn.values())
    second = generate_view_semantic_enrichment([view_input], lambda: provider, already_cached_keys=already_cached)
    assert second.reused_count == 1
    assert second.generated_count == 0
    assert len(provider.calls) == 1  # no new call


def test_changed_definition_invalidates_only_that_view():
    v1 = _make_view_input("dbo.V1")
    v2 = _make_view_input("dbo.V2")
    key1 = compute_view_cache_key(v1, "fake", "fake-model-1")
    key2 = compute_view_cache_key(v2, "fake", "fake-model-1")

    v1_changed = dataclasses.replace(v1, definition_evidence="SELECT * FROM dbo.SomethingElse")
    key1_changed = compute_view_cache_key(v1_changed, "fake", "fake-model-1")

    assert key1_changed.semantic_input_hash != key1.semantic_input_hash
    assert key2.semantic_input_hash == compute_view_cache_key(v2, "fake", "fake-model-1").semantic_input_hash


def test_changed_dependency_list_invalidates_only_that_view():
    from core.sqlservice.semantic_enrichment_service import ViewDependencyEdgeInput, DEPENDENCY_TRUST_CATALOG_CONFIRMED
    v1 = _make_view_input("dbo.V1")
    v1_with_dep = dataclasses.replace(
        v1,
        dependency_edges=(ViewDependencyEdgeInput("dbo.Other", DEPENDENCY_TRUST_CATALOG_CONFIRMED, "catalog_view_dependency"),),
    )
    assert compute_view_semantic_input_hash(v1) != compute_view_semantic_input_hash(v1_with_dep)


def test_changed_columns_invalidates_only_that_view():
    v1 = _make_view_input("dbo.V1")
    v1_more_cols = dataclasses.replace(
        v1, columns=v1.columns + (ColumnSemanticInput("NewCol", "TEXT", False, False),),
    )
    assert compute_view_semantic_input_hash(v1) != compute_view_semantic_input_hash(v1_more_cols)


def test_unrelated_table_change_never_appears_in_any_view_hash():
    """A ViewSemanticInput has no field derived from any OTHER object's
    schema -- structurally, an unrelated table's column change cannot
    reach this hash at all (proven by field enumeration, not merely by
    example)."""
    v1 = _make_view_input("dbo.V1")
    field_names = {f.name for f in dataclasses.fields(v1)}
    assert field_names == {
        "object_fqn", "schema_name", "view_name", "retrieval_tier", "columns",
        "primary_key_columns", "dictionary_business_name", "dictionary_description",
        "dictionary_synonyms", "definition_evidence", "definition_truncated",
        "dependency_edges", "confirmed_relationship_summary",
    }


def test_contract_version_bump_invalidates_old_payloads_safely():
    view_input = _make_view_input()
    real_hash = compute_view_semantic_input_hash(view_input)
    stale_key = SemanticEnrichmentCacheKey(
        object_fqn=view_input.object_fqn, semantic_input_hash=real_hash,
        provider_id="fake", model="fake-model-1", contract_version=999, phase=PHASE_VIEW_ENRICHED,
    )
    result, _provider = _run([view_input], {"views": [_view_item("v1")]}, already_cached_keys=frozenset({stale_key}))
    assert result.reused_count == 0
    assert result.generated_count == 1  # a mismatched contract_version never satisfies the lookup


def test_view_cache_key_never_collides_with_table_phases():
    view_input = _make_view_input()
    key = compute_view_cache_key(view_input, "fake", "fake-model-1")
    assert key.phase == PHASE_VIEW_ENRICHED
    assert key.contract_version == VIEW_ENRICHMENT_CONTRACT_VERSION


# ── Part 8: batching / failure isolation / resumability ────────────────────


def test_per_view_within_batch_and_per_batch_failure_isolation():
    # generate_view_semantic_enrichment sorts inputs by object_fqn.lower()
    # BEFORE batching, so "dbo.AGood" (batch 0) precedes "dbo.ZBadBatch"
    # (batch 1) regardless of the order passed in here.
    v1 = _make_view_input("dbo.AGood")
    v2 = _make_view_input("dbo.ZBadBatch")
    responses = [
        {"views": [_view_item("v1", business_name="Good view")]},  # batch 0 (dbo.AGood)
        {"malformed": "response"},  # batch 1 (dbo.ZBadBatch) -- fails validation
    ]
    result, _provider = _run([v1, v2], responses, max_objects_per_batch=1)
    assert result.generated_count == 1
    assert result.enriched_views[0].object_fqn == "dbo.AGood"
    assert result.failed_batch_count == 1
    assert result.successful_batch_count == 1
    assert any(s.object_fqn == "dbo.ZBadBatch" for s in result.skipped_objects)


def test_provider_call_exception_isolated_to_its_batch():
    v1 = _make_view_input("dbo.Ok")
    v2 = _make_view_input("dbo.Boom")
    responses = [{"views": [_view_item("v1")]}, RuntimeError("simulated provider outage")]
    result, _provider = _run([v1, v2], responses, max_objects_per_batch=1)
    assert result.generated_count == 1
    assert result.failed_batch_count == 1


def test_resumability_only_missing_views_are_re_enriched():
    v1 = _make_view_input("dbo.V1")
    v2 = _make_view_input("dbo.V2")
    provider = FakeSemanticEnrichmentProvider([
        {"views": [_view_item("v1", business_name="V1")]},
        {"views": [_view_item("v1", business_name="V2")]},
    ])
    first = generate_view_semantic_enrichment([v1, v2], lambda: provider, max_objects_per_batch=1)
    assert first.generated_count == 2
    assert len(provider.calls) == 2

    already_cached = frozenset({first.cache_keys_by_object_fqn["dbo.V1"]})
    provider2 = FakeSemanticEnrichmentProvider([{"views": [_view_item("v1", business_name="V2 again")]}])
    second = generate_view_semantic_enrichment(
        [v1, v2], lambda: provider2, already_cached_keys=already_cached, max_objects_per_batch=1,
    )
    assert second.reused_count == 1
    assert second.generated_count == 1
    assert second.enriched_views[0].object_fqn == "dbo.V2"
    assert len(provider2.calls) == 1


def test_max_total_views_truncation_reported():
    inputs = [_make_view_input(f"dbo.V{i}") for i in range(3)]
    response = {"views": [_view_item("v1")]}
    result, _provider = _run(inputs, response, max_total_objects=1)
    assert result.skipped_count == 2
    assert all(s.reason == SKIP_REASON_TRUNCATED_BY_MAX_TOTAL_OBJECTS for s in result.skipped_objects)


def test_single_oversized_view_is_skipped_not_fatal_step6c1():
    """Step 6C1, Part 3: unlike OBJECT_CORE (which raises
    SemanticEnrichmentBatchLimitError and aborts the entire call for a
    single oversized object), a single oversized VIEW is individually
    skipped with a typed reason and the run continues for every other
    view -- never a call-aborting exception, and never a silent drop."""
    huge_definition = "SELECT " + "x" * 100000
    huge_view = _make_view_input("dbo.Huge", definition_evidence=huge_definition)
    normal_view = _make_view_input("dbo.Normal")
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    result = generate_view_semantic_enrichment(
        [huge_view, normal_view], lambda: provider, max_batch_characters=2000,
    )
    assert result.generated_count == 1
    assert result.enriched_views[0].object_fqn == "dbo.Normal"
    huge_skip = next(s for s in result.skipped_objects if s.object_fqn == "dbo.Huge")
    assert huge_skip.reason == SKIP_REASON_INPUT_TOO_LARGE
    # exactly one provider call was made -- for dbo.Normal's own batch only
    assert len(provider.calls) == 1


def test_single_view_exceeding_output_token_budget_is_skipped():
    """A view with an unusually large real column count can alone exceed
    max_estimated_output_tokens even though its rendered INPUT text is
    small -- proving the two bounds (character, token) are genuinely
    independent, per Part 3's own 'maintain the existing character limit
    as an independent bound' requirement."""
    many_columns = tuple(ColumnSemanticInput(f"Col{i}", "TEXT", False, False) for i in range(500))
    wide_view = _make_view_input("dbo.Wide", columns=many_columns)
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    result = generate_view_semantic_enrichment(
        [wide_view], lambda: provider, max_estimated_output_tokens=100,
    )
    assert result.generated_count == 0
    assert result.skipped_objects[0].reason == SKIP_REASON_OUTPUT_BUDGET_EXCEEDED
    assert provider.calls == []


def test_exact_boundary_estimated_output_tokens_accepted():
    view_input = _make_view_input()
    exact_tokens = view_batch_estimated_output_tokens(
        (ViewEnrichmentBatchItem(local_id="v1", view_input=view_input, input_hash=""),)
    )
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    result = generate_view_semantic_enrichment(
        [view_input], lambda: provider, max_estimated_output_tokens=exact_tokens,
    )
    assert result.generated_count == 1
    assert result.skipped_count == 0


def test_one_token_over_boundary_is_skipped():
    view_input = _make_view_input()
    exact_tokens = view_batch_estimated_output_tokens(
        (ViewEnrichmentBatchItem(local_id="v1", view_input=view_input, input_hash=""),)
    )
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    result = generate_view_semantic_enrichment(
        [view_input], lambda: provider, max_estimated_output_tokens=exact_tokens - 1,
    )
    assert result.generated_count == 0
    assert result.skipped_objects[0].reason == SKIP_REASON_OUTPUT_BUDGET_EXCEEDED


def test_configured_safety_ratio_is_used_by_default():
    from core.config import (
        SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
        SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
        SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO,
    )
    expected = int(SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING * SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO)
    assert SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS == expected


def test_provider_receives_bounded_max_output_tokens():
    view_input = _make_view_input()
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    original_enrich_batch = provider.enrich_batch
    captured = {}

    def _capturing(system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        captured["max_output_tokens"] = max_output_tokens
        return original_enrich_batch(system_prompt, user_prompt, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)

    provider.enrich_batch = _capturing
    generate_view_semantic_enrichment([view_input], lambda: provider, max_output_tokens=12345)
    assert captured["max_output_tokens"] == 12345


def test_deterministic_batches_under_shuffled_input():
    import random
    inputs = [_make_view_input(f"dbo.V{i}") for i in range(6)]
    shuffled = inputs[:]
    random.Random(42).shuffle(shuffled)
    responses_by_fqn = {f"dbo.V{i}": {"views": [_view_item("v1")]} for i in range(6)}

    def _run_ordered(order):
        provider = FakeSemanticEnrichmentProvider([responses_by_fqn[v.object_fqn] for v in sorted(order, key=lambda x: x.object_fqn.lower())])
        return generate_view_semantic_enrichment(order, lambda: provider, max_objects_per_batch=2)

    result_a = _run_ordered(inputs)
    result_b = _run_ordered(shuffled)
    assert [v.object_fqn for v in result_a.enriched_views] == [v.object_fqn for v in result_b.enriched_views]


def test_duplicate_object_fqn_input_rejected():
    v1 = _make_view_input("dbo.Dup")
    v2 = _make_view_input("DBO.DUP")
    provider = FakeSemanticEnrichmentProvider({"views": []})
    with pytest.raises(SemanticEnrichmentUnsupportedMetadataError):
        generate_view_semantic_enrichment([v1, v2], lambda: provider)


def test_safe_logging_no_credential_or_exception_text_in_warnings():
    v1 = _make_view_input("dbo.Boom")
    provider = FakeSemanticEnrichmentProvider(RuntimeError("sk-super-secret-credential-12345"))
    result = generate_view_semantic_enrichment([v1], lambda: provider)
    combined = " ".join(result.warnings) + " ".join(result.errors)
    assert "sk-super-secret-credential-12345" not in combined
