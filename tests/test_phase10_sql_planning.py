"""
Tests for Program 3 Phase 4 — SQL Planning & Validation Engine.

Most tests construct a minimal query_plan dict by hand (matching
plan_business_query()'s real output shape) rather than re-running full
discovery — this tests build_sql_plan's transform/validation logic in
isolation, matching "don't duplicate query planning" in the test design
too. A couple of tests chain a real plan_business_query() call into
build_sql_plan() for end-to-end integration coverage, using the same
models.init_db()-on-temp-file pattern as test_phase7/8/9.

Run from the project root:
    venv/Scripts/pytest tests/test_phase10_sql_planning.py -v
"""
import json
import os
import re
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase10-sql-planning-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase10-salt-long-enough-value-12345678")

from data.sql_planning_service import build_sql_plan

_NOW = "2026-06-30T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Hand-built query_plan fixtures (matching plan_business_query's real shape)
# ---------------------------------------------------------------------------

def _measure(term, table_fqn, column_name, selected=True):
    sel = {"table_fqn": table_fqn, "column_name": column_name, "business_label": None,
           "score": 0.9, "is_approved": True, "data_type": "DECIMAL"} if selected else None
    return {"term": term, "selected": sel, "candidates": [sel] if sel else [], "warnings": []}


def _dimension(term, table_fqn, column_name, selected=True):
    sel = {"table_fqn": table_fqn, "column_name": column_name, "business_label": None,
           "score": 0.9, "is_approved": True, "data_type": "TEXT"} if selected else None
    return {"term": term, "selected": sel, "candidates": [sel] if sel else [], "warnings": []}


def _join_step(from_table, from_col, to_table, to_col, *, path_found=True,
                join_type="INNER", cardinality="MANY_TO_ONE", fanout_risk="LOW", confidence=100):
    return {
        "from_table": from_table, "from_column": from_col,
        "to_table": to_table, "to_column": to_col,
        "path_found": path_found, "hops": 1 if path_found else None,
        "join_type": join_type if path_found else None,
        "cardinality": cardinality if path_found else None,
        "fanout_risk": fanout_risk if path_found else None,
        "fanout_explanation": "structural", "join_quality": 80 if path_found else None,
        "join_quality_tier": "HIGH" if path_found else None,
        "relationship_strength": "STRONG" if path_found else None,
        "confidence": confidence,
    }


def _base_plan(*, measures=None, dimensions=None, columns=None, join_plan=None,
                filters=None, aggregation="SUM"):
    return {
        "intent": {"raw_question": "test", "type": "aggregate_by_dimension", "aggregation": aggregation},
        "tables": list((columns or {}).keys()),
        "columns": columns or {},
        "measures": measures or [],
        "dimensions": dimensions or [],
        "filters": filters or [],
        "join_plan": join_plan or {
            "required": False, "tables": [], "primary_table": None,
            "steps": [], "fanout_risk": None, "confidence": 100,
        },
        "warnings": [],
        "confidence": 80,
        "explanation": "test plan",
    }


# ---------------------------------------------------------------------------
# 1. Simple single-table plan
# ---------------------------------------------------------------------------

def test_simple_single_table_plan():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert plan["from"]["table_fqn"] == "dbo.orders"
    assert plan["joins"] == []
    assert len(plan["select"]) == 1
    assert plan["select"][0]["aggregation"] == "SUM"


# ---------------------------------------------------------------------------
# 2. Measure + dimension -> GROUP BY
# ---------------------------------------------------------------------------

def test_measure_and_dimension_group_by():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        dimensions=[_dimension("status", "dbo.orders", "status")],
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert len(plan["select"]) == 2
    assert plan["group_by"] == [{"table_fqn": "dbo.orders", "column_name": "status"}]
    # measure must not appear in group_by
    assert not any(g["column_name"] == "amount" for g in plan["group_by"])


# ---------------------------------------------------------------------------
# 3. Multi-table join plan
# ---------------------------------------------------------------------------

def test_multi_table_join_plan():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        dimensions=[_dimension("customer", "dbo.customers", "name")],
        columns={"dbo.orders": ["amount", "customer_id"], "dbo.customers": ["id", "name"]},
        join_plan={
            "required": True, "tables": ["dbo.orders", "dbo.customers"], "primary_table": "dbo.orders",
            "steps": [_join_step("dbo.orders", "customer_id", "dbo.customers", "id")],
            "fanout_risk": "LOW", "confidence": 100,
        },
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert len(plan["joins"]) == 1
    j = plan["joins"][0]
    assert j["left_table"] == "dbo.orders" and j["left_column"] == "customer_id"
    assert j["right_table"] == "dbo.customers" and j["right_column"] == "id"
    assert j["join_type"] == "INNER"
    assert j["cardinality"] == "MANY_TO_ONE"


# ---------------------------------------------------------------------------
# 4. Invalid raw filter rejected (bad operator, and injection-shaped value)
# ---------------------------------------------------------------------------

def test_invalid_filter_bad_operator_rejected():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        filters=[{"column": "status", "operator": "DROP TABLE", "value": "x",
                  "resolved": True, "table_fqn": "dbo.orders"}],
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["where"] == []
    assert any("operator" in r for r in plan["validation"]["blocking_reasons"])


def test_invalid_filter_injection_value_rejected():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        filters=[{"column": "status", "operator": "=", "value": "x; DROP TABLE users; --",
                  "resolved": True, "table_fqn": "dbo.orders"}],
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["where"] == []
    assert any("raw SQL" in r for r in plan["validation"]["blocking_reasons"])


def test_valid_filter_accepted():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        filters=[{"column": "status", "operator": "=", "value": "active",
                  "resolved": True, "table_fqn": "dbo.orders"}],
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert len(plan["where"]) == 1
    assert plan["where"][0]["operator"] == "="


# ---------------------------------------------------------------------------
# 5. Untrusted/missing join blocked
# ---------------------------------------------------------------------------

def test_untrusted_join_blocked():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        dimensions=[_dimension("region", "dbo.regions", "name")],
        columns={"dbo.orders": ["amount"], "dbo.regions": ["name"]},
        join_plan={
            "required": True, "tables": ["dbo.orders", "dbo.regions"], "primary_table": "dbo.orders",
            "steps": [_join_step("dbo.orders", None, "dbo.regions", None, path_found=False)],
            "fanout_risk": None, "confidence": 0,
        },
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["joins"] == []
    assert any("trusted" in r for r in plan["validation"]["blocking_reasons"])


# ---------------------------------------------------------------------------
# 6. PII warning (and allow_unconfirmed_pii override) — integration test,
# needs a real DB since PII state comes from get_column_business_context.
# ---------------------------------------------------------------------------

def _db_conn(path):
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _setup_pii_db(tmp_path, monkeypatch):
    import data.models as models
    db_path = str(tmp_path / "phase10.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in ("data.business_knowledge_service",):
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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)", (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.customers','email','TEXT',1,0,?,?)", (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, pii_risk, "
        " is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.customers','email','Email',1,1,'rule_based',?,?)", (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def test_pii_warning_blocks_by_default(tmp_path, monkeypatch):
    _setup_pii_db(tmp_path, monkeypatch)
    qp = _base_plan(
        dimensions=[_dimension("email", "dbo.customers", "email")],
        columns={"dbo.customers": ["email"]},
        join_plan={"required": False, "tables": ["dbo.customers"], "primary_table": "dbo.customers",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        aggregation=None,
    )
    plan = build_sql_plan(1, "u1", qp)
    pii_warnings = [w for w in plan["warnings"] if w["type"] == "pii_involved"]
    assert pii_warnings and pii_warnings[0]["severity"] == "HIGH"
    assert plan["validation"]["valid"] is False
    assert any("PII" in r for r in plan["validation"]["blocking_reasons"])


def test_pii_allowed_when_explicitly_overridden(tmp_path, monkeypatch):
    _setup_pii_db(tmp_path, monkeypatch)
    qp = _base_plan(
        dimensions=[_dimension("email", "dbo.customers", "email")],
        columns={"dbo.customers": ["email"]},
        join_plan={"required": False, "tables": ["dbo.customers"], "primary_table": "dbo.customers",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        aggregation=None,
    )
    plan = build_sql_plan(1, "u1", qp, allow_unconfirmed_pii=True)
    pii_warnings = [w for w in plan["warnings"] if w["type"] == "pii_involved"]
    assert pii_warnings  # warning still present
    assert plan["validation"]["valid"] is True  # but no longer blocks


# ---------------------------------------------------------------------------
# 7. Unresolved ambiguity blocks plan
# ---------------------------------------------------------------------------

def test_unresolved_ambiguity_blocks_plan():
    qp = _base_plan(
        measures=[_measure("profit", "dbo.orders", "x", selected=False)],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["select"] == []
    assert any("Unresolved" in r for r in plan["validation"]["blocking_reasons"])


def test_none_query_plan_blocks():
    plan = build_sql_plan(1, "u1", None)
    assert plan["validation"]["valid"] is False
    assert plan["select"] == []


# ---------------------------------------------------------------------------
# 8. No SQL execution — structural module check
# ---------------------------------------------------------------------------

def test_no_sql_execution_helpers_imported():
    import data.sql_planning_service as mod
    import inspect
    source = inspect.getsource(mod)
    # No sqlite3/connection-opening primitives anywhere in this module.
    assert "get_connection()" not in source
    assert "import sqlite3" not in source
    assert "conn.execute" not in source
    assert ".executemany(" not in source


def test_no_sql_string_in_response():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        dimensions=[_dimension("status", "dbo.orders", "status")],
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    text = json.dumps(plan)
    assert not re.search(r"\bSELECT\s+\w", text, re.IGNORECASE)
    assert not re.search(r"\bINSERT\s+INTO\b", text, re.IGNORECASE)
    assert not re.search(r"\bFROM\s+\w+\s+(WHERE|JOIN)\b", text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 9. No SELECT * — empty/unresolved select always blocks
# ---------------------------------------------------------------------------

def test_no_select_star_empty_select_blocks():
    qp = _base_plan(
        measures=[], dimensions=[],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["validation"]["checks"]["select_not_empty"] is False
    assert plan["select"] == []


# ---------------------------------------------------------------------------
# Columns not in query_plan["columns"] are rejected (defense against
# inventing a column that was never discovered)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Milestone M-1 — Enterprise Question Intelligence
# ---------------------------------------------------------------------------

def _count_all_measure(term, table_fqn):
    sel = {"table_fqn": table_fqn, "column_name": None, "business_label": None,
           "score": 0.9, "is_approved": True, "data_type": None}
    return {"term": term, "selected": sel, "candidates": [sel], "warnings": []}


def test_count_star_select_no_column_reference():
    qp = _base_plan(
        measures=[_count_all_measure("clients", "dbo.clients")],
        columns={"dbo.clients": ["id"]},
        join_plan={"required": False, "tables": ["dbo.clients"], "primary_table": "dbo.clients",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        aggregation="COUNT",
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert plan["select"][0]["column_name"] is None
    assert plan["select"][0]["aggregation"] == "COUNT"
    # column-existence check must not reject the synthesized COUNT(*) row
    assert plan["validation"]["checks"]["all_columns_exist"] is True


def test_query_plan_intent_dict_missing_gracefully_defaults():
    # A query_plan built without the newer intent keys (distinct/order) —
    # e.g. an older caller — must still produce a valid plan with safe defaults.
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["order_by"] == []
    assert plan["distinct"] is False
    assert plan["limits"]["row_limit"] == 1000


def test_order_by_built_from_resolved_intent():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    qp["intent"]["order"] = {"direction": "DESC", "limit": 10, "table_fqn": "dbo.orders", "column_name": "amount"}
    plan = build_sql_plan(1, "u1", qp)
    assert plan["order_by"] == [{"table_fqn": "dbo.orders", "column_name": "amount", "direction": "DESC"}]
    # requested Top-10 tightens the safety cap, never loosens it
    assert plan["limits"]["row_limit"] == 10


def test_order_by_requested_limit_never_exceeds_safety_cap():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    qp["intent"]["order"] = {"direction": "DESC", "limit": 5000, "table_fqn": "dbo.orders", "column_name": "amount"}
    plan = build_sql_plan(1, "u1", qp)
    assert plan["limits"]["row_limit"] == 1000


def test_order_by_unresolvable_column_drops_with_warning_not_block():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    qp["intent"]["order"] = {"direction": "DESC", "limit": 10}  # no table_fqn/column_name resolved
    plan = build_sql_plan(1, "u1", qp)
    assert plan["order_by"] == []
    assert plan["validation"]["valid"] is True  # must not block the plan
    assert any(w["type"] == "order_column_not_resolved" for w in plan["warnings"])


def test_query_level_distinct_only_when_no_aggregation():
    qp = _base_plan(
        dimensions=[_dimension("status", "dbo.orders", "status")],
        columns={"dbo.orders": ["status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        aggregation=None,
    )
    qp["intent"]["distinct"] = True
    plan = build_sql_plan(1, "u1", qp)
    assert plan["distinct"] is True


def test_distinct_not_applied_at_query_level_when_aggregated():
    # aggregation is present -> per-row COUNT(DISTINCT col) handles it
    # instead (see sql_generation tests) -- query-level DISTINCT must not
    # ALSO be set, which would be redundant/invalid SQL.
    qp = _base_plan(
        measures=[_measure("clients", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        aggregation="COUNT",
    )
    qp["intent"]["distinct"] = True
    plan = build_sql_plan(1, "u1", qp)
    assert plan["distinct"] is False
    assert plan["select"][0]["distinct"] is True


def test_column_not_in_query_plan_rejected():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "ghost_column")],
        columns={"dbo.orders": ["amount"]},  # ghost_column NOT listed
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert any("ghost_column" in r for r in plan["validation"]["blocking_reasons"])


# ---------------------------------------------------------------------------
# Integration — chain real plan_business_query() output into build_sql_plan()
# ---------------------------------------------------------------------------

def test_end_to_end_real_query_plan(tmp_path, monkeypatch):
    import data.models as models
    db_path = str(tmp_path / "phase10_e2e.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in (
        "data.query_planning_service", "data.knowledge_graph_service",
        "data.business_knowledge_service", "data.semantic_layer_service",
    ):
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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',1,'{}',?,?)", (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(id, source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.orders','orders','dbo','TABLE','Orders',1,'rule_based',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.orders','orders','dbo','Transactional','COMPLETE',100,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " uniqueness_score, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.orders','amount','DECIMAL',0.8,?,?)", (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(id, source_id, snapshot_id, table_fqn, column_name, business_label, is_metric, "
        " is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.orders','amount','Revenue',1,1,'rule_based',?,?)", (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    from data.query_planning_service import plan_business_query
    query_plan = plan_business_query(1, "u1", {"question": "revenue", "measures": ["revenue"]})
    plan = build_sql_plan(1, "u1", query_plan)

    assert plan["validation"]["valid"] is True
    assert plan["from"]["table_fqn"] == "dbo.orders"
    assert plan["select"][0]["column_name"] == "amount"


# ---------------------------------------------------------------------------
# Milestone M-4 — Enterprise Semantic Resolution — SQL handoff
#
# build_sql_plan must carry plan_business_query()'s resolved "concepts"
# semantic context through to its own output, so the SQL Planner's response
# reflects resolved business concepts rather than only isolated table
# candidates. Pure passthrough — must not affect select/joins/where/validation.
# ---------------------------------------------------------------------------

def test_semantic_context_passthrough():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
    )
    qp["concepts"] = [{
        "term": "orders", "resolved": True,
        "selected": {"table_fqn": "dbo.orders", "business_name": "Orders"},
        "candidates": [], "confidence": 0.9, "ambiguity_reason": None,
    }]

    plan = build_sql_plan(1, "u1", qp)

    assert plan["semantic_context"] == qp["concepts"]
    assert plan["validation"]["valid"] is True


def test_semantic_context_defaults_to_empty_list_when_absent():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"]},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["semantic_context"] == []


def test_semantic_context_present_on_none_query_plan():
    plan = build_sql_plan(1, "u1", None)
    assert plan["semantic_context"] == []
    assert plan["validation"]["valid"] is False
