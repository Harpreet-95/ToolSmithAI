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
    # Milestone M-5, Part 5 (2026-07-13): staffing/recruiting business
    # vocabulary, additive. "candidate" is intentionally shared with
    # "Applicant" (already a keyword there) — both entities score evidence
    # for a candidate-named table; the existing max-score +
    # _UNKNOWN_THRESHOLD mechanism resolves it using whatever other
    # evidence differs, the same way every other keyword overlap in this
    # table is already resolved. "hire"/"placement" overlap similarly with
    # "Employee".
    "Client": [
        "client", "customer",
    ],
    "Candidate": [
        "candidate", "consultant", "resume", "cv",
    ],
    "Placement": [
        "placement", "hire", "hiring",
    ],
    "Job": [
        "job", "opening", "position", "requisition", "vacancy",
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
# Deep profiling signal sets
# ---------------------------------------------------------------------------

_METRIC_SEMANTIC_TYPES = frozenset({
    "amount", "currency", "price", "revenue", "balance", "cost", "quantity",
})

_CONTACT_SEMANTIC_TYPES = frozenset({
    "email", "phone", "name", "ssn", "address",
})

_TIMESTAMP_SEMANTIC_TYPES = frozenset({
    "timestamp", "date", "datetime",
})

_LOW_CARDINALITY_TIERS = frozenset({"CONSTANT", "BINARY", "LOW"})
_HIGH_CARDINALITY_TIERS = frozenset({"UNIQUE", "HIGH"})

# Person-type entities boosted by PII/contact column signals
_PERSON_SIGNAL_ENTITIES: tuple[str, ...] = ("Student", "Applicant", "User", "Employee")

# Reference-type entities boosted by low-cardinality lookup signals
_REFERENCE_SIGNAL_ENTITIES: tuple[str, ...] = ("Department", "Course", "Program")

# Event-type entities boosted by timestamp/sequential signals
_EVENT_SIGNAL_ENTITIES: tuple[str, ...] = ("Event", "Campaign")

# Master data entities boosted by high-uniqueness identifier signals
_MASTER_SIGNAL_ENTITIES: tuple[str, ...] = ("Student", "Employee", "User", "Vendor")


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
# Deep profiling boosts
# ---------------------------------------------------------------------------

def _apply_deep_profiling_entity_boosts(
    column_profiles: list[dict],
    raw: dict[str, float],
    evidence: dict[str, list[str]],
) -> tuple[float, list[str]]:
    """Apply deep profiling column signals as additional entity score boosts.

    Mutates raw and evidence in-place.
    Returns (quality_confidence_multiplier, quality_evidence_strings).
    The multiplier is applied to the winner's confidence after threshold selection;
    it does not affect which entity wins.
    """
    n = len(column_profiles)
    if n == 0:
        return 1.0, []

    metric_cols        = 0
    skewed_cols        = 0
    timestamp_cols     = 0
    contact_cols       = 0
    pii_confirmed_cols = 0
    low_card_cols      = 0
    id_like_cols       = 0
    quality_vals: list[float] = []

    for cp in column_profiles:
        sem      = (cp.get("semantic_type")      or "").lower()
        card     = (cp.get("cardinality_tier")   or "").upper()
        shape    = (cp.get("distribution_shape") or "").lower()
        pii_json = (cp.get("pii_signals_json")   or "").lower()
        pii_conf = cp.get("pii_confirmed")
        qs       = cp.get("quality_score")
        uniq     = cp.get("uniqueness_score")

        if sem in _METRIC_SEMANTIC_TYPES:
            metric_cols += 1

        if shape in {"highly_skewed", "right_skewed"}:
            skewed_cols += 1

        if sem in _TIMESTAMP_SEMANTIC_TYPES:
            timestamp_cols += 1

        if sem in _CONTACT_SEMANTIC_TYPES:
            contact_cols += 1
        elif pii_json and any(k in pii_json for k in ("email", "phone", "contact")):
            contact_cols += 1

        if pii_conf and str(pii_conf).lower() not in ("", "false", "0", "none"):
            pii_confirmed_cols += 1

        if card in _LOW_CARDINALITY_TIERS:
            low_card_cols += 1

        if card in _HIGH_CARDINALITY_TIERS and uniq is not None:
            try:
                if float(uniq) >= 0.9:
                    id_like_cols += 1
            except (TypeError, ValueError):
                pass

        if qs is not None:
            try:
                quality_vals.append(float(qs))
            except (TypeError, ValueError):
                pass

    # -- Transaction Entity (Payment): metric/amount + skewed distributions ---
    finance_signals = metric_cols + skewed_cols
    if finance_signals >= 4:
        transaction_boost = 0.7
    elif finance_signals >= 2:
        transaction_boost = 0.4
    elif finance_signals >= 1:
        transaction_boost = 0.15
    else:
        transaction_boost = 0.0

    if transaction_boost > 0.0:
        parts: list[str] = []
        if metric_cols:
            parts.append(f"{metric_cols} metric-like numeric column(s)")
        if skewed_cols:
            parts.append(f"{skewed_cols} skewed numeric distribution(s)")
        raw["Payment"] += transaction_boost
        evidence["Payment"].append(
            "Deep profiling detected metric-heavy transaction structure: "
            + " and ".join(parts)
        )

    # Timestamp + amount combination is a strong payment record indicator.
    if timestamp_cols >= 1 and metric_cols >= 1:
        raw["Payment"] += 0.2
        evidence["Payment"].append(
            "Timestamp + amount combination suggests payment/transaction records"
        )

    # -- Person Entity: contact / PII columns ---------------------------------
    if contact_cols >= 3:
        contact_boost = 0.6
    elif contact_cols >= 2:
        contact_boost = 0.4
    elif contact_cols >= 1:
        contact_boost = 0.2
    else:
        contact_boost = 0.0

    if contact_boost > 0.0:
        msg = (
            f"Contact-related semantic types dominate "
            f"({contact_cols} contact column(s) detected)"
        )
        for entity in _PERSON_SIGNAL_ENTITIES:
            raw[entity] += contact_boost
            evidence[entity].append(msg)

    if pii_confirmed_cols >= 2:
        for entity in _PERSON_SIGNAL_ENTITIES:
            raw[entity] += 0.2
            evidence[entity].append(
                f"PII-confirmed columns detected ({pii_confirmed_cols} confirmed), "
                "suggesting person-level entity"
            )

    # -- Reference Entity: low-cardinality lookup patterns --------------------
    low_card_ratio = low_card_cols / n
    if low_card_ratio >= 0.7:
        ref_boost = 0.6
    elif low_card_ratio >= 0.5:
        ref_boost = 0.4
    elif low_card_cols >= 3:
        ref_boost = 0.25
    else:
        ref_boost = 0.0

    if ref_boost > 0.0:
        for entity in _REFERENCE_SIGNAL_ENTITIES:
            raw[entity] += ref_boost
            evidence[entity].append(
                f"Low-cardinality reference patterns detected "
                f"({low_card_cols}/{n} low-cardinality columns)"
            )

    # -- Event Entity: timestamp-heavy + sequential identifiers ---------------
    if timestamp_cols >= 3:
        event_boost = 0.65
    elif timestamp_cols >= 2:
        event_boost = 0.3
    elif timestamp_cols >= 1:
        event_boost = 0.15
    else:
        event_boost = 0.0

    if event_boost > 0.0:
        parts = [f"{timestamp_cols} timestamp/date column(s)"]
        if id_like_cols >= 1 and timestamp_cols >= 2:
            event_boost += 0.3
            parts.append(f"{id_like_cols} sequential identifier(s)")
        for entity in _EVENT_SIGNAL_ENTITIES:
            raw[entity] += event_boost
            evidence[entity].append(
                "Timestamp-heavy append-like structure detected: " + ", ".join(parts)
            )

    # -- Master Entity: multiple unique business key identifiers --------------
    if id_like_cols >= 3 and low_card_ratio < 0.5:
        master_boost = 0.65
    elif id_like_cols >= 2:
        master_boost = 0.25
    elif id_like_cols >= 1:
        master_boost = 0.1
    else:
        master_boost = 0.0

    if master_boost > 0.0:
        for entity in _MASTER_SIGNAL_ENTITIES:
            raw[entity] += master_boost
            evidence[entity].append(
                f"Deep profiling detected multiple unique identifiers "
                f"({id_like_cols} high-uniqueness column(s))"
            )

    # -- Quality confidence multiplier ----------------------------------------
    quality_evidence: list[str] = []
    quality_multiplier = 1.0
    if quality_vals:
        avg_qs = sum(quality_vals) / len(quality_vals)
        if avg_qs >= 75.0:
            quality_multiplier = 1.15
            quality_evidence.append(
                f"High data quality (avg score {avg_qs:.0f}/100) increased confidence"
            )
        elif avg_qs < 40.0:
            quality_multiplier = 0.80
            quality_evidence.append(
                f"Low data quality (avg score {avg_qs:.0f}/100) — classification less reliable"
            )

    return quality_multiplier, quality_evidence


# ---------------------------------------------------------------------------
# Entity detection
# ---------------------------------------------------------------------------

def detect_table_entity(
    table_profile: dict,
    semantic_types: list[str] | None = None,
    column_profiles: list[dict] | None = None,
) -> TableEntityAssignment:
    """Classify a profiled table into a business entity type.

    Args:
        table_profile: A dict with keys matching profiling_table_profiles columns.
            Required: table_fqn, table_name, schema_name.
            Optional signals: table_class, confirmed_pii_count, pii_column_count,
            fk_count, referenced_by_count.
        semantic_types: Semantic type strings from profiling_column_profiles
            (e.g. ['EMAIL', 'AMOUNT', 'STATUS']).  Case-insensitive.
        column_profiles: Optional full column profile row dicts from
            profiling_column_profiles.  When provided, deep profiling signals
            (cardinality_tier, distribution_shape, quality_score, pii_confirmed,
            pii_signals_json, uniqueness_score, etc.) improve entity classification
            and confidence.  If None, behaviour is identical to pre-deep-profiling.

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

    # -- Deep profiling column-level boosts -------------------------------
    quality_multiplier = 1.0
    quality_evidence: list[str] = []
    if column_profiles:
        quality_multiplier, quality_evidence = _apply_deep_profiling_entity_boosts(
            column_profiles, raw, evidence
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

    confidence = round(
        min(1.0, (top_score / _CONFIDENCE_DENOMINATOR) * quality_multiplier), 3
    )

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
        evidence=evidence[winner] + quality_evidence,
        competing_entities=competing,
    )
