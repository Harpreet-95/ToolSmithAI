"""
Focused tests — Phase 7, Step 6C2: the generic canonical-entity-inclusion
prompt rule added to core/sqlservice/fast_path_generation_service.py's own
system prompt (Part 1 of Step 6C2's brief).

Honesty note on what these tests actually prove: whether GPT-6 Sol itself
FOLLOWS a prompt instruction cannot be verified offline without a real
provider call. What these tests DO verify, precisely:
  1. the new rule's text is present in the rendered prompt, uses only
     generic language (no the reference database/domain-specific terms), and the prompt
     still renders correctly (no template/formatting regression);
  2. the CANDIDATE_CONTEXT rendering actually surfaces the evidence the
     rule refers to (a CANONICAL_ENTITY_SOURCE business-knowledge mapping)
     for a generic customer/order schema -- i.e. the input side of the
     rule is genuinely available to the model, not merely described;
  3. every downstream deterministic stage (relationship completion, exact
     context, validation) still correctly ACCEPTS a rule-compliant
     response and correctly REJECTS a rule-violating one (no confirmed
     relationship path) -- these are the SAME unchanged mechanisms Step
     6C already reuses, re-exercised here against the rule's own named
     examples (customer/order/region) rather than the generic widget/order
     fixture the rest of the Step 6C suite uses.

Uses the same SQLite fixture plumbing as tests/test_sqlservice_ai_first_
fast_path_service.py. No real OpenAI call, no real SQL Server.
"""

import dataclasses
import hashlib
from types import MappingProxyType

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
import core.sqlservice.sql_execution as sql_execution_module
import core.sqlservice.fast_path_context as fast_path_context_module
import data.business_knowledge_mapping_store as business_knowledge_mapping_store_module
import data.business_knowledge_package_service as business_knowledge_package_service_module
import data.db as data_db_module
from core.sqlservice.ai_first_fast_path_service import answer_question_ai_first_fast_path
from core.sqlservice.ai_first_result import (
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_OK,
    STATUS_SELECTION_FAILED,
    STATUS_VALIDATION_FAILED,
)
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.business_knowledge_evidence import (
    BusinessKnowledgeMappingEvidence,
    clear_business_knowledge_evidence_cache,
)
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.fast_path_generation_providers import (
    FastPathGenerationProvider,
    ProviderFastPathGenerationResponse,
)
from core.sqlservice.fast_path_generation_service import build_fast_path_prompts
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.sql_example_vector_index import clear_sql_example_vector_index_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    DIMS,
    MODEL,
    PROVIDER,
    SOURCE_ID,
    USER_ID,
    FakeAnswerGenerationProvider,
    FakeConnection,
    FakeCursor,
    FakeEmbeddingProvider,
    _connection_factory_for,
    _fingerprint,
    _insert_snapshot,
    _insert_source,
    _make_connection,
    _wire_execution_lookups,
    template_db_path,  # noqa: F401
)
from tests.test_sqlservice_ai_first_fast_path_service import FakeFastPathProvider


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(business_knowledge_package_service_module, "get_connection", lambda: _make_connection(path))
    # data/business_knowledge_mapping_store.py does `from data.db import
    # get_connection` (a name-bound import) rather than `import data.db as
    # data_db_module`, so patching data_db_module.get_connection above does
    # NOT reach it -- must be patched on its own module object too, exactly
    # like business_knowledge_package_service_module just above.
    monkeypatch.setattr(business_knowledge_mapping_store_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    # business_knowledge_evidence's own cache is process-level, keyed by
    # (source_id, schema_fingerprint) -- both SHARED across test files in
    # this same pytest session (sqlservice_fixtures.py's own
    # SOURCE_ID=1/_fingerprint() constants) -- so it must be cleared here
    # too, exactly like development validation material (not included here)
    # final.py already does, or a business-knowledge mapping injected by an
    # EARLIER test in the same session can silently leak into this one.
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    clear_business_knowledge_evidence_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_lexical_index_cache()
    yield
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    clear_business_knowledge_evidence_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_lexical_index_cache()


@pytest.fixture(autouse=True)
def _stub_sql_execution_sqlite_lookups(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: None)
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: None)
    yield


# ── Generic customer/order/region fixture (Step 6C2's own named examples,
#    never any database-specific name) ──────────────────────────────────────


def _customer_doc(source_id=SOURCE_ID):
    text = "OBJECT: dbo.customers\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  CustomerID INTEGER [PK]\n  CustomerName VARCHAR(100)"
    return RetrievalDocument(
        source_id=source_id, object_fqn="dbo.customers", object_type="TABLE", schema_name="dbo",
        object_name="customers", retrieval_tier=1, document_text=text,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        metadata=MappingProxyType({
            "primary_key_columns": ("CustomerID",), "column_names": ("CustomerID", "CustomerName"),
            # core.sqlservice.relationship_graph (used by complete_relationships)
            # reads these flat keys, NOT confirmed_relationship_edges below --
            # both are supplied so every reused stage (relationship graph AND
            # schema_frontier's JOIN_KEYS rendering) sees the same real edge.
            "confirmed_relationship_sources": ("dbo.orders.CustomerID",),
            "confirmed_relationship_edges": (
                {"direction": "inbound", "column": "CustomerID", "other": "dbo.orders.CustomerID", "kind": "PHYSICAL_FK"},
            ),
        }),
    )


def _orders_doc(source_id=SOURCE_ID, *, extra_columns=()):
    columns = ("OrderID", "CustomerID", *extra_columns)
    text = "OBJECT: dbo.orders\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + "\n  ".join(columns)
    return RetrievalDocument(
        source_id=source_id, object_fqn="dbo.orders", object_type="TABLE", schema_name="dbo",
        object_name="orders", retrieval_tier=1, document_text=text,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        metadata=MappingProxyType({
            "primary_key_columns": ("OrderID",), "column_names": columns,
            "confirmed_relationship_targets": ("dbo.customers.CustomerID",),
            "confirmed_relationship_edges": (
                {"direction": "outbound", "column": "CustomerID", "other": "dbo.customers.CustomerID", "kind": "PHYSICAL_FK"},
            ),
        }),
    )


def _customers_view_doc(source_id=SOURCE_ID):
    """An authoritative-equivalent VIEW standing in for dbo.customers --
    same confirmed relationship to dbo.orders, no relationship to the real
    dbo.customers table (they are never selected together in these
    tests)."""
    text = "OBJECT: dbo.customers_view\nTYPE: VIEW\nTIER: 1\nCOLUMNS:\n  CustomerID INTEGER [PK]\n  CustomerName VARCHAR(100)"
    return RetrievalDocument(
        source_id=source_id, object_fqn="dbo.customers_view", object_type="VIEW", schema_name="dbo",
        object_name="customers_view", retrieval_tier=1, document_text=text,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        metadata=MappingProxyType({
            "primary_key_columns": ("CustomerID",), "column_names": ("CustomerID", "CustomerName"),
            "confirmed_relationship_sources": ("dbo.orders.CustomerID",),
            "confirmed_relationship_edges": (
                {"direction": "inbound", "column": "CustomerID", "other": "dbo.orders.CustomerID", "kind": "PHYSICAL_FK"},
            ),
        }),
    )


def _unrelated_doc(fqn="dbo.regions_lookup", source_id=SOURCE_ID):
    """No confirmed relationship to anything else in this fixture."""
    text = f"OBJECT: {fqn}\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  RegionID INTEGER [PK]\n  RegionName VARCHAR(50)"
    schema_name, object_name = fqn.split(".", 1)
    return RetrievalDocument(
        source_id=source_id, object_fqn=fqn, object_type="TABLE", schema_name=schema_name,
        object_name=object_name, retrieval_tier=1, document_text=text,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        metadata=MappingProxyType({
            "primary_key_columns": ("RegionID",), "column_names": ("RegionID", "RegionName"),
        }),
    )


def _prepare_customer_order_source(db_path, docs, *, source_id=SOURCE_ID, user_id=USER_ID):
    _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id=source_id, source_id=source_id)
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=source_id, snapshot_version=1,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    build = result.build
    vectors_by_hash = {doc.document_hash: [1.0, 0.0, 0.0, 0.0] for doc in docs}
    embed_result = insert_embeddings(build.id, source_id, user_id, PROVIDER, MODEL, DIMS, vectors_by_hash)
    assert embed_result.status == EMBEDDING_STATUS_OK
    return build


def _inject_business_knowledge(monkeypatch, *, object_fqn, mapping_role, confidence=1.0):
    """Same monkeypatch-the-frontier-output pattern already established in
    tests/test_sqlservice_ai_first_fast_path_service.py's own business-
    knowledge test -- avoids standing up the real business-knowledge
    package tables just to inject one mapping for a unit test."""
    original = fast_path_context_module.build_schema_frontier

    def _patched(*args, **kwargs):
        result = original(*args, **kwargs)
        new_candidates = []
        for c in result.candidates:
            if c.object_fqn == object_fqn:
                c = dataclasses.replace(c, business_knowledge_mappings=(
                    BusinessKnowledgeMappingEvidence(
                        concept_id="CUSTOMER", concept_type="ENTITY", mapping_role=mapping_role,
                        mapping_status="VERIFIED", confidence=confidence, concept_origin="AUTHORITATIVE", evidence=(),
                    ),
                ))
            new_candidates.append(c)
        return dataclasses.replace(result, candidates=tuple(new_candidates))

    monkeypatch.setattr(fast_path_context_module, "build_schema_frontier", _patched)


def _inject_complete_view_profile(monkeypatch, *, object_fqn, schema_fingerprint, suitable_for_join=True):
    """Step 6E3B2, Part 1 -- generated SQL may reference a VIEW only when
    its persisted profile is COMPLETE (build/schema-fingerprint matched,
    secret-policy current). This test exercises a VIEW through the real
    validate_sql_candidate path, so it must supply one -- mirrors the
    monkeypatch-the-service-call pattern _inject_authority/_inject_
    business_knowledge already use elsewhere in this file, rather than
    standing up real sql_view_profiles rows."""
    import data.view_profile_service as view_profile_service_module
    from core.sqlservice.view_profiler import ViewProfile, ViewSuitability, VIEW_PROFILE_CONTRACT_VERSION
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION

    profile = ViewProfile(
        object_fqn=object_fqn, source_id=SOURCE_ID, build_id=1, schema_fingerprint=schema_fingerprint,
        definition_hash="hash", profiling_version=VIEW_PROFILE_CONTRACT_VERSION,
        completion_status="COMPLETE", failure_reason=None,
        duplication_risk="NONE", row_preservation_risk="PRESERVED",
        suitability=ViewSuitability(
            suitable_for_detail=True, suitable_for_count=True, suitable_for_aggregation=True,
            suitable_for_join=suitable_for_join, unsuitable_reasons=(), required_distinct_key=None,
            population_restriction=None, confidence="HIGH",
        ),
        secret_policy_version=SECRET_POLICY_VERSION,
        created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:00Z",
    )
    monkeypatch.setattr(
        view_profile_service_module, "get_view_profiles_by_fqns",
        lambda source_id, fqns: {object_fqn.lower(): profile} if object_fqn.lower() in [f.lower() for f in fqns] else {},
    )
    monkeypatch.setattr(view_profile_service_module, "get_current_definition_hashes", lambda source_id, fqns: {})


def _inject_authority(monkeypatch, *, object_fqn, authority_class, canonical_table_fqns=(), preferred_view_for_entities=()):
    original = fast_path_context_module.build_schema_frontier

    def _patched(*args, **kwargs):
        result = original(*args, **kwargs)
        new_candidates = []
        for c in result.candidates:
            if c.object_fqn == object_fqn:
                c = dataclasses.replace(
                    c, authority_class=authority_class, authority_canonical_table_fqns=canonical_table_fqns,
                    authority_preferred_view_for_entities=preferred_view_for_entities,
                    authority_is_declared_preferred_view=bool(preferred_view_for_entities),
                )
            new_candidates.append(c)
        return dataclasses.replace(result, candidates=tuple(new_candidates))

    monkeypatch.setattr(fast_path_context_module, "build_schema_frontier", _patched)


def _intent(**overrides):
    value = {
        "operation": "LIST", "entities": ["customer"], "measures": [], "filters": [],
        "date_constraints": [], "groupings": [], "requested_attributes": [],
    }
    value.update(overrides)
    return value


def _data_question_raw(selected, columns_by_object, sql, relationships_used=()):
    return {
        "request_kind": "DATA_QUESTION", "intent": _intent(),
        "selected_object_fqns": list(selected),
        "object_reasons": [{"object_fqn": fqn, "supporting_columns": cols} for fqn, cols in columns_by_object.items()],
        "confirmed_relationships_used": list(relationships_used), "sql": sql,
        "clarification_question": None, "response_text": None,
        "decision_evidence": ["direct match"], "confidence": 0.9,
        "follow_up_resolution": {"resolution_type": "none", "resolved_standalone_question": "placeholder"},
    }


def _run(db_path, monkeypatch, build, fast_path_raw, *, cursor_columns=("CustomerID",), cursor_rows=((1,),)):
    _wire_execution_lookups(monkeypatch, build)
    embedding_provider = FakeEmbeddingProvider()
    fast_path_provider = FakeFastPathProvider(fixed_raw=fast_path_raw)
    cursor = FakeCursor(cursor_columns, list(cursor_rows))
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)
    result = answer_question_ai_first_fast_path(
        "How many customers are there?", build.source_id, USER_ID, embedding_provider, fast_path_provider, None,
        connection_factory=connection_factory,
    )
    return result, embedding_provider, fast_path_provider, connection_factory


# ── 0. The rule itself: rendered, generic, no leaked domain terms ──────


def test_canonical_entity_rule_text_present_and_generic():
    from core.sqlservice.fast_path_context import FastPathContext
    from core.sqlservice.schema_frontier import SchemaFrontierResult

    frontier = SchemaFrontierResult(
        status="OK", build_id=1, schema_fingerprint="fp", embedding_provider="p", embedding_model="m",
        embedding_dimensions=4, candidates=(), total_characters=0, truncated=False, warnings=(), error=None,
    )
    context = FastPathContext(
        status="OK", build_id=1, schema_fingerprint="fp", frontier=frontier, context_text="OBJECT: dbo.x",
        object_count=1, total_characters=10, estimated_tokens=3, warnings=(), error=None,
    )
    system_prompt, _user_prompt = build_fast_path_prompts("test", context, max_objects=8)

    assert "CANONICAL_ENTITY_SOURCE" in system_prompt
    assert "confirmed relationship path" in system_prompt
    # reference/exampleco/edu_student never appear anywhere in this prompt.
    # "iplan"/"sql tuition" already appear elsewhere in this template as of
    # Step 6C (explicitly authorized then, out of scope for Step 6C2's own
    # narrow "add one generic rule" brief to remove) -- not re-asserted
    # against here.
    for forbidden in ("exampleco", "exampleco", "edu_student"):
        assert forbidden not in system_prompt.lower()


# ── 1. "Show customers and their orders" includes the canonical Customer
#    entity, not just Order.CustomerID -- and the evidence the rule needs
#    genuinely reaches the rendered prompt. ─────────────────────────────


def test_customers_and_orders_includes_canonical_customer_entity(db_path, monkeypatch):
    build = _prepare_customer_order_source(db_path, [_customer_doc(), _orders_doc()])
    _inject_business_knowledge(monkeypatch, object_fqn="dbo.customers", mapping_role="CANONICAL_ENTITY_SOURCE")

    raw = _data_question_raw(
        selected=("dbo.customers", "dbo.orders"),
        columns_by_object={"dbo.customers": ["CustomerID", "CustomerName"], "dbo.orders": ["OrderID", "CustomerID"]},
        sql="SELECT c.CustomerID, c.CustomerName, o.OrderID FROM dbo.customers c JOIN dbo.orders o ON c.CustomerID = o.CustomerID",
        relationships_used=("dbo.orders.CustomerID -> dbo.customers.CustomerID",),
    )
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, raw, cursor_columns=("CustomerID", "CustomerName", "OrderID"), cursor_rows=((1, "Acme", 100),),
    )

    assert result.status == STATUS_OK
    assert set(result.canonical_referenced_object_fqns) == {"dbo.customers", "dbo.orders"}
    assert len(embedding_provider.calls) == 1
    assert len(fast_path_provider.calls) == 1
    # The rule's own evidence (a CANONICAL_ENTITY_SOURCE mapping) genuinely
    # reached the rendered prompt -- not merely asserted to exist.
    assert "CUSTOMER:CANONICAL_ENTITY_SOURCE" in fast_path_provider.calls[0]["user_prompt"]


# ── 2. "Count distinct customers by region" may use the relationship
#    table's own customer identifier when no customer-owned attribute is
#    requested. ──────────────────────────────────────────────────────────


def test_count_distinct_customers_by_region_may_use_relationship_table_alone(db_path, monkeypatch):
    build = _prepare_customer_order_source(
        db_path, [_customer_doc(), _orders_doc(extra_columns=("Region",))],
    )
    _inject_business_knowledge(monkeypatch, object_fqn="dbo.customers", mapping_role="CANONICAL_ENTITY_SOURCE")

    raw = _data_question_raw(
        selected=("dbo.orders",),
        columns_by_object={"dbo.orders": ["CustomerID", "Region"]},
        sql="SELECT Region, COUNT(DISTINCT CustomerID) AS DistinctCustomers FROM dbo.orders GROUP BY Region",
    )
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, raw, cursor_columns=("Region", "DistinctCustomers"), cursor_rows=((1, "West", 3), (2, "East", 5)),
    )

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.orders",)
    # Not forced into an unnecessary join -- exactly what the rule's own
    # last sentence requires.


# ── 3. The canonical entity is not added -- and is rejected if claimed --
#    when no confirmed relationship path exists. ────────────────────────


def test_canonical_entity_rejected_without_confirmed_relationship(db_path, monkeypatch):
    build = _prepare_customer_order_source(
        db_path, [_orders_doc(extra_columns=("Region",)), _unrelated_doc()],
    )

    raw = _data_question_raw(
        selected=("dbo.orders", "dbo.regions_lookup"),
        columns_by_object={"dbo.orders": ["CustomerID", "Region"], "dbo.regions_lookup": ["RegionID", "RegionName"]},
        sql="SELECT o.Region, r.RegionName FROM dbo.orders o JOIN dbo.regions_lookup r ON o.Region = r.RegionName",
        relationships_used=("dbo.orders.Region -> dbo.regions_lookup.RegionName",),
    )
    result, embedding_provider, fast_path_provider, connection_factory = _run(db_path, monkeypatch, build, raw)

    # No real confirmed relationship connects these two objects -- rejected
    # by one of the reused, unchanged deterministic stages: the structural
    # response validator, complete_relationships() (DISCONNECTED -> a safe
    # clarification, never a guessed join), or sql_validation's own join
    # check. STATUS_OK must never occur, and nothing is ever executed.
    assert result.status in (STATUS_SELECTION_FAILED, STATUS_VALIDATION_FAILED, STATUS_CLARIFICATION_REQUIRED)
    assert len(connection_factory.calls) == 0


# ── 4. A proven authoritative equivalent may replace the canonical
#    entity. ──────────────────────────────────────────────────────────────


def test_authoritative_equivalent_view_may_replace_canonical_entity(db_path, monkeypatch):
    build = _prepare_customer_order_source(db_path, [_customers_view_doc(), _orders_doc()])
    _inject_authority(
        monkeypatch, object_fqn="dbo.customers_view", authority_class="DECLARED_PREFERRED_VIEW",
        canonical_table_fqns=("Customer",), preferred_view_for_entities=("Customer",),
    )
    _inject_complete_view_profile(
        monkeypatch, object_fqn="dbo.customers_view", schema_fingerprint=build.schema_fingerprint,
    )

    raw = _data_question_raw(
        selected=("dbo.customers_view", "dbo.orders"),
        columns_by_object={"dbo.customers_view": ["CustomerID", "CustomerName"], "dbo.orders": ["OrderID", "CustomerID"]},
        sql="SELECT c.CustomerID, c.CustomerName, o.OrderID FROM dbo.customers_view c JOIN dbo.orders o ON c.CustomerID = o.CustomerID",
        relationships_used=("dbo.orders.CustomerID -> dbo.customers_view.CustomerID",),
    )
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, raw, cursor_columns=("CustomerID", "CustomerName", "OrderID"), cursor_rows=((1, "Acme", 100),),
    )

    assert result.status == STATUS_OK
    assert set(result.canonical_referenced_object_fqns) == {"dbo.customers_view", "dbo.orders"}
