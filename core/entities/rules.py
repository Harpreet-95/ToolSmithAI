from __future__ import annotations

import re

from core.entities.models import ENTITY_UNKNOWN, EntityScore, TableEntityAssignment

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# Raw score that maps to confidence = 1.0.  Two strong name-level hits saturate.
_CONFIDENCE_DENOMINATOR: float = 2.0

# Raw score below which the winner is demoted to Unknown.
_UNKNOWN_THRESHOLD: float = 0.6

# ---------------------------------------------------------------------------
# Keyword map — keywords are prefix-matched against name tokens so short
# roots ("student") cover inflected forms ("students", "student_id").
# ---------------------------------------------------------------------------

_ENTITY_KEYWORDS: dict[str, list[str]] = {
    "Student": [
        "student", "enroll", "transcript", "grade", "learner",
        "attendance", "roster", "gpa",
    ],
    "Applicant": [
        "applicant", "application", "admission", "prospect",
        "lead", "recruit", "candidate", "inquiry",
    ],
    "User": [
        "user", "account", "login", "credential",
        "member", "subscriber", "contact",
    ],
    "Employee": [
        "employee", "staff", "payroll", "personnel",
        "workforce", "hire", "onboard",
    ],
    "Course": [
        "course", "curriculum", "subject", "lesson", "offering",
    ],
    "Program": [
        "program", "major", "minor", "degree", "concentration",
    ],
    "Department": [
        "department", "division", "faculty",
    ],
    "Payment": [
        "payment", "invoice", "billing", "transaction",
        "receipt", "charge", "fee", "remittance",
    ],
    "Vendor": [
        "vendor", "supplier", "contractor", "merchant",
    ],
    "Campaign": [
        "campaign", "marketing", "outreach", "newsletter",
        "promotion", "broadcast",
    ],
    "Event": [
        "event", "appointment", "schedule", "calendar",
        "meeting", "booking",
    ],
}

# table_class → (entity to boost, boost amount).
# Only classes with a clear structural association to a single entity type.
_CLASS_BOOSTS: dict[str, tuple[str, float]] = {
    "TRANSACTIONAL": ("Payment",    0.5),
    "REFERENCE":     ("Department", 0.3),
}

# Each entry: (set of semantic type tokens that trigger the boost, entity, boost amount).
# A boost fires when ANY token in the set appears in the table's column semantic types.
_SEMANTIC_TYPE_BOOSTS: list[tuple[frozenset[str], str, float]] = [
    (frozenset({"amount", "currency", "price"}), "Payment",   0.3),
    (frozenset({"date"}),                        "Event",     0.2),
    (frozenset({"email"}),                       "User",      0.2),
    (frozenset({"email"}),                       "Applicant", 0.15),
]

# Person-type entities that are lifted by PII presence.
_PII_PERSON_ENTITIES: tuple[str, ...] = ("Student", "Employee", "User", "Applicant")

# Entities that are typically referenced by many other tables (high fan-in).
_HIGH_FANIN_ENTITIES: tuple[str, ...] = ("Department", "Program", "Employee")


# ---------------------------------------------------------------------------
# Tokenizer (identical strategy to domain rules)
# ---------------------------------------------------------------------------

def _tokenize(name: str) -> list[str]:
    """Split a name into lowercase tokens, handling snake_case and CamelCase."""
    name = re.sub(r"[\.\-\s]+", "_", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [t.lower() for t in name.split("_") if t]


def _hits(keyword: str, tokens: list[str]) -> bool:
    """True when any token equals the keyword or starts with it (prefix match)."""
    return any(tok == keyword or tok.startswith(keyword) for tok in tokens)


# ---------------------------------------------------------------------------
# Entity detection
# ---------------------------------------------------------------------------

def detect_table_entity(
    table_profile: dict,
    semantic_types: list[str] | None = None,
) -> TableEntityAssignment:
    """Classify a profiled table into a business entity type.

    Args:
        table_profile: A dict with keys matching profiling_table_profiles columns.
            Required: table_fqn, table_name, schema_name.
            Optional signals: table_class, confirmed_pii_count, pii_column_count,
            fk_count, referenced_by_count.
        semantic_types: Semantic type strings from profiling_column_profiles
            (e.g. ['EMAIL', 'AMOUNT', 'STATUS']).  Case-insensitive.

    Returns:
        TableEntityAssignment with entity, confidence [0–1], evidence list, and
        competing_entities sorted by score descending.
    """
    table_fqn     = str(table_profile.get("table_fqn")    or "")
    table_name    = str(table_profile.get("table_name")   or "")
    schema_name   = str(table_profile.get("schema_name")  or "")
    table_class   = str(table_profile.get("table_class")  or "").upper()
    confirmed_pii = int(table_profile.get("confirmed_pii_count") or 0)
    pii_cols      = int(table_profile.get("pii_column_count")    or 0)
    referenced_by = int(table_profile.get("referenced_by_count") or 0)

    name_tokens   = _tokenize(table_name)
    schema_tokens = _tokenize(schema_name)

    raw: dict[str, float]          = {e: 0.0 for e in _ENTITY_KEYWORDS}
    evidence: dict[str, list[str]] = {e: []  for e in _ENTITY_KEYWORDS}

    # -- Keyword scoring --------------------------------------------------
    for entity, keywords in _ENTITY_KEYWORDS.items():
        for kw in keywords:
            if _hits(kw, name_tokens):
                raw[entity] += 1.0
                evidence[entity].append(f"table_name '{table_name}' matches '{kw}'")
            if _hits(kw, schema_tokens):
                raw[entity] += 0.5
                evidence[entity].append(f"schema_name '{schema_name}' matches '{kw}'")

    # -- table_class structural boost -------------------------------------
    if table_class in _CLASS_BOOSTS:
        target, boost = _CLASS_BOOSTS[table_class]
        raw[target] += boost
        evidence[target].append(f"table_class={table_class}")

    # -- PII boost --------------------------------------------------------
    # Strong PII presence lifts all person-level entities; keyword scoring
    # then determines which specific entity wins.
    if confirmed_pii >= 2:
        for entity in _PII_PERSON_ENTITIES:
            raw[entity] += 0.3
            evidence[entity].append(f"confirmed_pii_count={confirmed_pii}")
    elif pii_cols >= 2:
        for entity in _PII_PERSON_ENTITIES:
            raw[entity] += 0.15
            evidence[entity].append(f"pii_column_count={pii_cols}")

    # -- High fan-in → likely a core reference entity ---------------------
    if referenced_by >= 5:
        for entity in _HIGH_FANIN_ENTITIES:
            raw[entity] += 0.4
            evidence[entity].append(
                f"referenced_by_count={referenced_by} (high fan-in suggests reference entity)"
            )

    # -- Semantic type boosts ---------------------------------------------
    if semantic_types:
        sem = frozenset(s.lower() for s in semantic_types)
        for signal_set, entity, boost in _SEMANTIC_TYPE_BOOSTS:
            matched = signal_set & sem
            if matched:
                raw[entity] += boost
                evidence[entity].append(
                    f"column semantic types include {sorted(matched)}"
                )

    # -- Winner selection -------------------------------------------------
    winner = max(raw, key=lambda e: raw[e])
    top_score = raw[winner]

    if top_score < _UNKNOWN_THRESHOLD:
        return TableEntityAssignment(
            table_fqn=table_fqn,
            entity=ENTITY_UNKNOWN,
            confidence=0.0,
            evidence=["no signals matched any entity above the confidence threshold"],
            competing_entities=[],
        )

    confidence = round(min(1.0, top_score / _CONFIDENCE_DENOMINATOR), 3)

    competing = [
        EntityScore(
            entity=e,
            score=round(min(1.0, s / _CONFIDENCE_DENOMINATOR), 3),
            evidence=evidence[e],
        )
        for e, s in sorted(raw.items(), key=lambda x: x[1], reverse=True)
        if e != winner and s > 0.0
    ]

    return TableEntityAssignment(
        table_fqn=table_fqn,
        entity=winner,
        confidence=confidence,
        evidence=evidence[winner],
        competing_entities=competing,
    )
