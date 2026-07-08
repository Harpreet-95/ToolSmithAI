from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve(source_id: int, user_id: str, query_plan: dict) -> dict:
    """
    Enrich plan_business_query's join_plan with the raw FK inventory for
    every involved table. Does not recompute or re-score joins — the
    already-scored join_plan.steps (from semantic_layer_service, via
    plan_business_query) are passed through unchanged as "joins"; this adds
    "foreign_keys"/"dependencies" that the join plan alone doesn't expose.
    """
    from data.relationship_service import get_relationships_for_table

    join_plan = query_plan.get("join_plan") or {}
    tables = query_plan.get("tables") or []

    foreign_keys: dict[str, dict] = {}
    related_tables: set[str] = set()

    for table_fqn in tables:
        try:
            rels = get_relationships_for_table(source_id, user_id, table_fqn)
        except Exception:  # noqa: BLE001
            logger.warning(
                "relationship_resolver: get_relationships_for_table failed for %s", table_fqn
            )
            rels = None

        if not rels:
            foreign_keys[table_fqn] = {"outbound": [], "inbound": []}
            continue

        foreign_keys[table_fqn] = rels
        for row in (rels.get("outbound") or []):
            if row.get("to_table_fqn"):
                related_tables.add(row["to_table_fqn"])
        for row in (rels.get("inbound") or []):
            if row.get("from_table_fqn"):
                related_tables.add(row["from_table_fqn"])

    return {
        "joins": join_plan.get("steps", []),
        "join_required": join_plan.get("required", False),
        "primary_table": join_plan.get("primary_table"),
        "foreign_keys": foreign_keys,
        "related_tables": sorted(related_tables - set(tables)),
        "fanout_risk": join_plan.get("fanout_risk"),
        "confidence": join_plan.get("confidence"),
    }
