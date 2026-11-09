"""
Focused tests — Phase 7, Step 3A, Part 2: relationship-complete context
packing (core/sqlservice/schema_context_retrieval.py's connectivity-aware
anchor packing priority).

Reproduces, and then proves fixed, the verified Step 3 live-evaluation
failure: a confirmed connecting object (a low-fused-rank anchor that is
nonetheless a CONFIRMED direct neighbor of a higher-ranked anchor) was
silently dropped by context-budget packing purely because several
higher-ranked but structurally UNCONNECTED anchors consumed the whole
character budget ahead of it — causing the AI generator to invent an
unconfirmed join to some other, merely-present object instead.

Reuses the exact SQLite fixture plumbing, `_doc`/`_make_ready_build_with_docs`/
`_embed_all`/`FakeEmbeddingProvider` helpers already established in
tests/test_sqlservice_schema_context_retrieval.py (cross-imported, per this
codebase's own established convention — see e.g.
tests/test_aggregation_ranking_composition.py importing from
tests/test_phase9_query_planning.py). No database-specific object name appears
anywhere in this file — every fixture is generic.

Run:
    venv/Scripts/pytest tests/test_sqlservice_relationship_complete_packing.py -v
"""

from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_HYBRID_ANCHOR,
    STATUS_OK,
    retrieve_schema_context,
)

from tests.test_sqlservice_schema_context_retrieval import (
    DIMS,
    _doc,
    _embed_all,
    _isolated_vector_index_cache,  # noqa: F401 -- re-declared below (autouse is per-module)
    _make_ready_build_with_docs,
    db_path,  # noqa: F401 -- re-exported fixture
    template_db_path,  # noqa: F401 -- re-exported fixture
)
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.vector_index import clear_vector_index_cache

import pytest


# ── Re-declared autouse fixture (pytest resolves autouse per-module) ──────


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache_local():
    clear_vector_index_cache()
    clear_lexical_index_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()


# ── Fixture construction: one high-relevance "target" anchor with a
#    confirmed relationship to a LOW-relevance "connector" anchor, plus N
#    higher-ranked-but-unconnected "filler" anchors sized to exactly
#    exhaust the character budget before packing would ever reach the
#    connector under the OLD (pure fused-rank) ordering ───────────────────

_TARGET_TEXT = "OBJECT: dbo.target\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
_CONNECTOR_TEXT = "OBJECT: dbo.connector\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]\n  target_id INTEGER"
_N_FILLERS = 8


def _filler_text(i: int) -> str:
    return f"OBJECT: dbo.filler_{i}\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("x" * 150)


def _build_fixture(db_path):
    docs = [
        _doc(
            "dbo.target", text=_TARGET_TEXT,
            metadata={"column_names": ("id",), "confirmed_relationship_targets": ("dbo.connector.target_id",)},
        ),
        _doc(
            "dbo.connector", text=_CONNECTOR_TEXT,
            metadata={
                "column_names": ("id", "target_id"),
                "confirmed_relationship_sources": ("dbo.target.id",),
                "business_name": "Connector", "primary_key_columns": ("id",),
            },
        ),
    ] + [
        _doc(f"dbo.filler_{i}", text=_filler_text(i), metadata={"column_names": ("id",)})
        for i in range(1, _N_FILLERS + 1)
    ]
    build = _make_ready_build_with_docs(db_path, docs)

    vectors = {"dbo.target": [1.0, 0.0, 0.0, 0.0], "dbo.connector": [0.10, 0.90, 0.0, 0.0]}
    for i in range(1, _N_FILLERS + 1):
        # Strictly decreasing similarity to the query vector (1,0,0,0), all
        # still well above the connector's — deterministic rank order:
        # target > filler_1 > filler_2 > ... > filler_8 > connector.
        vectors[f"dbo.filler_{i}"] = [0.90 - i * 0.05, 0.10, 0.0, 0.0]
    _embed_all(db_path, build, vectors, docs=docs)
    return build, docs


def _exact_budget_for_target_plus_all_fillers() -> int:
    """The budget that fits dbo.target + all _N_FILLERS fillers' FULL text
    exactly, with zero characters left for anything else — reproduces the
    real "budget exhausted by higher-ranked-but-unconnected candidates"
    failure precisely, rather than approximating it."""
    sep_cost = 2  # "\n\n"
    total = len(_TARGET_TEXT)
    for i in range(1, _N_FILLERS + 1):
        total += sep_cost + len(_filler_text(i))
    return total


class _FakeProvider:
    """Deterministic fixed-vector embedding provider for the question
    itself — mirrors FakeEmbeddingProvider's own default query vector
    (1.0, 0.0, 0.0, 0.0) exactly."""

    def __init__(self):
        from core.sqlservice.embedding_providers import ProviderEmbeddingBatchResponse, ProviderEmbeddingItem
        self._response_cls = ProviderEmbeddingBatchResponse
        self._item_cls = ProviderEmbeddingItem

    provider_id = "fake"
    model = "fake-model"
    dimensions = DIMS

    def embed_batch(self, texts):
        return self._response_cls(
            items=(self._item_cls(index=0, vector=(1.0, 0.0, 0.0, 0.0)),), model=self.model,
        )


# ── 1. Connector preserved under a tight context budget ──────────────────


def test_connector_preserved_under_tight_context_budget(db_path):
    build, docs = _build_fixture(db_path)
    budget = _exact_budget_for_target_plus_all_fillers()

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=_N_FILLERS + 2, candidate_pool_size=_N_FILLERS + 2,
        max_context_characters=budget, enable_canonical_prior=False,
    )

    assert result.status == STATUS_OK
    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.target" in fqns
    # The confirmed connector survives even though its raw relevance rank
    # placed it dead last among the anchors, and the budget was sized to
    # exactly exhaust on target + every filler alone.
    assert "dbo.connector" in fqns, (
        "confirmed connector was dropped -- connectivity-aware packing priority regressed"
    )
    connector_doc = next(d for d in result.documents if d.object_fqn == "dbo.connector")
    assert connector_doc.selection_reason == SELECTION_REASON_HYBRID_ANCHOR
    # At least one filler was displaced to make room -- proof this isn't
    # simply "the budget happened to be big enough for everything".
    assert not all(f"dbo.filler_{i}" in fqns for i in range(1, _N_FILLERS + 1))


def test_without_a_confirmed_relationship_the_same_low_rank_object_is_dropped(db_path):
    """Negative control: an anchor with NO confirmed relationship to
    anything else, ranked exactly where dbo.connector was, is legitimately
    dropped under the same tight budget -- proving the fix promotes
    CONFIRMED CONNECTORS specifically, never merely "the last-ranked
    anchor" indiscriminately."""
    docs = [
        _doc("dbo.target", text=_TARGET_TEXT, metadata={"column_names": ("id",)}),
        _doc("dbo.isolated_low_rank", text=_CONNECTOR_TEXT, metadata={"column_names": ("id", "target_id")}),
    ] + [
        _doc(f"dbo.filler_{i}", text=_filler_text(i), metadata={"column_names": ("id",)})
        for i in range(1, _N_FILLERS + 1)
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    vectors = {"dbo.target": [1.0, 0.0, 0.0, 0.0], "dbo.isolated_low_rank": [0.10, 0.90, 0.0, 0.0]}
    for i in range(1, _N_FILLERS + 1):
        vectors[f"dbo.filler_{i}"] = [0.90 - i * 0.05, 0.10, 0.0, 0.0]
    _embed_all(db_path, build, vectors, docs=docs)
    budget = _exact_budget_for_target_plus_all_fillers()

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=_N_FILLERS + 2, candidate_pool_size=_N_FILLERS + 2,
        max_context_characters=budget, enable_canonical_prior=False,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.isolated_low_rank" not in fqns


# ── 2. Compact connector fallback ─────────────────────────────────────────


def test_compact_connector_fallback(db_path):
    """The connector's FULL text is too large to fit even after being
    promoted, but its COMPACT structured projection does -- proves the
    connectivity promotion composes correctly with the existing
    full-text-then-compact-fallback packing rule, never bypassing it."""
    huge_connector_text = (
        "OBJECT: dbo.connector\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("y" * 3000)
    )
    docs = [
        _doc(
            "dbo.target", text=_TARGET_TEXT,
            metadata={"column_names": ("id",), "confirmed_relationship_targets": ("dbo.connector.target_id",)},
        ),
        _doc(
            "dbo.connector", text=huge_connector_text,
            metadata={
                "column_names": ("id", "target_id"),
                "confirmed_relationship_sources": ("dbo.target.id",),
                "primary_key_columns": ("id",),
            },
        ),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.target": [1.0, 0.0, 0.0, 0.0], "dbo.connector": [0.5, 0.5, 0.0, 0.0]}, docs=docs)

    budget = len(_TARGET_TEXT) + 400  # enough for target + a compact projection, never the 3000-char huge text
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=2, candidate_pool_size=2, max_context_characters=budget, enable_canonical_prior=False,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.connector" in fqns
    connector_doc = next(d for d in result.documents if d.object_fqn == "dbo.connector")
    assert connector_doc.used_compact_text is True
    assert result.essential_context_incomplete is False


# ── 3. Impossible-to-fit connector produces an explicit incomplete signal ─


def test_impossible_to_fit_connector_produces_incomplete_warning(db_path):
    build, docs = _build_fixture(db_path)
    # Budget fits dbo.target alone and nothing else, in any form.
    budget = len(_TARGET_TEXT)

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=_N_FILLERS + 2, candidate_pool_size=_N_FILLERS + 2,
        max_context_characters=budget, enable_canonical_prior=False,
    )

    assert result.status == STATUS_OK
    fqns = {d.object_fqn for d in result.documents}
    assert fqns == {"dbo.target"}
    assert result.essential_context_incomplete is True
    assert any(w.startswith("INCOMPLETE_CONTEXT:") for w in result.warnings)
    assert any("dbo.connector" in w for w in result.warnings if w.startswith("INCOMPLETE_CONTEXT:"))
    # Never silently presented as sufficient -- truncated is also still True.
    assert result.truncated is True


# ── 4. Deterministic packing and ordering ─────────────────────────────────


def test_deterministic_packing_and_ordering(db_path):
    build, docs = _build_fixture(db_path)
    budget = _exact_budget_for_target_plus_all_fillers()

    result_a = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=_N_FILLERS + 2, candidate_pool_size=_N_FILLERS + 2,
        max_context_characters=budget, enable_canonical_prior=False,
    )
    result_b = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=_N_FILLERS + 2, candidate_pool_size=_N_FILLERS + 2,
        max_context_characters=budget, enable_canonical_prior=False,
    )

    assert [d.object_fqn for d in result_a.documents] == [d.object_fqn for d in result_b.documents]
    assert result_a.context_text == result_b.context_text


# ── 5. No Tier 3/4/archive leakage introduced by the reordering ──────────


def test_no_tier_3_4_leakage_from_connectivity_reordering(db_path):
    """A Tier-4 object confirmed-related to an anchor must still never
    appear, regardless of the new connectivity-aware promotion -- tier
    filtering happens upstream (effective_tiers), before this fix's own
    graph-connectivity logic ever runs, and is completely unaffected by it."""
    docs = [
        _doc(
            "dbo.target", text=_TARGET_TEXT,
            metadata={"column_names": ("id",), "confirmed_relationship_targets": ("dbo.archive_old.target_id",)},
        ),
        _doc(
            "dbo.archive_old", tier=4, text="OBJECT: dbo.archive_old\nTYPE: TABLE\nTIER: 4\nCOLUMNS:\n  id INTEGER",
            metadata={"column_names": ("id", "target_id"), "confirmed_relationship_sources": ("dbo.target.id",)},
        ),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.target": [1.0, 0.0, 0.0, 0.0], "dbo.archive_old": [0.9, 0.1, 0.0, 0.0]}, docs=docs)

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", _FakeProvider(),
        top_k=2, candidate_pool_size=2, max_context_characters=8000, enable_canonical_prior=False,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert fqns == {"dbo.target"}
    assert "dbo.archive_old" not in fqns
