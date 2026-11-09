"""
Transactional SQLite persistence for validated semantic enrichment — SQL
Architecture Correction, Phase 6, Step 4B.

Adapter between an already-validated `core.sqlservice.semantic_enrichment_
service.EnrichedObjectSemantics` (+ its `EnrichedColumnSemantics`) and
ToolSmithAI's local SQLite metadata store: `sql_semantic_enrichments` (see
data/models.py and migrations/015_create_semantic_enrichment.sql, which
define identical schemas).

Trust enforcement — mirrors core.sqlservice.sql_example_store's own
"never a general-purpose insert a caller could misuse" discipline exactly:
`insert_semantic_enrichment()` requires a REAL `EnrichedObjectSemantics`
(and `EnrichedColumnSemantics` for every column) — those types reject
direct construction from outside core.sqlservice.semantic_enrichment_
service (see that module's `_EnrichmentConstructionGuard`), so the
`isinstance()` checks below are a real, not merely conventional, proof that
every persisted field already cleared that module's full structural/enum/
bounds/contradiction validation gate. There is no keyword-argument form of
this function a caller could use to persist a hand-built or raw-provider-
JSON payload instead.

Scope: reads/writes ToolSmithAI's own local SQLite store only. Never
connects to a live SQL Server, never calls a semantic-enrichment provider,
never computes a semantic-input hash itself (that is core.sqlservice.
semantic_enrichment_service.compute_object_semantic_input_hash's job,
called by the caller BEFORE this module is ever invoked).

Cache identity / historical retention: a row's identity is (source_id,
object_fqn, semantic_input_hash, provider_id, model, contract_version) — a
UNIQUE index (see the migration). A schema/dictionary change that produces
a NEW semantic_input_hash for the same object simply adds a NEW row; this
module never updates or deletes a prior hash's row, so historical versions
are retained. "Current" is entirely a query-time concept: every load
function here requires the CALLER to supply the current per-object hash(es)
it considers authoritative (typically freshly computed from the source's
latest READY retrieval build) and only ever returns rows matching those
exact hashes — a stale hash is excluded by construction, never by deleting
old rows.

status ACTIVE|INVALID: `mark_enrichment_invalid_by_id()` flips a row to
INVALID without touching payload_json — every load function here excludes
INVALID rows from its results ("unusable without exposing content"), and
`insert_semantic_enrichment()` transparently REPAIRS (in place, at the same
row id) an INVALID or corrupt-payload row it finds occupying the exact
cache key it was asked to insert at, rather than leaving it permanently
stuck (see OUTCOME_REPAIRED) — this is what makes a preparation run safely
resumable after an interruption that left a partially-written or corrupted
row behind.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from core.sqlservice.semantic_catalog import EvidenceItem
from core.sqlservice.semantic_enrichment_phases import PHASE_COLUMN_DEFERRED, PHASE_VIEW_ENRICHED, VALID_PHASES
from core.sqlservice.semantic_enrichment_service import (
    EnrichedColumnSemantics,
    EnrichedObjectSemantics,
    EnrichedViewObjectSemantics,
    ViewStatusConceptRef,
)
from data.db import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_ACTIVE",
    "STATUS_INVALID",
    "OUTCOME_CREATED",
    "OUTCOME_REUSED",
    "OUTCOME_REPAIRED",
    "OUTCOME_UNOWNED",
    "COMPLETENESS_STATUS_OK",
    "COMPLETENESS_STATUS_UNOWNED",
    "StoredEnrichedColumn",
    "StoredSemanticEnrichment",
    "InsertEnrichmentResult",
    "EnrichmentCompletenessResult",
    "insert_semantic_enrichment",
    "get_enrichment_by_cache_key",
    "load_current_enrichments_for_source",
    "mark_enrichment_invalid_by_id",
    "count_enrichment_completeness",
    # Phase 6 Step 6C1 -- deep VIEW_ENRICHED persistence (dedicated table:
    # sql_view_semantic_enrichments, never sql_semantic_enrichments)
    "StoredViewSemanticEnrichment",
    "InsertViewEnrichmentResult",
    "insert_view_semantic_enrichment",
    "get_view_enrichment_by_cache_key",
    "load_current_view_enrichments_for_source",
    "mark_view_enrichment_invalid_by_id",
    "count_view_enrichment_completeness",
]

STATUS_ACTIVE = "ACTIVE"
STATUS_INVALID = "INVALID"

OUTCOME_CREATED = "CREATED"
OUTCOME_REUSED = "REUSED"
OUTCOME_REPAIRED = "REPAIRED"
OUTCOME_UNOWNED = "UNOWNED"

COMPLETENESS_STATUS_OK = "OK"
COMPLETENESS_STATUS_UNOWNED = "UNOWNED"

_HEX64_LEN = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == _HEX64_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


def _verify_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


# ── Public data model (plain, read-back shapes — never gated; the GATE is
#    on the write-path input types EnrichedObjectSemantics/
#    EnrichedColumnSemantics, not on what a load function returns) ────────


@dataclass(frozen=True)
class StoredEnrichedColumn:
    """One persisted column's semantics, read back from payload_json."""

    column_name: str
    business_label: str
    semantic_role: str
    date_role: str | None
    measure_role: str | None
    dimension_role: str | None
    identifier_role: str | None
    confidence: float
    provenance: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class StoredSemanticEnrichment:
    """One persisted object's semantics, read back from
    sql_semantic_enrichments. Immutable reconstruction: every list becomes a
    tuple, every nested mapping becomes a dataclass — never a raw dict a
    caller could mutate."""

    id: int
    source_id: int
    object_fqn: str
    semantic_input_hash: str
    provider_id: str
    model: str
    contract_version: int
    phase: str
    business_name: str
    description: str
    object_role: str
    entity_concepts: tuple[str, ...]
    aliases: tuple[str, ...]
    supported_operations: tuple[str, ...]
    confidence: float
    provenance: tuple[EvidenceItem, ...]
    columns: tuple[StoredEnrichedColumn, ...]
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class InsertEnrichmentResult:
    """Result of one insert_semantic_enrichment() call."""

    outcome: str  # OUTCOME_CREATED | OUTCOME_REUSED | OUTCOME_REPAIRED | OUTCOME_UNOWNED
    enrichment: StoredSemanticEnrichment | None


@dataclass(frozen=True)
class EnrichmentCompletenessResult:
    """Result of one count_enrichment_completeness() call."""

    status: str  # COMPLETENESS_STATUS_OK | COMPLETENESS_STATUS_UNOWNED
    requested_count: int
    cached_current_count: int
    missing_object_fqns: tuple[str, ...]


# ── Payload JSON serialization (canonical, strict decode) ─────────────────
#
# Never a prompt, a question, a row value, a credential, the provider's raw
# response, or exception text — exactly (and only) the fields
# EnrichedObjectSemantics/EnrichedColumnSemantics themselves carry.

_REQUIRED_PAYLOAD_OBJECT_KEYS = frozenset({
    "business_name", "description", "object_role", "entity_concepts",
    "aliases", "supported_operations", "confidence", "provenance",
})
_REQUIRED_PAYLOAD_COLUMN_KEYS = frozenset({
    "column_name", "business_label", "semantic_role", "date_role",
    "measure_role", "dimension_role", "identifier_role", "confidence", "provenance",
})


class CorruptEnrichmentPayloadError(Exception):
    """payload_json failed strict decoding — malformed JSON, wrong shape, or
    a missing/invalid required key. Never carries the raw payload text or
    the underlying exception message (only the exception's TYPE NAME) —
    "no ... exception text ... in logs" applies to this module's own
    payload decode path too."""


def _provenance_to_json(items: Sequence[EvidenceItem]) -> list[dict]:
    return [{"kind": item.kind, "detail": item.detail} for item in items]


def _provenance_from_json(value: object) -> tuple[EvidenceItem, ...]:
    if not isinstance(value, list):
        raise CorruptEnrichmentPayloadError("provenance must be a list")
    items = []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str) or not isinstance(entry.get("detail"), str):
            raise CorruptEnrichmentPayloadError("provenance entry malformed")
        items.append(EvidenceItem(kind=entry["kind"], detail=entry["detail"]))
    return tuple(items)


def _serialize_payload(
    object_semantics: EnrichedObjectSemantics, column_semantics: Sequence[EnrichedColumnSemantics],
) -> str:
    payload = {
        "object": {
            "business_name": object_semantics.business_name,
            "description": object_semantics.description,
            "object_role": object_semantics.object_role,
            "entity_concepts": list(object_semantics.entity_concepts),
            "aliases": list(object_semantics.aliases),
            "supported_operations": list(object_semantics.supported_operations),
            "confidence": object_semantics.confidence,
            "provenance": _provenance_to_json(object_semantics.provenance),
        },
        "columns": [
            {
                "column_name": c.column_name,
                "business_label": c.business_label,
                "semantic_role": c.semantic_role,
                "date_role": c.date_role,
                "measure_role": c.measure_role,
                "dimension_role": c.dimension_role,
                "identifier_role": c.identifier_role,
                "confidence": c.confidence,
                "provenance": _provenance_to_json(c.provenance),
            }
            for c in column_semantics
        ],
    }
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"enrichment payload is not JSON-safe: {type(exc).__name__}") from exc


def _deserialize_payload(payload_json: str) -> tuple[dict, tuple[StoredEnrichedColumn, ...]]:
    """Strict decode -> (object_fields_dict, columns_tuple). Raises
    CorruptEnrichmentPayloadError (never the raw text/underlying message)
    for anything that doesn't match the exact expected shape."""
    try:
        parsed = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CorruptEnrichmentPayloadError(type(exc).__name__) from exc

    if not isinstance(parsed, dict):
        raise CorruptEnrichmentPayloadError("payload root is not an object")
    obj = parsed.get("object")
    cols = parsed.get("columns")
    if not isinstance(obj, dict) or not isinstance(cols, list):
        raise CorruptEnrichmentPayloadError("payload missing 'object'/'columns'")

    missing_object_keys = _REQUIRED_PAYLOAD_OBJECT_KEYS - set(obj.keys())
    if missing_object_keys:
        raise CorruptEnrichmentPayloadError(f"object payload missing key(s): {sorted(missing_object_keys)}")
    if not isinstance(obj["confidence"], (int, float)) or isinstance(obj["confidence"], bool):
        raise CorruptEnrichmentPayloadError("object confidence is not a number")

    object_fields = {
        "business_name": obj["business_name"],
        "description": obj["description"],
        "object_role": obj["object_role"],
        "entity_concepts": tuple(obj["entity_concepts"]),
        "aliases": tuple(obj["aliases"]),
        "supported_operations": tuple(obj["supported_operations"]),
        "confidence": float(obj["confidence"]),
        "provenance": _provenance_from_json(obj["provenance"]),
    }

    columns: list[StoredEnrichedColumn] = []
    for col in cols:
        if not isinstance(col, dict):
            raise CorruptEnrichmentPayloadError("column payload entry is not an object")
        missing_column_keys = _REQUIRED_PAYLOAD_COLUMN_KEYS - set(col.keys())
        if missing_column_keys:
            raise CorruptEnrichmentPayloadError(f"column payload missing key(s): {sorted(missing_column_keys)}")
        if not isinstance(col["confidence"], (int, float)) or isinstance(col["confidence"], bool):
            raise CorruptEnrichmentPayloadError("column confidence is not a number")
        columns.append(
            StoredEnrichedColumn(
                column_name=col["column_name"],
                business_label=col["business_label"],
                semantic_role=col["semantic_role"],
                date_role=col["date_role"],
                measure_role=col["measure_role"],
                dimension_role=col["dimension_role"],
                identifier_role=col["identifier_role"],
                confidence=float(col["confidence"]),
                provenance=_provenance_from_json(col["provenance"]),
            )
        )

    return object_fields, tuple(columns)


def _row_to_stored(row) -> StoredSemanticEnrichment | None:
    """None (never a raised exception) for a row whose payload_json fails
    strict decoding — a corrupt row is simply excluded from results, exactly
    like an INVALID-status row (see this module's own docstring). Logged
    without the raw payload text."""
    try:
        object_fields, columns = _deserialize_payload(row["payload_json"])
    except CorruptEnrichmentPayloadError as exc:
        logger.warning(
            "sql_semantic_enrichments row id=%s failed payload decode (%s) -- excluded from results",
            row["id"], type(exc).__name__,
        )
        return None

    return StoredSemanticEnrichment(
        id=row["id"],
        source_id=row["source_id"],
        object_fqn=row["object_fqn"],
        semantic_input_hash=row["semantic_input_hash"],
        provider_id=row["provider_id"],
        model=row["model"],
        contract_version=row["contract_version"],
        phase=row["phase"],
        columns=columns,
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        **object_fields,
    )


# ── Validation (pure, no I/O — raises ValueError, never writes) ──────────


def _validate_insert_args(
    *, source_id: int, object_fqn: str, semantic_input_hash: str, provider_id: str, model: str,
    contract_version: int, phase: str,
) -> None:
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    if not isinstance(object_fqn, str) or not object_fqn.strip():
        raise ValueError("object_fqn must be a non-empty string")
    if not _is_hex64(semantic_input_hash):
        raise ValueError(f"semantic_input_hash is malformed: {semantic_input_hash!r}")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(contract_version, int) or isinstance(contract_version, bool) or contract_version <= 0:
        raise ValueError("contract_version must be a positive integer")
    if phase not in VALID_PHASES:
        raise ValueError(f"phase must be one of {sorted(VALID_PHASES)}, got {phase!r}")


# ── Public entry points ────────────────────────────────────────────────────


def insert_semantic_enrichment(
    user_id: str,
    *,
    source_id: int,
    object_fqn: str,
    semantic_input_hash: str,
    provider_id: str,
    model: str,
    contract_version: int,
    object_semantics: EnrichedObjectSemantics,
    column_semantics: Sequence[EnrichedColumnSemantics] = (),
    phase: str = PHASE_COLUMN_DEFERRED,
) -> InsertEnrichmentResult:
    """
    Persist one already-validated object's semantics, idempotently, at the
    cache key (source_id, object_fqn, semantic_input_hash, provider_id,
    model, contract_version, phase).

    `phase` (Phase 6 Step 4C2) defaults to PHASE_COLUMN_DEFERRED so every
    EXISTING call site (the Step 4A/4B/4C1 combined object+selected-columns
    contract) keeps its exact prior cache identity and behavior unchanged.
    An OBJECT_CORE caller passes phase=PHASE_OBJECT_CORE explicitly — the
    two phases' rows for the SAME object/hash/provider/model/contract can
    never collide (see the migration's own phase-inclusive UNIQUE index).

    Raises ValueError for:
      - any malformed identity argument (see _validate_insert_args) —
        before any database access;
      - `object_semantics` not a real EnrichedObjectSemantics, or any
        element of `column_semantics` not a real EnrichedColumnSemantics
        (see core.sqlservice.semantic_enrichment_service's construction
        gate) — "never allow callers to insert arbitrary raw provider
        JSON: accept only validated enrichment result types";
      - `object_semantics.object_fqn` (or any column's `object_fqn`) not
        matching `object_fqn` (case-insensitively) — a mismatched-
        association defense, never silently reassociated.

    Ownership is checked against the database and reported as
    outcome=UNOWNED (not raised) — a routine, expected condition.

    Idempotent: an existing ACTIVE row at this exact key with a payload
    that still decodes cleanly is returned UNCHANGED as outcome=REUSED
    (never overwritten, even if the newly supplied content differs — the
    FIRST successfully validated enrichment for a given input/provider/
    model/contract is authoritative). An existing row at this exact key
    that is INVALID or whose payload fails to decode is instead REPAIRED
    in place (same row id, status reset to ACTIVE, payload replaced) and
    reported as outcome=REPAIRED — this is what makes a preparation run
    safely resumable after an interruption that left a corrupt or
    administratively-invalidated row behind.
    """
    _validate_insert_args(
        source_id=source_id, object_fqn=object_fqn, semantic_input_hash=semantic_input_hash,
        provider_id=provider_id, model=model, contract_version=contract_version, phase=phase,
    )
    if not isinstance(object_semantics, EnrichedObjectSemantics):
        raise ValueError(
            "object_semantics must be a real core.sqlservice.semantic_enrichment_service."
            "EnrichedObjectSemantics produced by that module's own strict response validation "
            "-- a raw dict/kwargs payload is never accepted here"
        )
    if object_semantics.object_fqn.lower() != object_fqn.lower():
        raise ValueError("object_semantics.object_fqn does not match object_fqn")
    for i, col in enumerate(column_semantics):
        if not isinstance(col, EnrichedColumnSemantics):
            raise ValueError(
                f"column_semantics[{i}] must be a real core.sqlservice.semantic_enrichment_service."
                "EnrichedColumnSemantics produced by that module's own strict response validation"
            )
        if col.object_fqn.lower() != object_fqn.lower():
            raise ValueError(f"column_semantics[{i}].object_fqn does not match object_fqn")

    payload_json = _serialize_payload(object_semantics, column_semantics)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return InsertEnrichmentResult(outcome=OUTCOME_UNOWNED, enrichment=None)

        existing_row = conn.execute(
            "SELECT * FROM sql_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
            "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ? AND phase = ?",
            (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase),
        ).fetchone()

        if existing_row is not None:
            existing_is_healthy = existing_row["status"] == STATUS_ACTIVE and _row_to_stored(existing_row) is not None
            if existing_is_healthy:
                return InsertEnrichmentResult(outcome=OUTCOME_REUSED, enrichment=_row_to_stored(existing_row))

            now = _now()
            try:
                conn.execute(
                    "UPDATE sql_semantic_enrichments SET payload_json = ?, status = ?, updated_at = ? WHERE id = ?",
                    (payload_json, STATUS_ACTIVE, now, existing_row["id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            repaired_row = conn.execute(
                "SELECT * FROM sql_semantic_enrichments WHERE id = ?", (existing_row["id"],),
            ).fetchone()
            return InsertEnrichmentResult(outcome=OUTCOME_REPAIRED, enrichment=_row_to_stored(repaired_row))

        now = _now()
        try:
            cursor = conn.execute(
                "INSERT INTO sql_semantic_enrichments "
                "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
                " payload_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase,
                    payload_json, STATUS_ACTIVE, now, now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race to a concurrent, logically-identical insert (same
            # unique cache key) -- SQLite serializes writers, so the
            # winner's row must now be visible; hand it back as REUSED
            # rather than raising, mirroring core.sqlservice.retrieval_
            # store.persist_retrieval_build's own race handling.
            conn.rollback()
            winner = conn.execute(
                "SELECT * FROM sql_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
                "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ? AND phase = ?",
                (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase),
            ).fetchone()
            if winner is None:
                raise
            return InsertEnrichmentResult(outcome=OUTCOME_REUSED, enrichment=_row_to_stored(winner))
        except Exception:
            conn.rollback()
            raise

        row = conn.execute("SELECT * FROM sql_semantic_enrichments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return InsertEnrichmentResult(outcome=OUTCOME_CREATED, enrichment=_row_to_stored(row))
    finally:
        conn.close()


def get_enrichment_by_cache_key(
    source_id: int, user_id: str, *, object_fqn: str, semantic_input_hash: str,
    provider_id: str, model: str, contract_version: int, phase: str = PHASE_COLUMN_DEFERRED,
) -> StoredSemanticEnrichment | None:
    """One ACTIVE, decodable row at the exact cache key, for an owned
    source. None for an unowned source, no matching row, an INVALID row, or
    a row whose payload fails to decode -- "stale/untrusted/corrupt records
    must be excluded before catalog merging" applies to this single-key
    lookup exactly like the bulk loader below. `phase` defaults to
    PHASE_COLUMN_DEFERRED -- see insert_semantic_enrichment's own docstring."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM sql_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
            "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ? "
            "AND phase = ? AND status = ?",
            (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, STATUS_ACTIVE),
        ).fetchone()
        return _row_to_stored(row) if row is not None else None
    finally:
        conn.close()


def load_current_enrichments_for_source(
    source_id: int, user_id: str, *, provider_id: str, model: str, contract_version: int,
    current_hashes_by_object_fqn: Mapping[str, str], phase: str = PHASE_COLUMN_DEFERRED,
) -> tuple[StoredSemanticEnrichment, ...]:
    """
    Every ACTIVE, decodable row for this owned source under this exact
    (provider_id, model, contract_version, phase) whose OWN
    semantic_input_hash matches the CALLER-SUPPLIED
    `current_hashes_by_object_fqn` (object_fqn matched case-insensitively)
    -- a row whose hash does NOT match (a stale version of an object that
    has since changed) is excluded, never returned as current. Returns ()
    for an unowned source. Deterministically ordered by object_fqn.

    `phase` (Phase 6 Step 4C2) defaults to PHASE_COLUMN_DEFERRED -- see
    insert_semantic_enrichment's own docstring. An OBJECT_CORE row is never
    returned by a COLUMN_DEFERRED-phase call and vice versa.
    """
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ()
        rows = conn.execute(
            "SELECT * FROM sql_semantic_enrichments WHERE source_id = ? AND status = ? "
            "AND provider_id = ? AND model = ? AND contract_version = ? AND phase = ?",
            (source_id, STATUS_ACTIVE, provider_id, model, contract_version, phase),
        ).fetchall()

        current_lower = {fqn.lower(): h for fqn, h in current_hashes_by_object_fqn.items()}
        results = []
        for row in rows:
            expected_hash = current_lower.get(row["object_fqn"].lower())
            if expected_hash is None or expected_hash != row["semantic_input_hash"]:
                continue  # not requested, or a stale hash -- never returned as current
            stored = _row_to_stored(row)
            if stored is not None:
                results.append(stored)

        results.sort(key=lambda s: s.object_fqn.lower())
        return tuple(results)
    finally:
        conn.close()


def mark_enrichment_invalid_by_id(enrichment_id: int, source_id: int, user_id: str) -> bool:
    """
    Flip one row's status to INVALID without touching payload_json --
    "mark invalid/corrupt records unusable without exposing their content".
    Every load function above excludes INVALID rows from its results, so
    the row's content is retained (for audit) but never again surfaced.
    Returns True on a successful update, False for an unowned source or an
    enrichment_id that doesn't exist / belongs to a different source (never
    raises for that).
    """
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return False
        cursor = conn.execute(
            "UPDATE sql_semantic_enrichments SET status = ?, updated_at = ? WHERE id = ? AND source_id = ?",
            (STATUS_INVALID, _now(), enrichment_id, source_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def count_enrichment_completeness(
    source_id: int, user_id: str, *, provider_id: str, model: str, contract_version: int,
    current_hashes_by_object_fqn: Mapping[str, str], phase: str = PHASE_COLUMN_DEFERRED,
) -> EnrichmentCompletenessResult:
    """
    How many of `current_hashes_by_object_fqn`'s objects already have a
    CURRENT (matching hash), ACTIVE, decodable enrichment row FOR THIS EXACT
    PHASE -- and which object_fqns are missing (never enriched, stale, or
    excluded as invalid/corrupt). COMPLETENESS_STATUS_UNOWNED (never raised)
    for an unowned source, with requested_count still populated so a caller
    can tell the difference from a genuinely empty request.

    `phase` (Phase 6 Step 4C2) defaults to PHASE_COLUMN_DEFERRED -- see
    insert_semantic_enrichment's own docstring. Feeds
    core.sqlservice.semantic_enrichment_phases.readiness_status(phase, ...)
    directly: an OBJECT_CORE caller passes phase=PHASE_OBJECT_CORE and gets
    OBJECT_CORE's own completeness, entirely independent of COLUMN_DEFERRED
    completeness for the same objects.
    """
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return EnrichmentCompletenessResult(
                status=COMPLETENESS_STATUS_UNOWNED, requested_count=len(current_hashes_by_object_fqn),
                cached_current_count=0, missing_object_fqns=tuple(sorted(current_hashes_by_object_fqn)),
            )
    finally:
        conn.close()

    current = load_current_enrichments_for_source(
        source_id, user_id, provider_id=provider_id, model=model, contract_version=contract_version,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn, phase=phase,
    )
    found_lower = {e.object_fqn.lower() for e in current}
    missing = tuple(sorted(fqn for fqn in current_hashes_by_object_fqn if fqn.lower() not in found_lower))

    return EnrichmentCompletenessResult(
        status=COMPLETENESS_STATUS_OK, requested_count=len(current_hashes_by_object_fqn),
        cached_current_count=len(current), missing_object_fqns=missing,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6, Step 6C1 — deep VIEW_ENRICHED persistence (dedicated table)
# ═══════════════════════════════════════════════════════════════════════════
#
# Persists into `sql_view_semantic_enrichments` — a SEPARATE, dedicated
# table (see migrations/017_create_view_semantic_enrichments.sql and
# data/models.py's ensure_sql_view_semantic_enrichments_table), NOT the
# `sql_semantic_enrichments` table every function above targets.
#
# Step 6C's original design instead widened sql_semantic_enrichments'
# `phase` CHECK constraint (via a create-copy-drop-rename table rebuild) to
# add a third allowed value, 'VIEW_ENRICHED', alongside 'OBJECT_CORE'/
# 'COLUMN_DEFERRED'. Step 6C1's audit rejected that design and replaced it
# with this dedicated table for three concrete reasons (see migration
# 017's own docstring for the full writeup):
#   1. sql_semantic_enrichments is a LIVE, already-populated table (hundreds of
#      real ACTIVE OBJECT_CORE rows as of this audit) — rebuilding a live,
#      populated, indexed, foreign-keyed table is materially riskier than
#      creating a new, empty one.
#   2. data/db.py's get_connection() sets `PRAGMA foreign_keys = ON`, and
#      SQLite only allows toggling that pragma OUTSIDE an active
#      transaction — the abandoned rebuild never accounted for this.
#   3. SQLite has no ALTER TABLE ... ALTER COLUMN / DROP CONSTRAINT, so
#      widening a CHECK is inherently a multi-statement, partially-
#      completable operation; a brand-new table is a single atomic
#      CREATE TABLE IF NOT EXISTS that can never be partially applied.
#
# Still reuses everything genuinely reusable: the SAME ownership check
# (_verify_ownership), the SAME ACTIVE/INVALID status vocabulary, the SAME
# CREATED/REUSED/REPAIRED/UNOWNED outcome vocabulary, and the SAME
# StoredEnrichedColumn/EnrichedColumnSemantics column shape as every
# function above (columns are never re-typed for views). Only the
# OBJECT-level payload shape differs (EnrichedViewObjectSemantics has
# business_domain/status_concepts/source_role fields EnrichedObjectSemantics
# does not) and the target table — never a `phase` column here at all:
# every row in sql_view_semantic_enrichments is, by table identity alone,
# a VIEW_ENRICHED record. core.sqlservice.semantic_enrichment_phases.
# PHASE_VIEW_ENRICHED remains a valid Python-level constant (folded into
# every SemanticEnrichmentCacheKey this module's callers construct for a
# view, and used for readiness-status reporting) purely as an in-memory
# identity/bookkeeping label — it is never written to any column here.
#
# mark_enrichment_invalid_by_id() above does NOT work for a view row (it
# targets sql_semantic_enrichments specifically) — see
# mark_view_enrichment_invalid_by_id() below, a small dedicated mirror.

_REQUIRED_VIEW_PAYLOAD_OBJECT_KEYS = frozenset({
    "business_domain", "business_name", "description", "source_role", "entity_concepts",
    "status_concepts", "aliases", "supported_operations", "confidence", "provenance",
})
_REQUIRED_VIEW_STATUS_CONCEPT_KEYS = frozenset({"column_name", "label"})


@dataclass(frozen=True)
class StoredViewSemanticEnrichment:
    """One persisted view's VIEW_ENRICHED semantics, read back from
    sql_view_semantic_enrichments (a dedicated table — see this section's
    own module-level comment). `phase` is always PHASE_VIEW_ENRICHED,
    populated here for API-shape consistency with StoredSemanticEnrichment
    even though the underlying table has no `phase` column of its own (row
    identity as a view record comes from the table itself). Immutable
    reconstruction, mirroring StoredSemanticEnrichment exactly (every list
    becomes a tuple)."""

    id: int
    source_id: int
    object_fqn: str
    semantic_input_hash: str
    provider_id: str
    model: str
    contract_version: int
    phase: str
    business_domain: str
    business_name: str
    description: str
    source_role: str
    entity_concepts: tuple[str, ...]
    status_concepts: tuple[ViewStatusConceptRef, ...]
    aliases: tuple[str, ...]
    supported_operations: tuple[str, ...]
    confidence: float
    provenance: tuple[EvidenceItem, ...]
    columns: tuple[StoredEnrichedColumn, ...]
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class InsertViewEnrichmentResult:
    """Result of one insert_view_semantic_enrichment() call — same outcome
    vocabulary as InsertEnrichmentResult (OUTCOME_CREATED/REUSED/REPAIRED/
    UNOWNED)."""

    outcome: str
    enrichment: StoredViewSemanticEnrichment | None


def _serialize_view_payload(
    object_semantics: EnrichedViewObjectSemantics, column_semantics: Sequence[EnrichedColumnSemantics],
) -> str:
    payload = {
        "object": {
            "business_domain": object_semantics.business_domain,
            "business_name": object_semantics.business_name,
            "description": object_semantics.description,
            "source_role": object_semantics.source_role,
            "entity_concepts": list(object_semantics.entity_concepts),
            "status_concepts": [
                {"column_name": s.column_name, "label": s.label} for s in object_semantics.status_concepts
            ],
            "aliases": list(object_semantics.aliases),
            "supported_operations": list(object_semantics.supported_operations),
            "confidence": object_semantics.confidence,
            "provenance": _provenance_to_json(object_semantics.provenance),
        },
        "columns": [
            {
                "column_name": c.column_name,
                "business_label": c.business_label,
                "semantic_role": c.semantic_role,
                "date_role": c.date_role,
                "measure_role": c.measure_role,
                "dimension_role": c.dimension_role,
                "identifier_role": c.identifier_role,
                "confidence": c.confidence,
                "provenance": _provenance_to_json(c.provenance),
            }
            for c in column_semantics
        ],
    }
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"view enrichment payload is not JSON-safe: {type(exc).__name__}") from exc


def _deserialize_view_payload(payload_json: str) -> tuple[dict, tuple[StoredEnrichedColumn, ...]]:
    """Strict decode -> (object_fields_dict, columns_tuple) for a
    VIEW_ENRICHED payload. Mirrors _deserialize_payload exactly, with the
    VIEW-specific required-key set and status_concepts shape."""
    try:
        parsed = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CorruptEnrichmentPayloadError(type(exc).__name__) from exc

    if not isinstance(parsed, dict):
        raise CorruptEnrichmentPayloadError("payload root is not an object")
    obj = parsed.get("object")
    cols = parsed.get("columns")
    if not isinstance(obj, dict) or not isinstance(cols, list):
        raise CorruptEnrichmentPayloadError("payload missing 'object'/'columns'")

    missing_object_keys = _REQUIRED_VIEW_PAYLOAD_OBJECT_KEYS - set(obj.keys())
    if missing_object_keys:
        raise CorruptEnrichmentPayloadError(f"view object payload missing key(s): {sorted(missing_object_keys)}")
    if not isinstance(obj["confidence"], (int, float)) or isinstance(obj["confidence"], bool):
        raise CorruptEnrichmentPayloadError("view object confidence is not a number")

    status_concepts = []
    for entry in obj["status_concepts"]:
        if not isinstance(entry, dict) or set(entry.keys()) != _REQUIRED_VIEW_STATUS_CONCEPT_KEYS:
            raise CorruptEnrichmentPayloadError("status_concepts entry malformed")
        if not isinstance(entry["column_name"], str) or not isinstance(entry["label"], str):
            raise CorruptEnrichmentPayloadError("status_concepts entry field is not a string")
        status_concepts.append(ViewStatusConceptRef(column_name=entry["column_name"], label=entry["label"]))

    object_fields = {
        "business_domain": obj["business_domain"],
        "business_name": obj["business_name"],
        "description": obj["description"],
        "source_role": obj["source_role"],
        "entity_concepts": tuple(obj["entity_concepts"]),
        "status_concepts": tuple(status_concepts),
        "aliases": tuple(obj["aliases"]),
        "supported_operations": tuple(obj["supported_operations"]),
        "confidence": float(obj["confidence"]),
        "provenance": _provenance_from_json(obj["provenance"]),
    }

    columns: list[StoredEnrichedColumn] = []
    for col in cols:
        if not isinstance(col, dict):
            raise CorruptEnrichmentPayloadError("column payload entry is not an object")
        missing_column_keys = _REQUIRED_PAYLOAD_COLUMN_KEYS - set(col.keys())
        if missing_column_keys:
            raise CorruptEnrichmentPayloadError(f"column payload missing key(s): {sorted(missing_column_keys)}")
        if not isinstance(col["confidence"], (int, float)) or isinstance(col["confidence"], bool):
            raise CorruptEnrichmentPayloadError("column confidence is not a number")
        columns.append(
            StoredEnrichedColumn(
                column_name=col["column_name"],
                business_label=col["business_label"],
                semantic_role=col["semantic_role"],
                date_role=col["date_role"],
                measure_role=col["measure_role"],
                dimension_role=col["dimension_role"],
                identifier_role=col["identifier_role"],
                confidence=float(col["confidence"]),
                provenance=_provenance_from_json(col["provenance"]),
            )
        )

    return object_fields, tuple(columns)


def _row_to_stored_view(row) -> StoredViewSemanticEnrichment | None:
    """None (never a raised exception) for a row whose payload_json fails
    strict VIEW_ENRICHED decoding — mirrors _row_to_stored exactly."""
    try:
        object_fields, columns = _deserialize_view_payload(row["payload_json"])
    except CorruptEnrichmentPayloadError as exc:
        logger.warning(
            "sql_view_semantic_enrichments row id=%s failed payload decode (%s) -- excluded from results",
            row["id"], type(exc).__name__,
        )
        return None

    return StoredViewSemanticEnrichment(
        id=row["id"],
        source_id=row["source_id"],
        object_fqn=row["object_fqn"],
        semantic_input_hash=row["semantic_input_hash"],
        provider_id=row["provider_id"],
        model=row["model"],
        contract_version=row["contract_version"],
        phase=PHASE_VIEW_ENRICHED,
        columns=columns,
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        **object_fields,
    )


def insert_view_semantic_enrichment(
    user_id: str,
    *,
    source_id: int,
    object_fqn: str,
    semantic_input_hash: str,
    provider_id: str,
    model: str,
    contract_version: int,
    object_semantics: EnrichedViewObjectSemantics,
    column_semantics: Sequence[EnrichedColumnSemantics] = (),
) -> InsertViewEnrichmentResult:
    """
    Persist one already-validated VIEW's semantics, idempotently, at the
    cache key (source_id, object_fqn, semantic_input_hash, provider_id,
    model, contract_version) into `sql_view_semantic_enrichments` — a
    DEDICATED table (see this section's own module-level comment for why),
    never `sql_semantic_enrichments`. There is no caller-supplied `phase`
    parameter (unlike insert_semantic_enrichment) because this table has no
    `phase` column at all — every row here is a view record by construction.

    Same trust/idempotency/ownership contract as insert_semantic_enrichment
    (see that function's own docstring): raises ValueError for a malformed
    identity argument or a non-EnrichedViewObjectSemantics/-EnrichedColumnSemantics
    argument (never a raw dict/kwargs payload); returns outcome=UNOWNED
    (never raised) for a source_id that does not belong to user_id;
    REUSED for an existing healthy ACTIVE row at this exact key; REPAIRED
    for an existing INVALID/corrupt row at this exact key; CREATED
    otherwise.
    """
    _validate_insert_args(
        source_id=source_id, object_fqn=object_fqn, semantic_input_hash=semantic_input_hash,
        provider_id=provider_id, model=model, contract_version=contract_version, phase=PHASE_VIEW_ENRICHED,
    )
    if not isinstance(object_semantics, EnrichedViewObjectSemantics):
        raise ValueError(
            "object_semantics must be a real core.sqlservice.semantic_enrichment_service."
            "EnrichedViewObjectSemantics produced by that module's own strict response validation "
            "-- a raw dict/kwargs payload is never accepted here"
        )
    if object_semantics.object_fqn.lower() != object_fqn.lower():
        raise ValueError("object_semantics.object_fqn does not match object_fqn")
    for i, col in enumerate(column_semantics):
        if not isinstance(col, EnrichedColumnSemantics):
            raise ValueError(
                f"column_semantics[{i}] must be a real core.sqlservice.semantic_enrichment_service."
                "EnrichedColumnSemantics produced by that module's own strict response validation"
            )
        if col.object_fqn.lower() != object_fqn.lower():
            raise ValueError(f"column_semantics[{i}].object_fqn does not match object_fqn")

    payload_json = _serialize_view_payload(object_semantics, column_semantics)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return InsertViewEnrichmentResult(outcome=OUTCOME_UNOWNED, enrichment=None)

        existing_row = conn.execute(
            "SELECT * FROM sql_view_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
            "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ?",
            (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version),
        ).fetchone()

        if existing_row is not None:
            existing_is_healthy = existing_row["status"] == STATUS_ACTIVE and _row_to_stored_view(existing_row) is not None
            if existing_is_healthy:
                return InsertViewEnrichmentResult(outcome=OUTCOME_REUSED, enrichment=_row_to_stored_view(existing_row))

            now = _now()
            try:
                conn.execute(
                    "UPDATE sql_view_semantic_enrichments SET payload_json = ?, status = ?, updated_at = ? WHERE id = ?",
                    (payload_json, STATUS_ACTIVE, now, existing_row["id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            repaired_row = conn.execute(
                "SELECT * FROM sql_view_semantic_enrichments WHERE id = ?", (existing_row["id"],),
            ).fetchone()
            return InsertViewEnrichmentResult(outcome=OUTCOME_REPAIRED, enrichment=_row_to_stored_view(repaired_row))

        now = _now()
        try:
            cursor = conn.execute(
                "INSERT INTO sql_view_semantic_enrichments "
                "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, "
                " payload_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version,
                    payload_json, STATUS_ACTIVE, now, now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            winner = conn.execute(
                "SELECT * FROM sql_view_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
                "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ?",
                (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version),
            ).fetchone()
            if winner is None:
                raise
            return InsertViewEnrichmentResult(outcome=OUTCOME_REUSED, enrichment=_row_to_stored_view(winner))
        except Exception:
            conn.rollback()
            raise

        row = conn.execute("SELECT * FROM sql_view_semantic_enrichments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return InsertViewEnrichmentResult(outcome=OUTCOME_CREATED, enrichment=_row_to_stored_view(row))
    finally:
        conn.close()


def get_view_enrichment_by_cache_key(
    source_id: int, user_id: str, *, object_fqn: str, semantic_input_hash: str,
    provider_id: str, model: str, contract_version: int,
) -> StoredViewSemanticEnrichment | None:
    """One ACTIVE, decodable VIEW_ENRICHED row at the exact cache key, for an
    owned source — mirrors get_enrichment_by_cache_key exactly, against
    `sql_view_semantic_enrichments`."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM sql_view_semantic_enrichments WHERE source_id = ? AND object_fqn = ? "
            "AND semantic_input_hash = ? AND provider_id = ? AND model = ? AND contract_version = ? "
            "AND status = ?",
            (source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, STATUS_ACTIVE),
        ).fetchone()
        return _row_to_stored_view(row) if row is not None else None
    finally:
        conn.close()


def load_current_view_enrichments_for_source(
    source_id: int, user_id: str, *, provider_id: str, model: str, contract_version: int,
    current_hashes_by_object_fqn: Mapping[str, str],
) -> tuple[StoredViewSemanticEnrichment, ...]:
    """Every ACTIVE, decodable VIEW_ENRICHED row for this owned source whose
    OWN semantic_input_hash matches the caller-supplied current hash for
    that object_fqn — mirrors load_current_enrichments_for_source exactly,
    against `sql_view_semantic_enrichments`. Deterministically ordered by
    object_fqn."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ()
        rows = conn.execute(
            "SELECT * FROM sql_view_semantic_enrichments WHERE source_id = ? AND status = ? "
            "AND provider_id = ? AND model = ? AND contract_version = ?",
            (source_id, STATUS_ACTIVE, provider_id, model, contract_version),
        ).fetchall()

        current_lower = {fqn.lower(): h for fqn, h in current_hashes_by_object_fqn.items()}
        results = []
        for row in rows:
            expected_hash = current_lower.get(row["object_fqn"].lower())
            if expected_hash is None or expected_hash != row["semantic_input_hash"]:
                continue
            stored = _row_to_stored_view(row)
            if stored is not None:
                results.append(stored)

        results.sort(key=lambda s: s.object_fqn.lower())
        return tuple(results)
    finally:
        conn.close()


def mark_view_enrichment_invalid_by_id(enrichment_id: int, source_id: int, user_id: str) -> bool:
    """Flip one VIEW_ENRICHED row's status to INVALID without touching
    payload_json — mirrors mark_enrichment_invalid_by_id exactly, against
    `sql_view_semantic_enrichments`. Returns True on a successful update,
    False for an unowned source or a nonexistent/mismatched enrichment_id
    (never raises for that)."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return False
        cursor = conn.execute(
            "UPDATE sql_view_semantic_enrichments SET status = ?, updated_at = ? WHERE id = ? AND source_id = ?",
            (STATUS_INVALID, _now(), enrichment_id, source_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def count_view_enrichment_completeness(
    source_id: int, user_id: str, *, provider_id: str, model: str, contract_version: int,
    current_hashes_by_object_fqn: Mapping[str, str],
) -> EnrichmentCompletenessResult:
    """How many of `current_hashes_by_object_fqn`'s VIEWS already have a
    CURRENT, ACTIVE, decodable row in `sql_view_semantic_enrichments` —
    mirrors count_enrichment_completeness exactly."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return EnrichmentCompletenessResult(
                status=COMPLETENESS_STATUS_UNOWNED, requested_count=len(current_hashes_by_object_fqn),
                cached_current_count=0, missing_object_fqns=tuple(sorted(current_hashes_by_object_fqn)),
            )
    finally:
        conn.close()

    current = load_current_view_enrichments_for_source(
        source_id, user_id, provider_id=provider_id, model=model, contract_version=contract_version,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn,
    )
    found_lower = {e.object_fqn.lower() for e in current}
    missing = tuple(sorted(fqn for fqn in current_hashes_by_object_fqn if fqn.lower() not in found_lower))

    return EnrichmentCompletenessResult(
        status=COMPLETENESS_STATUS_OK, requested_count=len(current_hashes_by_object_fqn),
        cached_current_count=len(current), missing_object_fqns=missing,
    )
