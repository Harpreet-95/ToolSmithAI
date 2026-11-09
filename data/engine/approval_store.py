"""
Append-only persistence store for ApprovalEvent records.

Table: engine_approval_events
  id           TEXT PRIMARY KEY   (event.id — uuid)
  tool_id      TEXT NOT NULL
  tool_version TEXT NOT NULL
  event_type   TEXT NOT NULL
  actor_id     TEXT NOT NULL
  notes        TEXT
  timestamp    TEXT NOT NULL

The table is append-only: rows are never updated or deleted.
Approval history is an immutable audit trail.

No business logic. No approval decisions. No runtime execution.
"""

import logging
from datetime import datetime, timezone

from data.db import get_connection
from core.engine.contracts import ApprovalEvent, ApprovalEventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engine_approval_events (
                id           TEXT PRIMARY KEY,
                tool_id      TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                actor_id     TEXT NOT NULL,
                notes        TEXT,
                timestamp    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_approval_events_tool_id
                ON engine_approval_events (tool_id);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _row_to_event(row: dict) -> ApprovalEvent:
    return ApprovalEvent(
        id=row["id"],
        tool_id=row["tool_id"],
        tool_version=row["tool_version"],
        event_type=ApprovalEventType(row["event_type"]),
        actor_id=row["actor_id"],
        notes=row.get("notes"),
        timestamp=datetime.fromisoformat(row["timestamp"]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_approval_event(event: ApprovalEvent) -> None:
    """Append an ApprovalEvent to the log. Never updates existing rows."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO engine_approval_events
              (id, tool_id, tool_version, event_type, actor_id, notes, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.tool_id,
                event.tool_version,
                event.event_type.value,
                event.actor_id,
                event.notes,
                event.timestamp.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_approval_events(tool_id: str) -> list[ApprovalEvent]:
    """Return all approval events for a tool in chronological order."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM engine_approval_events
             WHERE tool_id = ?
             ORDER BY timestamp ASC
            """,
            (tool_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(_row_to_event(dict(row)))
            except Exception as e:
                logger.warning("Skipping malformed engine_approval_events row: %s", e)
        return result
    finally:
        conn.close()


_ensure_tables()
