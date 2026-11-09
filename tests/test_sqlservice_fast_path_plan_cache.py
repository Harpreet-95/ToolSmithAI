"""
Focused tests — Phase 7, Step 6E, Part 2: the bounded validated-SQL-plan
cache for the fast path (core/sqlservice/fast_path_plan_cache.py), wired
into core/sqlservice/ai_first_fast_path_service.py.

Two layers:
  1. Pure unit tests against core.sqlservice.fast_path_plan_cache directly
     (key identity, TTL, max-entries, isolation-by-field) -- no database.
  2. Pipeline-level tests reusing the SAME SQLite fixture harness as
     tests/test_sqlservice_ai_first_fast_path_service.py (via
     tests/sqlservice_fixtures.py's re-exported helpers).

No real OpenAI call, no real SQL Server, anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_fast_path_plan_cache.py -v
"""

import dataclasses
import time

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
    STATUS_NON_DATA,
    STATUS_OK,
    STATUS_VALIDATION_FAILED,
)
from core.sqlservice.fast_path_plan_cache import (
    FastPathCachedPlan,
    build_fast_path_plan_cache_key,
    clear_fast_path_plan_cache,
    fast_path_plan_cache_size,
    get_cached_fast_path_plan,
    normalize_fast_path_question,
    store_fast_path_plan,
)
from core.sqlservice.fast_path_generation_service import FastPathIntent
from core.sqlservice.sql_validation import STATUS_REJECTED as _VALID_STATUS_REJECTED
from core.sqlservice.sql_example_vector_index import clear_sql_example_vector_index_cache
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.test_sqlservice_ai_first_fast_path_service import (
    FakeFastPathProvider,
    _data_question_raw,
    _fast_path_provider,
    _non_data_raw,
)
from tests.sqlservice_fixtures import (
    SOURCE_ID,
    USER_ID,
    FakeConnection,
    FakeCursor,
    FakeEmbeddingProvider,
    _connection_factory_for,
    _make_connection,
    _prepare_source,
    _wire_execution_lookups,
    template_db_path,  # noqa: F401 -- re-exported fixture
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


@pytest.fixture(autouse=True)
def _isolated_caches():
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    clear_fast_path_plan_cache()
    yield
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    clear_fast_path_plan_cache()


@pytest.fixture(autouse=True)
def _stub_sql_execution_sqlite_lookups(monkeypatch):
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: None)
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: None)
    yield


def _base_key(**overrides):
    defaults = dict(
        question="How many widgets are there?", user_id="28", source_id=90004,
        schema_fingerprint="fp-a", business_knowledge_package_id=1, business_knowledge_package_version=1,
        fast_path_model="gpt-6-sol", fast_path_reasoning_effort="medium", fast_path_prompt_version="6C-1",
    )
    defaults.update(overrides)
    return build_fast_path_plan_cache_key(**defaults)


def _plan(**overrides):
    defaults = dict(
        intent=FastPathIntent(
            operation="COUNT", entities=("widget",), measures=(), filters=(),
            date_constraints=(), groupings=(), requested_attributes=(),
        ),
        selected_object_fqns=("dbo.widgets",), object_reasons=(), sql="SELECT COUNT(*) FROM dbo.widgets",
        decision_evidence=("direct match",), confidence=0.9, model="gpt-6-sol",
        resolved_standalone_question="How many widgets are there?", follow_up_resolution_type="none",
    )
    defaults.update(overrides)
    return FastPathCachedPlan(**defaults)


# ── Layer 1: pure cache-module unit tests ───────────────────────────────


def test_normalize_question_collapses_whitespace_and_case():
    assert normalize_fast_path_question("  How Many   Widgets  ") == "how many widgets"


def test_store_then_get_is_a_hit():
    key = _base_key()
    store_fast_path_plan(key, _plan())
    assert get_cached_fast_path_plan(key) is not None


def test_get_miss_returns_none():
    assert get_cached_fast_path_plan(_base_key()) is None


@pytest.mark.parametrize("field,value", [
    ("user_id", "99"),
    ("source_id", 99999),
    ("schema_fingerprint", "fp-b"),
    ("business_knowledge_package_id", 2),
    ("business_knowledge_package_version", 2),
    ("fast_path_model", "different-model"),
    ("fast_path_reasoning_effort", "high"),
    ("fast_path_prompt_version", "6C-2"),
    ("question", "How many widgets exist?"),
])
def test_any_identity_field_change_misses_the_cache(field, value):
    store_fast_path_plan(_base_key(), _plan())
    assert get_cached_fast_path_plan(_base_key(**{field: value})) is None


def test_cross_user_isolation_never_serves_another_users_plan():
    store_fast_path_plan(_base_key(user_id="28"), _plan())
    assert get_cached_fast_path_plan(_base_key(user_id="99")) is None


def test_cross_source_isolation_never_serves_another_sources_plan():
    store_fast_path_plan(_base_key(source_id=90004), _plan())
    assert get_cached_fast_path_plan(_base_key(source_id=20000)) is None


def test_view_profile_change_invalidates_cached_plan():
    # Phase 7, Step 6E3B1, Part 10 -- a view-profile recomputation (a
    # changed definition, schema fingerprint, or VIEW_PROFILE_CONTRACT_
    # VERSION bump) changes this opaque tuple and must miss the cache.
    store_fast_path_plan(_base_key(view_profile_state=(3, "2026-09-30T00:00:00Z")), _plan())
    assert get_cached_fast_path_plan(_base_key(view_profile_state=(3, "2026-10-01T00:00:00Z"))) is None
    assert get_cached_fast_path_plan(_base_key(view_profile_state=(3, "2026-09-30T00:00:00Z"))) is not None


def test_secret_policy_version_change_invalidates_cached_plan():
    # Phase 7, Step 6E3B2, Part 3 -- a widened/narrowed secret-column
    # policy (core.dictionary.pii_detector.SECRET_POLICY_VERSION) must
    # invalidate every previously cached plan rather than silently
    # reusing one validated under a stale policy.
    store_fast_path_plan(_base_key(secret_policy_version=1), _plan())
    assert get_cached_fast_path_plan(_base_key(secret_policy_version=2)) is None
    assert get_cached_fast_path_plan(_base_key(secret_policy_version=1)) is not None


def test_ttl_expiry_evicts_and_misses(monkeypatch):
    import core.sqlservice.fast_path_plan_cache as plan_cache_module

    key = _base_key()
    clock = {"t": 1000.0}
    monkeypatch.setattr(plan_cache_module.time, "monotonic", lambda: clock["t"])
    store_fast_path_plan(key, _plan())
    clock["t"] += 61.0  # past a 60s TTL
    assert get_cached_fast_path_plan(key, ttl_seconds=60) is None
    # And it was actually evicted, not merely hidden this call:
    assert fast_path_plan_cache_size() == 0


def test_ttl_zero_or_negative_means_no_expiry():
    key = _base_key()
    store_fast_path_plan(key, _plan())
    assert get_cached_fast_path_plan(key, ttl_seconds=0) is not None


def test_max_entries_evicts_oldest_first():
    for i in range(5):
        store_fast_path_plan(_base_key(question=f"question {i}"), _plan(), max_entries=3)
    assert fast_path_plan_cache_size() == 3
    assert get_cached_fast_path_plan(_base_key(question="question 0")) is None
    assert get_cached_fast_path_plan(_base_key(question="question 4")) is not None


# ── Layer 2: pipeline-level tests ───────────────────────────────────────


def _run_cached(
    db_path, monkeypatch, build, *,
    embedding_provider=None, fast_path_provider=None, question="How many widgets are there?",
    plan_cache_enabled=True, connection_rows=None,
):
    _wire_execution_lookups(monkeypatch, build)
    embedding_provider = embedding_provider or FakeEmbeddingProvider()
    fast_path_provider = fast_path_provider or _fast_path_provider()
    rows = connection_rows if connection_rows is not None else [(2,)]
    cursor = FakeCursor(("Count",), rows)
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)
    result = answer_question_ai_first_fast_path(
        question, build.source_id, USER_ID, embedding_provider, fast_path_provider, None,
        connection_factory=connection_factory, plan_cache_enabled=plan_cache_enabled,
    )
    return result, embedding_provider, fast_path_provider, connection_factory


def test_first_request_makes_one_embedding_and_one_model_call(db_path, monkeypatch):
    build = _prepare_source(db_path)
    result, embedding_provider, fast_path_provider, connection_factory = _run_cached(db_path, monkeypatch, build)

    assert result.status == STATUS_OK
    assert len(embedding_provider.calls) == 1
    assert len(fast_path_provider.calls) == 1
    assert len(connection_factory.calls) == 1


def test_identical_repeat_makes_zero_embedding_and_model_calls_but_reexecutes(db_path, monkeypatch):
    build = _prepare_source(db_path)
    first, _, _, first_conn = _run_cached(db_path, monkeypatch, build)
    assert first.status == STATUS_OK

    second, embedding_provider_2, fast_path_provider_2, second_conn = _run_cached(db_path, monkeypatch, build)

    assert second.status == STATUS_OK
    assert len(embedding_provider_2.calls) == 0
    assert len(fast_path_provider_2.calls) == 0
    # SQL is still re-executed live, against a FRESH connection factory
    # this second call constructed -- proves it is not merely replaying a
    # stored result.
    assert len(second_conn.calls) == 1
    assert first_conn is not second_conn


def test_disabling_plan_cache_never_hits_even_for_an_identical_repeat(db_path, monkeypatch):
    build = _prepare_source(db_path)
    _run_cached(db_path, monkeypatch, build, plan_cache_enabled=False)
    second, embedding_provider_2, fast_path_provider_2, _ = _run_cached(
        db_path, monkeypatch, build, plan_cache_enabled=False,
    )
    assert second.status == STATUS_OK
    assert len(embedding_provider_2.calls) == 1
    assert len(fast_path_provider_2.calls) == 1


def test_non_data_response_is_never_cached(db_path, monkeypatch):
    build = _prepare_source(db_path)
    greeting_provider = _fast_path_provider(fixed_raw=_non_data_raw())
    first, _, _, _ = _run_cached(db_path, monkeypatch, build, fast_path_provider=greeting_provider, question="Hello")
    assert first.status == STATUS_NON_DATA
    assert fast_path_plan_cache_size() == 0

    second, embedding_provider_2, fast_path_provider_2, _ = _run_cached(
        db_path, monkeypatch, build, fast_path_provider=_fast_path_provider(fixed_raw=_non_data_raw()), question="Hello",
    )
    # Still a fresh call every time -- never served from a cache that was
    # never populated.
    assert len(embedding_provider_2.calls) == 1
    assert len(fast_path_provider_2.calls) == 1


def test_cache_survives_only_after_successful_execution_not_before(db_path, monkeypatch):
    build = _prepare_source(db_path)
    # A validation failure on the very first (necessarily cache-miss) call
    # must never populate the cache.
    original = fast_path_module.validate_sql_candidate

    def _always_reject(*args, **kwargs):
        result = original(*args, **kwargs)
        return dataclasses.replace(result, status=_VALID_STATUS_REJECTED, error="forced-for-test")

    monkeypatch.setattr(fast_path_module, "validate_sql_candidate", _always_reject)
    result, _, _, _ = _run_cached(db_path, monkeypatch, build)
    assert result.status == STATUS_VALIDATION_FAILED
    assert fast_path_plan_cache_size() == 0


def test_cached_sql_rejected_by_fresh_validation_is_evicted_and_not_reused(db_path, monkeypatch):
    build = _prepare_source(db_path)
    first, _, _, _ = _run_cached(db_path, monkeypatch, build)
    assert first.status == STATUS_OK
    assert fast_path_plan_cache_size() == 1

    original = fast_path_module.validate_sql_candidate
    call_count = {"n": 0}

    def _reject_second_call(*args, **kwargs):
        call_count["n"] += 1
        result = original(*args, **kwargs)
        if call_count["n"] == 1:  # the replay triggered by THIS test's second _run_cached call
            return dataclasses.replace(result, status=_VALID_STATUS_REJECTED, error="forced-for-test")
        return result

    monkeypatch.setattr(fast_path_module, "validate_sql_candidate", _reject_second_call)

    second, embedding_provider_2, fast_path_provider_2, _ = _run_cached(db_path, monkeypatch, build)
    assert second.status == STATUS_VALIDATION_FAILED
    # It WAS a cache hit (zero fresh embedding/model calls) even though
    # revalidation then rejected it.
    assert len(embedding_provider_2.calls) == 0
    assert len(fast_path_provider_2.calls) == 0
    assert fast_path_plan_cache_size() == 0  # evicted, never retried forever

    monkeypatch.setattr(fast_path_module, "validate_sql_candidate", original)
    third, embedding_provider_3, fast_path_provider_3, _ = _run_cached(db_path, monkeypatch, build)
    assert third.status == STATUS_OK
    assert len(embedding_provider_3.calls) == 1  # evicted entry forced a fresh miss
    assert len(fast_path_provider_3.calls) == 1


def test_deterministic_insights_attached_on_a_multi_row_result(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = _fast_path_provider(fixed_raw=_data_question_raw(
        column="name", sql="SELECT name, id FROM dbo.widgets",
    ))
    # FakeCursor's declared column labels are simulated return values,
    # independent of the SQL text (validation is AST-only, never checks
    # cursor.description) -- "Count" here (not "id") so the deterministic
    # insight builder's own surrogate-id-column exclusion doesn't strip
    # the one numeric column this test needs.
    cursor_rows = [("Data Analytics", 120), ("UX Design", 80)]

    _wire_execution_lookups(monkeypatch, build)
    embedding_provider = FakeEmbeddingProvider()
    cursor = FakeCursor(("name", "Count"), cursor_rows)
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)
    result = answer_question_ai_first_fast_path(
        "Show name and id by group", build.source_id, USER_ID, embedding_provider, provider, None,
        connection_factory=connection_factory, plan_cache_enabled=True,
    )
    assert result.status == STATUS_OK
    assert any(i.provenance == "deterministic_result_analysis" for i in result.insights)
