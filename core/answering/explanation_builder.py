from __future__ import annotations

import re

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


def _humanize_table_label(table_fqn: str) -> str:
    short = table_fqn.split(".")[-1]
    words = re.split(r"[_\s]+", short)
    return " ".join(w.capitalize() for w in words if w)


# Accuracy Program A4, Fix #2/#3 — entity/domain-assignment names carry these
# two sentinel values (see data/entity_service.py) for tables that were never
# meaningfully classified; neither is a real business name worth showing, for
# a label OR as the standalone domain/entity metadata fields (Fix #3).
_UNUSABLE_METADATA_NAMES = {"unknown", "generic"}


def _clean_display_text(value) -> str | None:
    """Trim/collapse whitespace for display; None/blank stays None so the
    next precedence tier is tried. Never alters case — metadata is trusted
    as-authored."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or None


def _usable_metadata_name(value) -> str | None:
    """_clean_display_text, plus dropping the Unknown/Generic sentinels a
    never-classified table's domain/entity assignment carries."""
    cleaned = _clean_display_text(value)
    if cleaned and cleaned.lower() in _UNUSABLE_METADATA_NAMES:
        return None
    return cleaned


def _clarification_label(table_fqn: str, candidate: dict) -> str:
    """Display-only label precedence (does not affect score/order/selection):
    1. dictionary business_name (table-level)
    2. entity-assignment name, excluding Unknown/Generic
    3. existing candidate business_label (column-level dictionary text)
    4. humanized table/view name
    5. raw table_fqn, as a final technical fallback
    """
    dictionary_name = _clean_display_text(candidate.get("table_business_name"))
    if dictionary_name:
        return dictionary_name

    entity_name = _usable_metadata_name(candidate.get("entity_name"))
    if entity_name:
        return entity_name

    business_label = _clean_display_text(candidate.get("business_label"))
    if business_label:
        return business_label

    humanized = _clean_display_text(_humanize_table_label(table_fqn))
    if humanized:
        return humanized

    return table_fqn


def _term_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _text_matches_term(term: str, text: str | None) -> bool:
    term_tokens = _term_tokens(term)
    if not term_tokens or not text:
        return False
    return bool(term_tokens & _term_tokens(text))


def _clarification_why_relevant(term: str, table_fqn: str, candidate: dict) -> list[str]:
    """Accuracy Program A4, Fix #3 — deterministic, capped-at-3 factual
    reasons built only from evidence already on the candidate (never from
    score/confidence/ranking internals, never invented). Fixed priority
    order: business_name match -> table name match -> entity match ->
    domain present -> matching column -> related-table connections. Pure
    function of its inputs, so identical candidates always produce identical
    reasons in the same order.
    """
    reasons: list[str] = []

    business_name = _clean_display_text(candidate.get("table_business_name"))
    if business_name and _text_matches_term(term, business_name):
        reasons.append("Business name matches the requested term.")

    if len(reasons) < 3:
        table_name = table_fqn.split(".")[-1]
        if _text_matches_term(term, table_name):
            reasons.append(f"Name matches '{term}'.")

    if len(reasons) < 3:
        entity_name = _usable_metadata_name(candidate.get("entity_name"))
        if entity_name and _text_matches_term(term, entity_name):
            reasons.append(f"Entity assignment matches '{term}'.")

    if len(reasons) < 3:
        domain_name = _usable_metadata_name(candidate.get("domain_name"))
        if domain_name:
            reasons.append(f"Assigned to the {domain_name} domain.")

    if len(reasons) < 3:
        column_name = candidate.get("column_name")
        if column_name and _text_matches_term(term, column_name):
            reasons.append(f"Contains {term}-related columns.")

    if len(reasons) < 3:
        related = [t for t in (candidate.get("relationship_related_tables") or []) if t]
        names = [n for n in (_humanize_table_label(t) for t in related[:2]) if n]
        if names:
            reasons.append(f"Connected to {' and '.join(names)} tables.")

    return reasons[:3]


def _clarification_option(index: int, term: str, candidate: dict) -> dict:
    table_fqn = candidate.get("table_fqn") or ""
    label = _clarification_label(table_fqn, candidate)
    score = candidate.get("score")
    column_name = candidate.get("column_name")
    description = (
        f"Uses the '{column_name}' column for {term}." if column_name
        else f"A business table matching '{term}'."
    )
    return {
        "id": f"opt_{index}",
        "term": term,
        "table_fqn": table_fqn,
        "column_name": column_name,
        "label": label,
        "description": description,
        "score": score,
        # Accuracy Program A4, Fix #3 — additive, display/evidence-only.
        "object_type": _clean_display_text(candidate.get("table_object_type")),
        "business_name": _clean_display_text(candidate.get("table_business_name")),
        "domain": _usable_metadata_name(candidate.get("domain_name")),
        "entity": _usable_metadata_name(candidate.get("entity_name")),
        "why_relevant": _clarification_why_relevant(term, table_fqn, candidate),
    }


def _explain_clarification(data: dict) -> dict:
    ambiguous_terms = data.get("ambiguous_terms") or []
    terms = sorted({t.get("term") for t in ambiguous_terms if t.get("term")})

    # Phase 2 — a term with only one candidate (query_planning_service found
    # a match but it didn't clear the auto-select confidence threshold on
    # its own) is a single weak guess, not a tie, and must not be described
    # as "multiple concepts match with similar confidence" — that phrasing
    # is only true when a group actually has >=2 candidates (data["reason"]
    # == "low_confidence_match" means every group here is untied; see
    # context_builder._on_plan_resolved).
    if data.get("reason") == "low_confidence_match":
        reason = (
            f"I couldn't confidently match '{terms[0]}' to a specific table or column."
            if len(terms) == 1 else
            "I couldn't confidently match some terms in this question to a specific table or column: "
            + ", ".join(f"'{t}'" for t in terms) + "."
        )
    else:
        reason = (
            f"I found multiple business concepts that match '{terms[0]}' with similar confidence."
            if len(terms) == 1 else
            "Multiple terms in this question have more than one possible business match: "
            + ", ".join(f"'{t}'" for t in terms) + "."
        )

    options: list[dict] = []
    lines: list[str] = []
    idx = 1
    # Rule C — the same database object (table, or table+column) must never
    # be shown twice, even when it legitimately scores as a candidate for
    # more than one ambiguous term (e.g. a well-connected junction table
    # surfacing under two different bogus terms). Keyed on table_fqn +
    # column_name (None for a table-level concept candidate) — the same
    # "canonical entity id" the option itself is built from — never on the
    # display label, which can coincidentally collide for unrelated objects.
    seen_objects: set[tuple[str, str | None]] = set()
    for group in ambiguous_terms:
        term = group.get("term")
        candidates = group.get("candidates") or []
        group_lines: list[str] = []
        for candidate in candidates:
            dedup_key = (candidate.get("table_fqn") or "", candidate.get("column_name"))
            if dedup_key in seen_objects:
                continue
            seen_objects.add(dedup_key)
            option = _clarification_option(idx, term, candidate)
            options.append(option)
            group_lines.append(f"{idx}. {option['label']}")
            idx += 1
        if group_lines:
            lines.append(f"For '{term}':" if group.get("tied", True) else f"Closest match for '{term}':")
            lines.extend(group_lines)

    answer = reason + " " + " ".join(lines) + " Which would you like to use?"
    return {
        "answer": answer.strip(),
        "summary": "Clarification needed before this question can be answered.",
        "answer_type": AnswerType.CLARIFICATION_NEEDED,
        "confidence": 0,
        "limitations": ["No SQL has been generated or executed pending your selection."],
        "clarification": {
            "reason": reason,
            "options": options,
            "expected_impact": (
                "Selecting a different option resolves the question against a different underlying "
                "table or column and may change the result."
            ),
        },
    }


def _explain_current_state_clarification(data: dict) -> dict:
    """CURRENT-STATE MILESTONE, Part 6 — schema-grounded clarification for a
    "currently X" question whose resolved plan has more than one materially
    different current-state definition available in the source data (Part
    5: GENUINE_BUSINESS_AMBIGUITY, never silently picked). Distinct wording
    and option shape from _explain_clarification's table/column-ambiguity
    phrasing — a current-state pick is a WHERE-filter choice, not a
    table/column choice, and reuses that phrasing would misdescribe it as
    "multiple business concepts match" when the table/column is not in
    question at all."""
    candidates = data.get("current_state_candidates") or []
    lines = [f"{i}. {c.get('label')}" for i, c in enumerate(candidates, start=1)]
    options = [
        {
            "id": c.get("id") or f"cs_{i}", "kind": "current_state",
            "table_fqn": c.get("table_fqn"), "column_name": c.get("column_name"),
            "operator": c.get("operator"), "value": c.get("value"), "label": c.get("label"),
        }
        for i, c in enumerate(candidates, start=1)
    ]
    reason = (
        "This question asks for the current state, and the source data supports more than "
        "one definition of that."
    )
    answer = reason + " Do you mean:\n" + "\n".join(lines) + "\nWhich would you like to use?"
    return {
        "answer": answer.strip(),
        "summary": "Clarification needed: which current-state definition should be used?",
        "answer_type": AnswerType.CLARIFICATION_NEEDED,
        "confidence": 0,
        "limitations": ["No SQL has been generated or executed pending your selection."],
        "clarification": {
            "reason": reason,
            "options": options,
            "expected_impact": (
                "Selecting a different option changes which rows count as \"current\" and may "
                "change the result."
            ),
        },
    }


def _explain_live_query(package) -> dict:
    item = _find(package, "live_query")
    data = item.data if item and isinstance(item.data, dict) else None
    if not data:
        return _no_evidence_result(AnswerType.LIVE_QUERY)

    # Milestone Phase 6.6 — Enterprise Clarification Intelligence. Ambiguity
    # detected by query_planning_service's own ranking (context_builder's
    # _live_query short-circuit) is surfaced as a clarification question,
    # never as a silent refusal or a best guess. Phase 2 adds
    # "low_confidence_match" (a lone below-threshold candidate rather than a
    # genuine tie) — same CLARIFICATION_NEEDED answer type and resume
    # machinery, distinct wording only (see _explain_clarification).
    if data.get("reason") in ("clarification_required", "low_confidence_match"):
        return _explain_clarification(data)

    # CURRENT-STATE MILESTONE, Part 3/6 — see _explain_current_state_
    # clarification's own docstring for why this is a distinct branch from
    # the table/column ambiguity handled above.
    if data.get("reason") == "current_state_ambiguous":
        return _explain_current_state_clarification(data)
    if data.get("reason") == "source_data_unusable":
        # CURRENT-STATE MILESTONE follow-up, Part 8 — deterministic,
        # business-worded, table-free message; the physical table name
        # (data["table_fqn"]) stays out of the answer, developer-diagnostics
        # only (available via the raw evidence dict, never surfaced here).
        return {
            "answer": data.get("business_message") or (
                "Reliable analysis is not available from the current source for this request."
            ),
            "summary": "This entity's source data is not populated.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 10,
            "limitations": [data.get("business_message") or "Source data is not populated for this entity."],
            "technical_diagnostics": [{
                "issue_type": "ZERO_ROW_SOURCE_TABLE", "severity": "HIGH",
                "table_fqn": data.get("table_fqn"), "column_name": None,
                "business_concept": data.get("unusable_term"), "evidence": f"{data.get('table_fqn')} profiled at 0 rows",
                "affected_rows": None, "message": data.get("business_message"),
            }],
        }
    if data.get("reason") == "current_state_signal_not_found":
        return {
            "answer": (
                "This question asks for the current state, but the source data has no column "
                "that reliably indicates that state for this entity."
            ),
            "summary": "No current-state indicator is available in the source data.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 10,
            "limitations": [
                "No cancellation, end-date, or active-status column was found for this request.",
            ],
        }

    # Day 2E, Task 5 — a confirmed-unsupported entity (data.query_planning_
    # service._build_unsupported_entity_plan) gets its OWN business-worded,
    # table-free answer text — never the generic "This question could not
    # be translated..." branch below, whose `explanation`/`warnings`
    # messages carry raw discovery evidence (candidate table names, scores)
    # that must never reach a business user. `business_messages` is that
    # answer text, one plain-English sentence per unsupported entity; the
    # technical detail (entity name, original resolution_status, raw
    # evidence) stays in `limitations` for developer diagnostics only —
    # never physical table names shown as the primary answer.
    if data.get("reason") == "unsupported_entity":
        business_messages = list(data.get("business_messages") or [])
        answer = " ".join(business_messages) or "I could not find a reliable, verified data source for this request."
        diagnostics = [
            w.get("message") for w in (data.get("warnings") or [])
            if isinstance(w, dict) and w.get("message")
        ]
        return {
            "answer": answer,
            "summary": "No verified data source could answer this question.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 10,
            "limitations": diagnostics or answer,
        }

    # SQL generation was refused before any execution was attempted — a
    # distinct outcome from a QueryResult that executed and failed/blocked.
    # This dict has no "status" key at all (see context_builder._live_query),
    # so falling through to the status check below rendered "status: None".
    if data.get("executed") is False or data.get("reason") == "sql_generation_refused":
        reasons = list(data.get("explanation") or [])
        warning_messages = [
            w.get("message") for w in (data.get("warnings") or [])
            if isinstance(w, dict) and w.get("message")
        ]
        details = reasons or warning_messages
        answer = "This question could not be translated into a SQL query."
        if details:
            answer += " " + " ".join(details)
        return {
            "answer": answer.strip(),
            "summary": "SQL could not be generated for this question.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 10,
            "limitations": warning_messages or reasons or ["No SQL was generated or executed."],
        }

    status = data.get("status")
    if status != "success":
        return {
            "answer": f"The live query did not complete successfully (status: {status}). {data.get('error') or ''}".strip(),
            "summary": f"Live query status: {status}.",
            "answer_type": AnswerType.LIVE_QUERY, "confidence": 20,
            "limitations": [data.get("error")] if data.get("error") else [],
        }

    # Milestone M-25 — Enterprise Answer Value Rendering. Deterministic,
    # template-based business language built entirely from evidence already
    # present on `data` (business_plan + rows) — never invents a fact.
    from core.answering.result_formatter import build_business_answer
    business_answer = build_business_answer(data)
    return {
        **business_answer,
        "answer_type": AnswerType.LIVE_QUERY,
        "confidence": _compute_live_query_confidence(package, data),
        # NEXT MILESTONE, Part 9 — pass-through only. core.orchestrator.agent
        # already computed these (bounded, question-scoped
        # detect_data_quality_observations pass) and attached them to `data`
        # before this function ever ran; never recomputed here.
        "data_quality_notes": data.get("data_quality_notes") or [],
        "technical_diagnostics": data.get("technical_diagnostics") or [],
    }


def _compute_live_query_confidence(package, data: dict) -> int:
    """
    Rule G — final answer confidence must reflect critical stage confidence,
    never a flat constant. Blends two independently-computed, real scores:
      - intent_pct: how confident IntentResolver was that this question was
        even a SQL_REQUEST in the first place (package.intent.confidence,
        0-1 — see agent.build_live_query_package's intent_confidence param).
      - plan_pct: how confident query_planning_service was in the specific
        table/column resolution actually used to answer it — the bare-
        concept entity_confidence when this was a list_entities/
        joined_detail_list question (no measure/dimension score to average
        otherwise), falling back to plan_confidence (query_plan["confidence"],
        already a measure/dimension/join/warning blend) for every other
        shape.
    A weak intent match (e.g. 20%) can therefore never be dressed up as a
    95%-confident answer just because the resulting SQL happened to execute
    successfully — execution success alone was the old, sole signal.
    """
    business_plan = data.get("business_plan") or {}
    intent_pct = round((package.intent.confidence or 0.0) * 100)

    entity_confidence = business_plan.get("entity_confidence")
    plan_confidence = business_plan.get("plan_confidence")
    if entity_confidence is not None:
        plan_pct = round(entity_confidence * 100)
    elif plan_confidence is not None:
        plan_pct = round(plan_confidence)
    else:
        plan_pct = intent_pct  # no plan-level signal at all — don't invent one

    return max(0, min(100, round(intent_pct * 0.4 + plan_pct * 0.6)))


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
