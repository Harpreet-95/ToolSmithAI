import re

from core.connectors.schema import ColumnInfo, TableInfo
from core.dictionary.humanizer import humanize_table_name
from core.dictionary.pii_detector import detect_pii


# ── Column classification sets ────────────────────────────────────────────────

# Any token that by itself marks a column as an identifier.
_ID_ANYWHERE: frozenset[str] = frozenset({'id', 'uuid', 'guid', 'pk', 'fk'})

# Tokens that mark a column as an identifier when they are the LAST token.
_ID_SUFFIX: frozenset[str] = frozenset({
    'id', 'uuid', 'guid', 'pk', 'fk', 'key', 'code', 'num', 'no', 'ref', 'seq',
})

# Tokens that indicate a numeric metric (paired with a numeric data type check).
_METRIC_TOKENS: frozenset[str] = frozenset({
    'count', 'cnt', 'total', 'sum', 'amount', 'amt',
    'quantity', 'qty', 'price', 'cost', 'revenue', 'balance',
    'rate', 'score', 'size', 'weight', 'hours', 'days',
    'percent', 'pct', 'ratio', 'avg', 'average',
    'min', 'max', 'value', 'val',
    'fee', 'tax', 'discount', 'units', 'volume',
    'duration', 'length', 'width', 'height', 'area',
    'capacity', 'limit', 'threshold', 'budget',
    'forecast', 'actual', 'variance', 'delta',
})

# First-token prefixes that signal a boolean/flag column.
_FLAG_PREFIXES: frozenset[str] = frozenset({
    'is', 'has', 'can', 'was', 'did', 'will', 'should',
})

_NUMERIC_TYPES: frozenset[str] = frozenset({'INTEGER', 'DECIMAL'})


# ── Table domain rules ────────────────────────────────────────────────────────

# Ordered: first match wins. More specific domains precede general ones.
_DOMAIN_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({
        'order', 'orders', 'sale', 'sales', 'invoice', 'invoices',
        'purchase', 'purchases', 'cart', 'checkout', 'receipt', 'receipts',
    }), 'Sales'),
    (frozenset({
        'customer', 'customers', 'client', 'clients', 'account', 'accounts',
        'contact', 'contacts', 'lead', 'leads', 'prospect', 'prospects',
    }), 'Customer'),
    (frozenset({
        'product', 'products', 'item', 'items', 'sku', 'catalog', 'catalogue',
        'inventory', 'stock', 'goods', 'material', 'materials', 'asset', 'assets',
    }), 'Product'),
    (frozenset({
        'employee', 'employees', 'staff', 'user', 'users', 'person', 'people',
        'worker', 'workers', 'member', 'members', 'personnel',
    }), 'People'),
    (frozenset({
        'payment', 'payments', 'ledger', 'journal', 'financial', 'finance',
        'accounting', 'tax', 'taxes', 'budget', 'expense', 'expenses',
        'revenue', 'profit', 'cost', 'costs', 'billing', 'charge', 'charges',
    }), 'Finance'),
    (frozenset({
        'report', 'reports', 'summary', 'summaries', 'fact', 'facts',
        'aggregate', 'metric', 'metrics', 'kpi', 'dashboard',
        'analytics', 'stat', 'stats', 'statistic', 'statistics',
    }), 'Analytics'),
    (frozenset({
        'log', 'logs', 'audit', 'audits', 'event', 'events', 'history',
        'activity', 'activities', 'job', 'jobs', 'task', 'tasks',
        'queue', 'process', 'workflow', 'request', 'requests', 'error', 'errors',
    }), 'Operations'),
    (frozenset({
        'config', 'configuration', 'setting', 'settings', 'parameter', 'parameters',
        'lookup', 'reference', 'code', 'codes', 'type', 'types',
        'status', 'statuses', 'category', 'categories', 'option', 'options',
    }), 'Reference'),
    (frozenset({
        'student', 'students', 'course', 'courses', 'class', 'classes',
        'enrollment', 'enrollments', 'grade', 'grades', 'assignment', 'assignments',
        'curriculum', 'school', 'faculty', 'term', 'semester',
    }), 'Education'),
    (frozenset({
        'training', 'trainings', 'completion', 'completions',
        'certification', 'certifications', 'skill', 'skills',
        'learning', 'module', 'modules', 'quiz', 'assessment', 'assessments',
        'compliance',
    }), 'Training'),
)


# ── Shared tokenizer ──────────────────────────────────────────────────────────

def _tokenize(name: str) -> list[str]:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return [t.lower() for t in re.split(r'[^a-zA-Z0-9]+', s) if t]


# ── Public classifiers ────────────────────────────────────────────────────────

def classify_column(column: ColumnInfo, table: TableInfo) -> dict:
    """Classify a column into a semantic type and compute boolean signal flags.

    Priority order: id → date → flag → metric → dimension → other.
    PII is independent of semantic type and is always evaluated.
    """
    toks = _tokenize(column.column_name)
    tok_set = set(toks)
    last = toks[-1] if toks else ''

    pii = detect_pii(column.column_name, column.data_type)

    is_id = (
        column.is_primary_key
        or bool(_ID_ANYWHERE & tok_set)
        or last in _ID_SUFFIX
    )

    is_date = column.data_type == 'DATETIME'

    is_flag = (
        column.data_type == 'BOOLEAN'
        or (bool(toks) and toks[0] in _FLAG_PREFIXES)
    )

    # Metrics: numeric type + metric token + not already classified as an id
    is_metric = (
        column.data_type in _NUMERIC_TYPES
        and bool(_METRIC_TOKENS & tok_set)
        and not is_id
    )

    # Dimensions: free-text categoricals — excludes ids and PII
    is_dimension = (
        column.data_type == 'TEXT'
        and not is_id
        and not pii
    )

    if is_id:
        semantic_type = 'id'
    elif is_date:
        semantic_type = 'date'
    elif is_flag:
        semantic_type = 'flag'
    elif is_metric:
        semantic_type = 'metric'
    elif is_dimension:
        semantic_type = 'dimension'
    else:
        semantic_type = 'other'

    return {
        'semantic_type': semantic_type,
        'is_metric':     is_metric,
        'is_dimension':  is_dimension,
        'is_date':       is_date,
        'is_id':         is_id,
        'pii_risk':      pii,
    }


def classify_table(table: TableInfo, schema_name: str) -> dict:
    """Infer a business domain and grain description for a table."""
    tok_set = set(_tokenize(table.table_name))

    domain = 'General'
    for keywords, candidate in _DOMAIN_RULES:
        if tok_set & keywords:
            domain = candidate
            break

    grain = f"One row per {humanize_table_name(table.table_name, schema_name)}"

    return {'domain': domain, 'grain': grain}
