"""
Focused tests — Phase 7, Step 3C, Part 2: the broad compact candidate
frontier (core/sqlservice/schema_frontier.py).

Reuses the SQLite fixture plumbing and fakes already established in
tests/sqlservice_fixtures.py rather than duplicating them.
No real OpenAI call anywhere in this file — the embedding provider is a
fake with a caller-controlled fixed vector per call.
"""

import hashlib
import json
import sqlite3
from types import MappingProxyType

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import data.db as data_db_module
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_frontier import (
    LANE_LEXICAL,
    LANE_SEMANTIC,
    STATUS_OK,
    build_schema_frontier,
    render_frontier_candidate,
)
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.lexical_index import clear_lexical_index_cache, tokenize
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    DIMS,
    MODEL,
    PROVIDER,
    SOURCE_ID,
    USER_ID,
    FakeEmbeddingProvider,
    _insert_snapshot,
    _insert_source,
    _make_connection,
    template_db_path,  # noqa: F401 -- re-exported fixture, used by db_path below
)


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()


def _fingerprint():
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(b"fingerprint-a").hexdigest(),
        "table_set_hash": hashlib.sha256(b"table-set-a").hexdigest(),
    }


def _doc(object_fqn, *, tier=1, text_extra="", column_names=("id", "name"), metadata_extra=None):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\n{text_extra}"
    doc_hash = hashlib.sha256((text + object_fqn).encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    metadata = {"column_count": len(column_names), "column_names": tuple(column_names)}
    if metadata_extra:
        metadata.update(metadata_extra)
    return RetrievalDocument(
        source_id=SOURCE_ID, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _build_with_docs(db_path, docs, vectors_by_hash, *, source_id=SOURCE_ID, user_id=USER_ID):
    _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id=source_id, source_id=source_id)
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=source_id, snapshot_version=1,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    build = result.build
    embed_result = insert_embeddings(build.id, source_id, user_id, PROVIDER, MODEL, DIMS, vectors_by_hash)
    assert embed_result.status == EMBEDDING_STATUS_OK
    return build


def _unit(*components):
    import math
    norm = math.sqrt(sum(c * c for c in components))
    return [c / norm for c in components]


# ── 1. One embedding call, deterministic basic build ────────────────────


def test_frontier_makes_one_embedding_call_and_returns_candidates(db_path):
    docs = [_doc("dbo.widgets"), _doc("dbo.orders")]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.0, 1.0, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("How many widgets?", build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_OK
    assert len(provider.calls) == 1
    fqns = {c.object_fqn for c in result.candidates}
    assert fqns == {"dbo.widgets", "dbo.orders"}
    assert result.candidates[0].object_fqn == "dbo.widgets"  # closer cosine match ranks first


# ── 2. No evidence -> no admission ───────────────────────────────────────


def test_object_with_no_semantic_or_lexical_evidence_still_admitted_via_semantic_lane(db_path):
    """Every tier-allowed object always receives SOME cosine rank (the
    semantic lane covers the whole build) -- so with a small build, both
    objects are admitted regardless of lexical overlap; the "no evidence,
    no admission" guarantee is instead proven by the tier-exclusion test
    below (a Tier-3 object is excluded even though it would otherwise have
    a semantic rank)."""
    docs = [_doc("dbo.widgets"), _doc("dbo.orders")]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.0, 1.0, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)
    assert {c.object_fqn for c in result.candidates} == {"dbo.widgets", "dbo.orders"}


def test_tier3_object_excluded_even_with_strong_semantic_similarity(db_path):
    docs = [
        _doc("dbo.widgets", tier=1),
        _doc("dbo.widgets_archive_bak", tier=3),
    ]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(1.0, 0.0, 0.0, 0.0),  # identical vector -- would rank #1 if not tier-excluded
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)
    fqns = {c.object_fqn for c in result.candidates}
    assert "dbo.widgets_archive_bak" not in fqns
    assert fqns == {"dbo.widgets"}


# ── 3. Lane diversity: a lexical-only match survives many semantic decoys ─


def test_lane_diversity_admits_lexical_match_despite_many_semantic_decoys(db_path):
    """The real answer (dbo.EDU_Track-style object) has a real lexical/
    synonym match for the question's own distinctive term but a WEAK
    semantic (cosine) score. Many semantically-similar decoys rank ahead
    of it on the semantic lane alone. A small frontier_size must still
    admit the real answer via the lexical lane's own round-robin slot --
    this is the scaled-down analog of "EDU_Track ranked ~82nd overall but
    must remain in a 100-object frontier"."""
    decoys = [
        _doc(f"dbo.decoy_{i}", column_names=("id", f"col_{i}")) for i in range(12)
    ]
    real_answer = _doc(
        "dbo.program_path_ledger", column_names=("id", "program_code"),
        metadata_extra={"synonyms": ("distinctiveterm", "program path")},
    )
    docs = decoys + [real_answer]
    vectors = {d.document_hash: _unit(1.0, 0.0, 0.0, 0.0) for d in decoys}
    # The real answer scores WORSE semantically than every decoy.
    vectors[real_answer.document_hash] = _unit(0.0, 0.0, 0.0, 1.0)
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "Show the distinctiveterm records", build.id, SOURCE_ID, USER_ID, provider, frontier_size=6,
    )

    fqns = {c.object_fqn for c in result.candidates}
    assert "dbo.program_path_ledger" in fqns
    real_candidate = next(c for c in result.candidates if c.object_fqn == "dbo.program_path_ledger")
    assert LANE_LEXICAL in real_candidate.admitted_via_lanes


# ── 4. Determinism ────────────────────────────────────────────────────────


def test_deterministic_under_repeated_calls(db_path):
    docs = [_doc(f"dbo.table_{i}") for i in range(8)]
    vectors = {d.document_hash: _unit(1.0, float(i) * 0.01, 0.0, 0.0) for i, d in enumerate(docs)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result_a = build_schema_frontier("tables", build.id, SOURCE_ID, USER_ID, provider, frontier_size=5)
    result_b = build_schema_frontier("tables", build.id, SOURCE_ID, USER_ID, provider, frontier_size=5)

    fqns_a = tuple(c.object_fqn for c in result_a.candidates)
    fqns_b = tuple(c.object_fqn for c in result_b.candidates)
    assert fqns_a == fqns_b


# ── 5. Bounded characters ────────────────────────────────────────────────


def test_max_characters_per_object_is_enforced(db_path):
    docs = [_doc(
        "dbo.widgets",
        metadata_extra={
            "business_name": "W" * 2000,
            "synonyms": tuple(f"syn{i}" for i in range(50)),
        },
    )]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "widgets", build.id, SOURCE_ID, USER_ID, provider, max_characters_per_object=200,
    )
    assert result.status == STATUS_OK
    assert len(result.candidates) == 1
    assert len(render_frontier_candidate(result.candidates[0])) <= 200


def test_max_frontier_characters_truncates_tail(db_path):
    docs = [_doc(
        f"dbo.table_{i}", metadata_extra={"business_name": "X" * 300},
    ) for i in range(10)]
    vectors = {d.document_hash: _unit(1.0, float(i) * 0.001, 0.0, 0.0) for i, d in enumerate(docs)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "tables", build.id, SOURCE_ID, USER_ID, provider, frontier_size=10, max_frontier_characters=500,
    )
    assert result.truncated is True
    assert len(result.candidates) < 10
    assert result.total_characters <= 500


# ── 6. No vector/prompt leakage on the public result/candidate shape ──────


def test_candidate_carries_no_query_vector_or_prompt():
    from core.sqlservice.schema_frontier import FrontierCandidate
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(FrontierCandidate)}
    assert "vector" not in field_names
    assert "query_vector" not in field_names
    assert "prompt" not in field_names
    assert "embedding" not in field_names


# ── 7. Phase 7, Step 3E, Part 8 — authority facts in frontier rendering ────


def _insert_contract(db_path, entity_name, *, source_id=SOURCE_ID, canonical_table_fqn=None, preferred_view_fqn=None):
    conn = sqlite3.connect(db_path)
    now = "2026-09-24T00:00:00Z"
    conn.execute(
        "INSERT INTO semantic_entity_contracts "
        "(source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, "
        " resolution_status, confidence, contract_json, metadata_revision, generation_method, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, 'RESOLVED', 1.0, '{}', 'rev1', 'auto_grounding', ?, ?)",
        (source_id, entity_name, canonical_table_fqn, preferred_view_fqn, now, now),
    )
    conn.commit()
    conn.close()


def _insert_view_evidence(db_path, table_fqn, definition_sql, referenced_tables, *, source_id=SOURCE_ID):
    conn = sqlite3.connect(db_path)
    now = "2026-09-24T00:00:00Z"
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, referenced_tables_json, "
        " restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, ?, ?, 'x', ?, NULL, 0.0, 1, ?)",
        (source_id, table_fqn, definition_sql, json.dumps(referenced_tables), now),
    )
    conn.commit()
    conn.close()


def _view_doc(object_fqn, *, tier=1, column_names=("id", "name")):
    text = f"OBJECT: {object_fqn}\nTYPE: VIEW\nTIER: {tier}"
    doc_hash = hashlib.sha256((text + object_fqn).encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    metadata = {"column_names": tuple(column_names)}
    return RetrievalDocument(
        source_id=SOURCE_ID, object_fqn=object_fqn, object_type="VIEW",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def test_authority_facts_included_and_bounded_never_full_view_sql(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.widgets")
    definition_sql = "CREATE VIEW v AS SELECT a.id FROM dbo.widgets a WHERE a.RegionID = 999  -- " + ("x" * 2000)
    _insert_view_evidence(db_path, "dbo.vw_widgets", definition_sql, ["dbo.widgets"])

    docs = [_doc("dbo.widgets", metadata_extra={"primary_key_columns": ("id",)}), _view_doc("dbo.vw_widgets")]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0), docs[1].document_hash: _unit(0.9, 0.1, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)

    assert len(provider.calls) == 1  # exactly one embedding call, unchanged
    by_fqn = {c.object_fqn: c for c in result.candidates}
    view_candidate = by_fqn["dbo.vw_widgets"]
    assert view_candidate.authority_class == "RESTRICTED_VIEW"
    rendered = render_frontier_candidate(view_candidate)
    assert "AUTHORITY: RESTRICTED_VIEW" in rendered
    assert "AUTHORITY_DETAIL:" in rendered
    # Full view SQL (the definition text, or its distinctive padding) must
    # never reach the rendered candidate.
    assert "CREATE VIEW" not in rendered
    assert "x" * 100 not in rendered
    assert len(rendered) < len(definition_sql)


def test_canonical_preferred_equivalent_restricted_distinctions_visible(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.widgets", preferred_view_fqn="dbo.vw_preferred")
    _insert_view_evidence(db_path, "dbo.vw_preferred", "CREATE VIEW v AS SELECT * FROM dbo.widgets WHERE 1=1", ["dbo.widgets"])
    _insert_view_evidence(
        db_path, "dbo.vw_equivalent",
        "CREATE VIEW v AS SELECT a.id, a.name FROM dbo.widgets a LEFT JOIN dbo.vendors b ON a.vendor_id = b.id",
        ["dbo.widgets", "dbo.vendors"],
    )
    _insert_view_evidence(db_path, "dbo.vw_restricted", "CREATE VIEW v AS SELECT a.id FROM dbo.widgets a WHERE a.region = 1", ["dbo.widgets"])

    docs = [
        _doc("dbo.widgets", metadata_extra={"primary_key_columns": ("id",)}),
        _doc("dbo.vendors"),
        _view_doc("dbo.vw_preferred"),
        _view_doc("dbo.vw_equivalent"),
        _view_doc("dbo.vw_restricted"),
    ]
    vectors = {d.document_hash: _unit(1.0, i * 0.01, 0.0, 0.0) for i, d in enumerate(docs)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)
    by_fqn = {c.object_fqn: c for c in result.candidates}

    # Phase 7, Step 3F, Part 2: dbo.widgets (the canonical table) and
    # dbo.vw_equivalent (a merely proven-equivalent view for the SAME
    # entity) are now correctly COLLAPSED into dbo.vw_preferred, the
    # declared preferred view -- the selector must see one representative,
    # not three provably-interchangeable choices for "Widget".
    assert "dbo.widgets" not in by_fqn
    assert "dbo.vw_equivalent" not in by_fqn
    assert by_fqn["dbo.vw_preferred"].authority_class == "DECLARED_PREFERRED_VIEW"
    assert set(by_fqn["dbo.vw_preferred"].authority_collapsed_member_fqns) == {"dbo.widgets", "dbo.vw_equivalent"}
    assert by_fqn["dbo.vw_restricted"].authority_class == "RESTRICTED_VIEW"
    # RESTRICTED_VIEW is never a collapse candidate -- distinct classes
    # still render visibly and distinctly for the two survivors.
    of_interest = ["dbo.vw_preferred", "dbo.vw_restricted"]
    rendered = {fqn: render_frontier_candidate(by_fqn[fqn]) for fqn in of_interest}
    assert len({rendered[f].splitlines()[3] for f in of_interest}) == 2  # 2 distinct AUTHORITY lines


def test_authority_integration_does_not_change_ranking_order(db_path):
    """Adding authority facts must never perturb fused rank/score — same
    lane weights, same frontier size, same ranking algorithm."""
    docs = [_doc("dbo.widgets"), _doc("dbo.orders")]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0), docs[1].document_hash: _unit(0.0, 1.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)
    assert [c.object_fqn for c in result.candidates] == ["dbo.widgets", "dbo.orders"]
    assert result.candidates[0].fused_rank == 1
    assert result.candidates[0].semantic_rank == 1


# ── Phase 7, Step 3F, Part 6 — authority-family collapse (unit-level,
#    hand-built inputs; no DB, no embedding) ────────────────────────────────

from core.sqlservice.schema_frontier import _collapse_authority_family_representatives  # noqa: E402
from core.sqlservice.schema_object_authority import SchemaObjectAuthority  # noqa: E402


def _authority(
    object_fqn, authority_class, *, canonical_table_fqns=(), preferred_view_for_entities=(),
):
    return SchemaObjectAuthority(
        object_fqn=object_fqn, object_type="VIEW" if "vw" in object_fqn.lower() else "TABLE",
        canonical_table_fqns=tuple(canonical_table_fqns),
        preferred_view_for_entities=tuple(preferred_view_for_entities),
        authority_class=authority_class, restriction_status="NONE", restrictions=(),
        grain_class="BASE_ROW", aggregation_status="NONE", row_preservation_status="PRESERVED",
        dependency_fqns=(), unsafe_dependency_fqns=(), evidence=(), warnings=(),
    )


def test_declared_preferred_view_retained_over_canonical_table():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget": _authority("dbo.vw_Widget", "DECLARED_PREFERRED_VIEW", preferred_view_for_entities=["Widget"]),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.vw_widget"], authorities, fqn_by_key,
    )
    assert collapsed == ["dbo.vw_widget"]
    assert suppressed == {"dbo.vw_widget": ("dbo.Widget",)}


def test_canonical_table_retained_over_merely_proven_equivalent_view():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget": _authority("dbo.vw_Widget", "PROVEN_EQUIVALENT_VIEW", canonical_table_fqns=["Widget"]),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.vw_widget", "dbo.widget"], authorities, fqn_by_key,
    )
    assert collapsed == ["dbo.widget"]
    assert suppressed == {"dbo.widget": ("dbo.vw_Widget",)}


def test_restricted_view_never_collapsed():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget": _authority("dbo.vw_Widget", "RESTRICTED_VIEW"),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.vw_widget"], authorities, fqn_by_key,
    )
    assert set(collapsed) == {"dbo.widget", "dbo.vw_widget"}
    assert suppressed == {}


def test_conditionally_compatible_view_never_collapsed():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget": _authority("dbo.vw_Widget", "CONDITIONALLY_COMPATIBLE_VIEW"),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.vw_widget"], authorities, fqn_by_key,
    )
    assert set(collapsed) == {"dbo.widget", "dbo.vw_widget"}
    assert suppressed == {}


def test_unknown_view_never_collapsed():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget": _authority("dbo.vw_Widget", "UNKNOWN"),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.vw_widget"], authorities, fqn_by_key,
    )
    assert set(collapsed) == {"dbo.widget", "dbo.vw_widget"}
    assert suppressed == {}


def test_incompatible_and_unsafe_view_never_collapsed():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_agg": _authority("dbo.vw_Agg", "INCOMPATIBLE_GRAIN"),
        "dbo.vw_unsafe": _authority("dbo.vw_Unsafe", "UNSAFE_DEPENDENCY"),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.vw_agg", "dbo.vw_unsafe"], authorities, fqn_by_key,
    )
    assert set(collapsed) == {"dbo.widget", "dbo.vw_agg", "dbo.vw_unsafe"}
    assert suppressed == {}


def test_unrelated_objects_never_merged_across_different_entities():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.gadget": _authority("dbo.Gadget", "CANONICAL_TABLE", canonical_table_fqns=["Gadget"]),
        "dbo.vw_gadget": _authority("dbo.vw_Gadget", "PROVEN_EQUIVALENT_VIEW", canonical_table_fqns=["Gadget"]),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(
        ["dbo.widget", "dbo.gadget", "dbo.vw_gadget"], authorities, fqn_by_key,
    )
    assert "dbo.widget" in collapsed  # unrelated entity, untouched
    assert "dbo.vw_gadget" not in collapsed  # only ITS OWN entity's pair collapses
    assert suppressed == {"dbo.gadget": ("dbo.vw_Gadget",)}


def test_collapse_deterministic_under_shuffled_input_order():
    authorities = {
        "dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"]),
        "dbo.vw_widget_a": _authority("dbo.vw_Widget_A", "DECLARED_PREFERRED_VIEW", preferred_view_for_entities=["Widget"]),
        "dbo.gadget": _authority("dbo.Gadget", "CANONICAL_TABLE", canonical_table_fqns=["Gadget"]),
        "dbo.vw_gadget": _authority("dbo.vw_Gadget", "PROVEN_EQUIVALENT_VIEW", canonical_table_fqns=["Gadget"]),
    }
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    keys = list(authorities.keys())
    import random

    results = []
    for _ in range(5):
        shuffled = keys[:]
        random.Random(hash(tuple(shuffled))).shuffle(shuffled)
        collapsed, suppressed = _collapse_authority_family_representatives(shuffled, authorities, fqn_by_key)
        results.append((sorted(collapsed), suppressed))
    assert all(r == results[0] for r in results)


def test_bounded_collapsed_member_observability():
    from core.sqlservice.schema_frontier import _MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE

    authorities = {"dbo.widget": _authority("dbo.Widget", "CANONICAL_TABLE", canonical_table_fqns=["Widget"])}
    keys = ["dbo.widget"]
    for i in range(_MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE + 5):
        key = f"dbo.vw_widget_{i}"
        authorities[key] = _authority(f"dbo.vw_Widget_{i}", "PROVEN_EQUIVALENT_VIEW", canonical_table_fqns=["Widget"])
        keys.append(key)
    fqn_by_key = {k: a.object_fqn for k, a in authorities.items()}
    collapsed, suppressed = _collapse_authority_family_representatives(keys, authorities, fqn_by_key)
    assert collapsed == ["dbo.widget"]
    assert len(suppressed["dbo.widget"]) <= _MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE


def test_ranking_provenance_retained_on_representative(db_path):
    """Integration-level: the surviving representative's own FrontierCandidate
    carries bounded evidence that a member was collapsed (Part 2, requirement 7)."""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(db_path)
    now = "2026-09-24T00:00:00Z"
    conn.execute(
        "INSERT INTO semantic_entity_contracts "
        "(source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, "
        " resolution_status, confidence, contract_json, metadata_revision, generation_method, created_at, updated_at) "
        "VALUES (?, 'Widget', 1, 'dbo.widgets', NULL, 'RESOLVED', 1.0, '{}', 'r', 'auto_grounding', ?, ?)",
        (SOURCE_ID, now, now),
    )
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, referenced_tables_json, "
        " restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, 'dbo.vw_widgets', 'CREATE VIEW v AS SELECT a.id FROM dbo.widgets a', 'x', '[\"dbo.widgets\"]', NULL, 0.0, 1, ?)",
        (SOURCE_ID, now),
    )
    conn.commit()
    conn.close()

    docs = [_doc("dbo.widgets", metadata_extra={"primary_key_columns": ("id",)}), _view_doc("dbo.vw_widgets")]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0), docs[1].document_hash: _unit(0.9, 0.1, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)
    by_fqn = {c.object_fqn: c for c in result.candidates}
    assert "dbo.vw_widgets" not in by_fqn
    assert by_fqn["dbo.widgets"].authority_collapsed_member_fqns == ("dbo.vw_widgets",)
    assert "AUTHORITY_COLLAPSED: dbo.vw_widgets" in render_frontier_candidate(by_fqn["dbo.widgets"])
    assert any("authority-family collapse" in w for w in result.warnings)


def test_no_domain_specific_tokens_in_collapse_logic():
    import inspect

    source = inspect.getsource(_collapse_authority_family_representatives)
    forbidden = ("edu_", "exampleco", "student", "classsignup")
    lowered = source.lower()
    for token in forbidden:
        assert token not in lowered


# ── Phase 7, Step 3F, Part 6 — question-relevant column preservation ───────

from core.sqlservice.schema_frontier import (  # noqa: E402
    _order_columns_by_relevance,
    _trim_least_relevant_column,
    FrontierCandidate,
)


def test_camel_case_feepaid_matches_fee_paid_question():
    query_tokens = frozenset(tokenize("Total fees paid by month"))
    ordered = _order_columns_by_relevance(
        ["AccessExpirationDate", "AddlFee", "CancelID", "FeePaid", "HiredDate"], query_tokens,
    )
    assert ordered[0] == "FeePaid"


def test_underscore_and_punctuation_normalization():
    query_tokens = frozenset(tokenize("What is the fee_paid amount?"))
    ordered = _order_columns_by_relevance(["ZzTop", "Fee-Paid.Amount", "AaFirst"], query_tokens)
    assert ordered[0] == "Fee-Paid.Amount"


def test_relevant_late_alphabetical_column_displaces_irrelevant_early_column():
    query_tokens = frozenset(tokenize("Total fees paid by month"))
    columns = ["AAAAIrrelevant", "BBBBIrrelevant", "ZZZFeePaid"]
    ordered = _order_columns_by_relevance(columns, query_tokens)
    assert ordered[0] == "ZZZFeePaid"


def test_ties_broken_alphabetically_not_by_input_order():
    ordered_a = _order_columns_by_relevance(["Zebra", "Apple", "Mango"], frozenset())
    ordered_b = _order_columns_by_relevance(["Mango", "Zebra", "Apple"], frozenset())
    assert ordered_a == ordered_b == ("Apple", "Mango", "Zebra")


def _minimal_candidate(**overrides) -> FrontierCandidate:
    base = dict(
        object_fqn="dbo.T", object_type="TABLE", retrieval_tier=1, business_name=None, business_purpose=None,
        domain=None, aliases=(), primary_key_columns=("id",), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=(),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9, lexical_rank=None,
        lexical_score=None, business_concept_rank=None, business_concept_score=None, canonical_rank=None,
        canonical_total_score=None, fused_rank=1, fused_score=0.05, admitted_via_lanes=("semantic",),
        matched_business_concepts=(),
    )
    base.update(overrides)
    return FrontierCandidate(**base)


def test_pk_always_preserved_by_trim():
    candidate = _minimal_candidate(primary_key_columns=("id",), dimension_columns=("A", "B"))
    trimmed, _ = _trim_least_relevant_column(candidate, frozenset())
    assert trimmed.primary_key_columns == ("id",)


def test_local_fk_always_preserved_by_trim():
    candidate = _minimal_candidate(local_foreign_key_columns=("ClassID",), dimension_columns=("A",))
    trimmed, _ = _trim_least_relevant_column(candidate, frozenset())
    assert trimmed.local_foreign_key_columns == ("ClassID",)


def test_trim_removes_least_relevant_column_globally_across_categories():
    query_tokens = frozenset(tokenize("total fees paid"))
    candidate = _minimal_candidate(
        dimension_columns=_order_columns_by_relevance(("FeePaid", "Irrelevant"), query_tokens),
        date_columns=_order_columns_by_relevance(("StartDate", "EndDate"), query_tokens),
    )
    trimmed, removed_relevant = _trim_least_relevant_column(candidate, query_tokens)
    # Nothing in date_columns matches the question at all -- one of those
    # (its own tail, "EndDate" or "StartDate", alpha tie-broken) should be
    # cut before ever touching FeePaid (a genuine match) in dimension_columns.
    assert "FeePaid" in trimmed.dimension_columns
    assert not removed_relevant
    assert len(trimmed.date_columns) == 1


def test_relevant_column_survives_tight_character_budget(db_path):
    docs = [_doc("dbo.wide_table", column_names=[f"Irrelevant{i}" for i in range(20)] + ["FeePaid"])]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "total fees paid", build.id, SOURCE_ID, USER_ID, provider, max_characters_per_object=150,
    )
    assert result.status == STATUS_OK
    # FeePaid must survive the tight per-object budget even though it is
    # alphabetically last among 21 columns -- most/all of the irrelevant
    # ones are trimmed instead.
    assert "FeePaid" in result.candidates[0].dimension_columns
    assert len(result.candidates[0].dimension_columns) < 21


def test_incomplete_column_summary_observability(db_path):
    """When the budget is so tight that even a genuinely relevant column
    must be sacrificed, the frontier records a bounded warning (Part 3,
    requirement 10) -- never silently."""
    docs = [_doc("dbo.wide_table", column_names=["FeePaidA", "FeePaidB", "Irrelevant"])]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "total fees paid", build.id, SOURCE_ID, USER_ID, provider, max_characters_per_object=75,
    )
    assert result.status == STATUS_OK
    assert any("column summary incomplete" in w for w in result.warnings)


def test_column_and_character_bounds_still_enforced(db_path):
    docs = [_doc("dbo.wide_table", column_names=[f"Col{i}" for i in range(50)])]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("columns", build.id, SOURCE_ID, USER_ID, provider, max_columns_per_category=5)
    assert len(result.candidates[0].dimension_columns) <= 5
    assert len(render_frontier_candidate(result.candidates[0])) <= 600  # default max_characters_per_object


def test_target_fk_column_never_invented_as_local_still_holds(db_path):
    """Regression: the Step 3F column-relevance changes must never alter
    local_foreign_key_columns' own derivation (still sourced only from
    outbound confirmed edges, never a target-side descriptor)."""
    docs = [_doc("dbo.orders")]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("orders", build.id, SOURCE_ID, USER_ID, provider)
    assert result.candidates[0].local_foreign_key_columns == ()


def test_nonexistent_semantic_column_excluded():
    query_tokens = frozenset(tokenize("fee paid"))
    # Only real, already-supplied column names are ever scored/ordered --
    # this function never invents or adds a column not already passed in.
    ordered = _order_columns_by_relevance(["RealColumnA", "RealColumnB"], query_tokens)
    assert set(ordered) == {"RealColumnA", "RealColumnB"}


# ── Phase 7, Step 3J3, Part 3 — snapshot classification integration ──────


def test_confirmed_snapshot_excluded_with_observable_warning(db_path):
    from core.sqlservice.snapshot_classifier import clear_snapshot_classification_cache

    clear_snapshot_classification_cache()
    cols = ("id", "name", "amount")
    docs = [
        _doc("dbo.orders", column_names=cols, metadata_extra={"primary_key_columns": ("id",)}),
        _doc("dbo.orders_2019_05_21", column_names=cols),  # identical columns, no PK
    ]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.99, 0.01, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("orders", build.id, SOURCE_ID, USER_ID, provider)

    fqns = {c.object_fqn for c in result.candidates}
    assert "dbo.orders" in fqns
    assert "dbo.orders_2019_05_21" not in fqns  # confirmed snapshot excluded from admission

    assert any(
        "orders_2019_05_21" in w and "canonical_sibling" in w and "dbo.orders" in w for w in result.warnings
    )  # suppression is observable, never silent


def test_uncertain_snapshot_retained_with_penalty_and_annotation(db_path):
    from core.sqlservice.snapshot_classifier import clear_snapshot_classification_cache

    clear_snapshot_classification_cache()
    cols = ("id", "name", "amount")
    docs = [
        _doc("dbo.widgets", column_names=cols),  # no PK either side -- no differential signal
        _doc("dbo.widgets_2019_05_21", column_names=cols),
    ]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.99, 0.01, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("widgets", build.id, SOURCE_ID, USER_ID, provider)

    fqns = {c.object_fqn for c in result.candidates}
    assert "dbo.widgets_2019_05_21" in fqns  # uncertain -- never excluded

    candidate = next(c for c in result.candidates if c.object_fqn == "dbo.widgets_2019_05_21")
    assert candidate.snapshot_uncertain is True
    assert candidate.snapshot_canonical_sibling_fqn == "dbo.widgets"
    assert "UNCERTAIN_SNAPSHOT:" in render_frontier_candidate(candidate)

    canonical = next(c for c in result.candidates if c.object_fqn == "dbo.widgets")
    assert canonical.snapshot_uncertain is False  # the real object itself is never flagged


def test_snapshot_classification_never_affects_business_knowledge_or_authority_evidence(db_path):
    """A CONFIRMED snapshot's exclusion, and an UNCERTAIN one's penalty,
    must never remove or alter another candidate's own authority/business-
    knowledge evidence -- these are independent, additive layers."""
    from core.sqlservice.snapshot_classifier import clear_snapshot_classification_cache

    clear_snapshot_classification_cache()
    cols = ("id", "name")
    docs = [
        _doc("dbo.orders", column_names=cols, metadata_extra={"primary_key_columns": ("id",)}),
        _doc("dbo.orders_2019_05_21", column_names=cols),
    ]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.99, 0.01, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("orders", build.id, SOURCE_ID, USER_ID, provider)
    canonical = next(c for c in result.candidates if c.object_fqn == "dbo.orders")
    assert canonical.authority_class == "UNKNOWN"  # unaffected/untouched by this feature
    assert canonical.business_knowledge_mappings == ()  # no package configured -- unaffected


def test_no_question_text_in_frontier_cache_identity():
    """Question-column matching is computed fresh per request -- the
    build-level caches (vector/lexical/business-concept/canonical/
    authority) never take question text as part of their own cache key."""
    import inspect

    from core.sqlservice import schema_object_authority as authority_module
    from core.sqlservice import canonical_scoring as canonical_module

    for module, identity_cls_name in (
        (authority_module, "SchemaObjectAuthorityIdentity"),
        (canonical_module, "CanonicalScoringIdentity"),
    ):
        identity_cls = getattr(module, identity_cls_name)
        fields = {f for f in identity_cls.__dataclass_fields__}
        assert "question" not in fields and "query" not in fields


# ── Phase 7, Step 6E1 (replaces Step 6E, Part 4) — tier-3/4 admission now
#    requires a confirmed relationship AND an owner-confirmed VERIFIED
#    business-knowledge mapping; a confirmed relationship alone is never
#    sufficient. The positive (VERIFIED-mapping-present) case lives in
#    tests/test_business_knowledge_evidence_integration.py, which already
#    has the real package/mapping-store fixtures this needs; this file
#    keeps the default fail-closed (no mapping) case. ─────────────────────


def test_tier3_relationship_neighbor_without_verified_mapping_is_not_force_admitted(db_path):
    """A Tier-3 object (e.g. a generic framework "Users" table) reachable
    by a REAL declared foreign key from an admitted Tier-1 entity must NOT
    reach the frontier merely because of that relationship -- Phase 7,
    Step 6E1's safety audit found the prior rule (relationship alone)
    force-admitted arbitrary framework/authentication/permission objects
    for any question touching any of their many anchors. Admission now
    additionally requires its own VERIFIED business-knowledge governance
    mapping (see test_business_knowledge_evidence_integration.py for the
    positive case), which does not exist here -- fail closed."""
    entity = _doc(
        "dbo.Student", column_names=("StudentID", "StudentUserID"),
        metadata_extra={"confirmed_relationship_targets": ("framework.Users.UserID",)},
    )
    identity_table = _doc(
        "framework.Users", tier=3, column_names=("UserID", "FirstName", "LastName"),
    )
    docs = [entity, identity_table]
    vectors = {
        entity.document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        identity_table.document_hash: _unit(0.0, 1.0, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)

    fqns = {c.object_fqn for c in result.candidates}
    assert "framework.Users" not in fqns
    assert not any("framework.Users" in w for w in result.warnings)


def test_tier3_object_unrelated_to_any_admitted_object_stays_excluded(db_path):
    entity = _doc("dbo.Student", column_names=("StudentID",))
    unrelated_tier3 = _doc("framework.AuditLog", tier=3, column_names=("LogID", "Message"))
    docs = [entity, unrelated_tier3]
    vectors = {
        entity.document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        unrelated_tier3.document_hash: _unit(1.0, 0.0, 0.0, 0.0),  # even identical vector
    }
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)
    assert {c.object_fqn for c in result.candidates} == {"dbo.Student"}


def test_relationship_force_admit_without_verified_mapping_admits_neither_hop(db_path):
    """framework.B is reachable only THROUGH framework.A, and NEITHER
    carries a VERIFIED business-knowledge mapping -- both must stay
    excluded (fail closed). See test_business_knowledge_evidence_
    integration.py::test_relationship_force_admit_never_chains_two_hops
    for the positive case (A has a VERIFIED mapping, B does not, and B
    must still never be reachable by chaining through A)."""
    entity = _doc(
        "dbo.Student", column_names=("StudentID",),
        metadata_extra={"confirmed_relationship_targets": ("framework.A.ID",)},
    )
    tier3_a = _doc(
        "framework.A", tier=3, column_names=("ID", "BID"),
        metadata_extra={"confirmed_relationship_targets": ("framework.B.ID",)},
    )
    tier3_b = _doc("framework.B", tier=3, column_names=("ID", "Value"))
    docs = [entity, tier3_a, tier3_b]
    vectors = {d.document_hash: _unit(1.0, 0.0, 0.0, 0.0) for d in docs}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)
    fqns = {c.object_fqn for c in result.candidates}
    assert "framework.A" not in fqns
    assert "framework.B" not in fqns


def test_relationship_force_admit_bounded_by_max_admitted_candidates(db_path):
    entity = _doc(
        "dbo.Student", column_names=("StudentID",),
        metadata_extra={"confirmed_relationship_targets": ("framework.Users.UserID",)},
    )
    identity_table = _doc("framework.Users", tier=3, column_names=("UserID", "FirstName"))
    docs = [entity, identity_table]
    vectors = {d.document_hash: _unit(1.0, 0.0, 0.0, 0.0) for d in docs}
    build = _build_with_docs(db_path, docs, vectors)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    result = build_schema_frontier(
        "Show student names", build.id, SOURCE_ID, USER_ID, provider, max_admitted_candidates=1,
    )
    fqns = {c.object_fqn for c in result.candidates}
    assert fqns == {"dbo.Student"}  # already at the cap before the tier-3 neighbor is considered
