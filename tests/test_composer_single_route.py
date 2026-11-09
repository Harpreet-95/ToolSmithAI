"""
Phase 8A -- architecture consolidation: the AI-first fast path is the ONE
question-answering route for a connected SQL datasource.

Replaces the per-pipeline routing suites (SQL_NEW_QA_COMPOSER_ENABLED,
AI_FIRST_SQL_COMPOSER_ENABLED, multi-call AI-first, legacy agent,
IntentResolver pre-routing). These tests assert:

  - a data question on an enabled source reaches the fast path and nothing
    else, with IntentResolver / the legacy agent structurally never touched;
  - no automatic fallback: a source that is not enabled (or a disabled
    flag) gets a typed `not_enabled` answer, never another pipeline;
  - follow-up / clarification-answer / cancel / stale-state dispatch;
  - the retired modules and flags no longer exist;
  - POST /sources/{id}/execute-query shares the same single fast path.

Run:
    python -m pytest tests/test_composer_single_route.py -v
"""
from __future__ import annotations

import importlib
import inspect
import os
import uuid

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-composer-single-route-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-composer-single-route-salt-long-enough-1234567")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry

import api.v1.ai_first_composer_execution as execution_module
import api.v1.composer as composer_module
import core.sqlservice.retrieval_store as retrieval_store_module
from api.v1.composer import ComposerRequest, composer_ask
from api.v1.sql_pipeline_composer_state import build_answered_state, build_clarification_state
from auth.api_key import AuthenticatedUser

_USER = "user-1"
_ENABLED_SOURCE = 90004
_OTHER_SOURCE = 1


class _FakeBuildRow:
    def __init__(self, id, schema_fingerprint="fp"):
        self.id = id
        self.schema_fingerprint = schema_fingerprint


def _stub_current_build(monkeypatch, build_id=1):
    monkeypatch.setattr(
        retrieval_store_module, "get_latest_ready_retrieval_build",
        lambda source_id, user_id: _FakeBuildRow(id=build_id),
    )


def _enable_fast_path(monkeypatch, *, source_ids=frozenset({_ENABLED_SOURCE})):
    monkeypatch.setattr("core.config.AI_FIRST_SQL_FAST_PATH_ENABLED", True)
    monkeypatch.setattr("core.config.AI_FIRST_SQL_FAST_PATH_SOURCE_IDS", frozenset(source_ids))


def _poison_retired_paths(monkeypatch):
    """Compatibility helper retained while older test call sites are simplified."""
    return None


def _fake_response(**overrides) -> dict:
    base = {
        "status": "success", "request_id": "r1", "session_id": "s1",
        "business_answer": {}, "resolved_intent": {}, "services_selected": ["live_query"],
        "evidence_summary": {}, "evidence_package": None, "governance_state": None,
        "confidence": 0.0, "execution_time": 0.1, "warnings": [], "errors": [],
        "enterprise_answer": {"answer": "ok", "answer_type": "live_query", "clarification": None},
        "execution_strategy": {"strategy_type": "sql_required", "implementation": "sqlservice_ai_first"},
        "agent_status": "ok", "pipeline_status": "OK", "agent_trace": [], "conversation_state": None,
        "perf_trace": {},
    }
    base.update(overrides)
    return base


def _ask(message: str, *, session_id=None, selected_data_source=_ENABLED_SOURCE, **extra):
    body = ComposerRequest(
        session_id=session_id or str(uuid.uuid4()), message=message,
        selected_data_source=selected_data_source, **extra,
    )
    return composer_ask(body, AuthenticatedUser(role="user", user_id=_USER))


def _record_ask_and_continue(monkeypatch):
    calls = {"ask": [], "continue": []}
    monkeypatch.setattr(
        execution_module, "run_ai_first_fast_path_composer_ask",
        lambda source_id, user_id, question, **k: calls["ask"].append(question) or _fake_response(),
    )
    monkeypatch.setattr(
        execution_module, "run_ai_first_fast_path_composer_continue",
        lambda source_id, user_id, message, **k: calls["continue"].append(
            (message, k.get("is_clarification_resume"))
        ) or _fake_response(),
    )
    return calls


def _answered_state(**overrides):
    base = dict(
        source_id=_ENABLED_SOURCE, build_id=1, schema_fingerprint="fp",
        last_question="Show IPLAN installment payments by month.",
        selected_object_fqns=("dbo.iplan_payments",), selected_output_columns=("month", "amount"),
    )
    base.update(overrides)
    return build_answered_state(**base)


def _clarification_state(**overrides):
    base = dict(
        source_id=_ENABLED_SOURCE, build_id=1, schema_fingerprint="fp",
        last_question="How much did they pay?",
        pending_clarification_question="SQL course fees or IPLAN installment payments?",
        clarification_turns=1,
    )
    base.update(overrides)
    return build_clarification_state(**base)


# ---------------------------------------------------------------------------
# 1. The one route
# ---------------------------------------------------------------------------

def test_data_question_on_enabled_source_uses_fast_path_only(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("IPLAN installment payments")

    assert calls == {"ask": ["IPLAN installment payments"], "continue": []}
    assert result["agent_status"] == "ok"


def test_keyword_free_question_never_needs_an_intent_classifier(monkeypatch):
    """A question with no recognized keyword (the original IntentResolver
    misclassification defect) is answered by the fast path like any other."""
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    _ask("ExampleCo SQL course fees by month")

    assert calls["ask"] == ["ExampleCo SQL course fees by month"]


# ---------------------------------------------------------------------------
# 2. No fallback: not enabled -> typed answer, never another pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enabled,allow", [(False, {_ENABLED_SOURCE}), (True, {99999}), (True, set())])
def test_source_not_enabled_returns_typed_response_and_runs_nothing(monkeypatch, enabled, allow):
    monkeypatch.setattr("core.config.AI_FIRST_SQL_FAST_PATH_ENABLED", enabled)
    monkeypatch.setattr("core.config.AI_FIRST_SQL_FAST_PATH_SOURCE_IDS", frozenset(allow))
    _poison_retired_paths(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("Show enrollments", selected_data_source=_ENABLED_SOURCE)

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "not_enabled"
    assert result["pipeline_status"] == "NOT_ENABLED"
    assert "No query was executed" in result["business_answer"]["answer"]


def test_other_source_is_never_routed_to_a_legacy_pipeline(monkeypatch):
    _enable_fast_path(monkeypatch, source_ids={_ENABLED_SOURCE})
    _poison_retired_paths(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("Show enrollments", selected_data_source=_OTHER_SOURCE)

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "not_enabled"


def test_unexpected_pipeline_failure_is_sanitized_with_no_fallback(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("sk-SECRET-api-key and server=prod;password=hunter2")

    monkeypatch.setattr(execution_module, "_run_ai_first_fast_path_pipeline", _boom)

    result = _ask("How many students are there?")

    assert result["agent_status"] == "execution_failed"
    assert "hunter2" not in repr(result) and "sk-SECRET" not in repr(result)


# ---------------------------------------------------------------------------
# 3. Follow-ups, clarification answers, cancel, stale state
# ---------------------------------------------------------------------------

def test_follow_up_passes_raw_message_to_continuation(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    _stub_current_build(monkeypatch, build_id=1)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("Now show only 2025.", conversation_state=_answered_state())

    assert calls == {"ask": [], "continue": [("Now show only 2025.", False)]}
    assert result["agent_status"] == "ok"


def test_clarification_answer_passes_raw_text_to_continuation(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    _stub_current_build(monkeypatch, build_id=1)
    calls = _record_ask_and_continue(monkeypatch)

    _ask("SQL course fees", conversation_state=_clarification_state())

    assert calls == {"ask": [], "continue": [("SQL course fees", True)]}


def test_no_prior_state_is_a_fresh_ask(monkeypatch):
    _enable_fast_path(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    _ask("How many widgets are there?")

    assert calls == {"ask": ["How many widgets are there?"], "continue": []}


def test_legacy_shaped_state_without_state_type_is_ignored_as_fresh_ask(monkeypatch):
    _enable_fast_path(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    _ask("How many widgets are there?", conversation_state={"conversation_id": "x", "user_id": _USER})

    assert calls == {"ask": ["How many widgets are there?"], "continue": []}


def test_cancel_clarification_runs_no_sql(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    _stub_current_build(monkeypatch, build_id=1)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("never mind", conversation_state=_clarification_state(), cancel_clarification=True)

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "safely_refused"
    assert "No query was executed" in result["business_answer"]["answer"]


def test_cancel_without_any_pending_state_still_runs_no_sql(monkeypatch):
    _enable_fast_path(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("never mind", cancel_clarification=True)

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "safely_refused"


def test_malformed_pipeline_state_is_reset_not_reinterpreted(monkeypatch):
    _enable_fast_path(monkeypatch)
    _poison_retired_paths(monkeypatch)
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("Now show only 2025.", conversation_state={"state_type": "sql_pipeline_conversation", "version": 999})

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "state_reset"


def test_stale_build_state_is_reset(monkeypatch):
    _enable_fast_path(monkeypatch)
    _stub_current_build(monkeypatch, build_id=2)  # state was captured against build 1
    calls = _record_ask_and_continue(monkeypatch)

    result = _ask("Now show only 2025.", conversation_state=_answered_state())

    assert calls == {"ask": [], "continue": []}
    assert result["agent_status"] == "state_reset"


# ---------------------------------------------------------------------------
# 4. Requests with no datasource are not database Q&A
# ---------------------------------------------------------------------------

def test_request_without_datasource_never_reaches_the_fast_path(monkeypatch):
    _enable_fast_path(monkeypatch)

    def _raise(*a, **k):
        raise AssertionError("fast path must not run without a datasource")

    monkeypatch.setattr(execution_module, "answer_with_fast_path", _raise)

    result = _ask("describe the dictionary", selected_data_source=None)

    assert result["status"] == "partial"
    assert result["resolved_intent"]["intent_type"] == "selection_required"
    assert result["services_selected"] == []
    assert result["enterprise_answer"] is None


# ---------------------------------------------------------------------------
# 5. Retired architectures are really gone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", [
    "api.v1.sql_pipeline_composer_adapter",
    "api.v1.sql_pipeline_composer_execution",
    "api.v1.sql_pipeline_execution",
    "api.v1.sql_pipeline_response_adapter",
    "api.v1.agent_response_adapters",
    "core.sqlservice.question_answering_service",
    "core.sqlservice.schema_plan_options",
    "core.sqlservice.sql_review_service",
    "core.sqlservice.schema_selection_correction",
    "core.sqlservice.schema_selection_verifier",
])
def test_retired_module_no_longer_exists(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


@pytest.mark.parametrize("flag", [
    "AI_FIRST_SQL_COMPOSER_ENABLED",
    "AI_FIRST_SQL_COMPOSER_ENABLED_SOURCE_IDS",
    "SQL_NEW_QA_COMPOSER_ENABLED",
    "SQL_NEW_QA_PIPELINE_ENABLED",
    "AI_FIRST_SQL_REVIEW_ENABLED",
    "AI_FIRST_SQL_REVIEW_MODEL",
    "AI_FIRST_SQL_REVIEW_REASONING_EFFORT",
    "AI_FIRST_SQL_REVIEW_TIMEOUT_SECONDS",
])
def test_retired_flag_no_longer_exists(flag):
    import core.config as config

    assert not hasattr(config, flag)


def test_retired_runner_functions_no_longer_exist():
    for name in ("run_ai_first_composer_ask", "run_ai_first_composer_continue", "_run_ai_first_pipeline"):
        assert not hasattr(execution_module, name)


def test_composer_route_source_has_no_retired_references():
    src = inspect.getsource(composer_module.composer_ask)
    for needle in (
        "IntentResolver()", "intent_resolver import", "answer_business_question",
        "run_ai_first_composer_ask", "SQL_NEW_QA", "AI_FIRST_SQL_COMPOSER",
        "build_legacy_composer_response", "_build_conversation_context",
    ):
        assert needle not in src, needle


def test_answer_question_ai_first_multicall_entry_point_is_gone():
    import core.sqlservice.ai_first_result as shared

    assert not hasattr(shared, "answer_question_ai_first")
