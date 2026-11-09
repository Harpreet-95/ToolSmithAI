"""
Query response adapter -- Phase 8A.

Shared, pure response helpers for the AI-first fast path used by
POST /v1/composer/ask, plus a standalone query-response envelope builder
that has no active HTTP route (retained, with its tests, as a pure adapter):

  - _display_row_keys / _json_safe_cell / _insight_to_dict: used by the
    composer adapter (api/v1/ai_first_composer_adapter.py).
  - build_execute_query_response(result): AiFirstOrchestrationResult ->
    {"status", "agent_status", "stop_reason", "data": {...}, "warnings",
    "trace"}.

Row/column shape (Step 3A/3B contract with the frontend renderer):
"rows" is list[dict] keyed by a synthesized, globally-unique DISPLAY key per
column (duplicate names get " (N)" suffixes; blank names "Column N"; names
that are JS array-index keys such as "0"/"1" are rewritten to "Column 0" so
Object.keys() enumerates in column order), and "columns" holds those same
display keys, in order. The unmodified SQL names are additionally exposed
as "canonical_columns". Decimal cells are emitted as exact strings, never
floats. Nothing here recomputes, re-validates, or re-executes anything;
every field is read straight off the result.

execution.executed_at / execution_id are None (this module is pure -- no
uuid4()/now()). data.retrieval_documents, provider ids, and internal
prompts are never included.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional

from core.config import SQL_EXECUTION_MAX_ROWS
from core.sqlservice.ai_first_result import (
    STATUS_ANSWER_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_CONTEXT_BUILD_FAILED,
    STATUS_EXECUTION_FAILED,
    STATUS_GENERATION_FAILED,
    STATUS_NON_DATA,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RELATIONSHIP_COMPLETION_FAILED,
    STATUS_RETRIEVAL_FAILED,
    STATUS_SELECTION_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_VALIDATION_FAILED,
    AiFirstOrchestrationResult,
)

__all__ = ["build_execute_query_response", "build_execute_query_error_response"]

# Requirement 6 — NOT_PREPARED gets a stable, machine-readable code and a
# safe, actionable message (never AiFirstOrchestrationResult.error's own internal
# "No READY retrieval build exists for source_id=..." text, which names an
# internal identifier and is not phrased for an end user).
NOT_PREPARED_ERROR_CODE = "source_not_prepared"
NOT_PREPARED_MESSAGE = (
    "This data source has not been prepared for querying yet. "
    "Run schema preparation (or reconnect the source), then try again."
)

# Requirement 4 — deterministic status mapping. Every AiFirstOrchestrationResult
# status maps to exactly one agent_status string (top-level, additive/
# compatible key name reused from the AgentState-based adapter so an
# existing consumer reading response["agent_status"] never has to branch on
# which pipeline produced it).
_AGENT_STATUS_BY_ORCHESTRATION_STATUS: dict[str, str] = {
    STATUS_OK: "ok",
    STATUS_NOT_PREPARED: "not_prepared",
    STATUS_RETRIEVAL_FAILED: "retrieval_failed",
    STATUS_SELECTION_FAILED: "selection_failed",
    STATUS_RELATIONSHIP_COMPLETION_FAILED: "relationship_completion_failed",
    STATUS_CONTEXT_BUILD_FAILED: "context_build_failed",
    STATUS_GENERATION_FAILED: "generation_failed",
    STATUS_NON_DATA: "non_data",
    STATUS_UNSUPPORTED: "unsupported",
    STATUS_CLARIFICATION_REQUIRED: "clarification_required",
    STATUS_VALIDATION_FAILED: "validation_failed",
    STATUS_EXECUTION_FAILED: "execution_failed",
    STATUS_ANSWER_FAILED: "answer_failed",
    STATUS_BUDGET_EXCEEDED: "budget_exceeded",
}

# error_code for every status that never reaches execution (EXECUTION_FAILED
# gets its own, error_category-derived code below — see
# _EXECUTION_ERROR_CODE_BY_CATEGORY). STATUS_OK/STATUS_CLARIFICATION_REQUIRED
# are absent on purpose: neither is an error, so .get(...) correctly yields
# None for both.
_ERROR_CODE_BY_ORCHESTRATION_STATUS: dict[str, str] = {
    STATUS_NOT_PREPARED: NOT_PREPARED_ERROR_CODE,
    STATUS_RETRIEVAL_FAILED: "schema_retrieval_failed",
    STATUS_SELECTION_FAILED: "query_planning_failed",
    STATUS_RELATIONSHIP_COMPLETION_FAILED: "relationship_completion_failed",
    STATUS_CONTEXT_BUILD_FAILED: "schema_context_failed",
    STATUS_GENERATION_FAILED: "sql_generation_failed",
    STATUS_NON_DATA: "not_a_data_question",
    STATUS_UNSUPPORTED: "request_not_supported",
    STATUS_VALIDATION_FAILED: "sql_validation_failed",
    STATUS_ANSWER_FAILED: "answer_generation_failed",
    STATUS_BUDGET_EXCEEDED: "budget_exceeded",
}

# core.sqlservice.sql_execution.CATEGORY_* -> a stable execution.error_code /
# execution.status pair. This pipeline has no analog to the legacy
# "governance_block"/"rate_limited" execution statuses (those concerns are
# absorbed into VALIDATION_FAILED/EXECUTION_FAILED upstream of this
# adapter), so those two legacy values are never produced here.
_EXECUTION_ERROR_CODE_BY_CATEGORY: dict[Optional[str], str] = {
    "timeout": "timeout",
    "connectivity": "connection_failed",
    "permission": "permission_denied",
    "schema_changed": "schema_changed",
    "execution_error": "query_error",
}
_EXECUTION_STATUS_BY_CATEGORY: dict[Optional[str], str] = {
    "timeout": "timeout",
    "connectivity": "failed",
    "permission": "failed",
    "schema_changed": "failed",
    "execution_error": "failed",
}
_DEFAULT_EXECUTION_ERROR_CODE = "query_rejected"


def _json_safe_cell(value: object) -> Any:
    """
    One SqlExecutionResult cell -> a JSON-safe value, deliberately (never
    repr()/str() of an unrecognized object).

    core.sqlservice.sql_execution._normalize_value already reduces every
    live driver value to one of None/bool/int/float/Decimal/str before it
    ever reaches AiFirstOrchestrationResult.rows — this function's only real job
    is the Decimal -> str conversion `json.dumps` cannot do on its own
    (converting to float instead would silently round an exact decimal
    amount). The final branch is an unreachable-in-practice safety net
    that mirrors sql_execution's own "<unsupported:...>" placeholder
    convention rather than ever leaking an arbitrary object's repr().
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return f"<unsupported:{type(value).__name__}>"


def _insight_to_dict(insight: Any) -> dict:
    """One AnswerInsight -> a plain, JSON-safe dict. evidence_columns is a
    tuple of EvidenceColumnReference(position, name) — both already
    grounded/validated by answer_generation_service itself; this only
    reshapes them, never re-validates."""
    return {
        "title": insight.title,
        "finding": insight.finding,
        "evidence_row_numbers": list(insight.evidence_row_numbers),
        "evidence_columns": [
            {"position": c.position, "name": c.name} for c in insight.evidence_columns
        ],
        "provenance": getattr(insight, "provenance", "ai_generated"),
    }


# Step 3B — ECMA-262 "array index" detection (spec: CanonicalNumericIndexString
# / the ArrayIndex definition an ordinary object's OrdinaryOwnPropertyKeys
# enumerates first). A string key is a JS array index iff:
#   - it consists only of ASCII decimal digits 0-9 (never a sign, decimal
#     point, or any non-ASCII "digit" character some Unicode scripts have —
#     `str.isdigit()` accepts those too, which is why this uses an explicit
#     ASCII-only regex instead), AND
#   - it has no leading zero UNLESS the whole string is exactly "0" (so "0"
#     qualifies but "00"/"01" do not — ToUint32("01") stringifies back to
#     "1", not "01", so "01" is a normal string key in real engines), AND
#   - its numeric value is <= 2**32 - 2 (4294967294) — 2**32 - 1
#     (4294967295) is reserved as the maximum array LENGTH, not a valid
#     index, and is therefore also a normal string key.
_ARRAY_INDEX_SHAPE_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_MAX_JS_ARRAY_INDEX = 2**32 - 2  # 4294967294


def _is_js_array_index_key(key: str) -> bool:
    if not _ARRAY_INDEX_SHAPE_RE.match(key):
        return False
    return int(key) <= _MAX_JS_ARRAY_INDEX


def _display_row_keys(columns: tuple) -> list:
    """
    Positional column names -> the exact object keys
    DataSourceManager.jsx's `Object.keys(rows[0])` will read as table
    headers and use again, per row, as `row[h]` — Step 3A's contract with
    the current, UNMODIFIED frontend renderer.

    One deterministic, left-to-right pass over `columns` (never re-sorted,
    never grouped):
      1. A blank name (empty/whitespace-only string, or a non-string/None
         value from an unusual driver) becomes "Column {i+1}", where i is
         its own 0-based position — every blank's label is already
         distinct purely from its position, before rule 3 below even
         runs.
      2. A non-blank name's Nth occurrence (N counted across this whole
         column list, case-sensitively, in position order) becomes its
         canonical name unchanged when N == 1, or "{name} (N)" when N > 1.
      3. (Step 3B) If the label rules 1-2 produced is itself a JS
         array-index key (see _is_js_array_index_key — "0"/"1"/"10"/...;
         NOT "01", NOT a value above 2**32-2), it is rewritten to
         "Column {label}" (e.g. "0" -> "Column 0", "1" -> "Column 1").
         Only rule 2's N==1 (canonical-name) branch can ever produce a
         pure-digit label in the first place — the blank branch always
         contains "Column ", and the N>1 duplicate-suffix branch always
         contains " (" — so this step only ever touches a genuinely
         numeric-named column, never a synthesized one.
      4. Whatever label rules 1-3 produced is checked against every label
         already assigned so far; if the source query already happens to
         use that exact string for a different column (e.g. a real column
         literally named "Name (2)", "Column 3", or "Column 1" — the last
         of which a rule-3 rewrite of a raw "1" column could otherwise
         collide with), " (2)", " (3)", ... is appended again — as many
         times as it takes — until the label is free. This is what
         guarantees global uniqueness regardless of what the underlying
         SQL happens to name things, including collisions rules 1-3's own
         output could otherwise create.

    Every key this function returns is therefore guaranteed non-integer-
    like (see _is_js_array_index_key) as well as unique, so Object.keys()
    on the resulting row objects in a real JS engine enumerates them in
    exactly this function's own insertion order — see the module
    docstring's "Step 3B" note and this module's test suite for proof.
    """
    used: set = set()
    seen_counts: dict = {}
    keys: list = []
    for i, raw in enumerate(columns):
        name = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        base = name.strip()
        if not base:
            candidate = f"Column {i + 1}"
        else:
            seen_counts[base] = seen_counts.get(base, 0) + 1
            occurrence = seen_counts[base]
            candidate = base if occurrence == 1 else f"{base} ({occurrence})"

        if _is_js_array_index_key(candidate):
            candidate = f"Column {candidate}"

        final = candidate
        bump = 2
        while final in used:
            final = f"{candidate} ({bump})"
            bump += 1
        used.add(final)
        keys.append(final)
    return keys


def _execution_ran(result: AiFirstOrchestrationResult) -> bool:
    """True once core.sqlservice.sql_execution.execute_validated_sql was
    actually called for this question — i.e. validation succeeded and the
    budget had not yet been exhausted — regardless of whether that
    execution itself then succeeded or failed. durations.execution_ms is
    set on every path that reaches the execution stage (see
    core.sqlservice.ai_first_fast_path_service) and stays None on every
    path that stops before it (NOT_PREPARED, RETRIEVAL_FAILED,
    GENERATION_FAILED, CLARIFICATION_REQUIRED, VALIDATION_FAILED, or
    BUDGET_EXCEEDED raised before execution began)."""
    return result.durations.execution_ms is not None


def _execution_status_and_error(result: AiFirstOrchestrationResult) -> tuple[str, Optional[str], Optional[str]]:
    """Returns (execution.status, execution.error, execution.error_code).

    STATUS_ANSWER_FAILED and a STATUS_BUDGET_EXCEEDED that fired after
    execution completed both still report execution.status == "success" —
    the SQL itself ran fine; only the answer-generation stage (an entirely
    separate concern) failed or ran out of budget. This is a deliberate
    distinction from the pre-M-30 route's own behavior of collapsing any
    non-"success" agent outcome into a single execution status."""
    if result.status == STATUS_NOT_PREPARED:
        return "not_prepared", NOT_PREPARED_MESSAGE, NOT_PREPARED_ERROR_CODE
    if result.status == STATUS_EXECUTION_FAILED:
        category = result.error_category
        return (
            _EXECUTION_STATUS_BY_CATEGORY.get(category, "failed"),
            result.error,
            _EXECUTION_ERROR_CODE_BY_CATEGORY.get(category, _DEFAULT_EXECUTION_ERROR_CODE),
        )
    if _execution_ran(result):
        return "success", None, None
    return "not_executed", result.error, _ERROR_CODE_BY_ORCHESTRATION_STATUS.get(result.status)


def _execution_block(result: AiFirstOrchestrationResult) -> dict:
    status, error, error_code = _execution_status_and_error(result)
    ran = _execution_ran(result)
    display_columns = _display_row_keys(result.columns)
    rows = [
        {display_columns[position]: _json_safe_cell(value) for position, value in enumerate(row)}
        for row in result.rows
    ]
    return {
        "execution_id": None,
        "status": status,
        "source_id": result.source_id,
        "executed_at": None,
        "duration_ms": result.durations.execution_ms,
        "columns": display_columns,
        "canonical_columns": list(result.columns),
        "rows": rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "truncation_reason": result.truncation_reason,
        "row_limit_applied": SQL_EXECUTION_MAX_ROWS if ran else None,
        "warnings": list(result.warnings),
        "error": error,
        "error_code": error_code,
    }


def _stop_reason(result: AiFirstOrchestrationResult) -> Optional[str]:
    if result.status == STATUS_OK:
        return None
    if result.status == STATUS_NOT_PREPARED:
        return NOT_PREPARED_MESSAGE
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        return result.clarification_question
    if result.status in (STATUS_NON_DATA, STATUS_UNSUPPORTED):
        return result.answer or result.error
    return result.error


def build_execute_query_response(result: AiFirstOrchestrationResult) -> dict:
    """
    Adapts one fast-path AiFirstOrchestrationResult into a standalone
    query-response envelope (no active HTTP route returns it today):
    {"status": "success", "agent_status", "stop_reason", "data": {...},
    "warnings": [...], "trace": [...]}.

    Pure: reads AiFirstOrchestrationResult fields only, performs no I/O of any
    kind, never mutates its input (AiFirstOrchestrationResult/AiFirstStageDurations are
    both frozen dataclasses; every collection built here is a fresh
    list/dict), and never calls a route, provider, or database.

    This never returns an HTTP status code alongside the body: NOT_PREPARED
    already covers an unowned/nonexistent source (it has no READY retrieval
    build either) — every status here is a 200-shaped body.
    """
    agent_status = _AGENT_STATUS_BY_ORCHESTRATION_STATUS[result.status]

    data: dict[str, Any] = {
        "execution": _execution_block(result),
        "sql_generation": {
            "sql": result.sql,
            "interpretation": result.interpretation,
            "explanation": [result.interpretation] if result.interpretation else [],
            "governance_warnings": [],
        },
        "answer": result.answer,
        "insights": [_insight_to_dict(i) for i in result.insights],
        "caveats": list(result.caveats),
        "follow_up_questions": list(result.follow_up_questions),
    }
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        data["clarification"] = {
            "executed": False,
            "reason": "clarification_required",
            "message": result.clarification_question,
        }

    return {
        "status": "success",
        "agent_status": agent_status,
        "stop_reason": _stop_reason(result),
        "data": data,
        "warnings": list(result.warnings),
        "trace": [],
    }


def build_execute_query_error_response(
    source_id: int, *, agent_status: str, message: str, error_code: str,
) -> dict:
    """The query-response envelope for a failure that happens before any
    AiFirstOrchestrationResult exists (source not enabled, or a genuinely
    unexpected provider/construction failure). Built directly -- there is
    no result to adapt. `message` must already be a fixed, sanitized
    string, never an exception's own text."""
    return {
        "status": "success",
        "agent_status": agent_status,
        "stop_reason": message,
        "data": {
            "execution": {
                "execution_id": None,
                "status": "not_executed",
                "source_id": source_id,
                "executed_at": None,
                "duration_ms": None,
                "columns": [],
                "canonical_columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "truncation_reason": None,
                "row_limit_applied": None,
                "warnings": [],
                "error": message,
                "error_code": error_code,
            },
            "sql_generation": {"sql": None, "interpretation": None, "explanation": [], "governance_warnings": []},
            "answer": "",
            "insights": [],
            "caveats": [],
            "follow_up_questions": [],
        },
        "warnings": [],
        "trace": [],
    }
