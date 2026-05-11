from datetime import datetime, timezone

from data.usage_service import count_usage_events_since

# Quota policy configuration for ToolSmithAI plan tiers.
#
# PLAN_LIMITS maps each plan name to a dict of event_type → monthly limit.
# None means unlimited — used for enterprise.
# Add new event types here when new metered operations are introduced.

PLAN_LIMITS: dict[str, dict[str, int | None]] = {
    "free": {
        "interpret":    100,
        "workflow_run":  20,
    },
    "business": {
        "interpret":    5000,
        "workflow_run":  500,
    },
    "enterprise": {
        "interpret":    None,   # unlimited
        "workflow_run": None,   # unlimited
    },
}


def get_plan_limit(plan: str, event_type: str) -> int | None:
    """Return the monthly limit for a plan and event_type.

    Returns None for unlimited usage (enterprise or undefined event_type).
    Falls back to the free plan limits for any unrecognised plan name,
    ensuring unknown plans are treated as the most restrictive tier.
    """
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return limits.get(event_type)


def _billing_period_start() -> str:
    """Return the first second of the current UTC month as YYYY-MM-DD HH:MM:SS."""
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, 0, 0, 0).strftime("%Y-%m-%d %H:%M:%S")


def check_quota(tenant_id: str, plan: str, event_type: str) -> dict:
    """Check whether a tenant is within their plan quota for the current billing period.

    Returns a dict with at minimum an 'allowed' key. Does not raise — the
    caller decides how to handle a quota breach.
    """
    limit = get_plan_limit(plan, event_type)
    if limit is None:
        return {"allowed": True, "unlimited": True}
    current_usage = count_usage_events_since(tenant_id, event_type, _billing_period_start())
    return {
        "allowed": current_usage < limit,
        "current_usage": current_usage,
        "limit": limit,
    }
