"""
Unified Governance Engine — Phase 1 Foundation + Phase 2 Policy Engine.

Single governance model for every governed object in ToolSmithAI.
Every future governed type registers here; no new approval engine is ever created.

Registered governed object types:
  dict.table        — data_dictionary_tables
  dict.column       — data_dictionary_columns
  domain.rule       — domain_learning_rules
  domain.refinement — domain_rule_refinement_suggestions
  entity.rule       — entity_learning_rules
  tool.engine       — engine_tools  (core/engine state machine)
  pii.confirmation  — profiling_column_profiles.pii_confirmed

Architecture contract
---------------------
Phase 1 additions:
  1. log_governance_event()    — cross-cutting append-only audit log.
  2. upsert_governance_state() — fast state-map projection for dashboards.
  3. get_governance_profile()  — unified governance metadata for any object.
  4. confirm_pii_column()      — PII confirmation write path.

Phase 2 additions:
  5. evaluate_policies()       — policy engine: hard safety rules + DB-stored policies.
  6. get/create/toggle policy  — CRUD for governance_policies table.
  Profiles returned by get_governance_profile() are automatically enriched with
  policy evaluation results (auto_approval_eligible, blocking_policy, matched_policy).

Policy evaluation order:
  1. Hard safety policies (cannot be disabled, always evaluated first).
  2. DB-stored policies ordered by priority (lower number = higher priority).
  3. Default: no auto-approval.

log_governance_event() and upsert_governance_state() are called by existing
services as non-blocking side-effects after their own writes complete.  A
failure in governance logging never disrupts the approval that triggered it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from data.db import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unified Lifecycle States
# ---------------------------------------------------------------------------

class GovernanceState(str, Enum):
    """Approval lifecycle states shared by every governed object."""
    GENERATED      = "GENERATED"       # Produced by AI / rule engine; not yet reviewed
    SUGGESTED      = "SUGGESTED"       # In review queue; awaiting human action
    NEEDS_REVIEW   = "NEEDS_REVIEW"    # Explicitly flagged for mandatory review
    VALIDATED      = "VALIDATED"       # Confidence threshold met; policy auto-approve eligible
    AUTO_APPROVED  = "AUTO_APPROVED"   # Approved by policy without human intervention
    HUMAN_APPROVED = "HUMAN_APPROVED"  # Explicitly approved by a named human
    REJECTED       = "REJECTED"        # Explicitly rejected; inactive
    DEPRECATED     = "DEPRECATED"      # Was approved; superseded by a newer version
    ARCHIVED       = "ARCHIVED"        # Administratively closed; preserved for audit


# ---------------------------------------------------------------------------
# Policy Engine — Phase 2
# ---------------------------------------------------------------------------

class PolicyAction(str, Enum):
    """Actions a governance policy can trigger."""
    REQUIRE_HUMAN = "REQUIRE_HUMAN"  # Force human review; blocks auto-approval
    AUTO_APPROVE  = "AUTO_APPROVE"   # Object is eligible for auto-approval
    ESCALATE      = "ESCALATE"       # Escalate to Governance Admin; blocks auto-approval
    NO_ACTION     = "NO_ACTION"      # Policy matched but takes no action; next policy checked


@dataclass
class PolicyEvaluationResult:
    """Result of running the policy engine against one GovernanceProfile."""
    auto_approval_eligible: bool
    blocking_policy:        str | None   # Name of the policy that blocked auto-approval
    matched_policy:         str | None   # Name of the first matching policy
    review_required:        bool
    review_reason:          str | None


# ---------------------------------------------------------------------------
# Governed Object Type Registry
# ---------------------------------------------------------------------------

class GovernedObjectType(str, Enum):
    """
    Registry of all governed object types.

    Add new types here as new features are built.
    Never create a separate approval engine for a new type.
    """
    DICT_TABLE        = "dict.table"
    DICT_COLUMN       = "dict.column"
    DOMAIN_RULE       = "domain.rule"
    DOMAIN_REFINEMENT = "domain.refinement"
    ENTITY_RULE       = "entity.rule"
    ENGINE_TOOL       = "tool.engine"
    PII_CONFIRMATION  = "pii.confirmation"


_TYPE_META: dict[str, dict] = {
    "dict.table":        {"display_name": "Dictionary Table",             "source_table": "data_dictionary_tables"},
    "dict.column":       {"display_name": "Dictionary Column",            "source_table": "data_dictionary_columns"},
    "domain.rule":       {"display_name": "Domain Learning Rule",         "source_table": "domain_learning_rules"},
    "domain.refinement": {"display_name": "Domain Refinement Suggestion", "source_table": "domain_rule_refinement_suggestions"},
    "entity.rule":       {"display_name": "Entity Learning Rule",         "source_table": "entity_learning_rules"},
    "tool.engine":       {"display_name": "Engine Tool",                  "source_table": "engine_tools"},
    "pii.confirmation":  {"display_name": "PII Confirmation",             "source_table": "profiling_column_profiles"},
}


# ---------------------------------------------------------------------------
# Confidence Tier
# ---------------------------------------------------------------------------

def _confidence_tier(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.95:
        return "VERY_HIGH"
    if score >= 0.80:
        return "HIGH"
    if score >= 0.60:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# AI Trust Computation
# ---------------------------------------------------------------------------

def _compute_ai_trust(
    state: GovernanceState,
    confidence_score: float | None = None,
    pii_risk: bool = False,
    pii_confirmed: bool = False,
) -> tuple[bool, str | None]:
    """Return (can_ai_use, ai_warning) for any governed object state."""
    # Unconfirmed PII overrides approval state — AI must never silently use it
    if pii_risk and not pii_confirmed:
        return (
            False,
            "Column has PII risk indicators that have not been confirmed by a reviewer.",
        )

    if state in (GovernanceState.HUMAN_APPROVED, GovernanceState.AUTO_APPROVED):
        return True, None

    if state == GovernanceState.VALIDATED:
        return True, "Metadata meets the confidence threshold but has not been human-approved."

    if state == GovernanceState.SUGGESTED:
        score = confidence_score or 0.0
        if score >= 0.70:
            return True, "Metadata is AI-generated and awaiting human review."
        return False, "Metadata confidence is below threshold; human review required before AI use."

    if state == GovernanceState.GENERATED:
        return False, "Metadata is AI-generated and has not yet entered the review queue."

    if state == GovernanceState.NEEDS_REVIEW:
        return False, "Metadata has been flagged for mandatory human review."

    if state == GovernanceState.DEPRECATED:
        return False, "Metadata is deprecated; a superseding approved version should be used."

    if state in (GovernanceState.REJECTED, GovernanceState.ARCHIVED):
        return False, f"Metadata is {state.value.lower()} and must not be used by AI."

    return False, None


# ---------------------------------------------------------------------------
# Governance Profile
# ---------------------------------------------------------------------------

@dataclass
class GovernanceProfile:
    """
    Unified governance metadata for any governed object.

    Built from the source table for the object type — never from the state map
    alone.  The state map is a fast projection; the source table is authoritative.

    Phase 2 additions (all default to False/None for backward compatibility):
      pii_risk               — column carries PII risk signals
      domain_context         — domain or entity name for policy scoping
      auto_approval_eligible — policy engine determined this object can be auto-approved
      blocking_policy        — name of the policy that blocked auto-approval
      matched_policy         — name of the first policy that matched
    """
    object_type_id:    str
    object_id:         str
    approval_state:    GovernanceState
    confidence_score:  float | None
    confidence_tier:   str | None
    confidence_source: str | None
    review_required:   bool
    review_reason:     str | None
    reviewed_by:       str | None
    reviewed_at:       str | None
    created_by:        str | None
    created_at:        str | None
    updated_at:        str | None
    evidence:          list[dict]
    can_ai_use:        bool
    ai_warning:        str | None
    # Phase 2 — policy context (populated by profile builders where available)
    pii_risk:               bool       = False
    domain_context:         str | None = None
    # Phase 2 — policy evaluation results (set by _enrich_profile_with_policy)
    auto_approval_eligible: bool       = False
    blocking_policy:        str | None = None
    matched_policy:         str | None = None

    def to_dict(self) -> dict:
        return {
            "object_type_id":        self.object_type_id,
            "object_id":             self.object_id,
            "approval_state":        self.approval_state.value,
            "confidence_score":      self.confidence_score,
            "confidence_tier":       self.confidence_tier,
            "confidence_source":     self.confidence_source,
            "review_required":       self.review_required,
            "review_reason":         self.review_reason,
            "reviewed_by":           self.reviewed_by,
            "reviewed_at":           self.reviewed_at,
            "created_by":            self.created_by,
            "created_at":            self.created_at,
            "updated_at":            self.updated_at,
            "evidence":              self.evidence,
            "can_ai_use":            self.can_ai_use,
            "ai_warning":            self.ai_warning,
            "pii_risk":              self.pii_risk,
            "domain_context":        self.domain_context,
            "auto_approval_eligible": self.auto_approval_eligible,
            "blocking_policy":       self.blocking_policy,
            "matched_policy":        self.matched_policy,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_evidence(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _rule_status_to_state(approval_status: str) -> GovernanceState:
    return {
        "PENDING":  GovernanceState.SUGGESTED,
        "APPROVED": GovernanceState.HUMAN_APPROVED,
        "REJECTED": GovernanceState.REJECTED,
    }.get(approval_status, GovernanceState.GENERATED)


_ENGINE_TOOL_STATUS_MAP: dict[str, GovernanceState] = {
    "draft":            GovernanceState.GENERATED,
    "pending_approval": GovernanceState.SUGGESTED,
    "approved":         GovernanceState.HUMAN_APPROVED,
    "deprecated":       GovernanceState.DEPRECATED,
}


# ---------------------------------------------------------------------------
# Per-type profile builders (internal)
# ---------------------------------------------------------------------------

def _build_dict_table_profile(row: dict) -> GovernanceProfile:
    obj_id = f"{row['source_id']}:{row['table_fqn']}"

    if row.get("is_approved"):
        state = GovernanceState.HUMAN_APPROVED
        review_required = False
        review_reason = None
    elif row.get("business_name"):
        state = GovernanceState.SUGGESTED
        review_required = True
        review_reason = (
            f"Business name '{row['business_name']}' is AI-generated "
            "and awaiting human approval."
        )
    else:
        state = GovernanceState.GENERATED
        review_required = True
        review_reason = "No dictionary entry has been generated for this table yet."

    can_ai, warn = _compute_ai_trust(state)
    return GovernanceProfile(
        object_type_id    = GovernedObjectType.DICT_TABLE,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = None,
        confidence_tier   = None,
        confidence_source = None,
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = row.get("approved_by"),
        reviewed_at       = row.get("approved_at"),
        created_by        = None,
        created_at        = row.get("created_at"),
        updated_at        = row.get("updated_at"),
        evidence          = [],
        can_ai_use        = can_ai,
        ai_warning        = warn,
        domain_context    = row.get("domain") or None,
    )


def _build_dict_column_profile(row: dict) -> GovernanceProfile:
    obj_id = f"{row['source_id']}:{row['table_fqn']}:{row['column_name']}"
    pii_risk = bool(row.get("pii_risk"))

    if row.get("is_approved"):
        state = GovernanceState.HUMAN_APPROVED
        review_required = False
        review_reason = None
    elif row.get("business_label"):
        state = GovernanceState.SUGGESTED
        review_required = True
        review_reason = (
            f"Business label '{row['business_label']}' is AI-generated "
            "and awaiting human approval."
        )
    else:
        state = GovernanceState.GENERATED
        review_required = True
        review_reason = "No business label has been generated for this column yet."

    can_ai, warn = _compute_ai_trust(state, pii_risk=pii_risk)
    return GovernanceProfile(
        object_type_id    = GovernedObjectType.DICT_COLUMN,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = None,
        confidence_tier   = None,
        confidence_source = None,
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = row.get("approved_by"),
        reviewed_at       = row.get("approved_at"),
        created_by        = None,
        created_at        = row.get("created_at"),
        updated_at        = row.get("updated_at"),
        evidence          = [],
        can_ai_use        = can_ai,
        ai_warning        = warn,
        pii_risk          = pii_risk,
    )


def _build_rule_profile(row: dict, object_type: str) -> GovernanceProfile:
    obj_id = str(row["id"])
    state = _rule_status_to_state(row["approval_status"])
    confidence = float(row["confidence"]) if row.get("confidence") is not None else None
    review_required = state == GovernanceState.SUGGESTED

    if review_required and confidence is not None:
        review_reason: str | None = (
            f"{row['pattern_type']} rule '{row['pattern_value']}' "
            f"suggested with {confidence:.0%} confidence; awaiting human approval."
        )
    elif review_required:
        review_reason = (
            f"{row['pattern_type']} rule '{row['pattern_value']}' "
            "awaiting human approval."
        )
    else:
        review_reason = None

    can_ai, warn = _compute_ai_trust(state, confidence)
    # domain field exists on domain_learning_rules; entity field on entity_learning_rules
    _domain_ctx = row.get("domain") or row.get("entity") or None
    return GovernanceProfile(
        object_type_id    = object_type,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = confidence,
        confidence_tier   = _confidence_tier(confidence),
        confidence_source = "learning_engine",
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = row.get("approved_by"),
        reviewed_at       = row.get("approved_at"),
        created_by        = row.get("created_by"),
        created_at        = row.get("created_at"),
        updated_at        = row.get("approved_at") or row.get("created_at"),
        evidence          = [],
        can_ai_use        = can_ai,
        ai_warning        = warn,
        domain_context    = _domain_ctx,
    )


def _build_refinement_profile(row: dict) -> GovernanceProfile:
    obj_id = str(row["id"])
    state = _rule_status_to_state(row["approval_status"])
    confidence = float(row["confidence"]) if row.get("confidence") is not None else None
    review_required = state == GovernanceState.SUGGESTED

    if review_required:
        domain = row.get("suggested_domain", "")
        val = row.get("pattern_value", "")
        conf_str = f"{confidence:.0%}" if confidence is not None else "unknown confidence"
        review_reason: str | None = (
            f"TOKEN refinement '{val}' → '{domain}' "
            f"with {conf_str} awaiting human approval."
        )
    else:
        review_reason = None

    can_ai, warn = _compute_ai_trust(state, confidence)
    return GovernanceProfile(
        object_type_id    = GovernedObjectType.DOMAIN_REFINEMENT,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = confidence,
        confidence_tier   = _confidence_tier(confidence),
        confidence_source = "refinement_engine",
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = row.get("approved_by"),
        reviewed_at       = row.get("approved_at"),
        created_by        = None,
        created_at        = row.get("created_at"),
        updated_at        = row.get("approved_at") or row.get("created_at"),
        evidence          = [],
        can_ai_use        = can_ai,
        ai_warning        = warn,
        domain_context    = row.get("suggested_domain") or None,
    )


def _build_engine_tool_profile(row: dict) -> GovernanceProfile:
    obj_id = str(row["id"])
    status_str = row.get("status", "draft")
    state = _ENGINE_TOOL_STATUS_MAP.get(status_str, GovernanceState.GENERATED)

    created_by: str | None = None
    try:
        defn = json.loads(row.get("definition_json") or "{}")
        created_by = defn.get("metadata", {}).get("author_id") or None
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    review_required = state == GovernanceState.SUGGESTED
    review_reason = (
        "Engine tool requires human approval before it can be executed."
        if review_required else None
    )
    can_ai, warn = _compute_ai_trust(state)
    return GovernanceProfile(
        object_type_id    = GovernedObjectType.ENGINE_TOOL,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = 1.0 if state == GovernanceState.HUMAN_APPROVED else None,
        confidence_tier   = "VERY_HIGH" if state == GovernanceState.HUMAN_APPROVED else None,
        confidence_source = "human",
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = None,
        reviewed_at       = None,
        created_by        = created_by,
        created_at        = row.get("created_at"),
        updated_at        = row.get("updated_at"),
        evidence          = [],
        can_ai_use        = can_ai,
        ai_warning        = warn,
    )


def _build_pii_profile(row: dict) -> GovernanceProfile:
    obj_id = f"{row['source_id']}:{row['table_fqn']}:{row['column_name']}"
    pii_heuristic = bool(row.get("pii_name_heuristic"))
    pii_confirmed = bool(row.get("pii_confirmed"))

    if not pii_heuristic:
        state = GovernanceState.GENERATED
        review_required = False
        review_reason = None
    elif pii_confirmed:
        state = GovernanceState.HUMAN_APPROVED
        review_required = False
        review_reason = None
    else:
        state = GovernanceState.SUGGESTED
        review_required = True
        review_reason = (
            "PII heuristic signals detected on this column; "
            "confirmation required before AI use."
        )

    can_ai, warn = _compute_ai_trust(
        state, pii_risk=pii_heuristic, pii_confirmed=pii_confirmed
    )
    evidence = _parse_evidence(row.get("pii_signals_json"))

    return GovernanceProfile(
        object_type_id    = GovernedObjectType.PII_CONFIRMATION,
        object_id         = obj_id,
        approval_state    = state,
        confidence_score  = None,
        confidence_tier   = None,
        confidence_source = "pii_heuristic",
        review_required   = review_required,
        review_reason     = review_reason,
        reviewed_by       = None,
        reviewed_at       = None,
        created_by        = None,
        created_at        = row.get("created_at"),
        updated_at        = row.get("updated_at"),
        evidence          = evidence,
        can_ai_use        = can_ai,
        ai_warning        = warn,
        pii_risk          = pii_heuristic,
    )


# ---------------------------------------------------------------------------
# Public — Event Logging (called by existing services as side-effects)
# ---------------------------------------------------------------------------

def log_governance_event(
    *,
    object_type_id: str,
    object_id: str,
    event_type: str,
    from_state: str | None,
    to_state: str,
    actor_id: str,
    notes: str | None = None,
    source_service: str | None = None,
) -> None:
    """
    Append one event to governance_approval_events.

    Called by existing approval services immediately after their own state
    transitions.  Best-effort: logs a warning and returns normally on any
    failure so that the caller's approval logic is never disrupted.
    """
    try:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO governance_approval_events
                       (object_type_id, object_id, event_type,
                        from_state, to_state, actor_id,
                        notes, source_service, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (object_type_id, object_id, event_type,
                 from_state, to_state, actor_id,
                 notes, source_service, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "governance event log failed [type=%s id=%s event=%s]",
            object_type_id, object_id, event_type,
            exc_info=True,
        )


def upsert_governance_state(
    *,
    object_type_id: str,
    object_id: str,
    approval_state: str,
    confidence_score: float | None = None,
    reviewer_id: str | None = None,
    reviewed_at: str | None = None,
) -> None:
    """
    Upsert the current state into governance_state_map.

    The source table is always authoritative.  This projection enables fast
    cross-type queries (governance dashboard, review queues, KPIs) without
    joining seven separate domain tables.

    Best-effort: never raises; failure is logged as a warning.
    """
    try:
        now = _now()
        tier = _confidence_tier(confidence_score)
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO governance_state_map
                       (object_type_id, object_id, approval_state,
                        confidence_score, confidence_tier,
                        reviewer_id, reviewed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(object_type_id, object_id) DO UPDATE SET
                       approval_state   = excluded.approval_state,
                       confidence_score = excluded.confidence_score,
                       confidence_tier  = excluded.confidence_tier,
                       reviewer_id      = COALESCE(excluded.reviewer_id,
                                                   governance_state_map.reviewer_id),
                       reviewed_at      = COALESCE(excluded.reviewed_at,
                                                   governance_state_map.reviewed_at),
                       updated_at       = excluded.updated_at""",
                (object_type_id, object_id, approval_state,
                 confidence_score, tier, reviewer_id, reviewed_at, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "governance state upsert failed [type=%s id=%s state=%s]",
            object_type_id, object_id, approval_state,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Policy Engine — Phase 2
# ---------------------------------------------------------------------------

# Hard safety policy names — referenced in blocking_policy field
_HARD_POLICY_PII            = "HARD_PII_REQUIRES_HUMAN"
_HARD_POLICY_HIGH_RISK      = "HARD_HIGH_RISK_DOMAIN_REQUIRES_HUMAN"
_HARD_POLICY_IRREVERSIBLE   = "HARD_IRREVERSIBLE_STATE"

# Domains that always require human review.  These lists are intentionally
# conservative; enterprise admins can add DB-stored policies for additional
# high-risk domains without touching code.
_FINANCIAL_DOMAINS: frozenset[str] = frozenset({
    "Finance", "Financial", "Revenue", "Billing", "Payments",
    "Budget", "Treasury", "Accounting",
})
_REGULATORY_DOMAINS: frozenset[str] = frozenset({
    "Compliance", "Legal", "Regulatory", "HR", "Human Resources",
    "Audit", "Risk", "Governance", "Security",
})
_HIGH_RISK_DOMAINS: frozenset[str] = _FINANCIAL_DOMAINS | _REGULATORY_DOMAINS

# States that can never transition to AUTO_APPROVED (irreversible terminal states)
_IRREVERSIBLE_STATES: frozenset[GovernanceState] = frozenset({
    GovernanceState.REJECTED,
    GovernanceState.DEPRECATED,
    GovernanceState.ARCHIVED,
})

# States where policy evaluation is meaningful (objects awaiting a decision)
_EVALUABLE_STATES: frozenset[GovernanceState] = frozenset({
    GovernanceState.SUGGESTED,
    GovernanceState.GENERATED,
    GovernanceState.NEEDS_REVIEW,
    GovernanceState.VALIDATED,
})


def _check_hard_safety_policies(profile: GovernanceProfile) -> PolicyEvaluationResult | None:
    """
    Evaluate hard-coded safety policies. These run before DB-stored policies
    and CANNOT be disabled.  Returns a blocking result on the first match, or
    None if no hard policy applies.
    """
    # 1. Irreversible states — objects in REJECTED, DEPRECATED, ARCHIVED can
    #    never be auto-approved regardless of confidence.
    if profile.approval_state in _IRREVERSIBLE_STATES:
        return PolicyEvaluationResult(
            auto_approval_eligible = False,
            blocking_policy        = _HARD_POLICY_IRREVERSIBLE,
            matched_policy         = _HARD_POLICY_IRREVERSIBLE,
            review_required        = False,
            review_reason          = (
                f"Objects in '{profile.approval_state.value}' state cannot be auto-approved."
            ),
        )

    # 2. PII risk — unconfirmed PII columns always require a human reviewer.
    #    Covers both dict.column rows (pii_risk flag) and pii.confirmation objects
    #    that are still in SUGGESTED state.
    if profile.pii_risk:
        return PolicyEvaluationResult(
            auto_approval_eligible = False,
            blocking_policy        = _HARD_POLICY_PII,
            matched_policy         = _HARD_POLICY_PII,
            review_required        = True,
            review_reason          = (
                "Columns with PII risk signals require human review before auto-approval."
            ),
        )
    if profile.object_type_id == GovernedObjectType.PII_CONFIRMATION:
        if profile.approval_state == GovernanceState.SUGGESTED:
            return PolicyEvaluationResult(
                auto_approval_eligible = False,
                blocking_policy        = _HARD_POLICY_PII,
                matched_policy         = _HARD_POLICY_PII,
                review_required        = True,
                review_reason          = (
                    "Unconfirmed PII classification requires human review."
                ),
            )

    # 3. High-risk domains — Finance, Regulatory, Legal, etc.
    domain = (profile.domain_context or "").strip()
    if domain in _HIGH_RISK_DOMAINS:
        return PolicyEvaluationResult(
            auto_approval_eligible = False,
            blocking_policy        = _HARD_POLICY_HIGH_RISK,
            matched_policy         = _HARD_POLICY_HIGH_RISK,
            review_required        = True,
            review_reason          = (
                f"Domain '{domain}' is classified as high-risk "
                "and requires human review."
            ),
        )

    return None  # no hard policy matched


def _matches_condition(profile: GovernanceProfile, condition: dict) -> bool:
    """Return True if the profile satisfies every condition in the dict."""
    # confidence_min: profile's confidence_score must be >= this
    conf_min = condition.get("confidence_min")
    if conf_min is not None:
        score = profile.confidence_score
        if score is None or score < float(conf_min):
            return False

    # confidence_max: profile's confidence_score must be <= this
    conf_max = condition.get("confidence_max")
    if conf_max is not None:
        score = profile.confidence_score
        if score is None or score > float(conf_max):
            return False

    # domains: profile's domain_context must be in this list ([] = any domain)
    domains = condition.get("domains") or []
    if domains:
        if not profile.domain_context or profile.domain_context not in domains:
            return False

    # pii_required: True = only match PII items
    if condition.get("pii_required"):
        if not profile.pii_risk:
            return False

    return True


def _check_db_policies(
    profile: GovernanceProfile,
    conn,
) -> PolicyEvaluationResult | None:
    """
    Evaluate user-configurable policies from governance_policies table.
    Policies are evaluated in ascending priority order (lower = evaluated first).
    Returns the first result that is not NO_ACTION, or None if none match.
    """
    try:
        rows = conn.execute(
            """SELECT policy_name, object_types_json, condition_json, action
               FROM governance_policies
               WHERE enabled = 1
               ORDER BY priority ASC, id ASC""",
        ).fetchall()
    except Exception:
        logger.warning("governance_policies query failed; skipping DB policies", exc_info=True)
        return None

    for row in rows:
        d = dict(row)

        # Check object-type filter
        try:
            obj_types: list[str] = json.loads(d.get("object_types_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            obj_types = []
        if obj_types and profile.object_type_id not in obj_types:
            continue

        # Parse condition
        try:
            condition: dict = json.loads(d.get("condition_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            condition = {}

        if not _matches_condition(profile, condition):
            continue

        action      = d["action"]
        policy_name = d["policy_name"]

        if action == PolicyAction.AUTO_APPROVE:
            return PolicyEvaluationResult(
                auto_approval_eligible = True,
                blocking_policy        = None,
                matched_policy         = policy_name,
                review_required        = False,
                review_reason          = None,
            )
        if action in (PolicyAction.REQUIRE_HUMAN, PolicyAction.ESCALATE):
            return PolicyEvaluationResult(
                auto_approval_eligible = False,
                blocking_policy        = policy_name,
                matched_policy         = policy_name,
                review_required        = True,
                review_reason          = (
                    f"Policy '{policy_name}' requires human review."
                ),
            )
        # NO_ACTION: policy matched but does not block or approve — continue

    return None  # no decisive policy matched


def evaluate_policies(
    profile: GovernanceProfile,
    conn=None,
) -> PolicyEvaluationResult:
    """
    Evaluate all policies for a governed object.

    Evaluation order:
      1. Hard safety policies (cannot be disabled).
      2. DB-stored policies ordered by priority (lower = higher priority).
      3. Default: no auto-approval.

    Pass an existing connection as `conn` to avoid opening a second one when
    called from inside get_governance_profile().
    """
    # Hard safety policies always run first
    hard = _check_hard_safety_policies(profile)
    if hard is not None:
        return hard

    # Only evaluate further if the object is in an actionable state
    if profile.approval_state not in _EVALUABLE_STATES:
        return PolicyEvaluationResult(
            auto_approval_eligible = False,
            blocking_policy        = None,
            matched_policy         = None,
            review_required        = profile.approval_state == GovernanceState.NEEDS_REVIEW,
            review_reason          = None,
        )

    _close = False
    if conn is None:
        conn = get_connection()
        _close = True
    try:
        db_result = _check_db_policies(profile, conn)
    finally:
        if _close:
            conn.close()

    if db_result is not None:
        return db_result

    # Default — no matching policy
    review_required = profile.approval_state in (
        GovernanceState.SUGGESTED, GovernanceState.NEEDS_REVIEW
    )
    return PolicyEvaluationResult(
        auto_approval_eligible = False,
        blocking_policy        = None,
        matched_policy         = None,
        review_required        = review_required,
        review_reason          = profile.review_reason if review_required else None,
    )


def _enrich_profile_with_policy(profile: GovernanceProfile, conn) -> None:
    """Evaluate policies and set policy fields on the profile in-place."""
    result = evaluate_policies(profile, conn)
    profile.auto_approval_eligible = result.auto_approval_eligible
    profile.blocking_policy        = result.blocking_policy
    profile.matched_policy         = result.matched_policy
    # Use policy review_required if stricter than the profile's own assessment
    if result.review_required and not profile.review_required:
        profile.review_required = True
    # Use policy review_reason if profile has none or policy is more specific
    if result.review_reason:
        profile.review_reason = result.review_reason


# ---------------------------------------------------------------------------
# Public — Profile Retrieval
# ---------------------------------------------------------------------------

def get_governance_profile(
    *,
    object_type: str,
    source_id: int | None = None,
    table_fqn: str | None = None,
    column_name: str | None = None,
    rule_id: int | None = None,
    suggestion_id: int | None = None,
    tool_id: str | None = None,
) -> GovernanceProfile | None:
    """
    Return the unified governance profile for one governed object.

    Reads from the authoritative source table for the given object_type.
    Returns None when the object does not exist or when required parameters
    for the type are missing.

    object_type must be a valid GovernedObjectType value.
    """
    try:
        obj_type = GovernedObjectType(object_type)
    except ValueError:
        return None

    conn = get_connection()
    try:
        profile: GovernanceProfile | None = None

        if obj_type == GovernedObjectType.DICT_TABLE:
            if source_id is None or not table_fqn:
                return None
            row = conn.execute(
                "SELECT * FROM data_dictionary_tables "
                "WHERE source_id = ? AND table_fqn = ?",
                (source_id, table_fqn),
            ).fetchone()
            profile = _build_dict_table_profile(dict(row)) if row else None

        elif obj_type == GovernedObjectType.DICT_COLUMN:
            if source_id is None or not table_fqn or not column_name:
                return None
            row = conn.execute(
                "SELECT * FROM data_dictionary_columns "
                "WHERE source_id = ? AND table_fqn = ? AND column_name = ?",
                (source_id, table_fqn, column_name),
            ).fetchone()
            profile = _build_dict_column_profile(dict(row)) if row else None

        elif obj_type == GovernedObjectType.DOMAIN_RULE:
            if rule_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM domain_learning_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
            profile = (
                _build_rule_profile(dict(row), GovernedObjectType.DOMAIN_RULE)
                if row else None
            )

        elif obj_type == GovernedObjectType.ENTITY_RULE:
            if rule_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM entity_learning_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
            profile = (
                _build_rule_profile(dict(row), GovernedObjectType.ENTITY_RULE)
                if row else None
            )

        elif obj_type == GovernedObjectType.DOMAIN_REFINEMENT:
            if suggestion_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM domain_rule_refinement_suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
            profile = _build_refinement_profile(dict(row)) if row else None

        elif obj_type == GovernedObjectType.ENGINE_TOOL:
            if not tool_id:
                return None
            row = conn.execute(
                "SELECT id, name, version, status, definition_json, "
                "created_at, updated_at FROM engine_tools WHERE id = ?",
                (tool_id,),
            ).fetchone()
            profile = _build_engine_tool_profile(dict(row)) if row else None

        elif obj_type == GovernedObjectType.PII_CONFIRMATION:
            if source_id is None or not table_fqn or not column_name:
                return None
            row = conn.execute(
                "SELECT * FROM profiling_column_profiles "
                "WHERE source_id = ? AND table_fqn = ? AND column_name = ? "
                "ORDER BY profiling_snapshot_id DESC LIMIT 1",
                (source_id, table_fqn, column_name),
            ).fetchone()
            profile = _build_pii_profile(dict(row)) if row else None

        if profile is None:
            return None

        # Phase 2: enrich profile with policy evaluation result.
        # Pass the existing connection so the policy engine reuses it.
        _enrich_profile_with_policy(profile, conn)
        return profile

    finally:
        conn.close()


def list_governance_events(
    *,
    object_type_id: str,
    object_id: str,
) -> list[dict]:
    """Return the full audit trail for one governed object, oldest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM governance_approval_events
               WHERE object_type_id = ? AND object_id = ?
               ORDER BY created_at ASC""",
            (object_type_id, object_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_governed_object_types() -> list[dict]:
    """Return metadata for every registered governed object type."""
    return [{"id": k, **v} for k, v in _TYPE_META.items()]


# ---------------------------------------------------------------------------
# Public — PII Confirmation Write Path
# ---------------------------------------------------------------------------

def confirm_pii_column(
    source_id: int,
    user_id: str,
    table_fqn: str,
    column_name: str,
) -> dict | None:
    """
    Confirm that a column contains PII.

    Sets pii_confirmed = 1 on the column's latest profiling snapshot row.
    This resolves the CRITICAL 'Review PII Classification' review task for
    the column and records a HUMAN_APPROVED governance event.

    Returns the updated column profile dict, or None if:
      - the source does not belong to user_id
      - the column is not found in the latest snapshot
      - the column has no PII heuristic flag (nothing to confirm)
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        now = _now()
        cursor = conn.execute(
            """UPDATE profiling_column_profiles
                  SET pii_confirmed = 1,
                      updated_at    = ?
                WHERE profiling_snapshot_id = (
                    SELECT id FROM profiling_snapshots
                    WHERE source_id = ?
                    ORDER BY snapshot_version DESC LIMIT 1
                )
                  AND source_id    = ?
                  AND table_fqn   = ?
                  AND column_name = ?
                  AND pii_name_heuristic = 1""",
            (now, source_id, source_id, table_fqn, column_name),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return None

        updated = conn.execute(
            """SELECT * FROM profiling_column_profiles
               WHERE source_id = ? AND table_fqn = ? AND column_name = ?
               ORDER BY profiling_snapshot_id DESC LIMIT 1""",
            (source_id, table_fqn, column_name),
        ).fetchone()
    finally:
        conn.close()

    obj_id = f"{source_id}:{table_fqn}:{column_name}"
    log_governance_event(
        object_type_id = GovernedObjectType.PII_CONFIRMATION,
        object_id      = obj_id,
        event_type     = "PII_CONFIRMED",
        from_state     = GovernanceState.SUGGESTED,
        to_state       = GovernanceState.HUMAN_APPROVED,
        actor_id       = user_id,
        notes          = f"PII confirmed for {table_fqn}.{column_name}",
        source_service = "governance_service",
    )
    upsert_governance_state(
        object_type_id = GovernedObjectType.PII_CONFIRMATION,
        object_id      = obj_id,
        approval_state = GovernanceState.HUMAN_APPROVED,
        reviewer_id    = user_id,
        reviewed_at    = now,
    )

    return dict(updated) if updated else {"pii_confirmed": True, "updated_at": now}


# ---------------------------------------------------------------------------
# Public — Policy CRUD
# ---------------------------------------------------------------------------

def get_governance_policies(
    *,
    enabled_only: bool = False,
) -> list[dict]:
    """
    Return all governance policies ordered by priority.

    Parameters
    ----------
    enabled_only : If True, return only policies where enabled = 1.
    """
    conn = get_connection()
    try:
        sql = "SELECT * FROM governance_policies"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority ASC, id ASC"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["object_types"] = json.loads(d.get("object_types_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["object_types"] = []
        try:
            d["condition"] = json.loads(d.get("condition_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["condition"] = {}
        result.append(d)
    return result


def create_governance_policy(
    *,
    policy_name: str,
    action: str,
    priority: int = 100,
    object_types: list[str] | None = None,
    condition: dict | None = None,
    created_by: str,
    enabled: bool = True,
) -> dict:
    """
    Create a new user-configurable governance policy.

    Parameters
    ----------
    policy_name   : Unique name for the policy.
    action        : One of REQUIRE_HUMAN, AUTO_APPROVE, ESCALATE, NO_ACTION.
    priority      : Evaluation order (lower = higher priority).
    object_types  : List of GovernedObjectType ids; [] or None means all types.
    condition     : Dict with matching criteria (confidence_min, domains, etc.).
    created_by    : User or system actor creating the policy.
    enabled       : Whether the policy is active immediately.

    Raises ValueError for invalid action or duplicate policy_name.
    """
    valid_actions = {a.value for a in PolicyAction}
    if action not in valid_actions:
        raise ValueError(
            f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}"
        )

    now = _now()
    obj_json = json.dumps(object_types or [])
    cond_json = json.dumps(condition or {})

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM governance_policies WHERE policy_name = ?",
            (policy_name,),
        ).fetchone()
        if existing:
            raise ValueError(f"A policy named '{policy_name}' already exists.")

        cursor = conn.execute(
            """INSERT INTO governance_policies
                   (policy_name, enabled, priority, object_types_json,
                    condition_json, action, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (policy_name, 1 if enabled else 0, priority,
             obj_json, cond_json, action, created_by, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM governance_policies WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    finally:
        conn.close()

    return dict(row) if row else {}


def toggle_governance_policy(
    policy_id: int,
    enabled: bool,
    updated_by: str,
) -> dict | None:
    """
    Enable or disable a governance policy.

    Hard-coded safety policies cannot be managed via this function; they are
    always active.  This function only affects DB-stored policies.

    Returns the updated policy dict, or None if the policy does not exist.
    """
    now = _now()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM governance_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if existing is None:
            return None

        conn.execute(
            "UPDATE governance_policies SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now, policy_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM governance_policies WHERE id = ?", (policy_id,)
        ).fetchone()
    finally:
        conn.close()

    return dict(updated) if updated else None


# ---------------------------------------------------------------------------
# Bulk Operations — Phase 3
# ---------------------------------------------------------------------------

# States that are never eligible for bulk approval or rejection
_BULK_APPROVE_BLOCKED_STATES: frozenset[GovernanceState] = frozenset({
    GovernanceState.HUMAN_APPROVED,
    GovernanceState.AUTO_APPROVED,
    GovernanceState.REJECTED,
    GovernanceState.DEPRECATED,
    GovernanceState.ARCHIVED,
})

_BULK_REJECT_BLOCKED_STATES: frozenset[GovernanceState] = frozenset({
    GovernanceState.HUMAN_APPROVED,
    GovernanceState.AUTO_APPROVED,
    GovernanceState.REJECTED,
    GovernanceState.DEPRECATED,
    GovernanceState.ARCHIVED,
})

# Maximum candidates returned by a single bulk query (prevents runaway ops)
_BULK_QUERY_LIMIT = 1000


@dataclass
class BulkFilter:
    """
    Filter criteria for bulk governance operations.

    All fields are optional and additive (AND logic).
    exclude_pii defaults to True — PII columns are never bulk-approved
    unless explicitly overridden by a PII Officer (Phase 4).
    """
    object_type:    str               # Required — GovernedObjectType value
    source_id:      int | None = None
    confidence_min: float | None = None
    confidence_max: float | None = None
    approval_state: str | None = None  # None = all reviewable states
    domain:         str | None = None  # domain / suggested_domain / entity filter
    entity:         str | None = None  # entity filter (entity.rule only)
    schema_name:    str | None = None  # table schema prefix filter
    exclude_pii:    bool = True        # Safety default: True

    def to_dict(self) -> dict:
        return {
            "object_type":    self.object_type,
            "source_id":      self.source_id,
            "confidence_min": self.confidence_min,
            "confidence_max": self.confidence_max,
            "approval_state": self.approval_state,
            "domain":         self.domain,
            "entity":         self.entity,
            "schema_name":    self.schema_name,
            "exclude_pii":    self.exclude_pii,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BulkFilter":
        return cls(
            object_type    = d["object_type"],
            source_id      = d.get("source_id"),
            confidence_min = d.get("confidence_min"),
            confidence_max = d.get("confidence_max"),
            approval_state = d.get("approval_state"),
            domain         = d.get("domain"),
            entity         = d.get("entity"),
            schema_name    = d.get("schema_name"),
            exclude_pii    = bool(d.get("exclude_pii", True)),
        )


@dataclass
class BulkOpResult:
    """Result of a bulk governance operation (approve, reject, or dry-run)."""
    action:           str
    dry_run:          bool
    object_type:      str
    total_candidates: int
    affected_count:   int
    blocked_count:    int
    blocked_items:    list[dict]   # [{object_id, object_type_id, blocking_policy, reason}]
    executed_at:      str
    bulk_op_id:       int | None = None  # None for dry_run; set after DB write

    def to_dict(self) -> dict:
        return {
            "action":           self.action,
            "dry_run":          self.dry_run,
            "object_type":      self.object_type,
            "total_candidates": self.total_candidates,
            "affected_count":   self.affected_count,
            "blocked_count":    self.blocked_count,
            "blocked_items":    self.blocked_items,
            "executed_at":      self.executed_at,
            "bulk_op_id":       self.bulk_op_id,
        }


# ---------------------------------------------------------------------------
# Bulk helpers — internal
# ---------------------------------------------------------------------------

def _load_enabled_db_policies(conn) -> list[dict]:
    """
    Load all enabled governance policies in priority order.
    Called once per bulk operation; results are passed to _check_policies_with_cache
    to avoid one DB round-trip per candidate item.
    """
    try:
        rows = conn.execute(
            """SELECT policy_name, object_types_json, condition_json, action
               FROM governance_policies
               WHERE enabled = 1
               ORDER BY priority ASC, id ASC""",
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning("governance_policies query failed in bulk op; skipping DB policies",
                       exc_info=True)
        return []


def _check_policies_with_cache(
    profile: GovernanceProfile,
    cached_policies: list[dict],
) -> PolicyEvaluationResult:
    """
    Evaluate policies for a single profile using pre-loaded policy list.
    Identical semantics to evaluate_policies() but avoids a DB query per item.
    """
    # 1. Hard safety policies (always first, cannot be disabled)
    hard = _check_hard_safety_policies(profile)
    if hard is not None:
        return hard

    # 2. Object is not in a state that needs evaluation
    if profile.approval_state not in _EVALUABLE_STATES:
        return PolicyEvaluationResult(
            auto_approval_eligible = False,
            blocking_policy        = None,
            matched_policy         = None,
            review_required        = profile.approval_state == GovernanceState.NEEDS_REVIEW,
            review_reason          = None,
        )

    # 3. Evaluate cached DB policies
    for p in cached_policies:
        try:
            obj_types: list[str] = json.loads(p.get("object_types_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            obj_types = []
        if obj_types and profile.object_type_id not in obj_types:
            continue
        try:
            condition: dict = json.loads(p.get("condition_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            condition = {}
        if not _matches_condition(profile, condition):
            continue
        action      = p["action"]
        policy_name = p["policy_name"]
        if action == PolicyAction.AUTO_APPROVE:
            return PolicyEvaluationResult(
                auto_approval_eligible = True,
                blocking_policy        = None,
                matched_policy         = policy_name,
                review_required        = False,
                review_reason          = None,
            )
        if action in (PolicyAction.REQUIRE_HUMAN, PolicyAction.ESCALATE):
            return PolicyEvaluationResult(
                auto_approval_eligible = False,
                blocking_policy        = policy_name,
                matched_policy         = policy_name,
                review_required        = True,
                review_reason          = f"Policy '{policy_name}' requires human review.",
            )
        # NO_ACTION: fall through to next policy

    # 4. Default: no policy matched
    review_required = profile.approval_state in (
        GovernanceState.SUGGESTED, GovernanceState.NEEDS_REVIEW,
    )
    return PolicyEvaluationResult(
        auto_approval_eligible = False,
        blocking_policy        = None,
        matched_policy         = None,
        review_required        = review_required,
        review_reason          = profile.review_reason if review_required else None,
    )


def _build_profile_from_bulk_row(object_type: str, row: dict) -> GovernanceProfile | None:
    """Build a GovernanceProfile from a bulk-query result row."""
    try:
        ot = GovernedObjectType(object_type)
    except ValueError:
        return None

    if ot == GovernedObjectType.DICT_TABLE:
        return _build_dict_table_profile(row)
    if ot == GovernedObjectType.DICT_COLUMN:
        return _build_dict_column_profile(row)
    if ot in (GovernedObjectType.DOMAIN_RULE, GovernedObjectType.ENTITY_RULE):
        return _build_rule_profile(row, ot)
    if ot == GovernedObjectType.DOMAIN_REFINEMENT:
        return _build_refinement_profile(row)
    return None


def _apply_single_approval(object_type: str, row: dict, actor_id: str) -> bool:
    """
    Call the authoritative approval function for one item.

    Reuses the existing per-type approval functions so all their side-effects
    (ownership checks, governance event logging, state-map updates) fire normally.
    Returns True on success, False on any failure.
    """
    try:
        ot = GovernedObjectType(object_type)

        if ot == GovernedObjectType.DICT_TABLE:
            from data.dictionary_service import approve_table_dictionary
            r = approve_table_dictionary(
                source_id=int(row["source_id"]),
                user_id=actor_id,
                table_fqn=row["table_fqn"],
            )
            return r is not None

        if ot == GovernedObjectType.DICT_COLUMN:
            from data.dictionary_service import approve_column_dictionary
            r = approve_column_dictionary(
                source_id=int(row["source_id"]),
                user_id=actor_id,
                table_fqn=row["table_fqn"],
                column_name=row["column_name"],
            )
            return r is not None

        if ot == GovernedObjectType.DOMAIN_RULE:
            from data.domain_learning_service import approve_domain_rule
            r = approve_domain_rule(rule_id=int(row["id"]), user_id=actor_id)
            return r is not None

        if ot == GovernedObjectType.ENTITY_RULE:
            from data.entity_learning_service import approve_entity_rule
            r = approve_entity_rule(rule_id=int(row["id"]), user_id=actor_id)
            return r is not None

        if ot == GovernedObjectType.DOMAIN_REFINEMENT:
            from data.domain_refinement_service import approve_refinement_suggestion
            r = approve_refinement_suggestion(
                suggestion_id=int(row["id"]), user_id=actor_id
            )
            return r is not None

    except Exception:
        logger.warning(
            "bulk approval failed for %s id=%s", object_type, row.get("id"),
            exc_info=True,
        )
    return False


def _apply_single_rejection(object_type: str, row: dict, actor_id: str) -> bool:
    """
    Call the authoritative rejection function for one item.

    For types that have a dedicated reject function (domain.rule, entity.rule,
    domain.refinement) the existing function is called so all side-effects fire.

    For dict.table and dict.column (which have no source-level rejection), the
    governance audit log and state map are updated to record the governance-layer
    rejection without changing the source table.
    """
    try:
        ot = GovernedObjectType(object_type)

        if ot == GovernedObjectType.DOMAIN_RULE:
            from data.domain_learning_service import reject_domain_rule
            r = reject_domain_rule(rule_id=int(row["id"]), user_id=actor_id)
            return r is not None

        if ot == GovernedObjectType.ENTITY_RULE:
            from data.entity_learning_service import reject_entity_rule
            r = reject_entity_rule(rule_id=int(row["id"]), user_id=actor_id)
            return r is not None

        if ot == GovernedObjectType.DOMAIN_REFINEMENT:
            from data.domain_refinement_service import reject_refinement_suggestion
            r = reject_refinement_suggestion(
                suggestion_id=int(row["id"]), user_id=actor_id
            )
            return r is not None

        # dict.table and dict.column: governance-layer rejection only
        if ot == GovernedObjectType.DICT_TABLE:
            obj_id = f"{row['source_id']}:{row['table_fqn']}"
        elif ot == GovernedObjectType.DICT_COLUMN:
            obj_id = f"{row['source_id']}:{row['table_fqn']}:{row['column_name']}"
        else:
            return False

        now = _now()
        log_governance_event(
            object_type_id = object_type,
            object_id      = obj_id,
            event_type     = "REJECTED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.REJECTED,
            actor_id       = actor_id,
            source_service = "governance_service.bulk_reject",
        )
        upsert_governance_state(
            object_type_id = object_type,
            object_id      = obj_id,
            approval_state = GovernanceState.REJECTED,
            reviewer_id    = actor_id,
            reviewed_at    = now,
        )
        return True

    except Exception:
        logger.warning(
            "bulk rejection failed for %s id=%s", object_type, row.get("id"),
            exc_info=True,
        )
    return False


def _query_bulk_candidates(
    f: BulkFilter,
    conn,
    limit: int = _BULK_QUERY_LIMIT,
) -> list[dict]:
    """
    Build a type-specific SQL query and return candidate rows.
    All filter fields are optional; only provided values narrow the results.
    """
    try:
        ot = GovernedObjectType(f.object_type)
    except ValueError:
        return []

    sql: str
    params: list = []

    if ot == GovernedObjectType.DICT_TABLE:
        sql = (
            "SELECT source_id, table_fqn, table_name, schema_name, "
            "business_name, domain, is_approved, generation_method "
            "FROM data_dictionary_tables "
            "WHERE is_approved = 0 AND business_name IS NOT NULL"
        )
        if f.source_id is not None:
            sql += " AND source_id = ?"
            params.append(f.source_id)
        if f.schema_name:
            sql += " AND schema_name = ?"
            params.append(f.schema_name)
        if f.domain:
            sql += " AND domain = ?"
            params.append(f.domain)

    elif ot == GovernedObjectType.DICT_COLUMN:
        sql = (
            "SELECT source_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, generation_method "
            "FROM data_dictionary_columns "
            "WHERE is_approved = 0 AND business_label IS NOT NULL"
        )
        if f.exclude_pii:
            sql += " AND pii_risk = 0"
        if f.source_id is not None:
            sql += " AND source_id = ?"
            params.append(f.source_id)
        if f.schema_name:
            # table_fqn format is schema.table_name
            sql += " AND table_fqn LIKE ?"
            params.append(f.schema_name + ".%")

    elif ot == GovernedObjectType.DOMAIN_RULE:
        sql = (
            "SELECT id, source_id, pattern_type, pattern_value, "
            "domain, confidence, approval_status, created_by, created_at "
            "FROM domain_learning_rules WHERE approval_status = 'PENDING'"
        )
        if f.source_id is not None:
            sql += " AND source_id = ?"
            params.append(f.source_id)
        if f.confidence_min is not None:
            sql += " AND confidence >= ?"
            params.append(f.confidence_min)
        if f.confidence_max is not None:
            sql += " AND confidence <= ?"
            params.append(f.confidence_max)
        if f.domain:
            sql += " AND domain = ?"
            params.append(f.domain)

    elif ot == GovernedObjectType.ENTITY_RULE:
        sql = (
            "SELECT id, source_id, pattern_type, pattern_value, "
            "entity, confidence, approval_status, created_by, created_at "
            "FROM entity_learning_rules WHERE approval_status = 'PENDING'"
        )
        if f.source_id is not None:
            sql += " AND source_id = ?"
            params.append(f.source_id)
        if f.confidence_min is not None:
            sql += " AND confidence >= ?"
            params.append(f.confidence_min)
        if f.confidence_max is not None:
            sql += " AND confidence <= ?"
            params.append(f.confidence_max)
        if f.entity:
            sql += " AND entity = ?"
            params.append(f.entity)

    elif ot == GovernedObjectType.DOMAIN_REFINEMENT:
        sql = (
            "SELECT id, source_id, pattern_type, pattern_value, "
            "suggested_domain, confidence, approval_status, support_count, created_at "
            "FROM domain_rule_refinement_suggestions WHERE approval_status = 'PENDING'"
        )
        if f.source_id is not None:
            sql += " AND source_id = ?"
            params.append(f.source_id)
        if f.confidence_min is not None:
            sql += " AND confidence >= ?"
            params.append(f.confidence_min)
        if f.confidence_max is not None:
            sql += " AND confidence <= ?"
            params.append(f.confidence_max)
        if f.domain:
            sql += " AND suggested_domain = ?"
            params.append(f.domain)

    else:
        return []

    sql += f" LIMIT {limit}"

    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning(
            "bulk candidate query failed for %s", f.object_type, exc_info=True
        )
        return []


def _record_bulk_op(
    action: str,
    f: BulkFilter,
    result: BulkOpResult,
    actor_id: str,
) -> int | None:
    """Write a governance_bulk_ops row and return its id. Best-effort; never raises."""
    try:
        conn = get_connection()
        try:
            cursor = conn.execute(
                """INSERT INTO governance_bulk_ops
                       (actor_id, action, filter_json,
                        affected_count, blocked_count,
                        blocked_items_json, status, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', ?)""",
                (
                    actor_id,
                    action,
                    json.dumps(f.to_dict()),
                    result.affected_count,
                    result.blocked_count,
                    json.dumps(result.blocked_items[:500]),  # cap JSON size
                    result.executed_at,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    except Exception:
        logger.warning("failed to record bulk op", exc_info=True)
        return None


def _run_bulk_operation(
    f: BulkFilter,
    action: str,
    actor_id: str,
    dry_run: bool = False,
) -> BulkOpResult:
    """
    Core bulk operation engine.

    1. Query candidate objects using the filter.
    2. Load DB policies once for the entire batch.
    3. For each candidate, evaluate policies (hard safety + cached DB policies).
    4. If not dry_run and not blocked: call the existing per-type approval/rejection
       function (preserving all their side-effects).
    5. Write a governance_bulk_ops record (unless dry_run).
    6. Return BulkOpResult with full breakdown.
    """
    conn = get_connection()
    try:
        candidates = _query_bulk_candidates(f, conn)
        cached_policies = _load_enabled_db_policies(conn)
    finally:
        conn.close()

    affected: list[dict] = []
    blocked:  list[dict] = []

    for row in candidates:
        profile = _build_profile_from_bulk_row(f.object_type, row)
        if profile is None:
            continue

        obj_id = profile.object_id

        # ── Safety evaluation ──────────────────────────────────────────────
        if action == "approve":
            # Block if object is already approved / in irreversible state
            if profile.approval_state in _BULK_APPROVE_BLOCKED_STATES:
                blocked.append({
                    "object_id":       obj_id,
                    "object_type_id":  profile.object_type_id,
                    "blocking_policy": _HARD_POLICY_IRREVERSIBLE,
                    "reason":          (
                        f"State '{profile.approval_state.value}' cannot be bulk-approved."
                    ),
                })
                continue

            # Policy evaluation (hard safety + DB policies)
            policy_result = _check_policies_with_cache(profile, cached_policies)
            if policy_result.blocking_policy:
                blocked.append({
                    "object_id":       obj_id,
                    "object_type_id":  profile.object_type_id,
                    "blocking_policy": policy_result.blocking_policy,
                    "reason":          (
                        policy_result.review_reason or "Blocked by governance policy."
                    ),
                })
                continue

            # Safe to approve
            if not dry_run:
                success = _apply_single_approval(f.object_type, row, actor_id)
                if not success:
                    blocked.append({
                        "object_id":       obj_id,
                        "object_type_id":  profile.object_type_id,
                        "blocking_policy": None,
                        "reason":          (
                            "Approval failed — possible ownership mismatch "
                            "or the item was already processed."
                        ),
                    })
                    continue
            affected.append({"object_id": obj_id, "object_type_id": profile.object_type_id})

        elif action == "reject":
            # Block if object is in a state that cannot be rejected
            if profile.approval_state in _BULK_REJECT_BLOCKED_STATES:
                blocked.append({
                    "object_id":       obj_id,
                    "object_type_id":  profile.object_type_id,
                    "blocking_policy": _HARD_POLICY_IRREVERSIBLE,
                    "reason":          (
                        f"State '{profile.approval_state.value}' cannot be bulk-rejected."
                    ),
                })
                continue

            if not dry_run:
                success = _apply_single_rejection(f.object_type, row, actor_id)
                if not success:
                    blocked.append({
                        "object_id":       obj_id,
                        "object_type_id":  profile.object_type_id,
                        "blocking_policy": None,
                        "reason":          "Rejection failed — ownership mismatch.",
                    })
                    continue
            affected.append({"object_id": obj_id, "object_type_id": profile.object_type_id})

    now = _now()
    result = BulkOpResult(
        action           = action,
        dry_run          = dry_run,
        object_type      = f.object_type,
        total_candidates = len(candidates),
        affected_count   = len(affected),
        blocked_count    = len(blocked),
        blocked_items    = blocked,
        executed_at      = now,
    )

    if not dry_run:
        op_id = _record_bulk_op(action, f, result, actor_id)
        result.bulk_op_id = op_id

    return result


# ---------------------------------------------------------------------------
# Public — Bulk Operations
# ---------------------------------------------------------------------------

def bulk_dry_run(f: BulkFilter, actor_id: str) -> BulkOpResult:
    """
    Simulate a bulk approval and return the count of items that would be
    approved vs blocked — without writing any changes.

    Use this before bulk_approve() to preview impact.
    """
    return _run_bulk_operation(f, action="approve", actor_id=actor_id, dry_run=True)


def bulk_approve(f: BulkFilter, actor_id: str) -> BulkOpResult:
    """
    Bulk-approve all matching governed objects that pass policy evaluation.

    Calls the per-type approval function for each eligible item so all
    existing approval side-effects (ownership checks, governance events,
    state-map updates) fire normally.

    Returns a BulkOpResult with affected/blocked counts and the bulk_op_id
    written to governance_bulk_ops.
    """
    return _run_bulk_operation(f, action="approve", actor_id=actor_id, dry_run=False)


def bulk_reject(f: BulkFilter, actor_id: str) -> BulkOpResult:
    """
    Bulk-reject all matching governed objects in reviewable states.

    Types with existing rejection functions (domain.rule, entity.rule,
    domain.refinement) call those functions directly.  Dictionary entries
    receive governance-layer rejection (audit event + state-map update)
    since their source tables have no rejection state.

    Returns a BulkOpResult with affected/blocked counts.
    """
    return _run_bulk_operation(f, action="reject", actor_id=actor_id, dry_run=False)


# ---------------------------------------------------------------------------
# Stewardship & Work Management — Phase 4
# ---------------------------------------------------------------------------

class AssignmentPriority(str, Enum):
    """Enterprise priority levels for governance assignments."""
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


class AssignmentStatus(str, Enum):
    """Lifecycle status of a stewardship assignment."""
    OPEN      = "OPEN"
    COMPLETED = "COMPLETED"


# SLA thresholds (calendar days) per priority.
# Used to auto-calculate due_date when not explicitly provided.
_SLA_DAYS_BY_PRIORITY: dict[str, int] = {
    AssignmentPriority.CRITICAL: 1,
    AssignmentPriority.HIGH:     3,
    AssignmentPriority.MEDIUM:   7,
    AssignmentPriority.LOW:      14,
}

# Sort weight for priority ordering in SQL ORDER BY expressions
_PRIORITY_SQL_ORDER = (
    "CASE priority "
    "WHEN 'CRITICAL' THEN 0 "
    "WHEN 'HIGH'     THEN 1 "
    "WHEN 'MEDIUM'   THEN 2 "
    "ELSE                 3 "
    "END"
)


# ---------------------------------------------------------------------------
# Priority calculation (pure — no DB calls)
# ---------------------------------------------------------------------------

def calculate_priority_for_profile(profile: GovernanceProfile) -> str:
    """
    Determine assignment priority from governance profile signals.

    Evaluation order (highest priority wins):
    1. PII risk present                   → CRITICAL
    2. Hard PII safety policy blocked     → CRITICAL
    3. Hard high-risk domain policy       → HIGH
    4. NEEDS_REVIEW state                 → HIGH
    5. Confidence < 0.60                  → HIGH
    6. Auto-approval eligible             → LOW  (policy cleared it)
    7. Confidence 0.60–0.79               → MEDIUM
    8. Confidence ≥ 0.80 / default        → LOW
    """
    if profile.pii_risk:
        return AssignmentPriority.CRITICAL

    if profile.blocking_policy == _HARD_POLICY_PII:
        return AssignmentPriority.CRITICAL

    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        return AssignmentPriority.HIGH

    if profile.approval_state == GovernanceState.NEEDS_REVIEW:
        return AssignmentPriority.HIGH

    score = profile.confidence_score
    if score is not None and score < 0.60:
        return AssignmentPriority.HIGH

    if profile.auto_approval_eligible:
        return AssignmentPriority.LOW

    if score is None or (0.60 <= score < 0.80):
        return AssignmentPriority.MEDIUM

    return AssignmentPriority.LOW


# ---------------------------------------------------------------------------
# SLA calculation (pure — no DB calls)
# ---------------------------------------------------------------------------

def calculate_sla(
    assignment: dict,
    reference_date: str | None = None,
) -> dict:
    """
    Calculate SLA status for an assignment.

    Pure calculation — no DB access.  Pass reference_date in tests to get
    deterministic results without depending on wall-clock time.

    Parameters
    ----------
    assignment     : Row dict from governance_assignments.
    reference_date : ISO datetime string to use as "now".  Defaults to UTC now.

    Returns
    -------
    dict with keys:
        days_open            : int
        days_overdue         : int    (0 if not overdue)
        sla_status           : str    ON_TRACK | AT_RISK | OVERDUE | COMPLETED
        risk_level           : str    LOW | MEDIUM | HIGH | CRITICAL
        escalation_required  : bool
        sla_due_date         : str    YYYY-MM-DD
    """
    # Resolve reference time
    if reference_date:
        ref = datetime.fromisoformat(reference_date)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
    else:
        ref = datetime.now(timezone.utc)

    # Resolve created_at
    try:
        created = datetime.fromisoformat(assignment.get("created_at", ""))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        created = ref

    days_open = max(0, (ref - created).days)

    # Resolve SLA threshold
    priority  = assignment.get("priority", AssignmentPriority.MEDIUM)
    sla_days  = _SLA_DAYS_BY_PRIORITY.get(priority, 7)

    # Resolve due datetime
    due_str = assignment.get("due_date")
    if due_str:
        try:
            if len(due_str) == 10:  # date-only YYYY-MM-DD → treat as end of that day
                due = datetime.combine(
                    date.fromisoformat(due_str),
                    datetime.max.time(),
                ).replace(tzinfo=timezone.utc)
            else:
                due = datetime.fromisoformat(due_str)
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            due = created + timedelta(days=sla_days)
    else:
        due = created + timedelta(days=sla_days)

    sla_due_date = due.date().isoformat()

    # COMPLETED path
    if assignment.get("status") == AssignmentStatus.COMPLETED:
        return {
            "days_open":           days_open,
            "days_overdue":        0,
            "sla_status":          "COMPLETED",
            "risk_level":          "LOW",
            "escalation_required": False,
            "sla_due_date":        sla_due_date,
        }

    # OPEN path
    days_overdue   = max(0, (ref - due).days)
    days_until_due = max(0, (due - ref).days)

    if days_overdue > 0:
        sla_status = "OVERDUE"
    elif days_until_due <= 1:
        sla_status = "AT_RISK"
    else:
        sla_status = "ON_TRACK"

    if days_overdue > sla_days:
        risk_level = "CRITICAL"
    elif days_overdue > 0:
        risk_level = "HIGH"
    elif days_until_due <= 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return {
        "days_open":           days_open,
        "days_overdue":        days_overdue,
        "sla_status":          sla_status,
        "risk_level":          risk_level,
        "escalation_required": days_overdue > 0,
        "sla_due_date":        sla_due_date,
    }


# ---------------------------------------------------------------------------
# Stewardship helpers (internal)
# ---------------------------------------------------------------------------

def _parse_object_id_kwargs(object_type: str, object_id: str) -> dict:
    """
    Reverse-parse a composite object_id string back into keyword arguments
    for get_governance_profile().  Returns {} on any parse failure.
    """
    try:
        ot = GovernedObjectType(object_type)
    except ValueError:
        return {}

    try:
        if ot == GovernedObjectType.DICT_TABLE:
            src, fqn = object_id.split(":", 1)
            return {"source_id": int(src), "table_fqn": fqn}

        if ot == GovernedObjectType.DICT_COLUMN:
            src, fqn, col = object_id.split(":", 2)
            return {"source_id": int(src), "table_fqn": fqn, "column_name": col}

        if ot in (GovernedObjectType.DOMAIN_RULE, GovernedObjectType.ENTITY_RULE):
            return {"rule_id": int(object_id)}

        if ot == GovernedObjectType.DOMAIN_REFINEMENT:
            return {"suggestion_id": int(object_id)}

        if ot == GovernedObjectType.ENGINE_TOOL:
            return {"tool_id": object_id}

        if ot == GovernedObjectType.PII_CONFIRMATION:
            src, fqn, col = object_id.split(":", 2)
            return {"source_id": int(src), "table_fqn": fqn, "column_name": col}

    except (ValueError, TypeError):
        pass

    return {}


# ---------------------------------------------------------------------------
# Public — Stewardship Operations
# ---------------------------------------------------------------------------

def assign_governance_item(
    *,
    object_type: str,
    object_id: str,
    assigned_to: str,
    assigned_by: str,
    source_id: int | None = None,
    assignment_group: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
) -> dict:
    """
    Create a stewardship assignment for a governed object.

    Auto-calculates priority from the object's governance profile (PII risk,
    domain risk, confidence, policy evaluation) when not explicitly provided.
    Auto-calculates due_date from the SLA threshold for the resolved priority.

    Writes an ASSIGNED event to governance_approval_events.

    Returns the new assignment row as a dict.
    """
    # ── Resolve priority ──────────────────────────────────────────────────
    if priority is None:
        kwargs = _parse_object_id_kwargs(object_type, object_id)
        profile: GovernanceProfile | None = None
        if kwargs:
            try:
                profile = get_governance_profile(object_type=object_type, **kwargs)
            except Exception:
                pass
        priority = (
            calculate_priority_for_profile(profile)
            if profile else AssignmentPriority.MEDIUM
        )
    else:
        try:
            priority = AssignmentPriority(priority).value
        except ValueError:
            priority = AssignmentPriority.MEDIUM

    # ── Resolve due_date ─────────────────────────────────────────────────
    if due_date is None:
        sla_days = _SLA_DAYS_BY_PRIORITY.get(priority, 7)
        due_date = (
            datetime.now(timezone.utc).date() + timedelta(days=sla_days)
        ).isoformat()

    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO governance_assignments
                   (object_type, object_id, source_id,
                    assigned_to, assigned_by, assignment_group,
                    priority, status, due_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
            (object_type, object_id, source_id,
             assigned_to, assigned_by, assignment_group,
             priority, due_date, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM governance_assignments WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    finally:
        conn.close()

    log_governance_event(
        object_type_id = object_type,
        object_id      = object_id,
        event_type     = "ASSIGNED",
        from_state     = None,
        to_state       = "ASSIGNED",
        actor_id       = assigned_by,
        notes          = f"Assigned to '{assigned_to}' | priority: {priority}",
        source_service = "governance_service.assign",
    )

    return dict(row) if row else {}


def reassign_governance_item(
    *,
    assignment_id: int,
    new_assignee: str,
    reassigned_by: str,
    reason: str | None = None,
) -> dict | None:
    """
    Transfer an OPEN assignment to a different steward.

    Returns the updated assignment dict, or None if the assignment does not
    exist or is already COMPLETED.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM governance_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["status"] != AssignmentStatus.OPEN:
            return None  # cannot reassign completed assignments

        prev_assignee = d["assigned_to"]
        conn.execute(
            "UPDATE governance_assignments SET assigned_to = ?, updated_at = ? WHERE id = ?",
            (new_assignee, now, assignment_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM governance_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
    finally:
        conn.close()

    note = f"Reassigned from '{prev_assignee}' to '{new_assignee}'."
    if reason:
        note += f" Reason: {reason}"

    log_governance_event(
        object_type_id = d["object_type"],
        object_id      = d["object_id"],
        event_type     = "REASSIGNED",
        from_state     = "ASSIGNED",
        to_state       = "ASSIGNED",
        actor_id       = reassigned_by,
        notes          = note,
        source_service = "governance_service.reassign",
    )

    return dict(updated) if updated else None


def complete_assignment(
    *,
    assignment_id: int,
    completed_by: str,
) -> dict | None:
    """
    Mark a governance assignment as COMPLETED.

    Returns the updated assignment dict, or None if not found or already
    COMPLETED.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM governance_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["status"] != AssignmentStatus.OPEN:
            return None

        conn.execute(
            """UPDATE governance_assignments
                  SET status = 'COMPLETED', completed_at = ?, updated_at = ?
                WHERE id = ?""",
            (now, now, assignment_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM governance_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
    finally:
        conn.close()

    log_governance_event(
        object_type_id = d["object_type"],
        object_id      = d["object_id"],
        event_type     = "ASSIGNMENT_COMPLETED",
        from_state     = "ASSIGNED",
        to_state       = "COMPLETED",
        actor_id       = completed_by,
        source_service = "governance_service.complete",
    )

    return dict(updated) if updated else None


def list_assignments(
    *,
    assigned_to: str | None = None,
    assignment_group: str | None = None,
    source_id: int | None = None,
    priority: str | None = None,
    object_type: str | None = None,
    status: str | None = None,
    overdue_only: bool = False,
    reference_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """
    Return governance assignments matching the given filters.

    SLA data is attached to every row under the "sla" key.
    Results are sorted CRITICAL → HIGH → MEDIUM → LOW, then by created_at ASC.

    Parameters
    ----------
    overdue_only     : When True, only items whose SLA status is OVERDUE are returned.
    reference_date   : ISO datetime for SLA calculations (defaults to UTC now).
    """
    sql = f"SELECT * FROM governance_assignments WHERE 1=1"
    params: list = []

    if assigned_to:
        sql += " AND assigned_to = ?"
        params.append(assigned_to)
    if assignment_group:
        sql += " AND assignment_group = ?"
        params.append(assignment_group)
    if source_id is not None:
        sql += " AND source_id = ?"
        params.append(source_id)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if object_type:
        sql += " AND object_type = ?"
        params.append(object_type)
    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += f" ORDER BY {_PRIORITY_SQL_ORDER}, created_at ASC"
    sql += f" LIMIT {limit} OFFSET {offset}"

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        d["sla"] = calculate_sla(d, reference_date)
        if overdue_only and d["sla"]["sla_status"] != "OVERDUE":
            continue
        result.append(d)

    return result


def assignment_summary(
    *,
    assigned_to: str | None = None,
    source_id: int | None = None,
    reference_date: str | None = None,
) -> dict:
    """
    Return governance work metrics.

    Parameters
    ----------
    assigned_to    : Scope to one steward's queue.
    source_id      : Scope to one data source.
    reference_date : ISO datetime for SLA / "today" calculations.

    Returns
    -------
    {
        open:                int,
        completed_today:     int,
        overdue:             int,
        overdue_pct:         float,
        critical_backlog:    int,
        avg_resolution_days: float | None,
        by_priority:         {CRITICAL, HIGH, MEDIUM, LOW: int},
        by_object_type:      {object_type: int, ...},
        by_steward:          [{assigned_to, open, overdue}, ...],
    }
    """
    conn = get_connection()
    try:
        sql = "SELECT * FROM governance_assignments WHERE 1=1"
        params: list = []
        if assigned_to:
            sql += " AND assigned_to = ?"
            params.append(assigned_to)
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    all_items = [dict(r) for r in rows]

    # Resolve "today" for completed_today metric
    if reference_date:
        ref = datetime.fromisoformat(reference_date)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
    else:
        ref = datetime.now(timezone.utc)
    today_str = ref.date().isoformat()

    open_count      = 0
    overdue_count   = 0
    completed_today = 0
    resolution_secs: list[float] = []
    by_priority     = {p.value: 0 for p in AssignmentPriority}
    by_object_type: dict[str, int] = {}
    steward_map:    dict[str, dict[str, int]] = {}

    for item in all_items:
        sla = calculate_sla(item, reference_date)
        p   = item.get("priority", AssignmentPriority.MEDIUM)
        st  = item.get("status", AssignmentStatus.OPEN)
        ot  = item.get("object_type", "unknown")

        if st == AssignmentStatus.OPEN:
            open_count += 1
            by_priority[p] = by_priority.get(p, 0) + 1
            by_object_type[ot] = by_object_type.get(ot, 0) + 1

            steward = item.get("assigned_to", "")
            if steward not in steward_map:
                steward_map[steward] = {"open": 0, "overdue": 0}
            steward_map[steward]["open"] += 1

            if sla["days_overdue"] > 0:
                overdue_count += 1
                steward_map[steward]["overdue"] += 1

        elif st == AssignmentStatus.COMPLETED:
            comp_at = item.get("completed_at") or ""
            if comp_at[:10] == today_str:
                completed_today += 1
            try:
                created   = datetime.fromisoformat(item["created_at"])
                completed = datetime.fromisoformat(comp_at)
                resolution_secs.append(
                    abs((completed - created).total_seconds())
                )
            except (ValueError, TypeError):
                pass

    overdue_pct = (
        round(overdue_count / open_count * 100, 1) if open_count > 0 else 0.0
    )
    avg_resolution_days = (
        round(sum(resolution_secs) / len(resolution_secs) / 86400, 1)
        if resolution_secs else None
    )

    return {
        "open":                open_count,
        "completed_today":     completed_today,
        "overdue":             overdue_count,
        "overdue_pct":         overdue_pct,
        "critical_backlog":    by_priority.get(AssignmentPriority.CRITICAL, 0),
        "avg_resolution_days": avg_resolution_days,
        "by_priority":         by_priority,
        "by_object_type":      by_object_type,
        "by_steward": [
            {
                "assigned_to": k,
                "open":        v["open"],
                "overdue":     v["overdue"],
            }
            for k, v in sorted(steward_map.items(), key=lambda x: -x[1]["open"])
        ],
    }


# ---------------------------------------------------------------------------
# Decision Intelligence — Phase 5
# ---------------------------------------------------------------------------

class NextAction(str, Enum):
    """
    Recommended next action for a governed object.
    Computed purely from the GovernanceProfile — no DB calls.
    """
    APPROVE             = "APPROVE"
    REJECT              = "REJECT"
    REVIEW_PII          = "REVIEW_PII"
    REVIEW_DICTIONARY   = "REVIEW_DICTIONARY"
    REVIEW_DOMAIN       = "REVIEW_DOMAIN"
    REVIEW_ENTITY       = "REVIEW_ENTITY"
    ESCALATE            = "ESCALATE"
    ASSIGN_TO_STEWARD   = "ASSIGN_TO_STEWARD"
    NEEDS_MORE_METADATA = "NEEDS_MORE_METADATA"
    NO_ACTION           = "NO_ACTION"


@dataclass
class GovernanceExplanation:
    """
    Structured explanation for every governance decision.

    Built from an existing GovernanceProfile — no additional DB reads.
    Combines risk scoring, action recommendation, and human-readable
    narratives so stewards understand exactly what to do and why.
    """
    object_type_id:           str
    object_id:                str
    decision:                 str          # Human-readable decision sentence
    decision_type:            str          # AUTO_APPROVED | HUMAN_APPROVED | BLOCKED | PENDING_REVIEW | …
    risk_score:               int          # 0–100
    confidence_score:         float | None
    matched_policies:         list[str]
    blocking_policies:        list[str]
    evidence:                 list[dict]
    recommended_action:       str          # NextAction value
    recommended_steward:      str | None
    estimated_review_minutes: int
    priority_reason:          str
    risk_factors:             list[str]    # Human-readable risk narrative bullets
    can_ai_use:               bool
    ai_warning:               str | None

    def to_dict(self) -> dict:
        return {
            "object_type_id":           self.object_type_id,
            "object_id":                self.object_id,
            "decision":                 self.decision,
            "decision_type":            self.decision_type,
            "risk_score":               self.risk_score,
            "confidence_score":         self.confidence_score,
            "matched_policies":         self.matched_policies,
            "blocking_policies":        self.blocking_policies,
            "evidence":                 self.evidence,
            "recommended_action":       self.recommended_action,
            "recommended_steward":      self.recommended_steward,
            "estimated_review_minutes": self.estimated_review_minutes,
            "priority_reason":          self.priority_reason,
            "risk_factors":             self.risk_factors,
            "can_ai_use":               self.can_ai_use,
            "ai_warning":               self.ai_warning,
        }


# ---------------------------------------------------------------------------
# Intelligence helpers — pure functions, no DB calls
# ---------------------------------------------------------------------------

_STATE_RISK_BASE: dict[GovernanceState, int] = {
    GovernanceState.GENERATED:       60,  # unreviewed, unknown quality
    GovernanceState.SUGGESTED:       35,  # in queue, awaiting decision
    GovernanceState.NEEDS_REVIEW:    70,  # explicitly flagged as problematic
    GovernanceState.VALIDATED:       20,  # passed threshold, not yet approved
    GovernanceState.AUTO_APPROVED:    5,  # policy-approved, low risk
    GovernanceState.HUMAN_APPROVED:   5,  # reviewed by a human, very low risk
    GovernanceState.REJECTED:        30,  # was reviewed, found unacceptable
    GovernanceState.DEPRECATED:      25,  # no longer current
    GovernanceState.ARCHIVED:        20,  # closed, audit-preserved
}


def calculate_risk_score(profile: GovernanceProfile) -> int:
    """
    Compute a 0–100 risk score from the governance profile.

    Higher = riskier = more urgently needs attention.
    Uses only information already present in the GovernanceProfile;
    makes no DB calls.
    """
    score = _STATE_RISK_BASE.get(profile.approval_state, 40)

    # PII risk — highest weight (regulatory / legal exposure)
    if profile.pii_risk:
        score += 30
    if profile.blocking_policy == _HARD_POLICY_PII:
        score += 20

    # High-risk domain (Finance, Regulatory, Legal …)
    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        score += 20

    # Other governance policy blocking
    if profile.blocking_policy and profile.blocking_policy not in (
        _HARD_POLICY_PII, _HARD_POLICY_HIGH_RISK, _HARD_POLICY_IRREVERSIBLE,
    ):
        score += 10

    # Confidence adjustments
    c = profile.confidence_score
    if c is None:
        score += 5                        # unknown quality
    elif c < 0.60:
        score += 20                       # low accuracy
    elif c < 0.80:
        score += 10                       # medium accuracy
    elif c >= 0.95:
        score -= 10                       # very high accuracy → lower risk

    # Auto-approval eligible → policy cleared it → lower risk
    if profile.auto_approval_eligible:
        score -= 20

    # No evidence → unsubstantiated classification
    if not profile.evidence and profile.approval_state not in (
        GovernanceState.HUMAN_APPROVED,
        GovernanceState.AUTO_APPROVED,
    ):
        score += 5

    return max(0, min(100, score))


def recommend_next_action(profile: GovernanceProfile) -> str:
    """
    Determine the single most important action for a steward.
    Pure function — no DB calls.
    """
    state = profile.approval_state

    # Terminal states — nothing to do
    if state in (
        GovernanceState.HUMAN_APPROVED,
        GovernanceState.AUTO_APPROVED,
        GovernanceState.REJECTED,
        GovernanceState.DEPRECATED,
        GovernanceState.ARCHIVED,
    ):
        return NextAction.NO_ACTION

    # PII takes absolute precedence
    if profile.pii_risk or profile.blocking_policy == _HARD_POLICY_PII:
        return NextAction.REVIEW_PII

    # High-risk domain → escalate (e.g., to Domain Owner)
    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        return NextAction.ESCALATE

    # Explicitly flagged for review → escalate
    if state == GovernanceState.NEEDS_REVIEW:
        return NextAction.ESCALATE

    # Policy cleared it → approve
    if profile.auto_approval_eligible:
        return NextAction.APPROVE

    # Not yet surfaced → generate metadata first
    if state == GovernanceState.GENERATED:
        return NextAction.NEEDS_MORE_METADATA

    # For SUGGESTED / VALIDATED: recommend type-specific action
    ot = profile.object_type_id
    if ot == GovernedObjectType.PII_CONFIRMATION:
        return NextAction.REVIEW_PII
    if ot in (GovernedObjectType.DICT_TABLE, GovernedObjectType.DICT_COLUMN):
        return NextAction.REVIEW_DICTIONARY
    if ot in (GovernedObjectType.DOMAIN_RULE, GovernedObjectType.DOMAIN_REFINEMENT):
        return NextAction.REVIEW_DOMAIN
    if ot == GovernedObjectType.ENTITY_RULE:
        return NextAction.REVIEW_ENTITY
    if ot == GovernedObjectType.ENGINE_TOOL:
        return NextAction.ASSIGN_TO_STEWARD

    # DB policy blocking (non-hard) → assign to a steward
    if profile.blocking_policy:
        return NextAction.ASSIGN_TO_STEWARD

    return NextAction.REVIEW_DICTIONARY   # safe default


def _build_decision_text(profile: GovernanceProfile) -> tuple[str, str]:
    """Return (decision_sentence, decision_type_key)."""
    state = profile.approval_state

    if state == GovernanceState.HUMAN_APPROVED:
        reviewer = profile.reviewed_by or "a reviewer"
        when = (profile.reviewed_at or "")[:10] or "unknown date"
        return (f"Approved by {reviewer} on {when}.", "HUMAN_APPROVED")

    if state == GovernanceState.AUTO_APPROVED:
        policy = profile.matched_policy or "governance policy"
        return (f"Auto-approved by policy: {policy}.", "AUTO_APPROVED")

    if state == GovernanceState.REJECTED:
        return ("This item has been rejected and is inactive.", "REJECTED")

    if state == GovernanceState.DEPRECATED:
        return ("This item has been deprecated and superseded.", "DEPRECATED")

    if state == GovernanceState.ARCHIVED:
        return ("This item has been archived for audit purposes.", "ARCHIVED")

    if state == GovernanceState.NEEDS_REVIEW:
        return (
            "Flagged for mandatory human review — escalation required.",
            "ESCALATED",
        )

    if state == GovernanceState.VALIDATED:
        return (
            "Meets confidence threshold — eligible for auto-approval.",
            "PENDING_AUTO_APPROVE",
        )

    # SUGGESTED or GENERATED
    if profile.blocking_policy:
        reason = profile.review_reason or f"blocked by '{profile.blocking_policy}'"
        return (f"Blocked from auto-approval: {reason}", "BLOCKED")

    if state == GovernanceState.GENERATED:
        return (
            "Generated by AI/rules — not yet surfaced for review.",
            "GENERATED",
        )

    conf_str = ""
    if profile.confidence_score is not None:
        conf_str = f" Confidence: {profile.confidence_score:.0%}."
    return (f"Awaiting human review.{conf_str}", "PENDING_REVIEW")


def _build_risk_factors(profile: GovernanceProfile) -> list[str]:
    """Build an ordered list of human-readable risk narrative bullets."""
    factors: list[str] = []

    if profile.pii_risk:
        factors.append(
            "PII risk signals detected — confirmation required before AI use."
        )
    if profile.blocking_policy == _HARD_POLICY_PII:
        factors.append(
            "Unconfirmed PII data — hard safety policy requires a named human reviewer."
        )
    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        domain = profile.domain_context or "high-risk"
        factors.append(
            f"High-risk domain '{domain}' — financial or regulatory sensitivity."
        )
    if profile.approval_state == GovernanceState.NEEDS_REVIEW:
        factors.append(
            "Item has been explicitly flagged for mandatory review."
        )

    c = profile.confidence_score
    if c is not None and c < 0.60:
        factors.append(
            f"Low confidence: {c:.0%} — AI classification accuracy below acceptable threshold."
        )
    elif c is not None and c < 0.80:
        factors.append(
            f"Medium confidence: {c:.0%} — validation recommended before relying on this classification."
        )
    elif c is None:
        factors.append(
            "No confidence score — rule-based or human classification without AI scoring."
        )

    if profile.approval_state == GovernanceState.GENERATED:
        factors.append(
            "Not yet surfaced for review — metadata may lack business context."
        )

    if not profile.evidence and profile.approval_state not in (
        GovernanceState.HUMAN_APPROVED, GovernanceState.AUTO_APPROVED,
    ):
        factors.append(
            "No supporting evidence signals available for this classification."
        )

    if profile.blocking_policy and profile.blocking_policy not in (
        _HARD_POLICY_PII, _HARD_POLICY_HIGH_RISK, _HARD_POLICY_IRREVERSIBLE,
    ):
        factors.append(
            f"Governance policy '{profile.blocking_policy}' requires human review."
        )

    return factors


def _build_priority_reason(profile: GovernanceProfile) -> str:
    """Single-sentence explanation of why this priority was assigned."""
    if profile.pii_risk or profile.blocking_policy == _HARD_POLICY_PII:
        return "PII risk detected — CRITICAL priority for immediate human review."
    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        domain = profile.domain_context or "high-risk"
        return f"High-risk domain ({domain}) — HIGH priority for compliance review."
    if profile.approval_state == GovernanceState.NEEDS_REVIEW:
        return "Explicitly flagged for mandatory review — HIGH priority."
    c = profile.confidence_score
    if c is not None and c < 0.60:
        return (
            f"Low confidence ({c:.0%}) — HIGH priority; "
            "classification reliability below threshold."
        )
    if profile.auto_approval_eligible:
        return "Auto-approval eligible — LOW priority; policy has cleared this item."
    if c is not None and c < 0.80:
        return (
            f"Medium confidence ({c:.0%}) — MEDIUM priority; "
            "human validation recommended."
        )
    if c is not None and c >= 0.80:
        return f"High confidence ({c:.0%}) — LOW priority; classification is reliable."
    return "Standard review item — MEDIUM priority."


def _recommend_steward(profile: GovernanceProfile) -> str | None:
    """Suggest the most appropriate steward role for this object."""
    ot = profile.object_type_id

    if ot == GovernedObjectType.PII_CONFIRMATION or profile.pii_risk:
        return "PII Officer"

    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        domain = profile.domain_context or "Business"
        return f"{domain} Domain Owner"

    if ot in (GovernedObjectType.DOMAIN_RULE, GovernedObjectType.DOMAIN_REFINEMENT):
        domain = profile.domain_context or "Business"
        return f"{domain} Domain Steward"

    if ot == GovernedObjectType.ENTITY_RULE:
        entity = profile.domain_context or "Business"
        return f"{entity} Entity Steward"

    if ot in (GovernedObjectType.DICT_TABLE, GovernedObjectType.DICT_COLUMN):
        return "Business Analyst"

    if ot == GovernedObjectType.ENGINE_TOOL:
        return "Governance Admin"

    return "Data Steward"


def _estimate_review_minutes(profile: GovernanceProfile) -> int:
    """Estimate how long a human reviewer will spend on this item (minutes)."""
    if profile.approval_state in (
        GovernanceState.HUMAN_APPROVED,
        GovernanceState.AUTO_APPROVED,
        GovernanceState.REJECTED,
        GovernanceState.ARCHIVED,
    ):
        return 0

    _BASE: dict[str, int] = {
        GovernedObjectType.PII_CONFIRMATION:  10,
        GovernedObjectType.DICT_TABLE:         5,
        GovernedObjectType.DICT_COLUMN:        3,
        GovernedObjectType.DOMAIN_RULE:        2,
        GovernedObjectType.ENTITY_RULE:        2,
        GovernedObjectType.DOMAIN_REFINEMENT:  2,
        GovernedObjectType.ENGINE_TOOL:       15,
    }
    minutes = _BASE.get(profile.object_type_id, 5)

    if profile.pii_risk:
        minutes += 5
    if profile.blocking_policy == _HARD_POLICY_HIGH_RISK:
        minutes += 5
    if profile.confidence_score is not None and profile.confidence_score < 0.60:
        minutes += 3

    return minutes


# ---------------------------------------------------------------------------
# Public — Decision Intelligence
# ---------------------------------------------------------------------------

def get_governance_explanation(
    *,
    object_type: str,
    source_id: int | None = None,
    table_fqn: str | None = None,
    column_name: str | None = None,
    rule_id: int | None = None,
    suggestion_id: int | None = None,
    tool_id: str | None = None,
) -> GovernanceExplanation | None:
    """
    Return a structured explanation for every governance decision.

    Calls get_governance_profile() (Phase 1 + Phase 2 enrichment), then
    derives risk score, next-action recommendation, steward suggestion,
    and human-readable narratives from the profile.

    Makes no additional DB writes.  Returns None if the object does not exist.
    """
    profile = get_governance_profile(
        object_type   = object_type,
        source_id     = source_id,
        table_fqn     = table_fqn,
        column_name   = column_name,
        rule_id       = rule_id,
        suggestion_id = suggestion_id,
        tool_id       = tool_id,
    )
    if profile is None:
        return None

    risk_score   = calculate_risk_score(profile)
    action       = recommend_next_action(profile)
    decision, dt = _build_decision_text(profile)

    return GovernanceExplanation(
        object_type_id           = profile.object_type_id,
        object_id                = profile.object_id,
        decision                 = decision,
        decision_type            = dt,
        risk_score               = risk_score,
        confidence_score         = profile.confidence_score,
        matched_policies         = [profile.matched_policy] if profile.matched_policy else [],
        blocking_policies        = [profile.blocking_policy] if profile.blocking_policy else [],
        evidence                 = profile.evidence,
        recommended_action       = action,
        recommended_steward      = _recommend_steward(profile),
        estimated_review_minutes = _estimate_review_minutes(profile),
        priority_reason          = _build_priority_reason(profile),
        risk_factors             = _build_risk_factors(profile),
        can_ai_use               = profile.can_ai_use,
        ai_warning               = profile.ai_warning,
    )


def governance_readiness_summary(
    *,
    source_id: int | None = None,
) -> dict:
    """
    Compute overall governance health from governance_state_map.

    governance_state_map contains every governed object that has been
    explicitly evaluated (Phase 1 upsert on every approval/rejection).

    Parameters
    ----------
    source_id : Scope open-assignment count to one data source; all other
                metrics are global (state_map has no source_id column).

    Returns
    -------
    {
        governance_score  : int    (0–100)
        total_governed    : int
        objects_ready     : int
        objects_pending   : int
        objects_blocked   : int
        objects_escalated : int
        high_risk_pct     : float
        auto_approval_pct : float
        avg_confidence    : float | None
        open_assignments  : int
    }
    """
    conn = get_connection()
    try:
        state_rows = conn.execute(
            """SELECT approval_state,
                      COUNT(*)                                         AS cnt,
                      AVG(confidence_score)                           AS avg_conf,
                      SUM(CASE WHEN confidence_tier = 'LOW' THEN 1 ELSE 0 END) AS low_conf_cnt
               FROM governance_state_map
               GROUP BY approval_state"""
        ).fetchall()

        assign_sql    = "SELECT COUNT(*) FROM governance_assignments WHERE status = 'OPEN'"
        assign_params: list = []
        if source_id is not None:
            assign_sql += " AND source_id = ?"
            assign_params.append(source_id)
        open_assignments = conn.execute(assign_sql, assign_params).fetchone()[0]
    finally:
        conn.close()

    # Aggregate
    state_counts:    dict[str, int]   = {}
    low_conf_totals: dict[str, int]   = {}
    conf_wsum       = 0.0
    conf_wn         = 0

    for row in state_rows:
        d    = dict(row)
        s    = d["approval_state"]
        cnt  = d["cnt"] or 0
        state_counts[s]    = cnt
        low_conf_totals[s] = d["low_conf_cnt"] or 0
        if d["avg_conf"] is not None:
            conf_wsum += float(d["avg_conf"]) * cnt
            conf_wn   += cnt

    total     = sum(state_counts.values())
    ready     = (state_counts.get("HUMAN_APPROVED", 0) +
                 state_counts.get("AUTO_APPROVED",  0))
    pending   = (state_counts.get("SUGGESTED",  0) +
                 state_counts.get("GENERATED",   0) +
                 state_counts.get("VALIDATED",   0))
    blocked   = (state_counts.get("REJECTED",   0) +
                 state_counts.get("DEPRECATED",  0) +
                 state_counts.get("ARCHIVED",    0))
    escalated = state_counts.get("NEEDS_REVIEW", 0)
    auto_app  = state_counts.get("AUTO_APPROVED", 0)

    high_risk_cnt = sum(low_conf_totals.values()) + escalated
    high_risk_pct = round(high_risk_cnt / total * 100, 1) if total > 0 else 0.0
    auto_pct      = round(auto_app / ready * 100, 1) if ready > 0 else 0.0
    avg_conf      = round(conf_wsum / conf_wn, 3) if conf_wn > 0 else None

    # Governance score: readiness ratio penalised for escalations/blocks
    if total == 0:
        gov_score = 0
    else:
        readiness_ratio = ready / total
        risk_penalty    = (escalated * 2 + blocked) / total * 0.3
        gov_score       = max(0, min(100, round((readiness_ratio - risk_penalty) * 100)))

    return {
        "governance_score":  gov_score,
        "total_governed":    total,
        "objects_ready":     ready,
        "objects_pending":   pending,
        "objects_blocked":   blocked,
        "objects_escalated": escalated,
        "high_risk_pct":     high_risk_pct,
        "auto_approval_pct": auto_pct,
        "avg_confidence":    avg_conf,
        "open_assignments":  open_assignments,
    }


# ---------------------------------------------------------------------------
# Analytics & Executive Dashboard — Phase 6
# ---------------------------------------------------------------------------

def governance_kpis(*, source_id: int | None = None) -> dict:
    """
    Compute governance KPIs from governance_state_map and governance_assignments.

    State distribution metrics (total_governed, *_pct, avg_confidence,
    avg_risk_score) are always global — governance_state_map has no source_id.
    Assignment counts (open_assignments, overdue_assignments, critical_backlog,
    avg_resolution_days) are scoped to source_id when provided.
    """
    conn = get_connection()
    try:
        state_rows = conn.execute(
            """SELECT approval_state, COUNT(*) AS cnt, AVG(confidence_score) AS avg_conf
               FROM governance_state_map
               GROUP BY approval_state"""
        ).fetchall()

        # Assignment metrics — optionally scoped by source_id
        def _count(where_extra: str, params: list) -> int:
            base = "SELECT COUNT(*) FROM governance_assignments WHERE " + where_extra
            if source_id is not None:
                base += " AND source_id = ?"
                params = params + [source_id]
            return conn.execute(base, params).fetchone()[0]

        open_assignments    = _count("status = 'OPEN'", [])
        overdue_assignments = _count(
            "status = 'OPEN' AND substr(due_date,1,10) < date('now')", []
        )
        critical_backlog    = _count("status = 'OPEN' AND priority = 'CRITICAL'", [])

        res_sql = (
            "SELECT AVG(julianday(completed_at) - julianday(created_at)) "
            "FROM governance_assignments "
            "WHERE status = 'COMPLETED' AND completed_at IS NOT NULL "
            "AND created_at IS NOT NULL"
        )
        res_params: list = []
        if source_id is not None:
            res_sql += " AND source_id = ?"
            res_params.append(source_id)
        avg_res_row = conn.execute(res_sql, res_params).fetchone()[0]
    finally:
        conn.close()

    # Aggregate state distribution
    state_counts: dict[str, int] = {}
    conf_wsum = 0.0
    conf_wn   = 0

    for row in state_rows:
        d   = dict(row)
        s   = d["approval_state"]
        cnt = d["cnt"] or 0
        state_counts[s] = cnt
        if d["avg_conf"] is not None:
            conf_wsum += float(d["avg_conf"]) * cnt
            conf_wn   += cnt

    total         = sum(state_counts.values())
    human_app     = state_counts.get("HUMAN_APPROVED", 0)
    auto_app      = state_counts.get("AUTO_APPROVED",  0)
    pending       = (state_counts.get("SUGGESTED", 0) +
                     state_counts.get("GENERATED",  0) +
                     state_counts.get("VALIDATED",  0))
    blocked       = (state_counts.get("REJECTED",   0) +
                     state_counts.get("DEPRECATED", 0) +
                     state_counts.get("ARCHIVED",   0))
    escalated     = state_counts.get("NEEDS_REVIEW", 0)

    def _pct(n: int) -> float:
        return round(n / total * 100, 1) if total > 0 else 0.0

    # Approximate average risk from state base scores (no per-object profile loading)
    weighted_risk = sum(
        _STATE_RISK_BASE.get(GovernanceState(s), 40) * c
        for s, c in state_counts.items()
        if s in {gs.value for gs in GovernanceState}
    )
    avg_risk = round(weighted_risk / total) if total > 0 else None

    return {
        "total_governed":       total,
        "human_approved_pct":   _pct(human_app),
        "auto_approved_pct":    _pct(auto_app),
        "pending_pct":          _pct(pending),
        "blocked_pct":          _pct(blocked),
        "escalated_pct":        _pct(escalated),
        "avg_confidence":       round(conf_wsum / conf_wn, 3) if conf_wn > 0 else None,
        "avg_risk_score":       avg_risk,
        "avg_resolution_days":  round(float(avg_res_row), 1) if avg_res_row is not None else None,
        "open_assignments":     open_assignments,
        "overdue_assignments":  overdue_assignments,
        "critical_backlog":     critical_backlog,
    }


def governance_trends(*, source_id: int | None = None) -> dict:
    """
    Return governance trend data.

    No history table exists yet — trend_7d and trend_30d are empty arrays
    whose schema is established for future implementation.  The velocity
    object provides today's activity counts from governance_approval_events.
    """
    conn = get_connection()
    try:
        vel_row = conn.execute(
            """SELECT
                SUM(CASE WHEN to_state IN ('HUMAN_APPROVED','AUTO_APPROVED')
                         THEN 1 ELSE 0 END)                  AS approvals_today,
                SUM(CASE WHEN to_state = 'REJECTED'
                         THEN 1 ELSE 0 END)                  AS rejections_today,
                SUM(CASE WHEN event_type = 'ASSIGNMENT_COMPLETED'
                         THEN 1 ELSE 0 END)                  AS completions_today
               FROM governance_approval_events
               WHERE date(created_at) = date('now')"""
        ).fetchone()
    finally:
        conn.close()

    readiness = governance_readiness_summary(source_id=source_id)
    v = dict(vel_row) if vel_row else {}

    return {
        "trend_available": False,
        "note": (
            "Governance trend history will be available after the first 7 days "
            "of operation. Historical snapshots are not yet persisted."
        ),
        "current_snapshot": {
            "timestamp":         _now(),
            "governance_score":  readiness["governance_score"],
            "objects_ready":     readiness["objects_ready"],
            "objects_pending":   readiness["objects_pending"],
            "objects_blocked":   readiness["objects_blocked"],
            "objects_escalated": readiness["objects_escalated"],
            "avg_confidence":    readiness["avg_confidence"],
            "open_assignments":  readiness["open_assignments"],
        },
        "velocity": {
            "approvals_today":              v.get("approvals_today") or 0,
            "rejections_today":             v.get("rejections_today") or 0,
            "assignments_completed_today":  v.get("completions_today") or 0,
        },
        # Future shape — empty until a governance_history table is added.
        "trend_7d":  [],
        "trend_30d": [],
    }


def governance_bottlenecks(*, source_id: int | None = None) -> dict:
    """
    Identify governance bottlenecks: largest pending queues, low-confidence
    areas, overdue stewards, active blocking policies, and domain/entity queues.
    """
    def _maybe_source(sql: str, params: list) -> tuple[str, list]:
        if source_id is not None:
            sql += " AND source_id = ?"
            return sql, params + [source_id]
        return sql, params

    conn = get_connection()
    try:
        # Pending queue by object type (governance_state_map — no source_id filter)
        pending_by_type = conn.execute(
            """SELECT object_type_id, COUNT(*) AS cnt, AVG(confidence_score) AS avg_conf
               FROM governance_state_map
               WHERE approval_state NOT IN
                     ('HUMAN_APPROVED','AUTO_APPROVED','REJECTED','DEPRECATED','ARCHIVED')
               GROUP BY object_type_id ORDER BY cnt DESC"""
        ).fetchall()

        # Low-confidence areas (governance_state_map — global)
        low_conf = conn.execute(
            """SELECT object_type_id, COUNT(*) AS cnt, AVG(confidence_score) AS avg_conf
               FROM governance_state_map
               WHERE confidence_tier = 'LOW' OR confidence_score < 0.60
               GROUP BY object_type_id ORDER BY cnt DESC"""
        ).fetchall()

        # NEEDS_REVIEW by type (global)
        needs_review = conn.execute(
            """SELECT object_type_id, COUNT(*) AS cnt
               FROM governance_state_map
               WHERE approval_state = 'NEEDS_REVIEW'
               GROUP BY object_type_id ORDER BY cnt DESC"""
        ).fetchall()

        # Overdue stewards (optionally scoped by source_id)
        ov_sql = (
            "SELECT assigned_to, COUNT(*) AS overdue_cnt, "
            "CAST(MAX(julianday('now') - julianday(substr(due_date,1,10))) AS INTEGER)"
            " AS oldest_days_overdue "
            "FROM governance_assignments "
            "WHERE status = 'OPEN' AND substr(due_date,1,10) < date('now')"
        )
        ov_sql, ov_params = _maybe_source(ov_sql, [])
        ov_sql += " GROUP BY assigned_to ORDER BY overdue_cnt DESC LIMIT 10"
        overdue_stewards = conn.execute(ov_sql, ov_params).fetchall()

        # Active blocking policies
        blocking_policies = conn.execute(
            """SELECT policy_name, action, priority
               FROM governance_policies
               WHERE enabled = 1 AND action IN ('REQUIRE_HUMAN','ESCALATE')
               ORDER BY priority ASC LIMIT 10"""
        ).fetchall()

        # Pending domain rule queues (source-scoped)
        dom_sql = (
            "SELECT domain, COUNT(*) AS cnt, AVG(confidence) AS avg_conf "
            "FROM domain_learning_rules WHERE approval_status = 'PENDING'"
        )
        dom_sql, dom_params = _maybe_source(dom_sql, [])
        dom_sql += " GROUP BY domain ORDER BY cnt DESC LIMIT 10"
        pending_domains = conn.execute(dom_sql, dom_params).fetchall()

        # Pending entity rule queues (source-scoped)
        ent_sql = (
            "SELECT entity, COUNT(*) AS cnt, AVG(confidence) AS avg_conf "
            "FROM entity_learning_rules WHERE approval_status = 'PENDING'"
        )
        ent_sql, ent_params = _maybe_source(ent_sql, [])
        ent_sql += " GROUP BY entity ORDER BY cnt DESC LIMIT 10"
        pending_entities = conn.execute(ent_sql, ent_params).fetchall()
    finally:
        conn.close()

    def _avg(raw) -> float | None:
        return round(float(raw), 3) if raw is not None else None

    return {
        "pending_by_type": [
            {
                "object_type":    dict(r)["object_type_id"],
                "pending_count":  dict(r)["cnt"],
                "avg_confidence": _avg(dict(r)["avg_conf"]),
            }
            for r in pending_by_type
        ],
        "low_confidence_areas": [
            {
                "object_type":    dict(r)["object_type_id"],
                "low_conf_count": dict(r)["cnt"],
                "avg_confidence": _avg(dict(r)["avg_conf"]),
            }
            for r in low_conf
        ],
        "needs_review_by_type": [
            {"object_type": dict(r)["object_type_id"], "count": dict(r)["cnt"]}
            for r in needs_review
        ],
        "overdue_stewards": [
            {
                "assigned_to":         dict(r)["assigned_to"],
                "overdue_count":       dict(r)["overdue_cnt"],
                "oldest_days_overdue": dict(r)["oldest_days_overdue"] or 0,
            }
            for r in overdue_stewards
        ],
        "active_blocking_policies": [
            {
                "policy_name": dict(r)["policy_name"],
                "action":      dict(r)["action"],
                "priority":    dict(r)["priority"],
            }
            for r in blocking_policies
        ],
        "pending_domains": [
            {
                "domain":         dict(r)["domain"],
                "pending_count":  dict(r)["cnt"],
                "avg_confidence": _avg(dict(r)["avg_conf"]),
            }
            for r in pending_domains
        ],
        "pending_entities": [
            {
                "entity":         dict(r)["entity"],
                "pending_count":  dict(r)["cnt"],
                "avg_confidence": _avg(dict(r)["avg_conf"]),
            }
            for r in pending_entities
        ],
    }


def governance_recommendations(*, source_id: int | None = None) -> list[dict]:
    """
    Generate rule-based governance recommendations.

    Evaluates threshold conditions across existing tables and returns
    a prioritised list of actionable recommendations.  No AI; pure rules.

    Recommendations are sorted CRITICAL → HIGH → MEDIUM → LOW.
    """
    conn = get_connection()
    try:
        def _qcount(sql: str, params: list | None = None) -> int:
            return conn.execute(sql, params or []).fetchone()[0] or 0

        def _with_src(sql: str, params: list) -> tuple[str, list]:
            if source_id is not None:
                return sql + " AND source_id = ?", params + [source_id]
            return sql, params

        # PII column backlog
        pii_sql, pii_p = _with_src(
            "SELECT COUNT(*) FROM data_dictionary_columns "
            "WHERE pii_risk = 1 AND is_approved = 0", []
        )
        pii_count = _qcount(pii_sql, pii_p)

        # High-confidence pending rules (domain + entity)
        hc_dom_sql, hc_dom_p = _with_src(
            "SELECT COUNT(*) FROM domain_learning_rules "
            "WHERE confidence >= 0.95 AND approval_status = 'PENDING'", []
        )
        hc_ent_sql, hc_ent_p = _with_src(
            "SELECT COUNT(*) FROM entity_learning_rules "
            "WHERE confidence >= 0.95 AND approval_status = 'PENDING'", []
        )
        hc_total = _qcount(hc_dom_sql, hc_dom_p) + _qcount(hc_ent_sql, hc_ent_p)

        # Overdue assignments
        ov_sql, ov_p = _with_src(
            "SELECT COUNT(*) FROM governance_assignments "
            "WHERE status = 'OPEN' AND substr(due_date,1,10) < date('now')", []
        )
        overdue_count = _qcount(ov_sql, ov_p)

        # Critical assignment backlog
        cr_sql, cr_p = _with_src(
            "SELECT COUNT(*) FROM governance_assignments "
            "WHERE status = 'OPEN' AND priority = 'CRITICAL'", []
        )
        critical_count = _qcount(cr_sql, cr_p)

        # Finance/regulatory domain pending
        _fin_list = ",".join(f"'{d}'" for d in sorted(_FINANCIAL_DOMAINS | _REGULATORY_DOMAINS))
        fin_sql, fin_p = _with_src(
            f"SELECT COUNT(*) FROM domain_learning_rules "
            f"WHERE approval_status = 'PENDING' AND domain IN ({_fin_list})", []
        )
        finance_count = _qcount(fin_sql, fin_p)

        # Dictionary pending backlog
        dict_sql, dict_p = _with_src(
            "SELECT COUNT(*) FROM data_dictionary_tables "
            "WHERE is_approved = 0 AND business_name IS NOT NULL", []
        )
        dict_count = _qcount(dict_sql, dict_p)

        # Metadata gap
        meta_sql, meta_p = _with_src(
            "SELECT COUNT(*) FROM data_dictionary_tables "
            "WHERE business_name IS NULL OR business_name = ''", []
        )
        meta_gap = _qcount(meta_sql, meta_p)

        # Unassigned governed objects (SUGGESTED state, no open assignment).
        # Matches on (object_type, object_id) together — object_id alone can
        # collide across different object types (e.g. dict.table id "5" and
        # domain.rule id "5" are different objects).
        unassigned_count = _qcount(
            """SELECT COUNT(*) FROM governance_state_map gsm
               WHERE gsm.approval_state = 'SUGGESTED'
               AND NOT EXISTS (
                   SELECT 1 FROM governance_assignments ga
                   WHERE ga.status = 'OPEN'
                   AND ga.object_type = gsm.object_type_id
                   AND ga.object_id = gsm.object_id
               )""",
        )
    except Exception:
        logger.warning("governance_recommendations query failed", exc_info=True)
        return []
    finally:
        conn.close()

    # Readiness for governance_score-based recommendations
    try:
        readiness = governance_readiness_summary(source_id=source_id)
        gov_score = readiness["governance_score"]
        pending_objects = readiness["objects_pending"]
        total_governed = readiness["total_governed"]
    except Exception:
        gov_score = 100
        pending_objects = 0
        total_governed = 0

    recs: list[dict] = []

    if pii_count > 0:
        recs.append({
            "id":             "REVIEW_PII_BACKLOG",
            "title":          "Review PII Column Backlog",
            "description":    (
                f"{pii_count} unapproved PII column(s) detected. "
                "Immediate review required for data privacy compliance."
            ),
            "priority":       "CRITICAL",
            "affected_count": pii_count,
            "action_endpoint": "POST /governance/bulk/dry-run",
            "action_params":   {"object_type": "dict.column", "exclude_pii": False},
        })

    if critical_count > 0:
        recs.append({
            "id":             "CLEAR_CRITICAL_BACKLOG",
            "title":          "Clear Critical Assignment Backlog",
            "description":    (
                f"{critical_count} CRITICAL assignment(s) are open. "
                "These require same-day resolution per SLA."
            ),
            "priority":       "CRITICAL",
            "affected_count": critical_count,
            "action_endpoint": "GET /governance/assignments",
            "action_params":   {"priority": "CRITICAL", "status": "OPEN"},
        })

    if total_governed > 0 and gov_score < 50:
        recs.append({
            "id":             "IMPROVE_GOVERNANCE_COVERAGE",
            "title":          "Governance Coverage Is Critically Low",
            "description":    (
                f"Overall governance score is {gov_score}/100 — "
                "below acceptable threshold. Review pending items to improve coverage."
            ),
            "priority":       "CRITICAL",
            "affected_count": pending_objects,
            "action_endpoint": "GET /governance/readiness",
            "action_params":   {},
        })

    if overdue_count > 0:
        recs.append({
            "id":             "ADDRESS_OVERDUE_ASSIGNMENTS",
            "title":          "Address Overdue Governance Assignments",
            "description":    (
                f"{overdue_count} assignment(s) have passed their SLA due date. "
                "Escalation may be required."
            ),
            "priority":       "HIGH",
            "affected_count": overdue_count,
            "action_endpoint": "GET /governance/assignments",
            "action_params":   {"overdue_only": True},
        })

    if finance_count > 0:
        recs.append({
            "id":             "ESCALATE_FINANCE_APPROVALS",
            "title":          "Escalate Finance/Regulatory Domain Approvals",
            "description":    (
                f"{finance_count} pending domain rule(s) fall in financial or "
                "regulatory domains and require escalation to domain owners."
            ),
            "priority":       "HIGH",
            "affected_count": finance_count,
            "action_endpoint": "GET /governance/assignments",
            "action_params":   {"priority": "HIGH"},
        })

    if hc_total >= 5:
        recs.append({
            "id":             "BULK_APPROVE_HIGH_CONFIDENCE",
            "title":          "Bulk Approve High-Confidence Rules",
            "description":    (
                f"{hc_total} domain/entity rule(s) have confidence ≥ 95% "
                "and are eligible for bulk approval. Run a dry-run first."
            ),
            "priority":       "HIGH",
            "affected_count": hc_total,
            "action_endpoint": "POST /governance/bulk/dry-run",
            "action_params":   {"object_type": "domain.rule", "confidence_min": 0.95},
        })

    if dict_count > 10:
        recs.append({
            "id":             "REVIEW_DICTIONARY_ENTRIES",
            "title":          "Increase Dictionary Review Rate",
            "description":    (
                f"{dict_count} data dictionary entries are awaiting review. "
                "Consider assigning stewards or scheduling a review sprint."
            ),
            "priority":       "MEDIUM",
            "affected_count": dict_count,
            "action_endpoint": "POST /governance/bulk/dry-run",
            "action_params":   {"object_type": "dict.table"},
        })

    if meta_gap > 0:
        recs.append({
            "id":             "IMPROVE_METADATA_COVERAGE",
            "title":          "Improve Metadata Coverage",
            "description":    (
                f"{meta_gap} table(s) lack business names. "
                "Run the dictionary generation pipeline to improve metadata coverage."
            ),
            "priority":       "MEDIUM",
            "affected_count": meta_gap,
            "action_endpoint": None,
            "action_params":   {},
        })

    if unassigned_count > 0:
        recs.append({
            "id":             "ASSIGN_STEWARDS",
            "title":          "Assign Stewards to Unassigned Items",
            "description":    (
                f"{unassigned_count} governed object(s) in SUGGESTED state "
                "have no active stewardship assignment."
            ),
            "priority":       "MEDIUM",
            "affected_count": unassigned_count,
            "action_endpoint": "POST /governance/assign",
            "action_params":   {},
        })

    _PSORT = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    recs.sort(key=lambda r: _PSORT.get(r["priority"], 99))
    return recs


def get_governance_dashboard(*, source_id: int | None = None) -> dict:
    """
    Master governance analytics dashboard.

    Aggregates executive summary, KPIs, trends, bottlenecks, and
    recommendations into one response.  All sub-functions are read-only.

    Parameters
    ----------
    source_id : Scope assignment counts and source-table metrics to one
                data source.  State-map metrics remain global.
    """
    return {
        "generated_at":    _now(),
        "source_id":       source_id,
        "executive_summary": governance_readiness_summary(source_id=source_id),
        "kpis":              governance_kpis(source_id=source_id),
        "trends":            governance_trends(source_id=source_id),
        "bottlenecks":       governance_bottlenecks(source_id=source_id),
        "recommendations":   governance_recommendations(source_id=source_id),
    }
