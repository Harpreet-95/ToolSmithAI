import re

from core.profiling.models import CardinalityTier, ColumnProfile, ConfidenceScore, SemanticType

# ── Tokeniser ──────────────────────────────────────────────────────────────────

_SPLIT_RE  = re.compile(r'[^a-zA-Z0-9]+')
_CAMEL_RE1 = re.compile(r'([a-z0-9])([A-Z])')
_CAMEL_RE2 = re.compile(r'([A-Z]+)([A-Z][a-z])')


def _tokens(name: str) -> frozenset[str]:
    s = _CAMEL_RE1.sub(r'\1 \2', name)
    s = _CAMEL_RE2.sub(r'\1 \2', s)
    return frozenset(t.lower() for t in _SPLIT_RE.split(s) if t)


def _try_float(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ── Vocabulary sets ────────────────────────────────────────────────────────────

_ID_ANYWHERE    = frozenset({'id', 'uuid', 'guid', 'pk', 'fk'})
_ID_SUFFIX      = frozenset({'id', 'uuid', 'guid', 'pk', 'fk', 'key', 'num', 'no', 'ref', 'seq'})

_AMOUNT_TOKENS  = frozenset({
    'amount', 'amt', 'price', 'cost', 'revenue', 'balance', 'total',
    'fee', 'tax', 'discount', 'salary', 'wage', 'income', 'earnings',
    'payment', 'charge', 'sum', 'rate', 'value', 'val',
})

_COUNT_TOKENS   = frozenset({
    'count', 'cnt', 'qty', 'quantity', 'units', 'volume', 'size',
})

_FLAG_PREFIXES  = frozenset({'is', 'has', 'was', 'can', 'did', 'will', 'should'})

_STATUS_TOKENS  = frozenset({'status', 'state', 'stage', 'phase', 'condition'})
_CODE_TOKENS    = frozenset({'code', 'cd', 'type', 'typ', 'category', 'class', 'kind', 'group', 'grp'})

_PHONE_TOKENS   = frozenset({'phone', 'mobile', 'cell', 'tel', 'fax', 'pager'})
_SSN_TOKENS     = frozenset({'ssn', 'sin', 'nino'})

_RULE_VERSION   = '4.0.0'
_MIN_COMPETING  = 0.40   # minimum confidence to appear in competing list


# ── Per-type scorers  (each returns confidence 0.0–1.0 + evidence list) ────────

def _score_email(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    emr = p.email_match_rate

    if emr is None:
        if p.pii_name_heuristic and 'email' in _tokens(p.column_name):
            ev.append("Column name matches email pattern (heuristic only; no match-rate data available)")
            return 0.55, ev
        return 0.0, ev

    if p.pii_name_heuristic and emr >= 0.70:
        ev.append(f"PII name heuristic active AND email_match_rate={emr:.2f} ≥ 0.70")
        return 0.95, ev
    if emr >= 0.85:
        ev.append(f"email_match_rate={emr:.2f} ≥ 0.85")
        return 0.90, ev
    if emr >= 0.50 and p.data_type == 'TEXT':
        ev.append(f"email_match_rate={emr:.2f} ≥ 0.50 on TEXT column")
        return 0.70, ev
    return 0.0, ev


def _score_phone(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    pmr = p.phone_match_rate

    if pmr is None:
        if p.pii_name_heuristic and _tokens(p.column_name) & _PHONE_TOKENS:
            ev.append("Column name matches phone pattern (heuristic only; no match-rate data available)")
            return 0.55, ev
        return 0.0, ev

    if p.pii_name_heuristic and pmr >= 0.60:
        ev.append(f"PII name heuristic active AND phone_match_rate={pmr:.2f} ≥ 0.60")
        return 0.90, ev
    if pmr >= 0.75:
        ev.append(f"phone_match_rate={pmr:.2f} ≥ 0.75")
        return 0.85, ev
    return 0.0, ev


def _score_ssn(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    toks = _tokens(p.column_name)

    if toks & _SSN_TOKENS:
        matched = toks & _SSN_TOKENS
        ev.append(f"Column name contains SSN/SIN/NINO token(s) {matched}")
        return 0.92, ev

    if p.pii_name_heuristic and p.data_type == 'TEXT':
        nsr = p.numeric_string_rate or 0.0
        mnl = p.min_length
        mxl = p.max_length_observed
        # 9 digits (no dashes) or 11 chars (XXX-XX-XXXX)
        if nsr > 0.85 and mnl is not None and mxl is not None and mnl in (9, 11) and mxl in (9, 11):
            ev.append(
                f"PII heuristic active AND numeric_string_rate={nsr:.2f} > 0.85 "
                f"AND observed length range [{mnl}–{mxl}] is consistent with SSN format"
            )
            return 0.80, ev

    return 0.0, ev


def _score_id(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    toks = _tokens(p.column_name)

    if p.is_primary_key:
        ev.append("is_primary_key = True — definitive identifier")
        return 1.0, ev

    us = p.uniqueness_score
    if us is not None and us >= 0.99 and p.is_identity:
        ev.append(f"uniqueness_score={us:.4f} ≥ 0.99 AND is_identity = True")
        best = max(best, 0.98)

    gmr = p.guid_match_rate
    if gmr is not None and gmr >= 0.90:
        ev.append(f"guid_match_rate={gmr:.2f} ≥ 0.90 — UUID/GUID identifier column")
        best = max(best, 0.92)

    if toks & _ID_ANYWHERE:
        matched = toks & _ID_ANYWHERE
        ev.append(f"Column name contains unambiguous identifier token(s) {matched}")
        best = max(best, 0.90)
    elif p.cardinality_tier in (CardinalityTier.UNIQUE, CardinalityTier.HIGH) and toks & _ID_SUFFIX:
        matched = toks & _ID_SUFFIX
        ev.append(
            f"cardinality_tier={p.cardinality_tier.value} "
            f"AND column name has identifier suffix token(s) {matched}"
        )
        best = max(best, 0.80)

    return best, ev


def _score_amount(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    toks = _tokens(p.column_name)

    if p.data_type not in ('DECIMAL', 'INTEGER'):
        return 0.0, ev

    if toks & _AMOUNT_TOKENS:
        matched = toks & _AMOUNT_TOKENS
        ev.append(
            f"data_type={p.data_type} AND column name token(s) {matched} "
            f"match financial/amount vocabulary"
        )
        return 0.90, ev

    min_f = _try_float(p.min_value)
    max_f = _try_float(p.max_value)
    if (
        p.data_type == 'DECIMAL'
        and min_f is not None and min_f >= 0
        and max_f is not None and max_f < 10_000_000
    ):
        ev.append(
            f"data_type=DECIMAL AND min={min_f} ≥ 0 AND max={max_f} < 10 000 000 "
            f"(plausible financial range; no amount token in name)"
        )
        return 0.68, ev

    return 0.0, ev


def _score_count(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    toks = _tokens(p.column_name)

    if p.data_type != 'INTEGER':
        return 0.0, ev

    if toks & _COUNT_TOKENS:
        matched = toks & _COUNT_TOKENS
        ev.append(
            f"data_type=INTEGER AND column name token(s) {matched} "
            f"match count/quantity vocabulary"
        )
        return 0.88, ev

    return 0.0, ev


def _score_date(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []

    if p.data_type == 'DATETIME':
        ev.append("data_type=DATETIME — temporal by schema definition")
        return 1.0, ev

    if p.data_type == 'TEXT':
        dsr = p.date_string_rate
        if dsr is not None and dsr >= 0.90:
            ev.append(
                f"data_type=TEXT AND date_string_rate={dsr:.2f} ≥ 0.90 "
                f"— TEXT column storing dates as strings"
            )
            return 0.85, ev
        if dsr is not None and dsr >= 0.70:
            ev.append(
                f"data_type=TEXT AND date_string_rate={dsr:.2f} ≥ 0.70 "
                f"— TEXT column likely storing dates as strings"
            )
            return 0.65, ev

    return 0.0, ev


def _score_status(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    toks = _tokens(p.column_name)
    low_card = p.cardinality_tier in (
        CardinalityTier.BINARY, CardinalityTier.LOW, CardinalityTier.CONSTANT
    )

    if not (low_card and p.data_type == 'TEXT'):
        return 0.0, ev

    best = 0.72
    ev.append(
        f"cardinality_tier={p.cardinality_tier.value} on TEXT column "
        f"— few distinct values consistent with a status/state field"
    )

    if toks & _STATUS_TOKENS:
        matched = toks & _STATUS_TOKENS
        ev.append(f"Column name token(s) {matched} match status vocabulary (+0.12)")
        best = min(1.0, best + 0.12)

    cov = p.top_values_coverage
    if cov is not None and cov >= 0.95:
        ev.append(f"top_values_coverage={cov:.2f} ≥ 0.95 — few values cover nearly all rows (+0.05)")
        best = min(1.0, best + 0.05)

    return best, ev


def _score_code(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    toks = _tokens(p.column_name)

    # IDs take priority — early exit if this looks like an identifier
    if p.is_primary_key or toks & _ID_ANYWHERE:
        return 0.0, ev

    low_card = p.cardinality_tier in (
        CardinalityTier.BINARY, CardinalityTier.LOW, CardinalityTier.CONSTANT
    )
    if not (low_card and p.data_type in ('TEXT', 'INTEGER')):
        return 0.0, ev

    if toks & _CODE_TOKENS:
        matched = toks & _CODE_TOKENS
        ev.append(
            f"cardinality_tier={p.cardinality_tier.value} AND "
            f"column name token(s) {matched} match code/type/category vocabulary"
        )
        return 0.82, ev

    ev.append(
        f"cardinality_tier={p.cardinality_tier.value} on {p.data_type} column "
        f"— few distinct values without a stronger classification signal"
    )
    return 0.62, ev


def _score_flag(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []
    best = 0.0
    toks = _tokens(p.column_name)

    if p.data_type == 'BOOLEAN':
        ev.append("data_type=BOOLEAN — boolean flag by schema definition")
        return 1.0, ev

    is_binary_int = (
        p.cardinality_tier == CardinalityTier.BINARY
        and p.distinct_count is not None
        and p.distinct_count <= 2
        and p.data_type == 'INTEGER'
    )
    has_flag_prefix = bool(toks & _FLAG_PREFIXES)

    if is_binary_int:
        ev.append(
            f"cardinality_tier=BINARY AND distinct_count={p.distinct_count} ≤ 2 "
            f"on INTEGER column — binary flag stored as 0/1"
        )
        best = 0.88

    if has_flag_prefix:
        matched = toks & _FLAG_PREFIXES
        ev.append(f"Column name starts with flag-prefix token(s) {matched}")
        if is_binary_int:
            best = min(1.0, best + 0.08)
        else:
            best = max(best, 0.80)

    return best, ev


def _score_name(p: ColumnProfile) -> tuple[float, list[str]]:
    ev: list[str] = []

    if p.data_type != 'TEXT':
        return 0.0, ev

    toks = _tokens(p.column_name)
    if p.pii_name_heuristic and 'name' in toks:
        ev.append(
            "PII name heuristic active AND column name contains 'name' token "
            "— likely a person or user name field"
        )
        return 0.82, ev

    return 0.0, ev


def _score_text(p: ColumnProfile) -> tuple[float, list[str]]:
    if p.data_type == 'TEXT':
        return 0.42, ["data_type=TEXT with no stronger semantic type signal detected"]
    return 0.0, []


def _score_binary(p: ColumnProfile) -> tuple[float, list[str]]:
    if p.data_type == 'BINARY':
        return 1.0, ["data_type=BINARY — binary data by schema definition"]
    return 0.0, []


# ── Priority-ordered scorer registry ──────────────────────────────────────────

_SCORERS = [
    (SemanticType.EMAIL,  _score_email),
    (SemanticType.PHONE,  _score_phone),
    (SemanticType.SSN,    _score_ssn),
    (SemanticType.ID,     _score_id),
    (SemanticType.AMOUNT, _score_amount),
    (SemanticType.COUNT,  _score_count),
    (SemanticType.DATE,   _score_date),
    (SemanticType.STATUS, _score_status),
    (SemanticType.CODE,   _score_code),
    (SemanticType.FLAG,   _score_flag),
    (SemanticType.NAME,   _score_name),
    (SemanticType.TEXT,   _score_text),
    (SemanticType.BINARY, _score_binary),
]

_PRIORITY = {st: i for i, (st, _) in enumerate(_SCORERS)}


# ── Public classifier ──────────────────────────────────────────────────────────

def classify_column(profile: ColumnProfile) -> ConfidenceScore:
    """Determine the semantic type of a column from its ColumnProfile.

    Evaluates every scorer, then selects the highest-confidence type.
    When confidence scores are equal, priority order (EMAIL > PHONE > ... > BINARY)
    acts as the tiebreaker.  Competing types above _MIN_COMPETING are included
    in the returned ConfidenceScore for transparency.
    """
    scored: list[tuple[SemanticType, float, list[str]]] = []

    for sem_type, scorer in _SCORERS:
        confidence, evidence = scorer(profile)
        if confidence > 0.0:
            scored.append((sem_type, confidence, evidence))

    if not scored:
        return ConfidenceScore(
            classification=SemanticType.UNKNOWN.value,
            confidence=0.0,
            evidence=("No profiling signals matched any known semantic type.",),
            rule_version=_RULE_VERSION,
            competing=(),
        )

    # Highest confidence wins; priority index breaks ties
    scored.sort(key=lambda x: (-x[1], _PRIORITY[x[0]]))

    winner_type, winner_conf, winner_ev = scored[0]

    competing = tuple(
        {'classification': st.value, 'confidence': round(conf, 4)}
        for st, conf, _ in scored[1:]
        if conf >= _MIN_COMPETING
    )

    return ConfidenceScore(
        classification=winner_type.value,
        confidence=round(winner_conf, 4),
        evidence=tuple(winner_ev),
        rule_version=_RULE_VERSION,
        competing=competing,
    )
