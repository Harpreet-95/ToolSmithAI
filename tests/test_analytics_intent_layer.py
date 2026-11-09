"""
Tests for the Enterprise Implementation Phase 2 — Analytics Intent Layer.

Verifies the full chain (plan_business_query -> build_sql_plan -> generate_sql)
for the ranking-style business questions the layer targets: a "which <entity>
have the highest <measure>?" question must GROUP BY the entity, COUNT the
related measure-table's rows (not scalar-aggregate a column), and ORDER BY
that count DESC with a default LIMIT — without any change to SQL Generator,
Join Planner, Relationship Discovery, or the database schema.

Follows the exact fixture pattern established by test_phase9_query_planning.py
(same production schema via data.models.init_db, per-test temp SQLite file).
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase2-analytics-intent-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase2-analytics-salt-long-enough-value-123")

import data.models as models
from core.semantic.concept_resolver import extract_terms
from data.query_planning_service import plan_business_query
from data.sql_planning_service import build_sql_plan
from data.sql_generation_service import generate_sql

_NOW = "2026-07-15T00:00:00+00:00"

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
    db_path = str(tmp_path / "phase2.db")
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


def _add_table(db, table_fqn, *, table_class="Transactional", row_count=1000):
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
        "VALUES (1,1,?,?,?,'TABLE',?,1,?,?,?)",
        (table_fqn, name, schema, name.capitalize(), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_col_seq = [100]


def _add_column(db, table_fqn, col_name, *,
                data_type="TEXT", is_pk=0, is_id=0, uniqueness=0.05,
                cardinality_tier="MEDIUM", is_metric=None, is_dimension=None,
                business_label=None):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,0,?,0,0,?,0,0,?,?)",
        (_col_seq[0], table_fqn, col_name, data_type, is_pk, uniqueness, cardinality_tier, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?,0,?,0,1,?,?,?)",
        (table_fqn, col_name, business_label or col_name,
         int(bool(is_metric)), int(bool(is_dimension)), int(bool(is_id)),
         "rule_based", _NOW, _NOW),
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


def _seed_courses_enrollments(db):
    _add_table(db, "dbo.courses", table_class="Master")
    _add_table(db, "dbo.enrollments", table_class="Transactional")
    _add_column(db, "dbo.courses", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.courses", "name", data_type="TEXT", is_dimension=True,
                cardinality_tier="MEDIUM", business_label="Course Name")
    _add_column(db, "dbo.enrollments", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.enrollments", "course_id", data_type="INTEGER", uniqueness=0.02)
    _add_fk(db, "dbo.enrollments", "course_id", "dbo.courses", "id")


def _plan(db_path, question):
    concepts, measures, dimensions = extract_terms(question)
    return plan_business_query(1, "u1", {
        "question": question, "concepts": concepts,
        "measures": measures, "dimensions": dimensions,
    })


# ---------------------------------------------------------------------------
# "Which courses have the highest enrollment?" — WH-ranking pattern
# ---------------------------------------------------------------------------

def test_wh_ranking_counts_related_entity_not_scalar_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_courses_enrollments(db)

    result = _plan(db, "Which courses have the highest enrollment?")
    assert result is not None
    assert result["intent"]["aggregation"] == "COUNT"
    assert result["intent"]["aggregation_target"] == "entity_count"
    # Grouped by the course dimension, not a scalar MAX/enrollment column.
    assert result["dimensions"][0]["selected"]["column_name"] == "name"
    assert result["dimensions"][0]["selected"]["table_fqn"] == "dbo.courses"
    # Measure resolves to a COUNT of the enrollments table's own key, not a
    # column named "enrollment".
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.enrollments"
    assert result["measures"][0]["selected"]["column_name"] == "id"
    # Ranking defaults: DESC, top 10, even though the question named no
    # explicit number.
    assert result["intent"]["order"]["direction"] == "DESC"
    assert result["intent"]["order"]["limit"] == 10


def test_wh_ranking_generates_group_by_and_order_by_alias_sql(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_courses_enrollments(db)

    query_plan = _plan(db, "Which courses have the highest enrollment?")
    sql_plan = build_sql_plan(1, "u1", query_plan)
    assert sql_plan["validation"]["valid"] is True
    assert sql_plan["group_by"], "expected a GROUP BY on the course dimension"
    assert sql_plan["order_by"] == [{"alias": "count_id", "direction": "DESC"}]
    assert sql_plan["limits"]["row_limit"] == 10

    sql = generate_sql(1, "u1", sql_plan)["sql"]
    assert "GROUP BY" in sql
    assert "ORDER BY" in sql
    assert "count_id" in sql
    # The order-by reference is the SELECT alias, not a second raw column
    # reference to the join-side table — proves no duplicated/invented
    # column was introduced downstream.
    assert sql.count("ORDER BY") == 1
