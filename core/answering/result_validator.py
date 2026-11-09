"""
Result Validator — Enterprise AI Analyst Agent, Milestone M-27.

Bounded, purely structural validation of an already-executed live query
result, run before answer generation. Reuses the exact objects already
flowing through the pipeline:

- execution_result: the dict produced by
  core.live.query_engine.LiveQueryEngine.execute().to_dict() /
  data.query_execution_service.execute_generated_query(), with
  core.orchestrator.context_builder._build_business_plan()'s output already
  attached under execution_result["business_plan"] — the same convention
  core.answering.result_formatter.classify_result_shape()/build_business_answer()
  already rely on (a single combined `data` dict).
- sql_plan (optional): the existing data.sql_planning_service.build_sql_plan()
  output, read only for its already-computed joins[].fanout_risk — needed
  for the join-multiplication signal, which business_plan does not carry.

No SQL is built, parsed, or re-planned here. No LLM is consulted. Nothing
here decides to retry — a caller (the agent loop, Milestone M-28) inspects
.valid/.blocking_reasons and decides whether one bounded plan/SQL revision is
worth attempting; this module only reports what it structurally observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.answering.result_formatter import classify_result_shape

_HIGH_FANOUT_RISK = "HIGH"
_AGGREGATE_SCALAR_SHAPES = (
    "scalar_count", "scalar_count_distinct", "scalar_sum", "scalar_avg",
    "scalar_minmax", "null_scalar", "empty",
)


@dataclass(frozen=True)
class ResultValidation:
    valid: bool
    result_shape: str
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)


def validate_execution_result(execution_result: dict, *, sql_plan: dict | None = None) -> ResultValidation:
    """
    Validate an executed query result against the semantic plan that produced
    it. Returns a ResultValidation whose .valid mirrors
    data.sql_planning_service.build_sql_plan()'s own
    `valid = not blocking_reasons` convention — hard, structural mismatches
    (missing expected columns, unusable row shape, broken grain) block;
    softer risk signals (aggregation/shape mismatch, an unresolved ranking,
    a possible join-fanout inflation, an unexplained empty result) are
    recorded as warnings only, since none of them is certainly wrong.
    """
    checks: dict[str, bool] = {}
    warnings: list[dict] = []
    blocking_reasons: list[str] = []

    if not execution_result:
        return ResultValidation(
            valid=False, result_shape="tabular_fallback",
            checks={"executed_successfully": False},
            blocking_reasons=["No execution result was provided."],
        )

    shape = classify_result_shape(execution_result)
    plan = execution_result.get("business_plan") or {}
    row_count = execution_result.get("row_count") or 0
    rows = execution_result.get("rows") or []
    columns = execution_result.get("columns") or []
    status = execution_result.get("status")

    # --- execution succeeded ------------------------------------------------
    checks["executed_successfully"] = status == "success"
    if not checks["executed_successfully"]:
        blocking_reasons.append(
            f"Execution did not succeed (status={status!r}): "
            f"{execution_result.get('error') or 'no error detail'}."
        )
        return ResultValidation(
            valid=False, result_shape=shape, checks=checks,
            warnings=warnings, blocking_reasons=blocking_reasons,
        )

    # --- result has expected columns -----------------------------------------
    expected_aliases = {r.get("alias") for r in (plan.get("select") or []) if r.get("alias")}
    returned_column_names = {c.get("name") for c in columns if isinstance(c, dict)}
    missing_columns = sorted(expected_aliases - returned_column_names)
    checks["expected_columns_present"] = not missing_columns
    if missing_columns:
        blocking_reasons.append(
            f"Expected column(s) missing from the execution result: {', '.join(missing_columns)}."
        )

    # --- row shape is usable --------------------------------------------------
    checks["row_shape_usable"] = row_count == 0 or bool(columns)
    if row_count and not columns:
        blocking_reasons.append("Result has rows but no column metadata — shape is unusable.")

    # --- grouping / output grain plausible ------------------------------------
    # Checks the *output alias* the plan recorded for each grouping column,
    # not the logical column_name — a calendar-grain dimension (e.g.
    # "StartDate" grouped by year) projects under its grain alias
    # ("start_year"), never the raw column name, so the alias is the only
    # thing that can actually appear in a returned row. A plain (non-grain)
    # dimension's alias equals its column_name, so this is a strict
    # generalization, not a weakening, of the prior column_name-only check.
    group_by = plan.get("group_by") or []
    if group_by and rows:
        group_columns = {
            (g.get("alias") or g.get("column_name")) for g in group_by if g.get("column_name")
        }
        missing_grain = sorted(group_columns - set(rows[0].keys()))
        checks["grain_plausible"] = not missing_grain
        if missing_grain:
            blocking_reasons.append(
                f"Requested grouping column(s) not present in returned rows: {', '.join(missing_grain)}."
            )
    else:
        checks["grain_plausible"] = True

    # --- aggregation matches the semantic plan --------------------------------
    aggregation = plan.get("aggregation")
    if aggregation and not group_by:
        checks["aggregation_shape_consistent"] = shape in _AGGREGATE_SCALAR_SHAPES
    else:
        checks["aggregation_shape_consistent"] = True
    if not checks["aggregation_shape_consistent"]:
        warnings.append({
            "type": "aggregation_shape_mismatch", "severity": "MEDIUM",
            "message": f"An aggregation ({aggregation}) was planned but the result shape "
                       f"classified as '{shape}', not a scalar aggregate.",
        })

    # --- requested date range is represented ----------------------------------
    date_context = plan.get("date_context")
    if date_context:
        applied = any(
            w.get("operator") == "BETWEEN"
            and w.get("value") == [date_context.get("start"), date_context.get("end")]
            for w in (plan.get("where") or [])
        )
        checks["date_range_represented"] = applied
        if not applied:
            warnings.append({
                "type": "date_range_not_represented", "severity": "MEDIUM",
                "message": f"A '{date_context.get('label')}' date range was recorded on the plan "
                           "but is not present among the applied filters.",
            })
    else:
        checks["date_range_represented"] = True

    # --- ranking/order exists when requested ----------------------------------
    order_intent = plan.get("order_intent") or {}
    if order_intent.get("limit"):
        order_by = plan.get("order_by") or []
        checks["ranking_present_when_requested"] = bool(order_by)
        if not order_by:
            warnings.append({
                "type": "ranking_not_applied", "severity": "MEDIUM",
                "message": "A Top/Bottom-N ranking was requested but no ORDER BY was resolved — "
                           "returned rows are limited but not actually ranked.",
            })
    else:
        checks["ranking_present_when_requested"] = True

    # --- comparison period(s) represented --------------------------------------
    # No comparison-period concept exists on business_plan today (trend /
    # period-over-period planning is separate, not-yet-built scope) — this
    # check is always satisfied until that planning exists; reads
    # plan["comparison_periods"] defensively so it activates automatically,
    # without any new planning logic added here, if a future milestone adds
    # that field.
    comparison_periods = plan.get("comparison_periods")
    checks["comparison_periods_represented"] = (
        True if not comparison_periods else len(rows) >= len(comparison_periods)
    )

    # --- result is not empty without explanation -------------------------------
    if row_count == 0:
        has_explanation = bool(plan.get("where")) or bool(date_context) or bool(plan.get("status_label"))
        if not has_explanation:
            warnings.append({
                "type": "unexplained_empty_result", "severity": "LOW",
                "message": "The query returned zero rows with no filters, date range, or status "
                           "constraint applied — verify the selected table/entity is correct.",
            })

    # --- obvious join multiplication signal (where possible) -------------------
    joins = (sql_plan or {}).get("joins") or []
    high_fanout_joins = [j for j in joins if j.get("fanout_risk") == _HIGH_FANOUT_RISK]
    join_multiplication_risk = bool(
        high_fanout_joins and aggregation in ("SUM", "COUNT", "AVG") and not plan.get("distinct")
    )
    checks["no_join_multiplication_signal"] = not join_multiplication_risk
    if join_multiplication_risk:
        warnings.append({
            "type": "possible_join_multiplication", "severity": "HIGH",
            "message": f"{len(high_fanout_joins)} HIGH fan-out join(s) are present with a "
                       f"non-distinct {aggregation} aggregation — the result may be inflated "
                       "by row multiplication.",
        })

    return ResultValidation(
        valid=not blocking_reasons, result_shape=shape, checks=checks,
        warnings=warnings, blocking_reasons=blocking_reasons,
    )
