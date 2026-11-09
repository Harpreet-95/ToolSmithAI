"""
Transactional SQLite persistence for dynamic few-shot SQL examples — SQL
rebuild Phase 5, Steps 9/9A/9B.

Adapter between an already-validated candidate and ToolSmithAI's local
SQLite metadata store: `sql_examples` (see data/models.py and
migrations/014_create_sql_examples.sql, which define identical schemas).

Step 9B persistence trust enforcement — TWO separate, explicitly named
public entry points, never one general-purpose insert function a caller
could misuse:

  - insert_trusted_example(user_id, candidate) is the ONLY way to persist
    a row that can ever be trust_level=TRUSTED. `candidate` must be a real
    core.sqlservice.sql_example_candidate.TrustedSqlExampleCandidate —
    that class cannot itself be hand-constructed (see its own
    _gate_guard/__post_init__), so `isinstance(candidate,
    TrustedSqlExampleCandidate)` here is a real, not merely conventional,
    proof it came from build_trusted_example_candidate() and therefore
    already cleared every structural/read-only/referenced-object/
    normalization/provenance gate that function enforces. Every persisted
    field is read directly off `candidate` — there is no keyword-argument
    form of this function a caller could use to substitute a different
    trust_level, provenance, sql_template, or canonical_object_fqns than
    what the candidate itself carries.
  - insert_untrusted_observation(user_id, ...) is a SEPARATE, explicitly
    named API for an OBSERVED_UNVERIFIED record. provenance and
    trust_level are never parameters here — always hardcoded internally
    to PROVENANCE_OBSERVED_UNVERIFIED / TRUST_LEVEL_UNTRUSTED, so no
    caller can pass a trust boolean or provenance string through this path
    either. Recorded for audit/observability only: core.sqlservice.
    sql_example_vector_index.load_sql_example_vector_index() only ever
    indexes TRUST_LEVEL_TRUSTED rows, so a row inserted here can never be
    embedded, searched, or reach core.sqlservice.sql_generation_service's
    prompt, regardless of its `status` — structurally unretrievable, not
    merely by convention.

Neither function computes trust_level itself — that is always
core.sqlservice.sql_example_model.classify_trust_level's decision (via
core.sqlservice.sql_example_candidate.build_trusted_example_candidate for
the trusted path), made BEFORE a candidate ever reaches this module.

Scope: reads/writes ToolSmithAI's own local SQLite store only. Never
connects to a live SQL Server, never calls an embedding provider.

Reuse/idempotency: inserting is idempotent per (source_id, content_hash) —
a byte-identical re-submission returns the existing row (outcome=REUSED)
instead of inserting a duplicate. Content immutability: once persisted, an
example's own hashed identity fields (question_pattern, sql_template,
canonical_object_fqns, provenance, trust_level, ...) can never be changed
by update_example_status() — only `status` (ACTIVE/STALE) may be updated,
and even that is a targeted, single-column write, never a general row
update; it can never promote an untrusted record to trusted (there is no
trust_level parameter on that function at all).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from core.sqlservice.sql_example_candidate import TrustedSqlExampleCandidate
from core.sqlservice.sql_example_model import (
    ALL_PROVENANCES,
    ALL_RESULT_SHAPES,
    PROVENANCE_OBSERVED_UNVERIFIED,
    STATUS_ACTIVE,
    STATUS_STALE,
    TRUST_LEVEL_TRUSTED,
    TRUST_LEVEL_UNTRUSTED,
    SqlExample,
)
from data.db import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "OUTCOME_CREATED",
    "OUTCOME_REUSED",
    "OUTCOME_UNOWNED",
    "InsertExampleResult",
    "insert_trusted_example",
    "insert_untrusted_observation",
    "get_example",
    "list_examples_for_source",
    "update_example_status",
]

OUTCOME_CREATED = "CREATED"
OUTCOME_REUSED = "REUSED"
OUTCOME_UNOWNED = "UNOWNED"

_HEX64_RE_LEN = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == _HEX64_RE_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class InsertExampleResult:
    """Result of one insert_trusted_example() or insert_untrusted_observation() call."""

    outcome: str  # OUTCOME_CREATED | OUTCOME_REUSED | OUTCOME_UNOWNED
    example: SqlExample | None


# ── Validation (pure, no I/O — raises ValueError, never writes) ──────────


def _validate_candidate(
    *,
    source_id: int,
    schema_fingerprint: str,
    question_pattern: str,
    sql_template: str,
    canonical_object_fqns: Sequence[str],
    canonical_columns: Sequence[str],
    join_path: Sequence[str],
    result_shape: str,
    provenance: str,
    trust_level: str,
    search_document_text: str,
    search_document_hash: str,
    content_hash: str,
) -> None:
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    if not isinstance(schema_fingerprint, str) or not schema_fingerprint.strip():
        raise ValueError("schema_fingerprint must be a non-empty string")
    if not isinstance(question_pattern, str) or not question_pattern.strip():
        raise ValueError("question_pattern must be a non-empty string")
    if not isinstance(sql_template, str) or not sql_template.strip():
        raise ValueError("sql_template must be a non-empty string")
    if result_shape not in ALL_RESULT_SHAPES:
        raise ValueError(f"result_shape must be one of {sorted(ALL_RESULT_SHAPES)}, got {result_shape!r}")
    if provenance not in ALL_PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(ALL_PROVENANCES)}, got {provenance!r}")
    if trust_level not in (TRUST_LEVEL_TRUSTED, TRUST_LEVEL_UNTRUSTED):
        raise ValueError(f"trust_level must be TRUSTED or UNTRUSTED, got {trust_level!r}")
    if not isinstance(search_document_text, str) or not search_document_text.strip():
        raise ValueError("search_document_text must be a non-empty string")
    if not _is_hex64(search_document_hash):
        raise ValueError(f"search_document_hash is malformed: {search_document_hash!r}")
    if not _is_hex64(content_hash):
        raise ValueError(f"content_hash is malformed: {content_hash!r}")
    for label, values in (
        ("canonical_object_fqns", canonical_object_fqns),
        ("canonical_columns", canonical_columns),
        ("join_path", join_path),
    ):
        if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{label} must be a list/tuple of strings")


# ── Ownership / row <-> dataclass mapping ─────────────────────────────────


def _verify_ownership(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _row_to_example(row) -> SqlExample:
    return SqlExample(
        id=row["id"],
        source_id=row["source_id"],
        schema_fingerprint=row["schema_fingerprint"],
        question_pattern=row["question_pattern"],
        sql_template=row["sql_template"],
        canonical_object_fqns=tuple(json.loads(row["canonical_object_fqns_json"])),
        canonical_columns=tuple(json.loads(row["canonical_columns_json"])),
        join_path=tuple(json.loads(row["join_path_json"])),
        result_shape=row["result_shape"],
        provenance=row["provenance"],
        trust_level=row["trust_level"],
        search_document_text=row["search_document_text"],
        search_document_hash=row["search_document_hash"],
        content_hash=row["content_hash"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Shared low-level insert (private — see the two public entry points
#    below; NEITHER accepts trust_level/provenance as pass-through kwargs
#    from an external caller, so this function's own signature accepting
#    them is never itself a caller-facing bypass) ──────────────────────────


def _insert_row(
    user_id: str,
    *,
    source_id: int,
    schema_fingerprint: str,
    question_pattern: str,
    sql_template: str,
    canonical_object_fqns: Sequence[str],
    canonical_columns: Sequence[str],
    join_path: Sequence[str],
    result_shape: str,
    provenance: str,
    trust_level: str,
    search_document_text: str,
    search_document_hash: str,
    content_hash: str,
) -> InsertExampleResult:
    """
    Persist one already-normalized, already-trust-classified example row.
    Private — never exported, never imported outside this module. Called
    ONLY by insert_trusted_example() and insert_untrusted_observation()
    below, each of which derives/hardcodes `provenance`/`trust_level`
    itself rather than accepting them from an external caller — this
    function's own willingness to persist whatever it's given is never,
    by construction, reachable with a forged trust_level or provenance
    from outside core.sqlservice.sql_example_store.

    Ownership is checked against the database and reported as
    outcome=UNOWNED (not raised) — a routine, expected condition. Shape
    validation happens before any database access and raises ValueError.

    Idempotent: an existing row with the same (source_id, content_hash) is
    returned unchanged as outcome=REUSED — no new row, no update to it
    (content-hash-identified rows are immutable by construction; only
    update_example_status() may ever modify an existing row, and only its
    `status` column — never trust_level or provenance).
    """
    _validate_candidate(
        source_id=source_id, schema_fingerprint=schema_fingerprint, question_pattern=question_pattern,
        sql_template=sql_template, canonical_object_fqns=canonical_object_fqns,
        canonical_columns=canonical_columns, join_path=join_path, result_shape=result_shape,
        provenance=provenance, trust_level=trust_level, search_document_text=search_document_text,
        search_document_hash=search_document_hash, content_hash=content_hash,
    )

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return InsertExampleResult(outcome=OUTCOME_UNOWNED, example=None)

        existing = conn.execute(
            "SELECT * FROM sql_examples WHERE source_id = ? AND content_hash = ?",
            (source_id, content_hash),
        ).fetchone()
        if existing is not None:
            return InsertExampleResult(outcome=OUTCOME_REUSED, example=_row_to_example(existing))

        now = _now()
        try:
            cursor = conn.execute(
                "INSERT INTO sql_examples "
                "(source_id, schema_fingerprint, question_pattern, sql_template, "
                " canonical_object_fqns_json, canonical_columns_json, join_path_json, "
                " result_shape, provenance, trust_level, search_document_text, "
                " search_document_hash, content_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id, schema_fingerprint, question_pattern, sql_template,
                    json.dumps(list(canonical_object_fqns)), json.dumps(list(canonical_columns)),
                    json.dumps(list(join_path)), result_shape, provenance, trust_level,
                    search_document_text, search_document_hash, content_hash,
                    STATUS_ACTIVE, now, now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        row = conn.execute("SELECT * FROM sql_examples WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return InsertExampleResult(outcome=OUTCOME_CREATED, example=_row_to_example(row))
    finally:
        conn.close()


# ── Public entry points (Step 9B: two separate, explicitly named APIs —
#    never one general-purpose insert a caller could misuse) ──────────────


def insert_trusted_example(user_id: str, candidate: TrustedSqlExampleCandidate) -> InsertExampleResult:
    """
    THE ONLY way to persist a row that can ever be trust_level=TRUSTED.

    `candidate` must be a real core.sqlservice.sql_example_candidate.
    TrustedSqlExampleCandidate produced by that module's own
    build_trusted_example_candidate() — that class rejects direct
    construction from outside its own module (see its `_gate_guard`
    field), so the isinstance() check below is a real guarantee, not a
    convention, that every field persisted here already cleared
    build_trusted_example_candidate's full structural/read-only/
    referenced-object/normalization/provenance gate. Every persisted field
    is read directly off `candidate` — this function has no keyword
    parameters a caller could use to substitute a different trust_level,
    provenance, sql_template, or canonical_object_fqns.

    Raises ValueError (never silently downgrades or proceeds) for:
      - `candidate` not being a real TrustedSqlExampleCandidate instance
        (a dict, a plain object, a subclass built some other way — none of
        that is ever accepted as "close enough").
      - `candidate.trust_level` not being TRUST_LEVEL_TRUSTED — a defense-
        in-depth check that should be structurally unreachable (
        build_trusted_example_candidate() never returns an untrusted
        candidate as of Step 9B), kept here anyway so this function's own
        name stays a real guarantee even against a future bug upstream.
        Use insert_untrusted_observation() for an OBSERVED_UNVERIFIED
        record instead.
    """
    if not isinstance(candidate, TrustedSqlExampleCandidate):
        raise ValueError(
            "candidate must be a real core.sqlservice.sql_example_candidate."
            "TrustedSqlExampleCandidate produced by build_trusted_example_candidate() "
            "— a raw dict/kwargs payload is never accepted here"
        )
    if candidate.trust_level != TRUST_LEVEL_TRUSTED:
        raise ValueError(
            f"candidate.trust_level={candidate.trust_level!r} is not TRUSTED — "
            "insert_trusted_example() never persists an untrusted candidate; "
            "use insert_untrusted_observation() for an OBSERVED_UNVERIFIED record"
        )

    return _insert_row(
        user_id,
        source_id=candidate.source_id, schema_fingerprint=candidate.schema_fingerprint,
        question_pattern=candidate.question_pattern, sql_template=candidate.sql_template,
        canonical_object_fqns=candidate.canonical_object_fqns, canonical_columns=candidate.canonical_columns,
        join_path=candidate.join_path, result_shape=candidate.result_shape, provenance=candidate.provenance,
        trust_level=candidate.trust_level, search_document_text=candidate.search_document_text,
        search_document_hash=candidate.search_document_hash, content_hash=candidate.content_hash,
    )


def insert_untrusted_observation(
    user_id: str,
    *,
    source_id: int,
    schema_fingerprint: str,
    question_pattern: str,
    sql_template: str,
    canonical_object_fqns: Sequence[str],
    result_shape: str,
    search_document_text: str,
    search_document_hash: str,
    content_hash: str,
    canonical_columns: Sequence[str] = (),
    join_path: Sequence[str] = (),
) -> InsertExampleResult:
    """
    Separate, explicit API for an unverified observation. `provenance` and
    `trust_level` are NEVER parameters here — always hardcoded internally
    to PROVENANCE_OBSERVED_UNVERIFIED / TRUST_LEVEL_UNTRUSTED, so no
    caller can pass a trust boolean or a provenance string through this
    path to promote a record. Recorded for audit/observability only:
    core.sqlservice.sql_example_vector_index.load_sql_example_vector_index()
    only ever indexes TRUST_LEVEL_TRUSTED rows, so a row inserted here can
    never be embedded, searched, or reach core.sqlservice.
    sql_generation_service's prompt — regardless of its `status`, and
    regardless of anything a later update_example_status() call does to
    it (that function only ever touches `status`, never trust_level).
    """
    return _insert_row(
        user_id,
        source_id=source_id, schema_fingerprint=schema_fingerprint,
        question_pattern=question_pattern, sql_template=sql_template,
        canonical_object_fqns=canonical_object_fqns, canonical_columns=canonical_columns, join_path=join_path,
        result_shape=result_shape, provenance=PROVENANCE_OBSERVED_UNVERIFIED, trust_level=TRUST_LEVEL_UNTRUSTED,
        search_document_text=search_document_text, search_document_hash=search_document_hash,
        content_hash=content_hash,
    )


def get_example(example_id: int, source_id: int, user_id: str) -> SqlExample | None:
    """One example by id, for an owned source. None for an unowned source
    or an example_id that doesn't exist or belongs to a different source."""
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return None
        row = conn.execute(
            "SELECT * FROM sql_examples WHERE id = ? AND source_id = ?", (example_id, source_id),
        ).fetchone()
        return _row_to_example(row) if row is not None else None
    finally:
        conn.close()


def list_examples_for_source(
    source_id: int,
    user_id: str,
    *,
    status: str | None = STATUS_ACTIVE,
    trust_level: str | None = None,
) -> tuple[SqlExample, ...]:
    """
    Examples for one owned source, deterministically ordered by id.
    `status` defaults to STATUS_ACTIVE (STALE examples are excluded by
    default — pass None for every status, or STATUS_STALE explicitly).
    `trust_level` is unfiltered (None) by default — this is a general
    listing function; core.sqlservice.sql_example_vector_index is the
    layer that actually restricts what gets embedded/indexed to TRUSTED
    only. Returns () for an unowned source, never an error.
    """
    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return ()

        query = "SELECT * FROM sql_examples WHERE source_id = ?"
        params: list = [source_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if trust_level is not None:
            query += " AND trust_level = ?"
            params.append(trust_level)
        query += " ORDER BY id"

        rows = conn.execute(query, params).fetchall()
        return tuple(_row_to_example(r) for r in rows)
    finally:
        conn.close()


def update_example_status(example_id: int, source_id: int, user_id: str, *, status: str) -> bool:
    """
    Update ONLY an existing example's `status` column (ACTIVE <-> STALE) —
    the one field on a persisted row this module ever allows to change.
    Every content-identity field (question_pattern, sql_template,
    canonical_object_fqns, ...) is immutable once inserted; there is no
    function anywhere in this module that can alter them. In particular,
    `trust_level`/`provenance` can NEVER change via this function (there
    is no such parameter here) — a STALE<->ACTIVE flip can never promote
    an UNTRUSTED (e.g. insert_untrusted_observation()) row to TRUSTED, or
    change which provenance produced it. Returns True on a successful
    update, False for an unowned source or an example_id that doesn't
    exist / belongs to a different source (never raises for that).
    """
    if status not in (STATUS_ACTIVE, STATUS_STALE):
        raise ValueError(f"status must be ACTIVE or STALE, got {status!r}")

    conn = get_connection()
    try:
        if not _verify_ownership(conn, source_id, user_id):
            return False
        cursor = conn.execute(
            "UPDATE sql_examples SET status = ?, updated_at = ? WHERE id = ? AND source_id = ?",
            (status, _now(), example_id, source_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
