"""
Deterministic INPUT-column bounding and compact-output-token estimation for
the OBJECT_CORE enrichment phase -- SQL Architecture Correction, Phase 6,
Step 4C2.

Distinct from core.sqlservice.semantic_enrichment_selection (Step 4C1),
which selects which columns need model OUTPUT (semantic role assistance).
This module instead bounds which columns are shown to the model as INPUT
CONTEXT for one compact object-level record -- OBJECT_CORE never asks for
(or accepts) a per-column response at all, so there is no "output
selection" concept here, only "how many/which columns are worth the input
tokens to describe this object."

Pure and deterministic: no I/O, no provider call, no database access, and
no dependency on core.sqlservice.retrieval_documents' metadata mapping (or
any other module) -- every heuristic here reads ONLY a column's own name/
normalized SQL type/PK/FK flags (core.sqlservice.semantic_enrichment_
service.ColumnSemanticInput), so it can never diverge from what's actually
sent to a provider and stays trivially unit-testable in isolation.

Selection is a GENERIC, schema-shape-driven policy -- no table name, column
name, or business domain is ever hardcoded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "ObjectCoreInputSelectionLimits",
    "select_informative_input_columns",
    "ObjectCoreOutputEstimateLimits",
    "estimate_object_core_output_chars",
    "estimate_object_core_batch_output_tokens",
    "local_id_for_index",
]

# Generic snake_case/CamelCase/digit-boundary tokenizer -- same shape as
# core.sqlservice.semantic_enrichment_selection._TOKEN_SPLIT_PATTERN,
# duplicated locally per that module's own documented "small enough to
# duplicate" precedent.
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


_IDENTIFIER_HINT_TAIL_TOKENS = frozenset({"id", "no", "num", "code", "key", "ref"})
_DATE_HINT_TOKENS = frozenset({
    "date", "dt", "time", "timestamp", "created", "updated", "started", "completed", "effective",
})
_MEASURE_HINT_TOKENS = frozenset({
    "amount", "amt", "total", "qty", "quantity", "count", "price", "cost",
    "balance", "revenue", "score", "rate", "percent", "pct", "sum", "value", "val", "fee", "charge", "payment",
})
_STATUS_HINT_LEAD_TOKENS = frozenset({"is", "has"})
_STATUS_HINT_TAIL_TOKEN = "flag"
_STATUS_HINT_TOKENS = frozenset({"status", "active", "enabled", "disabled", "deleted"})


def _has_identifier_hint(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens) and tokens[-1] in _IDENTIFIER_HINT_TAIL_TOKENS


def _has_date_or_measure_or_status_hint(name: str) -> bool:
    tokens = _tokens(name)
    if not tokens:
        return False
    if any(tok in _DATE_HINT_TOKENS or tok.startswith("expir") for tok in tokens):
        return True
    if any(tok in _MEASURE_HINT_TOKENS for tok in tokens):
        return True
    if tokens[0] in _STATUS_HINT_LEAD_TOKENS or tokens[-1] == _STATUS_HINT_TAIL_TOKEN:
        return True
    return any(tok in _STATUS_HINT_TOKENS for tok in tokens)


def _is_opaque_name(name: str) -> bool:
    """Conservative "looks like a code, not a word" heuristic: every token is
    short (<=3 characters) or consonant-only. Mirrors core.sqlservice.
    semantic_enrichment_selection._is_opaque_name exactly (duplicated
    locally, same "small enough to duplicate" precedent)."""
    tokens = _tokens(name)
    if not tokens:
        return False
    return all(len(tok) <= 3 or not any(ch in _VOWELS for ch in tok) for tok in tokens)


# ── Part 2: informative input-column selection ────────────────────────────


@dataclass(frozen=True)
class ObjectCoreInputSelectionLimits:
    """Bounds applied by `select_informative_input_columns` -- a caller-
    supplied configuration value (core.config's own
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_INPUT_COLUMNS_PER_OBJECT); this
    module never reads core.config directly, keeping it independently
    testable."""

    max_input_columns_per_object: int


def select_informative_input_columns(
    columns: Sequence,  # Sequence[core.sqlservice.semantic_enrichment_service.ColumnSemanticInput]
    *,
    limits: ObjectCoreInputSelectionLimits,
) -> tuple:
    """
    Bound `columns` down to a small, deterministic INPUT summary for one
    object's OBJECT_CORE prompt block -- CONTEXT only; the model never
    echoes a column record back (see this module's own docstring). When the
    object already has at most `limits.max_input_columns_per_object`
    columns, every column is kept (already small; no need to drop
    anything).

    Otherwise, columns are kept in PRIORITY-TIER order (ties broken by
    column_name, case-insensitively) until the limit is reached:
      0. Real PRIMARY_KEY columns -- an object's own identity.
      1. Real FOREIGN_KEY columns -- its confirmed relationships.
      2. BUSINESS_KEY-shaped names (not a real PK/FK, but name-hinted as an
         identifier: ends in id/no/num/code/key/ref).
      3. MEASURE/DATE/STATUS name-hinted columns.
      4. OPAQUE-looking names (short/consonant-only tokens) -- the columns
         most likely to need a human/model gloss.
      5. Everything else, in original (already-alphabetical) order.

    A column matching more than one tier is kept at its HIGHEST (lowest-
    numbered) tier. Deterministic: the SAME input always produces the SAME
    selected set regardless of dict/set iteration order.
    """
    columns = tuple(columns)
    if len(columns) <= limits.max_input_columns_per_object:
        return columns

    def _tier(col) -> int:
        if col.is_primary_key:
            return 0
        if col.is_foreign_key:
            return 1
        if _has_identifier_hint(col.column_name):
            return 2
        if _has_date_or_measure_or_status_hint(col.column_name):
            return 3
        if _is_opaque_name(col.column_name):
            return 4
        return 5

    ranked = sorted(columns, key=lambda c: (_tier(c), c.column_name.lower()))
    kept = ranked[: limits.max_input_columns_per_object]
    return tuple(sorted(kept, key=lambda c: c.column_name.lower()))


# ── Part 3: compact output-token estimation ───────────────────────────────
#
# Same "worst-case field-length, single conservative chars/token conversion
# only at the very end" methodology as core.sqlservice.
# semantic_enrichment_selection's own OutputEstimateLimits/
# estimate_object_output_chars -- see that module's docstring for the full
# rationale. OBJECT_CORE never produces a "columns" array at all, so there
# is no per-column output cost to add here (unlike the Step 4C1 estimator).

# Fixed JSON punctuation/key-name overhead for one compact object record's
# own keys ("id","label","role","concepts","aliases","description",
# "confidence") plus surrounding braces/commas/quotes -- measured
# generously, not tuned to any one schema.
_OBJECT_CORE_JSON_OVERHEAD_CHARS = 110
# Longest possible enum token text (object-role enums are all well under
# this -- "CONFIGURATION" is the longest at 13 characters).
_MAX_ENUM_TOKEN_CHARS = 15
_MAX_CONFIDENCE_CHARS = 5  # e.g. "0.42,"
# A local id is always short ("o1".."o9999") -- bounded generously.
_MAX_LOCAL_ID_CHARS = 8


@dataclass(frozen=True)
class ObjectCoreOutputEstimateLimits:
    """The same field-size ceilings the OBJECT_CORE strict response
    validator enforces (core.config's own
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_* constants) -- the estimator's
    only inputs, so it can never silently drift out of sync with what a
    response is actually allowed to contain."""

    max_label_characters: int
    max_description_characters: int
    max_concepts: int
    max_concept_characters: int
    max_aliases: int
    max_alias_characters: int


def estimate_object_core_output_chars(local_id: str, limits: ObjectCoreOutputEstimateLimits) -> int:
    """Worst-case character count for ONE compact OBJECT_CORE record. Uses
    `local_id`'s REAL (known) length rather than guessing -- it is the only
    field whose actual value is already fixed at estimation time."""
    return (
        _OBJECT_CORE_JSON_OVERHEAD_CHARS
        + max(len(local_id), _MAX_LOCAL_ID_CHARS)
        + limits.max_label_characters
        + limits.max_description_characters
        + limits.max_concepts * (limits.max_concept_characters + 4)
        + limits.max_aliases * (limits.max_alias_characters + 4)
        + _MAX_ENUM_TOKEN_CHARS  # role
        + _MAX_CONFIDENCE_CHARS
    )


def estimate_object_core_batch_output_tokens(
    local_ids: Sequence[str],
    limits: ObjectCoreOutputEstimateLimits,
    *,
    conservative_chars_per_token: float,
) -> int:
    """
    Conservative worst-case output TOKEN estimate for a candidate OBJECT_CORE
    batch. The chars -> tokens conversion uses one documented, conservative
    ratio (see core.sqlservice.semantic_enrichment_selection.
    estimate_batch_output_tokens's own docstring for why this is only ever
    the FINAL unit-conversion step, never the basis of the character
    estimate itself).
    """
    total_chars = sum(estimate_object_core_output_chars(lid, limits) for lid in local_ids)
    if conservative_chars_per_token <= 0:
        raise ValueError("conservative_chars_per_token must be positive")
    return math.ceil(total_chars / conservative_chars_per_token)


def local_id_for_index(index: int) -> str:
    """Compact local id for the `index`-th (0-based) object of ONE batch --
    "o1", "o2", .... Scoped to a single batch (never global) -- exactly like
    the existing contract's own `object_fqn` match key is only ever unique
    WITHIN one batch's response, never across batches (see
    core.sqlservice.semantic_enrichment_service._validate_batch_response)."""
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index!r}")
    return f"o{index + 1}"
