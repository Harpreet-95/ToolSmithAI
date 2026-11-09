"""
Automatic Downstream Semantic Refresh After Profiling/Classification Changes.

Root cause traced: run_autonomous_lifecycle's change detection was schema-
diff-only (core.lifecycle.diff.diff_snapshots comparing two schema_snapshots
rows). A profiling/classification change with no accompanying schema change
(e.g. re-running full/batch profiling and getting a different table_class
for a table whose columns never changed) produced an empty ChangeSet, so
the whole lifecycle short-circuited to SKIPPED_NO_CHANGES — dictionary,
generated vocabulary, domain assignments, and entity assignments never
regenerated even though real new evidence existed.

Fix: core.lifecycle.diff.diff_profiling_classification (same ChangeSet
shape as diff_snapshots, comparing table_class across the two most recent
COMPLETE profiling snapshots instead of two schema snapshots) is merged
with the schema diff via merge_change_sets before the rest of
run_autonomous_lifecycle runs, completely unchanged. A new REFRESH_
VOCABULARY step (data.vocabulary_bootstrap_service.bootstrap_vocabulary)
was added alongside dictionary/domain/entity, all gated by the same
`affected` table set. data.profiling_service.continue_batch_profiling
fires run_autonomous_lifecycle(trigger=PROFILING_COMPLETE) exactly once,
right on the RUNNING -> COMPLETE transition — the one choke point every
profiling-advance path (scheduler tick, resumed run, manual "continue" API
call) already goes through.

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file, matching tests/test_lifecycle_runner.py's own convention.

Run from the project root:
    venv/Scripts/pytest tests/test_lifecycle_profiling_trigger.py -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-profiling-trigger-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-profiling-trigger-value")

import data.models as models
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.lifecycle.models import LifecycleTrigger, WorkflowStep
from core.lifecycle.runner import run_autonomous_lifecycle
from core.profiling.models import ProfilingStatus

_NOW = "2026-08-14T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.lifecycle_service",
    "data.review_task_service",
    "data.dictionary_service",
    "data.domain_service",
    "data.entity_service",
    "data.notification_service",
    "data.audit",
    "data.governance_service",
    "data.relationship_service",
    "data.vocabulary_bootstrap_service",
    "data.profiling_snapshot_resolver",
    "data.semantic_contract_service",
    "data.profiling_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch, db_name="lifecycle_profiling.db"):
    db_path = str(tmp_path / db_name)

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    for source_id in (1, 2):
        conn.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            " encrypted_config_json, config_schema_version, capabilities_json, "
            " metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (?,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
            (source_id, _NOW, _NOW),
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


def _snapshot(tables: list[TableInfo], source_id: int = 1) -> SchemaSnapshot:
    return SchemaSnapshot(source_id=source_id, source_type="mssql", discovered_at=_NOW,
                           schemas=[SchemaInfo(schema_name="dbo", tables=tables)])


def _insert_schema_snapshot(db_path: str, snapshot: SchemaSnapshot, version: int, source_id: int = 1) -> int:
    conn = _db_conn(db_path)
    snapshot_json = json.dumps(dataclasses.asdict(snapshot))
    cursor = conn.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, column_count, "
        " snapshot_json, discovered_at, created_at) VALUES (?, ?, 'mssql', ?, 0, ?, ?, ?, ?)",
        (source_id, version, snapshot.table_count, snapshot.column_count, snapshot_json, _NOW, _NOW),
    )
    conn.commit()
    snap_id = cursor.lastrowid
    conn.close()
    return snap_id


def _insert_profiling_snapshot(
    db_path: str, schema_snapshot_id: int, version: int,
    table_classes: dict[str, str | None], *, status: str = ProfilingStatus.COMPLETE.value, source_id: int = 1,
) -> int:
    """table_classes: {table_fqn: table_class}. status defaults to COMPLETE —
    the one status get_latest_two_complete_profiling_snapshots will ever
    read, matching what a real finished profiling run persists."""
    conn = _db_conn(db_path)
    cursor = conn.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, status, created_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, schema_snapshot_id, version, status, _NOW, _NOW),
    )
    prof_id = cursor.lastrowid
    for fqn, table_class in table_classes.items():
        schema_name, table_name = fqn.split(".", 1)
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (prof_id, source_id, fqn, table_name, schema_name, table_class, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return prof_id


def _dict_table_row(db_path: str, table_fqn: str, source_id: int = 1) -> dict | None:
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM data_dictionary_tables WHERE source_id = ? AND table_fqn = ?",
        (source_id, table_fqn),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _vocab_rows(db_path: str, source_id: int = 1) -> list[dict]:
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM generated_business_vocabulary WHERE source_id = ? ORDER BY id", (source_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _lifecycle_runs(db_path: str, source_id: int = 1) -> list[dict]:
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM metadata_lifecycle_runs WHERE source_id = ? ORDER BY id", (source_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run(source_id=1, trigger=LifecycleTrigger.MANUAL):
    return run_autonomous_lifecycle(source_id, "u1", trigger=trigger)


# ---------------------------------------------------------------------------
# 1. Profiling-only change (no schema change) triggers refresh.
# ---------------------------------------------------------------------------

def test_profiling_only_change_triggers_refresh(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Two IDENTICAL schema snapshots (v1, v2) -> schema diff sees no change,
    # isolating the profiling-classification diff as the sole driver.
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Transactional"})

    result1 = _run()
    assert result1.change_set.has_changes is True  # first-ever COMPLETE profiling snapshot
    assert _dict_table_row(db, "dbo.customers") is not None

    # New COMPLETE profiling snapshot, same schema, DIFFERENT classification.
    _insert_profiling_snapshot(db, schema_id_2, 2, {"dbo.customers": "Master"})

    result2 = _run()
    assert result2.status == "COMPLETE"
    assert result2.change_set.modified_tables == ["dbo.customers"]
    by_step = {s.step: s for s in result2.steps}
    assert by_step[WorkflowStep.REFRESH_DICTIONARY].status == "OK"
    assert by_step[WorkflowStep.REFRESH_VOCABULARY].status == "OK"


# ---------------------------------------------------------------------------
# 2. Classification-only change (single table, no coverage change) triggers
#    refresh scoped to exactly that table.
# ---------------------------------------------------------------------------

def test_classification_only_change_scoped_to_changed_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([
        _table("dbo", "customers", [_col("id")]),
        _table("dbo", "products", [_col("id")]),
    ])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(
        db, schema_id_1, 1, {"dbo.customers": "Transactional", "dbo.products": "Reference"},
    )
    _run()
    products_before = _dict_table_row(db, "dbo.products")

    # Only customers' classification changes.
    _insert_profiling_snapshot(
        db, schema_id_2, 2, {"dbo.customers": "Master", "dbo.products": "Reference"},
    )
    result = _run()

    assert result.change_set.modified_tables == ["dbo.customers"]
    assert "dbo.products" not in result.change_set.affected_table_fqns
    assert _dict_table_row(db, "dbo.products") == products_before


# ---------------------------------------------------------------------------
# 3. No-op profiling (identical classification) does not trigger a rebuild.
# ---------------------------------------------------------------------------

def test_noop_profiling_does_not_trigger_rebuild(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Transactional"})
    _run()
    before = _dict_table_row(db, "dbo.customers")

    # A fresh COMPLETE profiling snapshot with the SAME classification.
    _insert_profiling_snapshot(db, schema_id_2, 2, {"dbo.customers": "Transactional"})
    result = _run()

    assert result.change_set.has_changes is False
    by_step = {s.step: s for s in result.steps}
    assert by_step[WorkflowStep.REFRESH_DICTIONARY].status == "SKIPPED_NO_CHANGES"
    assert by_step[WorkflowStep.REFRESH_VOCABULARY].status == "SKIPPED_NO_CHANGES"
    assert _dict_table_row(db, "dbo.customers") == before


# ---------------------------------------------------------------------------
# 4. Source isolation — one source's profiling change never affects another.
# ---------------------------------------------------------------------------

def test_source_isolation(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id")])], source_id=1)
    snap2 = _snapshot([_table("dbo", "customers", [_col("id")])], source_id=2)
    s1_v1 = _insert_schema_snapshot(db, snap1, 1, source_id=1)
    s1_v2 = _insert_schema_snapshot(db, snap1, 2, source_id=1)
    s2_v1 = _insert_schema_snapshot(db, snap2, 1, source_id=2)
    # A second, content-identical schema snapshot for source 2 too, so its
    # own schema diff is a genuine "no change" (prev != None) rather than
    # the unrelated "only one schema_snapshots row ever" first-scan shape —
    # isolates this test to the profiling-diff behavior under test.
    _insert_schema_snapshot(db, snap2, 2, source_id=2)
    _insert_profiling_snapshot(db, s1_v1, 1, {"dbo.customers": "Transactional"}, source_id=1)
    _insert_profiling_snapshot(db, s2_v1, 1, {"dbo.customers": "Transactional"}, source_id=2)
    # Same reasoning as the schema snapshot above, applied to profiling: a
    # second, content-identical COMPLETE profiling snapshot for source 2 so
    # get_latest_two_complete_profiling_snapshots has a real (prev, latest)
    # pair to diff as "no change" rather than a first-completion shape.
    _insert_profiling_snapshot(db, s2_v1, 2, {"dbo.customers": "Transactional"}, source_id=2)
    _run(source_id=1)
    _run(source_id=2)

    source2_before = _dict_table_row(db, "dbo.customers", source_id=2)

    # Only source 1 gets a reclassification.
    _insert_profiling_snapshot(db, s1_v2, 2, {"dbo.customers": "Master"}, source_id=1)
    result1 = _run(source_id=1)
    assert result1.change_set.modified_tables == ["dbo.customers"]

    # source 2's second _run() repeats the exact same schema+profiling pair
    # as its first call (line 302) — the Part 16 duplicate-invocation guard
    # now short-circuits this to SKIPPED_DUPLICATE before any diffing runs
    # at all, which is a strictly stronger form of "source 2 unaffected by
    # source 1's change" than re-deriving an empty change_set every time.
    result2 = _run(source_id=2)
    assert result2.status == "SKIPPED_DUPLICATE"
    assert _dict_table_row(db, "dbo.customers", source_id=2) == source2_before


# ---------------------------------------------------------------------------
# 5. Idempotency — repeated lifecycle runs against the SAME profiling diff
#    (re-running without new evidence) produce no duplicate vocabulary rows.
# ---------------------------------------------------------------------------

def test_idempotent_no_duplicate_vocabulary_rows(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id"), _col("customer_name")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Transactional"})
    _run()
    _insert_profiling_snapshot(db, schema_id_2, 2, {"dbo.customers": "Master"})

    result1 = _run()
    vocab_after_first = _vocab_rows(db)
    assert result1.change_set.has_changes is True

    # Re-run without inserting any new evidence — same (v1, v2) profiling
    # pair again. Post-Part-16, the duplicate-invocation guard now catches
    # this before any work runs at all (a stronger idempotency guarantee
    # than "bootstrap_vocabulary happens to replace rather than accumulate
    # on a second real run") — so vocabulary is trivially unchanged because
    # no second run executed.
    result2 = _run()
    vocab_after_second = _vocab_rows(db)

    assert result2.status == "SKIPPED_DUPLICATE"
    assert len(vocab_after_second) == len(vocab_after_first)  # no accumulation either way


# ---------------------------------------------------------------------------
# 6. Existing schema-only behavior remains intact when there is no COMPLETE
#    profiling snapshot at all (the shape every pre-existing lifecycle test
#    fixture uses — status stays 'PENDING').
# ---------------------------------------------------------------------------

def test_schema_only_behavior_intact_with_no_complete_profiling(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap1 = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap1, 1)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version, "
        "status, created_at) VALUES (1, ?, 1, 'PENDING', ?)",
        (schema_id_1, _NOW),
    )
    conn.commit()
    conn.close()

    result = _run()
    assert result.change_set.is_first_scan is True
    assert result.change_set.added_tables == ["dbo.customers"]
    assert _dict_table_row(db, "dbo.customers") is not None


# ---------------------------------------------------------------------------
# 7-8. Real end-to-end batch profiling, driven through start_batch_profiling
#      / continue_batch_profiling (STRUCTURAL_ONLY mode — no live connector
#      needed), exercising the ACTUAL RUNNING -> COMPLETE transition inside
#      continue_batch_profiling that fires the automatic trigger, rather
#      than a hand-inserted COMPLETE snapshot.
# ---------------------------------------------------------------------------

def test_resumed_complete_profiling_triggers_lifecycle_once(tmp_path, monkeypatch):
    from data.profiling_service import continue_batch_profiling, start_batch_profiling

    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id"), _col("name")])])
    _insert_schema_snapshot(db, snap, 1)

    state = start_batch_profiling(1, "u1", mode="STRUCTURAL_ONLY")
    assert state.status.name == "RUNNING"
    assert len(_lifecycle_runs(db)) == 0  # not yet — profiling hasn't completed

    # Small fixture -> one batch (default batch_size=50) reaches COMPLETE.
    state = continue_batch_profiling(1, "u1", state.profiling_snapshot_id)
    assert state.status.name == "COMPLETE"

    runs = _lifecycle_runs(db)
    assert len(runs) == 1
    assert runs[0]["trigger_event"] == LifecycleTrigger.PROFILING_COMPLETE.value
    assert _dict_table_row(db, "dbo.customers") is not None

    # Calling continue_batch_profiling again on an already-COMPLETE snapshot
    # is the documented idempotent early-return (data/profiling_service.py:
    # "Already complete or cancelled") — must not fire the trigger a second
    # time for the same completion.
    state2 = continue_batch_profiling(1, "u1", state.profiling_snapshot_id)
    assert state2.status.name == "COMPLETE"
    assert len(_lifecycle_runs(db)) == 1


def test_scheduler_does_not_reprocess_complete_snapshot(tmp_path, monkeypatch):
    from data.profiling_service import (
        continue_all_active_profiling_batches, start_batch_profiling,
    )

    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    _insert_schema_snapshot(db, snap, 1)
    start_batch_profiling(1, "u1", mode="STRUCTURAL_ONLY")

    first_tick = continue_all_active_profiling_batches()
    assert len(first_tick) == 1
    assert first_tick[0]["status"] == "COMPLETE"
    assert len(_lifecycle_runs(db)) == 1

    # A second scheduler tick must find no incomplete profiling work left
    # for this source (the COMPLETE snapshot is excluded by
    # _sources_with_incomplete_profiling's own status filter) — so it does
    # not re-advance the same snapshot, and does not fire a second
    # automatic lifecycle run.
    second_tick = continue_all_active_profiling_batches()
    assert second_tick == []
    assert len(_lifecycle_runs(db)) == 1


def test_stale_semantic_contract_invalidated_by_new_profiling_snapshot(tmp_path, monkeypatch):
    """The semantic-contract cache (data.semantic_contract_service) is a
    separate, pre-existing, PULL-based invalidation mechanism (Task 6):
    current_metadata_revision() embeds the latest COMPLETE profiling
    snapshot id, and get_or_build_entity_contract() rebuilds whenever that
    revision no longer matches the persisted contract's own
    metadata_revision. This is not new behavior added by this milestone —
    this test confirms it stays correctly wired to the SAME profiling-
    snapshot signal run_autonomous_lifecycle's new profiling diff now also
    reads, so a "new COMPLETE profiling snapshot" is recognized as staleness
    by BOTH mechanisms consistently."""
    from data.semantic_contract_service import (
        current_metadata_revision, get_latest_entity_contract, persist_entity_contract,
    )

    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Master"})

    revision_at_persist = current_metadata_revision(1)
    persist_entity_contract(
        1, "Client", canonical_table_fqn="dbo.customers", preferred_view_fqn=None,
        resolution_status="RESOLVED", confidence=0.9, contract_json={},
        metadata_revision=revision_at_persist,
    )
    stored = get_latest_entity_contract(1, "Client")
    assert stored["metadata_revision"] == revision_at_persist

    # No new profiling evidence yet -> still fresh.
    assert current_metadata_revision(1) == stored["metadata_revision"]

    # A new COMPLETE profiling snapshot completes (schema unchanged) -> the
    # revision changes, correctly marking the persisted contract stale.
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)
    _insert_profiling_snapshot(db, schema_id_2, 2, {"dbo.customers": "Master"})
    assert current_metadata_revision(1) != stored["metadata_revision"]


# ---------------------------------------------------------------------------
# 9. Duplicate-invocation idempotency guard (Part 16 fix) — a direct caller
#    (not continue_batch_profiling, which already had its own correct
#    once-per-transition guard) invoking run_autonomous_lifecycle twice for
#    the exact same schema+profiling state must get a cheap no-op the
#    second time, never a second full run.
# ---------------------------------------------------------------------------

def test_duplicate_invocation_is_a_noop(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Transactional"})

    result1 = _run()
    assert result1.status == "COMPLETE"
    assert len(_lifecycle_runs(db)) == 1

    # Same trigger, same schema pair, same profiling pair, no new evidence
    # inserted — simulates the exact production scenario this guard closes
    # (a caller re-invoking run_autonomous_lifecycle for a state it already
    # fully processed).
    result2 = _run()
    assert result2.status == "SKIPPED_DUPLICATE"
    assert result2.run_id == result1.run_id
    assert len(_lifecycle_runs(db)) == 1  # no second row inserted


def test_duplicate_guard_does_not_suppress_new_profiling_evidence(tmp_path, monkeypatch):
    """Regression guard for the bug caught while implementing Part 16: keying
    the duplicate check on the SCHEMA pair alone would have wrongly
    collapsed test_idempotent_no_duplicate_vocabulary_rows' second _run()
    (new profiling evidence, unchanged schema pair) into a no-op. The guard
    must key on schema pair AND profiling pair together."""
    db = env(tmp_path, monkeypatch)
    snap = _snapshot([_table("dbo", "customers", [_col("id")])])
    schema_id_1 = _insert_schema_snapshot(db, snap, 1)
    schema_id_2 = _insert_schema_snapshot(db, snap, 2)  # identical schema, both exist up front
    _insert_profiling_snapshot(db, schema_id_1, 1, {"dbo.customers": "Transactional"})

    result1 = _run()
    assert result1.status == "COMPLETE"

    # New profiling evidence arrives; schema pair (schema_id_1, schema_id_2)
    # seen by get_latest_two_snapshots is UNCHANGED from result1's call.
    _insert_profiling_snapshot(db, schema_id_2, 2, {"dbo.customers": "Master"})
    result2 = _run()

    assert result2.status == "COMPLETE"  # must NOT be SKIPPED_DUPLICATE
    assert result2.change_set.modified_tables == ["dbo.customers"]
    assert len(_lifecycle_runs(db)) == 2
