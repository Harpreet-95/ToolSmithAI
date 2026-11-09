"""
AI-Native Phase 2, Stage B — semantic-consumption fail-closed refusal.

Covers data.query_planning_service._try_build_ai_consumed_terms_refusal
(unit-level, pure — no DB) and its end-to-end wiring inside
_plan_business_query_impl (integration-level, via the same env/_add_table/
_add_column/_plan fixtures tests/test_business_plan_hints_wiring.py already
uses), plus a regression check that Stage A v2's own ai_entity_hints wiring
(data.semantic_contract_service.apply_grounding) still works unmodified.

Live-confirmed root cause this closes: REG-R-04 "Show students and their
user accounts" -> dbo.SAMPLETBL. extract_terms() splits the question into raw
terms regardless of what the AI understood; when apply_grounding cannot
fully ground every AI-named entity, the code used to fall through into a
broad, unbounded raw-term search over those SAME words, letting an
unrelated table outscore the real entity tables. This suppresses ONLY that
duplicate fallback when every term is already accounted for by the AI's
own validated entities — an unconsumed (leftover) term still flows through
the existing, unmodified legacy path exactly as before.

Deliberately uses a generic employees/managers domain throughout, never
database-specific vocabulary or Golden Set IDs, per the "must work for any SQL
database" requirement this milestone was built under.

Run:
    python -m pytest tests/test_ai_semantic_consumption.py -v
"""
from data.query_planning_service import _try_build_ai_consumed_terms_refusal
from tests.test_phase9_query_planning import env, _add_table, _add_column, _plan
from tests.test_semantic_retrieval_service import _add_generated_vocab


_EMPTY_QUERY_INTENT = {
    "aggregation": None, "aggregation_target": None, "date_range": None,
    "order": None, "null_check_requested": False,
}


def _grounding(*, fully_grounded, grounded=None, matched_entities=None):
    return {
        "fully_grounded": fully_grounded,
        "grounded": grounded or {},
        "matched_entities": matched_entities or [],
        "unsupported": {},
        "seed_table_fqns": set(),
        "entity_table_map": {},
        "entity_trusted_tables": {},
        "status_hints": {},
    }


# ---------------------------------------------------------------------------
# Unit tests — _try_build_ai_consumed_terms_refusal, no DB
# ---------------------------------------------------------------------------

def test_fires_when_every_term_consumed_by_ai_entities():
    # Mirrors REG-R-04's exact shape: AI names "employee"/"manager", raw
    # extract_terms() split "employees"/"their"/"managers" -> ["employees","managers"].
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=False, matched_entities=["Employee"]),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    assert plan is not None
    assert plan["tables"] == []
    assert plan["join_plan"]["required"] is False


def test_multi_word_ai_entity_phrase_consumes_adjacent_raw_terms():
    # AI names the single word "account" but the question's own compound
    # phrase is "support accounts" -- entity_consumed_terms alone only
    # covers "accounts"; the adjacent-phrase sweep must pull in "support"
    # too since it's literally adjacent in the raw question text.
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show customers and their support accounts",
        _grounding(fully_grounded=False, matched_entities=["Customer"]),
        _EMPTY_QUERY_INTENT,
        ["customer", "account"], ["customers", "support", "accounts"], [],
    )
    assert plan is not None
    assert plan["tables"] == []


def test_no_sampletbl_style_disconnected_table_in_refusal_plan():
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=False),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    assert plan["tables"] == []
    assert plan["columns"] == {}
    assert plan["entity_table"] is None


def test_unconsumed_leftover_term_preserves_legacy_path():
    # "region" is not named by any AI entity and shares no words with one
    # -- a genuine leftover the AI never accounted for. Must decline
    # (return None) so the existing deterministic resolver still gets a
    # chance to resolve it, rather than refusing the whole question.
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers by region",
        _grounding(fully_grounded=False, matched_entities=["Employee"]),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers", "region"], [],
    )
    assert plan is None


def test_no_ai_entity_hints_preserves_legacy_path():
    # Missing/absent AI interpretation -> ai_entity_hints is empty ->
    # nothing for this mechanism to do; legacy resolution is untouched.
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=False),
        _EMPTY_QUERY_INTENT,
        [], ["employees", "managers"], [],
    )
    assert plan is None


def test_fully_grounded_preserves_relationship_plan_path():
    # When grounding already fully succeeded, _try_build_relationship_plan
    # (called later) is the one responsible for building the answer — this
    # function must stay out of the way entirely.
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=True, matched_entities=["Employee", "Manager"],
                   grounded={"Employee": {}, "Manager": {}}),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    assert plan is None


def test_aggregation_question_preserves_legacy_path():
    # An aggregate-shaped question has its own already-working handling
    # (_try_build_relationship_aggregate_plan / the legacy measure path);
    # this mechanism must never intercept it.
    query_intent = {**_EMPTY_QUERY_INTENT, "aggregation_target": "entity_count"}
    plan = _try_build_ai_consumed_terms_refusal(
        1, "How many employees report to each manager?",
        _grounding(fully_grounded=False, matched_entities=["Employee"]),
        query_intent,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    assert plan is None


def test_no_search_terms_preserves_legacy_path():
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=False),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], [], [],
    )
    assert plan is None


def test_refusal_message_names_unresolved_entities_not_tables():
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(fully_grounded=False, matched_entities=["Employee"], grounded={}),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    message = plan["business_messages"][0]
    assert "employee" in message and "manager" in message
    # Table-free — never surfaces a physical identifier (schema.table
    # shaped token) to the user.
    assert "dbo." not in message


def test_partially_grounded_reports_only_unresolved_entity():
    # grounded_names comes back title-cased ("Employee") from apply_
    # grounding's own dynamic-candidate convention while ai_entity_hints
    # keeps whatever case the AI returned ("employee") — the comparison
    # must be case-insensitive so the entity that DID ground is correctly
    # excluded from "unresolved" instead of appearing to have failed too.
    plan = _try_build_ai_consumed_terms_refusal(
        1, "Show employees and their managers",
        _grounding(
            fully_grounded=False, matched_entities=["Employee", "Manager"],
            grounded={"Employee": {}},
        ),
        _EMPTY_QUERY_INTENT,
        ["employee", "manager"], ["employees", "managers"], [],
    )
    assert plan["grounded_entities"] == ["Employee"]
    assert plan["unsupported_entities"] == ["manager"]


# ---------------------------------------------------------------------------
# Integration tests — full _plan_business_query_impl wiring
# ---------------------------------------------------------------------------

def _employees_schema(db):
    _add_table(db, "dbo.Employees")
    _add_column(db, "dbo.Employees", "EmployeeID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.Employees", "Salary", data_type="DECIMAL", is_metric=True,
                business_label="Salary", approved=True)


def test_missing_ai_interpretation_preserves_legacy_behavior(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _employees_schema(db)
    # No ai_interpretation key at all in the request -- must resolve
    # exactly as it always did before Stage B existed.
    result = _plan(db, question="total salary", concepts=["salary"], measures=["salary"], dimensions=[])
    assert any(m["selected"] and m["selected"]["column_name"] == "Salary" for m in result["measures"])


def test_ai_interpretation_single_entity_preserves_legacy_behavior(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _employees_schema(db)
    # relationship_intent="single_entity" -> Stage A v2 never populates
    # ai_entity_hints -> Stage B has nothing to consume -> untouched.
    result = _plan(
        db, question="total salary", concepts=["salary"], measures=["salary"], dimensions=[],
        ai_interpretation={
            "entities": ["employee"], "measures": [], "dimensions": [],
            "requested_attributes": [], "relationship_intent": "single_entity",
            "expected_result_shape": "aggregate", "date_range": None, "sorting": None,
            "clarification_required": False, "clarification_reason": None,
        },
    )
    assert any(m["selected"] and m["selected"]["column_name"] == "Salary" for m in result["measures"])


def test_relationship_question_with_one_ambiguous_entity_refuses_safely(tmp_path, monkeypatch):
    # apply_grounding itself is unit-tested exhaustively elsewhere (this
    # file's unit tests above, plus test_dynamic_entity_grounding.py) --
    # here the point is purely the WIRING inside _plan_business_query_impl:
    # a grounding result that isn't fully_grounded, combined with an AI
    # interpretation whose entities consume every search term, must reach
    # _try_build_ai_consumed_terms_refusal and become the actual returned
    # query_plan, in a full plan_business_query() round trip -- not just in
    # isolation. Grounding itself is stubbed so this test isn't coupled to
    # the deterministic scorer's exact internals (explicitly out of scope
    # to touch/depend on this milestone).
    db = env(tmp_path, monkeypatch)
    _employees_schema(db)
    # _plan_business_query_impl imports apply_grounding locally (a fresh
    # `from data.semantic_contract_service import apply_grounding` at call
    # time, not a module-level binding) -- patch it at its actual source.
    import data.semantic_contract_service as scs
    monkeypatch.setattr(
        scs, "apply_grounding",
        lambda *a, **k: {
            "matched_entities": ["Employee", "Manager"], "grounded": {}, "unsupported": {},
            "fully_grounded": False, "seed_table_fqns": set(), "entity_table_map": {},
            "entity_trusted_tables": {}, "status_hints": {},
        },
    )
    result = _plan(
        db, question="Show employees and their managers",
        concepts=["employees", "managers"], measures=["employees", "managers"], dimensions=[],
        ai_interpretation={
            "entities": ["employee", "manager"], "measures": [], "dimensions": [],
            "requested_attributes": [], "relationship_intent": "list_with_related",
            "expected_result_shape": "list", "date_range": None, "sorting": None,
            "clarification_required": False, "clarification_reason": None,
        },
    )
    assert result["tables"] == []
    assert result.get("business_messages")


def test_relationship_question_both_entities_grounded_still_produces_sql(tmp_path, monkeypatch):
    # Both AI-named entities resolve cleanly and a trusted relationship
    # exists -- Stage B must never block a genuinely answerable relationship
    # question; _try_build_relationship_plan should still win. Plain "ID"
    # primary keys (rather than "EmployeeID"/"ManagerID") avoid an
    # unrelated, pre-existing, also-correct mechanism (General Attribute
    # Resolution) that would otherwise read "ManagerID" as evidence
    # "manager" names an ATTRIBUTE of the already-grounded Employee's
    # related tables rather than a second entity in its own right.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Employee")
    _add_column(db, "dbo.EDU_Employee", "ID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.EDU_Manager")
    _add_column(db, "dbo.EDU_Manager", "ID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "employee", "dbo.EDU_Employee", tier="HIGH", column_name="")
    _add_generated_vocab(db, "manager", "dbo.EDU_Manager", tier="HIGH", column_name="")
    from tests.test_semantic_retrieval_service import _add_relationship
    _add_relationship(db, "dbo.EDU_Employee", "dbo.EDU_Manager", status="AUTO")

    result = _plan(
        db, question="Show employees and their managers",
        concepts=["employees", "managers"], measures=["employees", "managers"], dimensions=[],
        ai_interpretation={
            "entities": ["employee", "manager"], "measures": [], "dimensions": [],
            "requested_attributes": [], "relationship_intent": "list_with_related",
            "expected_result_shape": "list", "date_range": None, "sorting": None,
            "clarification_required": False, "clarification_reason": None,
        },
    )
    assert set(result["tables"]) == {"dbo.EDU_Employee", "dbo.EDU_Manager"}


def test_ai_entity_hints_still_ground_dynamic_entity_stage_a_v2(tmp_path, monkeypatch):
    # Stage A v2 regression check: an AI-named entity absent from the fixed
    # ENTITY_TAXONOMY (and never independently found by match_dynamic_
    # entity_terms's own split-token evidence check) still gets a real
    # grounding attempt purely because the AI named it. Plain "ID" primary
    # keys for the same reason as the test above.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Employee")
    _add_column(db, "dbo.EDU_Employee", "ID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_table(db, "dbo.EDU_Manager")
    _add_column(db, "dbo.EDU_Manager", "ID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_generated_vocab(db, "employee", "dbo.EDU_Employee", tier="HIGH", column_name="")
    _add_generated_vocab(db, "manager", "dbo.EDU_Manager", tier="HIGH", column_name="")
    from tests.test_semantic_retrieval_service import _add_relationship
    _add_relationship(db, "dbo.EDU_Employee", "dbo.EDU_Manager", status="AUTO")

    from data.semantic_contract_service import apply_grounding
    grounding = apply_grounding(
        1, "u1", "Show employees and their managers",
        ["employees"], ["employees"], [],
        ai_entity_hints=["employee", "manager"],
    )
    assert grounding["fully_grounded"] is True
    assert set(grounding["matched_entities"]) == {"Employee", "Manager"}


def test_ai_entity_hints_never_merged_into_concepts_or_all_terms(tmp_path, monkeypatch):
    # Stage A v2's own isolation guarantee: an AI hint that names an entity
    # with NO real evidence anywhere must never leak into the legacy
    # raw-term scorer via concepts/measures/dimensions -- confirmed here by
    # checking the returned query_plan's own "concepts" list never grows an
    # entry the caller never supplied.
    db = env(tmp_path, monkeypatch)
    _employees_schema(db)
    result = _plan(
        db, question="total salary", concepts=["salary"], measures=["salary"], dimensions=[],
        ai_interpretation={
            "entities": ["ufo_sighting_registry"], "measures": [], "dimensions": [],
            "requested_attributes": [], "relationship_intent": "list_with_related",
            "expected_result_shape": "list", "date_range": None, "sorting": None,
            "clarification_required": False, "clarification_reason": None,
        },
    )
    concept_terms = {c.get("term") for c in result.get("concepts") or []}
    assert "ufo_sighting_registry" not in concept_terms
    assert any(m["selected"] and m["selected"]["column_name"] == "Salary" for m in result["measures"])
