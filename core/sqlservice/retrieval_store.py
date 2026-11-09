"""
Transactional SQLite persistence for retrieval-document builds — SQL
rebuild Phase 1, Step 5.

Adapter between an in-memory `RetrievalDocumentBatch`
(core/sqlservice/metadata_loader.py) plus a structural fingerprint
(data/schema_fingerprint_service.py's `compute_fingerprint()` contract —
this module never invents its own fingerprint algorithm) and ToolSmithAI's
local SQLite metadata store: `sql_retrieval_builds` /
`sql_retrieval_documents` (see data/models.py and
migrations/012_create_sql_retrieval_store.sql, which define identical
schemas).

Scope: reads/writes ToolSmithAI's own local SQLite store only. Never
connects to a live SQL Server, never calls OpenAI, never computes
embeddings — embeddings are a later step and this schema deliberately has
no column for them.

Reuse: persisting is idempotent per (source, schema fingerprint,
document-set hash) — an unchanged source returns the existing READY build
(outcome="REUSED") instead of inserting duplicate rows. A genuine change
(document content or schema fingerprint) always creates a new build; older
READY builds are never deleted or overwritten by this step.
"""

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Mapping as AbcMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Sequence

from core.sqlservice.metadata_loader import STATUS_READY, RetrievalDocumentBatch
from core.sqlservice.retrieval_documents import RetrievalDocument
from data.db import get_connection

logger = logging.getLogger(__name__)


# ── Outcome vocabulary ────────────────────────────────────────────────────

OUTCOME_CREATED = "CREATED"
OUTCOME_REUSED = "REUSED"
OUTCOME_UNOWNED = "UNOWNED"

_ALLOWED_OBJECT_TYPES = frozenset({"TABLE", "VIEW"})
_ALLOWED_TIERS = frozenset({1, 2, 3, 4})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredRetrievalDocument:
    """One persisted retrieval document, read back from sql_retrieval_documents."""

    id: int
    build_id: int
    source_id: int
    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]  # parsed from metadata_json; read-only
    created_at: str


@dataclass(frozen=True)
class StoredRetrievalBuild:
    """One persisted build record, read back from sql_retrieval_builds."""

    id: int
    source_id: int
    snapshot_id: int
    snapshot_version: int
    fingerprint_version: int
    schema_fingerprint: str
    table_set_hash: str
    document_set_hash: str
    status: str
    document_count: int
    error_message: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PersistRetrievalBuildResult:
    """Result of one persist_retrieval_build() call."""

    outcome: str  # OUTCOME_CREATED | OUTCOME_REUSED | OUTCOME_UNOWNED
    build: StoredRetrievalBuild | None


# ── Metadata JSON serialization (canonical, fail-clear) ──────────────────


def _serialize_metadata(metadata: Mapping[str, object]) -> str:
    """
    Canonical JSON: sorted keys, compact separators, no NaN/Infinity.
    Raises ValueError (never silently stringifies) when metadata isn't
    JSON-safe — e.g. a set, a custom object, or a non-string-keyed dict.
    """
    try:
        return json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"document metadata is not JSON-safe: {exc}") from exc


def _deep_freeze(value: object) -> object:
    """
    Recursively convert a plain json.loads() result into an immutable
    structure: dicts -> MappingProxyType (recursively, including dicts
    nested inside lists/tuples), lists -> tuples (recursively). JSON
    scalars (str/int/float/bool/None) are already immutable and pass
    through unchanged.
    """
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deserialize_metadata(raw: str) -> Mapping[str, object]:
    """
    Raises ValueError on malformed stored JSON — never silently returns {}.
    The returned mapping is deeply immutable (see _deep_freeze): nested
    dicts/lists are read-only too, not just the top-level object.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stored metadata_json is malformed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("stored metadata_json did not decode to a JSON object")
    return _deep_freeze(parsed)


# ── Document-set hash (order-independent) ─────────────────────────────────
#
# SQL rebuild Phase 5, Step 4B — hashing contract.
#
# DOCUMENT_SET_HASH_VERSION is the domain separator for compute_document_set_
# hash()'s canonical payload SHAPE (which document fields it hashes, and how
# metadata is canonicalized) — orthogonal to fingerprint_version (which
# versions data.schema_fingerprint_service's own ALGORITHM for the schema
# structure itself) and orthogonal to the schema content captured by
# schema_fingerprint. Bump this constant, and ONLY this constant, whenever
# compute_document_set_hash()'s payload shape changes (e.g. a future field
# added to what gets hashed) — never for a change to document_text/
# document_hash's own construction (core.sqlservice.retrieval_documents
# already forces a new hash for that on its own, via document_hash itself).
#
# Because this version number is folded directly into the hashed payload
# (see compute_document_set_hash below), persist_retrieval_build()'s
# existing reuse-identity index — (source_id, fingerprint_version,
# schema_fingerprint, document_set_hash), unchanged, no migration — already
# does the right thing the moment this constant changes: a build persisted
# under an older DOCUMENT_SET_HASH_VERSION carries an old-format hash value
# that can never equal a newly computed one for the same logical documents
# (different version number hashed in, different canonical payload shape),
# so _find_existing_ready_build() simply never matches it and a new build
# is created — the old build's row is never touched. No separate stored
# "metadata version" column is needed or added; this comment IS the
# observability contract (see this module's own docstring note and the
# Phase 5 Step 4B report for the decision not to duplicate this in schema).
DOCUMENT_SET_HASH_VERSION = 2

_UNSUPPORTED_METADATA_TYPE = (
    "unsupported metadata value type {type_name!r} for document-set hashing "
    "(only str/int/float/bool/None, plus nested mappings/sequences of those, are supported — "
    "never silently stringified via repr())"
)


def _canonicalize_metadata(value: object) -> object:
    """
    Recursively normalize one metadata value into a plain, deterministically
    JSON-serializable structure — never document_text prose, only whatever
    the caller's own metadata already carries.

    - Mapping (dict, MappingProxyType, or any other Mapping) -> a plain
      dict with string keys; json.dumps(..., sort_keys=True) below is what
      actually sorts keys deterministically, so this step only needs to
      strip the Mapping wrapper (and recurse), never resolve ordering
      itself.
    - list/tuple (never str/bytes, which are handled as scalars) -> a plain
      list, recursively canonicalized, ORDER-PRESERVING. Sequences are
      never reordered here: core.sqlservice.retrieval_documents already
      constructs every list-shaped metadata field (synonyms, column_names,
      primary_key_columns, ...) in a fixed, deterministic sorted order at
      the source — this function's job is a consistent list/tuple ->
      canonical-array TYPE representation (so a list and an equal-content
      tuple hash identically), not a second, possibly-wrong reordering of
      data whose order can be semantically meaningful.
    - str/int/float/bool/None -> returned unchanged (json.dumps' own
      allow_nan=False, applied once at the top level in
      compute_document_set_hash, is what rejects a NaN/Infinity float —
      never checked here separately).
    - anything else (a set, a custom object, ...) -> raises ValueError
      immediately. Never falls back to str()/repr() — an unsupported value
      is a genuine caller bug to surface, not something to paper over with
      a nondeterministic or lossy string form.
    """
    if isinstance(value, AbcMapping):
        return {str(k): _canonicalize_metadata(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize_metadata(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(_UNSUPPORTED_METADATA_TYPE.format(type_name=type(value).__name__))


def compute_document_set_hash(documents: Sequence[RetrievalDocument]) -> str:
    """
    Deterministic hash over every document's full retrieval-relevant
    identity — canonical object_fqn, object_type, retrieval_tier,
    document_hash, AND recursively canonicalized metadata (see
    _canonicalize_metadata) — under the DOCUMENT_SET_HASH_VERSION domain
    separator (see this section's own comment above for the full
    versioning contract).

    Independent of input ordering (documents are sorted by object_fqn
    before hashing — object_fqn is already validated unique within one
    batch by _validate_batch, so this is a total order) and independent of
    mapping-key insertion order within any document's metadata (handled by
    json.dumps(sort_keys=True) below, applied recursively via
    _canonicalize_metadata's own dict-with-string-keys output).

    A build persisted under this function's OLD (Step 4B pre-upgrade)
    two-field (object_fqn, document_hash) payload shape can never match a
    hash computed by this version for the same documents — see
    DOCUMENT_SET_HASH_VERSION's own comment for why that is exactly the
    intended, migration-free upgrade behavior.

    document_hash itself (core.sqlservice.retrieval_documents'
    sha256(document_text)) is used here purely as one INPUT field, exactly
    as before — this function never changes document_text or document_hash
    to force a rebuild; embeddings remain keyed by document_hash alone
    (core/sqlservice/embedding_store.py), so they are always reused across
    a metadata-only-driven new build.

    Raises:
        ValueError: any document's metadata contains a value
            _canonicalize_metadata cannot represent (see its own
            docstring), or a NaN/Infinity float anywhere in the payload.
    """
    canonical_documents = [
        {
            "object_fqn": d.object_fqn,
            "object_type": d.object_type,
            "retrieval_tier": d.retrieval_tier,
            "document_hash": d.document_hash,
            "metadata": _canonicalize_metadata(dict(d.metadata)),
        }
        for d in documents
    ]
    canonical_documents.sort(key=lambda item: item["object_fqn"])

    payload = {
        "document_set_hash_version": DOCUMENT_SET_HASH_VERSION,
        "documents": canonical_documents,
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"document set is not hashable: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Validation (pure, no I/O — raises ValueError, never writes) ─────────


def _validate_batch(batch: RetrievalDocumentBatch) -> None:
    if batch.status != STATUS_READY:
        raise ValueError(f"batch status must be {STATUS_READY!r}, got {batch.status!r}")
    if not isinstance(batch.source_id, int) or isinstance(batch.source_id, bool) or batch.source_id <= 0:
        raise ValueError("batch.source_id is invalid")
    if batch.snapshot_id is None:
        raise ValueError("batch.snapshot_id is missing")
    if batch.snapshot_version is None:
        raise ValueError("batch.snapshot_version is missing")
    if not batch.documents:
        raise ValueError("batch.documents is empty")

    fqns = [d.object_fqn for d in batch.documents]
    if len(fqns) != len(set(fqns)):
        raise ValueError("batch.documents contains duplicate object_fqn values")

    for doc in batch.documents:
        if doc.source_id != batch.source_id:
            raise ValueError(
                f"document source_id {doc.source_id!r} does not match batch.source_id {batch.source_id!r}"
            )
        if not isinstance(doc.document_hash, str) or not _HEX64_RE.match(doc.document_hash):
            raise ValueError(f"document_hash is malformed for {doc.object_fqn!r}")
        if doc.retrieval_tier not in _ALLOWED_TIERS:
            raise ValueError(f"retrieval_tier {doc.retrieval_tier!r} is outside 1-4 for {doc.object_fqn!r}")
        if doc.object_type not in _ALLOWED_OBJECT_TYPES:
            raise ValueError(f"object_type {doc.object_type!r} is unsupported for {doc.object_fqn!r}")
        # Fail loudly on non-JSON-safe metadata now, rather than partway
        # through the transactional insert below.
        _serialize_metadata(doc.metadata)


def _validate_fingerprint(fingerprint: Mapping[str, object], batch: RetrievalDocumentBatch) -> None:
    fingerprint_version = fingerprint.get("fingerprint_version")
    fingerprint_hash = fingerprint.get("fingerprint_hash")
    table_set_hash = fingerprint.get("table_set_hash")

    if not isinstance(fingerprint_version, int) or isinstance(fingerprint_version, bool) or fingerprint_version <= 0:
        raise ValueError("fingerprint.fingerprint_version is missing or malformed")
    if not isinstance(fingerprint_hash, str) or not _HEX64_RE.match(fingerprint_hash):
        raise ValueError("fingerprint.fingerprint_hash is missing or malformed")
    if not isinstance(table_set_hash, str) or not _HEX64_RE.match(table_set_hash):
        raise ValueError("fingerprint.table_set_hash is missing or malformed")

    table_fqns = fingerprint.get("table_fqns")
    if table_fqns is not None:
        if not isinstance(table_fqns, (list, tuple, set, frozenset)):
            raise ValueError("fingerprint.table_fqns must be a list of strings when supplied")
        fingerprint_tables = set(table_fqns)
        batch_tables = {d.object_fqn for d in batch.documents}
        if fingerprint_tables != batch_tables:
            raise ValueError(
                "fingerprint.table_fqns does not agree with the batch's document object_fqns"
            )


# ── Ownership / snapshot verification ─────────────────────────────────────


def _verify_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _snapshot_belongs_to_source(conn, snapshot_id: int, source_id: int) -> bool:
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE id = ? AND source_id = ?",
        (snapshot_id, source_id),
    ).fetchone()
    return row is not None


# ── Row <-> dataclass mapping ──────────────────────────────────────────────


def _row_to_stored_build(row) -> StoredRetrievalBuild:
    return StoredRetrievalBuild(
        id=row["id"],
        source_id=row["source_id"],
        snapshot_id=row["snapshot_id"],
        snapshot_version=row["snapshot_version"],
        fingerprint_version=row["fingerprint_version"],
        schema_fingerprint=row["schema_fingerprint"],
        table_set_hash=row["table_set_hash"],
        document_set_hash=row["document_set_hash"],
        status=row["status"],
        document_count=row["document_count"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _row_to_stored_document(row) -> StoredRetrievalDocument:
    return StoredRetrievalDocument(
        id=row["id"],
        build_id=row["build_id"],
        source_id=row["source_id"],
        object_fqn=row["object_fqn"],
        object_type=row["object_type"],
        retrieval_tier=row["retrieval_tier"],
        document_hash=row["document_hash"],
        document_text=row["document_text"],
        metadata=_deserialize_metadata(row["metadata_json"]),
        created_at=row["created_at"],
    )


def _find_existing_ready_build(
    conn, source_id: int, fingerprint_version: int, schema_fingerprint: str, document_set_hash: str,
) -> StoredRetrievalBuild | None:
    """
    Reuse identity is (source_id, fingerprint_version, schema_fingerprint,
    document_set_hash) — the SAME four columns the unique index in
    data/models.py / migrations/012_create_sql_retrieval_store.sql
    enforces. fingerprint_version is deliberately part of this query: a
    schema_fingerprint computed under an older fingerprint algorithm
    version must never be reused by a caller supplying a newer version,
    even if the hash strings happened to collide.
    """
    row = conn.execute(
        "SELECT * FROM sql_retrieval_builds "
        "WHERE source_id = ? AND status = 'READY' "
        "AND fingerprint_version = ? AND schema_fingerprint = ? AND document_set_hash = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (source_id, fingerprint_version, schema_fingerprint, document_set_hash),
    ).fetchone()
    return _row_to_stored_build(row) if row is not None else None


def _is_unique_build_conflict(exc: sqlite3.IntegrityError) -> bool:
    """
    True only for a violation of the specific
    (source_id, fingerprint_version, schema_fingerprint, document_set_hash)
    unique index on sql_retrieval_builds — never for an unrelated
    IntegrityError (a CHECK constraint failure, a foreign-key violation,
    etc.), which must propagate rather than be silently reinterpreted as
    a successful concurrent build.
    """
    message = str(exc)
    return (
        "UNIQUE constraint failed" in message
        and "sql_retrieval_builds" in message
        and "fingerprint_version" in message
    )


# ── Transactional build creation ──────────────────────────────────────────


def _create_build(
    conn,
    batch: RetrievalDocumentBatch,
    fingerprint: Mapping[str, object],
    document_set_hash: str,
) -> StoredRetrievalBuild:
    """
    Insert one BUILDING row, bulk-insert its documents, verify the count,
    and transition to READY — all in the single implicit transaction
    Python's sqlite3 module opens on the first DML statement below.
    Any failure rolls back everything inserted here (the BUILDING row
    included), leaving no partial row and never touching a prior build.
    """
    now = _now()
    try:
        cursor = conn.execute(
            "INSERT INTO sql_retrieval_builds "
            "(source_id, snapshot_id, snapshot_version, fingerprint_version, "
            " schema_fingerprint, table_set_hash, document_set_hash, "
            " status, document_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'BUILDING', 0, ?)",
            (
                batch.source_id, batch.snapshot_id, batch.snapshot_version,
                fingerprint["fingerprint_version"], fingerprint["fingerprint_hash"],
                fingerprint["table_set_hash"], document_set_hash, now,
            ),
        )
        build_id = cursor.lastrowid

        rows = [
            (
                build_id, batch.source_id, doc.object_fqn, doc.object_type,
                doc.retrieval_tier, doc.document_hash, doc.document_text,
                _serialize_metadata(doc.metadata), now,
            )
            for doc in batch.documents
        ]
        conn.executemany(
            "INSERT INTO sql_retrieval_documents "
            "(build_id, source_id, object_fqn, object_type, retrieval_tier, "
            " document_hash, document_text, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        inserted = conn.execute(
            "SELECT COUNT(*) FROM sql_retrieval_documents WHERE build_id = ?",
            (build_id,),
        ).fetchone()[0]
        if inserted != len(batch.documents):
            raise RuntimeError(
                f"document insert count mismatch: expected {len(batch.documents)}, got {inserted}"
            )

        completed_at = _now()
        conn.execute(
            "UPDATE sql_retrieval_builds SET status = 'READY', document_count = ?, completed_at = ? "
            "WHERE id = ?",
            (inserted, completed_at, build_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    row = conn.execute(
        "SELECT * FROM sql_retrieval_builds WHERE id = ?", (build_id,)
    ).fetchone()
    return _row_to_stored_build(row)


# ── Public entry points ────────────────────────────────────────────────────


def persist_retrieval_build(
    batch: RetrievalDocumentBatch,
    fingerprint: Mapping[str, object],
    user_id: str,
) -> PersistRetrievalBuildResult:
    """
    Persist a READY RetrievalDocumentBatch (plus its structural fingerprint
    from data/schema_fingerprint_service.compute_fingerprint()) as one
    build + its documents, atomically.

    Validation (batch shape, fingerprint shape) happens before any
    database access and raises ValueError — a caller passing malformed
    input gets a clear exception, never a silently-empty or partial write.
    Ownership is checked against the database and reported as
    outcome=UNOWNED (not raised) — a routine, expected condition, not a
    caller error.

    An identical READY build (same source, fingerprint_version,
    schema_fingerprint, document_set_hash) is reused: outcome=REUSED, no
    new rows inserted. Otherwise a new build is created: outcome=CREATED.
    See _create_build for the atomic insert/verify/READY-transition/commit
    sequence and its rollback-on-any-failure guarantee.

    Concurrency: the same four columns are also a UNIQUE index on
    sql_retrieval_builds (see data/models.py /
    migrations/012_create_sql_retrieval_store.sql), so the check-then-
    insert above is not a TOCTOU race — SQLite serializes writers, so a
    losing concurrent caller's INSERT raises sqlite3.IntegrityError, which
    is caught below: its own (already-rolled-back-by-_create_build)
    transaction yields to a fresh lookup of the winner's now-committed
    build, returned as outcome=REUSED. An IntegrityError NOT matching that
    specific unique index (see _is_unique_build_conflict) is re-raised
    unchanged — never silently swallowed.

    NOTE for the future coordinator (not implemented in this step): the
    coordinator must derive or verify the schema fingerprint against the
    exact snapshot_id and snapshot_version contained in the loaded
    RetrievalDocumentBatch; it must not independently fingerprint
    whichever snapshot happens to be latest. This module never recomputes
    or duplicates data/schema_fingerprint_service's algorithm — it only
    validates the shape of whatever fingerprint dict it is handed.
    """
    _validate_batch(batch)
    _validate_fingerprint(fingerprint, batch)
    document_set_hash = compute_document_set_hash(batch.documents)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, batch.source_id, user_id):
            return PersistRetrievalBuildResult(outcome=OUTCOME_UNOWNED, build=None)

        if not _snapshot_belongs_to_source(conn, batch.snapshot_id, batch.source_id):
            raise ValueError("batch.snapshot_id does not belong to batch.source_id")

        existing = _find_existing_ready_build(
            conn, batch.source_id, fingerprint["fingerprint_version"],
            fingerprint["fingerprint_hash"], document_set_hash,
        )
        if existing is not None:
            return PersistRetrievalBuildResult(outcome=OUTCOME_REUSED, build=existing)

        try:
            build = _create_build(conn, batch, fingerprint, document_set_hash)
        except sqlite3.IntegrityError as exc:
            if not _is_unique_build_conflict(exc):
                raise
            # Lost a race to a concurrent, logically-identical build.
            # _create_build already rolled back our own transaction on
            # this same exception; the winner's build must now be visible
            # (SQLite serializes writers), so hand it back as REUSED.
            existing = _find_existing_ready_build(
                conn, batch.source_id, fingerprint["fingerprint_version"],
                fingerprint["fingerprint_hash"], document_set_hash,
            )
            if existing is None:
                # A unique-index conflict fired but no matching READY row
                # is visible — an unexpected state, not a normal race.
                # Don't paper over it as REUSED.
                raise
            return PersistRetrievalBuildResult(outcome=OUTCOME_REUSED, build=existing)

        return PersistRetrievalBuildResult(outcome=OUTCOME_CREATED, build=build)
    finally:
        conn.close()


def get_latest_ready_retrieval_build(
    source_id: int,
    user_id: str,
) -> StoredRetrievalBuild | None:
    """Latest READY build for an owned source, or None (unowned or none exist)."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM sql_retrieval_builds "
            "WHERE source_id = ? AND status = 'READY' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return _row_to_stored_build(row) if row is not None else None
    finally:
        conn.close()


def get_retrieval_build(
    build_id: int,
    source_id: int,
    user_id: str,
) -> StoredRetrievalBuild | None:
    """
    One build row by id, for an owned source — any status, not just READY
    (unlike get_latest_ready_retrieval_build). Returns None for an unowned
    source, a build_id that doesn't exist, or one that belongs to a
    different source. Added for SQL rebuild Phase 2, Step 4 (semantic
    schema retrieval), which already knows a specific build_id is READY
    (via core.sqlservice.vector_index's own gate) and just needs that
    build's schema_fingerprint for its result's identity fields.
    """
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM sql_retrieval_builds WHERE id = ? AND source_id = ?",
            (build_id, source_id),
        ).fetchone()
        return _row_to_stored_build(row) if row is not None else None
    finally:
        conn.close()


def load_stored_retrieval_documents(
    build_id: int,
    source_id: int,
    user_id: str,
    tiers: tuple[int, ...] | None = None,
) -> tuple[StoredRetrievalDocument, ...]:
    """
    Documents for one owned, source-matching, READY build, optionally
    filtered to specific retrieval tiers. Returns () for an unowned
    source, a build_id that doesn't exist, a build_id belonging to a
    different source, or a build whose status is not READY (BUILDING/
    FAILED documents are never returned) — never leaks whether the build
    exists to a non-owner, and never exposes an in-progress or failed
    build's rows as if they were a finished result.
    """
    if tiers is not None:
        for tier in tiers:
            if tier not in _ALLOWED_TIERS:
                raise ValueError(f"invalid tier filter: {tier!r}")

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ()

        build_row = conn.execute(
            "SELECT id FROM sql_retrieval_builds WHERE id = ? AND source_id = ? AND status = 'READY'",
            (build_id, source_id),
        ).fetchone()
        if build_row is None:
            return ()

        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            query = (
                f"SELECT * FROM sql_retrieval_documents "
                f"WHERE build_id = ? AND retrieval_tier IN ({placeholders}) "
                f"ORDER BY object_fqn"
            )
            params = (build_id, *tiers)
        else:
            query = "SELECT * FROM sql_retrieval_documents WHERE build_id = ? ORDER BY object_fqn"
            params = (build_id,)

        rows = conn.execute(query, params).fetchall()
        return tuple(_row_to_stored_document(r) for r in rows)
    finally:
        conn.close()
