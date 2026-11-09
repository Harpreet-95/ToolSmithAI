"""Request-scoped stage timing for the live-SQL answer pipeline.

Capability 6 (Performance) instrumentation, orchestration-layer only: call
sites live in agent.py / context_builder.py / query_execution_service.py /
query_engine.py / result_formatter.py. Nothing inside the frozen planner,
SQL-generation, or governance decision logic is modified — only wrapped.

Uses a contextvar so wrapped functions never need a timer parameter threaded
through their signatures, and `measure()` is a safe no-op when no timer is
active for the current context (e.g. tests or scripts that call these
functions directly without going through `start()`).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_current: ContextVar[Optional["StageTimer"]] = ContextVar("_current_stage_timer", default=None)


class StageTimer:
    def __init__(self) -> None:
        self.stages: list[dict] = []

    def record(self, stage: str, duration_ms: float) -> None:
        self.stages.append({"stage": stage, "duration_ms": round(duration_ms, 2)})

    def slowest(self) -> Optional[dict]:
        return max(self.stages, key=lambda s: s["duration_ms"]) if self.stages else None

    def to_dict(self) -> dict:
        return {"stages": list(self.stages), "slowest_stage": self.slowest()}


@contextmanager
def start() -> Iterator[StageTimer]:
    """Begin a new request-scoped timer. One call site per request, at the
    top of answer_business_question — everything it calls that uses
    `measure()` records into this timer via the contextvar."""
    timer = StageTimer()
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)


@contextmanager
def measure(stage: str) -> Iterator[None]:
    """Time one named stage. No-op if no timer is active for this context."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        timer = _current.get()
        if timer is not None:
            timer.record(stage, (time.monotonic() - t0) * 1000)


def current() -> Optional[StageTimer]:
    return _current.get()


def record(stage: str, duration_ms: float) -> None:
    """Record a duration already computed via an existing t0/time.monotonic()
    pattern (common in agent.py's trace steps) without wrapping a second,
    redundant `measure()` context manager around the same call. No-op if no
    timer is active."""
    timer = _current.get()
    if timer is not None:
        timer.record(stage, duration_ms)
