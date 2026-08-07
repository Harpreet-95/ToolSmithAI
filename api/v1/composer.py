from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from auth.jwt_auth import AuthenticatedUser, require_jwt
from core.errors.error_response import build_error_response
from core.orchestrator import (
    EnterpriseOrchestrator,
    IntentType,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

composer_router = APIRouter(tags=["composer"])


# ---------------------------------------------------------------------------
# Request Model
# ---------------------------------------------------------------------------

class ComposerRequest(BaseModel):
    workspace_id: Optional[str] = None
    session_id: str
    message: str
    selected_data_source: Optional[int] = None
    selected_dataset: Optional[str] = None
    selected_table: Optional[str] = None
    dataset_id: Optional[int] = None
    conversation_context: Optional[List[Dict[str, Any]]] = None
    request_options: Optional[Dict[str, Any]] = None
    clarification_selection: Optional[List[Dict[str, Any]]] = None
    cancel_clarification: bool = False
    # Milestone M-31 — round-tripped core.orchestrator.agent.ConversationContext
    # .to_dict(), returned as this same field's name on the previous agent
    # response's "conversation_state" key. Deliberately a new field, not a
    # reinterpretation of conversation_context above (that field's existing
    # List[Dict] shape/contract is untouched for back-compat).
    conversation_state: Optional[Dict[str, Any]] = None

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message cannot be empty")
        return v.strip()

    @field_validator("session_id")
    @classmethod
    def session_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("session_id cannot be empty")
        return v.strip()


# ---------------------------------------------------------------------------
# Serialization helpers — convert dataclass instances to JSON-safe dicts
# ---------------------------------------------------------------------------

def _serialize_evidence_item(item) -> dict:
    return {
        "evidence_id": item.evidence_id,
        "source_service": item.source_service,
        "source_function": item.source_function,
        "capability": item.capability.value,
        "data": item.data,
        "timestamp": item.timestamp.isoformat(),
        "confidence": item.confidence,
        "governance_state": item.governance_state,
        "error": item.error,
        "metadata": item.metadata,
    }


def _serialize_service_call(call) -> dict:
    return {
        "service_id": call.service_id,
        "function_name": call.function_name,
        "called_at": call.called_at.isoformat(),
        "duration_ms": call.duration_ms,
        "succeeded": call.succeeded,
        "error": call.error,
    }


def _serialize_package(package) -> dict:
    return {
        "request_id": package.request_id,
        "query": package.query,
        "built_at": package.built_at.isoformat(),
        "source_id": package.source_id,
        "total_evidence_items": package.total_evidence_items,
        "services_attempted": package.services_attempted,
        "services_succeeded": package.services_succeeded,
        "evidence": [_serialize_evidence_item(e) for e in package.evidence],
        "service_calls": [_serialize_service_call(c) for c in package.service_calls],
        "errors": package.errors,
    }


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _extract_warnings(package) -> List[str]:
    warnings: List[str] = []
    if package.intent.intent_type == IntentType.UNKNOWN:
        warnings.append(
            "Intent could not be resolved — defaulting to metadata search"
        )
    if (
        package.services_attempted > 0
        and package.services_succeeded < package.services_attempted
    ):
        failed = package.services_attempted - package.services_succeeded
        warnings.append(
            f"{failed} of {package.services_attempted} services returned errors"
        )
    for item in package.evidence:
        if item.data is None:
            warnings.append(
                f"Service '{item.source_service}' returned no data"
                " (missing required context or parameters)"
            )
    return warnings


def _determine_status(package) -> str:
    if package.services_attempted == 0:
        return "partial"
    if package.services_succeeded == 0:
        return "failed"
    if package.errors or package.intent.intent_type == IntentType.UNKNOWN:
        return "partial"
    return "success"


def _extract_governance_state(package) -> Optional[str]:
    for item in package.evidence:
        if item.governance_state:
            return item.governance_state
    return None


# ---------------------------------------------------------------------------
# Deterministic answer generator — no AI, no SQL, no writes
# ---------------------------------------------------------------------------

def _is_governance_restricted(governance_state: Optional[str]) -> bool:
    return bool(governance_state and "restricted" in governance_state.lower())


def _partial_flag(items_with_data: list, all_evidence: list) -> bool:
    return len(items_with_data) < len(all_evidence)


def _answer_no_data(reason: str) -> dict:
    return {
        "answer": f"No answer available. {reason}",
        "answer_type": "no_data",
        "source_summary": "No services returned usable data.",
        "confidence": 0.0,
        "limitations": [reason],
        "next_suggested_action": "Select a data source and re-submit the query.",
    }


def _answer_restricted() -> dict:
    return {
        "answer": (
            "Access to this data is restricted by governance policy. "
            "Contact your data steward to request access."
        ),
        "answer_type": "access_restricted",
        "source_summary": "Governance restriction detected.",
        "confidence": 1.0,
        "limitations": [
            "Data access is restricted — sensitive or PII fields may be governed."
        ],
        "next_suggested_action": (
            "Contact your data steward or review the Governance Command Center."
        ),
    }


def _answer_unknown() -> dict:
    return {
        "answer": (
            "The query intent could not be determined. Please rephrase your question "
            "using terms like 'dictionary', 'domain', 'profiling', 'governance', or 'relationships'."
        ),
        "answer_type": "unknown_intent",
        "source_summary": "Intent classification returned unknown.",
        "confidence": 0.0,
        "limitations": ["Query did not match any known catalog intent."],
        "next_suggested_action": (
            "Rephrase your query with specific catalog keywords "
            "(e.g. 'show dictionary', 'domain assignments', 'PII governance')."
        ),
    }


def _answer_dictionary(items_with_data: list, all_evidence: list) -> dict:
    dict_items = [e for e in items_with_data if e.source_service == "dictionary"]

    if not dict_items:
        return {
            "answer": (
                "No dictionary definitions found. "
                "The data dictionary may not have been generated for this data source."
            ),
            "answer_type": "dictionary_status",
            "source_summary": "Dictionary service returned no data.",
            "confidence": 0.0,
            "limitations": [
                "No dictionary evidence collected — data source may not be selected "
                "or dictionary not yet generated.",
                *(
                    ["Partial evidence only — some services returned no data."]
                    if _partial_flag(items_with_data, all_evidence) else []
                ),
            ],
            "next_suggested_action": (
                "Navigate to the Data Dictionary to generate definitions for this data source."
            ),
        }

    data = dict_items[0].data
    if not data:  # empty list
        return {
            "answer": (
                "The dictionary service responded but no table definitions were found. "
                "Generate dictionary entries first."
            ),
            "answer_type": "dictionary_status",
            "source_summary": "Dictionary service returned an empty list.",
            "confidence": 0.5,
            "limitations": ["Dictionary exists but contains no table entries yet."],
            "next_suggested_action": (
                "Navigate to the Data Dictionary to generate table definitions."
            ),
        }

    total     = len(data)
    with_desc = sum(1 for t in data if t.get("description") or t.get("business_name"))
    weak      = total - with_desc

    parts = [f"The data dictionary contains {total} table definition(s)."]
    if with_desc:
        parts.append(f"{with_desc} table(s) have descriptions or business names.")
    if weak:
        parts.append(f"{weak} table(s) lack descriptions and may need review.")

    limitations: List[str] = []
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")
    if weak:
        limitations.append(f"{weak} table(s) have incomplete definitions.")
    if not limitations:
        limitations.append(
            "Answer is based on catalog metadata only — verify in the Data Dictionary for full detail."
        )

    return {
        "answer": " ".join(parts),
        "answer_type": "dictionary_status",
        "source_summary": f"Dictionary service: {total} tables scanned.",
        "confidence": dict_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Open the Data Dictionary to review and approve table definitions."
            if weak else
            "Dictionary entries are available — navigate to Data Dictionary for full detail."
        ),
    }


def _answer_domain(items_with_data: list, all_evidence: list) -> dict:
    domain_items = [e for e in items_with_data if e.source_service == "domain"]

    if not domain_items:
        return {
            "answer": (
                "No domain assignment data found. "
                "Select a data source or generate domain assignments first."
            ),
            "answer_type": "domain_assignments",
            "source_summary": "Domain service returned no data.",
            "confidence": 0.0,
            "limitations": [
                "No domain evidence collected — data source may not be selected."
            ],
            "next_suggested_action": (
                "Navigate to Domain Assignments and generate assignments for this data source."
            ),
        }

    data = domain_items[0].data
    if not isinstance(data, dict):
        return {
            "answer": "Domain assignment data is unavailable or in an unexpected format.",
            "answer_type": "domain_assignments",
            "source_summary": "Domain service returned unexpected data format.",
            "confidence": 0.0,
            "limitations": ["Domain data format unrecognised."],
            "next_suggested_action": "Navigate to Domain Assignments to review current state.",
        }

    total         = data.get("tables_total", 0)
    assigned      = data.get("tables_assigned", 0)
    unknown       = data.get("tables_unknown", 0)
    domain_counts = data.get("domain_counts", {})

    if total == 0:
        return {
            "answer": "No domain assignments have been generated for this data source yet.",
            "answer_type": "domain_assignments",
            "source_summary": "Domain service: no assignments found.",
            "confidence": 0.5,
            "limitations": ["Domain assignments not yet generated for this source."],
            "next_suggested_action": (
                "Navigate to Domain Assignments to generate domain classifications."
            ),
        }

    top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_str     = ", ".join(f"{d} ({c})" for d, c in top_domains if d != "Unknown")

    parts = [f"{assigned} of {total} table(s) have domain assignments."]
    if top_str:
        parts.append(f"Top domains: {top_str}.")
    if unknown:
        parts.append(f"{unknown} table(s) remain unclassified.")

    limitations: List[str] = []
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")
    if unknown:
        limitations.append(f"{unknown} table(s) are unclassified ('Unknown' domain).")
    if not limitations:
        limitations.append(
            "Domain counts are summary-level only — navigate to Domain Assignments for table-level detail."
        )

    return {
        "answer": " ".join(parts),
        "answer_type": "domain_assignments",
        "source_summary": f"Domain service: {total} tables, {assigned} assigned.",
        "confidence": domain_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Navigate to Domain Assignments to review and approve domain classifications."
            if unknown else
            "Domain assignments are complete — navigate to Domain Assignments for full detail."
        ),
    }


def _answer_entity(items_with_data: list, all_evidence: list) -> dict:
    entity_items = [e for e in items_with_data if e.source_service == "entity"]

    if not entity_items:
        return {
            "answer": (
                "No entity assignment data found. "
                "Select a data source or generate entity assignments first."
            ),
            "answer_type": "entity_assignments",
            "source_summary": "Entity service returned no data.",
            "confidence": 0.0,
            "limitations": [
                "No entity evidence collected — data source may not be selected."
            ],
            "next_suggested_action": (
                "Navigate to Entity Assignments and generate assignments for this data source."
            ),
        }

    data = entity_items[0].data
    if not isinstance(data, dict):
        return {
            "answer": "Entity assignment data is unavailable or in an unexpected format.",
            "answer_type": "entity_assignments",
            "source_summary": "Entity service returned unexpected data format.",
            "confidence": 0.0,
            "limitations": ["Entity data format unrecognised."],
            "next_suggested_action": "Navigate to Entity Assignments to review current state.",
        }

    total         = data.get("tables_total", 0)
    assigned      = data.get("entities_assigned", 0)
    unknown       = data.get("entities_unknown", 0)
    entity_counts = data.get("entity_counts", {})

    if total == 0:
        return {
            "answer": "No entity assignments have been generated for this data source yet.",
            "answer_type": "entity_assignments",
            "source_summary": "Entity service: no assignments found.",
            "confidence": 0.5,
            "limitations": ["Entity assignments not yet generated for this source."],
            "next_suggested_action": (
                "Navigate to Entity Assignments to generate entity mappings."
            ),
        }

    top_entities = sorted(entity_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_str      = ", ".join(f"{e} ({c})" for e, c in top_entities if e != "Unknown")

    parts = [f"{assigned} of {total} table(s) have entity assignments."]
    if top_str:
        parts.append(f"Top entities: {top_str}.")
    if unknown:
        parts.append(f"{unknown} table(s) remain unmapped.")

    limitations: List[str] = []
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")
    if unknown:
        limitations.append(f"{unknown} table(s) are unmapped ('Unknown' entity).")
    if not limitations:
        limitations.append(
            "Entity counts are summary-level only — navigate to Entity Assignments for table-level detail."
        )

    return {
        "answer": " ".join(parts),
        "answer_type": "entity_assignments",
        "source_summary": f"Entity service: {total} tables, {assigned} assigned.",
        "confidence": entity_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Navigate to Entity Assignments to review and approve entity mappings."
            if unknown else
            "Entity assignments are complete — navigate to Entity Assignments for full detail."
        ),
    }


def _answer_profiling(items_with_data: list, all_evidence: list) -> dict:
    profiling_items = [e for e in items_with_data if e.source_service == "profiling"]

    if not profiling_items:
        return {
            "answer": (
                "No profiling data found. "
                "Select a data source or run a profiling job first."
            ),
            "answer_type": "profiling_status",
            "source_summary": "Profiling service returned no data.",
            "confidence": 0.0,
            "limitations": [
                "No profiling evidence collected — data source may not be selected "
                "or no profiling job has been run."
            ],
            "next_suggested_action": (
                "Navigate to the Profiling Job Center to run a profiling job for this data source."
            ),
        }

    data = profiling_items[0].data
    if not isinstance(data, dict):
        return {
            "answer": (
                "No profiling snapshot is available for this data source. "
                "Run a profiling job to generate quality statistics."
            ),
            "answer_type": "profiling_status",
            "source_summary": "Profiling service: no snapshot found.",
            "confidence": 0.5,
            "limitations": ["No profiling snapshot found for this data source."],
            "next_suggested_action": (
                "Navigate to the Profiling Job Center to run a profiling job."
            ),
        }

    snapshot     = data.get("snapshot") or {}
    tables       = data.get("tables") or []
    total_tables = len(tables)
    pii_tables   = sum(1 for t in tables if (t.get("pii_column_count") or 0) > 0)
    # PII column counts only — never expose column names or values
    confirmed_pii = sum(t.get("confirmed_pii_count", 0) or 0 for t in tables)

    parts = [f"Profiling snapshot covers {total_tables} table(s)."]
    if pii_tables:
        parts.append(
            f"{pii_tables} table(s) contain potential PII "
            f"({confirmed_pii} confirmed PII column(s))."
        )
    if snapshot.get("profiling_status"):
        parts.append(f"Profiling status: {snapshot['profiling_status']}.")

    limitations: List[str] = [
        "PII column names are not exposed — navigate to Profiling detail for column-level information."
    ]
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")

    return {
        "answer": " ".join(parts),
        "answer_type": "profiling_status",
        "source_summary": f"Profiling service: {total_tables} tables profiled.",
        "confidence": profiling_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Navigate to the Column Profile Explorer to review column-level "
            "quality metrics and PII classifications."
        ),
    }


def _answer_governance(items_with_data: list, all_evidence: list) -> dict:
    gov_items = [e for e in items_with_data if e.source_service == "governance"]

    if not gov_items:
        return {
            "answer": (
                "No governance data found. "
                "Governance may not have been configured for this data source."
            ),
            "answer_type": "governance",
            "source_summary": "Governance service returned no data.",
            "confidence": 0.0,
            "limitations": ["No governance evidence collected."],
            "next_suggested_action": (
                "Navigate to the Governance Command Center to configure governance policies."
            ),
        }

    data = gov_items[0].data
    if not isinstance(data, dict):
        return {
            "answer": "Governance data is unavailable or in an unexpected format.",
            "answer_type": "governance",
            "source_summary": "Governance service returned unexpected data format.",
            "confidence": 0.0,
            "limitations": ["Governance data format unrecognised."],
            "next_suggested_action": (
                "Navigate to the Governance Command Center to review governance state."
            ),
        }

    score            = data.get("governance_score", 0)
    total_governed   = data.get("total_governed", 0)
    ready            = data.get("objects_ready", 0)
    pending          = data.get("objects_pending", 0)
    escalated        = data.get("objects_escalated", 0)
    open_assignments = data.get("open_assignments", 0)
    high_risk_pct    = data.get("high_risk_pct", 0.0)

    parts = [f"Governance score: {score}/100."]
    if total_governed > 0:
        parts.append(f"{ready} of {total_governed} governed objects are approved.")
    if pending:
        parts.append(f"{pending} object(s) are pending review.")
    if escalated:
        parts.append(f"{escalated} object(s) require escalated review.")
    if open_assignments:
        parts.append(f"{open_assignments} open governance assignment(s) for this source.")

    limitations: List[str] = []
    if high_risk_pct:
        limitations.append(
            f"{high_risk_pct}% of governed objects are high-risk "
            "(low confidence or escalated)."
        )
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")
    if not limitations:
        limitations.append(
            "Governance counts are summary-level — navigate to Governance Command Center "
            "for object-level detail."
        )

    return {
        "answer": " ".join(parts),
        "answer_type": "governance",
        "source_summary": f"Governance service: score={score}, {total_governed} governed objects.",
        "confidence": gov_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Navigate to the Governance Command Center to review pending approvals and escalations."
            if (pending or escalated) else
            "Navigate to the Governance Command Center for full compliance detail."
        ),
    }


def _answer_relationship(items_with_data: list, all_evidence: list) -> dict:
    rel_items = [e for e in items_with_data if e.source_service == "relationship"]

    if not rel_items:
        return {
            "answer": (
                "No relationship data found. "
                "Select a data source or run a profiling job to discover relationships."
            ),
            "answer_type": "relationship_map",
            "source_summary": "Relationship service returned no data.",
            "confidence": 0.0,
            "limitations": [
                "No relationship evidence collected — data source may not be selected "
                "or profiling not yet run."
            ],
            "next_suggested_action": (
                "Run a profiling job to discover table relationships, "
                "then return to view the relationship map."
            ),
        }

    data = rel_items[0].data
    if not isinstance(data, dict):
        return {
            "answer": "Relationship data is unavailable or in an unexpected format.",
            "answer_type": "relationship_map",
            "source_summary": "Relationship service returned unexpected data format.",
            "confidence": 0.0,
            "limitations": ["Relationship data format unrecognised."],
            "next_suggested_action": "Navigate to the Knowledge Graph to explore relationships.",
        }

    total          = data.get("total_relationships", 0)
    outbound       = data.get("tables_with_outbound_fks", 0)
    referenced     = data.get("tables_referenced_by_fk", 0)
    most_referenced = data.get("most_referenced", [])

    if total == 0:
        return {
            "answer": (
                "No relationships (foreign keys) have been discovered for this data source. "
                "Run or update profiling to detect them."
            ),
            "answer_type": "relationship_map",
            "source_summary": "Relationship service: no relationships found.",
            "confidence": 0.5,
            "limitations": [
                "No relationships discovered — profiling may not have been run on this source."
            ],
            "next_suggested_action": (
                "Run a profiling job and then check the Knowledge Graph for relationship discovery."
            ),
        }

    parts = [f"{total} relationship(s) discovered."]
    parts.append(
        f"{outbound} table(s) have outbound foreign keys. "
        f"{referenced} table(s) are referenced."
    )
    if most_referenced:
        top = most_referenced[0]
        parts.append(
            f"Most referenced table: {top.get('table_fqn', 'unknown')} "
            f"({top.get('inbound_count', 0)} inbound relationship(s))."
        )

    limitations: List[str] = []
    if _partial_flag(items_with_data, all_evidence):
        limitations.append("Partial evidence — some services returned no data.")
    limitations.append(
        "Relationship data is summary-level — navigate to the Knowledge Graph for full join paths."
    )

    return {
        "answer": " ".join(parts),
        "answer_type": "relationship_map",
        "source_summary": f"Relationship service: {total} relationships across {outbound} source tables.",
        "confidence": rel_items[0].confidence,
        "limitations": limitations,
        "next_suggested_action": (
            "Navigate to the Knowledge Graph to explore full relationship paths and join conditions."
        ),
    }


def _answer_review(items_with_data: list, all_evidence: list) -> dict:
    gov_items  = [e for e in items_with_data if e.source_service == "governance"]
    dict_items = [e for e in items_with_data if e.source_service == "dictionary"]

    pending_total = 0
    parts: List[str] = []

    if gov_items and isinstance(gov_items[0].data, dict):
        gdata = gov_items[0].data
        g_pending = gdata.get("objects_pending", 0) + gdata.get("objects_escalated", 0)
        pending_total += g_pending
        if gdata.get("objects_pending"):
            parts.append(f"{gdata['objects_pending']} governance object(s) pending review.")
        if gdata.get("objects_escalated"):
            parts.append(f"{gdata['objects_escalated']} governance object(s) escalated.")

    if dict_items and isinstance(dict_items[0].data, list):
        weak = sum(
            1 for t in dict_items[0].data
            if not (t.get("description") or t.get("business_name"))
        )
        if weak:
            parts.append(f"{weak} dictionary entry(ies) need descriptions.")
            pending_total += weak

    if not parts:
        if not items_with_data:
            return {
                "answer": "No review items found. Select a data source to check for pending reviews.",
                "answer_type": "review_needed",
                "source_summary": "No evidence collected.",
                "confidence": 0.0,
                "limitations": ["No evidence collected for review assessment."],
                "next_suggested_action": "Select a data source to assess pending reviews.",
            }
        return {
            "answer": "No pending review items found for this data source.",
            "answer_type": "review_needed",
            "source_summary": f"Checked {len(items_with_data)} service(s) — no pending reviews detected.",
            "confidence": 0.8,
            "limitations": ["Review assessment is based on catalog metadata only."],
            "next_suggested_action": (
                "Navigate to the Governance Command Center or Data Dictionary "
                "to confirm review status."
            ),
        }

    return {
        "answer": " ".join(parts) + f" Total pending: {pending_total} item(s).",
        "answer_type": "review_needed",
        "source_summary": f"Review assessment based on {len(items_with_data)} service(s).",
        "confidence": 0.8,
        "limitations": [
            "Review counts are summary-level — navigate to the relevant module for item-level detail."
        ],
        "next_suggested_action": (
            "Review pending items in the Governance Command Center and/or the Data Dictionary."
        ),
    }


def _answer_metadata(items_with_data: list, all_evidence: list) -> dict:
    if not items_with_data:
        return {
            "answer": (
                "No catalog metadata found. "
                "Select a data source and ensure profiling has been run."
            ),
            "answer_type": "metadata_lookup",
            "source_summary": "No services returned usable data.",
            "confidence": 0.0,
            "limitations": [
                "No evidence collected — data source may not be selected or services unavailable."
            ],
            "next_suggested_action": "Select a data source, run profiling, and retry the query.",
        }

    services = list({e.source_service for e in items_with_data})
    parts    = [
        f"Catalog metadata retrieved from {len(services)} service(s): {', '.join(sorted(services))}."
    ]

    limitations: List[str] = []
    if _partial_flag(items_with_data, all_evidence):
        diff = len(all_evidence) - len(items_with_data)
        limitations.append(f"Partial evidence — {diff} service(s) returned no data.")
    limitations.append(
        "Metadata is summary-level — use Enterprise Search for deeper catalog queries."
    )

    return {
        "answer": " ".join(parts) + " Use the evidence detail below to review specific catalog information.",
        "answer_type": "metadata_lookup",
        "source_summary": f"{len(services)} service(s) returned data.",
        "confidence": max((e.confidence for e in items_with_data), default=0.0),
        "limitations": limitations,
        "next_suggested_action": (
            "Use Enterprise Search to run deeper cross-source metadata queries."
        ),
    }


def _answer_report_generation(
    user_id: str,
    dataset_id: Optional[int],
    intent_text: str,
) -> dict:
    """
    Delegate report generation to the existing report pipeline.
    Reuses run_dataset_report_plan() — no duplicate logic, no AI narration,
    no live SQL. Existing dataset ownership and trust guards are preserved
    because run_dataset_report_plan() enforces them internally.
    """
    import uuid as _uuid
    from core.workflows.workflow_runner import run_dataset_report_plan as _run_report

    plan = {
        "plan_id": str(_uuid.uuid4()),
        "intent": intent_text,
        "task_type": "generate_dataset_report",
        "steps": [],
    }

    try:
        result = _run_report(plan, user_id=user_id, dataset_id=dataset_id)
    except Exception as exc:
        logger.error("report_generation delegation failed: %s", exc)
        return {
            "answer": "Report generation failed. Please ensure a dataset is uploaded and try again.",
            "answer_type": "report_generation_failed",
            "source_summary": "Report pipeline returned an error.",
            "confidence": 0.0,
            "limitations": [str(exc)],
            "next_suggested_action": "Upload a dataset and retry the report generation.",
            "report_id": None,
            "report_title": None,
        }

    error = result.get("dataset_report_error")
    if error:
        return {
            "answer": error,
            "answer_type": "report_generation_failed",
            "source_summary": "Report pipeline returned an error.",
            "confidence": 0.0,
            "limitations": [error],
            "next_suggested_action": "Upload a dataset and retry the report generation.",
            "report_id": None,
            "report_title": None,
        }

    report_id     = result.get("report_id")
    dataset_report = result.get("dataset_report") or {}
    report_plan   = dataset_report.get("report_plan") or {}
    sections      = dataset_report.get("sections") or []
    report_title  = (
        dataset_report.get("title")
        or report_plan.get("report_title")
        or "Intelligence Report"
    )

    exec_sec     = next((s for s in sections if s.get("type") == "executive_summary"), None)
    summary_text = (exec_sec or {}).get("summary") or ""

    limitations: List[str] = []
    save_warning = result.get("report_save_warning")
    if save_warning:
        limitations.append(save_warning)
    limitations.append(
        "Report is based on data at time of generation — re-run for updated results."
    )

    if report_id:
        next_action = (
            f"Report saved (ID: {report_id}). "
            "Open the Reports tab to view, export as PDF/CSV/Excel, or share."
        )
    else:
        next_action = "Navigate to the Reports tab to find your generated report."

    answer_text = "Report generated successfully."
    if summary_text:
        answer_text += f" {summary_text[:300]}"

    return {
        "answer": answer_text.strip(),
        "answer_type": "report_generated",
        "source_summary": (
            f"Report pipeline: {len(sections)} section(s) generated from uploaded dataset."
        ),
        "confidence": 0.95 if report_id else 0.70,
        "limitations": limitations,
        "next_suggested_action": next_action,
        "report_id": report_id,
        "report_title": report_title,
    }


def _generate_answer(package) -> dict:
    """
    Deterministic business-facing answer from EvidencePackage.
    No AI, no SQL, no writes. Evidence rules enforced:
      - never invents facts
      - missing evidence → says evidence is missing
      - partial evidence → notes answer is partial
      - governance restricted → blocks answer, says access is limited
      - does not expose masked PII values
    """
    intent_type = package.intent.intent_type
    evidence    = package.evidence

    if not evidence:
        return _answer_no_data(
            "No evidence was collected. "
            "Ensure a data source is selected and retry."
        )

    items_with_data = [e for e in evidence if e.data is not None]

    # Governance restriction check — must run before any answer is built
    restricted = [e for e in evidence if _is_governance_restricted(e.governance_state)]
    if restricted:
        return _answer_restricted()

    if intent_type == IntentType.DICTIONARY:
        return _answer_dictionary(items_with_data, evidence)
    if intent_type == IntentType.DOMAIN:
        return _answer_domain(items_with_data, evidence)
    if intent_type == IntentType.ENTITY:
        return _answer_entity(items_with_data, evidence)
    if intent_type == IntentType.PROFILING:
        return _answer_profiling(items_with_data, evidence)
    if intent_type == IntentType.GOVERNANCE:
        return _answer_governance(items_with_data, evidence)
    if intent_type == IntentType.RELATIONSHIP:
        return _answer_relationship(items_with_data, evidence)
    if intent_type == IntentType.REVIEW:
        return _answer_review(items_with_data, evidence)
    if intent_type == IntentType.UNKNOWN:
        return _answer_unknown()

    # METADATA_LOOKUP, KNOWLEDGE_GRAPH, QUESTION_ANSWERING, and all other types
    return _answer_metadata(items_with_data, evidence)


# ---------------------------------------------------------------------------
# Milestone M-31 — Conversation Context
# ---------------------------------------------------------------------------

def _build_conversation_context(body: "ComposerRequest", user_id: str):
    """
    Resolves the core.orchestrator.agent.ConversationContext to pass into
    answer_business_question() for this request, or None when there is
    nothing to carry — a plain fresh question with no clarification resume/
    cancel and no round-tripped prior turn.

    This is the one place a round-tripped conversation_state's own
    conversation_id is checked against an independent reference (this
    request's own session_id) — a mismatch discards the prior turn (prior=
    None) rather than reusing it, since there is no server-side conversation
    registry to check it against otherwise. source_id/user_id are re-checked
    here AND independently inside core.orchestrator.agent.
    _resolve_conversation_context (defense in depth, and the only isolation
    guarantee available to any OTHER caller of answer_business_question).

    clarification_selection/cancel_clarification always come from THIS
    request's own top-level fields, never from the round-tripped prior
    turn — a resume/cancel is a one-turn instruction, not carried state.
    """
    from core.orchestrator.agent import ConversationContext

    if not (body.conversation_state or body.clarification_selection or body.cancel_clarification):
        return None

    prior = ConversationContext.from_dict(body.conversation_state) if body.conversation_state else None
    if prior is not None and (
        prior.conversation_id != body.session_id
        or prior.source_id != body.selected_data_source
        or prior.user_id != user_id
    ):
        prior = None  # cross-conversation/source/user — never reused

    return ConversationContext(
        conversation_id=body.session_id,
        source_id=body.selected_data_source,
        user_id=user_id,
        turn_number=(prior.turn_number + 1) if prior else 1,
        created_at=datetime.now(timezone.utc).isoformat(),
        previous_question=prior.previous_question if prior else None,
        previous_effective_question=prior.previous_effective_question if prior else None,
        previous_query_plan=prior.previous_query_plan if prior else None,
        previous_business_plan=prior.previous_business_plan if prior else None,
        selected_tables=prior.selected_tables if prior else (),
        metric=prior.metric if prior else None,
        dimensions=prior.dimensions if prior else (),
        filters=prior.filters if prior else (),
        time_range=prior.time_range if prior else None,
        clarification_selection=tuple(body.clarification_selection) if body.clarification_selection else None,
        cancel_clarification=bool(body.cancel_clarification),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@composer_router.post("/composer/ask")
def composer_ask(
    body: ComposerRequest,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    """
    Enterprise Composer API — Phase 2.

    Accepts a Composer request, resolves intent via the EnterpriseOrchestrator,
    collects read-only evidence from registered services, and returns a
    structured EvidencePackage.

    No AI generation. No SQL execution. No writes. Read-only.
    """
    request_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    logger.info(
        "composer.ask start request_id=%s session_id=%s user_id=%s",
        request_id,
        body.session_id,
        str(user.user_id),
    )

    # ── Milestone M-30/M-31 — Production Route Integration ─────────────────
    # A business-data question — fresh, a clarification resume/cancel, or a
    # recognized bounded follow-up (M-31) — is routed through the one shared
    # agent entry point (core.orchestrator.agent.answer_business_question)
    # instead of the orchestrator's legacy live_query adapter — using the
    # SAME IntentResolver this endpoint already relies on (via
    # EnterpriseOrchestrator.process()) to decide whether the agent applies,
    # so metadata/dictionary/domain/profiling/governance/relationship/
    # knowledge_graph/review/report_generation/workflow/search questions are
    # completely unaffected and keep going through
    # EnterpriseOrchestrator.process() below, unchanged.
    #
    # A recognized follow-up (e.g. "What about last quarter?") often carries
    # no SQL_REQUEST signal of its own on the bare phrase — is_recognized_
    # follow_up covers that case independently of _resolved_intent_for_
    # routing, only when a round-tripped conversation_state is present.
    #
    # One case deliberately stays on the legacy path rather than the agent:
    # the CCPP "classes/courses ... enrollment" question pattern
    # (core.orchestrator.context_builder._class_enrollment_ranking_entity) —
    # this hardcoded, source-verified business-model override lives inside
    # _live_query, not the agent (out of scope for M-28/M-29) — matching
    # questions keep going through the legacy path, where the override still
    # applies unchanged; _is_verified_ccpp_source's own fail-closed check
    # still falls through to the generic pipeline for every other source
    # exactly as it does today.
    try:
        from core.orchestrator.context_builder import _class_enrollment_ranking_entity
        from core.orchestrator.intent_resolver import IntentResolver

        _resolved_intent_for_routing = IntentResolver().resolve(body.message)
    except Exception:  # noqa: BLE001
        logger.warning("composer.ask: intent pre-resolution for agent routing failed request_id=%s", request_id)
        _resolved_intent_for_routing = None

    is_recognized_follow_up = False
    if body.conversation_state and not body.clarification_selection and not body.cancel_clarification:
        from core.orchestrator.agent import _classify_follow_up

        is_recognized_follow_up = _classify_follow_up(body.message) is not None

    if (
        _resolved_intent_for_routing is not None
        and body.selected_data_source is not None
        and _class_enrollment_ranking_entity(body.message) is None
        and (
            _resolved_intent_for_routing.intent_type == IntentType.SQL_REQUEST
            or bool(body.clarification_selection)
            or body.cancel_clarification
            or is_recognized_follow_up
        )
    ):
        try:
            from api.v1.agent_response_adapters import build_composer_agent_response, build_conversation_state
            from core.orchestrator.agent import answer_business_question

            conversation_context = _build_conversation_context(body, str(user.user_id))
            state = answer_business_question(
                body.selected_data_source, str(user.user_id), body.message,
                conversation_context=conversation_context,
            )
            execution_time = round(time.perf_counter() - t0, 4)
            this_turn_number = conversation_context.turn_number if conversation_context else 1
            conversation_state = build_conversation_state(
                state, conversation_id=body.session_id, source_id=body.selected_data_source,
                user_id=str(user.user_id), turn_number=this_turn_number,
            )
            response = build_composer_agent_response(
                state=state, resolved_intent=_resolved_intent_for_routing,
                session_id=body.session_id, request_id=request_id, execution_time=execution_time,
                conversation_state=conversation_state,
            )
        except Exception:
            logger.exception("composer.ask agent path failed request_id=%s", request_id)
            return JSONResponse(
                status_code=500,
                content=build_error_response("Internal orchestration failure"),
            )

        logger.info(
            "composer.ask complete via agent request_id=%s session_id=%s agent_status=%s duration=%.4fs",
            request_id, body.session_id, response.get("agent_status"), execution_time,
        )
        return response

    try:
        orch_request = OrchestratorRequest(
            query=body.message,
            source_id=body.selected_data_source,
            user_id=str(user.user_id),
            params={
                "workspace_id": body.workspace_id,
                "session_id": body.session_id,
                "selected_dataset": body.selected_dataset,
                "selected_table": body.selected_table,
                "clarification_selection": body.clarification_selection,
                "cancel_clarification": body.cancel_clarification,
            },
            request_id=request_id,
        )

        orchestrator = EnterpriseOrchestrator()
        package = orchestrator.process(orch_request)

    except Exception as exc:
        logger.error(
            "composer.ask internal failure request_id=%s type=%s",
            request_id,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=build_error_response("Internal orchestration failure"),
        )

    execution_time = round(time.perf_counter() - t0, 4)

    # ── Report generation delegation ──────────────────────────────────────────
    # When the intent is REPORT_GENERATION, delegate to the existing report
    # pipeline instead of the normal evidence collection path.
    # Existing dataset ownership and trust guards are enforced by the pipeline.
    if package.intent.intent_type == IntentType.REPORT_GENERATION:
        business_answer = _answer_report_generation(
            user_id=str(user.user_id),
            dataset_id=body.dataset_id,
            intent_text=body.message,
        )
        report_id   = business_answer.get("report_id")
        rpt_status  = "success" if report_id else "partial"

        logger.info(
            "composer.ask report_generation request_id=%s report_id=%s status=%s duration=%.4fs",
            package.request_id,
            report_id,
            rpt_status,
            execution_time,
        )

        return {
            "status": rpt_status,
            "request_id": package.request_id,
            "session_id": body.session_id,
            "business_answer": business_answer,
            "report_id": report_id,
            "report_title": business_answer.get("report_title"),
            "resolved_intent": {
                "intent_type": package.intent.intent_type.value,
                "confidence": package.intent.confidence,
                "required_capabilities": [
                    c.value for c in package.intent.required_capabilities
                ],
                "secondary_intents": [
                    i.value for i in package.intent.secondary_intents
                ],
                "keywords_matched": package.intent.keywords_matched,
            },
            "services_selected": ["report_pipeline"],
            "evidence_summary": {
                "total_items": 1 if report_id else 0,
                "services_attempted": 1,
                "services_succeeded": 1 if report_id else 0,
                "has_errors": business_answer["answer_type"] == "report_generation_failed",
            },
            "evidence_package": _serialize_package(package),
            "governance_state": None,
            "confidence": package.intent.confidence,
            "execution_time": execution_time,
            "warnings": [],
            "errors": [],
        }

    status           = _determine_status(package)
    governance_state = _extract_governance_state(package)
    warnings         = _extract_warnings(package)
    business_answer  = _generate_answer(package)

    # ── Enterprise Answer Generation Layer — Phase 10 ──────────────────────
    # Purely additive: reuses the same package computed above (no second
    # evidence-gathering pass) plus the Execution Planner (Phase 9) to
    # produce a richer, deterministic enterprise_answer alongside the
    # existing business_answer. No AI. Every existing response field is
    # unchanged — this only adds two new top-level keys.
    try:
        from core.answering.answer_planner import AnswerPlanner
        from core.execution.planner import ExecutionPlanner

        execution_strategy = ExecutionPlanner().plan(
            body.message, body.selected_data_source, str(user.user_id)
        )
        enterprise_answer = AnswerPlanner().build(execution_strategy, package)
    except Exception:
        logger.exception(
            "composer.ask enterprise_answer generation failed request_id=%s", request_id
        )
        execution_strategy = None
        enterprise_answer = None

    logger.info(
        "composer.ask complete request_id=%s session_id=%s intent=%s "
        "services=%d/%d status=%s duration=%.4fs",
        request_id,
        body.session_id,
        package.intent.intent_type.value,
        package.services_succeeded,
        package.services_attempted,
        status,
        execution_time,
    )

    return {
        "status": status,
        "request_id": package.request_id,
        "session_id": body.session_id,
        "business_answer": business_answer,
        "resolved_intent": {
            "intent_type": package.intent.intent_type.value,
            "confidence": package.intent.confidence,
            "required_capabilities": [
                c.value for c in package.intent.required_capabilities
            ],
            "secondary_intents": [
                i.value for i in package.intent.secondary_intents
            ],
            "keywords_matched": package.intent.keywords_matched,
        },
        "services_selected": [c.service_id for c in package.service_calls],
        "evidence_summary": {
            "total_items": package.total_evidence_items,
            "services_attempted": package.services_attempted,
            "services_succeeded": package.services_succeeded,
            "has_errors": bool(package.errors),
        },
        "evidence_package": _serialize_package(package),
        "governance_state": governance_state,
        "confidence": package.intent.confidence,
        "execution_time": execution_time,
        "warnings": warnings,
        "errors": package.errors,
        "enterprise_answer": enterprise_answer.to_dict() if enterprise_answer else None,
        "execution_strategy": execution_strategy.to_dict() if execution_strategy else None,
    }
