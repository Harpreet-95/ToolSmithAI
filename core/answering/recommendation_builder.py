from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.answering.models import Recommendation

_STALE_THRESHOLD = timedelta(days=30)


def _find(package, source_service):
    for item in package.evidence:
        if item.source_service == source_service:
            return item
    return None


def _is_empty(data) -> bool:
    if data is None:
        return True
    if isinstance(data, (list, dict)) and not data:
        return True
    return False


def _parse_iso(value) -> "datetime | None":
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def build_recommendations(strategy, package) -> list[Recommendation]:
    """Deterministic recommendation rules. Every rule is keyed off evidence
    or strategy fields already computed elsewhere — nothing here inspects
    raw source data itself."""
    recommendations: list[Recommendation] = []

    # 1. Missing profiling -> Recommend Deep Profile.
    profiling_item = _find(package, "profiling")
    profiling_missing = profiling_item is None or _is_empty(profiling_item.data)
    if not profiling_missing and isinstance(profiling_item.data, dict):
        profiling_missing = not profiling_item.data.get("tables")
    if profiling_missing and (getattr(strategy, "requires_profiling", False) or profiling_item is not None):
        recommendations.append(Recommendation(
            type="deep_profile",
            message="Run a deep profiling job on the relevant tables to improve confidence and surface data quality signals.",
            priority="MEDIUM",
        ))

    # 2. Missing dictionary -> Recommend Dictionary Generation.
    dict_item = _find(package, "dictionary")
    dictionary_missing = dict_item is None or _is_empty(dict_item.data)
    if not dictionary_missing and isinstance(dict_item.data, list):
        dictionary_missing = any(
            not (t.get("business_name") or t.get("description")) for t in dict_item.data
        )
    if dictionary_missing and (getattr(strategy, "requires_dictionary", False) or dict_item is not None):
        recommendations.append(Recommendation(
            type="dictionary_generation",
            message="Generate data dictionary entries for these tables to improve answer quality.",
            priority="MEDIUM",
        ))

    # 3. Governance warning -> Recommend Review.
    governance_flagged = bool(getattr(strategy, "governance_checks", None))
    if not governance_flagged:
        governance_flagged = any(item.governance_state for item in package.evidence)
    if governance_flagged:
        recommendations.append(Recommendation(
            type="review",
            message="Route this response through governance/data steward review before acting on it.",
            priority="HIGH",
        ))

    # 4. Stale metadata -> Recommend Rescan.
    schema_item = _find(package, "schema") or _find(package, "live_metadata")
    if getattr(strategy, "requires_metadata", False):
        discovered_at = None
        if schema_item is not None and isinstance(schema_item.data, dict):
            discovered_at = schema_item.data.get("discovered_at") or schema_item.data.get("last_discovered_at")
        parsed = _parse_iso(discovered_at)
        is_stale = schema_item is None or parsed is None or (
            datetime.now(timezone.utc) - parsed > _STALE_THRESHOLD
        )
        if is_stale:
            recommendations.append(Recommendation(
                type="rescan",
                message="Re-scan this source's metadata — the schema snapshot is missing or may be stale.",
                priority="LOW",
            ))

    return recommendations
