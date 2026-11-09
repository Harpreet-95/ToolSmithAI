"""
Tests for Enterprise Accuracy Program A2/Phase C — Metadata-Validated
Compound Business Phrases.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the exact fixture convention already
established by tests/test_phase9_query_planning.py (env/_add_table/
_add_column/_plan) — duplicated here rather than cross-imported, matching
this repo's one-file-per-feature test convention.

Table/column names below are representative generic fixtures chosen for
this test file only (e.g. "dbo.job_orders", "dbo.orders") — never a real production database
production table names, and nothing in production code references them.

Run from the project root:
    venv/Scripts/pytest tests/test_compound_phrase_resolution.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phaseC-compound-phrase-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phaseC-salt-long-enough-value-1234567890")

import data.models as models
from data.query_planning_service import (
    _resolve_concept, _resolve_role_with_compound_preference,
)

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
    db_path = str(tmp_path / "phaseC.db")
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
        (table_fqn, name, schema, name.replace("_", " ").title(), int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_col_seq = [900]


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


# ---------------------------------------------------------------------------
# Direct unit test of _resolve_role_with_compound_preference's suppression
# contract, independent of the full plan_business_query wiring.
# ---------------------------------------------------------------------------

def test_compound_preference_helper_suppresses_only_on_confident_resolution(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.job_orders", table_class="Master", row_count=5000, approved=True)
    _add_column(db, "dbo.job_orders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    from data.query_planning_service import get_table_business_context
    table_contexts = {"dbo.job_orders": get_table_business_context(1, "u1", "dbo.job_orders")}

    calls = []

    def resolver_fn(term):
        calls.append(term)
        return _resolve_concept(term, table_contexts, {}, {})

    entries = _resolve_role_with_compound_preference(
        ["job", "orders"], "How many open job orders are there?", 1, resolver_fn, budget=[5],
    )
    assert len(entries) == 1
    assert entries[0]["selected"]["table_fqn"] == "dbo.job_orders"
    # The compound was tried; the two components were never independently
    # re-resolved after it succeeded.
    assert "job orders" in calls
    assert "job" not in calls
    assert "orders" not in calls
