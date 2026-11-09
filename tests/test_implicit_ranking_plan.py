"""
Tests for data.query_planning_service._try_build_implicit_ranking_plan —
Reconnect Reliability / Implicit Activity Measure milestone.

Covers the fail-closed contract (no resolver -> None, no trusted relationship
-> None, resolver returns None/low-confidence -> None, resolver hallucinates
an identifier outside the evidence -> caller-side rejection since this
function never trusts an unvalidated ImplicitMeasureResult blindly) and the
happy path (confident related_row_count result -> a real, join-safe,
ordered aggregate_by_dimension plan whose join_plan/measures/dimensions/
order all come from the SAME deterministic helpers the rest of
query_planning_service.py already uses and already has its own tests for).

Built on the real production schema (data.models.init_db) against a per-test
temp SQLite file, following the pattern established by
tests/test_phase9_query_planning.py — reuses that file's own fixture helpers
philosophy rather than reinventing one.

Run:
    python -m pytest tests/test_implicit_ranking_plan.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-implicit-ranking-plan-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-implicit-ranking-salt-long-enough-value-1")

import data.models as models
from core.semantic.implicit_measure_adjudicator import ImplicitMeasureResult
from data.business_knowledge_service import get_table_business_contexts_batch
from data.query_planning_service import _try_build_implicit_ranking_plan

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
    "data.relationship_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "implicit_ranking.db")
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
    tid = abs(hash(table_fqn)) % 10000
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


_col_seq = [100]


def _add_column(db, table_fqn, col_name, *, data_type="INTEGER", is_pk=0, uniqueness=0.05,
                 is_dimension=None, business_label=None):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,0,?,0,0.0,'MEDIUM',0,0,?,?)",
        (_col_seq[0], table_fqn, col_name, data_type, is_pk, uniqueness, _NOW, _NOW),
    )
    if is_dimension is not None or business_label:
        c.execute(
            "INSERT OR REPLACE INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,?,0,?,0,0,0,1,?,?,?)",
            (table_fqn, col_name, business_label or col_name,
             int(bool(is_dimension)), "rule_based", _NOW, _NOW),
        )
    c.commit()
    c.close()
    return _col_seq[0]


_rel_seq = [500]


def _add_fk(db, from_fqn, from_col, to_fqn, to_col, *, status="AUTO"):
    c = _c(db)
    _rel_seq[0] += 1
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',1.0,'{}',?,?)",
        (_rel_seq[0], fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col,
         f"FK_{_rel_seq[0]}", _NOW, status),
    )
    c.commit()
    c.close()


def _contexts(db_path, *fqns):
    return get_table_business_contexts_batch(1, "u1", list(fqns))


def _common_args(table_contexts, *, status_value="Active"):
    return dict(
        source_id=1, user_id="u1", question="Show the most active clients",
        table_contexts=table_contexts,
        query_intent={"status_value": status_value, "status_negated": False, "order": None},
        resolved_filters=[{"column": "IsActive", "operator": "=", "value": 1, "resolved": True,
                            "table_fqn": "dbo.clients", "filter_kind": "status"}],
        warnings=[], candidate_ranking=[], remembered_terminology=[],
        generated_vocabulary_evidence=[], resolved_concepts=[],
    )


# ---------------------------------------------------------------------------
# Fail-closed contract
# ---------------------------------------------------------------------------

def test_no_resolver_returns_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "id", is_pk=1, uniqueness=1.0)
    ctx = _contexts(db, "dbo.clients")

    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=None,
    )
    assert result is None


def test_no_trusted_relationship_still_asks_with_empty_evidence(tmp_path, monkeypatch):
    # AI-FIRST CAPABILITY HARDENING milestone — no trusted relationship no
    # longer means "never even ask": the resolver may still find a
    # date_recency measure from the entity's OWN columns (live-confirmed
    # necessary against a real production database, dbo.EDU_Clients — zero relationships, but
    # a real LastUpdated column). It IS asked, with an empty relationships
    # list; a resolver that (mis)behaves and returns "related_row_count"
    # anyway is still safely rejected, since dbo.signups was never part of
    # the trusted evidence handed to it.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "id", is_pk=1, uniqueness=1.0)
    ctx = _contexts(db, "dbo.clients")

    seen_relationships = {}

    def resolver(entity_table_fqn, table_context, relationships):
        seen_relationships["value"] = relationships
        return ImplicitMeasureResult(kind="related_row_count", related_table_fqn="dbo.signups",
                                      related_fk_column="ClientID", confidence=0.9)

    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=resolver,
    )
    assert result is None  # "dbo.signups" was never trusted evidence -- rejected regardless
    assert seen_relationships["value"] == []  # asked, but with zero (not fabricated) trusted evidence


def test_pending_relationship_not_trusted(tmp_path, monkeypatch):
    # AI-FIRST CAPABILITY HARDENING milestone — a PENDING relationship must
    # still never be trusted evidence, but "not trusted" no longer means
    # "the resolver is never called at all": it may still be asked (with
    # that PENDING relationship excluded) in case a date_recency measure is
    # available from the entity's own columns instead. The security property
    # this test guards is narrower and stricter than before: PENDING
    # evidence must never reach the resolver, and a resolver result naming a
    # table reached only via that untrusted relationship must never be
    # trusted either (see the evidence_by_table check this milestone added).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_table(db, "dbo.signups")
    _add_column(db, "dbo.clients", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "ClientID", data_type="INTEGER")
    _add_fk(db, "dbo.signups", "ClientID", "dbo.clients", "id", status="PENDING")
    ctx = _contexts(db, "dbo.clients", "dbo.signups")

    seen_relationships = {}

    def resolver(entity_table_fqn, table_context, relationships):
        seen_relationships["value"] = relationships
        return ImplicitMeasureResult(kind="related_row_count", related_table_fqn="dbo.signups",
                                      related_fk_column="ClientID", confidence=0.9)

    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=resolver,
    )
    assert seen_relationships["value"] == []  # the PENDING relationship was never passed as evidence
    assert result is None  # and a result naming dbo.signups anyway is still rejected


def test_resolver_returns_none_falls_back(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_table(db, "dbo.signups")
    _add_column(db, "dbo.clients", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "ClientID", data_type="INTEGER")
    _add_fk(db, "dbo.signups", "ClientID", "dbo.clients", "id", status="AUTO")
    ctx = _contexts(db, "dbo.clients", "dbo.signups")

    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients",
        implicit_measure_resolver=lambda *a, **kw: None,
    )
    assert result is None


def test_date_recency_kind_not_wired_yet_falls_back(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_table(db, "dbo.signups")
    _add_column(db, "dbo.clients", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "id", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "ClientID", data_type="INTEGER")
    _add_fk(db, "dbo.signups", "ClientID", "dbo.clients", "id", status="AUTO")
    ctx = _contexts(db, "dbo.clients", "dbo.signups")

    resolver = lambda *a, **kw: ImplicitMeasureResult(kind="date_recency", date_column="LastLogin", confidence=0.9)
    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=resolver,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_related_row_count_builds_ordered_aggregate_plan(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_table(db, "dbo.signups")
    _add_column(db, "dbo.clients", "ClientID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.clients", "ClientsName", data_type="TEXT", is_dimension=True,
                business_label="Client Name")
    _add_column(db, "dbo.signups", "SignupID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "ClientID", data_type="INTEGER")
    _add_fk(db, "dbo.signups", "ClientID", "dbo.clients", "ClientID", status="AUTO")
    ctx = _contexts(db, "dbo.clients", "dbo.signups")

    seen_evidence = {}

    def resolver(entity_table_fqn, table_context, relationships):
        seen_evidence["entity_table_fqn"] = entity_table_fqn
        seen_evidence["relationships"] = relationships
        assert entity_table_fqn == "dbo.clients"
        assert len(relationships) == 1
        rel = relationships[0]
        assert rel["related_table_fqn"] == "dbo.signups"
        assert rel["fk_column"] == "ClientID"
        assert set(rel["columns"]) == {"SignupID", "ClientID"}
        assert rel["has_primary_key"] is True
        return ImplicitMeasureResult(
            kind="related_row_count", related_table_fqn="dbo.signups",
            related_fk_column="ClientID", confidence=0.9,
            evidence_reference="Signups is an event/transaction table referencing Clients.",
        )

    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=resolver,
    )

    assert result is not None
    assert seen_evidence["entity_table_fqn"] == "dbo.clients"
    assert result["intent"]["type"] == "aggregate_by_dimension"
    assert result["intent"]["aggregation"] == "COUNT"
    assert result["intent"]["aggregation_target"] == "entity_count"
    assert result["intent"]["order"]["direction"] == "DESC"
    assert result["intent"]["order"]["table_fqn"] == "dbo.signups"
    assert result["measures"][0]["selected"]["table_fqn"] == "dbo.signups"
    assert result["measures"][0]["selected"]["aggregation_target"] == "entity_count"
    assert result["dimensions"][0]["selected"]["table_fqn"] == "dbo.clients"
    assert result["dimensions"][0]["selected"]["column_name"] == "ClientsName"
    assert result["join_plan"]["required"] is True
    assert "dbo.signups" in result["join_plan"]["tables"]
    assert any(w["type"] == "implicit_activity_measure_used" for w in result["warnings"])
    # The status filter the caller already resolved (before this function was
    # ever called) must be carried through unchanged, not rebuilt.
    assert result["filters"][0]["column"] == "IsActive"


def test_untrusted_related_table_in_resolver_output_is_rejected(tmp_path, monkeypatch):
    # The resolver itself is expected to identifier-lock its own output
    # (core.semantic.implicit_measure_adjudicator._validate does this for
    # the real AI path) -- this test proves the CALLER side never blindly
    # trusts an ImplicitMeasureResult naming a table that wasn't actually in
    # the trusted-relationship evidence it was given, even if some future or
    # buggy resolver implementation returned one anyway.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_table(db, "dbo.signups")
    _add_table(db, "dbo.unrelated_table")
    _add_column(db, "dbo.clients", "ClientID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "SignupID", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.signups", "ClientID", data_type="INTEGER")
    _add_fk(db, "dbo.signups", "ClientID", "dbo.clients", "ClientID", status="AUTO")
    ctx = _contexts(db, "dbo.clients", "dbo.signups", "dbo.unrelated_table")

    resolver = lambda *a, **kw: ImplicitMeasureResult(
        kind="related_row_count", related_table_fqn="dbo.unrelated_table",
        related_fk_column="SomeColumn", confidence=0.99,
    )
    result = _try_build_implicit_ranking_plan(
        **_common_args(ctx), primary_table="dbo.clients", implicit_measure_resolver=resolver,
    )
    # dbo.unrelated_table was never in the trusted-relationship evidence
    # bundle -- _plan_joins finds no path to it and the function fails
    # closed rather than joining an arbitrary table.
    assert result is None
