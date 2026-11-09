"""
Direct-schema SQL generation experiment — Phase 6, Direct-Schema Experiment
Step 1.

Tests the simplest possible architecture for one question:

    complete cached Tier 1/2 schema + question
    -> one OpenAI call chooses tables AND generates SQL
    -> existing validate_sql_candidate() validates the SQL

No table selection happens anywhere in this module. The fixed Tier 1/2
safety classification was already completed at connection time (see
core.sqlservice.retrieval_documents.classify_retrieval_tier, run once per
source by core.sqlservice.retrieval_preparation). Every Tier 1/2 table/view
in the source's latest READY retrieval build is included in the prompt,
unconditionally — the model chooses among all of them in a single call.

This module deliberately imports NONE of: core.sqlservice.schema_linker,
core.sqlservice.join_planner, core.sqlservice.relationship_graph,
core.sqlservice.vector_index, core.sqlservice.lexical_index,
core.sqlservice.canonical_scoring, core.sqlservice.context_packing,
core.sqlservice.sql_example_retrieval (or any sql_example_* module),
core.sqlservice.semantic_enrichment_service, or any embedding_* module.
It reuses exactly three existing, unmodified building blocks:

  1. core.sqlservice.retrieval_store.get_latest_ready_retrieval_build() /
     load_stored_retrieval_documents() — the cached schema, already tiered
     at connection time. No question-time ranking or filtering of any kind
     beyond a bare Tier 1/2 membership check.
  2. core.sqlservice.sql_generation_service.generate_sql_candidate() — the
     SAME structured text-to-SQL call, prompt contract, and response
     validation every other pipeline step already uses. Exactly one
     provider call.
  3. core.sqlservice.sql_validation.validate_sql_candidate() — the SAME
     parser-backed read-only/structural/reference validator every other
     pipeline step already uses, completely unmodified and unweakened.

Scope: this module NEVER executes SQL, never connects to a live SQL
Server, never calls an embedding provider, and is not wired into any
route, the connection-time coordinator, or the frontend. It is gated by
core.config.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED (default False) —
see run_direct_schema_experiment's own check, performed via a fresh
per-call `from core.config import ...` so a test can monkeypatch
core.config.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED directly.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from core.config import SQL_GENERATION_MAX_OUTPUT_TOKENS, SQL_GENERATION_TIMEOUT_SECONDS
from core.sqlservice.retrieval_store import (
    StoredRetrievalBuild,
    StoredRetrievalDocument,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.schema_context_retrieval import STATUS_OK as _SCHEMA_CONTEXT_STATUS_OK
from core.sqlservice.schema_context_retrieval import ContextDocument, SchemaContextResult
from core.sqlservice.sql_generation_providers import TextToSqlProvider

# Private-but-pure helper reused verbatim for an accurate, zero-drift
# pre-flight character count of the EXACT prompt generate_sql_candidate()
# will build later (see measure_prompt_feasibility) — never a second,
# independently-maintained copy of the prompt template.
from core.sqlservice.sql_generation_service import _build_prompts as _build_generation_prompts
from core.sqlservice.sql_generation_service import SqlGenerationResult, generate_sql_candidate
from core.sqlservice.sql_validation import SqlValidationResult, validate_sql_candidate

__all__ = [
    "STATUS_OK",
    "STATUS_DISABLED",
    "STATUS_BUILD_NOT_FOUND",
    "STATUS_NO_ELIGIBLE_OBJECTS",
    "STATUS_SCHEMA_TOO_LARGE",
    "ELIGIBLE_TIERS",
    "SELECTION_REASON_DIRECT_FULL_SCHEMA",
    "RANKING_MODE_DIRECT_FULL_SCHEMA",
    "KNOWN_MODEL_CONTEXT_WINDOWS",
    "ObjectSizeInfo",
    "PromptFeasibilityReport",
    "DirectSchemaExperimentResult",
    "build_direct_schema_context_text",
    "build_direct_schema_context",
    "measure_prompt_feasibility",
    "run_direct_schema_experiment",
]

STATUS_OK = "OK"
STATUS_DISABLED = "DISABLED"  # SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED is false — zero provider calls made
STATUS_BUILD_NOT_FOUND = "BUILD_NOT_FOUND"  # no READY retrieval build for this source (unowned or none built yet)
STATUS_NO_ELIGIBLE_OBJECTS = "NO_ELIGIBLE_OBJECTS"  # the READY build has zero Tier 1/2 objects
STATUS_SCHEMA_TOO_LARGE = "SCHEMA_TOO_LARGE"  # the complete schema does not fit safely — stop, never truncate

# Tier classification is a connection-time, source-independent, generic
# naming/shape rule (core.sqlservice.retrieval_documents.
# classify_retrieval_tier) — never re-derived or re-ranked here. This
# module's only "selection" is this fixed membership check.
ELIGIBLE_TIERS = (1, 2)

SELECTION_REASON_DIRECT_FULL_SCHEMA = "DIRECT_FULL_SCHEMA_INCLUSION"
RANKING_MODE_DIRECT_FULL_SCHEMA = "direct_full_schema"

# Static, manually maintained table of published OpenAI model context
# windows (input+output tokens combined). Not queried live — the OpenAI
# chat-completions API exposes no context-window value as call metadata,
# and this project has no separate model-registry service. An unknown
# model key means "unknown", never a guessed number — see
# measure_prompt_feasibility, which refuses to report `fits=True` for a
# model missing from this table.
KNOWN_MODEL_CONTEXT_WINDOWS: Mapping[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "gpt-3.5-turbo": 16_385,
}

# Fraction of the model's published context window this module will ever
# treat as usable for input — the remainder is deliberately reserved as
# safety headroom (response formatting overhead, provider-side rounding,
# a future system-prompt edit) never counted as available budget.
_CONTEXT_SAFETY_MARGIN = 0.9

# Conservative characters-per-token heuristic for a PRE-FLIGHT estimate
# only (no tokenizer dependency — e.g. tiktoken — is installed in this
# project; see requirements.txt). 3.5 chars/token is deliberately BELOW
# the ~4 chars/token typically observed for English/SQL-identifier-heavy
# text, so this estimate can only ever OVER-count tokens, never under —
# it may wrongly call a prompt "too large" that would actually fit, but
# it must never wrongly call an oversized prompt "safe".
_CHARACTERS_PER_TOKEN_ESTIMATE = 3.5

_TOP_N_LARGEST_OBJECTS_DEFAULT = 10


def _estimate_tokens(characters: int) -> int:
    return math.ceil(characters / _CHARACTERS_PER_TOKEN_ESTIMATE)


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjectSizeInfo:
    """One object's contribution to the complete schema dump, for Part 2's
    "largest objects" report."""

    object_fqn: str
    column_count: int
    characters: int


@dataclass(frozen=True)
class PromptFeasibilityReport:
    """
    Result of one measure_prompt_feasibility() call — a pure, zero-provider-
    call pre-flight measurement, never an approximation used to silently
    truncate anything.

    fits: True only when estimated_total_input_tokens fits within
        model_context_window * _CONTEXT_SAFETY_MARGIN, minus
        max_output_tokens_reserved, AND model_context_window is known
        (never True for an unrecognized model — see reason).
    """

    eligible_object_count: int
    column_count: int
    schema_characters: int
    estimated_schema_tokens: int
    largest_objects: tuple[ObjectSizeInfo, ...]
    system_prompt_characters: int
    question_characters: int
    total_request_characters: int
    estimated_total_input_tokens: int
    model: str
    model_context_window: int | None
    max_output_tokens_reserved: int
    fits: bool
    reason: str


@dataclass(frozen=True)
class DirectSchemaExperimentResult:
    """Result of one run_direct_schema_experiment() call."""

    status: str
    feasibility: PromptFeasibilityReport | None
    generation: SqlGenerationResult | None
    validation: SqlValidationResult | None
    build_id: int | None
    provider_latency_seconds: float | None
    total_latency_seconds: float
    error: str | None


# ── Part 1: deterministic compact schema representation ──────────────────
#
# Every field below comes directly from metadata core.sqlservice.
# retrieval_documents already computed and persisted at connection time —
# no raw SchemaSnapshot re-read, no live database call, no relationship
# discovery/graph traversal of any kind happens here.


def _confirmed_physical_fk_targets_by_column(metadata: Mapping[str, object]) -> dict[str, list[str]]:
    """Per-column structurally-confirmed FK target(s) — PHYSICAL_FK
    outbound edges only (a real schema-declared foreign key; never an
    inferred/pending relationship-service row, never a catalog view
    dependency). Source: metadata["confirmed_relationship_edges"], the
    unbounded, provenance-tagged edge list core.sqlservice.
    retrieval_documents._build_document already writes for every object."""
    edges = metadata.get("confirmed_relationship_edges") or ()
    by_column: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("direction") == "outbound" and edge.get("kind") == "PHYSICAL_FK":
            by_column.setdefault(str(edge["column"]), []).append(str(edge["other"]))
    for targets in by_column.values():
        targets.sort()
    return by_column


def _column_line(column: Mapping[str, object], fk_targets_by_column: Mapping[str, Sequence[str]]) -> str:
    flags: list[str] = []
    if column.get("is_primary_key"):
        flags.append("PK")
    for target in fk_targets_by_column.get(str(column.get("column_name")), ()):
        flags.append(f"FK->{target}")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return f"  {column.get('column_name')} {column.get('normalized_sql_type')}{suffix}"


def _build_object_section(object_fqn: str, object_type: str, metadata: Mapping[str, object]) -> str:
    """One deterministic OBJECT block: schema-qualified name, table/view
    type, every canonical column name + normalized SQL type + PK indicator
    + structurally-confirmed FK target, and a concise business
    name/description when the connection-time dictionary already supplied
    one. Column order is inherited from metadata["column_descriptors"],
    which core.sqlservice.retrieval_documents already builds in
    alphabetical column-name order — never re-sorted or re-selected here."""
    lines = [f"OBJECT: {object_fqn} ({object_type})"]
    business_name = metadata.get("business_name")
    description = metadata.get("description")
    if business_name:
        lines.append(f"BUSINESS_NAME: {business_name}")
    if description:
        lines.append(f"DESCRIPTION: {description}")
    lines.append("COLUMNS:")
    fk_by_column = _confirmed_physical_fk_targets_by_column(metadata)
    for column in metadata.get("column_descriptors") or ():
        lines.append(_column_line(column, fk_by_column))
    return "\n".join(lines)


def _eligible_documents(documents: Sequence[StoredRetrievalDocument]) -> tuple[StoredRetrievalDocument, ...]:
    """Fail-closed Tier 1/2 filter, enforced HERE too (not merely trusted
    of a caller's own `tiers=` argument to load_stored_retrieval_documents)
    — a defense-in-depth guarantee that this module's own schema-building
    function can never include a Tier 3/4 (archive/system) object no
    matter what it is handed. Deterministic object_fqn ascending order —
    identical input, any order, always produces the identical output
    order."""
    return tuple(
        sorted((d for d in documents if d.retrieval_tier in ELIGIBLE_TIERS), key=lambda d: d.object_fqn)
    )


def build_direct_schema_context_text(documents: Sequence[StoredRetrievalDocument]) -> str:
    """Pure, deterministic: the same set of documents, in any input order,
    always produces the identical byte-for-byte schema dump text. Tier 3/4
    documents are silently excluded (see _eligible_documents) — never
    included, never counted."""
    ordered = _eligible_documents(documents)
    return "\n\n".join(_build_object_section(d.object_fqn, d.object_type, d.metadata) for d in ordered)


def build_direct_schema_context(
    build: StoredRetrievalBuild, documents: Sequence[StoredRetrievalDocument],
) -> SchemaContextResult:
    """
    Part 5 — existing-validator adaptation: construct a valid, complete
    SchemaContextResult covering every Tier 1/2 object, so
    core.sqlservice.sql_validation.validate_sql_candidate (completely
    unmodified) can verify the generated SQL exactly as it already does
    for the ranked-retrieval pipeline. `documents` metadata is carried
    through UNCHANGED (never re-wrapped, never stripped) — in particular
    metadata["column_names"], which validate_sql_candidate's own
    column-level check requires per referenced table.

    Every eligible (Tier 1/2) document becomes one ContextDocument with
    score=None and selection_reason=SELECTION_REASON_DIRECT_FULL_SCHEMA —
    there was no question-time ranking or selection to record; every
    object was included unconditionally.
    """
    ordered = _eligible_documents(documents)
    context_text = build_direct_schema_context_text(ordered)
    context_documents = tuple(
        ContextDocument(
            object_fqn=d.object_fqn,
            object_type=d.object_type,
            retrieval_tier=d.retrieval_tier,
            document_hash=d.document_hash,
            document_text=d.document_text,
            metadata=d.metadata,
            score=None,
            selection_reason=SELECTION_REASON_DIRECT_FULL_SCHEMA,
        )
        for d in ordered
    )
    return SchemaContextResult(
        status=_SCHEMA_CONTEXT_STATUS_OK,
        build_id=build.id,
        schema_fingerprint=build.schema_fingerprint,
        embedding_provider="none",
        embedding_model="none",
        embedding_dimensions=0,
        documents=context_documents,
        context_text=context_text,
        total_characters=len(context_text),
        truncated=False,
        warnings=(),
        error=None,
        ranking_mode=RANKING_MODE_DIRECT_FULL_SCHEMA,
    )


# ── Part 2: prompt feasibility measurement (zero provider calls) ─────────


def measure_prompt_feasibility(
    question: str,
    schema_context: SchemaContextResult,
    *,
    model: str,
    max_output_tokens: int = SQL_GENERATION_MAX_OUTPUT_TOKENS,
    top_n_largest: int = _TOP_N_LARGEST_OBJECTS_DEFAULT,
) -> PromptFeasibilityReport:
    """
    Measures the EXACT prompt generate_sql_candidate() would send (via the
    same private _build_prompts helper, not a re-derived approximation) —
    no provider call is made. Never truncates or removes anything; only
    reports.
    """
    documents = schema_context.documents

    sizes = tuple(
        ObjectSizeInfo(
            object_fqn=doc.object_fqn,
            column_count=len(doc.metadata.get("column_descriptors") or ()),
            characters=len(_build_object_section(doc.object_fqn, doc.object_type, doc.metadata)),
        )
        for doc in documents
    )
    column_count = sum(s.column_count for s in sizes)
    largest = tuple(sorted(sizes, key=lambda s: (-s.characters, s.object_fqn))[:top_n_largest])

    system_prompt, user_prompt = _build_generation_prompts(question, schema_context)
    total_request_characters = len(system_prompt) + len(user_prompt)
    estimated_total_input_tokens = _estimate_tokens(total_request_characters)

    context_window = KNOWN_MODEL_CONTEXT_WINDOWS.get(model)
    if context_window is None:
        fits = False
        reason = (
            f"model {model!r} has no known published context window in this module's static "
            f"KNOWN_MODEL_CONTEXT_WINDOWS table — cannot verify a safe fit, refusing to proceed"
        )
    else:
        safe_budget = int(context_window * _CONTEXT_SAFETY_MARGIN) - max_output_tokens
        fits = estimated_total_input_tokens <= safe_budget
        reason = (
            f"estimated {estimated_total_input_tokens} input tokens vs a safe budget of {safe_budget} "
            f"({context_window} published context window x {_CONTEXT_SAFETY_MARGIN:.0%} safety margin, "
            f"minus {max_output_tokens} reserved output tokens)"
        )

    return PromptFeasibilityReport(
        eligible_object_count=len(documents),
        column_count=column_count,
        schema_characters=schema_context.total_characters,
        estimated_schema_tokens=_estimate_tokens(schema_context.total_characters),
        largest_objects=largest,
        system_prompt_characters=len(system_prompt),
        question_characters=len(question),
        total_request_characters=total_request_characters,
        estimated_total_input_tokens=estimated_total_input_tokens,
        model=model,
        model_context_window=context_window,
        max_output_tokens_reserved=max_output_tokens,
        fits=fits,
        reason=reason,
    )


# ── Parts 3/4: isolated experimental service (flag-gated) ─────────────────


def run_direct_schema_experiment(
    question: str,
    source_id: int,
    user_id: str,
    provider: TextToSqlProvider,
    *,
    max_output_tokens: int = SQL_GENERATION_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = SQL_GENERATION_TIMEOUT_SECONDS,
) -> DirectSchemaExperimentResult:
    """
    load latest READY build -> build complete compact Tier 1/2 schema ->
    measure feasibility -> call generate_sql_candidate() once (if it fits)
    -> validate with validate_sql_candidate() -> return both results.

    Gated by core.config.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED
    (default False): when false, returns STATUS_DISABLED immediately —
    zero database reads, zero provider calls. The flag is imported fresh
    on every call (not at module import time) so a test can monkeypatch
    core.config.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED directly.

    Never executes the generated SQL against any database — validation
    only. Never wired into any route, composer, or the connection-time
    coordinator; this function has no caller anywhere in production code
    as of this step.
    """
    start = time.monotonic()
    from core.config import SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED as _enabled

    if not _enabled:
        return DirectSchemaExperimentResult(
            status=STATUS_DISABLED, feasibility=None, generation=None, validation=None,
            build_id=None, provider_latency_seconds=None,
            total_latency_seconds=time.monotonic() - start,
            error="SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED is false",
        )

    build = get_latest_ready_retrieval_build(source_id, user_id)
    if build is None:
        return DirectSchemaExperimentResult(
            status=STATUS_BUILD_NOT_FOUND, feasibility=None, generation=None, validation=None,
            build_id=None, provider_latency_seconds=None,
            total_latency_seconds=time.monotonic() - start,
            error=f"no READY retrieval build for source_id={source_id!r}",
        )

    documents = load_stored_retrieval_documents(build.id, source_id, user_id, tiers=ELIGIBLE_TIERS)
    if not documents:
        return DirectSchemaExperimentResult(
            status=STATUS_NO_ELIGIBLE_OBJECTS, feasibility=None, generation=None, validation=None,
            build_id=build.id, provider_latency_seconds=None,
            total_latency_seconds=time.monotonic() - start,
            error=f"build {build.id} has zero Tier 1/2 objects",
        )

    schema_context = build_direct_schema_context(build, documents)
    feasibility = measure_prompt_feasibility(
        question, schema_context, model=provider.model, max_output_tokens=max_output_tokens,
    )
    if not feasibility.fits:
        return DirectSchemaExperimentResult(
            status=STATUS_SCHEMA_TOO_LARGE, feasibility=feasibility, generation=None, validation=None,
            build_id=build.id, provider_latency_seconds=None,
            total_latency_seconds=time.monotonic() - start,
            error=feasibility.reason,
        )

    provider_start = time.monotonic()
    generation = generate_sql_candidate(
        question, schema_context, provider,
        max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
    )
    provider_latency = time.monotonic() - provider_start

    validation = validate_sql_candidate(generation, schema_context)

    return DirectSchemaExperimentResult(
        status=STATUS_OK,
        feasibility=feasibility,
        generation=generation,
        validation=validation,
        build_id=build.id,
        provider_latency_seconds=provider_latency,
        total_latency_seconds=time.monotonic() - start,
        error=None,
    )
