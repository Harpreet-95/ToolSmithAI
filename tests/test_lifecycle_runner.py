"""
End-to-end tests for core.lifecycle.runner.run_autonomous_lifecycle — the
Phase 11 Enterprise Autonomous Metadata Lifecycle.

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file, patched into every service module the runner touches, so
these tests exercise the actual dictionary/domain/entity/review-task/
notification/audit machinery rather than a hand-rolled stub.

Run from the project root:
    python -m pytest tests/test_lifecycle_runner.py -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-lifecycle-runner-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-lifecycle-runner-value")

import data.models as models
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.lifecycle.models import LifecycleTrigger, WorkflowStep
from core.lifecycle.runner import run_autonomous_lifecycle

_NOW = "2026-01-01T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.lifecycle_service",
    "data.review_task_service",
    "data.dictionary_service",
    "data.domain_service",
    "data.entity_service",
    "data.notification_service",
    "data.audit",
    "data.governance_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    """Build a fresh DB with the real schema, patch it into every module, seed source=1."""
    db_path = str(tmp_path / "lifecycle.db")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _col(name: str, data_type: str = "TEXT") -> ColumnInfo:
    return ColumnInfo(
        column_name=name, ordinal_position=1, data_type=data_type, raw_type=data_type,
        is_nullable=True, is_primary_key=False, is_identity=False,
    )


def _table(schema: str, name: str, cols: list[ColumnInfo]) -> TableInfo:
    return TableInfo(
        table_name=name, schema_name=schema, table_fqn=f"{schema}.{name}",
        table_type="TABLE", columns=cols,
    )


def _snapshot(tables: list[TableInfo]) -> SchemaSnapshot:
    return SchemaSnapshot(source_id=1, source_type="mssql", discovered_at=_NOW,
                           schemas=[SchemaInfo(schema_name="dbo", tables=tables)])


def _insert_schema_snapshot(db_path: str, snapshot: SchemaSnapshot, version: int) -> int:
    conn = _db_conn(db_path)
    snapshot_json = json.dumps(dataclasses.asdict(snapshot))
    cursor = conn.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, column_count, "
        " snapshot_json, discovered_at, created_at) VALUES (1, ?, 'mssql', ?, 0, ?, ?, ?, ?)",
        (version, snapshot.table_count, snapshot.column_count, snapshot_json, _NOW, _NOW),
    )
    conn.commit()
    snap_id = cursor.lastrowid
    conn.close()
    return snap_id


def _insert_profiling_snapshot(db_path: str, schema_snapshot_id: int, version: int, table_fqns: list[str]) -> int:
    conn = _db_conn(db_path)
    cursor = conn.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, created_at) VALUES (1, ?, ?, ?)",
        (schema_snapshot_id, version, _NOW),
    )
    prof_id = cursor.lastrowid
    for fqn in table_fqns:
        schema_name, table_name = fqn.split(".", 1)
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?)",
            (prof_id, fqn, table_name, schema_name, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return prof_id


def _dict_table_row(db_path: str, table_fqn: str) -> dict | None:
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM data_dictionary_tables WHERE source_id = 1 AND table_fqn = ?", (table_fqn,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _domain_row(db_path: str, table_fqn: str) -> dict | None:
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM domain_assignments WHERE source_id = 1 AND table_fqn = ?", (table_fqn,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _entity_row(db_path: str, table_fqn: str) -> dict | None:
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM entity_assignments WHERE source_id = 1 AND table_fqn = ?", (table_fqn,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _review_tasks(db_path: str) -> list[dict]:
    conn = _db_conn(db_path)
    rows = conn.execute("SELECT * FROM ai_semantic_suggestions ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _lifecycle_runs(db_path: str) -> list[dict]:
    conn = _db_conn(db_path)
    rows = conn.execute("SELECT * FROM metadata_lifecycle_runs ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _notifications(db_path: str) -> list[dict]:
    conn = _db_conn(db_path)
    rows = conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run(trigger=LifecycleTrigger.SCAN_COMPLETE):
    return run_autonomous_lifecycle(1, "u1", trigger=trigger)


# ---------------------------------------------------------------------------
# 1. First scan — every table is "added"
# ---------------------------------------------------------------------------

def test_first_scan_creates_dictionary_domain_entity_for_all_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id"), _col("name")])])
    schema_id = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id, 1, ["dbo.customers"])

    result = _run()

    assert result.status == "COMPLETE"
    assert result.change_set.is_first_scan is True
    assert result.change_set.added_tables == ["dbo.customers"]
    assert _dict_table_row(db, "dbo.customers") is not None
    assert _domain_row(db, "dbo.customers") is not None
    assert _entity_row(db, "dbo.customers") is not None


# ---------------------------------------------------------------------------
# 2. New table added — only the new table is refreshed
# ---------------------------------------------------------------------------

def test_new_table_only_new_table_refreshed(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()

    customers_before = _dict_table_row(db, "dbo.customers")

    snap2 = _snapshot([
        _table("dbo", "customers", [_col("id")]),
        _table("dbo", "orders", [_col("id"), _col("customer_id")]),
    ])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers", "dbo.orders"])

    result = _run()

    assert result.status == "COMPLETE"
    assert result.change_set.added_tables == ["dbo.orders"]
    assert _dict_table_row(db, "dbo.orders") is not None
    assert _domain_row(db, "dbo.orders") is not None
    # Unrelated, unchanged table was never touched by the second run.
    customers_after = _dict_table_row(db, "dbo.customers")
    assert customers_after == customers_before


# ---------------------------------------------------------------------------
# 3. Removed table — schema.drift review task created, existing rows untouched
# ---------------------------------------------------------------------------

def test_removed_table_creates_schema_drift_task_and_preserves_existing_rows(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([
        _table("dbo", "customers", [_col("id")]),
        _table("dbo", "legacy_orders", [_col("id")]),
    ])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers", "dbo.legacy_orders"])
    _run()

    legacy_before = _dict_table_row(db, "dbo.legacy_orders")
    assert legacy_before is not None

    snap2 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])

    result = _run()

    assert result.status == "COMPLETE"
    assert result.change_set.removed_tables == ["dbo.legacy_orders"]
    tasks = _review_tasks(db)
    drift_tasks = [t for t in tasks if t["object_type"] == "schema.drift"]
    assert len(drift_tasks) == 1
    assert drift_tasks[0]["table_fqn"] == "dbo.legacy_orders"
    assert drift_tasks[0]["column_name"] == ""
    # The removed table's dictionary row is not deleted — it was excluded from
    # the affected-table refresh entirely.
    legacy_after = _dict_table_row(db, "dbo.legacy_orders")
    assert legacy_after == legacy_before


# ---------------------------------------------------------------------------
# 4. Modified column — only affected table refreshed, schema.drift for removed column
# ---------------------------------------------------------------------------

def test_removed_column_creates_schema_drift_task(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id"), _col("fax_number")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()

    snap2 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])

    result = _run()

    assert result.change_set.modified_tables == ["dbo.customers"]
    tasks = [t for t in _review_tasks(db) if t["object_type"] == "schema.drift"]
    assert len(tasks) == 1
    assert tasks[0]["table_fqn"] == "dbo.customers"
    assert tasks[0]["column_name"] == "fax_number"


# ---------------------------------------------------------------------------
# 5. Human-edited dictionary row is preserved across an unrelated... and even
#    a same-table schema change.
# ---------------------------------------------------------------------------

def test_human_edited_dictionary_row_preserved(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id"), _col("name")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()

    # Simulate a human editing the table's dictionary entry.
    conn = _db_conn(db)
    conn.execute(
        "UPDATE data_dictionary_tables SET business_name = 'Human Named Customers', "
        "generation_method = 'human' WHERE source_id = 1 AND table_fqn = 'dbo.customers'"
    )
    conn.commit()
    conn.close()

    # Schema change on the SAME table — it will be in the affected set.
    snap2 = _snapshot([_table("dbo", "customers", [_col("id"), _col("name"), _col("email")])])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])

    result = _run()

    assert result.change_set.modified_tables == ["dbo.customers"]
    row = _dict_table_row(db, "dbo.customers")
    assert row["business_name"] == "Human Named Customers"
    assert row["generation_method"] == "human"


# ---------------------------------------------------------------------------
# 6. Human-locked domain assignment is preserved even when its table changes
# ---------------------------------------------------------------------------

def test_human_locked_domain_assignment_preserved(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()

    from data.domain_service import lock_domain_assignment
    locked = lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Locked Domain")
    assert locked is not None
    assert locked["assignment_source"] == "human"

    snap2 = _snapshot([_table("dbo", "customers", [_col("id"), _col("email")])])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])

    _run()

    row = _domain_row(db, "dbo.customers")
    assert row["domain"] == "Custom Locked Domain"
    assert row["assignment_source"] == "human"


# ---------------------------------------------------------------------------
# 7. Unaffected (unchanged) table is never touched at all
# ---------------------------------------------------------------------------

def test_unchanged_table_never_touched(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([
        _table("dbo", "customers", [_col("id")]),
        _table("dbo", "products", [_col("id")]),
    ])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers", "dbo.products"])
    _run()

    conn = _db_conn(db)
    conn.execute(
        "UPDATE data_dictionary_tables SET is_approved = 1, approved_by = 'u1' "
        "WHERE source_id = 1 AND table_fqn = 'dbo.products'"
    )
    conn.commit()
    conn.close()
    products_before = _dict_table_row(db, "dbo.products")

    snap2 = _snapshot([
        _table("dbo", "customers", [_col("id"), _col("email")]),
        _table("dbo", "products", [_col("id")]),
    ])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers", "dbo.products"])

    result = _run()

    assert result.change_set.modified_tables == ["dbo.customers"]
    assert "dbo.products" not in result.change_set.affected_table_fqns
    products_after = _dict_table_row(db, "dbo.products")
    assert products_after == products_before
    assert products_after["is_approved"] == 1


# ---------------------------------------------------------------------------
# 8. No duplicate review tasks when the same diff is processed twice
# ---------------------------------------------------------------------------

def test_no_duplicate_review_task_on_repeated_diff(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id"), _col("fax_number")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()

    snap2 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_2 = _insert_schema_snapshot(db, snap2, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])

    result1 = _run()
    assert result1.review_tasks_created == 1

    # Re-run without inserting a new snapshot — diffs the same (v1, v2) pair again.
    result2 = _run()
    assert result2.review_tasks_created == 0

    tasks = [t for t in _review_tasks(db) if t["object_type"] == "schema.drift"]
    assert len(tasks) == 1


# ---------------------------------------------------------------------------
# 9. Notifications: one per run-with-changes, zero when nothing changed
# ---------------------------------------------------------------------------

def test_notification_sent_only_when_changes_exist(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, ["dbo.customers"])
    _run()
    assert len(_notifications(db)) == 1

    # Identical snapshot content inserted as v2 — no actual diff.
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, ["dbo.customers"])
    result = _run()

    assert result.change_set.has_changes is False
    assert len(_notifications(db)) == 1  # unchanged — no new notification


# ---------------------------------------------------------------------------
# 10. Relationships / knowledge graph / dashboard steps are documented no-ops
# ---------------------------------------------------------------------------

def test_noop_steps_recorded_with_no_side_effects(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id, 1, ["dbo.customers"])

    result = _run()

    by_step = {s.step: s for s in result.steps}
    assert by_step[WorkflowStep.REFRESH_RELATIONSHIPS].status == "SKIPPED_NOOP"
    assert by_step[WorkflowStep.REFRESH_KNOWLEDGE_GRAPH].status == "SKIPPED_NOOP"
    assert by_step[WorkflowStep.UPDATE_DASHBOARD].status == "SKIPPED_NOOP"

    conn = _db_conn(db)
    rel_count = conn.execute("SELECT COUNT(*) AS c FROM table_relationships").fetchone()["c"]
    conn.close()
    assert rel_count == 0


# ---------------------------------------------------------------------------
# 11. metadata_lifecycle_runs row is populated correctly
# ---------------------------------------------------------------------------

def test_lifecycle_run_recorded_with_correct_counts(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id, 1, ["dbo.customers"])

    result = _run(trigger=LifecycleTrigger.MANUAL)

    runs = _lifecycle_runs(db)
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "COMPLETE"
    assert run["trigger_event"] == "manual"
    assert run["tables_added_count"] == 1
    assert run["source_id"] == 1
    assert run["id"] == result.run_id
    steps = json.loads(run["steps_executed_json"])
    assert len(steps) == 10


# ---------------------------------------------------------------------------
# 12. A hard failure in a refresh step is recorded, not raised
# ---------------------------------------------------------------------------

def test_dictionary_refresh_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id, 1, ["dbo.customers"])

    with patch(
        "data.dictionary_service.generate_and_save_dictionary",
        side_effect=RuntimeError("boom"),
    ):
        result = _run()

    assert result.status == "FAILED"
    assert "boom" in result.error_message
    runs = _lifecycle_runs(db)
    assert len(runs) == 1
    assert runs[0]["status"] == "FAILED"
    assert "boom" in runs[0]["error_message"]
