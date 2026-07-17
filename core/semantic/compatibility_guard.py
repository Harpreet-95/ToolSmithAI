from __future__ import annotations

from dataclasses import dataclass, field

from core.dictionary.rule_classifier import _tokenize
from core.profiling.classification.column_typer import (
    _AMOUNT_TOKENS,
    _COUNT_TOKENS,
    _ID_ANYWHERE,
    _PHONE_TOKENS,
    _SSN_TOKENS,
    _STATUS_TOKENS,
    _CODE_TOKENS,
)

# ---------------------------------------------------------------------------
# Semantic Correctness Guard (Milestone Phase 6.1)
#
# Prevents SQL generation from combining a requested business concept with a
# resolved column whose OWN metadata says it belongs to a different concept
# family (money vs. a date, a phone number vs. a salary, etc.). Reuses the
# vocabulary already shipped in core/profiling/classification/column_typer.py
# (the same tokens that classify a COLUMN'S semantic type) to classify a
# question TERM into the same family space, and compares that against the
# column's own already-computed profiling.semantic_type. No new metadata is
# read or invented — every signal here already exists in
# data.business_knowledge_service's table/column context.
#
# Deliberately conservative: a term or column with no confident family
# signal returns/compares as None ("no opinion") rather than ever blocking
# on a guess. This only ever ADDS a refusal condition on top of the existing
# score+ambiguity-margin auto-select gate in query_planning_service — it
# never loosens or replaces that gate.
# ---------------------------------------------------------------------------

# A bare calendar/temporal vocabulary — there is no existing DATE token set
# to reuse (column_typer's DATE scorer works off data_type/date_string_rate,
# not column-name tokens), so this is the one small, generic, non-CCPP-
# specific addition needed to classify a question TERM (not a column) as
# temporal. English calendar nouns only — never a business-domain word.
_DATE_TERM_TOKENS = frozenset({
    "year", "years", "month", "months", "week", "weeks",
    "day", "days", "quarter", "quarters", "date", "dates", "time",
})

_EMAIL_TERM_TOKENS = frozenset({"email", "emails"})

# Term-family classification, in priority order (first match wins) — mirrors
# the same priority-by-specificity idea column_typer._SCORERS already uses.
_TERM_FAMILY_TOKENS: tuple[tuple[str, frozenset[str]], ...] = (
    ("EMAIL", _EMAIL_TERM_TOKENS),
    ("PHONE", _PHONE_TOKENS),
    ("SSN", _SSN_TOKENS),
    ("ID", _ID_ANYWHERE),
    ("AMOUNT", _AMOUNT_TOKENS),
    ("COUNT", _COUNT_TOKENS),
    ("DATE", _DATE_TERM_TOKENS),
    ("STATUS", _STATUS_TOKENS),
    ("CODE", _CODE_TOKENS),
)

# Semantic-type families — two families are only INcompatible when both
# sides are known and fall in different groups. BINARY/TEXT/NAME/FLAG/
# UNKNOWN/None carry no strong opinion and never block.
_FAMILY_GROUPS: dict[str, str] = {
    "AMOUNT": "MONEY",
    "COUNT": "QUANTITY",
    "DATE": "TEMPORAL",
    "EMAIL": "CONTACT",
    "PHONE": "CONTACT",
    "SSN": "IDENTITY",
    "ID": "IDENTITY",
    "STATUS": "CATEGORICAL",
    "CODE": "CATEGORICAL",
}


def infer_term_family(term: str) -> str | None:
    """Classify a question term (e.g. "revenue", "year", "phone") into a
    coarse semantic family, using the same token vocabulary column_typer.py
    already uses to classify columns. Returns None for ordinary business
    nouns ("clients", "students") that carry no family signal — silence is
    the correct answer for most terms.
    """
    toks = set(_tokenize(term))
    if not toks:
        return None
    for family, vocab in _TERM_FAMILY_TOKENS:
        if toks & vocab:
            return family
    return None


@dataclass
class CompatibilityResult:
    compatible: bool
    term_family: str | None
    column_family: str | None
    reason: str | None = None
    suggested: dict | None = field(default=None)


def check_compatibility(
    term: str,
    term_family: str | None,
    column_semantic_type: str | None,
) -> CompatibilityResult:
    """Compare a question term's inferred family against the resolved
    column's own profiling.semantic_type. Only reports incompatible when
    BOTH sides carry a known, different-group family — never on a guess.
    """
    if not term_family or not column_semantic_type:
        return CompatibilityResult(True, term_family, column_semantic_type)

    term_group = _FAMILY_GROUPS.get(term_family)
    column_group = _FAMILY_GROUPS.get(column_semantic_type)
    if not term_group or not column_group or term_group == column_group:
        return CompatibilityResult(True, term_family, column_semantic_type)

    return CompatibilityResult(
        compatible=False,
        term_family=term_family,
        column_family=column_semantic_type,
        reason=(
            f"'{term}' implies a {term_group.lower()} concept ({term_family}), but the "
            f"best-matching column's own metadata classifies it as {column_semantic_type} "
            f"({column_group.lower()}) — refusing to combine mismatched business concepts."
        ),
    )
