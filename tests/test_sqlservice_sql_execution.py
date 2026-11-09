"""
Focused unit tests — SQL rebuild Phase 3, Step 3: bounded execution of an
already-validated read-only SQL Server query (core/sqlservice/sql_execution.py).

No real SQL Server, no real SQLite, no OpenAI call. execute_validated_sql()'s
two SQLite-backed dependencies (get_retrieval_build, get_connection_config)
are monkeypatched directly on the sql_execution module — the same pattern
core.sqlservice.retrieval_coordinator's own tests already use for
persist_retrieval_build/get_latest_snapshot — and its live-connection
dependency is exercised entirely through a fake DBAPI2-style
connection/cursor pair via the injectable `connection_factory` and `clock`
parameters (requirement 14). Never touches pyodbc or a real database file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_execution.py -v
"""

import datetime
import os
import uuid
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import core.sqlservice.sql_execution as sql_execution_module
from core.sqlservice.retrieval_store import StoredRetrievalBuild
from core.sqlservice.sql_execution import (
    CATEGORY_CONNECTIVITY,
    CATEGORY_EXECUTION_ERROR,
    CATEGORY_PERMISSION,
    CATEGORY_SCHEMA_CHANGED,
    CATEGORY_TIMEOUT,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    TRUNCATION_REASON_CELL_CHARACTERS,
    TRUNCATION_REASON_ROW_LIMIT,
    TRUNCATION_REASON_TOTAL_CHARACTERS,
    execute_validated_sql,
)
from core.sqlservice.sql_validation import STATUS_OK as VALID_STATUS_OK
from core.sqlservice.sql_validation import STATUS_REJECTED as VALID_STATUS_REJECTED
from core.sqlservice.sql_validation import SqlValidationResult

SOURCE_ID = 10
USER_ID = "user-1"
BUILD_ID = 1
FINGERPRINT = "fp-1"


# ── Fakes (no pyodbc, no SQLite) ────────────────────────────────────────────


class FakeCursor:
    def __init__(self, columns, rows, error=None):
        self._columns = columns
        self._rows = list(rows)
        self._error = error
        self.description = None
        self.executed_sql = None
        self.execute_call_count = 0
        self.fetchmany_calls = []
        self.closed = False

    def execute(self, sql):
        self.execute_call_count += 1
        self.executed_sql = sql
        if self._error is not None:
            raise self._error
        self.description = [(name,) for name in self._columns]

    def fetchmany(self, n):
        self.fetchmany_calls.append(n)
        batch, self._rows = self._rows[:n], self._rows[n:]
        return batch

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.timeout = None
        self.rollback_called = 0
        self.close_called = 0
        self.commit_called = 0

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_called += 1

    def close(self):
        self.close_called += 1

    def commit(self):
        self.commit_called += 1


def _factory_for(connection):
    calls = []

    def factory(config, timeout_s):
        calls.append((config, timeout_s))
        return connection

    factory.calls = calls
    return factory


def _raising_factory(exc):
    def factory(config, timeout_s):
        raise exc

    return factory


def _fake_clock(*values):
    it = iter(values)
    return lambda: next(it)


# ── Domain fakes ─────────────────────────────────────────────────────────


def _validation_result(
    sql="SELECT id FROM dbo.students",
    *,
    status=VALID_STATUS_OK,
    build_id=BUILD_ID,
    schema_fingerprint=FINGERPRINT,
    fqns=("dbo.students",),
):
    return SqlValidationResult(
        status=status,
        sql=sql,
        canonical_referenced_object_fqns=tuple(fqns),
        statement_type="Select" if status == VALID_STATUS_OK else None,
        build_id=build_id,
        schema_fingerprint=schema_fingerprint,
        warnings=(),
        error=None if status == VALID_STATUS_OK else "rejected upstream",
    )


def _build_row(*, id=BUILD_ID, source_id=SOURCE_ID, schema_fingerprint=FINGERPRINT):
    return StoredRetrievalBuild(
        id=id, source_id=source_id, snapshot_id=1, snapshot_version=1,
        fingerprint_version=1, schema_fingerprint=schema_fingerprint,
        table_set_hash="a" * 64, document_set_hash="b" * 64,
        status="READY", document_count=1, error_message=None,
        created_at="2026-01-01T00:00:00+00:00", completed_at="2026-01-01T00:00:01+00:00",
    )


def _connection_record(*, source_type="mssql", is_active=True, source_status="ACTIVE"):
    return {
        "source_type": source_type,
        "source_category": "relational_db",
        "display_name": "Test MSSQL",
        "is_active": is_active,
        "source_status": source_status,
        "capabilities": ["sql_query"],
        "live_query_enabled": True,
        "params": {
            "host": "db.internal", "port": 1433, "database": "AppDb",
            "username": "svc", "password": "S3cr3tPWD!",
        },
    }


@pytest.fixture(autouse=True)
def _default_lookups(monkeypatch):
    """Happy-path stand-ins for the two SQLite-backed dependencies. Individual
    tests override one or both via monkeypatch.setattr for failure cases."""
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: _build_row())
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: _connection_record())
    yield


def _run(validation_result=None, connection=None, factory=None, **kwargs):
    validation_result = validation_result or _validation_result()
    if factory is None:
        factory = _factory_for(connection) if connection is not None else _factory_for(FakeConnection(FakeCursor((), ())))
    kwargs.setdefault("clock", _fake_clock(100.0, 100.0))
    return execute_validated_sql(
        validation_result, SOURCE_ID, USER_ID, connection_factory=factory, **kwargs
    )


# ── 1. Successful SELECT ─────────────────────────────────────────────────


def test_successful_select():
    cursor = FakeCursor(("id", "name"), [(1, "Alice"), (2, "Bob")])
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.status == STATUS_OK
    assert result.columns == ("id", "name")
    assert result.rows == ((1, "Alice"), (2, "Bob"))
    assert result.row_count == 2
    assert result.truncated is False
    assert result.truncation_reason is None
    assert result.error is None and result.error_category is None
    assert result.source_id == SOURCE_ID
    assert result.build_id == BUILD_ID
    assert result.schema_fingerprint == FINGERPRINT
    assert result.canonical_referenced_object_fqns == ("dbo.students",)


# ── 2. Invalid validation result rejected before connection ────────────────


def test_rejected_before_connection_when_validation_not_ok(monkeypatch):
    build_calls = []
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: build_calls.append(a) or _build_row())
    factory = _raising_factory(AssertionError("connection must never be opened"))

    result = _run(
        validation_result=_validation_result(status=VALID_STATUS_REJECTED, sql=""),
        factory=factory,
    )

    assert result.status == STATUS_REJECTED
    assert "not" in result.error
    assert build_calls == []  # no DB lookups either — short-circuited immediately


def test_rejected_before_connection_when_sql_blank():
    factory = _raising_factory(AssertionError("connection must never be opened"))
    validation_result = SqlValidationResult(
        status=VALID_STATUS_OK, sql="   ", canonical_referenced_object_fqns=(),
        statement_type="Select", build_id=BUILD_ID, schema_fingerprint=FINGERPRINT,
        warnings=(), error=None,
    )
    result = _run(validation_result=validation_result, factory=factory)
    assert result.status == STATUS_REJECTED
    assert "blank" in result.error


def test_rejected_when_sql_exceeds_max_sql_characters():
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory, max_sql_characters=5)
    assert result.status == STATUS_REJECTED
    assert "exceeds" in result.error


# ── 3. Ownership / source / type / status failures ──────────────────────────


def test_rejected_when_build_unowned_or_missing(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: None)
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "does not own" in result.error or "does not exist" in result.error


def test_rejected_when_schema_fingerprint_mismatched(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: _build_row(schema_fingerprint="fp-OLD"))
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "fingerprint" in result.error


def test_rejected_when_source_not_found(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: None)
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "not found" in result.error


def test_rejected_when_source_inactive(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: _connection_record(is_active=False))
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "disabled" in result.error


def test_rejected_when_source_status_error(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: _connection_record(source_status="ERROR"))
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "disabled" in result.error


def test_rejected_when_source_not_mssql(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: _connection_record(source_type="postgresql"))
    factory = _raising_factory(AssertionError("connection must never be opened"))
    result = _run(factory=factory)
    assert result.status == STATUS_REJECTED
    assert "mssql" in result.error


# ── 4. Exact SQL passed unchanged ──────────────────────────────────────────


def test_exact_sql_passed_unchanged():
    sql = "SELECT   id ,  name  FROM dbo.students  -- odd spacing preserved"
    # Bypass validator's own comment rule by constructing the result directly —
    # this module trusts an already-OK SqlValidationResult verbatim.
    validation_result = SqlValidationResult(
        status=VALID_STATUS_OK, sql=sql, canonical_referenced_object_fqns=("dbo.students",),
        statement_type="Select", build_id=BUILD_ID, schema_fingerprint=FINGERPRINT,
        warnings=(), error=None,
    )
    cursor = FakeCursor(("id", "name"), [(1, "Alice")])
    connection = FakeConnection(cursor)
    _run(validation_result=validation_result, connection=connection)
    assert cursor.executed_sql == sql


# ── 5. Cursor query timeout configured ─────────────────────────────────────


def test_query_timeout_configured_on_connection():
    cursor = FakeCursor(("id",), [(1,)])
    connection = FakeConnection(cursor)
    _run(connection=connection, query_timeout_seconds=17)
    assert connection.timeout == 17


# ── 6. Bounded fetchmany behavior ──────────────────────────────────────────


def test_fetchmany_is_batched_not_unbounded():
    rows = [(i,) for i in range(5)]
    cursor = FakeCursor(("id",), rows)
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_rows=100, fetch_batch_size=2)

    assert result.row_count == 5
    assert len(cursor.fetchmany_calls) > 1
    assert all(n <= 2 for n in cursor.fetchmany_calls)


# ── 7. Row truncation detection ─────────────────────────────────────────────


def test_row_truncation_detected_and_capped():
    rows = [(i,) for i in range(4)]  # one more than max_rows below
    cursor = FakeCursor(("id",), rows)
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_rows=3)

    assert result.status == STATUS_OK
    assert result.row_count == 3
    assert result.truncated is True
    assert TRUNCATION_REASON_ROW_LIMIT in result.truncation_reason


def test_no_truncation_when_exactly_at_limit():
    rows = [(i,) for i in range(3)]
    cursor = FakeCursor(("id",), rows)
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_rows=3)

    assert result.row_count == 3
    assert result.truncated is False
    assert result.truncation_reason is None


# ── 8. Column-limit failure ─────────────────────────────────────────────────


def test_column_limit_rejected_before_fetch():
    cursor = FakeCursor(("a", "b", "c"), [(1, 2, 3)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_columns=2)

    assert result.status == STATUS_REJECTED
    assert "column" in result.error
    assert cursor.fetchmany_calls == []
    # cleanup still ran even though rejected mid-flight
    assert connection.rollback_called == 1
    assert connection.close_called == 1
    assert cursor.closed is True


# ── 9. Per-cell and total-character truncation ─────────────────────────────


def test_per_cell_character_truncation():
    cursor = FakeCursor(("note",), [("x" * 20,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_cell_characters=5)

    assert result.status == STATUS_OK
    assert result.rows == (("x" * 5,),)
    assert result.truncated is True
    assert TRUNCATION_REASON_CELL_CHARACTERS in result.truncation_reason


def test_total_character_budget_truncates_whole_rows():
    cursor = FakeCursor(("note",), [("a" * 50,), ("b" * 50,), ("c" * 50,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=60)

    assert result.status == STATUS_OK
    assert result.row_count == 1
    assert result.rows == (("a" * 50,),)
    assert result.truncated is True
    assert TRUNCATION_REASON_TOTAL_CHARACTERS in result.truncation_reason


# ── 9A. Total-character limit is a true hard boundary (Phase 3, Step 3A) ───


def test_first_row_exceeding_total_budget_returns_zero_rows():
    """The old behavior let one oversized first row through anyway. The
    budget must never be exceeded, including by the first row: zero rows,
    truncated, columns retained, and this is NOT a database error."""
    cursor = FakeCursor(("note",), [("z" * 100,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=10)

    assert result.status == STATUS_OK
    assert result.error is None and result.error_category is None
    assert result.columns == ("note",)
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is True
    assert result.truncation_reason == TRUNCATION_REASON_TOTAL_CHARACTERS


def test_serialized_content_never_exceeds_configured_maximum():
    cursor = FakeCursor(("note",), [("a" * 17,), ("b" * 17,), ("c" * 17,), ("d" * 17,)])
    connection = FakeConnection(cursor)
    max_total = 40
    result = _run(connection=connection, max_total_characters=max_total)

    serialized_characters = sum(
        len(str(value)) for row in result.rows for value in row if value is not None
    )
    assert serialized_characters <= max_total
    assert result.truncated is True


def test_exact_boundary_row_is_accepted():
    cursor = FakeCursor(("note",), [("x" * 50,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=50)

    assert result.status == STATUS_OK
    assert result.rows == (("x" * 50,),)
    assert result.row_count == 1
    assert result.truncated is False
    assert result.truncation_reason is None


def test_one_character_beyond_boundary_is_rejected():
    cursor = FakeCursor(("note",), [("x" * 50,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=49)

    assert result.status == STATUS_OK
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is True
    assert result.truncation_reason == TRUNCATION_REASON_TOTAL_CHARACTERS


def test_later_oversized_row_preserves_earlier_complete_rows():
    cursor = FakeCursor(("note",), [("a" * 10,), ("b" * 10,), ("c" * 100,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=25)

    assert result.status == STATUS_OK
    assert result.rows == (("a" * 10,), ("b" * 10,))
    assert result.row_count == 2
    assert result.truncated is True
    assert result.truncation_reason == TRUNCATION_REASON_TOTAL_CHARACTERS


def test_per_cell_truncation_can_make_a_row_fit():
    """A cell that would blow the total budget on its own can be brought
    inside budget by per-cell truncation happening first."""
    cursor = FakeCursor(("note",), [("y" * 1000,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_cell_characters=5, max_total_characters=5)

    assert result.status == STATUS_OK
    assert result.rows == (("y" * 5,),)
    assert result.row_count == 1
    assert result.truncated is True
    assert TRUNCATION_REASON_CELL_CHARACTERS in result.truncation_reason
    assert TRUNCATION_REASON_TOTAL_CHARACTERS not in result.truncation_reason


def test_cleanup_unchanged_when_total_budget_yields_zero_rows():
    """Rollback/cursor/connection cleanup must still run even when the
    total-character bound empties the resultset entirely."""
    cursor = FakeCursor(("note",), [("z" * 100,)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection, max_total_characters=10)

    assert result.row_count == 0
    assert cursor.closed is True
    assert connection.rollback_called == 1
    assert connection.close_called == 1
    assert connection.commit_called == 0


# ── 10. Duplicate columns preserved ─────────────────────────────────────────


def test_duplicate_column_names_preserved_by_position():
    cursor = FakeCursor(("id", "id"), [(1, 2)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.columns == ("id", "id")
    assert result.rows == ((1, 2),)  # positional tuple, never collapsed into a dict


# ── 11. Every supported value normalization ─────────────────────────────────


def test_every_supported_value_type_normalized():
    a_date = datetime.date(2026, 1, 1)
    a_time = datetime.time(9, 15, 0)
    a_datetime = datetime.datetime(2026, 1, 1, 12, 30, 0)
    a_uuid = uuid.UUID(int=1)
    a_binary = b"\x01\x02\xff"

    row = (None, True, 42, 3.14, Decimal("12.50"), a_date, a_time, a_datetime, a_uuid, a_binary, "hello")
    columns = ("n", "b", "i", "f", "d", "date", "time", "dt", "uuid", "bin", "s")
    cursor = FakeCursor(columns, [row])
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.rows == ((
        None, True, 42, 3.14, Decimal("12.50"),
        "2026-01-01", "09:15:00", "2026-01-01T12:30:00",
        "00000000-0000-0000-0000-000000000001", "0102ff", "hello",
    ),)
    assert result.warnings == ()


# ── 12. Unsupported value handling ──────────────────────────────────────────


class _Weird:
    pass


def test_unsupported_value_replaced_with_placeholder_and_warned():
    cursor = FakeCursor(("w",), [(_Weird(),)])
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.rows == (("<unsupported:_Weird>",),)
    assert len(result.warnings) == 1
    assert "_Weird" in result.warnings[0]
    assert "w" in result.warnings[0]


# ── 13. Error categorization ────────────────────────────────────────────────


class _DriverError(Exception):
    pass


@pytest.mark.parametrize(
    "exc, expected_category",
    [
        (_DriverError("HYT00", "Query timeout expired"), CATEGORY_TIMEOUT),
        (_DriverError("08001", "Unable to connect to data source"), CATEGORY_CONNECTIVITY),
        (_DriverError("28000", "Login failed for user 'svc'"), CATEGORY_PERMISSION),
        (_DriverError("42S02", "Invalid object name 'dbo.gone'"), CATEGORY_SCHEMA_CHANGED),
        (_DriverError("HY000", "Arithmetic overflow error"), CATEGORY_EXECUTION_ERROR),
    ],
)
def test_error_categorization(exc, expected_category):
    cursor = FakeCursor(("id",), [], error=exc)
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.status == STATUS_ERROR
    assert result.error_category == expected_category


def test_connection_open_failure_is_categorized_error():
    factory = _raising_factory(_DriverError("08001", "Unable to connect to data source"))
    result = _run(factory=factory)
    assert result.status == STATUS_ERROR
    assert result.error_category == CATEGORY_CONNECTIVITY


# ── 14. Credential redaction ────────────────────────────────────────────────


def test_error_message_never_leaks_credentials():
    secret_bearing = _DriverError(
        "28000",
        "Login failed for user 'svc'. Connection: Server=db.internal;UID=svc;PWD={S3cr3tPWD!}",
    )
    cursor = FakeCursor(("id",), [], error=secret_bearing)
    connection = FakeConnection(cursor)
    result = _run(connection=connection)

    assert result.status == STATUS_ERROR
    assert "S3cr3tPWD" not in result.error
    assert "PWD=" not in result.error
    assert "UID=" not in result.error
    assert "svc" not in result.error


# ── 15. Rollback after success and failure ──────────────────────────────────


def test_rollback_called_after_successful_select():
    cursor = FakeCursor(("id",), [(1,)])
    connection = FakeConnection(cursor)
    _run(connection=connection)
    assert connection.rollback_called == 1


def test_rollback_called_after_execution_error():
    cursor = FakeCursor(("id",), [], error=_DriverError("HY000", "boom"))
    connection = FakeConnection(cursor)
    _run(connection=connection)
    assert connection.rollback_called == 1


# ── 16. Commit never called ─────────────────────────────────────────────────


def test_commit_never_called_on_success_or_failure():
    cursor_ok = FakeCursor(("id",), [(1,)])
    connection_ok = FakeConnection(cursor_ok)
    _run(connection=connection_ok)
    assert connection_ok.commit_called == 0

    cursor_err = FakeCursor(("id",), [], error=_DriverError("HY000", "boom"))
    connection_err = FakeConnection(cursor_err)
    _run(connection=connection_err)
    assert connection_err.commit_called == 0


# ── 17. Cursor/connection always closed ─────────────────────────────────────


def test_cursor_and_connection_closed_on_success():
    cursor = FakeCursor(("id",), [(1,)])
    connection = FakeConnection(cursor)
    _run(connection=connection)
    assert cursor.closed is True
    assert connection.close_called == 1


def test_cursor_and_connection_closed_on_failure():
    cursor = FakeCursor(("id",), [], error=_DriverError("HY000", "boom"))
    connection = FakeConnection(cursor)
    _run(connection=connection)
    assert cursor.closed is True
    assert connection.close_called == 1


# ── 18. Deterministic injected timing ───────────────────────────────────────


def test_deterministic_duration_from_injected_clock():
    cursor = FakeCursor(("id",), [(1,)])
    connection = FakeConnection(cursor)
    factory = _factory_for(connection)
    result = execute_validated_sql(
        _validation_result(), SOURCE_ID, USER_ID,
        connection_factory=factory, clock=_fake_clock(100.0, 100.25),
    )
    assert result.duration_ms == 250


# ── 19. No retry or repair ──────────────────────────────────────────────────


def test_no_retry_on_execution_failure():
    cursor = FakeCursor(("id",), [], error=_DriverError("HY000", "boom"))
    connection = FakeConnection(cursor)
    _run(connection=connection)
    assert cursor.execute_call_count == 1


def test_sql_never_rewritten_even_when_it_would_overflow_row_limit():
    """Sanity check that no TOP/LIMIT or any other clause is ever injected —
    the exact validated string always reaches cursor.execute() unchanged,
    regardless of max_rows."""
    sql = "SELECT id FROM dbo.students"
    validation_result = _validation_result(sql=sql)
    cursor = FakeCursor(("id",), [(1,), (2,)])
    connection = FakeConnection(cursor)
    _run(validation_result=validation_result, connection=connection, max_rows=1)
    assert cursor.executed_sql == sql
