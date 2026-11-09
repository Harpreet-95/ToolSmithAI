"""
Tests for core.lifecycle.governance_impact — pure governance-impact detection
used by the autonomous metadata lifecycle (Phase 11). No DB access.

Run from the project root:
    python -m pytest tests/test_lifecycle_governance_impact.py -v
"""
from __future__ import annotations

from core.lifecycle.governance_impact import (
    detect_new_pii, detect_reclassification, detect_schema_drift,
)
from core.lifecycle.models import ChangeSet, ColumnChange


class TestDetectNewPii:
    def test_column_flipping_to_pii_is_flagged(self):
        before = {("dbo.customers", "ssn"): False}
        after = {("dbo.customers", "ssn"): True}
        items = detect_new_pii(before, after)
        assert len(items) == 1
        assert items[0].object_type == "pii.new"
        assert items[0].table_fqn == "dbo.customers"
        assert items[0].column_name == "ssn"

    def test_already_pii_not_reflagged(self):
        before = {("dbo.customers", "ssn"): True}
        after = {("dbo.customers", "ssn"): True}
        assert detect_new_pii(before, after) == []

    def test_non_pii_column_not_flagged(self):
        before = {("dbo.customers", "name"): False}
        after = {("dbo.customers", "name"): False}
        assert detect_new_pii(before, after) == []

    def test_brand_new_pii_column_flagged(self):
        # Column absent from `before` (didn't exist yet) and is PII in `after`.
        before: dict = {}
        after = {("dbo.customers", "ssn"): True}
        items = detect_new_pii(before, after)
        assert len(items) == 1
        assert items[0].column_name == "ssn"


class TestDetectReclassification:
    def test_domain_change_flagged(self):
        before = {"dbo.orders": "Sales"}
        after = {"dbo.orders": "Finance"}
        items = detect_reclassification("domain.assignment", before, after)
        assert len(items) == 1
        assert items[0].object_type == "domain.assignment"
        assert items[0].table_fqn == "dbo.orders"
        assert items[0].suggested_domain == "Finance"

    def test_entity_change_flagged(self):
        before = {"dbo.orders": "Order"}
        after = {"dbo.orders": "Transaction"}
        items = detect_reclassification("entity.assignment", before, after)
        assert len(items) == 1
        assert items[0].object_type == "entity.assignment"
        assert items[0].suggested_entity == "Transaction"

    def test_unchanged_value_not_flagged(self):
        before = {"dbo.orders": "Sales"}
        after = {"dbo.orders": "Sales"}
        assert detect_reclassification("domain.assignment", before, after) == []

    def test_first_time_assignment_not_flagged(self):
        # table_fqn absent from `before` — first-ever classification, not a reclassification.
        before: dict = {}
        after = {"dbo.orders": "Sales"}
        assert detect_reclassification("domain.assignment", before, after) == []


class TestDetectSchemaDrift:
    def test_removed_table_flagged(self):
        cs = ChangeSet(removed_tables=["dbo.legacy_orders"])
        items = detect_schema_drift(cs)
        assert len(items) == 1
        assert items[0].object_type == "schema.drift"
        assert items[0].table_fqn == "dbo.legacy_orders"
        assert items[0].column_name == ""

    def test_removed_column_flagged(self):
        cs = ChangeSet(
            modified_tables=["dbo.customers"],
            column_changes=[ColumnChange(
                table_fqn="dbo.customers", column_name="fax_number",
                change_type="removed", old_data_type="TEXT", new_data_type=None,
            )],
        )
        items = detect_schema_drift(cs)
        assert len(items) == 1
        assert items[0].column_name == "fax_number"

    def test_added_column_not_flagged(self):
        cs = ChangeSet(
            modified_tables=["dbo.customers"],
            column_changes=[ColumnChange(
                table_fqn="dbo.customers", column_name="email",
                change_type="added", old_data_type=None, new_data_type="TEXT",
            )],
        )
        assert detect_schema_drift(cs) == []

    def test_added_table_not_flagged(self):
        cs = ChangeSet(added_tables=["dbo.new_table"])
        assert detect_schema_drift(cs) == []

    def test_type_changed_column_not_flagged(self):
        cs = ChangeSet(
            modified_tables=["dbo.customers"],
            column_changes=[ColumnChange(
                table_fqn="dbo.customers", column_name="balance",
                change_type="type_changed", old_data_type="INTEGER", new_data_type="DECIMAL",
            )],
        )
        assert detect_schema_drift(cs) == []
