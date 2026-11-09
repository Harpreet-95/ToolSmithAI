"""
Tests for get_table_business_contexts_batch() — Phase 3.2A / Task 4.

Proves the batched candidate-hydration path is field-for-field equivalent
to calling get_table_business_context() once per table_fqn (Task 7,
requirement 7), and that a RequestMetadataSession reduces connections
opened/reused correctly (requirements 1, 6).
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-bkg-batch-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-bkg-batch-salt-long-enough-1234567890")

from tests.test_phase3_business_knowledge import _create_bkg_db

from data.business_knowledge_service import (
    get_table_business_context,
    get_table_business_contexts_batch,
)
from data.request_metadata_session import RequestMetadataSession


def _seed_two_tables(path: str) -> None:
    """Two fully-populated tables (dictionary/domain/entity/profiling) with
    a relationship between them, plus a third table with NO metadata at
    all — exercises grouping, cross-table relationship attribution, and
    the "missing data" case in one scenario."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-X', 'Prod DB', 'mssql', 'RELATIONAL')"
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 3)"
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, status) "
        "VALUES (1, 1, 1, 'COMPLETE')"
    )

    for fqn, name, cls, conf in (
        ("dbo.orders", "orders", "TRANSACTION", 0.9),
        ("dbo.customers", "customers", "DIMENSION", 0.8),
    ):
        conn.execute(
            """INSERT INTO profiling_table_profiles
               (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_type,
                estimated_row_count, row_count_tier, fk_count, referenced_by_count,
                is_junction_table, is_root_table, is_leaf_table, has_identity_column,
                completeness_score, table_class, classification_confidence,
                pii_column_count, confirmed_pii_count, profiling_depth, profiling_status)
               VALUES (1, 1, ?, ?, 'dbo', 'TABLE',
                       1000, 'MEDIUM', 1, 0,
                       0, 0, 0, 1,
                       0.95, ?, ?,
                       0, 0, 'STRUCTURAL_ONLY', 'COMPLETE')""",
            (fqn, name, cls, conf),
        )
        conn.execute(
            """INSERT INTO profiling_column_profiles
               (profiling_snapshot_id, source_id, table_fqn, column_name,
                data_type, raw_type, is_nullable, is_primary_key, ordinal_position,
                distinct_count, null_percentage, cardinality_tier,
                semantic_type, semantic_confidence, pii_name_heuristic, pii_confirmed)
               VALUES (1, 1, ?, 'id', 'INTEGER', 'int', 0, 1, 1, 1000, 0.0, 'HIGH', 'ID', 0.95, 0, 0)""",
            (fqn,),
        )
        conn.execute(
            """INSERT INTO data_dictionary_tables
               (source_id, snapshot_id, table_fqn, table_name, schema_name, table_type,
                business_name, description, grain, is_approved, generation_method)
               VALUES (1, 1, ?, ?, 'dbo', 'TABLE', ?, ?, 'One row per record', 1, 'rule_based')""",
            (fqn, name, name.title(), f"Stores all {name}"),
        )
        conn.execute(
            """INSERT INTO data_dictionary_columns
               (source_id, snapshot_id, table_fqn, column_name,
                business_label, meaning, semantic_type, is_id, pii_risk, is_approved, generation_method)
               VALUES (1, 1, ?, 'id', 'ID', 'Unique identifier', 'ID', 1, 0, 1, 'rule_based')""",
            (fqn,),
        )
        conn.execute(
            """INSERT INTO domain_assignments
               (source_id, profiling_snapshot_id, table_fqn, domain, confidence, evidence_json)
               VALUES (1, 1, ?, 'Sales', 0.85, '[]')""",
            (fqn,),
        )
        conn.execute(
            """INSERT INTO entity_assignments
               (source_id, profiling_snapshot_id, table_fqn, entity, confidence, evidence_json)
               VALUES (1, 1, ?, ?, 0.9, '[]')""",
            (fqn, name.title().rstrip("s")),
        )

    # Relationship BETWEEN the two seeded tables — exercises both the
    # outbound side (orders) and inbound side (customers) of batching.
    conn.execute(
        """INSERT INTO table_relationships
           (source_id, snapshot_id, from_schema, from_table, from_table_fqn,
            from_column, to_schema, to_table, to_table_fqn, to_column,
            relationship_name, relationship_type, confidence, relationship_status)
           VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id',
                   'dbo', 'customers', 'dbo.customers', 'id',
                   'FK_orders_customers', 'FOREIGN_KEY', 1.0, 'AUTO')"""
    )

    conn.commit()
    conn.close()


def _db_conn(path: str):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _patch_connections(monkeypatch, db_path):
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db_path))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db_path))
    import data.db as db_module
    monkeypatch.setattr(db_module, "get_connection", lambda: _db_conn(db_path))


def test_batch_matches_individual_calls_field_for_field(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    individual = {
        fqn: get_table_business_context(1, "user-X", fqn)
        for fqn in ("dbo.orders", "dbo.customers")
    }
    batch = get_table_business_contexts_batch(1, "user-X", ["dbo.orders", "dbo.customers"])

    assert set(batch.keys()) == {"dbo.orders", "dbo.customers"}
    for fqn in ("dbo.orders", "dbo.customers"):
        assert batch[fqn] == individual[fqn], f"mismatch for {fqn}"


def test_batch_relationship_attribution_matches_individual(tmp_path, monkeypatch):
    """The relationship between orders (outbound) and customers (inbound)
    must land in the correct table's section in the batch result, exactly
    as it does for two separate single-table calls."""
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    orders_individual = get_table_business_context(1, "user-X", "dbo.orders")
    customers_individual = get_table_business_context(1, "user-X", "dbo.customers")
    batch = get_table_business_contexts_batch(1, "user-X", ["dbo.orders", "dbo.customers"])

    assert len(orders_individual["relationships"]["outbound"]) == 1
    assert orders_individual["relationships"]["outbound"][0]["to_table_fqn"] == "dbo.customers"
    assert len(customers_individual["relationships"]["inbound"]) == 1
    assert customers_individual["relationships"]["inbound"][0]["from_table_fqn"] == "dbo.orders"

    assert batch["dbo.orders"]["relationships"] == orders_individual["relationships"]
    assert batch["dbo.customers"]["relationships"] == customers_individual["relationships"]
    assert batch["dbo.orders"]["relationship_evidence"] == orders_individual["relationship_evidence"]
    assert batch["dbo.customers"]["relationship_evidence"] == customers_individual["relationship_evidence"]


def test_batch_matches_individual_for_a_table_with_no_metadata(tmp_path, monkeypatch):
    """A table_fqn with zero matching rows anywhere (but an owned source)
    still gets a full context shell from get_table_business_context — every
    section None/empty rather than the function returning None outright
    (None is reserved for "source not owned"). The batch path must match
    this exactly, not silently drop the entry."""
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    individual_missing = get_table_business_context(1, "user-X", "dbo.nonexistent")
    batch = get_table_business_contexts_batch(1, "user-X", ["dbo.orders", "dbo.nonexistent"])

    assert individual_missing is not None
    assert individual_missing["dictionary"] is None
    assert "dbo.orders" in batch
    assert "dbo.nonexistent" in batch
    assert batch["dbo.nonexistent"] == individual_missing


def test_batch_returns_empty_dict_for_empty_input(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    assert get_table_business_contexts_batch(1, "user-X", []) == {}


def test_batch_returns_empty_dict_for_unowned_source(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    assert get_table_business_contexts_batch(1, "wrong-user", ["dbo.orders"]) == {}


def test_batch_deduplicates_repeated_table_fqns(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)

    batch = get_table_business_contexts_batch(1, "user-X", ["dbo.orders", "dbo.orders", "dbo.orders"])
    assert set(batch.keys()) == {"dbo.orders"}


def test_batch_with_session_reuses_connection(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db)

    session = RequestMetadataSession(1, "user-X")
    try:
        batch = get_table_business_contexts_batch(1, "user-X", ["dbo.orders", "dbo.customers"], session=session)
        assert session.connections_opened == 1  # only the session's own connection
        assert set(batch.keys()) == {"dbo.orders", "dbo.customers"}
    finally:
        session.close()


def test_get_table_business_context_with_session_matches_without(tmp_path, monkeypatch):
    """Passing a session must not change get_table_business_context's own
    single-table output, only how its connection is obtained."""
    db = str(tmp_path / "bkg_batch.db")
    _create_bkg_db(db)
    _seed_two_tables(db)
    _patch_connections(monkeypatch, db)
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db)

    without_session = get_table_business_context(1, "user-X", "dbo.orders")
    session = RequestMetadataSession(1, "user-X")
    try:
        with_session = get_table_business_context(1, "user-X", "dbo.orders", session=session)
    finally:
        session.close()

    assert without_session == with_session
