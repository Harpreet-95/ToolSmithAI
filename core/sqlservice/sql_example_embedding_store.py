"""
Content-addressed embedding cache for SQL-example search documents — SQL
rebuild Phase 5, Step 9.

Structurally mirrors core/sqlservice/embedding_store.py exactly (same
validation, same float32 encoding via serialize_vector/deserialize_vector
reused from there unchanged — no duplicated codec), but keyed against
`sql_example_embeddings` / `sql_examples` (source_id-scoped, not
build_id-scoped — an example survives across schema rebuilds as long as
compatibility checks pass at retrieval time; see
core/sqlservice/sql_example_retrieval.py) rather than
sql_retrieval_embeddings / sql_retrieval_documents.

Scope: reads/writes ToolSmithAI's own local SQLite store only. Never calls
an embedding provider, never generates a real embedding, never performs
vector similarity search.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from core.sqlservice.embedding_store import deserialize_vector, serialize_vector
from data.db import get_connection

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "StoredExampleEmbedding",
    "ExampleEmbeddingInsertResult",
    "insert_example_embeddings",
    "get_example_embeddings_for_source",
    "find_missing_example_search_document_hashes",
]

STATUS_OK = "OK"
STATUS_UNOWNED = "UNOWNED"

_HEX64_RE_LEN = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _validate_hash(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str) or len(value) != _HEX64_RE_LEN
        or not all(c in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{field_name} is malformed: {value!r}")


def _verify_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class StoredExampleEmbedding:
    id: int
    example_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    search_document_hash: str
    vector: tuple[float, ...]
    created_at: str


@dataclass(frozen=True)
class ExampleEmbeddingInsertResult:
    status: str  # STATUS_OK | STATUS_UNOWNED
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    inserted_hashes: tuple[str, ...]
    reused_hashes: tuple[str, ...]


def insert_example_embeddings(
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    vectors_by_search_document_hash: Mapping[str, Sequence[float]],
) -> ExampleEmbeddingInsertResult:
    """
    Atomically cache one or more vectors, keyed by (embedding_provider,
    embedding_model, embedding_dimensions, search_document_hash) —
    independent of which specific example row produced that hash, so two
    examples that happen to normalize to byte-identical search-document
    text reuse the same cached vector rather than duplicating it.

    Ownership of `source_id` is verified (STATUS_UNOWNED otherwise, never
    raised). Every hash must already belong to an existing example for
    this source (an unknown hash raises ValueError). A hash already cached
    under this identity with a DIFFERENT vector raises ValueError
    (conflicting vector) — an identical byte-for-byte reinsert is reused,
    not duplicated. Single transaction: any failure rolls back the whole
    batch.
    """
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)
    if not vectors_by_search_document_hash:
        raise ValueError("vectors_by_search_document_hash is empty")

    encoded: dict[str, bytes] = {}
    for doc_hash, values in vectors_by_search_document_hash.items():
        _validate_hash(doc_hash, "search_document_hash")
        try:
            length = len(values)
        except TypeError as exc:
            raise ValueError(f"vector for {doc_hash!r} must be a sized sequence of numbers") from exc
        if length != embedding_dimensions:
            raise ValueError(f"vector for {doc_hash!r} has {length} dimensions, expected {embedding_dimensions}")
        encoded[doc_hash] = serialize_vector(values)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ExampleEmbeddingInsertResult(
                status=STATUS_UNOWNED, embedding_provider=embedding_provider,
                embedding_model=embedding_model, embedding_dimensions=embedding_dimensions,
                inserted_hashes=(), reused_hashes=(),
            )

        known_hashes = {
            row["search_document_hash"]
            for row in conn.execute(
                "SELECT DISTINCT search_document_hash FROM sql_examples WHERE source_id = ?", (source_id,),
            ).fetchall()
        }
        unknown = sorted(set(encoded) - known_hashes)
        if unknown:
            raise ValueError(f"unknown search_document_hash(es) for source {source_id}: {unknown}")

        inserted: list[str] = []
        reused: list[str] = []
        try:
            for doc_hash, blob in encoded.items():
                existing = conn.execute(
                    "SELECT vector_blob FROM sql_example_embeddings "
                    "WHERE embedding_provider = ? AND embedding_model = ? "
                    "AND embedding_dimensions = ? AND search_document_hash = ?",
                    (embedding_provider, embedding_model, embedding_dimensions, doc_hash),
                ).fetchone()
                if existing is not None:
                    if bytes(existing["vector_blob"]) == blob:
                        reused.append(doc_hash)
                        continue
                    raise ValueError(
                        f"conflicting vector already cached for search_document_hash={doc_hash!r} "
                        f"under provider={embedding_provider!r} model={embedding_model!r} "
                        f"dimensions={embedding_dimensions}"
                    )
                conn.execute(
                    "INSERT INTO sql_example_embeddings "
                    "(embedding_provider, embedding_model, embedding_dimensions, search_document_hash, "
                    " vector_blob, vector_byte_length, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (embedding_provider, embedding_model, embedding_dimensions, doc_hash, blob, len(blob), _now()),
                )
                inserted.append(doc_hash)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return ExampleEmbeddingInsertResult(
            status=STATUS_OK, embedding_provider=embedding_provider, embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            inserted_hashes=tuple(sorted(inserted)), reused_hashes=tuple(sorted(reused)),
        )
    finally:
        conn.close()


def get_example_embeddings_for_source(
    source_id: int, user_id: str, embedding_provider: str, embedding_model: str, embedding_dimensions: int,
) -> tuple[StoredExampleEmbedding, ...]:
    """Cached vectors for one owned source's examples under one exact
    (provider, model, dimensions) combination, ordered by example id. An
    example with no cached vector is simply absent — never an error."""
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ()
        rows = conn.execute(
            "SELECT ee.id, se.id AS example_id, ee.embedding_provider, ee.embedding_model, "
            "ee.embedding_dimensions, ee.search_document_hash, ee.vector_blob, ee.created_at "
            "FROM sql_examples se "
            "JOIN sql_example_embeddings ee ON ee.search_document_hash = se.search_document_hash "
            "WHERE se.source_id = ? AND ee.embedding_provider = ? AND ee.embedding_model = ? "
            "AND ee.embedding_dimensions = ? "
            "ORDER BY se.id",
            (source_id, embedding_provider, embedding_model, embedding_dimensions),
        ).fetchall()
        return tuple(
            StoredExampleEmbedding(
                id=r["id"], example_id=r["example_id"], embedding_provider=r["embedding_provider"],
                embedding_model=r["embedding_model"], embedding_dimensions=r["embedding_dimensions"],
                search_document_hash=r["search_document_hash"], vector=deserialize_vector(r["vector_blob"]),
                created_at=r["created_at"],
            )
            for r in rows
        )
    finally:
        conn.close()


def find_missing_example_search_document_hashes(
    source_id: int, user_id: str, embedding_provider: str, embedding_model: str, embedding_dimensions: int,
) -> tuple[str, ...] | None:
    """Sorted search_document_hash values for this source's examples that
    have no cached vector under this exact identity. None (not an empty
    tuple) for an unowned source."""
    _validate_identity(embedding_provider, embedding_model, embedding_dimensions)

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        rows = conn.execute(
            "SELECT DISTINCT se.search_document_hash FROM sql_examples se "
            "LEFT JOIN sql_example_embeddings ee "
            "  ON ee.search_document_hash = se.search_document_hash "
            "  AND ee.embedding_provider = ? AND ee.embedding_model = ? AND ee.embedding_dimensions = ? "
            "WHERE se.source_id = ? AND ee.id IS NULL",
            (embedding_provider, embedding_model, embedding_dimensions, source_id),
        ).fetchall()
        return tuple(sorted(r["search_document_hash"] for r in rows))
    finally:
        conn.close()
