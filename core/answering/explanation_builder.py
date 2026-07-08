from __future__ import annotations

from core.answering.models import AnswerType
from core.orchestrator.models import IntentType


def _find(package, source_service):
    for item in package.evidence:
        if item.source_service == source_service:
            return item
    return None


def _items_with_data(package):
    return [e for e in package.evidence if e.data is not None]


def _is_governance_restricted(package) -> bool:
    return any(
        item.governance_state and "restricted" in item.governance_state.lower()
        for item in package.evidence
    )


def _no_evidence_result(answer_type: AnswerType) -> dict:
    return {
        "answer": "No evidence was available to answer this request. Select a data source and try again.",
        "summary": "No evidence collected.",
        "answer_type": answer_type,
        "confidence": 0,
        "limitations": ["No evidence was collected for this request."],
    }


def _restricted_result() -> dict:
    return {
        "answer": (
            "Access to this data is restricted by governance policy. This response has been "
            "withheld pending data steward review."
        ),
        "summary": "Governance restriction detected.",
        "answer_type": AnswerType.RESTRICTED,
        "confidence": 100,
        "limitations": ["Data access is restricted — sensitive or PII fields may be governed."],
    }


# ---------------------------------------------------------------------------
# Per-category prose builders — richer than composer.py's terser _answer_*
# functions, but grounded in exactly the same evidence, never inventing a
# fact not present in package.evidence.
# ---------------------------------------------------------------------------

def _explain_dictionary(package) -> dict:
    item = _find(package, "dictionary")
    if item is None or not item.data:
        return {
            "answer": "No data dictionary entries were found for this data source.",
            "summary": "Dictionary is empty or not yet generated.",
            "answer_type": AnswerType.DICTIONARY, "confidence": 0,
            "limitations": ["Dictionary has not been generated for this source."],
        }
    tables = item.data
    total = len(tables)
    approved = sum(1 for t in tables if t.get("is_approved"))
    named = [t for t in tables if t.get("business_name")]
    sample = named[0] if named else (tables[0] if tables else None)

    parts = [f"The data dictionary for this source covers {total} table(s), of which {approved} are approved."]
    if sample:
        label = sample.get("business_name") or sample.get("table_fqn")
        parts.append(
            f"For example, {sample.get('table_fqn')} is documented as \"{label}\", "
            f"classified as a {'core, approved' if sample.get('is_approved') else 'draft, unapproved'} business entity."
        )
    limitations = []
    if approved < total:
        limitations.append(f"{total - approved} table(s) have unapproved or draft definitions.")
    return {
        "answer": " ".join(parts),
        "summary": f"{approved}/{total} dictionary tables approved.",
        "answer_type": AnswerType.DICTIONARY,
        "confidence": 90 if approved == total else 65,
        "limitations": limitations,
    }


def _explain_domain(package) -> dict:
    item = _find(package, "domain")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data or not data.get("tables_total"):
        return {
            "answer": "No business domain assignments have been generated for this data source yet.",
            "summary": "Domain classification not yet available.",
            "answer_type": AnswerType.DOMAIN, "confidence": 0,
            "limitations": ["Domain assignments are not yet generated."],
        }
    total, assigned = data["tables_total"], data.get("tables_assigned", 0)
    top = sorted((data.get("domain_counts") or {}).items(), key=lambda x: -x[1])[:3]
    top_str = ", ".join(f"{d} ({c} tables)" for d, c in top if d != "Unknown")
    answer = (
        f"{assigned} of {total} table(s) in this source have been classified into a business domain."
    )
    if top_str:
        answer += f" The dominant domains are {top_str}."
    return {
        "answer": answer, "summary": f"{assigned}/{total} tables domain-classified.",
        "answer_type": AnswerType.DOMAIN,
        "confidence": round(100 * assigned / total) if total else 0,
        "limitations": [] if assigned == total else [f"{total - assigned} table(s) remain unclassified."],
    }


def _explain_entity(package) -> dict:
    item = _find(package, "entity")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data or not data.get("tables_total"):
        return {
            "answer": "No entity classifications have been generated for this data source yet.",
            "summary": "Entity classification not yet available.",
            "answer_type": AnswerType.ENTITY, "confidence": 0,
            "limitations": ["Entity assignments are not yet generated."],
        }
    total, assigned = data["tables_total"], data.get("entities_assigned", 0)
    top = sorted((data.get("entity_counts") or {}).items(), key=lambda x: -x[1])[:3]
    top_str = ", ".join(f"{e} ({c} tables)" for e, c in top if e != "Unknown")
    answer = f"{assigned} of {total} table(s) have been mapped to a master-data entity type."
    if top_str:
        answer += f" Detected entity types include {top_str}."
    return {
        "answer": answer, "summary": f"{assigned}/{total} tables entity-classified.",
        "answer_type": AnswerType.ENTITY,
        "confidence": round(100 * assigned / total) if total else 0,
        "limitations": [] if assigned == total else [f"{total - assigned} table(s) remain unmapped."],
    }


def _explain_profiling(package) -> dict:
    item = _find(package, "profiling")
    data = item.data if item and isinstance(item.data, dict) else None
    tables = (data or {}).get("tables") or []
    if not data or not tables:
        return {
            "answer": "No profiling results are available for this data source. Run a profiling job to generate quality statistics.",
            "summary": "No profiling data available.",
            "answer_type": AnswerType.PROFILING, "confidence": 0,
            "limitations": ["No profiling snapshot found."],
        }
    total = len(tables)
    pii_tables = [t for t in tables if (t.get("pii_column_count") or 0) > 0]
    confirmed_pii = sum(t.get("confirmed_pii_count", 0) or 0 for t in tables)
    sample = tables[0]
    row_count = sample.get("exact_row_count") or sample.get("row_count_estimate")
    parts = [f"Profiling has completed for {total} table(s) in this source."]
    if row_count:
        parts.append(
            f"For example, {sample.get('table_fqn', 'the sampled table')} contains approximately "
            f"{row_count:,} records."
        )
    if pii_tables:
        parts.append(
            f"{len(pii_tables)} table(s) contain potential PII ({confirmed_pii} confirmed PII column(s)), "
            "and should be accessed according to organizational data governance policy."
        )
    return {
        "answer": " ".join(parts),
        "summary": f"{total} tables profiled, {len(pii_tables)} with potential PII.",
        "answer_type": AnswerType.PROFILING,
        "confidence": 85,
        "limitations": ["PII column names are not exposed in this summary."] if pii_tables else [],
    }


def _explain_governance(package) -> dict:
    item = _find(package, "governance")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data:
        return {
            "answer": "No governance data is available for this source.",
            "summary": "Governance data unavailable.",
            "answer_type": AnswerType.GOVERNANCE, "confidence": 0,
            "limitations": ["No governance evidence collected."],
        }
    score = data.get("governance_score", 0)
    total = data.get("total_governed", 0)
    ready = data.get("objects_ready", 0)
    pending = data.get("objects_pending", 0)
    escalated = data.get("objects_escalated", 0)
    parts = [f"This source has a governance readiness score of {score}/100."]
    if total:
        parts.append(f"{ready} of {total} governed objects are approved.")
    if pending or escalated:
        parts.append(
            f"{pending} object(s) are pending review"
            + (f" and {escalated} are escalated for urgent attention" if escalated else "") + "."
        )
    return {
        "answer": " ".join(parts),
        "summary": f"Governance score {score}/100.",
        "answer_type": AnswerType.GOVERNANCE,
        "confidence": 90,
        "limitations": [] if not (pending or escalated) else ["Some governed objects await review."],
    }


def _explain_relationship(package) -> dict:
    item = _find(package, "relationship")
    data = item.data if item and isinstance(item.data, dict) else None
    total = (data or {}).get("total_relationships", 0)
    if not data or not total:
        return {
            "answer": "No relationships (foreign keys) have been discovered for this data source yet.",
            "summary": "No relationships discovered.",
            "answer_type": AnswerType.RELATIONSHIP, "confidence": 0,
            "limitations": ["Relationship discovery has not run, or found no relationships."],
        }
    outbound = data.get("tables_with_outbound_fks", 0)
    most_ref = data.get("most_referenced") or []
    parts = [
        f"{total} relationship(s) have been discovered across {outbound} table(s) with outbound foreign keys."
    ]
    if most_ref:
        top = most_ref[0]
        parts.append(
            f"{top.get('table_fqn', 'A table')} is the most referenced, with "
            f"{top.get('inbound_count', 0)} inbound relationship(s) — a strong signal it is a core reference table."
        )
    return {
        "answer": " ".join(parts),
        "summary": f"{total} relationships discovered.",
        "answer_type": AnswerType.RELATIONSHIP, "confidence": 85, "limitations": [],
    }


def _explain_knowledge_graph(package) -> dict:
    item = _find(package, "knowledge_graph")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data:
        return {
            "answer": "No knowledge graph coverage data is available for this source.",
            "summary": "Knowledge graph unavailable.",
            "answer_type": AnswerType.KNOWLEDGE_GRAPH, "confidence": 0,
            "limitations": ["No knowledge graph evidence collected."],
        }
    metrics = data.get("metrics") or {}
    answer = (
        "The knowledge graph for this source connects "
        f"{data.get('total_nodes', metrics.get('total_schema_tables', 'an unknown number of'))} table(s) "
        f"via {data.get('total_edges', metrics.get('total_relationships', 'an unknown number of'))} relationship(s)."
    )
    return {
        "answer": answer, "summary": "Knowledge graph coverage summarized.",
        "answer_type": AnswerType.KNOWLEDGE_GRAPH, "confidence": 75, "limitations": [],
    }


def _explain_review(package) -> dict:
    gov_item = _find(package, "governance")
    dict_item = _find(package, "dictionary")
    pending_total = 0
    parts = []
    if gov_item and isinstance(gov_item.data, dict):
        g = gov_item.data
        p = g.get("objects_pending", 0) + g.get("objects_escalated", 0)
        pending_total += p
        if p:
            parts.append(f"{p} governance object(s) await review.")
    if dict_item and isinstance(dict_item.data, list):
        weak = sum(1 for t in dict_item.data if not (t.get("business_name") or t.get("description")))
        pending_total += weak
        if weak:
            parts.append(f"{weak} dictionary entry(ies) need descriptions.")
    if not parts:
        return {
            "answer": "No pending review items were found for this data source.",
            "summary": "No pending reviews.",
            "answer_type": AnswerType.REVIEW, "confidence": 80, "limitations": [],
        }
    return {
        "answer": " ".join(parts) + f" Total pending items: {pending_total}.",
        "summary": f"{pending_total} item(s) pending review.",
        "answer_type": AnswerType.REVIEW, "confidence": 80, "limitations": [],
    }


def _explain_metadata(package) -> dict:
    live_item = _find(package, "live_metadata")
    schema_item = _find(package, "schema")
    if live_item and isinstance(live_item.data, dict) and live_item.data.get("tables") is not None:
        d = live_item.data
        answer = (
            f"A live scan of this source found {len(d.get('tables', []))} table(s) and "
            f"{len(d.get('views', []))} view(s) across {len(d.get('schemas', []))} schema(s)."
        )
        return {
            "answer": answer, "summary": "Live metadata retrieved.",
            "answer_type": AnswerType.LIVE_METADATA,
            "confidence": 90 if d.get("connection_state") == "online" else 40,
            "limitations": d.get("warnings") or [],
        }
    if schema_item and isinstance(schema_item.data, dict):
        d = schema_item.data
        answer = (
            f"The last recorded schema snapshot for this source contains {d.get('table_count', 0)} table(s), "
            f"{d.get('view_count', 0)} view(s), and {d.get('column_count', 0)} column(s)."
        )
        return {
            "answer": answer, "summary": "Metadata snapshot summarized.",
            "answer_type": AnswerType.METADATA, "confidence": 75, "limitations": [],
        }
    return _no_evidence_result(AnswerType.METADATA)


def _explain_live_query(package) -> dict:
    item = _find(package, "live_query")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data:
        return _no_evidence_result(AnswerType.LIVE_QUERY)
    status = data.get("status")
    if status != "success":
        return {
            "answer": f"The live query did not complete successfully (status: {status}). {data.get('error') or ''}".strip(),
            "summary": f"Live query status: {status}.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 20,
            "limitations": [data.get("error")] if data.get("error") else [],
        }
    row_count = data.get("row_count", 0)
    duration = data.get("duration_ms", 0)
    answer = (
        f"The live query returned {row_count} row(s) across {len(data.get('columns', []))} column(s) "
        f"in {duration}ms."
    )
    if data.get("truncated"):
        answer += " Results were truncated by the configured row limit."
    return {
        "answer": answer, "summary": f"{row_count} rows returned.",
        "answer_type": AnswerType.LIVE_QUERY, "confidence": 95,
        "limitations": ["Results were truncated."] if data.get("truncated") else [],
    }


def _explain_reports(package) -> dict:
    item = _find(package, "reports")
    reports = item.data if item and isinstance(item.data, list) else []
    if not reports:
        return {
            "answer": "No saved reports were found for this account.",
            "summary": "No reports found.",
            "answer_type": AnswerType.REPORT, "confidence": 50, "limitations": [],
        }
    return {
        "answer": f"{len(reports)} saved report(s) are available for this account.",
        "summary": f"{len(reports)} reports found.",
        "answer_type": AnswerType.REPORT, "confidence": 80, "limitations": [],
    }


def _explain_search(package) -> dict:
    item = _find(package, "search")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data or not data.get("results"):
        return {
            "answer": "The search returned no matching metadata assets.",
            "summary": "No search results.",
            "answer_type": AnswerType.SEARCH, "confidence": 30, "limitations": [],
        }
    total = data.get("total", len(data["results"]))
    return {
        "answer": f"The search found {total} matching metadata asset(s) across the catalog.",
        "summary": f"{total} search results.",
        "answer_type": AnswerType.SEARCH, "confidence": 70, "limitations": [],
    }


def _explain_cross_source_summary(package) -> dict:
    services = sorted({e.source_service for e in _items_with_data(package)})
    return {
        "answer": (
            f"This response combines evidence from {len(services)} enterprise service(s) "
            f"({', '.join(services)}) into a single holistic summary."
        ),
        "summary": f"Combined evidence from {len(services)} services.",
        "answer_type": AnswerType.CROSS_SOURCE_SUMMARY, "confidence": 60, "limitations": [],
    }


def _explain_unknown(package) -> dict:
    return {
        "answer": (
            "This request could not be matched to a specific enterprise answer category. "
            "Try rephrasing with terms like 'dictionary', 'profiling', 'governance', or 'relationships'."
        ),
        "summary": "Intent not recognized.",
        "answer_type": AnswerType.UNKNOWN, "confidence": 10,
        "limitations": ["Could not determine a confident answer category."],
    }


_INTENT_DISPATCH = {
    IntentType.DICTIONARY: _explain_dictionary,
    IntentType.DOMAIN: _explain_domain,
    IntentType.ENTITY: _explain_entity,
    IntentType.PROFILING: _explain_profiling,
    IntentType.GOVERNANCE: _explain_governance,
    IntentType.RELATIONSHIP: _explain_relationship,
    IntentType.KNOWLEDGE_GRAPH: _explain_knowledge_graph,
    IntentType.REVIEW: _explain_review,
    IntentType.METADATA_LOOKUP: _explain_metadata,
    IntentType.REPORTS: _explain_reports,
}


def build_explanation(strategy, package) -> dict:
    """Never invents a fact absent from package.evidence; explicitly states
    missing/partial evidence; short-circuits to RESTRICTED under governance."""
    if not package.evidence:
        return _no_evidence_result(AnswerType.UNKNOWN)

    if _is_governance_restricted(package):
        return _restricted_result()

    builder = _INTENT_DISPATCH.get(package.intent.intent_type)
    if builder is not None:
        return builder(package)

    # No specific intent match — probe by evidence content, most specific first.
    if _find(package, "live_query") is not None:
        return _explain_live_query(package)
    if _find(package, "live_metadata") is not None:
        return _explain_metadata(package)
    if _find(package, "search") is not None and len(_items_with_data(package)) == 1:
        return _explain_search(package)
    if len(_items_with_data(package)) >= 3:
        return _explain_cross_source_summary(package)
    return _explain_unknown(package)
