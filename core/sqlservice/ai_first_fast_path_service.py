"""
Phase 7, Step 6C — parallel single-model fast path.

Replaces the existing AI-first pipeline's THREE model calls (GPT-6 Sol
schema selection, GPT-4o-mini SQL generation, GPT-6 Sol SQL review) with
ONE combined GPT-6 Sol call (core.sqlservice.fast_path_generation_service),
then reuses every deterministic downstream stage from the EXISTING pipeline
completely UNCHANGED:

  core.sqlservice.fast_path_context.build_fast_path_context() — the ONE
  embedding call this pipeline spends, reusing core.sqlservice.
  schema_frontier.build_schema_frontier() verbatim
    -> core.sqlservice.fast_path_generation_service.generate_fast_path_candidate()
       — the ONE AI call: request-kind classification + business intent +
       schema selection + SQL generation, all at once
    -> [NON_DATA / UNSUPPORTED / CLARIFICATION_REQUIRED short-circuit —
       every stage below never runs]
    -> core.sqlservice.schema_relationship_completion.complete_relationships()
       — UNCHANGED, same call the existing pipeline makes
    -> [disconnected-selection clarification short-circuit]
    -> core.sqlservice.schema_exact_context.build_exact_schema_context() —
       UNCHANGED
    -> [context-too-large clarification short-circuit]
    -> core.sqlservice.ambiguity_guard.check_ambiguity() — UNCHANGED, fed a
       core.sqlservice.schema_selection_service.SchemaSelectionCandidate
       ADAPTER built from this pipeline's own combined-call output (never a
       second selection call; pure in-memory reshaping of already-validated
       fields)
    -> [ambiguity-guard clarification short-circuit]
    -> strict existing AST validation (core.sqlservice.sql_validation.
       validate_sql_candidate) — UNCHANGED, fed a core.sqlservice.
       sql_generation_service.SqlGenerationResult ADAPTER wrapping this
       pipeline's own combined-call SQL. THE FINAL AUTHORITY: never
       overridden, never skipped, never replaced by a correction/retry loop
       (Part 4's own explicit restriction — a validation failure returns a
       sanitized failure, never a second attempt).
    -> bounded existing read-only execution (core.sqlservice.sql_execution.
       execute_validated_sql) — UNCHANGED
    -> core.sqlservice.fast_path_answer_formatter — deterministic answer
       formatting for every simple result shape (zero AI calls); the
       EXISTING, UNCHANGED core.sqlservice.answer_generation_service.
       generate_answer() is used ONLY for a result fast_path_answer_
       formatter.is_simple_result() itself judges too complex to format
       safely on its own, and only when the caller supplies an
       answer_provider at all — never part of the normal simple-question
       path (Part 5's own explicit requirement).

There is no separate SQL-review stage, and the path never retries,
repairs, or runs a correction call of any kind: a validation failure
returns a sanitized failure, never a second attempt.

The retired keyword IntentResolver is structurally
unreachable here exactly as established in Step 6B — this module never
imports it, and api/v1/composer.py's own routing never calls it for any
source reaching this pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Callable, Optional

from core.config import (
    AI_FIRST_SCHEMA_MAX_CONNECTORS,
    AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED,
    AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_MAX_ROWS_PER_IDENTITY,
    AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_TTL_SECONDS,
    AI_FIRST_SQL_FAST_PATH_MAX_OBJECTS,
    AI_FIRST_SQL_FAST_PATH_MAX_OUTPUT_TOKENS,
    AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED,
    AI_FIRST_SQL_FAST_PATH_TIMEOUT_SECONDS,
    AI_FIRST_SQL_ORCHESTRATION_BUDGET_SECONDS,
)
from data.datasource_service import get_datasource_identity_key
from core.sqlservice.ambiguity_guard import check_ambiguity
from core.sqlservice.answer_generation_providers import AnswerGenerationProvider
from core.sqlservice.answer_generation_service import STATUS_NO_ROWS as _ANSWER_STATUS_NO_ROWS
from core.sqlservice.answer_generation_service import STATUS_OK as _ANSWER_STATUS_OK
from core.sqlservice.answer_generation_service import generate_answer
from core.sqlservice.embedding_providers import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from core.sqlservice.fast_path_answer_formatter import format_deterministic_answer, is_simple_result
from core.sqlservice.fast_path_context import STATUS_OK as _FAST_CONTEXT_STATUS_OK
from core.sqlservice.fast_path_context import build_fast_path_context
from core.sqlservice.fast_path_generation_providers import (
    FastPathGenerationConfigurationError,
    FastPathGenerationProvider,
    FastPathGenerationProviderError,
)
from core.sqlservice.fast_path_generation_service import FAST_PATH_PROMPT_VERSION
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_CLARIFICATION_REQUIRED as _FP_CLARIFICATION
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_DATA_QUESTION as _FP_DATA_QUESTION
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_NON_DATA as _FP_NON_DATA
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_UNSUPPORTED as _FP_UNSUPPORTED
from core.sqlservice.fast_path_generation_service import STATUS_OK as _FAST_GEN_STATUS_OK
from core.sqlservice.fast_path_generation_service import (
    FastPathFollowUpResolution,
    FastPathGenerationCandidate,
    FastPathGenerationValidationError,
    generate_fast_path_candidate,
)
from core.sqlservice.fast_path_prior_turn import FastPathPriorTurnContext, next_turn_context_from_candidate
from core.sqlservice.fast_path_plan_cache import (
    FastPathCachedPlan,
    build_fast_path_plan_cache_key,
    evict_fast_path_plan,
    get_cached_fast_path_plan,
    store_fast_path_plan,
)
from core.sqlservice.durable_plan_store import (
    DurablePlan,
    build_durable_plan_key,
    evict_durable_plan,
    get_durable_plan,
    store_durable_plan,
)
from core.sqlservice.result_insight_builder import build_deterministic_insights
from core.sqlservice.retrieval_store import StoredRetrievalBuild, get_latest_ready_retrieval_build
from core.sqlservice.schema_exact_context import STATUS_INCOMPLETE as _CONTEXT_STATUS_INCOMPLETE
from core.sqlservice.schema_exact_context import STATUS_OK as _CONTEXT_STATUS_OK
from core.sqlservice.schema_exact_context import build_exact_schema_context
from core.sqlservice.schema_relationship_completion import STATUS_CONNECTED as _RELATIONSHIP_STATUS_CONNECTED
from core.sqlservice.schema_relationship_completion import STATUS_DISCONNECTED as _RELATIONSHIP_STATUS_DISCONNECTED
from core.sqlservice.schema_relationship_completion import complete_relationships
from core.sqlservice.schema_selection_service import RESPONSE_STATUS_SELECTED as _SELECTION_RESPONSE_SELECTED
from core.sqlservice.schema_selection_service import (
    IntentSummary,
    ObjectSelectionReason,
    SchemaSelectionCandidate,
    SchemaSelectionResult,
)
from core.sqlservice.schema_selection_service import STATUS_OK as _SELECTION_STATUS_OK
from core.sqlservice.sql_execution import STATUS_OK as _EXEC_STATUS_OK
from core.sqlservice.sql_execution import ConnectionFactory, execute_validated_sql
from core.sqlservice.sql_generation_service import STATUS_OK as _GEN_STATUS_OK
from core.sqlservice.sql_generation_service import SqlGenerationCandidate, SqlGenerationResult
from core.sqlservice.sql_validation import STATUS_OK as _VALID_STATUS_OK
from core.sqlservice.sql_validation import validate_sql_candidate

from core.sqlservice.ai_first_result import (
    ERROR_CATEGORY_CONFIGURATION,
    ERROR_CATEGORY_INVALID_RESPONSE,
    ERROR_CATEGORY_PROVIDER,
    STATUS_ANSWER_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_CONTEXT_BUILD_FAILED,
    STATUS_EXECUTION_FAILED,
    STATUS_NON_DATA,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RELATIONSHIP_COMPLETION_FAILED,
    STATUS_RETRIEVAL_FAILED,
    STATUS_SELECTION_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_VALIDATION_FAILED,
    AiFirstOrchestrationResult,
    build_ai_first_orchestration_result,
)
from core.sqlservice.schema_frontier import SchemaFrontierResult, SchemaFrontierValidationError
import data.business_knowledge_package_service as _business_knowledge_package_service

__all__ = ["answer_question_ai_first_fast_path"]

logger = logging.getLogger(__name__)

_STAGE_CONTEXT = "fast-path context retrieval"
_STAGE_GENERATION = "fast-path combined generation"


class _NoAnswerProvider:
    """Sentinel reported as the answer-provider identity whenever no real
    AnswerGenerationProvider was supplied (the normal fast-path case: the
    answer was formatted deterministically, zero AI calls) -- honestly
    distinct from a real caller-supplied provider."""

    provider_id = "none"
    model = "none (fast path: deterministic formatting)"


_NO_ANSWER_PROVIDER = _NoAnswerProvider()


def _business_knowledge_cache_identity(source_id: int, user_id: str) -> tuple:
    """(package_id, package_version) of source_id's ACTIVE business-
    knowledge package, or (None, None) when there is none -- a cheap local
    DB read, never an AI call. Included in the plan-cache key so activating,
    deactivating, or re-versioning a package naturally misses the cache
    instead of ever serving a plan generated under different evidence."""
    try:
        package = _business_knowledge_package_service.get_active_package(source_id, user_id)
    except Exception:  # noqa: BLE001 -- cache-identity lookup must never break the request
        return (None, None)
    if package is None:
        return (None, None)
    return (package.get("id"), package.get("package_version"))


def _view_profile_cache_identity(source_id: int) -> tuple:
    """Phase 7, Step 6E3B, Part 10: cheap (count, max(updated_at)) local-DB
    read over sql_view_profiles for source_id -- same fail-open, never-
    break-the-request contract as _business_knowledge_cache_identity
    above. Included in the plan-cache key so any view re-profiling
    (definition change, schema-fingerprint change, or a core.sqlservice.
    view_profiler.VIEW_PROFILE_CONTRACT_VERSION bump) naturally misses the
    cache instead of ever serving a plan validated against a since-
    superseded view suitability profile."""
    try:
        from data.db import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM sql_view_profiles WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        finally:
            conn.close()
        return (int(row["c"] or 0), str(row["m"] or ""))
    except Exception:  # noqa: BLE001 -- cache-identity lookup must never break the request
        return (0, "")


def _candidate_from_cached_plan(plan: FastPathCachedPlan) -> FastPathGenerationCandidate:
    """Reconstructs a real FastPathGenerationCandidate (never a parallel
    type) from a cache hit -- only ever for request_kind DATA_QUESTION,
    since that is the only kind core.sqlservice.fast_path_plan_cache is
    ever asked to store (see ai_first_fast_path_service's own store call
    below)."""
    return FastPathGenerationCandidate(
        request_kind=_FP_DATA_QUESTION,
        intent=plan.intent,
        selected_object_fqns=plan.selected_object_fqns,
        object_reasons=plan.object_reasons,
        confirmed_relationships_used=(),
        sql=plan.sql,
        clarification_question=None,
        response_text=None,
        decision_evidence=plan.decision_evidence,
        confidence=plan.confidence,
        follow_up_resolution=FastPathFollowUpResolution(
            resolution_type=plan.follow_up_resolution_type,
            resolved_standalone_question=plan.resolved_standalone_question,
        ),
    )


def _candidate_from_durable_plan(plan: "DurablePlan", display_question: str) -> FastPathGenerationCandidate:
    """Phase 7, Step 6H, Part 5 -- durable-store counterpart to
    _candidate_from_cached_plan. `display_question` (the caller's own
    current-turn question text, never anything read back off disk) stands
    in for the resolved_standalone_question the durable tier deliberately
    never persists -- see core.sqlservice.durable_plan_store's module
    docstring."""
    return FastPathGenerationCandidate(
        request_kind=_FP_DATA_QUESTION,
        intent=plan.intent,
        selected_object_fqns=plan.selected_object_fqns,
        object_reasons=plan.object_reasons,
        confirmed_relationships_used=(),
        sql=plan.sql,
        clarification_question=None,
        response_text=None,
        decision_evidence=plan.decision_evidence,
        confidence=plan.confidence,
        follow_up_resolution=FastPathFollowUpResolution(
            resolution_type=plan.follow_up_resolution_type,
            resolved_standalone_question=display_question,
        ),
    )


def _sha256_of_sorted(values: tuple[str, ...]) -> str:
    """Phase 7, Step 6H, Part 4 -- a cheap, audit-only digest of a
    relationship-completion result, stored on fast_path_durable_plans for
    forensic/debugging value only (never part of the lookup key -- see
    core.sqlservice.durable_plan_store's module docstring for why it can't
    be). Never raw table/column names leaked anywhere a key is logged --
    this is a one-way hash, same construction as every other *_state/
    *_digest identity signature already in this pipeline."""
    canonical = ",".join(sorted(values))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_retrieval_question(question: str, prior_turn: Optional[FastPathPriorTurnContext]) -> str:
    """Phase 7, Step 6F, Part 3 -- the text Stage 1's one embedding call
    (and, symmetrically, the plan-cache key -- see fast_path_plan_cache.
    normalize_fast_path_question's own docstring) is built from. A plain,
    deterministic, zero-AI-call concatenation -- never a per-shape
    template (that was the regex-classifier design this step replaces) --
    chosen because Stage 1 runs BEFORE Stage 2's one combined call ever
    sees CONVERSATION_CONTEXT, so retrieval still needs some text that
    carries the prior topic forward for a low-content follow-up like "Sort
    them by last name." to retrieve the right candidate frontier at all.
    core.sqlservice.schema_frontier's own candidate pool is wide (100
    embedding-ranked candidates, AI_FIRST_SCHEMA_FRONTIER_SIZE) specifically
    so a moderately noisy concatenated query still reliably surfaces the
    genuinely relevant objects for EITHER the old or the new topic -- the
    one combined call's own schema-eligibility/validation/execution stages
    below are completely unchanged and remain the real safety backstop
    against ever selecting or executing against the wrong ones."""
    if prior_turn is None:
        return question
    return f"{prior_turn.resolved_standalone_question} {question}"


def _stage_exception_outcome(exc: Exception, stage: str) -> tuple[str, str]:
    """Same sanitization contract as core.sqlservice.
    ai_first_result._stage_exception_outcome."""
    if isinstance(exc, (SchemaFrontierValidationError, FastPathGenerationValidationError)):
        return str(exc), ERROR_CATEGORY_INVALID_RESPONSE
    if isinstance(exc, (EmbeddingConfigurationError, FastPathGenerationConfigurationError)):
        return f"The {stage} provider is not configured correctly.", ERROR_CATEGORY_CONFIGURATION
    return f"The {stage} provider call failed.", ERROR_CATEGORY_PROVIDER


def _selection_adapter(candidate, provider: FastPathGenerationProvider, model: str) -> SchemaSelectionResult:
    """Wraps the fast path's own FastPathGenerationCandidate as a
    core.sqlservice.schema_selection_service.SchemaSelectionResult/
    SchemaSelectionCandidate — pure in-memory reshaping of already-
    validated fields, never a second AI call — so core.sqlservice.
    ambiguity_guard.check_ambiguity() and core.sqlservice.
    ai_first_result.build_ai_first_orchestration_result
    can both reuse their existing, unchanged contracts unmodified."""
    shared_requirements = candidate.decision_evidence or ("fast-path combined selection",)
    object_reasons = tuple(
        ObjectSelectionReason(
            object_fqn=r.object_fqn, supported_requirements=shared_requirements, supporting_columns=r.supporting_columns,
        )
        for r in candidate.object_reasons
    )
    intent_summary = IntentSummary(
        operation=candidate.intent.operation, entities=candidate.intent.entities, measures=candidate.intent.measures,
        filters=candidate.intent.filters, dates=candidate.intent.date_constraints, groupings=candidate.intent.groupings,
    )
    selection_candidate = SchemaSelectionCandidate(
        response_status=_SELECTION_RESPONSE_SELECTED, intent_summary=intent_summary,
        selected_object_fqns=candidate.selected_object_fqns, object_reasons=object_reasons,
        clarification_question=None,
    )
    return SchemaSelectionResult(
        status=_SELECTION_STATUS_OK, candidate=selection_candidate, build_id=None, schema_fingerprint=None,
        provider_id=provider.provider_id, model=model, warnings=(), error=None,
    )


def _generation_adapter(
    candidate, provider: FastPathGenerationProvider, model: str, *, build_id: int, schema_fingerprint: str | None,
) -> SqlGenerationResult:
    """Wraps the fast path's own SQL text as a core.sqlservice.
    sql_generation_service.SqlGenerationResult/SqlGenerationCandidate so
    core.sqlservice.sql_validation.validate_sql_candidate() and (for a
    genuinely complex result) core.sqlservice.answer_generation_service.
    generate_answer() can both reuse their existing, unchanged contracts.

    `build_id`/`schema_fingerprint` MUST be the same build the schema_
    context passed to validate_sql_candidate() was itself built from --
    that function raises ValueError on any mismatch (its own build-
    identity cross-check), exactly as it already does for the existing
    pipeline's real SqlGenerationResult."""
    interpretation = "; ".join(candidate.decision_evidence) or "Fast-path combined selection and SQL generation."
    gen_candidate = SqlGenerationCandidate(
        sql=candidate.sql, interpretation=interpretation[:500], referenced_object_fqns=candidate.selected_object_fqns,
        assumptions=(), needs_clarification=False, clarification_question=None, selected_option_id=None,
    )
    return SqlGenerationResult(
        status=_GEN_STATUS_OK, candidate=gen_candidate, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id=provider.provider_id, model=model, warnings=(), error=None,
    )


def answer_question_ai_first_fast_path(
    question: str,
    source_id: int,
    user_id: str,
    embedding_provider: EmbeddingProvider,
    fast_path_provider: FastPathGenerationProvider,
    answer_provider: Optional[AnswerGenerationProvider] = None,
    *,
    connection_factory: Optional[ConnectionFactory] = None,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float = AI_FIRST_SQL_ORCHESTRATION_BUDGET_SECONDS,
    plan_cache_enabled: Optional[bool] = None,
    durable_plan_cache_enabled: Optional[bool] = None,
    prior_turn: Optional[FastPathPriorTurnContext] = None,
) -> AiFirstOrchestrationResult:
    """
    Run the fast-path question-to-answer pipeline for one question against
    one already-prepared source.

    `plan_cache_enabled` defaults to core.config.AI_FIRST_SQL_FAST_PATH_
    PLAN_CACHE_ENABLED when None (the normal case); tests pass an explicit
    True/False to exercise either behavior without needing to monkeypatch
    config. On a cache HIT (core.sqlservice.fast_path_plan_cache), Stage 1
    (the one embedding call) and Stage 2 (the one GPT-6 Sol call) are both
    skipped entirely -- every stage from confirmed-relationship completion
    onward still runs exactly as it does on a miss, so a schema, PII, or
    governance change since the plan was cached is still caught by
    validate_sql_candidate() before anything executes, and the SQL is
    always re-executed live so returned values stay current.

    Phase 7, Step 6H, Part 5 -- `durable_plan_cache_enabled` (defaults to
    core.config.AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED) gates a
    SECOND, SQLite-persisted tier (core.sqlservice.durable_plan_store),
    checked only when the in-memory tier above already missed. The runtime
    hierarchy is therefore: in-memory cache -> durable store -> embedding +
    GPT-6 Sol only if BOTH miss. A durable hit carries the exact same
    "never a shortcut past validation/execution" contract as an in-memory
    hit -- it is keyed on core.secrets.datasource_identity's stable
    identity (not source_id), so it is the one tier that can still hit
    after a disconnect/reconnect of the same physical database, and the
    one tier that survives a process/backend restart.

    `answer_provider` is OPTIONAL: when the executed result is simple
    (core.sqlservice.fast_path_answer_formatter.is_simple_result() —
    covers zero rows, any single-row result, and a bounded-width multi-row
    list/table), the answer is formatted deterministically and
    `answer_provider` is never called regardless of whether one was
    supplied. It is used ONLY for the rare, genuinely wide multi-row result
    is_simple_result() itself judges too complex to format safely — and
    only if a caller supplied one; when none was supplied, a neutral
    deterministic summary is used instead (Part 5's own two allowed
    designs). Either way, this is never part of the normal simple-question
    path.

    Returns the SAME AiFirstOrchestrationResult/AiFirstStageDurations
    shape documented in core.sqlservice.ai_first_result.

    `prior_turn` (Phase 7, Step 6F, Part 3): optional bounded context
    describing the immediately preceding turn (see core.sqlservice.
    fast_path_prior_turn.FastPathPriorTurnContext). When given, Stage 2's
    ONE combined call — not a second model call — both classifies whether
    `question` continues that prior turn and, if so, resolves it to one
    standalone question (see core.sqlservice.fast_path_generation_service's
    own STEP 0 prompt instructions and `follow_up_resolution` response
    field). `question` itself is ALWAYS the raw, as-typed message — never
    pre-combined by a template — reusing the pipeline's single existing
    AI call rather than adding another one, exactly as required. The
    result's own `resolved_standalone_question`/`follow_up_resolution_type`/
    `next_turn_context` fields carry the outcome back to the caller.
    """
    if not isinstance(budget_seconds, (int, float)) or isinstance(budget_seconds, bool) or budget_seconds <= 0:
        raise ValueError(f"budget_seconds must be a positive number, got {budget_seconds!r}")

    start = clock()
    durations: dict = {}
    # Phase 7, Step 6E3E, Part 1 -- initialized here (before _result below is
    # ever called, including the early STATUS_NOT_PREPARED return) so the
    # closure's read of this enclosing-scope variable can never hit an
    # UnboundLocalError on the short-circuit paths that return before the
    # real cache lookup further down ever runs.
    cached_plan = None
    # Phase 7, Step 6H, Part 5 -- same reasoning as cached_plan above, for
    # the new durable (SQLite-persisted) tier.
    durable_plan_hit = None
    # Phase 7, Step 6F, Part 3/4 -- same reasoning: read by _result() on
    # EVERY return (including early/failure returns), set once Stage 2 (or
    # a cache hit) actually resolves a candidate, so a caller can always
    # see what the model decided even when a LATER stage (validation,
    # execution) is what ultimately fails. next_turn_context stays None
    # unless/until a genuine DATA_QUESTION candidate is resolved.
    resolved_standalone_question = None
    follow_up_resolution_type = None
    next_turn_context = None

    def _elapsed_ms(since: float) -> int:
        return int((clock() - since) * 1000)

    def _total_ms() -> int:
        return _elapsed_ms(start)

    def _budget_exhausted() -> bool:
        return (clock() - start) >= budget_seconds

    def _result(status: str, **kwargs) -> AiFirstOrchestrationResult:
        built = build_ai_first_orchestration_result(
            status,
            source_id=source_id, build_row=build_row,
            embedding_provider=embedding_provider, schema_selection_provider=fast_path_provider,
            text_to_sql_provider=fast_path_provider, answer_provider=answer_provider if answer_provider is not None else _NO_ANSWER_PROVIDER,
            durations=durations, total_ms=_total_ms(),
            cache_hit=(cached_plan is not None or durable_plan_hit is not None),
            resolved_standalone_question=resolved_standalone_question,
            follow_up_resolution_type=follow_up_resolution_type,
            next_turn_context=next_turn_context,
            **kwargs,
        )
        logger.info(
            "answer_question_ai_first_fast_path: source_id=%s build_id=%s status=%s frontier_size=%s "
            "selector_status=%s total_ms=%s",
            source_id, built.build_id, built.status, built.frontier_size, built.selector_status, built.durations.total_ms,
        )
        return built

    build_row: Optional[StoredRetrievalBuild] = get_latest_ready_retrieval_build(source_id, user_id)
    if build_row is None:
        return _result(STATUS_NOT_PREPARED, error=f"No READY retrieval build exists for source_id={source_id}.")

    # Phase 7, Step 6F, Part 4 -- for a follow-up (prior_turn is not None),
    # both the cache LOOKUP and, on a miss, the cache STORE are keyed on
    # this SAME deterministic concatenation (never the model's own
    # resolved_standalone_question, which is only known after Stage 2 runs
    # -- see fast_path_plan_cache.normalize_fast_path_question's own
    # docstring for exactly why). For a fresh question (prior_turn is
    # None) this is simply `question` itself, byte-for-byte the same cache
    # key text as before this change -- zero behavior change for the
    # non-follow-up case.
    cache_question = _build_retrieval_question(question, prior_turn)

    cache_enabled = AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED if plan_cache_enabled is None else plan_cache_enabled
    durable_enabled = (
        AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED
        if durable_plan_cache_enabled is None else durable_plan_cache_enabled
    )
    cache_key = None
    cached_plan = None
    durable_key = None
    durable_plan_hit = None
    if cache_enabled or durable_enabled:
        bk_package_id, bk_package_version = _business_knowledge_cache_identity(source_id, user_id)
        from core.dictionary.pii_detector import SECRET_POLICY_VERSION
        view_profile_state = _view_profile_cache_identity(source_id)

        if cache_enabled:
            cache_key = build_fast_path_plan_cache_key(
                question=cache_question, user_id=user_id, source_id=source_id,
                schema_fingerprint=build_row.schema_fingerprint,
                business_knowledge_package_id=bk_package_id, business_knowledge_package_version=bk_package_version,
                fast_path_model=fast_path_provider.model,
                fast_path_reasoning_effort=getattr(fast_path_provider, "reasoning_effort", None),
                fast_path_prompt_version=FAST_PATH_PROMPT_VERSION,
                view_profile_state=view_profile_state,
                secret_policy_version=SECRET_POLICY_VERSION,
            )
            cached_plan = get_cached_fast_path_plan(cache_key)

        # Phase 7, Step 6H, Part 5 -- durable tier checked only on an
        # in-memory miss. Keyed on the stable datasource identity (not
        # source_id) so a reconnect of the same physical database -- even
        # one that reuses the existing source_id, per
        # data.datasource_service.reconnect_data_source -- can still hit.
        # No identity key yet (pre-migration row, or a source never
        # backfilled) simply disables this tier for the request, never an
        # error.
        if durable_enabled and cached_plan is None:
            identity_key = get_datasource_identity_key(source_id, user_id)
            if identity_key is not None:
                durable_key = build_durable_plan_key(
                    question=cache_question, datasource_identity_key=identity_key, user_id=user_id,
                    schema_fingerprint=build_row.schema_fingerprint, retrieval_build_id=build_row.id,
                    business_knowledge_package_id=bk_package_id, business_knowledge_package_version=bk_package_version,
                    fast_path_model=fast_path_provider.model,
                    fast_path_reasoning_effort=getattr(fast_path_provider, "reasoning_effort", None),
                    fast_path_prompt_version=FAST_PATH_PROMPT_VERSION,
                    view_profile_state=view_profile_state,
                    secret_policy_version=SECRET_POLICY_VERSION,
                )
                durable_plan_hit = get_durable_plan(
                    durable_key, ttl_seconds=AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_TTL_SECONDS,
                )

    frontier_for_result: Optional[SchemaFrontierResult] = None

    if cached_plan is not None or durable_plan_hit is not None:
        # ── Stage 1+2 replaced by a cache hit (in-memory OR durable) — zero
        #    embedding calls, zero GPT-6 Sol calls. Every stage below runs
        #    exactly as it does on a miss. ──────────────────────────────
        durations["frontier_ms"] = 0
        durations["selection_ms"] = 0
        if cached_plan is not None:
            candidate = _candidate_from_cached_plan(cached_plan)
            hit_model = cached_plan.model
        else:
            # The durable tier never persists resolved_standalone_question
            # (Part 4 -- no raw/derived question text on disk), so the
            # CURRENT turn's own already-in-hand question text stands in for
            # display -- never a previous turn's text read back off disk.
            candidate = _candidate_from_durable_plan(durable_plan_hit, cache_question)
            hit_model = durable_plan_hit.model
        selection_result = _selection_adapter(candidate, fast_path_provider, hit_model)
        effective_model = hit_model
        resolved_standalone_question = candidate.follow_up_resolution.resolved_standalone_question
        follow_up_resolution_type = candidate.follow_up_resolution.resolution_type
    else:
        # ── Stage 1: fast-path context (one embedding call, reused frontier) ─
        if _budget_exhausted():
            return _result(STATUS_BUDGET_EXCEEDED, error=f"Question-time budget exceeded before {_STAGE_CONTEXT}.")

        context_start = clock()
        try:
            fast_context = build_fast_path_context(cache_question, build_row.id, source_id, user_id, embedding_provider)
        except (SchemaFrontierValidationError, EmbeddingConfigurationError, EmbeddingProviderError) as exc:
            durations["frontier_ms"] = _elapsed_ms(context_start)
            message, category = _stage_exception_outcome(exc, _STAGE_CONTEXT)
            return _result(STATUS_RETRIEVAL_FAILED, error=message, error_category=category)
        durations["frontier_ms"] = _elapsed_ms(context_start)

        if fast_context.status != _FAST_CONTEXT_STATUS_OK or not fast_context.frontier.candidates:
            return _result(
                STATUS_RETRIEVAL_FAILED,
                candidate_frontier=fast_context.frontier,
                error=fast_context.error or f"fast-path context status={fast_context.status!r} carries zero candidates",
            )
        frontier_for_result = fast_context.frontier

        # ── Stage 2: the ONE combined call (request kind + intent + selection
        #    + SQL generation) ────────────────────────────────────────────────
        if _budget_exhausted():
            return _result(
                STATUS_BUDGET_EXCEEDED, candidate_frontier=fast_context.frontier,
                error=f"Question-time budget exceeded before {_STAGE_GENERATION}.",
            )

        generation_start = clock()
        try:
            generation_result = generate_fast_path_candidate(
                question, fast_context, fast_path_provider,
                max_objects=AI_FIRST_SQL_FAST_PATH_MAX_OBJECTS, max_output_tokens=AI_FIRST_SQL_FAST_PATH_MAX_OUTPUT_TOKENS,
                timeout_seconds=AI_FIRST_SQL_FAST_PATH_TIMEOUT_SECONDS, prior_turn=prior_turn,
            )
        except (FastPathGenerationValidationError, FastPathGenerationConfigurationError, FastPathGenerationProviderError) as exc:
            durations["selection_ms"] = _elapsed_ms(generation_start)
            message, category = _stage_exception_outcome(exc, _STAGE_GENERATION)
            return _result(
                STATUS_SELECTION_FAILED, candidate_frontier=fast_context.frontier, error=message, error_category=category,
            )
        durations["selection_ms"] = _elapsed_ms(generation_start)

        if generation_result.status != _FAST_GEN_STATUS_OK or generation_result.candidate is None:
            return _result(
                STATUS_SELECTION_FAILED, candidate_frontier=fast_context.frontier,
                error=generation_result.error or f"fast-path generation status={generation_result.status!r}",
            )

        candidate = generation_result.candidate
        selection_result = _selection_adapter(candidate, fast_path_provider, generation_result.model)
        effective_model = generation_result.model
        resolved_standalone_question = candidate.follow_up_resolution.resolved_standalone_question
        follow_up_resolution_type = candidate.follow_up_resolution.resolution_type

        if candidate.request_kind == _FP_NON_DATA:
            return _result(
                STATUS_NON_DATA, candidate_frontier=fast_context.frontier, selection_result=selection_result,
                answer_override=candidate.response_text,
            )
        if candidate.request_kind == _FP_UNSUPPORTED:
            # Even if this classification were somehow wrong, core.sqlservice.
            # sql_validation's deterministic forbidden-construct check (run
            # unconditionally below for every DATA_QUESTION) remains the final
            # backstop against any destructive SQL reaching execution.
            return _result(
                STATUS_UNSUPPORTED, candidate_frontier=fast_context.frontier, selection_result=selection_result,
                answer_override=candidate.response_text,
            )
        if candidate.request_kind == _FP_CLARIFICATION:
            return _result(
                STATUS_CLARIFICATION_REQUIRED, candidate_frontier=fast_context.frontier, selection_result=selection_result,
                clarification_question=candidate.clarification_question,
            )
        assert candidate.request_kind == _FP_DATA_QUESTION  # only remaining allowed value

    # Phase 7, Step 6F, Part 4 — resolved_standalone_question/
    # follow_up_resolution_type were already set above (both the cache-hit
    # and fresh-generation branches set them as soon as `candidate` is
    # known, including for NON_DATA/UNSUPPORTED/CLARIFICATION_REQUIRED
    # returns that happen before reaching here). next_turn_context is only
    # built here, reached only for a genuine DATA_QUESTION candidate — it
    # describes a plan's own structural shape (selected objects/columns/
    # filters/grouping), never a row value, and is only ever CONSULTED by
    # the caller when this request's final status is STATUS_OK (api/v1/
    # ai_first_composer_execution.py's own _next_state_for_result enforces
    # that).
    next_turn_context = next_turn_context_from_candidate(candidate)

    # ── Stage 3: confirmed relationship completion — UNCHANGED ──────────
    relationship_start = clock()
    relationship_result = complete_relationships(
        candidate.selected_object_fqns, build_row.id, source_id, user_id, embedding_provider,
        max_connectors=AI_FIRST_SCHEMA_MAX_CONNECTORS,
    )
    durations["relationship_ms"] = _elapsed_ms(relationship_start)

    if relationship_result.status == _RELATIONSHIP_STATUS_DISCONNECTED:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_CLARIFICATION_REQUIRED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result,
            clarification_question=(
                "The information needed to answer this question could not be confirmed as safely joinable -- "
                "could you narrow or rephrase the question?"
            ),
        )
    if relationship_result.status != _RELATIONSHIP_STATUS_CONNECTED:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_RELATIONSHIP_COMPLETION_FAILED, candidate_frontier=frontier_for_result,
            selection_result=selection_result, relationship_result=relationship_result,
            error=relationship_result.error or f"relationship completion status={relationship_result.status!r}",
        )

    # ── Stage 4: exact schema-context construction — UNCHANGED ──────────
    context_build_start = clock()
    schema_context = build_exact_schema_context(
        candidate.selected_object_fqns, relationship_result.connector_object_fqns,
        build_row.id, source_id, user_id, embedding_provider,
    )
    durations["context_ms"] = _elapsed_ms(context_build_start)

    if schema_context.status == _CONTEXT_STATUS_INCOMPLETE:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_CLARIFICATION_REQUIRED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            clarification_question=(
                "The information needed to answer this question is too large to fit a reliable query context -- "
                "could you narrow the question?"
            ),
        )
    if schema_context.status != _CONTEXT_STATUS_OK:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_CONTEXT_BUILD_FAILED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            error=schema_context.error or f"schema context status={schema_context.status!r}",
        )

    # ── Stage 4A: generic ambiguity guard — UNCHANGED, reused via adapter ─
    ambiguity_start = clock()
    ambiguity_result = check_ambiguity(
        question, source_id, user_id, selection_result.candidate, relationship_result, schema_context,
        build_row.schema_fingerprint,
    )
    durations["ambiguity_guard_ms"] = _elapsed_ms(ambiguity_start)

    if ambiguity_result.is_ambiguous:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_CLARIFICATION_REQUIRED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            clarification_question=ambiguity_result.clarification_question,
        )

    generation_adapter_result = _generation_adapter(
        candidate, fast_path_provider, effective_model,
        build_id=schema_context.build_id, schema_fingerprint=schema_context.schema_fingerprint,
    )

    # ── Stage 5: SQL validation — UNCHANGED, the final safety authority.
    #    Never overridden; a rejection here always returns a sanitized
    #    failure, never a correction/retry attempt (Part 4's own explicit
    #    restriction for this pilot). Re-run in full on EVERY request,
    #    cache hit or miss, against the CURRENT schema_context -- a cached
    #    plan is never trusted to still be valid. ────────────────────────
    validation_start = clock()
    validation_result = validate_sql_candidate(
        generation_adapter_result, schema_context, intent_evidence=None, source_id=source_id, user_id=user_id,
        # Phase 7, Step 6E3B1, Part 6 — the fast path's own declared
        # population/scope (candidate.intent.filters), the SAME field
        # schema_selection_service's unrestricted_request check already
        # uses on the legacy path. This is the only point that actually
        # enforces it here, since the fast path never runs that check.
        requested_population_filters=candidate.intent.filters,
    )
    durations["validation_ms"] = _elapsed_ms(validation_start)

    if validation_result.status != _VALID_STATUS_OK:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_VALIDATION_FAILED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            error=validation_result.error or f"SQL validation status={validation_result.status!r}",
        )

    # ── Stage 6: live SQL execution — UNCHANGED. Always re-executed, cache
    #    hit or miss, so returned values are always current. ─────────────
    if _budget_exhausted():
        return _result(
            STATUS_BUDGET_EXCEEDED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            error="Question-time budget exceeded before SQL execution.",
        )

    execution_start = clock()
    execution_result = execute_validated_sql(
        validation_result, source_id, user_id, connection_factory=connection_factory, clock=clock,
    )
    durations["execution_ms"] = _elapsed_ms(execution_start)

    if execution_result.status != _EXEC_STATUS_OK:
        if cached_plan is not None and cache_key is not None:
            evict_fast_path_plan(cache_key)
        if durable_plan_hit is not None and durable_key is not None:
            evict_durable_plan(durable_key)
        return _result(
            STATUS_EXECUTION_FAILED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            execution_result=execution_result, error=execution_result.error, error_category=execution_result.error_category,
        )

    # ── Cache store — ONLY a fresh (cache-miss), now fully validated AND
    #    executed, read-only DATA_QUESTION plan is ever written. A hit is
    #    never re-stored (idempotent no-op avoided for its own sake). ────
    if cache_key is not None and cached_plan is None:
        store_fast_path_plan(cache_key, FastPathCachedPlan(
            intent=candidate.intent, selected_object_fqns=candidate.selected_object_fqns,
            object_reasons=candidate.object_reasons, sql=candidate.sql,
            decision_evidence=candidate.decision_evidence, confidence=candidate.confidence,
            model=effective_model,
            resolved_standalone_question=resolved_standalone_question,
            follow_up_resolution_type=follow_up_resolution_type,
        ))

    # ── Durable store — Phase 7, Step 6H, Part 4/5. Only a fresh (durable
    #    miss), now fully validated AND executed, read-only DATA_QUESTION
    #    plan is ever written, exactly like the in-memory tier above. Keyed
    #    on durable_key (built earlier, before Stage 1/2, from the stable
    #    datasource identity) -- a durable HIT is never re-stored. Never
    #    persists resolved_standalone_question/raw question text (see
    #    core.sqlservice.durable_plan_store's module docstring).
    #    relationship_state_digest is audit-only (never part of the lookup
    #    key -- see that module's own docstring for why it cannot be). Best-
    #    effort: a storage failure here is already handled inside
    #    store_durable_plan and must never fail a request whose SQL already
    #    executed successfully. ─────────────────────────────────────────
    if durable_key is not None and durable_plan_hit is None:
        try:
            relationship_state_digest = _sha256_of_sorted(relationship_result.connector_object_fqns)
        except Exception:  # noqa: BLE001 -- audit-only field, never allowed to block storage
            relationship_state_digest = None
        store_durable_plan(
            durable_key,
            DurablePlan(
                intent=candidate.intent, selected_object_fqns=candidate.selected_object_fqns,
                object_reasons=candidate.object_reasons, sql=candidate.sql,
                decision_evidence=candidate.decision_evidence, confidence=candidate.confidence,
                model=effective_model, follow_up_resolution_type=follow_up_resolution_type,
            ),
            ttl_seconds=AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_TTL_SECONDS,
            relationship_state_digest=relationship_state_digest,
            max_rows_per_identity=AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_MAX_ROWS_PER_IDENTITY,
        )

    # ── Deterministic, zero-AI-call result insights (Part 3) — computed
    #    once here from the live execution_result, reused by every answer-
    #    formatting branch below. ─────────────────────────────────────────
    deterministic_insights = build_deterministic_insights(execution_result)

    # ── Stage 7: answer formatting — deterministic by default, the
    #    existing generate_answer() only for a genuinely complex result
    #    AND only when the caller supplied an answer_provider. ───────────
    answer_start = clock()
    if is_simple_result(execution_result):
        try:
            deterministic_answer = format_deterministic_answer(execution_result)
        except Exception:  # noqa: BLE001 -- pure formatting must never crash the request
            deterministic_answer = "Query completed successfully."
        durations["answer_ms"] = _elapsed_ms(answer_start)
        return _result(
            STATUS_OK, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            execution_result=execution_result, answer_override=deterministic_answer,
            deterministic_insights=deterministic_insights,
        )

    if answer_provider is None:
        durations["answer_ms"] = _elapsed_ms(answer_start)
        return _result(
            STATUS_OK, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            execution_result=execution_result,
            answer_override=(
                f"Found {execution_result.row_count:,} matching record(s) across {len(execution_result.columns)} "
                "columns. See the result table for full detail."
            ),
            deterministic_insights=deterministic_insights,
        )

    if _budget_exhausted():
        durations["answer_ms"] = _elapsed_ms(answer_start)
        return _result(
            STATUS_BUDGET_EXCEEDED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            execution_result=execution_result, error="Question-time budget exceeded before answer generation.",
        )

    answer_result = generate_answer(
        question, generation_adapter_result, validation_result, execution_result, answer_provider,
    )
    durations["answer_ms"] = _elapsed_ms(answer_start)

    if answer_result.status not in (_ANSWER_STATUS_OK, _ANSWER_STATUS_NO_ROWS):
        return _result(
            STATUS_ANSWER_FAILED, candidate_frontier=frontier_for_result, selection_result=selection_result,
            relationship_result=relationship_result, schema_context=schema_context,
            generation_result=generation_adapter_result, validation_result=validation_result,
            execution_result=execution_result, answer_result=answer_result,
            error=answer_result.error, error_category=answer_result.error_category,
        )

    return _result(
        STATUS_OK, candidate_frontier=frontier_for_result, selection_result=selection_result,
        relationship_result=relationship_result, schema_context=schema_context,
        generation_result=generation_adapter_result, validation_result=validation_result,
        execution_result=execution_result, answer_result=answer_result,
        deterministic_insights=deterministic_insights,
    )
