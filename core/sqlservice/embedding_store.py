"""
Provider-independent embedding cache — SQL rebuild Phase 2, Step 1.

Content-addressed local SQLite storage for schema-document embeddings:
`sql_retrieval_embeddings` (see data/models.py and
migrations/013_create_embedding_cache.sql, which define identical schemas).
The cache key is (embedding_provider, embedding_model, embedding_dimensions,
document_hash) — deliberately NOT a build_id or document id — so an
unchanged document (byte-identical document_text, hence the same
document_hash from core/sqlservice/retrieval_documents.py) reuses its
cached vector across every retrieval build and source that produces that
same hash, instead of recomputing or duplicating it.

Scope: reads/writes ToolSmithAI's own local SQLite store only. Never calls
OpenAI or any other embedding provider, never generates a real embedding,
and never performs vector similarity search — this module only validates,
encodes, and persists vectors a caller already computed elsewhere, and
reports which documents in a build still need one.
"""

import array
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from data.db import get_connection

# ── Status vocabulary (build-gate result, not embedding completeness) ────

STATUS_OK = "OK"
STATUS_UNOWNED = "UNOWNED"
STATUS_BUILD_NOT_FOUND = "BUILD_NOT_FOUND"
STATUS_BUILD_NOT_READY = "BUILD_NOT_READY"

_FLOAT32_SIZE = 4
_HEX64_RE_LEN = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredEmbedding:
    """One cached vector, read back from sql_retrieval_embeddings, joined
    to the object_fqn of a document in the requested build that shares its
    document_hash."""

    id: int
    object_fqn: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    document_hash: str
    vector: tuple[float, ...]
    created_at: str


@dataclass(frozen=True)
class EmbeddingInsertResult:
    """Result of one insert_embeddings() call."""

    status: str  # STATUS_OK | STATUS_UNOWNED | STATUS_BUILD_NOT_FOUND | STATUS_BUILD_NOT_READY
    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    inserted_hashes: tuple[str, ...]  # sorted; newly written this call
    reused_hashes: tuple[str, ...]  # sorted; already cached, byte-identical


@dataclass(frozen=True)
class EmbeddingBuildCompleteness:
    """Result of one get_embedding_build_completeness() call."""

    status: str  # STATUS_OK | STATUS_UNOWNED | STATUS_BUILD_NOT_FOUND | STATUS_BUILD_NOT_READY
    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    total_documents: int
    embedded_document_count: int
    missing_document_hashes: tuple[str, ...]  # sorted
    is_complete: bool


# ── Vector encoding (compact float32 binary, never JSON) ─────────────────


def serialize_vector(values: Sequence[float]) -> bytes:
    """
    Validate and pack a vector as compact float32 little-endian binary
    (4 bytes/dimension, independent of the host platform's native byte
    order). Raises ValueError — never silently drops, truncates, or
    coerces a bad value — for:
      - an empty vector
      - an element that cannot be converted to float
      - a non-finite element (NaN or +/-Infinity)
    """
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("vector values must be a sequence of numbers, not a string/bytes")
    try:
        length = len(values)
    except TypeError as exc:
        raise ValueError("vector values must be a sized sequence of numbers") from exc
    if length == 0:
        raise ValueError("vector must not be empty")

    floats: list[float] = []
    for i, raw in enumerate(values):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"vector element {i} is not a number: {raw!r}") from exc
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"vector element {i} is not finite: {value!r}")
        floats.append(value)

    packed = array.array("f", floats)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def deserialize_vector(blob: bytes) -> tuple[float, ...]:
    """
    Inverse of serialize_vector(). Raises ValueError when `blob` is not
    bytes-like or its length is not a positive multiple of 4 (the float32
    element size) — e.g. a truncated or corrupted BLOB.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise ValueError("vector blob must be bytes")
    if len(blob) == 0 or len(blob) % _FLOAT32_SIZE != 0:
        raise ValueError(
            f"vector blob length {len(blob)} is not a positive multiple of {_FLOAT32_SIZE}"
        )

    packed = array.array("f")
    packed.frombytes(bytes(blob))
    if sys.byteorder != "little":
        packed.byteswap()
    return tuple(packed)


# ── Validation (identity + hash shape) ────────────────────────────────────


def _validate_identity(embedding_provider: str, embedding_model: str, embedding_dimensions: int) -> None:
    if not isinstance(embedding_provider, str) or not embedding_provider.strip():
        raise ValueError("embedding_provider must be a non-empty string")
    if not isinstance(embedding_model, str) or not embedding_model.strip():
        raise ValueError("embedding_model must be a non-empty string")
    if (
        not isinstance(embedding_dimensions, int)
        or isinstance(embedding_dimensions, bool)
        or embedding_dimensions <= 0
    ):
        raise ValueError("embedding_dimensions must be a positive integer")


def _validate_document_hash(document_hash: str) -> None:
    if (
        not isinstance(document_hash, str)
        or len(document_hash) != _HEX64_RE_LEN
        or not all(c in "0123456789abcdef" for c in document_hash)
    ):
        raise ValueError(f"document_hash is malformed: {document_hash!r}")


# ── Ownership / build-gate verification ───────────────────────────────────


def _verify_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _resolve_build_gate(conn, build_id: int, source_id: int, user_id: str) -> str:
    """
    Every public entry point in this module funnels through here first:
    STATUS_OK only when the source is owned by user_id AND build_id exists
    under that exact source_id AND its status is READY. Never leaks
    whether a build exists to a non-owner (returns STATUS_UNOWNED before
    even looking at build_id), and never treats a BUILDING/FAILED build's
    documents as a finished result to embed against.
    """
    if not _verify_ownership(conn, source_id, user_id):
        return STATUS_UNOWNED
    row = conn.execute(
        "SELECT status FROM sql_retrieval_builds WHERE id = ? AND source_id = ?",
        (build_id, source_id),
    ).fetchone()
    if row is None:
        return STATUS_BUILD_NOT_FOUND
    if row["status"] != "READY":
        return STATUS_BUILD_NOT_READY
    return STATUS_OK


# ── Row <-> dataclass mapping ──────────────────────────────────────────────


def _row_to_stored_embedding(row) -> StoredEmbedding:
    return StoredEmbedding(
        id=row["id"],
        object_fqn=row["object_fqn"],
        embedding_provider=row["embedding_provider"],
        embedding_model=row["embedding_model"],
        embedding_dimensions=row["embedding_dimensions"],
        document_hash=row["document_hash"],
        vector=deserialize_vector(row["vector_blob"]),
        created_at=row["created_at"],
    )


# ── Missing-hash computation (shared by the two read entry points) ───────


def _missing_document_hashes(
    conn, build_id: int, embedding_provider: str, embedding_model: str, embedding_dimensions: int,
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT DISTINCT d.document_hash FROM sql_retrieval_documents d "
        "LEFT JOIN sql_retrieval_embeddings e "
        "  ON e.document_hash = d.document_hash "
        "  AND e.embedding_provider = ? AND e.embedding_model = ? AND e.embedding_dimensions = ? "
        "WHERE d.build_id = ? AND e.id IS NULL",
        (embedding_provider, embedding_model, embedding_dimensions, build_id),
    ).fetchall()
    return tuple(sorted(r["document_hash"] for r in rows))


# ── Public entry points ────────────────────────────────────────────────────


def insert_embeddings(
    build_id: int,
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    vectors_by_document_hash: Mapping[str, Sequence[float]],
) -> EmbeddingInsertResult:
    """
    Atomically cache one or more vectors for documents belonging to one
    owned, READY build. Reuse and conflict are resolved per document_hash
    against the (embedding_provider, embedding_model, embedding_dimensions,
    document_hash) cache key — independent of build_id, so a hash already
    cached via a different build or source is recognized as a reuse here
    too, never recomputed or duplicated.

    Validation (identity shape, non-empty batch, hash format, dimension
    match, non-finite values) happens before any database access and
    raises ValueError. Ownership/build-state failures are reported via
    `status` instead — a caller racing against build/source deletion is a
    routine condition, not a caller bug.

    Once the build gate passes, every document_hash in the batch must
    belong to this exact build's document set; an unknown hash raises
    ValueError (a caller bug — never silently ignored). A document_hash
    already cached under this exact provider/model/dimensions with a
    DIFFERENT vector raises ValueError (conflicting vector); an identical
    byte-for-byte reinsert is recorded as reused instead of inserted.

    Atomicity: this call opens exactly one transaction. Any failure partway
    through the batch (an unknown hash, a conflicting vector, or a
    database error) rolls back every row this call would have inserted —
    never a partial write.
    """
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)
    if not vectors_by_document_hash:
        raise ValueError("vectors_by_document_hash is empty")

    encoded: dict[str, bytes] = {}
    for document_hash, values in vectors_by_document_hash.items():
        _validate_document_hash(document_hash)
        try:
            length = len(values)
        except TypeError as exc:
            raise ValueError(f"vector for {document_hash!r} must be a sized sequence of numbers") from exc
        if length != embedding_dimensions:
            raise ValueError(
                f"vector for {document_hash!r} has {length} dimensions, expected {embedding_dimensions}"
            )
        encoded[document_hash] = serialize_vector(values)

    conn = get_connection()
    try:
        gate = _resolve_build_gate(conn, build_id, source_id, user_id)
        if gate != STATUS_OK:
            return EmbeddingInsertResult(
                status=gate,
                build_id=build_id,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                inserted_hashes=(),
                reused_hashes=(),
            )

        known_hashes = {
            row["document_hash"]
            for row in conn.execute(
                "SELECT DISTINCT document_hash FROM sql_retrieval_documents WHERE build_id = ?",
                (build_id,),
            ).fetchall()
        }
        unknown = sorted(set(encoded) - known_hashes)
        if unknown:
            raise ValueError(
                f"unknown document hash(es) for build {build_id}: {unknown}"
            )

        inserted: list[str] = []
        reused: list[str] = []
        try:
            for document_hash, blob in encoded.items():
                existing = conn.execute(
                    "SELECT vector_blob FROM sql_retrieval_embeddings "
                    "WHERE embedding_provider = ? AND embedding_model = ? "
                    "AND embedding_dimensions = ? AND document_hash = ?",
                    (embedding_provider, embedding_model, embedding_dimensions, document_hash),
                ).fetchone()
                if existing is not None:
                    if bytes(existing["vector_blob"]) == blob:
                        reused.append(document_hash)
                        continue
                    raise ValueError(
                        f"conflicting vector already cached for document_hash={document_hash!r} "
                        f"under provider={embedding_provider!r} model={embedding_model!r} "
                        f"dimensions={embedding_dimensions}"
                    )
                conn.execute(
                    "INSERT INTO sql_retrieval_embeddings "
                    "(embedding_provider, embedding_model, embedding_dimensions, document_hash, "
                    " vector_blob, vector_byte_length, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        embedding_provider, embedding_model, embedding_dimensions,
                        document_hash, blob, len(blob), _now(),
                    ),
                )
                inserted.append(document_hash)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return EmbeddingInsertResult(
            status=STATUS_OK,
            build_id=build_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            inserted_hashes=tuple(sorted(inserted)),
            reused_hashes=tuple(sorted(reused)),
        )
    finally:
        conn.close()


def get_embeddings_for_build(
    build_id: int,
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> tuple[StoredEmbedding, ...]:
    """
    Cached vectors for one owned, READY build's documents under one exact
    (provider, model, dimensions) combination, ordered deterministically by
    object_fqn (matching load_stored_retrieval_documents()'s convention).
    A document with no cached vector for this combination is simply absent
    from the result — never an error; use find_missing_document_hashes()
    or get_embedding_build_completeness() to detect that. Returns () for an
    unowned source, a build_id that doesn't exist or belongs to a
    different source, or a build whose status is not READY.
    """
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)

    conn = get_connection()
    try:
        if _resolve_build_gate(conn, build_id, source_id, user_id) != STATUS_OK:
            return ()

        rows = conn.execute(
            "SELECT e.id, d.object_fqn, e.embedding_provider, e.embedding_model, "
            "e.embedding_dimensions, e.document_hash, e.vector_blob, e.created_at "
            "FROM sql_retrieval_documents d "
            "JOIN sql_retrieval_embeddings e ON e.document_hash = d.document_hash "
            "WHERE d.build_id = ? AND e.embedding_provider = ? AND e.embedding_model = ? "
            "AND e.embedding_dimensions = ? "
            "ORDER BY d.object_fqn",
            (build_id, embedding_provider, embedding_model, embedding_dimensions),
        ).fetchall()
        return tuple(_row_to_stored_embedding(r) for r in rows)
    finally:
        conn.close()


def find_missing_document_hashes(
    build_id: int,
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> tuple[str, ...] | None:
    """
    Sorted document_hash values for documents in one owned, READY build
    that have no cached vector under this exact (provider, model,
    dimensions) combination — empty tuple when every document already has
    one. Returns None (not an empty tuple, which would misreport a
    non-existent build as fully embedded) for an unowned source, a
    build_id that doesn't exist or belongs to a different source, or a
    build whose status is not READY.
    """
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)

    conn = get_connection()
    try:
        if _resolve_build_gate(conn, build_id, source_id, user_id) != STATUS_OK:
            return None
        return _missing_document_hashes(
            conn, build_id, embedding_provider, embedding_model, embedding_dimensions,
        )
    finally:
        conn.close()


def get_embedding_build_completeness(
    build_id: int,
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> EmbeddingBuildCompleteness:
    """
    Whether every document in one owned, READY build has a cached vector
    under this exact (provider, model, dimensions) combination, plus the
    counts and missing-hash list behind that verdict. Always returns an
    EmbeddingBuildCompleteness — never None — so `status` alone tells a
    caller whether the counts are meaningful: STATUS_UNOWNED,
    STATUS_BUILD_NOT_FOUND, or STATUS_BUILD_NOT_READY report zero counts,
    an empty missing-hash tuple, and is_complete=False (never a false
    all-complete verdict for a build that was never actually checked).
    """
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)

    conn = get_connection()
    try:
        gate = _resolve_build_gate(conn, build_id, source_id, user_id)
        if gate != STATUS_OK:
            return EmbeddingBuildCompleteness(
                status=gate,
                build_id=build_id,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                total_documents=0,
                embedded_document_count=0,
                missing_document_hashes=(),
                is_complete=False,
            )

        total = conn.execute(
            "SELECT COUNT(DISTINCT document_hash) FROM sql_retrieval_documents WHERE build_id = ?",
            (build_id,),
        ).fetchone()[0]
        missing = _missing_document_hashes(
            conn, build_id, embedding_provider, embedding_model, embedding_dimensions,
        )

        return EmbeddingBuildCompleteness(
            status=STATUS_OK,
            build_id=build_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            total_documents=total,
            embedded_document_count=total - len(missing),
            missing_document_hashes=missing,
            is_complete=(len(missing) == 0),
        )
    finally:
        conn.close()
