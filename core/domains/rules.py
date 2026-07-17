from __future__ import annotations

import re

from core.domains.models import DOMAIN_UNKNOWN, DomainScore, TableDomainAssignment

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# Raw score that maps to confidence = 1.0.  Two strong name-level hits saturate.
_CONFIDENCE_DENOMINATOR: float = 2.0

# Raw score below which the winner is demoted to Unknown.
# A single schema-level match (0.5) is insufficient; a name-level match (1.0) is not.
_UNKNOWN_THRESHOLD: float = 0.6

# ---------------------------------------------------------------------------
# Keyword map  —  keywords are prefix-matched against name tokens so short
# roots ("finance") cover inflected forms ("financial", "finances").
# ---------------------------------------------------------------------------

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "Identity & Access": [
        "user", "auth", "role", "permission", "password", "login",
        "session", "token", "access", "account", "credential",
        "principal", "identity", "staff", "employee", "personnel",
    ],
    "Student Lifecycle": [
        "student", "enroll", "course", "class", "program",
        "grade", "attendance", "academic", "curriculum", "degree",
        "major", "section", "semester", "term", "registration",
        "transcript", "learner",
    ],
    "Admissions": [
        "applicant", "application", "admission", "admit", "prospect",
        "lead", "inquiry", "recruit", "apply", "decision", "offer",
        "candidate",
    ],
    "Alumni": [
        "alum", "donor", "donation", "gift", "advancement",
        "referral", "former",
    ],
    "Finance": [
        "payment", "transaction", "invoice", "billing", "charge",
        "fee", "finance", "ledger", "revenue", "expense", "budget",
        "cost", "price", "fund", "payroll", "receivable", "payable",
        "amount", "balance",
    ],
    "Communications": [
        "email", "message", "notification", "campaign", "template",
        "communication", "sms", "newsletter", "alert",
        "announcement", "broadcast",
    ],
    "Operations": [
        "audit", "log", "workflow", "task", "job", "process",
        "event", "history", "activity", "change", "track",
        "monitor", "schedule", "queue", "batch",
    ],
    "Reference Data": [
        "reference", "status", "type", "code", "lookup", "category",
        "classification", "config", "option", "flag", "enum",
    ],
    "Reporting & Analytics": [
        "report", "fact", "dim", "summary", "metric", "kpi",
        "dashboard", "aggregate", "stat", "analysis", "insight",
        "snapshot",
    ],
    "System / Platform": [
        "aspnet", "dnn", "django", "sys", "system", "module",
        "platform", "framework", "migration", "parameter", "metadata",
    ],
    # Milestone M-5, Part 5 (2026-07-13): staffing/recruiting business
    # vocabulary, additive — deliberately does NOT reuse "candidate"/
    # "recruit" (already Admissions' signal above) so existing Admissions
    # classification for CCPP's education-shaped tables doesn't shift.
    # "job" is intentionally shared with Operations (already a keyword
    # there) — other signals (e.g. domain/entity assignment already
    # present, relationship coverage) differentiate the two when a table
    # name alone is ambiguous between them.
    "Staffing & Recruiting": [
        "staffing", "recruiter", "recruiting", "placement", "submission",
        "timesheet", "hire", "hiring", "job",
    ],
}

# table_class value → (domain to boost, boost amount)
_CLASS_BOOSTS: dict[str, tuple[str, float]] = {
    "REFERENCE":     ("Reference Data",        0.8),
    "REPORTING":     ("Reporting & Analytics", 0.8),
    "AUDIT":         ("Operations",            0.6),
    "STAGING":       ("Operations",            0.4),
}

# Each entry: (set of semantic type tokens that trigger the boost, domain, boost amount)
_SEMANTIC_TYPE_BOOSTS: list[tuple[frozenset[str], str, float]] = [
    (frozenset({"email", "phone", "ssn", "name"}),  "Identity & Access", 0.3),
    (frozenset({"amount", "currency", "price"}),     "Finance",           0.3),
    (frozenset({"status", "code", "type"}),          "Reference Data",    0.2),
    (frozenset({"email", "template"}),               "Communications",    0.2),
]

# ---------------------------------------------------------------------------
# Deep profiling signal sets
# ---------------------------------------------------------------------------

# Semantic types strongly associated with financial/metric data
_METRIC_SEMANTIC_TYPES = frozenset({
    "amount", "currency", "price", "revenue", "balance", "cost", "quantity",
})

# Semantic types strongly associated with PII / contact data
_CONTACT_SEMANTIC_TYPES = frozenset({
    "email", "phone", "name", "ssn", "address",
})

# Cardinality tiers that indicate few distinct values (reference / lookup tables)
_LOW_CARDINALITY_TIERS = frozenset({"CONSTANT", "BINARY", "LOW"})

# Cardinality tiers that indicate many distinct values (entity / ID columns)
_HIGH_CARDINALITY_TIERS = frozenset({"UNIQUE", "HIGH"})


# ---------------------------------------------------------------------------
# Tokenizer
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

def _apply_deep_profiling_boosts(
    column_profiles: list[dict],
    raw: dict[str, float],
    evidence: dict[str, list[str]],
) -> tuple[float, list[str]]:
    """Apply deep profiling column signals as additional domain score boosts.

    Mutates raw and evidence in-place.
    Returns (quality_confidence_multiplier, quality_evidence_strings).
    quality_confidence_multiplier is applied to the winner's confidence after
    Unknown-threshold selection; it does not affect which domain wins.
    """
    n = len(column_profiles)
    if n == 0:
        return 1.0, []

    metric_cols = 0      # amount/currency/price semantic types
    skewed_cols = 0      # right_skewed or highly_skewed distributions
    contact_cols = 0     # email/phone/name PII-signal columns
    low_card_cols = 0    # CONSTANT/BINARY/LOW cardinality
    id_like_cols = 0     # UNIQUE/HIGH cardinality with uniqueness_score >= 0.9
    quality_vals: list[float] = []

    for cp in column_profiles:
        sem = (cp.get("semantic_type") or "").lower()
        card = (cp.get("cardinality_tier") or "").upper()
        shape = (cp.get("distribution_shape") or "").lower()
        pii_json = (cp.get("pii_signals_json") or "").lower()
        qs = cp.get("quality_score")
        uniqueness = cp.get("uniqueness_score")

        if sem in _METRIC_SEMANTIC_TYPES:
            metric_cols += 1

        if shape in {"highly_skewed", "right_skewed"}:
            skewed_cols += 1

        if sem in _CONTACT_SEMANTIC_TYPES:
            contact_cols += 1
        elif pii_json and any(k in pii_json for k in ("email", "phone", "contact")):
            contact_cols += 1

        if card in _LOW_CARDINALITY_TIERS:
            low_card_cols += 1

        if card in _HIGH_CARDINALITY_TIERS and uniqueness is not None:
            try:
                if float(uniqueness) >= 0.9:
                    id_like_cols += 1
            except (TypeError, ValueError):
                pass

        if qs is not None:
            try:
                quality_vals.append(float(qs))
            except (TypeError, ValueError):
                pass

    # -- Finance / Payments: metric-like and skewed numeric columns ----------
    finance_signals = metric_cols + skewed_cols
    if finance_signals >= 4:
        finance_boost = 0.7
    elif finance_signals >= 2:
        finance_boost = 0.4
    elif finance_signals >= 1:
        finance_boost = 0.15
    else:
        finance_boost = 0.0

    if finance_boost > 0.0:
        parts: list[str] = []
        if metric_cols:
            parts.append(f"{metric_cols} metric-like numeric column(s)")
        if skewed_cols:
            parts.append(f"{skewed_cols} skewed numeric distribution(s)")
        raw["Finance"] += finance_boost
        evidence["Finance"].append("Deep profiling found " + " and ".join(parts))

    # -- Contact / PII: email/phone/name/PII signal columns ------------------
    if contact_cols >= 3:
        contact_boost = 0.7
    elif contact_cols >= 2:
        contact_boost = 0.45
    elif contact_cols == 1:
        contact_boost = 0.2
    else:
        contact_boost = 0.0

    if contact_boost > 0.0:
        msg = (
            f"PII/contact signals suggest student/contact domain "
            f"({contact_cols} contact column(s))"
        )
        for d in ("Student Lifecycle", "Identity & Access"):
            raw[d] += contact_boost
            evidence[d].append(msg)

    # -- Reference / Lookup: majority low-cardinality columns ----------------
    low_card_ratio = low_card_cols / n
    if low_card_ratio >= 0.7:
        ref_boost = 0.7
    elif low_card_ratio >= 0.5:
        ref_boost = 0.45
    elif low_card_cols >= 3:
        ref_boost = 0.25
    else:
        ref_boost = 0.0

    if ref_boost > 0.0:
        raw["Reference Data"] += ref_boost
        evidence["Reference Data"].append(
            f"Majority low-cardinality columns ({low_card_cols}/{n}) indicate reference/lookup data"
        )

    # -- Master Data / Entity-oriented: unique/high-cardinality ID columns ---
    if id_like_cols >= 3:
        id_boost = 0.4
    elif id_like_cols >= 2:
        id_boost = 0.25
    elif id_like_cols >= 1:
        id_boost = 0.1
    else:
        id_boost = 0.0

    if id_boost > 0.0:
        for d in ("Identity & Access", "Student Lifecycle"):
            raw[d] += id_boost
            evidence[d].append(
                f"Deep profiling found {id_like_cols} unique/high-cardinality ID-like column(s)"
            )

    # -- Quality confidence multiplier ---------------------------------------
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
# Domain detection
# ---------------------------------------------------------------------------

def detect_table_domain(
    table_profile: dict,
    column_semantic_types: list[str] | None = None,
    column_profiles: list[dict] | None = None,
) -> TableDomainAssignment:
    """Classify a profiled table into a business domain.

    Args:
        table_profile: A dict with keys matching profiling_table_profiles columns.
            Required: table_fqn, table_name, schema_name.
            Optional signals: table_class, confirmed_pii_count, pii_column_count,
            fk_count, referenced_by_count.
        column_semantic_types: Semantic type strings from profiling_column_profiles
            (e.g. ['EMAIL', 'AMOUNT', 'STATUS']).  Case-insensitive.
        column_profiles: Optional full column profile row dicts from
            profiling_column_profiles.  When provided, deep profiling signals
            (cardinality_tier, distribution_shape, quality_score, pii_signals_json,
            uniqueness_score, etc.) are used to improve domain classification and
            confidence.  If None, behaviour is identical to the pre-deep-profiling
            logic.

    Returns:
        TableDomainAssignment with domain, confidence [0–1], evidence list, and
        competing_domains sorted by score descending.
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

    raw: dict[str, float]           = {d: 0.0 for d in _DOMAIN_KEYWORDS}
    evidence: dict[str, list[str]]  = {d: []  for d in _DOMAIN_KEYWORDS}

    # -- Keyword scoring --------------------------------------------------
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if _hits(kw, name_tokens):
                raw[domain] += 1.0
                evidence[domain].append(f"table_name '{table_name}' matches '{kw}'")
            if _hits(kw, schema_tokens):
                raw[domain] += 0.5
                evidence[domain].append(f"schema_name '{schema_name}' matches '{kw}'")

    # -- table_class structural boost -------------------------------------
    if table_class in _CLASS_BOOSTS:
        target, boost = _CLASS_BOOSTS[table_class]
        raw[target] += boost
        evidence[target].append(f"table_class={table_class}")

    # -- PII boost --------------------------------------------------------
    # Strong PII presence lifts both Identity & Access and Student Lifecycle
    # because person-level data appears in both domains.
    if confirmed_pii >= 2:
        for d in ("Identity & Access", "Student Lifecycle"):
            raw[d] += 0.4
            evidence[d].append(f"confirmed_pii_count={confirmed_pii}")
    elif pii_cols >= 2:
        for d in ("Identity & Access", "Student Lifecycle"):
            raw[d] += 0.2
            evidence[d].append(f"pii_column_count={pii_cols}")

    # -- High fan-in → likely a core reference entity ---------------------
    if referenced_by >= 5:
        raw["Reference Data"] += 0.4
        evidence["Reference Data"].append(
            f"referenced_by_count={referenced_by} (high fan-in suggests reference entity)"
        )

    # -- Semantic type boosts ---------------------------------------------
    if column_semantic_types:
        sem = frozenset(s.lower() for s in column_semantic_types)
        for signal_set, domain, boost in _SEMANTIC_TYPE_BOOSTS:
            matched = signal_set & sem
            if matched:
                raw[domain] += boost
                evidence[domain].append(
                    f"column semantic types include {sorted(matched)}"
                )

    # -- Deep profiling column-level boosts -------------------------------
    quality_multiplier = 1.0
    quality_evidence: list[str] = []
    if column_profiles:
        quality_multiplier, quality_evidence = _apply_deep_profiling_boosts(
            column_profiles, raw, evidence
        )

    # -- Winner selection -------------------------------------------------
    winner = max(raw, key=lambda d: raw[d])
    top_score = raw[winner]

    if top_score < _UNKNOWN_THRESHOLD:
        return TableDomainAssignment(
            table_fqn=table_fqn,
            domain=DOMAIN_UNKNOWN,
            confidence=0.0,
            evidence=["no signals matched any domain above the confidence threshold"],
            competing_domains=[],
        )

    confidence = round(
        min(1.0, (top_score / _CONFIDENCE_DENOMINATOR) * quality_multiplier), 3
    )

    competing = [
        DomainScore(
            domain=d,
            score=round(min(1.0, s / _CONFIDENCE_DENOMINATOR), 3),
            evidence=evidence[d],
        )
        for d, s in sorted(raw.items(), key=lambda x: x[1], reverse=True)
        if d != winner and s > 0.0
    ]

    return TableDomainAssignment(
        table_fqn=table_fqn,
        domain=winner,
        confidence=confidence,
        evidence=evidence[winner] + quality_evidence,
        competing_domains=competing,
    )
