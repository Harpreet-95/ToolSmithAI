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
# Domain detection
# ---------------------------------------------------------------------------

def detect_table_domain(
    table_profile: dict,
    column_semantic_types: list[str] | None = None,
) -> TableDomainAssignment:
    """Classify a profiled table into a business domain.

    Args:
        table_profile: A dict with keys matching profiling_table_profiles columns.
            Required: table_fqn, table_name, schema_name.
            Optional signals: table_class, confirmed_pii_count, pii_column_count,
            fk_count, referenced_by_count.
        column_semantic_types: Semantic type strings from profiling_column_profiles
            (e.g. ['EMAIL', 'AMOUNT', 'STATUS']).  Case-insensitive.

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

    confidence = round(min(1.0, top_score / _CONFIDENCE_DENOMINATOR), 3)

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
        evidence=evidence[winner],
        competing_domains=competing,
    )
