import re

# Longer prefixes precede shorter ones that share a leading character
# so the first match is always the longest applicable prefix.
_TABLE_PREFIXES: tuple[str, ...] = (
    'fact_', 'stg_', 'dim_', 'tbl_', 'vw_', 'tb_', 't_', 'v_',
)

_ABBR: dict[str, str] = {
    # Required by specification
    'id':   'ID',
    'qty':  'Quantity',
    'amt':  'Amount',
    'dt':   'Date',
    'ts':   'Timestamp',
    'desc': 'Description',
    'ref':  'Reference',
    'cust': 'Customer',
    'ord':  'Order',
    'inv':  'Invoice',
    'prod': 'Product',
    'emp':  'Employee',
    'dept': 'Department',
    'addr': 'Address',
    'hdr':  'Header',
    'dtl':  'Detail',
    # Common enterprise abbreviations
    'num':  'Number',
    'no':   'Number',
    'nbr':  'Number',
    'cd':   'Code',
    'flg':  'Flag',
    'ind':  'Indicator',
    'grp':  'Group',
    'mgr':  'Manager',
    'acct': 'Account',
    'std':  'Standard',
    'cnt':  'Count',
    'seq':  'Sequence',
    'usr':  'User',
    'yr':   'Year',
    'mo':   'Month',
    'pct':  'Percent',
    'wk':   'Week',
    'hr':   'Hour',
    'min':  'Minimum',
    'max':  'Maximum',
    'avg':  'Average',
    'val':  'Value',
    'nm':   'Name',
    'typ':  'Type',
    'src':  'Source',
    'tgt':  'Target',
    'trx':  'Transaction',
    'txn':  'Transaction',
    'pk':   'Primary Key',
    'fk':   'Foreign Key',
    # Acronyms that must stay uppercase
    'api':  'API',
    'url':  'URL',
    'uri':  'URI',
    'uuid': 'UUID',
    'guid': 'GUID',
    'iso':  'ISO',
    'sku':  'SKU',
    'upc':  'UPC',
}


def _split_tokens(name: str) -> list[str]:
    # Split camelCase: 'firstName' → 'first Name', 'APIKey' → 'API Key'
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return [t.lower() for t in re.split(r'[^a-zA-Z0-9]+', s) if t]


def _expand(token: str) -> str:
    return _ABBR.get(token, token.capitalize())


def humanize_name(name: str) -> str:
    """Convert any identifier style to a readable business label.

    Handles snake_case, camelCase, PascalCase, kebab-case, and dot.notation.
    Expands known abbreviations; title-cases everything else.
    """
    if not name:
        return ''
    return ' '.join(_expand(t) for t in _split_tokens(name) if t)


def humanize_table_name(table_name: str, schema_name: str = '') -> str:
    """Strip common table prefixes then humanize."""
    lower = table_name.lower()
    cleaned = table_name
    for prefix in _TABLE_PREFIXES:
        if lower.startswith(prefix):
            cleaned = table_name[len(prefix):]
            break
    return humanize_name(cleaned)


def humanize_column_name(column_name: str) -> str:
    """Humanize a column identifier."""
    return humanize_name(column_name)
