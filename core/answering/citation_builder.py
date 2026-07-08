from __future__ import annotations

from core.answering.models import Citation, CitationType


def _cite_dictionary(item) -> list[Citation]:
    tables = item.data if isinstance(item.data, list) else []
    return [
        Citation(
            source_type=CitationType.DICTIONARY_ENTRY,
            reference=t.get("table_fqn", ""),
            label=t.get("business_name") or t.get("table_fqn", "dictionary entry"),
            detail={"is_approved": t.get("is_approved")},
        )
        for t in tables if t.get("table_fqn")
    ] or [Citation(CitationType.DICTIONARY_ENTRY, "", "Data dictionary")]


def _cite_domain(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    return [Citation(
        source_type=CitationType.DOMAIN, reference="domain_summary",
        label="Domain classification", detail={"domain_counts": d.get("domain_counts")},
    )]


def _cite_entity(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    return [Citation(
        source_type=CitationType.ENTITY, reference="entity_summary",
        label="Entity classification", detail={"entity_counts": d.get("entity_counts")},
    )]


def _cite_schema(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    return [Citation(
        source_type=CitationType.TABLE, reference=str(d.get("snapshot_id", "")),
        label="Schema snapshot", detail={"table_count": d.get("table_count")},
    )]


def _cite_live_metadata(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    citations = [
        Citation(CitationType.TABLE, t.get("table_fqn", ""), t.get("table_name", "table"))
        for t in (d.get("tables") or []) if t.get("table_fqn")
    ]
    return citations or [Citation(CitationType.METADATA_SOURCE, "", "Live metadata")]


def _cite_relationship(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    return [Citation(
        source_type=CitationType.RELATIONSHIP, reference="relationship_summary",
        label="Relationship inventory", detail={"total_relationships": d.get("total_relationships")},
    )]


def _cite_knowledge_graph(item) -> list[Citation]:
    return [Citation(CitationType.KNOWLEDGE_GRAPH, "knowledge_graph_summary", "Knowledge graph")]


def _cite_profiling(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    tables = d.get("tables") or []
    return [
        Citation(CitationType.PROFILING, t.get("table_fqn", ""), "Profiling result")
        for t in tables if t.get("table_fqn")
    ] or [Citation(CitationType.PROFILING, "", "Profiling")]


def _cite_reports(item) -> list[Citation]:
    reports = item.data if isinstance(item.data, list) else []
    return [
        Citation(CitationType.REPORT, str(r.get("id", "")), r.get("title", "report"))
        for r in reports
    ] or [Citation(CitationType.REPORT, "", "Reports")]


def _cite_live_query(item) -> list[Citation]:
    d = item.data if isinstance(item.data, dict) else {}
    return [Citation(
        source_type=CitationType.LIVE_QUERY, reference=d.get("execution_id", ""),
        label="Live query result", detail={"row_count": d.get("row_count")},
    )]


_EXTRACTORS = {
    "dictionary": _cite_dictionary,
    "domain": _cite_domain,
    "entity": _cite_entity,
    "schema": _cite_schema,
    "live_metadata": _cite_live_metadata,
    "relationship": _cite_relationship,
    "knowledge_graph": _cite_knowledge_graph,
    "profiling": _cite_profiling,
    "reports": _cite_reports,
    "live_query": _cite_live_query,
}


def build_citations(package) -> list[Citation]:
    """Every evidence item with non-empty data produces at least one
    citation — nothing is silently dropped."""
    citations: list[Citation] = []
    for item in package.evidence:
        if item.data is None:
            continue
        extractor = _EXTRACTORS.get(item.source_service)
        if extractor is not None:
            try:
                citations.extend(extractor(item))
                continue
            except Exception:  # noqa: BLE001
                pass
        citations.append(Citation(
            source_type=CitationType.METADATA_SOURCE,
            reference=item.source_service,
            label=f"Evidence from {item.source_service}",
        ))
    return citations
