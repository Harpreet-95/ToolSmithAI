"""
Deterministic column-selection and output-budget estimation — SQL
Architecture Correction, Phase 6, Step 4C1.

Root cause this module fixes: a real Step 4C run against build 7 (a
large batch of objects and columns) asked the provider for a full semantic
record for EVERY column, producing a very large (tens-of-thousands-of-characters) response that still
exceeded gpt-4o-mini's real output-token ceiling even at
max_output_tokens=16000 — increasing the timeout/token cap further could
not fix an output volume problem. The fix is to stop asking for what the
schema itself already answers: most columns are structurally obvious (a
real PRIMARY_KEY, a column whose name and normalized SQL type already agree,
...) and get zero enrichment; only a column that genuinely needs semantic
help is selected, and batches are sized against an estimate of the
resulting OUTPUT volume — not just input size — before any provider call.

Pure and deterministic: no I/O, no provider call, no database access. Reads
only core.sqlservice.retrieval_documents' metadata contract (date_columns/
measure_columns/status_columns/confirmed_relationship_edges — the same
fields core.sqlservice.semantic_enrichment_service already reads for other
purposes) and core.sqlservice.semantic_enrichment_service's own
ColumnSemanticInput — never document_text, never a row value.

Selection is a GENERIC, schema-shape-driven policy — no table name, column
name, or business domain is ever hardcoded; every rule is expressed over
normalized SQL types, structural PK/FK flags, deterministic name-hint
patterns, and dictionary presence/absence, so it applies identically to any
schema.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "REASON_NAME_TYPE_MISMATCH",
    "REASON_CLASSIFICATION_CONFLICT",
    "REASON_AMBIGUOUS_ROLE",
    "REASON_BUSINESS_KEY_CANDIDATE",
    "REASON_RELATIONSHIP_ROLE_UNCLEAR",
    "REASON_OPAQUE_NAME",
    "REASON_DICTIONARY_FLAGGED_INCOMPLETE",
    "SELECTION_REASON_PRIORITY",
    "ColumnSelectionLimits",
    "SelectedColumn",
    "select_columns_for_enrichment",
    "OutputEstimateLimits",
    "estimate_object_output_chars",
    "estimate_column_output_chars",
    "estimate_batch_output_tokens",
]

# ── Selection reason vocabulary (stable strings, never free text) ────────

REASON_NAME_TYPE_MISMATCH = "NAME_TYPE_MISMATCH"
REASON_CLASSIFICATION_CONFLICT = "CLASSIFICATION_CONFLICT"
REASON_AMBIGUOUS_ROLE = "AMBIGUOUS_ROLE"
REASON_BUSINESS_KEY_CANDIDATE = "BUSINESS_KEY_CANDIDATE"
REASON_RELATIONSHIP_ROLE_UNCLEAR = "RELATIONSHIP_ROLE_UNCLEAR"
REASON_OPAQUE_NAME = "OPAQUE_NAME"
REASON_DICTIONARY_FLAGGED_INCOMPLETE = "DICTIONARY_FLAGGED_INCOMPLETE"

# Highest-priority reason first: when a per-object/per-batch cap forces a
# choice, columns are kept in this tier order (structural ambiguity/conflict
# outranks a merely-opaque name) — see `select_columns_for_enrichment`'s own
# docstring. A column matching multiple reasons is kept at its HIGHEST tier.
SELECTION_REASON_PRIORITY: tuple[str, ...] = (
    REASON_NAME_TYPE_MISMATCH,
    REASON_CLASSIFICATION_CONFLICT,
    REASON_AMBIGUOUS_ROLE,
    REASON_BUSINESS_KEY_CANDIDATE,
    REASON_DICTIONARY_FLAGGED_INCOMPLETE,
    REASON_RELATIONSHIP_ROLE_UNCLEAR,
    REASON_OPAQUE_NAME,
)

# Generic snake_case/CamelCase/digit-boundary tokenizer — same shape as
# core.sqlservice.semantic_catalog._RAW_SPLIT_PATTERN, duplicated locally
# per that module's own "small enough to duplicate" precedent.
_TOKEN_SPLIT_PATTERN = re.compile(
    r"[_\-\s./\\,;:!?()\[\]{}'\"]+"
    r"|(?<=[a-z0-9])(?=[A-Z])"
    r"|(?<=[A-Za-z])(?=[0-9])"
    r"|(?<=[0-9])(?=[A-Za-z])",
)

_VOWELS = frozenset("aeiou")


def _tokens(name: str) -> tuple[str, ...]:
    if not name:
        return ()
    return tuple(p.lower() for p in _TOKEN_SPLIT_PATTERN.split(name) if p)


# ── Generic, schema-shape-driven name-hint vocabularies ───────────────────
#
# Deliberately generic English business-vocabulary TOKENS (never a table- or
# column-specific literal), matched against `_tokens()`'s own output rather
# than a raw-string regex — a raw-string boundary regex (requiring "_" or
# start/end around each word) silently misses a plain CamelCase name with
# no underscores at all (e.g. "CreatedDate" has no "_" separating "Created"
# from "Date", so a "(^|_)...(_|$)" pattern never matches either word
# inside it); token-based matching handles snake_case AND CamelCase
# identically. Mirrors core.sqlservice.retrieval_documents' own
# _STATUS_NAME_PATTERN/_MEASURE_NAME_PATTERN intent and core.sqlservice.
# semantic_catalog's own _looks_like_flag_name convention, duplicated
# locally rather than imported (both modules document that same
# "small enough to duplicate" precedent).
_DATE_HINT_TOKENS = frozenset({
    "date", "dt", "time", "timestamp", "created", "updated", "started",
    "completed", "effective",
})
_MEASURE_HINT_TOKENS = frozenset({
    "amount", "amt", "total", "qty", "quantity", "count", "price", "cost",
    "balance", "revenue", "score", "rate", "percent", "pct", "sum", "value",
    "val", "fee", "charge", "payment",
})
_STATUS_HINT_LEAD_TOKENS = frozenset({"is", "has"})
_STATUS_HINT_TAIL_TOKEN = "flag"
_STATUS_HINT_TOKENS = frozenset({"status", "active", "enabled", "disabled", "deleted"})
_IDENTIFIER_HINT_TAIL_TOKENS = frozenset({"id", "no", "num", "code", "key", "ref"})


def _has_date_hint(name: str) -> bool:
    return any(tok in _DATE_HINT_TOKENS or tok.startswith("expir") for tok in _tokens(name))


def _has_measure_hint(name: str) -> bool:
    return any(tok in _MEASURE_HINT_TOKENS for tok in _tokens(name))


def _has_status_hint(name: str) -> bool:
    tokens = _tokens(name)
    if not tokens:
        return False
    return (
        tokens[0] in _STATUS_HINT_LEAD_TOKENS
        or tokens[-1] == _STATUS_HINT_TAIL_TOKEN
        or any(tok in _STATUS_HINT_TOKENS for tok in tokens)
    )


def _has_identifier_hint(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens) and tokens[-1] in _IDENTIFIER_HINT_TAIL_TOKENS


_DATE_STRUCTURALLY_COMPATIBLE_TYPES = frozenset({"OTHER", "DATETIME"})
_MEASURE_STRUCTURALLY_COMPATIBLE_TYPES = frozenset({"OTHER", "INTEGER", "DECIMAL"})


def _is_opaque_name(name: str) -> bool:
    """Conservative, generic "looks like a code, not a word" heuristic:
    every token is short (<=3 characters) or consonant-only (no vowel) —
    e.g. "F1", "Cd", "Txn", "EDU". Never a table/column-specific literal."""
    tokens = _tokens(name)
    if not tokens:
        return False
    return all(len(tok) <= 3 or not any(ch in _VOWELS for ch in tok) for tok in tokens)


def _str_set(metadata: Mapping[str, object], key: str) -> frozenset[str]:
    value = metadata.get(key) or ()
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(str(v).lower() for v in value if isinstance(v, str) and v.strip())


def _outbound_fk_targets_by_column_lower(metadata: Mapping[str, object]) -> Mapping[str, str]:
    targets: dict[str, str] = {}
    for edge in metadata.get("confirmed_relationship_edges") or ():
        if not isinstance(edge, Mapping) or edge.get("direction") != "outbound":
            continue
        column = edge.get("column")
        other = edge.get("other")
        if isinstance(column, str) and column and isinstance(other, str) and other:
            targets[column.lower()] = other
    return targets


def _relationship_role_unclear(column_name: str, other_object_fqn: str) -> bool:
    """True when the FK column's own name shares no meaningful token with
    the object it references — a generic identifier (a bare "RefID",
    "ParentID", or "FK1") gives no hint of the relationship's BUSINESS
    purpose, unlike a self-descriptive name ("StudentID" -> dbo.Student).
    Generic/structural tokens (id/no/num/code/key/ref/fk) are excluded from
    the overlap check on both sides so they never count as a "match" by
    themselves."""
    _GENERIC_TOKENS = frozenset({"id", "no", "num", "code", "key", "ref", "fk"})
    column_tokens = set(_tokens(column_name)) - _GENERIC_TOKENS
    other_simple_name = other_object_fqn.split(".")[-1]
    other_tokens = set(_tokens(other_simple_name)) - _GENERIC_TOKENS
    if not column_tokens or not other_tokens:
        return True
    return column_tokens.isdisjoint(other_tokens)


@dataclass(frozen=True)
class ColumnSelectionLimits:
    """Bounds applied by `select_columns_for_enrichment` — every field is a
    caller-supplied configuration value (core.config's own
    SQL_SEMANTIC_ENRICHMENT_MAX_* constants); this module never reads
    core.config directly, keeping it independently testable."""

    max_selected_columns_per_object: int
    max_opaque_candidates_per_object: int
    max_relationship_context_columns_per_object: int


@dataclass(frozen=True)
class SelectedColumn:
    """One column chosen for model enrichment, with every deterministic
    reason it was chosen (a column can match more than one condition)."""

    column_name: str
    reasons: tuple[str, ...]


def select_columns_for_enrichment(
    columns: Sequence,  # Sequence[core.sqlservice.semantic_enrichment_service.ColumnSemanticInput]
    *,
    metadata: Mapping[str, object],
    dictionary_business_name: str | None,
    dictionary_description: str | None,
    limits: ColumnSelectionLimits,
) -> tuple[SelectedColumn, ...]:
    """
    Select the subset of `columns` that genuinely needs model semantic
    assistance — never a column merely because it exists, and never a
    column whose role is already structurally clear (a real PRIMARY_KEY, a
    column whose name and type already agree, ...) unless it also matches
    one of the seven generic conditions below.

    A column is selected when AT LEAST ONE holds:
      1. NAME_TYPE_MISMATCH: a date/measure name-hint pattern matches but
         the column's real normalized SQL type contradicts it.
      2. CLASSIFICATION_CONFLICT: more than one of {date, measure, status}
         name-hint patterns match the same column simultaneously.
      3. AMBIGUOUS_ROLE: retrieval_documents' own deterministic classifier
         already flagged this column as date/measure/status (metadata's
         date_columns/measure_columns/status_columns), but its real SQL
         type is unknown ("OTHER") — the classification can't be
         structurally confirmed.
      4. BUSINESS_KEY_CANDIDATE: not a real PK/FK, but its name matches a
         generic identifier-like pattern (ends in id/no/num/code/key/ref).
      5. RELATIONSHIP_ROLE_UNCLEAR: a real FK column whose name shares no
         token with the object it references (see
         `_relationship_role_unclear`).
      6. OPAQUE_NAME: the name reduces to short/consonant-only tokens after
         normalization (see `_is_opaque_name`) — e.g. "F1", "Cd", "Txn".
      7. DICTIONARY_FLAGGED_INCOMPLETE: the dictionary curated a
         business_name for this OBJECT (signaling it was deemed important)
         but left its description absent, and this column is a real PK or
         FK — the object was flagged important, but not completely
         documented, and this column is part of its identity.

    Bounded, deterministic, and priority-ordered (see
    SELECTION_REASON_PRIORITY): when `limits.max_selected_columns_per_object`
    would otherwise be exceeded, columns are kept in reason-priority order
    (ties broken by column_name), so the SAME input always produces the
    SAME selected set regardless of dict/set iteration order. Opaque-name-
    only and relationship-context-only matches are additionally capped by
    their own dedicated limits before the overall cap is applied.
    """
    date_cols = _str_set(metadata, "date_columns")
    measure_cols = _str_set(metadata, "measure_columns")
    status_cols = _str_set(metadata, "status_columns")
    fk_targets = _outbound_fk_targets_by_column_lower(metadata)

    reasons_by_name: dict[str, set[str]] = {}

    def _add(column_name: str, reason: str) -> None:
        reasons_by_name.setdefault(column_name, set()).add(reason)

    for col in columns:
        name = col.column_name
        name_l = name.lower()

        date_hint = _has_date_hint(name)
        measure_hint = _has_measure_hint(name)
        status_hint = _has_status_hint(name)
        hint_count = sum((date_hint, measure_hint, status_hint))

        if date_hint and col.normalized_sql_type not in _DATE_STRUCTURALLY_COMPATIBLE_TYPES:
            _add(name, REASON_NAME_TYPE_MISMATCH)
        if (
            measure_hint and not col.is_primary_key
            and col.normalized_sql_type not in _MEASURE_STRUCTURALLY_COMPATIBLE_TYPES
        ):
            _add(name, REASON_NAME_TYPE_MISMATCH)
        if hint_count > 1:
            _add(name, REASON_CLASSIFICATION_CONFLICT)
        if name_l in (date_cols | measure_cols | status_cols) and col.normalized_sql_type == "OTHER":
            _add(name, REASON_AMBIGUOUS_ROLE)
        if not col.is_primary_key and not col.is_foreign_key and _has_identifier_hint(name):
            _add(name, REASON_BUSINESS_KEY_CANDIDATE)
        if col.is_foreign_key:
            other = fk_targets.get(name_l)
            if other is not None and _relationship_role_unclear(name, other):
                _add(name, REASON_RELATIONSHIP_ROLE_UNCLEAR)
        if _is_opaque_name(name):
            _add(name, REASON_OPAQUE_NAME)
        if (
            dictionary_business_name is not None and dictionary_description is None
            and (col.is_primary_key or col.is_foreign_key)
        ):
            _add(name, REASON_DICTIONARY_FLAGGED_INCOMPLETE)

    def _tier(reasons: set[str]) -> int:
        for i, r in enumerate(SELECTION_REASON_PRIORITY):
            if r in reasons:
                return i
        return len(SELECTION_REASON_PRIORITY)

    candidates = sorted(reasons_by_name.items(), key=lambda kv: (_tier(kv[1]), kv[0].lower()))

    selected: list[SelectedColumn] = []
    opaque_only_count = 0
    relationship_only_count = 0
    for column_name, reason_set in candidates:
        if len(selected) >= limits.max_selected_columns_per_object:
            break
        is_opaque_only = reason_set == {REASON_OPAQUE_NAME}
        is_relationship_only = reason_set == {REASON_RELATIONSHIP_ROLE_UNCLEAR}
        if is_opaque_only and opaque_only_count >= limits.max_opaque_candidates_per_object:
            continue
        if is_relationship_only and relationship_only_count >= limits.max_relationship_context_columns_per_object:
            continue
        ordered_reasons = tuple(r for r in SELECTION_REASON_PRIORITY if r in reason_set)
        selected.append(SelectedColumn(column_name=column_name, reasons=ordered_reasons))
        if is_opaque_only:
            opaque_only_count += 1
        if is_relationship_only:
            relationship_only_count += 1

    return tuple(sorted(selected, key=lambda s: s.column_name.lower()))


def reduce_selected_columns(
    selected: Sequence[SelectedColumn], target_count: int,
) -> tuple[SelectedColumn, ...]:
    """
    Shrink an already-selected column list to at most `target_count`,
    dropping the LOWEST-priority-reason columns first (see
    SELECTION_REASON_PRIORITY) — used only when a single object's full
    selected set still can't fit an output-budget-aware batch alone (see
    core.sqlservice.semantic_enrichment_service's batching). Never drops a
    column with a higher-tier reason while a lower-tier one is kept.
    """
    if target_count <= 0:
        return ()
    if len(selected) <= target_count:
        return tuple(selected)

    def _best_tier(item: SelectedColumn) -> int:
        for i, r in enumerate(SELECTION_REASON_PRIORITY):
            if r in item.reasons:
                return i
        return len(SELECTION_REASON_PRIORITY)

    ranked = sorted(selected, key=lambda s: (_best_tier(s), s.column_name.lower()))
    kept = ranked[:target_count]
    return tuple(sorted(kept, key=lambda s: s.column_name.lower()))


# ── Output-budget estimation (conservative, field-length-based) ──────────
#
# Deliberately NOT based on an average characters-per-token ratio applied to
# the WHOLE estimate: the estimate is built first from the maximum allowed
# size of every field a compact object/column record can contain (the
# actual worst case the strict response validator would ever accept), and
# only the final chars->tokens conversion uses a single, documented,
# conservative ratio (lower than typical English-prose chars/token, because
# short quoted JSON tokens/punctuation/enum values tokenize less
# efficiently than prose) -- see `estimate_batch_output_tokens`.

# Fixed JSON punctuation/key-name overhead for one object record's own keys
# ("object_fqn","business_name","description","object_role",
# "entity_concepts","aliases","supported_operations","confidence","columns")
# plus their surrounding braces/commas/quotes -- measured generously, not
# tuned to any one schema.
_OBJECT_JSON_OVERHEAD_CHARS = 220
# Fixed overhead for one column record's own keys ("column_name",
# "business_label","semantic_role","date_role","measure_role",
# "dimension_role","identifier_role","confidence") plus punctuation.
_COLUMN_JSON_OVERHEAD_CHARS = 170
# Longest possible enum token text (role enums are all well under this).
_MAX_ENUM_TOKEN_CHARS = 15
_MAX_CONFIDENCE_CHARS = 5  # e.g. "0.42,"


@dataclass(frozen=True)
class OutputEstimateLimits:
    """The same field-size ceilings the strict response validator enforces
    (core.sqlservice.semantic_enrichment_service's own
    SQL_SEMANTIC_ENRICHMENT_MAX_* constants) — the estimator's only
    inputs, so it can never silently drift out of sync with what a
    response is actually allowed to contain."""

    max_business_name_characters: int
    max_description_characters: int
    max_concepts_per_object: int
    max_concept_characters: int
    max_aliases_per_object: int
    max_alias_characters: int
    max_operations_per_object: int
    max_business_label_characters: int


def estimate_object_output_chars(object_fqn: str, limits: OutputEstimateLimits) -> int:
    """Worst-case character count for ONE compact object-level record,
    excluding its columns array (added separately per selected column)."""
    return (
        _OBJECT_JSON_OVERHEAD_CHARS
        + len(object_fqn)
        + limits.max_business_name_characters
        + limits.max_description_characters
        + limits.max_concepts_per_object * (limits.max_concept_characters + 4)
        + limits.max_aliases_per_object * (limits.max_alias_characters + 4)
        + limits.max_operations_per_object * (_MAX_ENUM_TOKEN_CHARS + 3)
        + _MAX_ENUM_TOKEN_CHARS  # object_role
        + _MAX_CONFIDENCE_CHARS
    )


def estimate_column_output_chars(column_name: str, limits: OutputEstimateLimits) -> int:
    """Worst-case character count for ONE compact selected-column record."""
    return (
        _COLUMN_JSON_OVERHEAD_CHARS
        + len(column_name)
        + limits.max_business_label_characters
        + 4 * _MAX_ENUM_TOKEN_CHARS  # semantic_role + date_role + measure_role + dimension_role (one non-null)
        + _MAX_ENUM_TOKEN_CHARS  # identifier_role
        + _MAX_CONFIDENCE_CHARS
    )


def estimate_batch_output_tokens(
    object_fqns_with_selected_counts: Sequence[tuple[str, int, tuple[str, ...]]],
    limits: OutputEstimateLimits,
    *,
    conservative_chars_per_token: float,
) -> int:
    """
    Conservative worst-case output TOKEN estimate for a candidate batch.
    `object_fqns_with_selected_counts` is a sequence of
    (object_fqn, selected_column_count, selected_column_names) — the names
    are used for their actual (real, known) length; only the semantic
    FIELDS of each record are estimated at their configured maximum, never
    guessed.

    The chars -> tokens conversion uses one documented, conservative ratio
    (deliberately LOWER than typical English-prose chars/token, since a
    JSON document's quotes/braces/short enum tokens split into more model
    tokens per character than prose) — this is the ONLY place an
    average-based assumption is used, and only as the final unit
    conversion, never as the basis for the character estimate itself.
    """
    total_chars = 0
    for object_fqn, _selected_count, selected_names in object_fqns_with_selected_counts:
        total_chars += estimate_object_output_chars(object_fqn, limits)
        for column_name in selected_names:
            total_chars += estimate_column_output_chars(column_name, limits)

    if conservative_chars_per_token <= 0:
        raise ValueError("conservative_chars_per_token must be positive")
    return math.ceil(total_chars / conservative_chars_per_token)
