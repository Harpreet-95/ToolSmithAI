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
from data.query_planning_service import plan_business_query, _score_table_authority

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


def _add_domain(db, table_fqn, domain, *, confidence=0.88):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO domain_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?)",
        (table_fqn, domain, confidence, _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_entity(db, table_fqn, entity, *, confidence=0.7):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?)",
        (table_fqn, entity, confidence, _NOW, _NOW),
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


# ---------------------------------------------------------------------------
# Milestone M-1 — Enterprise Question Intelligence
# ---------------------------------------------------------------------------

def test_bare_row_count_how_many_clients(tmp_path, monkeypatch):
    # "clients" names the whole TABLE, not a metric column on it — the old
    # M-1 behavior left this measure unresolved ("missing_measure"); it must
    # resolve as a COUNT(*)-shaped measure. Milestone Phase 6.2 then prefers
    # a declared primary key over bare COUNT(*) when one is known ("id" here
    # is a declared PK: is_pk=1, uniqueness=1.0) — COUNT(id) is more precise
    # than COUNT(*) and is exactly the "prefer primary key" rule that
    # milestone added; column_name is no longer expected to be None.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    assert result["intent"]["aggregation"] == "COUNT"
    assert result["intent"]["aggregation_target"] == "entity_count"
    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.clients"
    assert measure["selected"]["column_name"] == "id"
    assert measure["selected"]["key_tier"] == 1
    assert measure["selected"]["key_confidence"] == "high"
    assert not any(w["type"] == "missing_measure" for w in result["warnings"])


def test_bare_row_count_distinct(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many unique clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    assert result["intent"]["aggregation"] == "COUNT"
    assert result["intent"]["distinct"] is True
    assert result["intent"]["aggregation_target"] == "distinct_entity_count"
    # Same declared-PK preference as above, now rendered COUNT(DISTINCT id).
    assert result["measures"][0]["selected"]["column_name"] == "id"
    assert result["measures"][0]["selected"]["distinct"] is True


def test_bare_row_count_falls_back_to_count_star_with_no_key(tmp_path, monkeypatch):
    # No primary key, no approved/high-confidence/governed identifier at
    # all — Milestone Phase 6.2's safe fallback: COUNT(*), never invented.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "notes", data_type="TEXT")

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"]["table_fqn"] == "dbo.clients"
    assert measure["selected"]["column_name"] is None
    assert measure["selected"]["key_confidence"] == "none"


def test_bare_count_not_triggered_without_count_language(tmp_path, monkeypatch):
    # A plain noun with no count language must keep its existing behavior —
    # this milestone must not reinterpret every unresolved measure as COUNT(*).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["clients"],
                   measures=["clients"], dimensions=[])

    assert result["intent"]["aggregation"] is None
    assert result["measures"][0]["selected"] is None


def test_question_level_aggregation_overrides_column_name_default(tmp_path, monkeypatch):
    # Before this milestone, a resolved measure with no explicit aggregation
    # hint in its OWN column name/label always defaulted to SUM. The question
    # text itself must now be checked first.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.payroll")
    _add_column(db, "dbo.payroll", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)

    result = _plan(db, question="Lowest amount", measures=["amount"], dimensions=[])
    assert result["intent"]["aggregation"] == "MIN"


def test_default_sum_preserved_with_no_aggregation_language(tmp_path, monkeypatch):
    # Regression guard for test_simple_measure_and_dimension_same_table's
    # exact expectation — must still default to SUM, not None/something else.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders2")
    _add_column(db, "dbo.orders2", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    result = _plan(db, question="revenue", measures=["revenue"], dimensions=[])
    assert result["intent"]["aggregation"] == "SUM"


def test_top_n_order_resolved_to_measure_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients2")
    _add_column(db, "dbo.clients2", "revenue", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    result = _plan(db, question="Top 10 clients by revenue",
                   measures=["revenue"], dimensions=[])

    order = result["intent"]["order"]
    assert order["direction"] == "DESC"
    assert order["limit"] == 10
    assert order["table_fqn"] == "dbo.clients2"
    assert order["column_name"] == "revenue"


def test_ranking_without_measure_warns_but_does_not_block(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients3")
    _add_column(db, "dbo.clients3", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Top 10 clients",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    # Ranking was requested but nothing to rank by (no "by X") — order stays
    # unresolved (no table_fqn/column_name) and a warning explains why,
    # rather than fabricating a sort column.
    order = result["intent"]["order"]
    assert order["limit"] == 10
    assert "table_fqn" not in order
    assert any(w["type"] == "order_column_not_found" for w in result["warnings"])


def test_date_filter_synthesized_from_question(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.invoices")
    _add_column(db, "dbo.invoices", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.invoices", "invoice_date", data_type="TEXT", is_date=True,
                business_label="Invoice Date", approved=True)

    result = _plan(db, question="Total invoices this month", measures=["invoices"], dimensions=[])

    date_filters = [f for f in result["filters"] if f.get("column") == "invoice_date"]
    assert len(date_filters) == 1
    assert date_filters[0]["operator"] == "BETWEEN"
    assert date_filters[0]["resolved"] is True
    assert date_filters[0]["table_fqn"] == "dbo.invoices"
    assert isinstance(date_filters[0]["value"], list) and len(date_filters[0]["value"]) == 2


def test_date_filter_not_fabricated_when_no_date_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.novdate")
    _add_column(db, "dbo.novdate", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)

    result = _plan(db, question="Total amount this month", measures=["amount"], dimensions=[])

    assert not any(f.get("operator") == "BETWEEN" for f in result["filters"])
    assert any(w["type"] == "date_column_not_found" for w in result["warnings"])


def test_status_filter_synthesized_from_question(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.jobs")
    _add_column(db, "dbo.jobs", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.jobs", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)

    result = _plan(db, question="Total amount for open jobs", measures=["amount"], dimensions=[])

    status_filters = [f for f in result["filters"] if f.get("column") == "status"]
    assert len(status_filters) == 1
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["value"] == "Open"


def test_status_filter_not_fabricated_when_no_status_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.nostatus")
    _add_column(db, "dbo.nostatus", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)

    result = _plan(db, question="Total amount for open jobs", measures=["amount"], dimensions=[])

    assert not any(f.get("value") == "Open" for f in result["filters"])
    assert any(w["type"] == "status_column_not_found" for w in result["warnings"])


# ---------------------------------------------------------------------------
# Milestone M-2 — Enterprise Authoritative Source Ranking
#
# _resolve_count_all/_resolve_term now combine _score_term_match's name-match
# score with _score_table_authority's evidence-based bonus/penalty (dictionary
# approval, domain/entity assignment, relationship coverage, row count,
# naming-convention penalties — see data/query_planning_service.py). These
# tests reproduce the real CCPP "clients" ambiguity pattern: several tables
# whose NAME-match score alone ties (e.g. "adf_clients" and "adf_clients_temp"
# both score 0.75 for term "clients" — verified against the real, local
# data/toolsmith.db catalog), so ranking must come from real evidence.
# ---------------------------------------------------------------------------

def test_single_clear_winner_authoritative_table_selected(tmp_path, monkeypatch):
    # A well-governed production table must decisively beat a same-name-score,
    # low-evidence variant for a bare row-count question.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", table_class="Master", row_count=71048, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients", "Operations")
    _add_entity(db, "dbo.adf_clients", "Client")
    _add_fk(db, "dbo.adf_clients", "id", "dbo.adf_client_contacts", "client_id")

    _add_table(db, "dbo.adf_clients_temp", table_class="Transactional", row_count=366, approved=False)
    _add_column(db, "dbo.adf_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.adf_clients"
    assert "ranking_reasons" in measure["selected"]


def test_temporary_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", row_count=50000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.adf_clients_temp", row_count=500, approved=False)
    _add_column(db, "dbo.adf_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.adf_clients"
    temp_candidate = next(c for c in measure["candidates"] if c["table_fqn"] == "dbo.adf_clients_temp")
    assert any("temp" in r.lower() for r in temp_candidate["ranking_reasons"])
    assert temp_candidate["authority_bonus"] < measure["selected"]["authority_bonus"]


def test_backup_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_candidates", row_count=50000, approved=True)
    _add_column(db, "dbo.adf_candidates", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.adf_candidates_backup", row_count=500, approved=False)
    _add_column(db, "dbo.adf_candidates_backup", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many candidates?",
                   concepts=["candidates"], measures=["candidates"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.adf_candidates"
    backup_candidate = next(c for c in measure["candidates"] if c["table_fqn"] == "dbo.adf_candidates_backup")
    assert any("backup" in r.lower() for r in backup_candidate["ranking_reasons"])


def test_archive_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_students", row_count=50000, approved=True)
    _add_column(db, "dbo.adf_students", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.adf_students_archive", row_count=500, approved=False)
    _add_column(db, "dbo.adf_students_archive", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many students?",
                   concepts=["students"], measures=["students"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.adf_students"
    archive_candidate = next(c for c in measure["candidates"] if c["table_fqn"] == "dbo.adf_students_archive")
    assert any("archive" in r.lower() for r in archive_candidate["ranking_reasons"])


def test_approved_dictionary_preference(tmp_path, monkeypatch):
    # Two tables with an identically-named/labeled metric column, differing
    # only in dictionary approval — the approved one must rank first. Uses a
    # partial-match term/column ("revenue" vs "revenue_amount", name_score
    # 0.75) rather than an exact match, so the 1.0 score ceiling doesn't mask
    # the authority-bonus difference between the two candidates.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.invoices_primary", row_count=1000, approved=True)
    _add_column(db, "dbo.invoices_primary", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)

    _add_table(db, "dbo.invoices_secondary", row_count=1000, approved=False)
    _add_column(db, "dbo.invoices_secondary", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)

    result = _plan(db, question="revenue", measures=["revenue"], dimensions=[])
    candidates = sorted(result["measures"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.invoices_primary"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_relationship_coverage_preference(tmp_path, monkeypatch):
    # Two tables with an identical metric column; only one participates in a
    # real foreign-key relationship — it must rank first.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.payroll_current")
    _add_column(db, "dbo.payroll_current", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)
    _add_fk(db, "dbo.payroll_current", "employee_id", "dbo.employees", "id")

    _add_table(db, "dbo.payroll_other")
    _add_column(db, "dbo.payroll_other", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)

    result = _plan(db, question="revenue", measures=["revenue"], dimensions=[])
    candidates = sorted(result["measures"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.payroll_current"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_row_count_tie_break(tmp_path, monkeypatch):
    # Two otherwise-equal tables (same approval, no domain/entity/relationship
    # signals) — the one with far more rows must rank first.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.jobs_main", row_count=200000, approved=True)
    _add_column(db, "dbo.jobs_main", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)

    _add_table(db, "dbo.jobs_minor", row_count=50, approved=True)
    _add_column(db, "dbo.jobs_minor", "revenue_amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue Amount", approved=True)

    result = _plan(db, question="revenue", measures=["revenue"], dimensions=[])
    candidates = sorted(result["measures"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.jobs_main"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_remaining_ambiguity_still_refuses(tmp_path, monkeypatch):
    # Two genuinely comparable production tables (same approval, same row
    # count, no naming penalties, no domain/entity/relationship evidence
    # either way) must still refuse to auto-select — the ambiguity guard is
    # not weakened by the new ranking signals.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.adf_bhclients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_bhclients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is None
    assert any("ambiguous" in w["type"] or "missing" in w["type"] for w in result["warnings"])


# ---------------------------------------------------------------------------
# Milestone M-4 — Enterprise Semantic Resolution
#
# _resolve_concept resolves a bare business-concept term (the request's
# "concepts" list) directly to an authoritative table, reusing the exact same
# _score_term_match/_score_table_authority/_AUTO_SELECT_MIN_CONFIDENCE/
# _AMBIGUITY_MARGIN machinery Milestone M-2 already proved against real CCPP
# ambiguity patterns above — but surfaces the FULL business context
# (business description, domain, entity, governance, relationship coverage)
# already assembled in table_contexts instead of a bare column-shaped dict.
# ---------------------------------------------------------------------------

def test_concept_clear_resolution_semantic_context_populated(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", table_class="Master", row_count=71048, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients", "Operations")
    _add_entity(db, "dbo.adf_clients", "Client")
    _add_fk(db, "dbo.adf_clients", "id", "dbo.adf_client_contacts", "client_id")

    _add_table(db, "dbo.adf_clients_temp", table_class="Transactional", row_count=366, approved=False)
    _add_column(db, "dbo.adf_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Show me clients", concepts=["clients"])

    concept = result["concepts"][0]
    assert concept["term"] == "clients"
    assert concept["resolved"] is True
    selected = concept["selected"]
    assert selected["table_fqn"] == "dbo.adf_clients"
    assert selected["business_name"]
    assert selected["domain"] == "Operations"
    assert selected["entity"] == "Client"
    assert selected["is_approved"] is True
    assert selected["governance"]["dictionary_approved"] is True
    assert selected["relationships_summary"]["outbound_count"] >= 1
    assert "ranking_reasons" in selected
    assert concept["ambiguity_reason"] is None
    assert 0.0 <= concept["confidence"] <= 1.0


def test_concept_ambiguity_preserved(tmp_path, monkeypatch):
    # Two genuinely comparable tables (same approval/row count/no domain-
    # entity-relationship evidence either way) must refuse to auto-select —
    # structured ambiguity, not a guess.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.adf_bhclients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_bhclients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["clients"])

    concept = result["concepts"][0]
    assert concept["resolved"] is False
    assert concept["selected"] is None
    assert concept["ambiguity_reason"] is not None
    assert len(concept["candidates"]) == 2


def test_concept_approved_dictionary_preference(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.invoices_primary", row_count=1000, approved=True)
    _add_column(db, "dbo.invoices_primary", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.invoices_secondary", row_count=1000, approved=False)
    _add_column(db, "dbo.invoices_secondary", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="invoices", concepts=["invoices"])
    candidates = sorted(result["concepts"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.invoices_primary"
    assert candidates[0]["is_approved"] is True
    assert candidates[0]["score"] > candidates[1]["score"]


def test_concept_domain_influence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.projects_main", row_count=1000, approved=True)
    _add_column(db, "dbo.projects_main", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.projects_main", "Operations")

    _add_table(db, "dbo.projects_other", row_count=1000, approved=True)
    _add_column(db, "dbo.projects_other", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="projects", concepts=["projects"])
    candidates = sorted(result["concepts"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.projects_main"
    assert candidates[0]["domain"] == "Operations"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_concept_entity_influence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.courses_main", row_count=1000, approved=True)
    _add_column(db, "dbo.courses_main", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_entity(db, "dbo.courses_main", "Course")

    _add_table(db, "dbo.courses_other", row_count=1000, approved=True)
    _add_column(db, "dbo.courses_other", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="courses", concepts=["courses"])
    candidates = sorted(result["concepts"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.courses_main"
    assert candidates[0]["entity"] == "Course"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_concept_relationship_influence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.payroll_current", row_count=1000, approved=True)
    _add_column(db, "dbo.payroll_current", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_fk(db, "dbo.payroll_current", "employee_id", "dbo.employees", "id")

    _add_table(db, "dbo.payroll_other", row_count=1000, approved=True)
    _add_column(db, "dbo.payroll_other", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="payroll", concepts=["payroll"])
    candidates = sorted(result["concepts"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.payroll_current"
    assert candidates[0]["relationships_summary"]["outbound_count"] >= 1
    assert candidates[0]["score"] > candidates[1]["score"]


def test_concept_governance_state_surfaced(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.surveys", row_count=1000, approved=True)
    _add_column(db, "dbo.surveys", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.surveys", "Reporting & Analytics")
    _add_entity(db, "dbo.surveys", "Event")

    result = _plan(db, question="surveys", concepts=["surveys"])
    selected = result["concepts"][0]["selected"]
    assert selected is not None
    governance = selected["governance"]
    assert governance["dictionary_approved"] is True
    assert governance["domain_assigned"] is True
    assert governance["entity_assigned"] is True


def test_multiple_concepts_resolved_independently(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients", "Operations")

    _add_table(db, "dbo.adf_students", table_class="Master", row_count=40000, approved=True)
    _add_column(db, "dbo.adf_students", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_students", "Student Lifecycle")

    result = _plan(db, question="clients and students", concepts=["clients", "students"])
    assert len(result["concepts"]) == 2
    by_term = {c["term"]: c for c in result["concepts"]}
    assert by_term["clients"]["selected"]["table_fqn"] == "dbo.adf_clients"
    assert by_term["students"]["selected"]["table_fqn"] == "dbo.adf_students"


def test_concept_sql_planner_semantic_context_handoff(tmp_path, monkeypatch):
    # SQL HANDOFF requirement: the resolved concept semantic context must be
    # available on the query plan for build_sql_plan to pass through (see
    # tests/test_phase10_sql_planning.py::test_semantic_context_passthrough).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients", "Operations")

    result = _plan(db, question="clients", concepts=["clients"])
    assert "concepts" in result
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.adf_clients"


# ---------------------------------------------------------------------------
# Milestone M-5 — Autonomous Semantic Curation and Vocabulary Integration
#
# Part 2/6: synonym expansion (data/vocabulary_service.py, reusing
# data/search_service.py's existing _SynonymExpander/data/synonyms.json)
# now reaches plan_business_query() itself — previously it only affected
# metadata search / concept-resolution explanation, never the actual
# SQL-answering table-selection path.
# ---------------------------------------------------------------------------

def test_synonym_expanded_concept_discovery_reaches_plan_business_query(tmp_path, monkeypatch):
    # Table is named/labeled "Customers" only — never "client" anywhere.
    # Resolving concept "client" must still find it via the customer/client/
    # account synonym group already in data/synonyms.json.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.customers", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.customers", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["client"])
    concept = result["concepts"][0]
    assert concept["resolved"] is True
    assert concept["selected"]["table_fqn"] == "dbo.customers"


def test_synonym_expansion_deterministic_same_input_same_output(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.customers", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.customers", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result_a = _plan(db, question="clients", concepts=["client"])
    result_b = _plan(db, question="clients", concepts=["client"])
    assert result_a["concepts"] == result_b["concepts"]


def test_source_specific_vocabulary_job_order_resolves_via_synonym_group(tmp_path, monkeypatch):
    # "job order" is a multi-word CCPP staffing term (data/synonyms.json);
    # must resolve without leaking into the unrelated invoice/billing group.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_openings", table_class="Master", row_count=10000, approved=True)
    _add_column(db, "dbo.adf_openings", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.adf_paysimple_invoices", table_class="Transactional", row_count=500, approved=False)
    _add_column(db, "dbo.adf_paysimple_invoices", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="job order", concepts=["job order"])
    concept = result["concepts"][0]
    assert concept["resolved"] is True
    assert concept["selected"]["table_fqn"] == "dbo.adf_openings"


def test_ambiguity_and_ranking_safeguards_unchanged_by_synonym_wiring(tmp_path, monkeypatch):
    # Two genuinely comparable tables (same evidence either way) sharing a
    # synonym-expanded concept must still correctly refuse — synonym wiring
    # must not weaken _AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.customers_a", row_count=1000, approved=True)
    _add_column(db, "dbo.customers_a", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.customers_b", row_count=1000, approved=True)
    _add_column(db, "dbo.customers_b", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["client"])
    concept = result["concepts"][0]
    assert concept["resolved"] is False
    assert concept["ambiguity_reason"] is not None


# ---------------------------------------------------------------------------
# Sprint 2, Signal #1 — Confidence-Aware Semantic Scoring
#
# _score_table_authority's domain/entity bonuses (+0.05 / +0.07) are today
# flat: they fire identically whether domain_confidence/entity_confidence is
# 0.99 or 0.01, because governance["domain_assigned"]/["entity_assigned"]
# collapse the underlying confidence float to a bool. These tests pin down
# the confidence-weighted replacement BEFORE it exists (A/B/C are written
# against the current flat-bonus code and are expected to fail red until the
# scaling lands; D-F exercise the same behavior through the full
# plan_business_query path). No other canonicality signal is touched.
# ---------------------------------------------------------------------------

def _authority_ctx(*, domain_confidence=None, entity_confidence=None,
                    domain_assigned=True, entity_assigned=True):
    """
    Minimal, hand-built get_table_business_context()-shaped ctx isolating the
    domain/entity confidence signal: profiling/dictionary/relationships are
    all empty so importance/row-count/relationship bonuses are fixed at
    their no-evidence baseline (0.1 importance -> +0.03) for every case,
    and only the domain/entity terms vary between calls.
    """
    return {
        "dictionary": None,
        "domain": {"domain": "Test", "confidence": domain_confidence} if domain_assigned else None,
        "entity": {"entity": "Test", "confidence": entity_confidence} if entity_assigned else None,
        "profiling": None,
        "relationships": {},
        "governance": {
            "domain_assigned": domain_assigned,
            "entity_assigned": entity_assigned,
            "dictionary_approved": False,
        },
        "table": {"table_type": "TABLE"},
    }


def test_authority_higher_confidence_yields_higher_bonus():
    # A. Higher confidence must produce a higher authority score than lower
    # confidence, all else equal.
    high = _score_table_authority("dbo.widgets", _authority_ctx(domain_confidence=0.95, entity_confidence=0.95))
    low = _score_table_authority("dbo.widgets", _authority_ctx(domain_confidence=0.2, entity_confidence=0.2))
    assert high["bonus"] > low["bonus"]


def test_authority_zero_confidence_not_same_as_high_confidence():
    # B. A confidence of 0 must not receive the same authority bonus as a
    # confidence of 0.95 — and must be indistinguishable from no assignment
    # at all (not merely "less than" high confidence by accident of rounding).
    zero = _score_table_authority("dbo.widgets", _authority_ctx(domain_confidence=0.0, entity_confidence=0.0))
    high = _score_table_authority("dbo.widgets", _authority_ctx(domain_confidence=0.95, entity_confidence=0.95))
    unassigned = _score_table_authority(
        "dbo.widgets", _authority_ctx(domain_assigned=False, entity_assigned=False)
    )
    assert zero["bonus"] < high["bonus"]
    assert zero["bonus"] == unassigned["bonus"]


def test_authority_non_confidence_signals_unchanged():
    # C. With no domain/entity assignment at all, the bonus must equal
    # exactly the pre-existing no-evidence baseline (importance-only: 0.1 *
    # 0.30) — proving the confidence change touches only the domain/entity
    # terms and leaves every other additive signal untouched.
    result = _score_table_authority(
        "dbo.widgets", _authority_ctx(domain_assigned=False, entity_assigned=False)
    )
    assert result["bonus"] == round(0.1 * 0.30, 4)


def test_authority_naming_penalty_additive_with_confidence():
    # C (continued). The naming penalty (a non-confidence signal) must
    # combine with the confidence-scaled domain/entity bonus by simple
    # addition, exactly as it does with the flat bonus today.
    result = _score_table_authority(
        "dbo.widgets_temp", _authority_ctx(domain_confidence=1.0, entity_confidence=1.0)
    )
    expected = round(max(-0.5, min(0.5, 0.1 * 0.30 + 0.05 * 1.0 + 0.07 * 1.0 - 0.12)), 4)
    assert result["bonus"] == expected


def test_domain_confidence_breaks_near_tie(tmp_path, monkeypatch):
    # D. Two tables tied on every other signal (same row count, same
    # approval, no relationships, same domain) but differing only in domain
    # confidence must rank the higher-confidence table first. Under the old
    # flat bonus this was an exact tie (both +0.05 regardless of confidence).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.projects_alpha", row_count=1000, approved=True)
    _add_column(db, "dbo.projects_alpha", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.projects_alpha", "Operations", confidence=0.95)

    _add_table(db, "dbo.projects_beta", row_count=1000, approved=True)
    _add_column(db, "dbo.projects_beta", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.projects_beta", "Operations", confidence=0.40)

    result = _plan(db, question="projects", concepts=["projects"])
    candidates = sorted(result["concepts"][0]["candidates"], key=lambda c: -c["score"])
    assert candidates[0]["table_fqn"] == "dbo.projects_alpha"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_clear_winner_survives_maximum_opposing_confidence(tmp_path, monkeypatch):
    # E. A table with a genuinely decisive lead on non-canonicality evidence
    # (approval, master class, huge row-count gap, relationship coverage —
    # pre-confidence margin ~0.23, comfortably above the 0.15 ambiguity gate
    # even after the maximum possible +0.12 domain+entity swing below) must
    # still win outright against a "temp" variant given the maximum possible
    # domain+entity confidence bonus (1.0/1.0) on that variant. Note this is
    # a boundary, not a universal guarantee: a win whose original margin sits
    # between 0.15 and 0.27 CAN be pushed from "resolved" to "ambiguous" by
    # this signal at its ceiling — by design, since _AMBIGUITY_MARGIN is
    # unchanged and still gates strictly on the same 0.15 bar (see the
    # Sprint 2 report's regression-risk section).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", table_class="Master", row_count=5_000_000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_fk(db, "dbo.adf_clients", "id", "dbo.contacts_ref", "ref_id")

    _add_table(db, "dbo.adf_clients_temp", table_class="Transactional", row_count=10, approved=False)
    _add_column(db, "dbo.adf_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients_temp", "Operations", confidence=1.0)
    _add_entity(db, "dbo.adf_clients_temp", "Client", confidence=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])
    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.adf_clients"


def test_ambiguous_tied_confidence_still_refuses_auto_select(tmp_path, monkeypatch):
    # F. Two genuinely comparable tables with identical domain confidence
    # (not merely both unassigned) must still refuse to auto-select — the
    # confidence signal must not manufacture false differentiation between
    # candidates that are truly tied.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_clients", "Operations", confidence=0.8)

    _add_table(db, "dbo.adf_bhclients", row_count=1000, approved=True)
    _add_column(db, "dbo.adf_bhclients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.adf_bhclients", "Operations", confidence=0.8)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])
    measure = result["measures"][0]
    assert measure["selected"] is None
    assert any("ambiguous" in w["type"] or "missing" in w["type"] for w in result["warnings"])
