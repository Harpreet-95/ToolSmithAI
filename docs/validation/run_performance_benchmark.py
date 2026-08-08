"""
Day 4, Capability 6, Task 2 — Fixed-question performance benchmark.

Drives the exact same production entry point run_acceptance_suite.py uses —
core.orchestrator.agent.answer_business_question() (the real path behind
POST /v1/composer/ask) — for 7 fixed questions, 3 rounds each, against the
real CCPP database. Read-only: every execution is the agent's own governed,
SELECT-only path: no writes, no schema changes, no bypassed governance/PII.

Round design: all 7 questions run once per round (round 1 = "cold", round 2
= "warm run 1", round 3 = "warm run 2"), matching run_acceptance_suite.py's
own _EXECUTION_SPACING_S sleep before every single call — LiveQueryEngine's
_check_user_rate_limit is keyed by user_id ALONE (RATE_LIMIT_WINDOW_S=2s,
execution_kind="user_query"), not per-question, so consecutive calls for
DIFFERENT questions collide exactly the same as consecutive calls for the
SAME question. This is a real safety limit, not something this harness
works around by weakening it — every call is spaced, not just repeats.

Captures, per run, from the real response path:
  - state.perf_trace (Task 1's orchestration-layer stage timing)
  - whether the AI question interpreter ran (an "ai_question_interpretation"
    stage present in perf_trace)
  - SQL Server execution time (perf_trace's "sql_server_execution" stage)

Additionally captures, via TEMPORARY counting wrappers installed only for
the duration of this script (never shipped in product code, same pattern
as the existing acceptance-suite harness's own read-only posture):
  - number of sqlite3 connections opened (data.db.get_connection)
  - number of OpenAI chat-completion calls (core.semantic.ai_interpreter)
  - whether the broad candidate-table search ran
    (data.semantic_retrieval_service.get_candidate_tables_with_ranking)

Usage:
    venv/Scripts/python docs/validation/run_performance_benchmark.py \
        --source-id 1 --user-id 28 \
        [--output docs/validation/results/perf_benchmark.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Same reason as run_acceptance_suite.py: populates core.connectors.registry,
# a side effect this standalone script needs but normally gets for free from
# importing api/v1/routes.py at app startup.
import core.connectors.relational.mssql       # noqa: E402,F401
import core.connectors.relational.mysql       # noqa: E402,F401
import core.connectors.relational.postgresql  # noqa: E402,F401

QUESTIONS = [
    "How many students are in the database?",
    "How many students started each year?",
    "How many clients did we have last quarter?",
    "Which states have the most clients?",
    "Show students, their enrollments, and course details.",
    "Show clients and their recruiters.",
    "Display candidates.",
]

ROUND_LABELS = ["cold", "warm_1", "warm_2"]

# Matches run_acceptance_suite.py's own _EXECUTION_SPACING_S — LiveQueryEngine
# rate-limits per user_id (RATE_LIMIT_WINDOW_S=2s), not per-question, so this
# must precede every call, not just repeats of the same question.
_EXECUTION_SPACING_S = 2.5


class _CountingWrap:
    """Wraps a module-level callable with a call counter, restoring the
    original on __exit__. Used only inside this benchmark script."""

    def __init__(self, module, attr: str):
        self._module = module
        self._attr = attr
        self._original = getattr(module, attr)
        self.count = 0

    def __enter__(self):
        original = self._original

        def _counted(*args, **kwargs):
            self.count += 1
            return original(*args, **kwargs)

        setattr(self._module, self._attr, _counted)
        return self

    def __exit__(self, *exc_info):
        setattr(self._module, self._attr, self._original)


def _run_one(source_id: int, user_id: str, question: str) -> dict:
    import sqlite3

    import data.db as db_module
    import data.query_planning_service as planning_module
    import data.semantic_retrieval_service as retrieval_module  # noqa: F401 (kept for clarity of origin)
    import core.semantic.ai_interpreter as interp_module
    from core.orchestrator.agent import answer_business_question

    # Counts every sqlite3 statement executed via Connection.execute/
    # executemany across ALL connections opened during this one run — the
    # "statements" half of the spec's "number of SQLite connections/
    # statements" metric. sqlite3.Connection is an immutable C extension
    # type (no per-instance __dict__, class-level method patching raises
    # TypeError), so this counts via a Connection subclass injected as
    # sqlite3.connect's factory instead, restored in finally.
    statement_count = {"n": 0}

    class _CountingConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            statement_count["n"] += 1
            return super().execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            statement_count["n"] += 1
            return super().executemany(*args, **kwargs)

    original_connect = sqlite3.connect

    def _counted_connect(*args, **kwargs):
        kwargs.setdefault("factory", _CountingConnection)
        return original_connect(*args, **kwargs)

    # data.query_planning_service imported get_candidate_tables_with_ranking
    # by value at module load time (`from ... import ... as
    # _get_ai_candidate_tables_ranked`), so patching the origin module after
    # import would miss it — patch the bound name it actually calls.
    original_ranked = planning_module._get_ai_candidate_tables_ranked
    broad_search_count = {"n": 0}

    def _counted_ranked(*args, **kwargs):
        broad_search_count["n"] += 1
        return original_ranked(*args, **kwargs)

    # ai_interpreter.interpret() constructs one openai.OpenAI(...) client per
    # call and makes exactly one chat.completions.create() call on it (no
    # internal retry loop) — counting client constructions is therefore an
    # exact count of OpenAI calls made.
    original_openai_cls = interp_module._openai.OpenAI if interp_module._OPENAI_AVAILABLE else None
    openai_call_count = {"n": 0}

    def _counted_openai_ctor(*args, **kwargs):
        openai_call_count["n"] += 1
        return original_openai_cls(*args, **kwargs)

    time.sleep(_EXECUTION_SPACING_S)

    with _CountingWrap(db_module, "get_connection") as conn_wrap:
        planning_module._get_ai_candidate_tables_ranked = _counted_ranked
        if original_openai_cls is not None:
            interp_module._openai.OpenAI = _counted_openai_ctor
        sqlite3.connect = _counted_connect
        try:
            wall_t0 = time.monotonic()
            state = answer_business_question(source_id, user_id, question)
            wall_ms = (time.monotonic() - wall_t0) * 1000
        finally:
            planning_module._get_ai_candidate_tables_ranked = original_ranked
            if original_openai_cls is not None:
                interp_module._openai.OpenAI = original_openai_cls
            sqlite3.connect = original_connect

    openai_calls = openai_call_count["n"] if original_openai_cls is not None else None

    perf_trace = state.perf_trace or {"stages": [], "slowest_stage": None}
    stages_by_name = {s["stage"]: s["duration_ms"] for s in perf_trace["stages"]}

    return {
        "question": question,
        "wall_ms": round(wall_ms, 2),
        "agent_status": state.status.value if state.status else None,
        "ai_ran": "ai_question_interpretation" in stages_by_name,
        "broad_metadata_search_ran": broad_search_count["n"] > 0,
        "sqlite_connections": conn_wrap.count,
        "sqlite_statements": statement_count["n"],
        "openai_calls": openai_calls,
        "sql_server_execution_ms": stages_by_name.get("sql_server_execution"),
        "perf_trace": perf_trace,
        "slowest_stage": perf_trace.get("slowest_stage"),
    }


def run(source_id: int, user_id: str, questions: list[str] = QUESTIONS) -> dict:
    # Round design (see module docstring) — not per-question sleep spacing.
    rounds: list[list[dict]] = []
    for label in ROUND_LABELS:
        round_results = []
        for question in questions:
            round_results.append(_run_one(source_id, user_id, question))
        rounds.append(round_results)
        print(f"  completed round: {label}")

    # Reshape from [round][question] to [question] -> {round_label: entry}
    by_question: dict[str, dict] = {q: {} for q in questions}
    for label, round_results in zip(ROUND_LABELS, rounds):
        for entry in round_results:
            by_question[entry["question"]][label] = entry

    summary = []
    for question in questions:
        runs = by_question[question]
        wall_times = [runs[label]["wall_ms"] for label in ROUND_LABELS]
        summary.append({
            "question": question,
            "cold_ms": runs["cold"]["wall_ms"],
            "warm_1_ms": runs["warm_1"]["wall_ms"],
            "warm_2_ms": runs["warm_2"]["wall_ms"],
            "median_ms": round(statistics.median(wall_times), 2),
            "slowest_stage_warm_2": runs["warm_2"]["slowest_stage"],
            "ai_ran_cold": runs["cold"]["ai_ran"],
            "ai_ran_warm_2": runs["warm_2"]["ai_ran"],
            "broad_metadata_search_ran_cold": runs["cold"]["broad_metadata_search_ran"],
            "broad_metadata_search_ran_warm_2": runs["warm_2"]["broad_metadata_search_ran"],
            "sqlite_connections_cold": runs["cold"]["sqlite_connections"],
            "sqlite_connections_warm_2": runs["warm_2"]["sqlite_connections"],
            "sqlite_statements_cold": runs["cold"]["sqlite_statements"],
            "sqlite_statements_warm_2": runs["warm_2"]["sqlite_statements"],
            "openai_calls_cold": runs["cold"]["openai_calls"],
            "sql_server_execution_ms_warm_2": runs["warm_2"]["sql_server_execution_ms"],
            "agent_status": runs["warm_2"]["agent_status"],
        })

    return {"source_id": source_id, "user_id": user_id, "summary": summary, "raw_rounds": rounds}


def _print_report(result: dict) -> None:
    print(f"\n=== Performance Benchmark — source_id={result['source_id']} ===\n")
    header = (
        f"{'Question':<55} {'Cold':>8} {'Warm1':>8} {'Warm2':>8} {'Median':>8}  "
        f"{'Slowest stage (warm2)':<30} {'AI':>4} {'Broad':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in result["summary"]:
        slowest = row["slowest_stage_warm_2"]
        slowest_label = f"{slowest['stage']}={slowest['duration_ms']:.0f}ms" if slowest else "-"
        print(
            f"{row['question'][:54]:<55} {row['cold_ms']:>8.0f} {row['warm_1_ms']:>8.0f} "
            f"{row['warm_2_ms']:>8.0f} {row['median_ms']:>8.0f}  "
            f"{slowest_label:<30} {'Y' if row['ai_ran_warm_2'] else 'N':>4} "
            f"{'Y' if row['broad_metadata_search_ran_warm_2'] else 'N':>6}"
        )
    print()
    for row in result["summary"]:
        print(
            f"  {row['question']!r}: sqlite_conns(cold/warm2)="
            f"{row['sqlite_connections_cold']}/{row['sqlite_connections_warm_2']} "
            f"sqlite_stmts(cold/warm2)={row['sqlite_statements_cold']}/{row['sqlite_statements_warm_2']} "
            f"openai_calls(cold)={row['openai_calls_cold']} "
            f"sql_server_exec_ms(warm2)={row['sql_server_execution_ms_warm_2']} "
            f"agent_status={row['agent_status']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--output", type=Path, default=None, help="Write full JSON results here")
    args = parser.parse_args()

    result = run(args.source_id, args.user_id)
    _print_report(result)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nFull results written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
