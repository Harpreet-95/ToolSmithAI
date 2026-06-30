"""
Tests for Business Knowledge Graph Phase 1 — Relationship Engine.

Covers:
  - extract_relationships: parsing FK data from snapshot_json, malformed JSON, missing fields
  - persist_relationships: basic insert and idempotent INSERT OR IGNORE behaviour
  - get_relationships_for_table: outbound and inbound FK views, ownership check
  - get_relationships_for_source: full list for a source, ownership check
  - get_relationship_summary: aggregate counts
  - extract_and_persist_relationships: end-to-end integration

Run from the project root:
    venv/Scripts/pytest tests/test_phase1_relationships.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-rel-phase1-secret-long-enough-12345")
os.environ.setdefault("USER_ID_SALT", "test-salt-rel-phase1-long-enough-value1")

from data.relationship_service import (
    extract_and_persist_relationships,
    extract_relationships,
    get_relationship_summary,
    get_relationships_for_source,
    get_relationships_for_table,
    persist_relationships,
)


# ---------------------------------------------------------------------------
# Minimal schema (only tables the relationship service reads/writes)
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        source_category       TEXT    NOT NULL DEFAULT 'RELATIONAL',
        encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json     TEXT    NOT NULL DEFAULT '[]',
        metadata_json         TEXT    NOT NULL DEFAULT '{}',
        source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
        is_active             INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT    NOT NULL DEFAULT '2026-01-01',
        updated_at            TEXT    NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE schema_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type      TEXT    NOT NULL DEFAULT 'mssql',
        table_count      INTEGER NOT NULL DEFAULT 0,
        view_count       INTEGER NOT NULL DEFAULT 0,
        column_count     INTEGER NOT NULL DEFAULT 0,
        snapshot_json    TEXT    NOT NULL DEFAULT '{}',
        discovered_at    TEXT    NOT NULL DEFAULT '2026-01-01',
        created_at       TEXT    NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE table_relationships (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL,
        from_schema       TEXT    NOT NULL,
        from_table        TEXT    NOT NULL,
        from_table_fqn    TEXT    NOT NULL,
        from_column       TEXT    NOT NULL,
        to_schema         TEXT    NOT NULL,
        to_table          TEXT    NOT NULL,
        to_table_fqn      TEXT    NOT NULL,
        to_column         TEXT    NOT NULL,
        relationship_name TEXT,
        relationship_type TEXT    NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence        REAL    NOT NULL DEFAULT 1.0,
        evidence_json     TEXT,
        created_at        TEXT    NOT NULL DEFAULT '2026-01-01'
    );
    CREATE UNIQUE INDEX idx_tr_uniq ON table_relationships
        (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
"""


def _create_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Snapshot JSON builders
# ---------------------------------------------------------------------------

def _snap_json(fks: list[dict], schema: str = "dbo", table: str = "orders") -> str:
    """Build a minimal snapshot_json string with one table containing the given FKs."""
    return json.dumps({
        "schemas": [{
            "schema_name": schema,
            "tables": [{
                "table_name": table,
                "table_fqn":  f"{schema}.{table}",
                "foreign_keys": fks,
            }],
        }]
    })


_FK_ORDERS_CUSTOMERS = {
    "from_column": "customer_id",
    "to_schema":   "dbo",
    "to_table":    "customers",
    "to_column":   "id",
    "fk_name":     "FK_orders_customers",
}

_FK_ORDERS_PRODUCTS = {
    "from_column": "product_id",
    "to_schema":   "dbo",
    "to_table":    "products",
    "to_column":   "id",
    "fk_name":     "FK_orders_products",
}


def _rel_dict(
    source_id: int,
    snapshot_id: int,
    from_fqn: str,
    from_col: str,
    to_fqn: str,
    to_col: str,
    fk_name: str = "FK_test",
) -> dict:
    f_parts = from_fqn.split(".", 1)
    t_parts = to_fqn.split(".", 1)
    return {
        "source_id":         source_id,
        "snapshot_id":       snapshot_id,
        "from_schema":       f_parts[0],
        "from_table":        f_parts[1] if len(f_parts) > 1 else from_fqn,
        "from_table_fqn":    from_fqn,
        "from_column":       from_col,
        "to_schema":         t_parts[0],
        "to_table":          t_parts[1] if len(t_parts) > 1 else to_fqn,
        "to_table_fqn":      to_fqn,
        "to_column":         to_col,
        "relationship_name": fk_name,
        "relationship_type": "FOREIGN_KEY",
        "confidence":        1.0,
        "evidence_json":     "{}",
        "created_at":        "2026-01-01T00:00:00+00:00",
    }


def _seed_two_fks(path: str) -> None:
    """
    Seed source=1/snapshot=1 with two FK rows:
      dbo.orders.customer_id  → dbo.customers.id
      dbo.invoices.order_id   → dbo.orders.id

    For dbo.orders this means:
      outbound: orders → customers  (orders declares the FK)
      inbound:  invoices → orders   (invoices declares the FK)
    """
    c = _db_conn(path)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u-rel')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, table_count)"
        " VALUES (1, 1, 1, 3)"
    )
    for r in [
        _rel_dict(1, 1, "dbo.orders",   "customer_id", "dbo.customers", "id",
                  "FK_orders_customers"),
        _rel_dict(1, 1, "dbo.invoices",  "order_id",    "dbo.orders",   "id",
                  "FK_invoices_orders"),
    ]:
        c.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn,"
            " from_column, to_schema, to_table, to_table_fqn, to_column,"
            " relationship_name, relationship_type, confidence, evidence_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["source_id"], r["snapshot_id"], r["from_schema"], r["from_table"],
             r["from_table_fqn"], r["from_column"], r["to_schema"], r["to_table"],
             r["to_table_fqn"], r["to_column"], r["relationship_name"],
             r["relationship_type"], r["confidence"], r["evidence_json"], r["created_at"]),
        )
    c.commit()
    c.close()


# ===========================================================================
# Part 1 — extract_relationships
# ===========================================================================

def test_extract_returns_correct_fields(tmp_path, monkeypatch):
    """extract_relationships parses snapshot_json and returns well-formed dicts."""
    db = str(tmp_path / "r1.db")
    _create_db(db)
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        (_snap_json([_FK_ORDERS_CUSTOMERS]),),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    rels = extract_relationships(snapshot_id=1, source_id=1)

    assert len(rels) == 1
    r = rels[0]
    assert r["from_table_fqn"]    == "dbo.orders"
    assert r["from_column"]       == "customer_id"
    assert r["to_table_fqn"]      == "dbo.customers"
    assert r["to_column"]         == "id"
    assert r["relationship_type"] == "FOREIGN_KEY"
    assert r["confidence"]        == 1.0
    assert r["source_id"]         == 1
    assert r["snapshot_id"]       == 1
    assert r["relationship_name"] == "FK_orders_customers"


def test_extract_multiple_fks_on_one_table(tmp_path, monkeypatch):
    """Multiple FKs on a single table are all extracted."""
    db = str(tmp_path / "r2.db")
    _create_db(db)
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        (_snap_json([_FK_ORDERS_CUSTOMERS, _FK_ORDERS_PRODUCTS]),),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    rels = extract_relationships(snapshot_id=1, source_id=1)

    assert len(rels) == 2
    assert {r["to_table"] for r in rels} == {"customers", "products"}


def test_extract_malformed_json_returns_empty(tmp_path, monkeypatch):
    """Malformed snapshot_json must return [] without raising."""
    db = str(tmp_path / "r3.db")
    _create_db(db)
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        ("NOT VALID JSON {{{",),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    assert extract_relationships(snapshot_id=1, source_id=1) == []


def test_extract_incomplete_fk_fields_skipped(tmp_path, monkeypatch):
    """FK entries missing from_column, to_table, or to_column are silently skipped;
    a valid FK in the same snapshot still passes through."""
    db = str(tmp_path / "r4.db")
    _create_db(db)

    bad_no_from_col = {"to_schema": "dbo", "to_table": "customers", "to_column": "id"}
    bad_no_to_table = {"from_column": "x_id", "to_schema": "dbo", "to_column": "id"}
    bad_no_to_col   = {"from_column": "y_id", "to_schema": "dbo", "to_table": "customers"}

    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        (_snap_json([bad_no_from_col, bad_no_to_table, bad_no_to_col,
                     _FK_ORDERS_CUSTOMERS]),),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    rels = extract_relationships(snapshot_id=1, source_id=1)
    assert len(rels) == 1
    assert rels[0]["from_column"] == "customer_id"


def test_extract_nonexistent_snapshot_returns_empty(tmp_path, monkeypatch):
    """Requesting a snapshot_id that doesn't exist returns []."""
    db = str(tmp_path / "r5.db")
    _create_db(db)
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    assert extract_relationships(snapshot_id=999, source_id=1) == []


def test_extract_no_fks_in_snapshot_returns_empty(tmp_path, monkeypatch):
    """A valid snapshot where tables have no FK entries returns []."""
    db = str(tmp_path / "r6.db")
    _create_db(db)
    snap_json = json.dumps({
        "schemas": [{"schema_name": "dbo", "tables": [
            {"table_name": "lookup", "table_fqn": "dbo.lookup", "foreign_keys": []}
        ]}]
    })
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        (snap_json,),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    assert extract_relationships(snapshot_id=1, source_id=1) == []


# ===========================================================================
# Part 2 — persist_relationships (idempotency)
# ===========================================================================

def test_persist_inserts_rows_and_returns_count(tmp_path):
    """persist_relationships inserts rows and returns the correct inserted count."""
    db = str(tmp_path / "p1.db")
    _create_db(db)

    rels = [
        _rel_dict(1, 1, "dbo.orders", "customer_id", "dbo.customers", "id"),
        _rel_dict(1, 1, "dbo.orders", "product_id",  "dbo.products",  "id"),
    ]
    conn = _db_conn(db)
    inserted = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=rels)
    conn.close()

    assert inserted == 2
    verify = _db_conn(db)
    assert verify.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0] == 2
    verify.close()


def test_persist_is_idempotent(tmp_path):
    """Second persist call over the same snapshot inserts 0 rows (INSERT OR IGNORE)."""
    db = str(tmp_path / "p2.db")
    _create_db(db)

    rels = [_rel_dict(1, 1, "dbo.orders", "customer_id", "dbo.customers", "id")]
    conn = _db_conn(db)
    first  = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=rels)
    second = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=rels)
    conn.close()

    assert first  == 1
    assert second == 0

    verify = _db_conn(db)
    assert verify.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0] == 1
    verify.close()


def test_persist_empty_list_returns_zero(tmp_path):
    """persist_relationships with an empty list returns 0 without touching the DB."""
    db = str(tmp_path / "p3.db")
    _create_db(db)

    conn = _db_conn(db)
    result = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[])
    conn.close()

    assert result == 0


# ===========================================================================
# Part 3 — get_relationships_for_table (outbound + inbound + ownership)
# ===========================================================================

def test_get_for_table_outbound(tmp_path, monkeypatch):
    """outbound list contains the FK that dbo.orders declares."""
    db = str(tmp_path / "t1.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    result = get_relationships_for_table(source_id=1, user_id="u-rel", table_fqn="dbo.orders")

    assert result is not None
    assert len(result["outbound"]) == 1
    ob = result["outbound"][0]
    assert ob["from_column"]  == "customer_id"
    assert ob["to_table_fqn"] == "dbo.customers"
    assert ob["to_column"]    == "id"


def test_get_for_table_inbound(tmp_path, monkeypatch):
    """inbound list contains the FK that dbo.invoices declares pointing at dbo.orders."""
    db = str(tmp_path / "t2.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    result = get_relationships_for_table(source_id=1, user_id="u-rel", table_fqn="dbo.orders")

    assert result is not None
    assert len(result["inbound"]) == 1
    ib = result["inbound"][0]
    assert ib["from_table_fqn"] == "dbo.invoices"
    assert ib["from_column"]    == "order_id"
    assert ib["to_column"]      == "id"


def test_get_for_table_wrong_user_returns_none(tmp_path, monkeypatch):
    """A wrong user_id fails the ownership check and returns None."""
    db = str(tmp_path / "t3.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    assert get_relationships_for_table(1, "WRONG-USER", "dbo.orders") is None


def test_get_for_table_no_fks_returns_empty_lists(tmp_path, monkeypatch):
    """A table with no FK edges returns outbound=[] and inbound=[]."""
    db = str(tmp_path / "t4.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    result = get_relationships_for_table(source_id=1, user_id="u-rel", table_fqn="dbo.products")

    assert result is not None
    assert result["outbound"] == []
    assert result["inbound"]  == []


# ===========================================================================
# Part 4 — get_relationships_for_source + get_relationship_summary
# ===========================================================================

def test_get_for_source_returns_all_rows(tmp_path, monkeypatch):
    """get_relationships_for_source returns both FK rows for the source."""
    db = str(tmp_path / "s1.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    rows = get_relationships_for_source(source_id=1, user_id="u-rel")

    assert rows is not None
    assert len(rows) == 2
    assert {r["from_table_fqn"] for r in rows} == {"dbo.orders", "dbo.invoices"}


def test_get_for_source_wrong_user_returns_none(tmp_path, monkeypatch):
    db = str(tmp_path / "s2.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    assert get_relationships_for_source(source_id=1, user_id="WRONG") is None


def test_get_relationship_summary_counts(tmp_path, monkeypatch):
    """get_relationship_summary returns correct aggregate counts."""
    db = str(tmp_path / "s3.db")
    _create_db(db)
    _seed_two_fks(db)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    summary = get_relationship_summary(source_id=1, user_id="u-rel")

    assert summary is not None
    assert summary["total_relationships"]      == 2
    # Both orders and invoices have outbound FKs
    assert summary["tables_with_outbound_fks"] == 2
    # customers and orders are each referenced by one FK
    assert summary["tables_referenced_by_fk"]  == 2
    # most_referenced list has at least one entry
    assert len(summary["most_referenced"]) >= 1


# ===========================================================================
# Part 5 — extract_and_persist_relationships (end-to-end)
# ===========================================================================

def test_extract_and_persist_end_to_end(tmp_path, monkeypatch):
    """extract_and_persist_relationships returns correct counts and rows land in DB."""
    db = str(tmp_path / "e2e.db")
    _create_db(db)
    c = _db_conn(db)
    c.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (1, 1, ?)",
        (_snap_json([_FK_ORDERS_CUSTOMERS, _FK_ORDERS_PRODUCTS]),),
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: _db_conn(db))

    result = extract_and_persist_relationships(snapshot_id=1, source_id=1)

    assert result["relationships_found"]    == 2
    assert result["relationships_inserted"] == 2

    # Second call must be idempotent — inserts 0 new rows
    result2 = extract_and_persist_relationships(snapshot_id=1, source_id=1)
    assert result2["relationships_found"]    == 2
    assert result2["relationships_inserted"] == 0
