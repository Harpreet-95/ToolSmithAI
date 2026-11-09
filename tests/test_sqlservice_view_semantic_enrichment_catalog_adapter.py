"""
Focused unit tests -- SQL Architecture Correction, Phase 6, Step 6C, Part 7:
the pure semantic-catalog compatibility adapter
(core/sqlservice/view_semantic_enrichment_catalog_adapter.py) and its
byte-for-byte-unchanged-when-unused boundary with
core.sqlservice.semantic_catalog.build_semantic_catalog. Pure/offline,
except the Phase 6 Step 6F Part 4 stored-shape round-trip tests near the
bottom, which use a temporary on-disk SQLite database (never the real
data/toolsmith.db) to exercise the ACTUAL reloaded-from-the-store shape
(StoredViewSemanticEnrichment/StoredEnrichedColumn), not just the fresh
in-memory one -- closing the exact gap that let a real
adapt_view_enrichment_to_catalog_input(stored_views, flattened_columns)
misuse reach production code unnoticed (StoredEnrichedColumn carries no
`object_fqn`, so that flatten-and-regroup-by-fqn pattern only ever worked
for the fresh shape).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step6f-adapter-stored-shape-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-step6f-adapter-stored-shape-salt-long-enough-1234")

import data.models as models_module
import core.sqlservice.semantic_enrichment_store as store_module
from core.sqlservice.semantic_catalog import build_semantic_catalog
from core.sqlservice.semantic_enrichment_providers import ProviderSemanticEnrichmentResponse
from core.sqlservice.semantic_enrichment_service import (
    ColumnSemanticInput,
    SOURCE_ROLE_CURRENT_STATE,
    VIEW_ENRICHMENT_CONTRACT_VERSION,
    ViewSemanticInput,
    generate_view_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_store import (
    insert_view_semantic_enrichment,
    load_current_view_enrichments_for_source,
)
from core.sqlservice.view_semantic_enrichment_catalog_adapter import (
    ViewCatalogEnrichmentEntry,
    adapt_stored_view_enrichment_to_catalog_input,
    adapt_view_enrichment_to_catalog_input,
)
from tests.test_sqlservice_view_semantic_enrichment import FakeSemanticEnrichmentProvider, _view_item


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("template") / "template.db")
    original_get_connection = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original_get_connection
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-17T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


@dataclass
class _Doc:
    object_fqn: str
    object_type: str = "VIEW"
    retrieval_tier: int = 1
    metadata: dict = field(default_factory=lambda: {
        "business_name": None, "description": None, "synonyms": (), "column_descriptors": (),
        "column_names": ("StudentId", "EnrollmentStatus", "EnrolledDate"),
        "primary_key_columns": ("StudentId",), "confirmed_relationship_edges": (),
    })

    def __post_init__(self):
        self.metadata = MappingProxyType(self.metadata)


def _generate_one_view():
    view_input = ViewSemanticInput(
        object_fqn="dbo.ActiveStudents", schema_name="dbo", view_name="ActiveStudents", retrieval_tier=1,
        columns=(
            ColumnSemanticInput("StudentId", "INTEGER", True, False),
            ColumnSemanticInput("EnrollmentStatus", "TEXT", False, False),
            ColumnSemanticInput("EnrolledDate", "DATETIME", False, False),
        ),
        primary_key_columns=("StudentId",), dictionary_business_name=None, dictionary_description=None,
        dictionary_synonyms=(), definition_evidence="SELECT 1", definition_truncated=False,
        dependency_edges=(), confirmed_relationship_summary=(),
    )
    provider = FakeSemanticEnrichmentProvider({"views": [_view_item("v1")]})
    return generate_view_semantic_enrichment([view_input], lambda: provider)


def test_adapter_produces_duck_typed_entries():
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, ViewCatalogEnrichmentEntry)
    assert entry.object_fqn == "dbo.ActiveStudents"
    assert entry.object_role == SOURCE_ROLE_CURRENT_STATE
    assert len(entry.columns) == 2
    assert {c.column_name for c in entry.columns} == {"EnrollmentStatus", "EnrolledDate"}


def test_enrichment_adds_object_and_column_evidence_to_catalog():
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)
    doc = _Doc("dbo.ActiveStudents")
    catalog = build_semantic_catalog([doc], enrichment_input=entries)
    assert len(catalog.model_inferred_object_evidence) == 1
    obj_evidence = catalog.model_inferred_object_evidence[0]
    assert obj_evidence.object_fqn == "dbo.ActiveStudents"
    assert "student" in [c.lower() for c in obj_evidence.entity_concept_candidates]
    assert len(catalog.model_inferred_column_evidence) >= 1


def test_existing_structural_concepts_unaffected_by_enrichment():
    doc = _Doc("dbo.ActiveStudents")
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)

    catalog_without = build_semantic_catalog([doc])
    catalog_with = build_semantic_catalog([doc], enrichment_input=entries)

    assert catalog_without.entities == catalog_with.entities
    assert catalog_without.measures == catalog_with.measures
    assert catalog_without.dimensions == catalog_with.dimensions
    assert catalog_without.date_roles == catalog_with.date_roles
    assert catalog_without.identifiers == catalog_with.identifiers
    assert catalog_without.object_families == catalog_with.object_families


def test_no_ai_semantic_claim_becomes_a_graph_join():
    """SemanticCatalog carries no join-construction logic at all -- that
    lives exclusively in core.sqlservice.join_planner, which never reads
    model_inferred_object_evidence/model_inferred_column_evidence. Proven
    structurally: neither evidence dataclass has any join/relationship
    field, so there is nothing a join_planner even COULD read from them."""
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)
    doc = _Doc("dbo.ActiveStudents")
    catalog = build_semantic_catalog([doc], enrichment_input=entries)
    for evidence in catalog.model_inferred_object_evidence:
        field_names = {f for f in evidence.__dataclass_fields__}
        assert not any("join" in f.lower() or "relationship" in f.lower() for f in field_names)


def test_base_table_catalog_unchanged_when_no_view_enrichment_supplied():
    @dataclass
    class _TableDoc:
        object_fqn: str = "dbo.Students"
        object_type: str = "TABLE"
        retrieval_tier: int = 1
        metadata: dict = field(default_factory=lambda: {
            "business_name": None, "description": None, "synonyms": (), "column_descriptors": (),
            "column_names": ("Id", "Name"), "primary_key_columns": ("Id",), "confirmed_relationship_edges": (),
        })

        def __post_init__(self):
            self.metadata = MappingProxyType(self.metadata)

    doc = _TableDoc()
    catalog_a = build_semantic_catalog([doc])
    catalog_b = build_semantic_catalog([doc], enrichment_input=None)
    assert catalog_a.fingerprint == catalog_b.fingerprint


def test_business_domain_status_and_operations_round_trip_to_catalog():
    """Phase 6, Step 6F, Part 4: business_domain/status_concepts/
    supported_operations -- previously dropped by the adapter entirely --
    now survive generation -> adapter -> ModelInferredObjectEvidence."""
    doc = _Doc("dbo.ActiveStudents")
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)
    entry = entries[0]
    assert entry.business_domain == "Enrollment"
    assert entry.status_concepts
    assert entry.supported_operations

    catalog = build_semantic_catalog([doc], enrichment_input=entries)
    obj_evidence = catalog.model_inferred_object_evidence[0]
    assert "Enrollment" in obj_evidence.business_domain_candidates
    assert obj_evidence.status_concept_label_candidates
    assert obj_evidence.supported_operation_candidates

    # Still never structural -- these three fields change ONLY the
    # advisory model-inferred evidence, never entities/measures/etc.
    catalog_without = build_semantic_catalog([doc])
    assert catalog_without.entities == catalog.entities
    assert catalog_without.object_families == catalog.object_families


def test_requirement_fingerprint_unaffected_by_view_enrichment_by_default():
    doc = _Doc("dbo.ActiveStudents")
    result = _generate_one_view()
    entries = adapt_view_enrichment_to_catalog_input(result.enriched_views, result.enriched_columns)
    catalog_without = build_semantic_catalog([doc])
    catalog_with = build_semantic_catalog([doc], enrichment_input=entries)
    # model_inferred_* evidence differs (by design) but every structural
    # field feeding requirement/join planning is untouched -- proven above
    # (test_existing_structural_concepts_unaffected_by_enrichment); this
    # test additionally confirms the fingerprint DOES legitimately change
    # (evidence is part of fingerprint per Step 4B), never silently ignored.
    assert catalog_without.fingerprint != catalog_with.fingerprint


# ── Phase 6, Step 6F, Part 4: stored (reloaded-from-the-store) shape ─────


def test_adapt_stored_view_enrichment_round_trips_through_real_persistence(db_path):
    """The exact path core.sqlservice.schema_plan_options._load_view_
    enrichment_input uses in production: generate -> persist -> reload
    from the store -> adapt -> build_semantic_catalog. Proves
    adapt_stored_view_enrichment_to_catalog_input works against the REAL
    StoredViewSemanticEnrichment/StoredEnrichedColumn shape, not just the
    fresh in-memory one adapt_view_enrichment_to_catalog_input handles."""
    _insert_source(db_path, 1)
    result = _generate_one_view()
    obj = result.enriched_views[0]
    cols = [c for c in result.enriched_columns if c.object_fqn == obj.object_fqn]
    cache_key = result.cache_keys_by_object_fqn[obj.object_fqn]

    insert_result = insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn,
        semantic_input_hash=cache_key.semantic_input_hash,
        provider_id=cache_key.provider_id, model=cache_key.model,
        contract_version=cache_key.contract_version,
        object_semantics=obj, column_semantics=cols,
    )
    assert insert_result.enrichment is not None

    stored_views = load_current_view_enrichments_for_source(
        1, "alice", provider_id=cache_key.provider_id, model=cache_key.model,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn={obj.object_fqn: cache_key.semantic_input_hash},
    )
    assert len(stored_views) == 1

    entries = adapt_stored_view_enrichment_to_catalog_input(stored_views)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.object_fqn == obj.object_fqn
    assert entry.business_domain == obj.business_domain
    assert {c.column_name for c in entry.columns} == {c.column_name for c in cols}

    doc = _Doc(obj.object_fqn)
    catalog = build_semantic_catalog([doc], enrichment_input=entries)
    assert len(catalog.model_inferred_object_evidence) == 1
    assert catalog.model_inferred_object_evidence[0].object_fqn == obj.object_fqn
    assert len(catalog.model_inferred_column_evidence) == len(cols)


def test_adapt_stored_view_enrichment_handles_multiple_views_independently(db_path):
    """Two DIFFERENT stored views' columns must never cross-contaminate --
    the exact failure mode the flatten-and-regroup-by-(missing)-object_fqn
    bug would have produced if it had silently defaulted instead of
    raising AttributeError."""
    _insert_source(db_path, 2)

    def _persist(object_fqn, column_name):
        view_input = ViewSemanticInput(
            object_fqn=object_fqn, schema_name="dbo", view_name=object_fqn.split(".")[-1], retrieval_tier=1,
            columns=(ColumnSemanticInput(column_name, "TEXT", False, False),),
            primary_key_columns=(), dictionary_business_name=None, dictionary_description=None,
            dictionary_synonyms=(), definition_evidence="SELECT 1", definition_truncated=False,
            dependency_edges=(), confirmed_relationship_summary=(),
        )
        response = {"views": [_view_item("v1", columns=[{
            "column": column_name, "label": "x", "semantic_role": "DESCRIPTIVE",
            "date_role": None, "measure_role": None, "dimension_role": None, "identifier_role": None,
            "confidence": 0.5,
        }])]}
        provider = FakeSemanticEnrichmentProvider(response)
        result = generate_view_semantic_enrichment([view_input], lambda: provider)
        obj = result.enriched_views[0]
        cols = [c for c in result.enriched_columns if c.object_fqn == obj.object_fqn]
        cache_key = result.cache_keys_by_object_fqn[obj.object_fqn]
        insert_view_semantic_enrichment(
            "alice", source_id=2, object_fqn=obj.object_fqn,
            semantic_input_hash=cache_key.semantic_input_hash,
            provider_id=cache_key.provider_id, model=cache_key.model,
            contract_version=cache_key.contract_version,
            object_semantics=obj, column_semantics=cols,
        )
        return cache_key

    key_a = _persist("dbo.ViewA", "ColumnA")
    key_b = _persist("dbo.ViewB", "ColumnB")

    stored_views = load_current_view_enrichments_for_source(
        2, "alice", provider_id=key_a.provider_id, model=key_a.model,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn={
            "dbo.ViewA": key_a.semantic_input_hash, "dbo.ViewB": key_b.semantic_input_hash,
        },
    )
    assert len(stored_views) == 2

    entries = adapt_stored_view_enrichment_to_catalog_input(stored_views)
    by_fqn = {e.object_fqn: e for e in entries}
    assert {c.column_name for c in by_fqn["dbo.ViewA"].columns} == {"ColumnA"}
    assert {c.column_name for c in by_fqn["dbo.ViewB"].columns} == {"ColumnB"}
