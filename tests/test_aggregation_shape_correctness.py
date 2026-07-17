"""
Tests for Milestone Phase 6.2 — Aggregation Shape Correctness.

Two layers, matching the two ways the milestone changes behavior:
  A. End-to-end tests against a real sqlite-backed plan_business_query()
     call (same env()/_add_table()/_add_column() pattern as
     test_phase9_query_planning.py) — proves entity-key SELECTION works
     against real metadata (declared PK / approved dict ID / high-confidence
     profiling key / weak unapproved dict ID / PII exclusion / safe
     COUNT(*) fallback).
  B. Direct unit tests of data.query_planning_service._apply_join_fanout_
     safety with hand-built measures/join_plan dicts — the existing test
     suite's own test_fanout_warning already notes real fan-out risk is
     hard to force deterministically end-to-end through the real join-
     quality pipeline ("we accept either: fanout warning present, or join
     not found"), so the fan-out SAFETY LOGIC itself is tested directly
     here rather than only through that nondeterministic path.

Run from the project root:
    venv/Scripts/pytest tests/test_aggregation_shape_correctness.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase6-2-agg-shape-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-phase6-2-salt-long-enough-value-1234567890")

import data.models as models
from data.query_planning_service import _apply_join_fanout_safety, plan_business_query
from data.sql_planning_service import build_sql_plan
from data.sql_generation_service import detect_dialect, generate_sql

_NOW = "2026-07-13T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
)


# ---------------------------------------------------------------------------
# Fixture harness — self-contained, mirrors test_phase9_query_planning.py's
# env()/_add_table()/_plan() pattern, with its own _add_column() that
# exposes primary-key / dictionary-ID / profiling-identity / PII signals
# independently (needed to test each entity-key tier in isolation, which
# the shared M-1-era helper's single `is_id` kwarg conflates).
# ---------------------------------------------------------------------------

def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase6_2.db")
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


def _add_table(db, table_fqn, *, row_count=1000):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash(table_fqn)) % 100000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,'Transactional','COMPLETE',?,?,?)",
        (tid, table_fqn, name, schema, row_count, _NOW, _NOW),
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


_col_seq = [500]


def _add_column(
    db, table_fqn, col_name, *,
    data_type="TEXT", is_primary_key=False, is_identity=False, uniqueness_score=0.05,
    dict_is_id=False, dict_approved=False, pii_name_heuristic=False, pii_risk=False,
    is_metric=False,
):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,0,0.0,'HIGH',?,0,?,?)",
        (_col_seq[0], table_fqn, col_name, data_type,
         int(is_primary_key), int(is_identity), uniqueness_score, int(pii_name_heuristic), _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,0,0,?,?,?,?,?,?)",
        (table_fqn, col_name, col_name, int(is_metric),
         int(dict_is_id), int(pii_risk), int(dict_approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _plan(db_path, **kwargs):
    return plan_business_query(1, "u1", kwargs)


# ---------------------------------------------------------------------------
# A1 — Core reproduced-bug scenarios
# ---------------------------------------------------------------------------

def test_how_many_students_counts_entity_not_stored_metric(tmp_path, monkeypatch):
    # The Phase 6 report's headline bug: a stored metric column
    # (TotalStudents) must not override entity-count intent.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "student_id", data_type="INTEGER",
                is_primary_key=True, uniqueness_score=1.0)
    _add_table(db, "dbo.class_position_analytics")
    _add_column(db, "dbo.class_position_analytics", "TotalStudents", data_type="INTEGER", is_metric=True)

    result = _plan(db, question="How many students are enrolled?",
                   concepts=["students", "enrolled"], measures=["students", "enrolled"], dimensions=[])

    measure = result["measures"][0]
    assert measure["selected"]["table_fqn"] == "dbo.students"
    assert measure["selected"]["column_name"] == "student_id"
    assert measure["selected"]["aggregation_target"] == "entity_count"


def test_number_of_clients_resolves_entity_count(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "client_id", data_type="INTEGER",
                is_primary_key=True, uniqueness_score=1.0)

    result = _plan(db, question="Number of clients", concepts=["clients"], measures=["clients"], dimensions=[])

    assert result["intent"]["aggregation_target"] == "entity_count"
    assert result["measures"][0]["selected"]["column_name"] == "client_id"


def test_how_many_distinct_students_enrolled(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "student_id", data_type="INTEGER",
                is_primary_key=True, uniqueness_score=1.0)

    result = _plan(db, question="How many distinct students enrolled?",
                   concepts=["students", "enrolled"], measures=["students", "enrolled"], dimensions=[])

    measure = result["measures"][0]
    assert result["intent"]["aggregation_target"] == "distinct_entity_count"
    assert measure["selected"]["column_name"] == "student_id"
    assert measure["selected"]["distinct"] is True


def test_stored_total_students_column_not_selected_over_entity(tmp_path, monkeypatch):
    # Same shape as the headline test, phrased differently, and with the
    # decoy column scoring a strong direct name match ("students" is a
    # substring of "TotalStudents") to prove the entity path is preferred
    # even when the decoy would otherwise win on name-match alone.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "student_id", data_type="INTEGER",
                is_primary_key=True, uniqueness_score=1.0)
    _add_table(db, "dbo.rollup")
    _add_column(db, "dbo.rollup", "TotalStudents", data_type="INTEGER", is_metric=True)

    result = _plan(db, question="How many students?", concepts=["students"], measures=["students"], dimensions=[])

    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.students"


def test_explicit_sum_total_students_still_uses_sum(tmp_path, monkeypatch):
    # The companion case: when the question explicitly asks to total/sum a
    # stored metric, that is aggregation_target=measure_sum, not
    # entity_count — unchanged column-level _resolve_term path.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.rollup")
    _add_column(db, "dbo.rollup", "TotalStudents", data_type="INTEGER", is_metric=True)

    result = _plan(db, question="Sum of TotalStudents", measures=["totalstudents"], dimensions=[])

    assert result["intent"]["aggregation_target"] == "measure_sum"
    assert result["measures"][0]["selected"]["column_name"] == "TotalStudents"
    assert "aggregation_target" not in result["measures"][0]["selected"] or \
        result["measures"][0]["selected"].get("aggregation_target") is None


# ---------------------------------------------------------------------------
# A2 — Entity-key selection tiers (the "one additional requirement")
# ---------------------------------------------------------------------------

def test_declared_pk_selected_first(tmp_path, monkeypatch):
    # Table has BOTH a declared PK and an approved dictionary ID column —
    # tier 1 (PK) must win over tier 2 (approved dict ID).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "row_id", data_type="INTEGER",
                is_primary_key=True, uniqueness_score=1.0)
    _add_column(db, "dbo.clients", "client_code", data_type="TEXT",
                dict_is_id=True, dict_approved=True)

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["column_name"] == "row_id"
    assert sel["key_tier"] == 1
    assert sel["key_confidence"] == "high"


def test_approved_dictionary_id_selected_second(tmp_path, monkeypatch):
    # No declared PK — approved dictionary ID (tier 2) must win over a
    # high-confidence profiling key candidate (tier 3) also present.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "client_code", data_type="TEXT",
                dict_is_id=True, dict_approved=True)
    _add_column(db, "dbo.clients", "row_guid", data_type="TEXT",
                is_identity=True, uniqueness_score=0.999)

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["column_name"] == "client_code"
    assert sel["key_tier"] == 2
    assert sel["key_confidence"] == "high"


def test_high_confidence_profiling_key_selected_next(tmp_path, monkeypatch):
    # No PK, no approved dictionary ID — a high-confidence profiling key
    # candidate (uniqueness_score >= 0.99 AND is_identity) is tier 3.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "row_guid", data_type="TEXT",
                is_identity=True, uniqueness_score=0.999)

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["column_name"] == "row_guid"
    assert sel["key_tier"] == 3
    assert sel["key_confidence"] == "medium"


def test_unapproved_dictionary_id_treated_as_weak_fallback(tmp_path, monkeypatch):
    # Only an UNAPPROVED dictionary ID exists — tier 4, the weakest signal,
    # still selected (better than nothing) but flagged as "weak".
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "legacy_code", data_type="TEXT",
                dict_is_id=True, dict_approved=False)

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["column_name"] == "legacy_code"
    assert sel["key_tier"] == 4
    assert sel["key_confidence"] == "weak"
    assert "unapproved" in sel["key_selection_reason"].lower()


def test_pii_key_excluded_from_candidacy(tmp_path, monkeypatch):
    # The only "id"-shaped column is PII-flagged — must be excluded
    # entirely, even though it would otherwise be tier 2 (approved dict ID).
    # No safe fallback key exists, so COUNT(*) is used, never the PII column.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "ssn", data_type="TEXT",
                dict_is_id=True, dict_approved=True, pii_risk=True)

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["column_name"] is None
    assert sel["key_confidence"] == "none"

    # PII column must never appear anywhere in the generated SQL.
    sql_plan = build_sql_plan(1, "u1", result)
    sql_gen = generate_sql(1, "u1", sql_plan, dialect=detect_dialect(1))
    assert sql_gen["sql"] is not None
    assert "ssn" not in sql_gen["sql"].lower()


def test_safe_single_table_count_star_fallback(tmp_path, monkeypatch):
    # No PK, no dictionary ID at any approval level, no high-confidence
    # profiling key — safe COUNT(*) fallback on a single, unambiguous table.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "notes", data_type="TEXT")

    result = _plan(db, question="How many clients?", concepts=["clients"], measures=["clients"], dimensions=[])

    sel = result["measures"][0]["selected"]
    assert sel["table_fqn"] == "dbo.clients"
    assert sel["column_name"] is None
    assert sel["key_confidence"] == "none"

    sql_plan = build_sql_plan(1, "u1", result)
    assert sql_plan["validation"]["valid"] is True
    sql_gen = generate_sql(1, "u1", sql_plan, dialect=detect_dialect(1))
    assert "COUNT(*)" in sql_gen["sql"]


# ---------------------------------------------------------------------------
# B — Join fan-out safety: direct unit tests of _apply_join_fanout_safety
# (deterministic hand-built inputs — see module docstring for why).
# ---------------------------------------------------------------------------

def _entity_measure(term, table_fqn, *, column_name, key_tier, key_confidence, distinct=False):
    return {
        "term": term,
        "selected": {
            "table_fqn": table_fqn, "column_name": column_name,
            "aggregation_target": "entity_count", "key_tier": key_tier,
            "key_confidence": key_confidence, "key_selection_reason": f"tier {key_tier}",
            "distinct": distinct,
        },
        "candidates": [], "warnings": [],
    }


def test_count_distinct_key_under_trusted_one_to_many_join():
    # Tier 1 (declared PK) key + HIGH fan-out risk -> promoted to
    # COUNT(DISTINCT key), not refused.
    measures = [_entity_measure("candidates", "dbo.candidates", column_name="candidate_id", key_tier=1, key_confidence="high")]
    join_plan = {"required": True, "fanout_risk": "HIGH"}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    sel = measures[0]["selected"]
    assert sel is not None
    assert sel["distinct"] is True
    assert not warnings


def test_weak_fallback_rejected_under_uncertain_join_fanout():
    # Tier 4 (unapproved dictionary ID) key must NOT be trusted to control
    # MEDIUM/HIGH fan-out — refuse rather than guess.
    measures = [_entity_measure("candidates", "dbo.candidates", column_name="legacy_code", key_tier=4, key_confidence="weak")]
    join_plan = {"required": True, "fanout_risk": "MEDIUM"}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    assert measures[0]["selected"] is None
    assert any(w["type"] == "uncontrolled_fanout_entity_count" for w in warnings)
    assert "unapproved dictionary identifier" in warnings[0]["message"]


def test_uncontrolled_fanout_with_no_key_refuses():
    # No key at all (COUNT(*) case) + MEDIUM/HIGH fan-out -> refuse, since
    # there's nothing to de-duplicate on and COUNT(*) would double-count.
    measures = [_entity_measure("candidates", "dbo.candidates", column_name=None, key_tier=None, key_confidence="none")]
    join_plan = {"required": True, "fanout_risk": "HIGH"}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    assert measures[0]["selected"] is None
    assert any(w["type"] == "uncontrolled_fanout_entity_count" for w in warnings)


def test_low_fanout_does_not_force_distinct_or_refuse():
    measures = [_entity_measure("candidates", "dbo.candidates", column_name="candidate_id", key_tier=1, key_confidence="high")]
    join_plan = {"required": True, "fanout_risk": "LOW"}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    sel = measures[0]["selected"]
    assert sel is not None
    assert sel["distinct"] is False
    assert not warnings


def test_no_join_required_is_a_no_op():
    measures = [_entity_measure("candidates", "dbo.candidates", column_name=None, key_tier=None, key_confidence="none")]
    join_plan = {"required": False, "fanout_risk": None}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    assert measures[0]["selected"] is not None
    assert not warnings


def test_non_entity_count_measure_untouched_by_fanout_safety():
    # measure_sum entries (no aggregation_target) must never be touched by
    # this function, regardless of fan-out risk.
    measures = [{
        "term": "revenue", "selected": {"table_fqn": "dbo.orders", "column_name": "amount"},
        "candidates": [], "warnings": [],
    }]
    join_plan = {"required": True, "fanout_risk": "HIGH"}
    warnings = []

    _apply_join_fanout_safety(measures, join_plan, warnings)

    assert measures[0]["selected"] is not None
    assert not warnings


# ---------------------------------------------------------------------------
# C — Existing SUM/AVG/MIN/MAX behavior does not regress
# ---------------------------------------------------------------------------

def test_sum_avg_min_max_unaffected_by_entity_count_routing(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.invoices")
    _add_column(db, "dbo.invoices", "amount", data_type="DECIMAL", is_metric=True)

    for question, terms, expected_agg, expected_target in [
        ("Total amount", ["total", "amount"], "SUM", "measure_sum"),
        ("Average amount", ["average", "amount"], "AVG", "measure_average"),
        ("Lowest amount", ["lowest", "amount"], "MIN", "measure_min"),
        ("Highest amount", ["highest", "amount"], "MAX", "measure_max"),
    ]:
        result = _plan(db, question=question, measures=terms, dimensions=[])
        assert result["intent"]["aggregation"] == expected_agg
        assert result["intent"]["aggregation_target"] == expected_target
        assert result["measures"][-1]["selected"]["column_name"] == "amount"
