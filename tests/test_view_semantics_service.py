"""
View Semantic Discovery milestone.

Unit tests for data.view_semantics_service — the connect-time step that
turns a VIEW's own SQL definition into persisted, provenance-taggable
evidence (definition text, validated base-table dependencies, restriction
evidence, and a generic curated-quality gate). inspect_targeted_values is
monkeypatched here (its own ownership/live-connection machinery is
exercised separately by the end-to-end SQLite fixture test), so these tests
focus on this module's own parsing/persistence/idempotency contract.

Run:
    python -m pytest tests/test_view_semantics_service.py -v
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-view-semantics-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-view-semantics-salt-long-enough-1234567")

import data.db as db_module
import data.models as models
from data.db import get_connection
import data.view_semantics_service as view_semantics_service
from data.investigation_service import InvestigationResult

_USER = "view-semantics-test-user"


@pytest.fixture(scope="module", autouse=True)
def _isolated_metadata_db(tmp_path_factory):
    """This file used to call models.init_db() at import time against the
    real, unpatched DB_PATH -- every collection of this file permanently
    wrote rows into data/toolsmith.db. Redirect DB_PATH to a module-scoped
    temp file before initializing schema (Phase 5 Step 6C)."""
    db_path = tmp_path_factory.mktemp("view_semantics_service") / "test.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    yield
    monkeypatch.undo()


def _make_source(name: str) -> int:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO data_source_connections "
            "(user_id, display_name, source_type, source_category, encrypted_config_json, "
            " config_schema_version, capabilities_json, metadata_json, source_status, "
            " is_active, created_at, updated_at) "
            "VALUES (?, ?, 'fixture_relational', 'relational_db', '{}', 1, "
            " '[\"schema_discovery\",\"sql_query\"]', '{}', 'ACTIVE', 1, ?, ?)",
            (_USER, name, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _make_snapshot(source_id: int, snapshot_json: dict) -> int:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO schema_snapshots "
            "(source_id, snapshot_version, source_type, table_count, view_count, "
            " column_count, snapshot_json, discovered_at, created_at) "
            "VALUES (?, 1, 'fixture_relational', 1, 1, 4, ?, ?, ?)",
            (source_id, json.dumps(snapshot_json), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


_SAMPLE_SNAPSHOT_JSON = {
    "schemas": [
        {
            "schema_name": "main",
            "tables": [
                {"table_name": "Tickets", "schema_name": "main", "table_fqn": "main.Tickets", "table_type": "TABLE"},
                {"table_name": "Customers", "schema_name": "main", "table_fqn": "main.Customers", "table_type": "TABLE"},
                {
                    "table_name": "vw_OpenTickets", "schema_name": "main",
                    "table_fqn": "main.vw_OpenTickets", "table_type": "VIEW",
                },
                {
                    "table_name": "vw_TicketsBackup2019", "schema_name": "main",
                    "table_fqn": "main.vw_TicketsBackup2019", "table_type": "VIEW",
                },
            ],
        },
    ],
}

_OPEN_TICKETS_DEFINITION = (
    "CREATE VIEW main.vw_OpenTickets AS "
    "SELECT t.TicketID, t.Subject, c.CustomerName FROM main.Tickets t "
    "JOIN main.Customers c ON t.CustomerID = c.CustomerID WHERE t.Status = 'Open'"
)
_BACKUP_DEFINITION = "CREATE VIEW main.vw_TicketsBackup2019 AS SELECT * FROM main.Tickets"


def _valid_result(text: str | None) -> InvestigationResult:
    return InvestigationResult(
        investigation_type="view_definition", table="x", column=None, reason="test",
        summary="ok", row_count=1, sample_values=[text] if text else [], duration_ms=1.0,
        valid=True,
    )


def _refused_result() -> InvestigationResult:
    return InvestigationResult(
        investigation_type="view_definition", table="x", column=None, reason="test",
        summary="refused", row_count=0, sample_values=[], duration_ms=1.0, valid=False,
    )


def test_parse_views_and_catalog_from_snapshot_json():
    views, catalog = view_semantics_service._parse_views_and_catalog_from_snapshot_json(
        json.dumps(_SAMPLE_SNAPSHOT_JSON)
    )
    view_fqns = {v["table_fqn"] for v in views}
    assert view_fqns == {"main.vw_OpenTickets", "main.vw_TicketsBackup2019"}
    assert catalog == {
        "main.Tickets", "main.Customers", "main.vw_OpenTickets", "main.vw_TicketsBackup2019",
    }


def test_parse_views_and_catalog_handles_malformed_json():
    views, catalog = view_semantics_service._parse_views_and_catalog_from_snapshot_json("not json")
    assert views == []
    assert catalog == set()


def test_discover_and_persist_curates_a_clean_view_with_resolved_dependencies(monkeypatch):
    source_id = _make_source("view-semantics-clean")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        if table_fqn == "main.vw_OpenTickets":
            return _valid_result(_OPEN_TICKETS_DEFINITION)
        return _refused_result()

    monkeypatch.setattr(view_semantics_service, "inspect_targeted_values", _fake_inspect, raising=False)
    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)

    summary = view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    assert summary["views_examined"] == 2
    assert summary["views_curated"] == 1

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM view_semantic_evidence WHERE source_id = ? AND table_fqn = ?",
            (source_id, "main.vw_OpenTickets"),
        ).fetchone()
        assert row is not None
        assert row["is_curated"] == 1
        referenced = json.loads(row["referenced_tables_json"])
        assert set(referenced) == {"main.Tickets", "main.Customers"}
        restriction = json.loads(row["restriction_json"])
        assert restriction["restriction_detected"] is True

        backup_row = conn.execute(
            "SELECT * FROM view_semantic_evidence WHERE source_id = ? AND table_fqn = ?",
            (source_id, "main.vw_TicketsBackup2019"),
        ).fetchone()
        assert backup_row is None  # refused probe -> nothing persisted this run

        deps = conn.execute(
            "SELECT from_table_fqn, to_table_fqn, relationship_type, inference_method "
            "FROM table_relationships WHERE source_id = ? AND relationship_type = 'VIEW_DEPENDENCY'",
            (source_id,),
        ).fetchall()
        to_fqns = {d["to_table_fqn"] for d in deps}
        assert to_fqns == {"main.Tickets", "main.Customers"}
        assert all(d["from_table_fqn"] == "main.vw_OpenTickets" for d in deps)
    finally:
        conn.close()


def test_discover_and_persist_never_curates_a_backup_named_view(monkeypatch):
    source_id = _make_source("view-semantics-backup")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        if table_fqn == "main.vw_TicketsBackup2019":
            return _valid_result(_BACKUP_DEFINITION)
        return _refused_result()

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)

    summary = view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    assert summary["views_curated"] == 0

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_curated, naming_penalty_score FROM view_semantic_evidence "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, "main.vw_TicketsBackup2019"),
        ).fetchone()
        assert row is not None
        assert row["is_curated"] == 0
        assert row["naming_penalty_score"] > 0
    finally:
        conn.close()


def test_discover_and_persist_is_idempotent_on_rerun(monkeypatch):
    source_id = _make_source("view-semantics-idempotent")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        if table_fqn == "main.vw_OpenTickets":
            return _valid_result(_OPEN_TICKETS_DEFINITION)
        return _refused_result()

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)

    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)

    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM view_semantic_evidence WHERE source_id = ?", (source_id,),
        ).fetchone()["n"]
        assert count == 1  # upsert, not duplicate rows
        dep_count = conn.execute(
            "SELECT COUNT(*) AS n FROM table_relationships "
            "WHERE source_id = ? AND relationship_type = 'VIEW_DEPENDENCY'",
            (source_id,),
        ).fetchone()["n"]
        assert dep_count == 2  # INSERT OR IGNORE -> no duplicate edges on rerun
    finally:
        conn.close()


def test_discover_and_persist_makes_progress_across_a_catalog_larger_than_the_cap(monkeypatch):
    """Real the reference database has hundreds of views, far more than _MAX_VIEWS_PER_DISCOVERY_RUN
    -- without prioritizing not-yet-discovered views, every re-discovery
    call would re-probe the SAME first-N-alphabetically views forever and
    the rest of a large catalog would never be reached. Verifies successive
    calls make real forward progress instead."""
    monkeypatch.setattr(view_semantics_service, "_MAX_VIEWS_PER_DISCOVERY_RUN", 2)

    snapshot_json = {
        "schemas": [{
            "schema_name": "main",
            "tables": [
                {"table_name": "Tickets", "schema_name": "main", "table_fqn": "main.Tickets", "table_type": "TABLE"},
                {"table_name": "vw_A", "schema_name": "main", "table_fqn": "main.vw_A", "table_type": "VIEW"},
                {"table_name": "vw_B", "schema_name": "main", "table_fqn": "main.vw_B", "table_type": "VIEW"},
                {"table_name": "vw_C", "schema_name": "main", "table_fqn": "main.vw_C", "table_type": "VIEW"},
            ],
        }],
    }
    source_id = _make_source("view-semantics-large-catalog")
    snapshot_id = _make_snapshot(source_id, snapshot_json)

    probed_calls: list[str] = []

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        probed_calls.append(table_fqn)
        return _valid_result(f"CREATE VIEW {table_fqn} AS SELECT * FROM main.Tickets")

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)

    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    first_run = set(probed_calls)
    assert len(first_run) == 2  # capped at 2, alphabetically: vw_A, vw_B

    probed_calls.clear()
    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    second_run = set(probed_calls)
    # vw_C was never reached on the first run -- without prioritization it
    # would be starved forever (the same alphabetical vw_A/vw_B slice would
    # win every time); this asserts real forward progress instead.
    assert "main.vw_C" in second_run

    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM view_semantic_evidence WHERE source_id = ?", (source_id,),
        ).fetchone()["n"]
        assert count == 3  # all three views eventually discovered across the two runs
    finally:
        conn.close()


def test_discover_and_persist_never_raises_on_probe_exception(monkeypatch):
    source_id = _make_source("view-semantics-exception")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _boom(*a, **k):
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _boom)

    summary = view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)
    assert summary["views_curated"] == 0


# ── SQL rebuild Phase 5, Step 6B: explicit PENDING provenance ─────────────


def test_regex_view_dependency_rows_persist_explicit_pending_status(monkeypatch):
    """This is a heuristic (regex-over-view-SQL-text) discovery path, never
    an authoritative source — its rows must persist relationship_status=
    'PENDING' explicitly, never rely on persist_relationships()'s own
    DEFAULT-mirroring fallback ('AUTO'), so
    core.sqlservice.retrieval_documents.classify_relationship_row() never
    treats them as confirmed without an explicit human approval."""
    source_id = _make_source("view-semantics-pending-status")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        if table_fqn == "main.vw_OpenTickets":
            return _valid_result(_OPEN_TICKETS_DEFINITION)
        return _refused_result()

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)

    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT relationship_status, inference_method FROM table_relationships "
            "WHERE source_id = ? AND relationship_type = 'VIEW_DEPENDENCY'",
            (source_id,),
        ).fetchall()
        assert len(rows) == 2
        assert all(r["relationship_status"] == "PENDING" for r in rows)
        assert all(r["inference_method"] == "view_dependency" for r in rows)
    finally:
        conn.close()


def test_regex_view_dependency_rows_classify_as_inferred_not_confirmed(monkeypatch):
    from core.sqlservice.retrieval_documents import classify_relationship_row

    source_id = _make_source("view-semantics-classify-inferred")
    snapshot_id = _make_snapshot(source_id, _SAMPLE_SNAPSHOT_JSON)

    def _fake_inspect(sid, uid, table_fqn, col, *, investigation_type, reason):
        if table_fqn == "main.vw_OpenTickets":
            return _valid_result(_OPEN_TICKETS_DEFINITION)
        return _refused_result()

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", _fake_inspect)
    view_semantics_service.discover_and_persist_view_semantics(snapshot_id, source_id, _USER)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM table_relationships WHERE source_id = ? AND relationship_type = 'VIEW_DEPENDENCY'",
            (source_id,),
        ).fetchall()
        assert rows
        assert all(classify_relationship_row(dict(r)) == "inferred" for r in rows)
    finally:
        conn.close()
