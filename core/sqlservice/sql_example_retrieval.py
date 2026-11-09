"""
Dynamic few-shot SQL-example retrieval — SQL rebuild Phase 5, Step 9.

Combines core.sqlservice.sql_example_vector_index's cosine similarity with
structural compatibility checks (current source/schema, referenced object/
column existence, allowed tiers, result shape) to pick at most 3
TRUSTED examples for a question, reusing the SAME query vector
core.sqlservice.schema_context_retrieval already computed — this module
never embeds anything itself and never accepts a raw question string.

Scope: pure, in-memory over an already-loaded example vector index plus an
already-loaded map of the CURRENT schema build's documents (for existence/
tier checks) — no SQLite access beyond what load_sql_example_vector_index
itself does, no embedding provider call, no live SQL Server connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.sqlservice.sql_example_vector_index import (
    STATUS_EMPTY,
    STATUS_OK as _INDEX_STATUS_OK,
    STATUS_UNOWNED,
    IndexedSqlExample,
    SqlExampleVectorIndexLoadResult,
    load_sql_example_vector_index,
    search_sql_example_vector_index,
)

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "STATUS_NO_EXAMPLES",
    "DEFAULT_MAX_EXAMPLES",
    "MAX_ALLOWED_EXAMPLES",
    "DEFAULT_MIN_SIMILARITY",
    "RetrievedSqlExample",
    "ExampleRetrievalResult",
    "retrieve_sql_examples",
]

STATUS_OK = "OK"
STATUS_UNOWNED = "UNOWNED"
STATUS_NO_EXAMPLES = "NO_EXAMPLES"  # owned source, but no trusted/active/embedded/compatible example available

# "at most three examples" — a caller-supplied larger max_examples is
# rejected, never silently clamped (same fail-loud convention as
# core.sqlservice.relationship_graph.MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH).
MAX_ALLOWED_EXAMPLES = 3
DEFAULT_MAX_EXAMPLES = 3
DEFAULT_MIN_SIMILARITY = 0.15


@dataclass(frozen=True)
class RetrievedSqlExample:
    """One example that passed both the similarity threshold and every
    compatibility check, plus what it proposes for downstream candidate
    injection (core.sqlservice.schema_context_retrieval's
    `example_proposed_objects` parameter accepts exactly the
    (object_fqn, example_id, reason) shape `proposed_object_fqns` implies)."""

    example: IndexedSqlExample
    similarity: float
    proposed_object_fqns: tuple[str, ...]  # canonical_object_fqns that still exist AND are tier-allowed now
    unavailable_object_fqns: tuple[str, ...]  # canonical_object_fqns that no longer exist or are tier-excluded


@dataclass(frozen=True)
class ExampleRetrievalResult:
    status: str  # STATUS_OK | STATUS_UNOWNED | STATUS_NO_EXAMPLES
    examples: tuple[RetrievedSqlExample, ...]


def _example_is_schema_compatible(
    example: IndexedSqlExample,
    *,
    schema_documents_by_fqn_lower: Mapping[str, object],  # fqn_lower -> object exposing .retrieval_tier
    allowed_tiers: frozenset[int],
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """
    An example is compatible when AT LEAST ONE of its canonical objects
    still exists and is tier-allowed right now — a schema can drift
    (a referenced object renamed/dropped/retiered) without instantly
    invalidating an otherwise-excellent pattern match; a fully-vanished
    example (zero surviving objects) is incompatible outright. Returns
    (is_compatible, proposed_fqns, unavailable_fqns) — `proposed_fqns` uses
    the CURRENT build's own canonical casing (via the schema document's own
    object_fqn), never the possibly-stale casing stored on the example.
    """
    proposed: list[str] = []
    unavailable: list[str] = []
    for fqn in example.canonical_object_fqns:
        doc = schema_documents_by_fqn_lower.get(fqn.lower())
        if doc is not None and doc.retrieval_tier in allowed_tiers:
            proposed.append(doc.object_fqn)
        else:
            unavailable.append(fqn)
    return (bool(proposed), tuple(proposed), tuple(unavailable))


def retrieve_sql_examples(
    query_vector: Sequence[float],
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_documents_by_fqn_lower: Mapping[str, object],
    *,
    allowed_tiers: frozenset[int],
    max_examples: int = DEFAULT_MAX_EXAMPLES,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    expected_result_shape: str | None = None,
) -> ExampleRetrievalResult:
    """
    Retrieve at most `max_examples` (<= MAX_ALLOWED_EXAMPLES) TRUSTED
    examples for this source, reusing `query_vector` — the EXACT vector
    core.sqlservice.schema_context_retrieval.retrieve_schema_context()
    already computed for this question. This function never calls an
    embedding provider itself; there is no question-text parameter here at
    all, structurally preventing a second embedding call for this stage.

    Trust: already fully enforced by core.sqlservice.sql_example_vector_index
    at index-load time (TRUST_LEVEL_TRUSTED, STATUS_ACTIVE only) — this
    function never re-checks it, and never could accidentally loosen it,
    since untrusted/stale examples are simply absent from the index it
    searches.

    Compatibility (requirement: "current source/schema; referenced objects
    still exist; referenced columns still exist where known [reserved —
    canonical_columns is carried on every RetrievedSqlExample.example, but
    this step's compatibility gate is object-level; a future step may
    tighten to column-level once a cheap per-column existence check is
    available]; allowed tiers; trust level; result/intent shape"):
      - schema/object existence + tier: see _example_is_schema_compatible.
      - result/intent shape: when `expected_result_shape` is given, only
        examples with that exact result_shape are considered — None (the
        default, since no caller yet computes an expected shape for a live
        question) means no shape filtering.
      - similarity: cosine score must be >= min_similarity.

    Ranking: cosine similarity descending, example_id ascending tie-break
    — deterministic, identical to core.sqlservice.vector_index's own
    convention. Returns at most `max_examples`.

    Raises:
        ValueError: max_examples outside [1, MAX_ALLOWED_EXAMPLES],
            min_similarity outside [0.0, 1.0], or an invalid query_vector.
    """
    if (
        not isinstance(max_examples, int) or isinstance(max_examples, bool)
        or not (1 <= max_examples <= MAX_ALLOWED_EXAMPLES)
    ):
        raise ValueError(
            f"max_examples must be an integer in [1, {MAX_ALLOWED_EXAMPLES}], got {max_examples!r}"
        )
    if (
        isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float))
        or not (0.0 <= min_similarity <= 1.0)
    ):
        raise ValueError(f"min_similarity must be a number in [0.0, 1.0], got {min_similarity!r}")
    if not allowed_tiers:
        raise ValueError("allowed_tiers must be a non-empty set")

    load_result: SqlExampleVectorIndexLoadResult = load_sql_example_vector_index(
        source_id, user_id, embedding_provider, embedding_model, embedding_dimensions,
    )
    if load_result.status == STATUS_UNOWNED:
        return ExampleRetrievalResult(status=STATUS_UNOWNED, examples=())
    if load_result.status == STATUS_EMPTY or load_result.index is None:
        return ExampleRetrievalResult(status=STATUS_NO_EXAMPLES, examples=())

    # Search a generous top_k first (compatibility/shape filtering happens
    # AFTER similarity ranking, so a lower-ranked-but-compatible example is
    # never missed just because a higher-ranked, incompatible one crowded
    # it out of a too-small initial search window).
    ranked = search_sql_example_vector_index(
        load_result.index, query_vector, top_k=len(load_result.index.examples),
    )

    retrieved: list[RetrievedSqlExample] = []
    for hit in ranked:
        if len(retrieved) >= max_examples:
            break
        if hit.score < min_similarity:
            continue
        if expected_result_shape is not None and hit.example.result_shape != expected_result_shape:
            continue
        is_compatible, proposed, unavailable = _example_is_schema_compatible(
            hit.example, schema_documents_by_fqn_lower=schema_documents_by_fqn_lower, allowed_tiers=allowed_tiers,
        )
        if not is_compatible:
            continue
        retrieved.append(
            RetrievedSqlExample(
                example=hit.example, similarity=hit.score,
                proposed_object_fqns=proposed, unavailable_object_fqns=unavailable,
            )
        )

    if not retrieved:
        return ExampleRetrievalResult(status=STATUS_NO_EXAMPLES, examples=())
    return ExampleRetrievalResult(status=STATUS_OK, examples=tuple(retrieved))
