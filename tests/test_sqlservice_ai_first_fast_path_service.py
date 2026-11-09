"""
Focused tests — Phase 7, Step 6C: the parallel single-model fast path
(core/sqlservice/ai_first_fast_path_service.py).

Reuses the SAME SQLite fixture plumbing, fakes, and helpers already
established in tests/sqlservice_fixtures.py (via
tests/test_sqlservice_ai_first_question_answering.py's own re-export
pattern) rather than duplicating them.

No real OpenAI call, no real SQL Server, anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_ai_first_fast_path_service.py -v
"""

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
import core.sqlservice.sql_execution as sql_execution_module
import core.sqlservice.ai_first_fast_path_service as fast_path_module
import data.business_knowledge_package_service as business_knowledge_package_service_module
import data.db as data_db_module
from core.sqlservice.ai_first_fast_path_service import answer_question_ai_first_fast_path
from core.sqlservice.ai_first_result import (
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_CONTEXT_BUILD_FAILED,
    STATUS_NON_DATA,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RETRIEVAL_FAILED,
    STATUS_SELECTION_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_VALIDATION_FAILED,
)
from core.sqlservice.fast_path_generation_providers import (
    FastPathGenerationProvider,
    ProviderFastPathGenerationResponse,
)
from core.sqlservice.sql_example_vector_index import clear_sql_example_vector_index_cache
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    SOURCE_ID,
    USER_ID,
    FakeAnswerGenerationProvider,
    FakeConnection,
    FakeCursor,
    FakeEmbeddingProvider,
    _connection_factory_for,
    _prepare_source,
    _wire_execution_lookups,
    template_db_path,  # noqa: F401 -- re-exported fixture, used by db_path below
)


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(business_knowledge_package_service_module, "get_connection", lambda: _make_connection(path))
    return path


from tests.sqlservice_fixtures import _make_connection  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    yield
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()


@pytest.fixture(autouse=True)
def _stub_sql_execution_sqlite_lookups(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: None)
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: None)
    yield


# ── Fake fast-path (combined) provider ──────────────────────────────────


class FakeFastPathProvider(FastPathGenerationProvider):
    def __init__(self, provider_id="fake-fast-path", model="fake-fast-path-model", fixed_raw=None, on_call=None, raise_exc=None):
        self._provider_id = provider_id
        self._model = model
        self._fixed_raw = fixed_raw
        self._on_call = on_call
        self._raise_exc = raise_exc
        self.calls: list = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def generate(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        if self._on_call is not None:
            self._on_call()
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "timeout_seconds": timeout_seconds})
        if self._raise_exc is not None:
            raise self._raise_exc
        raw = self._fixed_raw if self._fixed_raw is not None else _data_question_raw()
        return ProviderFastPathGenerationResponse(raw=raw, model=self._model)


def _intent_raw(**overrides):
    value = {
        "operation": "LIST", "entities": ["widget"], "measures": [], "filters": [],
        "date_constraints": [], "groupings": [], "requested_attributes": [],
    }
    value.update(overrides)
    return value


def _none_resolution_raw(question="placeholder"):
    return {"resolution_type": "none", "resolved_standalone_question": question}


def _data_question_raw(
    selected=("dbo.widgets",), column="id", sql="SELECT id, name FROM dbo.widgets",
    relationships_used=(), **overrides,
):
    value = {
        "request_kind": "DATA_QUESTION",
        "intent": _intent_raw(),
        "selected_object_fqns": list(selected),
        "object_reasons": [{"object_fqn": fqn, "supporting_columns": [column]} for fqn in selected],
        "confirmed_relationships_used": list(relationships_used),
        "sql": sql,
        "clarification_question": None,
        "response_text": None,
        "decision_evidence": ["direct question-term match"],
        "confidence": 0.9,
        "follow_up_resolution": _none_resolution_raw(),
    }
    value.update(overrides)
    return value


def _non_data_raw(response_text="Hi! I can answer questions about your connected data source."):
    return {
        "request_kind": "NON_DATA", "intent": _intent_raw(operation="NONE", entities=[]),
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [],
        "sql": None, "clarification_question": None, "response_text": response_text,
        "decision_evidence": [], "confidence": 0.95,
        "follow_up_resolution": _none_resolution_raw(),
    }


def _unsupported_raw(response_text="This assistant only answers read-only questions and cannot delete data."):
    return {
        "request_kind": "UNSUPPORTED", "intent": _intent_raw(operation="NONE", entities=[]),
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [],
        "sql": None, "clarification_question": None, "response_text": response_text,
        "decision_evidence": [], "confidence": 0.9,
        "follow_up_resolution": _none_resolution_raw(),
    }


def _clarification_raw(question="SQL tuition or IPLAN installment payments?"):
    return {
        "request_kind": "CLARIFICATION_REQUIRED", "intent": _intent_raw(entities=[]),
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [],
        "sql": None, "clarification_question": question, "response_text": None,
        "decision_evidence": [], "confidence": 0.4,
        "follow_up_resolution": _none_resolution_raw(),
    }


def _fast_path_provider(fixed_raw=None, raise_exc=None, on_call=None):
    return FakeFastPathProvider(fixed_raw=fixed_raw, raise_exc=raise_exc, on_call=on_call)


def _run(
    db_path, monkeypatch, build, *,
    embedding_provider=None, fast_path_provider=None, answer_provider=None,
    cursor=None, connection=None, connection_factory=None,
    question="How many widgets are there?",
    prior_turn=None, plan_cache_enabled=None,
):
    _wire_execution_lookups(monkeypatch, build)
    embedding_provider = embedding_provider or FakeEmbeddingProvider()
    fast_path_provider = fast_path_provider or _fast_path_provider()
    if connection_factory is None:
        cursor = cursor or FakeCursor(("id", "name"), [(1, "Widget A"), (2, "Widget B")])
        connection = connection or FakeConnection(cursor)
        connection_factory = _connection_factory_for(connection)
    result = answer_question_ai_first_fast_path(
        question, build.source_id, USER_ID, embedding_provider, fast_path_provider, answer_provider,
        connection_factory=connection_factory, prior_turn=prior_turn, plan_cache_enabled=plan_cache_enabled,
    )
    return result, embedding_provider, fast_path_provider, connection_factory


# ── 1. Short noun phrases are treated as data questions, no keyword needed ─


@pytest.mark.parametrize("question", ["Active students", "IPLAN installment payments", "Class enrollments"])
def test_short_noun_phrase_is_treated_as_data_question(db_path, monkeypatch, question):
    build = _prepare_source(db_path)
    result, embedding_provider, fast_path_provider, connection_factory = _run(db_path, monkeypatch, build, question=question)

    assert result.status == STATUS_OK
    assert len(embedding_provider.calls) == 1
    assert len(fast_path_provider.calls) == 1
    assert len(connection_factory.calls) == 1
    # The literal question text reached the provider verbatim -- no
    # keyword/imperative-verb rewriting happened anywhere in this path.
    assert question in fast_path_provider.calls[0]["user_prompt"]


# ── 2. Greeting produces no SQL, zero execution ─────────────────────────


def test_greeting_produces_no_sql_no_execution(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_non_data_raw())
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, question="Hello",
    )

    assert result.status == STATUS_NON_DATA
    assert result.sql is None
    assert result.answer == "Hi! I can answer questions about your connected data source."
    assert len(connection_factory.calls) == 0


# ── 3. Destructive request produces no SQL, zero execution ─────────────


def test_destructive_request_produces_no_sql_no_execution(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_unsupported_raw())
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, question="Delete all students",
    )

    assert result.status == STATUS_UNSUPPORTED
    assert result.sql is None
    assert len(connection_factory.calls) == 0


# ── 4/5. Intent, object selection, and SQL come from exactly ONE model
#    call -- the old selector/generator/reviewer are structurally
#    unreachable (never imported by this module at all, confirmed by the
#    module's own import list -- this test proves it behaviorally: only
#    ONE provider object exists in this whole flow and it is called
#    exactly once). ───────────────────────────────────────────────────────


def test_exactly_one_combined_call_no_separate_selector_generator_reviewer(db_path, monkeypatch):
    build = _prepare_source(db_path)
    result, embedding_provider, fast_path_provider, connection_factory = _run(db_path, monkeypatch, build)

    assert result.status == STATUS_OK
    assert len(fast_path_provider.calls) == 1
    assert len(embedding_provider.calls) == 1
    # The old selector/generator/reviewer entry points are not even names
    # in this module's namespace -- structurally unreachable, not merely
    # unused. Only ONE provider object was ever constructed/passed into
    # this call (see _run above) for a hidden second call to go through.
    import core.sqlservice.ai_first_fast_path_service as mod
    assert "review_sql_candidate" not in vars(mod)
    assert "select_schema_for_question" not in vars(mod)
    assert "generate_sql_candidate" not in vars(mod)


# ── 6. Provider call count is exactly one GPT call + one embedding call
#    on the normal fast path. ────────────────────────────────────────────


def test_provider_call_count_is_one_gpt_plus_one_embedding(db_path, monkeypatch):
    build = _prepare_source(db_path)
    result, embedding_provider, fast_path_provider, connection_factory = _run(db_path, monkeypatch, build)

    assert result.status == STATUS_OK
    assert len(embedding_provider.calls) == 1
    assert len(fast_path_provider.calls) == 1


# ── 7. Business knowledge reaches the combined prompt ───────────────────


def test_business_knowledge_reaches_combined_prompt(db_path, monkeypatch):
    import core.sqlservice.fast_path_context as fast_path_context_module
    from core.sqlservice.business_knowledge_evidence import BusinessKnowledgeMappingEvidence

    build = _prepare_source(db_path)
    monkeypatch.setattr(fast_path_context_module, "AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS", 180000, raising=False)

    import core.sqlservice.schema_frontier as schema_frontier_module

    original_build_schema_frontier = schema_frontier_module.build_schema_frontier

    def _patched(*args, **kwargs):
        result = original_build_schema_frontier(*args, **kwargs)
        if not result.candidates:
            return result
        import dataclasses
        patched_first = dataclasses.replace(
            result.candidates[0],
            business_knowledge_mappings=(
                BusinessKnowledgeMappingEvidence(
                    concept_id="SQL_COURSE_FEE", concept_type="PAYMENT_CONCEPT", mapping_role="FEE_SOURCE",
                    mapping_status="VERIFIED", confidence=0.95, concept_origin="AUTHORITATIVE", evidence=(),
                ),
            ),
        )
        return dataclasses.replace(result, candidates=(patched_first,) + result.candidates[1:])

    monkeypatch.setattr(fast_path_context_module, "build_schema_frontier", _patched)

    result, embedding_provider, fast_path_provider, connection_factory = _run(db_path, monkeypatch, build)

    assert result.status == STATUS_OK
    assert "BUSINESS_KNOWLEDGE: SQL_COURSE_FEE:FEE_SOURCE(0.95)" in fast_path_provider.calls[0]["user_prompt"]


# ── 8. Unknown selected object is rejected (structural validation) ─────


def test_unknown_selected_object_rejected(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(selected=("dbo.not_a_real_table",)))
    result, *_rest = _run(db_path, monkeypatch, build, fast_path_provider=provider)

    assert result.status == STATUS_SELECTION_FAILED


# ── 9. Fabricated (invented) column is rejected ─────────────────────────


def test_fabricated_column_rejected(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(column="not_a_real_column"))
    result, *_rest = _run(db_path, monkeypatch, build, fast_path_provider=provider)

    assert result.status == STATUS_SELECTION_FAILED


# ── 10. Fabricated join is rejected by the UNCHANGED deterministic
#     validator (never a confirmed relationship column pair). ─────────


def test_fabricated_join_rejected_by_unchanged_validator(db_path, monkeypatch):
    build = _prepare_source(db_path)
    # Two real objects the fixture builds (dbo.widgets, dbo.orders --
    # see tests/sqlservice_fixtures.py's own _prepare_source),
    # joined on a column pair with no confirmed relationship behind it.
    provider = _fast_path_provider(fixed_raw=_data_question_raw(
        selected=("dbo.widgets", "dbo.orders"),
        sql="SELECT w.id FROM dbo.widgets w JOIN dbo.orders o ON w.id = o.id",
        relationships_used=["dbo.widgets.id -> dbo.orders.id"],
    ))
    result, *_rest = _run(db_path, monkeypatch, build, fast_path_provider=provider)

    # Either the structural validator (fast_path_generation_service) or
    # the reused sql_validation AST join check rejects this -- both are
    # acceptable outcomes of "read-only/join validation remains
    # unconditional"; what must never happen is STATUS_OK.
    assert result.status in (STATUS_SELECTION_FAILED, STATUS_VALIDATION_FAILED)


# ── 11. Read-only validation remains unconditional: even a DATA_QUESTION
#     response carrying destructive SQL is rejected by the UNCHANGED
#     deterministic validator, never executed. ─────────────────────────


def test_destructive_sql_in_data_question_response_never_executes(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(sql="DELETE FROM dbo.widgets"))
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
    )

    assert result.status == STATUS_VALIDATION_FAILED
    assert len(connection_factory.calls) == 0


# ── 12. Invalid SQL never executes (generic AST-invalid case) ──────────


def test_invalid_sql_never_executes(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(sql="not even close to valid SQL"))
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
    )

    assert result.status == STATUS_VALIDATION_FAILED
    assert len(connection_factory.calls) == 0


# ── 13. Genuine ambiguity (combined call itself asks) clarifies ────────


def test_genuine_fee_ambiguity_clarifies(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_clarification_raw("SQL tuition or IPLAN installment payments?"))
    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, question="How much have they paid?",
    )

    assert result.status == STATUS_CLARIFICATION_REQUIRED
    assert result.clarification_question == "SQL tuition or IPLAN installment payments?"
    assert len(connection_factory.calls) == 0


# ── 14. Simple counts use deterministic formatting, zero answer calls ──


def test_simple_scalar_count_uses_deterministic_formatting(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(sql="SELECT COUNT(*) AS TotalWidgets FROM dbo.widgets"))
    answer_provider = FakeAnswerGenerationProvider()
    cursor = FakeCursor(("TotalWidgets",), [(12000,)])
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)

    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, answer_provider=answer_provider,
        connection_factory=connection_factory,
    )

    assert result.status == STATUS_OK
    assert result.answer == "There are 12,000 widgets."
    assert len(answer_provider.calls) == 0


# ── 15. Simple lists use deterministic formatting ───────────────────────


def test_simple_list_uses_deterministic_formatting(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw())
    answer_provider = FakeAnswerGenerationProvider()
    cursor = FakeCursor(("id", "name"), [(1, "Widget A"), (2, "Widget B"), (3, "Widget C")])
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)

    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, answer_provider=answer_provider,
        connection_factory=connection_factory,
    )

    assert result.status == STATUS_OK
    assert result.answer == "Found 3 matching record(s)."
    assert len(answer_provider.calls) == 0


# ── 16. Zero-row results use deterministic formatting ───────────────────


def test_zero_row_result_uses_deterministic_formatting(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw())
    answer_provider = FakeAnswerGenerationProvider()
    cursor = FakeCursor(("id", "name"), [])
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)

    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, answer_provider=answer_provider,
        connection_factory=connection_factory,
    )

    assert result.status == STATUS_OK
    assert result.answer == "No matching records were found."
    assert len(answer_provider.calls) == 0


# ── 17. Unfamiliar filter values generate valid filters rather than a
#     schema clarification (the model's own free judgment, but this
#     proves nothing in the deterministic post-processing layer forces
#     a clarification merely because a filter value is unfamiliar). ────


def test_unfamiliar_filter_value_does_not_trigger_schema_clarification(db_path, monkeypatch):
    build = _prepare_source(db_path)
    raw = _data_question_raw(sql="SELECT id, name FROM dbo.widgets WHERE name = 'Zzyzx Robotics'")
    raw["intent"]["filters"] = ["name = 'Zzyzx Robotics'"]
    provider = _fast_path_provider(fixed_raw=raw)

    result, embedding_provider, fast_path_provider, connection_factory = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, question="Widgets in Zzyzx Robotics",
    )

    assert result.status == STATUS_OK
    assert len(connection_factory.calls) == 1


# ── 18. Not-prepared / retrieval failure short-circuits with zero calls ─


def test_not_prepared_short_circuits(db_path, monkeypatch):
    from tests.sqlservice_fixtures import _insert_snapshot, _insert_source

    _insert_source(db_path)
    embedding_provider = FakeEmbeddingProvider()
    provider = _fast_path_provider()
    result = answer_question_ai_first_fast_path(
        "How many widgets?", SOURCE_ID, USER_ID, embedding_provider, provider, None,
    )
    assert result.status == STATUS_NOT_PREPARED
    assert len(embedding_provider.calls) == 0
    assert len(provider.calls) == 0


# ── 19. Composer-execution WIRING layer never constructs a legacy provider ─
#
# Tests 5/6 above (test_exactly_one_combined_call_no_separate_selector_
# generator_reviewer / test_provider_call_count_is_one_gpt_plus_one_
# embedding) prove this module's own answer_question_ai_first_fast_path()
# only ever makes one embedding + one combined-generation call when given
# directly-injected fake providers. They do NOT exercise api.v1.ai_first_
# composer_execution._run_ai_first_fast_path_pipeline — the ONE place in
# the whole app that actually CONSTRUCTS the real OpenAI* provider objects
# for a live HTTP request (api/v1/composer.py routes straight to
# run_ai_first_fast_path_composer_ask, which calls that private helper). A
# regression there (e.g. someone later adds an OpenAISchemaSelectionProvider/
# OpenAITextToSqlProvider construction to "help" the fast path) would be
# invisible to every test above, since they never import that module at
# all. This test closes exactly that gap: it monkeypatches the five
# provider CLASSES api.v1.ai_first_composer_execution imports (poisoning
# the two legacy ones so merely constructing one fails the test, faking the
# three fast-path ones so no real network call happens), then calls the
# REAL, unmocked run_ai_first_fast_path_composer_ask -- the exact function
# api/v1/composer.py's HTTP route calls for an AI-first-fast-path-enabled
# source -- end to end.
def test_composer_execution_wiring_constructs_only_the_fast_path_providers(db_path, monkeypatch):
    import api.v1.ai_first_composer_execution as composer_execution_module

    build = _prepare_source(db_path)
    _wire_execution_lookups(monkeypatch, build)

    # Phase 8A -- the separate selector / text-to-SQL / reviewer providers are
    # gone from the composer execution layer entirely.
    for retired in ("OpenAISchemaSelectionProvider", "OpenAITextToSqlProvider"):
        assert not hasattr(composer_execution_module, retired)

    embedding_fake = FakeEmbeddingProvider()
    fast_path_fake = _fast_path_provider()
    answer_fake = FakeAnswerGenerationProvider()

    monkeypatch.setattr(composer_execution_module, "OpenAIEmbeddingProvider", lambda: embedding_fake)
    monkeypatch.setattr(composer_execution_module, "OpenAIFastPathGenerationProvider", lambda: fast_path_fake)
    monkeypatch.setattr(composer_execution_module, "OpenAIAnswerGenerationProvider", lambda: answer_fake)

    # No real connection_factory is reachable from this layer (the private
    # helper never accepts one -- production always resolves a real live
    # connection). That is fine: embedding + combined-generation both run
    # on local/fixture DB reads, well before SQL execution would need a
    # live connection, so the one assertion this test cares about (provider
    # construction/call counts) is already decided by the time any
    # execution-stage failure would occur. Never raises -- same
    # credential-safe contract as every other composer_execution entry
    # point.
    response = composer_execution_module.run_ai_first_fast_path_composer_ask(
        build.source_id, USER_ID, "How many widgets are there?",
        session_id="s-wiring-probe", request_id="r-wiring-probe",
        start_time=__import__("time").perf_counter(),
    )

    assert isinstance(response, dict)
    assert len(embedding_fake.calls) == 1
    assert len(fast_path_fake.calls) == 1


# ── Phase 7, Step 6F, Part 3/4 — prior_turn threading ───────────────────


def _prior_turn(**overrides):
    from core.sqlservice.fast_path_prior_turn import FastPathPriorTurnContext

    base = dict(
        previous_user_question="How many widgets are there?",
        resolved_standalone_question="How many widgets are there?",
        selected_object_fqns=("dbo.widgets",), selected_output_columns=("id",),
        filters=(), groupings=(), date_constraints=(), operation="COUNT",
        requested_attributes=(), pending_clarification_question=None,
        is_clarification_resume=False, schema_fingerprint="fp",
        business_knowledge_package_id=None, business_knowledge_package_version=None,
        view_profile_state=(), secret_policy_version=None, source_id=SOURCE_ID,
        conversation_id="conv-1", user_id=USER_ID,
    )
    base.update(overrides)
    return FastPathPriorTurnContext(**base)


def _continuation_raw(resolved_standalone_question, **overrides):
    from core.sqlservice.fast_path_generation_service import RESOLUTION_TYPE_CONTINUATION

    raw = _data_question_raw(**overrides)
    raw["follow_up_resolution"] = {
        "resolution_type": RESOLUTION_TYPE_CONTINUATION,
        "resolved_standalone_question": resolved_standalone_question,
    }
    return raw


def test_follow_up_embeds_concatenation_of_prior_question_and_raw_message(db_path, monkeypatch):
    """Stage 1's one embedding call is built from a deterministic
    concatenation (prior resolved question + this turn's raw message) --
    never the raw message alone -- so a low-content follow-up like "Only
    active ones." still retrieves a relevant candidate frontier."""
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_continuation_raw("How many active widgets are there?"))
    result, embedding_provider, _, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="Only active ones.", prior_turn=_prior_turn(),
    )

    assert result.status == STATUS_OK
    assert embedding_provider.calls == [["How many widgets are there? Only active ones."]]


def test_follow_up_resolves_standalone_question_and_builds_next_turn_context(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_continuation_raw(
        "How many active widgets are there?",
        selected=("dbo.widgets",), column="id", sql="SELECT COUNT(*) FROM dbo.widgets",
    ))
    result, _, fast_path_provider_used, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="Only active ones.", prior_turn=_prior_turn(),
    )

    assert result.status == STATUS_OK
    assert result.resolved_standalone_question == "How many active widgets are there?"
    assert result.follow_up_resolution_type == "continuation"
    assert result.next_turn_context is not None
    assert result.next_turn_context["selected_object_fqns"] == ("dbo.widgets",)
    # Raw message never reaches the model as a pre-combined template string
    # -- it is passed through verbatim, with CONVERSATION_CONTEXT separate.
    assert "Only active ones." in fast_path_provider_used.calls[0]["user_prompt"]
    assert "CONVERSATION_CONTEXT" in fast_path_provider_used.calls[0]["user_prompt"]


def test_fresh_question_gets_none_resolution_type_and_still_builds_next_turn_context(db_path, monkeypatch):
    """A fresh (non-follow-up) question has nothing to resolve against
    (resolution_type "none"), but still populates next_turn_context --
    every successfully answered turn builds forward context for a LATER
    follow-up to it, not only turns that themselves were follow-ups."""
    build = _prepare_source(db_path)
    raw = _data_question_raw()
    raw["follow_up_resolution"] = {
        "resolution_type": "none", "resolved_standalone_question": "How many widgets are there?",
    }
    provider = _fast_path_provider(fixed_raw=raw)
    result, _, _, _ = _run(db_path, monkeypatch, build, fast_path_provider=provider, question="How many widgets are there?")

    assert result.status == STATUS_OK
    assert result.resolved_standalone_question == "How many widgets are there?"
    assert result.follow_up_resolution_type == "none"
    assert result.next_turn_context is not None
    assert result.next_turn_context["selected_object_fqns"] == ("dbo.widgets",)


def test_unrelated_follow_up_does_not_inherit_prior_objects(db_path, monkeypatch):
    """A genuinely unrelated new question (after a completed turn) must
    select objects based on its OWN evidence, never the stale prior
    turn's selection -- the model's own classification is what the
    downstream deterministic stages (unchanged) then execute against."""
    from core.sqlservice.fast_path_generation_service import RESOLUTION_TYPE_UNRELATED

    build = _prepare_source(db_path)
    raw = _data_question_raw(selected=("dbo.widgets",), column="name", sql="SELECT name FROM dbo.widgets")
    raw["follow_up_resolution"] = {
        "resolution_type": RESOLUTION_TYPE_UNRELATED, "resolved_standalone_question": "List widget names.",
    }
    provider = _fast_path_provider(fixed_raw=raw)

    result, _, _, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="List widget names.", prior_turn=_prior_turn(),
    )

    assert result.status == STATUS_OK
    assert result.follow_up_resolution_type == "unrelated"
    assert result.resolved_standalone_question == "List widget names."


def test_clarification_required_response_still_carries_resolved_question(db_path, monkeypatch):
    """resolved_standalone_question/follow_up_resolution_type must be set
    even on an early CLARIFICATION_REQUIRED return, not only on the final
    STATUS_OK path -- Part 5's clarification continuity needs the standalone
    restatement regardless of whether this round still needs clarification."""
    from core.sqlservice.fast_path_generation_service import RESOLUTION_TYPE_CLARIFICATION_ANSWER

    build = _prepare_source(db_path)
    raw = _clarification_raw("SQL course fees or IPLAN installment payments?")
    raw["follow_up_resolution"] = {
        "resolution_type": RESOLUTION_TYPE_CLARIFICATION_ANSWER,
        "resolved_standalone_question": "How much did students pay for SQL course fees?",
    }
    provider = _fast_path_provider(fixed_raw=raw)

    result, _, _, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="SQL course fees", prior_turn=_prior_turn(
            is_clarification_resume=True, pending_clarification_question="SQL course fees or IPLAN installment payments?",
        ),
    )

    assert result.status == STATUS_CLARIFICATION_REQUIRED
    assert result.resolved_standalone_question == "How much did students pay for SQL course fees?"
    assert result.follow_up_resolution_type == "clarification_answer"


@pytest.fixture(autouse=True)
def _isolated_fast_path_plan_cache():
    """These Part 4 cache tests are the only ones in this file that ever
    pass plan_cache_enabled=True -- the module-level in-memory cache
    (core.sqlservice.fast_path_plan_cache) otherwise persists across
    tests in the same process, which would let one test's stored plan
    leak into another's "fresh" first call."""
    from core.sqlservice.fast_path_plan_cache import clear_fast_path_plan_cache

    clear_fast_path_plan_cache()
    yield
    clear_fast_path_plan_cache()


def test_identical_follow_up_pair_is_a_cache_hit_zero_provider_calls(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_continuation_raw("How many active widgets are there?"))

    first, embedding_1, fast_path_1, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="Only active ones.", prior_turn=_prior_turn(), plan_cache_enabled=True,
    )
    assert first.status == STATUS_OK
    assert len(embedding_1.calls) == 1
    assert len(fast_path_1.calls) == 1
    calls_before_second = len(fast_path_1.calls)

    # Same `provider`/`embedding_1` instance is reused for both calls (the
    # test passes the same fake in twice) -- a cache hit means the SECOND
    # call adds ZERO new entries to its already-accumulated call list.
    second, embedding_2, fast_path_2, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider, embedding_provider=embedding_1,
        question="Only active ones.", prior_turn=_prior_turn(), plan_cache_enabled=True,
    )
    assert second.status == STATUS_OK
    assert second.cache_hit is True
    assert len(embedding_2.calls) == 1  # unchanged from calls_before_second
    assert len(fast_path_2.calls) == calls_before_second  # unchanged -- zero NEW calls


def test_same_follow_up_text_after_a_different_prior_question_is_a_cache_miss(db_path, monkeypatch):
    """A follow-up's cache identity includes the PRIOR question, not just
    this turn's raw message -- "Only active ones." means something
    different depending on what it is following up on."""
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_continuation_raw("How many active widgets are there?"))

    first, _, fast_path_1, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="Only active ones.", prior_turn=_prior_turn(resolved_standalone_question="How many widgets are there?"),
        plan_cache_enabled=True,
    )
    assert first.status == STATUS_OK
    assert len(fast_path_1.calls) == 1
    calls_before_second = len(fast_path_1.calls)

    second, _, fast_path_2, _ = _run(
        db_path, monkeypatch, build, fast_path_provider=provider,
        question="Only active ones.",
        prior_turn=_prior_turn(resolved_standalone_question="How many orders are there?"),
        plan_cache_enabled=True,
    )
    assert second.status == STATUS_OK
    assert second.cache_hit is False
    assert len(fast_path_2.calls) == calls_before_second + 1  # one NEW call -- a genuine miss
