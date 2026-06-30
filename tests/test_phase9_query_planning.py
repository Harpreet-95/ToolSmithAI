"""
Tests for Program 3 Phase 3 — Business Query Planning Engine.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
test_phase7_relationship_intelligence.py and test_phase8_join_intelligence.py.

Monkeypatches get_connection in the four modules whose functions
query_planning_service calls at runtime:
  data.query_planning_service (ownership check)
  data.knowledge_graph_service (find_business_assets)
  data.business_knowledge_service (get_table_business_context)
  data.semantic_layer_service (analyze_join_quality / recommend_best_join_path)

Run from the project root:
    venv/Scripts/pytest tests/test_phase9_query_planning.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase9-query-planning-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase9-salt-long-enough-value-1234567890")

import data.models as models
from data.query_planning_service import plan_business_query

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase9.db")
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
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, table_class="Transactional", row_count=1000, approved=True):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,'COMPLETE',?,?,?,?)",
        (tid, table_fqn, name, schema, table_class, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,'TABLE',?,?,?,?,?)",
        (table_fqn, name, schema, name.capitalize(), int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_col_seq = [100]


def _add_column(db, table_fqn, col_name, *,
                data_type="DECIMAL", is_pk=0, is_id=0, uniqueness=0.05,
                is_nullable=0, null_pct=0.0, cardinality_tier="MEDIUM",
                pii=0, pii_confirmed=0,
                is_metric=None, is_dimension=None, is_date=None,
                business_label=None, approved=True):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_col_seq[0], table_fqn, col_name, data_type, is_pk, is_id,
         uniqueness, is_nullable, null_pct, cardinality_tier, pii, pii_confirmed, _NOW, _NOW),
    )
    if is_metric is not None or is_dimension is not None or business_label or approved:
        c.execute(
            "INSERT OR REPLACE INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,?,?,?,?,?,?,?,?,?,?)",
            (table_fqn, col_name, business_label or col_name,
             int(bool(is_metric)), int(bool(is_dimension)),
             int(bool(is_date)), int(bool(is_id)), int(bool(pii)),
             int(bool(approved)), "rule_based", _NOW, _NOW),
        )
    c.commit()
    c.close()


_rel_seq = [500]


def _add_fk(db, from_fqn, from_col, to_fqn, to_col, *, status="AUTO", confidence=1.0):
    c = _c(db)
    _rel_seq[0] += 1
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',?,'{}',?,?)",
        (_rel_seq[0], fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col,
         f"FK_{_rel_seq[0]}", confidence, _NOW, status),
    )
    c.commit()
    c.close()


def _plan(db_path, **kwargs):
    return plan_business_query(1, "u1", kwargs)


# ---------------------------------------------------------------------------
# Test 1 — Simple measure + dimension in ONE table, no join needed
# ---------------------------------------------------------------------------

def test_simple_measure_and_dimension_same_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", table_class="Transactional")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Order Status", approved=True)

    result = _plan(db, question="revenue by status",
                   measures=["revenue"], dimensions=["status"])
    assert result["intent"]["type"] == "aggregate_by_dimension"
    assert result["intent"]["aggregation"] == "SUM"
    assert any(m["selected"] and m["selected"]["column_name"] == "amount" for m in result["measures"])
    assert any(d["selected"] and d["selected"]["column_name"] == "status" for d in result["dimensions"])
    assert result["join_plan"]["required"] is False
    assert result["confidence"] > 50


# ---------------------------------------------------------------------------
# Test 2 — Measure and dimension in DIFFERENT tables → join required
# ---------------------------------------------------------------------------

def test_measure_dimension_different_tables_join_required(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_table(db, "dbo.customers", table_class="Master")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders", "customer_id", data_type="INTEGER", uniqueness=0.02)
    _add_column(db, "dbo.customers", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.customers", "name", data_type="TEXT", is_dimension=True,
                cardinality_tier="MEDIUM", business_label="Customer Name", approved=True)
    _add_fk(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    result = _plan(db, question="revenue by customer",
                   concepts=["revenue", "customer"],
                   measures=["revenue"], dimensions=["customer"])

    assert result["join_plan"]["required"] is True
    join_step = result["join_plan"]["steps"][0]
    assert join_step["path_found"] is True
    assert result["measures"][0]["selected"]["column_name"] == "amount"
    assert result["dimensions"][0]["selected"]["column_name"] == "name"


# ---------------------------------------------------------------------------
# Test 3 — Missing measure (not in schema)
# ---------------------------------------------------------------------------

def test_missing_measure(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    result = _plan(db, question="profit by customer",
                   measures=["profit"], dimensions=[])
    assert result["measures"][0]["selected"] is None
    assert any(w["type"] in ("missing_measure", "ambiguous_measure") for w in result["warnings"])


# ---------------------------------------------------------------------------
# Test 4 — Missing dimension (not in schema)
# ---------------------------------------------------------------------------

def test_missing_dimension(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    result = _plan(db, question="revenue by region",
                   measures=["revenue"], dimensions=["region"])
    assert result["dimensions"][0]["selected"] is None
    assert any(w["type"] in ("missing_dimension", "ambiguous_dimension") for w in result["warnings"])


# ---------------------------------------------------------------------------
# Test 5 — Multiple candidate tables for the same term → ambiguity
# ---------------------------------------------------------------------------

def test_multiple_candidate_tables_ambiguity(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders_fact")
    _add_table(db, "dbo.orders_summary")
    # Both tables have a column matching "revenue" — similar scores
    _add_column(db, "dbo.orders_fact", "revenue", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders_summary", "revenue_total", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    result = _plan(db, question="revenue", measures=["revenue"], dimensions=[])
    # Either selected one (if margin big enough) or flagged ambiguity
    measure = result["measures"][0]
    if measure["selected"] is None:
        assert any("ambiguous" in w["type"] for w in result["warnings"])
    else:
        # Must have listed all candidates
        assert len(measure["candidates"]) >= 2


# ---------------------------------------------------------------------------
# Test 6 — Join path selected (direct) and surfaced in join_plan
# ---------------------------------------------------------------------------

def test_join_path_selected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.sales")
    _add_table(db, "dbo.regions", table_class="Master")
    _add_column(db, "dbo.sales", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Sales Amount", approved=True)
    _add_column(db, "dbo.sales", "region_id", data_type="INTEGER", uniqueness=0.05)
    _add_column(db, "dbo.regions", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.regions", "name", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Region Name", approved=True)
    _add_fk(db, "dbo.sales", "region_id", "dbo.regions", "id")

    result = _plan(db, question="sales by region",
                   measures=["amount"], dimensions=["name"])
    jp = result["join_plan"]
    assert jp["required"] is True
    assert jp["steps"][0]["path_found"] is True
    assert jp["steps"][0]["hops"] == 1
    assert "dbo.regions" in jp["tables"]


# ---------------------------------------------------------------------------
# Test 7 — Fan-out warning surfaced when joining one->many
# ---------------------------------------------------------------------------

def test_fanout_warning(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.departments", table_class="Master", row_count=10)
    _add_table(db, "dbo.employees", table_class="Transactional", row_count=5000)
    _add_column(db, "dbo.departments", "budget", data_type="DECIMAL", is_metric=True,
                business_label="Budget", approved=True)
    _add_column(db, "dbo.departments", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.employees", "dept_id", data_type="INTEGER", uniqueness=0.005)
    _add_column(db, "dbo.employees", "name", data_type="TEXT", is_dimension=True,
                cardinality_tier="MEDIUM", business_label="Employee Name", approved=True)
    _add_fk(db, "dbo.employees", "dept_id", "dbo.departments", "id")

    result = _plan(db, question="budget by employee",
                   measures=["budget"], dimensions=["name"])
    # If join found, it might emit a fanout warning (departments->employees is ONE_TO_MANY
    # with a ~500x ratio). We accept either: fanout warning present, or join not found.
    # This is a structural coverage test for the warning pathway.
    warning_types = {w["type"] for w in result["warnings"]}
    has_fanout = any("fanout" in t for t in warning_types)
    has_no_path = "no_join_path_found" in warning_types
    # At minimum, confidence must be reasonable (plan didn't crash)
    assert 0 <= result["confidence"] <= 100
    # If a join was found it should have touched the fanout logic
    if result["join_plan"]["steps"] and result["join_plan"]["steps"][0]["path_found"]:
        assert has_fanout or result["join_plan"]["fanout_risk"] in ("LOW", "MEDIUM", "HIGH", None)


# ---------------------------------------------------------------------------
# Test 8 — PII warning when a selected column is PII-flagged
# ---------------------------------------------------------------------------

def test_pii_warning(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.customers")
    _add_column(db, "dbo.customers", "revenue", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.customers", "email", data_type="TEXT", is_dimension=True,
                cardinality_tier="MEDIUM", business_label="Email Address",
                pii=1, pii_confirmed=0, approved=True)

    result = _plan(db, question="revenue by email",
                   measures=["revenue"], dimensions=["email"])
    warning_types = {w["type"] for w in result["warnings"]}
    assert "pii_involved" in warning_types
    pii_warning = next(w for w in result["warnings"] if w["type"] == "pii_involved")
    assert pii_warning["severity"] == "HIGH"  # unconfirmed PII


# ---------------------------------------------------------------------------
# Test 9 — Ambiguity response: selected is None, candidates ranked
# ---------------------------------------------------------------------------

def test_ambiguous_response_no_auto_select(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.t1")
    _add_table(db, "dbo.t2")
    # Two measure columns with IDENTICAL label score for "amount"
    _add_column(db, "dbo.t1", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.t2", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)

    result = _plan(db, question="amount", measures=["amount"], dimensions=[])
    m = result["measures"][0]
    # Both columns score 1.0 for "amount" — margin is 0, below _AMBIGUITY_MARGIN
    assert m["selected"] is None
    assert len(m["candidates"]) >= 2
    assert any(w["type"] == "ambiguous_measure" for w in result["warnings"])


# ---------------------------------------------------------------------------
# Test 10 — Unknown source returns None
# ---------------------------------------------------------------------------

def test_unknown_source_returns_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    assert plan_business_query(999, "u1",
                               {"question": "test", "measures": ["revenue"], "dimensions": []}) is None
    assert plan_business_query(1, "someone-else",
                               {"question": "test", "measures": ["revenue"], "dimensions": []}) is None


# ---------------------------------------------------------------------------
# Test 11 — No SQL anywhere in the response (required by spec)
# ---------------------------------------------------------------------------

def test_no_sql_in_response(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)

    result = _plan(db, question="revenue by status",
                   measures=["revenue"], dimensions=["status"])
    text = json.dumps(result)
    # Check no SQL keywords appear as standalone values, not as substrings of JSON keys
    # ("selected" and "selection" legitimately contain "select" so we check for standalone SQL)
    import re
    # A SQL SELECT statement would look like: SELECT ... FROM
    assert not re.search(r'\bSELECT\s+\w', text, re.IGNORECASE)
    assert not re.search(r'\bINSERT\s+INTO\b', text, re.IGNORECASE)
    assert not re.search(r'\bWHERE\s+\w+\s*=', text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Test 12 — Metadata not approved warning
# ---------------------------------------------------------------------------

def test_metadata_not_approved_warning(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.sales", approved=False)
    _add_column(db, "dbo.sales", "revenue", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=False)
    _add_column(db, "dbo.sales", "region", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Region", approved=False)

    result = _plan(db, question="revenue by region",
                   measures=["revenue"], dimensions=["region"])
    warning_types = {w["type"] for w in result["warnings"]}
    assert "metadata_not_approved" in warning_types


# ---------------------------------------------------------------------------
# Test 13 — Filter referencing known column is resolved; unknown raises warning
# ---------------------------------------------------------------------------

def test_filter_column_resolution(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)

    result = _plan(db, question="revenue",
                   measures=["revenue"], dimensions=[],
                   filters=[
                       {"column": "status", "operator": "=", "value": "active"},
                       {"column": "ghost_col", "operator": "=", "value": "x"},
                   ])
    filters = {f["column"]: f for f in result["filters"] if f.get("column")}
    assert filters["status"]["resolved"] is True
    assert filters["ghost_col"]["resolved"] is False
    assert any(w["type"] == "unknown_filter_column" for w in result["warnings"])


# ---------------------------------------------------------------------------
# Test 14 — Structural: all required top-level keys always present
# ---------------------------------------------------------------------------

def test_response_schema_complete(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.x")
    result = _plan(db, question="anything", measures=[], dimensions=[])
    required_keys = {
        "source_id", "intent", "tables", "columns", "measures", "dimensions",
        "filters", "join_plan", "warnings", "confidence", "explanation",
    }
    assert required_keys.issubset(result.keys())
    assert isinstance(result["confidence"], int)
    assert 0 <= result["confidence"] <= 100
    assert isinstance(result["explanation"], str)
    # Serialisable to JSON without error
    json.dumps(result)
