import re

from core.profiling.models import (
    ConfidenceScore, DataCurrency, RowCountTier,
    SemanticType, TableClass, TableProfile,
)

# ── Tokeniser ──────────────────────────────────────────────────────────────────

_SPLIT_RE  = re.compile(r'[^a-zA-Z0-9]+')
_CAMEL_RE1 = re.compile(r'([a-z0-9])([A-Z])')
_CAMEL_RE2 = re.compile(r'([A-Z]+)([A-Z][a-z])')


def _tokens(name: str) -> frozenset[str]:
    s = _CAMEL_RE1.sub(r'\1 \2', name)
    s = _CAMEL_RE2.sub(r'\1 \2', s)
    return frozenset(t.lower() for t in _SPLIT_RE.split(s) if t)


# ── Vocabulary ─────────────────────────────────────────────────────────────────

_STAGING_PREFIXES = (
    'stg_', 'tmp_', 'temp_', 'stage_', 'import_', 'load_',
    'bak_', 'backup_', 'arc_', 'archive_', 'old_',
)
_STAGING_TOKENS = frozenset({
    'stg', 'tmp', 'temp', 'stage', 'import', 'bak', 'backup',
    'arc', 'archive', 'old', 'test', 'copy', 'mock', 'sample',
})
_DATE_STAMP_RE = re.compile(r'_\d{6,8}$|_\d{4}[_\-]\d{2}')

_REFERENCE_PREFIXES = (
    'ref_', 'lkp_', 'lookup_', 'code_', 'type_', 'status_', 'config_', 'param_',
)
_REFERENCE_TOKENS = frozenset({
    'ref', 'lookup', 'lkp', 'code', 'codes', 'type', 'types',
    'status', 'statuses', 'config', 'param', 'params',
    'setting', 'settings', 'option', 'options',
    'category', 'categories', 'enum', 'enums',
})

_MASTER_TOKENS = frozenset({
    'student', 'students', 'customer', 'customers', 'client', 'clients',
    'employee', 'employees', 'staff', 'user', 'users', 'person', 'people',
    'contact', 'contacts', 'account', 'accounts', 'member', 'members',
    'alumni', 'alumna', 'graduate', 'graduates', 'applicant', 'applicants',
    'patient', 'patients', 'vendor', 'vendors', 'supplier', 'suppliers',
    'instructor', 'faculty', 'worker', 'workers', 'agent', 'agents',
    'candidate', 'candidates', 'participant', 'participants',
})

_TRANSACTIONAL_TOKENS = frozenset({
    'order', 'orders', 'payment', 'payments', 'transaction', 'transactions',
    'enrollment', 'enrollments', 'registration', 'registrations',
    'charge', 'charges', 'invoice', 'invoices', 'receipt', 'receipts',
    'purchase', 'purchases', 'sale', 'sales', 'booking', 'bookings',
    'application', 'applications', 'request', 'requests',
    'disbursement', 'disbursements', 'deposit', 'deposits',
})

_AUDIT_TOKENS = frozenset({
    'audit', 'audits', 'log', 'logs', 'history', 'trail', 'trails',
    'change', 'changes', 'event', 'events', 'activity', 'activities',
    'tracking', 'changelog', 'changeset',
})
_AUDIT_COL_TOKENS = frozenset({
    'oldvalue', 'newvalue', 'before', 'after', 'actiontype',
    'changedby', 'modifiedby', 'action', 'operation', 'changedfield',
})

_REPORTING_PREFIXES = (
    'fact_', 'dim_', 'report_', 'summary_', 'agg_',
    'rpt_', 'kpi_', 'metric_', 'stats_', 'snap_',
)
_REPORTING_TOKENS = frozenset({
    'fact', 'dim', 'summary', 'summaries', 'aggregate', 'agg',
    'report', 'reports', 'kpi', 'metric', 'metrics',
    'snapshot', 'snapshots', 'stat', 'stats',
})

_RULE_VERSION  = '4.0.0'
_MIN_COMPETING = 0.40


# ── Column semantic type helpers ───────────────────────────────────────────────

def _sem_count(profile: TableProfile, stype: SemanticType) -> int:
    return sum(1 for cp in profile.column_profiles if cp.semantic_type == stype)


def _sem_fraction(profile: TableProfile, stype: SemanticType) -> float:
    if not profile.column_count:
        return 0.0
    return _sem_count(profile, stype) / profile.column_count


def _has_audit_col_names(profile: TableProfile) -> bool:
    for cp in profile.column_profiles:
        if _tokens(cp.column_name) & _AUDIT_COL_TOKENS:
            return True
    return False


# ── Per-class scorers ──────────────────────────────────────────────────────────

def _score_staging(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    tname = p.table_name.lower()
    toks  = _tokens(p.table_name)

    if any(tname.startswith(pfx) for pfx in _STAGING_PREFIXES):
        matched = next(pfx for pfx in _STAGING_PREFIXES if tname.startswith(pfx))
        ev.append(f"Table name starts with staging prefix '{matched}'")
        best = max(best, 0.68)

    if _DATE_STAMP_RE.search(tname):
        ev.append("Table name contains a date stamp — likely a dated extract or snapshot")
        best = max(best, 0.62)

    if toks & _STAGING_TOKENS:
        matched_t = toks & _STAGING_TOKENS
        ev.append(f"Table name contains staging/temp token(s) {matched_t}")
        best = max(best, 0.55)

    if best == 0.0:
        return 0.0, []

    if p.row_count_tier == RowCountTier.EMPTY:
        ev.append("row_count_tier=EMPTY — empty table consistent with staging (+0.15)")
        best = min(1.0, best + 0.15)

    if p.fk_count == 0 and p.referenced_by_count == 0:
        ev.append("No FK relationships (isolated table) — consistent with staging (+0.10)")
        best = min(1.0, best + 0.10)

    if p.data_currency == DataCurrency.HISTORICAL:
        ev.append("data_currency=HISTORICAL — no recently loaded data (+0.08)")
        best = min(1.0, best + 0.08)

    return best, ev


def _score_reference(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    tname = p.table_name.lower()
    toks  = _tokens(p.table_name)

    # Reference tables are small and narrow
    if p.row_count_tier not in (RowCountTier.EMPTY, RowCountTier.TINY, RowCountTier.SMALL, None):
        return 0.0, []
    if p.column_count > 8:
        return 0.0, []

    if any(tname.startswith(pfx) for pfx in _REFERENCE_PREFIXES):
        matched = next(pfx for pfx in _REFERENCE_PREFIXES if tname.startswith(pfx))
        ev.append(f"Table name starts with reference/lookup prefix '{matched}'")
        best = max(best, 0.52)

    if toks & _REFERENCE_TOKENS:
        matched_t = toks & _REFERENCE_TOKENS
        ev.append(f"Table name contains reference/lookup token(s) {matched_t}")
        best = max(best, 0.45)

    if best == 0.0:
        return 0.0, []

    if p.referenced_by_count >= 5:
        ev.append(f"referenced_by_count={p.referenced_by_count} ≥ 5 — heavily used as FK target (+0.30)")
        best = min(1.0, best + 0.30)
    elif p.referenced_by_count >= 2:
        ev.append(f"referenced_by_count={p.referenced_by_count} ≥ 2 — used as FK target (+0.15)")
        best = min(1.0, best + 0.15)

    if p.fk_count == 0:
        ev.append("fk_count=0 — reference table does not look up other tables (+0.10)")
        best = min(1.0, best + 0.10)

    if p.data_currency == DataCurrency.HISTORICAL:
        ev.append("data_currency=HISTORICAL — static or rarely-changing table (+0.08)")
        best = min(1.0, best + 0.08)

    return best, ev


def _score_master(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    toks = _tokens(p.table_name)

    if p.is_junction_table:
        return 0.0, []
    if p.referenced_by_count < 1:
        return 0.0, []

    if toks & _MASTER_TOKENS:
        matched = toks & _MASTER_TOKENS
        ev.append(f"Table name contains entity keyword(s) {matched}")
        best = max(best, 0.42)

    if best == 0.0:
        return 0.0, []

    if p.referenced_by_count >= 10:
        ev.append(f"referenced_by_count={p.referenced_by_count} ≥ 10 — highly central entity (+0.30)")
        best = min(1.0, best + 0.30)
    elif p.referenced_by_count >= 4:
        ev.append(f"referenced_by_count={p.referenced_by_count} ≥ 4 — well-referenced entity (+0.18)")
        best = min(1.0, best + 0.18)

    if p.has_identity_column:
        ev.append("has_identity_column=True — surrogate primary key consistent with master entity (+0.15)")
        best = min(1.0, best + 0.15)

    name_count = _sem_count(p, SemanticType.NAME)
    if name_count >= 1:
        ev.append(f"{name_count} NAME-type column(s) — person/entity name fields present (+0.12)")
        best = min(1.0, best + 0.12)

    cs = p.completeness_score
    if cs is not None and cs >= 0.85:
        ev.append(f"completeness_score={cs:.2f} ≥ 0.85 — well-populated entity record (+0.08)")
        best = min(1.0, best + 0.08)

    if p.data_currency == DataCurrency.ACTIVE:
        ev.append("data_currency=ACTIVE — actively maintained master record (+0.06)")
        best = min(1.0, best + 0.06)

    if p.row_count_tier in (RowCountTier.SMALL, RowCountTier.MEDIUM):
        ev.append(f"row_count_tier={p.row_count_tier.value} — typical entity table size (+0.05)")
        best = min(1.0, best + 0.05)

    return best, ev


def _score_transactional(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    toks = _tokens(p.table_name)

    if not p.has_date_column:
        return 0.0, []
    if p.fk_count < 1 and not p.is_junction_table:
        return 0.0, []

    if toks & _TRANSACTIONAL_TOKENS:
        matched = toks & _TRANSACTIONAL_TOKENS
        ev.append(f"Table name contains transactional keyword(s) {matched}")
        best = max(best, 0.42)

    if p.is_junction_table:
        ev.append("is_junction_table=True AND has_date_column=True — time-stamped association record")
        best = max(best, 0.52)

    if best == 0.0:
        return 0.0, []

    if p.data_currency == DataCurrency.ACTIVE:
        ev.append("data_currency=ACTIVE — actively growing transaction table (+0.22)")
        best = min(1.0, best + 0.22)

    if p.row_count_tier in (RowCountTier.MEDIUM, RowCountTier.LARGE, RowCountTier.VERY_LARGE):
        ev.append(
            f"row_count_tier={p.row_count_tier.value} — "
            f"high row count consistent with transactional volume (+0.18)"
        )
        best = min(1.0, best + 0.18)

    amount_cols = _sem_count(p, SemanticType.AMOUNT)
    if amount_cols >= 1:
        ev.append(f"{amount_cols} AMOUNT-type column(s) — financial measures present (+0.15)")
        best = min(1.0, best + 0.15)

    if p.fk_count >= 2:
        ev.append(f"fk_count={p.fk_count} ≥ 2 — connects to multiple entities (+0.12)")
        best = min(1.0, best + 0.12)

    status_cols = _sem_count(p, SemanticType.STATUS)
    if status_cols >= 1:
        ev.append(f"{status_cols} STATUS-type column(s) — transaction lifecycle state present (+0.08)")
        best = min(1.0, best + 0.08)

    return best, ev


def _score_audit(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    toks = _tokens(p.table_name)

    if toks & _AUDIT_TOKENS:
        matched = toks & _AUDIT_TOKENS
        ev.append(f"Table name contains audit/log keyword(s) {matched}")
        best = max(best, 0.58)

    if best == 0.0:
        return 0.0, []

    if _has_audit_col_names(p):
        ev.append(
            "Columns contain audit-pattern names (old_value, new_value, "
            "changed_by, action_type, etc.) (+0.25)"
        )
        best = min(1.0, best + 0.25)

    if p.data_currency == DataCurrency.ACTIVE and p.row_count_tier in (
        RowCountTier.LARGE, RowCountTier.VERY_LARGE
    ):
        ev.append(
            f"data_currency=ACTIVE AND row_count_tier={p.row_count_tier.value} "
            f"— large, actively growing append-only table (+0.15)"
        )
        best = min(1.0, best + 0.15)

    if p.referenced_by_count == 0:
        ev.append("referenced_by_count=0 — leaf node; nothing depends on it (typical for logs) (+0.08)")
        best = min(1.0, best + 0.08)

    if p.has_date_column:
        ev.append("has_date_column=True — temporal audit record with timestamp (+0.05)")
        best = min(1.0, best + 0.05)

    return best, ev


def _score_reporting(p: TableProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    tname = p.table_name.lower()
    toks  = _tokens(p.table_name)

    if any(tname.startswith(pfx) for pfx in _REPORTING_PREFIXES):
        matched = next(pfx for pfx in _REPORTING_PREFIXES if tname.startswith(pfx))
        ev.append(f"Table name starts with reporting/analytics prefix '{matched}'")
        best = max(best, 0.58)

    if toks & _REPORTING_TOKENS:
        matched_t = toks & _REPORTING_TOKENS
        ev.append(f"Table name contains reporting/analytics token(s) {matched_t}")
        best = max(best, 0.50)

    if best == 0.0:
        return 0.0, []

    metric_frac = _sem_fraction(p, SemanticType.AMOUNT) + _sem_fraction(p, SemanticType.COUNT)
    if metric_frac >= 0.30:
        ev.append(
            f"{metric_frac:.0%} of columns are AMOUNT or COUNT type — "
            f"high metric density consistent with a reporting table (+0.20)"
        )
        best = min(1.0, best + 0.20)

    if p.fk_count >= 4:
        ev.append(f"fk_count={p.fk_count} ≥ 4 — fan-out joins typical of fact tables (+0.18)")
        best = min(1.0, best + 0.18)

    if not p.has_identity_column:
        ev.append("has_identity_column=False — reporting tables use composite or natural keys (+0.08)")
        best = min(1.0, best + 0.08)

    return best, ev


# ── Priority-ordered scorer registry ──────────────────────────────────────────

# Staging is checked first — a staging name overrides any other signals.
# Audit before Transactional — log tables share transactional signals but are distinct.
_SCORERS = [
    (TableClass.STAGING,       _score_staging),
    (TableClass.REFERENCE,     _score_reference),
    (TableClass.AUDIT,         _score_audit),
    (TableClass.REPORTING,     _score_reporting),
    (TableClass.TRANSACTIONAL, _score_transactional),
    (TableClass.MASTER,        _score_master),
]

_PRIORITY = {tc: i for i, (tc, _) in enumerate(_SCORERS)}


# ── Public classifier ──────────────────────────────────────────────────────────

def classify_table(profile: TableProfile) -> ConfidenceScore:
    """Classify a table's class from its TableProfile.

    Evaluates all scorers, selects the highest confidence.
    Priority order breaks ties; Unknown is returned when no scorer fires.
    """
    scored: list[tuple[TableClass, float, list[str]]] = []

    for tclass, scorer in _SCORERS:
        confidence, evidence = scorer(profile)
        if confidence > 0.0:
            scored.append((tclass, confidence, evidence))

    if not scored:
        return ConfidenceScore(
            classification=TableClass.UNKNOWN.value,
            confidence=0.0,
            evidence=("No classification signals matched any known table class.",),
            rule_version=_RULE_VERSION,
            competing=(),
        )

    scored.sort(key=lambda x: (-x[1], _PRIORITY[x[0]]))
    winner_class, winner_conf, winner_ev = scored[0]

    competing = tuple(
        {'classification': tc.value, 'confidence': round(conf, 4)}
        for tc, conf, _ in scored[1:]
        if conf >= _MIN_COMPETING
    )

    return ConfidenceScore(
        classification=winner_class.value,
        confidence=round(winner_conf, 4),
        evidence=tuple(winner_ev),
        rule_version=_RULE_VERSION,
        competing=competing,
    )
