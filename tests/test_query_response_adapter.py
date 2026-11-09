"""
Focused unit tests — SQL rebuild Phase 4, Step 3 / Step 3A / Step 3B: the
pure OrchestrationResult -> execute-query response adapter
(api/v1/query_response_adapter.py).

Every OrchestrationResult here is hand-built via _result(), never produced
by actually running the pipeline — this module is tested in complete
isolation from retrieval/generation/validation/execution/answer-generation,
exactly like tests/test_execute_query_agent_integration.py hand-builds an
AgentState to test api/v1/agent_response_adapters.py in isolation. No
database, no provider, no network call anywhere in this file.

Step 3A note: "rows" is dict-shaped (list[dict[str, Any]]), one dict per
row keyed by a synthesized DISPLAY key per column — this reproduces
DataSourceManager.jsx's actual access pattern:
    qryCols = Object.keys(qryRows[0])
    ... row[h] ...
so every test below that touches rows/columns asserts on that same
dict/key shape, never the plain positional list[list] Step 3 originally
produced (see api/v1/query_response_adapter.py's module docstring
for why Step 3A supersedes that shape).

Step 3B note: display keys that would be JS array-index keys under
ECMA-262 (e.g. "0", "1", "10") are rewritten so real-engine
Object.keys() reordering can no longer happen at all — see the
"Step 3B" test block below, which PROVES this (via
_simulate_js_object_key_order, an actual implementation of the
ECMA-262 rule) rather than merely documenting the risk the way Step 3A's
now-removed test_integer_like_unique_column_name_would_reorder_in_js did.

Run:
    venv/Scripts/pytest tests/test_query_response_adapter.py -v
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from api.v1.query_response_adapter import (
    NOT_PREPARED_ERROR_CODE,
    NOT_PREPARED_MESSAGE,
    _is_js_array_index_key,
    build_execute_query_response,
)
from core.sqlservice.answer_generation_service import AnswerInsight, EvidenceColumnReference
from core.sqlservice.ai_first_result import (
    STATUS_ANSWER_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_EXECUTION_FAILED,
    STATUS_GENERATION_FAILED,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RETRIEVAL_FAILED,
    STATUS_VALIDATION_FAILED,
    AiFirstOrchestrationResult as OrchestrationResult,
    AiFirstStageDurations,
)

SOURCE_ID = 1


def _durations(**overrides) -> AiFirstStageDurations:
    base = dict(
        frontier_ms=None, selection_ms=None, relationship_ms=None, context_ms=None, ambiguity_guard_ms=None,
        generation_ms=None, validation_ms=None, execution_ms=None, answer_ms=None, total_ms=5,
    )
    if "retrieval_ms" in overrides:  # legacy spelling used throughout this file
        overrides["frontier_ms"] = overrides.pop("retrieval_ms")
    base.update(overrides)
    return AiFirstStageDurations(**base)


def _result(status: str = STATUS_OK, **overrides) -> OrchestrationResult:
    base = dict(
        status=status,
        source_id=SOURCE_ID,
        build_id=10,
        snapshot_id=20,
        schema_fingerprint="fp-1",
        answer="",
        insights=(),
        caveats=(),
        follow_up_questions=(),
        clarification_question=None,
        interpretation=None,
        sql=None,
        canonical_referenced_object_fqns=(),
        columns=(),
        rows=(),
        row_count=0,
        truncated=False,
        truncation_reason=None,
        retrieval_documents=(),
        frontier_size=0,
        selector_status=None,
        selected_object_fqns=(),
        connector_object_fqns=(),
        selector_provider_id="fake-selector",
        selector_model="fake-selector-model",
        embedding_provider_id="fake-embed",
        embedding_model="fake-embed-model",
        sql_provider_id="fake-sql",
        sql_model="fake-sql-model",
        answer_provider_id="fake-answer",
        answer_model="fake-answer-model",
        warnings=(),
        error=None,
        error_category=None,
        durations=_durations(),
    )
    base.update(overrides)
    return OrchestrationResult(**base)


# ---------------------------------------------------------------------------
# 1. Successful response
# ---------------------------------------------------------------------------

def test_successful_response_shape():
    result = _result(
        STATUS_OK,
        sql="SELECT amount FROM dbo.orders",
        interpretation="Sums the amount column from orders.",
        columns=("amount",),
        rows=((100,),),
        row_count=1,
        answer="Total revenue is 100.",
        durations=_durations(retrieval_ms=5, generation_ms=10, validation_ms=1, execution_ms=8, answer_ms=12, total_ms=36),
    )

    body = build_execute_query_response(result)

    assert body["status"] == "success"
    assert body["agent_status"] == "ok"
    assert body["stop_reason"] is None
    execution = body["data"]["execution"]
    assert execution["status"] == "success"
    assert execution["source_id"] == SOURCE_ID
    assert execution["columns"] == ["amount"]
    assert execution["canonical_columns"] == ["amount"]
    assert execution["rows"] == [{"amount": 100}]
    assert list(execution["rows"][0].keys()) == ["amount"]
    assert execution["row_count"] == 1
    assert execution["truncated"] is False
    assert execution["error"] is None
    assert execution["error_code"] is None
    assert execution["duration_ms"] == 8
    assert execution["row_limit_applied"] == 1000
    assert body["data"]["sql_generation"]["sql"] == "SELECT amount FROM dbo.orders"
    assert body["data"]["sql_generation"]["explanation"] == ["Sums the amount column from orders."]
    assert body["data"]["answer"] == "Total revenue is 100."


# ---------------------------------------------------------------------------
# 2. Zero rows
# ---------------------------------------------------------------------------

def test_zero_rows():
    result = _result(
        STATUS_OK, sql="SELECT 1", columns=("x", "y"), rows=(), row_count=0,
        durations=_durations(execution_ms=3, total_ms=3),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["status"] == "success"
    assert execution["rows"] == []
    assert execution["row_count"] == 0
    # Columns/canonical_columns must still be correct even with no rows to
    # derive Object.keys() from — the frontend needs them to render an
    # empty-but-correctly-headed table, and a future consumer may render
    # headers independently of whether any row exists.
    assert execution["columns"] == ["x", "y"]
    assert execution["canonical_columns"] == ["x", "y"]


# ---------------------------------------------------------------------------
# 3. Truncation
# ---------------------------------------------------------------------------

def test_truncation():
    result = _result(
        STATUS_OK, sql="SELECT x FROM t", columns=("x",), rows=((1,), (2,)), row_count=2,
        truncated=True, truncation_reason="row_limit",
        durations=_durations(execution_ms=4, total_ms=4),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["truncated"] is True
    assert execution["truncation_reason"] == "row_limit"
    assert execution["row_limit_applied"] == 1000


# ---------------------------------------------------------------------------
# 4. Clarification
# ---------------------------------------------------------------------------

def test_clarification_required():
    result = _result(
        STATUS_CLARIFICATION_REQUIRED,
        interpretation="Ambiguous term detected.",
        clarification_question="Did you mean the Clients table or the Customers table?",
        durations=_durations(retrieval_ms=5, generation_ms=6, total_ms=11),
    )
    body = build_execute_query_response(result)

    assert body["agent_status"] == "clarification_required"
    assert body["stop_reason"] == "Did you mean the Clients table or the Customers table?"
    clarification = body["data"]["clarification"]
    assert clarification["executed"] is False
    assert clarification["reason"] == "clarification_required"
    assert clarification["message"] == "Did you mean the Clients table or the Customers table?"
    assert body["data"]["execution"]["status"] == "not_executed"
    assert body["data"]["execution"]["error"] is None
    assert body["data"]["execution"]["error_code"] is None


def test_non_clarification_status_has_no_clarification_key():
    body = build_execute_query_response(_result(STATUS_OK))
    assert "clarification" not in body["data"]


# ---------------------------------------------------------------------------
# 5. Not prepared
# ---------------------------------------------------------------------------

def test_not_prepared():
    result = _result(STATUS_NOT_PREPARED, error=f"No READY retrieval build exists for source_id={SOURCE_ID}.")
    body = build_execute_query_response(result)

    assert body["agent_status"] == "not_prepared"
    assert body["stop_reason"] == NOT_PREPARED_MESSAGE
    execution = body["data"]["execution"]
    assert execution["status"] == "not_prepared"
    assert execution["error"] == NOT_PREPARED_MESSAGE
    assert execution["error_code"] == NOT_PREPARED_ERROR_CODE
    # The internal "source_id=1" message is never leaked to the user-facing fields.
    assert "source_id" not in (execution["error"] or "")
    assert str(SOURCE_ID) not in (execution["error"] or "")


# ---------------------------------------------------------------------------
# 6. Every failure status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected_agent_status,expected_execution_status",
    [
        (STATUS_RETRIEVAL_FAILED, "retrieval_failed", "not_executed"),
        (STATUS_GENERATION_FAILED, "generation_failed", "not_executed"),
        (STATUS_VALIDATION_FAILED, "validation_failed", "not_executed"),
        (STATUS_ANSWER_FAILED, "answer_failed", "success"),
    ],
)
def test_pre_execution_and_answer_failure_statuses(status, expected_agent_status, expected_execution_status):
    durations_kwargs = dict(total_ms=9)
    if status == STATUS_ANSWER_FAILED:
        durations_kwargs.update(execution_ms=7, answer_ms=2)
    result = _result(status, error="a safe, code-authored failure description", durations=_durations(**durations_kwargs))

    body = build_execute_query_response(result)

    assert body["agent_status"] == expected_agent_status
    assert body["stop_reason"] == "a safe, code-authored failure description"
    assert body["data"]["execution"]["status"] == expected_execution_status
    if expected_execution_status == "success":
        assert body["data"]["execution"]["error"] is None
    else:
        assert body["data"]["execution"]["error"] == "a safe, code-authored failure description"


@pytest.mark.parametrize(
    "category,expected_status,expected_code",
    [
        ("timeout", "timeout", "timeout"),
        ("connectivity", "failed", "connection_failed"),
        ("permission", "failed", "permission_denied"),
        ("schema_changed", "failed", "schema_changed"),
        ("execution_error", "failed", "query_error"),
        (None, "failed", "query_rejected"),
    ],
)
def test_execution_failed_maps_error_category(category, expected_status, expected_code):
    result = _result(
        STATUS_EXECUTION_FAILED, error="sanitized execution failure message", error_category=category,
        durations=_durations(execution_ms=15, total_ms=15),
    )
    body = build_execute_query_response(result)

    assert body["agent_status"] == "execution_failed"
    execution = body["data"]["execution"]
    assert execution["status"] == expected_status
    assert execution["error_code"] == expected_code
    assert execution["error"] == "sanitized execution failure message"


# ---------------------------------------------------------------------------
# 7. Budget exceeded — before vs. after execution
# ---------------------------------------------------------------------------

def test_budget_exceeded_before_execution():
    result = _result(
        STATUS_BUDGET_EXCEEDED, error="Question-time budget exceeded before SQL generation.",
        durations=_durations(retrieval_ms=5, total_ms=5),
    )
    body = build_execute_query_response(result)
    assert body["agent_status"] == "budget_exceeded"
    assert body["data"]["execution"]["status"] == "not_executed"
    assert body["data"]["execution"]["error_code"] == "budget_exceeded"


def test_budget_exceeded_after_execution():
    result = _result(
        STATUS_BUDGET_EXCEEDED, error="Question-time budget exceeded before answer generation.",
        columns=("x",), rows=((1,),), row_count=1,
        durations=_durations(execution_ms=9, total_ms=9),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["status"] == "success"
    assert execution["error"] is None
    assert execution["error_code"] is None


# ---------------------------------------------------------------------------
# 8. Columns/rows preserved positionally; display keys built from them
#    (Step 3A: unique / duplicate / three-duplicate / blank / suffix-
#    collision display-key generation, plus positional value alignment).
# ---------------------------------------------------------------------------

def test_unique_columns_keep_canonical_names_and_positional_values():
    result = _result(
        STATUS_OK, columns=("b", "a", "c"), rows=((2, 1, 3), (20, 10, 30)), row_count=2,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["columns"] == ["b", "a", "c"]
    assert execution["canonical_columns"] == ["b", "a", "c"]
    assert execution["rows"] == [{"b": 2, "a": 1, "c": 3}, {"b": 20, "a": 10, "c": 30}]
    # Object.keys()-equivalent order must match the display "columns" list,
    # not be re-sorted alphabetically or otherwise.
    assert list(execution["rows"][0].keys()) == ["b", "a", "c"]


def test_duplicate_columns_get_stable_suffix_and_no_value_dropped():
    result = _result(
        STATUS_OK, columns=("id", "name", "id"), rows=((1, "orders", 999),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["columns"] == ["id", "name", "id (2)"]
    assert execution["canonical_columns"] == ["id", "name", "id"]
    assert execution["rows"] == [{"id": 1, "name": "orders", "id (2)": 999}]
    # Both "id" values are independently present — nothing was dropped.
    assert execution["rows"][0]["id"] == 1
    assert execution["rows"][0]["id (2)"] == 999
    assert len(execution["rows"][0]) == 3


def test_three_duplicate_columns_get_sequential_suffixes():
    result = _result(
        STATUS_OK, columns=("Name", "Name", "Name"), rows=(("a", "b", "c"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["columns"] == ["Name", "Name (2)", "Name (3)"]
    assert len(set(execution["columns"])) == 3
    assert execution["rows"] == [{"Name": "a", "Name (2)": "b", "Name (3)": "c"}]


def test_blank_column_names_get_deterministic_positional_labels():
    result = _result(
        STATUS_OK, columns=("", "Name", "   "), rows=((1, "x", 3),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    # Position 0 (1-based "Column 1") and position 2 ("Column 3") — the
    # whitespace-only third column is treated as blank too.
    assert execution["columns"] == ["Column 1", "Name", "Column 3"]
    assert execution["rows"] == [{"Column 1": 1, "Name": "x", "Column 3": 3}]


def test_existing_suffix_like_name_collision_stays_unique():
    # The source query already has a real column literally named "Name (2)"
    # sitting where our own duplicate-suffixing logic would otherwise place
    # the second "Name" occurrence — the collision guard must keep bumping
    # until every label is unique, never silently reuse "Name (2)" twice.
    result = _result(
        STATUS_OK, columns=("Name", "Name (2)", "Name"), rows=((1, 2, 3),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["columns"] == ["Name", "Name (2)", "Name (2) (2)"]
    assert len(set(execution["columns"])) == 3
    assert execution["rows"] == [{"Name": 1, "Name (2)": 2, "Name (2) (2)": 3}]


def test_values_remain_aligned_with_original_positions_when_mixed():
    # A mix of unique/duplicate/blank columns — every value must land under
    # the display key generated for ITS OWN position, never a neighbor's.
    result = _result(
        STATUS_OK, columns=("id", "", "id", "name"),
        rows=((1, "blank-col-value", 2, "orders"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]
    assert execution["columns"] == ["id", "Column 2", "id (2)", "name"]
    row = execution["rows"][0]
    assert row["id"] == 1
    assert row["Column 2"] == "blank-col-value"
    assert row["id (2)"] == 2
    assert row["name"] == "orders"


# ---------------------------------------------------------------------------
# Step 3B: JS array-index-like column names are eliminated, not merely
# documented. _simulate_js_object_key_order below implements the actual
# ECMA-262 OrdinaryOwnPropertyKeys rule (every canonical array-index string
# key — "0"/"1"/"10"/.../4294967294 — enumerates first, ascending
# numerically, ahead of every other string key, regardless of insertion
# order; "01" and anything above 4294967294 is NOT a canonical array index
# and is therefore a normal string key, enumerated in insertion order) and
# is applied against this adapter's ACTUAL output to PROVE reordering can
# no longer happen — not just to assert it in prose.
# ---------------------------------------------------------------------------

def _simulate_js_object_key_order(keys):
    def _is_canonical_array_index(k):
        if not (k == "0" or (k[:1] in "123456789" and k.isdigit())):
            return False
        return int(k) <= 2**32 - 2

    integer_keys = sorted((k for k in keys if _is_canonical_array_index(k)), key=int)
    string_keys = [k for k in keys if not _is_canonical_array_index(k)]
    return integer_keys + string_keys


@pytest.mark.parametrize(
    "key,expected",
    [
        ("0", True), ("1", True), ("10", True), ("42", True),
        ("01", False), ("00", False), ("-1", False), ("1.0", False), ("", False),
        ("name", False), (" 1", False), ("1 ", False),
        ("4294967294", True), ("4294967295", False), ("99999999999999999999", False),
    ],
)
def test_is_js_array_index_key_matches_ecma262(key, expected):
    # The adapter's own predicate must agree with an independently written
    # reference implementation of the ECMA-262 rule on every case this
    # module's tests rely on.
    assert _is_js_array_index_key(key) is expected
    assert _is_js_array_index_key_reference(key) is expected


def test_no_display_key_is_ever_a_js_array_index_key():
    # A broad sweep: blanks, duplicates, and genuinely numeric-named
    # columns all mixed together — none of the emitted display keys may be
    # a JS array-index key.
    result = _result(
        STATUS_OK,
        columns=("0", "1", "10", "name", "", "name", "01", "4294967294", "4294967295"),
        rows=((0, 1, 2, "orders", "blank", "orders2", "leading-zero", "max-index", "past-max"),),
        row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    columns = body["data"]["execution"]["columns"]

    for key in columns:
        assert not _is_js_array_index_key_reference(key), f"{key!r} is still a JS array-index key"
    assert len(set(columns)) == len(columns)  # still globally unique
    # Our own insertion order is preserved verbatim (SELECT order).
    assert list(body["data"]["execution"]["rows"][0].keys()) == columns


def _is_js_array_index_key_reference(k: str) -> bool:
    """Independent re-implementation of the ECMA-262 rule, kept separate
    from api/v1/sql_pipeline_response_adapter._is_js_array_index_key so
    this test verifies actual behavior against the spec, not against the
    adapter's own (possibly buggy) predicate."""
    if not (k == "0" or (k[:1] in "123456789" and k.isdigit())):
        return False
    return int(k) <= 2**32 - 2


@pytest.mark.parametrize("raw_name", ["0", "1", "10"])
def test_simple_array_index_names_are_rewritten_and_never_reorder(raw_name):
    result = _result(
        STATUS_OK, columns=("name", raw_name, "amount"), rows=(("orders", 1, 100),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    expected_key = f"Column {raw_name}"
    assert execution["columns"] == ["name", expected_key, "amount"]
    assert execution["canonical_columns"] == ["name", raw_name, "amount"]
    assert list(execution["rows"][0].keys()) == ["name", expected_key, "amount"]

    # Proof, not assertion-by-prose: simulating real JS enumeration on our
    # OWN output now yields the SAME order we emitted — no reordering.
    js_order = _simulate_js_object_key_order(execution["columns"])
    assert js_order == execution["columns"]

    # Values are still correctly reachable under the rewritten key.
    assert execution["rows"][0][expected_key] == 1


def test_mixed_numeric_and_normal_names_preserve_select_order_under_js():
    result = _result(
        STATUS_OK, columns=("2", "region", "0", "total", "7"),
        rows=(("x", "West", "y", 100, "z"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    assert execution["columns"] == ["Column 2", "region", "Column 0", "total", "Column 7"]
    js_order = _simulate_js_object_key_order(execution["columns"])
    assert js_order == execution["columns"]
    assert js_order != sorted(execution["columns"])  # sanity: order isn't trivially already-sorted


def test_numeric_key_conversion_colliding_with_real_column_1_alias():
    # A genuine column already named "Column 1" sits where converting the
    # raw "1" column would otherwise also want to land — the existing
    # global collision guard must keep them both distinct.
    result = _result(
        STATUS_OK, columns=("Column 1", "1"), rows=(("alias-value", 42),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    assert execution["columns"] == ["Column 1", "Column 1 (2)"]
    assert len(set(execution["columns"])) == 2
    assert execution["rows"][0]["Column 1"] == "alias-value"
    assert execution["rows"][0]["Column 1 (2)"] == 42
    js_order = _simulate_js_object_key_order(execution["columns"])
    assert js_order == execution["columns"]


def test_numeric_key_conversion_collision_reversed_order():
    # Same collision, opposite arrival order — whichever column reaches
    # "Column 1" first keeps it; the later one is bumped, regardless of
    # which one was the raw numeric name and which was the real alias.
    result = _result(
        STATUS_OK, columns=("1", "Column 1"), rows=((42, "alias-value"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    assert execution["columns"] == ["Column 1", "Column 1 (2)"]
    assert execution["rows"][0]["Column 1"] == 42
    assert execution["rows"][0]["Column 1 (2)"] == "alias-value"


def test_leading_zero_numeric_looking_name_is_not_an_array_index_and_is_untouched():
    # "01" is NOT a canonical array index per ECMA-262 (ToUint32("01")
    # stringifies back to "1", not "01") — real JS engines treat it as an
    # ordinary string key, enumerated in insertion order. This adapter
    # must therefore leave it exactly as-is: no rewrite is needed, and
    # rewriting it anyway would be an unwarranted behavior change.
    result = _result(
        STATUS_OK, columns=("01", "name"), rows=(("value", "orders"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    assert execution["columns"] == ["01", "name"]
    assert execution["rows"][0]["01"] == "value"
    js_order = _simulate_js_object_key_order(execution["columns"])
    assert js_order == execution["columns"]  # "01" is a normal string key: no reordering to begin with


@pytest.mark.parametrize(
    "raw_name,should_be_rewritten",
    [
        ("4294967294", True),   # 2**32 - 2: the LARGEST valid JS array index
        ("4294967295", False),  # 2**32 - 1: reserved as max array length, NOT a valid index
        ("99999999999999999999", False),  # numeric-looking but astronomically past the bound
    ],
)
def test_array_index_boundary_values(raw_name, should_be_rewritten):
    # First, confirm the boundary itself is drawn where ECMA-262 actually
    # draws it (independent reference predicate, not the adapter's own).
    assert _is_js_array_index_key_reference(raw_name) is should_be_rewritten

    result = _result(
        STATUS_OK, columns=(raw_name, "name"), rows=((1, "orders"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    expected_key = f"Column {raw_name}" if should_be_rewritten else raw_name
    assert execution["columns"] == [expected_key, "name"]
    assert execution["canonical_columns"] == [raw_name, "name"]
    assert execution["rows"][0][expected_key] == 1

    # Whichever way the boundary fell, our own output must never reorder
    # under a real JS engine: a rewritten key is no longer array-index-
    # shaped at all, and an un-rewritten key was correctly identified as
    # never having been an array index to begin with.
    js_order = _simulate_js_object_key_order(execution["columns"])
    assert js_order == execution["columns"]


def test_synthesized_labels_are_never_integer_like():
    # Our OWN synthesized disambiguation labels ("Column N", "Name (N)")
    # must never themselves be canonical-integer-index strings — otherwise
    # this adapter would be silently reintroducing the exact JS reordering
    # risk it exists to eliminate.
    result = _result(
        STATUS_OK, columns=("", "", "Name", "Name", "0", "1"), rows=((1, 2, 3, 4, 5, 6),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    for key in body["data"]["execution"]["columns"]:
        assert not _is_js_array_index_key_reference(key)


# ---------------------------------------------------------------------------
# 9. Decimal / JSON-safety
# ---------------------------------------------------------------------------

def test_decimal_converted_to_exact_string_not_float():
    result = _result(
        STATUS_OK, columns=("amount",), rows=((Decimal("19.99"),), (Decimal("0.10"),)), row_count=2,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    rows = body["data"]["execution"]["rows"]
    assert rows == [{"amount": "19.99"}, {"amount": "0.10"}]
    # Never a float — 0.10 cannot be represented exactly in binary float.
    assert isinstance(rows[1]["amount"], str)


def test_json_serialization_succeeds_for_every_status():
    for status in (
        STATUS_OK, STATUS_NOT_PREPARED, STATUS_RETRIEVAL_FAILED, STATUS_GENERATION_FAILED,
        STATUS_CLARIFICATION_REQUIRED, STATUS_VALIDATION_FAILED, STATUS_EXECUTION_FAILED,
        STATUS_ANSWER_FAILED, STATUS_BUDGET_EXCEEDED,
    ):
        result = _result(
            status, error="x" if status not in (STATUS_OK, STATUS_CLARIFICATION_REQUIRED) else None,
            clarification_question="q?" if status == STATUS_CLARIFICATION_REQUIRED else None,
            columns=("a",), rows=((Decimal("1.5"),),), row_count=1,
            durations=_durations(execution_ms=1, total_ms=1),
        )
        body = build_execute_query_response(result)
        json.dumps(body)  # must not raise


def test_json_serialization_succeeds_with_duplicate_and_blank_columns():
    result = _result(
        STATUS_OK, columns=("", "id", "id"), rows=((1, 2, 3), (4, 5, 6)), row_count=2,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    blob = json.dumps(body)
    reloaded = json.loads(blob)
    assert reloaded["data"]["execution"]["rows"] == [
        {"Column 1": 1, "id": 2, "id (2)": 3},
        {"Column 1": 4, "id": 5, "id (2)": 6},
    ]


# ---------------------------------------------------------------------------
# 10. Grounded insights mapped safely
# ---------------------------------------------------------------------------

def test_insights_mapped_safely():
    insight = AnswerInsight(
        title="Top region",
        finding="The West region had the highest total.",
        evidence_row_numbers=(1, 2),
        evidence_columns=(EvidenceColumnReference(position=1, name="region"),),
    )
    result = _result(STATUS_OK, insights=(insight,))
    body = build_execute_query_response(result)

    insights = body["data"]["insights"]
    assert insights == [
        {
            "title": "Top region",
            "finding": "The West region had the highest total.",
            "evidence_row_numbers": [1, 2],
            "evidence_columns": [{"position": 1, "name": "region"}],
            "provenance": "ai_generated",
        }
    ]
    json.dumps(body)


def test_empty_insights_is_empty_list():
    body = build_execute_query_response(_result(STATUS_OK))
    assert body["data"]["insights"] == []


# ---------------------------------------------------------------------------
# 11. Warnings propagation
# ---------------------------------------------------------------------------

def test_warnings_propagate_to_top_level_and_execution():
    result = _result(STATUS_OK, warnings=("a retrieval warning", "an execution warning"))
    body = build_execute_query_response(result)
    assert body["warnings"] == ["a retrieval warning", "an execution warning"]
    assert body["data"]["execution"]["warnings"] == ["a retrieval warning", "an execution warning"]


# ---------------------------------------------------------------------------
# 12. No credentials/internal context leakage
# ---------------------------------------------------------------------------

def test_no_internal_context_leaked():
    result = _result(
        STATUS_OK,
        retrieval_documents=("<this should never appear in output>",),
        embedding_provider_id="openai", embedding_model="text-embedding-3-small",
        sql_provider_id="openai", sql_model="gpt-4o",
        answer_provider_id="openai", answer_model="gpt-4o",
    )
    body = build_execute_query_response(result)
    blob = json.dumps(body)
    assert "this should never appear in output" not in blob
    for forbidden in ("password", "secret", "api_key", "connection_string", "embedding_provider", "sql_provider", "answer_provider"):
        assert forbidden not in blob.lower()


def test_execution_error_never_echoes_raw_exception_text():
    # error/error_category are always pre-sanitized upstream (see
    # OrchestrationResult's own docstring) — this adapter must pass them
    # through unchanged, never substitute or append raw exception text.
    result = _result(
        STATUS_EXECUTION_FAILED, error="The query exceeded the configured execution timeout.",
        error_category="timeout", durations=_durations(execution_ms=30000, total_ms=30000),
    )
    body = build_execute_query_response(result)
    assert body["data"]["execution"]["error"] == "The query exceeded the configured execution timeout."


# ---------------------------------------------------------------------------
# 13. Input result remains unchanged (purity)
# ---------------------------------------------------------------------------

def test_input_result_is_not_mutated():
    result = _result(
        STATUS_OK, columns=("a", "b"), rows=((1, 2), (3, 4)), row_count=2,
        warnings=("w1",), insights=(AnswerInsight("t", "f", (1,), (EvidenceColumnReference(1, "a"),)),),
    )
    before = copy.deepcopy(
        {
            "status": result.status, "columns": result.columns, "rows": result.rows,
            "warnings": result.warnings, "row_count": result.row_count,
        }
    )

    build_execute_query_response(result)

    assert result.status == before["status"]
    assert result.columns == before["columns"]
    assert result.rows == before["rows"]
    assert result.warnings == before["warnings"]
    assert result.row_count == before["row_count"]
    # Still the exact same frozen dataclass instance/tuples (identity, not just equality).
    assert isinstance(result.columns, tuple)
    assert isinstance(result.rows, tuple)


# ---------------------------------------------------------------------------
# 14. Compatibility with fields DataSourceManager.jsx actually reads:
#     qryExec.status / .rows / .row_count / .duration_ms / .truncated /
#     .error_code, and qryResult.sql_generation.explanation — plus, per
#     Step 3A, the frontend's actual row-rendering access pattern:
#         qryCols = qryRows.length > 0 ? Object.keys(qryRows[0]) : []
#         ... row[h] ...
# ---------------------------------------------------------------------------

def test_datasourcemanager_consumed_fields_present_and_typed():
    result = _result(
        STATUS_OK, sql="SELECT 1", interpretation="One literal row.",
        columns=("n",), rows=((1,),), row_count=1, truncated=False,
        durations=_durations(execution_ms=2, total_ms=2),
    )
    body = build_execute_query_response(result)
    execution = body["data"]["execution"]

    # qryExec.status
    assert execution["status"] == "success"
    # qryExec.row_count / qryExec.rows
    assert execution["row_count"] == 1
    assert execution["rows"] == [{"n": 1}]
    # qryExec.duration_ms
    assert execution["duration_ms"] == 2
    # qryExec.truncated
    assert execution["truncated"] is False
    # qryExec.error_code (optional-chained by the frontend; None is fine)
    assert execution["error_code"] is None
    # qryResult.sql_generation.explanation
    assert body["data"]["sql_generation"]["explanation"] == ["One literal row."]


def test_datasourcemanager_object_keys_access_pattern_shows_column_labels_not_indices():
    """
    Reproduces DataSourceManager.jsx's exact rendering logic (lines ~3543-
    3547/3746-3751 of frontend/src/components/DataSourceManager.jsx):
        qryRows = qryExec.rows
        qryCols = qryRows.length > 0 ? Object.keys(qryRows[0]) : []
        ... row[h] ...
    against multi-column, duplicate-column, and blank-column results, and
    asserts the derived headers are column LABELS, never numeric string
    indices ("0", "1", ...) the way a positional array would render.
    """
    result = _result(
        STATUS_OK, columns=("region", "total", "", "region"),
        rows=(("West", 100, "n/a", "East"),), row_count=1,
        durations=_durations(execution_ms=1, total_ms=1),
    )
    body = build_execute_query_response(result)
    qry_exec = body["data"]["execution"]
    qry_rows = qry_exec["rows"]

    # qryCols = Object.keys(qryRows[0])
    qry_cols = list(qry_rows[0].keys()) if qry_rows else []

    assert qry_cols == ["region", "total", "Column 3", "region (2)"]
    for h in qry_cols:
        assert not h.isdigit()  # never "0"/"1"/"2"/"3"

    # row[h] for every h in qryCols must resolve to the right value.
    row = qry_rows[0]
    assert row["region"] == "West"
    assert row["total"] == 100
    assert row["Column 3"] == "n/a"
    assert row["region (2)"] == "East"

    # This is also exactly what "columns" reports, for a consumer that
    # wants headers without inspecting a row.
    assert qry_exec["columns"] == qry_cols


def test_datasourcemanager_error_code_present_on_failure_for_ui_message():
    result = _result(
        STATUS_EXECUTION_FAILED, error="The database rejected the query due to insufficient permissions.",
        error_category="permission", durations=_durations(execution_ms=3, total_ms=3),
    )
    body = build_execute_query_response(result)
    assert body["data"]["execution"]["error_code"] == "permission_denied"


# ---------------------------------------------------------------------------
# Step 9A: the JSON response body never contains a query embedding
# ---------------------------------------------------------------------------

def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, key) for v in value)
    return False


def test_response_body_never_contains_a_query_vector_key():
    result = _result(STATUS_OK, sql="SELECT 1", columns=("n",), rows=((1,),), row_count=1)
    body = build_execute_query_response(result)
    # json.dumps proves the body is actually JSON-serializable -- the same
    # step a real route takes before it ever reaches a client.
    serialized = json.loads(json.dumps(body))
    assert not _contains_key(serialized, "query_vector")


# ---------------------------------------------------------------------------
# Phase 8A -- fast-path statuses and the pre-result error envelope
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,agent_status,error_code", [
    ("NON_DATA", "non_data", "not_a_data_question"),
    ("UNSUPPORTED", "unsupported", "request_not_supported"),
    ("SELECTION_FAILED", "selection_failed", "query_planning_failed"),
    ("RELATIONSHIP_COMPLETION_FAILED", "relationship_completion_failed", "relationship_completion_failed"),
    ("CONTEXT_BUILD_FAILED", "context_build_failed", "schema_context_failed"),
])
def test_fast_path_statuses_map_without_executing(status, agent_status, error_code):
    body = build_execute_query_response(_result(status, answer="Not a data question.", error="x"))

    assert body["agent_status"] == agent_status
    execution = body["data"]["execution"]
    assert execution["status"] == "not_executed"
    assert execution["error_code"] == error_code
    assert execution["rows"] == []


def test_destructive_request_stop_reason_is_the_classifier_text_and_runs_nothing():
    body = build_execute_query_response(_result("UNSUPPORTED", answer="I can't modify data.", error=None))

    assert body["stop_reason"] == "I can't modify data."
    assert body["data"]["sql_generation"]["sql"] is None
    assert body["data"]["execution"]["status"] == "not_executed"


def test_error_envelope_is_fixed_shape_with_no_rows_or_sql():
    from api.v1.query_response_adapter import build_execute_query_error_response

    body = build_execute_query_error_response(
        SOURCE_ID, agent_status="not_enabled", message="Not enabled.", error_code="source_not_enabled",
    )

    assert body["agent_status"] == "not_enabled"
    assert body["stop_reason"] == "Not enabled."
    assert body["data"]["execution"]["status"] == "not_executed"
    assert body["data"]["execution"]["error_code"] == "source_not_enabled"
    assert body["data"]["execution"]["rows"] == [] and body["data"]["sql_generation"]["sql"] is None
