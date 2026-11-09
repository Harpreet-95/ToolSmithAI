from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from core.entities.models import ENTITY_UNKNOWN, TableEntityAssignment

_MIN_SUPPORT: int = 3


@dataclass
class LearnedEntityRule:
    id: int
    source_id: int
    pattern_type: str      # PREFIX | SUFFIX | TOKEN | SCHEMA
    pattern_value: str
    entity: str
    confidence: float
    approval_status: str   # PENDING | APPROVED | REJECTED
    created_by: str
    approved_by: str | None
    created_at: str
    approved_at: str | None
    active: bool


# ---------------------------------------------------------------------------
# Tokenizer (identical to rules.py — kept local to preserve zero-import purity)
# ---------------------------------------------------------------------------

def _tokenize(name: str) -> list[str]:
    name = re.sub(r"[\.\-\s]+", "_", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [t.lower() for t in name.split("_") if t]


def _matches(rule: LearnedEntityRule, table_name: str, schema_name: str) -> bool:
    """Return True when the table satisfies a learned rule's pattern."""
    val   = rule.pattern_value.lower()
    ptype = rule.pattern_type.upper()
    toks  = _tokenize(table_name)

    if ptype == "PREFIX":
        return bool(toks) and (toks[0] == val or toks[0].startswith(val))
    if ptype == "SUFFIX":
        return len(toks) >= 2 and toks[-1] == val
    if ptype == "TOKEN":
        return any(tok == val or tok.startswith(val) for tok in toks)
    if ptype == "SCHEMA":
        return schema_name.lower() == val
    return False


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def apply_learned_entity_rules(
    table_profile: dict,
    learned_rules: list[LearnedEntityRule],
) -> TableEntityAssignment | None:
    """Apply APPROVED active learned rules to a table profile.

    Checks rules in the order supplied (callers should sort by specificity or
    confidence).  Returns the first matching TableEntityAssignment, or None
    when no rule matches — the caller should then fall through to
    detect_table_entity().

    Only APPROVED active rules should be passed; this function does not filter
    by approval_status or active flag.
    """
    table_name  = str(table_profile.get("table_name")  or "")
    schema_name = str(table_profile.get("schema_name") or "")
    table_fqn   = str(table_profile.get("table_fqn")   or "")

    for rule in learned_rules:
        if _matches(rule, table_name, schema_name):
            return TableEntityAssignment(
                table_fqn=table_fqn,
                entity=rule.entity,
                confidence=rule.confidence,
                evidence=[
                    f"learned rule [{rule.pattern_type}] "
                    f"'{rule.pattern_value}' → {rule.entity}"
                ],
                competing_entities=[],
            )
    return None


def suggest_entity_rules(
    unknown_tables: list[dict],
    min_support: int = _MIN_SUPPORT,
) -> list[dict]:
    """Analyse Unknown entity assignment tables and produce rule suggestions.

    Args:
        unknown_tables: Dicts with at least table_name, schema_name, table_fqn,
            and optionally competing_entities (list of dicts with 'entity' key).
        min_support: A pattern must cover at least this many tables to appear
            in suggestions.

    Returns:
        Suggestions ordered by support_count desc then pattern-type priority
        (SCHEMA > PREFIX > SUFFIX > TOKEN).  Each entry:
            pattern_type, pattern_value, support_count,
            example_tables, suggested_entity, suggested_confidence.
    """
    prefix_tables: dict[str, list[str]] = {}
    suffix_tables: dict[str, list[str]] = {}
    token_tables:  dict[str, list[str]] = {}
    schema_tables: dict[str, list[str]] = {}

    prefix_entities: dict[str, Counter] = {}
    suffix_entities: dict[str, Counter] = {}
    token_entities:  dict[str, Counter] = {}
    schema_entities: dict[str, Counter] = {}

    def _tally(bucket: dict[str, Counter], key: str, competing: list[dict]) -> None:
        ctr = bucket.setdefault(key, Counter())
        for c in competing:
            e = c.get("entity")
            if e and e != ENTITY_UNKNOWN:
                ctr[e] += 1

    for t in unknown_tables:
        name      = str(t.get("table_name")  or "")
        schema    = str(t.get("schema_name") or "").lower()
        competing = t.get("competing_entities") or []
        toks      = _tokenize(name)

        if toks:
            p = toks[0]
            prefix_tables.setdefault(p, []).append(name)
            _tally(prefix_entities, p, competing)

        if len(toks) >= 2:
            s = toks[-1]
            suffix_tables.setdefault(s, []).append(name)
            _tally(suffix_entities, s, competing)

        for tok in toks:
            token_tables.setdefault(tok, []).append(name)
            _tally(token_entities, tok, competing)

        if schema:
            schema_tables.setdefault(schema, []).append(name)
            _tally(schema_entities, schema, competing)

    def _infer_entity(entity_ctr: dict[str, Counter], key: str) -> tuple[str, float]:
        ctr = entity_ctr.get(key, Counter())
        if not ctr:
            return ENTITY_UNKNOWN, 0.75
        top_entity, top_count = ctr.most_common(1)[0]
        total      = sum(ctr.values())
        consensus  = top_count / total
        # 0.75 baseline + up to 0.20 for a unanimous competing signal
        confidence = round(min(0.95, 0.75 + 0.20 * consensus), 3)
        return top_entity, confidence

    # Prefixes that already meet the threshold — suppresses redundant TOKEN
    # suggestions for the same value (PREFIX is always more specific).
    strong_prefix_vals = {
        val for val, tables in prefix_tables.items() if len(tables) >= min_support
    }

    suggestions: list[dict] = []
    seen: set[tuple[str, str]] = set()

    _ptype_order = {"SCHEMA": 0, "PREFIX": 1, "SUFFIX": 2, "TOKEN": 3}

    for bucket, entity_bucket, ptype in (
        (schema_tables, schema_entities, "SCHEMA"),
        (prefix_tables, prefix_entities, "PREFIX"),
        (suffix_tables, suffix_entities, "SUFFIX"),
        (token_tables,  token_entities,  "TOKEN"),
    ):
        for val, tables in bucket.items():
            if len(tables) < min_support:
                continue
            if ptype == "TOKEN" and val in strong_prefix_vals:
                continue  # Covered by a more specific PREFIX rule
            key = (ptype, val)
            if key in seen:
                continue
            seen.add(key)

            entity, confidence = _infer_entity(entity_bucket, val)
            suggestions.append({
                "pattern_type":         ptype,
                "pattern_value":        val,
                "support_count":        len(tables),
                "example_tables":       sorted(set(tables))[:5],
                "suggested_entity":     entity,
                "suggested_confidence": confidence,
            })

    suggestions.sort(
        key=lambda s: (-s["support_count"], _ptype_order.get(s["pattern_type"], 9))
    )
    return suggestions
