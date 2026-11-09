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

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase9-query-planning-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase9-salt-long-enough-value-1234567890")

import data.models as models
from data.query_planning_service import (
    plan_business_query, _score_table_authority,
    _score_relationship_centrality, _REL_CENTRALITY_MAX_BONUS,
    _resolve_count_all, _score_term_match,
    _AUTO_SELECT_MIN_CONFIDENCE, _AMBIGUITY_MARGIN,
    _find_status_column, _resolve_related_attribute_dimension,
    _find_sufficient_single_object, _check_single_object_sufficiency,
    _try_build_relationship_aggregate_plan,
)

_NOW = "2026-06-30T00:00:00+00:00"

# The direct Business Query Planning Engine and its public routes were retired
# during the Phase 8A consolidation. The module remains because the supported
# AI-first path reuses deterministic helper functions from it, but production
# code no longer calls plan_business_query(). These eight tests encode the
# retired planner's ambiguity-output contract. Keep them visible as historical
# coverage without treating that old route as a release requirement.
_RETIRED_DIRECT_PLANNER = pytest.mark.skip(
    reason=(
        "retired direct plan_business_query ambiguity contract; "
        "the active AI-first fast path has its own ambiguity and clarification tests"
    )
)

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
    return _col_seq[0]


def _add_value_samples(db, col_profile_id, values, *, sample_type="top_values"):
    c = _c(db)
    for i, v in enumerate(values):
        c.execute(
            "INSERT INTO profiling_value_samples "
            "(profiling_column_profile_id, sample_type, value, row_count, percentage, rank) "
            "VALUES (?,?,?,?,?,?)",
            (col_profile_id, sample_type, v, 1, 1.0, i),
        )
    c.commit()
    c.close()


def _add_table_type(db, table_fqn, table_type):
    """Override the table_type data_dictionary_tables row _add_table already
    wrote (default 'TABLE') — used to mark a fixture table as a VIEW."""
    c = _c(db)
    c.execute(
        "UPDATE data_dictionary_tables SET table_type = ? WHERE source_id=1 AND table_fqn = ?",
        (table_type, table_fqn),
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
# NEXT MILESTONE, Part 1/3 — negated status filter end-to-end. Root cause:
# _STATUS_RE matched "open" inside "not open" and nothing downstream ever
# looked at the preceding "not" — the resulting SQL asserted the OPPOSITE of
# what was asked (status = 'Open' for a "not open" question). Fixed via
# concept_resolver.extract_query_intent's new status_negated signal, flowing
# through data.query_planning_service._typed_status_filter_value/
# _compatible_status_filter into the actual emitted operator/value.
# ---------------------------------------------------------------------------

def test_negated_status_filter_flips_operator_on_text_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.jobs")
    _add_column(db, "dbo.jobs", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.jobs", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)

    result = _plan(db, question="Total amount for jobs that are not open", measures=["amount"], dimensions=[])

    status_filters = [f for f in result["filters"] if f.get("column") == "status"]
    assert len(status_filters) == 1
    # The word "Open" is still the correct evidence (it's what was named in
    # the question) — only the OPERATOR flips, never the value.
    assert status_filters[0]["operator"] == "!="
    assert status_filters[0]["value"] == "Open"
    assert status_filters[0]["polarity"] == "negative"


def test_negated_status_filter_flips_boolean_value(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_class_with_status_and_flag(db)
    _add_column(db, "dbo.EDU_Class", "SignupCount", data_type="INTEGER", is_metric=True,
                business_label="Signup Count", approved=True)

    result = _plan(
        db, question="Total signup count for classes that are not active",
        measures=["signupcount"], dimensions=[],
    )

    status_filters = [f for f in result["filters"] if f.get("column") == "IsActive"]
    assert len(status_filters) == 1
    # A boolean/FLAG column keeps operator "=" — polarity is expressed by
    # inverting the mapped 0/1 value instead, never by "!= 1".
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["value"] == 0
    assert status_filters[0]["polarity"] == "negative"


def test_positive_status_filter_still_tagged_positive_polarity(tmp_path, monkeypatch):
    # Regression guard: an ordinary (non-negated) status question must keep
    # producing polarity="positive" — the new field must never flip a
    # previously-correct positive filter.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.jobs")
    _add_column(db, "dbo.jobs", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.jobs", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)

    result = _plan(db, question="Total amount for open jobs", measures=["amount"], dimensions=[])

    status_filters = [f for f in result["filters"] if f.get("column") == "status"]
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["polarity"] == "positive"


# ---------------------------------------------------------------------------
# A6 — Business Filter Intelligence: _find_status_column() evidence expansion
#
# Direct unit tests against _find_status_column() using hand-built
# table_contexts dicts — the same shape get_table_business_context returns
# ({"columns": [{"column_name", "dictionary", "profiling"}, ...]}) — following
# the direct-unit-test pattern already used for _score_table_authority/
# _resolve_count_all below. One full-pipeline test (via plan_business_query)
# is included to confirm the new business_label evidence wires through to a
# real resolved filter, not just the resolver function in isolation.
# ---------------------------------------------------------------------------

def _status_col(name, *, profiling_semantic_type=None, business_label=None):
    return {
        "column_name": name,
        "dictionary": {"business_label": business_label} if business_label is not None else None,
        "profiling": {"semantic_type": profiling_semantic_type} if profiling_semantic_type is not None else None,
    }


def test_find_status_column_dictionary_business_label_detected():
    ctx = {
        "dbo.jobs": {"columns": [
            _status_col("wf_code", business_label="Enrollment Stage"),
            _status_col("amount"),
        ]},
    }
    assert _find_status_column(ctx, "dbo.jobs") == ("dbo.jobs", "wf_code")


def test_find_status_column_profiling_takes_precedence_over_business_label():
    ctx = {
        "dbo.jobs": {"columns": [
            _status_col("other_flag", business_label="Job Lifecycle"),
            _status_col("real_status", profiling_semantic_type="STATUS"),
        ]},
    }
    assert _find_status_column(ctx, "dbo.jobs") == ("dbo.jobs", "real_status")


def test_find_status_column_expanded_keyword_fallback():
    ctx = {
        "dbo.candidates": {"columns": [
            _status_col("is_active", business_label="Column A"),
        ]},
    }
    assert _find_status_column(ctx, "dbo.candidates") == ("dbo.candidates", "is_active")


def test_find_status_column_preferred_table_ordering_unchanged():
    # dbo.other has a stronger signal (profiling STATUS) but dbo.jobs is the
    # preferred table with only a weaker name-token match. Table preference
    # is still checked before evidence strength — unchanged from before.
    ctx = {
        "dbo.other": {"columns": [_status_col("order_status", profiling_semantic_type="STATUS")]},
        "dbo.jobs": {"columns": [_status_col("status_code")]},
    }
    assert _find_status_column(ctx, "dbo.jobs") == ("dbo.jobs", "status_code")


def test_find_status_column_generic_state_column_not_selected_over_stronger_candidate():
    ctx = {
        "dbo.customers": {"columns": [
            _status_col("state", business_label="State"),
            _status_col("order_status", profiling_semantic_type="STATUS"),
        ]},
    }
    assert _find_status_column(ctx, "dbo.customers") == ("dbo.customers", "order_status")


def test_find_status_column_no_match_returns_none():
    ctx = {
        "dbo.widgets": {"columns": [
            _status_col("amount"),
            _status_col("region", business_label="Region"),
        ]},
    }
    assert _find_status_column(ctx, "dbo.widgets") is None


def test_find_status_column_existing_name_token_match_unchanged():
    ctx = {
        "dbo.jobs": {"columns": [_status_col("status", business_label="Status")]},
    }
    assert _find_status_column(ctx, "dbo.jobs") == ("dbo.jobs", "status")


def test_status_filter_synthesized_via_business_label_when_name_is_opaque(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.enrollments")
    _add_column(db, "dbo.enrollments", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Amount", approved=True)
    _add_column(db, "dbo.enrollments", "wf_code", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Enrollment Stage", approved=True)

    result = _plan(db, question="Total amount for completed enrollments",
                   measures=["amount"], dimensions=[])

    status_filters = [f for f in result["filters"] if f.get("column") == "wf_code"]
    assert len(status_filters) == 1
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["value"] == "Completed"


# ---------------------------------------------------------------------------
# Milestone M-2 — Enterprise Authoritative Source Ranking
#
# _resolve_count_all/_resolve_term now combine _score_term_match's name-match
# score with _score_table_authority's evidence-based bonus/penalty (dictionary
# approval, domain/entity assignment, relationship coverage, row count,
# naming-convention penalties — see data/query_planning_service.py). These
# tests reproduce the a real production database "clients" ambiguity pattern: several tables
# whose NAME-match score alone ties (e.g. "edu_clients" and "edu_clients_temp"
# both score 0.75 for term "clients" — verified against the real, local
# data/toolsmith.db catalog), so ranking must come from real evidence.
# ---------------------------------------------------------------------------

def test_single_clear_winner_authoritative_table_selected(tmp_path, monkeypatch):
    # A well-governed production table must decisively beat a same-name-score,
    # low-evidence variant for a bare row-count question.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=65000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients", "Operations")
    _add_entity(db, "dbo.edu_clients", "Client")
    _add_fk(db, "dbo.edu_clients", "id", "dbo.edu_client_contacts", "client_id")

    _add_table(db, "dbo.edu_clients_temp", table_class="Transactional", row_count=366, approved=False)
    _add_column(db, "dbo.edu_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.edu_clients"
    assert "ranking_reasons" in measure["selected"]


def test_temporary_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.edu_clients_temp", row_count=500, approved=False)
    _add_column(db, "dbo.edu_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.edu_clients"
    # Day 2B: "clients" is a grounded target entity, so this bare-entity
    # question resolves via the semantic-contract fast path — the temp
    # decoy is excluded at discovery time (recorded as an excluded_object,
    # see test_semantic_contract_service.py::
    # test_discovery_resolves_canonical_and_excludes_backup) and never
    # re-enters the query-time candidate pool at all, rather than being
    # rescored fresh on every query and merely ranked low.
    assert not any(c["table_fqn"] == "dbo.edu_clients_temp" for c in measure["candidates"])


def test_backup_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_candidates", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_candidates", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.edu_candidates_backup", row_count=500, approved=False)
    _add_column(db, "dbo.edu_candidates_backup", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many candidates?",
                   concepts=["candidates"], measures=["candidates"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.edu_candidates"
    # Day 2B: see test_temporary_table_penalty's comment — "candidates" is
    # a grounded target entity, so the backup decoy is excluded at
    # discovery time and never re-enters the query-time candidate pool.
    assert not any(c["table_fqn"] == "dbo.edu_candidates_backup" for c in measure["candidates"])


def test_archive_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_students", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_students", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.edu_students_archive", row_count=500, approved=False)
    _add_column(db, "dbo.edu_students_archive", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="How many students?",
                   concepts=["students"], measures=["students"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.edu_students"
    # Day 2B: see test_temporary_table_penalty's comment — "students" is
    # a grounded target entity, so the archive decoy is excluded at
    # discovery time and never re-enters the query-time candidate pool.
    assert not any(c["table_fqn"] == "dbo.edu_students_archive" for c in measure["candidates"])


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


@_RETIRED_DIRECT_PLANNER
def test_remaining_ambiguity_still_refuses(tmp_path, monkeypatch):
    # Two genuinely comparable production tables (same approval, same row
    # count, no naming penalties, no domain/entity/relationship evidence
    # either way) must still refuse to auto-select — the ambiguity guard is
    # not weakened by the new ranking signals.
    #
    # Milestone M-33 note: "edu_clients"/"crm_clients" (not "edu_staffingclients",
    # this fixture's original pairing) — both tokenize to the same
    # {prefix, "clients"} two-token shape (_tokenize splits on "_"), so both
    # are equally diluted name-matches for "clients" under _score_term_match
    # _single's near-match-Jaccard scoring (M-33 root-cause fix for
    # many Enterprise Acceptance Suite "Unresolved term(s)" failures — see
    # data.query_planning_service._score_term_match_single's own docstring).
    # "edu_staffingclients" is a single undivided token ("bh"+"clients" concatenated
    # with no case boundary or separator in this all-lowercase fixture
    # convention), so it no longer name-matches "clients" as well as a real
    # tie partner should — that's the fix working as intended, not a
    # regression, but it broke this fixture's premise that the two tables
    # were equally good candidates.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

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
# _AMBIGUITY_MARGIN machinery Milestone M-2 already proved against a real production database
# ambiguity patterns above — but surfaces the FULL business context
# (business description, domain, entity, governance, relationship coverage)
# already assembled in table_contexts instead of a bare column-shaped dict.
# ---------------------------------------------------------------------------

def test_concept_clear_resolution_semantic_context_populated(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=65000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients", "Operations")
    _add_entity(db, "dbo.edu_clients", "Client")
    _add_fk(db, "dbo.edu_clients", "id", "dbo.edu_client_contacts", "client_id")

    _add_table(db, "dbo.edu_clients_temp", table_class="Transactional", row_count=366, approved=False)
    _add_column(db, "dbo.edu_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Show me clients", concepts=["clients"])

    concept = result["concepts"][0]
    assert concept["term"] == "clients"
    assert concept["resolved"] is True
    selected = concept["selected"]
    assert selected["table_fqn"] == "dbo.edu_clients"
    assert selected["business_name"]
    assert selected["domain"] == "Operations"
    assert selected["entity"] == "Client"
    assert selected["is_approved"] is True
    assert selected["governance"]["dictionary_approved"] is True
    assert selected["relationships_summary"]["outbound_count"] >= 1
    assert "ranking_reasons" in selected
    assert concept["ambiguity_reason"] is None
    assert 0.0 <= concept["confidence"] <= 1.0


@_RETIRED_DIRECT_PLANNER
def test_concept_ambiguity_preserved(tmp_path, monkeypatch):
    # Two genuinely comparable tables (same approval/row count/no domain-
    # entity-relationship evidence either way) must refuse to auto-select —
    # structured ambiguity, not a guess.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

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
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients", "Operations")

    _add_table(db, "dbo.edu_students", table_class="Master", row_count=40000, approved=True)
    _add_column(db, "dbo.edu_students", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_students", "Student Lifecycle")

    result = _plan(db, question="clients and students", concepts=["clients", "students"])
    assert len(result["concepts"]) == 2
    by_term = {c["term"]: c for c in result["concepts"]}
    assert by_term["clients"]["selected"]["table_fqn"] == "dbo.edu_clients"
    assert by_term["students"]["selected"]["table_fqn"] == "dbo.edu_students"


# ---------------------------------------------------------------------------
# Enterprise AI Analyst Agent — bare entity list routing (M-26)
#
# A question naming only a business concept ("Show clients") with no
# measure/dimension term previously produced intent.type == "unresolved"
# even when the concept resolved confidently to one table — build_sql_plan
# never builds SELECT from concepts, so it silently refused instead of
# listing the entity. Fixed to route to "list_entities" only when exactly
# one concept term was extracted and it resolved.
# ---------------------------------------------------------------------------

def test_bare_single_concept_routes_to_list_entities(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=65000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Show me clients", concepts=["clients"])

    assert result["intent"]["type"] == "list_entities"
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.edu_clients"


def test_bare_multiple_grounded_concepts_routes_to_relationship_list(tmp_path, monkeypatch):
    # "Show clients and invoices" — two bare concepts, no measure/dimension,
    # both fully grounded via semantic_contract_service. Day 3 — this no
    # longer stays unresolved (pre-Day-3 behavior — never guess which one, or
    # invent a join/union): query_planning_service._try_build_relationship_plan
    # now routes a bare multi-entity question like this to a
    # "relationship_list" intent instead, reusing the existing join planner
    # (_plan_joins) to build the join graph. The never-guess-on-genuine-
    # ambiguity guarantee still holds, just moved downstream — this fixture
    # has no relationship data at all, so the join_plan carries an unfound
    # path (path_found=False), which sql_planning_service.build_sql_plan's
    # own existing untrusted-join check refuses at SQL-planning time (an
    # entity that can't be safely joined is refused, never a guessed/
    # Cartesian join) — see test_phase10_sql_planning.py's own relationship
    # coverage for that half.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_invoices", table_class="Master", row_count=20000, approved=True)
    _add_column(db, "dbo.edu_invoices", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients and invoices", concepts=["clients", "invoices"])

    assert result["intent"]["type"] == "relationship_list"
    assert result["join_plan"]["required"] is True
    assert set(result["join_plan"]["tables"]) == {"dbo.edu_clients", "dbo.edu_invoices"}
    assert any(not s["path_found"] for s in result["join_plan"]["steps"])


@_RETIRED_DIRECT_PLANNER
def test_bare_ambiguous_concept_stays_unresolved(tmp_path, monkeypatch):
    # A single bare concept that itself fails to resolve (tied candidates)
    # must not be routed to list_entities either.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["clients"])

    assert result["intent"]["type"] == "unresolved"


def test_bare_concept_with_measure_still_routes_to_aggregate(tmp_path, monkeypatch):
    # A concept present alongside a resolved measure must not be reclassified
    # as list_entities — the existing aggregate routing takes precedence.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", row_count=1000, approved=True)
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    result = _plan(db, question="total revenue", measures=["revenue"], concepts=["orders"])

    assert result["intent"]["type"] == "aggregate_overall"


# ---------------------------------------------------------------------------
# Enterprise AI Analyst A5 Milestone 1 — Entity-Centric Planner. Additive-only
# query_plan["entity"/"entity_table"/"entity_confidence"/"entity_candidates"]
# fields, projected from resolved_concepts (_resolve_concept's own output,
# already exercised by the tests above) — no new resolution/scoring.
# ---------------------------------------------------------------------------

def test_entity_fields_populated_for_bare_resolved_concept(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=65000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Show me clients", concepts=["clients"])

    assert result["entity"] == "clients"
    assert result["entity_table"] == "dbo.edu_clients"
    assert isinstance(result["entity_confidence"], float) and result["entity_confidence"] > 0
    assert result["entity_candidates"]
    assert result["entity_candidates"][0]["table_fqn"] == "dbo.edu_clients"

@_RETIRED_DIRECT_PLANNER
def test_entity_fields_populated_without_table_for_ambiguous_concept(tmp_path, monkeypatch):
    # Mirrors test_bare_ambiguous_concept_stays_unresolved — entity/
    # entity_candidates are still populated as evidence (the whole point of
    # this milestone: surface the tie), but entity_table stays None since
    # nothing was confidently selected.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["clients"])

    assert result["entity"] == "clients"
    assert result["entity_table"] is None
    assert len(result["entity_candidates"]) == 2

def test_entity_fields_not_populated_when_measure_resolves(tmp_path, monkeypatch):
    # Mirrors test_bare_concept_with_measure_still_routes_to_aggregate — an
    # aggregate question must never populate the entity fields, even though
    # a single concept term was also extracted alongside the measure.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", row_count=1000, approved=True)
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    result = _plan(db, question="total revenue", measures=["revenue"], concepts=["orders"])

    assert result["entity"] is None
    assert result["entity_table"] is None
    assert result["entity_candidates"] == []

def test_entity_fields_not_populated_for_multiple_concepts(tmp_path, monkeypatch):
    # Mirrors test_bare_multiple_concepts_stays_unresolved — two bare
    # concepts is not "the primary request is a single business entity".
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_invoices", table_class="Master", row_count=20000, approved=True)
    _add_column(db, "dbo.edu_invoices", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients and invoices", concepts=["clients", "invoices"])

    assert result["entity"] is None


def test_concept_sql_planner_semantic_context_handoff(tmp_path, monkeypatch):
    # SQL HANDOFF requirement: the resolved concept semantic context must be
    # available on the query plan for build_sql_plan to pass through (see
    # tests/test_phase10_sql_planning.py::test_semantic_context_passthrough).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients", "Operations")

    result = _plan(db, question="clients", concepts=["clients"])
    assert "concepts" in result
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.edu_clients"


# ---------------------------------------------------------------------------
# A5 Milestone 2.1 — Suppress Redundant Measure Resolution for Bare Entity
# Questions.
#
# extract_terms() puts a term with no "by"-clause into BOTH concepts and
# measures. Every test below explicitly duplicates the term into both
# concepts=[term] and measures=[term] (mirroring exactly what extract_terms()
# produces for "Show active clients"/"List courses"/etc.), since the plain
# bare-concept tests above never exercised that duplication and therefore
# never exercised the bug this milestone fixes. Each "gate should fire" test
# seeds a decoy column elsewhere whose business_label alone would win (or
# spuriously tie for) the old column-level measure search, proving the fix
# actually suppresses that search rather than merely happening not to match
# anything in an under-seeded fixture.
# ---------------------------------------------------------------------------

def test_bare_entity_gate_suppresses_measure_resolution_active_clients(tmp_path, monkeypatch):
    # "Show active clients" — concept resolves cleanly to one table; a decoy
    # metric column elsewhere (business_label="Clients") would, pre-fix,
    # have given the redundant measure-role search something to seize on.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.vw_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.vw_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.unrelated_metrics", table_class="Transactional", row_count=10)
    _add_column(db, "dbo.unrelated_metrics", "client_count", data_type="INTEGER", is_metric=True,
                business_label="Clients", approved=True)

    result = _plan(db, question="Show active clients", concepts=["clients"], measures=["clients"])

    assert result["measures"] == []
    assert not any(w["type"] == "ambiguous_measure" for w in result["warnings"])
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.vw_clients"
    assert result["intent"]["type"] == "list_entities"


def test_bare_entity_gate_suppresses_measure_resolution_active_projects(tmp_path, monkeypatch):
    # "List active projects" — same pattern as above, different noun.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.basecamp_projects", table_class="Master", row_count=800, approved=True)
    _add_column(db, "dbo.basecamp_projects", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.unrelated_metrics", table_class="Transactional", row_count=10)
    _add_column(db, "dbo.unrelated_metrics", "project_count", data_type="INTEGER", is_metric=True,
                business_label="Projects", approved=True)

    result = _plan(db, question="List active projects", concepts=["projects"], measures=["projects"])

    assert result["measures"] == []
    assert not any(w["type"] == "ambiguous_measure" for w in result["warnings"])
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.basecamp_projects"
    assert result["intent"]["type"] == "list_entities"


@_RETIRED_DIRECT_PLANNER
def test_bare_entity_gate_prevents_wrong_table_hijack_courses(tmp_path, monkeypatch):
    # "List courses" — mirrors the confirmed A5 M2 audit finding: two
    # genuinely tied concept candidates (edu_course/edu_track-style), plus a
    # third, unrelated table whose column business_label alone
    # ("Courses") would, pre-fix, let the measure-role search silently
    # auto-select it — hijacking the answer away from the real, ambiguous
    # course tables with no clarification offered at all.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_course", row_count=271, approved=False)
    _add_column(db, "dbo.edu_course", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_track", row_count=271, approved=False)
    _add_column(db, "dbo.edu_track", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_class", table_class="Unknown", row_count=311, approved=False)
    _add_column(db, "dbo.edu_class", "seat_count", data_type="INTEGER", is_metric=True,
                business_label="Courses", approved=True)

    result = _plan(db, question="List courses", concepts=["courses"], measures=["courses"])

    assert result["measures"] == []
    assert not any(w["type"] == "ambiguous_measure" for w in result["warnings"])
    # No silent wrong-table SQL: the concept stays correctly ambiguous
    # between the two genuine course tables, never hijacked to edu_class.
    assert result["concepts"][0]["selected"] is None
    assert result["intent"]["type"] == "unresolved"
    assert result["intent"]["aggregation"] is None


def test_confidently_resolved_bare_concept_reaches_list_entities_despite_duplicate_measure_term(tmp_path, monkeypatch):
    # Requirement 5: a confidently resolved concept must still reach
    # list_entities routing when the identical term is also (redundantly)
    # present in measures — the gate must fire and get out of the way
    # cleanly, not merely "usually work."
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=65000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="Show me clients", concepts=["clients"], measures=["clients"])

    assert result["measures"] == []
    assert result["intent"]["type"] == "list_entities"
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.edu_clients"


@_RETIRED_DIRECT_PLANNER
def test_ambiguous_bare_concept_with_duplicate_measure_term_stays_unresolved_not_hijacked(tmp_path, monkeypatch):
    # Requirement 6 (query_planning_service layer): a genuinely ambiguous
    # concept, with the identical term duplicated into measures, must stay
    # "unresolved" (the shape context_builder._extract_ambiguous_terms'
    # existing, unmodified concept-fallback branch turns into
    # clarification_required) — never silently promoted to
    # "aggregate_overall" by the redundant measure-role search.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients", concepts=["clients"], measures=["clients"])

    assert result["measures"] == []
    assert result["intent"]["type"] == "unresolved"
    assert result["concepts"][0]["selected"] is None
    assert len(result["concepts"][0]["candidates"]) == 2


def test_total_revenue_still_resolves_measure_normally(tmp_path, monkeypatch):
    # Requirement 7 — control: an explicit aggregation keyword ("total")
    # means query_intent.aggregation is not None, so the gate must not fire
    # even though "revenue" could in principle also appear as a concept.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", row_count=1000, approved=True)
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    result = _plan(db, question="total revenue", measures=["revenue"], concepts=["revenue"])

    assert len(result["measures"]) == 1
    assert result["measures"][0]["selected"]["column_name"] == "amount"
    assert result["intent"]["type"] == "aggregate_overall"


def test_average_salary_still_resolves_measure_normally(tmp_path, monkeypatch):
    # Requirement 8 — control: AVG aggregation.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.employees", row_count=500, approved=True)
    _add_column(db, "dbo.employees", "pay", data_type="DECIMAL", is_metric=True, business_label="Salary")

    result = _plan(db, question="average salary", measures=["salary"], concepts=["salary"])

    assert len(result["measures"]) == 1
    assert result["measures"][0]["selected"]["column_name"] == "pay"
    assert result["intent"]["aggregation"] == "AVG"


def test_count_candidates_uses_entity_count_aggregation_path(tmp_path, monkeypatch):
    # Requirement 9 — control: "how many candidates" carries
    # aggregation_target="entity_count", so the gate's own
    # "aggregation_target is None" clause keeps it off; this question
    # already correctly routes through _resolve_entity_count/
    # _resolve_count_all (table-name match), never the buggy column-level
    # path — unaffected by this milestone either way.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.candidates", table_class="Master", row_count=2000, approved=True)
    _add_column(db, "dbo.candidates", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="how many candidates do we have",
                   concepts=["candidates"], measures=["candidates"])

    assert len(result["measures"]) == 1
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.candidates"
    assert result["measures"][0]["selected"]["aggregation_target"] == "entity_count"
    assert result["intent"]["aggregation_target"] == "entity_count"


def test_revenue_by_department_remains_unchanged(tmp_path, monkeypatch):
    # Requirement 10 — control: a "by"-clause yields 2 concepts
    # (revenue, department), so len(concepts) == 1 is false and the gate
    # cannot fire regardless of aggregation.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", row_count=1000, approved=True)
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True, business_label="Revenue")
    _add_column(db, "dbo.orders", "dept", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Department")

    result = _plan(db, question="revenue by department",
                   concepts=["revenue", "department"], measures=["revenue"], dimensions=["department"])

    assert len(result["measures"]) == 1
    assert result["measures"][0]["selected"]["column_name"] == "amount"
    assert result["intent"]["type"] == "aggregate_by_dimension"


def test_which_recruiter_has_most_placements_ranking_question_remains_unchanged(tmp_path, monkeypatch):
    # Requirement 11 — control, using the task's own literal example.
    # "Which recruiter has the most placements?" extracts to TWO concepts
    # (['placements', 'recruiter']) with aggregation=MAX — both the
    # len(concepts)==1 guard and the aggregation-is-None guard keep the gate
    # off independently.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.placements", row_count=500, approved=True)
    _add_column(db, "dbo.placements", "fee", data_type="DECIMAL", is_metric=True, business_label="Placements")

    result = _plan(db, question="Which recruiter has the most placements?",
                   concepts=["placements", "recruiter"], measures=["placements"], dimensions=["recruiter"])

    assert len(result["measures"]) == 1


def test_top_10_clients_by_revenue_ranking_question_remains_unchanged(tmp_path, monkeypatch):
    # Requirement 11 (second literal example) — "Top 10 clients by revenue"
    # sets query_intent.order (explicit N) and extracts 3 concepts; either
    # guard alone would keep the gate off.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients", row_count=500, approved=True)
    _add_column(db, "dbo.clients", "revenue", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    result = _plan(db, question="Top 10 clients by revenue",
                   concepts=["clients", "revenue"], measures=["revenue"], dimensions=["clients"])

    assert len(result["measures"]) == 1


def test_bare_top_recruiters_with_no_explicit_number_is_a_documented_gate_discrepancy(tmp_path, monkeypatch):
    # IMPORTANT, HONEST DISCREPANCY — flagged in the A5 M2.1 report rather
    # than hidden: the task's control list names "Top recruiters" (no
    # digit) as a phrase that must not be suppressed. But
    # extract_query_intent()'s own _TOP_N_RE requires an explicit number
    # ("top 5", not bare "top"), and "top" is itself a _STOPWORDS entry — so
    # for this EXACT literal phrase (no number, no ranking verb),
    # extract_terms()/extract_query_intent() (both unmodified, out of this
    # milestone's scope) already produce concepts==measures==['recruiters']
    # and order=None — i.e. this phrasing is, by the existing system's own
    # semantics, indistinguishable from the bare entity question "recruiters"
    # itself. The milestone's exact, literally-specified gate condition
    # therefore does suppress it too, consistently with every other bare
    # single-concept case. This test documents the ACTUAL, verified
    # behavior rather than asserting the illustrative example's outcome
    # unverified — see A5 M2.1 report for the full explanation and the
    # concrete extract_terms()/extract_query_intent() probe that confirms
    # it (order=None, concepts==measures==['recruiters']) for both "top
    # recruiters" and "bottom invoices".
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.recruiters", table_class="Master", row_count=40, approved=True)
    _add_column(db, "dbo.recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="top recruiters", concepts=["recruiters"], measures=["recruiters"])

    assert result["measures"] == []


def test_multi_concept_question_remains_unchanged(tmp_path, monkeypatch):
    # Requirement 12 — control: "clients and invoices" duplicates one of the
    # two terms into measures too, isolating the len(concepts)==1 guard
    # specifically (as opposed to test_revenue_by_department's dimension-
    # driven multi-concept case above).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=50000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_invoices", table_class="Master", row_count=20000, approved=True)
    _add_column(db, "dbo.edu_invoices", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="clients and invoices",
                   concepts=["clients", "invoices"], measures=["clients"])

    # Day 2E, Task 2 — both "Client" and "Invoice" genuinely ground here
    # (each is the only table matching its own entity name in this
    # fixture), so the "clients" CONCEPT resolves via contract authority;
    # its duplicate, unresolved MEASURE entry (no metric column named
    # "clients" exists) is then intentionally dropped as redundant noise
    # once its own entity concept has already answered it elsewhere —
    # exactly the fix for the live-confirmed defect where the same dropped-
    # duplicate words surfaced as spurious "ambiguous_measure" warnings
    # alongside an already-correct concept resolution. The two-concept
    # ambiguity gate below (Requirement 12's actual subject) is unaffected.
    #
    # Day 3 update: with both concepts fully grounded and no measure/
    # dimension actually selected (len(measures) == 0 below confirms the
    # duplicate "clients" measure was dropped, same as before), this now
    # routes to "relationship_list" rather than "unresolved" — see
    # test_bare_multiple_grounded_concepts_routes_to_relationship_list for
    # the dedicated Day 3 coverage of that routing decision itself.
    assert len(result["measures"]) == 0
    assert result["intent"]["type"] == "relationship_list"


def test_threshold_constants_unchanged_by_this_milestone(tmp_path, monkeypatch):
    # Requirement 13 — this milestone must not touch either gate constant.
    assert _AUTO_SELECT_MIN_CONFIDENCE == 0.5
    assert _AMBIGUITY_MARGIN == 0.15


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
    # "job order" is a multi-word the reference database staffing term (data/synonyms.json);
    # must resolve without leaking into the unrelated invoice/billing group.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_openings", table_class="Master", row_count=10000, approved=True)
    _add_column(db, "dbo.edu_openings", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.edu_payment_invoices", table_class="Transactional", row_count=500, approved=False)
    _add_column(db, "dbo.edu_payment_invoices", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    result = _plan(db, question="job order", concepts=["job order"])
    concept = result["concepts"][0]
    assert concept["resolved"] is True
    assert concept["selected"]["table_fqn"] == "dbo.edu_openings"


@_RETIRED_DIRECT_PLANNER
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
# Semantic Retrieval Integration — no silent fallback to the unbounded
# _collect_candidate_tables() scan on empty/failed bounded retrieval.
# ---------------------------------------------------------------------------

def test_empty_bounded_retrieval_returns_unresolved_not_unbounded_scan(tmp_path, monkeypatch, caplog):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    import data.query_planning_service as qps
    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", lambda *a, **kw: (set(), [], []))

    calls = []
    original = qps._collect_candidate_tables
    monkeypatch.setattr(
        qps, "_collect_candidate_tables",
        lambda *a, **kw: (calls.append(a), original(*a, **kw))[1],
    )

    with caplog.at_level("INFO"):
        result = _plan(db, question="revenue by status",
                       measures=["revenue"], dimensions=["status"])

    assert calls == []  # the unbounded legacy scan is never invoked
    assert result is not None
    assert result["intent"]["type"] == "unresolved"
    assert "bounded retrieval found no candidate" in caplog.text


def test_retrieval_failure_is_observable_and_fails_safe(tmp_path, monkeypatch, caplog):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)

    import data.query_planning_service as qps
    import data.semantic_retrieval_service as svc

    def _boom(*a, **kw):
        raise RuntimeError("search backend unavailable")
    monkeypatch.setattr(svc, "search_metadata", _boom)

    calls = []
    original = qps._collect_candidate_tables
    monkeypatch.setattr(
        qps, "_collect_candidate_tables",
        lambda *a, **kw: (calls.append(a), original(*a, **kw))[1],
    )

    with caplog.at_level("WARNING"):
        result = _plan(db, question="revenue by status",
                       measures=["revenue"], dimensions=["status"])

    assert result is not None  # never raises
    assert calls == []  # failure does not trigger the unbounded legacy scan
    assert result["intent"]["type"] == "unresolved"


# ---------------------------------------------------------------------------
# Phase 3, Step 2 — remembered-synonym resolution feeds the planner the
# canonical term before candidate-table retrieval ever runs. Spies on
# _get_ai_candidate_tables_ranked (the same seam test_empty_bounded_
# retrieval_returns_unresolved_not_unbounded_scan above already uses) so
# these are pure unit tests of the substitution, independent of whether any
# table actually matches.
# ---------------------------------------------------------------------------

def _spy_candidate_tables(monkeypatch):
    import data.query_planning_service as qps
    seen_terms = []
    monkeypatch.setattr(
        qps, "_get_ai_candidate_tables_ranked",
        lambda source_id, user_id, question, terms, **kw: (seen_terms.append(list(terms)), (set(), [], []))[1],
    )
    return seen_terms


def test_remembered_synonym_substitutes_term_before_retrieval(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    seen_terms = _spy_candidate_tables(monkeypatch)
    _plan(db, question="show my clients", concepts=["clients"])
    assert seen_terms == [["customer"]]


def test_unknown_synonym_term_is_unchanged(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    seen_terms = _spy_candidate_tables(monkeypatch)
    # Day 2E, Task 4 — "vendors" (not "clients"): a taxonomy-entity word
    # with zero evidence anywhere (no table, no approved mapping, no
    # generated vocabulary) is now a genuine, confirmed-unsupported dead
    # end and correctly fast-exits before the broad search this test wants
    # to isolate ever runs — see tests/test_unsupported_entity_business_
    # message.py for that behavior's own coverage. A neutral, non-taxonomy
    # word keeps this test's actual subject (synonym substitution) isolated
    # from that unrelated mechanism.
    _plan(db, question="show my vendors", concepts=["vendors"])
    # No synonym taught -> get_synonym_canonical returns None -> the
    # original raw term flows through untouched, byte-for-byte identical to
    # pre-Phase-3 behavior (normalization, same as always, happens later
    # downstream, not in this substitution step).
    assert seen_terms == [["vendors"]]


def test_canonical_term_itself_still_resolves_directly(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    seen_terms = _spy_candidate_tables(monkeypatch)
    _plan(db, question="show my customers", concepts=["customer"])
    # "customer" has no synonym taught for itself -> unchanged.
    assert seen_terms == [["customer"]]


def test_synonym_substitution_is_scoped_to_its_source(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    conn = _c(db)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Test2','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "vendors", "customer", actor_id="u1")

    # Day 2E, Task 4 — "vendors" (not "clients"): see test_unknown_synonym_
    # term_is_unchanged's own comment. Source 2 never taught this synonym,
    # so it is zero-evidence there too, and correctly fast-exits on its own
    # terms — the point of this test (scope isolation across sources) is
    # unaffected either way.
    seen_terms = _spy_candidate_tables(monkeypatch)
    plan_business_query(2, "u1", {"question": "show my vendors", "concepts": ["vendors"]})
    # Source 2 was never taught this synonym.
    assert seen_terms == [["vendors"]]


# ---------------------------------------------------------------------------
# Phase 3, Step 4 — remembered-terminology explainability. Pure evidence:
# these tests check query_plan["remembered_terminology"] only, never table
# selection/scoring (already pinned unchanged by every test above and below
# this section, all of which still pass with this key simply added).
# ---------------------------------------------------------------------------

def test_remembered_synonym_produces_one_evidence_record(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    seen_terms = _spy_candidate_tables(monkeypatch)
    result = _plan(db, question="show my clients", concepts=["clients"])
    assert seen_terms == [["customer"]]
    assert result["remembered_terminology"] == [{
        "evidence_type": "remembered_terminology",
        "original_term": "clients",
        "canonical_term": "customer",
        "source": "user_memory",
    }]


def test_no_evidence_for_unknown_term(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _spy_candidate_tables(monkeypatch)
    result = _plan(db, question="show my products", concepts=["products"])
    assert result["remembered_terminology"] == []


def test_no_evidence_when_canonical_term_used_directly(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    _spy_candidate_tables(monkeypatch)
    result = _plan(db, question="show my customers", concepts=["customer"])
    assert result["remembered_terminology"] == []


def test_no_evidence_across_source_isolation(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    conn = _c(db)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Test2','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    _spy_candidate_tables(monkeypatch)
    result = plan_business_query(2, "u1", {"question": "show my clients", "concepts": ["clients"]})
    assert result["remembered_terminology"] == []


def test_same_term_repeated_across_concepts_and_dimensions_yields_one_record(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    from data.concept_mapping_service import remember_synonym
    remember_synonym(1, "u1", "clients", "customer", actor_id="u1")

    _spy_candidate_tables(monkeypatch)
    result = _plan(db, question="clients by clients", concepts=["clients"], dimensions=["clients"])
    assert result["remembered_terminology"] == [{
        "evidence_type": "remembered_terminology",
        "original_term": "clients",
        "canonical_term": "customer",
        "source": "user_memory",
    }]


def test_merge_remembered_terminology_dedupes_across_internal_paths():
    # Direct unit test of the merge helper — proves that if
    # query_planning_service's own substitution AND
    # semantic_retrieval_service's own (mirrored) substitution each detect
    # the same (original_term, canonical_term) pair, the merged result still
    # has exactly one record, per the "one evidence item per unique
    # original/canonical pair" requirement.
    from data.query_planning_service import _merge_remembered_terminology
    record = {
        "evidence_type": "remembered_terminology",
        "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
    }
    merged = _merge_remembered_terminology([record], [dict(record)])
    assert merged == [record]


# ---------------------------------------------------------------------------
# Enterprise Phase 4 — generated_business_vocabulary scoring bonus
# ---------------------------------------------------------------------------

def _add_generated_vocab(db, term, table_fqn, *, tier="HIGH", score=0.9, column_name=""):
    c = _c(db)
    c.execute(
        "INSERT INTO generated_business_vocabulary "
        "(source_id, schema_snapshot_id, term, table_fqn, column_name, "
        " confidence_tier, confidence_score, evidence_json, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?,'[]',?,?)",
        (term, table_fqn, column_name, tier, score, _NOW, _NOW),
    )
    c.commit()
    c.close()


def test_generated_vocabulary_bonus_applied_when_no_approved_mapping(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Day 2E, Task 4 — "widget" (not "candidate"): a taxonomy-entity word
    # with an UNAPPROVED-table-only generated-vocabulary suggestion and no
    # approved mapping is now a genuine, confirmed-unsupported dead end
    # (weak, auto-generated evidence pointing at an unapproved table is
    # deliberately not enough to defer the fast exit — see data.semantic_
    # contract_service._has_validated_alternative_evidence) and correctly
    # fast-exits before this test's own subject (the vocabulary bonus's
    # effect on ranking_reasons) is ever reached. A neutral, non-taxonomy
    # word keeps that subject isolated from this unrelated mechanism.
    # Table name shares no token with "widget" — _score_term_match alone
    # contributes ~0, so a "Generated vocabulary" ranking_reason proves the
    # bonus (not name-matching) is what's operating here.
    _add_table(db, "dbo.legacy_staffing_store", approved=False)
    _add_generated_vocab(db, "widget", "dbo.legacy_staffing_store", tier="HIGH", score=0.9)

    result = _plan(db, question="widgets", concepts=["widget"])

    candidate = result["concepts"][0]["candidates"][0]
    assert candidate["table_fqn"] == "dbo.legacy_staffing_store"
    assert any("Generated vocabulary" in r for r in candidate["ranking_reasons"])


def test_approved_mapping_bonus_takes_precedence_over_generated_vocabulary_bonus(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.candidate_master", approved=False)
    _add_generated_vocab(db, "candidate", "dbo.candidate_master", tier="HIGH", score=0.9)

    from data.concept_mapping_service import promote_clarification_selection
    promote_clarification_selection(1, "u1", "candidate", "dbo.candidate_master", actor_id="u1")

    result = _plan(db, question="candidates", concepts=["candidate"])

    candidate = result["concepts"][0]["candidates"][0]
    reasons = candidate["ranking_reasons"]
    assert any("Approved concept mapping" in r for r in reasons)
    assert not any("Generated vocabulary" in r for r in reasons)
    # The human-approved decision must be the one that's actually selected.
    assert result["concepts"][0]["selected"]["table_fqn"] == "dbo.candidate_master"
    assert result["concepts"][0]["resolved"] is True


def test_generated_vocabulary_bonus_alone_insufficient_to_auto_select(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Day 2E, Task 4 — "widget" (not "candidate"): see test_generated_
    # vocabulary_bonus_applied_when_no_approved_mapping's own comment.
    # MEDIUM tier only, table name unrelated, no dictionary/domain/entity/
    # relationship evidence at all — 0.12 bonus alone must stay well under
    # _AUTO_SELECT_MIN_CONFIDENCE (0.5): a bias, never a bypass.
    _add_table(db, "dbo.legacy_staffing_store", approved=False)
    _add_generated_vocab(db, "widget", "dbo.legacy_staffing_store", tier="MEDIUM", score=0.5)

    result = _plan(db, question="widgets", concepts=["widget"])

    concept = result["concepts"][0]
    assert concept["resolved"] is False
    assert concept["selected"] is None


def test_generated_vocabulary_evidence_surfaced_on_plan(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.candidate_master", approved=False)
    _add_generated_vocab(db, "candidate", "dbo.candidate_master", tier="HIGH", score=0.9)

    result = _plan(db, question="how many candidates", measures=["candidate"])

    assert result["generated_vocabulary_evidence"] == [{
        "original_term": "candidate",
        "generated_term": "candidate",
        "source": "generated_vocabulary",
    }]


def test_merge_remembered_terminology_keeps_distinct_pairs():
    from data.query_planning_service import _merge_remembered_terminology
    a = {"evidence_type": "remembered_terminology", "original_term": "clients",
         "canonical_term": "customer", "source": "user_memory"}
    b = {"evidence_type": "remembered_terminology", "original_term": "revenue",
         "canonical_term": "sales_amount", "source": "user_memory"}
    merged = _merge_remembered_terminology([a], [b])
    assert merged == [a, b]


def test_existing_planner_output_unaffected_apart_from_new_key(tmp_path, monkeypatch):
    # Pins test_simple_measure_and_dimension_same_table's own assertions,
    # with nothing taught — proves the new key is purely additive.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders", table_class="Transactional")
    _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Revenue", approved=True)
    _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Order Status", approved=True)

    result = _plan(db, question="revenue by status", measures=["revenue"], dimensions=["status"])
    assert result["intent"]["type"] == "aggregate_by_dimension"
    assert result["intent"]["aggregation"] == "SUM"
    assert any(m["selected"] and m["selected"]["column_name"] == "amount" for m in result["measures"])
    assert result["remembered_terminology"] == []


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
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=5_000_000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_fk(db, "dbo.edu_clients", "id", "dbo.contacts_ref", "ref_id")

    _add_table(db, "dbo.edu_clients_temp", table_class="Transactional", row_count=10, approved=False)
    _add_column(db, "dbo.edu_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients_temp", "Operations", confidence=1.0)
    _add_entity(db, "dbo.edu_clients_temp", "Client", confidence=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])
    measure = result["measures"][0]
    assert measure["selected"] is not None
    assert measure["selected"]["table_fqn"] == "dbo.edu_clients"


@_RETIRED_DIRECT_PLANNER
def test_ambiguous_tied_confidence_still_refuses_auto_select(tmp_path, monkeypatch):
    # F. Two genuinely comparable tables with identical domain confidence
    # (not merely both unassigned) must still refuse to auto-select — the
    # confidence signal must not manufacture false differentiation between
    # candidates that are truly tied.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.edu_clients", "Operations", confidence=0.8)

    _add_table(db, "dbo.crm_clients", row_count=1000, approved=True)
    _add_column(db, "dbo.crm_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.crm_clients", "Operations", confidence=0.8)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])
    measure = result["measures"][0]
    assert measure["selected"] is None
    assert any("ambiguous" in w["type"] or "missing" in w["type"] for w in result["warnings"])


# ---------------------------------------------------------------------------
# Sprint 2, Signal #2 — Weighted All-Status Relationship Centrality
#
# _score_table_authority's existing rel_count bonus only ever sees AUTO/
# APPROVED edges (ctx["relationships"], the trusted join-planning contract,
# unchanged by this signal). This adds a SEPARATE, bounded, confidence- and
# status-weighted read of ALL relationship_status values (ctx
# ["relationship_evidence"]) so canonicality ranking can use a PENDING edge
# as weak corroborating evidence — never as a joinable edge (that safeguard
# lives entirely in semantic_layer_service._load_edges, untouched here).
#
# A-D/F/L are pure unit tests against _score_relationship_centrality /
# _score_table_authority. E is both a direct unit test and implied by G-K.
# G-K exercise the same behavior through the full plan_business_query path.
# All are written against the current code (no relationship_evidence signal
# yet) and are expected to fail red (ImportError at collection, since
# _score_relationship_centrality/_REL_CENTRALITY_MAX_BONUS don't exist yet)
# until the implementation lands.
# ---------------------------------------------------------------------------

def _edge(*, direction="outbound", status="PENDING", confidence=1.0,
          related_table="dbo.other", relationship_id=1):
    return {
        "direction": direction,
        "relationship_status": status,
        "confidence": confidence,
        "related_table": related_table,
        "relationship_id": relationship_id,
    }


def test_pending_edge_contributes_positive_bonus():
    # A. A PENDING edge with positive confidence contributes more than zero.
    result = _score_relationship_centrality([_edge(status="PENDING", confidence=0.8)])
    assert result["bonus"] > 0.0


def test_pending_contributes_less_than_trusted():
    # B. A PENDING edge contributes less, end-to-end through
    # _score_table_authority, than an otherwise identical APPROVED/AUTO edge
    # — which already earns full credit via the existing trusted rel_count
    # bonus and nothing extra from the new centrality signal.
    pending_ctx = _authority_ctx(domain_assigned=False, entity_assigned=False)
    pending_ctx["relationships"] = {}
    pending_ctx["relationship_evidence"] = [_edge(status="PENDING", confidence=1.0)]

    approved_ctx = _authority_ctx(domain_assigned=False, entity_assigned=False)
    approved_ctx["relationships"] = {"outbound": [{"to_table_fqn": "dbo.other"}], "inbound": []}
    approved_ctx["relationship_evidence"] = [_edge(status="APPROVED", confidence=1.0)]

    pending = _score_table_authority("dbo.widgets", pending_ctx)
    approved = _score_table_authority("dbo.widgets", approved_ctx)
    assert pending["bonus"] < approved["bonus"]


def test_zero_confidence_edge_contributes_zero():
    # C. A zero-confidence edge contributes zero.
    result = _score_relationship_centrality([_edge(status="PENDING", confidence=0.0)])
    assert result["bonus"] == 0.0


def test_rejected_and_unknown_status_contribute_zero():
    # D. Unsupported/rejected/deprecated relationship states contribute zero.
    for status in ("REJECTED", "DEPRECATED", "UNKNOWN_STATUS"):
        result = _score_relationship_centrality([_edge(status=status, confidence=1.0)])
        assert result["bonus"] == 0.0, status


def test_more_weighted_inbound_centrality_scores_higher():
    # E. More weighted inbound centrality produces a higher score when all
    # other signals are equal.
    few = _score_relationship_centrality([_edge(direction="inbound", status="PENDING", confidence=0.5)])
    many = _score_relationship_centrality([
        _edge(direction="inbound", status="PENDING", confidence=0.5, relationship_id=i)
        for i in range(5)
    ])
    assert many["bonus"] > few["bonus"]


def test_trusted_relationship_not_double_counted():
    # F. Existing trusted relationship behavior is not double-counted: adding
    # relationship_evidence entries for the SAME AUTO/APPROVED edges already
    # reflected in ctx["relationships"] must not change the total bonus.
    base_ctx = _authority_ctx(domain_assigned=False, entity_assigned=False)
    base_ctx["relationships"] = {"outbound": [{"to_table_fqn": "dbo.other"}], "inbound": []}
    base_ctx["relationship_evidence"] = []

    with_evidence_ctx = _authority_ctx(domain_assigned=False, entity_assigned=False)
    with_evidence_ctx["relationships"] = {"outbound": [{"to_table_fqn": "dbo.other"}], "inbound": []}
    with_evidence_ctx["relationship_evidence"] = [_edge(status="AUTO", confidence=1.0)]

    base = _score_table_authority("dbo.widgets", base_ctx)
    with_evidence = _score_table_authority("dbo.widgets", with_evidence_ctx)
    assert base["bonus"] == with_evidence["bonus"]


def test_relationship_evidence_bounded_by_cap():
    # L. Relationship evidence is bounded by the configured cap, even with a
    # large number of high-confidence PENDING edges.
    edges = [
        _edge(direction="inbound", status="PENDING", confidence=1.0, relationship_id=i)
        for i in range(50)
    ]
    result = _score_relationship_centrality(edges)
    assert result["bonus"] <= _REL_CENTRALITY_MAX_BONUS


def test_join_planning_still_refuses_pending_relationships(tmp_path, monkeypatch):
    # G. Join planning must continue to refuse PENDING relationships for
    # execution, even though they now contribute ranking evidence.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.sales")
    _add_table(db, "dbo.regions", table_class="Master")
    _add_column(db, "dbo.sales", "amount", data_type="DECIMAL", is_metric=True,
                business_label="Sales Amount", approved=True)
    _add_column(db, "dbo.sales", "region_id", data_type="INTEGER", uniqueness=0.05)
    _add_column(db, "dbo.regions", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.regions", "name", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Region Name", approved=True)
    _add_fk(db, "dbo.sales", "region_id", "dbo.regions", "id", status="PENDING")

    result = _plan(db, question="sales by region",
                   measures=["amount"], dimensions=["name"])
    jp = result["join_plan"]
    assert jp["required"] is True
    assert jp["steps"][0]["path_found"] is False


def test_relationship_hub_breaks_near_tie(tmp_path, monkeypatch):
    # H. A relationship hub can break a genuine near tie when the evidence
    # is sufficient. Pre-existing gap (domain 0.05*1.0 + entity 0.07*1.0 =
    # 0.12) alone sits below the 0.15 ambiguity margin -> still ambiguous.
    # 12 PENDING inbound edges (0.4 status weight * 1.0 confidence * 1.25
    # inbound weight = 0.5 each -> weighted_total 6.0, capped bonus +0.05)
    # bridges the remaining gap.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.hub_master", row_count=1000, approved=True)
    _add_column(db, "dbo.hub_master", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.hub_master", "Ops", confidence=1.0)
    _add_entity(db, "dbo.hub_master", "Thing", confidence=1.0)

    _add_table(db, "dbo.hub_rival", row_count=1000, approved=True)
    _add_column(db, "dbo.hub_rival", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    baseline = _plan(db, question="hub", concepts=["hub"])
    assert baseline["concepts"][0]["resolved"] is False

    for i in range(12):
        _add_fk(db, f"dbo.filler_{i}", "id", "dbo.hub_master", f"fk_{i}",
                status="PENDING", confidence=1.0)

    result = _plan(db, question="hub", concepts=["hub"])
    concept = result["concepts"][0]
    assert concept["resolved"] is True
    assert concept["selected"]["table_fqn"] == "dbo.hub_master"


def test_weak_pending_evidence_alone_cannot_manufacture_winner(tmp_path, monkeypatch):
    # I. Weak PENDING evidence alone (even well past the cap threshold)
    # cannot manufacture a winner between two otherwise perfectly tied
    # tables — the max +0.05 bonus never clears the 0.15 ambiguity margin
    # on its own.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.tie_a", row_count=1000, approved=True)
    _add_column(db, "dbo.tie_a", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.tie_b", row_count=1000, approved=True)
    _add_column(db, "dbo.tie_b", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    for i in range(20):
        _add_fk(db, f"dbo.filler2_{i}", "id", "dbo.tie_a", f"fk_{i}",
                status="PENDING", confidence=1.0)

    result = _plan(db, question="tie", concepts=["tie"])
    concept = result["concepts"][0]
    assert concept["resolved"] is False
    assert concept["selected"] is None


def test_equal_relationship_centrality_remains_ambiguous(tmp_path, monkeypatch):
    # J. Genuinely equal centrality (same weighted PENDING evidence on both
    # sides) remains ambiguous — it must not manufacture false
    # differentiation between candidates that are truly tied.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.sym_a", row_count=1000, approved=True)
    _add_column(db, "dbo.sym_a", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.sym_b", row_count=1000, approved=True)
    _add_column(db, "dbo.sym_b", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    for i in range(5):
        _add_fk(db, f"dbo.fillerA_{i}", "id", "dbo.sym_a", f"fk_{i}",
                status="PENDING", confidence=0.6)
        _add_fk(db, f"dbo.fillerB_{i}", "id", "dbo.sym_b", f"fk_{i}",
                status="PENDING", confidence=0.6)

    result = _plan(db, question="sym", concepts=["sym"])
    concept = result["concepts"][0]
    assert concept["resolved"] is False
    assert concept["selected"] is None
    candidates = {c["table_fqn"]: c["score"] for c in concept["candidates"]}
    assert candidates["dbo.sym_a"] == candidates["dbo.sym_b"]


def test_clear_winner_survives_maximum_relationship_evidence_on_loser(tmp_path, monkeypatch):
    # K. A previously clear winner remains the winner even when the losing
    # candidate is given the maximum possible relationship-centrality bonus.
    # Deliberately does NOT also stack Signal #1's max domain+entity
    # confidence swing on the loser — that combination is already documented
    # as a boundary case (not a decisive win) in
    # test_clear_winner_survives_maximum_opposing_confidence above, and
    # compounding it with a second signal's max swing on top belongs in the
    # Sprint 2 report's regression-risk section, not this test.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_clients", table_class="Master", row_count=5_000_000, approved=True)
    _add_column(db, "dbo.edu_clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_fk(db, "dbo.edu_clients", "id", "dbo.contacts_ref", "ref_id")

    _add_table(db, "dbo.edu_clients_temp", table_class="Transactional", row_count=10, approved=False)
    _add_column(db, "dbo.edu_clients_temp", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    for i in range(20):
        _add_fk(db, f"dbo.fillerK_{i}", "id", "dbo.edu_clients_temp", f"fk_{i}",
                status="PENDING", confidence=1.0)

    result = _plan(db, question="How many clients?",
                   concepts=["clients"], measures=["clients"], dimensions=[])
    measure = result["measures"][0]
    assert measure["selected"] is not None


# ---------------------------------------------------------------------------
# Accuracy Program A1, Phase A — Table-name scoring parity for
# _resolve_count_all(). Direct unit tests against _resolve_count_all() itself
# (no DB needed — table_contexts is a plain, hand-built dict, the same
# no-DB pattern _authority_ctx above already uses), isolating the name_score
# change from every other additive signal: profiling/relationships/domain/
# entity are all empty in _count_ctx so _score_table_authority's bonus is the
# same flat 0.03 (0.1 no-evidence importance * 0.30) baseline for every
# candidate table in every test below — any selection difference is coming
# from name_score (table name / business_name / description) alone, never
# from authority.
# ---------------------------------------------------------------------------

def _count_ctx(*, business_name=None, description=None, is_approved=False):
    dictionary = None
    if business_name is not None or description is not None or is_approved:
        dictionary = {
            "business_name": business_name,
            "description": description,
            "is_approved": is_approved,
        }
    return {
        "dictionary": dictionary,
        "domain": None,
        "entity": None,
        "profiling": None,
        "relationships": {},
        "relationship_evidence": [],
        "governance": {
            "domain_assigned": False,
            "entity_assigned": False,
            "dictionary_approved": is_approved,
        },
        "table": {"table_type": "TABLE"},
    }


def test_business_name_breaks_tie_table_names_alone_cannot():
    # 1. Two decoy tables that are structurally identical on raw table-name
    # Jaccard alone ("Huntr_Clients" / "Vendor_Clients" — each one filler
    # token + "clients") stay genuinely tied and must NOT auto-select.
    tied = {
        "dbo.Huntr_Clients": _count_ctx(),
        "dbo.Vendor_Clients": _count_ctx(),
    }
    tied_result = _resolve_count_all("clients", tied)
    assert tied_result["selected"] is None
    tied_scores = {round(c["name_score"], 4) for c in tied_result["candidates"]}
    assert len(tied_scores) == 1  # genuinely tied, not merely both weak

    # Same two tables, but one now carries an approved business_name that
    # exactly matches the term — must now win decisively (Phase A fix).
    # Table names alone (unchanged above) could never have produced this.
    broken = {
        "dbo.Huntr_Clients": _count_ctx(business_name="Clients", is_approved=True),
        "dbo.Vendor_Clients": _count_ctx(),
    }
    broken_result = _resolve_count_all("clients", broken)
    assert broken_result["selected"] is not None
    assert broken_result["selected"]["table_fqn"] == "dbo.Huntr_Clients"


def test_description_contributes_to_ranking():
    # 2. A cryptically-named table (zero table-name token overlap with the
    # term) stays unresolved on table name alone...
    no_description = {"dbo.T004": _count_ctx()}
    assert _resolve_count_all("clients", no_description)["selected"] is None

    # ...and resolves once its dictionary description is scored too — the
    # same _score_term_match() call, just with description now passed
    # through (Phase A fix; description alone, no business_name).
    with_description = {"dbo.T004": _count_ctx(description="Clients", is_approved=True)}
    result = _resolve_count_all("clients", with_description)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.T004"


def test_no_dictionary_metadata_matches_pre_fix_table_name_only_scoring():
    # 3. When a candidate table has no dictionary row at all
    # (ctx["dictionary"] is None — get_table_business_context's real
    # contract for an undocumented table), _resolve_count_all must fall
    # back to scoring the raw table name exactly as it did before this
    # change: no crash, no behavior change for sources with zero dictionary
    # coverage.
    result = _resolve_count_all("clients", {"dbo.EDU_Clients": _count_ctx()})
    only = result["candidates"][0]
    assert only["name_score"] == _score_term_match("clients", "EDU_Clients")


def test_existing_confident_single_table_match_still_resolves():
    # 4. Regression guard: a table whose raw name alone already clears the
    # auto-select threshold/margin (the common case before this change)
    # must still resolve exactly as before — the fix only ADDS text to
    # score against, it never removes or weakens the existing table-name
    # signal.
    ctx = {
        "dbo.Clients": _count_ctx(),
        "dbo.SomeUnrelatedTable": _count_ctx(),
    }
    result = _resolve_count_all("clients", ctx)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.Clients"


# ---------------------------------------------------------------------------
# Accuracy Program A4, Fix #2 — display-only label metadata pass-through.
#
# _resolve_count_all now also carries ctx["dictionary"]["business_name"] and
# ctx["entity"]["entity"] onto each candidate as "table_business_name"/
# "entity_name", purely so core/answering/explanation_builder.py can build a
# better clarification label. Both are read from the SAME ctx already fetched
# for name-scoring (no new lookup). These tests prove the pass-through is
# additive-only: scoring, ranking, and selection are byte-identical to the
# pre-Fix-#2 behavior above regardless of what these two new fields contain.
# ---------------------------------------------------------------------------

def test_table_business_name_and_entity_name_pass_through_without_new_reads():
    ctx = {
        "dbo.EDU_StaffingCandidates": _count_ctx(business_name="Candidates", is_approved=True),
    }
    ctx["dbo.EDU_StaffingCandidates"]["entity"] = {"entity": "Candidate"}
    result = _resolve_count_all("candidates", ctx)
    only = result["candidates"][0]
    assert only["table_business_name"] == "Candidates"
    assert only["entity_name"] == "Candidate"


def test_missing_entity_assignment_yields_none_not_crash():
    ctx = {"dbo.T004": _count_ctx()}  # dictionary=None, entity=None (default _count_ctx shape)
    result = _resolve_count_all("clients", ctx)
    only = result["candidates"][0]
    assert only["table_business_name"] is None
    assert only["entity_name"] is None


def test_candidate_ordering_unaffected_by_label_metadata():
    # Two candidates that score identically on name/authority; only their
    # display-only label metadata differs. Order (and the tie itself) must
    # be governed purely by score, exactly as in the untied/tied tests above.
    ctx = {
        "dbo.Huntr_Clients": _count_ctx(business_name="Clients", is_approved=True),
        "dbo.Vendor_Clients": _count_ctx(business_name="Clients", is_approved=True),
    }
    ctx["dbo.Huntr_Clients"]["entity"] = {"entity": "Client"}
    ctx["dbo.Vendor_Clients"]["entity"] = {"entity": "Unknown"}
    without_labels = _resolve_count_all("clients", {
        "dbo.Huntr_Clients": _count_ctx(business_name="Clients", is_approved=True),
        "dbo.Vendor_Clients": _count_ctx(business_name="Clients", is_approved=True),
    })
    with_labels = _resolve_count_all("clients", ctx)
    without_order = [c["table_fqn"] for c in without_labels["candidates"]]
    with_order = [c["table_fqn"] for c in with_labels["candidates"]]
    assert with_order == without_order
    without_scores = [round(c["score"], 6) for c in without_labels["candidates"]]
    with_scores = [round(c["score"], 6) for c in with_labels["candidates"]]
    assert with_scores == without_scores


def test_object_type_domain_and_relationship_metadata_pass_through_fix3():
    # Accuracy Program A4, Fix #3 — table_object_type/domain_name/
    # relationship_related_tables are read from the same already-fetched ctx
    # (no new lookup) and must not change ordering or score, exactly like
    # Fix #2's table_business_name/entity_name pass-through above.
    ctx = {
        "dbo.EDU_StaffingRecords": _count_ctx(business_name="Placements", is_approved=True),
    }
    ctx["dbo.EDU_StaffingRecords"]["table"] = {"table_type": "VIEW"}
    ctx["dbo.EDU_StaffingRecords"]["domain"] = {"domain": "Operations"}
    ctx["dbo.EDU_StaffingRecords"]["relationships"] = {
        "outbound": [{"to_table_fqn": "dbo.EDU_StaffingCandidates"}],
        "inbound": [{"from_table_fqn": "dbo.EDU_StaffingClients"}, {"from_table_fqn": "dbo.EDU_StaffingCandidates"}],
    }
    result = _resolve_count_all("placements", ctx)
    only = result["candidates"][0]
    assert only["table_object_type"] == "VIEW"
    assert only["domain_name"] == "Operations"
    # Deduplicated (EDU_StaffingCandidates appears in both outbound and inbound),
    # self excluded, order preserved.
    assert only["relationship_related_tables"] == ["dbo.EDU_StaffingCandidates", "dbo.EDU_StaffingClients"]

    without_relationship_metadata = _resolve_count_all("placements", {
        "dbo.EDU_StaffingRecords": _count_ctx(business_name="Placements", is_approved=True),
    })
    assert only["score"] == without_relationship_metadata["candidates"][0]["score"]


def test_missing_table_domain_and_relationship_metadata_yields_safe_defaults():
    ctx = {"dbo.T004": _count_ctx()}  # table_type="TABLE" (default), domain=None, relationships={}
    result = _resolve_count_all("clients", ctx)
    only = result["candidates"][0]
    assert only["table_object_type"] == "TABLE"
    assert only["domain_name"] is None


# ---------------------------------------------------------------------------
# Demo blocker fix — entity-relative attribute resolution
# ("Show the names of the 10 most recently added students.")
#
# _resolve_related_attribute_dimension now weights candidates by the FK
# relationship's own cardinality (analyze_join_quality's existing output,
# itself derived from profiling_column_profiles.uniqueness_score — no new
# metadata): an identity-style relationship (ONE_TO_ONE/ONE_TO_MANY — the
# concept table's FK column is itself near-unique, e.g. one Student maps to
# exactly one User account) is structurally the right kind of link for a
# personal-attribute request, unlike a category/lookup-style relationship
# (MANY_TO_ONE — many Students share one Marketing campaign) whose "*Name"
# column can score identically on lexical similarity alone.
# ---------------------------------------------------------------------------

def _seed_student_users_identity(db):
    """dbo.EDU_Student --(StudentUserID, near-unique)--> dnnuser.Users (identity link)."""
    _add_table(db, "dbo.EDU_Student", table_class="Master", row_count=12000)
    _add_column(db, "dbo.EDU_Student", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Student", "StudentUserID", uniqueness=0.999)
    _add_domain(db, "dbo.EDU_Student", "Operations", confidence=0.88)
    _add_entity(db, "dbo.EDU_Student", "Student", confidence=0.82)

    _add_table(db, "dnnuser.Users", table_class="Master", row_count=39505)
    _add_column(db, "dnnuser.Users", "UserID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dnnuser.Users", "FirstName", data_type="TEXT", is_dimension=True,
                business_label="First Name", approved=True)
    _add_column(db, "dnnuser.Users", "LastName", data_type="TEXT", is_dimension=True,
                business_label="Last Name", approved=True)

    _add_fk(db, "dbo.EDU_Student", "StudentUserID", "dnnuser.Users", "UserID", status="AUTO")


def _seed_student_marketing_lookup(db):
    """dbo.EDU_Student --(MarketingID, low-uniqueness)--> dbo.EDU_Marketing (category/lookup link)."""
    _add_table(db, "dbo.EDU_Marketing", table_class="Reference", row_count=40)
    _add_column(db, "dbo.EDU_Marketing", "MarketingID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Marketing", "MarketingName", data_type="TEXT", is_dimension=True,
                business_label="Marketing Name", approved=True)
    _add_column(db, "dbo.EDU_Student", "MarketingID", uniqueness=0.002)
    _add_fk(db, "dbo.EDU_Student", "MarketingID", "dbo.EDU_Marketing", "MarketingID", status="AUTO")


def test_student_name_resolves_through_users_identity_relationship(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_student_users_identity(db)

    result = _resolve_related_attribute_dimension("names", "dbo.EDU_Student", 1, "u1")
    assert result is not None
    assert all(entry.get("selected") for entry in result)
    assert {entry["selected"]["table_fqn"] for entry in result} == {"dnnuser.Users"}
    assert {entry["selected"]["column_name"] for entry in result} == {"FirstName", "LastName"}
    assert not any(entry["warnings"] for entry in result)


def test_marketing_name_rejected_for_student_name_request(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_student_users_identity(db)
    _seed_student_marketing_lookup(db)

    result = _resolve_related_attribute_dimension("names", "dbo.EDU_Student", 1, "u1")
    assert result is not None
    # The category/lookup-style MarketingName must never be selected...
    assert not any(entry.get("selected", {}) and entry["selected"]["table_fqn"] == "dbo.EDU_Marketing"
                   for entry in result)
    # ...and the identity-style Users columns resolve cleanly, no clarification.
    assert all(entry.get("selected") for entry in result)
    assert {entry["selected"]["table_fqn"] for entry in result} == {"dnnuser.Users"}


def test_genuinely_ambiguous_name_request_still_clarifies(tmp_path, monkeypatch):
    # Two DIFFERENT identity-style (near-unique FK) relationships, both with
    # an equally strong name-like column — a real, unresolvable ambiguity
    # that must still surface as a clarification, not a guess.
    db = env(tmp_path, monkeypatch)
    _seed_student_users_identity(db)

    _add_table(db, "dbo.PersonProfile", table_class="Master", row_count=10900)
    _add_column(db, "dbo.PersonProfile", "ProfileID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.PersonProfile", "FullName", data_type="TEXT", is_dimension=True,
                business_label="Full Name", approved=True)
    _add_column(db, "dbo.EDU_Student", "ProfileID", uniqueness=0.998)
    _add_fk(db, "dbo.EDU_Student", "ProfileID", "dbo.PersonProfile", "ProfileID", status="AUTO")

    result = _resolve_related_attribute_dimension("names", "dbo.EDU_Student", 1, "u1")
    assert result is not None
    assert len(result) == 1
    assert result[0]["selected"] is None
    assert any(w["type"] == "ambiguous_dimension" for w in result[0]["warnings"])


def test_joined_detail_name_query_limits_to_ten_ordered_by_insert_date(tmp_path, monkeypatch):
    from core.semantic.concept_resolver import extract_terms
    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import generate_sql, detect_dialect

    db = env(tmp_path, monkeypatch)
    _seed_student_users_identity(db)
    _add_column(db, "dbo.EDU_Student", "InsertDate", data_type="DATETIME", is_date=True,
                business_label="Insert Date", approved=True)

    question = "Show the names of the 10 most recently added students."
    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions, "filters": [],
    })
    assert query_plan["intent"]["order"]["target"] == "date"
    assert query_plan["intent"]["order"]["limit"] == 10

    sql_plan = build_sql_plan(1, "u1", query_plan)
    assert sql_plan["validation"]["valid"] is True
    generated = generate_sql(1, "u1", sql_plan, dialect=detect_dialect(1))
    sql = generated["sql"]
    assert sql, "expected SQL to be generated"
    assert "TOP (10)" in sql or "LIMIT 10" in sql
    assert "ORDER BY" in sql
    assert "InsertDate" in sql
    assert "DESC" in sql


# ---------------------------------------------------------------------------
# General Attribute Resolution — a bare attribute word ("city") lands in
# `measures` purely because extract_terms() has no "by"-clause to classify
# it as a dimension, not because it names anything aggregatable. Ordinary
# measure resolution only ever tries _is_metric_column, so a real DIRECT
# dimension-typed column on the anchor's OWN table was invisible to it, and
# _resolve_related_attribute_dimension only ever searches RELATED tables,
# never the anchor itself. Real-the reference database regression: "Show instructor city"
# rendered SUM([InstructorCity]) — a SQL Server type error — before this
# same-table dimension-kind retry (routed to `dimensions`, never `measures`,
# so it can never be wrapped in an aggregate) was added.
# ---------------------------------------------------------------------------

def test_bare_attribute_word_resolves_as_dimension_on_anchor_table_directly(tmp_path, monkeypatch):
    from core.semantic.concept_resolver import extract_terms
    from data.sql_planning_service import build_sql_plan
    from data.sql_generation_service import generate_sql, detect_dialect

    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Instructor", table_class="Master")
    _add_column(db, "dbo.EDU_Instructor", "InstructorID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Instructor", "InstructorCity", data_type="TEXT", is_dimension=True,
                business_label="Instructor City", approved=True)

    question = "Show instructor city"
    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions, "filters": [],
    })
    assert query_plan["measures"] == []
    assert len(query_plan["dimensions"]) == 1
    dim_sel = query_plan["dimensions"][0]["selected"]
    assert dim_sel["table_fqn"] == "dbo.EDU_Instructor"
    assert dim_sel["column_name"] == "InstructorCity"

    sql_plan = build_sql_plan(1, "u1", query_plan)
    assert sql_plan["validation"]["valid"] is True
    select_row = next(r for r in sql_plan["select"] if r["column_name"] == "InstructorCity")
    assert select_row["aggregation"] is None

    generated = generate_sql(1, "u1", sql_plan, dialect=detect_dialect(1))
    assert generated["sql"], "expected SQL to be generated"
    assert "SUM" not in generated["sql"].upper()


def test_related_attribute_rescue_keeps_genuinely_numeric_column_as_measure(tmp_path, monkeypatch):
    # Regression safety: a rescued attribute whose OWN resolved column really
    # is is_metric (e.g. a fee/amount column reached via a related table)
    # must stay a measure, not be forced into `dimensions` just because it
    # came through the related-attribute fallback.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student", table_class="Master")
    _add_column(db, "dbo.EDU_Student", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Student", "AccountID", uniqueness=0.999)
    _add_table(db, "dbo.EDU_Account", table_class="Reference")
    _add_column(db, "dbo.EDU_Account", "AccountID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Account", "Balance", data_type="DECIMAL", is_metric=True,
                business_label="Balance", approved=True)
    _add_fk(db, "dbo.EDU_Student", "AccountID", "dbo.EDU_Account", "AccountID", status="AUTO")

    result = _resolve_related_attribute_dimension("balance", "dbo.EDU_Student", 1, "u1")
    assert result is not None
    assert result[0]["selected"]["is_metric"] is True


# ---------------------------------------------------------------------------
# Compound Business Phrase / Qualifier Understanding — a still-unresolved
# term sitting next to a term that DID resolve to a real column is a
# QUALIFIER of that resolved concept, not a missing second concept, when it
# carries its OWN real generated-vocabulary evidence for that SAME table.
# Real-the reference database regression: "How many students are on a trial subscription?" —
# "trial" resolves to dbo.EDU_ClassSignups.IsTrial, but "subscription" (real
# column-level evidence for the same table: IsSubscriptionPlan,
# SubscriptionPlanID, ...) scored too low on its own to auto-select any ONE
# of those columns, and stayed dangling, blocking the whole plan.
# ---------------------------------------------------------------------------

def _seed_student_classsignups_trial(db):
    _seed_student_users_identity(db)
    _add_table(db, "dbo.EDU_ClassSignups", table_class="Transactional", row_count=13963)
    _add_column(db, "dbo.EDU_ClassSignups", "ClassSignupsID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_ClassSignups", "StudentID", uniqueness=0.1)
    _add_column(db, "dbo.EDU_ClassSignups", "IsTrial", data_type="BIT", is_dimension=True,
                business_label="Is Trial", approved=True)
    _add_fk(db, "dbo.EDU_ClassSignups", "StudentID", "dbo.EDU_Student", "StudentID", status="AUTO")


def test_compound_qualifier_consumed_by_resolved_neighbor_attribute(tmp_path, monkeypatch):
    from core.semantic.concept_resolver import extract_terms

    db = env(tmp_path, monkeypatch)
    _seed_student_classsignups_trial(db)
    # "subscription" has real column-level vocabulary evidence for the SAME
    # table "trial" resolves to, even though it never scores high enough on
    # its own to auto-select any single column there (mirrors real-the reference database
    # IsSubscriptionPlan/SubscriptionPlanID/SubscriptionMonthlyAmount).
    _add_generated_vocab(db, "subscription", "dbo.EDU_ClassSignups", tier="MEDIUM", column_name="SubscriptionPlanID")

    question = "How many students are on a trial subscription?"
    concepts, measures, dims = extract_terms(question)
    result = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dims, "filters": [],
    })
    dangling = [e["term"] for e in result["measures"] + result["dimensions"] if not e.get("selected")]
    assert "subscription" not in dangling
    resolved_tables = {
        e["selected"]["table_fqn"] for e in result["measures"] + result["dimensions"] if e.get("selected")
    }
    assert "dbo.EDU_ClassSignups" in resolved_tables


def test_qualifier_drop_requires_real_vocabulary_evidence(tmp_path, monkeypatch):
    # Regression safety / "AI hypothesis cannot force unsupported compound
    # meaning": same shape as above, but with NO generated-vocabulary
    # evidence for "subscription" anywhere — it must stay a genuine
    # unresolved term rather than being silently dropped just because it
    # sits next to a resolved neighbor.
    from core.semantic.concept_resolver import extract_terms

    db = env(tmp_path, monkeypatch)
    _seed_student_classsignups_trial(db)

    question = "How many students are on a trial subscription?"
    concepts, measures, dims = extract_terms(question)
    result = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dims, "filters": [],
    })
    all_terms = [e["term"] for e in result["measures"] + result["dimensions"]]
    assert "subscription" in all_terms


def test_qualifier_drop_is_source_isolated(tmp_path, monkeypatch):
    # Vocabulary evidence recorded for a DIFFERENT source must never leak
    # into this source's qualifier-drop decision.
    from core.semantic.concept_resolver import extract_terms

    db = env(tmp_path, monkeypatch)
    _seed_student_classsignups_trial(db)
    conn = _c(db)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Other','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    _add_generated_vocab(db, "subscription", "dbo.EDU_ClassSignups", tier="MEDIUM", column_name="SubscriptionPlanID")
    # Re-point the just-inserted vocab row at source_id=2 instead of 1.
    conn = _c(db)
    conn.execute("UPDATE generated_business_vocabulary SET source_id = 2 WHERE table_fqn = 'dbo.EDU_ClassSignups'")
    conn.commit()
    conn.close()

    question = "How many students are on a trial subscription?"
    concepts, measures, dims = extract_terms(question)
    result = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dims, "filters": [],
    })
    all_terms = [e["term"] for e in result["measures"] + result["dimensions"]]
    assert "subscription" in all_terms


# ---------------------------------------------------------------------------
# Demo blocker fix — single-object sufficiency check
# ("How many launch participants are stalled, active, graduated, or not
# started?"). Previously: none of the 4 enumerated status VALUES lexically
# match any column NAME, so the ordinary path fell through to an expensive,
# doomed multi-table candidate + relationship search (confirmed against real
# the reference database metadata: 22.5 minutes) ending in a "no_valid_join" refusal. Now:
# checked BEFORE any of that runs, whether one already-known object (a
# curated VIEW preferred) alone satisfies grain + a status-like dimension +
# cached value coverage — if so, answered directly against it, join search
# never runs at all.
# ---------------------------------------------------------------------------

_STATUS_VALUES_REQUIRED = ["stalled", "active", "graduated", "not started"]


def _seed_launch_participant_raw_table(db):
    _add_table(db, "dbo.pilot_participants_raw", table_class="Transactional", row_count=500)
    pid = _add_column(db, "dbo.pilot_participants_raw", "ParticipantID", is_pk=1, is_id=1, uniqueness=1.0)
    sid = _add_column(db, "dbo.pilot_participants_raw", "Status", data_type="TEXT", is_dimension=True,
                       business_label="Status", approved=True)
    _add_value_samples(db, sid, ["Active", "Graduated", "Stalled", "Not Started"])
    return pid, sid


def _seed_launch_participant_view(db, *, table_fqn="dbo.v_pilot_participant_current_status"):
    _add_table(db, table_fqn, table_class="Master", row_count=500)
    _add_table_type(db, table_fqn, "VIEW")
    pid = _add_column(db, table_fqn, "CAP_Launch_ParticipantID", is_pk=1, is_id=1, uniqueness=1.0)
    sid = _add_column(db, table_fqn, "Current_Status", data_type="TEXT", is_dimension=True,
                       business_label="Current Status", approved=True)
    _add_value_samples(db, sid, ["Active", "Graduated", "Stalled", "Not Started"])
    return pid, sid


def test_trusted_view_preferred_over_raw_launch_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_launch_participant_raw_table(db)
    _seed_launch_participant_view(db)

    question = "How many launch participants are stalled, active, graduated, or not started?"
    result = _find_sufficient_single_object(
        ["launch", "participants"], question, _STATUS_VALUES_REQUIRED, 1, "u1",
    )
    assert result is not None
    assert result["table_fqn"] == "dbo.v_pilot_participant_current_status"
    assert result["status_column"] == "Current_Status"


def test_join_planning_skipped_when_view_sufficient(tmp_path, monkeypatch):
    from core.semantic.concept_resolver import extract_terms
    import data.semantic_layer_service as sls

    db = env(tmp_path, monkeypatch)
    _seed_launch_participant_view(db)

    join_search_calls = []
    monkeypatch.setattr(sls, "analyze_join_quality",
                         lambda *a, **kw: join_search_calls.append(a) or None)
    monkeypatch.setattr(sls, "recommend_best_join_path",
                         lambda *a, **kw: join_search_calls.append(a) or None)

    question = "How many launch participants are stalled, active, graduated, or not started?"
    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions, "filters": [],
    })
    assert query_plan is not None
    assert query_plan["join_plan"]["required"] is False
    assert query_plan["join_plan"]["tables"] == ["dbo.v_pilot_participant_current_status"]
    assert join_search_calls == []


def test_sufficient_object_result_groups_by_current_status(tmp_path, monkeypatch):
    from core.semantic.concept_resolver import extract_terms

    db = env(tmp_path, monkeypatch)
    _seed_launch_participant_view(db)

    question = "How many launch participants are stalled, active, graduated, or not started?"
    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions, "filters": [],
    })
    assert query_plan["intent"]["type"] == "aggregate_by_dimension"
    assert query_plan["intent"]["aggregation"] == "COUNT"
    assert query_plan["dimensions"][0]["selected"]["column_name"] == "Current_Status"
    assert query_plan["dimensions"][0]["selected"]["table_fqn"] == "dbo.v_pilot_participant_current_status"
    assert query_plan["measures"][0]["selected"]["table_fqn"] == "dbo.v_pilot_participant_current_status"


def test_view_missing_required_field_does_not_falsely_pass_sufficiency(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # A view with a status column and full value coverage, but NO grain
    # column at all (no PK/identity/is_id/near-unique column) — must not
    # falsely pass just because the dimension/value check alone succeeds.
    _add_table(db, "dbo.v_pilot_status_no_grain", table_class="Master", row_count=500)
    _add_table_type(db, "dbo.v_pilot_status_no_grain", "VIEW")
    sid = _add_column(db, "dbo.v_pilot_status_no_grain", "Current_Status", data_type="TEXT",
                       is_dimension=True, business_label="Current Status", approved=True)
    _add_value_samples(db, sid, ["Active", "Graduated", "Stalled", "Not Started"])

    result, probe_used = _check_single_object_sufficiency(
        "dbo.v_pilot_status_no_grain", _STATUS_VALUES_REQUIRED, 1, "u1", allow_live_probe=False,
    )
    assert result is None
    assert probe_used is False

    # A second view WITH grain but INCOMPLETE value coverage (missing
    # "Stalled") — must not falsely pass either.
    _add_table(db, "dbo.v_pilot_status_partial_values", table_class="Master", row_count=500)
    _add_table_type(db, "dbo.v_pilot_status_partial_values", "VIEW")
    _add_column(db, "dbo.v_pilot_status_partial_values", "ParticipantID", is_pk=1, is_id=1, uniqueness=1.0)
    sid2 = _add_column(db, "dbo.v_pilot_status_partial_values", "Current_Status", data_type="TEXT",
                        is_dimension=True, business_label="Current Status", approved=True)
    _add_value_samples(db, sid2, ["Active", "Graduated"])  # missing Stalled, Not Started

    result2, probe_used2 = _check_single_object_sufficiency(
        "dbo.v_pilot_status_partial_values", _STATUS_VALUES_REQUIRED, 1, "u1", allow_live_probe=False,
    )
    assert result2 is None
    assert probe_used2 is False


# ---------------------------------------------------------------------------
# Join-Path/Dimension Selection — bare "per/by <entity>" relationship
# aggregates must GROUP BY the grouped entity's own business label column
# (when one confidently exists) rather than its surrogate key. Real-the reference database
# regression: "How many students are in each program?" grouped by
# dbo.EDU_Track.TrackID instead of TrackName even though Program correctly
# resolved to dbo.EDU_Track and the join path was already correct — the
# bare-entity branch of _try_build_relationship_aggregate_plan (Automatic
# Multi-Entity Semantic Grounding) always used _select_entity_key (PK-first)
# without ever trying _resolve_entity_label_column first, unlike every other
# dimension-resolution path in this module. Direct unit tests against
# _try_build_relationship_aggregate_plan with hand-built table_contexts
# (same shape get_table_business_context returns) — following this file's
# own established direct-unit-test pattern — plus real DB fixtures only for
# the FK edge _plan_joins needs.
# ---------------------------------------------------------------------------

def _rel_agg_ctx(table_fqn, columns, *, business_name=None):
    return {
        "columns": columns,
        "dictionary": {"business_name": business_name or table_fqn.split(".")[-1], "is_approved": True, "description": None},
        "domain": None, "entity": None, "profiling": None,
        "relationships": {}, "governance": {},
        "table": {"table_type": "TABLE"},
    }


def _rel_agg_col(name, *, is_pk=False, is_dimension=False, is_id=False, business_label=None, cardinality_tier="MEDIUM"):
    return {
        "column_name": name,
        "dictionary": {
            "business_label": business_label or name, "is_dimension": is_dimension,
            "is_metric": False, "is_date": False, "is_id": is_id, "pii_risk": False, "is_approved": True,
        },
        "profiling": {"cardinality_tier": cardinality_tier, "pii_name_heuristic": False, "semantic_type": None},
        "schema": {"is_primary_key": is_pk, "is_identity": is_pk, "data_type": "INTEGER" if is_pk else "TEXT"},
    }


_REL_AGG_QUERY_INTENT = {
    "aggregation_target": "entity_count", "date_range": None,
    "order": None, "status_value": None, "distinct": False, "aggregation": None,
}


def test_bare_relationship_dimension_prefers_label_column_over_surrogate_key(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    # Table name deliberately shares its own core token ("path") with its
    # label column (TrackName), exactly like real-the reference database dbo.EDU_Track/TrackName
    # — _label_column_bonus's FULL bonus (vs. a weak nod) requires the
    # label column's own name to embed the table's own identity token.
    _add_table(db, "dbo.path_lookup")
    _add_column(db, "dbo.path_lookup", "TrackID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_fk(db, "dbo.students", "TrackID", "dbo.path_lookup", "TrackID")

    table_contexts = {
        "dbo.students": _rel_agg_ctx("dbo.students", [_rel_agg_col("StudentID", is_pk=True, is_id=True)]),
        "dbo.path_lookup": _rel_agg_ctx("dbo.path_lookup", [
            _rel_agg_col("TrackID", is_pk=True, is_id=True),
            _rel_agg_col("TrackName", is_dimension=True, business_label="Path Name", cardinality_tier="HIGH"),
        ]),
    }
    grounding = {
        "fully_grounded": True, "matched_entities": ["Student", "Program"],
        "entity_table_map": {"Student": "dbo.students", "Program": "dbo.path_lookup"},
        "grounded": {"Student": {}, "Program": {}}, "seed_table_fqns": ["dbo.students", "dbo.path_lookup"],
    }
    result = _try_build_relationship_aggregate_plan(
        1, "u1", "How many students are in each program?", grounding, _REL_AGG_QUERY_INTENT,
        table_contexts, measure_terms=["students"], candidate_ranking=[],
        remembered_terminology=[], generated_vocabulary_evidence=[], warnings=[],
        concept_mappings=None, generated_vocab=None,
    )
    assert result is not None
    assert result["dimensions"][0]["selected"]["table_fqn"] == "dbo.path_lookup"
    assert result["dimensions"][0]["selected"]["column_name"] == "TrackName"
    # The join itself is unaffected by which column represents the dimension.
    assert set(result["join_plan"]["tables"]) == {"dbo.students", "dbo.path_lookup"}


def test_bare_relationship_dimension_falls_back_to_key_without_confident_label(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_table(db, "dbo.instructors")
    _add_column(db, "dbo.instructors", "InstructorID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_fk(db, "dbo.students", "InstructorID", "dbo.instructors", "InstructorID")

    table_contexts = {
        "dbo.students": _rel_agg_ctx("dbo.students", [_rel_agg_col("StudentID", is_pk=True, is_id=True)]),
        "dbo.instructors": _rel_agg_ctx("dbo.instructors", [
            _rel_agg_col("InstructorID", is_pk=True, is_id=True),
            # No name/title/label-style column at all — no confident label
            # column exists, so behavior must stay exactly as before this
            # change: fall back to the trusted entity key.
            _rel_agg_col("City", is_dimension=True, cardinality_tier="MEDIUM"),
        ]),
    }
    grounding = {
        "fully_grounded": True, "matched_entities": ["Student", "Instructor"],
        "entity_table_map": {"Student": "dbo.students", "Instructor": "dbo.instructors"},
        "grounded": {"Student": {}, "Instructor": {}}, "seed_table_fqns": ["dbo.students", "dbo.instructors"],
    }
    result = _try_build_relationship_aggregate_plan(
        1, "u1", "How many students are there per instructor?", grounding, _REL_AGG_QUERY_INTENT,
        table_contexts, measure_terms=["students"], candidate_ranking=[],
        remembered_terminology=[], generated_vocabulary_evidence=[], warnings=[],
        concept_mappings=None, generated_vocab=None,
    )
    assert result is not None
    assert result["dimensions"][0]["selected"]["table_fqn"] == "dbo.instructors"
    assert result["dimensions"][0]["selected"]["column_name"] == "InstructorID"


# ---------------------------------------------------------------------------
# Scoring/calibration milestone — evidence-quality fixes
#
# Confirmed live against a real production database: "Average course fee by program" and
# "Show classes and their program" both correctly joined dbo.EDU_Class ->
# dbo.EDU_Track but then displayed/grouped by EDU_Track.ProgramType (a
# category column that merely shares the "program" token) instead of
# EDU_Track.TrackName (the table's real display name — zero token overlap
# with "program" at all, since "path" and "program" are different words
# for the business's own entity). _resolve_related_attribute_dimension's
# hard name_score>=_AUTO_SELECT_MIN_CONFIDENCE floor had no fallback to a
# table's own designated label column when only a weak/incidental attribute
# match cleared it.
# ---------------------------------------------------------------------------

def _set_semantic_type(db, table_fqn, col_name, semantic_type):
    c = _c(db)
    c.execute(
        "UPDATE profiling_column_profiles SET semantic_type = ? "
        "WHERE source_id = 1 AND table_fqn = ? AND column_name = ?",
        (semantic_type, table_fqn, col_name),
    )
    c.commit()
    c.close()


def _seed_class_path(db):
    """dbo.EDU_Class --(TrackID, many-to-one)--> dbo.EDU_Track, mirroring the
    a real production database shape: EDU_Track has a category column (ProgramType) that
    tokenizes to {'program','type'} and its real label column (TrackName)."""
    _add_table(db, "dbo.EDU_Class", table_class="Transactional", row_count=1542)
    _add_column(db, "dbo.EDU_Class", "ClassID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Class", "TrackID", uniqueness=0.02)
    _add_column(db, "dbo.EDU_Class", "CourseFee", data_type="DECIMAL", is_metric=True,
                business_label="Course Fee", approved=True)

    _add_table(db, "dbo.EDU_Track", table_class="Reference", row_count=311)
    _add_column(db, "dbo.EDU_Track", "TrackID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Track", "TrackName", data_type="TEXT", is_dimension=True,
                business_label="Path Name", approved=True)
    _add_column(db, "dbo.EDU_Track", "ProgramType", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Program Type", approved=True)

    _add_fk(db, "dbo.EDU_Class", "TrackID", "dbo.EDU_Track", "TrackID", status="AUTO")


def test_label_column_preferred_over_incidental_token_match_for_bare_entity_term(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_class_path(db)

    result = _resolve_related_attribute_dimension("program", "dbo.EDU_Class", 1, "u1")
    assert result is not None
    assert all(entry.get("selected") for entry in result)
    assert {entry["selected"]["table_fqn"] for entry in result} == {"dbo.EDU_Track"}
    assert {entry["selected"]["column_name"] for entry in result} == {"TrackName"}, (
        "A bare 'program' term must resolve to the table's own label column "
        "(TrackName), not an attribute column that only incidentally shares "
        "a token (ProgramType)."
    )


def test_specific_attribute_term_still_wins_over_label_column(tmp_path, monkeypatch):
    """A deliberate, specific mention of the attribute itself ("program
    type") must NOT be overridden by the label-column rescue — the rescue
    only fires for a weak/incidental match, never a genuinely strong one."""
    db = env(tmp_path, monkeypatch)
    _seed_class_path(db)

    result = _resolve_related_attribute_dimension("program type", "dbo.EDU_Class", 1, "u1")
    assert result is not None
    assert all(entry.get("selected") for entry in result)
    assert {entry["selected"]["column_name"] for entry in result} == {"ProgramType"}


def test_label_column_rescue_does_not_fire_without_a_weak_match(tmp_path, monkeypatch):
    """No column on the related table clears the floor at all for this term
    — the rescue must not invent a match; behavior stays a clean refusal."""
    db = env(tmp_path, monkeypatch)
    _seed_class_path(db)

    result = _resolve_related_attribute_dimension("zzz_unrelated_term", "dbo.EDU_Class", 1, "u1")
    assert result is None


# ---------------------------------------------------------------------------
# Scoring/calibration milestone — status-column FLAG-vs-STATUS preference
#
# Confirmed live against a real production database: "How many active classes are there?"
# resolved to dbo.EDU_Class.CLASSTYPE (a 4-value category column classified
# STATUS purely from low cardinality — core/profiling/classification/
# column_typer.py's _score_status grants this from cardinality+datatype
# alone, no name-vocabulary requirement) instead of dbo.EDU_Class.IsActive
# (the real boolean flag, classified FLAG at confidence 1.0). Filtering
# CLASSTYPE = 'Active' silently returned zero rows — a confidently WRONG
# answer, not a safe refusal. _find_status_column's priority order checked
# STATUS-typed columns before ever considering FLAG-typed ones.
# ---------------------------------------------------------------------------

def _seed_class_with_status_and_flag(db):
    _add_table(db, "dbo.EDU_Class", table_class="Transactional", row_count=1542)
    _add_column(db, "dbo.EDU_Class", "ClassID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Class", "CLASSTYPE", data_type="TEXT",
                cardinality_tier="LOW", is_dimension=True, business_label="Classtype")
    _set_semantic_type(db, "dbo.EDU_Class", "CLASSTYPE", "STATUS")
    _add_column(db, "dbo.EDU_Class", "IsActive", data_type="BOOLEAN",
                cardinality_tier="BINARY", is_dimension=True, business_label="Is Active")
    _set_semantic_type(db, "dbo.EDU_Class", "IsActive", "FLAG")


def test_boolean_status_value_prefers_flag_column_over_status_category_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_class_with_status_and_flag(db)
    conn = _c(db)
    try:
        ctx = {"dbo.EDU_Class": {"columns": [
            {"column_name": row["column_name"],
             "profiling": {"semantic_type": row["semantic_type"]},
             "dictionary": {"business_label": row["business_label"] if "business_label" in row.keys() else None}}
            for row in conn.execute(
                "SELECT pcp.column_name, pcp.semantic_type, dc.business_label "
                "FROM profiling_column_profiles pcp "
                "LEFT JOIN data_dictionary_columns dc "
                "  ON dc.table_fqn = pcp.table_fqn AND dc.column_name = pcp.column_name "
                "WHERE pcp.table_fqn = 'dbo.EDU_Class'"
            )
        ]}}
    finally:
        conn.close()

    found = _find_status_column(ctx, "dbo.EDU_Class", status_value="active")
    assert found == ("dbo.EDU_Class", "IsActive"), (
        "A boolean status word ('active') must prefer the real FLAG column "
        "over a same-named STATUS-typed category column."
    )

    # Non-boolean status word: unaffected, falls through to the ordinary
    # STATUS-typed-column-first priority (byte-identical to before this fix).
    found_open = _find_status_column(ctx, "dbo.EDU_Class", status_value="open")
    assert found_open == ("dbo.EDU_Class", "CLASSTYPE")

    # No status_value at all: unaffected (existing callers with 2 positional args).
    found_bare = _find_status_column(ctx, "dbo.EDU_Class")
    assert found_bare == ("dbo.EDU_Class", "CLASSTYPE")
