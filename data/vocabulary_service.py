"""
Vocabulary Service — Milestone M-5, Part 2.

Single shared normalization + synonym-expansion entry point used by both the
metadata-search path (data/search_service.py) and the SQL-answering path
(data/query_planning_service.py), so a business concept expands to the same
governed vocabulary regardless of which path resolves it.

Reuses data/search_service.py's existing _SynonymExpander (backed by
data/synonyms.json) verbatim — this module adds only what was missing:
deterministic term normalization (case/whitespace/punctuation/plural) and
multi-word phrase handling (e.g. "job order") so a phrase expands as one
unit instead of being decomposed into its individual, separately-grouped
words. No fuzzy/AI-generated synonyms; no new synonym data structure.
"""
from __future__ import annotations

import re

from data.search_service import _SYNONYM_EXPANDER

# Multi-word phrases already registered in data/synonyms.json's groups (e.g.
# "job order", "bank transfer", "case study") — detected so a phrase is
# expanded as one unit via _SYNONYM_EXPANDER.expand(phrase) before any
# per-word expansion runs. Without this, "job order" would decompose into
# "job" + "order", and "order" alone already belongs to the unrelated
# invoice/billing/finance/purchase group.
_MULTI_WORD_PHRASES: frozenset[str] = frozenset(
    term for term in _SYNONYM_EXPANDER._map if " " in term
)

_PLURAL_IES_RE = re.compile(r"ies$")
_PLURAL_S_RE = re.compile(r"(?<!s)s$")


def normalize_term(term: str) -> str:
    """
    Deterministic normalization: lowercase, squash whitespace/punctuation,
    strip a common plural suffix. No stemming library, no NLP, no fuzzy
    matching — a fixed set of regex rules only, applied identically
    everywhere this module is used.

    Known, accepted limitation: a bare trailing "s" not preceded by another
    "s" is always stripped (e.g. "clients" -> "client"), which also
    over-strips genuinely singular words ending in "s" that aren't plurals
    ("status" -> "statu", "gas" -> "ga"). A stoplist of exceptions would
    require a growing, hand-maintained word list — the same "no fuzzy/
    AI-generated synonyms" constraint that keeps this deterministic makes
    that trade-off acceptable rather than adding an exception dictionary
    that itself needs upkeep.
    """
    if not term:
        return ""
    t = term.strip().lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if _PLURAL_IES_RE.search(t):
        t = _PLURAL_IES_RE.sub("y", t)
    elif _PLURAL_S_RE.search(t) and not t.endswith("ss"):
        t = t[:-1]
    return t.strip()


def expand_concept(term: str) -> list[str]:
    """
    Normalize `term`, then expand it through the existing governed synonym
    dictionary. Returns the normalized term first, followed by every
    synonym (deduplicated) — never empty for a non-empty input.

    Multi-word phrases matching a known synonym-group entry are expanded as
    one unit; otherwise each whitespace-separated word is expanded
    independently and the results are unioned.
    """
    normalized = normalize_term(term)
    if not normalized:
        return []

    seen = {normalized}
    result = [normalized]

    if normalized in _MULTI_WORD_PHRASES:
        for syn in sorted(_SYNONYM_EXPANDER.expand(normalized)):
            if syn not in seen:
                seen.add(syn)
                result.append(syn)
        return result

    for word in normalized.split():
        for syn in sorted(_SYNONYM_EXPANDER.expand(word)):
            if syn not in seen:
                seen.add(syn)
                result.append(syn)
    return result
