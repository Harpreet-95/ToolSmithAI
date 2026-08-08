"""
Shared profiling-snapshot resolution — Phase 0 of the Authoritative Profiling
Snapshot Architecture.

This module extracts the single, currently-duplicated query every profiling
consumer uses to find "the snapshot to read" into one shared boundary, WITHOUT
changing what that query returns.

Current policy (this phase): highest snapshot_version for a source_id, with
no regard to status or schema coverage. This is deliberately named
`get_latest_profiling_snapshot` rather than anything implying authority,
trust, or success — it does not guarantee the returned snapshot is complete
or usable, only that it is the most recently created one. A later phase may
introduce an authoritative-selection policy (status- and coverage-aware) as
a separate operation; this module intentionally does not implement that yet.

No writes. No ownership/user_id check — callers that need to verify a
source_id belongs to a user already do so themselves (see entity_service.py,
domain_service.py, etc.); duplicating that here would couple this neutral
primitive to a concern it doesn't own.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from data.db import get_connection


@dataclass(frozen=True)
class ProfilingSnapshotRef:
    """Minimal identifying/state fields for a profiling_snapshots row.

    Deliberately not named to imply authority — `status` is exposed as-read,
    not evaluated. Consumers that need to decide whether the snapshot is
    usable in some stronger sense must apply that policy themselves for now.
    """
    id: int
    source_id: int
    snapshot_version: int
    status: str
    schema_snapshot_id: int


def get_latest_profiling_snapshot(
    source_id: int,
    conn: sqlite3.Connection | None = None,
) -> ProfilingSnapshotRef | None:
    """Return the highest-snapshot_version profiling_snapshots row for source_id.

    Preserves the exact selection semantics already used across the codebase's
    existing call sites: ORDER BY snapshot_version DESC LIMIT 1, with no status
    or coverage filter. A newer CANCELLED or RUNNING snapshot is returned in
    preference to an older COMPLETE one — this is current, pre-existing
    platform behavior, not a defect introduced here.

    If conn is supplied, it is used as-is: this function does not open,
    close, commit, or roll it back, and executes exactly one query against
    it. If conn is omitted, a connection is opened via get_connection() and
    closed before returning, preserving prior behavior exactly.

    Returns None when no profiling_snapshots row exists for source_id.
    Performs no writes.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source_id, snapshot_version, status, schema_snapshot_id "
            "FROM profiling_snapshots "
            "WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None

    return ProfilingSnapshotRef(
        id=row["id"],
        source_id=row["source_id"],
        snapshot_version=row["snapshot_version"],
        status=row["status"],
        schema_snapshot_id=row["schema_snapshot_id"],
    )


@dataclass(frozen=True)
class ProfilingSnapshotDetail:
    """Extended, read-only view of a profiling_snapshots row for presentation
    consumers that need more than identity/state fields.

    Introduced instead of expanding ProfilingSnapshotRef — see module design
    note in get_latest_profiling_snapshot_detail(). Field set is fixed by the
    Phase 1F.0 contract; do not add, remove, or rename fields here without a
    corresponding change to that contract.
    """
    id: int
    source_id: int
    snapshot_version: int
    status: str
    schema_snapshot_id: int
    mode: str
    created_at: str
    completed_at: str | None
    tables_total: int
    tables_profiled: int
    columns_profiled: int
    pii_columns_found: int
    total_rows_profiled: int


def get_latest_profiling_snapshot_detail(source_id: int) -> ProfilingSnapshotDetail | None:
    """Return the highest-snapshot_version profiling_snapshots row for source_id,
    with the extended field set presentation consumers need.

    Selection policy is identical to get_latest_profiling_snapshot(): ORDER BY
    snapshot_version DESC LIMIT 1, no status or coverage filter. This function
    exists because remaining presentation consumers need fields beyond
    ProfilingSnapshotRef; rather than keep expanding that identity-focused
    dataclass, this adds a parallel, wider read for the consumers that
    actually need it. ProfilingSnapshotRef and get_latest_profiling_snapshot()
    are unchanged.

    Returns None when no profiling_snapshots row exists for source_id.
    Performs no writes.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source_id, snapshot_version, status, schema_snapshot_id, "
            "mode, created_at, completed_at, tables_total, tables_profiled, "
            "columns_profiled, pii_columns_found, total_rows_profiled "
            "FROM profiling_snapshots "
            "WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return ProfilingSnapshotDetail(
        id=row["id"],
        source_id=row["source_id"],
        snapshot_version=row["snapshot_version"],
        status=row["status"],
        schema_snapshot_id=row["schema_snapshot_id"],
        mode=row["mode"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        tables_total=row["tables_total"],
        tables_profiled=row["tables_profiled"],
        columns_profiled=row["columns_profiled"],
        pii_columns_found=row["pii_columns_found"],
        total_rows_profiled=row["total_rows_profiled"],
    )
