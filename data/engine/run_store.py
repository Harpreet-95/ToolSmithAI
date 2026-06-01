"""
Persistence store for RunRecord (engine tool execution history).

Table: engine_runs
  run_id             TEXT PRIMARY KEY
  tool_id            TEXT NOT NULL
  tool_version       TEXT NOT NULL
  trigger_source     TEXT NOT NULL
  status             TEXT NOT NULL
  inputs_json        TEXT NOT NULL
  step_results_json  TEXT NOT NULL
  outputs_json       TEXT NOT NULL
  started_at         TEXT NOT NULL
  finished_at        TEXT
  duration_ms        INTEGER
  user_id            TEXT NOT NULL
  error              TEXT

No business logic. No approval decisions. No runtime execution.
"""

import json
import logging
from datetime import datetime, timezone

from data.db import get_connection
from core.engine.contracts import ActionStatus, RunRecord, RunStatus, StepResult

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engine_runs (
                run_id             TEXT PRIMARY KEY,
                tool_id            TEXT NOT NULL,
                tool_version       TEXT NOT NULL,
                trigger_source     TEXT NOT NULL,
                status             TEXT NOT NULL,
                inputs_json        TEXT NOT NULL,
                step_results_json  TEXT NOT NULL,
                outputs_json       TEXT NOT NULL,
                started_at         TEXT NOT NULL,
                finished_at        TEXT,
                duration_ms        INTEGER,
                user_id            TEXT NOT NULL,
                error              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_engine_runs_tool_id
                ON engine_runs (tool_id);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialise_step_results(steps: list[StepResult]) -> str:
    return json.dumps([
        {
            "node_id":     s.node_id,
            "action_type": s.action_type,
            "status":      s.status.value,
            "output":      s.output,
            "error":       s.error,
            "duration_ms": s.duration_ms,
        }
        for s in steps
    ])


def _parse_step_result(d: dict) -> StepResult:
    return StepResult(
        node_id=d.get("node_id", ""),
        action_type=d.get("action_type", ""),
        status=ActionStatus(d.get("status", ActionStatus.COMPLETED.value)),
        output=d.get("output") or {},
        error=d.get("error"),
        duration_ms=d.get("duration_ms", 0),
    )


def _row_to_run_record(row: dict) -> RunRecord:
    steps_raw = json.loads(row["step_results_json"] or "[]")
    return RunRecord(
        run_id=row["run_id"],
        tool_id=row["tool_id"],
        tool_version=row["tool_version"],
        trigger_source=row["trigger_source"],
        status=RunStatus(row["status"]),
        inputs=json.loads(row["inputs_json"] or "{}"),
        step_results=[_parse_step_result(s) for s in steps_raw],
        outputs=json.loads(row["outputs_json"] or "{}"),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row.get("finished_at") else None,
        duration_ms=row.get("duration_ms"),
        user_id=row["user_id"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_run(run_record: RunRecord) -> None:
    """Insert a new RunRecord. Raises sqlite3.IntegrityError on duplicate run_id."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO engine_runs
              (run_id, tool_id, tool_version, trigger_source, status,
               inputs_json, step_results_json, outputs_json,
               started_at, finished_at, duration_ms, user_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_record.run_id,
                run_record.tool_id,
                run_record.tool_version,
                run_record.trigger_source,
                run_record.status.value,
                json.dumps(run_record.inputs),
                _serialise_step_results(run_record.step_results),
                json.dumps(run_record.outputs),
                run_record.started_at.isoformat(),
                run_record.finished_at.isoformat() if run_record.finished_at else None,
                run_record.duration_ms,
                run_record.user_id,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_run_status(
    run_id: str,
    status: RunStatus,
    outputs: dict | None = None,
    error: str | None = None,
) -> None:
    """
    Update status, outputs, and error on an existing run.

    Sets finished_at and computes duration_ms automatically when status
    is a terminal value (completed, failed, cancelled).
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT started_at FROM engine_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return

        finished_at = None
        duration_ms = None
        if status in _TERMINAL_STATUSES:
            finished_at = now
            try:
                # Strip tzinfo so naive and aware ISO strings compare cleanly.
                def _naive(s: str) -> datetime:
                    return datetime.fromisoformat(s).replace(tzinfo=None)
                duration_ms = int((_naive(now) - _naive(row["started_at"])).total_seconds() * 1000)
            except Exception:
                pass

        conn.execute(
            """
            UPDATE engine_runs
               SET status = ?, outputs_json = ?, error = ?,
                   finished_at = ?, duration_ms = ?
             WHERE run_id = ?
            """,
            (
                status.value,
                json.dumps(outputs or {}),
                error,
                finished_at,
                duration_ms,
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_run_steps(run_id: str, step_results: list[StepResult]) -> None:
    """Persist accumulated step_results for a run.

    Called by the runtime after all nodes have executed (before the final
    status update) so that get_run() returns a fully populated RunRecord.
    Safe to call with an empty list — stores [] rather than leaving the
    column at its initial empty value from create_run.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE engine_runs SET step_results_json = ? WHERE run_id = ?",
            (_serialise_step_results(step_results), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_run(run_id: str) -> RunRecord | None:
    """Return a RunRecord by run_id, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM engine_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_run_record(dict(row))
    finally:
        conn.close()


def list_runs_for_tool(tool_id: str) -> list[RunRecord]:
    """Return all RunRecords for a tool, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM engine_runs WHERE tool_id = ? ORDER BY started_at DESC",
            (tool_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(_row_to_run_record(dict(row)))
            except Exception as e:
                logger.warning("Skipping malformed engine_runs row %s: %s", dict(row).get("run_id"), e)
        return result
    finally:
        conn.close()


_ensure_tables()
