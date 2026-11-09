"""
Centralized PII access-decision policy.

History: the original (2026-08-22) "Tuesday Demo Readiness" version of this
module applied exactly one rule to every non-owner/admin requester —
CONFIRMED_PII always masked, UNCERTAIN/unclassified always blocked — with an
ALLOW exception only for the source's own owner/admin. The 2026-08-24
"remove standard-PII blocking for live SQL" milestone replaced that: an
explicit product decision that ToolSmithAI must not require any special
role, manual approval, or ownership to view ORDINARY business identity/
contact data (names, emails, phone numbers, addresses, ...) from a source
the requester is already authorized to query. Only CREDENTIAL/SECRET data
(passwords, API keys, tokens, private keys, connection strings, security
answers, government/tax IDs, payment instruments — core.dictionary.
pii_detector.is_restricted_category) remains protected, for every requester,
with no override anywhere in this codebase.

decide_pii_access() is the single place that turns "is this column PII-
flagged" into an ALLOW / BLOCK outcome (MASK no longer exists — see below).
No caller should special-case a column or role elsewhere.

Policy, in priority order:
  1. Aggregated (COUNT/SUM/AVG/...)                    -> ALLOW (aggregate, not raw values)
  2. Restricted category (credential/secret/government-ID/payment)
                                                        -> BLOCK, always, for
     every requester, regardless of classification (even a stale/incorrect
     CONFIRMED_NON_PII) or PII-confirmation state. No override exists.
  3. Everything else that reaches this function (ordinary business
     identity/contact PII, confirmed or not, classified or not)
                                                        -> ALLOW. PII
     classification remains useful METADATA (surfaced via `policy`/`reason`
     for transparency/audit — see the pii_* warning types in
     data.query_execution_service._governance_recheck) but no longer gates
     access on its own.

role/ownership are deliberately NOT parameters here — Part 7 of the
2026-08-24 milestone is explicit that this policy must work identically for
every authorized ToolSmithAI user, not create a role-based exception. Do not
re-add a role or ownership parameter to solve a future problem; if owner-only
access to something is ever needed again, that is a new, explicitly-named
policy, not a change to this one.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.dictionary.pii_detector import is_restricted_category

ALLOW = "ALLOW"
BLOCK = "BLOCK"
# MASK removed (2026-08-24 milestone) — ordinary PII is no longer masked,
# only ever ALLOWed or (restricted category) BLOCKed. Kept as a recognized
# constant only where a caller's own signature still names it for its own
# unrelated reasons; decide_pii_access itself never returns it.
MASK = "MASK"

CONFIRMED_PII = "CONFIRMED_PII"
CONFIRMED_NON_PII = "CONFIRMED_NON_PII"
UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class PIIAccessDecision:
    action: str            # ALLOW | BLOCK
    reason: str
    policy: str
    table_fqn: str
    column_name: str
    is_restricted: bool = False


def decide_pii_access(
    *,
    table_fqn: str,
    column_name: str,
    classification: "str | None" = None,
    confirmed: bool = False,
    aggregation: "str | None" = None,
) -> PIIAccessDecision:
    """Return the access decision for one PII-flagged column.

    Callers only invoke this once a column has already been identified as
    PII-flagged (pii_name_heuristic / dictionary pii_risk) — this function
    does not itself decide whether a column is PII, only what to do about
    one that already is. classification/confirmed are accepted for the
    informational reason/policy string only (never gate the outcome) —
    kept as parameters so callers can still report what the classifier
    found without this module re-deriving it.
    """
    if aggregation:
        return PIIAccessDecision(
            ALLOW, f"aggregated ({aggregation}) — no raw values returned",
            "pii.aggregate_exempt", table_fqn, column_name,
        )

    # The only remaining gate: credential/secret/government-ID/payment
    # columns stay blocked for every requester, unconditionally, even via a
    # stale or incorrect CONFIRMED_NON_PII classification.
    restricted = is_restricted_category(column_name)
    if restricted:
        return PIIAccessDecision(
            BLOCK, "restricted category (credential/secret/government-ID/payment) — no exception for any requester",
            "pii.restricted_always_blocked", table_fqn, column_name, is_restricted=True,
        )

    if classification == CONFIRMED_NON_PII:
        return PIIAccessDecision(
            ALLOW, "classified CONFIRMED_NON_PII — heuristic false positive cleared",
            "pii.confirmed_non_pii", table_fqn, column_name,
        )

    if classification == CONFIRMED_PII or confirmed:
        return PIIAccessDecision(
            ALLOW, "confirmed PII, but a standard business identity/contact category — "
                   "queryable by any authorized user under the current product policy",
            "pii.standard_identity_allow", table_fqn, column_name,
        )

    # UNCERTAIN or never classified — same standard-identity policy applies;
    # PII classification is informational only, not an access gate, so an
    # ambiguous/unclassified column is treated identically to a confirmed one.
    return PIIAccessDecision(
        ALLOW, "PII-flagged but unclassified/uncertain — standard business identity/contact "
               "policy applies (not a restricted category), queryable by any authorized user",
        "pii.standard_identity_allow_unclassified", table_fqn, column_name,
    )
