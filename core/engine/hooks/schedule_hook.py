"""
Schedule integration hook stub for the Dynamic Tool Creation Engine.

Future responsibility:
  Bridge between tool definitions and the platform's scheduling subsystem
  (currently APScheduler via data/scheduled_workflow_service.py).

  after_plan   — if tool_def.schedule.enabled is True, register a pending
                 schedule entry so operators can review it before it goes live.
                 The entry should be stored with status='pending' until the
                 tool itself reaches status='approved'.
  before_run   — if the run was triggered by the scheduler (trigger_source ==
                 'scheduled'), validate that the schedule entry is still active
                 and has not been paused or deleted since the job was enqueued.
  after_run    — update the schedule entry's last_run_at, run_count, and
                 last_status; compute next_run_at from the cron expression.
  on_failure   — record the failed run against the schedule's health record
                 so the /scheduled-workflows/{id}/health endpoint can surface
                 it to operators.

  before_plan  — reserved; no scheduling action needed at planning time.

When implemented, this hook will call data/scheduled_workflow_service.py
functions. It must not create or modify any schedule until the tool is
approved (ToolStatus.APPROVED).

Currently a no-op — no schedules are created or modified.
"""

from __future__ import annotations

from core.engine.contracts import ExecutionContext, RunRecord, ToolDefinition
from core.engine.hooks.base import EngineHook


class ScheduleHook(EngineHook):
    """
    Schedule integration hook.

    All methods are no-ops until the scheduling subsystem is integrated.
    Override individual lifecycle methods to wire in schedule persistence
    without touching any other part of the engine.

    Must not create, modify, or delete any schedule entry in its current
    no-op form.
    """

    def before_plan(self, intent: str, context: dict) -> None:
        """Reserved — no scheduling action required at planning time."""

    def after_plan(self, tool_def: ToolDefinition, context: dict) -> None:
        """Future: register a pending schedule entry if tool_def.schedule.enabled."""

    def before_run(self, tool_def: ToolDefinition, context: ExecutionContext) -> None:
        """Future: validate schedule entry is still active before executing."""

    def after_run(self, run_record: RunRecord, context: ExecutionContext) -> None:
        """Future: update schedule health record (last_run_at, next_run_at, run_count)."""

    def on_failure(self, error: Exception, context: ExecutionContext) -> None:
        """Future: record failed scheduled run in schedule health record."""
