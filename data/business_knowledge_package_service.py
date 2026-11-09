"""
Business Knowledge Package service -- Phase 7, Step 3J, Part 2.

Source-scoped, versioned storage for a database owner's business-knowledge
package: structured concepts (domains/entities/programs/lifecycle stages/
measures/statuses/payment concepts/activity concepts), relationships between
them, and authoritative free-text facts. This module is the ONLY write path
for these tables (data.models.ensure_business_knowledge_tables creates them).

Generic and reusable for any connected source -- no source-specific
constants live here. Any one customer's actual package content (its own
domains/programs/fees/lifecycle stages, etc.) lives entirely in a separate,
per-deployment seed script under scripts/ that calls this module's functions
with its own source_id/user_id passed in explicitly -- never hardcoded here.

Authoritative-vs-AI trust boundary (mirrors data.entity_service's
'human' assignment_source lock, e.g. lock_entity_assignment's own docstring):
  - business_knowledge_facts rows are ALWAYS origin='AUTHORITATIVE' -- the
    only writer is store_authoritative_fact(), which the AI mapping path
    (core.sqlservice.business_knowledge_mapper) never calls.
  - business_knowledge_concepts/relationships rows carry an explicit
    `origin` column. upsert_concept()/upsert_relationship() REFUSE to
    overwrite an existing AUTHORITATIVE row with an AI_INFERRED write --
    the existing row is returned unchanged with skipped_authoritative_
    protection=True, never silently replaced. An AUTHORITATIVE write may
    always overwrite a prior AI_INFERRED row (a human correcting the AI).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)

CONCEPT_TYPES = frozenset({
    "DOMAIN", "ENTITY", "PROGRAM", "LIFECYCLE_STAGE", "MEASURE",
    "STATUS", "PAYMENT_CONCEPT", "ACTIVITY_CONCEPT",
})
ORIGINS = frozenset({"AUTHORITATIVE", "AI_INFERRED"})
PACKAGE_STATUSES = frozenset({"DRAFT", "ACTIVE", "SUPERSEDED"})


class BusinessKnowledgePackageError(ValueError):
    """Raised for an invalid argument (bad concept_type/origin/status, etc.)
    -- never for "not found"/"not owned", which return None instead."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    return conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone() is not None


def _row_to_package(row) -> dict:
    return {
        "id": row["id"], "source_id": row["source_id"], "user_id": row["user_id"],
        "package_version": row["package_version"], "status": row["status"],
        "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _row_to_concept(row) -> dict:
    return {
        "id": row["id"], "package_id": row["package_id"], "concept_id": row["concept_id"],
        "concept_type": row["concept_type"], "display_name": row["display_name"],
        "description": row["description"],
        "aliases": json.loads(row["aliases_json"]) if row["aliases_json"] else [],
        "origin": row["origin"], "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else [],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _row_to_relationship(row) -> dict:
    return {
        "id": row["id"], "package_id": row["package_id"],
        "from_concept_id": row["from_concept_id"], "relationship_type": row["relationship_type"],
        "to_concept_id": row["to_concept_id"], "origin": row["origin"],
        "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else [],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _row_to_fact(row) -> dict:
    return {
        "id": row["id"], "package_id": row["package_id"], "fact_key": row["fact_key"],
        "fact_text": row["fact_text"], "related_concept_id": row["related_concept_id"],
        "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Package lifecycle
# ---------------------------------------------------------------------------

def create_or_get_draft_package(source_id: int, user_id: str, created_by: str) -> dict | None:
    """Returns the source's current DRAFT package, creating package_version=1
    if none exists yet. If an ACTIVE package already exists and no DRAFT
    does, creates the next version (active_version + 1) as a new DRAFT --
    editing never mutates an already-ACTIVE package in place. Returns None
    if source_id is not owned by user_id."""
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None

        draft = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE source_id = ? AND status = 'DRAFT' "
            "ORDER BY package_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if draft is not None:
            return _row_to_package(draft)

        latest = conn.execute(
            "SELECT MAX(package_version) AS v FROM business_knowledge_packages WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        next_version = (latest["v"] or 0) + 1

        now = _now()
        cursor = conn.execute(
            "INSERT INTO business_knowledge_packages "
            "(source_id, user_id, package_version, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'DRAFT', ?, ?, ?)",
            (source_id, user_id, next_version, created_by, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
        return _row_to_package(row)
    finally:
        conn.close()


def activate_package(source_id: int, user_id: str, package_version: int) -> dict | None:
    """Marks package_version ACTIVE and supersedes any previously-ACTIVE
    package for this source (there is at most one ACTIVE package per source
    at any time -- the one core.sqlservice.business_knowledge_evidence reads
    at question time). Returns None if source_id is not owned by user_id or
    package_version does not exist for it."""
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
        target = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE source_id = ? AND package_version = ?",
            (source_id, package_version),
        ).fetchone()
        if target is None:
            return None

        now = _now()
        conn.execute(
            "UPDATE business_knowledge_packages SET status = 'SUPERSEDED', updated_at = ? "
            "WHERE source_id = ? AND status = 'ACTIVE' AND package_version != ?",
            (now, source_id, package_version),
        )
        conn.execute(
            "UPDATE business_knowledge_packages SET status = 'ACTIVE', updated_at = ? WHERE id = ?",
            (now, target["id"]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE id = ?", (target["id"],),
        ).fetchone()
        return _row_to_package(row)
    finally:
        conn.close()


def get_package(source_id: int, user_id: str, package_version: int | None = None) -> dict | None:
    """package_version=None returns the ACTIVE package if one exists, else
    the latest DRAFT. Returns None if source_id is not owned by user_id or
    no package exists yet at all."""
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
        if package_version is not None:
            row = conn.execute(
                "SELECT * FROM business_knowledge_packages WHERE source_id = ? AND package_version = ?",
                (source_id, package_version),
            ).fetchone()
            return _row_to_package(row) if row else None

        row = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE source_id = ? AND status = 'ACTIVE'",
            (source_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM business_knowledge_packages WHERE source_id = ? "
                "ORDER BY package_version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return _row_to_package(row) if row else None
    finally:
        conn.close()


def get_active_package(source_id: int, user_id: str) -> dict | None:
    """Convenience wrapper used by the read-only/question-time path -- never
    returns a DRAFT package, only ever one explicitly activate_package()'d."""
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM business_knowledge_packages WHERE source_id = ? AND status = 'ACTIVE'",
            (source_id,),
        ).fetchone()
        return _row_to_package(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------

def upsert_concept(
    package_id: int, concept_id: str, concept_type: str, display_name: str,
    *, origin: str, description: str | None = None, aliases: list[str] | None = None,
    confidence: float = 1.0, evidence: list[str] | None = None,
) -> dict:
    """Idempotent insert-or-update keyed on (package_id, concept_id).

    Authoritative-write guard: if a row already exists with
    origin='AUTHORITATIVE' and this call's origin='AI_INFERRED', the
    existing row is returned UNCHANGED (with skipped_authoritative_
    protection=True) -- an AI mapping/enrichment pass can never silently
    rewrite an owner-confirmed concept. An origin='AUTHORITATIVE' call
    always applies (a human correction always wins), including over a
    prior AI_INFERRED row.
    """
    if concept_type not in CONCEPT_TYPES:
        raise BusinessKnowledgePackageError(f"invalid concept_type {concept_type!r}; must be one of {sorted(CONCEPT_TYPES)}")
    if origin not in ORIGINS:
        raise BusinessKnowledgePackageError(f"invalid origin {origin!r}; must be one of {sorted(ORIGINS)}")
    if not concept_id or not concept_id.strip():
        raise BusinessKnowledgePackageError("concept_id must be non-empty")
    if not display_name or not display_name.strip():
        raise BusinessKnowledgePackageError("display_name must be non-empty")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM business_knowledge_concepts WHERE package_id = ? AND concept_id = ?",
            (package_id, concept_id),
        ).fetchone()

        if existing is not None and existing["origin"] == "AUTHORITATIVE" and origin == "AI_INFERRED":
            result = _row_to_concept(existing)
            result["skipped_authoritative_protection"] = True
            return result

        now = _now()
        aliases_json = json.dumps(aliases or [])
        evidence_json = json.dumps(evidence or [])

        if existing is None:
            cursor = conn.execute(
                "INSERT INTO business_knowledge_concepts "
                "(package_id, concept_id, concept_type, display_name, description, "
                " aliases_json, origin, confidence, evidence_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, concept_id, concept_type, display_name, description,
                 aliases_json, origin, confidence, evidence_json, now, now),
            )
            conn.commit()
            row_id = cursor.lastrowid
        else:
            conn.execute(
                "UPDATE business_knowledge_concepts SET "
                "concept_type = ?, display_name = ?, description = ?, aliases_json = ?, "
                "origin = ?, confidence = ?, evidence_json = ?, updated_at = ? "
                "WHERE id = ?",
                (concept_type, display_name, description, aliases_json,
                 origin, confidence, evidence_json, now, existing["id"]),
            )
            conn.commit()
            row_id = existing["id"]

        row = conn.execute("SELECT * FROM business_knowledge_concepts WHERE id = ?", (row_id,)).fetchone()
        result = _row_to_concept(row)
        result["skipped_authoritative_protection"] = False
        return result
    finally:
        conn.close()


def list_concepts(package_id: int, *, concept_type: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if concept_type is not None:
            rows = conn.execute(
                "SELECT * FROM business_knowledge_concepts WHERE package_id = ? AND concept_type = ? "
                "ORDER BY concept_id",
                (package_id, concept_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM business_knowledge_concepts WHERE package_id = ? ORDER BY concept_id",
                (package_id,),
            ).fetchall()
        return [_row_to_concept(r) for r in rows]
    finally:
        conn.close()


def get_concept(package_id: int, concept_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM business_knowledge_concepts WHERE package_id = ? AND concept_id = ?",
            (package_id, concept_id),
        ).fetchone()
        return _row_to_concept(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def upsert_relationship(
    package_id: int, from_concept_id: str, relationship_type: str, to_concept_id: str,
    *, origin: str, confidence: float = 1.0, evidence: list[str] | None = None,
) -> dict:
    """Idempotent insert-or-update keyed on (package_id, from_concept_id,
    relationship_type, to_concept_id). Same authoritative-write guard as
    upsert_concept -- an AI_INFERRED call never overwrites an existing
    AUTHORITATIVE edge."""
    if origin not in ORIGINS:
        raise BusinessKnowledgePackageError(f"invalid origin {origin!r}; must be one of {sorted(ORIGINS)}")
    for name, value in (("from_concept_id", from_concept_id), ("relationship_type", relationship_type),
                         ("to_concept_id", to_concept_id)):
        if not value or not value.strip():
            raise BusinessKnowledgePackageError(f"{name} must be non-empty")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM business_knowledge_relationships WHERE package_id = ? AND from_concept_id = ? "
            "AND relationship_type = ? AND to_concept_id = ?",
            (package_id, from_concept_id, relationship_type, to_concept_id),
        ).fetchone()

        if existing is not None and existing["origin"] == "AUTHORITATIVE" and origin == "AI_INFERRED":
            result = _row_to_relationship(existing)
            result["skipped_authoritative_protection"] = True
            return result

        now = _now()
        evidence_json = json.dumps(evidence or [])

        if existing is None:
            cursor = conn.execute(
                "INSERT INTO business_knowledge_relationships "
                "(package_id, from_concept_id, relationship_type, to_concept_id, origin, confidence, "
                " evidence_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, from_concept_id, relationship_type, to_concept_id, origin, confidence,
                 evidence_json, now, now),
            )
            conn.commit()
            row_id = cursor.lastrowid
        else:
            conn.execute(
                "UPDATE business_knowledge_relationships SET origin = ?, confidence = ?, "
                "evidence_json = ?, updated_at = ? WHERE id = ?",
                (origin, confidence, evidence_json, now, existing["id"]),
            )
            conn.commit()
            row_id = existing["id"]

        row = conn.execute("SELECT * FROM business_knowledge_relationships WHERE id = ?", (row_id,)).fetchone()
        result = _row_to_relationship(row)
        result["skipped_authoritative_protection"] = False
        return result
    finally:
        conn.close()


def list_relationships(package_id: int, *, from_concept_id: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if from_concept_id is not None:
            rows = conn.execute(
                "SELECT * FROM business_knowledge_relationships WHERE package_id = ? AND from_concept_id = ? "
                "ORDER BY relationship_type, to_concept_id",
                (package_id, from_concept_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM business_knowledge_relationships WHERE package_id = ? "
                "ORDER BY from_concept_id, relationship_type, to_concept_id",
                (package_id,),
            ).fetchall()
        return [_row_to_relationship(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authoritative facts -- the ONLY writer is this function. The AI mapping
# path (core.sqlservice.business_knowledge_mapper) never imports it.
# ---------------------------------------------------------------------------

def store_authoritative_fact(
    package_id: int, fact_key: str, fact_text: str,
    *, related_concept_id: str | None = None, created_by: str,
) -> dict:
    """Idempotent insert-or-update keyed on (package_id, fact_key). Always
    origin-equivalent to AUTHORITATIVE by construction -- there is no
    `origin` parameter here at all, and no other function in this module or
    anywhere in core.sqlservice.business_knowledge_mapper writes to
    business_knowledge_facts."""
    if not fact_key or not fact_key.strip():
        raise BusinessKnowledgePackageError("fact_key must be non-empty")
    if not fact_text or not fact_text.strip():
        raise BusinessKnowledgePackageError("fact_text must be non-empty")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM business_knowledge_facts WHERE package_id = ? AND fact_key = ?",
            (package_id, fact_key),
        ).fetchone()
        now = _now()

        if existing is None:
            cursor = conn.execute(
                "INSERT INTO business_knowledge_facts "
                "(package_id, fact_key, fact_text, related_concept_id, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (package_id, fact_key, fact_text, related_concept_id, created_by, now, now),
            )
            conn.commit()
            row_id = cursor.lastrowid
        else:
            conn.execute(
                "UPDATE business_knowledge_facts SET fact_text = ?, related_concept_id = ?, updated_at = ? "
                "WHERE id = ?",
                (fact_text, related_concept_id, now, existing["id"]),
            )
            conn.commit()
            row_id = existing["id"]

        row = conn.execute("SELECT * FROM business_knowledge_facts WHERE id = ?", (row_id,)).fetchone()
        return _row_to_fact(row)
    finally:
        conn.close()


def list_facts(package_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM business_knowledge_facts WHERE package_id = ? ORDER BY fact_key",
            (package_id,),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]
    finally:
        conn.close()
