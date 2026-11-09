"""
Deterministic structured lexical index for schema retrieval — SQL rebuild
Phase 5, Step 4.

Companion signal to core.sqlservice.vector_index's cosine-similarity search:
where the vector index answers "what does this document mean", this module
answers "which literal words does this document actually contain, and in
which structured field". core.sqlservice.schema_context_retrieval fuses the
two via reciprocal-rank fusion (see that module) rather than trusting either
signal alone — Phase 5 Step 3's forensic audit found the vector-only design
let short, undocumented, narrowly-scoped tables systematically outrank wide,
well-documented canonical business tables whose name/synonyms/columns
directly match the question.

Scope: pure, in-memory, no I/O of any kind. Reads only the STRUCTURED
metadata fields core.sqlservice.retrieval_documents.build_retrieval_documents
already attaches to every RetrievalDocument/IndexedSchemaDocument
(object_fqn, retrieval_tier, and metadata keys "business_name",
"description", "synonyms", "column_names", "confirmed_relationship_targets",
"confirmed_relationship_sources") — it never parses document_text prose.
Every one of those keys is optional/absent-tolerant: an older persisted
build that predates this step's metadata additions degrades gracefully to
whatever structured signal it actually has (typically just object/schema
name), never raises, and never falls back to prose parsing.

Tokenization, stop-words, and light singularization below are entirely
generic English-language rules — no specific customer schema's table
names or business vocabulary appear anywhere in this file (see
tests/test_sqlservice_lexical_index.py::test_no_domain_specific_tokens_in_production_module
for an enforced guard).
"""

from __future__ import annotations

import math
import re
from collections import OrderedDict, Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "FIELD_OBJECT_NAME",
    "FIELD_SCHEMA_NAME",
    "FIELD_BUSINESS_NAME",
    "FIELD_SYNONYMS",
    "FIELD_DESCRIPTION",
    "FIELD_COLUMN_NAMES",
    "FIELD_RELATIONSHIPS",
    "MAX_RELATIONSHIP_LEXICAL_NAMES",
    "FIELD_WEIGHTS",
    "FIELD_LENGTH_NORM_B",
    "BM25_K1",
    "tokenize",
    "LexicalDocument",
    "LexicalIndexIdentity",
    "LexicalIndex",
    "LexicalSearchResult",
    "build_lexical_index",
    "search_lexical_index",
    "load_lexical_index",
    "clear_lexical_index_cache",
    "lexical_index_cache_size",
]

# ── Tokenization (generic — snake_case, CamelCase, punctuation, plurals) ──

# Generic English business-question stop words only — never a domain term.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "for", "to", "from", "with", "and", "or", "but",
    "how", "what", "which", "who", "whom", "whose", "why", "when", "where",
    "show", "list", "get", "find", "give", "tell", "display", "provide",
    "many", "much", "do", "does", "did", "please", "me", "my", "i", "we",
    "us", "our", "you", "your", "it", "its", "this", "that", "these",
    "those", "by", "as", "if", "than", "then", "there", "here", "also",
    "each", "every", "all", "any", "some", "per", "there's", "can",
})

_SPLIT_PATTERN = re.compile(
    r"[_\-\s./\\,;:!?()\[\]{}'\"]+"     # explicit separators/punctuation
    r"|(?<=[a-z0-9])(?=[A-Z])"          # lower/digit -> upper (camelCase)
    r"|(?<=[A-Za-z])(?=[0-9])"          # letter -> digit
    r"|(?<=[0-9])(?=[A-Za-z])",         # digit -> letter
)


def _singularize(token: str) -> str:
    """Light, generic English pluralization stripping — never a domain rule.
    Deliberately conservative: only well-known, low-risk suffix patterns.
    Explicitly excludes common "-us"/"-ss"/"-is" false positives (status,
    campus, bonus, class, basis, ...) that are not plurals despite ending
    in a bare "s" — a well-known generic stemming pitfall, not a
    domain-specific carve-out."""
    if len(token) <= 3:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "xes", "ses", "zes")):
        return token[:-2]
    if token.endswith(("us", "ss", "is")):
        return token
    if token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: object) -> tuple[str, ...]:
    """
    Split `text` into lowercase, singularized, stop-word-filtered tokens.
    Used identically for both document-field text and question text, so a
    document and a question always tokenize into the same vocabulary.
    Deterministic: identical input always produces identical output, in the
    original left-to-right order (never sorted, never deduplicated here —
    a caller wanting a term-frequency count uses Counter(tokenize(...))).
    """
    if not text:
        return ()
    raw_parts = [p for p in _SPLIT_PATTERN.split(str(text)) if p]
    tokens = []
    for part in raw_parts:
        lowered = part.lower()
        if not lowered or not any(c.isalnum() for c in lowered):
            continue
        singular = _singularize(lowered)
        if singular in _STOP_WORDS or lowered in _STOP_WORDS:
            continue
        tokens.append(singular)
    return tuple(tokens)


# ── Structured field extraction (metadata only — never document_text) ────

FIELD_OBJECT_NAME = "OBJECT_NAME"
FIELD_SCHEMA_NAME = "SCHEMA_NAME"
FIELD_BUSINESS_NAME = "BUSINESS_NAME"
FIELD_SYNONYMS = "SYNONYMS"
FIELD_DESCRIPTION = "DESCRIPTION"
FIELD_COLUMN_NAMES = "COLUMN_NAMES"
FIELD_RELATIONSHIPS = "RELATIONSHIPS"

# Field weight: how strongly a match in this field counts (requirement 5 —
# exact object/business-name/synonym matches are strongest; column matches
# help; relationships help a little). Field length-norm `b` (standard Okapi
# BM25 parameter, 0..1): how much a field LONGER than the corpus average for
# that field is penalized. COLUMN_NAMES gets a low `b` specifically so a
# wide table with many columns is not punished merely for having many of
# them (requirement 5) — its per-field score is normalized against the
# corpus's own average column-field length, and only mildly at that.
FIELD_WEIGHTS: Mapping[str, float] = {
    FIELD_OBJECT_NAME: 5.0,
    FIELD_BUSINESS_NAME: 5.0,
    FIELD_SYNONYMS: 4.0,
    FIELD_DESCRIPTION: 2.0,
    FIELD_COLUMN_NAMES: 1.5,
    FIELD_RELATIONSHIPS: 1.0,
    FIELD_SCHEMA_NAME: 0.5,
}

FIELD_LENGTH_NORM_B: Mapping[str, float] = {
    FIELD_OBJECT_NAME: 0.75,
    FIELD_BUSINESS_NAME: 0.75,
    # Low b: an auto-generated synonym list scales with how rich/complex a
    # table's real vocabulary coverage is, not with how narrow/undocumented
    # it is — a well-covered business table can legitimately carry dozens
    # of synonym terms. Standard-b normalization would otherwise punish
    # exactly the richly-covered tables this field exists to help surface,
    # the same "wide field penalized" failure mode requirement 5 calls out
    # for COLUMN_NAMES — confirmed empirically against real generated
    # vocabulary before this value was chosen (see Step 4's own report).
    FIELD_SYNONYMS: 0.3,
    FIELD_DESCRIPTION: 0.75,
    FIELD_COLUMN_NAMES: 0.3,   # low b: long column lists aren't punished merely for length
    FIELD_RELATIONSHIPS: 0.75,
    FIELD_SCHEMA_NAME: 0.75,
}

BM25_K1 = 1.5  # standard Okapi BM25 term-frequency saturation constant

_ALL_FIELDS: tuple[str, ...] = tuple(FIELD_WEIGHTS.keys())


# SQL rebuild Phase 5, Step 7, Part 1, requirement 5 — hub-table bloat
# protection for the lexical relationship field. A table referenced by
# hundreds of confirmed relationships (typically catalog view dependencies)
# previously contributed a proportionally huge token count to
# FIELD_RELATIONSHIPS, which not only self-penalizes that one document
# under BM25F's length normalization but also skews the WHOLE corpus's
# average_field_length for this field (see build_lexical_index), inflating
# every OTHER document's weighted term frequency in turn. Bounding the
# number of relationship NAMES contributed here (same generic priority —
# physical FK first, then catalog view dependency, then any other confirmed
# provenance, stable FQN order — core.sqlservice.retrieval_documents
# applies to its own bounded document-text sections) fixes both directions
# of harm without ever excluding a table-specific case.
MAX_RELATIONSHIP_LEXICAL_NAMES = 15

_RELATIONSHIP_KIND_PRIORITY: Mapping[str, int] = {
    "PHYSICAL_FK": 0,
    "CATALOG_VIEW_DEPENDENCY": 1,
    "OTHER_CONFIRMED": 2,
}


def _bare_name(descriptor: str) -> str | None:
    object_fqn, sep, _column = descriptor.rpartition(".")
    if not sep or not object_fqn:
        return None
    _schema, sep2, bare = object_fqn.rpartition(".")
    return bare if sep2 else object_fqn


def _relationship_bare_names(metadata: Mapping[str, object]) -> tuple[str, ...]:
    """
    Bounded, deterministically prioritized list of the OTHER side's bare
    object names for every confirmed relationship — never document_text,
    never unbounded (see MAX_RELATIONSHIP_LEXICAL_NAMES's own docstring).

    Prefers the kind-tagged "confirmed_relationship_edges" metadata key
    (core.sqlservice.retrieval_documents, Step 7) when present, so a hub
    table's physical FKs are never displaced out of the bounded lexical
    signal merely because many catalog-view-dependency edges sort earlier
    alphabetically. A document from an older build that predates this key
    degrades gracefully to the flat confirmed_relationship_targets/sources
    keys, still bounded to the same cap (just without provenance
    prioritization — nothing in an older build carries provenance kind).
    """
    edges = metadata.get("confirmed_relationship_edges")
    if edges:
        ordered = sorted(
            edges,
            key=lambda e: (
                _RELATIONSHIP_KIND_PRIORITY.get(e.get("kind"), 99),
                _bare_name(str(e.get("other") or "")) or "",
                str(e.get("column") or ""),
            ),
        )
        names: list[str] = []
        for edge in ordered:
            bare = _bare_name(str(edge.get("other") or ""))
            if bare:
                names.append(bare)
            if len(names) >= MAX_RELATIONSHIP_LEXICAL_NAMES:
                break
        return tuple(names)

    names = []
    for key in ("confirmed_relationship_targets", "confirmed_relationship_sources"):
        for descriptor in metadata.get(key) or ():
            bare = _bare_name(str(descriptor))
            if bare:
                names.append(bare)
    return tuple(sorted(set(names))[:MAX_RELATIONSHIP_LEXICAL_NAMES])


def _extract_fields(object_fqn: str, metadata: Mapping[str, object]) -> Mapping[str, tuple[str, ...]]:
    """
    Every field's raw (pre-tokenize) text/word-list, read ONLY from
    object_fqn and the document's own structured metadata — never from
    document_text. Every metadata key is read with .get(...) and a safe
    default, so a document from an older build that predates one or more of
    these keys degrades to whatever it has (commonly just object/schema
    name) rather than raising or silently mis-scoring.
    """
    schema_name, _sep, object_name = object_fqn.rpartition(".")
    business_name = metadata.get("business_name") or ""
    description = metadata.get("description") or ""
    synonyms = metadata.get("synonyms") or ()
    column_names = metadata.get("column_names") or ()
    relationships = _relationship_bare_names(metadata)

    return {
        FIELD_OBJECT_NAME: tokenize(object_name),
        FIELD_SCHEMA_NAME: tokenize(schema_name),
        FIELD_BUSINESS_NAME: tokenize(business_name),
        FIELD_SYNONYMS: tuple(tok for term in synonyms for tok in tokenize(term)),
        FIELD_DESCRIPTION: tokenize(description),
        FIELD_COLUMN_NAMES: tuple(tok for col in column_names for tok in tokenize(col)),
        FIELD_RELATIONSHIPS: tuple(tok for name in relationships for tok in tokenize(name)),
    }


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LexicalDocument:
    """One document's precomputed per-field term frequencies — the unit
    build_lexical_index() indexes and search_lexical_index() scores against.
    `field_term_counts` maps field name -> {token: count}; `field_lengths`
    maps field name -> total token count in that field (both only for
    fields that have at least one token — an empty/absent field is simply
    not a key, never a zero-length placeholder that would skew averages)."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]
    field_term_counts: Mapping[str, Mapping[str, int]]
    field_lengths: Mapping[str, int]


@dataclass(frozen=True)
class LexicalIndexIdentity:
    """Exact cache identity — deliberately mirrors
    core.sqlservice.vector_index.VectorIndexIdentity field-for-field
    (build_id, provider, model, dimensions), per this step's own
    requirement to cache lexical data under the same strict identity as the
    vector index, even though the lexical content itself does not depend on
    embedding provider/model/dimensions — this ties the lexical cache's
    lifecycle to the same build/provider/model/dimension identity the
    vector index already uses, so the two never drift out of sync."""

    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class LexicalIndex:
    """An immutable, ready-to-search lexical index: every document's
    per-field term frequencies precomputed once, plus the corpus-level
    statistics (document frequency per (field, token) and average field
    length per field) BM25F scoring needs. `documents` is in the same
    deterministic object_fqn order the source document sequence was in."""

    identity: LexicalIndexIdentity
    documents: tuple[LexicalDocument, ...]
    document_count: int
    document_frequency: Mapping[str, Mapping[str, int]]   # field -> token -> #docs containing it
    average_field_length: Mapping[str, float]              # field -> mean length across docs THAT HAVE the field


@dataclass(frozen=True)
class LexicalSearchResult:
    """One ranked hit from search_lexical_index() — mirrors
    core.sqlservice.vector_index.VectorSearchResult's shape for symmetry."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]
    score: float
    matched_fields: tuple[str, ...]
    matched_tokens: tuple[str, ...]


# ── Index construction (pure, no I/O) ──────────────────────────────────────


def build_lexical_index(documents: Sequence, identity: LexicalIndexIdentity) -> LexicalIndex:
    """
    Build an immutable LexicalIndex from an already-loaded document sequence
    (typically core.sqlservice.vector_index.VectorIndex.documents —
    IndexedSchemaDocument instances — but any object exposing object_fqn/
    object_type/retrieval_tier/document_hash/document_text/metadata works).
    Pure and in-memory: never touches SQLite, never calls an embedding
    provider, never reads document_text as prose.
    """
    lexical_docs: list[LexicalDocument] = []
    doc_freq: dict[str, Counter] = {field: Counter() for field in _ALL_FIELDS}
    field_length_sum: dict[str, int] = {field: 0 for field in _ALL_FIELDS}
    field_doc_count: dict[str, int] = {field: 0 for field in _ALL_FIELDS}

    for doc in documents:
        raw_fields = _extract_fields(doc.object_fqn, doc.metadata)
        field_term_counts: dict[str, dict[str, int]] = {}
        field_lengths: dict[str, int] = {}
        for field, tokens in raw_fields.items():
            if not tokens:
                continue
            counts = Counter(tokens)
            field_term_counts[field] = dict(counts)
            field_lengths[field] = len(tokens)
            field_length_sum[field] += len(tokens)
            field_doc_count[field] += 1
            for token in counts:
                doc_freq[field][token] += 1

        lexical_docs.append(
            LexicalDocument(
                object_fqn=doc.object_fqn,
                object_type=doc.object_type,
                retrieval_tier=doc.retrieval_tier,
                document_hash=doc.document_hash,
                document_text=doc.document_text,
                metadata=doc.metadata,
                field_term_counts=field_term_counts,
                field_lengths=field_lengths,
            )
        )

    average_field_length = {
        field: (field_length_sum[field] / field_doc_count[field]) if field_doc_count[field] else 0.0
        for field in _ALL_FIELDS
    }
    document_frequency = {field: dict(counter) for field, counter in doc_freq.items()}

    return LexicalIndex(
        identity=identity,
        documents=tuple(lexical_docs),
        document_count=len(lexical_docs),
        document_frequency=document_frequency,
        average_field_length=average_field_length,
    )


# ── BM25F scoring (pure, no I/O) ────────────────────────────────────────────


def _idf(document_frequency: int, total_documents: int) -> float:
    # Okapi BM25's smoothed IDF variant — always non-negative, never
    # penalizes a term that appears in every document down to a negative
    # weight (the classic un-smoothed formula can go negative for very
    # common terms, which is never desirable for a small schema corpus).
    return math.log(1.0 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5))


def search_lexical_index(
    index: LexicalIndex,
    query_tokens: Sequence[str],
    *,
    top_k: int = 10,
) -> tuple[LexicalSearchResult, ...]:
    """
    Score every document in `index` against `query_tokens` (already
    tokenized — see tokenize()) using field-weighted, length-normalized
    BM25F, and return the top_k by score descending (object_fqn ascending
    tie-break, matching core.sqlservice.vector_index.search_vector_index's
    convention). A document with zero matching tokens in any field scores
    0.0 and is never included in the result (there is nothing to rank it
    on) — see schema_context_retrieval's fusion logic for how a
    zero-lexical-score document is still discoverable via its semantic rank
    alone.
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")

    unique_query_tokens = list(dict.fromkeys(t for t in query_tokens if t))
    if not unique_query_tokens:
        return ()

    idf_by_field_token: dict[str, dict[str, float]] = {}
    for field in _ALL_FIELDS:
        field_df = index.document_frequency.get(field, {})
        idf_by_field_token[field] = {
            token: _idf(field_df.get(token, 0), index.document_count)
            for token in unique_query_tokens
            if field_df.get(token, 0) > 0
        }

    scored: list[tuple[LexicalDocument, float, tuple[str, ...], tuple[str, ...]]] = []
    for doc in index.documents:
        total_score = 0.0
        matched_fields: list[str] = []
        matched_tokens: set[str] = set()

        for field in _ALL_FIELDS:
            counts = doc.field_term_counts.get(field)
            if not counts:
                continue
            field_len = doc.field_lengths[field]
            avg_len = index.average_field_length.get(field) or 1.0
            b = FIELD_LENGTH_NORM_B.get(field, 0.75)
            weight = FIELD_WEIGHTS.get(field, 1.0)
            norm = (1.0 - b + b * (field_len / avg_len)) if avg_len > 0 else 1.0

            field_hit = False
            for token in unique_query_tokens:
                tf = counts.get(token, 0)
                if tf == 0:
                    continue
                idf = idf_by_field_token[field].get(token)
                if idf is None:
                    continue
                weighted_tf = weight * tf / norm if norm > 0 else 0.0
                contribution = idf * (weighted_tf * (BM25_K1 + 1.0)) / (weighted_tf + BM25_K1)
                total_score += contribution
                matched_tokens.add(token)
                field_hit = True
            if field_hit:
                matched_fields.append(field)

        if total_score > 0.0:
            scored.append((doc, total_score, tuple(matched_fields), tuple(sorted(matched_tokens))))

    scored.sort(key=lambda item: (-item[1], item[0].object_fqn))

    return tuple(
        LexicalSearchResult(
            object_fqn=doc.object_fqn,
            object_type=doc.object_type,
            retrieval_tier=doc.retrieval_tier,
            document_hash=doc.document_hash,
            document_text=doc.document_text,
            metadata=doc.metadata,
            score=score,
            matched_fields=matched_fields,
            matched_tokens=matched_tokens,
        )
        for doc, score, matched_fields, matched_tokens in scored[:top_k]
    )


# ── Bounded in-process cache (mirrors core.sqlservice.vector_index's) ─────

_MAX_CACHE_ENTRIES = 16
_lexical_cache: "OrderedDict[LexicalIndexIdentity, LexicalIndex]" = OrderedDict()


def load_lexical_index(documents: Sequence, identity: LexicalIndexIdentity, *, use_cache: bool = True) -> LexicalIndex:
    """
    Build (or reuse a cached) LexicalIndex for `identity`. Never touches
    SQLite — `documents` must already be loaded (e.g. from
    core.sqlservice.vector_index.load_vector_index()'s own result), so a
    cache hit here costs nothing beyond a dict lookup and a cache miss costs
    only in-memory tokenization/counting, never I/O. No question data is
    ever stored in this cache — only corpus-level, per-build document/field
    statistics that are identical for every question asked against the same
    build.
    """
    if use_cache:
        cached = _lexical_cache.get(identity)
        if cached is not None:
            _lexical_cache.move_to_end(identity)
            return cached

    index = build_lexical_index(documents, identity)
    if use_cache:
        _lexical_cache[identity] = index
        _lexical_cache.move_to_end(identity)
        while len(_lexical_cache) > _MAX_CACHE_ENTRIES:
            _lexical_cache.popitem(last=False)
    return index


def clear_lexical_index_cache() -> None:
    """Explicitly evict every cached LexicalIndex — for tests, and for
    future rebuild invalidation, mirroring clear_vector_index_cache()."""
    _lexical_cache.clear()


def lexical_index_cache_size() -> int:
    """Number of identities currently cached — exposed for tests."""
    return len(_lexical_cache)
