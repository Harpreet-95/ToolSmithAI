"""
AI-first fast-path Composer conversation state (versioned, JSON-safe).

A small payload carried in POST /v1/composer/ask's `conversation_state`
request/response field. The frontend hook (frontend/src/hooks/
useConversationState.js) is a generic, backend-authoritative round-tripper:
it never inspects `conversation_state`, just stores whatever the last
response returned and attaches it to the next request for the same data
source -- so this module's job is purely server-side.

(The module/class names keep their historical "sql_pipeline"/"NewPipeline"
spelling; they describe the one conversation-state shape the fast path
uses.)

Pure module: no I/O, no database access, no provider calls. Every function
here either builds a plain dict from already-computed values, or validates
an untrusted dict into a frozen dataclass (or returns None).

── State schema ────────────────────────────────────────────────────────────

{
  "state_type": "sql_pipeline_conversation",   # fixed discriminator (see below)
  "version": 1,                                 # STATE_VERSION — exact match required
  "source_id": 7,                                # int — must equal the CURRENT request's selected_data_source
  "build_id": 42,                                # int | null — informational identity only, never used to
  "schema_fingerprint": "a1b2c3...",              #   skip re-validation; answer_question() always re-resolves
                                                   #   the current READY build itself (requirement: "build/schema
                                                   #   identity must be revalidated by the normal pipeline")
  "last_question": "How many active clients?",    # str — the complete bounded contextualized question so far:
                                                   #   the original question on the FIRST clarification round
                                                   #   (kind="clarification_pending"), the chained combination of
                                                   #   every clarification answer received so far on a SUBSEQUENT
                                                   #   round (Step 7A), or the exact question that was last
                                                   #   answered successfully (kind="answered")
  "kind": "answered" | "clarification_pending",   # single source of truth for "did the previous turn complete
                                                   #   successfully" (requirement 2's own bullet) — "answered"
                                                   #   means yes, "clarification_pending" means no, still waiting
  "pending_clarification_question": "..." | null, # the model's own clarification question — non-null only when
                                                   #   kind == "clarification_pending"
  "clarification_turns": 0,                       # 0 when kind == "answered"; 1 or 2 when
                                                   #   kind == "clarification_pending" (see MAX_CLARIFICATION_TURNS)
  "created_at": "2026-09-05T21:00:00+00:00",       # ISO-8601 UTC — drives the same 600s expiry window
}                                                  #   600s expiry window (CONVERSATION_CONTEXT_MAX_AGE_SECONDS)

Deliberately NEVER stored: result rows, embeddings, retrieval documents/
schema context, credentials, prompts, provider identities/internals, or
user_id (requirement 3 — "never derive user identity or authorization from
it": user identity always comes from the authenticated JWT on the CURRENT
request, exactly like every other pipeline call in this codebase; this
state is never consulted for who the caller is, only for what question
context to continue). There is also no separate "answers accumulated so
far" list — `last_question` itself IS that accumulated context, chained
one combination at a time and always kept under MAX_QUESTION_CHARS (see
exceeds_question_length_limit's own docstring).

── Recognizing this state ──────────────────────────────────────────────────

has_new_pipeline_state_type() checks the fixed "state_type" discriminator;
parse_new_pipeline_state() is strict: it requires state_type == STATE_TYPE
and version == STATE_VERSION exactly and returns None for anything else.
A dict with no pipeline state_type at all (an old client's stale state) is
ignored by the caller and the message is answered as a fresh question.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.config import JWT_SECRET, SQL_ANSWER_MAX_QUESTION_CHARACTERS

__all__ = [
    "STATE_TYPE",
    "STATE_VERSION",
    "MAX_CLARIFICATION_TURNS",
    "MAX_QUESTION_CHARS",
    "KIND_ANSWERED",
    "KIND_CLARIFICATION_PENDING",
    "NewPipelineConversationState",
    "has_new_pipeline_state_type",
    "exceeds_question_length_limit",
    "parse_new_pipeline_state",
    "build_answered_state",
    "build_clarification_state",
    "sign_conversation_state",
]

STATE_TYPE = "sql_pipeline_conversation"
# Phase 7, Step 6F — bumped 1 -> 2: adds the bounded structural follow-up
# fields below (selected_object_fqns/selected_output_columns/filters/
# groupings/date_constraints/operation/requested_attributes/
# follow_up_resolution_type/business_knowledge_*/view_profile_state/
# secret_policy_version). A version-1 round-tripped state (from a client
# that loaded the page before this deploy) fails the exact-version check in
# parse_new_pipeline_state() below -- requirement 3's own "recognized but
# invalid state" path handles that safely (a deterministic reset response,
# never a crash, never a silent downgrade). This state is short-lived and
# client-round-tripped only (never persisted server-side), so no migration
# path is needed -- the user is simply asked to restate the question once.
STATE_VERSION = 3
_SIGNATURE_FIELD = "signature"

# Requirement 6 — a strict maximum of two clarification turns per chain.
MAX_CLARIFICATION_TURNS = 2

KIND_ANSWERED = "answered"
KIND_CLARIFICATION_PENDING = "clarification_pending"
_VALID_KINDS = (KIND_ANSWERED, KIND_CLARIFICATION_PENDING)

# Reused, not reinvented — the SAME question-length bound
# core.sqlservice.answer_generation_service already enforces on a raw
# question before prompting with it. Exported publicly (not just as the
# private alias below) so api/v1/ai_first_composer_execution.py can
# enforce the identical bound on a COMBINED (clarification/follow-up)
# question without importing core.config a second, independent way.
MAX_QUESTION_CHARS = SQL_ANSWER_MAX_QUESTION_CHARACTERS
_MAX_QUESTION_CHARS = MAX_QUESTION_CHARS
_MAX_SCHEMA_FINGERPRINT_CHARS = 128

# Phase 7, Step 6F, Part 2 -- bounds for the new structural follow-up
# fields. Deliberately small and generic (never a row/value bound) -- these
# describe SHAPE (which objects/columns/filters were used), never DATA
# (never a returned value, PII, or row). Mirrors core.sqlservice.
# fast_path_generation_service's own _MAX_INTENT_LIST_ITEMS/_MAX_INTENT_
# ITEM_CHARACTERS/_MAX_OBJECT_FQN_CHARACTERS/_MAX_COLUMN_NAME_CHARACTERS
# bounds (not imported from there to keep this module pipeline-agnostic,
# per its own docstring -- just the same order of magnitude).
_MAX_STRUCTURAL_LIST_ITEMS = 25
_MAX_STRUCTURAL_ITEM_CHARS = 256
_MAX_OPERATION_CHARS = 50
_MAX_RESOLUTION_TYPE_CHARS = 32
_MAX_VIEW_PROFILE_STATE_ITEMS = 4
_MAX_VIEW_PROFILE_STATE_ITEM_CHARS = 64

# Requirement 5 — a client-supplied created_at claiming to be from the
# future (beyond ordinary clock skew) is never trusted at face value: it
# would otherwise let a forged, implausibly-recent timestamp dodge expiry
# indefinitely. Small enough to never reject a genuinely fresh, real state.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5

# One bounded conversation lifetime: a state older than this is treated as
# expired and never reused (10 minutes).
CONVERSATION_CONTEXT_MAX_AGE_SECONDS = 600


@dataclass(frozen=True)
class NewPipelineConversationState:
    source_id: int
    build_id: Optional[int]
    schema_fingerprint: Optional[str]
    last_question: str
    kind: str
    pending_clarification_question: Optional[str]
    clarification_turns: int
    created_at: str
    # Phase 7, Step 6F, Part 2 -- the bounded "follow-up contract": only
    # STRUCTURE (which objects/columns/filters/grouping the last successful
    # turn used), never returned row values, raw PII values, credentials,
    # vectors, or complete prompts. All default to empty/None so every
    # EXISTING call site (build_clarification_state, and any legacy caller
    # that never passes them) keeps compiling and behaving exactly as
    # before -- only the new AI-first fast-path call sites populate them
    # with real data (see core.sqlservice.fast_path_prior_turn).
    selected_object_fqns: tuple = ()
    selected_output_columns: tuple = ()
    filters: tuple = ()
    groupings: tuple = ()
    date_constraints: tuple = ()
    operation: Optional[str] = None
    requested_attributes: tuple = ()
    follow_up_resolution_type: Optional[str] = None
    business_knowledge_package_id: Optional[int] = None
    business_knowledge_package_version: Optional[int] = None
    view_profile_state: tuple = ()
    secret_policy_version: Optional[int] = None

    def to_dict(self) -> dict:
        return sign_conversation_state({
            "state_type": STATE_TYPE,
            "version": STATE_VERSION,
            "source_id": self.source_id,
            "build_id": self.build_id,
            "schema_fingerprint": self.schema_fingerprint,
            "last_question": self.last_question,
            "kind": self.kind,
            "pending_clarification_question": self.pending_clarification_question,
            "clarification_turns": self.clarification_turns,
            "created_at": self.created_at,
            "selected_object_fqns": list(self.selected_object_fqns),
            "selected_output_columns": list(self.selected_output_columns),
            "filters": list(self.filters),
            "groupings": list(self.groupings),
            "date_constraints": list(self.date_constraints),
            "operation": self.operation,
            "requested_attributes": list(self.requested_attributes),
            "follow_up_resolution_type": self.follow_up_resolution_type,
            "business_knowledge_package_id": self.business_knowledge_package_id,
            "business_knowledge_package_version": self.business_knowledge_package_version,
            "view_profile_state": list(self.view_profile_state),
            "secret_policy_version": self.secret_policy_version,
        })


def sign_conversation_state(payload: dict) -> dict:
    """Return a copy with an HMAC signature over its canonical JSON shape."""
    unsigned = {key: value for key, value in payload.items() if key != _SIGNATURE_FIELD}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {**unsigned, _SIGNATURE_FIELD: signature}


def _has_valid_signature(raw: dict) -> bool:
    supplied = raw.get(_SIGNATURE_FIELD)
    if not isinstance(supplied, str) or len(supplied) != 64:
        return False
    expected = sign_conversation_state(raw)[_SIGNATURE_FIELD]
    return hmac.compare_digest(supplied, expected)


def _is_expired(created_at: str, *, now: Optional[datetime] = None) -> bool:
    """
    Requirement 5 — created_at is client round-tripped (it lives inside
    body.conversation_state, the same untrusted payload as everything
    else in this module), so it is never trusted at face value:

    - not a str, or not a datetime.fromisoformat()-parseable value -> True
      (rejected exactly like any other malformed field).
    - naive (no tzinfo/utcoffset) -> True. Every timestamp this module
      itself ever writes (build_answered_state / build_clarification_state)
      is timezone-aware; a naive value can only be a forged or corrupted
      client payload, and there is no honest way to know what timezone it
      would even mean, so it is refused rather than silently assumed UTC.
    - implausibly in the future (beyond a small, fixed clock-skew
      tolerance) -> True. A genuinely server-issued timestamp is never
      later than "now" by more than ordinary clock skew; anything further
      ahead is refused rather than trusted as "not expired yet, forever".
    - otherwise: expired iff older than CONVERSATION_CONTEXT_MAX_AGE_SECONDS.

    `now` is accepted purely so callers (production code and tests alike)
    can pass a fixed, deterministic instant instead of this function
    silently reading the wall clock — see parse_new_pipeline_state's own
    `now` parameter, which is the only way this ever receives one.
    """
    if not isinstance(created_at, str):
        return True
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if created.tzinfo is None or created.utcoffset() is None:
        return True
    created_utc = created.astimezone(timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    age_seconds = (current - created_utc).total_seconds()
    if age_seconds < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        return True
    return age_seconds > CONVERSATION_CONTEXT_MAX_AGE_SECONDS


def has_new_pipeline_state_type(raw: object) -> bool:
    """
    Requirement 3/4/10 — the ONLY question this function answers is "does
    this dict CLAIM to be this module's state shape", independent of
    whether it goes on to pass full validation. This is what lets
    composer.py distinguish two very different "parse_new_pipeline_state
    returned None" cases:

    - state_type never matched (missing, wrong, or not a dict at all) ->
      this is legacy-shaped (or no) state; requirement 4 says it alone may
      keep going through the legacy ConversationContext path.
    - state_type DID match STATE_TYPE, but parse_new_pipeline_state (or the
      caller's own build/schema-currency check) rejected it anyway
      (malformed/expired/unknown version/source-mismatched/build-schema-
      stale/otherwise invalid) -> requirement 3 says this must NEVER fall
      through to the legacy pipeline; composer.py returns a deterministic
      reset response instead (see api/v1/ai_first_composer_execution.
      build_new_pipeline_state_reset_response).

    Pure/side-effect-free, like every other function in this module — a
    single dict.get(), never a partial/lenient re-validation of anything
    parse_new_pipeline_state already owns.
    """
    return isinstance(raw, dict) and raw.get("state_type") == STATE_TYPE


def exceeds_question_length_limit(question: str) -> bool:
    """
    Requirement 2 — the SAME bound this module already enforces on every
    individual question field (last_question / pending_clarification_
    question), reused here so api/v1/ai_first_composer_execution.py can
    apply it to a COMBINED (clarification-chained or follow-up) question
    before ever calling either SQL pipeline with it.
    """
    return not isinstance(question, str) or len(question) > MAX_QUESTION_CHARS


def _validate_structural_string_list(raw: object, *, max_items: int, max_item_chars: int) -> Optional[tuple]:
    """Untrusted -> a bounded tuple[str, ...], or None (invalid). Mirrors
    core.sqlservice.fast_path_generation_service._require_string_list's own
    shape of check, kept local here (pure, no import) since this module is
    deliberately pipeline-agnostic."""
    if not isinstance(raw, list) or len(raw) > max_items:
        return None
    items = []
    for item in raw:
        if not isinstance(item, str) or len(item) > max_item_chars:
            return None
        items.append(item)
    return tuple(items)


def _validate_optional_str(raw: object, *, max_chars: int) -> tuple[bool, Optional[str]]:
    """Returns (is_valid, value). None is always valid (the field is
    optional); a non-string or an over-length string is invalid."""
    if raw is None:
        return True, None
    if not isinstance(raw, str) or len(raw) > max_chars:
        return False, None
    return True, raw


def _validate_optional_int(raw: object) -> tuple[bool, Optional[int]]:
    if raw is None:
        return True, None
    if not isinstance(raw, int) or isinstance(raw, bool):
        return False, None
    return True, raw


def parse_new_pipeline_state(
    raw: object, *, source_id: Optional[int], now: Optional[datetime] = None,
) -> Optional[NewPipelineConversationState]:
    """
    Untrusted `body.conversation_state` -> a validated
    NewPipelineConversationState, or None.

    Requirement 3 — treats the input as fully untrusted: strict type/length/
    version checks, source_id must equal the CURRENT request's
    selected_data_source, and malformed/stale/mismatched input is always
    ignored safely (returns None) rather than raising or partially trusting
    it. Every field is read individually by name — this function never
    does `dict(raw)`/`**raw`, so an unexpected/extra key in the input can
    never reach the returned dataclass or leak any further (no arbitrary
    dictionary passthrough).
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("state_type") != STATE_TYPE:
        return None
    if raw.get("version") != STATE_VERSION:
        return None
    if not _has_valid_signature(raw):
        return None

    raw_source_id = raw.get("source_id")
    if not isinstance(raw_source_id, int) or isinstance(raw_source_id, bool):
        return None
    if source_id is None or raw_source_id != source_id:
        return None

    raw_build_id = raw.get("build_id")
    if raw_build_id is not None and (not isinstance(raw_build_id, int) or isinstance(raw_build_id, bool)):
        return None

    raw_fingerprint = raw.get("schema_fingerprint")
    if raw_fingerprint is not None and (
        not isinstance(raw_fingerprint, str) or len(raw_fingerprint) > _MAX_SCHEMA_FINGERPRINT_CHARS
    ):
        return None

    raw_last_question = raw.get("last_question")
    if (
        not isinstance(raw_last_question, str)
        or not raw_last_question.strip()
        or len(raw_last_question) > _MAX_QUESTION_CHARS
    ):
        return None

    raw_kind = raw.get("kind")
    if raw_kind not in _VALID_KINDS:
        return None

    raw_pending_question = raw.get("pending_clarification_question")
    if raw_kind == KIND_CLARIFICATION_PENDING:
        if (
            not isinstance(raw_pending_question, str)
            or not raw_pending_question.strip()
            or len(raw_pending_question) > _MAX_QUESTION_CHARS
        ):
            return None
    else:
        if raw_pending_question is not None:
            return None

    raw_turns = raw.get("clarification_turns")
    if not isinstance(raw_turns, int) or isinstance(raw_turns, bool):
        return None
    if raw_kind == KIND_ANSWERED:
        if raw_turns != 0:
            return None
    else:
        if raw_turns < 1 or raw_turns > MAX_CLARIFICATION_TURNS:
            return None

    raw_created_at = raw.get("created_at")
    if not isinstance(raw_created_at, str) or not raw_created_at:
        return None
    if _is_expired(raw_created_at, now=now):
        return None

    # Phase 7, Step 6F, Part 2 -- the bounded structural follow-up fields.
    # Every one of them is OPTIONAL (defaults to empty/None) so a state
    # built by a caller that never populates them (build_clarification_
    # state, or any future non-AI-first caller) still parses cleanly -- but
    # a PRESENT value is still strictly validated, never partially trusted.
    selected_object_fqns = _validate_structural_string_list(
        raw.get("selected_object_fqns", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if selected_object_fqns is None:
        return None
    selected_output_columns = _validate_structural_string_list(
        raw.get("selected_output_columns", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if selected_output_columns is None:
        return None
    filters = _validate_structural_string_list(
        raw.get("filters", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if filters is None:
        return None
    groupings = _validate_structural_string_list(
        raw.get("groupings", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if groupings is None:
        return None
    date_constraints = _validate_structural_string_list(
        raw.get("date_constraints", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if date_constraints is None:
        return None
    requested_attributes = _validate_structural_string_list(
        raw.get("requested_attributes", []), max_items=_MAX_STRUCTURAL_LIST_ITEMS, max_item_chars=_MAX_STRUCTURAL_ITEM_CHARS,
    )
    if requested_attributes is None:
        return None
    view_profile_state = _validate_structural_string_list(
        raw.get("view_profile_state", []),
        max_items=_MAX_VIEW_PROFILE_STATE_ITEMS, max_item_chars=_MAX_VIEW_PROFILE_STATE_ITEM_CHARS,
    )
    if view_profile_state is None:
        return None

    operation_valid, operation = _validate_optional_str(raw.get("operation"), max_chars=_MAX_OPERATION_CHARS)
    if not operation_valid:
        return None
    resolution_type_valid, follow_up_resolution_type = _validate_optional_str(
        raw.get("follow_up_resolution_type"), max_chars=_MAX_RESOLUTION_TYPE_CHARS,
    )
    if not resolution_type_valid:
        return None
    bk_id_valid, bk_package_id = _validate_optional_int(raw.get("business_knowledge_package_id"))
    if not bk_id_valid:
        return None
    bk_version_valid, bk_package_version = _validate_optional_int(raw.get("business_knowledge_package_version"))
    if not bk_version_valid:
        return None
    secret_version_valid, secret_policy_version = _validate_optional_int(raw.get("secret_policy_version"))
    if not secret_version_valid:
        return None

    return NewPipelineConversationState(
        source_id=raw_source_id,
        build_id=raw_build_id,
        schema_fingerprint=raw_fingerprint,
        last_question=raw_last_question,
        kind=raw_kind,
        pending_clarification_question=raw_pending_question,
        clarification_turns=raw_turns,
        created_at=raw_created_at,
        selected_object_fqns=selected_object_fqns,
        selected_output_columns=selected_output_columns,
        filters=filters,
        groupings=groupings,
        date_constraints=date_constraints,
        operation=operation,
        requested_attributes=requested_attributes,
        follow_up_resolution_type=follow_up_resolution_type,
        business_knowledge_package_id=bk_package_id,
        business_knowledge_package_version=bk_package_version,
        view_profile_state=view_profile_state,
        secret_policy_version=secret_policy_version,
    )


def build_answered_state(
    *, source_id: int, build_id: Optional[int], schema_fingerprint: Optional[str], last_question: str,
    now: Optional[datetime] = None,
    selected_object_fqns: tuple = (), selected_output_columns: tuple = (), filters: tuple = (),
    groupings: tuple = (), date_constraints: tuple = (), operation: Optional[str] = None,
    requested_attributes: tuple = (), follow_up_resolution_type: Optional[str] = None,
    business_knowledge_package_id: Optional[int] = None, business_knowledge_package_version: Optional[int] = None,
    view_profile_state: tuple = (), secret_policy_version: Optional[int] = None,
) -> dict:
    """Requirement 7 — a minimal state for a successful new-pipeline
    answer: just enough to validate and continue a follow-up next turn.

    Phase 7, Step 6F, Part 2 — the new structural kwargs are all optional
    (default empty/None) so every existing caller (the legacy multi-call
    AI-first pipeline, the "new SQL pipeline") keeps building the exact
    same minimal state as before. Only api/v1/ai_first_composer_execution.
    py's fast-path call sites pass real values, built from the fast path's
    own already-validated FastPathGenerationCandidate via
    core.sqlservice.fast_path_prior_turn.next_turn_context_from_candidate.

    `now` (Requirement 5) lets tests pin a deterministic created_at instead
    of depending on the wall clock / sleeping past the expiry window;
    production call sites always omit it and get the real current instant."""
    created = now if now is not None else datetime.now(timezone.utc)
    return NewPipelineConversationState(
        source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        last_question=last_question, kind=KIND_ANSWERED,
        pending_clarification_question=None, clarification_turns=0,
        created_at=created.isoformat(),
        selected_object_fqns=tuple(selected_object_fqns), selected_output_columns=tuple(selected_output_columns),
        filters=tuple(filters), groupings=tuple(groupings), date_constraints=tuple(date_constraints),
        operation=operation, requested_attributes=tuple(requested_attributes),
        follow_up_resolution_type=follow_up_resolution_type,
        business_knowledge_package_id=business_knowledge_package_id,
        business_knowledge_package_version=business_knowledge_package_version,
        view_profile_state=tuple(view_profile_state), secret_policy_version=secret_policy_version,
    ).to_dict()


def build_clarification_state(
    *, source_id: int, build_id: Optional[int], schema_fingerprint: Optional[str],
    last_question: str, pending_clarification_question: str, clarification_turns: int,
    now: Optional[datetime] = None,
) -> dict:
    """A resumable state for a pending-clarification result. `last_question`
    is the model's resolved standalone restatement of the question being
    clarified (never the bare raw answer text).

    `now` (Requirement 5) — see build_answered_state's own docstring."""
    created = now if now is not None else datetime.now(timezone.utc)
    return NewPipelineConversationState(
        source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        last_question=last_question, kind=KIND_CLARIFICATION_PENDING,
        pending_clarification_question=pending_clarification_question,
        clarification_turns=clarification_turns,
        created_at=created.isoformat(),
    ).to_dict()
