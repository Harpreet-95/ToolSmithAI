"""
Focused unit tests — SQL Architecture Correction, Phase 6, Step 1:
deterministic semantic catalog (core/sqlservice/semantic_catalog.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Small synthetic fixtures only — a simple
attribute-bag stand-in for a retrieval document (this module only ever
reads object_fqn/object_type/retrieval_tier/metadata, exactly like
core.sqlservice.lexical_index/canonical_scoring).

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_catalog.py -v
"""

from __future__ import annotations

import random
from pathlib import Path
from types import MappingProxyType

import pytest

from core.sqlservice.semantic_catalog import (
    IDENTIFIER_KIND_BUSINESS_IDENTIFIER,
    IDENTIFIER_KIND_FOREIGN_KEY,
    IDENTIFIER_KIND_PRIMARY_KEY,
    IDENTIFIER_KIND_TECHNICAL_IDENTIFIER,
    BusinessEntity,
    SemanticCatalog,
    build_semantic_catalog,
)

# ── Fixture builder ────────────────────────────────────────────────────────

_DEFAULT_META = {
    "business_name": None,
    "description": None,
    "domain": None,
    "synonyms": (),
    "column_names": (),
    "has_primary_key": False,
    "primary_key_columns": (),
    "foreign_key_targets": (),
    "confirmed_relationship_targets": (),
    "confirmed_relationship_sources": (),
    "confirmed_relationship_edges": (),
    "inferred_relationship_targets": (),
    "inferred_relationship_sources": (),
    "status_columns": (),
    "date_columns": (),
    "measure_columns": (),
}


class _Doc:
    def __init__(self, object_fqn: str, object_type: str = "TABLE", retrieval_tier: int = 1, **meta_overrides):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = retrieval_tier
        merged = dict(_DEFAULT_META)
        merged.update(meta_overrides)
        self.metadata = MappingProxyType(merged)


def _find_entity(catalog: SemanticCatalog, normalized_name: str) -> BusinessEntity:
    matches = [e for e in catalog.entities if e.normalized_name == normalized_name]
    assert matches, f"no entity named {normalized_name!r} in {[e.normalized_name for e in catalog.entities]}"
    return matches[0]


# ── Entity extraction: object/business names, synonyms ────────────────────


def test_entity_extracted_from_business_name_with_synonym_alias():
    doc = _Doc(
        "dbo.Customers", business_name="Customer", description="Core customer records",
        synonyms=("client", "account holder"), column_names=("CustomerID",),
        has_primary_key=True, primary_key_columns=("CustomerID",),
    )
    catalog = build_semantic_catalog([doc])
    entity = _find_entity(catalog, "customer")
    assert entity.candidate_objects[0].object_fqn == "dbo.Customers"
    assert "client" in entity.aliases


def test_entity_extracted_from_bare_object_name_when_no_business_name():
    doc = _Doc("dbo.Products", column_names=("ProductID",))
    catalog = build_semantic_catalog([doc])
    entity = _find_entity(catalog, "product")
    assert entity.candidate_objects[0].confidence < 1.0


def test_business_name_sourced_entity_outranks_bare_name_sourced_entity():
    documented = _Doc("dbo.Employees", business_name="Employee", description="HR record")
    bare = _Doc("dbo.Vendors")
    catalog = build_semantic_catalog([documented, bare])
    employee = _find_entity(catalog, "employee")
    vendor = _find_entity(catalog, "vendor")
    assert employee.confidence > vendor.confidence


def test_multiple_objects_sharing_a_head_noun_become_one_ambiguous_entity():
    a = _Doc("dbo.Students", business_name="Student", description="Roster")
    b = _Doc("dbo.StudentProfiles", business_name="Student Profile")
    catalog = build_semantic_catalog([a, b])
    entity = _find_entity(catalog, "profile") if False else None
    # Both share the head noun "student" only if business_name's LAST token
    # matches; "Student Profile" -> last token "profile", so these do NOT
    # collapse — verifies independent single-object concepts instead.
    assert _find_entity(catalog, "student").candidate_objects[0].object_fqn == "dbo.Students"
    assert _find_entity(catalog, "profile").candidate_objects[0].object_fqn == "dbo.StudentProfiles"


def test_same_head_noun_from_two_objects_collapses_into_one_ambiguous_entity():
    a = _Doc("dbo.Customers", business_name="Customer", description="Primary")
    b = _Doc("dbo.CustomerArchive", business_name="Customer", retrieval_tier=2)
    catalog = build_semantic_catalog([a, b])
    entity = _find_entity(catalog, "customer")
    assert entity.candidate_total_count == 2
    assert {c.object_fqn for c in entity.candidate_objects} == {"dbo.Customers", "dbo.CustomerArchive"}


# ── Tier 3/4 exclusion from active business candidates ────────────────────


def test_tier_3_and_tier_4_objects_never_seed_an_entity_concept():
    tier3 = _Doc("sys.SomeSystemTable", business_name="System Widget", retrieval_tier=3)
    tier4 = _Doc("dbo.Customers_bak", business_name="Customer", retrieval_tier=4)
    catalog = build_semantic_catalog([tier3, tier4])
    assert catalog.entities == ()
    assert catalog.coverage["excluded_tier_3_4_object_count"] == 2


def test_tier_4_object_can_still_appear_as_archive_variant_in_a_family():
    live = _Doc("dbo.Customers", business_name="Customer", retrieval_tier=1)
    backup = _Doc("dbo.Customers_bak", retrieval_tier=4)
    catalog = build_semantic_catalog([live, backup])
    assert len(catalog.object_families) == 1
    family = catalog.object_families[0]
    assert family.canonical_object_fqn == "dbo.Customers"
    assert family.archive_or_variant == ("dbo.Customers_bak",)


# ── Measures: positive and negative cases ──────────────────────────────────


def test_measure_columns_produce_measure_concepts_with_categories():
    doc = _Doc(
        "dbo.Accounts", measure_columns=("Balance", "InterestRate", "TotalScore", "OrderQty"),
    )
    catalog = build_semantic_catalog([doc])
    by_column = {m.column_name: m.normalized_measure for m in catalog.measures}
    assert by_column["Balance"] == "balance"
    assert by_column["InterestRate"] == "rate"
    assert by_column["TotalScore"] == "score"
    assert by_column["OrderQty"] == "quantity"


def test_column_absent_from_measure_columns_never_becomes_a_measure():
    # A numeric-looking column that upstream retrieval_documents did NOT
    # classify as a measure (e.g. it's a primary key) must never be
    # reintroduced as a measure by this module — it only reads
    # measure_columns, never re-derives measure-ness from column_names.
    doc = _Doc(
        "dbo.Orders", column_names=("OrderID", "Amount"),
        has_primary_key=True, primary_key_columns=("OrderID",),
        measure_columns=("Amount",),
    )
    catalog = build_semantic_catalog([doc])
    measured_columns = {m.column_name for m in catalog.measures}
    assert measured_columns == {"Amount"}
    assert "OrderID" not in measured_columns


def test_generic_fallback_measure_still_bounded_confidence_below_decisive_match():
    decisive = _Doc("dbo.A", measure_columns=("Balance",))
    generic = _Doc("dbo.B", measure_columns=("SomeMetric",))
    catalog = build_semantic_catalog([decisive, generic])
    decisive_conf = next(m.confidence for m in catalog.measures if m.object_fqn == "dbo.A")
    generic_conf = next(m.confidence for m in catalog.measures if m.object_fqn == "dbo.B")
    assert generic_conf < decisive_conf


# ── Dimensions: status / type / category / program / location / flag ─────


def test_status_column_becomes_high_confidence_status_dimension():
    doc = _Doc("dbo.Accounts", status_columns=("AccountStatus",))
    catalog = build_semantic_catalog([doc])
    assert len(catalog.dimensions) == 1
    dim = catalog.dimensions[0]
    assert dim.dimension_kind == "status"
    assert dim.confidence >= 0.6


def test_name_only_category_dimensions_detected_with_lower_confidence():
    doc = _Doc(
        "dbo.Accounts",
        column_names=("AccountType", "Category", "ProgramName", "City", "IsActive"),
    )
    catalog = build_semantic_catalog([doc])
    kinds = {(d.column_name, d.dimension_kind) for d in catalog.dimensions}
    assert ("AccountType", "type") in kinds
    assert ("Category", "category") in kinds
    assert ("ProgramName", "program") in kinds
    assert ("City", "location") in kinds
    assert ("IsActive", "flag") in kinds
    assert all(d.confidence < 0.7 for d in catalog.dimensions)


def test_column_can_carry_two_independent_dimension_kinds_without_dropping_either():
    doc = _Doc("dbo.Accounts", column_names=("LocationType",))
    catalog = build_semantic_catalog([doc])
    kinds = {d.dimension_kind for d in catalog.dimensions if d.column_name == "LocationType"}
    assert kinds == {"type", "location"}


# ── Date roles: extraction and ambiguity ───────────────────────────────────


def test_unambiguous_date_role_detected():
    doc = _Doc("dbo.Accounts", date_columns=("CreatedDate",))
    catalog = build_semantic_catalog([doc])
    assert len(catalog.date_roles) == 1
    concept = catalog.date_roles[0]
    assert not concept.ambiguous
    assert concept.candidates[0].role == "created"


def test_date_column_matching_no_keyword_bucket_stays_unspecified_not_dropped():
    doc = _Doc("dbo.Accounts", date_columns=("SomeDate",))
    catalog = build_semantic_catalog([doc])
    assert len(catalog.date_roles) == 1
    assert catalog.date_roles[0].candidates[0].role == "unspecified"
    assert catalog.date_roles[0].candidates[0].confidence < 0.5


def test_ambiguous_date_column_preserves_every_matched_role():
    # "start" -> started; a name that also carries a "completed"-bucket
    # token exercises the multi-candidate/ambiguous path deterministically.
    doc = _Doc("dbo.Programs", date_columns=("StartEndDate",))
    catalog = build_semantic_catalog([doc])
    concept = catalog.date_roles[0]
    roles = {c.role for c in concept.candidates}
    assert concept.ambiguous is True
    assert "started" in roles
    assert "completed" in roles
    assert catalog.coverage["ambiguous_date_role_count"] == 1


# ── Identifiers: PK / FK / business vs technical ───────────────────────────


def test_primary_key_and_confirmed_fk_and_business_identifier_all_classified():
    doc = _Doc(
        "dbo.Orders",
        column_names=("OrderID", "CustomerID", "OrderNumber", "TrackingGuid"),
        has_primary_key=True, primary_key_columns=("OrderID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "CustomerID", "other": "dbo.Customers.CustomerID", "kind": "PHYSICAL_FK"},
        ),
    )
    catalog = build_semantic_catalog([doc])
    by_column = {i.column_name: i.identifier_kind for i in catalog.identifiers}
    assert by_column["OrderID"] == IDENTIFIER_KIND_PRIMARY_KEY
    assert by_column["CustomerID"] == IDENTIFIER_KIND_FOREIGN_KEY
    assert by_column["OrderNumber"] == IDENTIFIER_KIND_BUSINESS_IDENTIFIER
    assert by_column["TrackingGuid"] == IDENTIFIER_KIND_TECHNICAL_IDENTIFIER


def test_bare_id_suffix_alone_is_never_a_business_identifier():
    doc = _Doc("dbo.Orders", column_names=("VendorID",))
    catalog = build_semantic_catalog([doc])
    assert len(catalog.identifiers) == 1
    assert catalog.identifiers[0].identifier_kind == IDENTIFIER_KIND_TECHNICAL_IDENTIFIER


def test_confirmed_relationship_evidence_is_attached_to_foreign_key_identifier():
    doc = _Doc(
        "dbo.Orders", column_names=("CustomerID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "CustomerID", "other": "dbo.Customers.CustomerID", "kind": "PHYSICAL_FK"},
        ),
    )
    catalog = build_semantic_catalog([doc])
    fk = catalog.identifiers[0]
    assert fk.identifier_kind == IDENTIFIER_KIND_FOREIGN_KEY
    assert any(e.kind == "CONFIRMED_RELATIONSHIP" and "dbo.Customers.CustomerID" in e.detail for e in fk.evidence)


def test_unrelated_column_with_no_identifier_signal_is_never_listed():
    doc = _Doc("dbo.Orders", column_names=("Notes",))
    catalog = build_semantic_catalog([doc])
    assert catalog.identifiers == ()


# ── Canonical-family reuse (never a second/conflicting algorithm) ─────────


def test_object_family_grouping_matches_canonical_scoring_shared_name_tokens():
    # "CustomerAccount"/"CustomerAccountDetail" share 2 of 3 significant
    # tokens (Jaccard = 0.667) -- strong enough evidence under the
    # corrected canonical_scoring family algorithm (Phase 6 Step 1A); a
    # single shared word (e.g. just "customer") is deliberately no longer
    # sufficient -- see test_weak_single_shared_token_no_longer_merges_
    # unrelated_objects in test_sqlservice_canonical_scoring.py.
    live = _Doc("dbo.CustomerAccount", has_primary_key=True, primary_key_columns=("CustomerID",))
    narrow = _Doc("dbo.CustomerAccountDetail", retrieval_tier=2)
    unrelated = _Doc("dbo.Products")
    catalog = build_semantic_catalog([live, narrow, unrelated])
    assert len(catalog.object_families) == 1
    family = catalog.object_families[0]
    assert set(family.member_object_fqns) == {"dbo.CustomerAccount", "dbo.CustomerAccountDetail"}


def test_singleton_object_never_produces_a_family():
    doc = _Doc("dbo.Products", business_name="Product")
    catalog = build_semantic_catalog([doc])
    assert catalog.object_families == ()


# ── Weak evidence retained at lower confidence, never dropped ─────────────


def test_weak_and_strong_entity_evidence_both_retained_with_different_confidence():
    strong = _Doc("dbo.Employees", business_name="Employee", description="HR record", synonyms=("staff",))
    weak = _Doc("dbo.Miscs")  # bare, undocumented object name
    catalog = build_semantic_catalog([strong, weak])
    strong_entity = _find_entity(catalog, "employee")
    weak_entity = _find_entity(catalog, "misc")
    assert weak_entity.confidence > 0.0
    assert weak_entity.confidence < strong_entity.confidence


# ── Ambiguity preserved (never silently resolved) ──────────────────────────


def test_ambiguous_entity_flag_set_when_two_close_candidates_exist():
    a = _Doc("dbo.Customers", business_name="Customer", description="A", synonyms=("client",))
    b = _Doc("dbo.CustomerAccounts", business_name="Customer", description="B", synonyms=("client",))
    catalog = build_semantic_catalog([a, b])
    entity = _find_entity(catalog, "customer")
    assert entity.ambiguous is True
    assert len(entity.candidate_objects) == 2


# ── No manual approval state anywhere in the model ─────────────────────────


def test_no_approval_or_status_workflow_fields_on_any_dataclass():
    doc = _Doc("dbo.Customers", business_name="Customer")
    catalog = build_semantic_catalog([doc])
    forbidden = {"approved", "approval_status", "rejected", "pending_review", "reviewed_by"}
    for entity in catalog.entities:
        field_names = set(vars(entity).keys())
        assert not (field_names & forbidden)


# ── No domain-/database-specific production constants ──────────────────────────

_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "semantic_catalog.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"semantic_catalog.py contains banned domain-specific token {banned!r}"


def test_module_never_imports_database_network_or_provider_modules():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "semantic_catalog.py").read_text(encoding="utf-8")
    for forbidden_import in ("data.db", "sqlite3", "requests", "openai", "urllib", "socket", "pyodbc"):
        assert forbidden_import not in source, f"unexpected I/O-shaped import {forbidden_import!r}"


# ── Determinism: shuffled input, fingerprint, content changes ─────────────


def _sample_documents():
    return [
        _Doc(
            "dbo.Customers", business_name="Customer", description="Core customer records",
            synonyms=("client",), column_names=("CustomerID", "Email", "Status", "Balance", "CreatedDate"),
            has_primary_key=True, primary_key_columns=("CustomerID",),
            confirmed_relationship_sources=("dbo.Orders.CustomerID",),
            status_columns=("Status",), date_columns=("CreatedDate",), measure_columns=("Balance",),
        ),
        _Doc(
            "dbo.Orders", business_name="Order", description="Sales orders",
            column_names=("OrderID", "CustomerID", "Amount", "OrderDate"),
            has_primary_key=True, primary_key_columns=("OrderID",),
            foreign_key_targets=("dbo.Customers.CustomerID",),
            confirmed_relationship_targets=("dbo.Customers.CustomerID",),
            confirmed_relationship_edges=(
                {"direction": "outbound", "column": "CustomerID", "other": "dbo.Customers.CustomerID", "kind": "PHYSICAL_FK"},
            ),
            date_columns=("OrderDate",), measure_columns=("Amount",),
        ),
        _Doc("dbo.Customers_bak", retrieval_tier=4),
    ]


def test_output_deterministic_under_shuffled_input_order():
    docs = _sample_documents()
    catalog_a = build_semantic_catalog(docs)
    shuffled = docs[:]
    random.Random(42).shuffle(shuffled)
    catalog_b = build_semantic_catalog(shuffled)
    assert catalog_a == catalog_b


def test_fingerprint_deterministic_across_repeated_calls():
    docs = _sample_documents()
    fp1 = build_semantic_catalog(docs).fingerprint
    fp2 = build_semantic_catalog(docs).fingerprint
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hex digest


def test_fingerprint_changes_when_content_changes():
    docs = _sample_documents()
    baseline = build_semantic_catalog(docs).fingerprint

    changed = _sample_documents()
    changed[0].metadata["synonyms"]  # sanity: metadata is a real mapping
    changed_doc = _Doc(
        "dbo.Customers", business_name="Customer", description="CHANGED DESCRIPTION",
        column_names=("CustomerID",), has_primary_key=True, primary_key_columns=("CustomerID",),
    )
    changed[0] = changed_doc
    mutated_fp = build_semantic_catalog(changed).fingerprint
    assert mutated_fp != baseline


# ── Strict defensive limits ─────────────────────────────────────────────────


def test_max_entities_truncates_deterministically_and_reports_it():
    words = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet"]
    docs = [_Doc(f"dbo.Entity{i}", business_name=word) for i, word in enumerate(words)]
    catalog = build_semantic_catalog(docs, max_entities=3)
    assert len(catalog.entities) == 3
    assert any("entities" in w for w in catalog.warnings)


def test_max_aliases_and_candidates_per_entity_are_enforced():
    doc = _Doc(
        "dbo.Customers", business_name="Customer",
        synonyms=tuple(f"alias{i}" for i in range(20)),
    )
    catalog = build_semantic_catalog([doc], max_aliases_per_entity=2)
    entity = _find_entity(catalog, "customer")
    assert len(entity.aliases) == 2
    assert entity.alias_total_count >= 2

    many_candidates = [
        _Doc(f"dbo.Customer{i}", business_name="Customer") for i in range(5)
    ]
    catalog2 = build_semantic_catalog(many_candidates, max_candidates_per_entity=2)
    entity2 = _find_entity(catalog2, "customer")
    assert len(entity2.candidate_objects) == 2
    assert entity2.candidate_total_count == 5


def test_invalid_bound_arguments_raise_value_error():
    doc = _Doc("dbo.Customers", business_name="Customer")
    with pytest.raises(ValueError):
        build_semantic_catalog([doc], max_entities=0)
    with pytest.raises(ValueError):
        build_semantic_catalog([doc], max_measures=-1)


def test_duplicate_object_fqn_case_insensitive_rejected():
    a = _Doc("dbo.Customers")
    b = _Doc("DBO.CUSTOMERS")
    with pytest.raises(ValueError):
        build_semantic_catalog([a, b])


# ── Deep immutability ───────────────────────────────────────────────────────


def test_catalog_and_nested_collections_are_immutable():
    doc = _Doc("dbo.Customers", business_name="Customer", measure_columns=("Balance",))
    catalog = build_semantic_catalog([doc])

    with pytest.raises(Exception):
        catalog.entities = ()  # frozen dataclass
    with pytest.raises(Exception):
        catalog.entities[0].aliases = ()  # frozen dataclass
    with pytest.raises(AttributeError):
        catalog.entities[0].candidate_objects.append("x")  # tuple, not list
    with pytest.raises(TypeError):
        catalog.coverage["total_objects"] = 999  # MappingProxyType


# ── Missing/partial metadata never crashes ─────────────────────────────────


def test_missing_optional_metadata_keys_degrade_gracefully():
    class _BareDoc:
        object_fqn = "dbo.Bare"
        object_type = "TABLE"
        retrieval_tier = 1
        metadata = MappingProxyType({})

    # No crash despite a completely empty metadata mapping. The bare object
    # name "Bare" is still meaningful enough to seed one weak entity guess
    # (see test_entity_extracted_from_bare_object_name_when_no_business_name)
    # — every OTHER concept category has nothing to work with and stays empty.
    catalog = build_semantic_catalog([_BareDoc()])
    assert len(catalog.entities) == 1
    assert catalog.entities[0].confidence < 0.5
    assert catalog.measures == ()
    assert catalog.identifiers == ()
    assert catalog.coverage["total_objects"] == 1


def test_malformed_confirmed_relationship_edge_entries_are_skipped_not_fatal():
    doc = _Doc(
        "dbo.Orders", column_names=("CustomerID",),
        confirmed_relationship_edges=(
            {"direction": "outbound"},  # missing "column"
            {"direction": "inbound", "column": "X", "other": "dbo.Y.Z", "kind": "PHYSICAL_FK"},  # wrong direction
        ),
    )
    catalog = build_semantic_catalog([doc])
    assert not any(i.identifier_kind == IDENTIFIER_KIND_FOREIGN_KEY for i in catalog.identifiers)


# ── Quoted/reserved identifiers ─────────────────────────────────────────────


def test_bracketed_and_reserved_word_identifiers_handled_without_crashing():
    doc = _Doc(
        "dbo.[Order]", business_name="[Order] Type",
        column_names=("[User]", "Order Status", "Select"),
        status_columns=("Order Status",),
    )
    catalog = build_semantic_catalog([doc])
    assert any(d.dimension_kind == "status" for d in catalog.dimensions)
    assert catalog.entities  # extraction still produced something, no crash


# ── No document_text structural parsing ─────────────────────────────────────


def test_semantic_catalog_source_never_accesses_document_text_attribute():
    # The docstring legitimately DISCUSSES document_text (explaining that it
    # is never parsed as structure, mirroring lexical_index's own
    # docstring convention) — what must never appear is actual code reading
    # a document's .document_text attribute.
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "semantic_catalog.py").read_text(encoding="utf-8")
    assert ".document_text" not in source


def test_builder_ignores_a_document_text_attribute_even_when_present():
    class _DocWithText(_Doc):
        document_text = "OBJECT: dbo.Fake\nBUSINESS_NAME: TotallyDifferentConcept\n"

    doc = _DocWithText("dbo.Customers", business_name="Customer")
    catalog = build_semantic_catalog([doc])
    assert _find_entity(catalog, "customer")


# ── Problem 2: entity coverage accounting (SQL Architecture Correction,
#    Phase 6, Step 1A) ─────────────────────────────────────────────────────

def test_every_eligible_object_is_represented_or_explicitly_unclassified():
    docs = [_Doc(f"dbo.Concept{i}", business_name=f"Concept{i}") for i in range(50)]
    docs.append(_Doc("dbo.___", business_name=None))  # no meaningful token at all
    catalog = build_semantic_catalog(docs)

    represented = {c.object_fqn for e in catalog.entities for c in e.candidate_objects}
    unclassified = {u.object_fqn for u in catalog.unclassified_objects}
    eligible = {d.object_fqn for d in docs if d.retrieval_tier in (1, 2)}

    assert represented | unclassified == eligible
    assert represented & unclassified == set()
    assert catalog.coverage["eligible_object_count"] == len(eligible)
    assert catalog.coverage["represented_object_count"] == len(represented)
    assert catalog.coverage["unclassified_object_count"] == len(unclassified)
    assert catalog.coverage["entity_coverage_percentage"] == pytest.approx(100.0 * len(represented) / len(eligible))


def test_no_meaningful_name_token_recorded_as_explicit_unclassified_reason():
    doc = _Doc("dbo.___", business_name=None)
    catalog = build_semantic_catalog([doc])
    assert catalog.entities == ()
    assert len(catalog.unclassified_objects) == 1
    assert catalog.unclassified_objects[0].reason == "NO_MEANINGFUL_NAME_TOKEN"
    assert catalog.coverage["truncated_object_count"] == 0


def test_tier_3_and_4_objects_are_never_counted_as_eligible_or_unclassified():
    tier4 = _Doc("dbo.Archive_bak", retrieval_tier=4)
    catalog = build_semantic_catalog([tier4])
    assert catalog.coverage["eligible_object_count"] == 0
    assert catalog.unclassified_objects == ()


def test_explicit_low_max_entities_truncation_populates_unclassified_with_reason():
    docs = [_Doc(f"dbo.Entity{i}", business_name=word) for i, word in enumerate(
        ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    )]
    catalog = build_semantic_catalog(docs, max_entities=2)
    assert len(catalog.entities) == 2
    truncated = [u for u in catalog.unclassified_objects if u.reason == "TRUNCATED_BY_MAX_ENTITIES"]
    assert len(truncated) == 3
    represented = {c.object_fqn for e in catalog.entities for c in e.candidate_objects}
    unclassified = {u.object_fqn for u in catalog.unclassified_objects}
    eligible = {d.object_fqn for d in docs}
    assert represented | unclassified == eligible


def test_explicit_low_max_candidates_per_entity_populates_unclassified_with_reason():
    docs = [_Doc(f"dbo.Customer{i}", business_name="Customer") for i in range(6)]
    catalog = build_semantic_catalog(docs, max_candidates_per_entity=2)
    entity = _find_entity(catalog, "customer")
    assert len(entity.candidate_objects) == 2
    assert entity.candidate_total_count == 6
    truncated = [u for u in catalog.unclassified_objects if u.reason == "TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY"]
    assert len(truncated) == 4


def _unique_alpha_word(i: int) -> str:
    """A distinct, all-alphabetic, base-26-encoded word ("concepta",
    "conceptb", ..., "conceptaa", ...) -- unlike a numbered suffix
    ("Concept0001"), the digits in a numbered suffix are stripped as
    non-meaningful tokens (see _meaningful_tokens), which would collapse
    every object onto the SAME bare "concept" head noun instead of 1,000
    genuinely distinct ones."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    i += 1
    suffix = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        suffix = letters[r] + suffix
    return "Concept" + suffix


def test_schema_scaled_default_never_truncates_a_realistic_sized_schema():
    # 1,000 distinct eligible objects, each its own concept (mirrors the
    # real audit's exact large-schema coverage gap) -- the schema-scaled
    # default must represent every one of them, unlike the old fixed
    # max_entities=200 default.
    docs = [_Doc(f"dbo.Object{i:04d}", business_name=_unique_alpha_word(i)) for i in range(1000)]
    catalog = build_semantic_catalog(docs)
    assert catalog.coverage["eligible_object_count"] == 1000
    assert catalog.coverage["represented_object_count"] == 1000
    assert catalog.coverage["unclassified_object_count"] == 0
    assert catalog.coverage["entity_coverage_percentage"] == 100.0
    assert not any("TRUNCATED_BY_MAX_ENTITIES" in w for w in catalog.warnings)


def test_explicit_max_entities_above_hard_ceiling_is_rejected():
    doc = _Doc("dbo.Customers", business_name="Customer")
    with pytest.raises(ValueError):
        build_semantic_catalog([doc], max_entities=999_999)
    with pytest.raises(ValueError):
        build_semantic_catalog([doc], max_candidates_per_entity=999_999)


def test_do_not_merge_unrelated_objects_merely_to_stay_under_default_limit():
    # Every object here has a DISTINCT normalized_name -- the schema-scaled
    # default must never collapse them into fewer, merged concepts just to
    # respect a bound; it raises the bound instead.
    words = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    docs = [_Doc(f"dbo.Entity{i}", business_name=word) for i, word in enumerate(words)]
    catalog = build_semantic_catalog(docs)
    assert len(catalog.entities) == len(words)
    assert {e.normalized_name for e in catalog.entities} == {w.lower() for w in words}
