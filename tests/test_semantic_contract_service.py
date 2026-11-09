"""
Day 2B, Task 1/2 — data.semantic_contract_service.

Part 1: match_entities_for_terms / match_entity_for_terms (pure, no DB).
Discovery/persistence/cache tests are added alongside their implementation.

Run:
    python -m pytest tests/test_semantic_contract_service.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2b-semantic-contract-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-day2b-semantic-contract-salt-long-enough12")

import data.semantic_retrieval_service as semantic_retrieval_service
from data.semantic_contract_service import (
    ENTITY_TAXONOMY,
    TARGET_ENTITIES,
    apply_grounding,
    current_metadata_revision,
    discover_entity_contract,
    generate_all_entity_contracts,
    get_latest_entity_contract,
    get_or_build_entity_contract,
    match_entities_for_terms,
    match_entity_for_terms,
    persist_entity_contract,
    specialize_entity_term,
)
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain, _add_value_samples


def test_all_ten_target_entities_present():
    assert set(TARGET_ENTITIES) == {
        "Student", "Enrollment", "Course", "Candidate", "Recruiter",
        "Placement", "Client", "Job Order", "Invoice", "Pilot Participant",
    }
    for entity, synonyms in ENTITY_TAXONOMY.items():
        assert synonyms, f"{entity} has no synonyms"


def test_single_word_entity_matches_via_term_list():
    assert match_entity_for_terms("Active invoices", ["invoices"], ["invoices"], []) == "Invoice"


def test_single_word_entity_matches_singular_and_plural():
    assert match_entity_for_terms("Show client", ["client"], ["client"], []) == "Client"
    assert match_entity_for_terms("Show clients", ["clients"], ["clients"], []) == "Client"


def test_multiword_entity_matches_via_raw_question_text():
    # extract_terms splits "job orders" into ['job', 'orders'] — neither
    # token alone matches the "Job Order" synonym via the term list, so the
    # raw-question-text path is what has to catch this.
    result = match_entity_for_terms("List open job orders", ["job", "orders"], ["job", "orders"], [])
    assert result == "Job Order"


def test_no_match_returns_none():
    assert match_entity_for_terms("Show widgets", ["widgets"], ["widgets"], []) is None


def test_multi_entity_question_returns_both_in_order():
    matched = match_entities_for_terms(
        "Show recruiters and their placements",
        ["recruiters", "placements"], ["recruiters", "placements"], [],
    )
    assert matched == ["Recruiter", "Placement"]


def test_three_entity_question_returns_all_three_in_order():
    matched = match_entities_for_terms(
        "Show students, their enrollments, and course details",
        ["students", "enrollments", "course"], ["students", "enrollments", "course"], [],
    )
    assert matched == ["Student", "Enrollment", "Course"]


def test_relationship_question_clients_and_job_orders():
    matched = match_entities_for_terms(
        "Which clients have open job orders?",
        ["clients", "job", "orders"], ["clients", "job", "orders"], [],
    )
    assert matched == ["Client", "Job Order"]


def test_candidates_and_placements_relationship_question():
    matched = match_entities_for_terms(
        "Show candidates linked to active placements",
        ["candidates", "placements"], ["candidates", "placements"], [],
    )
    assert matched == ["Candidate", "Placement"]


def test_unrelated_word_does_not_false_positive():
    # "class" must not spuriously match "Course" via a loose substring —
    # only genuine near-match tokens (singular/plural of the same root).
    assert match_entity_for_terms("Show recent classes", ["classes"], ["classes"], []) is None


# ---------------------------------------------------------------------------
# Persistence — versioning, latest-read, metadata-revision fingerprint
# ---------------------------------------------------------------------------

def test_persist_and_read_latest_contract(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    result = persist_entity_contract(
        1, "Invoice",
        canonical_table_fqn="dnnuser.FIN_Invoice", preferred_view_fqn=None,
        resolution_status="PARTIAL", confidence=0.6,
        contract_json={"synonyms": ["invoice", "invoices"], "status_semantics": {}},
        metadata_revision="1:1",
    )
    assert result["contract_version"] == 1

    latest = get_latest_entity_contract(1, "Invoice")
    assert latest is not None
    assert latest["canonical_table_fqn"] == "dnnuser.FIN_Invoice"
    assert latest["resolution_status"] == "PARTIAL"
    assert latest["contract"]["synonyms"] == ["invoice", "invoices"]


def test_rebuild_inserts_new_version_never_overwrites(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    persist_entity_contract(
        1, "Client", canonical_table_fqn="dbo.old_clients", preferred_view_fqn=None,
        resolution_status="RESOLVED", confidence=0.7, contract_json={}, metadata_revision="1:1",
    )
    persist_entity_contract(
        1, "Client", canonical_table_fqn="dbo.vw_Clients", preferred_view_fqn="dbo.vw_Clients",
        resolution_status="RESOLVED", confidence=0.9, contract_json={}, metadata_revision="2:2",
    )
    latest = get_latest_entity_contract(1, "Client")
    assert latest["contract_version"] == 2
    assert latest["canonical_table_fqn"] == "dbo.vw_Clients"

    import data.db as db_module
    conn = db_module.get_connection()
    rows = conn.execute(
        "SELECT contract_version, canonical_table_fqn FROM semantic_entity_contracts "
        "WHERE source_id=1 AND entity_name='Client' ORDER BY contract_version",
    ).fetchall()
    conn.close()
    assert [dict(r) for r in rows] == [
        {"contract_version": 1, "canonical_table_fqn": "dbo.old_clients"},
        {"contract_version": 2, "canonical_table_fqn": "dbo.vw_Clients"},
    ]


def test_get_latest_contract_returns_none_when_absent(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    assert get_latest_entity_contract(1, "Recruiter") is None


def test_metadata_revision_reflects_latest_snapshots(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    rev = current_metadata_revision(1)
    assert rev == "1:1"  # env() fixture seeds schema_snapshot id=1, profiling_snapshot id=1


# ---------------------------------------------------------------------------
# discover_entity_contract — Task 1. Retrieval is monkeypatched (the real
# search_metadata index isn't populated by these lightweight fixture
# inserts — same constraint hit and worked around the same way in Day 2A's
# Task 2 tests); everything downstream (ranking merge, exclusion, status/
# date/measure/relationship extraction) runs against real sqlite-backed
# business context, exercising the real discovery logic.
# ---------------------------------------------------------------------------

def _fake_retrieval(table_fqns):
    def _fn(source_id, user_id, question, terms, *, session=None):
        return set(table_fqns), [], []
    return _fn


def test_discovery_resolves_canonical_and_excludes_backup(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dnnuser.FIN_Invoice", "PaidAmount", data_type="DECIMAL", is_metric=True,
                business_label="Paid Amount", approved=True)
    _add_column(db, "dnnuser.FIN_Invoice", "InsertDate", data_type="DATETIME", is_date=True,
                business_label="Insert Date", approved=True)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")

    _add_table(db, "dnnuser.FIN_Invoice_backup_2023", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice_backup_2023", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dnnuser.FIN_Invoice", "dnnuser.FIN_Invoice_backup_2023"]),
    )

    result = discover_entity_contract(1, "u1", "Invoice")
    assert result["canonical_table_fqn"] == "dnnuser.FIN_Invoice"
    assert result["resolution_status"] == "RESOLVED"
    doc = result["contract_json"]
    assert any(m["column_name"] == "PaidAmount" for m in doc["measures"])
    assert doc["date_semantics"].get("created", {}).get("column_name") == "InsertDate"
    assert any(o["table_fqn"] == "dnnuser.FIN_Invoice_backup_2023" for o in doc["excluded_objects"])
    assert doc["status_semantics"] is None  # no status-like column on this table at all


def test_discovery_finds_status_semantics_with_cached_values(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.JobOrders", table_class="Transactional")
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    status_col_id = _add_column(db, "dbo.JobOrders", "status_code", data_type="TEXT", is_dimension=True,
                                 cardinality_tier="LOW", business_label="Status", approved=True)
    _add_value_samples(db, status_col_id, ["open", "closed"])
    _add_domain(db, "dbo.JobOrders", "Operations")

    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.JobOrders"]),
    )

    result = discover_entity_contract(1, "u1", "Job Order")
    assert result["canonical_table_fqn"] == "dbo.JobOrders"
    assert result["resolution_status"] == "RESOLVED"
    status = result["contract_json"]["status_semantics"]
    assert status["column_name"] == "status_code"
    assert set(status["verified_values"]) == {"open", "closed"}
    assert status["value_source"] == "cached"


def test_discovery_marks_partial_when_status_column_found_but_unverified(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Candidates", table_class="Transactional")
    _add_column(db, "dbo.Candidates", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    # Status-like column exists but was never value-sampled, and this
    # fixture's source has no live connector wired up, so the one bounded
    # live probe fails closed too.
    _add_column(db, "dbo.Candidates", "status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)
    _add_domain(db, "dbo.Candidates", "Operations")

    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.Candidates"]),
    )

    result = discover_entity_contract(1, "u1", "Candidate")
    assert result["canonical_table_fqn"] == "dbo.Candidates"
    assert result["resolution_status"] == "PARTIAL"
    status = result["contract_json"]["status_semantics"]
    assert status["column_name"] == "status"
    assert status["verified_values"] == []


def test_discovery_returns_no_candidate_when_retrieval_is_empty(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval([]))

    result = discover_entity_contract(1, "u1", "Recruiter")
    assert result["canonical_table_fqn"] is None
    assert result["resolution_status"] == "NO_CANDIDATE"


def test_discovery_returns_no_safe_selection_on_genuine_ambiguity(tmp_path, monkeypatch):
    from core.semantic import candidate_adjudicator
    from core.semantic.candidate_adjudicator import AdjudicationResult

    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.PlacementA", table_class="Transactional", row_count=100)
    _add_column(db, "dbo.PlacementA", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.PlacementA", "Operations")
    _add_table(db, "dbo.PlacementB", table_class="Transactional", row_count=100)
    _add_column(db, "dbo.PlacementB", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.PlacementB", "Operations")

    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.PlacementA", "dbo.PlacementB"]),
    )
    monkeypatch.setattr(
        candidate_adjudicator,
        "adjudicate_candidates",
        lambda *_args, **_kwargs: AdjudicationResult(
            no_safe_selection=True,
            confidence=0.95,
        ),
    )
    monkeypatch.setattr(
        "core.config.ENABLE_AI_CANDIDATE_ADJUDICATION",
        True,
    )

    result = discover_entity_contract(1, "u1", "Placement")
    assert result["canonical_table_fqn"] is None
    assert result["resolution_status"] == "NO_SAFE_SELECTION"


# ---------------------------------------------------------------------------
# get_or_build_entity_contract — Task 6 cache
# ---------------------------------------------------------------------------

def test_cache_hit_skips_discovery_entirely(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Recruiters", table_class="Transactional")
    _add_column(db, "dbo.Recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Recruiters", "Operations")

    calls = []
    real_retrieval = _fake_retrieval(["dbo.Recruiters"])

    def counting_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", counting_retrieval)

    first = get_or_build_entity_contract(1, "u1", "Recruiter")
    assert first["canonical_table_fqn"] == "dbo.Recruiters"
    assert len(calls) == 1

    second = get_or_build_entity_contract(1, "u1", "Recruiter")
    assert second["canonical_table_fqn"] == "dbo.Recruiters"
    assert second["contract_version"] == first["contract_version"]
    assert len(calls) == 1  # no rediscovery on a fresh cache hit


def test_stale_metadata_revision_triggers_rebuild(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Recruiters", table_class="Transactional")
    _add_column(db, "dbo.Recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Recruiters", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.Recruiters"]),
    )

    first = get_or_build_entity_contract(1, "u1", "Recruiter")
    assert first["contract_version"] == 1

    # Simulate a re-profiling run: a new profiling_snapshots row bumps the
    # metadata_revision fingerprint.
    import data.db as db_module
    conn = db_module.get_connection()
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (2,1,1,2,?)", ("2026-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    second = get_or_build_entity_contract(1, "u1", "Recruiter")
    assert second["contract_version"] == 2


def test_generate_all_entity_contracts_covers_all_ten(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval([]))

    results = generate_all_entity_contracts(1, "u1")
    assert set(results.keys()) == set(TARGET_ENTITIES)
    assert all(r["resolution_status"] == "NO_CANDIDATE" for r in results.values())


# ---------------------------------------------------------------------------
# apply_grounding — Task 3/5 orchestration, including the REAL-the reference database-observed
# case of partial grounding across a multi-entity question.
# ---------------------------------------------------------------------------

def _seed_recruiter_and_placement(db):
    _add_table(db, "dbo.Recruiters", table_class="Transactional")
    _add_column(db, "dbo.Recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Recruiters", "Operations")
    # "Placement" has NO good candidate — mirrors the a real production database finding
    # (genuine ambiguity across several placement-related tables).


def test_single_entity_question_fully_grounds(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dnnuser.FIN_Invoice"]),
    )

    result = apply_grounding(1, "u1", "Active invoices", ["invoices"], ["invoices"], [])
    assert result["matched_entities"] == ["Invoice"]
    assert result["fully_grounded"] is True
    assert result["seed_table_fqns"] == {"dnnuser.FIN_Invoice"}
    assert "Invoice" in result["grounded"]


def test_multi_entity_question_partial_grounding_mirrors_real_reference(tmp_path, monkeypatch):
    """Recruiter grounds, Placement doesn't (genuine ambiguity) — the exact
    shape found running discovery against a real production database. apply_grounding must
    degrade gracefully: return the Recruiter seed, leave fully_grounded
    False, and never raise."""
    db = env(tmp_path, monkeypatch)
    _seed_recruiter_and_placement(db)
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.Recruiters"]),
    )

    result = apply_grounding(
        1, "u1", "Show recruiters and their placements",
        ["recruiters", "placements"], ["recruiters", "placements"], [],
    )
    assert result["matched_entities"] == ["Recruiter", "Placement"]
    assert set(result["grounded"].keys()) == {"Recruiter"}
    assert result["fully_grounded"] is False
    assert result["seed_table_fqns"] == {"dbo.Recruiters"}


def test_no_matched_entities_returns_empty_grounding(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    result = apply_grounding(1, "u1", "Show widgets", ["widgets"], ["widgets"], [])
    assert result["matched_entities"] == []
    assert result["grounded"] == {}
    assert result["fully_grounded"] is False
    assert result["seed_table_fqns"] == set()


def test_status_hints_populated_from_grounded_contract(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.JobOrders", table_class="Transactional")
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    status_col_id = _add_column(db, "dbo.JobOrders", "status_code", data_type="TEXT", is_dimension=True,
                                 cardinality_tier="LOW", business_label="Status", approved=True)
    _add_value_samples(db, status_col_id, ["open", "closed"])
    _add_domain(db, "dbo.JobOrders", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.JobOrders"]),
    )

    result = apply_grounding(1, "u1", "List open job orders", ["job", "orders"], ["job", "orders"], [])
    assert result["fully_grounded"] is True
    hint = result["status_hints"]["dbo.JobOrders"]
    assert hint["column_name"] == "status_code"
    assert set(hint["verified_values"]) == {"open", "closed"}


# ---------------------------------------------------------------------------
# Specialized Concept Principle (Final Demo Hardening, Part 2/3/4) — a more
# specific user concept ("mentorship programs", live-confirmed against real
# the reference database source_id=90004 collapsing into the generic already-grounded "Program"
# -> dbo.EDU_Track) must not be silently absorbed by a generic entity's own
# richer, previously-grounded metadata. Generic fixtures only (no the reference database/
# mentorship/Program names) — "Invoice" is an existing ENTITY_TAXONOMY entry
# already exercised above; "draft"/"legacy" are neutral, generic modifiers.
# ---------------------------------------------------------------------------

def test_specialize_entity_term_widens_for_unconsumed_modifier():
    # The AI physical planner's own bare entity label ("program") next to a
    # genuine, surviving modifier word from the raw question widens to the
    # full compound phrase, in the order the words actually appear.
    assert specialize_entity_term(
        "program", "How many mentorship programs are there?", ["mentorship", "programs"],
    ) == "mentorship program"


def test_specialize_entity_term_noop_for_bare_concept():
    # No modifier present -> entity_name passes through completely
    # unchanged (Part 4-F / Part 4-C: the generic case must be unaffected).
    assert specialize_entity_term(
        "program", "How many programs are there?", ["programs"],
    ) == "program"


def test_specialize_entity_term_noop_when_neighbor_is_a_stopword():
    # A raw-text neighbor that never survived extract_terms() (not in
    # all_terms) must never be treated as a modifier.
    assert specialize_entity_term(
        "invoice", "How many invoices are there?", ["invoices"],
    ) == "invoice"


def test_generic_entity_question_still_fully_grounds(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dnnuser.FIN_Invoice"]),
    )

    result = apply_grounding(1, "u1", "How many invoices are there?", ["invoices"], ["invoices"], [])
    assert result["matched_entities"] == ["Invoice"]
    assert result["fully_grounded"] is True


def test_specialized_entity_question_does_not_fully_ground(tmp_path, monkeypatch):
    """Part 4-A/D — 'draft invoices' bare-matches Invoice via the single
    word 'invoices' exactly like plain 'invoices' does, but 'draft' is a
    real, unconsumed adjacent modifier -> fully_grounded must be False so
    the broad-search path stays open for a specific 'draft invoice' table
    to compete, instead of silently narrowing to Invoice's own generic
    canonical table."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dnnuser.FIN_Invoice"]),
    )

    result = apply_grounding(
        1, "u1", "How many draft invoices are there?", ["draft", "invoices"], ["draft", "invoices"], [],
    )
    assert result["matched_entities"] == ["Invoice"]
    assert "Invoice" in result["grounded"]  # still grounded — additive, not removed
    assert result["fully_grounded"] is False  # but no longer safe to skip broad search


def test_multiword_synonym_match_is_unaffected_by_modifier_check(tmp_path, monkeypatch):
    """Part 4-F equivalent — a matched MULTI-WORD synonym (e.g. "Job
    Order") already names the complete, specific concept by itself; the
    modifier check must never demote fully_grounded for it, even though an
    adjacent word ("open") is present and unconsumed."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.JobOrders", table_class="Transactional")
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.JobOrders", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.JobOrders"]),
    )

    result = apply_grounding(1, "u1", "List open job orders", ["job", "orders"], ["job", "orders"], [])
    assert result["matched_entities"] == ["Job Order"]
    assert result["fully_grounded"] is True
