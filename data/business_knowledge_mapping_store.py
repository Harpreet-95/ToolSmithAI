"""
Business Knowledge mapping storage -- Phase 7, Step 3J, Part 4.

Raw CRUD for business_knowledge_mappings/business_knowledge_mapping_runs
(data.models.ensure_business_knowledge_tables). This module never calls an
AI provider and never decides whether a mapping is trustworthy -- it only
persists whatever core.sqlservice.business_knowledge_mapper has ALREADY
deterministically verified, and gives that module the read/write primitives
it needs for resumability and the zero-repeat-provider-call guarantee.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from data.db import get_connection

MAPPING_ROLES = frozenset({
    "CANONICAL_ENTITY_SOURCE", "PREFERRED_BUSINESS_VIEW", "DETAIL_ACTIVITY_SOURCE",
    "ENROLLMENT_SOURCE", "FEE_SOURCE", "FULL_PAYMENT_SOURCE", "INSTALLMENT_PAYMENT_SOURCE",
    "STATUS_SOURCE", "OUTCOME_SOURCE", "CONNECTOR_ONLY", "UNSUPPORTED_OR_CONFLICTING",
    # Phase 7, Step 3J1 -- generic split of "fee" into what is SCHEDULED/
    # LISTED (a price the program defines) vs. what an enrolled individual
    # actually PAID (an enrollment/signup-level payment record).
    # Deliberately two distinct roles, never conflated under FEE_SOURCE,
    # since a caller (e.g. core.sqlservice.business_knowledge_evidence)
    # must be able to tell "what does this cost" apart from "what was
    # actually paid" for any source, not just one customer's package.
    "FEE_DEFINITION_SOURCE", "FEE_PAYMENT_SOURCE",
})
MAPPING_KINDS = frozenset({"DIRECT", "DEPENDENT", "EQUIVALENT", "CONDITIONAL", "UNRESOLVED"})
MAPPING_STATUSES = frozenset({"PROPOSED", "VERIFIED", "UNRESOLVED", "REJECTED"})
RUN_STATUSES = frozenset({"RUNNING", "COMPLETE", "FAILED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_mapping(row) -> dict:
    return {
        "id": row["id"], "package_id": row["package_id"], "concept_id": row["concept_id"],
        "object_fqn": row["object_fqn"], "mapping_role": row["mapping_role"],
        "columns": json.loads(row["columns_json"]) if row["columns_json"] else [],
        "mapping_kind": row["mapping_kind"], "status": row["status"], "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else [],
        "schema_fingerprint": row["schema_fingerprint"], "provider_id": row["provider_id"],
        "model": row["model"], "contract_version": row["contract_version"],
        "content_hash": row["content_hash"], "rejection_reason": row["rejection_reason"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _row_to_run(row) -> dict:
    return {
        "id": row["id"], "package_id": row["package_id"], "schema_fingerprint": row["schema_fingerprint"],
        "content_hash": row["content_hash"], "status": row["status"],
        "batches_total": row["batches_total"], "batches_completed": row["batches_completed"],
        "provider_calls_made": row["provider_calls_made"],
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "started_at": row["started_at"], "completed_at": row["completed_at"],
    }


def upsert_mapping(
    package_id: int, concept_id: str, object_fqn: str, mapping_role: str,
    *, columns: list[str], mapping_kind: str, status: str, confidence: float,
    evidence: list[str], schema_fingerprint: str, content_hash: str,
    provider_id: str | None = None, model: str | None = None, contract_version: int = 1,
    rejection_reason: str | None = None,
) -> dict:
    """Idempotent insert-or-update keyed on (package_id, concept_id,
    object_fqn, mapping_role). A rerun that reproduces the identical
    proposal is a no-op write (same values, updated_at bumped) -- callers
    never need to pre-check existence."""
    if mapping_role not in MAPPING_ROLES:
        raise ValueError(f"invalid mapping_role {mapping_role!r}")
    if mapping_kind not in MAPPING_KINDS:
        raise ValueError(f"invalid mapping_kind {mapping_kind!r}")
    if status not in MAPPING_STATUSES:
        raise ValueError(f"invalid status {status!r}")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM business_knowledge_mappings WHERE package_id = ? AND concept_id = ? "
            "AND object_fqn = ? AND mapping_role = ?",
            (package_id, concept_id, object_fqn, mapping_role),
        ).fetchone()
        now = _now()
        columns_json = json.dumps(columns or [])
        evidence_json = json.dumps(evidence or [])

        if existing is None:
            cursor = conn.execute(
                "INSERT INTO business_knowledge_mappings "
                "(package_id, concept_id, object_fqn, mapping_role, columns_json, mapping_kind, status, "
                " confidence, evidence_json, schema_fingerprint, provider_id, model, contract_version, "
                " content_hash, rejection_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, concept_id, object_fqn, mapping_role, columns_json, mapping_kind, status,
                 confidence, evidence_json, schema_fingerprint, provider_id, model, contract_version,
                 content_hash, rejection_reason, now, now),
            )
            conn.commit()
            row_id = cursor.lastrowid
        else:
            conn.execute(
                "UPDATE business_knowledge_mappings SET columns_json = ?, mapping_kind = ?, status = ?, "
                "confidence = ?, evidence_json = ?, schema_fingerprint = ?, provider_id = ?, model = ?, "
                "contract_version = ?, content_hash = ?, rejection_reason = ?, updated_at = ? WHERE id = ?",
                (columns_json, mapping_kind, status, confidence, evidence_json, schema_fingerprint,
                 provider_id, model, contract_version, content_hash, rejection_reason, now, existing["id"]),
            )
            conn.commit()
            row_id = existing["id"]

        row = conn.execute("SELECT * FROM business_knowledge_mappings WHERE id = ?", (row_id,)).fetchone()
        return _row_to_mapping(row)
    finally:
        conn.close()


def list_mappings(
    package_id: int, *, status: str | None = None, object_fqn: str | None = None,
    schema_fingerprint: str | None = None,
) -> list[dict]:
    conn = get_connection()
    try:
        query = "SELECT * FROM business_knowledge_mappings WHERE package_id = ?"
        params: list = [package_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if object_fqn is not None:
            query += " AND object_fqn = ?"
            params.append(object_fqn)
        if schema_fingerprint is not None:
            query += " AND schema_fingerprint = ?"
            params.append(schema_fingerprint)
        query += " ORDER BY concept_id, object_fqn, mapping_role"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_mapping(r) for r in rows]
    finally:
        conn.close()


def get_concept_content_hashes_by_schema_fingerprint(package_id: int, schema_fingerprint: str) -> dict[str, set]:
    """concept_id -> set of per-CONCEPT content_hash values already mapped
    at this exact schema_fingerprint (across every generation, not just the
    current package-wide one) -- used by core.sqlservice.
    business_knowledge_mapper's per-concept resumability check: a concept
    whose own content and the current schema are both unchanged is skipped
    even if OTHER concepts in the same package have since changed (which
    would otherwise change the whole-package content_hash and make every
    concept look "new" again)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT concept_id, content_hash FROM business_knowledge_mappings "
            "WHERE package_id = ? AND schema_fingerprint = ?",
            (package_id, schema_fingerprint),
        ).fetchall()
        result: dict[str, set] = {}
        for r in rows:
            result.setdefault(r["concept_id"], set()).add(r["content_hash"])
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mapping runs -- resumability + zero-repeat-provider-call bookkeeping
# ---------------------------------------------------------------------------

def get_mapping_run(package_id: int, schema_fingerprint: str, content_hash: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM business_knowledge_mapping_runs WHERE package_id = ? AND schema_fingerprint = ? "
            "AND content_hash = ?",
            (package_id, schema_fingerprint, content_hash),
        ).fetchone()
        return _row_to_run(row) if row else None
    finally:
        conn.close()


def start_or_resume_mapping_run(package_id: int, schema_fingerprint: str, content_hash: str) -> dict:
    """Returns the existing run row for this exact (package_id,
    schema_fingerprint, content_hash) generation if one exists (RUNNING,
    COMPLETE, or FAILED -- the caller decides what to do with each), else
    creates a new RUNNING row. Never creates a second row for the same
    generation (unique index idx_bkmr_package_fingerprint_hash)."""
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM business_knowledge_mapping_runs WHERE package_id = ? AND schema_fingerprint = ? "
            "AND content_hash = ?",
            (package_id, schema_fingerprint, content_hash),
        ).fetchone()
        if existing is not None:
            return _row_to_run(existing)

        now = _now()
        cursor = conn.execute(
            "INSERT INTO business_knowledge_mapping_runs "
            "(package_id, schema_fingerprint, content_hash, status, started_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?)",
            (package_id, schema_fingerprint, content_hash, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM business_knowledge_mapping_runs WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
        return _row_to_run(row)
    finally:
        conn.close()


def update_mapping_run_progress(
    run_id: int, *, batches_total: int, batches_completed: int, provider_calls_made: int,
) -> dict:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE business_knowledge_mapping_runs SET batches_total = ?, batches_completed = ?, "
            "provider_calls_made = ? WHERE id = ?",
            (batches_total, batches_completed, provider_calls_made, run_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM business_knowledge_mapping_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row)
    finally:
        conn.close()


def complete_mapping_run(run_id: int) -> dict:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE business_knowledge_mapping_runs SET status = 'COMPLETE', completed_at = ? WHERE id = ?",
            (_now(), run_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM business_knowledge_mapping_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row)
    finally:
        conn.close()


def fail_mapping_run(run_id: int, error: dict) -> dict:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE business_knowledge_mapping_runs SET status = 'FAILED', error_json = ?, completed_at = ? "
            "WHERE id = ?",
            (json.dumps(error), _now(), run_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM business_knowledge_mapping_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row)
    finally:
        conn.close()
