"""
Tests for Enterprise Accuracy Safety A3.1 — Name-Relevance-Gated Authority
Scoring.

Root cause (A3 audit): _score_table_authority() previously granted its full
generic "this table is important somewhere in the schema" bonus (row count,
Master/root classification, relationship centrality, domain/entity
assignment) regardless of whether the table has ANY textual relevance to
the term being resolved — confirmed, against real production metadata, to
let framework/system tables (dnnuser.Users, aspnet_Users) outrank the
actual business-entity table for "interview"/"candidate". This suite tests
the new `name_score` parameter and gating formula directly.

Most tests are pure (no DB) — _score_table_authority() takes a plain dict,
following the exact fixture pattern already established by
tests/test_phase9_query_planning.py's `_authority_ctx()`. Two tests are
DB-backed integration tests reusing the env()/_add_table()/_add_column()
convention from tests/test_compound_phrase_resolution.py.

Run from the project root:
    python -m pytest tests/test_name_relevance_authority_gate.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-a3-1-authority-gate-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-a3-1-authority-gate-salt-long-enough-123")

from data.query_planning_service import (
    _score_table_authority, _resolve_concept,
    _AUTO_SELECT_MIN_CONFIDENCE, _AMBIGUITY_MARGIN, _NAME_RELEVANCE_GATE_FLOOR,
)


# ---------------------------------------------------------------------------
# Pure unit tests — hand-built ctx dicts, no DB.
# ---------------------------------------------------------------------------

def _ctx(*, domain_confidence=0.9, entity_confidence=0.9, row_count=30000,
         is_root_table=False, table_class=None, is_junction_table=False,
         table_type="TABLE", dictionary_approved=False):
    """A deliberately high-generic-authority ctx — high row count, confident
    domain/entity assignment, optionally root/Master/junction — so every
    test below isolates the name_score gate's effect, not evidence strength."""
    return {
        "dictionary": None,
        "domain": {"domain": "Test Domain", "confidence": domain_confidence},
        "entity": {"entity": "Test Entity", "confidence": entity_confidence},
        "profiling": {
            "exact_row_count": row_count,
            "is_root_table": is_root_table,
            "table_class": table_class,
            "is_junction_table": is_junction_table,
        },
        "relationships": {"outbound": ["a", "b"], "inbound": ["c"]},
        "relationship_evidence": [],
        "governance": {
            "domain_assigned": True, "entity_assigned": True,
            "dictionary_approved": dictionary_approved,
        },
        "table": {"table_type": table_type},
    }


def test_zero_name_relevance_strongly_suppresses_positive_bonus():
    """(1) A zero-name-relevance, high-authority table must have its
    positive authority bonus strongly suppressed relative to the same
    evidence with full relevance."""
    unsuppressed = _score_table_authority("dbo.widgets", _ctx(), name_score=1.0)
    suppressed = _score_table_authority("dbo.widgets", _ctx(), name_score=0.0)
    assert suppressed["bonus"] < unsuppressed["bonus"] * 0.10
    assert suppressed["bonus"] < 0.05


def test_semantically_relevant_table_retains_normal_contribution():
    """(2) A table at or above the relevance floor keeps its full,
    unscaled authority bonus — identical to omitting name_score entirely."""
    at_floor = _score_table_authority("dbo.widgets", _ctx(), name_score=_NAME_RELEVANCE_GATE_FLOOR)
    above_floor = _score_table_authority("dbo.widgets", _ctx(), name_score=0.9)
    no_name_score = _score_table_authority("dbo.widgets", _ctx())
    assert at_floor["bonus"] == above_floor["bonus"] == no_name_score["bonus"]


def test_partial_relevance_scales_linearly_between_zero_and_floor():
    """Below the floor, the bonus scales proportionally with name_score —
    not an all-or-nothing cliff."""
    quarter = _score_table_authority("dbo.widgets", _ctx(), name_score=_NAME_RELEVANCE_GATE_FLOOR / 4)
    half = _score_table_authority("dbo.widgets", _ctx(), name_score=_NAME_RELEVANCE_GATE_FLOOR / 2)
    full = _score_table_authority("dbo.widgets", _ctx(), name_score=_NAME_RELEVANCE_GATE_FLOOR)
    assert 0 < quarter["bonus"] < half["bonus"] < full["bonus"]


def test_junction_table_lowers_score_vs_identical_non_junction():
    """(3) is_junction_table must lower the score relative to an otherwise
    identical non-junction candidate — a signal that exists in profiling
    but was never previously read by any ranking function."""
    non_junction = _score_table_authority(
        "dbo.widgets", _ctx(table_class="Transactional", is_junction_table=False), name_score=0.9,
    )
    junction = _score_table_authority(
        "dbo.widgets", _ctx(table_class="Transactional", is_junction_table=True), name_score=0.9,
    )
    assert junction["bonus"] < non_junction["bonus"]
    assert any("Junction" in r for r in junction["reasons"])


def test_root_and_master_signals_remain_bounded():
    """(4) The new root/master structural bonuses (+0.06/+0.05) never push
    the final score outside the existing [-0.5, 0.5] clamp, and their own
    direct contribution (isolated from the pre-existing, ×0.30-discounted
    importance-term overlap and row-count bonus, which are held identical
    between the two calls) stays small relative to the overall range."""
    plain = _score_table_authority("dbo.widgets", _ctx(row_count=30000), name_score=0.9)
    root_master = _score_table_authority(
        "dbo.widgets", _ctx(is_root_table=True, table_class="Master", row_count=30000), name_score=0.9,
    )
    assert root_master["bonus"] <= 0.5
    # Delta includes the new direct +0.06/+0.05 terms PLUS the pre-existing
    # importance-term's own root(+0.20)/Master(+0.20) contribution at its
    # 0.30 discount (+0.12) — i.e. the entire root/master signal, old and
    # new combined, not just the new top-up — bounded well under half the
    # total [-0.5, 0.5] range.
    assert root_master["bonus"] - plain["bonus"] <= 0.25


def test_framework_system_table_cannot_manufacture_confident_winner():
    """(5) A framework/system-style table (near-zero name relevance, but
    strong generic authority evidence — high row count, Master class, many
    relationships, confident domain/entity assignment) must not out-total a
    modestly-matched real business table, mirroring the confirmed real
    dnnuser.Users/aspnet_Users cases."""
    framework_table = _ctx(
        domain_confidence=1.0, entity_confidence=1.0, row_count=40000,
        is_root_table=True, table_class="Master",
    )
    business_table = _ctx(domain_confidence=0.88, entity_confidence=0.85, row_count=None)

    framework_score = 0.0 + _score_table_authority("dnnuser.Users", framework_table, name_score=0.0)["bonus"]
    business_score = 0.3333 + _score_table_authority("dbo.EDU_StaffingCandidates", business_table, name_score=0.3333)["bonus"]
    assert business_score > framework_score


def test_already_correct_strongly_named_table_remains_ranked_first():
    """(6) A table that already wins today (strong name match) must remain
    ranked first and must not be penalized by the new gate."""
    strong_match = _score_table_authority("dbo.EDU_Student", _ctx(row_count=12000), name_score=1.0)
    weak_decoy = _score_table_authority("dbo.SomeDecoy", _ctx(row_count=None, domain_confidence=0.5, entity_confidence=0.5), name_score=0.1)
    assert (1.0 + strong_match["bonus"]) > (0.1 + weak_decoy["bonus"])


def test_ambiguity_thresholds_unchanged():
    """(7) Existing ambiguity thresholds are literally unchanged by this
    milestone."""
    assert _AUTO_SELECT_MIN_CONFIDENCE == 0.5
    assert _AMBIGUITY_MARGIN == 0.15


def test_no_name_score_argument_preserves_exact_prior_behavior():
    """Backward compatibility: any caller not supplying name_score (every
    pre-A3.1 call site, plus data/dictionary_curation_service.py) must see
    byte-identical behavior to before this change."""
    ctx = _ctx(is_root_table=True, table_class="Master", is_junction_table=False)
    omitted = _score_table_authority("dbo.widgets", ctx)
    explicit_full = _score_table_authority("dbo.widgets", ctx, name_score=1.0)
    assert omitted["bonus"] == explicit_full["bonus"]


# ---------------------------------------------------------------------------
# DB-backed integration tests — reuses the env()/_add_table()/_add_column()
# convention from tests/test_compound_phrase_resolution.py.
# ---------------------------------------------------------------------------

_NOW = "2026-06-30T00:00:00+00:00"
_PATCHED_MODULES = (
    "data.query_planning_service", "data.knowledge_graph_service",
    "data.business_knowledge_service", "data.semantic_layer_service",
)


def _db_conn(path):
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    import data.models as models
    db_path = str(tmp_path / "authority_gate.db")
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


def _add_table(db, table_fqn, *, table_class="Transactional", row_count=1000, approved=True):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _db_conn(db)
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


def _add_column(db, table_fqn, col_name, *, data_type="INTEGER", is_pk=0, uniqueness=1.0):
    c = _db_conn(db)
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,0,?,0,0.0,'MEDIUM',0,0,?,?)",
        (abs(hash((table_fqn, col_name))) % 100000, table_fqn, col_name, data_type, is_pk, uniqueness, _NOW, _NOW),
    )
    c.commit()
    c.close()


def test_safe_refusal_remains_safe_where_evidence_is_weak(tmp_path, monkeypatch):
    """(8) Two genuinely comparable business tables sharing similar evidence
    must still correctly refuse — the gate must not manufacture false
    confidence where none existed before."""
    env(tmp_path, monkeypatch)
    _add_table(tmp_path / "authority_gate.db", "dbo.customers_a", row_count=1000, approved=True)
    _add_column(tmp_path / "authority_gate.db", "dbo.customers_a", "id", is_pk=1)
    _add_table(tmp_path / "authority_gate.db", "dbo.customers_b", row_count=1000, approved=True)
    _add_column(tmp_path / "authority_gate.db", "dbo.customers_b", "id", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.customers_a", "dbo.customers_b")
    }
    result = _resolve_concept("client", table_contexts, {}, {})
    assert result["resolved"] is False
    assert result["ambiguity_reason"] is not None


def test_previously_correct_fixture_does_not_flip_to_unrelated_table(tmp_path, monkeypatch):
    """(9) The pre-existing "job order" targeted fixture (same scenario as
    tests/test_phase9_query_planning.py::test_source_specific_vocabulary_job_order_resolves_via_synonym_group)
    must still resolve to the correct table, not flip to the unrelated
    decoy, now that authority scoring is name-relevance-gated."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.edu_openings", table_class="Master", row_count=10000, approved=True)
    _add_column(db, "dbo.edu_openings", "id", is_pk=1)
    _add_table(db, "dbo.edu_payment_invoices", table_class="Transactional", row_count=500, approved=False)
    _add_column(db, "dbo.edu_payment_invoices", "id", is_pk=1)

    from data.query_planning_service import plan_business_query
    result = plan_business_query(1, "u1", {"question": "job order", "concepts": ["job order"]})
    concept = result["concepts"][0]
    assert concept["resolved"] is True
    assert concept["selected"]["table_fqn"] == "dbo.edu_openings"


# ---------------------------------------------------------------------------
# Phase 4, Milestone 1 — Name-Relevance Gate PARITY for _resolve_term().
#
# Confirmed defect (Phase 4 failure audit, 2026-08-04): _resolve_concept()
# and _resolve_count_all() above both already pass their per-candidate
# name_score into _score_table_authority() (see tests 1-9). _resolve_term()
# — used for EVERY measure/dimension term, i.e. every aggregation, ranking,
# and dimension/grouping resolution in the whole planner — computes ONE
# _score_table_authority(table_fqn, ctx) per TABLE with no name_score at
# all, before its own per-COLUMN loop even starts. So the exact gate these
# tests 1-9 validate never engages for measure/dimension resolution: a
# column on a high-authority-but-textually-irrelevant table (big audit/
# system table with many rows, confident domain/entity assignment) can
# still outrank, or fraudulently tie, a column that is a direct, obvious
# name match on a smaller, less "important" table. Traced against the real
# the reference database acceptance suite, this was the single largest failure cluster
# (18 of 48 failing questions) — e.g. "payroll"/"recruiters"/"placed"/
# "candidate" all independently "resolving" to an ASP.NET membership table
# purely on row-count/relationship authority, name_score=0.0 throughout.
#
# Fixtures below reuse the exact same _ctx()-style values already proven
# correct at the _score_table_authority level by test 5
# (test_framework_system_table_cannot_manufacture_confident_winner) —
# these tests just prove that _resolve_term(), which sits one layer above
# _score_table_authority() and is the only remaining ungated caller, now
# also produces the correct outcome end-to-end.
# ---------------------------------------------------------------------------

def _dim_col(name):
    return {"column_name": name, "dictionary": {"is_dimension": True}, "schema": {"data_type": "VARCHAR"}, "profiling": {}}


def _metric_col(name):
    return {"column_name": name, "dictionary": {"is_metric": True}, "schema": {"data_type": "DECIMAL"}, "profiling": {}}


def _no_relationships(ctx: dict) -> dict:
    """_ctx()'s default relationships are plain strings (["a", "b"]), which
    is fine for the pure _score_table_authority tests above (it only counts
    len()) but crashes _related_table_names — a real dict-shaped field —
    once a ctx flows through _resolve_term()/_score_candidates(). Tests
    below don't exercise relationship-centrality evidence, so relationships
    are simply cleared rather than reshaped into fake dicts."""
    ctx["relationships"] = {"outbound": [], "inbound": []}
    return ctx


def _high_authority_irrelevant_ctx(row_count=1000000):
    """A big, well-governed, root/Master table whose column is textually
    unrelated to whatever term is being resolved — the confirmed real
    pattern (framework/audit/membership tables) that must not win purely
    on generic importance."""
    return _no_relationships(_ctx(
        domain_confidence=1.0, entity_confidence=1.0, row_count=row_count,
        is_root_table=True, table_class="Master",
    ))


def _weakly_governed_relevant_ctx(row_count=None):
    """A small table whose column is a direct, obvious name match —
    deliberately given zero-confidence domain/entity assignment and no row
    count, so any win it gets comes from name relevance, not from competing
    authority evidence."""
    return _no_relationships(_ctx(
        domain_confidence=0.0, entity_confidence=0.0, row_count=row_count,
    ))


def test_measure_resolution_gates_authority_by_column_name_score():
    """(10) MEASURE resolution: a high-authority table with a column bearing
    zero textual relevance to the term ("UserID" vs "candidates") must not
    out-rank, or manufacture a false tie against, a modestly-governed table
    whose column is a direct name match ("CandidateCount"). Before this
    fix, the two candidates land 0.1384 apart — under the 0.15 ambiguity
    margin — turning an unambiguous question into a false clarification."""
    from data.query_planning_service import _resolve_term

    irrelevant = _high_authority_irrelevant_ctx()
    irrelevant["columns"] = [_metric_col("UserID")]
    relevant = _weakly_governed_relevant_ctx()
    relevant["columns"] = [_metric_col("CandidateCount")]

    table_contexts = {"dnnuser.system_accounts": irrelevant, "dbo.applicant_tracking": relevant}
    result = _resolve_term("candidates", table_contexts, "measure", {}, {})

    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.applicant_tracking"
    assert result["selected"]["column_name"] == "CandidateCount"
    # The irrelevant table's column must score at or near zero once gated —
    # not merely "lose", but lose decisively, matching _score_table_authority's
    # own contract (test 1 above).
    irrelevant_candidate = next(c for c in result["candidates"] if c["table_fqn"] == "dnnuser.system_accounts")
    assert irrelevant_candidate["authority_bonus"] < 0.05


def test_dimension_resolution_gates_authority_by_column_name_score():
    """(11) DIMENSION resolution: same defect, same fix, for the dimension
    role. Pre-fix, a 1,000,000-row root/Master table's "Notes" column
    (name_score=0.0 for "department") ties exactly at 0.5 against a small,
    ungoverned table's "DepartmentName" column (name_score=0.5) — both
    candidates hit the identical authority cap (0.5 ungated vs. 0.5 from
    pure name match), so the plan reports "ambiguous_dimension" and asks
    the user to disambiguate a question that was never actually ambiguous.
    Post-fix, the irrelevant table's authority collapses toward zero and
    "department" resolves outright."""
    from data.query_planning_service import _resolve_term

    irrelevant = _high_authority_irrelevant_ctx()
    irrelevant["columns"] = [_dim_col("Notes")]
    relevant = _weakly_governed_relevant_ctx()
    relevant["columns"] = [_dim_col("DepartmentName")]

    table_contexts = {"dbo.audit_log": irrelevant, "dbo.hr_department": relevant}
    result = _resolve_term("department", table_contexts, "dimension", {}, {})

    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.hr_department"
    assert result["selected"]["column_name"] == "DepartmentName"
    assert not any(str(w.get("type", "")).startswith("ambiguous_") for w in result["warnings"])


def test_filter_dimension_resolution_gates_authority_by_column_name_score():
    """(12) The dimension role IS this codebase's filter-column resolution
    path — a status/category dimension term ("region") used to filter or
    group results resolves through the exact same _resolve_term(kind=
    "dimension") call as test 11, not a separate scorer. (Explicit
    request-supplied `filters=[...]` and the query-intent-derived status/
    date auto-filters in plan_business_query — _find_status_column/
    _find_date_column — do direct dictionary-flag column lookup with no
    _score_table_authority call at all, so there is no separate gate to
    test for those two paths; this test is the complete coverage for
    name_score-gated filter-relevant column resolution.) Same fixture
    shape as test 11, different term/columns, confirming the fix isn't
    coincidentally specific to "department"."""
    from data.query_planning_service import _resolve_term

    irrelevant = _high_authority_irrelevant_ctx()
    irrelevant["columns"] = [_dim_col("InternalFlag")]
    relevant = _weakly_governed_relevant_ctx()
    relevant["columns"] = [_dim_col("RegionName")]

    table_contexts = {"dbo.system_config": irrelevant, "dbo.client_regions": relevant}
    result = _resolve_term("region", table_contexts, "dimension", {}, {})

    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.client_regions"
    assert result["selected"]["column_name"] == "RegionName"


def test_resolve_concept_and_resolve_count_all_unaffected_by_resolve_term_fix():
    """(13) Non-regression: _resolve_concept() and _resolve_count_all() are
    not touched by the _resolve_term() fix and must keep producing byte-
    identical bonuses for the same ctx/name_score inputs tests 1-9 already
    established — this fix is additive to _resolve_term() only."""
    from data.query_planning_service import _resolve_concept, _resolve_count_all

    ctx = _high_authority_irrelevant_ctx()
    table_contexts = {"dnnuser.Users": ctx}

    concept_result = _resolve_concept("widgets", table_contexts, {}, {})
    count_result = _resolve_count_all("widgets", table_contexts, {}, {})

    # Same assertions test 5/1 already make at the _score_table_authority
    # level, now confirmed unchanged at the _resolve_concept/_resolve_count_all
    # call sites themselves.
    assert concept_result["candidates"][0]["authority_bonus"] < 0.05
    assert count_result["candidates"][0]["authority_bonus"] < 0.05


def test_candidate_ordering_remains_deterministic():
    """(14) _resolve_term()'s candidate list must stay sorted strictly by
    rank (name_score + authority_bonus) descending, and identical across
    repeated calls, after authority scoring moves from once-per-table to
    once-per-candidate."""
    from data.query_planning_service import _resolve_term

    strong = _no_relationships(_ctx(row_count=5000))
    strong["columns"] = [_metric_col("RevenueAmount")]
    medium = _no_relationships(_ctx(row_count=100, domain_confidence=0.2, entity_confidence=0.2))
    medium["columns"] = [_metric_col("RevenueTotal")]
    weak = _weakly_governed_relevant_ctx(row_count=10)
    weak["columns"] = [_metric_col("Amount")]

    table_contexts = {"dbo.a": strong, "dbo.b": medium, "dbo.c": weak}
    first = _resolve_term("revenue", table_contexts, "measure", {}, {})
    second = _resolve_term("revenue", table_contexts, "measure", {}, {})

    scores_first = [c["score"] for c in first["candidates"]]
    assert scores_first == sorted(scores_first, reverse=True)
    assert scores_first[0] > scores_first[1] > scores_first[2]
    assert [c["table_fqn"] for c in first["candidates"]] == [c["table_fqn"] for c in second["candidates"]]


# ---------------------------------------------------------------------------
# LIVE the reference database VERIFICATION MILESTONE, Part 3 — "no name/label column" fallback
# junction-shape signal.
#
# Root cause, live-confirmed against a real production database: bare "program" resolved to
# dbo.EDU_Mentoring_Program (a per-student mentor ASSIGNMENT table — MentorID/
# InstructorID/StudentUserID/ClassSignupsID/IsActive/MentorGrpID/CreateDate/
# UpdateDate, no name/label column at all) instead of dbo.EDU_Track (the real
# curriculum catalog — TrackName/TrackDesc/CertificateName). profiling.
# is_junction_table SHOULD have penalized the former, but it requires
# declared-FK evidence (fk_count), which this schema's tables essentially
# never have (every real edge is inferred via STRUCTURAL_PK_NAME_MATCH/
# business_key, never a declared FK constraint) -- so is_junction_table was
# False for BOTH tables, and the penalty never engaged. This new signal is
# entirely column-dictionary-based (semantic_type), no declared-FK
# dependency, so it works on this exact class of schema.
# ---------------------------------------------------------------------------

def _id_col(name):
    return {"column_name": name, "dictionary": {"semantic_type": "id"}, "schema": {"data_type": "INTEGER"}, "profiling": {}}


def _date_col(name):
    return {"column_name": name, "dictionary": {"semantic_type": "date"}, "schema": {"data_type": "DATETIME"}, "profiling": {}}


def _flag_col(name):
    return {"column_name": name, "dictionary": {"semantic_type": "flag"}, "schema": {"data_type": "BOOLEAN"}, "profiling": {}}


def _named_dimension_col(name):
    return {"column_name": name, "dictionary": {"semantic_type": "dimension"}, "schema": {"data_type": "VARCHAR"}, "profiling": {}}


def test_all_id_shaped_columns_penalized_as_unlikely_named_entity():
    # Mirrors dbo.EDU_Mentoring_Program's real column shape: every
    # classified column is id/date/flag -- no genuine label/name column.
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [_id_col("MentorID"), _id_col("InstructorID"), _id_col("StudentUserID"),
                       _flag_col("IsActive"), _date_col("CreateDate")]
    penalized = _score_table_authority("dbo.EDU_Mentoring_Program", ctx, name_score=1.0)
    assert any("No name/label-type column found" in r for r in penalized["reasons"])


def test_table_with_a_named_dimension_column_is_not_penalized():
    # Mirrors dbo.EDU_Track's real column shape: TrackName/TrackDesc/
    # CertificateName are all dictionary semantic_type='dimension'.
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [_id_col("TrackID"), _named_dimension_col("TrackName"),
                       _named_dimension_col("TrackDesc"), _date_col("InsertDate")]
    not_penalized = _score_table_authority("dbo.EDU_Track", ctx, name_score=1.0)
    assert not any("No name/label-type column found" in r for r in not_penalized["reasons"])


def test_no_label_column_penalty_lowers_score_relative_to_labeled_candidate():
    # The actual competitive effect: given otherwise-IDENTICAL generic
    # authority evidence, the labeled candidate must score higher.
    junction_like = _ctx(is_junction_table=False)
    junction_like["columns"] = [_id_col("MentorID"), _id_col("InstructorID"), _id_col("StudentUserID"),
                                  _flag_col("IsActive"), _date_col("CreateDate")]
    catalog_like = _ctx(is_junction_table=False)
    catalog_like["columns"] = [_id_col("TrackID"), _named_dimension_col("TrackName"),
                                 _named_dimension_col("TrackDesc"), _date_col("InsertDate")]

    junction_score = _score_table_authority("dbo.EDU_Mentoring_Program", junction_like, name_score=1.0)
    catalog_score = _score_table_authority("dbo.EDU_Track", catalog_like, name_score=1.0)
    assert catalog_score["bonus"] > junction_score["bonus"]


def test_under_profiled_table_with_few_columns_not_penalized():
    # Part 13's "don't flag what you don't have evidence for" discipline
    # applied to this signal too: fewer than 4 classified columns means
    # not enough evidence to judge the table's shape -- must not penalize.
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [_id_col("SomeID")]
    result = _score_table_authority("dbo.sparse_table", ctx, name_score=1.0)
    assert not any("No name/label-type column found" in r for r in result["reasons"])


def _person_ref_col(name):
    return {"column_name": name, "dictionary": {"semantic_type": "id"}, "schema": {"data_type": "INTEGER"}, "profiling": {}}


def _loose_text_col(name):
    # Mirrors the real regression: an incidental text field (an alias, not
    # a genuine catalog name) that dictionary generation happens to
    # classify as "dimension" purely because it's not id/date/flag/metric.
    return {"column_name": name, "dictionary": {"semantic_type": "dimension"}, "schema": {"data_type": "VARCHAR"}, "profiling": {}}


def test_incidental_non_label_text_column_no_longer_escapes_the_penalty():
    # The exact live regression: dbo.EDU_InternshipProgram survived the
    # prior milestone's check purely because InternBaseCampAlis classified
    # as "dimension" despite not being a genuine label/name column.
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [
        _person_ref_col("InternUserID"), _person_ref_col("InternManagerID"),
        _id_col("InternID"), _loose_text_col("InternBaseCampAlis"),
        _flag_col("InternIsActive"), _date_col("InternStartDate"),
    ]
    result = _score_table_authority("dbo.EDU_InternshipProgram", ctx, name_score=1.0)
    assert any("No name/label-type column found" in r for r in result["reasons"])


def test_person_reference_column_penalized_as_assignment_evidence():
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [_person_ref_col("StudentUserID"), _person_ref_col("InstructorID"),
                       _id_col("MentorID"), _flag_col("IsActive"), _date_col("CreateDate")]
    result = _score_table_authority("dbo.EDU_Mentoring_Program", ctx, name_score=1.0)
    assert any("person-reference column" in r for r in result["reasons"])


def test_catalog_table_with_no_person_reference_unaffected():
    ctx = _ctx(is_junction_table=False)
    ctx["columns"] = [_id_col("TrackID"), _named_dimension_col("TrackName"),
                       _named_dimension_col("TrackDesc"), _date_col("InsertDate")]
    result = _score_table_authority("dbo.EDU_Track", ctx, name_score=1.0)
    assert not any("person-reference column" in r for r in result["reasons"])
    assert not any("No name/label-type column found" in r for r in result["reasons"])


def test_catalog_beats_both_assignment_tables_with_incidental_text_columns():
    # The full, decisive competitive proof: EDU_Track must now outscore
    # BOTH dbo.EDU_Mentoring_Program and dbo.EDU_InternshipProgram even
    # though the latter has an incidental text column.
    path_ctx = _ctx(is_junction_table=False)
    path_ctx["columns"] = [_id_col("TrackID"), _named_dimension_col("TrackName"),
                             _named_dimension_col("TrackDesc"), _date_col("InsertDate")]
    mentorship_ctx = _ctx(is_junction_table=False)
    mentorship_ctx["columns"] = [_person_ref_col("StudentUserID"), _person_ref_col("InstructorID"),
                                   _id_col("MentorID"), _flag_col("IsActive"), _date_col("CreateDate")]
    internship_ctx = _ctx(is_junction_table=False)
    internship_ctx["columns"] = [
        _person_ref_col("InternUserID"), _person_ref_col("InternManagerID"),
        _id_col("InternID"), _loose_text_col("InternBaseCampAlis"),
        _flag_col("InternIsActive"), _date_col("InternStartDate"),
    ]

    path_score = _score_table_authority("dbo.EDU_Track", path_ctx, name_score=1.0)
    mentorship_score = _score_table_authority("dbo.EDU_Mentoring_Program", mentorship_ctx, name_score=1.0)
    internship_score = _score_table_authority("dbo.EDU_InternshipProgram", internship_ctx, name_score=1.0)

    assert path_score["bonus"] > mentorship_score["bonus"]
    assert path_score["bonus"] > internship_score["bonus"]


def test_real_is_junction_table_flag_still_takes_priority_and_is_not_doubled():
    # When profiling DID confirm is_junction_table=True (declared-FK schema),
    # the original penalty applies and the new fallback signal must not
    # ALSO fire (no double-penalizing the same table).
    ctx = _ctx(is_junction_table=True)
    ctx["columns"] = [_id_col("A"), _id_col("B"), _id_col("C"), _id_col("D")]
    result = _score_table_authority("dbo.real_junction", ctx, name_score=1.0)
    assert any("Junction/bridge table" in r for r in result["reasons"])
    assert not any("No name/label-type column found" in r for r in result["reasons"])
