"""
Tests for Sprint 1's AI Brain candidate-table retrieval
(data/semantic_retrieval_service.py) — the drop-in replacement for
_collect_candidate_tables() wired at data/query_planning_service.py:1111.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
test_phase9_query_planning.py. Since data.db.get_connection() re-reads
DB_PATH from data.db's module namespace at call time, patching
db_module.DB_PATH alone redirects every module's get_connection() call —
no per-module monkeypatch needed.

Run from the project root:
    venv/Scripts/pytest tests/test_semantic_retrieval_service.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-semantic-retrieval-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-semantic-retrieval-salt-long-enough-1234567")

import data.models as models
from data.semantic_retrieval_service import (
    _rank_domains,
    _select_domain_filter,
    _expand_relationships,
    _search_tables,
    _merge_best,
    get_candidate_tables,
)

_NOW = "2026-07-14T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _empty_snapshot_json():
    return json.dumps({
        "source_id": 1, "source_type": "mssql", "discovered_at": _NOW,
        "schemas": [], "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None, "warnings": [],
    })


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "semantic_retrieval.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    # A real (if initially empty) snapshot_json — not the '{}' placeholder —
    # so tests route through the discovery-anchored search_metadata path
    # (Sprint 1.1), matching how real CCPP data behaves, rather than the
    # legacy profiling_table_profiles-anchored SQL-LIKE-prefiltered path
    # (which predates Fix 1's singular/plural normalization and never
    # applies it before its own SQL WHERE clause runs). _add_table() below
    # appends each table it inserts into this same snapshot.
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',0,?,?,?)",
        (_empty_snapshot_json(), _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _register_in_snapshot(db, table_fqn):
    """Append table_fqn to source 1's schema_snapshots row so the
    discovery-anchored search path (Sprint 1.1) can see it."""
    schema_name, table_name = table_fqn.split(".", 1)
    c = _c(db)
    row = c.execute("SELECT snapshot_json FROM schema_snapshots WHERE id = 1").fetchone()
    data = json.loads(row["snapshot_json"])
    schemas = data.setdefault("schemas", [])
    target = next((s for s in schemas if s["schema_name"] == schema_name), None)
    if target is None:
        target = {"schema_name": schema_name, "tables": []}
        schemas.append(target)
    if not any(t["table_fqn"] == table_fqn for t in target["tables"]):
        target["tables"].append({
            "table_name": table_name, "schema_name": schema_name,
            "table_fqn": table_fqn, "table_type": "TABLE",
            "row_count_estimate": None, "columns": [], "primary_keys": [], "foreign_keys": [],
        })
    c.execute("UPDATE schema_snapshots SET snapshot_json = ? WHERE id = 1", (json.dumps(data),))
    c.commit()
    c.close()


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, domain=None, domain_confidence=0.0, business_name=None):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    _register_in_snapshot(db, table_fqn)
    c = _c(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,'Transactional','COMPLETE',1000,?,?)",
        (tid, table_fqn, name, schema, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,'TABLE',?,1,'rule_based',?,?)",
        (table_fqn, name, schema, business_name or name.capitalize(), _NOW, _NOW),
    )
    if domain:
        c.execute(
            "INSERT OR REPLACE INTO domain_assignments "
            "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, "
            " evidence_json, competing_domains_json, created_at, updated_at) "
            "VALUES (1,1,?,?,?,'[]','[]',?,?)",
            (table_fqn, domain, domain_confidence, _NOW, _NOW),
        )
    c.commit()
    c.close()


def _add_relationship(db, from_fqn, to_fqn, *, status="AUTO", rel_id=None):
    c = _c(db)
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, relationship_status, created_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?, 'FOREIGN_KEY', 1.0, ?, ?)",
        (
            rel_id or abs(hash((from_fqn, to_fqn))) % 100000,
            from_fqn.split(".")[0], from_fqn.split(".")[-1], from_fqn, "id",
            to_fqn.split(".")[0], to_fqn.split(".")[-1], to_fqn, "id",
            f"{from_fqn}->{to_fqn}", status, _NOW,
        ),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# Domain ranking gate
# ---------------------------------------------------------------------------

def test_domain_gate_strong_unambiguous_match_selects_domain():
    domain = _select_domain_filter("list all student enrollment records by course", ["student"])
    assert domain == "Student Lifecycle"


def test_domain_gate_weak_single_hit_stays_unfiltered():
    ranked = _rank_domains("show account details", ["account"])
    assert ranked and ranked[0]["confidence"] < 0.70
    assert _select_domain_filter("show account details", ["account"]) is None


def test_domain_gate_ambiguous_margin_stays_unfiltered(monkeypatch):
    import data.semantic_retrieval_service as svc
    monkeypatch.setattr(svc, "_DOMAIN_KEYWORDS", {
        "A": ["student", "course"],
        "B": ["enroll", "class"],
    })
    # Two hits each -> equal top confidence, margin 0.0 < _DOMAIN_MARGIN_MIN.
    assert _select_domain_filter("student course enroll class", ["student"]) is None


def test_domain_gate_no_keyword_match_returns_none():
    assert _select_domain_filter("xyzzy plugh", ["xyzzy"]) is None


# ---------------------------------------------------------------------------
# get_candidate_tables — end to end against the fixture DB
# ---------------------------------------------------------------------------

def test_get_candidate_tables_no_terms_returns_empty_set(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert get_candidate_tables(1, "u1", "anything", []) == set()


def test_get_candidate_tables_finds_domain_filtered_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Students", domain="Student Lifecycle", domain_confidence=0.9,
               business_name="Students")
    result = get_candidate_tables(
        1, "u1", "list all student enrollment records by course", ["student"],
    )
    assert "dbo.Students" in result


def test_get_candidate_tables_retries_unfiltered_when_domain_filter_empties_result(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Table is domain-assigned to Finance, but the question strongly (and
    # correctly, per its own wording) ranks Student Lifecycle — the domain
    # filter would find zero rows, so the retry-without-filter must recover
    # the match via plain keyword search instead of falling all the way back.
    _add_table(db, "dbo.Students", domain="Finance", domain_confidence=0.9,
               business_name="Students")
    result = get_candidate_tables(
        1, "u1", "list all student enrollment records by course", ["student"],
    )
    assert "dbo.Students" in result


def test_get_candidate_tables_returns_empty_when_nothing_matches(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Students", domain="Student Lifecycle", domain_confidence=0.9)
    result = get_candidate_tables(1, "u1", "unrelated gibberish query", ["gibberish"])
    assert result == set()


def test_get_candidate_tables_never_raises_on_search_failure(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    import data.semantic_retrieval_service as svc

    def _boom(*a, **kw):
        raise RuntimeError("search_metadata unavailable")

    monkeypatch.setattr(svc, "search_metadata", _boom)
    assert get_candidate_tables(1, "u1", "student enrollment", ["student"]) == set()


# ---------------------------------------------------------------------------
# Relationship expansion
# ---------------------------------------------------------------------------

def test_relationship_expansion_adds_one_hop_neighbor(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_relationship(db, "dbo.Enrollments", "dbo.Students", status="AUTO")
    result = _expand_relationships(1, {"dbo.Enrollments"})
    assert result == {"dbo.Students"}


def test_relationship_expansion_ignores_pending_status(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_relationship(db, "dbo.Enrollments", "dbo.Students", status="PENDING")
    result = _expand_relationships(1, {"dbo.Enrollments"})
    assert result == set()


def test_relationship_expansion_caps_at_ten(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    for i in range(15):
        _add_relationship(db, "dbo.Enrollments", f"dbo.Neighbor{i}", status="AUTO", rel_id=1000 + i)
    result = _expand_relationships(1, {"dbo.Enrollments"})
    assert len(result) == 10


def test_relationship_expansion_no_op_without_snapshot(tmp_path, monkeypatch):
    db_path = str(tmp_path / "no_snapshot.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    # No schema_snapshots row exists for source_id=1 -> must no-op, not raise.
    assert _expand_relationships(1, {"dbo.Enrollments"}) == set()


# ---------------------------------------------------------------------------
# Sprint 1.3 — candidate diversity (Problem 1) and domain rescue (Problem 2)
# ---------------------------------------------------------------------------

def _add_crowd(db, n, *, name_prefix="StudentRecord"):
    """n broad tables all matching "students incomplete" strongly via
    business_name — enough (n > 25) to fill the whole retrieval cap in a
    combined-question search, deliberately crowding out a narrower match."""
    for i in range(n):
        _add_table(
            db, f"dbo.{name_prefix}{i}",
            business_name=f"Incomplete Student Record {i}",
        )


def test_courses_enrollment_includes_both_course_and_enrollment_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Domain-assigned and text-matching "enrollment" — found by the base
    # domain-filtered search.
    _add_table(db, "dbo.Fact_ClassSignups", domain="Student Lifecycle", domain_confidence=0.9,
               business_name="Class Enrollment Signups")
    # NOT domain-assigned at all — excluded by the domain-filtered base
    # search; only the domain-rescue path can surface it.
    _add_table(db, "dbo.ADF_Course", business_name="Course")

    question = "Which courses have the highest enrollment?"
    result = get_candidate_tables(1, "u1", question, ["courses", "enrollment"])
    assert "dbo.Fact_ClassSignups" in result
    assert "dbo.ADF_Course" in result


def test_domain_matched_table_still_scores_at_or_above_rescued_table(tmp_path, monkeypatch):
    # White-box companion to the test above: proves the domain-native match
    # keeps its own (higher) score — rescue only adds candidates, it never
    # changes any table's relevance_score.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Fact_ClassSignups", domain="Student Lifecycle", domain_confidence=0.9,
               business_name="Class Enrollment Signups")
    _add_table(db, "dbo.ADF_Course", business_name="Course")

    domain = _select_domain_filter("Which courses have the highest enrollment?", ["courses", "enrollment"])
    assert domain == "Student Lifecycle"
    base = _search_tables("courses enrollment", 1, domain)
    rescue = _search_tables("courses", 1, None)
    base_score = next(r["relevance_score"] for r in base if r["qualified_name"] == "dbo.Fact_ClassSignups")
    rescue_score = next(r["relevance_score"] for r in rescue if r["qualified_name"] == "dbo.ADF_Course")
    assert base_score >= rescue_score


def test_students_incomplete_homework_includes_student_and_homework_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # business_name is the plural "Students" (not "Student") so this table
    # gets an exact-match hit on the "students" term specifically, clearly
    # outscoring the crowd (which only ties it via the normalized-singular
    # tier) — otherwise a same-score tie against 30 crowd rows makes the
    # per-term top-5 slice nondeterministic, which isn't what this test
    # means to exercise (that's a fixture-tie artifact, not a product bug).
    _add_table(db, "dbo.ADF_Student", business_name="Students")
    _add_table(db, "dbo.ADF_Homework", business_name="Homework Assignment")
    _add_crowd(db, 30)  # forces real crowding, see test below

    question = "Which students have incomplete homework?"
    result = get_candidate_tables(1, "u1", question, ["students", "incomplete", "homework"])
    assert "dbo.ADF_Student" in result
    assert "dbo.ADF_Homework" in result


def test_clients_active_jobs_includes_clean_client_and_job_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.ADF_Clients", business_name="Clients")
    _add_table(db, "dbo.ADF_Jobs", business_name="Jobs")

    question = "Which clients have the most active jobs?"
    result = get_candidate_tables(1, "u1", question, ["clients", "jobs"])
    assert "dbo.ADF_Clients" in result
    assert "dbo.ADF_Jobs" in result


def test_broad_plural_matches_do_not_consume_whole_candidate_budget(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.ADF_Homework", business_name="Homework Assignment")
    _add_crowd(db, 30)  # 30 > _RETRIEVAL_TABLE_LIMIT (25), all outscoring Homework

    terms = ["students", "incomplete", "homework"]
    question = "Which students have incomplete homework?"

    # Prove the crowding is real: the base combined-question search alone
    # (Sprint 1.2 behavior) fills every slot before Homework is reached.
    base = _search_tables(" ".join(terms), 1, None)
    base_fqns = {r["qualified_name"] for r in base}
    assert "dbo.ADF_Homework" not in base_fqns, "fixture must actually crowd out Homework for this test to be meaningful"

    result = get_candidate_tables(1, "u1", question, terms)
    assert "dbo.ADF_Homework" in result, "per-term diversity must recover it despite the crowd"
    assert len(result) <= 25


def test_rescue_candidates_cannot_exceed_their_cap(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Domain-assigned so the base (domain-filtered) search is non-empty —
    # this must NOT trigger the whole-query unfiltered retry, isolating the
    # per-term rescue cap specifically.
    _add_table(db, "dbo.ADF_StudentCore", domain="Student Lifecycle", domain_confidence=0.9,
               business_name="Student Core")
    # 8 tables that would all qualify for rescue (not domain-assigned, equally
    # strong text match) — more than _RESCUE_LIMIT (5).
    for i in range(8):
        _add_table(db, f"dbo.OtherStudentTable{i}", business_name=f"Student Registry {i}")

    question = "list all student enrollment records by course"
    domain = _select_domain_filter(question, ["student"])
    assert domain == "Student Lifecycle"

    result = get_candidate_tables(1, "u1", question, ["student"])
    assert "dbo.ADF_StudentCore" in result
    rescued = [fqn for fqn in result if fqn.startswith("dbo.OtherStudentTable")]
    assert len(rescued) <= 5


def test_merge_best_keeps_higher_score_on_collision():
    merged: dict[str, dict] = {"dbo.A": {"qualified_name": "dbo.A", "relevance_score": 100}}
    _merge_best(merged, [
        {"qualified_name": "dbo.A", "relevance_score": 50},   # lower — ignored
        {"qualified_name": "dbo.B", "relevance_score": 10},   # new — added
    ])
    assert merged["dbo.A"]["relevance_score"] == 100
    assert merged["dbo.B"]["relevance_score"] == 10
