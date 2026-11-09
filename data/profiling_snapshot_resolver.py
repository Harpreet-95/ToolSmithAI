"""
Shared profiling-snapshot resolution — Phase 1 of the Authoritative Profiling
Snapshot Architecture (status-aware selection).

This module is the single shared boundary every profiling consumer uses to
find "the snapshot to read" — see the ~30 call sites across business_
knowledge_service, dictionary_service, domain_service/domain_quality_service/
domain_refinement_service, entity_service, governance_service,
knowledge_graph_service, metadata_preparation_service,
relationship_service, semantic_contract_service, semantic_layer_service, and
core/profile_view/sql_adapter.py. Fixing selection HERE fixes all of them
with no call-site changes.

Policy (this phase): prefer the highest-snapshot_version row whose
status == ProfilingStatus.COMPLETE for a source_id. A newer CANCELLED/
RUNNING/etc. snapshot must never shadow older, usable COMPLETE profiling
evidence — confirmed live against a real production database: the newest snapshot for
a real source was CANCELLED after profiling only a fraction of its tables, while the
prior COMPLETE snapshot (one version older) had full, valid data for every
table, including the specific table (dbo.EDU_Track) whose missing primary-key
evidence broke downstream entity-key selection. Falls back to the highest-
snapshot_version row of ANY status only when the source has NO COMPLETE
snapshot at all (e.g. a brand-new source whose first profiling run is still
RUNNING) — this preserves the prior "always return the most recent attempt"
behavior for that one case, so a source with no good evidence yet degrades
exactly as it did before this phase, rather than newly returning None where
a (partial) row used to be returned.

Deliberately status-only, not table-coverage-aware: verified empirically
against a real production database's full profiling history (every COMPLETE snapshot back to
the source's first full run) that a COMPLETE snapshot's table_fqn coverage
never regresses versus the prior COMPLETE one — profiling_service.py's own
batch-completion logic (core/profiling/execution.py, `new_status =
ProfilingStatus.COMPLETE if is_done else ProfilingStatus.RUNNING`) only ever
marks a snapshot COMPLETE once every table has been visited, so a COMPLETE
snapshot omitting a table an earlier COMPLETE one had is not a shape this
architecture currently produces. If a future profiling mode changes that
invariant (e.g. an intentionally-scoped COMPLETE run), a table-aware
resolution function belongs here, additively, without touching this one —
not invented ahead of that need.

No writes. No ownership/user_id check — callers that need to verify a
source_id belongs to a user already do so themselves (see entity_service.py,
domain_service.py, etc.); duplicating that here would couple this neutral
primitive to a concern it doesn't own.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from data.db import get_connection
from core.profiling.models import ProfilingStatus

# Single-query selection: `(status = 'COMPLETE')` evaluates to 1/0 in SQLite,
# so ordering by it DESC first partitions every COMPLETE row ahead of every
# non-COMPLETE row; snapshot_version DESC then picks the newest within
# whichever partition LIMIT 1 lands in. One round trip, same policy as two
# separate "try COMPLETE, then fall back" queries would give — preserves the
# original single-query connection-count contract every caller (and their
# own tests) already relies on, rather than doubling query count per call.
_ORDER_BY_LATEST_COMPLETE_ELSE_LATEST = (
    f"ORDER BY (status = '{ProfilingStatus.COMPLETE.value}') DESC, snapshot_version DESC LIMIT 1"
)


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
    """Return the best profiling_snapshots row to read for source_id: the
    highest-snapshot_version row with status == COMPLETE, or (only when no
    COMPLETE row exists) the highest-snapshot_version row of any status. See
    module docstring for the full policy and why it is centralized here.

    If conn is supplied, it is used as-is: this function does not open,
    close, commit, or roll it back. If conn is omitted, a connection is
    opened via get_connection() and closed before returning.

    Returns None when no profiling_snapshots row exists for source_id.
    Performs no writes.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source_id, snapshot_version, status, schema_snapshot_id "
            "FROM profiling_snapshots WHERE source_id = ? "
            + _ORDER_BY_LATEST_COMPLETE_ELSE_LATEST,
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


def get_latest_two_complete_profiling_snapshots(
    source_id: int,
    conn: sqlite3.Connection | None = None,
) -> tuple[ProfilingSnapshotRef | None, ProfilingSnapshotRef | None]:
    """(prev, latest) — the two most recent status==COMPLETE profiling_snapshots
    rows for source_id, by snapshot_version. Mirrors data.lifecycle_service.
    get_latest_two_snapshots' prev/latest shape for schema snapshots, applied
    to profiling instead — the pairing a profiling/classification change
    detector needs (core.lifecycle.diff.diff_profiling_classification).

    Deliberately COMPLETE-only, not "latest of any status" like
    get_latest_profiling_snapshot's own read-time fallback: a RUNNING or
    CANCELLED snapshot is a partial, in-progress attempt whose classification
    coverage is not yet trustworthy to diff against — comparing against it
    would report every not-yet-reached table as a spurious "change" the
    moment the run finishes and the snapshot flips to COMPLETE, even though
    nothing about that table actually changed.

    latest is None only when the source has no COMPLETE profiling snapshot
    at all yet. prev is None when latest is the only (or first-ever)
    COMPLETE profiling snapshot for this source.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, source_id, snapshot_version, status, schema_snapshot_id "
            "FROM profiling_snapshots WHERE source_id = ? AND status = ? "
            "ORDER BY snapshot_version DESC LIMIT 2",
            (source_id, ProfilingStatus.COMPLETE.value),
        ).fetchall()
    finally:
        if owns_conn:
            conn.close()

    refs = [
        ProfilingSnapshotRef(
            id=r["id"], source_id=r["source_id"], snapshot_version=r["snapshot_version"],
            status=r["status"], schema_snapshot_id=r["schema_snapshot_id"],
        )
        for r in rows
    ]
    if not refs:
        return None, None
    if len(refs) == 1:
        return None, refs[0]
    return refs[1], refs[0]


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
    """Return the best profiling_snapshots row to read for source_id (same
    selection policy as get_latest_profiling_snapshot() — see module
    docstring), with the extended field set presentation consumers need.

    This function exists because remaining presentation consumers need
    fields beyond ProfilingSnapshotRef; rather than keep expanding that
    identity-focused dataclass, this adds a parallel, wider read for the
    consumers that actually need it. Both functions share the exact same
    ORDER BY selection clause (_ORDER_BY_LATEST_COMPLETE_ELSE_LATEST) so
    they can never disagree on which snapshot is "latest".

    Returns None when no profiling_snapshots row exists for source_id.
    Performs no writes.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source_id, snapshot_version, status, schema_snapshot_id, "
            "mode, created_at, completed_at, tables_total, tables_profiled, "
            "columns_profiled, pii_columns_found, total_rows_profiled "
            "FROM profiling_snapshots WHERE source_id = ? "
            + _ORDER_BY_LATEST_COMPLETE_ELSE_LATEST,
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
