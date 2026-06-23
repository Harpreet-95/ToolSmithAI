"""
Smoke tests for Phase 4A — Enterprise Metadata Profiling Engine.

Run from the project root:
    venv/Scripts/pytest tests/test_phase4_profiling.py -v
"""

import os
import pytest
from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
# Profiling models/classifiers don't need these, but some transitive imports might.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

from core.profiling.models import (
    CardinalityTier, ColumnProfile, ConfidenceScore, DataCurrency,
    ProfilingConfig, ProfilingMode, ProfilingSnapshot, ProfilingStatus,
    RowCountTier, SemanticType, TableClass, TableProfile,
)
from core.profiling.classification.column_typer import classify_column
from core.profiling.classification.table_classifier import classify_table
from core.profiling.sql.mssql import (
    build_column_stats_query,
    build_date_range_query,
    build_row_count_query,
    build_sample_values_query,
    build_top_values_query,
)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _col(name: str, data_type: str, **kwargs) -> ColumnProfile:
    defaults = dict(
        source_id=1, profiling_snapshot_id=1,
        table_fqn='dbo.test', column_name=name,
        data_type=data_type, raw_type=data_type.lower(),
        is_nullable=True, is_primary_key=False,
        is_identity=False, ordinal_position=1,
    )
    defaults.update(kwargs)
    return ColumnProfile(**defaults)


def _table(name: str, schema: str = 'dbo', **kwargs) -> TableProfile:
    defaults = dict(
        source_id=1, profiling_snapshot_id=1,
        table_fqn=f'{schema}.{name}', table_name=name,
        schema_name=schema, table_type='TABLE',
    )
    defaults.update(kwargs)
    return TableProfile(**defaults)


# ── 1. ProfilingConfig defaults ───────────────────────────────────────────────

def test_profiling_config_defaults():
    cfg = ProfilingConfig()
    assert cfg.mode == ProfilingMode.FULL
    assert cfg.sample_rate == 1.0
    assert cfg.max_top_values == 20
    assert cfg.max_sample_values == 10
    assert cfg.row_limit_for_full == 1_000_000
    assert cfg.timeout_per_table_s == 60
    assert cfg.timeout_per_col_s == 30
    assert cfg.max_column_count == 300
    assert cfg.excluded_schemas == []
    assert cfg.excluded_prefixes == []
    assert cfg.excluded_table_fqns == []
    assert cfg.priority_tables == []


# ── 2. ColumnProfile creation ─────────────────────────────────────────────────

def test_column_profile_defaults_are_none():
    cp = _col('student_id', 'INTEGER', is_primary_key=True, is_identity=True)
    assert cp.column_name == 'student_id'
    assert cp.is_primary_key is True
    assert cp.null_count is None
    assert cp.distinct_count is None
    assert cp.semantic_type is None
    assert cp.pii_name_heuristic is False
    assert cp.top_values == []
    assert cp.sample_values == []


# ── 3. TableProfile creation ──────────────────────────────────────────────────

def test_table_profile_defaults():
    tp = _table('students')
    assert tp.column_count == 0
    assert tp.fk_count == 0
    assert tp.referenced_by_count == 0
    assert tp.is_junction_table is False
    assert tp.has_identity_column is False
    assert tp.has_date_column is False
    assert tp.classification is None
    assert tp.column_profiles == []
    assert tp.data_currency == DataCurrency.UNKNOWN


# ── 4. ProfilingSnapshot creation ────────────────────────────────────────────

def test_profiling_snapshot_defaults():
    snap = ProfilingSnapshot(
        source_id=1, schema_snapshot_id=1, snapshot_version=1,
        mode=ProfilingMode.FULL, sample_rate=1.0,
        profiling_rules_version='4.0.0',
    )
    assert snap.status == ProfilingStatus.PENDING
    assert snap.tables_total == 0
    assert snap.columns_profiled == 0
    assert snap.pii_columns_found == 0
    assert snap.id is None
    assert snap.started_at is None


# ── 5. ConfidenceScore (frozen) ───────────────────────────────────────────────

def test_confidence_score_is_frozen():
    cs = ConfidenceScore(
        classification='Master',
        confidence=0.92,
        evidence=('Evidence A', 'Evidence B'),
        rule_version='4.0.0',
        competing=({'classification': 'Transactional', 'confidence': 0.45},),
    )
    assert cs.classification == 'Master'
    assert cs.confidence == 0.92
    assert isinstance(cs.evidence, tuple)
    assert isinstance(cs.competing, tuple)
    with pytest.raises(AttributeError):
        cs.confidence = 0.5  # type: ignore


# ── 6. Enum coverage checks ───────────────────────────────────────────────────

def test_table_class_has_all_seven():
    values = {tc.value for tc in TableClass}
    assert values == {'Reference', 'Master', 'Transactional', 'Audit', 'Staging', 'Reporting', 'Unknown'}


def test_semantic_type_has_all_fourteen():
    values = {st.value for st in SemanticType}
    expected = {'EMAIL', 'PHONE', 'SSN', 'ID', 'AMOUNT', 'COUNT', 'DATE',
                'STATUS', 'CODE', 'FLAG', 'NAME', 'TEXT', 'BINARY', 'UNKNOWN'}
    assert values == expected


# ── 7. classify_column — EMAIL ────────────────────────────────────────────────

def test_classify_column_email_from_match_rate_and_pii():
    cp = _col('customer_email', 'TEXT', pii_name_heuristic=True, email_match_rate=0.95)
    r = classify_column(cp)
    assert r.classification == SemanticType.EMAIL.value
    assert r.confidence >= 0.90


def test_classify_column_email_from_match_rate_only():
    cp = _col('addr', 'TEXT', email_match_rate=0.88)
    r = classify_column(cp)
    assert r.classification == SemanticType.EMAIL.value
    assert r.confidence >= 0.85


# ── 8. classify_column — PHONE ───────────────────────────────────────────────

def test_classify_column_phone_from_pii_and_rate():
    cp = _col('mobile_number', 'TEXT', pii_name_heuristic=True, phone_match_rate=0.82)
    r = classify_column(cp)
    assert r.classification == SemanticType.PHONE.value
    assert r.confidence >= 0.88


# ── 9. classify_column — ID ──────────────────────────────────────────────────

def test_classify_column_id_from_primary_key():
    cp = _col('order_id', 'INTEGER', is_primary_key=True)
    r = classify_column(cp)
    assert r.classification == SemanticType.ID.value
    assert r.confidence == 1.0


def test_classify_column_id_from_name_token():
    cp = _col('customer_uuid', 'TEXT',
              cardinality_tier=CardinalityTier.UNIQUE,
              uniqueness_score=0.999)
    r = classify_column(cp)
    assert r.classification == SemanticType.ID.value
    assert r.confidence >= 0.85


def test_classify_column_id_from_guid_match_rate():
    cp = _col('record_id', 'TEXT', guid_match_rate=0.97)
    r = classify_column(cp)
    assert r.classification == SemanticType.ID.value


# ── 10. classify_column — AMOUNT ─────────────────────────────────────────────

def test_classify_column_amount_from_name_token():
    cp = _col('total_amount', 'DECIMAL')
    r = classify_column(cp)
    assert r.classification == SemanticType.AMOUNT.value
    assert r.confidence >= 0.88


def test_classify_column_amount_from_payment_token():
    cp = _col('payment_balance', 'DECIMAL')
    r = classify_column(cp)
    assert r.classification == SemanticType.AMOUNT.value


# ── 11. classify_column — STATUS ─────────────────────────────────────────────

def test_classify_column_status_from_cardinality_and_name():
    cp = _col('enrollment_status', 'TEXT',
              cardinality_tier=CardinalityTier.LOW,
              top_values_coverage=0.98)
    r = classify_column(cp)
    assert r.classification == SemanticType.STATUS.value
    assert r.confidence >= 0.80


# ── 12. classify_column — CODE ───────────────────────────────────────────────

def test_classify_column_code_from_name_and_cardinality():
    cp = _col('country_code', 'TEXT', cardinality_tier=CardinalityTier.LOW)
    r = classify_column(cp)
    assert r.classification == SemanticType.CODE.value
    assert r.confidence >= 0.78


# ── Additional semantic type checks ──────────────────────────────────────────

def test_classify_column_date_from_datetime_schema():
    cp = _col('created_at', 'DATETIME')
    r = classify_column(cp)
    assert r.classification == SemanticType.DATE.value
    assert r.confidence == 1.0


def test_classify_column_flag_from_boolean_schema():
    cp = _col('is_active', 'BOOLEAN')
    r = classify_column(cp)
    assert r.classification == SemanticType.FLAG.value
    assert r.confidence == 1.0


def test_classify_column_binary_from_schema():
    cp = _col('file_blob', 'BINARY')
    r = classify_column(cp)
    assert r.classification == SemanticType.BINARY.value
    assert r.confidence == 1.0


def test_classify_column_unknown_for_unrecognised_type():
    cp = _col('misc_field', 'OTHER')
    r = classify_column(cp)
    assert r.classification == SemanticType.UNKNOWN.value
    assert r.confidence == 0.0


def test_classify_column_evidence_is_populated():
    cp = _col('order_id', 'INTEGER', is_primary_key=True)
    r = classify_column(cp)
    assert len(r.evidence) >= 1
    assert all(isinstance(e, str) for e in r.evidence)


def test_classify_column_rule_version_is_set():
    cp = _col('amount', 'DECIMAL')
    r = classify_column(cp)
    assert r.rule_version == '4.0.0'


# ── 13. classify_table — Reference ───────────────────────────────────────────

def test_classify_table_reference():
    tp = _table('ref_status_codes',
                row_count_tier=RowCountTier.TINY,
                column_count=4, fk_count=0,
                referenced_by_count=8,
                data_currency=DataCurrency.HISTORICAL)
    r = classify_table(tp)
    assert r.classification == TableClass.REFERENCE.value
    assert r.confidence >= 0.75


# ── 14. classify_table — Master ───────────────────────────────────────────────

def test_classify_table_master():
    tp = _table('students',
                row_count_tier=RowCountTier.SMALL,
                column_count=15, fk_count=2,
                referenced_by_count=12,
                has_identity_column=True,
                data_currency=DataCurrency.ACTIVE)
    r = classify_table(tp)
    assert r.classification == TableClass.MASTER.value
    assert r.confidence >= 0.70


# ── 15. classify_table — Transactional ───────────────────────────────────────

def test_classify_table_transactional():
    tp = _table('payments',
                row_count_tier=RowCountTier.LARGE,
                has_date_column=True, fk_count=3,
                referenced_by_count=0,
                data_currency=DataCurrency.ACTIVE)
    r = classify_table(tp)
    assert r.classification == TableClass.TRANSACTIONAL.value
    assert r.confidence >= 0.60


# ── 16. classify_table — Audit ───────────────────────────────────────────────

def test_classify_table_audit():
    tp = _table('audit_log',
                row_count_tier=RowCountTier.LARGE,
                has_date_column=True,
                referenced_by_count=0,
                data_currency=DataCurrency.ACTIVE)
    r = classify_table(tp)
    assert r.classification == TableClass.AUDIT.value
    assert r.confidence >= 0.55


# ── 17. classify_table — Staging ─────────────────────────────────────────────

def test_classify_table_staging_from_prefix():
    tp = _table('stg_enrollment_data',
                row_count_tier=RowCountTier.EMPTY,
                fk_count=0, referenced_by_count=0)
    r = classify_table(tp)
    assert r.classification == TableClass.STAGING.value
    assert r.confidence >= 0.65


def test_classify_table_staging_from_date_stamp():
    tp = _table('student_export_20240315',
                row_count_tier=RowCountTier.SMALL,
                fk_count=0, referenced_by_count=0)
    r = classify_table(tp)
    assert r.classification == TableClass.STAGING.value


# ── 18. classify_table — Reporting ───────────────────────────────────────────

def test_classify_table_reporting_from_fact_prefix():
    tp = _table('fact_enrollment_summary',
                row_count_tier=RowCountTier.MEDIUM,
                column_count=20, fk_count=6,
                has_identity_column=False,
                referenced_by_count=0)
    r = classify_table(tp)
    assert r.classification == TableClass.REPORTING.value
    assert r.confidence >= 0.55


# ── 19. classify_table — Unknown fallback ────────────────────────────────────

def test_classify_table_unknown_fallback():
    tp = _table('misc_data',
                row_count_tier=RowCountTier.TINY,
                column_count=3, fk_count=0,
                referenced_by_count=0,
                has_date_column=False)
    r = classify_table(tp)
    assert r.classification == TableClass.UNKNOWN.value
    assert r.confidence == 0.0


# ── 20. SQL query builders — correct quoting ─────────────────────────────────

def test_row_count_query_quotes_identifiers():
    sql = build_row_count_query('dbo.orders')
    assert '[dbo]' in sql
    assert '[orders]' in sql
    assert 'COUNT_BIG' in sql
    assert 'WITH (NOLOCK)' in sql


def test_row_count_query_no_nolock():
    sql = build_row_count_query('dbo.orders', use_nolock=False)
    assert 'WITH (NOLOCK)' not in sql
    assert '[dbo].[orders]' in sql


def test_date_range_query_quotes_column():
    sql = build_date_range_query('dbo.enrollments', 'created_at')
    assert '[dbo].[enrollments]' in sql
    assert '[created_at]' in sql
    assert 'MIN(' in sql
    assert 'MAX(' in sql


def test_column_stats_text_includes_length_metrics():
    sql = build_column_stats_query('dbo.students', 'first_name', 'TEXT')
    assert '[dbo].[students]' in sql
    assert '[first_name]' in sql
    assert 'avg_length' in sql
    assert 'LEN(' in sql
    assert 'empty_string_count' in sql


def test_column_stats_decimal_includes_distribution_metrics():
    sql = build_column_stats_query('dbo.payments', 'amount', 'DECIMAL')
    assert '[amount]' in sql
    assert 'mean_value' in sql
    assert 'STDEV' in sql
    assert 'zero_count' in sql


def test_column_stats_datetime_includes_cast():
    sql = build_column_stats_query('dbo.orders', 'order_date', 'DATETIME')
    assert 'NVARCHAR' in sql
    assert 'min_value' in sql


def test_top_values_query_uses_limit():
    sql = build_top_values_query('dbo.students', 'status', limit=15)
    assert 'TOP (15)' in sql
    assert '[dbo].[students]' in sql
    assert '[status]' in sql
    assert 'COUNT_BIG' in sql
    assert 'GROUP BY' in sql
    assert 'ORDER BY' in sql


def test_sample_values_query_uses_tablesample():
    sql = build_sample_values_query('dbo.students', 'first_name', limit=5, sample_percent=10)
    assert 'TOP (5)' in sql
    assert 'TABLESAMPLE (10 PERCENT)' in sql
    assert 'NEWID()' in sql
    assert '[first_name]' in sql


# ── 21. Unsafe identifiers are rejected ──────────────────────────────────────

def test_bracket_injection_in_table_rejected():
    with pytest.raises(ValueError):
        build_row_count_query('dbo.orders]; DROP TABLE orders--')


def test_bracket_injection_in_schema_rejected():
    with pytest.raises(ValueError):
        build_row_count_query('dbo].orders')


def test_null_byte_in_identifier_rejected():
    with pytest.raises(ValueError):
        build_row_count_query('dbo.\x00orders')


def test_invalid_fqn_no_dot_rejected():
    with pytest.raises(ValueError):
        build_row_count_query('orders_without_schema')


def test_invalid_fqn_empty_schema_rejected():
    with pytest.raises(ValueError):
        build_row_count_query('.orders')


def test_top_values_limit_zero_rejected():
    with pytest.raises(ValueError):
        build_top_values_query('dbo.t', 'col', limit=0)


def test_top_values_limit_too_large_rejected():
    with pytest.raises(ValueError):
        build_top_values_query('dbo.t', 'col', limit=99_999)


def test_sample_percent_zero_rejected():
    with pytest.raises(ValueError):
        build_sample_values_query('dbo.t', 'col', sample_percent=0)


def test_sample_percent_over_100_rejected():
    with pytest.raises(ValueError):
        build_sample_values_query('dbo.t', 'col', sample_percent=101)
