"""
Enterprise Acceptance Test Suite runner — Milestone M-23 (Phase 6.5).

A thin, deterministic driver, not a new engine: it reads
docs/validation/enterprise_business_validation_suite.v1.json and, for every
question, calls the exact same pipeline stages every prior milestone
(M-19 through M-22) already drove by hand:

    IntentResolver.resolve()
      -> core.semantic.concept_resolver.extract_terms()   (only for
         expected_intent == "sql_request" questions)
      -> data.query_planning_service.plan_business_query()
      -> data.sql_planning_service.build_sql_plan()
      -> data.sql_generation_service.generate_sql()

No SQL is executed (live_query_enabled stays untouched, out of scope for this
milestone) — grading only checks whether real SQL was *generated*.

Grading contract (matches docs/validation/README.md and the exact rule M-22
already established, ENTERPRISE_DELIVERY_PROGRAM.md's M-22 section):
  - expected_outcome == "SUCCESS"       -> pass iff real SQL was generated.
  - expected_outcome == "KNOWN_DEFECT"  -> always graded as pass (a
    documented, already-cataloged residual either way); if the pipeline now
    safely refuses instead of generating the previously-known-wrong SQL, that
    is reported separately as an IMPROVED case, not silently absorbed.
  - every other expected_outcome (REFUSED_SAFE, AMBIGUOUS, CLARIFICATION_NEEDED,
    NOT_SUPPORTED) -> pass iff the pipeline safely refuses (no SQL generated)
    rather than fabricating an answer.

For questions whose expected_intent is not "sql_request" (Metadata,
Dictionary, Governance, ...), only intent-match is graded — the SQL pipeline
does not apply to those intents, matching M-21's own scope.

Usage:
    venv/Scripts/python docs/validation/run_acceptance_suite.py \
        --source-id 1 --user-id 28 \
        [--output docs/validation/results/after.json] \
        [--before docs/validation/results/before.json]

This script makes no writes anywhere — it is read-only against whatever
source_id/user_id you point it at.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SUITE_PATH = Path(__file__).parent / "enterprise_business_validation_suite.v1.json"

_REFUSAL_OUTCOMES = frozenset({
    "REFUSED_SAFE", "AMBIGUOUS", "CLARIFICATION_NEEDED", "NOT_SUPPORTED",
})


def _load_suite(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = []
    for cat in data["categories"]:
        for q in cat["questions"]:
            q = dict(q)
            q.setdefault("category", cat["name"])
            questions.append(q)
    return questions


def _grade_one(source_id: int, user_id: str, q: dict) -> dict:
    """Run one question through the pipeline and grade it. Never raises —
    any exception is recorded as a failure with the traceback as the reason,
    so one bad question never aborts the whole run."""
    from core.orchestrator.intent_resolver import IntentResolver

    question = q["question"]
    expected_intent = q["expected_intent"]
    expected_outcome = q["expected_outcome"]

    entry = {
        "id": q["id"], "category": q["category"], "question": question,
        "expected_intent": expected_intent, "expected_outcome": expected_outcome,
        "actual_intent": None, "sql_generated": None, "passed": False,
        "reason": "", "improved_known_defect": False,
    }

    try:
        resolved = IntentResolver().resolve(question)
        entry["actual_intent"] = resolved.intent_type.value
        intent_match = entry["actual_intent"] == expected_intent

        if expected_intent != "sql_request":
            entry["passed"] = intent_match
            if not intent_match:
                entry["reason"] = f"Intent mismatch: got '{entry['actual_intent']}'."
            return entry

        from core.semantic.concept_resolver import extract_terms
        from data.query_planning_service import plan_business_query
        from data.sql_planning_service import build_sql_plan
        from data.sql_generation_service import generate_sql

        concepts, measures, dimensions = extract_terms(question)
        query_plan = plan_business_query(
            source_id, user_id,
            {"question": question, "concepts": concepts, "measures": measures, "dimensions": dimensions},
        )
        sql_plan = build_sql_plan(source_id, user_id, query_plan)
        result = generate_sql(source_id, user_id, sql_plan)
        generated = bool(result.get("sql"))
        entry["sql_generated"] = generated

        if expected_outcome == "SUCCESS":
            outcome_pass = generated
        elif expected_outcome == "KNOWN_DEFECT":
            outcome_pass = True
            if not generated:
                entry["improved_known_defect"] = True
        else:
            outcome_pass = not generated

        entry["passed"] = intent_match and outcome_pass
        if not entry["passed"]:
            if not intent_match:
                entry["reason"] = f"Intent mismatch: got '{entry['actual_intent']}'. "
            explanation = result.get("explanation") or []
            blocking = ((sql_plan or {}).get("validation") or {}).get("blocking_reasons") or []
            entry["reason"] += "; ".join(explanation or blocking) or (
                "SQL was generated but expected_outcome required a safe refusal."
                if generated else "No SQL generated and no explanation returned."
            )
        return entry

    except Exception as exc:  # noqa: BLE001 - never let one question abort the run
        entry["reason"] = f"ERROR: {exc}"
        entry["_traceback"] = traceback.format_exc()
        return entry


def run(source_id: int, user_id: str, suite_path: Path = _SUITE_PATH) -> dict:
    questions = _load_suite(suite_path)
    results = [_grade_one(source_id, user_id, q) for q in questions]
    return {"source_id": source_id, "user_id": user_id, "results": results}


def _category_table(results: list[dict]) -> list[tuple[str, int, int]]:
    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    rows = []
    for cat, items in by_cat.items():
        passed = sum(1 for i in items if i["passed"])
        rows.append((cat, passed, len(items)))
    return rows


def _print_report(run_result: dict) -> None:
    results = run_result["results"]
    total_passed = sum(1 for r in results if r["passed"])
    print(f"\n=== Enterprise Acceptance Test Suite — source_id={run_result['source_id']} ===")
    print(f"Overall: {total_passed}/{len(results)} passed\n")

    print("| Category | Pass | Total |")
    print("|---|---|---|")
    for cat, passed, total in sorted(_category_table(results)):
        print(f"| {cat} | {passed} | {total} |")

    sql_scope = [r for r in results if r["expected_intent"] == "sql_request"]
    sql_passed = sum(1 for r in sql_scope if r["passed"])
    print(f"\nsql_request-scoped: {sql_passed}/{len(sql_scope)} passed")

    improved = [r for r in results if r["improved_known_defect"]]
    if improved:
        print(f"\nKNOWN_DEFECT questions that now safely refuse instead of generating wrong SQL ({len(improved)}):")
        for r in improved:
            print(f"  - {r['id']}: {r['question']!r}")

    failing = [r for r in results if not r["passed"]]
    if failing:
        print(f"\nFailing ({len(failing)}):")
        for r in failing:
            print(f"  - {r['id']} [{r['category']}] {r['question']!r}")
            print(f"      expected_intent={r['expected_intent']} expected_outcome={r['expected_outcome']} "
                  f"actual_intent={r['actual_intent']} sql_generated={r['sql_generated']}")
            print(f"      reason: {r['reason']}")


def _print_diff(before: dict, after: dict) -> None:
    before_by_id = {r["id"]: r for r in before["results"]}
    after_by_id = {r["id"]: r for r in after["results"]}

    before_passed = sum(1 for r in before["results"] if r["passed"])
    after_passed = sum(1 for r in after["results"] if r["passed"])
    print(f"\n=== Before/After ===")
    print(f"Before: {before_passed}/{len(before['results'])} passed")
    print(f"After:  {after_passed}/{len(after['results'])} passed")

    newly_passing = [
        qid for qid, a in after_by_id.items()
        if a["passed"] and qid in before_by_id and not before_by_id[qid]["passed"]
    ]
    newly_failing = [
        qid for qid, a in after_by_id.items()
        if not a["passed"] and qid in before_by_id and before_by_id[qid]["passed"]
    ]

    if newly_passing:
        print(f"\nNewly passing ({len(newly_passing)}):")
        for qid in newly_passing:
            print(f"  - {qid}: {after_by_id[qid]['question']!r}")
    if newly_failing:
        print(f"\nNewly failing / regressed ({len(newly_failing)}):")
        for qid in newly_failing:
            print(f"  - {qid}: {after_by_id[qid]['question']!r}")
    if not newly_passing and not newly_failing:
        print("\nNo change in pass/fail status for any question.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--suite-path", type=Path, default=_SUITE_PATH)
    parser.add_argument("--output", type=Path, default=None, help="Write full JSON results here")
    parser.add_argument("--before", type=Path, default=None, help="Prior run's JSON output to diff against")
    args = parser.parse_args()

    run_result = run(args.source_id, args.user_id, args.suite_path)
    _print_report(run_result)

    if args.before is not None:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        _print_diff(before, run_result)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(run_result, indent=2), encoding="utf-8")
        print(f"\nWrote results to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
