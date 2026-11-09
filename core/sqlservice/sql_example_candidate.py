"""
Trust-hardened SQL-example candidate assembly — SQL rebuild Phase 5,
Steps 9A/9B.

The ONLY sanctioned way to turn "I have some SQL, a provenance, and some
evidence" into a persistable, TRUSTED example payload. No caller anywhere
in this codebase should call core.sqlservice.sql_example_model.
classify_trust_level() directly with hand-picked boolean flags to build a
real example candidate for persistence — classify_trust_level() itself
stays a pure, deterministic function of whatever flags it is given (and
stays directly unit-testable that way — see
tests/test_sqlservice_sql_example_model.py), but THIS module is the only
place those flags are ever DERIVED from real, already-validated evidence
artifacts. No provenance label alone may bypass validation:

Enforced here for EVERY provenance this function accepts, including
PROVENANCE_DEVELOPER_VERIFICATION_SET (which classify_trust_level() itself
always trusts unconditionally once reached — "verified by construction" —
but which must still clear every gate below BEFORE this module ever calls
it):

  1. parser-backed structural validation — core.sqlservice.sql_validation.
     validate_sql_candidate, reused via its own result, never re-run or
     re-implemented here.
  2. read-only validation — the SAME validate_sql_candidate call; that
     module validates structure and read-only-ness as one inseparable
     pass, so there is no separate "read-only only" flag to derive.
  3. referenced-object compatibility — also the SAME validate_sql_candidate
     call, which cross-checks declared vs. SQL-referenced objects in both
     directions and returns the real, canonical object_fqn spellings.
  4. safe normalization — core.sqlservice.sql_example_model.
     normalize_sql_template (the sqlglot-based, parser-backed normalizer,
     Step 9B literal-safety closure included), run on validation_result.sql
     itself — never on a separately supplied string a caller could let
     diverge from what was actually validated. A TemplateNormalizationError
     here rejects the whole candidate; this module never falls back to
     storing a raw, unnormalized SQL string.
  5. provenance-specific automated gates — see build_trusted_example_
     candidate's own docstring for PROVENANCE_VALIDATED_EXECUTION's
     execution/consistency evidence requirement.

Step 9B tightening: build_trusted_example_candidate() NEVER returns a
candidate whose trust_level is anything but TRUSTED — an outcome that
would classify as UNTRUSTED (PROVENANCE_OBSERVED_UNVERIFIED, or a
PROVENANCE_VALIDATED_EXECUTION candidate whose two captured executions
disagree) raises ExampleCandidateRejected instead of ever being wrapped in
a TrustedSqlExampleCandidate — the class name is a promise about every
instance that ever escapes this module, not merely the common case. An
untrusted OBSERVED_UNVERIFIED record, if retained at all for audit
purposes, is built directly by core.sqlservice.sql_example_store.
insert_untrusted_observation() instead, which never uses this module or
this gate.

TrustedSqlExampleCandidate itself additionally CANNOT be hand-constructed
from outside this module — see _ConstructionGuard/_THE_GATE_TOKEN below —
so core.sqlservice.sql_example_store.insert_trusted_example() checking
`isinstance(candidate, TrustedSqlExampleCandidate)` is a real, not merely
conventional, guarantee that the object actually came from
build_trusted_example_candidate() and therefore actually cleared every
gate above.

A caller cannot bypass any of this by passing execution_passed=True or
consistency_passed=True directly — there is no such parameter here. Every
gate flag this module ever passes to classify_trust_level() is derived
from an actual, typed evidence artifact's own `.status` (or, for
consistency, a real structural comparison between two independently
captured artifacts) — never accepted as a bare caller-supplied boolean.

Scope: never executes SQL, never calls an embedding provider, never opens
a database connection, and never inserts anything itself —
core.sqlservice.sql_example_store's insert_trusted_example()/
insert_untrusted_observation() remain the only places a row is actually
written; this module only prepares a candidate that is SAFE to hand them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.sqlservice.sql_example_model import (
    ALL_RESULT_SHAPES,
    PROVENANCE_DEVELOPER_VERIFICATION_SET,
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
    PROVENANCE_VALIDATED_EXECUTION,
    TRUST_LEVEL_TRUSTED,
    TemplateNormalizationError,
    build_example_search_document,
    classify_trust_level,
    compute_content_hash,
    normalize_sql_template,
)
from core.sqlservice.sql_execution import STATUS_OK as _EXECUTION_STATUS_OK
from core.sqlservice.sql_execution import SqlExecutionResult
from core.sqlservice.sql_validation import STATUS_OK as _VALIDATION_STATUS_OK
from core.sqlservice.sql_validation import SqlValidationResult

__all__ = [
    "ExampleCandidateRejected",
    "TrustedSqlExampleCandidate",
    "verify_execution_consistency",
    "build_trusted_example_candidate",
]

# Provenances build_trusted_example_candidate() will ever consider —
# deliberately NOT core.sqlservice.sql_example_model.ALL_PROVENANCES:
# PROVENANCE_OBSERVED_UNVERIFIED is excluded on purpose (see module
# docstring) — it can never classify as TRUSTED, so accepting it here
# would only ever end in ExampleCandidateRejected; a caller wanting to
# record one uses core.sqlservice.sql_example_store.
# insert_untrusted_observation() directly instead, which doesn't need
# (and doesn't run) this gate at all.
_ELIGIBLE_PROVENANCES = frozenset({
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE, PROVENANCE_DEVELOPER_VERIFICATION_SET, PROVENANCE_VALIDATED_EXECUTION,
})


class ExampleCandidateRejected(ValueError):
    """Raised instead of ever producing a candidate whose trust_level would
    rest on anything less than real, verified evidence — a non-OK
    validation_result, a normalization failure, an ineligible provenance,
    or (for PROVENANCE_VALIDATED_EXECUTION) missing, mismatched, or
    inconsistent execution evidence — INCLUDING the case where every gate
    ran but the resulting trust_level would be UNTRUSTED; this module
    never returns an untrusted TrustedSqlExampleCandidate, it raises
    instead. Never caught and silently downgraded by this module — a
    rejected candidate is simply not producible at all, exactly like
    core.sqlservice.sql_example_model.normalize_sql_template's own
    TemplateNormalizationError, which this exception wraps."""


class _ConstructionGuard:
    """Opaque, module-private construction token. TrustedSqlExampleCandidate
    requires one to construct (see its own `_gate_guard` field) and
    build_trusted_example_candidate() below is the only code in this
    module — and therefore, by convention and by this codebase's own
    layering discipline, the only code anywhere — that ever creates and
    passes _THE_GUARD. This does not make hand-construction physically
    impossible (nothing in Python does), but it makes it require a
    deliberate reach into a private module attribute, never an accidental
    or superficially-reasonable-looking call — see
    tests/test_sqlservice_sql_example_candidate.py's own direct-
    construction-rejection tests."""

    __slots__ = ()


_THE_GUARD = _ConstructionGuard()


@dataclass(frozen=True)
class TrustedSqlExampleCandidate:
    """
    Exactly the field shape core.sqlservice.sql_example_store.
    insert_trusted_example() needs — every content/trust/provenance field
    that function persists is read directly off an instance of this class,
    never reconstructed from separate keyword arguments. Every instance
    that exists has trust_level == TRUST_LEVEL_TRUSTED (see module
    docstring) and came from build_trusted_example_candidate() (see
    `_gate_guard`, checked in __post_init__) — never hand-built.
    """

    source_id: int
    schema_fingerprint: str
    question_pattern: str
    sql_template: str
    canonical_object_fqns: tuple[str, ...]
    canonical_columns: tuple[str, ...]
    join_path: tuple[str, ...]
    result_shape: str
    provenance: str
    trust_level: str
    search_document_text: str
    search_document_hash: str
    content_hash: str
    _gate_guard: _ConstructionGuard = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._gate_guard is not _THE_GUARD:
            raise ExampleCandidateRejected(
                "TrustedSqlExampleCandidate must be constructed via "
                "core.sqlservice.sql_example_candidate.build_trusted_example_candidate() "
                "-- direct construction is not permitted"
            )


def verify_execution_consistency(first: SqlExecutionResult, second: SqlExecutionResult) -> bool:
    """
    Pure structural comparison between two INDEPENDENTLY captured
    executions of the same validated SQL — never itself executes anything.
    True only when both executions succeeded (status OK) and returned
    identical columns, row_count, and rows.

    This is the real "consistency" gate PROVENANCE_VALIDATED_EXECUTION's
    trust policy requires (core.sqlservice.sql_example_model.
    classify_trust_level's own documented requirement: "execution success
    alone must not make an example trusted"). A caller wanting a
    PROVENANCE_VALIDATED_EXECUTION candidate must actually capture the SQL
    execution twice (or reuse a second independent capture) and pass BOTH
    real SqlExecutionResult artifacts to build_trusted_example_candidate —
    there is no parameter anywhere in this module that accepts a bare
    "trust me, it was consistent" boolean.
    """
    if first.status != _EXECUTION_STATUS_OK or second.status != _EXECUTION_STATUS_OK:
        return False
    return (
        first.columns == second.columns
        and first.row_count == second.row_count
        and first.rows == second.rows
    )


def _require_positive_int(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ExampleCandidateRejected(f"{name} must be a positive integer, got {value!r}")


def build_trusted_example_candidate(
    *,
    source_id: int,
    provenance: str,
    validation_result: SqlValidationResult,
    question_pattern: str,
    result_shape: str,
    join_path: Sequence[str] = (),
    execution_result: Optional[SqlExecutionResult] = None,
    repeat_execution_result: Optional[SqlExecutionResult] = None,
) -> TrustedSqlExampleCandidate:
    """
    Build one TrustedSqlExampleCandidate — ALWAYS trust_level TRUSTED — or
    raise ExampleCandidateRejected. Never anything in between: no partial
    candidate, and (Step 9B) no untrusted candidate either — an example
    this module cannot vouch for as TRUSTED is simply not produced at all,
    exactly like core.sqlservice.sql_example_model.normalize_sql_template's
    own all-or-nothing contract.

    Order of operations:
      1. `provenance` must be one of the three provenances that can ever
         be TRUSTED (PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
         PROVENANCE_DEVELOPER_VERIFICATION_SET,
         PROVENANCE_VALIDATED_EXECUTION) — PROVENANCE_OBSERVED_UNVERIFIED
         is rejected immediately; see core.sqlservice.sql_example_store.
         insert_untrusted_observation() for that provenance instead.
      2. `validation_result` must be an actual SqlValidationResult with
         status == OK — this is gates 1-3 (parser-backed structural
         validation, read-only validation, referenced-object
         compatibility), all three enforced by that one call, which must
         have already run, successfully, BEFORE this function is ever
         called. A non-OK (or wrong-typed) validation_result rejects the
         candidate immediately, regardless of provenance — no provenance
         label is ever consulted before this check passes.
      3. `question_pattern`/`result_shape` are validated for shape (non-
         blank string / a recognized RESULT_SHAPE_*).
      4. Safe normalization (gate 4) — normalize_sql_template() is run on
         validation_result.sql itself (never a separately supplied SQL
         string), and canonical_object_fqns is taken from
         validation_result.canonical_referenced_object_fqns (the real,
         already-verified canonical spellings — never the caller's own
         casing). A TemplateNormalizationError rejects the candidate.
         canonical_columns is likewise DERIVED, never caller-supplied —
         see the Step 10 note below; there is no `canonical_columns`
         parameter on this function at all.
      5. Provenance-specific gates (gate 5):
         - PROVENANCE_SCHEMA_GENERATED_TEMPLATE / PROVENANCE_
           DEVELOPER_VERIFICATION_SET: no further evidence is required —
           gates 1-4 above are already everything either provenance's
           trust policy needs. Passing execution_result/
           repeat_execution_result for either of these is simply ignored,
           never an error.
         - PROVENANCE_VALIDATED_EXECUTION: REQUIRES both execution_result
           AND repeat_execution_result — two real, independently captured
           SqlExecutionResult artifacts for the SAME build as
           validation_result. execution_passed is derived from
           execution_result.status; consistency_passed is derived by
           actually comparing the two artifacts via
           verify_execution_consistency() — never accepted as a bare
           boolean. Missing either artifact, a build_id mismatch, or a
           wrong type rejects the candidate before classify_trust_level()
           is ever reached.
      6. classify_trust_level() makes the final, pure trust determination
         from the derived flags. If the result is not TRUST_LEVEL_TRUSTED
         (only reachable via PROVENANCE_VALIDATED_EXECUTION with
         disagreeing/failed execution evidence, since step 1 already
         excludes the only unconditionally-untrusted provenance), this
         function raises ExampleCandidateRejected rather than returning an
         untrusted candidate.

    Never itself calls insert_trusted_example() — see
    TrustedSqlExampleCandidate.
    """
    if provenance not in _ELIGIBLE_PROVENANCES:
        raise ExampleCandidateRejected(
            f"provenance must be one of {sorted(_ELIGIBLE_PROVENANCES)}, got {provenance!r} -- "
            "PROVENANCE_OBSERVED_UNVERIFIED can never be trusted; use "
            "core.sqlservice.sql_example_store.insert_untrusted_observation() for that instead"
        )

    _require_positive_int(source_id, "source_id")

    if not isinstance(validation_result, SqlValidationResult):
        raise ExampleCandidateRejected("validation_result must be a real core.sqlservice.sql_validation.SqlValidationResult")
    if validation_result.status != _VALIDATION_STATUS_OK:
        raise ExampleCandidateRejected(
            f"validation_result.status={validation_result.status!r} is not {_VALIDATION_STATUS_OK!r} — "
            "structural/read-only/referenced-object validation must succeed before any provenance is considered"
        )
    if validation_result.schema_fingerprint is None:
        raise ExampleCandidateRejected("validation_result.schema_fingerprint must not be None for an OK result")

    if not isinstance(question_pattern, str) or not question_pattern.strip():
        raise ExampleCandidateRejected("question_pattern must be a non-empty string")
    if result_shape not in ALL_RESULT_SHAPES:
        raise ExampleCandidateRejected(f"result_shape must be one of {sorted(ALL_RESULT_SHAPES)}, got {result_shape!r}")
    if not isinstance(join_path, (list, tuple)) or not all(isinstance(v, str) for v in join_path):
        raise ExampleCandidateRejected("join_path must be a list/tuple of strings")

    try:
        sql_template = normalize_sql_template(validation_result.sql)
    except TemplateNormalizationError as exc:
        raise ExampleCandidateRejected(f"safe normalization failed: {exc}") from exc

    canonical_object_fqns = tuple(validation_result.canonical_referenced_object_fqns)
    # Step 10A: canonical_columns is DERIVED from the validation
    # artifact's own validated_column_references — never accepted from a
    # caller (there is no `canonical_columns` parameter on this function
    # at all). SqlValidationResult.validated_column_references defaults
    # to () for any hand-built/fake result that predates Step 10 (or
    # simply references no physical column, e.g. `SELECT 1`), so this is
    # always safe even against an older-shaped validation_result.
    #
    # Step 10A fix: each reference is encoded as its own
    # "{object_fqn}.{column_name}" string (object_fqn is already
    # "schema.table", so this is exactly the suggested unambiguous
    # "schema.table.column" canonical encoding) — NEVER a bare column
    # name. A bare-name encoding silently collapsed e.g. dbo.A.id and
    # dbo.B.id into a single "id" string, losing which table a column
    # actually belongs to whenever two referenced objects share a column
    # name (a very common case: id, Name, CreatedDate, ...). The
    # sql_examples.canonical_columns_json column is (and remains) a plain
    # JSON array of strings with no format constraint — this changes only
    # the STRING CONTENT written into that existing array, never the
    # database schema (see migrations/014_create_sql_examples.sql).
    #
    # Deduplicated on the (object_fqn, column_name) PAIR, case-
    # insensitively — never merged across different objects just because
    # their bare column names happen to match, and never merged across
    # different casings of the same object/column either. The first
    # canonical casing encountered for a given pair is kept (in practice
    # there is only ever one: validate_sql_candidate's own catalog lookup
    # already returns one fixed canonical spelling per object/column).
    # Final ordering is the sorted encoded strings themselves — since
    # object_fqn never contains a literal '.'-ambiguous prefix collision
    # (a validated T-SQL identifier never contains '.'), this is exactly
    # a deterministic sort by (object_fqn, column_name).
    _columns_by_identity: dict[tuple[str, str], str] = {}
    for ref in validation_result.validated_column_references:
        identity = (ref.object_fqn.lower(), ref.column_name.lower())
        _columns_by_identity.setdefault(identity, f"{ref.object_fqn}.{ref.column_name}")
    canonical_columns = tuple(sorted(_columns_by_identity.values()))

    # Gates 1-3 (structural, read-only, referenced-object compatibility)
    # are all satisfied by validation_result.status == OK, already
    # required above — never re-derived, never accepted separately.
    structural_validation_passed = True
    read_only_validation_passed = True
    execution_passed = False
    consistency_passed = False

    if provenance == PROVENANCE_VALIDATED_EXECUTION:
        if execution_result is None or repeat_execution_result is None:
            raise ExampleCandidateRejected(
                "PROVENANCE_VALIDATED_EXECUTION requires both execution_result and "
                "repeat_execution_result — two independently captured SqlExecutionResult "
                "artifacts; execution success alone is never sufficient"
            )
        if not isinstance(execution_result, SqlExecutionResult) or not isinstance(repeat_execution_result, SqlExecutionResult):
            raise ExampleCandidateRejected("execution_result/repeat_execution_result must be real SqlExecutionResult instances")
        if (
            execution_result.build_id != validation_result.build_id
            or repeat_execution_result.build_id != validation_result.build_id
        ):
            raise ExampleCandidateRejected(
                "execution_result/repeat_execution_result must be for the SAME build as validation_result"
            )
        execution_passed = execution_result.status == _EXECUTION_STATUS_OK
        consistency_passed = verify_execution_consistency(execution_result, repeat_execution_result)

    trust_level = classify_trust_level(
        provenance,
        structural_validation_passed=structural_validation_passed,
        read_only_validation_passed=read_only_validation_passed,
        execution_passed=execution_passed,
        consistency_passed=consistency_passed,
    )
    if trust_level != TRUST_LEVEL_TRUSTED:
        raise ExampleCandidateRejected(
            f"candidate classified as {trust_level!r}, not TRUSTED — "
            "build_trusted_example_candidate() never returns an untrusted candidate "
            "(PROVENANCE_VALIDATED_EXECUTION's two captured executions must agree)"
        )

    search_document_text = build_example_search_document(
        question_pattern=question_pattern, canonical_object_fqns=canonical_object_fqns,
        join_path=join_path, result_shape=result_shape,
    )
    search_document_hash = hashlib.sha256(search_document_text.encode("utf-8")).hexdigest()
    content_hash = compute_content_hash(
        source_id=source_id, question_pattern=question_pattern,
        sql_template=sql_template, canonical_object_fqns=canonical_object_fqns,
    )

    return TrustedSqlExampleCandidate(
        source_id=source_id,
        schema_fingerprint=validation_result.schema_fingerprint,
        question_pattern=question_pattern,
        sql_template=sql_template,
        canonical_object_fqns=canonical_object_fqns,
        canonical_columns=canonical_columns,
        join_path=tuple(join_path),
        result_shape=result_shape,
        provenance=provenance,
        trust_level=trust_level,
        search_document_text=search_document_text,
        search_document_hash=search_document_hash,
        content_hash=content_hash,
        _gate_guard=_THE_GUARD,
    )
