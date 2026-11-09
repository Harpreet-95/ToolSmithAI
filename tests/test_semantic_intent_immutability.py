"""
FINAL CLEANUP MILESTONE, Part 3 — Semantic Immutability.

Proves the hard boundary added to data.query_planning_service._plan_business_
query_impl: once a request carries a populated "ai_interpretation" (the
signal core.orchestrator.context_builder attaches only after the AI question
interpreter has already resolved a question's business meaning), the
deterministic construction layer's own analytics-intent override (the
wh_ranking/trend block keyed on derive_analytics_intent) must never
re-derive or silently replace the caller-supplied measure, dimension,
concept, aggregation, or ordering.

Reuses the exact fixture pattern and trigger question ("Which courses have
the highest enrollment?") from test_analytics_intent_layer.py, which already
proves that SAME question, with NO ai_interpretation attached, gets its
caller-supplied measure/dimension overridden by the wh_ranking analytics
shape. This file adds a course-level "rating" metric column and supplies it
as the caller's own measure choice, so the two tests below can assert
opposite outcomes for the identical question text depending solely on
whether ai_interpretation is present.
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-immutability-secret-long-enough-value-1")
os.environ.setdefault("USER_ID_SALT", "test-immutability-salt-long-enough-value-123")

import data.models as models
from data.query_planning_service import plan_business_query

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
    db_path = str(tmp_path / "immutability.db")
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
    # Caller's own resolvable measure choice — deliberately NOT what the
    # wh_ranking analytics-intent shape would derive from the question text
    # ("enrollment"/entity-count of dbo.enrollments).
    _add_column(db, "dbo.courses", "rating", data_type="INTEGER", is_metric=True,
                cardinality_tier="MEDIUM", business_label="Rating")
    _add_column(db, "dbo.enrollments", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.enrollments", "course_id", data_type="INTEGER", uniqueness=0.02)
    _add_fk(db, "dbo.enrollments", "course_id", "dbo.courses", "id")


_QUESTION = "Which courses have the highest enrollment?"

# A non-empty ai_interpretation payload, matching the shape
# core.orchestrator.context_builder attaches to request_payload once the AI
# question interpreter has already resolved this question (see that
# module's request_payload["ai_interpretation"] wiring). Content values
# themselves are not read by the boundary check — only truthiness is: the
# guard is "did an upstream authority already finalize this question's
# meaning," not "what exactly did it decide."
_AI_INTERPRETATION = {
    "entities": ["courses"],
    "measures": ["rating"],
    "dimensions": [],
    "requested_attributes": [],
    "relationship_intent": "single_entity",
    "expected_result_shape": "ranked_list",
    "date_range": None,
    "sorting": None,
    "clarification_required": False,
    "clarification_reason": None,
}


def test_without_ai_interpretation_analytics_intent_still_overrides(tmp_path, monkeypatch):
    """Baseline (unchanged behavior): with no ai_interpretation attached,
    this is the sole resolver of business shape, so the wh_ranking override
    still legitimately replaces the caller's own measure/dimension choice —
    exactly what test_analytics_intent_layer.py already proves for this
    same question."""
    db = env(tmp_path, monkeypatch)
    _seed_courses_enrollments(db)

    result = plan_business_query(1, "u1", {
        "question": _QUESTION,
        "concepts": ["courses"],
        "measures": ["rating"],
        "dimensions": ["name"],
    })
    assert result is not None
    assert result["intent"]["aggregation"] == "COUNT"
    assert result["intent"]["aggregation_target"] == "entity_count"
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.enrollments"
    assert result["measures"][0]["selected"]["column_name"] == "id"


def test_with_ai_interpretation_caller_supplied_intent_is_immutable(tmp_path, monkeypatch):
    """With ai_interpretation attached (finalized upstream semantic intent),
    the SAME question and the SAME caller-supplied measure/dimension must
    resolve to the caller's own choice ("rating" on dbo.courses) — the
    deterministic construction layer must not silently substitute the
    wh_ranking-derived "enrollment count" shape instead."""
    db = env(tmp_path, monkeypatch)
    _seed_courses_enrollments(db)

    result = plan_business_query(1, "u1", {
        "question": _QUESTION,
        "concepts": ["courses"],
        "measures": ["rating"],
        "dimensions": ["name"],
        "ai_interpretation": _AI_INTERPRETATION,
    })
    assert result is not None
    # NOT overridden to the wh_ranking entity-count shape.
    assert result["intent"]["aggregation_target"] != "entity_count"
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.courses"
    assert result["measures"][0]["selected"]["column_name"] == "rating"
    assert result["dimensions"][0]["selected"]["table_fqn"] == "dbo.courses"
    assert result["dimensions"][0]["selected"]["column_name"] == "name"


def test_empty_ai_interpretation_dict_does_not_count_as_finalized(tmp_path, monkeypatch):
    """An empty dict (falsy) must behave identically to the key being
    absent — only a genuinely populated ai_interpretation counts as a
    finalized upstream resolution."""
    db = env(tmp_path, monkeypatch)
    _seed_courses_enrollments(db)

    result = plan_business_query(1, "u1", {
        "question": _QUESTION,
        "concepts": ["courses"],
        "measures": ["rating"],
        "dimensions": ["name"],
        "ai_interpretation": {},
    })
    assert result is not None
    assert result["intent"]["aggregation_target"] == "entity_count"
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.enrollments"
