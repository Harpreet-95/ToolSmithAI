"""
workflow_optimizer

Phase 4 module for analyzing and improving stored workflow definitions.
Will use execution history data to recommend step reordering, removal of
redundant steps, and promotion of repeated interpreter intents to workflows.
"""


class WorkflowOptimizer:

    def analyze_steps(self, step_results: list[dict]) -> None:
        """Identify the slowest step and print an optimization recommendation.

        Each entry in step_results may carry a 'duration_ms' key (float).
        Steps without that key are skipped.
        """
        timed = [s for s in step_results if s.get("duration_ms") is not None]
        if not timed:
            print("[WorkflowOptimizer] No timing data available to analyze.")
            return

        slowest = max(timed, key=lambda s: s["duration_ms"])
        step_id = slowest.get("step_id", "unknown")
        print(
            f"[WorkflowOptimizer] Step '{step_id}' is the slowest "
            f"({slowest['duration_ms']:.2f} ms). "
            f"Consider optimizing or parallelizing it."
        )
