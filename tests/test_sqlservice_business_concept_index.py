"""
Focused unit tests — Phase 7, Step 3B, Part 2/5: automatic business-concept
retrieval (core/sqlservice/business_concept_index.py).

These tests inject the four trust-source readers directly (monkeypatch)
rather than fabricating the exact OBJECT_CORE/VIEW_ENRICHED cache-hash
machinery or a full semantic_entity_contracts discovery run — those
subsystems already have their own dedicated test suites
(tests/test_sqlservice_semantic_enrichment_object_core*.py,
tests/test_sqlservice_view_semantic_enrichment*.py,
tests/test_semantic_contract_service.py, tests/test_entity_service.py,
tests/test_domain_service.py); this file tests ONLY this module's own
combination/trust/exclusion/caching logic. The end-to-end mechanism (real
OBJECT_CORE/entity_assignments/semantic_entity_contracts data actually
surfacing a real object for a real business term) was additionally verified
manually against a real, already-populated database during this milestone's
own audit — no fixture here repeats that dataset's names, per the "no
the reference database/golden-specific literals" requirement enforced by
test_no_domain_specific_literals_in_production_module below.

Uses only fabricated, generic fixture names (widgets/gadgets) — never a real
customer schema or business term.

Run:
    venv/Scripts/pytest tests/test_sqlservice_business_concept_index.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-business-concept-index-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-business-concept-index-salt-long-enough12")

from types import MappingProxyType

import pytest

import core.sqlservice.business_concept_index as bci
from core.sqlservice.lexical_index import tokenize


# ── Fixture plumbing (pure — no database at all for most tests below) ─────


class _Doc:
    """Minimal duck-typed retrieval-document stand-in — only the attributes
    core.sqlservice.business_concept_index._assemble_index itself reads."""

    def __init__(self, object_fqn, *, object_type="TABLE", tier=1, column_names=()):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = tier
        self.document_hash = f"hash-{object_fqn}"
        self.document_text = ""
        self.metadata = MappingProxyType({"column_names": tuple(column_names)})


def _patch_sources(
    monkeypatch,
    *,
    object_core=None,
    view_enriched=None,
    entity_domain=None,
    semantic_contract=None,
):
    monkeypatch.setattr(bci, "_object_core_terms_by_fqn_lower", lambda *a, **k: dict(object_core or {}))
    monkeypatch.setattr(bci, "_view_enriched_terms_by_fqn_lower", lambda *a, **k: dict(view_enriched or {}))
    monkeypatch.setattr(bci, "_entity_domain_terms_by_fqn_lower", lambda *a, **k: dict(entity_domain or {}))
    monkeypatch.setattr(
        bci, "_semantic_contract_terms_by_fqn_lower", lambda *a, **k: dict(semantic_contract or {}),
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    bci.clear_business_concept_index_cache()
    yield
    bci.clear_business_concept_index_cache()


# ── Combination / trust-source correctness ─────────────────────────────────


def test_generic_business_term_retrieves_a_differently_named_object(monkeypatch):
    """The core Part 2 scenario, in miniature: a table named for its
    internal technical identity (dbo.zz_container) is retrievable by a
    generic business term ("gadget") solely because a trusted entity
    assignment says so — no name/column overlap between the question term
    and the object's own real name/columns at all."""
    docs = [_Doc("dbo.zz_container", column_names=("id", "flag_a"))]
    _patch_sources(
        monkeypatch,
        entity_domain={"dbo.zz_container": [("Gadget", "ENTITY_ASSIGNMENT", "synonyms")]},
    )
    index = bci.load_business_concept_index(
        1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False,
    )
    results = bci.search_business_concept_index(index, tokenize("how many gadgets are there"), top_k=10)
    assert len(results) == 1
    assert results[0].object_fqn == "dbo.zz_container"
    assert results[0].matched_evidence[0].source == bci.SOURCE_ENTITY_ASSIGNMENT
    assert results[0].matched_evidence[0].concept == "Gadget"


def test_no_match_without_any_trusted_evidence(monkeypatch):
    docs = [_Doc("dbo.zz_container")]
    _patch_sources(monkeypatch)  # every source empty
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    results = bci.search_business_concept_index(index, tokenize("gadget"), top_k=10)
    assert results == ()


def test_unrelated_query_term_does_not_match(monkeypatch):
    docs = [_Doc("dbo.zz_container")]
    _patch_sources(
        monkeypatch, entity_domain={"dbo.zz_container": [("Gadget", "ENTITY_ASSIGNMENT", "synonyms")]},
    )
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    results = bci.search_business_concept_index(index, tokenize("widget"), top_k=10)
    assert results == ()


def test_multiple_sources_merge_with_provenance_preserved(monkeypatch):
    docs = [_Doc("dbo.zz_container")]
    _patch_sources(
        monkeypatch,
        object_core={"dbo.zz_container": [("gadget catalog", bci.SOURCE_OBJECT_CORE_ALIAS, "synonyms")]},
        view_enriched={"dbo.zz_container": [("Gadget Domain", bci.SOURCE_VIEW_ENRICHED_DOMAIN, "synonyms")]},
        entity_domain={"dbo.zz_container": [("Gadget", bci.SOURCE_ENTITY_ASSIGNMENT, "synonyms")]},
    )
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    results = bci.search_business_concept_index(index, tokenize("gadget"), top_k=10)
    assert len(results) == 1
    sources = {ev.source for ev in results[0].matched_evidence}
    assert sources == {bci.SOURCE_OBJECT_CORE_ALIAS, bci.SOURCE_VIEW_ENRICHED_DOMAIN, bci.SOURCE_ENTITY_ASSIGNMENT}


# ── Tier / staleness / wrong-object exclusion ───────────────────────────────


def test_tier3_object_excluded_even_with_evidence(monkeypatch):
    docs = [_Doc("dbo.zz_container", tier=3)]
    _patch_sources(
        monkeypatch, entity_domain={"dbo.zz_container": [("Gadget", "ENTITY_ASSIGNMENT", "synonyms")]},
    )
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    results = bci.search_business_concept_index(index, tokenize("gadget"), top_k=10)
    assert results == ()


def test_tier4_archive_object_excluded_even_with_evidence(monkeypatch):
    docs = [_Doc("dbo.zz_container_backup", tier=4)]
    _patch_sources(
        monkeypatch, entity_domain={"dbo.zz_container_backup": [("Gadget", "ENTITY_ASSIGNMENT", "synonyms")]},
    )
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    assert bci.search_business_concept_index(index, tokenize("gadget"), top_k=10) == ()


def test_evidence_for_object_not_in_this_build_is_dropped(monkeypatch):
    """Simulates a stale/wrong-source row (a table_fqn from a DIFFERENT
    build/source) leaking through a trust-source reader — must never be
    trusted just because the reader returned it."""
    docs = [_Doc("dbo.zz_container")]
    _patch_sources(
        monkeypatch,
        entity_domain={"dbo.some_other_source_table": [("Gadget", "ENTITY_ASSIGNMENT", "synonyms")]},
    )
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    assert bci.search_business_concept_index(index, tokenize("gadget"), top_k=10) == ()


def test_semantic_contract_attribute_with_nonexistent_column_excluded(monkeypatch):
    """_semantic_contract_terms_by_fqn_lower drops an attribute naming a
    column that does not exist on the real object, per its own
    real_columns_by_fqn_lower contract — tested directly, no DB needed."""
    real_columns = {"dbo.zz_container": frozenset({"id", "flag_a"})}

    def fake_get_latest_entity_contract(source_id, entity_name):
        return {
            "resolution_status": "RESOLVED",
            "contract": {
                "primary_key": {"table_fqn": "dbo.zz_container"},
                "synonyms": ["gadget"],
                "attributes": [
                    {"business_name": "Flag A Label", "column_name": "flag_a"},
                    {"business_name": "Ghost Column Label", "column_name": "does_not_exist"},
                ],
            },
        }

    monkeypatch.setattr(bci, "_distinct_entity_names", lambda source_id, user_id: ("Gadget",))
    monkeypatch.setattr(bci, "get_latest_entity_contract", fake_get_latest_entity_contract)
    terms = bci._semantic_contract_terms_by_fqn_lower(42, "u1", real_columns)

    entries = terms["dbo.zz_container"]
    business_names = {e[0] for e in entries}
    assert "Flag A Label" in business_names
    assert "Ghost Column Label" not in business_names
    column_names_kept = {e[3] for e in entries if e[3]}
    assert column_names_kept == {"flag_a"}


def test_semantic_contract_partial_status_excluded(monkeypatch):
    monkeypatch.setattr(bci, "_distinct_entity_names", lambda source_id, user_id: ("Gadget",))
    monkeypatch.setattr(
        bci, "get_latest_entity_contract",
        lambda source_id, entity_name: {
            "resolution_status": "PARTIAL",
            "contract": {"primary_key": {"table_fqn": "dbo.zz_container"}, "synonyms": ["gadget"]},
        },
    )
    terms = bci._semantic_contract_terms_by_fqn_lower(42, "u1", {"dbo.zz_container": frozenset({"id"})})
    assert terms == {}


def test_entity_assignment_unknown_label_excluded(monkeypatch):
    monkeypatch.setattr(
        bci, "list_entity_assignments",
        lambda source_id, user_id: [
            {"table_fqn": "dbo.zz_container", "entity": "Unknown", "assignment_source": "rule"},
        ],
    )
    monkeypatch.setattr(bci, "list_domain_assignments", lambda source_id, user_id: [])
    terms = bci._entity_domain_terms_by_fqn_lower(42, "u1")
    assert terms == {}


def test_preliminary_connect_time_assignment_source_excluded(monkeypatch):
    monkeypatch.setattr(
        bci, "list_entity_assignments",
        lambda source_id, user_id: [
            {"table_fqn": "dbo.zz_container", "entity": "Gadget", "assignment_source": "preliminary_connect_time"},
        ],
    )
    monkeypatch.setattr(bci, "list_domain_assignments", lambda source_id, user_id: [])
    terms = bci._entity_domain_terms_by_fqn_lower(42, "u1")
    assert terms == {}


# ── Fail-open behavior ──────────────────────────────────────────────────────


def test_vector_lexical_unaffected_when_no_semantic_index_exists(monkeypatch):
    """An object with zero business-concept evidence must score exactly
    "not present" (never a fabricated zero/None entry that could change
    downstream fusion arithmetic) — this is what lets
    core.sqlservice.schema_context_retrieval's existing vector/lexical
    behavior remain byte-for-byte unchanged whenever this lane has nothing
    to contribute."""
    docs = [_Doc("dbo.zz_container")]
    _patch_sources(monkeypatch)
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    assert index.lexical_index.document_count == 1  # the document exists...
    assert bci.search_business_concept_index(index, tokenize("gadget"), top_k=10) == ()  # ...but never scores


def test_a_failing_trust_source_never_breaks_index_loading(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated trust-source failure")

    monkeypatch.setattr(bci, "_object_core_terms_by_fqn_lower", _boom)
    docs = [_Doc("dbo.zz_container")]
    index = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}), use_cache=False)
    assert index is not None
    assert bci.search_business_concept_index(index, tokenize("anything"), top_k=10) == ()


# ── Caching: reuse, invalidation, and no question data retained ────────────


def test_cache_reuse_when_enrichment_state_unchanged(monkeypatch):
    call_count = {"n": 0}

    def _counting_object_core(*a, **k):
        call_count["n"] += 1
        return {}

    monkeypatch.setattr(bci, "_object_core_terms_by_fqn_lower", _counting_object_core)
    monkeypatch.setattr(bci, "_view_enriched_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_entity_domain_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_semantic_contract_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_enrichment_state", lambda source_id: ("fixed-state",))

    docs = [_Doc("dbo.zz_container")]
    idx1 = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}))
    idx2 = bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}))
    assert idx1 is idx2
    assert call_count["n"] == 1  # second call was a pure cache hit


def test_cache_invalidates_when_enrichment_state_changes(monkeypatch):
    call_count = {"n": 0}

    def _counting_object_core(*a, **k):
        call_count["n"] += 1
        return {}

    state = {"value": ("state-a",)}
    monkeypatch.setattr(bci, "_object_core_terms_by_fqn_lower", _counting_object_core)
    monkeypatch.setattr(bci, "_view_enriched_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_entity_domain_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_semantic_contract_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_enrichment_state", lambda source_id: state["value"])

    docs = [_Doc("dbo.zz_container")]
    bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}))
    state["value"] = ("state-b",)  # simulates a new enrichment/adjudication run
    bci.load_business_concept_index(1, 42, "u1", docs, allowed_tiers=frozenset({1, 2}))
    assert call_count["n"] == 2  # rebuilt, not reused


def test_cache_survives_reconnect_with_same_build_and_state(monkeypatch):
    """Same build_id + unchanged enrichment_state (an ordinary reconnect to
    the identical schema) must be a cache hit even across two separate
    load_business_concept_index() calls with fresh document objects."""
    monkeypatch.setattr(bci, "_object_core_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_view_enriched_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_entity_domain_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_semantic_contract_terms_by_fqn_lower", lambda *a, **k: {})
    monkeypatch.setattr(bci, "_enrichment_state", lambda source_id: ("same-state",))

    idx1 = bci.load_business_concept_index(7, 42, "u1", [_Doc("dbo.zz_container")], allowed_tiers=frozenset({1, 2}))
    idx2 = bci.load_business_concept_index(7, 42, "u1", [_Doc("dbo.zz_container")], allowed_tiers=frozenset({1, 2}))
    assert idx1 is idx2


def test_no_question_data_retained_in_index_or_identity(monkeypatch):
    """Structural guarantee: nothing question-shaped is ever a field on the
    cached objects — search_business_concept_index takes query_tokens as a
    pure function argument only, never stored."""
    import dataclasses

    identity_fields = {f.name for f in dataclasses.fields(bci.BusinessConceptIndexIdentity)}
    index_fields = {f.name for f in dataclasses.fields(bci.BusinessConceptIndex)}
    for banned in ("question", "query", "query_vector", "embedding"):
        assert not any(banned in f for f in identity_fields)
        assert not any(banned in f for f in index_fields)


def test_clear_and_size_helpers():
    bci.clear_business_concept_index_cache()
    assert bci.business_concept_index_cache_size() == 0


# ── No hardcoded/dataset-specific literals in production logic ────────────


def test_no_domain_specific_literals_in_production_module():
    import inspect

    source = inspect.getsource(bci)
    # Deliberately does NOT ban the substring "exampleco" itself — this module's
    # own docstring legitimately explains, in the negative ("no the reference database-
    # specific... logic"), that no such reference exists; banning the bare
    # acronym would make that sentence itself a false positive. The real
    # guard is for actual dataset object/column names a hardcoded mapping
    # would use.
    banned = ("edu_track", "pathname", "mentorship", "internshipprogram")
    lowered = source.lower()
    for token in banned:
        assert token not in lowered, f"found dataset-specific literal {token!r} in production module"
