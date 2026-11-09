"""
Day 2C, Task 1 — data.semantic_contract_service.validate_candidate_contract.

Pure function tests: hand-build a `candidate` dict (the shape
data.query_planning_service._resolve_concept returns per candidate) and a
`ctx` dict (the shape data.business_knowledge_service.
get_table_business_contexts_batch returns per table) and assert
accept/reject + the specific reason.

Run:
    python -m pytest tests/test_semantic_contract_validation_rules.py -v
"""
from data.semantic_contract_service import validate_candidate_contract


def _candidate(**overrides):
    base = {
        "table_fqn": "dbo.Recruiters",
        "name_score": 0.8,
        "authority_bonus": 0.1,
        "ranking_reasons": [],
        "generated_vocabulary_term": None,
    }
    base.update(overrides)
    return base


def _ctx(**overrides):
    base = {
        "table": {"table_fqn": "dbo.Recruiters"},
        "dictionary": {"business_name": "Recruiters", "grain": None},
        "profiling": {"table_class": "Transactional"},
        "columns": [
            {
                "column_name": "id",
                "schema": {"is_primary_key": True, "is_identity": False},
                "dictionary": {"business_label": "ID"},
                "profiling": {"uniqueness_score": 1.0},
            },
            {
                "column_name": "status",
                "schema": {"is_primary_key": False, "is_identity": False},
                "dictionary": {"business_label": "Status"},
                "profiling": {"uniqueness_score": 0.02},
            },
        ],
        "relationships": {"outbound": [{"table_fqn": "dbo.Placements"}], "inbound": []},
    }
    base.update(overrides)
    return base


def test_well_evidenced_candidate_is_accepted():
    result = validate_candidate_contract("Recruiter", _candidate(), _ctx())
    assert result.accepted is True
    assert result.reasons == ()


def test_rejects_when_no_metadata_context_at_all():
    result = validate_candidate_contract("Recruiter", _candidate(), None)
    assert result.accepted is False
    assert "no metadata context" in result.reasons[0]


def test_rejects_when_no_profiling_or_dictionary_evidence():
    ctx = _ctx(dictionary=None, profiling=None)
    result = validate_candidate_contract("Recruiter", _candidate(), ctx)
    assert result.accepted is False
    assert any("no profiling or dictionary evidence" in r for r in result.reasons)


def test_rejects_when_grain_cannot_be_described():
    ctx = _ctx(columns=[
        {"column_name": "notes", "schema": {"is_primary_key": False, "is_identity": False},
         "dictionary": {"business_label": "Notes"}, "profiling": {"uniqueness_score": 0.1}},
    ])
    result = validate_candidate_contract("Recruiter", _candidate(), ctx)
    assert result.accepted is False
    assert any("grain cannot be described" in r for r in result.reasons)


def test_rejects_when_requested_attribute_missing():
    result = validate_candidate_contract(
        "Recruiter", _candidate(), _ctx(), requested_attributes=["commission rate"],
    )
    assert result.accepted is False
    assert any("commission rate" in r for r in result.reasons)


def test_rejects_generic_word_only_match():
    candidate = _candidate(name_score=0.0, generated_vocabulary_term="recruiter")
    ctx = _ctx(dictionary={"business_name": None, "grain": None})
    result = validate_candidate_contract("Recruiter", candidate, ctx)
    assert result.accepted is False
    assert any("matches only a generic word" in r for r in result.reasons)


def test_rejects_missing_relationship_for_relationship_shaped_entity():
    ctx = _ctx(relationships={"outbound": [], "inbound": []})
    result = validate_candidate_contract("Enrollment", _candidate(table_fqn="dbo.Enrollments"), ctx)
    assert result.accepted is False
    assert any("requires a relationship path" in r for r in result.reasons)


def test_pending_only_relationship_is_a_warning_not_a_rejection():
    """Real the reference database finding (Pilot Participant revalidation): a table can have
    real, unapproved (PENDING) FK evidence without any AUTO/APPROVED edge —
    that is meaningfully different from zero evidence at any status, and
    must not be treated as a hard rejection even for a relationship-
    required entity."""
    ctx = _ctx(
        relationships={"outbound": [], "inbound": []},
        relationship_evidence=[
            {"direction": "inbound", "related_table": "dbo.EDU_Student",
             "relationship_status": "PENDING", "confidence": 0.76},
        ],
    )
    result = validate_candidate_contract("Enrollment", _candidate(table_fqn="dbo.Enrollments"), ctx)
    assert result.accepted is True
    assert any("unapproved (PENDING) relationship evidence" in w for w in result.warnings)


def test_missing_relationship_is_only_a_warning_for_non_relationship_entity():
    ctx = _ctx(relationships={"outbound": [], "inbound": []})
    result = validate_candidate_contract("Invoice", _candidate(table_fqn="dnnuser.FIN_Invoice"), ctx)
    assert result.accepted is True
    assert any("no discovered relationships" in w for w in result.warnings)


def test_rejects_derivative_object_flagged_by_naming_penalty():
    candidate = _candidate(ranking_reasons=["Naming penalty: backup/export table pattern matched."])
    result = validate_candidate_contract("Recruiter", candidate, _ctx())
    assert result.accepted is False
    assert any("flagged as a derivative object" in r for r in result.reasons)


def test_intentional_analytical_bypasses_derivative_rejection():
    candidate = _candidate(ranking_reasons=["Naming penalty: rolling-period table pattern matched."])
    result = validate_candidate_contract("Recruiter", candidate, _ctx(), intentional_analytical=True)
    assert result.accepted is True


def test_rejects_claimed_status_column_not_on_table():
    result = validate_candidate_contract(
        "Recruiter", _candidate(), _ctx(), claimed_status_column="does_not_exist",
    )
    assert result.accepted is False
    assert any("Claimed status column" in r for r in result.reasons)


def test_rejects_claimed_date_column_not_on_table():
    result = validate_candidate_contract(
        "Recruiter", _candidate(), _ctx(), claimed_date_columns=["missing_date"],
    )
    assert result.accepted is False
    assert any("Claimed date column" in r for r in result.reasons)


def test_unverified_status_values_is_a_warning_not_a_rejection():
    result = validate_candidate_contract(
        "Recruiter", _candidate(), _ctx(), claimed_status_column="status", value_samples=None,
    )
    assert result.accepted is True
    assert any("no verified sample values" in w for w in result.warnings)


def test_verified_status_values_produce_no_warning():
    result = validate_candidate_contract(
        "Recruiter", _candidate(), _ctx(), claimed_status_column="status", value_samples=["active", "inactive"],
    )
    assert result.accepted is True
    assert result.warnings == ()
