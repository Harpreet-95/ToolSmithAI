"""
Focused tests — Phase 7, Step 6E3B4, Part 2/4: the MSSQL bulk complete-
definition capture fix (data.view_semantics_service.capture_complete_
mssql_view_definitions) and its wiring into discover_and_persist_view_
semantics / _upsert_view_semantic_evidence.

No live SQL Server connection anywhere in this file. core.live.connection_
resolver.LiveConnectionResolver.resolve and data.query_execution_service.
_execute_with_timeout are monkeypatched directly — the same "construct the
collaborator's result by hand, never a real network/DB call" style every
other sibling test in this package already uses (see tests/test_
investigation_service.py's own connection-layer mocking for the same
module family).
"""
from __future__ import annotations

import logging
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-view-semantics-capture-secret-123456")
os.environ.setdefault("USER_ID_SALT", "test-view-semantics-capture-salt-1234567")

import data.db as db_module
import data.models as models
from data.db import get_connection
import data.view_semantics_service as vss
from core.live.models import ConnectionContext, ResolutionResult, ResolutionStatus
from core.connectors.base import DataSourceConfig

SOURCE_ID = 777
USER_ID = "capture-test-user"


@pytest.fixture(scope="module", autouse=True)
def _isolated_metadata_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("view_semantics_capture") / "test.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    yield
    monkeypatch.undo()


class _FakeConnector:
    """Stands in for core.connectors.relational.mssql.SQLServerConnector —
    open_connection() returns a sentinel; this module never actually talks
    to it (data.query_execution_service._execute_with_timeout is
    monkeypatched below, so no real cursor/execute ever runs)."""

    def open_connection(self, config):
        return object()


def _resolved_mssql_context():
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        context=ConnectionContext(
            source_id=SOURCE_ID, source_type="mssql", source_category="relational_db",
            display_name="fake mssql", connector_cls=_FakeConnector,
            config=DataSourceConfig(source_type="mssql", params={}), capabilities=frozenset({"sql_query"}),
        ),
        message="ok",
    )


def _patch_resolver(monkeypatch, result):
    import core.live.connection_resolver as resolver_module
    monkeypatch.setattr(resolver_module.LiveConnectionResolver, "resolve", lambda self, *a, **k: result)


def _patch_execute(monkeypatch, *, description=(("c",),), rows=(), error=None, timed_out=False):
    import data.query_execution_service as qes_module
    monkeypatch.setattr(
        qes_module, "_execute_with_timeout",
        lambda *a, **k: (description, list(rows), error, timed_out),
    )


# ── capture_complete_mssql_view_definitions ─────────────────────────────


def test_available_complete_definition_captured_with_length_and_hash(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    definition = "CREATE VIEW dbo.v_x AS SELECT 1 AS A"
    _patch_execute(monkeypatch, rows=[("dbo", "v_x", definition, 0)])

    result = vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)

    assert "dbo.v_x" in result
    entry = result["dbo.v_x"]
    assert entry["capture_status"] == vss.CAPTURE_STATUS_AVAILABLE_COMPLETE
    assert entry["definition_sql"] == definition
    assert entry["definition_length"] == len(definition)
    import hashlib
    assert entry["definition_checksum"] == hashlib.sha256(definition.encode("utf-8")).hexdigest()


def test_encrypted_definition_typed_status_no_text(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    _patch_execute(monkeypatch, rows=[("dbo", "v_enc", None, 1)])

    result = vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)

    entry = result["dbo.v_enc"]
    assert entry["capture_status"] == vss.CAPTURE_STATUS_ENCRYPTED
    assert entry["definition_sql"] is None


def test_null_not_encrypted_is_permission_denied_not_conflated_with_encrypted(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    _patch_execute(monkeypatch, rows=[("dbo", "v_noperm", None, 0)])

    result = vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)

    entry = result["dbo.v_noperm"]
    assert entry["capture_status"] == vss.CAPTURE_STATUS_INACCESSIBLE
    assert entry["capture_status"] != vss.CAPTURE_STATUS_ENCRYPTED


def test_blank_definition_is_empty_not_inaccessible(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    _patch_execute(monkeypatch, rows=[("dbo", "v_blank", "   ", 0)])

    result = vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)

    assert result["dbo.v_blank"]["capture_status"] == vss.CAPTURE_STATUS_EMPTY


def test_non_mssql_source_returns_empty_and_never_queries(monkeypatch):
    context = ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        context=ConnectionContext(
            source_id=SOURCE_ID, source_type="postgresql", source_category="relational_db",
            display_name="pg", connector_cls=_FakeConnector,
            config=DataSourceConfig(source_type="postgresql", params={}), capabilities=frozenset({"sql_query"}),
        ),
        message="ok",
    )
    _patch_resolver(monkeypatch, context)

    def _boom(*a, **k):
        raise AssertionError("must never execute a query for a non-mssql source")

    import data.query_execution_service as qes_module
    monkeypatch.setattr(qes_module, "_execute_with_timeout", _boom)

    assert vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID) == {}


def test_unresolved_source_returns_empty(monkeypatch):
    _patch_resolver(monkeypatch, ResolutionResult(status=ResolutionStatus.NOT_FOUND, context=None, message="no"))
    assert vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID) == {}


def test_query_timeout_returns_empty_never_raises(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    _patch_execute(monkeypatch, timed_out=True)
    assert vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID) == {}


def test_query_error_returns_empty_never_raises(monkeypatch):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    _patch_execute(monkeypatch, error="connection reset")
    assert vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID) == {}


def test_bounded_row_count_logs_warning_never_crashes(monkeypatch, caplog):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    # one more row than the bound -- _execute_with_timeout's own row_limit+1
    # fetchmany contract is what signals "more rows exist than the bound".
    overflow_rows = [("dbo", f"v_{i}", f"SELECT {i}", 0) for i in range(vss._MAX_MSSQL_VIEWS_PER_CAPTURE + 1)]
    _patch_execute(monkeypatch, rows=overflow_rows)
    with caplog.at_level(logging.WARNING):
        result = vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)
    assert len(result) == vss._MAX_MSSQL_VIEWS_PER_CAPTURE
    assert any("bound" in r.message for r in caplog.records)


def test_no_definition_text_ever_appears_in_logs(monkeypatch, caplog):
    _patch_resolver(monkeypatch, _resolved_mssql_context())
    secret_marker = "ZzUNIQUE_DEFINITION_TEXT_MARKER_zZ"
    _patch_execute(monkeypatch, error=f"failure referencing {secret_marker} somehow")
    with caplog.at_level(logging.WARNING):
        vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID)
    # The warning path logs only a generic message -- never interpolates
    # exec_error or any row content into the log line.
    assert all(secret_marker not in r.message for r in caplog.records)


def test_connection_open_failure_returns_empty(monkeypatch):
    class _BrokenConnector:
        def open_connection(self, config):
            raise RuntimeError("connection refused")

    context = ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        context=ConnectionContext(
            source_id=SOURCE_ID, source_type="mssql", source_category="relational_db",
            display_name="fake", connector_cls=_BrokenConnector,
            config=DataSourceConfig(source_type="mssql", params={}), capabilities=frozenset({"sql_query"}),
        ),
        message="ok",
    )
    _patch_resolver(monkeypatch, context)
    assert vss.capture_complete_mssql_view_definitions(SOURCE_ID, USER_ID) == {}


# ── _upsert_view_semantic_evidence persistence (real sqlite fixture DB) ──
#
# view_semantic_evidence has real FK constraints to data_source_connections
# and schema_snapshots (FK enforcement is ON by default — see data.db.
# get_connection's own PRAGMA foreign_keys = ON) -- seed both once so the
# persistence tests below don't trip a foreign-key violation.


@pytest.fixture(scope="module", autouse=True)
def _seed_source_and_snapshots(_isolated_metadata_db):
    import json as _json
    from datetime import datetime, timezone

    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
            " config_schema_version, capabilities_json, metadata_json, source_status, "
            " is_active, created_at, updated_at) "
            "VALUES (?, ?, 'capture test source', 'mssql', 'relational_db', '{}', 1, "
            " '[\"schema_discovery\",\"sql_query\"]', '{}', 'ACTIVE', 1, ?, ?)",
            (SOURCE_ID, USER_ID, now, now),
        )
        for snapshot_id in (1, 2):
            conn.execute(
                "INSERT INTO schema_snapshots "
                "(id, source_id, snapshot_version, source_type, table_count, view_count, "
                " column_count, snapshot_json, discovered_at, created_at) "
                "VALUES (?, ?, ?, 'mssql', 0, 0, 0, ?, ?, ?)",
                (snapshot_id, SOURCE_ID, snapshot_id, _json.dumps({}), now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _row_for(table_fqn):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM view_semantic_evidence WHERE source_id = ? AND table_fqn = ?",
            (SOURCE_ID, table_fqn),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_upsert_persists_capture_status_and_length():
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=1, table_fqn="dbo.v_persist",
        definition_sql="SELECT 1 AS A", referenced_tables=[], restriction=None,
        naming_penalty_score=0.0, is_curated=False, discovered_at="2026-10-02T00:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_AVAILABLE_COMPLETE, definition_length=13,
    )
    row = _row_for("dbo.v_persist")
    assert row is not None
    assert row["definition_capture_status"] == vss.CAPTURE_STATUS_AVAILABLE_COMPLETE
    assert row["definition_length"] == 13
    assert row["definition_checksum"] is not None


def test_upsert_with_none_definition_persists_typed_status_only():
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=1, table_fqn="dbo.v_encrypted_row",
        definition_sql=None, referenced_tables=[], restriction=None,
        naming_penalty_score=0.0, is_curated=False, discovered_at="2026-10-02T00:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_ENCRYPTED, definition_length=None,
    )
    row = _row_for("dbo.v_encrypted_row")
    assert row["definition_sql"] is None
    assert row["definition_checksum"] is None
    assert row["definition_capture_status"] == vss.CAPTURE_STATUS_ENCRYPTED


def test_unchanged_definition_reuse_leaves_checksum_identical():
    definition = "SELECT u.Id FROM dbo.Users u"
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=1, table_fqn="dbo.v_stable",
        definition_sql=definition, referenced_tables=["dbo.Users"], restriction=None,
        naming_penalty_score=0.0, is_curated=True, discovered_at="2026-10-02T00:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_AVAILABLE_COMPLETE, definition_length=len(definition),
    )
    first = _row_for("dbo.v_stable")

    # Re-discovery with the IDENTICAL definition -- same checksum, same
    # status; nothing about this row's identity changes.
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=2, table_fqn="dbo.v_stable",
        definition_sql=definition, referenced_tables=["dbo.Users"], restriction=None,
        naming_penalty_score=0.0, is_curated=True, discovered_at="2026-10-02T01:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_AVAILABLE_COMPLETE, definition_length=len(definition),
    )
    second = _row_for("dbo.v_stable")
    assert second["definition_checksum"] == first["definition_checksum"]


def test_changed_definition_invalidates_checksum():
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=1, table_fqn="dbo.v_changes",
        definition_sql="SELECT u.Id FROM dbo.Users u", referenced_tables=["dbo.Users"], restriction=None,
        naming_penalty_score=0.0, is_curated=True, discovered_at="2026-10-02T00:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_LEGACY_UNKNOWN, definition_length=27,
    )
    before = _row_for("dbo.v_changes")

    # The truncated-prefix -> complete-definition case this whole fix
    # exists for: the SAME view, now captured completely.
    full_definition = "SELECT u.Id, u.Name, u.Email FROM dbo.Users u WHERE u.Active = 1"
    vss._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=2, table_fqn="dbo.v_changes",
        definition_sql=full_definition, referenced_tables=["dbo.Users"], restriction={"has_where": True},
        naming_penalty_score=0.0, is_curated=True, discovered_at="2026-10-02T01:00:00Z",
        definition_capture_status=vss.CAPTURE_STATUS_AVAILABLE_COMPLETE, definition_length=len(full_definition),
    )
    after = _row_for("dbo.v_changes")

    assert after["definition_checksum"] != before["definition_checksum"]
    assert after["definition_capture_status"] == vss.CAPTURE_STATUS_AVAILABLE_COMPLETE
    assert after["definition_length"] == len(full_definition)


def test_backward_compatible_upsert_without_new_columns(monkeypatch):
    # Simulates a database that has NOT picked up data.models' additive
    # migration yet (PRAGMA table_info won't list the two new columns) --
    # the pre-6E3B4 INSERT must still work, never crash.
    import data.view_semantics_service as vss_module

    real_get_connection = vss_module.get_connection

    class _ColumnHidingCursor:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def fetchall(self):
            rows = self._inner.fetchall()
            return [r for r in rows if r["name"] not in ("definition_capture_status", "definition_length")]

    class _ColumnHidingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            cur = self._inner.execute(sql, *args, **kwargs)
            if "PRAGMA table_info" in sql:
                return _ColumnHidingCursor(cur)
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(vss_module, "get_connection", lambda: _ColumnHidingConn(real_get_connection()))

    vss_module._upsert_view_semantic_evidence(
        source_id=SOURCE_ID, snapshot_id=1, table_fqn="dbo.v_legacy_schema",
        definition_sql="SELECT 1 AS A", referenced_tables=[], restriction=None,
        naming_penalty_score=0.0, is_curated=False, discovered_at="2026-10-02T00:00:00Z",
        definition_capture_status=vss_module.CAPTURE_STATUS_AVAILABLE_COMPLETE, definition_length=13,
    )
    row = _row_for("dbo.v_legacy_schema")
    assert row is not None
    assert row["definition_sql"] == "SELECT 1 AS A"
