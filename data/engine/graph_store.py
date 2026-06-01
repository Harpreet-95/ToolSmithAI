"""
Persistence store for ExecutionGraph records.

Table: engine_graphs
  tool_id    TEXT PRIMARY KEY   (foreign key to engine_tools.id)
  graph_json TEXT NOT NULL      (ExecutionGraph serialised to JSON)
  saved_at   TEXT NOT NULL

Graphs are stored separately from the full ToolDefinition so callers can
fetch just the graph without loading the entire tool. Supports upsert so
a graph can be updated when a tool draft is revised.

No business logic. No cycle detection. No runtime execution.
"""

import dataclasses
import json
import logging
from datetime import datetime, timezone
from enum import Enum

from data.db import get_connection
from core.engine.contracts import (
    ActionEdge,
    ActionNode,
    ExecutionGraph,
    FailureMode,
    RetryConfig,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def _ensure_tables() -> None:
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engine_graphs (
                tool_id    TEXT PRIMARY KEY,
                graph_json TEXT NOT NULL,
                saved_at   TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict_to_graph(raw: dict) -> ExecutionGraph:
    """Reconstruct an ExecutionGraph from a stored dict. Trusts stored data."""
    nodes = [
        ActionNode(
            id=n["id"],
            label=n.get("label", ""),
            action_type=n["action_type"],
            params=n.get("params") or {},
            template_refs=n.get("template_refs") or {},
            condition=n.get("condition"),
            retry=RetryConfig(
                max_attempts=n.get("retry", {}).get("max_attempts", 1),
                backoff_ms=n.get("retry", {}).get("backoff_ms", 1_000),
                retryable_errors=n.get("retry", {}).get("retryable_errors", []),
            ),
            timeout_ms=n.get("timeout_ms", 30_000),
            on_failure=FailureMode(n.get("on_failure", FailureMode.FAIL_GRAPH.value)),
        )
        for n in raw.get("nodes", [])
    ]
    edges = [
        ActionEdge(
            from_node_id=e["from_node_id"],
            to_node_id=e["to_node_id"],
            condition=e.get("condition"),
        )
        for e in raw.get("edges", [])
    ]
    return ExecutionGraph(
        entry_node_id=raw.get("entry_node_id", ""),
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_graph(tool_id: str, graph: ExecutionGraph) -> None:
    """Persist an ExecutionGraph for tool_id. Upserts on conflict."""
    raw = json.dumps(dataclasses.asdict(graph), cls=_Encoder)
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO engine_graphs (tool_id, graph_json, saved_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tool_id) DO UPDATE
               SET graph_json = excluded.graph_json,
                   saved_at   = excluded.saved_at
            """,
            (tool_id, raw, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_graph(tool_id: str) -> ExecutionGraph | None:
    """Return the ExecutionGraph for tool_id, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT graph_json FROM engine_graphs WHERE tool_id = ?", (tool_id,)
        ).fetchone()
        if row is None:
            return None
        return _dict_to_graph(json.loads(row["graph_json"]))
    finally:
        conn.close()


_ensure_tables()
