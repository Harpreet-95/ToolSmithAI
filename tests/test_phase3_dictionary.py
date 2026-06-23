"""
Smoke tests for Phase 3A-E — Data Dictionary Foundation.

Run from the project root:
    venv/Scripts/pytest tests/test_phase3_dictionary.py -v
"""

import os

from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.dictionary.generator import generate_dictionary
from core.dictionary.humanizer import humanize_column_name, humanize_table_name
from core.dictionary.pii_detector import detect_pii
from core.dictionary.rule_classifier import classify_column, classify_table


# ── Shared helpers ────────────────────────────────────────────────────────────

def _col(name: str, data_type: str, is_pk: bool = False) -> ColumnInfo:
    return ColumnInfo(
        column_name=name, ordinal_position=1,
        data_type=data_type, raw_type='',
        is_nullable=True, is_primary_key=is_pk, is_identity=False,
    )

def _table(name: str = 'orders', schema: str = 'dbo') -> TableInfo:
    return TableInfo(
        table_name=name, schema_name=schema,
        table_fqn=f'{schema}.{name}', table_type='TABLE',
    )

def _snap(*cols: ColumnInfo) -> SchemaSnapshot:
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    s = SchemaInfo(schema_name='dbo')
    t = _table()
    t.columns.extend(cols)
    s.tables.append(t)
    snap.schemas.append(s)
    return snap


# ── 1. detect_pii — positive cases ───────────────────────────────────────────

def test_pii_email():
    assert detect_pii('email', 'TEXT') is True
    assert detect_pii('customer_email', 'TEXT') is True
    assert detect_pii('EmailAddress', 'TEXT') is True

def test_pii_phone():
    assert detect_pii('phone', 'TEXT') is True
    assert detect_pii('mobile_number', 'TEXT') is True

def test_pii_password_and_api_key():
    assert detect_pii('password', 'TEXT') is True
    assert detect_pii('user_password_hash', 'TEXT') is True
    assert detect_pii('api_key', 'TEXT') is True

def test_pii_ssn():
    assert detect_pii('ssn', 'TEXT') is True
    assert detect_pii('social_security_number', 'TEXT') is True

def test_pii_name_fields():
    assert detect_pii('first_name', 'TEXT') is True
    assert detect_pii('last_name', 'TEXT') is True
    assert detect_pii('full_name', 'TEXT') is True

def test_pii_dob():
    assert detect_pii('dob', 'DATETIME') is True
    assert detect_pii('birth_date', 'DATETIME') is True
    assert detect_pii('date_of_birth', 'DATETIME') is True

def test_pii_salary_income():
    assert detect_pii('salary', 'DECIMAL') is True
    assert detect_pii('annual_income', 'DECIMAL') is True

def test_pii_location():
    assert detect_pii('latitude', 'DECIMAL') is True
    assert detect_pii('longitude', 'DECIMAL') is True
    assert detect_pii('ip_address', 'TEXT') is True


# ── 2. detect_pii — negative cases ───────────────────────────────────────────

def test_pii_does_not_flag_pk_column():
    assert detect_pii('order_id', 'INTEGER') is False
    assert detect_pii('product_id', 'INTEGER') is False

def test_pii_does_not_flag_sort_key():
    assert detect_pii('sort_key', 'INTEGER') is False

def test_pii_does_not_flag_foreign_key_name():
    assert detect_pii('foreign_key_id', 'INTEGER') is False

def test_pii_does_not_flag_common_columns():
    assert detect_pii('is_active', 'BOOLEAN') is False
    assert detect_pii('created_at', 'DATETIME') is False
    assert detect_pii('total_amount', 'DECIMAL') is False
    assert detect_pii('product_category', 'TEXT') is False


# ── 3. humanize_table_name ────────────────────────────────────────────────────

def test_humanize_table_strips_tbl_prefix():
    assert humanize_table_name('tbl_student_master') == 'Student Master'

def test_humanize_table_strips_fact_prefix():
    assert humanize_table_name('fact_sales_summary') == 'Sales Summary'

def test_humanize_table_strips_dim_prefix():
    assert humanize_table_name('dim_customer') == 'Customer'

def test_humanize_table_strips_vw_prefix():
    assert humanize_table_name('vw_active_employees') == 'Active Employees'

def test_humanize_table_expands_abbr():
    assert humanize_table_name('ord_hdr') == 'Order Header'
    assert humanize_table_name('emp_addr') == 'Employee Address'


# ── 4. humanize_column_name ───────────────────────────────────────────────────

def test_humanize_column_snake_case():
    assert humanize_column_name('order_date') == 'Order Date'
    assert humanize_column_name('total_amount') == 'Total Amount'

def test_humanize_column_camel_case():
    assert humanize_column_name('CustomerID') == 'Customer ID'
    assert humanize_column_name('firstName') == 'First Name'

def test_humanize_column_abbreviations():
    assert humanize_column_name('customer_id') == 'Customer ID'
    assert humanize_column_name('created_dt') == 'Created Date'
    assert humanize_column_name('order_amt') == 'Order Amount'


# ── 5. classify_column ────────────────────────────────────────────────────────

def test_classify_column_id_from_pk():
    r = classify_column(_col('order_id', 'INTEGER', is_pk=True), _table())
    assert r['semantic_type'] == 'id'
    assert r['is_id'] is True

def test_classify_column_id_from_name_suffix():
    r = classify_column(_col('customer_id', 'INTEGER'), _table())
    assert r['semantic_type'] == 'id'
    assert r['is_id'] is True

def test_classify_column_date():
    r = classify_column(_col('order_date', 'DATETIME'), _table())
    assert r['semantic_type'] == 'date'
    assert r['is_date'] is True

def test_classify_column_flag_boolean():
    r = classify_column(_col('is_active', 'BOOLEAN'), _table())
    assert r['semantic_type'] == 'flag'

def test_classify_column_flag_prefix():
    r = classify_column(_col('has_discount', 'INTEGER'), _table())
    assert r['semantic_type'] == 'flag'

def test_classify_column_metric():
    r = classify_column(_col('total_amount', 'DECIMAL'), _table())
    assert r['semantic_type'] == 'metric'
    assert r['is_metric'] is True

def test_classify_column_dimension():
    r = classify_column(_col('product_category', 'TEXT'), _table())
    assert r['semantic_type'] == 'dimension'
    assert r['is_dimension'] is True

def test_classify_column_pii_excluded_from_dimension():
    r = classify_column(_col('customer_name', 'TEXT'), _table())
    assert r['pii_risk'] is True
    assert r['is_dimension'] is False
    assert r['semantic_type'] == 'other'

def test_classify_column_returns_all_required_keys():
    r = classify_column(_col('amount', 'DECIMAL'), _table())
    assert set(r.keys()) == {'semantic_type', 'is_metric', 'is_dimension', 'is_date', 'is_id', 'pii_risk'}


# ── 6. classify_table domain ──────────────────────────────────────────────────

def test_classify_table_sales():
    assert classify_table(_table('orders'), 'dbo')['domain'] == 'Sales'

def test_classify_table_customer():
    assert classify_table(_table('customers'), 'dbo')['domain'] == 'Customer'

def test_classify_table_product():
    assert classify_table(_table('products'), 'dbo')['domain'] == 'Product'

def test_classify_table_people():
    assert classify_table(_table('employees'), 'dbo')['domain'] == 'People'

def test_classify_table_finance():
    assert classify_table(_table('payments'), 'dbo')['domain'] == 'Finance'

def test_classify_table_operations():
    assert classify_table(_table('audit_logs'), 'dbo')['domain'] == 'Operations'

def test_classify_table_reference():
    assert classify_table(_table('ref_codes'), 'dbo')['domain'] == 'Reference'

def test_classify_table_education():
    assert classify_table(_table('student_enrollment'), 'dbo')['domain'] == 'Education'

def test_classify_table_training():
    assert classify_table(_table('training_completion'), 'dbo')['domain'] == 'Training'

def test_classify_table_general_fallback():
    assert classify_table(_table('misc_table'), 'dbo')['domain'] == 'General'

def test_classify_table_grain_prefix():
    grain = classify_table(_table('orders'), 'dbo')['grain']
    assert grain.startswith('One row per ')


# ── 7. generate_dictionary — table entries ───────────────────────────────────

def test_generate_creates_one_table_entry():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    s = SchemaInfo(schema_name='dbo')
    s.tables.append(_table('orders'))
    snap.schemas.append(s)
    result = generate_dictionary(snap, snapshot_id=42)
    assert len(result.table_entries) == 1
    assert result.table_entries[0].table_fqn == 'dbo.orders'
    assert result.table_entries[0].snapshot_id == 42
    assert result.table_entries[0].generation_method == 'rule_based'

def test_generate_table_entry_has_business_name():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    s = SchemaInfo(schema_name='dbo')
    s.tables.append(_table('tbl_student_master'))
    snap.schemas.append(s)
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.table_entries[0].business_name == 'Student Master'


# ── 8. generate_dictionary — column entries ───────────────────────────────────

def test_generate_creates_column_entries():
    snap = _snap(_col('order_id', 'INTEGER', is_pk=True), _col('order_date', 'DATETIME'))
    result = generate_dictionary(snap, snapshot_id=1)
    assert len(result.column_entries) == 2
    assert {e.column_name for e in result.column_entries} == {'order_id', 'order_date'}

def test_generate_column_entry_has_business_label():
    snap = _snap(_col('order_date', 'DATETIME'))
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.column_entries[0].business_label == 'Order Date'

def test_generate_column_entry_generation_method():
    snap = _snap(_col('total_amount', 'DECIMAL'))
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.column_entries[0].generation_method == 'rule_based'


# ── 9. PII column meaning ─────────────────────────────────────────────────────

def test_pii_column_gets_fixed_placeholder_meaning():
    snap = _snap(_col('customer_email', 'TEXT'), _col('order_date', 'DATETIME'))
    result = generate_dictionary(snap, snapshot_id=1)
    email = next(e for e in result.column_entries if e.column_name == 'customer_email')
    assert email.pii_risk is True
    assert email.meaning == "[PII — manual review required]"

def test_non_pii_column_does_not_get_pii_meaning():
    snap = _snap(_col('order_date', 'DATETIME'))
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.column_entries[0].meaning != "[PII — manual review required]"


# ── 10. DictionaryResult pii_column_count ────────────────────────────────────

def test_pii_column_count_matches_pii_entries():
    snap = _snap(
        _col('customer_email', 'TEXT'),
        _col('customer_phone', 'TEXT'),
        _col('order_date', 'DATETIME'),
    )
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.pii_column_count == 2

def test_pii_column_count_zero_when_no_pii():
    snap = _snap(_col('order_id', 'INTEGER', is_pk=True), _col('total_amount', 'DECIMAL'))
    result = generate_dictionary(snap, snapshot_id=1)
    assert result.pii_column_count == 0

def test_pii_column_count_is_int():
    result = generate_dictionary(
        SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00'),
        snapshot_id=1,
    )
    assert isinstance(result.pii_column_count, int)


# ── 11. dictionary_service imports ───────────────────────────────────────────

def test_dictionary_service_imports_successfully():
    from data.dictionary_service import (
        generate_and_save_dictionary,
        list_dictionary_tables,
        get_table_dictionary,
    )
    assert all(callable(f) for f in [
        generate_and_save_dictionary,
        list_dictionary_tables,
        get_table_dictionary,
    ])


# ── 12. API routes import ─────────────────────────────────────────────────────

def test_api_dictionary_routes_registered():
    from api.v1.routes import router
    paths = [r.path for r in router.routes]
    assert any('dictionary/generate' in p for p in paths)
    assert any('/dictionary' in p for p in paths)
    assert any('dictionary/tables' in p for p in paths)
