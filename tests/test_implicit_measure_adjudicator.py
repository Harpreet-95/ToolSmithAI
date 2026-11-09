"""
Tests for core/semantic/implicit_measure_adjudicator.py — Reconnect
Reliability / Implicit Activity Measure milestone.

Mirrors tests/test_candidate_adjudicator.py's structure: strict identifier-
lock validation scoped to the evidence given, a fail-closed contract (no API
key, no openai package, no relationships, API exception, malformed/
hallucinated identifiers all return None rather than raising), and a happy
path with a mocked OpenAI client.

Run:
    python -m pytest tests/test_implicit_measure_adjudicator.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from core.semantic.implicit_measure_adjudicator import (
    ImplicitMeasureResult,
    SchemaViolation,
    _validate,
    infer_activity_measure,
)


def _table_context():
    return {
        "columns": [
            {"column_name": "ClientID"},
            {"column_name": "IsActive"},
            {"column_name": "LastLogin"},
        ],
    }


def _relationships():
    return [
        {
            "related_table_fqn": "dbo.signups", "fk_column": "ClientID",
            "columns": ["SignupID", "ClientID", "SignupDate"], "has_primary_key": True,
        },
        {
            "related_table_fqn": "dbo.client_categories", "fk_column": "ClientID",
            "columns": ["ClientID", "Category"], "has_primary_key": False,
        },
    ]


def _mock_client_returning(content: str):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=content))]
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# _validate — identifier-lock
# ---------------------------------------------------------------------------

def test_validate_accepts_related_row_count():
    raw = {
        "kind": "related_row_count", "related_table_fqn": "dbo.signups",
        "related_fk_column": "ClientID", "confidence": 0.9,
        "evidence_reference": "Signups is a real activity/event table.",
    }
    result = _validate(raw, "dbo.clients", {"ClientID", "IsActive", "LastLogin"}, _relationships())
    assert result == ImplicitMeasureResult(
        kind="related_row_count", related_table_fqn="dbo.signups",
        related_fk_column="ClientID", confidence=0.9,
        evidence_reference="Signups is a real activity/event table.",
    )


def test_validate_accepts_date_recency():
    raw = {"kind": "date_recency", "date_column": "LastLogin", "confidence": 0.7}
    result = _validate(raw, "dbo.clients", {"ClientID", "IsActive", "LastLogin"}, _relationships())
    assert result.kind == "date_recency"
    assert result.date_column == "LastLogin"


def test_validate_returns_none_for_kind_none():
    raw = {"kind": "none", "confidence": 0.1}
    assert _validate(raw, "dbo.clients", {"ClientID"}, _relationships()) is None


def test_validate_rejects_hallucinated_related_table():
    raw = {
        "kind": "related_row_count", "related_table_fqn": "dbo.made_up_table",
        "related_fk_column": "ClientID", "confidence": 0.9,
    }
    try:
        _validate(raw, "dbo.clients", {"ClientID"}, _relationships())
        assert False, "expected SchemaViolation"
    except SchemaViolation:
        pass


def test_validate_rejects_hallucinated_fk_column():
    raw = {
        "kind": "related_row_count", "related_table_fqn": "dbo.signups",
        "related_fk_column": "NotARealColumn", "confidence": 0.9,
    }
    try:
        _validate(raw, "dbo.clients", {"ClientID"}, _relationships())
        assert False, "expected SchemaViolation"
    except SchemaViolation:
        pass


def test_validate_rejects_hallucinated_date_column():
    raw = {"kind": "date_recency", "date_column": "NotARealColumn", "confidence": 0.9}
    try:
        _validate(raw, "dbo.clients", {"ClientID", "IsActive", "LastLogin"}, _relationships())
        assert False, "expected SchemaViolation"
    except SchemaViolation:
        pass


def test_validate_rejects_unrecognized_kind():
    raw = {"kind": "sql_injection_attempt", "confidence": 0.9}
    try:
        _validate(raw, "dbo.clients", {"ClientID"}, _relationships())
        assert False, "expected SchemaViolation"
    except SchemaViolation:
        pass


# ---------------------------------------------------------------------------
# infer_activity_measure() — fail-closed contract
# ---------------------------------------------------------------------------

def test_returns_none_without_api_key():
    assert infer_activity_measure(
        "dbo.clients", _table_context(), _relationships(), api_key="",
    ) is None


def test_returns_none_on_empty_relationships():
    assert infer_activity_measure(
        "dbo.clients", _table_context(), [], api_key="sk-test",
    ) is None


def test_returns_none_when_openai_package_missing():
    with patch("core.semantic.implicit_measure_adjudicator._OPENAI_AVAILABLE", False):
        assert infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test",
        ) is None


def test_returns_none_on_api_exception():
    with patch("core.semantic.implicit_measure_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.side_effect = RuntimeError("boom")
        assert infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test",
        ) is None


def test_returns_none_on_hallucinated_identifier():
    mock_client = _mock_client_returning(json.dumps({
        "kind": "related_row_count", "related_table_fqn": "dbo.made_up_table",
        "related_fk_column": "X", "confidence": 0.9,
    }))
    with patch("core.semantic.implicit_measure_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        assert infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test",
        ) is None


def test_returns_none_below_confidence_threshold():
    mock_client = _mock_client_returning(json.dumps({
        "kind": "related_row_count", "related_table_fqn": "dbo.signups",
        "related_fk_column": "ClientID", "confidence": 0.3,
    }))
    with patch("core.semantic.implicit_measure_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        assert infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test",
        ) is None


# ---------------------------------------------------------------------------
# infer_activity_measure() — happy path
# ---------------------------------------------------------------------------

def test_happy_path_picks_transaction_table_over_lookup_table():
    response = {
        "kind": "related_row_count", "related_table_fqn": "dbo.signups",
        "related_fk_column": "ClientID", "confidence": 0.9,
        "evidence_reference": "Signups is event-shaped; client_categories is a static lookup.",
    }
    mock_client = _mock_client_returning(json.dumps(response))
    with patch("core.semantic.implicit_measure_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test",
        )

    assert result is not None
    assert result.kind == "related_row_count"
    assert result.related_table_fqn == "dbo.signups"
    assert result.related_fk_column == "ClientID"
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["response_format"] == {"type": "json_object"}


def test_session_cache_avoids_second_api_call():
    response = {
        "kind": "related_row_count", "related_table_fqn": "dbo.signups",
        "related_fk_column": "ClientID", "confidence": 0.9,
    }
    mock_client = _mock_client_returning(json.dumps(response))

    class _FakeSession:
        def __init__(self):
            self._cache = {}

        def get_or_compute_unless_none(self, key, fn):
            if key in self._cache:
                return self._cache[key]
            value = fn()
            if value is not None:
                self._cache[key] = value
            return value

    session = _FakeSession()
    with patch("core.semantic.implicit_measure_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        first = infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test", session=session,
        )
        second = infer_activity_measure(
            "dbo.clients", _table_context(), _relationships(), api_key="sk-test", session=session,
        )

    assert first is not None and second is not None
    assert mock_client.chat.completions.create.call_count == 1
