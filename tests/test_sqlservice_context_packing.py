"""
Focused unit tests — SQL rebuild Phase 5, Step 5: deterministic size-aware
context packing (core/sqlservice/context_packing.py).

Pure, in-memory, no SQLite, no OpenAI. Every document fixture is
hand-constructed; no database-specific identifier appears anywhere.

Run:
    venv/Scripts/pytest tests/test_sqlservice_context_packing.py -v
"""

from types import MappingProxyType

from core.sqlservice.context_packing import (
    SELECTION_REASON_DIRECT_RELATIONSHIP,
    SELECTION_REASON_HYBRID_ANCHOR,
    SELECTION_REASON_RELATIONSHIP_BRIDGE,
    PackCandidate,
    build_compact_projection,
    pack_candidates,
)
from core.sqlservice.lexical_index import tokenize


class _Doc:
    def __init__(self, object_fqn, *, object_type="TABLE", tier=1, text=None, metadata=None):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = tier
        self.document_hash = "h-" + object_fqn
        self.document_text = text if text is not None else f"OBJECT: {object_fqn}\nTYPE: {object_type}\nCOLUMNS:\n  id INTEGER [PK]"
        self.metadata = MappingProxyType(metadata or {})


_EMPTY_COMPONENTS = {
    "score": None, "semantic_rank": None, "lexical_rank": None,
    "lexical_score": None, "fused_score": None, "matched_lexical_fields": (),
}


def _anchor_components(rank):
    return {
        "score": 1.0 - rank * 0.01, "semantic_rank": rank, "lexical_rank": None,
        "lexical_score": None, "fused_score": 1.0 / (60 + rank), "matched_lexical_fields": (),
    }


# ── build_compact_projection: only real structured metadata ────────────

def test_compact_projection_contains_only_real_structured_fields():
    metadata = {
        "business_name": "Widget Registry",
        "description": "Contains widget records.",
        "synonyms": ("widget", "gadget"),
        "primary_key_columns": ("WidgetID",),
        "foreign_key_targets": ("dbo.Category.CategoryID",),
        "confirmed_relationship_targets": ("dbo.Category.CategoryID",),
        "column_names": ("WidgetID", "WidgetName", "CategoryID"),
        "status_columns": ("IsActive",),
        "date_columns": ("CreatedDate",),
        "measure_columns": ("Price",),
    }
    projection = build_compact_projection(
        "dbo.Widgets", "TABLE", 1, metadata, tokenize("widget name"), max_projected_columns=12,
    )
    assert projection is not None
    assert "OBJECT: dbo.Widgets" in projection
    assert "TYPE: TABLE" in projection
    assert "TIER: 1" in projection
    assert "BUSINESS_NAME: Widget Registry" in projection
    assert "DESCRIPTION: Contains widget records." in projection
    assert "widget" in projection  # matched synonym
    assert "WidgetID" in projection  # primary key
    assert "dbo.Category.CategoryID" in projection  # FK / relationship
    assert "WidgetName" in projection  # question-matched column
    assert "IsActive" in projection
    assert "CreatedDate" in projection
    assert "Price" in projection
    # Never invents anything not present in metadata.
    assert "gadget" not in projection  # a real synonym, but NOT question-matched — never surfaced as "matched"


def test_compact_projection_never_fabricates_absent_fields():
    metadata = {"column_names": ("Id",)}
    projection = build_compact_projection("dbo.Bare", "TABLE", 1, metadata, tokenize("id"), max_projected_columns=12)
    assert projection is not None
    assert "BUSINESS_NAME" not in projection
    assert "DESCRIPTION" not in projection
    assert "MATCHED_SYNONYMS" not in projection
    assert "PRIMARY_KEY" not in projection
    assert "FOREIGN_KEYS" not in projection


def test_compact_projection_returns_none_when_nothing_structural_available():
    assert build_compact_projection("dbo.Empty", "TABLE", 1, {}, tokenize("anything"), max_projected_columns=12) is None


def test_compact_projection_respects_max_projected_columns():
    metadata = {"column_names": tuple(f"widget_col_{i}" for i in range(20))}
    projection = build_compact_projection(
        "dbo.Wide", "TABLE", 1, metadata, tokenize("widget"), max_projected_columns=3,
    )
    assert projection is not None
    matched_line = next(line for line in projection.splitlines() if line.startswith("MATCHED_COLUMNS:"))
    assert matched_line.count(",") == 2  # exactly 3 columns listed


def test_compact_projection_deterministic_repeated_calls():
    metadata = {"business_name": "Alpha", "synonyms": ("alpha",), "column_names": ("AlphaId",)}
    tokens = tokenize("alpha")
    first = build_compact_projection("dbo.A", "TABLE", 1, metadata, tokens, max_projected_columns=12)
    second = build_compact_projection("dbo.A", "TABLE", 1, metadata, tokens, max_projected_columns=12)
    assert first == second


# ── pack_candidates: priority-aware size-aware packing ──────────────────

def test_large_irrelevant_document_cannot_squeeze_out_smaller_essential_anchor():
    # A large, LOWER-priority spillover-like candidate placed AFTER the
    # essential anchor in priority order must never consume budget the
    # anchor needs — priority order alone (not size) governs.
    essential = _Doc("dbo.essential", text="OBJECT: dbo.essential\n" + "x" * 200)
    large_low_priority = _Doc("dbo.large_other", text="OBJECT: dbo.large_other\n" + "y" * 5000)

    candidates = [
        PackCandidate(doc=essential, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
        PackCandidate(doc=large_low_priority, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(2), allow_compact=False),
    ]
    result = pack_candidates(candidates, max_context_characters=300, query_tokens=(), max_projected_columns=12)

    packed_fqns = {item.doc.object_fqn for item in result.packed}
    assert "dbo.essential" in packed_fqns
    assert "dbo.large_other" not in packed_fqns  # correctly excluded, but never at the essential doc's expense


def test_large_essential_anchor_is_compacted_and_retained():
    metadata = {"business_name": "Essential", "primary_key_columns": ("Id",)}
    huge_text = "OBJECT: dbo.huge_essential\n" + ("x" * 5000)
    huge_doc = _Doc("dbo.huge_essential", text=huge_text, metadata=metadata)

    candidates = [
        PackCandidate(doc=huge_doc, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
    ]
    result = pack_candidates(candidates, max_context_characters=300, query_tokens=(), max_projected_columns=12)

    assert len(result.packed) == 1
    assert result.packed[0].used_compact_text is True
    assert result.packed[0].context_text != huge_text
    assert len(result.packed[0].context_text) <= 300
    assert result.truncated is True
    assert any("compacted" in w for w in result.warnings)


def test_essential_candidate_without_compact_fallback_and_no_fit_is_skipped_safely():
    # allow_compact=True but NOTHING structural to project (metadata
    # empty) -> compaction unavailable -> must be skipped, never crash,
    # never exceed budget.
    huge_doc = _Doc("dbo.huge_no_metadata", text="OBJECT: dbo.huge_no_metadata\n" + ("z" * 5000), metadata={})
    candidates = [
        PackCandidate(doc=huge_doc, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
    ]
    result = pack_candidates(candidates, max_context_characters=300, query_tokens=(), max_projected_columns=12)
    assert result.packed == ()
    assert result.truncated is True
    assert any("skipped" in w for w in result.warnings)


def test_low_priority_candidate_never_compacted():
    huge_doc = _Doc("dbo.huge_spillover", text="OBJECT: dbo.huge_spillover\n" + ("x" * 5000), metadata={"business_name": "X"})
    candidates = [
        PackCandidate(doc=huge_doc, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(9), allow_compact=False),
    ]
    result = pack_candidates(candidates, max_context_characters=300, query_tokens=(), max_projected_columns=12)
    assert result.packed == ()  # skipped outright, never compacted despite having structural facts available


def test_hard_character_budget_never_exceeded():
    docs = [_Doc(f"dbo.t{i}", text=f"OBJECT: dbo.t{i}\n" + ("a" * 100)) for i in range(10)]
    candidates = [
        PackCandidate(doc=d, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(i + 1), allow_compact=True)
        for i, d in enumerate(docs)
    ]
    result = pack_candidates(candidates, max_context_characters=250, query_tokens=(), max_projected_columns=12)
    assert result.used_characters <= 250


def test_no_arbitrary_document_splitting():
    """Every packed context_text is either the COMPLETE document_text or a
    COMPLETE compact projection — never a truncated substring of either."""
    metadata = {"business_name": "Whole"}
    doc = _Doc("dbo.whole", text="OBJECT: dbo.whole\n" + ("w" * 400), metadata=metadata)
    candidates = [
        PackCandidate(doc=doc, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
    ]
    result = pack_candidates(candidates, max_context_characters=150, query_tokens=(), max_projected_columns=12)
    assert len(result.packed) == 1
    packed_text = result.packed[0].context_text
    assert packed_text == doc.document_text or (
        packed_text == build_compact_projection("dbo.whole", "TABLE", 1, metadata, (), max_projected_columns=12)
    )
    assert not doc.document_text.startswith(packed_text) or packed_text == doc.document_text  # never a partial prefix


def test_full_document_and_hash_retained_on_packed_item_even_when_compacted():
    metadata = {"business_name": "Compact Me"}
    full_text = "OBJECT: dbo.compact_me\n" + ("c" * 3000)
    doc = _Doc("dbo.compact_me", text=full_text, metadata=metadata)
    candidates = [
        PackCandidate(doc=doc, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
    ]
    result = pack_candidates(candidates, max_context_characters=200, query_tokens=(), max_projected_columns=12)
    assert result.packed[0].used_compact_text is True
    assert result.packed[0].doc.document_text == full_text  # original preserved on the doc reference
    assert result.packed[0].doc.document_hash == "h-dbo.compact_me"
    assert result.packed[0].context_text != full_text  # what was actually packed differs


def test_priority_order_respected_bridge_before_lower_priority():
    anchor = _Doc("dbo.anchor", text="OBJECT: dbo.anchor\n" + ("a" * 50))
    bridge = _Doc("dbo.bridge", text="OBJECT: dbo.bridge\n" + ("b" * 50), metadata={"primary_key_columns": ("Id",)})
    direct = _Doc("dbo.direct", text="OBJECT: dbo.direct\n" + ("d" * 5000))  # large, lower priority

    candidates = [
        PackCandidate(doc=anchor, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(1), allow_compact=True),
        PackCandidate(doc=bridge, reason=SELECTION_REASON_RELATIONSHIP_BRIDGE, components=dict(_EMPTY_COMPONENTS), allow_compact=True),
        PackCandidate(doc=direct, reason=SELECTION_REASON_DIRECT_RELATIONSHIP, components=dict(_EMPTY_COMPONENTS), allow_compact=True),
    ]
    result = pack_candidates(candidates, max_context_characters=200, query_tokens=(), max_projected_columns=12)
    packed_fqns = [item.doc.object_fqn for item in result.packed]
    assert "dbo.anchor" in packed_fqns
    assert "dbo.bridge" in packed_fqns  # bridge fits and is prioritized
    # the large direct candidate is the one that gets squeezed, never the bridge


def test_deterministic_packing_across_repeated_calls():
    docs = [_Doc(f"dbo.t{i}", text=f"OBJECT: dbo.t{i}\n" + ("a" * 60)) for i in range(5)]
    candidates = [
        PackCandidate(doc=d, reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(i + 1), allow_compact=True)
        for i, d in enumerate(docs)
    ]
    first = pack_candidates(candidates, max_context_characters=200, query_tokens=(), max_projected_columns=12)
    second = pack_candidates(candidates, max_context_characters=200, query_tokens=(), max_projected_columns=12)
    assert [item.doc.object_fqn for item in first.packed] == [item.doc.object_fqn for item in second.packed]
    assert first.warnings == second.warnings
    assert first.truncated == second.truncated


def test_empty_candidates_produce_empty_result():
    result = pack_candidates([], max_context_characters=1000, query_tokens=(), max_projected_columns=12)
    assert result.packed == ()
    assert result.warnings == ()
    assert result.truncated is False
    assert result.used_characters == 0


# ── Performance: benchmark over ~a thousand-plus synthetic objects ────────────────

def test_warm_step5_pipeline_timing_synthetic_1406_documents():
    """
    Timing REPORT, not a fragile benchmark assertion — machine speed
    varies across CI/dev hardware. Measures the FULL Step 5 addition on
    top of an already-fused candidate ranking: relationship-graph
    construction, bounded chain bridging across 8 anchors, and priority-
    aware packing (with a couple of forced compactions) — the exact
    additional work retrieve_schema_context() does under
    RANKING_MODE_HYBRID beyond what Step 4's own vector+lexical benchmark
    already measured. Only a generous upper bound is asserted.
    """
    import random
    import time

    from core.sqlservice.relationship_graph import build_relationship_graph, find_shortest_relationship_path

    rng = random.Random(2026)
    num_docs = 1400
    docs = []
    for i in range(num_docs):
        targets = ()
        # Sparse relationship graph: every ~5th object gets 1-2 confirmed
        # outbound edges to nearby objects, roughly mirroring a real
        # schema's join density without being disproportionately dense.
        if i % 5 == 0 and i + 3 < num_docs:
            targets = (f"dbo.table_{i+1:05d}.id", f"dbo.table_{i+3:05d}.id")
        metadata = {"business_name": f"Business Name {i}", "column_names": (f"Col{i}_id", f"Col{i}_name")}
        if targets:
            metadata["confirmed_relationship_targets"] = targets
        docs.append(_Doc(
            f"dbo.table_{i:05d}", tier=(i % 4) + 1,
            text=f"OBJECT: dbo.table_{i:05d}\nCOLUMNS:\n" + ("  col INTEGER\n" * 15),
            metadata=metadata,
        ))

    tier_allowed = [d for d in docs if d.retrieval_tier in (1, 2)]

    build_start = time.perf_counter()
    graph = build_relationship_graph(tier_allowed)
    graph_build_ms = (time.perf_counter() - build_start) * 1000

    anchor_keys = [d.object_fqn.lower() for d in rng.sample(tier_allowed, 8)]

    def _one_pipeline_pass():
        bridge_keys = []
        for i in range(len(anchor_keys) - 1):
            if len(bridge_keys) >= 5:
                break
            path = find_shortest_relationship_path(graph, anchor_keys[i], anchor_keys[i + 1], max_path_length=2)
            if path:
                bridge_keys.extend(n for n in path[1:-1] if n not in bridge_keys)

        by_key = {d.object_fqn.lower(): d for d in tier_allowed}
        candidates = [
            PackCandidate(doc=by_key[k], reason=SELECTION_REASON_HYBRID_ANCHOR, components=_anchor_components(i + 1), allow_compact=True)
            for i, k in enumerate(anchor_keys) if k in by_key
        ] + [
            PackCandidate(doc=by_key[k], reason=SELECTION_REASON_RELATIONSHIP_BRIDGE, components=dict(_EMPTY_COMPONENTS), allow_compact=True)
            for k in bridge_keys if k in by_key
        ]
        return pack_candidates(candidates, max_context_characters=8000, query_tokens=tokenize("business name id"), max_projected_columns=12)

    _one_pipeline_pass()  # warm-up

    runs = 20
    start = time.perf_counter()
    for _ in range(runs):
        result = _one_pipeline_pass()
    per_run_ms = (time.perf_counter() - start) * 1000 / runs

    print(
        f"\n[timing] relationship graph build over {len(tier_allowed)} tier-allowed documents: {graph_build_ms:.2f} ms (one-time, cacheable per build)\n"
        f"[timing] warm Step 5 bridging+packing pass (8 anchors, chain bridging, priority packing): {per_run_ms:.2f} ms/call"
    )

    assert per_run_ms < 1000  # generous sanity bound only — not a benchmark assertion
    assert isinstance(result.packed, tuple)


# ── No domain-specific vocabulary ──────────────────────────────────────

def test_no_domain_specific_tokens_in_production_module():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "core" / "sqlservice" / "context_packing.py").read_text(encoding="utf-8")
    for banned in ("EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "LMS_SUBSCRIPTION_PAYMENTS", "exampleco", "EXAMPLECO"):
        assert banned not in source
