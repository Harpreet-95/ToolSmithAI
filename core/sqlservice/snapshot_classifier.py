"""
Generic dated-snapshot/archive detection -- Phase 7, Step 3J3.

Root cause this closes: core.sqlservice.retrieval_documents.
classify_retrieval_tier's own _has_dated_copy_signal only recognizes the
YYYY-MM-DD and DD-MonthName-YYYY token orderings -- a real, live object named
with the (equally common, US-style) MM-DD-YYYY ordering, e.g. tokens
["03","19","2018"], is never flagged there and stays Tier 1. This module
does NOT touch classify_retrieval_tier (Step 3J2 explicitly reserved changing
the global tier classifier for a separate, deliberate step) -- it is a
SEPARATE, additional, structural-comparison layer, computed once per build
from data this codebase already has (retrieval documents' own column/PK/FK
metadata plus core.sqlservice.schema_object_authority's already-computed
classification), consumed only by core.sqlservice.schema_frontier to keep a
confirmed historical snapshot out of the AI schema-selection frontier.

Never a name-pattern-alone decision (see this module's own docstring on
compute_snapshot_classification): a suffix that LOOKS like a date/version/
backup/copy token is only ever a precondition to look for a same-schema,
same-source, unsuffixed sibling object; classification always requires that
sibling to actually exist, and confidence is built from HOW similar the two
objects' own structures (columns, primary key, authority) actually are --
never from the name alone, and never from a cross-schema "sibling" (a same-
named object in a different schema is evidence of nothing -- confirmed live
against a real customer schema, where a suspicious-looking dated/backup-
suffixed table's only same-name match existed in an unrelated framework
schema, not its own).

No table/view/schema name from any specific customer's schema appears
anywhere in this file -- this module operates purely on generic tokenization,
structural similarity, and whatever core.sqlservice.schema_object_authority
already classified (see tests/test_sqlservice_snapshot_classifier.py's own
test_no_domain_specific_tokens_in_production_module guard, mirroring
core.sqlservice.schema_object_authority's identical guarantee).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.sqlservice.retrieval_documents import (
    _MONTH_NAME_TOKENS,
    _STALE_COPY_TOKENS,
    _is_day_num_token,
    _is_month_num_token,
    _is_year_token,
    _tokenize,
)
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_CANONICAL_TABLE,
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
    _authority_state,
)

__all__ = [
    "SNAPSHOT_CONTRACT_VERSION",
    "SNAPSHOT_CLASS_CONFIRMED",
    "SNAPSHOT_CLASS_UNCERTAIN",
    "SNAPSHOT_CLASS_NONE",
    "CONFIRMED_SNAPSHOT_MIN_CONFIDENCE",
    "UNCERTAIN_SNAPSHOT_MIN_CONFIDENCE",
    "SnapshotClassification",
    "SnapshotClassificationIdentity",
    "SnapshotClassificationResult",
    "compute_snapshot_classification",
    "load_snapshot_classification",
    "clear_snapshot_classification_cache",
    "snapshot_classification_cache_size",
]

SNAPSHOT_CONTRACT_VERSION = 1

SNAPSHOT_CLASS_CONFIRMED = "CONFIRMED_SNAPSHOT"
SNAPSHOT_CLASS_UNCERTAIN = "UNCERTAIN_SNAPSHOT"
SNAPSHOT_CLASS_NONE = "NOT_A_SNAPSHOT"

CONFIRMED_SNAPSHOT_MIN_CONFIDENCE = 0.75
UNCERTAIN_SNAPSHOT_MIN_CONFIDENCE = 0.4

_STRONG_AUTHORITY_CLASSES = frozenset({
    AUTHORITY_CLASS_CANONICAL_TABLE, AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
})
_MIN_STRUCTURAL_SIMILARITY = 0.3  # below this, a name match is treated as coincidental


@dataclass(frozen=True)
class SnapshotClassification:
    """One object's snapshot classification. `canonical_sibling_fqn`/
    `evidence` are always populated together with a non-NONE
    `classification` -- never left implicit. `evidence` is a bounded tuple
    of short, structured strings (never raw row data, never the sibling's
    full column list beyond what's needed to justify the score)."""

    object_fqn: str
    classification: str
    confidence: float
    canonical_sibling_fqn: str | None
    reason: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotClassificationIdentity:
    build_id: int
    source_id: int
    schema_fingerprint: str | None
    authority_state: tuple


@dataclass(frozen=True)
class SnapshotClassificationResult:
    identity: SnapshotClassificationIdentity
    classifications_by_fqn_lower: Mapping[str, SnapshotClassification]


def _suffix_token_is_suspicious(token: str) -> bool:
    return (
        token in _STALE_COPY_TOKENS
        or token in _MONTH_NAME_TOKENS
        or token in ("update", "updated", "new", "final", "v")
        or token.isdigit()
        or (len(token) >= 2 and token[0] == "v" and token[1:].isdigit())
    )


def _strip_suspicious_suffix(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Iteratively pops suspicious trailing tokens (see
    _suffix_token_is_suspicious) off `tokens`, one at a time, stopping at
    the first non-suspicious token from the end. Returns (base_tokens,
    suffix_tokens) -- base_tokens is never empty unless every token was
    suspicious (in which case there is no usable business-name prefix to
    match a sibling against, and the caller treats this as "no sibling")."""
    tokens_list = list(tokens)
    suffix: list[str] = []
    while tokens_list and _suffix_token_is_suspicious(tokens_list[-1]):
        suffix.insert(0, tokens_list.pop())
    return tuple(tokens_list), tuple(suffix)


def _has_plausible_date_or_version_signal(suffix_tokens: tuple[str, ...]) -> bool:
    """Narrower check on the STRIPPED SUFFIX ONLY (never the whole name):
    at least one token in the stripped suffix must look like part of an
    actual date/version/backup/copy marker (not just any trailing digit --
    a single lone trailing "2" is not enough evidence by itself, but it is
    still eligible to look for a sibling since it may combine with other
    signals; a bare business-identity number with no such marker anywhere
    in the run is excluded by requiring ONE of these more specific forms)."""
    if any(t in _STALE_COPY_TOKENS or t in _MONTH_NAME_TOKENS for t in suffix_tokens):
        return True
    digit_tokens = [t for t in suffix_tokens if t.isdigit()]
    if any(_is_year_token(t) for t in digit_tokens):
        return True
    if any(len(t) == 8 for t in digit_tokens):
        return True
    if len(digit_tokens) >= 2 and any(_is_month_num_token(t) for t in digit_tokens) and any(
        _is_day_num_token(t) for t in digit_tokens
    ):
        return True
    return False


def _column_similarity(candidate_columns: frozenset, sibling_columns: frozenset) -> float:
    if not candidate_columns and not sibling_columns:
        return 0.0
    union = candidate_columns | sibling_columns
    if not union:
        return 0.0
    return len(candidate_columns & sibling_columns) / len(union)


def compute_snapshot_classification(
    documents: Sequence, identity: SnapshotClassificationIdentity,
    authority_by_fqn_lower: Mapping[str, object],
) -> SnapshotClassificationResult:
    """
    Classifies every document as SNAPSHOT_CLASS_NONE, _UNCERTAIN, or
    _CONFIRMED. A name-suffix pattern is only ever a PRECONDITION to look
    for a sibling -- it is never, by itself, sufficient for any
    classification above SNAPSHOT_CLASS_NONE (confidence 0.0).

    For a candidate whose trailing tokens are suspicious (see
    _strip_suspicious_suffix) AND whose stripped suffix carries a genuine
    date/version/backup/copy marker (_has_plausible_date_or_version_signal):
      1. Look for a sibling: an object in the SAME schema (case-
         insensitive) whose own tokenized name equals the candidate's base
         tokens exactly. A same-NAME object in a DIFFERENT schema is never
         treated as a sibling (see this module's own docstring for the
         real cross-schema false-match this guards against).
      2. No sibling -> SNAPSHOT_CLASS_NONE, confidence 0.0. This exclusion
         is absolute -- nothing else below ever overrides it.
      3. Structural similarity (Jaccard over column-name sets) below
         _MIN_STRUCTURAL_SIMILARITY -> SNAPSHOT_CLASS_NONE (a coincidental
         name match against a structurally different object is not
         evidence of anything).
      4. The CANDIDATE's own authority classification already in
         _STRONG_AUTHORITY_CLASSES -> SNAPSHOT_CLASS_NONE (independently
         authoritative business evidence always wins over a suspicious
         name, regardless of any sibling).
      5. Otherwise, confidence is built from: structural similarity
         (scaled), the candidate lacking a primary key while the sibling
         has one, and the sibling's authority being strictly stronger than
         the candidate's. Thresholded into UNCERTAIN or CONFIRMED (see
         CONFIRMED_SNAPSHOT_MIN_CONFIDENCE/UNCERTAIN_SNAPSHOT_MIN_
         CONFIDENCE) -- never a hard binary from one signal alone.

    Deterministic: relies only on each document's own already-tokenized
    name, already-loaded columns/PK, and already-computed authority class
    -- never on document iteration order (a fresh, sorted-by-fqn scan is
    used to build the sibling lookup) or any AI/embedding call.
    """
    # Keyed on the CONCATENATED token string (never the raw token tuple):
    # the same logical business name can tokenize into a different number
    # of tokens depending purely on the original object's own casing/
    # underscore style (e.g. "MyTableSignups" -> ("my","table","signups")
    # via camelCase boundaries, but "Mytablesignups" -> ("mytablesignups"),
    # one token, since there is no internal case transition to split on) --
    # comparing the joined letter sequence collapses that purely stylistic
    # difference while still requiring an exact match, never a fuzzy one.
    by_schema_and_name: dict[tuple[str, str], object] = {}
    for doc in documents:
        schema = doc.object_fqn.split(".", 1)[0].lower() if "." in doc.object_fqn else ""
        name = doc.object_fqn.split(".", 1)[1] if "." in doc.object_fqn else doc.object_fqn
        name = name.strip("[]")
        tokens = tuple(_tokenize(name))
        by_schema_and_name[(schema, "".join(tokens))] = doc

    classifications: dict[str, SnapshotClassification] = {}
    for doc in sorted(documents, key=lambda d: d.object_fqn.lower()):
        key = doc.object_fqn.lower()
        schema = doc.object_fqn.split(".", 1)[0].lower() if "." in doc.object_fqn else ""
        name = doc.object_fqn.split(".", 1)[1] if "." in doc.object_fqn else doc.object_fqn
        name = name.strip("[]")
        tokens = tuple(_tokenize(name))

        base_tokens, suffix_tokens = _strip_suspicious_suffix(tokens)
        if not base_tokens or not suffix_tokens or not _has_plausible_date_or_version_signal(suffix_tokens):
            classifications[key] = SnapshotClassification(
                object_fqn=doc.object_fqn, classification=SNAPSHOT_CLASS_NONE, confidence=0.0,
                canonical_sibling_fqn=None, reason=None, evidence=(),
            )
            continue

        sibling_doc = by_schema_and_name.get((schema, "".join(base_tokens)))
        if sibling_doc is None or sibling_doc.object_fqn.lower() == key:
            classifications[key] = SnapshotClassification(
                object_fqn=doc.object_fqn, classification=SNAPSHOT_CLASS_NONE, confidence=0.0,
                canonical_sibling_fqn=None, reason="no_plausible_sibling", evidence=(),
            )
            continue

        candidate_authority = authority_by_fqn_lower.get(key)
        if candidate_authority is not None and getattr(candidate_authority, "authority_class", None) in _STRONG_AUTHORITY_CLASSES:
            classifications[key] = SnapshotClassification(
                object_fqn=doc.object_fqn, classification=SNAPSHOT_CLASS_NONE, confidence=0.0,
                canonical_sibling_fqn=None, reason="independently_authoritative", evidence=(),
            )
            continue

        candidate_columns = frozenset(c.lower() for c in (doc.metadata.get("column_names") or ()))
        sibling_columns = frozenset(c.lower() for c in (sibling_doc.metadata.get("column_names") or ()))
        similarity = _column_similarity(candidate_columns, sibling_columns)
        if similarity < _MIN_STRUCTURAL_SIMILARITY:
            classifications[key] = SnapshotClassification(
                object_fqn=doc.object_fqn, classification=SNAPSHOT_CLASS_NONE, confidence=0.0,
                canonical_sibling_fqn=None, reason="structurally_dissimilar_from_name_match", evidence=(),
            )
            continue

        # Weighting rationale (Part 2's own four named signals: suffix
        # pattern and sibling existence are already hard preconditions to
        # reach this point; structural similarity and weaker sibling
        # authority are scored here). A near-perfect structural match
        # alone tops out at 0.6 -- UNCERTAIN, never CONFIRMED by itself,
        # matching "confirmed only when multiple signals agree" -- it only
        # reaches CONFIRMED once corroborated by a real differentiating
        # signal (no PK where the sibling has one, and/or the sibling
        # carrying independently stronger authority than the candidate).
        confidence = 0.0
        evidence: list[str] = [f"column_similarity={similarity:.2f}"]
        confidence += similarity * 0.6

        candidate_pk = tuple(doc.metadata.get("primary_key_columns") or ())
        sibling_pk = tuple(sibling_doc.metadata.get("primary_key_columns") or ())
        if not candidate_pk and sibling_pk:
            confidence += 0.2
            evidence.append("candidate has no primary key while sibling does")

        sibling_authority = authority_by_fqn_lower.get(sibling_doc.object_fqn.lower())
        candidate_class = getattr(candidate_authority, "authority_class", None) if candidate_authority else None
        sibling_class = getattr(sibling_authority, "authority_class", None) if sibling_authority else None
        if sibling_class in _STRONG_AUTHORITY_CLASSES and candidate_class not in _STRONG_AUTHORITY_CLASSES:
            confidence += 0.2
            evidence.append(f"sibling authority={sibling_class} vs candidate authority={candidate_class or 'UNKNOWN'}")

        evidence.append(f"suffix_tokens={list(suffix_tokens)}")
        confidence = min(1.0, confidence)

        if confidence >= CONFIRMED_SNAPSHOT_MIN_CONFIDENCE:
            classification = SNAPSHOT_CLASS_CONFIRMED
            reason = "structurally_confirmed_snapshot"
        elif confidence >= UNCERTAIN_SNAPSHOT_MIN_CONFIDENCE:
            classification = SNAPSHOT_CLASS_UNCERTAIN
            reason = "uncertain_snapshot_candidate"
        else:
            classification = SNAPSHOT_CLASS_NONE
            reason = "insufficient_combined_evidence"

        classifications[key] = SnapshotClassification(
            object_fqn=doc.object_fqn, classification=classification, confidence=round(confidence, 4),
            canonical_sibling_fqn=(sibling_doc.object_fqn if classification != SNAPSHOT_CLASS_NONE else None),
            reason=reason, evidence=tuple(evidence) if classification != SNAPSHOT_CLASS_NONE else (),
        )

    return SnapshotClassificationResult(identity=identity, classifications_by_fqn_lower=classifications)


# ── Bounded in-process cache (mirrors core.sqlservice.schema_object_authority's) ──

_MAX_CACHE_ENTRIES = 16
_snapshot_cache: "dict[SnapshotClassificationIdentity, SnapshotClassificationResult]" = {}
_snapshot_cache_order: list = []


def load_snapshot_classification(
    documents: Sequence, build_id: int, source_id: int, schema_fingerprint: str | None,
    authority_by_fqn_lower: Mapping[str, object], *, use_cache: bool = True,
) -> SnapshotClassificationResult:
    """Build (or reuse a cached) SnapshotClassificationResult. Cached under
    (build_id, source_id, schema_fingerprint, authority_state) -- the SAME
    authority_state signature core.sqlservice.schema_object_authority uses,
    so this cache invalidates whenever authority evidence changes even
    without a new build. Never calls an AI/embedding provider -- purely a
    CPU-bound structural comparison over already-loaded documents/
    authority, so "caching" here is a performance nicety, not a cost
    guarantee (there is no cost to avoid in the first place)."""
    identity = SnapshotClassificationIdentity(
        build_id=build_id, source_id=source_id, schema_fingerprint=schema_fingerprint,
        authority_state=_authority_state(source_id),
    )
    if use_cache and identity in _snapshot_cache:
        _snapshot_cache_order.remove(identity)
        _snapshot_cache_order.append(identity)
        return _snapshot_cache[identity]

    result = compute_snapshot_classification(documents, identity, authority_by_fqn_lower)
    if use_cache:
        _snapshot_cache[identity] = result
        _snapshot_cache_order.append(identity)
        while len(_snapshot_cache_order) > _MAX_CACHE_ENTRIES:
            oldest = _snapshot_cache_order.pop(0)
            _snapshot_cache.pop(oldest, None)
    return result


def clear_snapshot_classification_cache() -> None:
    _snapshot_cache.clear()
    _snapshot_cache_order.clear()


def snapshot_classification_cache_size() -> int:
    return len(_snapshot_cache)
