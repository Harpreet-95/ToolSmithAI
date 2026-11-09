"""
Validated fast-path SQL-plan cache — Phase 7, Step 6E, Part 2.

Memoizes ONLY the output of the fast path's two AI-bearing stages (Stage 1:
core.sqlservice.fast_path_context.build_fast_path_context's one embedding
call; Stage 2: core.sqlservice.fast_path_generation_service.
generate_fast_path_candidate's one GPT-6 Sol call) for an identical repeat
question — never a shortcut around anything downstream. Every stage after
generation (core.sqlservice.schema_relationship_completion.
complete_relationships, core.sqlservice.schema_exact_context.
build_exact_schema_context, core.sqlservice.ambiguity_guard.check_ambiguity,
core.sqlservice.sql_validation.validate_sql_candidate, core.sqlservice.
sql_execution.execute_validated_sql) is documented, by each of those
modules' own docstrings, as pure/local/no-AI-call -- so replaying them on
every cache hit costs nothing beyond one live SQL execution, and a schema or
governance change since the plan was cached is still caught by
validate_sql_candidate() exactly as it would be on a fresh request.

Only a fully-validated, successfully-EXECUTED, read-only DATA_QUESTION plan
is ever stored (core.sqlservice.ai_first_fast_path_service stores it once,
right after Stage 6 execution succeeds) -- a validation failure, execution
failure, clarification, unsupported/non-data classification, or any
exception is never written here.

Cache key identity -- ALL of the following must match for a hit, so a
change to any one of them naturally (not heuristically) misses the cache
rather than ever serving a stale or cross-scoped plan:
  - user_id, source_id            -- never served across a user or a source
  - normalized_question           -- includes any resumed-clarification
                                      text already folded into the question
                                      string by the composer layer (see
                                      _normalize_question's own docstring)
  - schema_fingerprint             -- the retrieval build's own schema
                                      identity; a reconnect/schema change
                                      naturally produces a new fingerprint
  - business_knowledge_identity    -- (package_id, package_version) of the
                                      source's ACTIVE business-knowledge
                                      package, or a fixed "none" sentinel
                                      when there is none
  - fast_path_model / reasoning_effort / FAST_PATH_PROMPT_VERSION
                                      -- the exact model configuration and
                                      prompt shape that produced the plan
  - view_profile_state             -- Phase 7, Step 6E3B, Part 10: a cheap
                                      (count, max(updated_at)) signature of
                                      the source's sql_view_profiles rows,
                                      mirroring core.sqlservice.schema_
                                      object_authority's own _authority_
                                      state idiom -- any view-profile
                                      recomputation (a changed definition,
                                      schema fingerprint, lineage result,
                                      or a core.sqlservice.view_profiler.
                                      VIEW_PROFILE_CONTRACT_VERSION bump)
                                      naturally changes this tuple and
                                      misses the cache, never serving a
                                      plan validated against a since-
                                      superseded view suitability profile
  - secret_policy_version          -- core.dictionary.pii_detector.
                                      SECRET_POLICY_VERSION; any widening
                                      or narrowing of the restricted-
                                      column token/substring lists
                                      invalidates every previously cached
                                      plan instead of silently reusing one
                                      validated under a stale policy

TTL + max-entries bound memory and staleness generically -- never a
substitute for the identity-based invalidation above, only a backstop for
entries whose identity never changes but whose usefulness has simply aged
out.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Optional

from core.config import (
    AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_MAX_ENTRIES,
    AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_TTL_SECONDS,
)
from core.sqlservice.fast_path_generation_service import FastPathIntent

__all__ = [
    "FastPathPlanCacheKey",
    "FastPathCachedPlan",
    "build_fast_path_plan_cache_key",
    "normalize_fast_path_question",
    "get_cached_fast_path_plan",
    "store_fast_path_plan",
    "evict_fast_path_plan",
    "clear_fast_path_plan_cache",
    "fast_path_plan_cache_size",
]

_cache: "OrderedDict[FastPathPlanCacheKey, tuple[float, FastPathCachedPlan]]" = OrderedDict()
_cache_lock = RLock()


@dataclass(frozen=True)
class FastPathPlanCacheKey:
    """Immutable, hashable cache identity. Every field is required -- there
    is no "partial key" lookup, so a caller can never accidentally match a
    plan generated under a different user/source/schema/package/model."""

    user_id: str
    source_id: int
    normalized_question: str
    schema_fingerprint: str
    business_knowledge_package_id: Optional[int]
    business_knowledge_package_version: Optional[int]
    fast_path_model: str
    fast_path_reasoning_effort: Optional[str]
    fast_path_prompt_version: str
    view_profile_state: tuple = ()
    secret_policy_version: int = 0


@dataclass(frozen=True)
class FastPathCachedPlan:
    """The bounded, sanitized structural metadata needed to replay Stage 3
    onward -- never raw credentials, prompts, embeddings, or unrestricted
    result rows (none of those are ever part of a FastPathGenerationCandidate
    in the first place, so none can leak into this cache).

    Phase 7, Step 6F, Part 4 -- `resolved_standalone_question`/
    `follow_up_resolution_type` are carried through a cache hit purely for
    HONEST DISPLAY (Technical Details) and for building the NEXT turn's own
    prior-turn context -- never used to decide cache identity (the cache
    KEY, built before this plan is ever stored, already governs that; see
    core.sqlservice.ai_first_fast_path_service's own cache-key construction
    for exactly what text a follow-up is keyed on and why)."""

    intent: FastPathIntent
    selected_object_fqns: tuple
    object_reasons: tuple
    sql: str
    decision_evidence: tuple
    confidence: float
    model: str
    resolved_standalone_question: str
    follow_up_resolution_type: str


def normalize_fast_path_question(question: str) -> str:
    """Lowercased, whitespace-collapsed question text.

    Phase 7, Step 6F -- for a FRESH question (no prior turn), the caller
    passes the raw question text, exactly as before. For a follow-up, the
    TRUE resolved standalone question is only known AFTER Stage 2's one
    combined call runs (core.sqlservice.fast_path_generation_service's
    model-based resolver, not a template) -- it cannot be the pre-call
    LOOKUP key without a second model call to compute it first, which Part
    3 of Step 6F explicitly avoids. core.sqlservice.ai_first_fast_path_
    service therefore keys a follow-up's pre-call lookup on a deterministic,
    zero-AI-call concatenation of (previous resolved question + this turn's
    raw message) instead -- symmetric with what is stored on a miss, so a
    hit can only ever replay a plan generated for that EXACT same
    (previous-question, new-message) pair (plus every other existing
    identity field below, unchanged) -- never an incorrect cross-
    conversation reuse. The model's own true resolved_standalone_question
    is still recorded on the stored FastPathCachedPlan for honest display
    and for the NEXT turn's own prior-turn context -- it is simply not the
    cache KEY for a follow-up, by construction of when it becomes known."""
    return " ".join(str(question or "").split()).strip().lower()


def build_fast_path_plan_cache_key(
    *,
    question: str,
    user_id: str,
    source_id: int,
    schema_fingerprint: str,
    business_knowledge_package_id: Optional[int],
    business_knowledge_package_version: Optional[int],
    fast_path_model: str,
    fast_path_reasoning_effort: Optional[str],
    fast_path_prompt_version: str,
    view_profile_state: tuple = (),
    secret_policy_version: int = 0,
) -> FastPathPlanCacheKey:
    return FastPathPlanCacheKey(
        user_id=str(user_id),
        source_id=int(source_id),
        normalized_question=normalize_fast_path_question(question),
        schema_fingerprint=str(schema_fingerprint or ""),
        business_knowledge_package_id=business_knowledge_package_id,
        business_knowledge_package_version=business_knowledge_package_version,
        fast_path_model=str(fast_path_model or ""),
        fast_path_reasoning_effort=fast_path_reasoning_effort,
        fast_path_prompt_version=str(fast_path_prompt_version or ""),
        view_profile_state=view_profile_state,
        secret_policy_version=int(secret_policy_version),
    )


def get_cached_fast_path_plan(
    key: FastPathPlanCacheKey, *, ttl_seconds: int = AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_TTL_SECONDS,
) -> Optional[FastPathCachedPlan]:
    """Returns None on a miss OR an expired entry (expired entries are
    evicted immediately, never served). Never raises."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, plan = entry
        if ttl_seconds > 0 and (now - stored_at) > ttl_seconds:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return plan


def store_fast_path_plan(
    key: FastPathPlanCacheKey,
    plan: FastPathCachedPlan,
    *,
    max_entries: int = AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_MAX_ENTRIES,
) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), plan)
        _cache.move_to_end(key)
        while max_entries > 0 and len(_cache) > max_entries:
            _cache.popitem(last=False)


def evict_fast_path_plan(key: FastPathPlanCacheKey) -> None:
    """Removes one entry, e.g. after a cache-hit replay fails deterministic
    re-validation or live execution -- prevents repeatedly retrying a now-
    invalid cached plan on every subsequent identical question."""
    with _cache_lock:
        _cache.pop(key, None)


def clear_fast_path_plan_cache() -> None:
    with _cache_lock:
        _cache.clear()


def fast_path_plan_cache_size() -> int:
    with _cache_lock:
        return len(_cache)
