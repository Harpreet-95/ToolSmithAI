"""
AI Analyst Agent — Enterprise Milestones M-28/M-30/M-31.

Shared orchestration contract for natural-language business questions:
answer_business_question() is the one public entry point both production
question routes (POST /v1/composer/ask, POST /v1/sources/{id}/execute-query)
call. No second planner, SQL generator, execution engine, or answer-
generation path is introduced by this module; every step below calls an
existing, unmodified service function.

Reused verbatim (imported lazily inside the functions below, matching this
package's own existing import style in context_builder.py):
  - core.orchestrator.intent_resolver.IntentResolver.resolve()
  - core.orchestrator.context_builder._plan_with_autonomous_preparation()
    — plan_business_query -> build_sql_plan -> generate_sql, with its own
    already-bounded, exactly-one metadata-preparation retry. Unmodified.
  - core.orchestrator.context_builder._extract_ambiguous_terms() /
    _filter_joinable_clarification() (Milestone 6.6 / Sprint 1.5
    clarification detection), _build_business_plan() (M-25 business-plan
    bridge), and _apply_clarification_overrides() (Milestone 6.6
    clarification RESUME — M-31 wires it into this module's own
    _on_plan_resolved, the same way _live_query already calls it) — all
    called here as the same module-level functions _live_query calls, never
    reimplemented.
  - core.live.query_engine.LiveQueryEngine.execute() — governed execution:
    ownership, live_query_enabled, safety gate, PII recheck, row limits,
    timeout, rate limits, audit logging all untouched.
  - core.answering.result_validator.validate_execution_result() (M-27).
  - core.answering.answer_planner.AnswerPlanner.build() — the same
    explanation/citation/recommendation/response construction already used
    for live_query answers, invoked here with a single-item EvidencePackage
    shaped exactly like the data _live_query already hands it.

  - data.investigation_service.inspect_targeted_values() (M-29) — one
    bounded "investigation phase" per question, run once after SQL is
    planned/generated but before execution, purely to gather evidence about
    real column values that no metadata store tracks (distinct category
    values, date coverage). Never alters query_plan/sql_plan/generated_sql;
    never loops; capped at MAX_INVESTIGATION_CALLS.

Milestone M-31 — ConversationContext (this module), _resolve_conversation_
context (isolation/expiry), _classify_follow_up/_build_follow_up_plan
(bounded follow-up merge), and clarification resume/cancel wired directly
into answer_business_question()'s own loop. Bounded to the current and
immediately preceding analytical turn only — never long-term chat memory,
never persisted to a database. See ConversationContext's own docstring.

Not yet integrated: the frontend (M-32).
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from core.answering.models import EnterpriseAnswer
from core.answering.result_validator import ResultValidation, validate_execution_result
from core.orchestrator.models import ResolvedIntent

logger = logging.getLogger(__name__)

# --- Bounded execution limits -----------------------------------------------
MAX_TOTAL_STEPS = 10
MAX_VALIDATION_REVISIONS = 1  # at most one full replan+execute+validate revision after a hard validation failure
MAX_INVESTIGATION_CALLS = 3  # one investigation phase per question, never recursive, never looped

# Milestone M-31 — no repository-configured conversation-context TTL exists
# today, so a small, safe default is used, the same way MAX_TOTAL_STEPS/
# MAX_INVESTIGATION_CALLS above are hardcoded bounds rather than settings.
CONVERSATION_CONTEXT_MAX_AGE_SECONDS = 600  # 10 minutes

_TRACE_SUMMARY_MAX_LEN = 200


class AgentStatus(str, Enum):
    ANSWERED = "answered"
    CLARIFICATION_REQUIRED = "clarification_required"
    SAFELY_REFUSED = "safely_refused"
    GOVERNANCE_BLOCKED = "governance_blocked"
    EXECUTION_FAILED = "execution_failed"
    VALIDATION_FAILED = "validation_failed"
    STEP_LIMIT_REACHED = "step_limit_reached"


@dataclass(frozen=True)
class ConversationContext:
    """
    Milestone M-31 — narrow, request-carried bounded context for the CURRENT
    and immediately preceding analytical turn only. This is not long-term
    chat memory: no field here is ever persisted to a database (there is no
    conversation store in this repository, and this milestone deliberately
    doesn't add one — see is_expired/CONVERSATION_CONTEXT_MAX_AGE_SECONDS and
    _resolve_conversation_context below, the caller round-trips this object's
    to_dict()/from_dict() across requests itself). Never stores credentials,
    raw rows, unrestricted samples, or hidden model reasoning.

    conversation_id/source_id/user_id identify whose turn this is; isolation
    is enforced by _resolve_conversation_context (source_id/user_id are
    checked against the live, authenticated request — the two dimensions
    with an independent trusted reference to check against) and, for
    conversation_id, by whichever caller resolves a prior turn's stored
    context against the current request's own session/conversation
    identifier before ever constructing this object (see
    api/v1/composer.py::_build_conversation_context) — there is no
    independent conversation registry to check conversation_id against.

    previous_query_plan/previous_business_plan are carried verbatim (the
    exact dicts core.orchestrator.context_builder._build_business_plan and
    this module's own query_plan already produce) so a follow-up merge never
    re-derives them a second way. metric/dimensions/filters/time_range are
    the same data in a flatter, directly inspectable/testable shape.
    clarification_selection/cancel_clarification are this turn's resume/
    cancel instruction, not carried state — a caller building the NEXT
    turn's context must never copy these forward (see
    api/v1/agent_response_adapters.build_conversation_state), which is what
    bounds clarification resume to at most one cycle: there is no chained
    "previous clarification" to resume a second time.
    """
    conversation_id: str
    source_id: int
    user_id: str
    turn_number: int = 1
    created_at: Optional[str] = None  # ISO-8601 UTC; drives is_expired()

    previous_question: Optional[str] = None
    previous_effective_question: Optional[str] = None
    previous_query_plan: Optional[dict] = None
    previous_business_plan: Optional[dict] = None
    selected_tables: tuple[str, ...] = field(default_factory=tuple)
    metric: Optional[str] = None
    dimensions: tuple[str, ...] = field(default_factory=tuple)
    filters: tuple[dict, ...] = field(default_factory=tuple)
    time_range: Optional[dict] = None

    clarification_selection: Optional[tuple[dict, ...]] = None
    cancel_clarification: bool = False

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        if not self.created_at:
            return True
        try:
            created = datetime.fromisoformat(self.created_at)
        except (ValueError, TypeError):
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return (current - created).total_seconds() > CONVERSATION_CONTEXT_MAX_AGE_SECONDS

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["ConversationContext"]:
        """Never raises on malformed input — a candidate missing its
        required identity fields is simply not a usable context (returns
        None), the same safe-fallback treatment as no context at all."""
        if not data or not data.get("conversation_id") or data.get("source_id") is None or not data.get("user_id"):
            return None
        try:
            source_id = int(data["source_id"])
        except (TypeError, ValueError):
            return None
        clarification_selection = data.get("clarification_selection")
        return cls(
            conversation_id=str(data["conversation_id"]),
            source_id=source_id,
            user_id=str(data["user_id"]),
            turn_number=int(data.get("turn_number") or 1),
            created_at=data.get("created_at"),
            previous_question=data.get("previous_question"),
            previous_effective_question=data.get("previous_effective_question"),
            previous_query_plan=data.get("previous_query_plan"),
            previous_business_plan=data.get("previous_business_plan"),
            selected_tables=tuple(data.get("selected_tables") or ()),
            metric=data.get("metric"),
            dimensions=tuple(data.get("dimensions") or ()),
            filters=tuple(data.get("filters") or ()),
            time_range=data.get("time_range"),
            clarification_selection=tuple(clarification_selection) if clarification_selection else None,
            cancel_clarification=bool(data.get("cancel_clarification")),
        )

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id, "source_id": self.source_id, "user_id": self.user_id,
            "turn_number": self.turn_number, "created_at": self.created_at,
            "previous_question": self.previous_question,
            "previous_effective_question": self.previous_effective_question,
            "previous_query_plan": self.previous_query_plan,
            "previous_business_plan": self.previous_business_plan,
            "selected_tables": list(self.selected_tables),
            "metric": self.metric, "dimensions": list(self.dimensions),
            "filters": list(self.filters), "time_range": self.time_range,
            "clarification_selection": list(self.clarification_selection) if self.clarification_selection else None,
            "cancel_clarification": self.cancel_clarification,
        }


def _resolve_conversation_context(
    candidate: Optional[ConversationContext], *, source_id: int, user_id: str,
) -> Optional[ConversationContext]:
    """The one isolation/expiry gate every use of conversation context goes
    through, regardless of caller. Returns None (safe fallback to treating
    the question as fresh) unless candidate.source_id/user_id both match the
    live, authenticated request and the context has not expired. Never
    raises — a malformed or mismatched candidate is treated exactly like "no
    context provided"."""
    if candidate is None:
        return None
    if candidate.source_id != source_id or candidate.user_id != user_id:
        return None
    if candidate.is_expired():
        return None
    return candidate


# ---------------------------------------------------------------------------
# Milestone M-31 — bounded follow-up recognition. A strict allow-list of the
# 6 documented forms, not a general classifier: anything that doesn't match
# one of these exact shapes returns None and is always treated as a complete
# new question, never guessed at.
# ---------------------------------------------------------------------------
_FOLLOW_UP_TIME_RANGE_RE = re.compile(
    r"^what about (?:the )?(last|this|next) (day|week|month|quarter|year)s?\??$", re.IGNORECASE,
)
_FOLLOW_UP_LIMIT_RE = re.compile(r"^(top|bottom)\s+(\d+)\s+instead\.?$", re.IGNORECASE)
_FOLLOW_UP_BREAKDOWN_RE = re.compile(r"^break (?:it|that|this) down by ([a-z0-9][a-z0-9 _\-]*?)\.?$", re.IGNORECASE)
_FOLLOW_UP_FILTER_RE = re.compile(r"^only ([a-z0-9][a-z0-9 &'\-]*?)\.?$", re.IGNORECASE)
_FOLLOW_UP_COMPARE_RE = re.compile(r"^compare (?:both|the) periods?\.?$", re.IGNORECASE)

_TOP_BOTTOM_IN_TEXT_RE = re.compile(r"\b(top|bottom)\s+\d+\b", re.IGNORECASE)


def _classify_follow_up(question: str) -> Optional[tuple[str, str]]:
    """Returns (follow_up_type, extracted_value) or None. follow_up_type is
    one of: time_range_change, limit_change, dimension_breakdown, filter_add,
    compare_periods."""
    q = question.strip()
    m = _FOLLOW_UP_TIME_RANGE_RE.match(q)
    if m:
        return "time_range_change", f"{m.group(1)} {m.group(2)}"
    m = _FOLLOW_UP_LIMIT_RE.match(q)
    if m:
        return "limit_change", f"{m.group(1)} {m.group(2)}"
    m = _FOLLOW_UP_BREAKDOWN_RE.match(q)
    if m:
        return "dimension_breakdown", m.group(1).strip()
    m = _FOLLOW_UP_FILTER_RE.match(q)
    if m:
        return "filter_add", m.group(1).strip()
    m = _FOLLOW_UP_COMPARE_RE.match(q)
    if m:
        return "compare_periods", q
    return None


def _forward_filters(context: ConversationContext, *, extra: Optional[dict] = None) -> list[dict]:
    """Converts context.filters (stored in sql_plan "where"-row shape —
    table_fqn/column_name/operator/value, exactly what
    core.orchestrator.context_builder._build_business_plan already produces)
    into the "column"/"operator"/"value" shape data.query_planning_service.
    plan_business_query's own `filters` request key already accepts and
    validates against discovered columns — the same existing, generic
    filter-passthrough mechanism _plan_with_autonomous_preparation already
    exposes via its own `filters=` parameter, never a new one."""
    forwarded = [
        {"column": f["column_name"], "operator": f["operator"], "value": f["value"]}
        for f in (context.filters or ())
        if f.get("column_name") and f.get("operator") is not None
    ]
    if extra:
        forwarded.append(extra)
    return forwarded


def _build_follow_up_plan(
    follow_up_type: str, extracted: str, context: ConversationContext,
) -> Optional[tuple[str, list[dict]]]:
    """Returns (effective_question, filters) to feed into the SAME
    _plan_with_autonomous_preparation pipeline a fresh question already
    uses — never a second planner or a direct query_plan patch. Returns None
    when the follow-up cannot be safely merged (no prior plan to merge
    against), which the caller treats as a safe fallback to a fresh
    question — never a guess.

    "compare_periods" is deliberately never merged here — this codebase has
    no multi-period comparison SQL shape to reuse, so the caller safely
    refuses that form outright rather than reaching this function.
    """
    if not context.previous_effective_question or not context.previous_query_plan:
        return None

    base = context.previous_effective_question
    forwarded_filters = _forward_filters(context)

    if follow_up_type == "time_range_change":
        if context.time_range and context.time_range.get("label"):
            base = base.replace(context.time_range["label"], "").strip()
        return f"{base} {extracted}".strip(), forwarded_filters

    if follow_up_type == "limit_change":
        word, _, number = extracted.partition(" ")
        if _TOP_BOTTOM_IN_TEXT_RE.search(base):
            return _TOP_BOTTOM_IN_TEXT_RE.sub(f"{word} {number}", base, count=1), forwarded_filters
        return f"{word} {number} {base}".strip(), forwarded_filters

    if follow_up_type == "dimension_breakdown":
        return f"{base} by {extracted}".strip(), forwarded_filters

    if follow_up_type == "filter_add":
        dimension_selected = next(
            (
                entry.get("selected") for entry in (context.previous_query_plan.get("dimensions") or [])
                if entry.get("selected") and entry["selected"].get("column_name")
            ),
            None,
        )
        if dimension_selected is None:
            return None
        new_filter = {"column": dimension_selected["column_name"], "operator": "=", "value": extracted}
        return base, forwarded_filters + [new_filter]

    return None


@dataclass(frozen=True)
class TraceStep:
    """One AgentTrace entry. Concise operational evidence only — counts,
    table names, status/reason codes — never chain-of-thought reasoning,
    raw row values, credentials, or connection details."""
    step: int
    tool: str
    status: Literal["ok", "blocked", "failed"]
    input_summary: str
    output_summary: str
    reason_code: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class AgentState:
    """Operational state and evidence only — no hidden chain-of-thought is
    stored anywhere on this object."""
    source_id: int
    user_id: str
    original_question: str
    effective_question: str
    conversation_context: Optional[ConversationContext] = None
    execution_mode: str = "governed"

    # Milestone M-31 — conversation-context/clarification-resume observability.
    conversation_context_used: bool = False
    follow_up_type: Optional[str] = None
    clarification_resumed: bool = False

    detected_intent: Optional[ResolvedIntent] = None
    candidate_tables: list[str] = field(default_factory=list)
    selected_tables: list[str] = field(default_factory=list)
    prepared_tables: list[str] = field(default_factory=list)

    query_plan: Optional[dict] = None
    sql_plan: Optional[dict] = None
    generated_sql: Optional[str] = None
    sql_generation: Optional[dict] = None  # full generate_sql() output — M-30 route-adapter parity
    preparation_trace: Optional[dict] = None  # full metadata-prep trace dict — M-30 route-adapter parity

    investigation_results: list = field(default_factory=list)  # list[InvestigationResult], M-29
    execution_result: Optional[dict] = None
    result_validation: Optional[ResultValidation] = None

    answer: Optional[EnterpriseAnswer] = None
    # The exact evidence dict answer was built from — the same shape
    # core.orchestrator.context_builder._live_query() itself produces per
    # outcome. Exposed so a caller building a different response envelope
    # (e.g. api/v1/composer.py's SQL-request branch, M-30) can reuse
    # build_live_query_package()/composer's own existing _generate_answer()/
    # _extract_warnings()/_determine_status() helpers unchanged, rather than
    # re-deriving business_answer-shaped output a second way.
    answer_evidence_data: Optional[dict] = None
    warnings: list[dict] = field(default_factory=list)
    clarification: Optional[dict] = None

    status: Optional[AgentStatus] = None
    # Short, stable machine-readable code for the terminal stop (e.g.
    # "unowned_source", "no_valid_join", "sql_generation_refused",
    # "clarification_required", "low_confidence_match",
    # "result_validation_failed", "step_limit", or an execution QueryStatus
    # value) — None only when status == ANSWERED. Mirrors the same
    # reason_code already recorded on the corresponding TraceStep, promoted
    # to state level so route adapters (M-30) never have to string-match
    # stop_reason prose to distinguish cases.
    stop_code: Optional[str] = None
    step_count: int = 0
    stop_reason: Optional[str] = None
    trace: list[TraceStep] = field(default_factory=list)


def _truncate(text: str) -> str:
    text = str(text)
    return text if len(text) <= _TRACE_SUMMARY_MAX_LEN else text[: _TRACE_SUMMARY_MAX_LEN - 1] + "…"


def build_live_query_package(
    question: str, data: Optional[dict], source_id: Optional[int] = None,
    intent_confidence: Optional[float] = None,
) -> Any:
    """
    Builds the single-item EvidencePackage core.answering.explanation_builder.
    _explain_live_query() already dispatches on today (a "live_query"
    EvidenceItem carrying the exact dict core.orchestrator.context_builder.
    _live_query() itself produces per outcome — clarification / refused /
    non-success status / success). Public (not underscore-prefixed)
    specifically so a caller outside this module — api/v1/composer.py's
    SQL-request branch, M-30 — can feed it into composer.py's own existing
    _generate_answer()/_extract_warnings()/_determine_status()/
    _extract_governance_state()/_serialize_package() helpers unchanged,
    rather than reimplementing an equivalent legacy-response builder.

    intent_confidence — Rule G. The real core.orchestrator.intent_resolver.
    IntentResolver score for this question (0-1), threaded through from
    answer_business_question's own "resolve_intent" step. Previously this
    package's intent.confidence was a binary 1.0/0.0 flag ("did any data
    come back at all"), completely disconnected from how confident intent
    CLASSIFICATION itself was — that binary flag is kept as the per-evidence-
    item data-presence signal (unchanged, below), but package.intent.confidence
    now carries the real classification score so _explain_live_query can
    derive a final answer confidence that cannot exceed what intent
    resolution actually supports. None (caller had no resolved intent yet,
    e.g. the handful of pre-intent-resolution early exits in
    answer_business_question) falls back to the old binary signal rather
    than fabricating a score.
    """
    from core.orchestrator.models import (
        EvidenceItem, EvidencePackage, IntentType, ResolvedIntent as _ResolvedIntent, ServiceCapability,
    )

    intent = _ResolvedIntent(
        intent_type=IntentType.SQL_REQUEST,
        confidence=intent_confidence if intent_confidence is not None else (1.0 if data else 0.0),
        required_capabilities=[ServiceCapability.LIVE_QUERY],
    )
    item = EvidenceItem(
        evidence_id="live_query",
        source_service="live_query",
        source_function="answer_business_question",
        capability=ServiceCapability.LIVE_QUERY,
        data=data,
        timestamp=datetime.now(timezone.utc),
        confidence=1.0 if data else 0.0,
    )
    return EvidencePackage(
        request_id=str(uuid.uuid4()), query=question, intent=intent,
        evidence=[item], service_calls=[], built_at=datetime.now(timezone.utc),
        source_id=source_id, errors=[], total_evidence_items=1,
        services_attempted=1, services_succeeded=1 if data else 0,
    )


def _build_answer(
    question: str, data: Optional[dict], source_id: Optional[int] = None,
    intent_confidence: Optional[float] = None,
) -> EnterpriseAnswer:
    """Reuses the exact existing answer-generation facade — AnswerPlanner.
    build() over build_live_query_package()'s output. No new answer-
    generation logic."""
    from core.answering.answer_planner import AnswerPlanner
    from core.execution.execution_strategy import ExecutionStrategy, StrategyType

    package = build_live_query_package(question, data, source_id=source_id, intent_confidence=intent_confidence)
    strategy = ExecutionStrategy(strategy_type=StrategyType.SQL_REQUIRED, requires_sql=True, requires_live_data=True)
    return AnswerPlanner().build(strategy, package)


def answer_business_question(
    source_id: int,
    user_id: str,
    question: str,
    *,
    conversation_context: Optional[ConversationContext] = None,
    execution_mode: Optional[str] = None,
) -> AgentState:
    """
    Shared production entry point for natural-language business questions.
    Bounded state-machine loop: at most MAX_TOTAL_STEPS trace steps total,
    at most MAX_VALIDATION_REVISIONS full replan+execute+validate revisions
    after a hard result-validation failure (the metadata-preparation retry
    and the clarification-vs-SQL-generation split are already independently
    bounded inside the reused _plan_with_autonomous_preparation/
    prepare_selected_tables/query_planning_service functions themselves).

    execution_mode is accepted and recorded on AgentState for forward
    compatibility with the typed contract; it does not alter safety
    behavior in this milestone — every path below goes through the same
    governed LiveQueryEngine execution regardless of its value. There is no
    ungoverned "default" execution path to fall back to.

    Milestone M-31 — conversation_context may carry a resumed clarification
    selection/cancellation, or (via previous_query_plan/previous_effective_
    question/etc.) the immediately preceding turn's state for a bounded set
    of follow-up questions. Isolation (source_id/user_id) and expiry are
    enforced by _resolve_conversation_context before any of it is trusted;
    a mismatched, expired, or absent context is always a safe fallback to
    treating `question` as a complete new question — see
    _build_follow_up_plan/_classify_follow_up.
    """
    state = AgentState(
        source_id=source_id, user_id=user_id,
        original_question=question, effective_question=question,
        conversation_context=conversation_context,
        execution_mode=execution_mode or "governed",
    )
    # Rule G — the real IntentResolver score (set once "resolve_intent" below
    # runs), threaded through to _build_answer/build_live_query_package so
    # the final answer confidence can never wildly exceed how confident
    # intent classification itself was. None for the handful of early-exit
    # _stop() calls below that return before intent resolution ever runs
    # (cancelled clarification, unsupported follow-up) — _build_answer
    # treats None as "no intent signal available", never as a high score.
    intent_confidence: Optional[float] = None

    def _step(
        tool: str, status: Literal["ok", "blocked", "failed"],
        input_summary: str, output_summary: str,
        *, reason_code: Optional[str] = None, duration_ms: Optional[float] = None,
    ) -> None:
        state.step_count += 1
        state.trace.append(TraceStep(
            step=state.step_count, tool=tool, status=status,
            input_summary=_truncate(input_summary), output_summary=_truncate(output_summary),
            reason_code=reason_code, duration_ms=duration_ms,
        ))

    def _stop(status: AgentStatus, reason: str, data: Optional[dict], *, code: Optional[str] = None) -> AgentState:
        state.status = status
        state.stop_reason = reason
        state.stop_code = code
        state.answer_evidence_data = data
        state.answer = _build_answer(question, data, source_id=source_id, intent_confidence=intent_confidence)
        return state

    # --- Milestone M-31 — conversation context: cancel / follow-up --------
    resolved_context = _resolve_conversation_context(conversation_context, source_id=source_id, user_id=user_id)
    state.conversation_context = resolved_context

    if resolved_context is not None and resolved_context.cancel_clarification:
        _step(
            "cancel_clarification", "blocked", f"conversation_id={resolved_context.conversation_id}",
            "clarification cancelled by user — no SQL generated or executed",
            reason_code="clarification_cancelled",
        )
        return _stop(
            AgentStatus.SAFELY_REFUSED,
            "Clarification was cancelled; no query was executed.",
            {
                "executed": False, "reason": "clarification_cancelled", "question": question,
                "message": "Clarification was cancelled. No query was executed.",
            },
            code="clarification_cancelled",
        )

    extra_filters: list[dict] = []
    if (
        resolved_context is not None
        and not resolved_context.clarification_selection
        and not resolved_context.cancel_clarification
    ):
        classified = _classify_follow_up(question)
        if classified is not None:
            follow_up_type, extracted = classified
            state.follow_up_type = follow_up_type

            if follow_up_type == "compare_periods":
                _step(
                    "apply_conversation_context", "blocked",
                    f"conversation_id={resolved_context.conversation_id} follow_up_type={follow_up_type}",
                    "multi-period comparison is not supported", reason_code="follow_up_unsupported",
                )
                return _stop(
                    AgentStatus.SAFELY_REFUSED,
                    "Comparing two time periods in a single answer isn't supported yet — "
                    "ask about one period at a time.",
                    {
                        "executed": False, "reason": "follow_up_unsupported", "question": question,
                        "message": "Comparing two time periods in a single answer isn't supported yet — "
                                   "ask about one period at a time.",
                    },
                    code="follow_up_unsupported",
                )

            merged = _build_follow_up_plan(follow_up_type, extracted, resolved_context)
            if merged is not None:
                question, extra_filters = merged
                state.effective_question = question
                state.conversation_context_used = True
                _step(
                    "apply_conversation_context", "ok",
                    f"conversation_id={resolved_context.conversation_id} follow_up_type={follow_up_type}",
                    f"effective_question composed; filters_forwarded={len(extra_filters)}",
                )
            else:
                _step(
                    "apply_conversation_context", "blocked",
                    f"conversation_id={resolved_context.conversation_id} follow_up_type={follow_up_type}",
                    "no prior plan available — falling back to a fresh question",
                    reason_code="no_prior_plan",
                )

    # --- Step: resolve_intent -----------------------------------------------
    from core.orchestrator.intent_resolver import IntentResolver

    t0 = time.monotonic()
    resolved_intent = IntentResolver().resolve(question)
    state.detected_intent = resolved_intent
    intent_confidence = getattr(resolved_intent, "confidence", None)
    _confidence_summary = f"{intent_confidence:.2f}" if intent_confidence is not None else "n/a"
    _step(
        "resolve_intent", "ok", f"question={question!r}",
        f"intent={resolved_intent.intent_type.value} confidence={_confidence_summary}",
        duration_ms=(time.monotonic() - t0) * 1000,
    )

    clarification_step_recorded = False
    revisions_used = 0
    while True:
        # Reset per-iteration evidence: a revision starts a genuinely fresh
        # plan/execute/validate cycle, so warnings from a discarded prior
        # attempt must not accumulate onto the cycle that actually decides
        # the final answer.
        state.warnings = []

        if state.step_count >= MAX_TOTAL_STEPS:
            _step(
                "agent_loop", "blocked", f"step_count={state.step_count}",
                "maximum total agent steps reached", reason_code="step_limit",
            )
            return _stop(
                AgentStatus.STEP_LIMIT_REACHED,
                "Maximum agent steps were reached before an answer could be produced.",
                None, code="step_limit",
            )

        from core.orchestrator.context_builder import (
            NO_VALID_JOIN_MESSAGE,
            _build_business_plan,
            _extract_ambiguous_terms_after_family_collapse,
            _filter_joinable_clarification,
            _plan_with_autonomous_preparation,
        )

        def _on_plan_resolved(query_plan: dict) -> Optional[dict]:
            # Milestone M-31 — clarification RESUME: reuses the exact same
            # _apply_clarification_overrides implementation _live_query
            # already calls, applied here to the agent's own freshly-resolved
            # query_plan before the ambiguity check below ever runs, so a
            # resumed selection is never re-asked as if it were still
            # unresolved.
            if resolved_context is not None and resolved_context.clarification_selection:
                from core.orchestrator.context_builder import _apply_clarification_overrides
                from core.semantic.concept_resolver import extract_query_intent

                distinct_requested = (
                    extract_query_intent(question).get("aggregation_target") == "distinct_entity_count"
                )
                _apply_clarification_overrides(
                    query_plan, list(resolved_context.clarification_selection), source_id, user_id,
                    distinct_requested=distinct_requested,
                )
                state.clarification_resumed = True

            # EDP Day 1 — obvious same-object candidate-family duplicates are
            # collapsed silently inside this call, same as _live_query's own
            # (see context_builder._extract_ambiguous_terms_after_family_collapse).
            ambiguous_terms = _extract_ambiguous_terms_after_family_collapse(
                query_plan, source_id, user_id, question,
            )
            if not ambiguous_terms:
                return None
            ambiguous_terms, no_valid_join = _filter_joinable_clarification(
                ambiguous_terms, source_id, user_id,
            )
            if no_valid_join:
                return {
                    "executed": False, "reason": "no_valid_join",
                    "question": question, "message": NO_VALID_JOIN_MESSAGE,
                }
            reason = (
                "clarification_required" if any(t.get("tied") for t in ambiguous_terms)
                else "low_confidence_match"
            )
            return {
                "executed": False, "reason": reason,
                "question": question, "ambiguous_terms": ambiguous_terms,
            }

        # --- Step: plan_and_generate_sql (plan_business_query -> build_sql_plan
        # -> generate_sql, with its own bounded metadata-preparation retry) ---
        t0 = time.monotonic()
        outcome = _plan_with_autonomous_preparation(
            source_id, user_id, question, filters=extra_filters, on_plan_resolved=_on_plan_resolved,
        )
        dur = (time.monotonic() - t0) * 1000

        if (
            resolved_context is not None and resolved_context.clarification_selection
            and state.clarification_resumed and not clarification_step_recorded
        ):
            clarification_step_recorded = True
            _step(
                "apply_clarification_selection", "ok",
                f"conversation_id={resolved_context.conversation_id} "
                f"terms={len(resolved_context.clarification_selection)}",
                "prior clarification selection applied to the resolved query plan",
            )

        if outcome["outcome"] == "unowned":
            _step(
                "plan_and_generate_sql", "blocked", f"source_id={source_id}",
                "source not found or not owned by this user",
                reason_code="unowned_source", duration_ms=dur,
            )
            return _stop(
                AgentStatus.SAFELY_REFUSED,
                "The requested data source was not found or is not owned by this user.",
                None, code="unowned_source",
            )

        if outcome["outcome"] == "early_exit":
            result = outcome["result"]
            reason = result.get("reason")
            if reason == "unsupported_entity":
                # Day 2E, Task 4/5 — a confirmed-unsupported entity (e.g.
                # Recruiter/Placement with no reliable canonical table for
                # the current metadata revision). Business-worded, table-
                # free message only — never the raw entity list or any
                # physical table name; those stay in `result` for developer
                # diagnostics only, never surfaced to the end user.
                _step(
                    "plan_and_generate_sql", "blocked",
                    f"entities: {', '.join(result.get('unsupported_entities') or [])}",
                    "confirmed unsupported by a cached negative contract — no broad search or AI call",
                    reason_code=reason, duration_ms=dur,
                )
                message = " ".join(result.get("business_messages") or []) or (
                    "I could not find a reliable, verified data source for this request."
                )
                return _stop(AgentStatus.SAFELY_REFUSED, message, result, code=reason)
            if reason == "no_valid_join":
                _step(
                    "validate_join_path", "blocked",
                    "checking joinable combinations across ambiguous terms",
                    "no valid join path across any combination of candidates",
                    reason_code=reason, duration_ms=dur,
                )
                return _stop(AgentStatus.SAFELY_REFUSED, NO_VALID_JOIN_MESSAGE, result, code="no_valid_join")

            ambiguous_terms = result.get("ambiguous_terms") or []
            terms = sorted({t.get("term") for t in ambiguous_terms if t.get("term")})
            _step(
                "request_clarification", "blocked",
                f"ambiguous terms: {', '.join(terms) or 'unknown'}",
                f"{len(ambiguous_terms)} term(s) need clarification",
                reason_code=reason, duration_ms=dur,
            )
            state.clarification = result
            return _stop(
                AgentStatus.CLARIFICATION_REQUIRED,
                "Genuine ambiguity requires user clarification before this question can be answered.",
                result, code=reason,
            )

        query_plan = outcome["query_plan"]
        sql_plan = outcome["sql_plan"]
        generated = outcome["generated"]
        state.query_plan = query_plan
        state.sql_plan = sql_plan
        state.candidate_tables = query_plan.get("tables") or []

        # Day 2B, Task 6 — semantic-contract grounding stage visibility.
        # data.query_planning_service._plan_business_query_impl already
        # attaches this (matched entities, whether every one was fully
        # grounded); this just gives it a trace step alongside the other
        # planning stages. No duration_ms here deliberately: query_plan is
        # expected to be a reproducible function of (source_id, request —
        # e.g. with vs. without a RequestMetadataSession must match
        # byte-for-byte, see tests/test_metadata_search_infrastructure_and_
        # session.py), so wall-clock timing is logged at the source
        # instead (plan_business_query's own logger.info) rather than
        # carried on query_plan; this step's own timing is still covered by
        # the surrounding "plan_and_generate_sql" step's duration_ms.
        # Absent entirely for a question with no search terms at all
        # (grounding never runs) — not an error, so no step then either.
        grounding_info = query_plan.get("grounding")
        if grounding_info and grounding_info["matched_entities"]:
            _step(
                "semantic_grounding", "ok",
                f"matched_entities={', '.join(grounding_info['matched_entities']) or 'none'}",
                f"grounded={', '.join(grounding_info['grounded_entities']) or 'none'}; "
                f"fully_grounded={grounding_info['fully_grounded']}; "
                f"seed_tables={', '.join(grounding_info['seed_table_fqns']) or 'none'}",
            )

        prep_trace = outcome.get("preparation_trace")
        if prep_trace:
            state.prepared_tables = prep_trace.get("prepared_tables") or []
            state.preparation_trace = prep_trace
            _step(
                "prepare_selected_tables", "ok",
                f"candidate_tables={', '.join(prep_trace.get('candidate_tables') or [])}",
                f"prepared={', '.join(state.prepared_tables) or 'none'}; "
                f"skipped={len(prep_trace.get('skipped_tables') or [])}",
            )

        if outcome["outcome"] == "refused":
            _step(
                "generate_sql", "blocked", "building SQL from the resolved semantic plan",
                "SQL generation was refused", reason_code="sql_generation_refused", duration_ms=dur,
            )
            return _stop(
                AgentStatus.SAFELY_REFUSED,
                "This question could not be safely translated into SQL.",
                {
                    "executed": False, "reason": "sql_generation_refused",
                    "explanation": generated.get("explanation") or [],
                    "warnings": generated.get("warnings") or sql_plan.get("warnings") or [],
                },
                code="sql_generation_refused",
            )

        # outcome == "planned"
        state.generated_sql = generated["sql"]
        state.sql_generation = generated
        state.selected_tables = sorted({
            r["table_fqn"] for r in (sql_plan.get("select") or []) if r.get("table_fqn")
        })
        _step(
            "plan_and_generate_sql", "ok",
            f"candidate_tables={', '.join(state.candidate_tables) or 'none'}",
            f"selected_tables={', '.join(state.selected_tables) or 'none'}; sql generated",
            duration_ms=dur,
        )

        # Built here (moved earlier than M-28's original post-execution call)
        # so the investigation phase below can read status_label/date_context
        # — _build_business_plan is a pure function of query_plan/sql_plan
        # only, unaffected by execution; reused again after execution
        # succeeds instead of being recomputed.
        business_plan = _build_business_plan(question, query_plan, sql_plan)

        # --- Investigation phase (M-29): at most MAX_INVESTIGATION_CALLS
        # bounded, read-only probes, run only when metadata alone cannot
        # know a real column value the plan is about to filter on — no
        # metadata store in this codebase tracks actual distinct values or
        # date coverage, so any string-equality or date-range filter is
        # genuinely "metadata insufficient" in that narrow sense. One phase,
        # never recursive, never looped back into; never alters query_plan/
        # sql_plan/generated_sql — purely additive evidence and warnings.
        from data.investigation_service import inspect_targeted_values

        investigation_calls = 0
        for where_entry in (sql_plan.get("where") or []):
            if investigation_calls >= MAX_INVESTIGATION_CALLS:
                break
            entry_table = where_entry.get("table_fqn")
            entry_column = where_entry.get("column_name")
            if not entry_table or not entry_column or entry_table not in state.selected_tables:
                continue

            if where_entry.get("operator") == "=" and isinstance(where_entry.get("value"), str):
                investigation_type = "distinct_values"
                reason = f"confirm the requested filter value exists among real values of {entry_column}"
            elif where_entry.get("operator") == "BETWEEN":
                investigation_type = "min_max"
                reason = f"confirm the requested date range falls within {entry_column}'s actual coverage"
            else:
                continue

            t_inv = time.monotonic()
            inv_result = inspect_targeted_values(
                source_id, user_id, entry_table, entry_column,
                investigation_type=investigation_type, reason=reason,
            )
            investigation_calls += 1
            state.investigation_results.append(inv_result)
            _step(
                "inspect_targeted_values", "ok" if inv_result.valid else "blocked",
                f"reason={reason!r} type={investigation_type} table={entry_table} column={entry_column}",
                f"rows={inv_result.row_count} status={'ok' if inv_result.valid else 'refused'}",
                reason_code=None if inv_result.valid else inv_result.reason,
                duration_ms=inv_result.duration_ms if inv_result.duration_ms else (time.monotonic() - t_inv) * 1000,
            )

            if not inv_result.valid:
                continue
            if investigation_type == "distinct_values" and where_entry["value"] not in (inv_result.sample_values or []):
                state.warnings.append({
                    "type": "investigated_value_not_found", "severity": "LOW",
                    "message": f"The requested value for {entry_column} was not found among sampled distinct values.",
                })
            elif investigation_type == "min_max" and business_plan.get("date_context"):
                min_v, max_v = ((inv_result.sample_values or []) + [None, None])[:2]
                requested_start = business_plan["date_context"].get("start")
                requested_end = business_plan["date_context"].get("end")
                if min_v is not None and max_v is not None and requested_start and requested_end:
                    if str(requested_end) < str(min_v) or str(requested_start) > str(max_v):
                        state.warnings.append({
                            "type": "date_range_outside_coverage", "severity": "LOW",
                            "message": f"The requested date range falls outside {entry_column}'s "
                                       f"observed coverage ({min_v} to {max_v}).",
                        })

        # --- Step: execute_sql (governed execution — blocked_cols enforced) --
        from data.query_execution_service import execute_governed_query

        t0 = time.monotonic()
        query_result, gov_warnings = execute_governed_query(
            source_id, user_id, generated["sql"], sql_plan,
            params=generated["parameters"]["values"],
        )
        dur = (time.monotonic() - t0) * 1000
        state.warnings.extend(gov_warnings)
        data = query_result.to_dict()
        state.execution_result = data

        if data["status"] == "blocked":
            _step(
                "execute_sql", "blocked", "governed execution (ownership/live_query_enabled/safety gate)",
                f"status={data['status']}", reason_code="governance_or_policy_block", duration_ms=dur,
            )
            return _stop(
                AgentStatus.GOVERNANCE_BLOCKED,
                data.get("error") or "Execution was blocked by governance or live-query policy.",
                data, code="governance_or_policy_block",
            )

        if data["status"] != "success":
            _step(
                "execute_sql", "failed", "governed execution",
                f"status={data['status']}", reason_code=data["status"], duration_ms=dur,
            )
            return _stop(
                AgentStatus.EXECUTION_FAILED,
                data.get("error") or f"Execution did not succeed (status={data['status']}).",
                data, code=data["status"],
            )

        data["generated_sql"] = generated["sql"]
        data["sql_generation_explanation"] = generated.get("explanation") or []
        data["business_plan"] = business_plan  # built earlier, before the investigation phase
        _step(
            "execute_sql", "ok", f"row_limit_applied={data.get('row_limit_applied')}",
            f"row_count={data.get('row_count')} truncated={data.get('truncated')}", duration_ms=dur,
        )

        # --- Step: validate_execution_result (M-27) -------------------------
        t0 = time.monotonic()
        validation = validate_execution_result(data, sql_plan=sql_plan)
        dur = (time.monotonic() - t0) * 1000
        state.result_validation = validation
        # Extend, never overwrite — the investigation phase above may have
        # already appended warnings (e.g. investigated_value_not_found) that
        # must survive to the final answer, not be silently discarded.
        state.warnings.extend(validation.warnings)

        if not validation.valid:
            _step(
                "validate_execution_result", "blocked", "structural result validation",
                "; ".join(validation.blocking_reasons),
                reason_code="result_validation_failed", duration_ms=dur,
            )
            if revisions_used < MAX_VALIDATION_REVISIONS and state.step_count < MAX_TOTAL_STEPS:
                revisions_used += 1
                continue  # bounded: at most one full replan+execute+validate revision
            return _stop(
                AgentStatus.VALIDATION_FAILED,
                "; ".join(validation.blocking_reasons) or "The executed result failed structural validation.",
                {
                    "executed": False, "reason": "result_validation_failed",
                    "explanation": validation.blocking_reasons, "warnings": validation.warnings,
                },
                code="result_validation_failed",
            )

        _step(
            "validate_execution_result", "ok", "structural result validation",
            f"shape={validation.result_shape} warnings={len(validation.warnings)}", duration_ms=dur,
        )

        # --- Step: compute_period_comparison_insight (Day 4, Capability 2) --
        # Bounded, best-effort: one supplementary governed query for a
        # time-bound single-scalar aggregate only (data.insight_service
        # itself checks eligibility). Any failure here is silently None —
        # never surfaced as a warning/error and never affects the primary
        # answer already validated above.
        if validation.result_shape in ("scalar_count", "scalar_count_distinct", "scalar_sum", "scalar_avg"):
            t0 = time.monotonic()
            rows = data.get("rows") or []
            current_value = next(iter(rows[0].values()), None) if rows else None
            from data.insight_service import compute_period_comparison_insight

            insight = compute_period_comparison_insight(
                source_id, user_id, business_plan, sql_plan, current_value,
            )
            if insight is not None:
                data["insight"] = insight
                _step(
                    "compute_period_comparison_insight", "ok", f"shape={validation.result_shape}",
                    f"percent_change={insight['percent_change']}", duration_ms=(time.monotonic() - t0) * 1000,
                )

        # --- Step: build_business_answer ------------------------------------
        t0 = time.monotonic()
        state.answer_evidence_data = data
        state.answer = _build_answer(question, data, source_id=source_id, intent_confidence=intent_confidence)
        dur = (time.monotonic() - t0) * 1000
        _step(
            "build_business_answer", "ok", f"result_shape={validation.result_shape}",
            "business answer constructed", duration_ms=dur,
        )
        state.status = AgentStatus.ANSWERED
        state.stop_reason = "Answer constructed from a validated, governed execution result."
        return state
