"""
workflow_optimizer

Phase 4 module for analyzing and improving stored workflow definitions.
Will use execution history data to recommend step reordering, removal of
redundant steps, and promotion of repeated interpreter intents to workflows.
"""


class WorkflowOptimizer:

    def analyze_steps(self, step_results: list[dict]) -> dict | None:
        """Identify the slowest step and print an optimization recommendation.

        Each entry in step_results may carry a 'duration_ms' key (float).
        Steps without that key are skipped.
        Returns the slowest step dict, or None if no timing data is available.
        """
        timed = [s for s in step_results if s.get("duration_ms") is not None]
        if not timed:
            print("[WorkflowOptimizer] No timing data available to analyze.")
            return None

        slowest = max(timed, key=lambda s: s["duration_ms"])
        step_id = slowest.get("step_id", "unknown")
        print(
            f"[WorkflowOptimizer] Step '{step_id}' is the slowest "
            f"({slowest['duration_ms']:.2f} ms). "
            f"Consider optimizing or parallelizing it."
        )
        return slowest

    def recommend_action(self, slowest_step: dict) -> str:
        """Return a recommended action based on the slowest step's duration.

        Returns 'optimize' if duration_ms > 1, otherwise 'no_action'.
        """
        action = "optimize" if slowest_step.get("duration_ms", 0) > 1 else "no_action"
        print(f"[WorkflowOptimizer] Recommended action: {action}")
        return action
