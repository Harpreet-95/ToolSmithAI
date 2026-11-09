"""
Automatic Multi-Entity Semantic Grounding — dynamic entity detection beyond
the fixed ENTITY_TAXONOMY (data.semantic_contract_service).

Covers:
  - entity_synonyms()'s fixed-taxonomy vs generic-fallback behavior, and
    _term_lookup_keys()'s "-es"-plural handling ("classes" -> "class"),
    including as defense-in-depth alongside normalize_term's own sibilant
    "-es" rule.
  - match_dynamic_entity_terms(): table-level generated-vocabulary evidence
    qualifies a term; column-only evidence, structural query language, and
    terms already consumed by a fixed match do not.
  - apply_grounding()'s end-to-end promotion of a dynamic entity alongside
    a fixed one.
  - _relationship_disambiguate() / _joint_disambiguate_pair(): the Part 3/5
    relationship-corroboration tie-break for a lexically near-tied dynamic
    candidate, including the _MAX_TIEBREAK_HOPS safeguard that stops a
    plain attribute word from being promoted to a full second entity via a
    long, coincidental join chain.

Run:
    python -m pytest tests/test_dynamic_entity_grounding.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-dynamic-entity-grounding-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-dynamic-entity-grounding-salt-long-enough12")

import data.semantic_contract_service as semantic_contract_service
from data.semantic_contract_service import (
    apply_grounding,
    entity_synonyms,
    get_latest_entity_contract,
    match_dynamic_entity_terms,
    match_entities_for_terms,
    _joint_disambiguate_pair,
    _relationship_disambiguate,
    _relationship_rescued_candidate,
    _term_lookup_keys,
    _tied_top_candidates,
    _trusted_connection,
    _trusted_connection_path,
    _term_matches_grounded_table_column,
)
from data.semantic_retrieval_service import get_relationship_distance_map
from tests.test_phase9_query_planning import env, _add_table, _add_column
from tests.test_semantic_retrieval_service import _add_generated_vocab, _add_relationship

import pytest


@pytest.fixture(autouse=True)
def _isolated_metadata_db(tmp_path, monkeypatch):
    """_relationship_disambiguate/_joint_disambiguate_pair's tie-break
    (_build_tiebreak_contract -> current_metadata_revision) reaches the
    metadata DB unconditionally, even for tests that build no fixture DB of
    their own -- a previously missing fixture (Phase 5 Step 6C)."""
    import data.db as db_module
    import data.models as models

    db_path = str(tmp_path / "dynamic_entity_grounding_default.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    yield


# ---------------------------------------------------------------------------
# entity_synonyms / _term_lookup_keys — pure, no DB
# ---------------------------------------------------------------------------

def test_entity_synonyms_uses_fixed_taxonomy_when_present():
    assert entity_synonyms("Student") == ("student", "students")


def test_entity_synonyms_generic_fallback_for_unknown_entity():
    synonyms = entity_synonyms("Instructor")
    assert "instructor" in synonyms
    assert "instructors" in synonyms


def test_term_lookup_keys_handles_es_plural_normalize_term_misses():
    # normalize_term("classes") -> "class" directly (sibilant "-es" rule).
    # "class" must be the (first, deduplicated) key so a real "class"
    # vocabulary entry is found.
    keys = _term_lookup_keys("classes")
    assert keys[0] == "class"
    assert keys.count("class") == 1


def test_term_lookup_keys_bare_s_plural():
    keys = _term_lookup_keys("instructors")
    assert "instructor" in keys


# ---------------------------------------------------------------------------
# match_dynamic_entity_terms — table-level evidence gate
# ---------------------------------------------------------------------------

def test_dynamic_candidate_requires_table_level_evidence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_generated_vocab(db, "instructor", "dbo.EDU_Instructor", tier="HIGH", column_name="")

    candidates = match_dynamic_entity_terms(
        1, ["instructor"], ["instructor"], [], already_matched=[],
    )
    assert candidates == ["Instructor"]


def test_dynamic_candidate_rejects_column_only_evidence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Only a COLUMN happens to share this word — never sufficient alone to
    # promote a term to entity status.
    _add_generated_vocab(
        db, "instructor", "dbo.SomeUnrelatedTable", tier="MEDIUM", column_name="InstructorID",
    )

    candidates = match_dynamic_entity_terms(
        1, ["instructor"], ["instructor"], [], already_matched=[],
    )
    assert candidates == []


def test_dynamic_candidate_rejects_low_tier_evidence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_generated_vocab(db, "instructor", "dbo.Noise", tier="LOW", column_name="")

    candidates = match_dynamic_entity_terms(
        1, ["instructor"], ["instructor"], [], already_matched=[],
    )
    assert candidates == []


def test_dynamic_candidate_excludes_terms_consumed_by_fixed_match(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_generated_vocab(db, "client", "dbo.SomeOtherClientsTable", tier="HIGH", column_name="")

    # "clients" is already consumed by the fixed "Client" taxonomy entry —
    # must not ALSO surface as an independent dynamic candidate.
    candidates = match_dynamic_entity_terms(
        1, ["clients"], ["clients"], [], already_matched=["Client"],
    )
    assert candidates == []


def test_dynamic_candidate_excludes_structural_query_language(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # A coincidental unrelated view happens to have "month" in its name —
    # must never read as a competing entity (mirrors validate_ai_entity_
    # hypotheses's own structural-term guard).
    _add_generated_vocab(db, "month", "dbo.vw_SomeCommunication_Month", tier="MEDIUM", column_name="")

    candidates = match_dynamic_entity_terms(
        1, ["month"], ["month"], [], already_matched=[],
    )
    assert candidates == []


def test_dynamic_candidate_no_vocabulary_returns_empty(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert match_dynamic_entity_terms(1, ["widgets"], ["widgets"], [], already_matched=[]) == []


# ---------------------------------------------------------------------------
# apply_grounding — end-to-end dynamic promotion
# ---------------------------------------------------------------------------

def test_apply_grounding_promotes_unambiguous_dynamic_entity(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Instructor")
    _add_column(db, "dbo.EDU_Instructor", "InstructorID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "instructor", "dbo.EDU_Instructor", tier="HIGH", column_name="")

    grounding = apply_grounding(
        1, "u1", "List instructors", ["instructors"], ["instructors"], [],
    )
    assert grounding["matched_entities"] == ["Instructor"]
    assert grounding["entity_table_map"]["Instructor"] == "dbo.EDU_Instructor"
    assert grounding["fully_grounded"] is True


def test_apply_grounding_dynamic_entity_never_marked_unsupported(tmp_path, monkeypatch):
    # A dynamic candidate that fails to confirm (no matching table at all)
    # must be silently dropped, never routed into `unsupported` (it was
    # only ever a hypothesis, not a taxonomy-confirmed entity name).
    db = env(tmp_path, monkeypatch)
    env_result = db
    grounding = apply_grounding(
        1, "u1", "Show widgets", ["widgets"], ["widgets"], [],
    )
    assert grounding["matched_entities"] == []
    assert grounding["unsupported"] == {}


# ---------------------------------------------------------------------------
# _relationship_disambiguate — Part 3/5 corroboration tie-break
# ---------------------------------------------------------------------------

def _fake_evidence(candidates):
    return {"candidates_by_fqn": {c["table_fqn"]: {"candidate": c, "ctx": {}} for c in candidates}}


def test_tied_top_candidates_requires_real_margin_ambiguity(monkeypatch):
    # A clear, unambiguous winner is not the shape _tied_top_candidates
    # exists to catch.
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.A", "score": 0.9},
            {"table_fqn": "dbo.B", "score": 0.2},
        ]),
    )
    assert _tied_top_candidates(1, "u1", "Widget") is None


def test_tied_top_candidates_below_confidence_floor_is_none(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.A", "score": 0.3},
            {"table_fqn": "dbo.B", "score": 0.28},
        ]),
    )
    assert _tied_top_candidates(1, "u1", "Widget") is None


def test_tied_top_candidates_returns_the_tie(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.A", "score": 0.62},
            {"table_fqn": "dbo.B", "score": 0.53},
            {"table_fqn": "dbo.C", "score": 0.10},
        ]),
    )
    tied = _tied_top_candidates(1, "u1", "Widget")
    assert {c["table_fqn"] for c in tied} == {"dbo.A", "dbo.B"}


def _fake_path(hops):
    return {"best_join_path": {"hops": hops}}


def test_trusted_connection_true_within_hop_cap(monkeypatch):
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: _fake_path(2),
    )
    assert _trusted_connection(1, "u1", "dbo.A", "dbo.B") is True


def test_trusted_connection_false_beyond_hop_cap(monkeypatch):
    # A long, coincidental chain (e.g. through an unrelated marketing/
    # tracking table) must not corroborate — this is exactly what stops a
    # plain attribute word from being promoted to a whole second entity.
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: _fake_path(3),
    )
    assert _trusted_connection(1, "u1", "dbo.A", "dbo.B") is False


def test_trusted_connection_false_when_no_path(monkeypatch):
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: {"best_join_path": None},
    )
    assert _trusted_connection(1, "u1", "dbo.A", "dbo.B") is False


def test_relationship_disambiguate_promotes_the_one_connected_candidate(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.EDU_Instructor", "score": 0.62},
            {"table_fqn": "dbo.InstructorCommissions", "score": 0.53},
        ]),
    )

    def fake_path(source_id, user_id, a, b):
        if {a, b} == {"dbo.EDU_Instructor", "dbo.EDU_ClassSignups"}:
            return _fake_path(2)
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    result = _relationship_disambiguate(1, "u1", "Instructor", {"dbo.EDU_ClassSignups"})
    assert result is not None
    assert result["canonical_table_fqn"] == "dbo.EDU_Instructor"
    assert result["resolution_status"] == "PARTIAL"
    assert result["generation_method"] == "relationship_tiebreak"


def test_relationship_disambiguate_refuses_when_both_connect(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.A", "score": 0.62},
            {"table_fqn": "dbo.B", "score": 0.53},
        ]),
    )
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: _fake_path(1),
    )
    assert _relationship_disambiguate(1, "u1", "Widget", {"dbo.Anchor"}) is None


def test_relationship_disambiguate_refuses_when_none_connect(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.A", "score": 0.62},
            {"table_fqn": "dbo.B", "score": 0.53},
        ]),
    )
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: {"best_join_path": None},
    )
    assert _relationship_disambiguate(1, "u1", "Widget", {"dbo.Anchor"}) is None


def test_relationship_disambiguate_no_anchors_returns_none():
    assert _relationship_disambiguate(1, "u1", "Widget", set()) is None


# ---------------------------------------------------------------------------
# Consistent Automatic Business-Term Resolution — a second tied candidate's
# connection to the anchor that merely ROUTES THROUGH the incumbent (tied[0])
# is not independent competing evidence and must not block confirming the
# incumbent. Real-the reference database regression: dbo.EDU_ClassSignups "connects" to
# dbo.EDU_Track only via dbo.EDU_Class (2 hops: ClassSignups -> Class ->
# Path) -- an echo of the same evidence that already connects the incumbent
# dbo.EDU_Class directly, not a second, independently-connected candidate.
# ---------------------------------------------------------------------------

def _fake_path_via(hops, path):
    return {"best_join_path": {"hops": hops, "path": path}}


def test_relationship_disambiguate_confirms_incumbent_despite_derivative_connection(monkeypatch):
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.EDU_Class", "score": 0.87},
            {"table_fqn": "dbo.EDU_ClassSignups", "score": 0.73},
        ]),
    )

    def fake_path(source_id, user_id, a, b):
        # Incumbent connects directly (1 hop).
        if {a, b} == {"dbo.EDU_Class", "dbo.EDU_Track"}:
            return _fake_path_via(1, ["dbo.EDU_Class", "dbo.EDU_Track"])
        # The OTHER tied candidate only reaches the anchor BY ROUTING
        # THROUGH the incumbent -- a derivative echo, not independent
        # evidence.
        if {a, b} == {"dbo.EDU_ClassSignups", "dbo.EDU_Track"}:
            return _fake_path_via(2, ["dbo.EDU_ClassSignups", "dbo.EDU_Class", "dbo.EDU_Track"])
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    result = _relationship_disambiguate(1, "u1", "Class", {"dbo.EDU_Track"})
    assert result is not None
    assert result["canonical_table_fqn"] == "dbo.EDU_Class"


def test_relationship_disambiguate_refuses_when_second_connection_is_independent(monkeypatch):
    # Same shape as above, but the OTHER tied candidate reaches the anchor
    # via its OWN direct edge (not routed through the incumbent) -- genuine
    # independent competing evidence, must still safely refuse.
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.EDU_Class", "score": 0.87},
            {"table_fqn": "dbo.EDU_ClassEvent", "score": 0.78},
        ]),
    )

    def fake_path(source_id, user_id, a, b):
        if {a, b} == {"dbo.EDU_Class", "dbo.EDU_Track"}:
            return _fake_path_via(1, ["dbo.EDU_Class", "dbo.EDU_Track"])
        if {a, b} == {"dbo.EDU_ClassEvent", "dbo.EDU_Track"}:
            return _fake_path_via(1, ["dbo.EDU_ClassEvent", "dbo.EDU_Track"])
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    assert _relationship_disambiguate(1, "u1", "Class", {"dbo.EDU_Track"}) is None


def test_relationship_disambiguate_ignores_tied_candidate_that_is_the_anchor_itself(monkeypatch):
    # A tied candidate that IS one of the anchor tables (e.g. it's already
    # grounded as a DIFFERENT entity in this same question) must not count
    # as competing evidence via the trivial same-table path.
    monkeypatch.setattr(
        semantic_contract_service, "gather_candidate_evidence",
        lambda *a, **k: _fake_evidence([
            {"table_fqn": "dbo.EDU_Class", "score": 0.87},
            {"table_fqn": "dbo.EDU_ClassSignups", "score": 0.73},
        ]),
    )

    def fake_path(source_id, user_id, a, b):
        if {a, b} == {"dbo.EDU_Class", "dbo.EDU_ClassSignups"}:
            return _fake_path_via(1, ["dbo.EDU_Class", "dbo.EDU_ClassSignups"])
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    result = _relationship_disambiguate(1, "u1", "Class", {"dbo.EDU_ClassSignups"})
    assert result is not None
    assert result["canonical_table_fqn"] == "dbo.EDU_Class"


def test_trusted_connection_path_returns_route(monkeypatch):
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: _fake_path_via(2, ["dbo.A", "dbo.Mid", "dbo.B"]),
    )
    assert _trusted_connection_path(1, "u1", "dbo.A", "dbo.B") == ["dbo.A", "dbo.Mid", "dbo.B"]


def test_trusted_connection_path_none_beyond_hop_cap(monkeypatch):
    monkeypatch.setattr(
        "data.semantic_layer_service.recommend_best_join_path",
        lambda *a, **k: _fake_path_via(5, ["dbo.A", "dbo.X", "dbo.Y", "dbo.Z", "dbo.W", "dbo.B"]),
    )
    assert _trusted_connection_path(1, "u1", "dbo.A", "dbo.B") is None


# ---------------------------------------------------------------------------
# _joint_disambiguate_pair — Part 3 mutual resolution (no anchor yet)
# ---------------------------------------------------------------------------

def test_joint_disambiguate_pair_resolves_unique_combination(monkeypatch):
    tied_a = [{"table_fqn": "dbo.EDU_Class", "score": 0.87}, {"table_fqn": "dbo.EDU_ClassSignups", "score": 0.73}]
    tied_b = [{"table_fqn": "dbo.EDU_Instructor", "score": 0.62}, {"table_fqn": "dbo.InstructorCommissions", "score": 0.53}]

    def fake_path(source_id, user_id, a, b):
        if {a, b} == {"dbo.EDU_Class", "dbo.EDU_Instructor"}:
            return _fake_path(2)
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    result = _joint_disambiguate_pair(1, "u1", "Class", tied_a, "Instructor", tied_b)
    assert result is not None
    contract_a, contract_b = result
    assert contract_a["canonical_table_fqn"] == "dbo.EDU_Class"
    assert contract_b["canonical_table_fqn"] == "dbo.EDU_Instructor"


def test_joint_disambiguate_pair_refuses_multiple_connections(monkeypatch):
    tied_a = [{"table_fqn": "dbo.A1", "score": 0.87}, {"table_fqn": "dbo.A2", "score": 0.73}]
    tied_b = [{"table_fqn": "dbo.B1", "score": 0.62}, {"table_fqn": "dbo.B2", "score": 0.53}]

    # Both A1 and A2 connect to B1 -- genuinely ambiguous, must refuse.
    def fake_path(source_id, user_id, a, b):
        if b in ("dbo.A1", "dbo.A2") and a == "dbo.B1" or a in ("dbo.A1", "dbo.A2") and b == "dbo.B1":
            return _fake_path(1)
        return {"best_join_path": None}

    monkeypatch.setattr("data.semantic_layer_service.recommend_best_join_path", fake_path)

    assert _joint_disambiguate_pair(1, "u1", "A", tied_a, "B", tied_b) is None


# ---------------------------------------------------------------------------
# get_relationship_distance_map — bounded multi-hop BFS
# ---------------------------------------------------------------------------

def test_relationship_distance_map_multi_hop_chain(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Real the reference database shape: Student -> ClassSignups -> Class -> Path is 3 hops,
    # not reachable within the tighter 2-hop tie-break bound.
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Student")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Class")
    _add_relationship(db, "dbo.EDU_Class", "dbo.EDU_Track")

    distances = get_relationship_distance_map(1, {"dbo.EDU_Student"}, max_hops=3)
    assert distances["dbo.EDU_Student"] == 0
    assert distances["dbo.EDU_ClassSignups"] == 1
    assert distances["dbo.EDU_Class"] == 2
    assert distances["dbo.EDU_Track"] == 3


def test_relationship_distance_map_respects_max_hops(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Student")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Class")
    _add_relationship(db, "dbo.EDU_Class", "dbo.EDU_Track")

    distances = get_relationship_distance_map(1, {"dbo.EDU_Student"}, max_hops=2)
    assert "dbo.EDU_Track" not in distances
    assert distances["dbo.EDU_Class"] == 2


def test_relationship_distance_map_ignores_pending_edges(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_relationship(db, "dbo.A", "dbo.B", status="PENDING")
    distances = get_relationship_distance_map(1, {"dbo.A"}, max_hops=3)
    assert "dbo.B" not in distances


def test_relationship_distance_map_empty_seeds_returns_empty():
    assert get_relationship_distance_map(1, set()) == {}


# ---------------------------------------------------------------------------
# _relationship_rescued_candidate — Enterprise Phase 4 joint resolution.
# Reproduces the a real production database "program" case: dbo.EDU_Track's own name/
# description share no words with "program" at all (only a "ProgramType"
# column does, evidence too weak alone to enter the ordinary candidate
# pool), but it sits 3 trusted hops from an already-grounded Student.
# ---------------------------------------------------------------------------

def test_relationship_rescued_candidate_promotes_sole_connected_evidenced_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Track")
    _add_column(db, "dbo.EDU_Track", "TrackName")
    _add_generated_vocab(db, "program", "dbo.EDU_Track", tier="MEDIUM", column_name="ProgramType")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Student")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Class")
    _add_relationship(db, "dbo.EDU_Class", "dbo.EDU_Track")

    result = _relationship_rescued_candidate(1, "u1", "Program", {"dbo.EDU_Student"})
    assert result is not None
    assert result["canonical_table_fqn"] == "dbo.EDU_Track"
    assert result["generation_method"] == "relationship_rescue"

    # Persisted (unlike _relationship_disambiguate) so a later, standalone
    # question naming "program" alone gets a normal cache hit.
    persisted = get_latest_entity_contract(1, "Program")
    assert persisted is not None
    assert persisted["canonical_table_fqn"] == "dbo.EDU_Track"


def test_relationship_rescued_candidate_no_anchors_returns_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_generated_vocab(db, "program", "dbo.EDU_Track", tier="MEDIUM", column_name="ProgramType")
    assert _relationship_rescued_candidate(1, "u1", "Program", set()) is None


def test_relationship_rescued_candidate_no_vocabulary_evidence_returns_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Student")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Track")
    # No generated_business_vocabulary row at all for "program" — nothing to rescue.
    assert _relationship_rescued_candidate(1, "u1", "Program", {"dbo.EDU_Student"}) is None


def test_relationship_rescued_candidate_refuses_when_multiple_tables_connect(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Track")
    _add_table(db, "dbo.EDU_OtherProgramTable")
    _add_generated_vocab(db, "program", "dbo.EDU_Track", tier="MEDIUM", column_name="ProgramType")
    _add_generated_vocab(db, "program", "dbo.EDU_OtherProgramTable", tier="MEDIUM", column_name="ProgramCode")
    _add_relationship(db, "dbo.EDU_Student", "dbo.EDU_Track")
    _add_relationship(db, "dbo.EDU_Student", "dbo.EDU_OtherProgramTable")

    # Two equally-evidenced, equally-connected candidates — genuine
    # ambiguity, must refuse rather than guess.
    assert _relationship_rescued_candidate(1, "u1", "Program", {"dbo.EDU_Student"}) is None


def test_relationship_rescued_candidate_refuses_when_evidenced_table_unreachable(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Track")
    _add_generated_vocab(db, "program", "dbo.EDU_Track", tier="MEDIUM", column_name="ProgramType")
    # No relationship at all to the grounded anchor.
    assert _relationship_rescued_candidate(1, "u1", "Program", {"dbo.EDU_Student"}) is None


# ---------------------------------------------------------------------------
# Full apply_grounding integration — a term stuck in Pass 2's `pending`
# (tied lexical candidates, none connected) must still reach the rescue in
# the final pass, and a later standalone question must reuse the persisted
# result.
# ---------------------------------------------------------------------------

def test_apply_grounding_rescues_dynamic_entity_stuck_pending(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "student", "dbo.EDU_Student", tier="HIGH", column_name="")

    _add_table(db, "dbo.EDU_Track")
    # Two tied, unrelated "program" candidates via coarse table-level
    # evidence (mirrors a real production database's InternshipProgram-family tie) — neither
    # connects to Student, so ordinary tie-break correctly refuses them.
    _add_generated_vocab(db, "program", "dbo.EDU_UnrelatedProgramA", tier="MEDIUM", column_name="")
    _add_generated_vocab(db, "program", "dbo.EDU_UnrelatedProgramB", tier="MEDIUM", column_name="")
    # The REAL match: weaker, column-only evidence, but relationship-connected.
    _add_generated_vocab(db, "program", "dbo.EDU_Track", tier="MEDIUM", column_name="ProgramType")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Student")
    _add_relationship(db, "dbo.EDU_ClassSignups", "dbo.EDU_Class")
    _add_relationship(db, "dbo.EDU_Class", "dbo.EDU_Track")

    grounding = apply_grounding(
        1, "u1", "How many students are in each program?",
        ["students", "program"], ["students"], ["program"],
    )
    assert grounding["grounded"]["Program"]["canonical_table_fqn"] == "dbo.EDU_Track"
    assert "Program" in grounding["matched_entities"]

    # A later, standalone question with no other entity to rescue against
    # benefits from the persisted contract.
    standalone = apply_grounding(1, "u1", "Show programs", ["programs"], ["programs"], [])
    assert standalone["grounded"]["Program"]["canonical_table_fqn"] == "dbo.EDU_Track"


# ---------------------------------------------------------------------------
# Contextual Ambiguity Resolution — rescue is tried BEFORE the local
# tie-break in Pass 2/3 (previously tie-break ran first and, once it
# connected to any tied candidate, permanently locked that answer in before
# rescue's wider, more-complete-pool check ever got a chance). Reproduces
# the real dbo.EDU_Mentoring_Program/dbo.EDU_Track "program" case at small
# scale: a coarse table-level tie DOES connect to the grounded anchor (so
# the OLD ordering would pick it), but a materially different table with
# real column-level evidence is uniquely rescue-connected too and must win.
# ---------------------------------------------------------------------------

def test_apply_grounding_prefers_rescue_over_a_connected_local_tie(tmp_path, monkeypatch):
    """Isolates the Pass 2 ORDERING decision itself (not _relationship_
    rescued_candidate's or _relationship_disambiguate's own internal
    correctness — each already has dedicated tests above): given a
    dynamic candidate for which BOTH mechanisms would independently
    succeed with DIFFERENT answers, apply_grounding must ground the
    rescue's answer, not the tie-break's — mirroring the real dbo.
    EDU_Mentoring_Program/dbo.EDU_Track case, where the tied LOCAL pool's
    own top scorer had a trusted connection (so the OLD ordering picked
    it) while a materially different table, invisible to that local pool,
    was uniquely rescue-connected via real column-level vocabulary
    evidence."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "student", "dbo.EDU_Student", tier="HIGH", column_name="")

    monkeypatch.setattr(
        semantic_contract_service, "match_dynamic_entity_terms",
        lambda *a, **k: ["Program"],
    )
    real_get_or_build = semantic_contract_service.get_or_build_entity_contract

    def fake_get_or_build(source_id, user_id, entity_name, *, session=None):
        if entity_name == "Program":
            return {
                "canonical_table_fqn": None, "preferred_view_fqn": None,
                "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
                "contract": {"evidence": [], "alternate_candidates": []},
                "metadata_revision": 1,
            }
        return real_get_or_build(source_id, user_id, entity_name, session=session)

    monkeypatch.setattr(semantic_contract_service, "get_or_build_entity_contract", fake_get_or_build)

    tiebreak_result = {
        "canonical_table_fqn": "dbo.EDU_ConnectedDecoy", "preferred_view_fqn": None,
        "resolution_status": "PARTIAL", "confidence": 0.7,
        "contract": {"evidence": ["tiebreak"], "alternate_candidates": []},
        "metadata_revision": 1, "generation_method": "relationship_tiebreak",
    }
    rescue_result = {
        "canonical_table_fqn": "dbo.EDU_Track", "preferred_view_fqn": None,
        "resolution_status": "PARTIAL", "confidence": 0.75,
        "contract": {"evidence": ["rescue"], "alternate_candidates": []},
        "metadata_revision": 1, "generation_method": "relationship_rescue",
    }
    monkeypatch.setattr(
        semantic_contract_service, "_relationship_disambiguate",
        lambda *a, **k: tiebreak_result,
    )
    monkeypatch.setattr(
        semantic_contract_service, "_relationship_rescued_candidate",
        lambda *a, **k: rescue_result,
    )

    grounding = apply_grounding(
        1, "u1", "How many students are in each program?",
        ["students", "program"], ["students"], ["program"],
    )
    assert grounding["grounded"]["Program"]["canonical_table_fqn"] == "dbo.EDU_Track"
    assert grounding["grounded"]["Program"]["generation_method"] == "relationship_rescue"


def test_apply_grounding_falls_back_to_tiebreak_when_rescue_returns_none(tmp_path, monkeypatch):
    """When rescue doesn't apply (returns None), the ordinary local
    tie-break still runs and wins exactly as before — rescue-first costs
    nothing when it doesn't fire."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "student", "dbo.EDU_Student", tier="HIGH", column_name="")

    monkeypatch.setattr(
        semantic_contract_service, "match_dynamic_entity_terms",
        lambda *a, **k: ["Program"],
    )
    real_get_or_build = semantic_contract_service.get_or_build_entity_contract

    def fake_get_or_build(source_id, user_id, entity_name, *, session=None):
        if entity_name == "Program":
            return {
                "canonical_table_fqn": None, "preferred_view_fqn": None,
                "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
                "contract": {"evidence": [], "alternate_candidates": []},
                "metadata_revision": 1,
            }
        return real_get_or_build(source_id, user_id, entity_name, session=session)

    monkeypatch.setattr(semantic_contract_service, "get_or_build_entity_contract", fake_get_or_build)

    tiebreak_result = {
        "canonical_table_fqn": "dbo.EDU_ConnectedProgram", "preferred_view_fqn": None,
        "resolution_status": "PARTIAL", "confidence": 0.7,
        "contract": {"evidence": ["tiebreak"], "alternate_candidates": []},
        "metadata_revision": 1, "generation_method": "relationship_tiebreak",
    }
    monkeypatch.setattr(
        semantic_contract_service, "_relationship_disambiguate",
        lambda *a, **k: tiebreak_result,
    )
    monkeypatch.setattr(
        semantic_contract_service, "_relationship_rescued_candidate",
        lambda *a, **k: None,
    )

    grounding = apply_grounding(
        1, "u1", "How many students are in each program?",
        ["students", "program"], ["students"], ["program"],
    )
    assert grounding["grounded"]["Program"]["canonical_table_fqn"] == "dbo.EDU_ConnectedProgram"
    assert grounding["grounded"]["Program"]["generation_method"] == "relationship_tiebreak"



# ---------------------------------------------------------------------------
# Contextual Ambiguity Resolution — fixed-taxonomy entities now also feed
# into `pending` on a genuine close-margin tie, instead of being silently
# dropped with no path to relationship-based disambiguation at all (dynamic
# candidates always had this; fixed ones previously did not).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# General Attribute Resolution — _term_matches_grounded_table_column /
# apply_grounding's Pass 2 veto. Real-the reference database regression: "Show student emails"
# grounds "Student" fine, but "Email" ALSO clears match_dynamic_entity_
# terms's table-level bar (dbo.Emails, an unrelated inbox/message-log table
# whose own NAME happens to match "email") and gets promoted to a second
# grounded entity — hijacking relationship-plan routing instead of ever
# reaching the related-attribute fallback that would find the REAL match,
# dnnuser.Users.Email, one trusted hop from the already-grounded
# dbo.EDU_Student. Real column-level evidence on an already-grounded
# entity's own table (or its trusted 1-hop relations) must veto that
# promotion — see this function's own docstring for why.
# ---------------------------------------------------------------------------

def test_term_matches_grounded_table_column_direct_match(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Instructor")
    _add_column(db, "dbo.EDU_Instructor", "InstructorCity", data_type="TEXT",
                is_dimension=True, business_label="Instructor City", approved=True)

    assert _term_matches_grounded_table_column(1, "u1", "City", {"dbo.EDU_Instructor"}) is True


def test_term_matches_grounded_table_column_via_trusted_related_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentUserID", data_type="INTEGER")
    _add_table(db, "dnnuser.Users")
    _add_column(db, "dnnuser.Users", "UserID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dnnuser.Users", "Email", data_type="TEXT", is_dimension=True,
                business_label="Email", approved=True)
    _add_relationship(db, "dbo.EDU_Student", "dnnuser.Users")

    assert _term_matches_grounded_table_column(1, "u1", "Email", {"dbo.EDU_Student"}) is True


def test_term_matches_grounded_table_column_no_match_returns_false(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)

    assert _term_matches_grounded_table_column(1, "u1", "Program", {"dbo.EDU_Student"}) is False


def test_apply_grounding_does_not_promote_attribute_of_already_grounded_entity(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Student")
    _add_column(db, "dbo.EDU_Student", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Student", "StudentUserID", data_type="INTEGER")
    _add_generated_vocab(db, "student", "dbo.EDU_Student", tier="HIGH", column_name="")

    _add_table(db, "dnnuser.Users")
    _add_column(db, "dnnuser.Users", "UserID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_column(db, "dnnuser.Users", "Email", data_type="TEXT", is_dimension=True,
                business_label="Email", approved=True)
    _add_relationship(db, "dbo.EDU_Student", "dnnuser.Users")

    # The coincidental decoy: an unrelated table whose own NAME matches
    # "email" (mirrors real-the reference database dbo.Emails, an inbox/message-log table).
    _add_table(db, "dbo.Emails")
    _add_column(db, "dbo.Emails", "EmailID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_generated_vocab(db, "email", "dbo.Emails", tier="HIGH", column_name="")

    grounding = apply_grounding(
        1, "u1", "Show student emails",
        ["student", "email"], ["student", "email"], [],
    )
    assert grounding["entity_table_map"] == {"Student": "dbo.EDU_Student"}
    assert "Email" not in grounding["grounded"]


def test_apply_grounding_still_promotes_genuine_second_entity(tmp_path, monkeypatch):
    # Regression safety: two DYNAMIC entities with real, mutually-unrelated
    # evidence (neither matches a column on the other's table) must still
    # both ground normally — the veto only ever fires on real column-level
    # evidence, never suppresses a genuine second entity. Contract discovery
    # itself is mocked directly (same shortcut this file's own Pass 2/3
    # ordering tests already use) so this isolates the veto's own behavior
    # from the bounded candidate-retrieval search's fixture requirements.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Class")
    _add_column(db, "dbo.EDU_Class", "ClassID", is_pk=1, is_id=1, uniqueness=1.0)
    _add_table(db, "dbo.EDU_Track")
    _add_column(db, "dbo.EDU_Track", "TrackID", is_pk=1, is_id=1, uniqueness=1.0)

    monkeypatch.setattr(
        semantic_contract_service, "match_dynamic_entity_terms",
        lambda *a, **k: ["Class", "Program"],
    )

    def _fake_contract(source_id, user_id, entity_name, session=None):
        table = {"Class": "dbo.EDU_Class", "Program": "dbo.EDU_Track"}[entity_name]
        return {
            "canonical_table_fqn": table, "preferred_view_fqn": None,
            "resolution_status": "RESOLVED", "confidence": 0.9,
            "contract": {"evidence": ["fake"], "alternate_candidates": []},
            "metadata_revision": 1, "generation_method": "auto_grounding",
        }
    monkeypatch.setattr(semantic_contract_service, "get_or_build_entity_contract", _fake_contract)

    grounding = apply_grounding(
        1, "u1", "Show classes and their program",
        ["class", "program"], ["class", "program"], [],
    )
    assert grounding["entity_table_map"] == {"Class": "dbo.EDU_Class", "Program": "dbo.EDU_Track"}
