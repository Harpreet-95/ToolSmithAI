"""
performance_tracker

Phase 4 module for tracking execution performance metrics per step and plan.
Will analyze duration, step latency, and failure rates to surface bottlenecks
and inform optimization decisions.
"""

import time


class PerformanceTracker:

    def __init__(self):
        self._start_times: dict[str, float] = {}

    def start_timer(self, plan_id: str) -> None:
        self._start_times[plan_id] = time.perf_counter()

    def end_timer(self, plan_id: str) -> None:
        start = self._start_times.pop(plan_id, None)
        if start is None:
            print(f"[PerformanceTracker] No timer found for plan_id: '{plan_id}'")
            return
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(f"[PerformanceTracker] plan_id='{plan_id}' completed in {elapsed_ms:.2f}ms", flush=True)
