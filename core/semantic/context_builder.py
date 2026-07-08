from __future__ import annotations

import logging

from core.semantic.execution_plan import SemanticContext

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("semantic.context_builder: %s failed", getattr(fn, "__name__", fn))
        return None


def build(source_id: int, user_id: str, query_plan: dict, relationship_result: dict) -> SemanticContext:
    """
    Assemble the Semantic Context from already-existing reads only.
    Every section is a pass-through of an existing service's own return
    shape — this function aggregates, it does not reinterpret metadata.
    """
    from data.business_knowledge_service import get_table_business_context
    from data.dictionary_service import list_dictionary_tables
    from data.domain_service import get_domain_summary
    from data.entity_service import get_entity_summary
    from data.governance_service import governance_readiness_summary
    from data.knowledge_graph_service import knowledge_graph_summary
    from data.schema_service import get_latest_snapshot

    metadata = _safe(get_latest_snapshot, source_id, user_id)
    dictionary = _safe(list_dictionary_tables, source_id, user_id)
    domains = _safe(get_domain_summary, source_id, user_id)
    entities = _safe(get_entity_summary, source_id, user_id)
    kg = _safe(knowledge_graph_summary, source_id, user_id)
    governance = _safe(governance_readiness_summary, source_id=source_id)

    warnings = query_plan.get("warnings") or []
    needs_dictionary = any(w.get("type") == "metadata_not_approved" for w in warnings)

    needs_profiling = False
    for table_fqn in (query_plan.get("tables") or []):
        ctx = _safe(get_table_business_context, source_id, user_id, table_fqn)
        if not ctx or not ctx.get("profiling"):
            needs_profiling = True
            break
        if any(not col.get("profiling") for col in (ctx.get("columns") or [])):
            needs_profiling = True
            break

    return SemanticContext(
        metadata=metadata,
        dictionary=dictionary,
        domains=domains,
        entities=entities,
        relationships=relationship_result,
        knowledge_graph=kg,
        governance=governance,
        business_rules={
            "needs_profiling": needs_profiling,
            "needs_dictionary": needs_dictionary,
        },
    )
