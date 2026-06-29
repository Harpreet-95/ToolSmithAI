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
from datetime import datetime, timezone
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
