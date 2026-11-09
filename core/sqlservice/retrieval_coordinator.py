"""
Connection-time retrieval-build coordinator — SQL rebuild Phase 1, Step 6.

Thin orchestration layer over the three existing Phase 1 building blocks —
it does not duplicate any of their logic:

  1. core.sqlservice.metadata_loader.load_retrieval_document_batch()
     — reads the local SQLite store and reconstructs a RetrievalDocumentBatch.
  2. data.schema_fingerprint_service.compute_fingerprint()
     — the ONE structural-fingerprint algorithm; never reimplemented here.
  3. core.sqlservice.retrieval_store.persist_retrieval_build()
     — atomic, idempotent, ownership-checked persistence.

The one piece of real coordination logic this module adds is snapshot
consistency: compute_fingerprint() does not report which schema_snapshots
row it fingerprinted, so this module independently reads
data.schema_service.get_latest_snapshot() immediately before AND after
calling compute_fingerprint() and compares both reads' (id, snapshot_version)
against each other and against the batch's own (snapshot_id,
snapshot_version). Only when all three agree is the fingerprint trusted to
belong to the exact snapshot the batch's documents were built from — see
persist_retrieval_build()'s own docstring note, which this coordinator is
what fulfills: "the coordinator must derive or verify the schema
fingerprint against the exact snapshot_id and snapshot_version contained
in the loaded RetrievalDocumentBatch; it must not independently fingerprint
whichever snapshot happens to be latest." A disagreement means a newer
snapshot appeared mid-flight (e.g. discovery re-ran concurrently); the
complete load-and-fingerprint sequence is retried, with a strict maximum
of two retries (three attempts total) before giving up.

Scope: reads/writes ToolSmithAI's own local SQLite store only, via the
three functions above. Never connects to a live SQL Server, never calls
OpenAI, never computes embeddings, never runs schema discovery.
"""

import logging
from dataclasses import dataclass
from typing import Mapping

from core.sqlservice.metadata_loader import STATUS_READY as _LOADER_STATUS_READY
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, load_retrieval_document_batch
from core.sqlservice.retrieval_store import (
    OUTCOME_CREATED,
    OUTCOME_REUSED,
    OUTCOME_UNOWNED,
    PersistRetrievalBuildResult,
    persist_retrieval_build,
)
from data.schema_fingerprint_service import compute_fingerprint
from data.schema_service import get_latest_snapshot

logger = logging.getLogger(__name__)


# ── Status vocabulary ─────────────────────────────────────────────────────

STATUS_CREATED = "READY_CREATED"
STATUS_REUSED = "READY_REUSED"
STATUS_METADATA_LOAD_FAILED = "METADATA_LOAD_FAILED"
STATUS_SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
STATUS_FINGERPRINT_FAILED = "FINGERPRINT_FAILED"
STATUS_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"

# "a strict maximum of two retries" -> up to 3 total load-and-fingerprint attempts.
_MAX_ATTEMPTS = 3


# ── Public result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalBuildCoordinationResult:
    """
    Result of one build_and_persist_retrieval_documents() call. Carries
    every id/hash a caller might need for later API/status reporting, even
    on a failure path (whatever was actually known at that point).
    """

    status: str  # one of the STATUS_* constants above
    source_id: int
    snapshot_id: int | None
    snapshot_version: int | None
    fingerprint_version: int | None
    schema_fingerprint: str | None
    persist_outcome: str | None  # OUTCOME_CREATED | OUTCOME_REUSED | OUTCOME_UNOWNED, once persistence ran
    build_id: int | None
    document_count: int | None
    attempts: int
    warnings: tuple[str, ...]
    error: str | None


# ── Result constructors (keep the main loop readable) ────────────────────


def _metadata_load_failed(
    source_id: int, batch: RetrievalDocumentBatch, attempts: int,
) -> RetrievalBuildCoordinationResult:
    return RetrievalBuildCoordinationResult(
        status=STATUS_METADATA_LOAD_FAILED,
        source_id=source_id,
        snapshot_id=batch.snapshot_id,
        snapshot_version=batch.snapshot_version,
        fingerprint_version=None,
        schema_fingerprint=None,
        persist_outcome=None,
        build_id=None,
        document_count=None,
        attempts=attempts,
        warnings=batch.warnings,
        error=f"metadata loader did not reach READY (status={batch.status!r})",
    )


def _fingerprint_failed(
    source_id: int, batch: RetrievalDocumentBatch, attempts: int, message: str,
) -> RetrievalBuildCoordinationResult:
    return RetrievalBuildCoordinationResult(
        status=STATUS_FINGERPRINT_FAILED,
        source_id=source_id,
        snapshot_id=batch.snapshot_id,
        snapshot_version=batch.snapshot_version,
        fingerprint_version=None,
        schema_fingerprint=None,
        persist_outcome=None,
        build_id=None,
        document_count=None,
        attempts=attempts,
        warnings=batch.warnings,
        error=message,
    )


def _snapshot_mismatch(
    source_id: int, batch: RetrievalDocumentBatch | None, attempts: int,
) -> RetrievalBuildCoordinationResult:
    return RetrievalBuildCoordinationResult(
        status=STATUS_SNAPSHOT_MISMATCH,
        source_id=source_id,
        snapshot_id=batch.snapshot_id if batch is not None else None,
        snapshot_version=batch.snapshot_version if batch is not None else None,
        fingerprint_version=None,
        schema_fingerprint=None,
        persist_outcome=None,
        build_id=None,
        document_count=None,
        attempts=attempts,
        warnings=batch.warnings if batch is not None else (),
        error=(
            f"schema snapshot kept changing across {attempts} load-and-fingerprint "
            f"attempts; giving up rather than persisting a mismatched batch/fingerprint pair"
        ),
    )


def _persistence_failed(
    source_id: int,
    batch: RetrievalDocumentBatch,
    fingerprint: Mapping[str, object],
    attempts: int,
    message: str,
) -> RetrievalBuildCoordinationResult:
    return RetrievalBuildCoordinationResult(
        status=STATUS_PERSISTENCE_FAILED,
        source_id=source_id,
        snapshot_id=batch.snapshot_id,
        snapshot_version=batch.snapshot_version,
        fingerprint_version=fingerprint.get("fingerprint_version"),
        schema_fingerprint=fingerprint.get("fingerprint_hash"),
        persist_outcome=None,
        build_id=None,
        document_count=None,
        attempts=attempts,
        warnings=batch.warnings,
        error=message,
    )


def _from_persist_result(
    source_id: int,
    batch: RetrievalDocumentBatch,
    fingerprint: Mapping[str, object],
    persist_result: PersistRetrievalBuildResult,
    attempts: int,
) -> RetrievalBuildCoordinationResult:
    if persist_result.outcome == OUTCOME_UNOWNED:
        # Ownership was verified when the batch loaded READY; reaching
        # UNOWNED here means it changed between load and persist. Treated
        # as a persistence failure, not a silent no-op.
        return RetrievalBuildCoordinationResult(
            status=STATUS_PERSISTENCE_FAILED,
            source_id=source_id,
            snapshot_id=batch.snapshot_id,
            snapshot_version=batch.snapshot_version,
            fingerprint_version=fingerprint.get("fingerprint_version"),
            schema_fingerprint=fingerprint.get("fingerprint_hash"),
            persist_outcome=OUTCOME_UNOWNED,
            build_id=None,
            document_count=None,
            attempts=attempts,
            warnings=batch.warnings,
            error="persistence reported UNOWNED (source ownership changed since load)",
        )

    status = STATUS_CREATED if persist_result.outcome == OUTCOME_CREATED else STATUS_REUSED
    build = persist_result.build
    return RetrievalBuildCoordinationResult(
        status=status,
        source_id=source_id,
        snapshot_id=batch.snapshot_id,
        snapshot_version=batch.snapshot_version,
        fingerprint_version=fingerprint.get("fingerprint_version"),
        schema_fingerprint=fingerprint.get("fingerprint_hash"),
        persist_outcome=persist_result.outcome,
        build_id=build.id if build is not None else None,
        document_count=build.document_count if build is not None else None,
        attempts=attempts,
        warnings=batch.warnings,
        error=None,
    )


# ── Public entry point ────────────────────────────────────────────────────


def build_and_persist_retrieval_documents(
    source_id: int, user_id: str,
) -> RetrievalBuildCoordinationResult:
    """
    Load, fingerprint-verify, and persist one owned source's retrieval
    documents, end to end. See the module docstring for the snapshot-
    consistency check and retry behavior.

    Never raises for an expected failure mode (loader not READY, an
    exception from compute_fingerprint(), an exception from
    persist_retrieval_build(), or exhausting the retry budget on a moving
    snapshot) — each becomes a distinct, typed `status`. persist_
    retrieval_build() is only ever called once a fingerprint has been
    verified to belong to exactly the loaded batch's snapshot; no
    persistence call happens on any validation/consistency failure.
    """
    batch: RetrievalDocumentBatch | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        batch = load_retrieval_document_batch(source_id, user_id)
        if batch.status != _LOADER_STATUS_READY:
            return _metadata_load_failed(source_id, batch, attempt)

        try:
            pre_snapshot = get_latest_snapshot(source_id, user_id)
            fingerprint = compute_fingerprint(source_id, user_id)
            post_snapshot = get_latest_snapshot(source_id, user_id)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any fingerprinting failure is reported, never crashes the coordinator
            logger.warning(
                "build_and_persist_retrieval_documents: fingerprinting failed for "
                "source_id=%s attempt=%s", source_id, attempt, exc_info=True,
            )
            return _fingerprint_failed(source_id, batch, attempt, f"{type(exc).__name__}: {exc}")

        if fingerprint is None or pre_snapshot is None or post_snapshot is None:
            return _fingerprint_failed(
                source_id, batch, attempt, "no schema snapshot available to fingerprint",
            )

        # The fingerprint must belong to exactly the snapshot the batch's
        # documents were built from — never combine documents from one
        # snapshot with a fingerprint from another. Bracketing
        # compute_fingerprint() with a get_latest_snapshot() read on each
        # side detects a snapshot that changed mid-fingerprint (pre != post);
        # comparing pre against the batch's own snapshot_id/snapshot_version
        # detects one that changed between the batch load and fingerprinting.
        fingerprint_snapshot_stable = (
            pre_snapshot["id"] == post_snapshot["id"]
            and pre_snapshot["snapshot_version"] == post_snapshot["snapshot_version"]
        )
        fingerprint_matches_batch = (
            pre_snapshot["id"] == batch.snapshot_id
            and pre_snapshot["snapshot_version"] == batch.snapshot_version
        )

        if not (fingerprint_snapshot_stable and fingerprint_matches_batch):
            logger.info(
                "build_and_persist_retrieval_documents: snapshot changed mid-flight for "
                "source_id=%s attempt=%s; retrying", source_id, attempt,
            )
            continue

        try:
            persist_result = persist_retrieval_build(batch, fingerprint, user_id)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any persistence failure is reported, never crashes the coordinator
            logger.warning(
                "build_and_persist_retrieval_documents: persistence failed for "
                "source_id=%s attempt=%s", source_id, attempt, exc_info=True,
            )
            return _persistence_failed(source_id, batch, fingerprint, attempt, f"{type(exc).__name__}: {exc}")

        return _from_persist_result(source_id, batch, fingerprint, persist_result, attempt)

    return _snapshot_mismatch(source_id, batch, _MAX_ATTEMPTS)
