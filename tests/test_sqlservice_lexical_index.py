"""
Focused unit tests — SQL rebuild Phase 5, Step 4: deterministic structured
lexical index (core/sqlservice/lexical_index.py).

Pure, in-memory, no SQLite, no OpenAI, no real embeddings anywhere in this
file — every IndexedSchemaDocument-shaped fixture is hand-constructed.

Run:
    venv/Scripts/pytest tests/test_sqlservice_lexical_index.py -v
"""

import ast
import math
import random
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from core.sqlservice.lexical_index import (
    FIELD_BUSINESS_NAME,
    FIELD_COLUMN_NAMES,
    FIELD_OBJECT_NAME,
    FIELD_SYNONYMS,
    LexicalIndexIdentity,
    build_lexical_index,
    clear_lexical_index_cache,
    lexical_index_cache_size,
    load_lexical_index,
    search_lexical_index,
    tokenize,
)


@pytest.fixture(autouse=True)
def _isolated_lexical_cache():
    clear_lexical_index_cache()
    yield
    clear_lexical_index_cache()


# ── Fixture plumbing ────────────────────────────────────────────────────

class _Doc:
    """Duck-typed stand-in for IndexedSchemaDocument — build_lexical_index
    only reads object_fqn/object_type/retrieval_tier/document_hash/
    document_text/metadata, never anything vector-specific."""

    def __init__(self, object_fqn, *, object_type="TABLE", tier=1, metadata=None, text=None):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = tier
        self.document_hash = "h-" + object_fqn
        self.document_text = text or f"OBJECT: {object_fqn}"
        self.metadata = MappingProxyType(metadata or {})


_IDENTITY = LexicalIndexIdentity(build_id=1, embedding_provider="openai", embedding_model="m", embedding_dimensions=4)


# ── 1. Tokenizer: generic normalization rules ──────────────────────────

def test_tokenize_snake_case():
    assert tokenize("student_signup_date") == ("student", "signup", "date")


def test_tokenize_camel_case():
    assert tokenize("StudentSignupDate") == ("student", "signup", "date")


def test_tokenize_punctuation_and_case_insensitivity():
    assert tokenize("Students, Classes & Programs!") == ("student", "class", "program")


def test_tokenize_simple_plural_singular_matching():
    assert tokenize("classes") == ("class",)
    assert tokenize("students") == ("student",)
    assert tokenize("policies") == ("policy",)
    assert tokenize("status") == ("status",)  # ends in "s" but <=3-safe suffix guard doesn't over-strip words like this incorrectly
    assert tokenize("class") == tokenize("classes")


def test_tokenize_common_stop_words_removed():
    tokens = tokenize("How many students are there in the program?")
    assert "how" not in tokens
    assert "many" not in tokens
    assert "are" not in tokens
    assert "there" not in tokens
    assert "in" not in tokens
    assert "the" not in tokens
    assert "student" in tokens
    assert "program" in tokens


def test_tokenize_exact_object_phrase_produces_matching_tokens():
    # "EDU_Student" and "student" must land on the same token so an exact
    # object-name phrase match is possible downstream.
    assert "student" in tokenize("EDU_Student")


def test_tokenize_empty_and_none_safe():
    assert tokenize("") == ()
    assert tokenize(None) == ()


# ── 2. BM25F: exact/business-name/synonym/column matches ──────────────

def test_exact_canonical_name_outranks_semantically_narrow_alternative():
    docs = [
        _Doc("dbo.EDU_Student", metadata={
            "business_name": "Adf Student", "description": "Contains education data.",
            "synonyms": ("student", "learner", "enrollee"), "column_names": ("StudentID", "StartDate", "EndDate"),
        }),
        # A short, narrow, undocumented alternative whose only signal is a
        # synonym list — mirrors the Step 3 forensic finding.
        _Doc("dbo.EDU_StudentSignup", metadata={
            "synonyms": ("signup", "registration", "student signup"), "column_names": ("ClassID", "StudentUserID"),
        }),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("How many students are there?"), top_k=5)

    assert results[0].object_fqn == "dbo.EDU_Student"
    assert results[0].score > results[1].score
    assert FIELD_OBJECT_NAME in results[0].matched_fields


def test_synonym_match_scores_and_is_reported():
    docs = [
        _Doc("dbo.EDU_Track", metadata={"synonyms": ("program", "curriculum path")}),
        _Doc("dbo.unrelated_thing", metadata={}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("show me every program"), top_k=5)

    assert len(results) == 1
    assert results[0].object_fqn == "dbo.EDU_Track"
    assert FIELD_SYNONYMS in results[0].matched_fields
    assert "program" in results[0].matched_tokens


def test_business_name_match_scores_and_is_reported():
    docs = [
        _Doc("dbo.tbl1", metadata={"business_name": "Graduate Survey Answers"}),
        _Doc("dbo.tbl2", metadata={"business_name": "Unrelated Object"}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("graduate survey"), top_k=5)

    assert results[0].object_fqn == "dbo.tbl1"
    assert FIELD_BUSINESS_NAME in results[0].matched_fields


def test_column_name_match_scores_and_is_reported():
    docs = [
        _Doc("dbo.tbl1", metadata={"column_names": ("FeePaid", "AmountDue")}),
        _Doc("dbo.tbl2", metadata={"column_names": ("Notes", "Comments")}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("total fee paid"), top_k=5)

    assert results[0].object_fqn == "dbo.tbl1"
    assert FIELD_COLUMN_NAMES in results[0].matched_fields


def test_camelcase_snakecase_and_plural_column_names_all_match_singular_query():
    docs = [
        _Doc("dbo.a", metadata={"column_names": ("ClassSignups",)}),
        _Doc("dbo.b", metadata={"column_names": ("class_signup",)}),
        _Doc("dbo.c", metadata={"column_names": ("unrelated_column",)}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("class signup"), top_k=5)

    matched = {r.object_fqn for r in results}
    assert matched == {"dbo.a", "dbo.b"}


def test_missing_metadata_fields_degrade_gracefully_never_raises():
    # An older-build document with none of the new metadata keys — only
    # object_fqn is usable. Must not raise, must score on name alone.
    docs = [_Doc("dbo.EDU_Student", metadata={}), _Doc("dbo.unrelated", metadata={})]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("how many students"), top_k=5)

    assert [r.object_fqn for r in results] == ["dbo.EDU_Student"]


# ── 3. Length normalization: wide tables not punished, narrow ones don't auto-win ──

def test_long_wide_table_not_punished_for_having_many_columns():
    # A wide, well-documented table whose relevant terms ("fee", "paid")
    # are diluted among many other real columns must still clearly
    # outscore a narrow, single-purpose table with no matching content at
    # all — length normalization must not zero out its relevant signal.
    wide_columns = tuple([f"Col{i}" for i in range(40)]) + ("FeePaid", "AmountDue")
    docs = [
        _Doc("dbo.wide_signups", metadata={
            "business_name": "Class Signups", "synonyms": ("fee", "payment", "enrollment"),
            "column_names": wide_columns,
        }),
        _Doc("dbo.narrow_unrelated", metadata={"column_names": ("Id", "Name")}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("total fee paid"), top_k=5)

    assert len(results) == 1
    assert results[0].object_fqn == "dbo.wide_signups"
    assert results[0].score > 0


def test_narrow_undocumented_table_does_not_win_solely_from_short_text():
    # A short document with ZERO relevant tokens must never outrank a
    # longer document that actually contains the query terms — length
    # normalization rewards relevance, not brevity for its own sake.
    docs = [
        _Doc("dbo.EDU_Student", metadata={
            "business_name": "Adf Student", "description": "Contains education data about students.",
            "synonyms": ("student", "learner"), "column_names": ("StudentID", "StartDate", "EndDate", "CancelID"),
        }),
        _Doc("dbo.ML_ClassStudents_Placed", metadata={"column_names": ("CLoc", "ClassER")}),  # short, irrelevant
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("how many students are there"), top_k=5)

    assert results[0].object_fqn == "dbo.EDU_Student"


# ── 4. Deterministic ties and repeated searches ────────────────────────

def test_deterministic_tie_break_by_object_fqn():
    docs = [
        _Doc("dbo.zzz", metadata={"synonyms": ("widget",)}),
        _Doc("dbo.aaa", metadata={"synonyms": ("widget",)}),
        _Doc("dbo.mmm", metadata={"synonyms": ("widget",)}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("widget"), top_k=5)

    assert [r.object_fqn for r in results] == ["dbo.aaa", "dbo.mmm", "dbo.zzz"]
    assert results[0].score == results[1].score == results[2].score


def test_repeated_searches_are_bit_identical():
    docs = [
        _Doc("dbo.a", metadata={"synonyms": ("alpha", "widget")}),
        _Doc("dbo.b", metadata={"synonyms": ("beta", "widget")}),
    ]
    index = build_lexical_index(docs, _IDENTITY)
    query = tokenize("widget alpha")

    first = search_lexical_index(index, query, top_k=5)
    second = search_lexical_index(index, query, top_k=5)
    assert first == second


def test_no_query_token_overlap_returns_empty_never_raises():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("completely unrelated words"), top_k=5)
    assert results == ()


def test_empty_query_tokens_returns_empty():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    index = build_lexical_index(docs, _IDENTITY)
    assert search_lexical_index(index, (), top_k=5) == ()


def test_invalid_top_k_rejected():
    docs = [_Doc("dbo.a")]
    index = build_lexical_index(docs, _IDENTITY)
    with pytest.raises(ValueError):
        search_lexical_index(index, ("alpha",), top_k=0)


# ── 5. Cache identity / isolation ───────────────────────────────────────

def test_cache_hit_returns_same_object():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    first = load_lexical_index(docs, _IDENTITY)
    assert lexical_index_cache_size() == 1
    second = load_lexical_index(docs, _IDENTITY)
    assert second is first


def test_cache_identity_isolation_across_build_id():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    id_a = LexicalIndexIdentity(build_id=1, embedding_provider="openai", embedding_model="m", embedding_dimensions=4)
    id_b = LexicalIndexIdentity(build_id=2, embedding_provider="openai", embedding_model="m", embedding_dimensions=4)

    idx_a = load_lexical_index(docs, id_a)
    idx_b = load_lexical_index(docs, id_b)
    assert idx_a is not idx_b
    assert lexical_index_cache_size() == 2


def test_cache_identity_isolation_across_provider_model_dims():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    id_a = LexicalIndexIdentity(build_id=1, embedding_provider="openai", embedding_model="small", embedding_dimensions=4)
    id_b = LexicalIndexIdentity(build_id=1, embedding_provider="openai", embedding_model="large", embedding_dimensions=6)

    idx_a = load_lexical_index(docs, id_a)
    idx_b = load_lexical_index(docs, id_b)
    assert idx_a is not idx_b


def test_explicit_cache_clear_forces_rebuild():
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    first = load_lexical_index(docs, _IDENTITY)
    clear_lexical_index_cache()
    assert lexical_index_cache_size() == 0
    second = load_lexical_index(docs, _IDENTITY)
    assert second is not first
    assert second == first  # structurally identical, just rebuilt


def test_no_question_data_ever_cached():
    """The cache holds only corpus-level LexicalIndex objects, keyed by
    build/provider/model/dims — never anything question-shaped. Proven by
    showing the SAME cached index serves two totally different queries
    with no per-question state leaking between them."""
    docs = [
        _Doc("dbo.a", metadata={"synonyms": ("alpha",)}),
        _Doc("dbo.b", metadata={"synonyms": ("beta",)}),
    ]
    index = load_lexical_index(docs, _IDENTITY)
    assert lexical_index_cache_size() == 1

    r1 = search_lexical_index(index, tokenize("alpha"), top_k=5)
    r2 = search_lexical_index(index, tokenize("beta"), top_k=5)

    assert [r.object_fqn for r in r1] == ["dbo.a"]
    assert [r.object_fqn for r in r2] == ["dbo.b"]
    assert lexical_index_cache_size() == 1  # asking two different questions never grew the cache


# ── 6. Immutability ──────────────────────────────────────────────────────

def test_index_and_results_are_immutable():
    import dataclasses
    docs = [_Doc("dbo.a", metadata={"synonyms": ("alpha",)})]
    index = build_lexical_index(docs, _IDENTITY)
    results = search_lexical_index(index, tokenize("alpha"), top_k=5)

    with pytest.raises(dataclasses.FrozenInstanceError):
        index.document_count = 999
    with pytest.raises(dataclasses.FrozenInstanceError):
        results[0].score = 0.0


# ── 7. No database-specific (or any domain-specific) tokens in production code ──

_PRODUCTION_FILES = (
    "core/sqlservice/lexical_index.py",
    "core/sqlservice/schema_context_retrieval.py",
    "core/sqlservice/retrieval_documents.py",
    "core/sqlservice/context_packing.py",
)

# Real the reference database table-name fragments and business vocabulary confirmed present
# in this project's golden fixtures (development validation material (not included here))
# — none of these literal strings may appear in production ranking code.
_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    for rel_path in _PRODUCTION_FILES:
        source = (repo_root / rel_path).read_text(encoding="utf-8")
        for banned in _BANNED_DOMAIN_TOKENS:
            assert banned not in source, f"{rel_path} contains banned domain-specific token {banned!r}"


def test_stop_words_and_field_weights_are_plain_english_not_domain_vocabulary():
    """A structural guard, not a string search: every entry in the
    tokenizer's stop-word set must be a short, common English function
    word (no digits, no underscores — the shape of a real business
    identifier), so a future edit can't quietly smuggle a domain synonym
    in as a 'stop word'."""
    import core.sqlservice.lexical_index as lexical_index_module

    for word in lexical_index_module._STOP_WORDS:
        assert word.isascii()
        assert all(c.isalpha() or c == "'" for c in word), word
        assert len(word) <= 8, word


# ── 8. Performance: benchmark over ~a thousand-plus synthetic documents ───────────

def _build_synthetic_documents(num_docs=1400, seed=1234):
    rng = random.Random(seed)
    docs = []
    for i in range(num_docs):
        columns = tuple(f"Column{j}_{rng.choice(['id','name','date','amount','status'])}" for j in range(rng.randint(3, 30)))
        synonyms = tuple(f"term{rng.randint(0, 500)}" for _ in range(rng.randint(0, 15)))
        docs.append(_Doc(
            f"dbo.table_{i:05d}",
            tier=(i % 4) + 1,
            metadata={
                "business_name": f"Business Name {i}",
                "description": f"Contains synthetic data. One row per entity {i}.",
                "synonyms": synonyms,
                "column_names": columns,
            },
        ))
    return docs


def test_warm_hybrid_lexical_ranking_timing_synthetic_1406_documents():
    """
    Timing REPORT, not a fragile benchmark assertion — machine speed varies
    across CI/dev hardware. Index construction (paid once, cached) is
    timed separately from repeated WARM searches, which is the number the
    Step 4 requirement (<100ms additional warm overhead) actually concerns.
    Only a very generous upper bound is asserted, to catch a true
    catastrophic regression (e.g. accidental O(n^2) blowup), never a tight
    target.
    """
    docs = _build_synthetic_documents(num_docs=1400)

    build_start = time.perf_counter()
    index = build_lexical_index(docs, _IDENTITY)
    build_ms = (time.perf_counter() - build_start) * 1000

    query_tokens = tokenize("how many students are enrolled in each program this year")
    search_lexical_index(index, query_tokens, top_k=8)  # warm-up

    runs = 20
    start = time.perf_counter()
    for _ in range(runs):
        results = search_lexical_index(index, query_tokens, top_k=8)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_search_ms = elapsed_ms / runs

    print(
        f"\n[timing] lexical index build over {len(docs)} documents: {build_ms:.2f} ms (one-time, cached per build)\n"
        f"[timing] warm BM25F search: {per_search_ms:.2f} ms/search (average of {runs} runs)"
    )

    assert per_search_ms < 1000  # generous sanity bound only — not a benchmark assertion
    assert isinstance(results, tuple)


def test_additional_warm_overhead_of_hybrid_vs_pure_vector_ranking():
    """
    Direct apples-to-apples timing REPORT: pure cosine search alone vs
    cosine search + BM25F lexical search + reciprocal-rank fusion, both
    warm (index already built/cached), over the same a thousand-plus-document
    synthetic corpus core.sqlservice.vector_index's own benchmark uses.
    Reports the delta the Step 4 requirement (<100ms additional warm
    overhead) actually concerns. Only a generous upper bound is asserted
    on the delta itself — never a tight target (machine-speed varies).
    """
    from core.sqlservice.vector_index import IndexedSchemaDocument, VectorIndex, VectorIndexIdentity, search_vector_index

    docs = _build_synthetic_documents(num_docs=1400)
    rng = random.Random(999)
    vector_docs = []
    for i, doc in enumerate(docs):
        raw = [rng.uniform(-1.0, 1.0) for _ in range(1536)]
        norm = math.sqrt(sum(v * v for v in raw))
        vector_docs.append(IndexedSchemaDocument(
            object_fqn=doc.object_fqn, object_type=doc.object_type, retrieval_tier=doc.retrieval_tier,
            document_hash=doc.document_hash, document_text=doc.document_text, metadata=doc.metadata,
            vector=tuple(v / norm for v in raw),
        ))
    vindex = VectorIndex(
        identity=VectorIndexIdentity(build_id=999998, embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536),
        documents=tuple(vector_docs),
    )
    lindex = build_lexical_index(vector_docs, _IDENTITY)  # warm/cached in real usage — built once here too

    query_vector = [rng.uniform(-1.0, 1.0) for _ in range(1536)]
    query_tokens = tokenize("how many students are enrolled in each program this year")

    # Warm-up both paths.
    search_vector_index(vindex, query_vector, top_k=8)
    search_lexical_index(lindex, query_tokens, top_k=8)

    runs = 20

    start = time.perf_counter()
    for _ in range(runs):
        search_vector_index(vindex, query_vector, top_k=len(vindex.documents))
    vector_only_ms = (time.perf_counter() - start) * 1000 / runs

    start = time.perf_counter()
    for _ in range(runs):
        # Exactly what retrieve_schema_context itself does under hybrid
        # mode: one full-index semantic ranking, one full-index lexical
        # ranking, then reciprocal-rank-fusion arithmetic over every
        # tier-filtered candidate — no redundant extra calls.
        sem_results = search_vector_index(vindex, query_vector, top_k=len(vindex.documents))
        lex_results = search_lexical_index(lindex, query_tokens, top_k=len(lindex.documents))
        lex_rank_by_fqn = {r.object_fqn: i + 1 for i, r in enumerate(lex_results)}
        fused = sorted(
            ((1.0 / (60 + i + 1)) + (1.0 / (60 + lex_rank_by_fqn[r.object_fqn]) if r.object_fqn in lex_rank_by_fqn else 0.0), r.object_fqn)
            for i, r in enumerate(sem_results)
        )
        fused.sort(reverse=True)
    hybrid_ms = (time.perf_counter() - start) * 1000 / runs

    overhead_ms = hybrid_ms - vector_only_ms
    print(
        f"\n[timing] pure-vector warm ranking: {vector_only_ms:.2f} ms/call\n"
        f"[timing] hybrid warm ranking (vector + lexical + fusion): {hybrid_ms:.2f} ms/call\n"
        f"[timing] additional hybrid overhead: {overhead_ms:.2f} ms/call"
    )

    assert overhead_ms < 2000  # generous sanity bound only — not a benchmark assertion
