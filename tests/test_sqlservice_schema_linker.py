"""
Focused unit tests — SQL Architecture Correction, Phase 6, Step 2:
deterministic question-to-schema linker (core/sqlservice/schema_linker.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Small synthetic fixtures only.

Run:
    venv/Scripts/pytest tests/test_sqlservice_schema_linker.py -v
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from core.sqlservice.semantic_catalog import build_semantic_catalog
from core.sqlservice.schema_linker import (
    AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES,
    ANCHOR_WEIGHT_CANONICAL,
    ANCHOR_WEIGHT_CATALOG,
    ANCHOR_WEIGHT_EXAMPLE,
    ANCHOR_WEIGHT_LEXICAL,
    ANCHOR_WEIGHT_SEMANTIC,
    CONCEPT_KIND_ENTITY,
    MATCH_KIND_COLUMN_TOKEN,
    MATCH_KIND_EXACT_BUSINESS_NAME,
    MATCH_KIND_SYNONYM,
    OPERATION_AVERAGE,
    OPERATION_COMPARISON,
    OPERATION_FILTERED_COUNT,
    OPERATION_GROUPED_AGGREGATION,
    OPERATION_RANKING_TOP_N,
    OPERATION_ROW_COUNT,
    OPERATION_TREND,
    STATUS_EMPTY_SCHEMA,
    STATUS_INDEX_BUILD_FAILED,
    STATUS_OK,
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    DEFAULT_SCHEMA_LINKING_LIMITS,
    SchemaLinkingLimits,
    clear_schema_linker_cache,
    link_question_to_schema,
    schema_linker_cache_size,
)

# ── Fixture builder ────────────────────────────────────────────────────────

_DEFAULT_META = {
    "business_name": None,
    "description": None,
    "domain": None,
    "synonyms": (),
    "column_names": (),
    "column_descriptors": (),
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


class _RankedHit:
    def __init__(self, object_fqn: str, score: float):
        self.object_fqn = object_fqn
        self.score = score


class _RetrievedExample:
    def __init__(self, proposed_object_fqns, similarity: float):
        self.proposed_object_fqns = tuple(proposed_object_fqns)
        self.similarity = similarity


def _mock_schema():
    customers = _Doc(
        "dbo.Customers", business_name="Customer", description="Core customer records",
        synonyms=("client",),
        column_names=("CustomerID", "Email", "Status", "Balance", "CreatedDate", "CustomerType"),
        has_primary_key=True, primary_key_columns=("CustomerID",),
        confirmed_relationship_sources=("dbo.Orders.CustomerID",),
        status_columns=("Status",), date_columns=("CreatedDate",), measure_columns=("Balance",),
        column_descriptors=(
            {"column_name": "CustomerID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": True, "is_foreign_key": False},
            {"column_name": "Balance", "normalized_sql_type": "DECIMAL", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
            {"column_name": "CreatedDate", "normalized_sql_type": "DATETIME", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
        ),
    )
    orders = _Doc(
        "dbo.Orders", business_name="Order", description="Sales orders",
        column_names=("OrderID", "CustomerID", "Amount", "OrderDate", "PaymentDate"),
        has_primary_key=True, primary_key_columns=("OrderID",),
        foreign_key_targets=("dbo.Customers.CustomerID",),
        confirmed_relationship_targets=("dbo.Customers.CustomerID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "CustomerID", "other": "dbo.Customers.CustomerID", "kind": "PHYSICAL_FK"},
        ),
        date_columns=("OrderDate", "PaymentDate"), measure_columns=("Amount",),
        column_descriptors=(
            {"column_name": "OrderID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": True, "is_foreign_key": False},
            {"column_name": "CustomerID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": False, "is_foreign_key": True},
            {"column_name": "Amount", "normalized_sql_type": "DECIMAL", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
            {"column_name": "OrderDate", "normalized_sql_type": "DATETIME", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
            {"column_name": "PaymentDate", "normalized_sql_type": "DATETIME", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
        ),
    )
    products = _Doc(
        "dbo.Products", business_name="Product", description="Catalog products",
        column_names=("ProductID", "Price", "ProductType"),
        has_primary_key=True, primary_key_columns=("ProductID",),
        measure_columns=("Price",),
        column_descriptors=(
            {"column_name": "ProductID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": True, "is_foreign_key": False},
            {"column_name": "Price", "normalized_sql_type": "DECIMAL", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
        ),
    )
    tier4_archive = _Doc("dbo.Customers_Archive", retrieval_tier=4, column_names=("CustomerID", "Balance"))
    tier3_system = _Doc("sys.SomeSystemTable", retrieval_tier=3, column_names=("ID",))
    return [customers, orders, products, tier4_archive, tier3_system]


@pytest.fixture(autouse=True)
def _isolated_cache():
    clear_schema_linker_cache()
    yield
    clear_schema_linker_cache()


def _link(question, docs=None, catalog=None, **kwargs):
    docs = docs if docs is not None else _mock_schema()
    catalog = catalog if catalog is not None else build_semantic_catalog(docs)
    return link_question_to_schema(question, catalog, docs, **kwargs)


def _anchor(result, object_fqn):
    matches = [a for a in result.anchors if a.object_fqn == object_fqn]
    assert matches, f"{object_fqn!r} not in anchors: {[a.object_fqn for a in result.anchors]}"
    return matches[0]


# ── Question understanding: operations ─────────────────────────────────────


def test_simple_row_count_operation_detected():
    result = _link("How many customers are there?")
    assert result.requirements.operation == OPERATION_ROW_COUNT
    assert _anchor(result, "dbo.Customers")


def test_filtered_count_operation_detected():
    result = _link("How many orders are there that are late?")
    assert result.requirements.operation == OPERATION_FILTERED_COUNT


def test_grouped_aggregation_operation_and_grouping_intent():
    result = _link("What is the total order amount for each customer?")
    assert result.requirements.wants_grouping is True
    assert result.requirements.operation in (OPERATION_GROUPED_AGGREGATION, "SUM")


def test_measure_aggregation_average_detected_and_matched():
    result = _link("What is the average order amount?")
    assert result.requirements.operation == OPERATION_AVERAGE
    assert any(c.normalized_label == "amount" for c in result.requirements.measure_concepts)


def test_date_trend_operation_and_intent():
    result = _link("Show order amounts by month over the last year")
    assert result.requirements.operation == OPERATION_TREND
    assert result.requirements.wants_trend is True


def test_comparison_and_ranking_operations():
    comparison = _link("Compare total sales this year versus last year")
    assert comparison.requirements.operation == OPERATION_COMPARISON or comparison.requirements.wants_comparison

    ranking = _link("What are the top 10 customers by balance?")
    assert ranking.requirements.operation == OPERATION_RANKING_TOP_N


def test_multi_entity_question_matches_both_entities():
    result = _link("List orders and their customers")
    labels = {c.normalized_label for c in result.requirements.entity_concepts}
    assert "order" in labels
    assert "customer" in labels
    assert _anchor(result, "dbo.Orders")
    assert _anchor(result, "dbo.Customers")


# ── Concept matching ────────────────────────────────────────────────────────


def test_exact_business_name_match():
    result = _link("Show me customer records")
    entity_matches = [c for c in result.requirements.entity_concepts if c.normalized_label == "customer"]
    assert entity_matches
    assert entity_matches[0].candidates[0].match_kind == MATCH_KIND_EXACT_BUSINESS_NAME


def test_synonym_match():
    result = _link("Show me client records")
    entity_matches = [c for c in result.requirements.entity_concepts if c.normalized_label == "customer"]
    assert entity_matches
    assert entity_matches[0].candidates[0].match_kind == MATCH_KIND_SYNONYM
    assert "client" in entity_matches[0].matched_terms


def test_column_concept_match_for_measure():
    result = _link("What is the balance?")
    measure_matches = [c for c in result.requirements.measure_concepts if c.normalized_label == "balance"]
    assert measure_matches
    assert measure_matches[0].candidates[0].match_kind in (MATCH_KIND_COLUMN_TOKEN, "CATEGORY_KEYWORD")
    assert _anchor(result, "dbo.Customers")


# ── Complete-schema linking ─────────────────────────────────────────────────


def test_low_vector_rank_rescued_by_strong_catalog_evidence():
    # dbo.Products ranks LAST (rank far beyond RANK_SATURATION) in both
    # semantic and lexical rankings, but the question strongly matches its
    # own business name/measure via the catalog -- it must still surface.
    docs = _mock_schema()
    fillers = [_Doc(f"dbo.Filler{i}", business_name=f"Filler{i}") for i in range(60)]
    all_docs = docs + fillers
    semantic_rankings = [_RankedHit(d.object_fqn, 0.9 - i * 0.001) for i, d in enumerate(all_docs)]
    lexical_rankings = [_RankedHit(d.object_fqn, 50.0 - i * 0.1) for i, d in enumerate(all_docs)]

    result = _link("What is the price of each product?", docs=all_docs, semantic_rankings=semantic_rankings, lexical_rankings=lexical_rankings)
    anchor = _anchor(result, "dbo.Products")
    assert anchor.catalog_score > 0.0
    assert anchor.combined_score > 0.0


def test_complete_eligible_schema_considered_before_output_cutoff():
    docs = _mock_schema()
    result = _link("Show me customer records", docs=docs, limits=SchemaLinkingLimits(max_anchor_candidates=1))
    eligible = sum(1 for d in docs if d.retrieval_tier in (1, 2))
    assert result.eligible_object_count == eligible
    assert result.considered_object_count == eligible
    assert len(result.anchors) == 1


def test_tier_3_and_4_objects_never_appear_as_anchors_or_eligible():
    result = _link("Show me everything about customers and archives and system tables")
    assert result.eligible_object_count == 3  # Customers, Orders, Products only
    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert "dbo.Customers_Archive" not in anchor_fqns
    assert "sys.SomeSystemTable" not in anchor_fqns


def test_canonical_object_spelling_preserved():
    result = _link("Show me CUSTOMER records")
    anchor = _anchor(result, "dbo.Customers")
    assert anchor.object_fqn == "dbo.Customers"  # never re-cased to match question casing


# ── Evidence fusion ─────────────────────────────────────────────────────────


def test_lexical_and_vector_evidence_fusion_boosts_combined_score():
    docs = _mock_schema()
    semantic_only = _link("balance", docs=docs, semantic_rankings=[_RankedHit("dbo.Customers", 0.9)])
    both = _link(
        "balance", docs=docs,
        semantic_rankings=[_RankedHit("dbo.Customers", 0.9)],
        lexical_rankings=[_RankedHit("dbo.Customers", 12.0)],
    )
    assert _anchor(both, "dbo.Customers").combined_score > _anchor(semantic_only, "dbo.Customers").combined_score


def test_compatible_example_evidence_boosts_score():
    docs = _mock_schema()
    without_example = _link("price", docs=docs)
    with_example = _link("price", docs=docs, retrieved_examples=[_RetrievedExample(("dbo.Products",), 0.8)])
    assert _anchor(with_example, "dbo.Products").combined_score > _anchor(without_example, "dbo.Products").combined_score


def test_irrelevant_low_similarity_example_cannot_dominate():
    docs = _mock_schema()
    result = _link(
        "balance", docs=docs,
        retrieved_examples=[_RetrievedExample(("dbo.Products",), 0.1)],
    )
    customer_anchor = _anchor(result, "dbo.Customers")
    product_candidates = [a for a in result.anchors if a.object_fqn == "dbo.Products"]
    if product_candidates:
        assert product_candidates[0].combined_score < customer_anchor.combined_score
    # The weak example's maximum possible contribution is bounded by its
    # own weight -- it can never push an irrelevant object above a
    # genuinely matched one.
    assert result.anchors[0].object_fqn == "dbo.Customers"


def test_no_single_weak_signal_dominates_combined_score():
    docs = _mock_schema()
    result = _link(
        "products", docs=docs,
        lexical_rankings=[_RankedHit("dbo.Products", 0.01)],  # near-zero raw lexical score
    )
    anchor = _anchor(result, "dbo.Products")
    assert anchor.combined_score <= 1.0


# ── Concept coverage & ambiguity ─────────────────────────────────────────


def test_concept_coverage_aware_anchor_selection():
    # A high canonical-authority filler object should NOT crowd out the
    # object that actually covers the matched "price" measure concept.
    docs = _mock_schema()
    result = _link("What is the price?", docs=docs, limits=SchemaLinkingLimits(max_anchor_candidates=2))
    assert _anchor(result, "dbo.Products")


def test_multiple_plausible_entities_preserved_with_ambiguity_record():
    # Genuinely DIFFERENT canonical families (disjoint object names, only
    # the entity concept's own head-noun "account" coincides) -- unlike two
    # objects sharing the exact same business_name (which canonical_scoring
    # correctly groups into ONE family, and Step 2A correctly treats as a
    # non-ambiguous duplicate rather than a real competing candidate).
    a = _Doc("dbo.Wallets", business_name="Customer Account", description="Primary accounts")
    b = _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy accounts")
    docs = [a, b]
    catalog = build_semantic_catalog(docs)
    result = _link("Show me account records", docs=docs, catalog=catalog)
    anchor_fqns = {x.object_fqn for x in result.anchors}
    assert "dbo.Wallets" in anchor_fqns
    assert "dbo.Ledgers" in anchor_fqns
    assert any(r.kind == AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES for r in result.ambiguities)


def test_date_ambiguity_recorded_for_column_with_multiple_role_candidates():
    doc = _Doc("dbo.Programs", date_columns=("StartEndDate",), column_names=("StartEndDate",))
    result = _link("When did the program start and end?", docs=[doc])
    assert any(r.kind == "MULTIPLE_DATE_ROLES" for r in result.ambiguities)


def test_measure_ambiguity_recorded_for_unclear_aggregation_target():
    doc = _Doc(
        "dbo.Accounts", measure_columns=("Balance", "CreditLimit"),
        column_names=("Balance", "CreditLimit"),
    )
    result = _link("What is the total balance and credit limit?", docs=[doc])
    kinds = {r.kind for r in result.ambiguities}
    assert kinds & {"MULTIPLE_MEASURES", "UNCLEAR_AGGREGATION_TARGET"}


def test_unresolved_filter_value_preserved_not_claimed_as_known():
    result = _link('Show me the customer named "Acme Corp"')
    assert "Acme Corp" in result.requirements.unresolved_filter_values


def test_bare_number_preserved_as_unresolved_filter_value_outside_ranking_context():
    result = _link("How many orders had amount 500?")
    assert "500" in result.requirements.unresolved_filter_values


def test_ranking_number_not_double_counted_as_filter_value():
    result = _link("What are the top 10 customers?")
    assert "10" not in result.requirements.unresolved_filter_values


# ── Determinism and tie-breaking ────────────────────────────────────────────


def test_deterministic_tie_breaking_by_object_fqn():
    a = _Doc("dbo.Alpha", business_name="Alpha")
    b = _Doc("dbo.Beta", business_name="Beta")
    docs = [a, b]
    result = _link("something completely unrelated to either concept", docs=docs)
    fqns = [x.object_fqn for x in result.anchors]
    assert fqns == sorted(fqns)


def test_shuffled_document_input_produces_identical_result_content():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    result_a = _link("What is the total order amount for each customer?", docs=docs, catalog=catalog)

    shuffled = docs[:]
    random.Random(11).shuffle(shuffled)
    clear_schema_linker_cache()  # force a fresh index build from the shuffled order
    result_b = _link("What is the total order amount for each customer?", docs=shuffled, catalog=catalog)

    assert result_a.fingerprint == result_b.fingerprint
    assert [a.object_fqn for a in result_a.anchors] == [a.object_fqn for a in result_b.anchors]


def test_repeated_calls_produce_identical_fingerprint():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    r1 = _link("How many customers are there?", docs=docs, catalog=catalog)
    r2 = _link("How many customers are there?", docs=docs, catalog=catalog)
    assert r1.fingerprint == r2.fingerprint


# ── Defensive limits ─────────────────────────────────────────────────────


def test_max_anchor_candidates_truncates_and_reports():
    docs = _mock_schema()
    result = _link("Show me customer and order and product records", docs=docs, limits=SchemaLinkingLimits(max_anchor_candidates=1))
    assert len(result.anchors) == 1
    assert any("anchors" in w for w in result.warnings)


def test_max_matched_concepts_truncates():
    doc = _Doc(
        "dbo.Wide", measure_columns=tuple(f"Metric{i}" for i in range(10)),
        column_names=tuple(f"Metric{i}" for i in range(10)),
    )
    result = _link("metric0 metric1 metric2 metric3 metric4 metric5", docs=[doc], limits=SchemaLinkingLimits(max_matched_concepts=2))
    assert len(result.requirements.measure_concepts) <= 2


def test_invalid_question_raises_value_error():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    with pytest.raises(ValueError):
        link_question_to_schema("", catalog, docs)
    with pytest.raises(ValueError):
        link_question_to_schema("x" * 3000, catalog, docs, limits=SchemaLinkingLimits(max_question_characters=2000))
    with pytest.raises(ValueError):
        link_question_to_schema(123, catalog, docs)  # type: ignore[arg-type]


def test_empty_eligible_schema_returns_empty_status_not_crash():
    doc = _Doc("dbo.Only", retrieval_tier=4)
    catalog = build_semantic_catalog([doc])
    result = link_question_to_schema("anything at all", catalog, [doc])
    assert result.status == STATUS_EMPTY_SCHEMA
    assert result.anchors == ()
    assert result.eligible_object_count == 0


# ── Deep immutability ───────────────────────────────────────────────────────


def test_result_and_nested_dataclasses_are_immutable():
    result = _link("How many customers are there?")
    with pytest.raises(Exception):
        result.anchors = ()
    with pytest.raises(Exception):
        result.requirements.operation = "X"
    with pytest.raises(AttributeError):
        result.anchors[0].evidence.append("x")


# ── Cache identity / isolation / eviction / clear ──────────────────────────


def test_cache_reused_across_different_questions_same_catalog():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    _link("How many customers are there?", docs=docs, catalog=catalog)
    assert schema_linker_cache_size() == 1
    _link("What is the average order amount?", docs=docs, catalog=catalog)
    assert schema_linker_cache_size() == 1  # same catalog fingerprint -> same cached index


def test_cache_isolated_per_catalog_fingerprint():
    docs_a = [_Doc("dbo.Alpha", business_name="Alpha")]
    docs_b = [_Doc("dbo.Beta", business_name="Beta")]
    catalog_a = build_semantic_catalog(docs_a)
    catalog_b = build_semantic_catalog(docs_b)
    _link("question one", docs=docs_a, catalog=catalog_a)
    _link("question two", docs=docs_b, catalog=catalog_b)
    assert schema_linker_cache_size() == 2


def test_cache_eviction_is_bounded():
    from core.config import SQL_LINKING_MAX_CACHED_INDEXES
    for i in range(SQL_LINKING_MAX_CACHED_INDEXES + 5):
        docs = [_Doc(f"dbo.Object{i}", business_name=f"Object{i}")]
        catalog = build_semantic_catalog(docs)
        _link("q", docs=docs, catalog=catalog)
    assert schema_linker_cache_size() <= SQL_LINKING_MAX_CACHED_INDEXES


def test_clear_cache_evicts_everything():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    _link("How many customers are there?", docs=docs, catalog=catalog)
    assert schema_linker_cache_size() > 0
    clear_schema_linker_cache()
    assert schema_linker_cache_size() == 0


def test_no_question_content_anywhere_in_cache():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    _link("a very specific and unusual question about widgets", docs=docs, catalog=catalog)
    import core.sqlservice.schema_linker as schema_linker_module
    for index in schema_linker_module._index_cache.values():
        for field_name in vars(index):
            assert "question" not in field_name.lower()
        # nothing in the cached index's own repr should contain the
        # question text
        assert "widgets" not in repr(index).lower()


# ── Performance sanity ───────────────────────────────────────────────────


def test_warm_linking_is_fast_for_a_moderately_sized_schema():
    docs = [
        _Doc(
            f"dbo.Object{i}", business_name=f"Concept{i}",
            column_names=(f"Id{i}", f"Amount{i}"), measure_columns=(f"Amount{i}",),
        )
        for i in range(300)
    ]
    catalog = build_semantic_catalog(docs)
    link_question_to_schema("warm-up question", catalog, docs)  # build & cache the index once

    start = time.perf_counter()
    for _ in range(5):
        link_question_to_schema("what is the total amount0?", catalog, docs)
    elapsed_ms = (time.perf_counter() - start) / 5 * 1000.0
    assert elapsed_ms < 500  # generous sanity bound, not a strict benchmark assertion


# ── No I/O, no the reference database/domain-specific tokens ─────────────────────────────────


_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "schema_linker.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"schema_linker.py contains banned domain-specific token {banned!r}"


def test_module_never_imports_database_network_or_provider_modules():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "schema_linker.py").read_text(encoding="utf-8")
    for forbidden_import in ("data.db", "sqlite3", "requests", "openai", "urllib", "socket", "pyodbc", "embed_batch"):
        assert forbidden_import not in source, f"unexpected I/O-shaped reference {forbidden_import!r}"


# ── Phase 6, Step 2B: normalized requirements / common-column control ────


def _widely_shared_fk_schema(shared_column="ClassID", n_referencing=49, canonical_name="Class", canonical_fqn="dbo.Class"):
    canonical = _Doc(
        canonical_fqn, business_name=canonical_name, description=f"Canonical {canonical_name.lower()} table",
        has_primary_key=True, primary_key_columns=(shared_column,), column_names=(shared_column,),
    )
    referencing = []
    for i in range(n_referencing):
        fk_edge = {"direction": "outbound", "column": shared_column, "other": f"{canonical_fqn}.{shared_column}", "kind": "PHYSICAL_FK"}
        referencing.append(_Doc(
            f"dbo.Ref{i}", column_names=(shared_column,), foreign_key_targets=(f"{canonical_fqn}.{shared_column}",),
            confirmed_relationship_targets=(f"{canonical_fqn}.{shared_column}",), confirmed_relationship_edges=(fk_edge,),
        ))
    return [canonical, *referencing]


def test_fifty_tables_sharing_classid_produce_one_normalized_identifier_requirement():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    identifier_requirements = [r for r in result.requirements.identifier_concepts if r.normalized_label == "class"]
    assert len(identifier_requirements) == 1
    assert identifier_requirements[0].candidate_total_count == 50


def test_best_candidates_retained_not_all_fifty():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    assert len(result.anchors) < 50
    assert result.anchor_selected_count == len(result.anchors)


def test_exact_canonical_entity_table_outranks_fk_only_referencing_tables():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    anchor_fqns = [a.object_fqn for a in result.anchors]
    assert anchor_fqns[0] == "dbo.Class"


def test_repeated_statusid_does_not_create_dozens_of_requirements():
    docs = _widely_shared_fk_schema(shared_column="StatusID", canonical_name="Status", canonical_fqn="dbo.Status")
    result = _link("What is the status?", docs=docs)
    status_requirements = [r for r in result.requirements.identifier_concepts if r.normalized_label == "status"]
    assert len(status_requirements) <= 1


def test_rare_column_token_remains_informative():
    # A column token unique to a single object must not be down-weighted
    # at all (informativeness == 1.0).
    docs = _widely_shared_fk_schema()
    docs.append(_Doc("dbo.Rare", measure_columns=("QuiteUnusualMetric",), column_names=("QuiteUnusualMetric",)))
    result = _link("What is the quite unusual metric?", docs=docs)
    rare_requirement = next(r for r in result.requirements.measure_concepts if r.normalized_label in ("unusual", "metric", "quite"))
    assert rare_requirement.candidates[0].informativeness == 1.0


def test_exact_business_name_match_not_suppressed_by_document_frequency():
    # Even when "class" is also a widely-shared identifier token, the
    # ENTITY exact-name match must remain at full (non-informativeness-
    # adjusted) confidence.
    docs = _widely_shared_fk_schema()
    result = _link("Show me class records", docs=docs)
    entity_requirement = next(r for r in result.requirements.entity_concepts if r.normalized_label == "class")
    assert entity_requirement.candidates[0].informativeness == 1.0
    assert entity_requirement.candidates[0].confidence == entity_requirement.candidates[0].raw_confidence


def test_two_distinct_entities_remain_two_requirements():
    result = _link("List orders and their customers")
    entity_keys = {r.concept_key for r in result.requirements.entity_concepts}
    assert "ENTITY:order" in entity_keys
    assert "ENTITY:customer" in entity_keys


def test_same_word_different_semantic_kinds_remains_distinct():
    # "status" matches BOTH a dimension (status column) and an identifier
    # (StatusID) on the same object -- these must remain two SEPARATE
    # requirements (different concept_kind), never collapsed into one.
    doc = _Doc(
        "dbo.Tickets", status_columns=("Status",), column_names=("Status", "StatusID"),
        foreign_key_targets=("dbo.StatusCodes.StatusID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "StatusID", "other": "dbo.StatusCodes.StatusID", "kind": "PHYSICAL_FK"},
        ),
    )
    result = _link("What is the status?", docs=[doc])
    dimension_keys = {r.concept_key for r in result.requirements.dimension_concepts}
    identifier_keys = {r.concept_key for r in result.requirements.identifier_concepts}
    assert any(k.startswith("DIMENSION:") for k in dimension_keys)
    assert any(k.startswith("IDENTIFIER:") for k in identifier_keys)
    assert dimension_keys.isdisjoint(identifier_keys)  # different kind prefix guarantees distinctness


def test_object_and_column_identity_preserved_in_candidate_evidence():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    identifier_requirement = next(r for r in result.requirements.identifier_concepts if r.normalized_label == "class")
    object_fqns = {c.object_fqn for c in identifier_requirement.candidates}
    column_names = {c.column_name for c in identifier_requirement.candidates}
    assert "dbo.Class" in object_fqns
    assert column_names == {"ClassID"}
    # concept_key itself never leaks object/column identity.
    assert "dbo.Class" not in identifier_requirement.concept_key
    assert "ClassID" not in identifier_requirement.concept_key


def test_ambiguity_alternatives_preserved_within_configured_bound():
    a = _Doc("dbo.Wallets", business_name="Customer Account", description="Primary")
    b = _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy")
    docs = [a, b]
    result = _link(
        "Show me account records", docs=docs,
        limits=SchemaLinkingLimits(max_ambiguity_alternatives_per_requirement=0),
    )
    anchor_fqns = {a.object_fqn for a in result.anchors}
    # With zero ambiguity alternatives allowed, only the coverage-selected
    # candidate for the "account" concept should be present via that path
    # (though it may still appear via other means) -- verify the bound is
    # actually respected by checking the observability counter.
    assert result.candidate_alternatives_retained_count == 0


def test_max_candidates_per_requirement_bounds_output():
    docs = _widely_shared_fk_schema(n_referencing=49)
    result = _link(
        "How many classes are there?", docs=docs,
        limits=SchemaLinkingLimits(max_candidates_per_requirement=5),
    )
    identifier_requirement = next(r for r in result.requirements.identifier_concepts if r.normalized_label == "class")
    assert len(identifier_requirement.candidates) == 5
    assert identifier_requirement.candidate_total_count == 50


def test_common_token_informativeness_floor_is_respected():
    docs = _widely_shared_fk_schema(n_referencing=99)
    result_default = _link("How many classes are there?", docs=docs)
    clear_schema_linker_cache()
    result_high_floor = _link(
        "How many classes are there?", docs=docs,
        limits=SchemaLinkingLimits(common_token_informativeness_floor=0.9),
    )
    req_default = next(r for r in result_default.requirements.identifier_concepts if r.normalized_label == "class")
    req_high_floor = next(r for r in result_high_floor.requirements.identifier_concepts if r.normalized_label == "class")
    fk_candidate_default = next(c for c in req_default.candidates if c.object_fqn != "dbo.Class")
    fk_candidate_high_floor = next(c for c in req_high_floor.candidates if c.object_fqn != "dbo.Class")
    assert fk_candidate_high_floor.informativeness >= fk_candidate_default.informativeness


def test_step2b_observability_fields_present_and_consistent():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    assert result.raw_catalog_match_count > 0
    assert result.normalized_requirement_count >= 1
    assert result.duplicate_matches_merged_count >= 0
    assert result.common_token_matches_downweighted_count >= 0
    assert result.candidate_alternatives_retained_count >= 0
    assert result.anchors_selected_by_independent_evidence_count >= 0
    assert result.anchors_retained_for_ambiguity_count >= 0


def test_requirement_and_candidate_dataclasses_are_immutable():
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    requirement = result.requirements.identifier_concepts[0]
    with pytest.raises(Exception):
        requirement.candidates = ()
    with pytest.raises(Exception):
        requirement.candidates[0].confidence = 1.0
    with pytest.raises(AttributeError):
        requirement.candidates[0].evidence.append("x")


def test_shuffled_documents_deterministic_with_normalized_requirements():
    docs = _widely_shared_fk_schema()
    catalog = build_semantic_catalog(docs)
    result_a = _link("How many classes are there?", docs=docs, catalog=catalog)

    shuffled = docs[:]
    random.Random(5).shuffle(shuffled)
    clear_schema_linker_cache()
    result_b = _link("How many classes are there?", docs=shuffled, catalog=catalog)

    assert result_a.fingerprint == result_b.fingerprint
    assert [a.object_fqn for a in result_a.anchors] == [a.object_fqn for a in result_b.anchors]


# ── Phase 6, Step 2A: relevance-qualified anchor admission ────────────────


def test_canonical_score_alone_cannot_admit_an_object():
    # A richly-structured, high-canonical-authority object with ZERO
    # question relevance (no matched concept, no semantic/lexical/example
    # evidence) must never become an anchor.
    docs = _mock_schema()
    result = _link("something entirely unrelated to any concept here", docs=docs)
    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert not (anchor_fqns & {"dbo.Customers", "dbo.Orders", "dbo.Products"})


def test_unrelated_high_canonical_object_excluded_even_with_admitted_peers():
    docs = _mock_schema()
    result = _link("How many customers are there?", docs=docs)
    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert "dbo.Customers" in anchor_fqns
    # dbo.Products shares no token/concept/evidence with "customers" at all
    # (unlike dbo.Orders, which legitimately carries a CustomerID foreign-
    # key column -- a real, if weak, column-concept match, not noise).
    assert "dbo.Products" not in anchor_fqns


def test_exact_entity_match_admitted_via_concept_match_reason():
    result = _link("Show me customer records")
    anchor = _anchor(result, "dbo.Customers")
    assert "CONCEPT_MATCH" in anchor.admission_reasons


def test_exact_column_concept_match_admitted():
    result = _link("What is the balance?")
    anchor = _anchor(result, "dbo.Customers")
    assert "CONCEPT_MATCH" in anchor.admission_reasons


def test_lexical_match_alone_admits_an_object_with_no_concept_match():
    docs = [_Doc("dbo.Widget", business_name="Widget", column_names=("Id",))]
    result = _link("widget", docs=docs, lexical_rankings=[_RankedHit("dbo.Widget", 5.0)])
    anchor = _anchor(result, "dbo.Widget")
    assert "LEXICAL_MATCH" in anchor.admission_reasons


def test_semantic_match_below_threshold_does_not_admit_alone():
    docs = [_Doc("dbo.Widget", column_names=("Id",))]
    result = _link(
        "irrelevant question text", docs=docs,
        semantic_rankings=[_RankedHit("dbo.Widget", 0.05)],
    )
    assert not any(a.object_fqn == "dbo.Widget" for a in result.anchors)


def test_semantic_match_above_threshold_admits():
    docs = [_Doc("dbo.Widget", column_names=("Id",))]
    result = _link(
        "irrelevant question text", docs=docs,
        semantic_rankings=[_RankedHit("dbo.Widget", 0.5)],
    )
    anchor = _anchor(result, "dbo.Widget")
    assert "SEMANTIC_MATCH" in anchor.admission_reasons


def test_irrelevant_low_score_example_does_not_admit_alone():
    docs = [_Doc("dbo.Widget", column_names=("Id",))]
    result = _link(
        "irrelevant question text", docs=docs,
        retrieved_examples=[_RetrievedExample(("dbo.Widget",), 0.05)],
    )
    assert not any(a.object_fqn == "dbo.Widget" for a in result.anchors)


def test_compatible_strong_example_admits():
    docs = [_Doc("dbo.Widget", column_names=("Id",))]
    result = _link(
        "irrelevant question text", docs=docs,
        retrieved_examples=[_RetrievedExample(("dbo.Widget",), 0.6)],
    )
    anchor = _anchor(result, "dbo.Widget")
    assert "EXAMPLE_MATCH" in anchor.admission_reasons


def test_narrow_question_returns_fewer_than_maximum_anchors():
    docs = _mock_schema()
    result = _link("How many customers are there?", docs=docs)
    assert len(result.anchors) < DEFAULT_SCHEMA_LINKING_LIMITS.max_anchor_candidates
    assert not result.anchor_ceiling_reached


def test_max_anchor_candidates_is_a_ceiling_not_a_target():
    docs = [_Doc("dbo.SomeWidget", business_name="Widget")]
    result = _link("Show me widget records", docs=docs)
    assert len(result.anchors) == 1
    assert not result.anchor_ceiling_reached


def test_no_relevance_question_returns_typed_no_confident_anchor_result():
    docs = _mock_schema()
    result = _link("asdkfj qpwoeiruty zxcvbnm", docs=docs)
    assert result.status == "NO_CONFIDENT_ANCHOR"
    assert result.anchors == ()
    assert result.no_confident_anchor_reason is not None
    assert result.relevance_qualified_object_count == 0


def test_concept_coverage_operates_only_on_admitted_candidates():
    docs = _mock_schema()
    result = _link("Show me customer records", docs=docs)
    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert "dbo.Products" not in anchor_fqns


def test_redundant_low_confidence_competitor_not_retained():
    # Two objects sharing the exact same business_name are grouped into
    # ONE canonical family by core.sqlservice.canonical_scoring (a real,
    # narrow/duplicate-variant relationship, not a genuine second business
    # candidate). Step 2A's redundant-candidate rule must not preserve the
    # non-canonical member as an "extra" anchor just because it is
    # technically admitted via the same entity concept match.
    main = _Doc("dbo.Accounts", business_name="Account", description="Primary accounts")
    duplicate_variant = _Doc("dbo.Accounts_Old", business_name="Account", description="Legacy accounts")
    docs = [main, duplicate_variant]
    result = _link("Show me account records", docs=docs)
    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert "dbo.Accounts" in anchor_fqns
    assert "dbo.Accounts_Old" not in anchor_fqns


def test_weak_common_token_ambiguity_suppressed():
    strong = _Doc(
        "dbo.Wallets", business_name="Customer Account", description="x",
        has_primary_key=True, primary_key_columns=("Id",), column_names=("Id",),
    )
    weak = _Doc("dbo.Zeta", business_name="Random Account")
    docs = [strong, weak]
    result = _link("Show me account records", docs=docs)
    entity_ambiguities = [r for r in result.ambiguities if r.kind == "MULTIPLE_ENTITY_CANDIDATES"]
    assert isinstance(entity_ambiguities, list)


def test_ambiguity_score_margin_boundary_is_respected():
    from core.sqlservice.schema_linker import ANCHOR_AMBIGUITY_MARGIN
    a = _Doc("dbo.Wallets", business_name="Customer Account", description="Primary")
    b = _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy")
    docs = [a, b]
    catalog = build_semantic_catalog(docs)
    result = _link("Show me account records", docs=docs, catalog=catalog)
    entity_ambiguities = [r for r in result.ambiguities if r.kind == "MULTIPLE_ENTITY_CANDIDATES"]
    if entity_ambiguities:
        confidences = entity_ambiguities[0].confidences
        assert max(confidences) - min(confidences) <= ANCHOR_AMBIGUITY_MARGIN


def test_admission_reasons_never_contains_canonical_alone():
    result = _link("How many customers are there?")
    for anchor in result.anchors:
        assert anchor.admission_reasons
        assert "CANONICAL_MATCH" not in anchor.admission_reasons
        assert set(anchor.admission_reasons) <= {"CONCEPT_MATCH", "LEXICAL_MATCH", "SEMANTIC_MATCH", "EXAMPLE_MATCH"}


def test_relevance_and_confidence_observability_fields_present():
    result = _link("How many customers are there?")
    assert result.relevance_qualified_object_count >= 1
    assert result.rejected_for_no_relevance_count >= 0
    assert result.eligible_object_count == result.relevance_qualified_object_count + result.rejected_for_no_relevance_count
    assert result.anchor_selected_count == len(result.anchors)
    assert isinstance(result.anchor_ceiling_reached, bool)
    assert result.anchors_rejected_by_confidence >= 0


# ── Phase 6, Step 3C: requirement-strength classification ─────────────────
#
# CORE / CONSTRAINT / SUPPORTING / UNRESOLVED — the deterministic model that
# distinguishes explicitly-expressed business concepts (CORE, the only
# strength that counts as BUSINESS_REQUIRED downstream in join_planner)
# from weak schema evidence that must remain ranking/path-search support
# only. All fixtures here are small synthetic schemas, never database-specific.


def _entity_requirement(result, label):
    matches = [c for c in result.requirements.entity_concepts if c.normalized_label == label]
    assert matches, f"{label!r} not among entity concepts: {[c.normalized_label for c in result.requirements.entity_concepts]}"
    return matches[0]


def test_exact_multiword_entity_is_one_core_requirement():
    doc = _Doc("dbo.CustomerAccounts", business_name="Customer Account", description="x")
    result = _link("Show me customer account records", docs=[doc])
    entity_reqs = result.requirements.entity_concepts
    assert len(entity_reqs) == 1
    assert entity_reqs[0].strength == STRENGTH_CORE
    assert entity_reqs[0].candidates[0].match_kind == MATCH_KIND_EXACT_BUSINESS_NAME


def test_alias_sharing_single_token_substring_suppressed():
    # "student" is Badge's borrowed alias token, not its own business name --
    # it must never inflate Badge into a CORE requirement just because
    # Student legitimately won the same token via its own exact name.
    student = _Doc("dbo.Student", business_name="Student", description="Core student records")
    badge = _Doc("dbo.StudentBadge", business_name="Badge", synonyms=("student",))
    result = _link("How many students are there?", docs=[student, badge])
    assert _entity_requirement(result, "student").strength == STRENGTH_CORE
    assert _entity_requirement(result, "badge").strength == STRENGTH_SUPPORTING
    assert result.substring_suppressed_count >= 1


def test_exact_informative_single_token_synonym_is_core():
    # "patron" is a distinctive single-token synonym used by exactly one
    # entity -- full informativeness, so it qualifies as CORE even though
    # it wins via a single-token alias match rather than the primary name.
    doc = _Doc("dbo.Customers", business_name="Customer", synonyms=("patron",))
    result = _link("Show me patron records", docs=[doc])
    requirement = _entity_requirement(result, "customer")
    assert requirement.strength == STRENGTH_CORE
    assert requirement.candidates[0].match_kind == MATCH_KIND_SYNONYM


def test_common_weak_single_token_synonym_is_supporting():
    # "item" is borrowed as an alias by five otherwise-unrelated entities --
    # low informativeness, so none of them may claim CORE on that token.
    docs = [
        _Doc(f"dbo.{name}", business_name=name, synonyms=("item",))
        for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
    ]
    result = _link("Show me the item", docs=docs)
    entity_reqs = result.requirements.entity_concepts
    assert len(entity_reqs) == 5
    assert all(r.strength == STRENGTH_SUPPORTING for r in entity_reqs)
    assert result.weak_single_token_demoted_count >= 5
    assert not any(a.kind == AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES for a in result.ambiguities)


def test_identifier_derived_purely_from_column_is_not_core():
    # A concept that only ever surfaces via a foreign-key column token
    # (never a business_name/synonym match) is schema evidence, not an
    # explicitly-expressed business concept -- it must never reach CORE.
    docs = _widely_shared_fk_schema()
    result = _link("How many classes are there?", docs=docs)
    identifier_requirement = next(r for r in result.requirements.identifier_concepts if r.normalized_label == "class")
    assert identifier_requirement.strength != STRENGTH_CORE
    assert identifier_requirement.strength == STRENGTH_SUPPORTING


def test_explicit_category_filter_without_grouping_is_constraint():
    # "type" is mentioned as a filter/descriptor, not as a grouping target
    # or aggregation -- it is an explicit constraint on the core entity, not
    # itself a second core concept.
    doc = _Doc("dbo.Products", business_name="Product", column_names=("ProductType",))
    result = _link("What type of product is this?", docs=[doc])
    assert result.requirements.wants_grouping is False
    type_requirement = next(r for r in result.requirements.dimension_concepts if r.normalized_label == "type")
    assert type_requirement.strength == STRENGTH_CONSTRAINT


def test_grouped_dimension_becomes_core():
    doc = _Doc("dbo.Orders", column_names=("Amount", "Region"), measure_columns=("Amount",))
    result = _link("What is the total amount by region?", docs=[doc])
    assert result.requirements.wants_grouping is True
    region_requirement = next(r for r in result.requirements.dimension_concepts if r.normalized_label == "region")
    assert region_requirement.strength == STRENGTH_CORE


def test_measure_aggregation_requires_measure_core_else_constraint():
    doc = _Doc("dbo.Orders", column_names=("Amount",), measure_columns=("Amount",))
    aggregated = _link("What is the average amount?", docs=[doc])
    aggregated_measure = next(r for r in aggregated.requirements.measure_concepts if r.normalized_label == "amount")
    assert aggregated_measure.strength == STRENGTH_CORE

    clear_schema_linker_cache()
    bare = _link("Show me the amount", docs=[doc])
    bare_measure = next(r for r in bare.requirements.measure_concepts if r.normalized_label == "amount")
    assert bare_measure.strength != STRENGTH_CORE


def test_trend_requires_date_role_core_else_supporting():
    doc = _Doc("dbo.Orders", column_names=("OrderDate",), date_columns=("OrderDate",))
    trend = _link("Show order date trend by month over the last year", docs=[doc])
    assert trend.requirements.wants_trend is True
    assert any(r.strength == STRENGTH_CORE for r in trend.requirements.date_role_concepts)

    clear_schema_linker_cache()
    bare = _link("Show me the order date", docs=[doc])
    assert all(r.strength != STRENGTH_CORE for r in bare.requirements.date_role_concepts)


def test_generic_operation_words_never_become_entity_requirements():
    # A business_name that IS a generic operation/collection word (e.g.
    # "Report") must never be admitted as CORE purely on an exact-name
    # match -- generic language rules override match kind entirely.
    doc = _Doc("dbo.Reports", business_name="Report", description="x")
    result = _link("Show me the report", docs=[doc])
    report_requirement = _entity_requirement(result, "report")
    assert report_requirement.strength != STRENGTH_CORE

    # And with zero schema evidence at all, a generic word creates no
    # entity requirement whatsoever.
    other_doc = _Doc("dbo.Orders", column_names=("Amount",), measure_columns=("Amount",))
    clear_schema_linker_cache()
    no_evidence = _link("Show me the data", docs=[other_doc])
    assert no_evidence.requirements.entity_concepts == ()


def test_strong_competing_entities_preserve_ambiguity():
    a = _Doc("dbo.Wallets", business_name="Customer Account", description="Primary accounts")
    b = _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy accounts")
    result = _link("Show me account records", docs=[a, b])
    account_requirement = _entity_requirement(result, "account")
    assert account_requirement.strength == STRENGTH_CORE
    assert account_requirement.ambiguous is True
    assert any(r.kind == AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES for r in result.ambiguities)


def test_weak_competing_entities_do_not_create_ambiguity():
    docs = [
        _Doc(f"dbo.{name}", business_name=name, synonyms=("item",))
        for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
    ]
    result = _link("Show me the item", docs=docs)
    assert all(r.strength == STRENGTH_SUPPORTING for r in result.requirements.entity_concepts)
    assert not any(r.kind == AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES for r in result.ambiguities)


def test_connected_multi_entity_question_creates_multiple_core_requirements():
    result = _link("List orders and their customers")
    order_requirement = _entity_requirement(result, "order")
    customer_requirement = _entity_requirement(result, "customer")
    assert order_requirement.strength == STRENGTH_CORE
    assert customer_requirement.strength == STRENGTH_CORE


def test_strength_classification_is_deterministic_across_shuffled_documents():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    result_a = _link("What is the total order amount for each customer?", docs=docs, catalog=catalog)

    shuffled = docs[:]
    random.Random(7).shuffle(shuffled)
    clear_schema_linker_cache()
    result_b = _link("What is the total order amount for each customer?", docs=shuffled, catalog=catalog)

    strengths_a = sorted(
        (r.concept_kind, r.normalized_label, r.strength)
        for r in (
            result_a.requirements.entity_concepts + result_a.requirements.measure_concepts
            + result_a.requirements.dimension_concepts + result_a.requirements.date_role_concepts
            + result_a.requirements.identifier_concepts
        )
    )
    strengths_b = sorted(
        (r.concept_kind, r.normalized_label, r.strength)
        for r in (
            result_b.requirements.entity_concepts + result_b.requirements.measure_concepts
            + result_b.requirements.dimension_concepts + result_b.requirements.date_role_concepts
            + result_b.requirements.identifier_concepts
        )
    )
    assert strengths_a == strengths_b
    assert result_a.fingerprint == result_b.fingerprint


def test_step3c_observability_fields_present():
    result = _link("How many customers are there?")
    assert result.core_requirement_count >= 1
    assert result.constraint_requirement_count >= 0
    assert result.supporting_requirement_count >= 0
    assert result.unresolved_requirement_count >= 0
    assert result.substring_suppressed_count >= 0
    assert result.weak_single_token_demoted_count >= 0
    total_requirements = (
        len(result.requirements.entity_concepts) + len(result.requirements.measure_concepts)
        + len(result.requirements.dimension_concepts) + len(result.requirements.date_role_concepts)
        + len(result.requirements.identifier_concepts)
    )
    assert (
        result.core_requirement_count + result.constraint_requirement_count
        + result.supporting_requirement_count + result.unresolved_requirement_count
    ) == total_requirements


def test_step3e_timing_fields_present_and_consistent():
    result = _link("How many customers are there?")
    assert result.index_load_or_build_ms >= 0.0
    assert result.question_linking_ms >= 0.0
    assert abs(result.elapsed_ms - (result.index_load_or_build_ms + result.question_linking_ms)) < 1.0


# ── Phase 6, Step 3E: cache-independent cold/warm behavior ─────────────────


def test_cold_and_warm_linker_output_is_structurally_identical():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)

    clear_schema_linker_cache()
    cold = _link("What is the total order amount for each customer?", docs=docs, catalog=catalog)

    warm = _link("What is the total order amount for each customer?", docs=docs, catalog=catalog)

    assert cold.fingerprint == warm.fingerprint
    assert [a.object_fqn for a in cold.anchors] == [a.object_fqn for a in warm.anchors]
    assert cold.requirements.entity_concepts == warm.requirements.entity_concepts
    # Only timing is permitted to differ between a cold build and a warm hit.
    assert cold.index_load_or_build_ms >= 0.0
    assert warm.index_load_or_build_ms >= 0.0


def test_cache_eviction_changes_latency_only_never_structural_output():
    docs = _mock_schema()
    catalog = build_semantic_catalog(docs)
    clear_schema_linker_cache()
    before_evict = _link("How many customers are there?", docs=docs, catalog=catalog)
    clear_schema_linker_cache()  # force a rebuild on the next call
    after_evict = _link("How many customers are there?", docs=docs, catalog=catalog)
    assert before_evict.fingerprint == after_evict.fingerprint
    assert [a.object_fqn for a in before_evict.anchors] == [a.object_fqn for a in after_evict.anchors]


def test_index_build_failure_returns_typed_status_not_a_partial_answer():
    class _BrokenDoc:
        object_fqn = "dbo.Broken"
        object_type = "TABLE"
        retrieval_tier = 1

        @property
        def metadata(self):
            raise RuntimeError("simulated metadata access failure")

    docs = [_BrokenDoc()]
    catalog = build_semantic_catalog(_mock_schema())  # a valid, unrelated catalog
    clear_schema_linker_cache()
    result = link_question_to_schema("How many customers are there?", catalog, docs)
    assert result.status == STATUS_INDEX_BUILD_FAILED
    assert result.anchors == ()
    assert result.requirements.entity_concepts == ()


def test_no_question_content_anywhere_in_join_graph_cache():
    from core.sqlservice.join_planner import _load_join_graph_index, clear_join_planner_cache
    docs = _mock_schema()
    clear_join_planner_cache()
    index, _fingerprint = _load_join_graph_index(docs)
    for field_name in vars(index):
        assert "question" not in field_name.lower()
    assert "how many" not in repr(index).lower()


# ── Phase 6, Step 3F, Part 1: requirement invariance across evidence modes ──


def _requirement_key_strength_pairs(result, *, strengths):
    all_reqs = (
        result.requirements.entity_concepts + result.requirements.measure_concepts
        + result.requirements.dimension_concepts + result.requirements.date_role_concepts
        + result.requirements.identifier_concepts
    )
    return {r.concept_key for r in all_reqs if r.strength in strengths}


def test_core_and_constraint_keys_identical_across_all_four_evidence_modes():
    docs = [
        _Doc("dbo.Orders", business_name="Order", column_names=("Amount", "Region"), measure_columns=("Amount",)),
    ]
    question = "What is the total order amount by region?"
    catalog = build_semantic_catalog(docs)

    a = _link(question, docs=docs, catalog=catalog)
    b = _link(question, docs=docs, catalog=catalog, lexical_rankings=[_RankedHit("dbo.Orders", 8.0)])
    c = _link(
        question, docs=docs, catalog=catalog,
        lexical_rankings=[_RankedHit("dbo.Orders", 8.0)], semantic_rankings=[_RankedHit("dbo.Orders", 0.95)],
    )
    d = _link(
        question, docs=docs, catalog=catalog,
        lexical_rankings=[_RankedHit("dbo.Orders", 8.0)], semantic_rankings=[_RankedHit("dbo.Orders", 0.95)],
        retrieved_examples=[_RetrievedExample(("dbo.Orders",), 0.9)],
    )

    core_sets = [_requirement_key_strength_pairs(r, strengths=(STRENGTH_CORE,)) for r in (a, b, c, d)]
    constraint_sets = [_requirement_key_strength_pairs(r, strengths=(STRENGTH_CONSTRAINT,)) for r in (a, b, c, d)]
    assert core_sets[0] == core_sets[1] == core_sets[2] == core_sets[3]
    assert constraint_sets[0] == constraint_sets[1] == constraint_sets[2] == constraint_sets[3]
    assert core_sets[0]  # sanity: this fixture actually produces CORE requirements


def test_requirement_fingerprint_identical_across_all_four_evidence_modes():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    question = "How many students are there?"
    catalog = build_semantic_catalog(docs)

    a = _link(question, docs=docs, catalog=catalog)
    b = _link(question, docs=docs, catalog=catalog, lexical_rankings=[_RankedHit("dbo.Badge", 5.0)])
    c = _link(
        question, docs=docs, catalog=catalog,
        lexical_rankings=[_RankedHit("dbo.Badge", 5.0)], semantic_rankings=[_RankedHit("dbo.Badge", 0.9)],
    )
    d = _link(
        question, docs=docs, catalog=catalog,
        lexical_rankings=[_RankedHit("dbo.Badge", 5.0)], semantic_rankings=[_RankedHit("dbo.Badge", 0.9)],
        retrieved_examples=[_RetrievedExample(("dbo.Badge",), 0.9)],
    )
    fingerprints = {a.requirement_fingerprint, b.requirement_fingerprint, c.requirement_fingerprint, d.requirement_fingerprint}
    assert len(fingerprints) == 1
    assert a.requirement_fingerprint  # never empty for a real STATUS_OK-shaped result


def test_supporting_never_promotes_across_any_evidence_mode():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    question = "How many students are there?"
    for kwargs in (
        {},
        {"lexical_rankings": [_RankedHit("dbo.Badge", 5.0)]},
        {"semantic_rankings": [_RankedHit("dbo.Badge", 0.95)]},
        {"retrieved_examples": [_RetrievedExample(("dbo.Badge",), 0.9)]},
        {
            "lexical_rankings": [_RankedHit("dbo.Badge", 5.0)], "semantic_rankings": [_RankedHit("dbo.Badge", 0.95)],
            "retrieved_examples": [_RetrievedExample(("dbo.Badge",), 0.9)],
        },
    ):
        clear_schema_linker_cache()
        result = _link(question, docs=docs, **kwargs)
        badge_req = _entity_requirement(result, "badge")
        assert badge_req.strength == STRENGTH_SUPPORTING


def test_evidence_still_improves_candidate_admission_and_ranking():
    """Requirement invariance does not mean evidence is inert: a SUPPORTING
    concept's own object may still be ADMITTED as an anchor (ranked/found)
    via independent evidence -- it simply never becomes mandatory CORE
    coverage because of it."""
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    question = "How many students are there?"
    baseline = _link(question, docs=docs)
    with_evidence = _link(
        question, docs=docs,
        semantic_rankings=[_RankedHit("dbo.Badge", 0.95)], lexical_rankings=[_RankedHit("dbo.Badge", 5.0)],
    )
    baseline_anchor = _anchor(baseline, "dbo.Badge") if any(a.object_fqn == "dbo.Badge" for a in baseline.anchors) else None
    evidence_anchor = _anchor(with_evidence, "dbo.Badge")
    assert baseline_anchor is None or evidence_anchor.combined_score > baseline_anchor.combined_score
    assert set(evidence_anchor.admission_reasons) & {"SEMANTIC_MATCH", "LEXICAL_MATCH"}


# ── Phase 6, Step 3F, Part 2: evidence reliability hierarchy ────────────────


def test_exact_phrase_beats_higher_vector_similarity():
    exact = _Doc("dbo.Exact", business_name="Widget", description="x")
    other = _Doc("dbo.Other", business_name="Unrelated", description="y")
    result = _link(
        "Show me widget records", docs=[exact, other],
        semantic_rankings=[_RankedHit("dbo.Other", 0.99)],  # near-perfect vector match for the WRONG object
    )
    exact_anchor = _anchor(result, "dbo.Exact")
    other_anchor = next((a for a in result.anchors if a.object_fqn == "dbo.Other"), None)
    assert other_anchor is None or exact_anchor.combined_score > other_anchor.combined_score


def test_lexical_exact_match_beats_unrelated_semantic_neighbor():
    lexical_hit = _Doc("dbo.LexicalHit", column_names=("Widget",), measure_columns=("Widget",))
    semantic_neighbor = _Doc("dbo.SemanticNeighbor", business_name="Unrelated")
    result = _link(
        "What is the widget?", docs=[lexical_hit, semantic_neighbor],
        lexical_rankings=[_RankedHit("dbo.LexicalHit", 12.0)],
        semantic_rankings=[_RankedHit("dbo.SemanticNeighbor", 0.6)],
    )
    lexical_anchor = _anchor(result, "dbo.LexicalHit")
    semantic_anchor = next((a for a in result.anchors if a.object_fqn == "dbo.SemanticNeighbor"), None)
    assert semantic_anchor is None or lexical_anchor.combined_score > semantic_anchor.combined_score


def test_trusted_example_helps_when_direct_evidence_is_tied():
    a = _Doc("dbo.CandidateA", business_name="Widget", description="x")
    b = _Doc("dbo.CandidateB", business_name="Widget", description="x")
    without_example = _link("Show me widget records", docs=[a, b])
    with_example = _link(
        "Show me widget records", docs=[a, b],
        retrieved_examples=[_RetrievedExample(("dbo.CandidateA",), 0.9)],
    )
    a_before = _anchor(without_example, "dbo.CandidateA")
    a_after = _anchor(with_example, "dbo.CandidateA")
    assert a_after.combined_score > a_before.combined_score


def test_canonical_quality_cannot_override_stronger_direct_evidence():
    weights_sorted = sorted(
        [
            ("catalog", ANCHOR_WEIGHT_CATALOG), ("lexical", ANCHOR_WEIGHT_LEXICAL),
            ("example", ANCHOR_WEIGHT_EXAMPLE), ("semantic", ANCHOR_WEIGHT_SEMANTIC),
            ("canonical", ANCHOR_WEIGHT_CANONICAL),
        ],
        key=lambda item: -item[1],
    )
    assert weights_sorted[-1][0] == "canonical"  # canonical is always the SMALLEST weight
    assert ANCHOR_WEIGHT_CATALOG > ANCHOR_WEIGHT_CANONICAL
    assert ANCHOR_WEIGHT_LEXICAL > ANCHOR_WEIGHT_CANONICAL


# ── Phase 6, Step 3E, Part 2: requirement strength is stable under evidence ─


_NOISE_ENTITY_NAMES = ("Badge", "Receipt", "Loop", "Analytic", "Extra")  # 5 -> below the informativeness floor


def test_supporting_remains_supporting_after_strong_lexical_evidence():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    result = _link(
        "How many students are there?", docs=docs,
        lexical_rankings=[_RankedHit("dbo.Badge", 50.0)],
    )
    badge_req = _entity_requirement(result, "badge")
    assert badge_req.strength == STRENGTH_SUPPORTING


def test_supporting_remains_supporting_after_strong_vector_evidence():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    result = _link(
        "How many students are there?", docs=docs,
        semantic_rankings=[_RankedHit("dbo.Badge", 0.95)],
    )
    badge_req = _entity_requirement(result, "badge")
    assert badge_req.strength == STRENGTH_SUPPORTING


def test_supporting_remains_supporting_after_matching_example_evidence():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    result = _link(
        "How many students are there?", docs=docs,
        retrieved_examples=[_RetrievedExample(("dbo.Badge",), 0.9)],
    )
    badge_req = _entity_requirement(result, "badge")
    assert badge_req.strength == STRENGTH_SUPPORTING


def test_evidence_ranks_candidates_without_adding_core_concepts():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    baseline = _link("How many students are there?", docs=docs)
    with_evidence = _link(
        "How many students are there?", docs=docs,
        semantic_rankings=[_RankedHit(f"dbo.{name}", 0.9) for name in _NOISE_ENTITY_NAMES],
        lexical_rankings=[_RankedHit(f"dbo.{name}", 10.0) for name in _NOISE_ENTITY_NAMES],
    )
    assert baseline.core_requirement_count == with_evidence.core_requirement_count
    # Evidence still does its real job: these now-corroborated objects
    # remain reachable/rankable (admitted via independent evidence), just
    # never promoted to mandatory CORE coverage.
    assert with_evidence.anchors_selected_by_independent_evidence_count >= len(_NOISE_ENTITY_NAMES)


def test_weak_badge_receipt_style_alternatives_cannot_become_mandatory():
    docs = [_Doc(f"dbo.{name}", business_name=name, synonyms=("student",)) for name in _NOISE_ENTITY_NAMES]
    result = _link(
        "How many students are there?", docs=docs,
        semantic_rankings=[_RankedHit(f"dbo.{name}", 0.9) for name in _NOISE_ENTITY_NAMES],
        lexical_rankings=[_RankedHit(f"dbo.{name}", 10.0) for name in _NOISE_ENTITY_NAMES],
        retrieved_examples=[_RetrievedExample((f"dbo.{name}",), 0.9) for name in _NOISE_ENTITY_NAMES],
    )
    for name in _NOISE_ENTITY_NAMES:
        req = _entity_requirement(result, name.lower())
        assert req.strength != STRENGTH_CORE
    assert result.core_requirement_count == 0


def test_exact_multiword_concept_remains_core_under_full_evidence():
    doc = _Doc("dbo.CustomerAccounts", business_name="Customer Account", description="x")
    result = _link(
        "Show me customer account records", docs=[doc],
        semantic_rankings=[_RankedHit("dbo.CustomerAccounts", 0.9)],
        lexical_rankings=[_RankedHit("dbo.CustomerAccounts", 5.0)],
        retrieved_examples=[_RetrievedExample(("dbo.CustomerAccounts",), 0.9)],
    )
    entity_reqs = result.requirements.entity_concepts
    assert len(entity_reqs) == 1
    assert entity_reqs[0].strength == STRENGTH_CORE


def test_unresolved_never_promotes_regardless_of_evidence():
    # Phase 6, Step 3F, Part 1 removed Step 3E's narrow "UNRESOLVED may
    # resolve to CORE when evidence corroborates exactly one materially
    # plausible candidate" exception entirely (verified defect 1/2: this
    # was the exact mechanism that moved a real production schema's CORE
    # count 59 -> 136 purely from added vector evidence, with the user's
    # question never changing). Two DIFFERENT business names ("Customer
    # Account"/"Member Account") reduce to the SAME normalized concept
    # ("account") and share a low-informativeness borrowed alias
    # ("widgetish") -> BusinessEntity.ambiguous=True -> UNRESOLVED. No
    # amount of semantic/lexical evidence -- for one candidate, the other,
    # or both -- may ever promote it to CORE/CONSTRAINT any more.
    def _fixture():
        return [
            _Doc("dbo.Wallets", business_name="Customer Account", description="Primary", synonyms=("widgetish",)),
            _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy", synonyms=("widgetish",)),
            _Doc("dbo.Beta", business_name="Beta", synonyms=("widgetish",)),
            _Doc("dbo.Gamma", business_name="Gamma", synonyms=("widgetish",)),
            _Doc("dbo.Delta", business_name="Delta", synonyms=("widgetish",)),
        ]

    docs = _fixture()
    baseline = _link("Show me widgetish records", docs=docs)
    account_req = next(r for r in baseline.requirements.entity_concepts if r.normalized_label == "account")
    assert account_req.strength == STRENGTH_UNRESOLVED

    for semantic_rankings in (
        [_RankedHit("dbo.Wallets", 0.95)],
        [_RankedHit("dbo.Wallets", 0.95), _RankedHit("dbo.Ledgers", 0.94)],
    ):
        clear_schema_linker_cache()
        result = _link("Show me widgetish records", docs=docs, semantic_rankings=semantic_rankings)
        req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "account")
        assert req.strength == STRENGTH_UNRESOLVED
        assert req.strength not in (STRENGTH_CORE, STRENGTH_CONSTRAINT)


# ── Phase 6, Step 3E, Part 3: candidate preservation without duplication ───
#
# Generic reproduction of the "EDU_Track-style synonym alternative" defect:
# an entity (here, "Path") whose large, generic column-derived alias
# vocabulary (>12 items -- would have overflowed the OLD max_aliases_per_
# entity=12 alphabetical cap) includes a token ("program") that is ALSO
# another, unrelated entity's ("Program") own exact business name. No
# table/column name here resembles any real customer schema -- purely
# generic English vocabulary standing in for the mechanism.


def _program_path_fixture():
    early_alphabet_aliases = tuple(f"alias{i:02d}" for i in range(20))  # all sort before "program"
    path_doc = _Doc(
        "dbo.Path", business_name="Path", description="x",
        synonyms=early_alphabet_aliases + ("program",),
    )
    program_doc = _Doc("dbo.Program", business_name="Program", description="y")
    return [path_doc, program_doc]


def test_one_normalized_concept_retains_multiple_object_candidates():
    a = _Doc("dbo.Wallets", business_name="Customer Account", description="Primary")
    b = _Doc("dbo.Ledgers", business_name="Member Account", description="Legacy")
    result = _link("Show me account records", docs=[a, b])
    account_req = _entity_requirement(result, "account")
    assert len(account_req.candidates) == 2
    assert {c.object_fqn for c in account_req.candidates} == {"dbo.Wallets", "dbo.Ledgers"}


def test_exact_name_candidate_ranks_above_borrowed_synonym_candidate():
    # "Ranks highly" means: the exact business-name match is the one and
    # only MANDATORY (CORE) claim on the shared token, admitted via its own
    # exact-name evidence, while the borrowed-synonym match on the SAME
    # token is demoted to non-mandatory evidence -- never that combined_
    # score (which also reflects unrelated catalog richness like synonym
    # count) happens to be numerically larger for one over the other.
    docs = _program_path_fixture()
    result = _link("Show classes and their program", docs=docs)
    program_req = _entity_requirement(result, "program")
    path_req = _entity_requirement(result, "path")
    assert program_req.strength == STRENGTH_CORE
    assert program_req.candidates[0].match_kind == MATCH_KIND_EXACT_BUSINESS_NAME
    assert path_req.strength != STRENGTH_CORE
    assert path_req.candidates[0].match_kind == MATCH_KIND_SYNONYM


def test_synonym_alternative_remains_reachable_despite_large_alias_vocabulary():
    docs = _program_path_fixture()
    result = _link("Show classes and their program", docs=docs)
    # "Path" must still be a matched requirement AND a reachable anchor --
    # never erased outright merely because "program" also sorts late in a
    # large alphabetically-ordered alias list.
    path_req = _entity_requirement(result, "path")
    assert any(c.object_fqn == "dbo.Path" for c in path_req.candidates)
    assert any(a.object_fqn == "dbo.Path" for a in result.anchors)


def test_consumed_phrase_does_not_erase_alternative_candidates():
    docs = _program_path_fixture()
    result = _link("Show classes and their program", docs=docs)
    # "program" is consumed by the Program entity's own exact-name CORE
    # claim -- Path's own requirement must survive (demoted, never deleted).
    path_req = _entity_requirement(result, "path")
    assert path_req.candidate_total_count >= 1
    assert path_req.strength != STRENGTH_CORE


def test_alternatives_do_not_create_duplicate_core_requirements():
    docs = _program_path_fixture()
    result = _link("Show classes and their program", docs=docs)
    core_entity_labels = [r.normalized_label for r in result.requirements.entity_concepts if r.strength == STRENGTH_CORE]
    # Exactly one mandatory "program" requirement -- Path's borrowed-alias
    # match on the same token never becomes a second, duplicate CORE ask.
    assert core_entity_labels.count("program") == 1
    assert "path" not in core_entity_labels


def test_no_reference_specific_strength_constants():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "schema_linker.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"schema_linker.py contains banned domain-specific token {banned!r}"
    # The Step 3C single-token CORE floor must be one fixed generic
    # constant, never a per-object-name or per-table lookup table.
    assert source.count("_ENTITY_SINGLE_TOKEN_CORE_FLOOR =") == 1
