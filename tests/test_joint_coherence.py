"""
Cross-Term Join Consistency / Coherent Multi-Concept Resolution.

Covers data.query_planning_service._apply_joint_coherence — the bounded
joint-validation step that runs after concepts/measures/dimensions are each
independently resolved (their own per-term candidate search, no visibility
into what any other term picked) but before `_primary_table_hint` anchors
every other unresolved term's same-table/related-table rescue fallback onto
whichever table happened to resolve.

Live-confirmed failure this closes: "Show students and their user accounts"
— the CONCEPT-role resolution of "user accounts" correctly finds a real,
strongly name-matched table, but the MEASURE-role resolution of the exact
same phrase independently picks an unrelated, weakly name-matched table
(dbo.SAMPLETBL in production) purely on generic authority bonuses. Whichever
one resolves first becomes the anchor unconditionally, silently dragging
every other term onto itself too.

Unit tests (1-6) call _apply_joint_coherence directly with hand-built
entries against a small fixture schema (dbo.A -> dbo.B -> dbo.C is a real,
trusted FK chain; dbo.Island has no relationship to anything) so the
algorithm itself is verified independent of any particular scoring
function's output. Integration tests (7-9) replay real questions through
plan_business_query end-to-end against a production-style fixture (mirrors
tests/test_live_sql_qa_repro.py's own dbo.EDU_Student / dnnuser.Users
schema) plus one deliberately unrelated island table, proving the fix at
the same layer the real regression was reported at.
"""
import sqlite3

from cryptography.fernet import Fernet
import os

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-joint-coherence-secret-long-enough-value-1")
os.environ.setdefault("USER_ID_SALT", "test-joint-coherence-salt-long-enough-value-123")

import data.models as models
from core.semantic.concept_resolver import extract_terms
from data.query_planning_service import (
    _apply_joint_coherence, plan_business_query,
)
from data.sql_planning_service import build_sql_plan

_NOW = "2026-08-13T00:00:00+00:00"

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


def env(tmp_path, monkeypatch, source_id=1):
    db_path = str(tmp_path / f"joint_coherence_{source_id}.db")
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
        "VALUES (?,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (source_id, _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,?,1,'mssql',3,'{}',?,?)",
        (source_id, _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,?,1,1,?)", (source_id, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, table_class="Transactional", row_count=1000, business_name=None, source_id=1):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash((source_id, table_fqn))) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,?,?,?,?,'COMPLETE',?,?,?,?)",
        (tid, source_id, table_fqn, name, schema, table_class, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (?,1,?,?,?,'TABLE',?,1,?,?,?)",
        (source_id, table_fqn, name, schema, business_name or name.capitalize(), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_col_seq = [100]


def _add_column(
    db, table_fqn, col_name, *,
    data_type="TEXT", is_pk=0, is_id=0, uniqueness=0.05,
    cardinality_tier="MEDIUM", is_metric=None, is_dimension=None, is_date=None,
    business_label=None, source_id=1,
):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,?,?,?,?,?,0,?,0,0,?,0,0,?,?)",
        (_col_seq[0], source_id, table_fqn, col_name, data_type, is_pk, uniqueness, cardinality_tier, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (?,1,?,?,?,?,?,?,?,0,1,?,?,?)",
        (source_id, table_fqn, col_name, business_label or col_name,
         int(bool(is_metric)), int(bool(is_dimension)), int(bool(is_date)), int(bool(is_id)),
         "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_rel_seq = [500]


def _add_fk(db, from_fqn, from_col, to_fqn, to_col, *, status="AUTO", confidence=1.0, source_id=1):
    c = _c(db)
    _rel_seq[0] += 1
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (?,?,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',?,'{}',?,?)",
        (_rel_seq[0], source_id, fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col,
         f"FK_{_rel_seq[0]}", confidence, _NOW, status),
    )
    c.commit()
    c.close()


def _seed_chain(db, source_id=1):
    """dbo.A -> dbo.B -> dbo.C (real, trusted FK chain) plus dbo.Island,
    which has no relationship to anything."""
    for fqn in ("dbo.A", "dbo.B", "dbo.C", "dbo.Island"):
        _add_table(db, fqn, source_id=source_id)
        _add_column(db, fqn, "ID", data_type="INTEGER", is_pk=1, is_id=1, uniqueness=1.0, source_id=source_id)
    _add_column(db, "dbo.A", "BID", data_type="INTEGER", uniqueness=0.5, source_id=source_id)
    _add_column(db, "dbo.B", "CID", data_type="INTEGER", uniqueness=0.5, source_id=source_id)
    _add_fk(db, "dbo.A", "BID", "dbo.B", "ID", source_id=source_id)
    _add_fk(db, "dbo.B", "CID", "dbo.C", "ID", source_id=source_id)


def _entry(term, table_fqn, score=1.0, name_score=None, candidates=None):
    selected = {"table_fqn": table_fqn, "column_name": None, "score": score, "name_score": name_score}
    return {
        "term": term, "selected": dict(selected),
        "candidates": candidates if candidates is not None else [dict(selected)],
        "warnings": [],
    }


def _warning_types(entry):
    return [w["type"] for w in entry.get("warnings", [])]


# ---------------------------------------------------------------------------
# 1. Disconnected high-score candidate rejected (no coherent alternative in
#    its own candidate pool).
# ---------------------------------------------------------------------------

def test_disconnected_high_score_candidate_rejected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    # anchor has the stronger NAME-relevance evidence (a real textual match)
    # even though disconnected has a higher raw `score` (built mostly from
    # generic authority bonuses, weak name evidence) -- the same shape as
    # the real regression, where a table's high overall score came from
    # authority bonuses rather than actually matching the term.
    anchor = _entry("x", "dbo.A", score=0.6, name_score=0.9)
    disconnected = _entry(
        "y", "dbo.Island", score=0.95, name_score=0.3,
        candidates=[{"table_fqn": "dbo.Island", "score": 0.95, "name_score": 0.3}],
    )
    _apply_joint_coherence([], [anchor, disconnected], [], 1, "u1")

    assert anchor["selected"]["table_fqn"] == "dbo.A"
    assert disconnected["selected"] is None
    assert "joint_coherence_rejected" in _warning_types(disconnected)


# ---------------------------------------------------------------------------
# 2. Connected slightly-lower candidate preferred over a disconnected top
#    pick, from the SAME term's own already-bounded candidate pool.
# ---------------------------------------------------------------------------

def test_connected_lower_score_candidate_preferred(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    anchor = _entry("x", "dbo.A", score=0.9, name_score=0.9)
    conflicted = _entry(
        "y", "dbo.Island", score=0.9, name_score=0.5,
        candidates=[
            {"table_fqn": "dbo.Island", "score": 0.9, "name_score": 0.5},
            {"table_fqn": "dbo.B", "score": 0.7, "name_score": 0.4},
        ],
    )
    _apply_joint_coherence([], [anchor, conflicted], [], 1, "u1")

    assert conflicted["selected"]["table_fqn"] == "dbo.B"
    assert "joint_coherence_reassigned" in _warning_types(conflicted)


# ---------------------------------------------------------------------------
# 3. Same-table terms remain valid (Part 4 — same-table coherence is always
#    valid, no relationship required).
# ---------------------------------------------------------------------------

def test_same_table_terms_remain_valid(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    e1 = _entry("x", "dbo.A", score=0.9, name_score=0.9)
    e2 = _entry("y", "dbo.A", score=0.5, name_score=0.5)
    _apply_joint_coherence([], [e1, e2], [], 1, "u1")

    assert e1["selected"]["table_fqn"] == "dbo.A"
    assert e2["selected"]["table_fqn"] == "dbo.A"
    assert e2["warnings"] == []


# ---------------------------------------------------------------------------
# 4. Coherent multi-hop path accepted (dbo.A -> dbo.B -> dbo.C, no direct
#    edge from A to C).
# ---------------------------------------------------------------------------

def test_coherent_multi_hop_path_accepted(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    anchor = _entry("x", "dbo.A", score=0.9, name_score=0.9)
    multihop = _entry("z", "dbo.C", score=0.6, name_score=0.6)
    _apply_joint_coherence([], [anchor, multihop], [], 1, "u1")

    assert multihop["selected"]["table_fqn"] == "dbo.C"
    assert multihop["warnings"] == []


# ---------------------------------------------------------------------------
# 5. Genuine no-path case safely refuses for the isolated term while leaving
#    the other, genuinely-connected terms correctly resolved.
# ---------------------------------------------------------------------------

def test_genuine_no_path_case_safely_refuses(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    anchor = _entry("x", "dbo.A", score=0.9, name_score=0.9)
    connected = _entry("y", "dbo.B", score=0.7, name_score=0.7)
    # Highest raw `score` of the three, but weakest name-relevance evidence
    # -- processed last, so it cannot preempt the anchor.
    isolated = _entry(
        "z", "dbo.Island", score=0.99, name_score=0.2,
        candidates=[{"table_fqn": "dbo.Island", "score": 0.99, "name_score": 0.2}],
    )
    _apply_joint_coherence([], [anchor, connected, isolated], [], 1, "u1")

    assert anchor["selected"]["table_fqn"] == "dbo.A"
    assert connected["selected"]["table_fqn"] == "dbo.B"
    assert isolated["selected"] is None


# ---------------------------------------------------------------------------
# 6. No global isolated-table penalty — an isolated table resolved as the
#    ONLY term (or as the anchor itself) is never rejected; the mechanism
#    only ever fires on a genuine 2+-term disagreement.
# ---------------------------------------------------------------------------

def test_no_global_isolated_table_penalty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    only_term = _entry("z", "dbo.Island", score=0.99, name_score=0.99)
    _apply_joint_coherence([], [only_term], [], 1, "u1")
    assert only_term["selected"]["table_fqn"] == "dbo.Island"
    assert only_term["warnings"] == []

    anchor_is_island = _entry("z", "dbo.Island", score=0.99, name_score=0.99)
    other = _entry("y", "dbo.B", score=0.1, name_score=0.1, candidates=[{"table_fqn": "dbo.B", "score": 0.1, "name_score": 0.1}])
    _apply_joint_coherence([], [anchor_is_island, other], [], 1, "u1")
    assert anchor_is_island["selected"]["table_fqn"] == "dbo.Island"
    # dbo.B has no trusted path to dbo.Island and no coherent alternative of
    # its own -> demoted, but the anchor itself (Island) is never penalized
    # just for being resolved first.
    assert other["selected"] is None


# ---------------------------------------------------------------------------
# 7. Source isolation — a relationship that exists on a DIFFERENT source_id
#    must never make an otherwise-disconnected table look coherent.
# ---------------------------------------------------------------------------

def test_source_isolation(tmp_path, monkeypatch):
    db1 = env(tmp_path, monkeypatch, source_id=1)
    _seed_chain(db1, source_id=1)
    # A second, unrelated source where a real relationship happens to exist
    # between two tables of the SAME name/FQN as source 1's disconnected pair.
    monkeypatch2_db = env(tmp_path, monkeypatch, source_id=2)
    _seed_chain(monkeypatch2_db, source_id=2)
    _add_fk(monkeypatch2_db, "dbo.Island", "ID", "dbo.A", "ID", source_id=2)

    anchor = _entry("x", "dbo.A", score=0.9, name_score=0.9)
    conflicted = _entry(
        "y", "dbo.Island", score=0.9, name_score=0.9,
        candidates=[{"table_fqn": "dbo.Island", "score": 0.9, "name_score": 0.9}],
    )
    _apply_joint_coherence([], [anchor, conflicted], [], source_id=1, user_id="u1")
    assert conflicted["selected"] is None, (
        "a relationship that only exists on source_id=2 must not make "
        "dbo.Island coherent on source_id=1"
    )


# ---------------------------------------------------------------------------
# 8. A concept-role entry only enters the coherence pool when its own term
#    also appears among measures/dimensions (a genuine cross-role duplicate
#    of the same phrase) — two standalone, unrelated concepts named in the
#    same question are informational and must not be forced to cohere.
#    Concepts are READ-ONLY coherence evidence even when they DO
#    participate: they can anchor the plan and cause a conflicting
#    measure/dimension to be rejected/reassigned (mirrors the real "user
#    accounts" concept-vs-measure fix), but are never themselves mutated —
#    a concept's own candidate pool is separately load-bearing evidence for
#    the orchestrator's ambiguity/clarification detection, which silent
#    reassignment would erase (live-confirmed regression).
# ---------------------------------------------------------------------------

def test_concept_participates_only_when_term_shared_with_measure_or_dimension(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_chain(db)

    # No shared term with any measure/dimension -> both concepts are
    # independent, standalone entries and must be left completely alone,
    # even though they disagree on table and have no relationship.
    concept_a = _entry("alpha", "dbo.A", score=0.9, name_score=0.9)
    concept_island = _entry("beta", "dbo.Island", score=0.9, name_score=0.9)
    _apply_joint_coherence([concept_a, concept_island], [], [], 1, "u1")
    assert concept_a["selected"]["table_fqn"] == "dbo.A"
    assert concept_island["selected"]["table_fqn"] == "dbo.Island"
    assert concept_island["warnings"] == []

    # Same shape, but "beta" ALSO appears as a measure term -- now it's a
    # genuine cross-role duplicate of the same phrase, so the (strongly-
    # evidenced) concept participates as coherence evidence and anchors the
    # plan; the conflicting, weakly-evidenced measure is rejected in favor
    # of it. The concept itself is left completely untouched either way.
    concept_beta = _entry("beta", "dbo.Island", score=0.95, name_score=0.95)
    measure_beta_conflict = _entry(
        "beta", "dbo.A", score=0.5, name_score=0.3,
        candidates=[{"table_fqn": "dbo.A", "score": 0.5, "name_score": 0.3}],
    )
    _apply_joint_coherence([concept_beta], [measure_beta_conflict], [], 1, "u1")
    assert concept_beta["selected"]["table_fqn"] == "dbo.Island"
    assert concept_beta["warnings"] == []
    assert measure_beta_conflict["selected"] is None, (
        "'beta' is dual-classified (also a concept term), so its measure "
        "resolution must be checked for coherence against the concept's "
        "stronger, anchoring evidence"
    )


# ---------------------------------------------------------------------------
# 8. Integration — the exact reported regression no longer confidently
#    resolves to an unrelated island table; the plan safely refuses instead.
# ---------------------------------------------------------------------------

def _seed_reference_with_island(db, source_id=1):
    _add_table(db, "dbo.EDU_Student", table_class="Master", row_count=12000, source_id=source_id)
    _add_table(db, "dnnuser.Users", table_class="Reference", row_count=50000, source_id=source_id)
    _add_table(db, "dbo.Island", table_class="Master", row_count=147, source_id=source_id,
               business_name="Island")

    _add_column(db, "dbo.EDU_Student", "StudentID", data_type="INTEGER", is_pk=1, is_id=1, uniqueness=1.0, source_id=source_id)
    _add_column(db, "dbo.EDU_Student", "StudentUserID", data_type="INTEGER", uniqueness=0.9, source_id=source_id)
    _add_column(db, "dbo.EDU_Student", "StartDate", data_type="DATETIME", is_date=True, source_id=source_id)

    _add_column(db, "dnnuser.Users", "UserID", data_type="INTEGER", is_pk=1, is_id=1, uniqueness=1.0, source_id=source_id)
    _add_column(db, "dnnuser.Users", "Username", data_type="TEXT", is_dimension=True, business_label="Username", source_id=source_id)

    # An unrelated island table with columns that superficially name-match
    # BOTH question terms, and no relationship to anything else — the exact
    # shape of the real production regression (dbo.SAMPLETBL).
    _add_column(db, "dbo.Island", "ID", data_type="INTEGER", is_pk=1, is_id=1, uniqueness=1.0, source_id=source_id)
    _add_column(db, "dbo.Island", "StudentProfile", data_type="TEXT", is_dimension=True, business_label="Student Profile", source_id=source_id)
    _add_column(db, "dbo.Island", "AccountBalance", data_type="DECIMAL", is_metric=True, business_label="Account Balance", source_id=source_id)

    _add_fk(db, "dbo.EDU_Student", "StudentUserID", "dnnuser.Users", "UserID", source_id=source_id)


def test_disconnected_island_no_longer_wins_multi_concept_question(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_reference_with_island(db)

    question = "Show students and their user accounts"
    concepts, measures, dimensions = extract_terms(question)
    query_plan = plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures, "dimensions": dimensions,
    })

    all_selected_tables = {
        e["selected"]["table_fqn"]
        for e in (query_plan["concepts"] + query_plan["measures"] + query_plan["dimensions"])
        if e.get("selected")
    }
    assert "dbo.Island" not in all_selected_tables

    sql_plan = build_sql_plan(1, "u1", query_plan)
    if sql_plan["validation"]["valid"]:
        # If evidence was strong enough to resolve confidently, it must be
        # the real, connected pair — never the island.
        involved = {sql_plan["from"]["table_fqn"]} | {
            j["left_table"] for j in sql_plan.get("joins") or []
        } | {j["right_table"] for j in sql_plan.get("joins") or []}
        assert "dbo.Island" not in involved
