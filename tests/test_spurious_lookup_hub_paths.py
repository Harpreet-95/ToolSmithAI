"""
Automatic Relationship Validation — spurious shared-lookup-hub avoidance in
recommend_best_join_path (data.semantic_layer_service).

Covers the general, evidence-based rule that stops a join path from
routing THROUGH a small, broadly-shared reference/lookup table (e.g. real
the reference database's 32-row dbo.EDU_Cancel, referenced by 4 otherwise business-unrelated
tables) purely because it happens to offer a shorter hop count than the
genuine business relationship:
  - a small (< 100 row), high-fan-in (>= 3 connected tables), non-junction
    table is excluded as an INTERMEDIATE pass-through when a real
    alternative path exists;
  - it is NEVER excluded when it is the literal, directly-queried start or
    end table (a question genuinely about it still works unchanged);
  - it is NEVER excluded when it is the ONLY connector between two tables
    (a genuine star-schema hub/dimension bridging otherwise-disconnected
    facts must still be found — the two-pass fallback in
    recommend_best_join_path guarantees this);
  - a table with ZERO profiled rows is never treated as "small" (no
    evidence either way, must not be conflated with a known-small table);
  - a genuine junction/bridge table is never excluded regardless of size
    or fan-in (that IS its designed purpose).

Run:
    python -m pytest tests/test_spurious_lookup_hub_paths.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-spurious-hub-paths-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-spurious-hub-paths-salt-long-enough-12")

from data.semantic_layer_service import recommend_best_join_path
from tests.test_phase8_join_intelligence import (
    env, _insert_table, _insert_column, _insert_relationship, _conn,
)


def _mark_junction(db_path, table_fqn):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE profiling_table_profiles SET is_junction_table = 1 WHERE table_fqn = ?",
        (table_fqn,),
    )
    conn.commit()
    conn.close()


def test_small_high_fanin_hub_is_avoided_when_alternative_exists(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    # dbo.a and dbo.z both real, populated tables.
    _insert_table(db, "dbo.a", row_count=5000)
    _insert_column(db, "dbo.a", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.a", "hub_id")
    _insert_table(db, "dbo.z", row_count=5000)
    _insert_column(db, "dbo.z", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.z", "hub_id")

    # dbo.hub: small (32 rows), referenced by 3+ otherwise-unrelated tables
    # -- the real dbo.EDU_Cancel shape.
    _insert_table(db, "dbo.hub", row_count=32)
    _insert_column(db, "dbo.hub", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_table(db, "dbo.other1", row_count=1000)
    _insert_column(db, "dbo.other1", "hub_id")
    _insert_table(db, "dbo.other2", row_count=1000)
    _insert_column(db, "dbo.other2", "hub_id")

    _insert_relationship(db, "dbo.a", "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.z", "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.other1", "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.other2", "hub_id", "dbo.hub", "id")

    # A genuine, direct, real business relationship between a and z.
    _insert_column(db, "dbo.a", "z_id")
    _insert_relationship(db, "dbo.a", "z_id", "dbo.z", "id")

    result = recommend_best_join_path(1, "u1", "dbo.a", "dbo.z")
    best = result["best_join_path"]
    assert best["path"] == ["dbo.a", "dbo.z"]
    assert "dbo.hub" not in best["path"]


def test_small_high_fanin_hub_still_used_as_sole_connector(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    _insert_table(db, "dbo.hub", row_count=50)
    _insert_column(db, "dbo.hub", "id", is_primary_key=1, uniqueness_score=1.0)

    for name in ("leaf0", "leaf1", "leaf2"):
        fqn = f"dbo.{name}"
        _insert_table(db, fqn, row_count=1000)
        _insert_column(db, fqn, "hub_id")
        _insert_relationship(db, fqn, "hub_id", "dbo.hub", "id")

    # No direct/alternative path between leaf0 and leaf1 -- the hub is the
    # ONLY connector, matching a genuine star-schema dimension. Must still
    # be found (two-pass fallback), never a false refusal.
    result = recommend_best_join_path(1, "u1", "dbo.leaf0", "dbo.leaf1")
    assert result["total_paths_found"] >= 1
    assert result["best_join_path"]["path"] == ["dbo.leaf0", "dbo.hub", "dbo.leaf1"]


def test_hub_table_still_usable_as_literal_endpoint(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    _insert_table(db, "dbo.a", row_count=5000)
    _insert_column(db, "dbo.a", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.a", "hub_id")
    _insert_table(db, "dbo.hub", row_count=32)
    _insert_column(db, "dbo.hub", "id", is_primary_key=1, uniqueness_score=1.0)
    for name in ("other1", "other2"):
        fqn = f"dbo.{name}"
        _insert_table(db, fqn, row_count=1000)
        _insert_column(db, fqn, "hub_id")
        _insert_relationship(db, fqn, "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.a", "hub_id", "dbo.hub", "id")

    # A question literally about dbo.hub as the target must resolve
    # directly -- never excluded when it IS the endpoint, not a waypoint.
    result = recommend_best_join_path(1, "u1", "dbo.a", "dbo.hub")
    assert result["best_join_path"]["path"] == ["dbo.a", "dbo.hub"]


def test_zero_row_table_not_treated_as_small_hub(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    _insert_table(db, "dbo.a", row_count=5000)
    _insert_column(db, "dbo.a", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.a", "hub_id")
    _insert_table(db, "dbo.z", row_count=5000)
    _insert_column(db, "dbo.z", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.z", "hub_id")

    # dbo.hub has ZERO rows -- no evidence it's a small lookup table (could
    # simply be unpopulated right now); must not be excluded on that basis.
    _insert_table(db, "dbo.hub", row_count=0)
    _insert_column(db, "dbo.hub", "id", is_primary_key=1, uniqueness_score=1.0)
    for name in ("other1", "other2"):
        fqn = f"dbo.{name}"
        _insert_table(db, fqn, row_count=1000)
        _insert_column(db, fqn, "hub_id")
        _insert_relationship(db, fqn, "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.a", "hub_id", "dbo.hub", "id")
    _insert_relationship(db, "dbo.z", "hub_id", "dbo.hub", "id")

    # No other connector between a and z besides the (zero-row, high-fanin)
    # hub -- since it's not excluded, this must still resolve through it.
    result = recommend_best_join_path(1, "u1", "dbo.a", "dbo.z")
    assert result["best_join_path"]["path"] == ["dbo.a", "dbo.hub", "dbo.z"]


def test_junction_table_never_excluded_regardless_of_size_or_fanin(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    _insert_table(db, "dbo.a", row_count=5000)
    _insert_column(db, "dbo.a", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.a", "hub_id")
    _insert_table(db, "dbo.z", row_count=5000)
    _insert_column(db, "dbo.z", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.z", "hub_id")

    _insert_table(db, "dbo.bridge", row_count=40)
    _insert_column(db, "dbo.bridge", "id", is_primary_key=1, uniqueness_score=1.0)
    _mark_junction(db, "dbo.bridge")
    for name in ("other1", "other2"):
        fqn = f"dbo.{name}"
        _insert_table(db, fqn, row_count=1000)
        _insert_column(db, fqn, "hub_id")
        _insert_relationship(db, fqn, "hub_id", "dbo.bridge", "id")
    _insert_relationship(db, "dbo.a", "hub_id", "dbo.bridge", "id")
    _insert_relationship(db, "dbo.z", "hub_id", "dbo.bridge", "id")

    # A genuine, more direct alternative also exists.
    _insert_column(db, "dbo.a", "z_id")
    _insert_relationship(db, "dbo.a", "z_id", "dbo.z", "id")

    result = recommend_best_join_path(1, "u1", "dbo.a", "dbo.z")
    # The direct path still wins on hop count (unaffected, this test only
    # asserts the junction table is never a member of the EXCLUDED set --
    # verified directly below), not a claim about ranking preference.
    from data.db import get_connection
    from data.semantic_layer_service import _load_edges, _spurious_lookup_hub_tables, _latest_schema_snap_id
    conn = get_connection()
    snap_id = _latest_schema_snap_id(conn, 1)
    edges = _load_edges(conn, 1, snap_id)
    hubs = _spurious_lookup_hub_tables(conn, 1, edges)
    conn.close()
    assert "dbo.bridge" not in hubs
    assert result["best_join_path"]["path"] == ["dbo.a", "dbo.z"]


def test_low_fanin_small_table_not_excluded(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    _insert_table(db, "dbo.a", row_count=5000)
    _insert_column(db, "dbo.a", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.a", "small_id")
    _insert_table(db, "dbo.small", row_count=10)
    _insert_column(db, "dbo.small", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.a", "small_id", "dbo.small", "id")

    # Only ONE table connects to dbo.small -- below _SMALL_LOOKUP_MIN_FANIN,
    # so it is never a "shared hub" in the first place (no shortcut risk).
    result = recommend_best_join_path(1, "u1", "dbo.a", "dbo.small")
    assert result["best_join_path"]["path"] == ["dbo.a", "dbo.small"]
