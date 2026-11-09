"""
Deterministic, build-level canonical-object scoring — SQL rebuild Phase 5,
Step 8.

Answers a question orthogonal to "is this document relevant to THIS
question" (the job of vector_index/lexical_index): "given several objects
that already look relevant, which one is the authoritative business
object, and which are narrow, derivative, duplicate, or incidental?" —
computed ONCE per build from already-available structured metadata
(core.sqlservice.retrieval_documents' metadata contract), never per
question, never from a source-specific table/domain list, never from row
counts (not safely/cheaply available at this layer).

Scope: pure, in-memory, no I/O of any kind. Reads only object_fqn,
object_type, retrieval_tier, and metadata already attached to every
RetrievalDocument/IndexedSchemaDocument. Every metadata key is read with
.get(...) and a safe default, so a document from an older build that
predates one or more keys degrades gracefully (a lower score on the
missing signal, never a crash).

No specific customer schema's table names or business vocabulary appear
anywhere in this file (see
tests/test_sqlservice_canonical_scoring.py::test_no_domain_specific_tokens_in_production_module
for an enforced guard).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

__all__ = [
    "COMPONENT_OBJECT_TYPE",
    "COMPONENT_PRIMARY_KEY",
    "COMPONENT_PHYSICAL_FK",
    "COMPONENT_RELATIONSHIP_CONNECTIVITY",
    "COMPONENT_REFERENCED_BY_VIEWS",
    "COMPONENT_BUSINESS_DOCUMENTATION",
    "COMPONENT_SYNONYM_QUALITY",
    "COMPONENT_BUSINESS_COLUMNS",
    "COMPONENT_NAME_QUALITY",
    "COMPONENT_TIER_QUALITY",
    "COMPONENT_WEIGHTS",
    "RELATIONSHIP_DEGREE_SATURATION",
    "PHYSICAL_FK_SATURATION",
    "REFERENCED_BY_VIEWS_SATURATION",
    "SYNONYM_SATURATION",
    "BUSINESS_COLUMN_SATURATION",
    "MIN_SIGNIFICANT_NAME_TOKEN_LENGTH",
    "NEIGHBOR_FAMILY_MIN_SHARED_NEIGHBORS",
    "NEIGHBOR_FAMILY_MIN_JACCARD",
    "GENERIC_TOKEN_DF_RATIO",
    "GENERIC_TOKEN_DF_MIN_ABSOLUTE",
    "NAME_SIMILARITY_JACCARD_THRESHOLD",
    "MIN_SHARED_TOKENS_FOR_NAME_PASS",
    "VARIANT_BASE_JACCARD_THRESHOLD",
    "STRUCTURAL_COLUMN_JACCARD_THRESHOLD",
    "MIN_COLUMNS_FOR_STRUCTURAL_COMPARISON",
    "STRUCTURAL_COLUMN_BUCKET_CAP",
    "HUB_NEIGHBOR_EXCLUSION_THRESHOLD",
    "MAX_REASONABLE_FAMILY_SIZE",
    "MAX_FAMILY_WARNINGS",
    "CanonicalObjectScore",
    "CanonicalScoringIdentity",
    "CanonicalScoringResult",
    "compute_canonical_scores",
    "load_canonical_scores",
    "clear_canonical_scoring_cache",
    "canonical_scoring_cache_size",
]

# ── Component identifiers (stable strings — used as dict keys, never as
#    free-text so observability output is machine-comparable) ────────────

COMPONENT_OBJECT_TYPE = "OBJECT_TYPE"
COMPONENT_PRIMARY_KEY = "PRIMARY_KEY"
COMPONENT_PHYSICAL_FK = "PHYSICAL_FK"
COMPONENT_RELATIONSHIP_CONNECTIVITY = "RELATIONSHIP_CONNECTIVITY"
COMPONENT_REFERENCED_BY_VIEWS = "REFERENCED_BY_VIEWS"
COMPONENT_BUSINESS_DOCUMENTATION = "BUSINESS_DOCUMENTATION"
COMPONENT_SYNONYM_QUALITY = "SYNONYM_QUALITY"
COMPONENT_BUSINESS_COLUMNS = "BUSINESS_COLUMNS"
COMPONENT_NAME_QUALITY = "NAME_QUALITY"
COMPONENT_TIER_QUALITY = "TIER_QUALITY"

# Each component is independently bounded to [0.0, 1.0]; weights sum to
# 1.0, so the total canonical score is always in [0.0, 1.0] too — never an
# arbitrary/unbounded raw number. No single component dominates: the two
# largest (RELATIONSHIP_CONNECTIVITY, REFERENCED_BY_VIEWS,
# BUSINESS_DOCUMENTATION at 0.15 each) still leave the remaining 0.55
# spread across seven other independent signals, so an object cannot
# become "canonical" purely by maximizing one dimension (e.g. relationship
# degree — see RELATIONSHIP_DEGREE_SATURATION's own docstring for the
# separate, complementary anti-hub-domination safeguard).
COMPONENT_WEIGHTS: Mapping[str, float] = {
    COMPONENT_OBJECT_TYPE: 0.05,
    COMPONENT_PRIMARY_KEY: 0.10,
    COMPONENT_PHYSICAL_FK: 0.10,
    COMPONENT_RELATIONSHIP_CONNECTIVITY: 0.15,
    COMPONENT_REFERENCED_BY_VIEWS: 0.15,
    COMPONENT_BUSINESS_DOCUMENTATION: 0.15,
    COMPONENT_SYNONYM_QUALITY: 0.10,
    COMPONENT_BUSINESS_COLUMNS: 0.10,
    COMPONENT_NAME_QUALITY: 0.05,
    COMPONENT_TIER_QUALITY: 0.05,
}
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9

# Saturation caps for log-normalized count-based components — "never treat
# highest relationship degree as automatically canonical": past this many
# edges/references, additional ones add essentially nothing further
# (log1p(x)/log1p(cap) flattens hard beyond `cap`), so a hub with 500
# confirmed edges scores only marginally above one with 20, never
# proportionally 25x higher.
RELATIONSHIP_DEGREE_SATURATION = 20
PHYSICAL_FK_SATURATION = 5
REFERENCED_BY_VIEWS_SATURATION = 8
SYNONYM_SATURATION = 8
BUSINESS_COLUMN_SATURATION = 6

MIN_SIGNIFICANT_NAME_TOKEN_LENGTH = 4

# Relationship-context family merge (see _compute_families) — two objects
# not already grouped by a shared name token are still considered the same
# comparable family when they share this many (or more) confirmed
# relationship neighbors AND their neighbor-set Jaccard similarity clears
# this bar — i.e. they connect to essentially the same surrounding schema,
# a strong structural signal of being alternative/duplicate representations
# of the same underlying data (e.g. two views built over the same base
# tables), independent of naming.
NEIGHBOR_FAMILY_MIN_SHARED_NEIGHBORS = 2
NEIGHBOR_FAMILY_MIN_JACCARD = 0.6

# ── Family detection: corrected in SQL Architecture Correction Phase 6,
#    Step 1A. The ORIGINAL rule unioned any two objects sharing even ONE
#    significant (>=4-char) name token, with no bound on how many objects a
#    single token could transitively connect. A real thousand-plus-object schema
#    audit found this produced one thousand-member family purely through
#    transitive chaining across many moderately common words (no single
#    token exceeded ~6% document frequency; the giant component emerged
#    from many overlapping weak pairs — A/B share "survey", B/C share
#    "activity", C/D share "class", ... — eventually connecting almost the
#    whole schema, confirmed by isolating the name-token pass alone). See
#    test_family_detection_never_produces_a_transitive_mega_family and the
#    Phase 6 Step 1A report for the full reproduction.
#
#    The fix replaces "do the two names share a token" with four
#    independent, deliberately STRONG evidence types — any ONE of which is
#    sufficient to union a pair, but each is a genuine pairwise similarity
#    check, never a shared-token membership test:
#      1. Name-token JACCARD similarity over each object's OWN FULL
#         significant-token set (how much of each name's own vocabulary
#         overlaps, not merely "do they share one word"), computed only
#         over tokens that are not themselves too common IN THIS BUILD
#         (see GENERIC_TOKEN_DF_RATIO) — a schema/module prefix or an
#         ordinary business noun reused across many unrelated tables can no
#         longer, by itself, bridge two otherwise-dissimilar names.
#      2. An explicit archive/backup/version relationship: one name is the
#         other's own name plus generic variant-marker tokens (backup/
#         archive/history/staging/temp/copy/test/old/deprecated/a bare
#         numeric-looking token) — independent of how common the shared
#         BASE tokens are corpus-wide, since this is pairwise evidence
#         about one specific relationship, not general vocabulary.
#      3. Strong structural column-name similarity — two objects whose
#         column sets overlap heavily are plausible alternate
#         representations of the same underlying data (e.g. a view built
#         over the same columns as its base table), independent of naming.
#      4. Bounded shared-relationship-neighbor evidence — the ORIGINAL
#         threshold, unchanged (already empirically hub-safe: isolated on
#         its own against the same thousand-plus-object schema, its largest
#         resulting family was 10 members, not part of the mega-family
#         bug) — now ALSO excludes any object whose own neighbor set is
#         itself hub-sized, as defense in depth (HUB_NEIGHBOR_EXCLUSION_
#         THRESHOLD). This is a same-build, single-hop NEIGHBOR-SET overlap
#         between the two candidate objects themselves — never a multi-hop
#         relationship PATH (that traversal lives entirely in the separate
#         core.sqlservice.relationship_graph module, used only for join-
#         bridging, and is never consulted here) — so "a relationship path
#         alone" never establishes family membership by construction, not
#         merely by convention.
#
#    Every pass still uses an inverted-index candidate-generation step
#    (never a raw O(n^2) pairwise scan) and remains fully deterministic
#    under any input ordering — see _compute_families's own docstring.
GENERIC_TOKEN_DF_RATIO = 0.02
GENERIC_TOKEN_DF_MIN_ABSOLUTE = 5
NAME_SIMILARITY_JACCARD_THRESHOLD = 0.5
# A real audit found Jaccard alone still insufficient: many objects reduce
# to a SINGLE remaining significant token after generic-token filtering
# (e.g. a short module-prefixed name whose only other word is itself
# common), and Jaccard(one-token-set, any-set-containing-it) is always
# exactly 1/(other_set_size), which trivially clears 0.5 against any other
# object with at most one additional token — a live-data reproduction
# isolating Pass 1 alone found this alone still produced a 458-member
# component. Requiring at least this many ACTUAL shared tokens (not just a
# ratio) closes that gap; on the same real schema it cut the largest
# Pass-1-only family from hundreds of members to a few dozen.
MIN_SHARED_TOKENS_FOR_NAME_PASS = 2
VARIANT_BASE_JACCARD_THRESHOLD = 0.8
STRUCTURAL_COLUMN_JACCARD_THRESHOLD = 0.8
MIN_COLUMNS_FOR_STRUCTURAL_COMPARISON = 3
# A single very common column name (e.g. "Id", "CreatedDate") would
# otherwise generate a combinatorial number of structural-similarity
# candidate pairs; a bucket larger than this is skipped entirely for
# candidate generation (that column name is not distinctive enough to seed
# a comparison — the OTHER, less common columns in a genuine near-duplicate
# pair will still seed it).
STRUCTURAL_COLUMN_BUCKET_CAP = 50
# An object confirmed-adjacent to more than this many other objects is a
# structural hub (e.g. a lookup table referenced by dozens of views) — it
# is excluded from ever PARTICIPATING in a neighbor-based family merge,
# regardless of Jaccard, as defense in depth beyond the Jaccard math itself
# (a hub's own huge neighbor set already tends to suppress Jaccard, but
# this makes the "hubs do not collapse the graph" guarantee explicit and
# independently testable).
HUB_NEIGHBOR_EXCLUSION_THRESHOLD = 30
# A family larger than this, even after the corrected evidence rules above,
# is unusual enough to warrant an explicit, deterministic warning (never a
# silent truncation or a crash) — see compute_canonical_scores's
# `family_warnings` output.
MAX_REASONABLE_FAMILY_SIZE = 25
MAX_FAMILY_WARNINGS = 25

# Deliberately duplicated (not imported) from core.sqlservice.
# retrieval_documents._STALE_COPY_TOKENS — same zero-cross-module-
# dependency convention this file already documents for _local_tokenize
# below. Generic English archive/backup/version vocabulary only.
_VARIANT_MARKER_TOKENS = frozenset({
    "backup", "bak", "bkp", "bckup",
    "archive", "archived",
    "history", "hist",
    "staging", "stg",
    "temp", "tmp",
    "copy", "test", "old", "new",
    "deprecated", "obsolete",
    "prior", "prev", "previous",
})


# ── Local, dependency-free tokenizer ───────────────────────────────────────
#
# Deliberately duplicated (not imported) from core.sqlservice.
# retrieval_documents._tokenize / core.sqlservice.lexical_index.tokenize —
# same convention core.sqlservice.relationship_graph already established
# for itself ("zero dependency on schema_context_retrieval... zero
# import-cycle risk"): this module has zero dependency on either sibling
# module. Generic English/identifier splitting only — no domain vocabulary.

_SPLIT_PATTERN = re.compile(
    r"[_\-\s./\\,;:!?()\[\]{}'\"]+"
    r"|(?<=[a-z0-9])(?=[A-Z])"
    r"|(?<=[A-Za-z])(?=[0-9])"
    r"|(?<=[0-9])(?=[A-Za-z])",
)


def _local_tokenize(text: object) -> tuple[str, ...]:
    if not text:
        return ()
    parts = [p for p in _SPLIT_PATTERN.split(str(text)) if p]
    return tuple(p.lower() for p in parts if p)


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalObjectScore:
    """One object's immutable canonical-authority assessment for one build.

    `total_score`: weighted sum of `components`, always in [0.0, 1.0].
    `components`: every COMPONENT_* identifier -> its own [0.0, 1.0] value —
        full observability into exactly why an object scored as it did
        (Step 8 requirement 7).
    `family_key`: a deterministic identifier (the lexicographically
        smallest object_fqn in the family) shared by every object this
        build's automatic family detection grouped with this one — never a
        hand-authored category, never source-specific.
    `family_size`: how many objects (including this one) share `family_key`
        — 1 means this object was not judged comparable to anything else.
    `family_rank`: this object's 1-indexed position within its own family
        when sorted by total_score descending, object_fqn ascending
        tie-break. 1 means it is the family's own most-canonical member.
    """

    object_fqn: str
    total_score: float
    components: Mapping[str, float]
    family_key: str
    family_size: int
    family_rank: int


@dataclass(frozen=True)
class CanonicalScoringIdentity:
    """Exact cache identity (Step 8 requirement 8) — build_id, provider,
    model, dimensions only. Never question content: canonical scoring
    never sees a question at all."""

    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class CanonicalScoringResult:
    """An immutable, ready-to-use canonical scoring pass over one build's
    full document set (every tier — tier filtering for a specific question
    remains the caller's own responsibility, applied exactly as before;
    see core.sqlservice.schema_context_retrieval). `rank_by_fqn_lower` is
    the 1-indexed GLOBAL canonical rank (descending total_score, object_fqn
    ascending tie-break) across every document in the build — the caller
    combines this with query-relevance ranks via its own bounded,
    rank-based fusion; this module never mixes in relevance itself."""

    identity: CanonicalScoringIdentity
    scores_by_fqn_lower: Mapping[str, CanonicalObjectScore]
    rank_by_fqn_lower: Mapping[str, int]
    document_count: int
    # Phase 6, Step 1A addition (default-safe: every prior caller/test that
    # never mentions this field keeps working unchanged). Deterministic,
    # bounded (see MAX_FAMILY_WARNINGS) warnings about unusually large
    # families detected THIS build, even after the corrected evidence
    # rules — see MAX_REASONABLE_FAMILY_SIZE's own docstring.
    family_warnings: tuple[str, ...] = ()


# ── Component scoring (each independently bounded [0.0, 1.0]) ────────────


def _saturating(count: int, cap: int) -> float:
    """log1p-normalized count in [0.0, 1.0], flattening hard past `cap` —
    the shared anti-domination shape every count-based component uses."""
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(cap))


def _score_object_type(object_type: str) -> float:
    """A real, recognized object (TABLE or VIEW) scores identically —
    "never assume every base table is better than every view" is enforced
    literally here: this component carries NO type preference at all.
    Object type instead meaningfully modulates COMPONENT_PRIMARY_KEY below
    (a view is not expected to declare a primary key the way a table is).
    An unrecognized type scores lower only as a defensive sanity signal,
    never observed for a real schema object."""
    return 1.0 if object_type in ("TABLE", "VIEW") else 0.5


def _score_primary_key(object_type: str, has_primary_key: bool) -> float:
    if object_type == "VIEW":
        # No structural expectation either way for a view.
        return 1.0 if has_primary_key else 0.7
    return 1.0 if has_primary_key else 0.4


def _score_physical_fk(foreign_key_target_count: int) -> float:
    return _saturating(foreign_key_target_count, PHYSICAL_FK_SATURATION)


def _score_relationship_connectivity(confirmed_degree: int) -> float:
    return _saturating(confirmed_degree, RELATIONSHIP_DEGREE_SATURATION)


def _score_referenced_by_views(referencing_view_count: int) -> float:
    return _saturating(referencing_view_count, REFERENCED_BY_VIEWS_SATURATION)


def _score_business_documentation(has_business_name: bool, has_description: bool) -> float:
    return (0.6 if has_business_name else 0.0) + (0.4 if has_description else 0.0)


def _score_synonym_quality(synonym_count: int) -> float:
    """Presence + saturating quality, never raw volume: a bare 0.3 for
    having ANY synonym coverage at all, plus up to 0.7 more that flattens
    well before synonym_count could ever dominate the total score just by
    being large (Step 8 requirement: "as presence/quality, not raw synonym
    count")."""
    if synonym_count <= 0:
        return 0.0
    return 0.3 + 0.7 * _saturating(synonym_count, SYNONYM_SATURATION)


def _score_business_columns(status_count: int, date_count: int, measure_count: int, pk_count: int) -> float:
    relevant = status_count + date_count + measure_count + (1 if pk_count else 0)
    return _saturating(relevant, BUSINESS_COLUMN_SATURATION)


def _score_name_quality(object_fqn: str) -> float:
    """Fraction of the object's own name tokens that look like real,
    meaningful words (length >= 3, at least one letter, not purely
    numeric/cryptic) — a fully generic, language-level heuristic; no
    identifier or word list from any specific schema."""
    _schema, _sep, object_name = object_fqn.rpartition(".")
    tokens = _local_tokenize(object_name)
    if not tokens:
        return 0.0
    meaningful = sum(1 for t in tokens if len(t) >= 3 and any(c.isalpha() for c in t) and not t.isdigit())
    return meaningful / len(tokens)


def _score_tier_quality(retrieval_tier: int) -> float:
    return {1: 1.0, 2: 0.7, 3: 0.2, 4: 0.0}.get(retrieval_tier, 0.0)


# ── Family detection (name-token + relationship-context union-find) ──────


class _DisjointSet:
    """Deterministic union-find: union always attaches the LARGER root to
    the SMALLER (by string comparison) root, so the final root of any
    component is always its lexicographically smallest member regardless
    of processing order — the family_key IS that root, with no extra
    bookkeeping needed."""

    def __init__(self, items: Sequence[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb


def _significant_name_tokens(object_fqn: str, business_name: object) -> frozenset[str]:
    _schema, _sep, object_name = object_fqn.rpartition(".")
    tokens = set(_local_tokenize(object_name))
    if business_name:
        tokens |= set(_local_tokenize(business_name))
    return frozenset(t for t in tokens if len(t) >= MIN_SIGNIFICANT_NAME_TOKEN_LENGTH)


def _object_name_only_tokens(object_fqn: str) -> frozenset[str]:
    """Every raw token (no length filter, and deliberately NOT including
    business_name) of just the bare object name — used ONLY by the Phase 6
    Step 1A archive/variant family pass below, which is fundamentally about
    PHYSICAL NAMING similarity (does one object's name look like another's
    own name plus a marker/date suffix), not documentation content. Mixing
    in business_name here would let one side's documented, human-written
    business name (which the other, undocumented Tier 4 copy naturally
    lacks) dilute an otherwise-exact base-name match — exactly the
    asymmetry a real "Customers" (documented) vs "Customers_bak"
    (undocumented) pair exposed. Also needs no length filter, so it can see
    SHORT marker tokens ("bak", "old") that _significant_name_tokens' own
    >=4-char bar would otherwise hide entirely."""
    _schema, _sep, object_name = object_fqn.rpartition(".")
    return frozenset(_local_tokenize(object_name))


def _neighbor_fqns_lower(metadata: Mapping[str, object]) -> frozenset[str]:
    neighbors = set()
    for key in ("confirmed_relationship_targets", "confirmed_relationship_sources"):
        for descriptor in metadata.get(key) or ():
            object_fqn, sep, _column = str(descriptor).rpartition(".")
            if sep and object_fqn:
                neighbors.add(object_fqn.lower())
    return frozenset(neighbors)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _generic_tokens(tokens_by_fqn: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """
    Tokens too common IN THIS BUILD to serve as name-similarity evidence —
    a generic, build-relative statistic (max-document-frequency cutoff, the
    same well-known technique behind e.g. scikit-learn's `max_df` stop-word
    filtering), never a fixed word list and never tuned to any specific
    schema's own vocabulary. A token qualifies once its document frequency
    reaches max(GENERIC_TOKEN_DF_MIN_ABSOLUTE, ceil(GENERIC_TOKEN_DF_RATIO *
    total_object_count)) — the absolute floor keeps small schemas from
    having everything excluded.
    """
    total = len(tokens_by_fqn)
    if total == 0:
        return frozenset()
    threshold = max(GENERIC_TOKEN_DF_MIN_ABSOLUTE, math.ceil(GENERIC_TOKEN_DF_RATIO * total))
    doc_frequency: dict[str, int] = defaultdict(int)
    for tokens in tokens_by_fqn.values():
        for token in tokens:
            doc_frequency[token] += 1
    return frozenset(token for token, count in doc_frequency.items() if count >= threshold)


def _strip_variant_markers(tokens: frozenset[str]) -> frozenset[str]:
    """Remove generic archive/backup/version marker tokens and bare numeric
    tokens (dates, version numbers) — what remains is the object's own
    "base identity" vocabulary, used only by the archive/variant pass."""
    return frozenset(t for t in tokens if t not in _VARIANT_MARKER_TOKENS and not t.isdigit())


def _generate_candidate_pairs(index: Mapping[str, set[str]], *, bucket_cap: int | None = None) -> set[tuple[str, str]]:
    """Shared inverted-index candidate-pair generator: two fqns are a
    candidate pair whenever they co-occur in the same index bucket (a
    token, or a column name). Deterministic (buckets and members sorted
    before pairing) and never a raw O(n^2) pairwise scan — a bucket larger
    than `bucket_cap` (when given) is skipped, since that key is too
    common to be distinctive comparison evidence on its own."""
    pairs: set[tuple[str, str]] = set()
    for key in sorted(index):
        members = sorted(index[key])
        if bucket_cap is not None and len(members) > bucket_cap:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pairs.add((a, b))
    return pairs


def _compute_families(
    fqn_lower_list: Sequence[str],
    tokens_by_fqn: Mapping[str, frozenset[str]],
    neighbors_by_fqn: Mapping[str, frozenset[str]],
    column_names_by_fqn: Mapping[str, frozenset[str]],
    object_name_tokens_by_fqn: Mapping[str, frozenset[str]],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """
    Returns (fqn_lower -> family_key, family_warnings). family_key is the
    family's own lexicographically smallest fqn_lower. Four independent,
    deterministic evidence passes — see this section's own module-level
    comment (above GENERIC_TOKEN_DF_RATIO) for the full corrected-algorithm
    rationale and the Phase 6 Step 1A bug it fixes. Any ONE pass unioning a
    pair is sufficient; passes are independent, not sequential filters.
    """
    dsu = _DisjointSet(fqn_lower_list)
    generic_tokens = _generic_tokens(tokens_by_fqn)
    filtered_tokens_by_fqn = {
        fqn: frozenset(t for t in tokens if t not in generic_tokens) for fqn, tokens in tokens_by_fqn.items()
    }
    # Base (variant-marker-stripped) tokens for Pass 2 come from the RAW,
    # object-NAME-ONLY token set — never `tokens_by_fqn` (which has already
    # dropped any token shorter than MIN_SIGNIFICANT_NAME_TOKEN_LENGTH,
    # hiding short markers like "bak"/"old") and never business_name (see
    # _object_name_only_tokens' own docstring for why mixing in
    # documentation content breaks this specific comparison).
    base_tokens_by_fqn = {fqn: _strip_variant_markers(tokens) for fqn, tokens in object_name_tokens_by_fqn.items()}

    # ── Pass 1: strong name-token Jaccard similarity (generic-filtered) ──
    token_index: dict[str, set[str]] = defaultdict(set)
    for fqn, tokens in filtered_tokens_by_fqn.items():
        for token in tokens:
            token_index[token].add(fqn)
    for a, b in sorted(_generate_candidate_pairs(token_index)):
        ta, tb = filtered_tokens_by_fqn.get(a, frozenset()), filtered_tokens_by_fqn.get(b, frozenset())
        if len(ta & tb) < MIN_SHARED_TOKENS_FOR_NAME_PASS:
            continue
        if _jaccard(ta, tb) >= NAME_SIMILARITY_JACCARD_THRESHOLD:
            dsu.union(a, b)

    # ── Pass 2: explicit archive/backup/version relationship ────────────
    # Base-token (variant-marker-stripped) index — NOT generic-filtered:
    # this is pairwise evidence about one specific relationship, so it must
    # never be suppressed merely because the shared base vocabulary is also
    # common elsewhere in the schema.
    base_index: dict[str, set[str]] = defaultdict(set)
    for fqn, tokens in base_tokens_by_fqn.items():
        for token in tokens:
            base_index[token].add(fqn)
    for a, b in sorted(_generate_candidate_pairs(base_index)):
        ba, bb = base_tokens_by_fqn.get(a, frozenset()), base_tokens_by_fqn.get(b, frozenset())
        if not ba or not bb:
            continue
        # Require a genuine "same base, plus a variant/date marker" shape:
        # at least one side must actually carry a marker/numeric token the
        # other lacks — otherwise this degenerates into plain name equality
        # (already covered, at a lower bar, by Pass 1).
        a_has_marker = bool(object_name_tokens_by_fqn.get(a, frozenset()) - ba)
        b_has_marker = bool(object_name_tokens_by_fqn.get(b, frozenset()) - bb)
        if not (a_has_marker or b_has_marker):
            continue
        if _jaccard(ba, bb) >= VARIANT_BASE_JACCARD_THRESHOLD:
            dsu.union(a, b)

    # ── Pass 3: strong structural column-name similarity ─────────────────
    column_index: dict[str, set[str]] = defaultdict(set)
    for fqn, columns in column_names_by_fqn.items():
        for column in columns:
            column_index[column.lower()].add(fqn)
    for a, b in sorted(_generate_candidate_pairs(column_index, bucket_cap=STRUCTURAL_COLUMN_BUCKET_CAP)):
        ca = column_names_by_fqn.get(a, frozenset())
        cb = column_names_by_fqn.get(b, frozenset())
        if len(ca) < MIN_COLUMNS_FOR_STRUCTURAL_COMPARISON or len(cb) < MIN_COLUMNS_FOR_STRUCTURAL_COMPARISON:
            continue
        lower_a = frozenset(c.lower() for c in ca)
        lower_b = frozenset(c.lower() for c in cb)
        if _jaccard(lower_a, lower_b) >= STRUCTURAL_COLUMN_JACCARD_THRESHOLD:
            dsu.union(a, b)

    # ── Pass 4: bounded shared-relationship-neighbor evidence ────────────
    # Unchanged threshold from the original design (already empirically
    # hub-safe on its own), plus an explicit hub-size exclusion as defense
    # in depth. This compares the two CANDIDATE objects' own immediate
    # (1-hop) neighbor sets to each other — never a multi-hop relationship
    # PATH — so "a relationship path alone" cannot establish family
    # membership by construction.
    neighbor_index: dict[str, set[str]] = defaultdict(set)
    for fqn, neighbors in neighbors_by_fqn.items():
        for neighbor in neighbors:
            neighbor_index[neighbor].add(fqn)
    for a, b in sorted(_generate_candidate_pairs(neighbor_index)):
        na, nb = neighbors_by_fqn.get(a, frozenset()), neighbors_by_fqn.get(b, frozenset())
        if len(na) > HUB_NEIGHBOR_EXCLUSION_THRESHOLD or len(nb) > HUB_NEIGHBOR_EXCLUSION_THRESHOLD:
            continue
        shared = na & nb
        if len(shared) < NEIGHBOR_FAMILY_MIN_SHARED_NEIGHBORS:
            continue
        if _jaccard(na, nb) >= NEIGHBOR_FAMILY_MIN_JACCARD:
            dsu.union(a, b)

    families = {fqn: dsu.find(fqn) for fqn in fqn_lower_list}

    # ── Defensive limit: deterministic warnings for unusually large
    #    families (never a silent truncation, never a crash) ─────────────
    members_by_root: dict[str, list[str]] = defaultdict(list)
    for fqn, root in families.items():
        members_by_root[root].append(fqn)

    warnings: list[str] = []
    for root in sorted(members_by_root):
        members = sorted(members_by_root[root])
        if len(members) > MAX_REASONABLE_FAMILY_SIZE:
            sample = ", ".join(members[:10])
            warnings.append(
                f"family rooted at {root!r} has {len(members)} members, exceeding "
                f"MAX_REASONABLE_FAMILY_SIZE={MAX_REASONABLE_FAMILY_SIZE} "
                f"(first 10 of {len(members)}: {sample})"
            )
    bounded_warnings = tuple(warnings[:MAX_FAMILY_WARNINGS])

    return families, bounded_warnings


# ── Public batch entry point (pure) ────────────────────────────────────────


def compute_canonical_scores(documents: Sequence, identity: CanonicalScoringIdentity) -> CanonicalScoringResult:
    """
    Build a CanonicalScoringResult from an already-loaded document sequence
    (typically core.sqlservice.vector_index.VectorIndex.documents — any
    object exposing object_fqn/object_type/retrieval_tier/metadata works).
    Pure and in-memory: never touches SQLite, never calls an embedding
    provider, never reads a question. Every object in `documents` gets a
    score — none are excluded (mirrors core.sqlservice.retrieval_documents'
    "every object is documented, only tiered" contract); tier is instead
    one of the scored components (COMPONENT_TIER_QUALITY) and the CALLER
    applies its own allowed-tier filter before ever consulting this result,
    exactly as it already does for semantic/lexical ranking.
    """
    fqn_lower_list: list[str] = []
    fqn_by_lower: dict[str, str] = {}
    object_type_by_fqn: dict[str, str] = {}
    tier_by_fqn: dict[str, int] = {}
    metadata_by_fqn: dict[str, Mapping[str, object]] = {}

    for doc in documents:
        key = doc.object_fqn.lower()
        fqn_lower_list.append(key)
        fqn_by_lower[key] = doc.object_fqn
        object_type_by_fqn[key] = doc.object_type
        tier_by_fqn[key] = doc.retrieval_tier
        metadata_by_fqn[key] = doc.metadata

    tokens_by_fqn = {
        fqn: _significant_name_tokens(fqn_by_lower[fqn], metadata_by_fqn[fqn].get("business_name"))
        for fqn in fqn_lower_list
    }
    object_name_tokens_by_fqn = {fqn: _object_name_only_tokens(fqn_by_lower[fqn]) for fqn in fqn_lower_list}
    neighbors_by_fqn = {fqn: _neighbor_fqns_lower(metadata_by_fqn[fqn]) for fqn in fqn_lower_list}

    # "Whether other views depend on the object" — count DISTINCT VIEW-typed
    # sources among this object's confirmed inbound edges via the
    # kind-tagged confirmed_relationship_edges key (Step 7); an older build
    # predating that key simply scores 0 on this component (graceful
    # degradation, never a crash).
    referencing_view_count_by_fqn: dict[str, int] = {}
    for fqn in fqn_lower_list:
        edges = metadata_by_fqn[fqn].get("confirmed_relationship_edges") or ()
        views: set[str] = set()
        for edge in edges:
            if edge.get("direction") != "inbound" or edge.get("kind") != "CATALOG_VIEW_DEPENDENCY":
                continue
            other = str(edge.get("other") or "")
            other_fqn, sep, _column = other.rpartition(".")
            if not sep:
                continue
            other_key = other_fqn.lower()
            if object_type_by_fqn.get(other_key) == "VIEW":
                views.add(other_key)
        referencing_view_count_by_fqn[fqn] = len(views)

    column_names_by_fqn = {
        fqn: frozenset(str(c) for c in (metadata_by_fqn[fqn].get("column_names") or ()))
        for fqn in fqn_lower_list
    }
    families, family_warnings = _compute_families(
        fqn_lower_list, tokens_by_fqn, neighbors_by_fqn, column_names_by_fqn, object_name_tokens_by_fqn,
    )
    family_members: dict[str, list[str]] = defaultdict(list)
    for fqn, family_key in families.items():
        family_members[family_key].append(fqn)

    scores_by_fqn: dict[str, float] = {}
    components_by_fqn: dict[str, Mapping[str, float]] = {}

    for fqn in fqn_lower_list:
        metadata = metadata_by_fqn[fqn]
        object_type = object_type_by_fqn[fqn]
        has_pk = bool(metadata.get("has_primary_key"))
        fk_count = len(metadata.get("foreign_key_targets") or ())
        confirmed_degree = len(metadata.get("confirmed_relationship_targets") or ()) + len(
            metadata.get("confirmed_relationship_sources") or ()
        )
        synonym_count = int(metadata.get("synonym_count") or 0)
        status_count = len(metadata.get("status_columns") or ())
        date_count = len(metadata.get("date_columns") or ())
        measure_count = len(metadata.get("measure_columns") or ())
        pk_count = len(metadata.get("primary_key_columns") or ())

        components = {
            COMPONENT_OBJECT_TYPE: _score_object_type(object_type),
            COMPONENT_PRIMARY_KEY: _score_primary_key(object_type, has_pk),
            COMPONENT_PHYSICAL_FK: _score_physical_fk(fk_count),
            COMPONENT_RELATIONSHIP_CONNECTIVITY: _score_relationship_connectivity(confirmed_degree),
            COMPONENT_REFERENCED_BY_VIEWS: _score_referenced_by_views(referencing_view_count_by_fqn[fqn]),
            COMPONENT_BUSINESS_DOCUMENTATION: _score_business_documentation(
                bool(metadata.get("has_business_name")), bool(metadata.get("has_description")),
            ),
            COMPONENT_SYNONYM_QUALITY: _score_synonym_quality(synonym_count),
            COMPONENT_BUSINESS_COLUMNS: _score_business_columns(status_count, date_count, measure_count, pk_count),
            COMPONENT_NAME_QUALITY: _score_name_quality(fqn_by_lower[fqn]),
            COMPONENT_TIER_QUALITY: _score_tier_quality(tier_by_fqn[fqn]),
        }
        total = sum(components[name] * weight for name, weight in COMPONENT_WEIGHTS.items())
        scores_by_fqn[fqn] = total
        components_by_fqn[fqn] = MappingProxyType(dict(components))

    scores_by_fqn_lower: dict[str, CanonicalObjectScore] = {}
    for family_key, members in family_members.items():
        ordered = sorted(members, key=lambda fqn: (-scores_by_fqn[fqn], fqn))
        for family_rank, fqn in enumerate(ordered, start=1):
            scores_by_fqn_lower[fqn] = CanonicalObjectScore(
                object_fqn=fqn_by_lower[fqn],
                total_score=scores_by_fqn[fqn],
                components=components_by_fqn[fqn],
                family_key=fqn_by_lower[family_key],
                family_size=len(members),
                family_rank=family_rank,
            )

    global_order = sorted(fqn_lower_list, key=lambda fqn: (-scores_by_fqn[fqn], fqn))
    rank_by_fqn_lower = {fqn: rank for rank, fqn in enumerate(global_order, start=1)}

    return CanonicalScoringResult(
        identity=identity,
        scores_by_fqn_lower=MappingProxyType(scores_by_fqn_lower),
        rank_by_fqn_lower=MappingProxyType(rank_by_fqn_lower),
        document_count=len(fqn_lower_list),
        family_warnings=family_warnings,
    )


# ── Bounded in-process cache (mirrors core.sqlservice.lexical_index's) ────

_MAX_CACHE_ENTRIES = 16
_canonical_cache: "dict[CanonicalScoringIdentity, CanonicalScoringResult]" = {}
_canonical_cache_order: list = []


def load_canonical_scores(
    documents: Sequence, identity: CanonicalScoringIdentity, *, use_cache: bool = True,
) -> CanonicalScoringResult:
    """
    Build (or reuse a cached) CanonicalScoringResult for `identity`. Never
    touches SQLite — `documents` must already be loaded. Cached ONLY under
    (build_id, embedding_provider, embedding_model, embedding_dimensions) —
    no question content is ever part of the cache key or the cached value
    (Step 8 requirement 8): this module never receives a question at all.
    """
    if use_cache and identity in _canonical_cache:
        _canonical_cache_order.remove(identity)
        _canonical_cache_order.append(identity)
        return _canonical_cache[identity]

    result = compute_canonical_scores(documents, identity)
    if use_cache:
        _canonical_cache[identity] = result
        _canonical_cache_order.append(identity)
        while len(_canonical_cache_order) > _MAX_CACHE_ENTRIES:
            oldest = _canonical_cache_order.pop(0)
            _canonical_cache.pop(oldest, None)
    return result


def clear_canonical_scoring_cache() -> None:
    """Explicitly evict every cached CanonicalScoringResult — for tests,
    and for future rebuild invalidation, mirroring
    clear_lexical_index_cache()/clear_vector_index_cache()."""
    _canonical_cache.clear()
    _canonical_cache_order.clear()


def canonical_scoring_cache_size() -> int:
    """Number of identities currently cached — exposed for tests."""
    return len(_canonical_cache)
