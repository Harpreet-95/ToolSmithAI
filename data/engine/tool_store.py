"""
Persistence store for ToolDefinition records.

Table: engine_tools
  id              TEXT PRIMARY KEY   (tool_def.id — uuid)
  name            TEXT NOT NULL      (indexed column for filtering)
  version         TEXT NOT NULL
  status          TEXT NOT NULL      (indexed column for filtering)
  definition_json TEXT NOT NULL      (full ToolDefinition serialized to JSON)
  created_at      TEXT NOT NULL
  updated_at      TEXT NOT NULL

No business logic. No approval decisions. No runtime execution.
"""

import dataclasses
import json
import logging
from datetime import datetime, timezone
from enum import Enum

from data.db import get_connection
from core.engine.contracts import ToolDefinition, ToolStatus
from core.engine.schema import validate_tool_definition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engine_tools (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                version         TEXT NOT NULL,
                status          TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_engine_tools_status
                ON engine_tools (status);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

class _Encoder(json.JSONEncoder):
    """Handles datetime → ISO string and Enum → value for JSON serialisation."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def _to_json(tool_def: ToolDefinition) -> str:
    return json.dumps(dataclasses.asdict(tool_def), cls=_Encoder)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_tool(tool_def: ToolDefinition) -> None:
    """Persist a new ToolDefinition. Raises sqlite3.IntegrityError on duplicate id."""
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO engine_tools
              (id, name, version, status, definition_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_def.id,
                tool_def.name,
                tool_def.version,
                tool_def.status.value,
                _to_json(tool_def),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_tool(tool_id: str) -> ToolDefinition | None:
    """Return a ToolDefinition by id, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT definition_json, created_at, updated_at FROM engine_tools WHERE id = ?",
            (tool_id,),
        ).fetchone()
        if row is None:
            return None
        tool = validate_tool_definition(json.loads(row["definition_json"]))
        # schema._build_metadata leaves timestamps at utcnow(); restore from table columns.
        tool.metadata.created_at = datetime.fromisoformat(row["created_at"])
        tool.metadata.updated_at = datetime.fromisoformat(row["updated_at"])
        return tool
    finally:
        conn.close()


def update_tool_status(tool_id: str, status: ToolStatus) -> None:
    """Update status in both the indexed column and inside definition_json."""
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT definition_json FROM engine_tools WHERE id = ?", (tool_id,)
        ).fetchone()
        if row is None:
            return
        raw = json.loads(row["definition_json"])
        raw["status"] = status.value
        conn.execute(
            """
            UPDATE engine_tools
               SET status = ?, definition_json = ?, updated_at = ?
             WHERE id = ?
            """,
            (status.value, json.dumps(raw), now, tool_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_tools() -> list[ToolDefinition]:
    """Return all ToolDefinitions ordered newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT definition_json, created_at, updated_at FROM engine_tools ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for row in rows:
            try:
                tool = validate_tool_definition(json.loads(row["definition_json"]))
                tool.metadata.created_at = datetime.fromisoformat(row["created_at"])
                tool.metadata.updated_at = datetime.fromisoformat(row["updated_at"])
                result.append(tool)
            except Exception as e:
                logger.warning("Skipping malformed engine_tools row: %s", e)
        return result
    finally:
        conn.close()


_ensure_tables()
