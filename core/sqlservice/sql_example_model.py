"""
Dynamic few-shot SQL-example data model, trust policy, and SQL-template
normalization — SQL rebuild Phase 5, Step 9.

Pure, in-memory, no I/O of any kind. This module never opens a database
connection, never calls an embedding provider, and never executes SQL —
it only defines the SqlExample shape, decides (deterministically, from
explicit evidence a caller attests to) whether a candidate example is
trusted, and normalizes a validated SQL string into a reusable template.

Trust policy — NO manual approval workflow anywhere in this codebase:
classify_trust_level() is the ONLY function allowed to produce a trust
level, and it is a pure function of (provenance, gate-evidence flags) —
there is no "approved_by"/"approved_at" column or function anywhere in
this feature, unlike data/relationship_service.py's human-gated
approve_relationship() for the older EDP track. Eligible TRUSTED
provenances are exactly the three named in the Step 9 brief:

  1. PROVENANCE_SCHEMA_GENERATED_TEMPLATE — a deterministic,
     schema-generated template that passed structural validation.
  2. PROVENANCE_DEVELOPER_VERIFICATION_SET — a separate developer
     verification/training set, never the same questions used as
     evaluation cases (see tests/test_sql_example_golden_leakage.py).
  3. PROVENANCE_VALIDATED_EXECUTION — previously generated SQL that
     passed table reference, column reference, read-only, execution, AND
     consistency gates — ALL FOUR, never execution alone (see
     classify_trust_level's own docstring).

Anything else — including PROVENANCE_OBSERVED_UNVERIFIED, an unrecognized
provenance string, or a VALIDATED_EXECUTION candidate missing even one of
its four gate flags — is UNTRUSTED, unconditionally. An untrusted example
may still be recorded (for audit/observability) but core.sqlservice.
sql_example_retrieval never surfaces one, and core.sqlservice.
sql_generation_service never puts one in a prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from sqlglot import exp

from core.config import SQL_VALIDATION_MAX_AST_NODES, SQL_VALIDATION_MAX_SQL_CHARACTERS
from core.sqlservice.sql_validation import (
    TSQL_DIALECT,
    find_forbidden_construct_reasons,
    find_structural_table_reference_reasons,
    parse_single_read_only_statement,
)

__all__ = [
    "PROVENANCE_SCHEMA_GENERATED_TEMPLATE",
    "PROVENANCE_DEVELOPER_VERIFICATION_SET",
    "PROVENANCE_VALIDATED_EXECUTION",
    "PROVENANCE_OBSERVED_UNVERIFIED",
    "ALL_PROVENANCES",
    "TRUST_LEVEL_TRUSTED",
    "TRUST_LEVEL_UNTRUSTED",
    "STATUS_ACTIVE",
    "STATUS_STALE",
    "RESULT_SHAPE_COUNT",
    "RESULT_SHAPE_LIST",
    "RESULT_SHAPE_GROUPED_AGGREGATION",
    "RESULT_SHAPE_TREND",
    "RESULT_SHAPE_ATTRIBUTE",
    "ALL_RESULT_SHAPES",
    "SqlExample",
    "classify_trust_level",
    "TemplateNormalizationError",
    "normalize_sql_template",
    "build_example_search_document",
    "compute_content_hash",
]

# ── Provenance / trust vocabulary ──────────────────────────────────────────

PROVENANCE_SCHEMA_GENERATED_TEMPLATE = "SCHEMA_GENERATED_TEMPLATE"
PROVENANCE_DEVELOPER_VERIFICATION_SET = "DEVELOPER_VERIFICATION_SET"
PROVENANCE_VALIDATED_EXECUTION = "VALIDATED_EXECUTION"
PROVENANCE_OBSERVED_UNVERIFIED = "OBSERVED_UNVERIFIED"  # never trusted -- see module docstring
ALL_PROVENANCES = frozenset({
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE, PROVENANCE_DEVELOPER_VERIFICATION_SET,
    PROVENANCE_VALIDATED_EXECUTION, PROVENANCE_OBSERVED_UNVERIFIED,
})

TRUST_LEVEL_TRUSTED = "TRUSTED"
TRUST_LEVEL_UNTRUSTED = "UNTRUSTED"

STATUS_ACTIVE = "ACTIVE"
STATUS_STALE = "STALE"

RESULT_SHAPE_COUNT = "COUNT"
RESULT_SHAPE_LIST = "LIST"
RESULT_SHAPE_GROUPED_AGGREGATION = "GROUPED_AGGREGATION"
RESULT_SHAPE_TREND = "TREND"
RESULT_SHAPE_ATTRIBUTE = "ATTRIBUTE"
ALL_RESULT_SHAPES = frozenset({
    RESULT_SHAPE_COUNT, RESULT_SHAPE_LIST, RESULT_SHAPE_GROUPED_AGGREGATION,
    RESULT_SHAPE_TREND, RESULT_SHAPE_ATTRIBUTE,
})


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SqlExample:
    """One trusted (or, if trust_level is UNTRUSTED, audit-only)
    natural-language-question/SQL-template pair. Every field required by
    the Step 9 brief's "Example model must include" list is present.

    Never carries: credentials, result rows, raw provider prompts/errors,
    personal/literal data values (see normalize_sql_template — sql_template
    is always already-normalized), or an "approved_by"/"approved_at" pair
    (no manual approval workflow exists for this feature)."""

    id: int
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
    status: str
    created_at: str
    updated_at: str


# ── Trust policy (pure — no manual approval workflow) ──────────────────────


def classify_trust_level(
    provenance: str,
    *,
    structural_validation_passed: bool = False,
    read_only_validation_passed: bool = False,
    execution_passed: bool = False,
    consistency_passed: bool = False,
) -> str:
    """
    The ONLY function in this codebase allowed to decide an example's
    trust_level. Deterministic, pure — a function of `provenance` and the
    explicit gate-evidence flags a caller attests to, never a stored
    "approved" flag someone flips by hand.

    - PROVENANCE_SCHEMA_GENERATED_TEMPLATE: trusted iff
      structural_validation_passed (its only applicable gate — there is no
      execution for a bare deterministic template).
    - PROVENANCE_DEVELOPER_VERIFICATION_SET: always trusted — verified by
      construction (a curated, human-authored training set), never by an
      approval workflow on individual rows.
    - PROVENANCE_VALIDATED_EXECUTION: trusted ONLY when ALL FOUR of
      structural_validation_passed, read_only_validation_passed,
      execution_passed, AND consistency_passed are True. Execution success
      alone (execution_passed=True, anything else False) is explicitly
      UNTRUSTED — the Step 9 brief's own "execution success alone must not
      make an example trusted" requirement, enforced structurally here,
      not by caller discipline.
    - PROVENANCE_OBSERVED_UNVERIFIED or any unrecognized provenance:
      always untrusted, regardless of any flag.
    """
    if provenance == PROVENANCE_SCHEMA_GENERATED_TEMPLATE:
        return TRUST_LEVEL_TRUSTED if structural_validation_passed else TRUST_LEVEL_UNTRUSTED
    if provenance == PROVENANCE_DEVELOPER_VERIFICATION_SET:
        return TRUST_LEVEL_TRUSTED
    if provenance == PROVENANCE_VALIDATED_EXECUTION:
        if (
            structural_validation_passed and read_only_validation_passed
            and execution_passed and consistency_passed
        ):
            return TRUST_LEVEL_TRUSTED
        return TRUST_LEVEL_UNTRUSTED
    return TRUST_LEVEL_UNTRUSTED


# ── SQL template normalization (pure — literals out, structure kept) ──────
#
# Parser-based (sqlglot, dialect="tsql") — NEVER regex-as-structural-
# authority. Reuses core.sqlservice.sql_validation's own shared,
# schema-independent parser helpers (parse_single_read_only_statement,
# find_forbidden_construct_reasons, find_structural_table_reference_reasons)
# for exactly the same statement-shape/read-only/forbidden-construct/cross-
# database gate that module already established — this is a second,
# independent invocation of the SAME authority (this module has no
# schema_context to resolve real object existence against, so it cannot
# run sql_validation.validate_sql_candidate's full check; it runs every
# OTHER part of that check that doesn't need one), never a re-implemented
# or looser one.


class TemplateNormalizationError(ValueError):
    """The candidate SQL could not be safely normalized into a reusable
    template — never silently stored as-is with raw literals still
    present, and never silently stripped of a literal it cannot classify
    with confidence."""


# ── Literal-context classification (pure, AST-only) — Step 9B closure ─────
#
# Every data-bearing literal node in the tree — exp.Literal (string or
# number), exp.National/exp.UnicodeString (unicode string escapes),
# exp.Date/exp.Timestamp (ODBC {d '...'} / {ts '...'} literal escapes), and
# exp.HexString/exp.ByteString/exp.BitString/exp.RawString (binary/byte
# literals such as T-SQL's 0x1A2B) — is classified by its IMMEDIATE
# structural context (a unary minus is unwrapped first, so `-5` is treated
# as one unit, never leaving a bare leading `-` beside a placeholder):
#
#   - EXEMPT (left untouched — a strict, closed allowlist of explicitly
#     STRUCTURAL numeric positions, never a filter's data value):
#       * TOP / OFFSET / FETCH row-count controls (exp.Limit/Offset/Fetch).
#       * a positional ORDER BY ordinal, `ORDER BY 1` (exp.Ordered).
#       * a CAST/CONVERT type parameter — DECIMAL(10,2)'s 10/2,
#         VARCHAR(50)'s 50 (exp.DataTypeParam) — part of the SQL TYPE
#         shape, never a data value; sqlglot always wraps these in their
#         own DataTypeParam node, one level removed from Cast/Convert
#         itself, so they are never mistaken for a Func's own argument.
#     A DATEADD/DATEDIFF date-part identifier (`day`, `month`, ...) is
#     ALREADY never a literal at all — sqlglot always parses it as
#     exp.Var, so it can never appear in (or need special-casing out of)
#     this classification in the first place; nothing here can "mistake"
#     it for a user-variable value, structurally.
#   - REPLACED with a deterministic typed placeholder (:string_literal /
#     :number_literal / :date_literal / :binary_literal) — EVERYTHING
#     ELSE: the operand(s) of a comparison (=, <>, >, >=, <, <=),
#     LIKE/NOT LIKE, an IN list element, a BETWEEN low/high bound, AND —
#     Step 9B widening — every other position too: a numeric function
#     argument (DATEADD's delta, LEFT's length, ROUND's precision,
#     COALESCE's default, ...), a CASE branch result, a bare SELECT-list
#     projection constant, an arithmetic operand, or any other position
#     not in the allowlist above. There is no longer a blanket "any
#     function argument is left alone" exemption — Step 9A's version of
#     this module had one; Step 9B closes it, because a function argument
#     is exactly as likely to carry a real, identifying user-supplied
#     value (DATEADD's delta, COALESCE's default) as a WHERE-clause
#     comparison is, and leaving it untouched left literal data sitting in
#     a stored "template".
#
# See _find_disallowed_literal_reasons below for the final invariant this
# classification is also reused to enforce — proof, not assumption, that
# nothing outside the allowlist survives.
_COMPARISON_TYPES: tuple[type, ...] = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_PATTERN_MATCH_TYPES: tuple[type, ...] = (exp.Like, exp.ILike)
_EXEMPT_STRUCTURAL_PARENT_TYPES: tuple[type, ...] = (exp.Limit, exp.Offset, exp.Fetch, exp.Ordered, exp.DataTypeParam)
_TYPED_DATE_WRAPPER_TYPES: tuple[type, ...] = (exp.Date, exp.Timestamp)
_TYPED_BINARY_WRAPPER_TYPES: tuple[type, ...] = (exp.HexString, exp.ByteString, exp.BitString, exp.RawString)
_STANDALONE_STRING_WRAPPER_TYPES: tuple[type, ...] = (exp.National, exp.UnicodeString)
_ALL_LITERAL_BEARING_TYPES: tuple[type, ...] = (
    (exp.Literal,) + _STANDALONE_STRING_WRAPPER_TYPES + _TYPED_DATE_WRAPPER_TYPES + _TYPED_BINARY_WRAPPER_TYPES
)

_PLACEHOLDER_STRING = ":string_literal"
_PLACEHOLDER_NUMBER = ":number_literal"
_PLACEHOLDER_DATE = ":date_literal"
_PLACEHOLDER_BINARY = ":binary_literal"


def _literal_kind(node: exp.Expression) -> str | None:
    """None for anything that isn't a literal-bearing node this function
    knows how to classify at all (never reached by a real T-SQL parse —
    see _find_disallowed_literal_reasons, which would catch it)."""
    if isinstance(node, exp.Literal):
        return _PLACEHOLDER_STRING if node.is_string else _PLACEHOLDER_NUMBER
    if isinstance(node, _STANDALONE_STRING_WRAPPER_TYPES):
        return _PLACEHOLDER_STRING
    if isinstance(node, _TYPED_DATE_WRAPPER_TYPES):
        return _PLACEHOLDER_DATE
    if isinstance(node, _TYPED_BINARY_WRAPPER_TYPES):
        return _PLACEHOLDER_BINARY
    return None


def _base_unit(node: exp.Expression) -> exp.Expression:
    """Climb one level past a wrapping unary minus, if present, so `-5` is
    treated (and, if replaced, replaced) as a single unit — never leaving
    a bare `-` immediately before a placeholder."""
    parent = node.parent
    return parent if isinstance(parent, exp.Neg) else node


def _is_supported_filter_context(parent: exp.Expression, unit: exp.Expression) -> bool:
    if isinstance(parent, _COMPARISON_TYPES) or isinstance(parent, _PATTERN_MATCH_TYPES):
        return True
    if isinstance(parent, exp.In):
        return unit.arg_key == "expressions"
    if isinstance(parent, exp.Between):
        return unit.arg_key in ("low", "high")
    return False


def _is_exempt(unit: exp.Expression) -> bool:
    parent = unit.parent
    return parent is not None and isinstance(parent, _EXEMPT_STRUCTURAL_PARENT_TYPES)


def _wrapper_inner_literal_ids(tree: exp.Expression) -> set[int]:
    """exp.Date/Timestamp wrap an inner exp.Literal holding the actual date
    text — that inner node is never classified on its own (it has no
    meaningful context of its own; the WRAPPER's context is what matters),
    so both the collection pass and the final invariant scan exclude it by
    id() the same way."""
    return {
        id(w.this) for w in tree.find_all(_TYPED_DATE_WRAPPER_TYPES) if isinstance(w.this, exp.Literal)
    }


def _collect_replaceable_units(tree: exp.Expression) -> tuple[tuple[exp.Expression, str], ...]:
    """Returns (node_to_replace, placeholder_text) pairs — EVERY literal-
    bearing node not in the strict structural allowlist (see the module
    comment above _COMPARISON_TYPES). Collected in one pass and replaced in
    a second (by the caller) — this function never mutates `tree` itself,
    so the walk below is never disturbed by in-place structural changes
    mid-traversal."""
    skip_ids = _wrapper_inner_literal_ids(tree)
    candidates = [node for node in tree.find_all(_ALL_LITERAL_BEARING_TYPES) if id(node) not in skip_ids]

    units: dict[int, tuple[exp.Expression, str]] = {}
    for node in candidates:
        placeholder = _literal_kind(node)
        if placeholder is None:
            continue
        unit = _base_unit(node)
        if _is_exempt(unit):
            continue
        units[id(unit)] = (unit, placeholder)
    return tuple(units.values())


def _find_disallowed_literal_reasons(tree: exp.Expression) -> tuple[str, ...]:
    """
    The Step 9B final invariant: walk the (already-transformed) tree and
    prove — not assume — that every remaining literal-bearing node sits at
    an allowlisted structural position (TOP/OFFSET/FETCH, positional
    ORDER BY, a CAST/CONVERT type parameter). Anything else still present
    is a bug in _collect_replaceable_units (a literal-bearing node type
    this module doesn't classify, or a classified one this scan disagrees
    is exempt) — reported here rather than silently shipped, so
    normalize_sql_template can refuse to store the template instead.

    Deliberately does NOT re-check _is_supported_filter_context — every
    non-exempt position must already be gone by now, replaced with an
    exp.Var placeholder (which is not itself a literal-bearing type and so
    can never be found by this scan) — the only two possible outcomes for
    a literal-bearing node at this point are "exempt" or "bug".
    """
    skip_ids = _wrapper_inner_literal_ids(tree)
    reasons: list[str] = []
    for node in tree.find_all(_ALL_LITERAL_BEARING_TYPES):
        if id(node) in skip_ids:
            continue
        if _literal_kind(node) is None:
            reasons.append(f"unclassifiable literal-bearing node {type(node).__name__!r} survived normalization")
            continue
        unit = _base_unit(node)
        if not _is_exempt(unit):
            reasons.append(
                f"{type(node).__name__} literal survived normalization outside the structural allowlist "
                f"(context: {type(unit.parent).__name__ if unit.parent is not None else 'root'!r})"
            )
    return tuple(reasons)


def normalize_sql_template(
    sql: str,
    *,
    max_sql_characters: int = SQL_VALIDATION_MAX_SQL_CHARACTERS,
    max_ast_nodes: int = SQL_VALIDATION_MAX_AST_NODES,
) -> str:
    """
    Parse `sql` (sqlglot, dialect="tsql" — a real AST, never regex-as-
    structural-authority) and replace every data-bearing literal with a
    deterministic typed placeholder (":string_literal", ":number_literal",
    ":date_literal", ":binary_literal") while retaining every structural
    join/alias/grouping/aggregation/ordering clause. Deterministic:
    identical input always produces byte-identical output; the OUTPUT is
    sqlglot's own canonical regeneration of the parsed tree (e.g. an
    implicit table alias `t` is rendered as the equivalent, canonical
    `AS t`) rather than a substring edit of the original text — a
    stronger, more useful determinism guarantee than "same input bytes
    back with some substrings swapped", and the only way to guarantee the
    output is itself always well-formed SQL shape.

    Step 9B literal-safety closure — EVERY literal is either replaced or
    the whole candidate is rejected; there is no third "leave it as data"
    outcome outside a strict, closed structural allowlist:
      - REPLACED: the operand(s) of a comparison/LIKE/IN/BETWEEN (as
        before), AND — widened from Step 9A — every other position too: a
        numeric function argument (DATEADD's delta, LEFT's length,
        ROUND's precision, COALESCE's default, ...), a CASE branch
        result, a bare SELECT-list projection constant, an arithmetic
        operand. Step 9A's blanket "any function argument is exempt" rule
        is gone — a function argument is exactly as likely to carry a
        real, identifying value as a WHERE-clause comparison.
      - EXEMPT (left untouched — never data, always structural): TOP,
        OFFSET/FETCH row-count controls; a positional ORDER BY ordinal
        (`ORDER BY 1`); a CAST/CONVERT type parameter (DECIMAL(10,2)'s
        10/2). A DATEADD/DATEDIFF date-part identifier (`day`, `month`)
        is never a literal at all (sqlglot always parses it as a plain
        identifier/Var), so it can never be mistaken for one of these —
        or for a user-supplied value — in the first place.
      - FINAL INVARIANT: after replacement, the transformed tree is
        walked ONE more time (_find_disallowed_literal_reasons) to PROVE
        no data-bearing literal AST node remains anywhere outside the
        allowlist above — an AST walk, never a substring/regex scan.
        Anything found (which the replacement pass above should make
        structurally unreachable) raises TemplateNormalizationError
        rather than ever storing an uncertain template.

    Raises TemplateNormalizationError (never silently stores an unsafe or
    uncertain template) when:
      - `sql` is not a string, or is blank.
      - `sql` is not exactly one parsed, read-only SELECT/CTE/set-operation
        statement — a parse failure, more than one statement (including one
        hidden behind a semicolon embedded in a string literal, which a
        real parser — unlike substring counting — correctly does NOT treat
        as a second statement), a comment marker, or a root statement type
        that isn't SELECT/UNION/EXCEPT/INTERSECT.
      - `sql` contains a write, a command (EXEC/EXECUTE, including dynamic
        SQL such as `EXEC sp_executesql ...`), SELECT INTO, or a banned
        table-valued rowset function (OPENROWSET/OPENQUERY/OPENDATASOURCE)
        anywhere in the tree, including nested inside a CTE body.
      - `sql` references a temp table, a table variable, or a cross-
        database/linked-server (catalog-qualified) table — see
        core.sqlservice.sql_validation.find_structural_table_reference_
        reasons; this module has no schema_context, so it cannot and does
        not check whether a referenced table actually EXISTS (that stays
        core.sqlservice.sql_validation.validate_sql_candidate's job,
        required to have already run — successfully — before an example
        candidate ever reaches this function in a real pipeline; see
        core.sqlservice.sql_example_candidate).
      - the final invariant check finds a data-bearing literal outside the
        structural allowlist still present after replacement.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise TemplateNormalizationError("sql must be a non-empty string")

    tree, shape_reason = parse_single_read_only_statement(
        sql, max_sql_characters=max_sql_characters, max_ast_nodes=max_ast_nodes,
    )
    if tree is None:
        raise TemplateNormalizationError(shape_reason)

    forbidden_reasons = find_forbidden_construct_reasons(tree)
    if forbidden_reasons:
        raise TemplateNormalizationError("; ".join(forbidden_reasons))

    table_reasons = find_structural_table_reference_reasons(tree)
    if table_reasons:
        raise TemplateNormalizationError("; ".join(table_reasons))

    for unit, placeholder in _collect_replaceable_units(tree):
        unit.replace(exp.Var(this=placeholder))

    invariant_reasons = _find_disallowed_literal_reasons(tree)
    if invariant_reasons:
        raise TemplateNormalizationError(
            "normalization invariant violated -- refusing to store an uncertain template: "
            + "; ".join(invariant_reasons)
        )

    return tree.sql(dialect=TSQL_DIALECT).strip()


# ── Search-document construction (pure — mirrors retrieval_documents.py's
#    approach: a compact, deterministic text built only from already-
#    validated structured fields) ──────────────────────────────────────────


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_example_search_document(
    *,
    question_pattern: str,
    canonical_object_fqns: Sequence[str],
    join_path: Sequence[str],
    result_shape: str,
) -> str:
    """
    A compact, deterministic embedding-ready document emphasizing exactly
    the Step 9 "Vector indexing, requirement 1" signal list: question
    pattern, canonical objects (a proxy for business intent — the objects
    a question is actually about), join shape, and
    aggregation/date/result-shape pattern. Never includes sql_template
    text itself (SQL syntax is not useful embedding signal and would only
    dilute the semantic match against a natural-language question) and
    never a literal value (question_pattern is itself already a pattern,
    never raw user input — see core.sqlservice.sql_example_store's own
    insert-time contract).
    """
    lines = [
        f"QUESTION_PATTERN: {question_pattern}",
        f"RESULT_SHAPE: {result_shape}",
    ]
    if canonical_object_fqns:
        lines.append(f"OBJECTS: {', '.join(sorted(canonical_object_fqns))}")
    if join_path:
        lines.append("JOIN_PATH:")
        lines.extend(f"  {edge}" for edge in join_path)
    return "\n".join(lines).strip()


def compute_content_hash(
    *,
    source_id: int,
    question_pattern: str,
    sql_template: str,
    canonical_object_fqns: Sequence[str],
) -> str:
    """
    The example's immutable content identity — sha256 over a canonical
    JSON payload of exactly the fields that define "this is logically the
    same example" (source, question pattern, normalized SQL, canonical
    objects — never timestamps/status/trust_level, which legitimately
    change over an example's lifetime without it becoming a different
    example). Used by core.sqlservice.sql_example_store for idempotent
    insert (a re-submission of the identical example is a no-op reuse, not
    a duplicate row) and to prove content immutability (any attempt to
    UPDATE a stored row's own hashed fields is rejected there).
    """
    payload = {
        "source_id": source_id,
        "question_pattern": question_pattern,
        "sql_template": sql_template,
        "canonical_object_fqns": sorted(canonical_object_fqns),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _hash_text(canonical)
