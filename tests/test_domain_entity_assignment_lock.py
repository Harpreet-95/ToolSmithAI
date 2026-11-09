"""
Tests for the domain/entity assignment human-lock mechanism added in Phase 11
(assignment_source column + lock_domain_assignment / lock_entity_assignment).

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file.

Run from the project root:
    python -m pytest tests/test_domain_entity_assignment_lock.py -v
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-assignment-lock-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-assignment-lock-value")

import data.models as models
from data.domain_service import (
    generate_domain_assignments, lock_domain_assignment, auto_mature_domain_assignment,
)
from data.entity_service import (
    generate_entity_assignments, lock_entity_assignment, auto_mature_entity_assignment,
)
from data.profiling_snapshot_resolver import ProfilingSnapshotRef, get_latest_profiling_snapshot

_NOW = "2026-01-01T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.domain_service",
    "data.entity_service",
    "data.governance_service",
    # Phase 2G.5 — added ahead of the future migration of
    # generate_domain_assignments() (and later generate_entity_assignments())
    # onto the shared resolver. Neither module calls it yet; this only
    # affects test isolation, not production behavior for either service.
    "data.profiling_snapshot_resolver",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lock.db")

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
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, 1, 1, 'mssql', '{}', ?, ?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1, 1, 1, 1, ?)",
        (_NOW,),
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, created_at, updated_at) "
        "VALUES (1, 1, 'dbo.customers', 'customers', 'dbo', ?, ?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _domain_row(db_path):
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM domain_assignments WHERE source_id = 1 AND table_fqn = 'dbo.customers'"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _entity_row(db_path):
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM entity_assignments WHERE source_id = 1 AND table_fqn = 'dbo.customers'"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _governance_events(db_path, object_type_id):
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM governance_approval_events WHERE object_type_id = ?", (object_type_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2G.5 — seed helpers for Domain Service profiling-snapshot baseline
# tests. Additive only; env() and the helpers above are untouched.
# ---------------------------------------------------------------------------

def _seed_source(db_path, source_id, user_id="u1"):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (?,?,'Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (source_id, user_id, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _seed_schema_snapshot(db_path, snap_id, source_id, snapshot_version=1):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snap_id, source_id, snapshot_version, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _seed_profiling_snapshot(db_path, snap_id, *, source_id, schema_snapshot_id, snapshot_version, status):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (snap_id, source_id, schema_snapshot_id, snapshot_version, status, _NOW),
    )
    conn.commit()
    conn.close()


def _seed_table_profile(db_path, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _domain_rows_for_source(db_path, source_id):
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM domain_assignments WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _entity_rows_for_source(db_path, source_id):
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM entity_assignments WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class _MarkingConnWrapper:
    """Wraps a real (file-backed) sqlite3.Connection; records which wrapper
    instance executes each SQL statement matching one of marker_sql_map's
    substring sets, and captures total_changes at close() — used to prove
    both connection-count and read-only behavior for a specific block
    without needing to alter production."""

    def __init__(self, conn: sqlite3.Connection, events: dict, marker_sql_map: dict) -> None:
        self._conn = conn
        self._events = events
        self._marker_sql_map = marker_sql_map
        self.total_changes_at_close = None

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str):
            for label, needles in self._marker_sql_map.items():
                if all(n in sql for n in needles):
                    self._events.setdefault(label, []).append(self)
        return self._conn.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self.total_changes_at_close = self._conn.total_changes
        self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class TestDomainAssignmentLock:
    def test_generate_sets_assignment_source_rule_by_default(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        row = _domain_row(db)
        assert row["assignment_source"] == "rule"

    def test_lock_sets_human_and_optional_new_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        result = lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        assert result["assignment_source"] == "human"
        assert result["domain"] == "Custom Domain"

    def test_lock_without_domain_keeps_existing_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        before = _domain_row(db)
        lock_domain_assignment(1, "u1", "dbo.customers")
        after = _domain_row(db)
        assert after["domain"] == before["domain"]
        assert after["assignment_source"] == "human"

    def test_locked_row_not_overwritten_by_regeneration(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        generate_domain_assignments(1, "u1")  # re-run — must not overwrite
        row = _domain_row(db)
        assert row["domain"] == "Custom Domain"
        assert row["assignment_source"] == "human"

    def test_lock_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_domain_assignment(1, "u1", "dbo.nonexistent") is None

    def test_lock_wrong_user_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_domain_assignment(1, "someone-else", "dbo.customers") is None

    def test_lock_logs_governance_event(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        events = _governance_events(db, "domain.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "HUMAN_LOCK"
        assert events[0]["to_state"] == "HUMAN_APPROVED"


# ---------------------------------------------------------------------------
# Phase 2G.5 — pre-migration behavioral baseline for the inline
# "latest profiling snapshot" lookup inside generate_domain_assignments().
# Scoped to Domain Service only (data.domain_service.generate_domain_assignments).
#
# Discriminating signal: result["profiling_snapshot_id"] (returned directly)
# and which table(s) produce a domain_assignments row. A profiling_snapshots
# row is only "used" if its own profiling_table_profiles rows are the ones
# read — seeding each candidate snapshot with its own distinct table_fqn
# makes the resolution directly observable without depending on the domain
# classifier's internal heuristics at all.
# ---------------------------------------------------------------------------

class TestDomainServiceProfilingSnapshotBaseline:
    def test_newer_cancelled_snapshot_beats_older_complete(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)  # seeds profiling_snapshots id=1 v1 (dbo.customers)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="CANCELLED",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        assert result["tables_total"] == 1

        rows = _domain_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"
        assert rows[0]["profiling_snapshot_id"] == newer_id

    def test_newer_running_snapshot_beats_older_complete(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="RUNNING",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        assert result["tables_total"] == 1

        rows = _domain_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"
        assert rows[0]["profiling_snapshot_id"] == newer_id

    def test_highest_snapshot_version_wins(self, tmp_path, monkeypatch):
        """Both snapshots COMPLETE — isolates version as the only
        distinguishing factor, separate from the CANCELLED/RUNNING
        precedence tests above."""
        db = env(tmp_path, monkeypatch)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="COMPLETE",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        rows = _domain_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"

    def test_source_scoping_two_sources(self, tmp_path, monkeypatch):
        """A second source's far-newer profiling snapshot must never be
        picked up for source_id=1's own call."""
        db = env(tmp_path, monkeypatch)  # source 1 already seeded by env()

        _seed_source(db, 2)
        _seed_schema_snapshot(db, 2, source_id=2, snapshot_version=1)
        other_id = 99
        _seed_profiling_snapshot(
            db, other_id, source_id=2, schema_snapshot_id=2, snapshot_version=99, status="COMPLETE",
        )
        _seed_table_profile(db, other_id, 2, "dbo.vendors", "vendors", "dbo")

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == 1  # source 1's own (only) snapshot
        assert result["tables_total"] == 1

        rows = _domain_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.customers"  # source 1's own table, not dbo.vendors

    def test_no_profiling_snapshot_raises_exact_value_error(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _seed_source(db, 2)
        _seed_schema_snapshot(db, 2, source_id=2, snapshot_version=1)
        # No profiling_snapshots row seeded for source_id=2 at all.

        with pytest.raises(
            ValueError,
            match=r"^No profiling snapshot found\. Run POST /sources/\{id\}/profile/structural first\.$",
        ):
            generate_domain_assignments(2, "u1")

    def test_connection_count_baseline(self, tmp_path, monkeypatch):
        """Pre-migration connection model: exactly one Domain-Service-owned
        connection handles the initial read block (ownership + snapshot
        lookup + table/column profile reads); the resolver's own
        get_connection is never called (not wired in yet). The function's
        two later blocks (domain_learning_rules read, and the UPSERT+commit)
        each open their own additional connection today — reported
        accurately here, not collapsed into a false single-connection total.
        """
        db = env(tmp_path, monkeypatch)

        events: dict = {}
        call_count = {"n": 0}

        def _counting_get_connection():
            call_count["n"] += 1
            return _MarkingConnWrapper(
                _db_conn(db), events,
                {
                    "snapshot_lookup": ("FROM profiling_snapshots", "WHERE source_id = ?"),
                    "rules_read":      ("FROM domain_learning_rules",),
                    "upsert":          ("INSERT INTO domain_assignments",),
                },
            )

        mock_resolver_get_connection = MagicMock()
        monkeypatch.setattr("data.domain_service.get_connection", _counting_get_connection)
        monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection)

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert len(events.get("snapshot_lookup", [])) == 1
        assert len(events.get("rules_read", [])) == 1
        assert len(events.get("upsert", [])) == 1
        # Current pre-migration reality: three separate get_connection() calls
        # across the whole function (initial read block, rules read, upsert).
        assert call_count["n"] == 3
        assert mock_resolver_get_connection.call_count == 0

    def test_initial_read_block_is_read_only(self, tmp_path, monkeypatch):
        """The connection serving the initial read block (ownership check +
        snapshot lookup + table/column profile reads) must make zero writes
        — proven via total_changes captured at that connection's own close(),
        before the later write-phase connection ever opens."""
        db = env(tmp_path, monkeypatch)

        events: dict = {}

        def _tracking_get_connection():
            return _MarkingConnWrapper(
                _db_conn(db), events,
                {"snapshot_lookup": ("FROM profiling_snapshots", "WHERE source_id = ?")},
            )

        monkeypatch.setattr("data.domain_service.get_connection", _tracking_get_connection)

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert len(events.get("snapshot_lookup", [])) == 1
        lookup_conn = events["snapshot_lookup"][0]
        assert lookup_conn.total_changes_at_close == 0

    def test_result_shape_preserved(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert set(result.keys()) == {
            "source_id",
            "profiling_snapshot_id",
            "tables_total",
            "tables_assigned",
            "tables_unknown",
            "domain_counts",
            "learned_rule_matches",
            "generic_rule_matches",
            "unknown_assignments",
            "generated_at",
        }
        assert isinstance(result["source_id"], int)
        assert isinstance(result["profiling_snapshot_id"], int)
        assert isinstance(result["tables_total"], int)
        assert isinstance(result["tables_assigned"], int)
        assert isinstance(result["tables_unknown"], int)
        assert isinstance(result["domain_counts"], dict)
        assert isinstance(result["learned_rule_matches"], int)
        assert isinstance(result["generic_rule_matches"], int)
        assert isinstance(result["unknown_assignments"], int)
        assert isinstance(result["generated_at"], str)

    # -----------------------------------------------------------------------
    # Phase 2G.6 — migration proof: generate_domain_assignments() now
    # delegates to the shared resolver instead of running its own inline SQL.
    # -----------------------------------------------------------------------

    def test_domain_resolver_called_once_with_source_id_and_caller_conn(self, tmp_path, monkeypatch):
        """Proves: the resolver is called exactly once, with source_id and
        the exact connection object generate_domain_assignments() itself
        opened for the initial read block (not a new one), and the resolver
        never opens its own connection."""
        db = env(tmp_path, monkeypatch)

        opened_connections: list = []

        def _tracking_get_connection():
            c = _db_conn(db)
            opened_connections.append(c)
            return c

        resolver_calls: list = []
        real_resolver = get_latest_profiling_snapshot

        def _tracking_resolver(source_id_arg, conn=None):
            # Captured at call time — the function goes on to open more
            # connections later (domain_learning_rules read, UPSERT+commit),
            # so comparing against opened_connections' final state after the
            # whole call finishes would compare against the wrong connection.
            most_recently_opened = opened_connections[-1] if opened_connections else None
            resolver_calls.append((source_id_arg, conn, most_recently_opened))
            return real_resolver(source_id_arg, conn=conn)

        mock_resolver_get_connection = MagicMock()
        monkeypatch.setattr("data.domain_service.get_connection", _tracking_get_connection)
        monkeypatch.setattr("data.domain_service.get_latest_profiling_snapshot", _tracking_resolver)
        monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection)

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert len(resolver_calls) == 1
        called_source_id, called_conn, most_recently_opened_at_call_time = resolver_calls[0]
        assert called_source_id == 1
        assert called_conn is most_recently_opened_at_call_time
        assert mock_resolver_get_connection.call_count == 0

    def test_domain_resolver_result_drives_downstream_profile_queries(self, tmp_path, monkeypatch):
        """Patches the resolver symbol imported into domain_service to
        return a controlled ProfilingSnapshotRef pointing at a real but
        deliberately LOWER-version profiling snapshot than env()'s default
        (version 0 vs 1) — so the removed inline query, which always orders
        by snapshot_version DESC, could never have picked it. The downstream
        profiling_table_profiles/profiling_column_profiles reads are scoped
        only by profiling_snapshot_id, so seeing this lower-version
        snapshot's table drive the result proves the function now uses
        snapshot.id from the resolver's return value directly, not the
        removed inline SELECT."""
        db = env(tmp_path, monkeypatch)  # seeds default snapshot id=1, v1, dbo.customers

        fake_snapshot_id = 777
        _seed_profiling_snapshot(
            db, fake_snapshot_id, source_id=1, schema_snapshot_id=1,
            snapshot_version=0, status="COMPLETE",
        )
        _seed_table_profile(db, fake_snapshot_id, 1, "dbo.transactions", "transactions", "dbo")
        fake_ref = ProfilingSnapshotRef(
            id=fake_snapshot_id, source_id=1, snapshot_version=0,
            status="COMPLETE", schema_snapshot_id=1,
        )

        monkeypatch.setattr(
            "data.domain_service.get_latest_profiling_snapshot",
            lambda source_id, conn=None: fake_ref,
        )

        result = generate_domain_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == fake_snapshot_id
        assert result["tables_total"] == 1

        rows = _domain_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.transactions"
        assert rows[0]["profiling_snapshot_id"] == fake_snapshot_id

    def test_domain_service_has_no_inline_latest_profiling_snapshot_sql(self):
        source = inspect.getsource(generate_domain_assignments)
        assert "FROM profiling_snapshots" not in source
        assert "SELECT id FROM profiling_snapshots" not in source

    def test_domain_service_uses_shared_resolver_with_conn(self):
        """Narrow AST check scoped to generate_domain_assignments()'s own
        source only: exactly one get_latest_profiling_snapshot(...) call,
        with source_id passed positionally and conn=conn passed by keyword."""
        source = inspect.getsource(generate_domain_assignments)
        tree = ast.parse(source)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_latest_profiling_snapshot"
        ]
        assert len(calls) == 1, "expected exactly one get_latest_profiling_snapshot(...) call"

        call = calls[0]
        positional_names = [a.id for a in call.args if isinstance(a, ast.Name)]
        assert "source_id" in positional_names

        keyword_values = {kw.arg: kw.value for kw in call.keywords}
        assert "conn" in keyword_values
        conn_value = keyword_values["conn"]
        assert isinstance(conn_value, ast.Name) and conn_value.id == "conn"


class TestEntityAssignmentLock:
    def test_generate_sets_assignment_source_rule_by_default(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        row = _entity_row(db)
        assert row["assignment_source"] == "rule"

    def test_lock_sets_human_and_optional_new_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        result = lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        assert result["assignment_source"] == "human"
        assert result["entity"] == "Custom Entity"

    def test_locked_row_not_overwritten_by_regeneration(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        generate_entity_assignments(1, "u1")
        row = _entity_row(db)
        assert row["entity"] == "Custom Entity"
        assert row["assignment_source"] == "human"

    def test_lock_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_entity_assignment(1, "u1", "dbo.nonexistent") is None

    def test_lock_logs_governance_event(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        events = _governance_events(db, "entity.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "HUMAN_LOCK"


# ---------------------------------------------------------------------------
# Phase 2G.7 — pre-migration behavioral baseline for the inline
# "latest profiling snapshot" lookup inside generate_entity_assignments().
# Scoped to Entity Service only (data.entity_service.generate_entity_assignments).
# Mirrors the Domain Service baseline (Phase 2G.5) with the same
# discriminating signal (result["profiling_snapshot_id"] + which table
# produces an entity_assignments row), adjusted for Entity Service's
# different connection lifecycle: entity_learning_rules is read inside the
# SAME initial connection as the snapshot lookup (Domain Service reads
# domain_learning_rules on a separate second connection), so the total
# get_connection() count here is 2, not 3.
# ---------------------------------------------------------------------------

class TestEntityServiceProfilingSnapshotBaseline:
    def test_newer_cancelled_snapshot_beats_older_complete(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)  # seeds profiling_snapshots id=1 v1 (dbo.customers)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="CANCELLED",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        assert result["tables_total"] == 1

        rows = _entity_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"
        assert rows[0]["profiling_snapshot_id"] == newer_id

    def test_newer_running_snapshot_beats_older_complete(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="RUNNING",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        assert result["tables_total"] == 1

        rows = _entity_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"
        assert rows[0]["profiling_snapshot_id"] == newer_id

    def test_highest_snapshot_version_wins(self, tmp_path, monkeypatch):
        """Both snapshots COMPLETE — isolates version as the only
        distinguishing factor, separate from the CANCELLED/RUNNING
        precedence tests above."""
        db = env(tmp_path, monkeypatch)
        newer_id = 2
        _seed_profiling_snapshot(
            db, newer_id, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="COMPLETE",
        )
        _seed_table_profile(db, newer_id, 1, "dbo.orders", "orders", "dbo")

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == newer_id
        rows = _entity_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.orders"

    def test_source_scoping_two_sources(self, tmp_path, monkeypatch):
        """A second source's far-newer profiling snapshot must never be
        picked up for source_id=1's own call."""
        db = env(tmp_path, monkeypatch)  # source 1 already seeded by env()

        _seed_source(db, 2)
        _seed_schema_snapshot(db, 2, source_id=2, snapshot_version=1)
        other_id = 99
        _seed_profiling_snapshot(
            db, other_id, source_id=2, schema_snapshot_id=2, snapshot_version=99, status="COMPLETE",
        )
        _seed_table_profile(db, other_id, 2, "dbo.vendors", "vendors", "dbo")

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == 1  # source 1's own (only) snapshot
        assert result["tables_total"] == 1

        rows = _entity_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.customers"  # source 1's own table, not dbo.vendors

    def test_no_profiling_snapshot_raises_exact_value_error(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _seed_source(db, 2)
        _seed_schema_snapshot(db, 2, source_id=2, snapshot_version=1)
        # No profiling_snapshots row seeded for source_id=2 at all.

        with pytest.raises(
            ValueError,
            match=r"^No profiling snapshot found\. Run POST /sources/\{id\}/profile/structural first\.$",
        ):
            generate_entity_assignments(2, "u1")

    def test_connection_count_baseline(self, tmp_path, monkeypatch):
        """Pre-migration connection model: exactly one Entity-Service-owned
        connection handles the ENTIRE initial read block — ownership check,
        snapshot lookup, table/column profile reads, AND the
        entity_learning_rules read (all four share one connection here,
        unlike Domain Service, which reads its learning rules on a separate
        second connection). The resolver's own get_connection is never
        called (not wired in yet). The write phase (UPSERT + commit) opens
        one further connection, for a total of 2 — not Domain Service's 3.
        """
        db = env(tmp_path, monkeypatch)

        events: dict = {}
        call_count = {"n": 0}

        def _counting_get_connection():
            call_count["n"] += 1
            return _MarkingConnWrapper(
                _db_conn(db), events,
                {
                    "snapshot_lookup": ("FROM profiling_snapshots", "WHERE source_id = ?"),
                    "rules_read":      ("FROM entity_learning_rules",),
                    "upsert":          ("INSERT INTO entity_assignments",),
                },
            )

        mock_resolver_get_connection = MagicMock()
        monkeypatch.setattr("data.entity_service.get_connection", _counting_get_connection)
        monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection)

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert len(events.get("snapshot_lookup", [])) == 1
        assert len(events.get("rules_read", [])) == 1
        assert len(events.get("upsert", [])) == 1
        # The snapshot lookup and the entity_learning_rules read share the
        # SAME connection here — the structural difference from Domain
        # Service this phase was specifically told not to assume away.
        assert events["snapshot_lookup"][0] is events["rules_read"][0]
        assert events["snapshot_lookup"][0] is not events["upsert"][0]
        # Current pre-migration reality: two get_connection() calls total
        # (initial combined read block, then the upsert/write block).
        assert call_count["n"] == 2
        assert mock_resolver_get_connection.call_count == 0

    def test_initial_read_block_is_read_only(self, tmp_path, monkeypatch):
        """The connection serving the initial read block (ownership check +
        snapshot lookup + table/column profile reads + entity_learning_rules
        read) must make zero writes — proven via total_changes captured at
        that connection's own close(), before the later write-phase
        connection ever opens."""
        db = env(tmp_path, monkeypatch)

        events: dict = {}

        def _tracking_get_connection():
            return _MarkingConnWrapper(
                _db_conn(db), events,
                {"snapshot_lookup": ("FROM profiling_snapshots", "WHERE source_id = ?")},
            )

        monkeypatch.setattr("data.entity_service.get_connection", _tracking_get_connection)

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert len(events.get("snapshot_lookup", [])) == 1
        lookup_conn = events["snapshot_lookup"][0]
        assert lookup_conn.total_changes_at_close == 0

    def test_result_shape_preserved(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert set(result.keys()) == {
            "source_id",
            "profiling_snapshot_id",
            "tables_total",
            "entities_assigned",
            "entities_unknown",
            "entity_counts",
            "learned_matches",
            "heuristic_matches",
            "unknown_matches",
            "generated_at",
        }
        assert isinstance(result["source_id"], int)
        assert isinstance(result["profiling_snapshot_id"], int)
        assert isinstance(result["tables_total"], int)
        assert isinstance(result["entities_assigned"], int)
        assert isinstance(result["entities_unknown"], int)
        assert isinstance(result["entity_counts"], dict)
        assert isinstance(result["learned_matches"], int)
        assert isinstance(result["heuristic_matches"], int)
        assert isinstance(result["unknown_matches"], int)
        assert isinstance(result["generated_at"], str)

    # -----------------------------------------------------------------------
    # Phase 2G.8 — migration proof: generate_entity_assignments() now
    # delegates to the shared resolver instead of running its own inline SQL.
    # -----------------------------------------------------------------------

    def test_entity_resolver_called_once_with_source_id_and_caller_conn(self, tmp_path, monkeypatch):
        """Proves: the resolver is called exactly once, with source_id and
        the exact connection object generate_entity_assignments() itself
        opened for the initial read block (not a new one), and the resolver
        never opens its own connection."""
        db = env(tmp_path, monkeypatch)

        opened_connections: list = []

        def _tracking_get_connection():
            c = _db_conn(db)
            opened_connections.append(c)
            return c

        resolver_calls: list = []
        real_resolver = get_latest_profiling_snapshot

        def _tracking_resolver(source_id_arg, conn=None):
            # Captured at call time — the function goes on to open more
            # connections later (the UPSERT+commit write phase), so
            # comparing against opened_connections' final state after the
            # whole call finishes would compare against the wrong connection.
            most_recently_opened = opened_connections[-1] if opened_connections else None
            resolver_calls.append((source_id_arg, conn, most_recently_opened))
            return real_resolver(source_id_arg, conn=conn)

        mock_resolver_get_connection = MagicMock()
        monkeypatch.setattr("data.entity_service.get_connection", _tracking_get_connection)
        monkeypatch.setattr("data.entity_service.get_latest_profiling_snapshot", _tracking_resolver)
        monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection)

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert len(resolver_calls) == 1
        called_source_id, called_conn, most_recently_opened_at_call_time = resolver_calls[0]
        assert called_source_id == 1
        assert called_conn is most_recently_opened_at_call_time
        assert mock_resolver_get_connection.call_count == 0

    def test_entity_resolver_result_drives_downstream_profile_queries(self, tmp_path, monkeypatch):
        """Patches the resolver symbol imported into entity_service to
        return a controlled ProfilingSnapshotRef pointing at a real but
        deliberately LOWER-version profiling snapshot than env()'s default
        (version 0 vs 1) — so the removed inline query, which always orders
        by snapshot_version DESC, could never have picked it. The downstream
        profiling_table_profiles/profiling_column_profiles reads are scoped
        only by profiling_snapshot_id, so seeing this lower-version
        snapshot's table drive the result proves the function now uses
        snapshot.id from the resolver's return value directly, not the
        removed inline SELECT."""
        db = env(tmp_path, monkeypatch)  # seeds default snapshot id=1, v1, dbo.customers

        fake_snapshot_id = 777
        _seed_profiling_snapshot(
            db, fake_snapshot_id, source_id=1, schema_snapshot_id=1,
            snapshot_version=0, status="COMPLETE",
        )
        _seed_table_profile(db, fake_snapshot_id, 1, "dbo.transactions", "transactions", "dbo")
        fake_ref = ProfilingSnapshotRef(
            id=fake_snapshot_id, source_id=1, snapshot_version=0,
            status="COMPLETE", schema_snapshot_id=1,
        )

        monkeypatch.setattr(
            "data.entity_service.get_latest_profiling_snapshot",
            lambda source_id, conn=None: fake_ref,
        )

        result = generate_entity_assignments(1, "u1")

        assert result is not None
        assert result["profiling_snapshot_id"] == fake_snapshot_id
        assert result["tables_total"] == 1

        rows = _entity_rows_for_source(db, 1)
        assert len(rows) == 1
        assert rows[0]["table_fqn"] == "dbo.transactions"
        assert rows[0]["profiling_snapshot_id"] == fake_snapshot_id

    def test_entity_service_has_no_inline_latest_profiling_snapshot_sql(self):
        source = inspect.getsource(generate_entity_assignments)
        assert "FROM profiling_snapshots" not in source
        assert "SELECT id FROM profiling_snapshots" not in source

    def test_entity_service_uses_shared_resolver_with_conn(self):
        """Narrow AST check scoped to generate_entity_assignments()'s own
        source only: exactly one get_latest_profiling_snapshot(...) call,
        with source_id passed positionally and conn=conn passed by keyword."""
        source = inspect.getsource(generate_entity_assignments)
        tree = ast.parse(source)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_latest_profiling_snapshot"
        ]
        assert len(calls) == 1, "expected exactly one get_latest_profiling_snapshot(...) call"

        call = calls[0]
        positional_names = [a.id for a in call.args if isinstance(a, ast.Name)]
        assert "source_id" in positional_names

        keyword_values = {kw.arg: kw.value for kw in call.keywords}
        assert "conn" in keyword_values
        conn_value = keyword_values["conn"]
        assert isinstance(conn_value, ast.Name) and conn_value.id == "conn"


# ---------------------------------------------------------------------------
# Milestone M-23 — auto-maturation write path (governed automation, not human)
# ---------------------------------------------------------------------------

def _state_map_row(db_path, object_type_id, object_id):
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM governance_state_map WHERE object_type_id = ? AND object_id = ?",
        (object_type_id, object_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


class TestAutoMatureDomainAssignment:
    def test_matures_row_and_writes_audit_trail(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        result = auto_mature_domain_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        assert result["assignment_source"] == "auto_governance"

        row = _domain_row(db)
        assert row["assignment_source"] == "auto_governance"

        events = _governance_events(db, "domain.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "AUTO_GOVERNANCE_APPROVED"
        assert events[0]["to_state"] == "AUTO_APPROVED"
        assert events[0]["actor_id"] == "system:test-rollout"

        state = _state_map_row(db, "domain.assignment", "1:dbo.customers")
        assert state is not None
        assert state["approval_state"] == "AUTO_APPROVED"

    def test_never_overwrites_human_lock(self, tmp_path, monkeypatch):
        # Rollback safety: a human decision always wins over a later
        # governed-automation attempt on the same row.
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        result = auto_mature_domain_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        assert result is None

        row = _domain_row(db)
        assert row["assignment_source"] == "human"
        assert row["domain"] == "Custom Domain"

    def test_matured_row_survives_subsequent_regeneration(self, tmp_path, monkeypatch):
        # Rollback safety: the widened UPSERT WHERE clause must protect an
        # auto_governance row from the next scheduled rule re-run exactly
        # like it already protects a human-locked row.
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        before = _domain_row(db)
        auto_mature_domain_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        generate_domain_assignments(1, "u1")  # re-run — must not overwrite
        after = _domain_row(db)
        assert after["domain"] == before["domain"]
        assert after["assignment_source"] == "auto_governance"

    def test_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert auto_mature_domain_assignment(1, "dbo.nonexistent", actor_id="system:test") is None


class TestAutoMatureEntityAssignment:
    def test_matures_row_and_writes_audit_trail(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        result = auto_mature_entity_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        assert result["assignment_source"] == "auto_governance"

        row = _entity_row(db)
        assert row["assignment_source"] == "auto_governance"

        events = _governance_events(db, "entity.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "AUTO_GOVERNANCE_APPROVED"
        assert events[0]["to_state"] == "AUTO_APPROVED"

        state = _state_map_row(db, "entity.assignment", "1:dbo.customers")
        assert state is not None
        assert state["approval_state"] == "AUTO_APPROVED"

    def test_never_overwrites_human_lock(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        result = auto_mature_entity_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        assert result is None

        row = _entity_row(db)
        assert row["assignment_source"] == "human"
        assert row["entity"] == "Custom Entity"

    def test_matured_row_survives_subsequent_regeneration(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        before = _entity_row(db)
        auto_mature_entity_assignment(1, "dbo.customers", actor_id="system:test-rollout")
        generate_entity_assignments(1, "u1")  # re-run — must not overwrite
        after = _entity_row(db)
        assert after["entity"] == before["entity"]
        assert after["assignment_source"] == "auto_governance"

    def test_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert auto_mature_entity_assignment(1, "dbo.nonexistent", actor_id="system:test") is None
