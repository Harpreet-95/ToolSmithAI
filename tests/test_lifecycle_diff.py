"""
Tests for core.lifecycle.diff.diff_snapshots — pure schema-diffing logic used
by the autonomous metadata lifecycle (Phase 11). No DB access.

Run from the project root:
    python -m pytest tests/test_lifecycle_diff.py -v
"""
from __future__ import annotations

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.lifecycle.diff import diff_snapshots


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
    return SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at="2026-01-01T00:00:00+00:00",
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )


class TestFirstScan:
    def test_old_none_reports_every_table_as_added(self):
        new = _snapshot([_table("dbo", "customers", [_col("id"), _col("name")])])
        cs = diff_snapshots(None, new)
        assert cs.is_first_scan is True
        assert cs.added_tables == ["dbo.customers"]
        assert cs.removed_tables == []
        assert cs.modified_tables == []
        assert cs.column_changes == []
        assert cs.has_changes is True
        assert cs.affected_table_fqns == ["dbo.customers"]


class TestIdenticalSnapshots:
    def test_no_changes_detected(self):
        tables = [_table("dbo", "customers", [_col("id"), _col("name")])]
        old = _snapshot(tables)
        new = _snapshot([_table("dbo", "customers", [_col("id"), _col("name")])])
        cs = diff_snapshots(old, new)
        assert cs.added_tables == []
        assert cs.removed_tables == []
        assert cs.modified_tables == []
        assert cs.column_changes == []
        assert cs.has_changes is False
        assert cs.is_first_scan is False


class TestTableAddedRemoved:
    def test_new_table_detected(self):
        old = _snapshot([_table("dbo", "customers", [_col("id")])])
        new = _snapshot([
            _table("dbo", "customers", [_col("id")]),
            _table("dbo", "orders", [_col("id"), _col("customer_id")]),
        ])
        cs = diff_snapshots(old, new)
        assert cs.added_tables == ["dbo.orders"]
        assert cs.removed_tables == []
        assert cs.modified_tables == []
        assert cs.affected_table_fqns == ["dbo.orders"]

    def test_removed_table_detected(self):
        old = _snapshot([
            _table("dbo", "customers", [_col("id")]),
            _table("dbo", "orders", [_col("id")]),
        ])
        new = _snapshot([_table("dbo", "customers", [_col("id")])])
        cs = diff_snapshots(old, new)
        assert cs.removed_tables == ["dbo.orders"]
        assert cs.added_tables == []
        assert cs.modified_tables == []
        # Removed tables are excluded from affected_table_fqns — nothing to regenerate.
        assert cs.affected_table_fqns == []
        assert cs.has_changes is True


class TestColumnChanges:
    def test_column_added(self):
        old = _snapshot([_table("dbo", "customers", [_col("id")])])
        new = _snapshot([_table("dbo", "customers", [_col("id"), _col("email")])])
        cs = diff_snapshots(old, new)
        assert cs.modified_tables == ["dbo.customers"]
        assert len(cs.column_changes) == 1
        change = cs.column_changes[0]
        assert change.table_fqn == "dbo.customers"
        assert change.column_name == "email"
        assert change.change_type == "added"
        assert change.old_data_type is None
        assert change.new_data_type == "TEXT"
        assert cs.affected_table_fqns == ["dbo.customers"]

    def test_column_removed(self):
        old = _snapshot([_table("dbo", "customers", [_col("id"), _col("email")])])
        new = _snapshot([_table("dbo", "customers", [_col("id")])])
        cs = diff_snapshots(old, new)
        assert cs.modified_tables == ["dbo.customers"]
        change = cs.column_changes[0]
        assert change.column_name == "email"
        assert change.change_type == "removed"
        assert change.old_data_type == "TEXT"
        assert change.new_data_type is None

    def test_column_type_changed(self):
        old = _snapshot([_table("dbo", "customers", [_col("balance", "INTEGER")])])
        new = _snapshot([_table("dbo", "customers", [_col("balance", "DECIMAL")])])
        cs = diff_snapshots(old, new)
        assert cs.modified_tables == ["dbo.customers"]
        change = cs.column_changes[0]
        assert change.change_type == "type_changed"
        assert change.old_data_type == "INTEGER"
        assert change.new_data_type == "DECIMAL"

    def test_unrelated_table_not_marked_modified(self):
        old = _snapshot([
            _table("dbo", "customers", [_col("id")]),
            _table("dbo", "products", [_col("id")]),
        ])
        new = _snapshot([
            _table("dbo", "customers", [_col("id"), _col("email")]),
            _table("dbo", "products", [_col("id")]),
        ])
        cs = diff_snapshots(old, new)
        assert cs.modified_tables == ["dbo.customers"]
        assert "dbo.products" not in cs.modified_tables


class TestAffectedTableFqnsDedup:
    def test_added_and_modified_are_deduped_and_order_stable(self):
        cs_added_only = _snapshot([_table("dbo", "a", [_col("id")])])
        # affected_table_fqns should never contain duplicates even if a table
        # somehow appeared in both lists (defensive — shouldn't happen via diff_snapshots).
        from core.lifecycle.models import ChangeSet
        cs = ChangeSet(added_tables=["dbo.a", "dbo.b"], modified_tables=["dbo.b", "dbo.c"])
        assert cs.affected_table_fqns == ["dbo.a", "dbo.b", "dbo.c"]
