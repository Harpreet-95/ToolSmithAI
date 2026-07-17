import json
import logging
import time
from datetime import datetime, timezone

from core.lifecycle.diff import diff_snapshots
from core.lifecycle.governance_impact import (
    ImpactItem, detect_new_pii, detect_reclassification, detect_schema_drift,
)
from core.lifecycle.models import LifecycleRunResult, LifecycleTrigger, StepResult, WorkflowStep

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_autonomous_lifecycle(
    source_id: int,
    user_id: str,
    trigger: LifecycleTrigger,
    job_id: int | None = None,
) -> LifecycleRunResult:
    """Run the autonomous metadata lifecycle (AUTONOMOUS_METADATA_WORKFLOW) for source_id.

    Fixed 10-step sequence: detect changes -> refresh dictionary/domains/entities
    for changed objects only -> relationships/knowledge graph (no-op, already
    current) -> detect governance impact -> create review tasks -> notify ->
    update dashboard (no-op, all reads are live). Never raises — every failure
    is recorded on the returned LifecycleRunResult instead.
    """
    from data.lifecycle_service import (
        get_domain_values, get_entity_values, get_latest_two_snapshots,
        get_pii_flags, record_lifecycle_run_complete, record_lifecycle_run_failed,
        record_lifecycle_run_start,
    )

    started_at = _now()
    result = LifecycleRunResult(
        run_id=None, source_id=source_id, trigger=trigger,
        status="RUNNING", started_at=started_at,
    )

    try:
        prev, latest = get_latest_two_snapshots(source_id)
    except Exception as exc:
        logger.exception("lifecycle: failed to load snapshots for source_id=%s", source_id)
        result.status = "FAILED"
        result.error_message = f"Failed to load schema snapshots: {exc}"
        result.completed_at = _now()
        _audit(result, user_id)
        return result

    old_snapshot_id, old_snapshot = (prev if prev is not None else (None, None))
    new_snapshot_id, new_snapshot = latest

    t0 = time.monotonic()
    change_set = diff_snapshots(old_snapshot, new_snapshot)
    result.change_set = change_set
    result.steps.append(StepResult(
        step=WorkflowStep.CHANGE_DETECTION, status="OK",
        detail=(
            f"{len(change_set.added_tables)} added, {len(change_set.removed_tables)} removed, "
            f"{len(change_set.modified_tables)} modified"
        ),
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))

    run_id = record_lifecycle_run_start(
        source_id, user_id, job_id, trigger.value, old_snapshot_id, new_snapshot_id,
    )
    result.run_id = run_id

    if not change_set.has_changes:
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_DICTIONARY, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_DOMAINS, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_ENTITIES, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_RELATIONSHIPS, status="SKIPPED_NO_CHANGES",
            detail="Declared FK relationships already refreshed automatically by "
                   "schema_service.run_discovery(); no schema change to re-run candidate discovery for",
        ))
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_KNOWLEDGE_GRAPH, status="SKIPPED_NOOP",
            detail="Knowledge graph is computed live on read; no persisted artifact to refresh",
        ))
        result.steps.append(StepResult(step=WorkflowStep.DETECT_GOVERNANCE_IMPACT, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.CREATE_REVIEW_TASKS, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(
            step=WorkflowStep.NOTIFY, status="SKIPPED_NO_CHANGES",
            detail="No notification sent — schema unchanged since last scan",
        ))
        result.steps.append(StepResult(
            step=WorkflowStep.UPDATE_DASHBOARD, status="SKIPPED_NOOP",
            detail="All dashboard/read endpoints query tables live; no cache to invalidate",
        ))
        result.status = "COMPLETE"
        result.completed_at = _now()
        record_lifecycle_run_complete(run_id, result)
        _audit(result, user_id)
        return result

    affected = change_set.affected_table_fqns

    # --- REFRESH_DICTIONARY --------------------------------------------------
    before_pii = get_pii_flags(source_id, affected)
    if affected:
        t0 = time.monotonic()
        try:
            from data.dictionary_service import generate_and_save_dictionary
            result.dictionary_summary = generate_and_save_dictionary(
                source_id, user_id, table_fqns=affected,
            )
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_DICTIONARY, status="OK",
                detail=f"{len(affected)} table(s) refreshed",
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
        except Exception as exc:
            logger.exception("lifecycle: dictionary refresh failed for source_id=%s", source_id)
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_DICTIONARY, status="FAILED", detail=str(exc),
            ))
            result.status = "FAILED"
            result.error_message = f"Dictionary refresh failed: {exc}"
            result.completed_at = _now()
            record_lifecycle_run_failed(run_id, result.error_message, result)
            _audit(result, user_id)
            return result
    else:
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_DICTIONARY, status="SKIPPED_NO_CHANGES",
            detail="No added/modified tables to refresh",
        ))
    after_pii = get_pii_flags(source_id, affected)

    # --- REFRESH_DOMAINS -------------------------------------------------------
    before_domain = get_domain_values(source_id, affected)
    if affected:
        t0 = time.monotonic()
        try:
            from data.domain_service import generate_domain_assignments
            result.domain_summary = generate_domain_assignments(
                source_id, user_id, table_fqns=affected,
            )
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_DOMAINS, status="OK",
                detail=f"{len(affected)} table(s) refreshed",
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
        except Exception as exc:
            logger.exception("lifecycle: domain refresh failed for source_id=%s", source_id)
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_DOMAINS, status="FAILED", detail=str(exc),
            ))
            result.status = "FAILED"
            result.error_message = f"Domain refresh failed: {exc}"
            result.completed_at = _now()
            record_lifecycle_run_failed(run_id, result.error_message, result)
            _audit(result, user_id)
            return result
    else:
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_DOMAINS, status="SKIPPED_NO_CHANGES",
            detail="No added/modified tables to refresh",
        ))
    after_domain = get_domain_values(source_id, affected)

    # --- REFRESH_ENTITIES -------------------------------------------------------
    before_entity = get_entity_values(source_id, affected)
    if affected:
        t0 = time.monotonic()
        try:
            from data.entity_service import generate_entity_assignments
            result.entity_summary = generate_entity_assignments(
                source_id, user_id, table_fqns=affected,
            )
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_ENTITIES, status="OK",
                detail=f"{len(affected)} table(s) refreshed",
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
        except Exception as exc:
            logger.exception("lifecycle: entity refresh failed for source_id=%s", source_id)
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_ENTITIES, status="FAILED", detail=str(exc),
            ))
            result.status = "FAILED"
            result.error_message = f"Entity refresh failed: {exc}"
            result.completed_at = _now()
            record_lifecycle_run_failed(run_id, result.error_message, result)
            _audit(result, user_id)
            return result
    else:
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_ENTITIES, status="SKIPPED_NO_CHANGES",
            detail="No added/modified tables to refresh",
        ))
    after_entity = get_entity_values(source_id, affected)

    # --- REFRESH_RELATIONSHIPS ---------------------------------------------------
    # Declared FK relationships are already refreshed automatically by
    # schema_service.run_discovery(). This step covers the remaining piece: inferred
    # (non-FK) relationship candidates, persisted as PENDING and never auto-trusted.
    t0 = time.monotonic()
    try:
        from data.relationship_service import discover_relationship_candidates
        result.relationship_summary = discover_relationship_candidates(source_id, user_id)
        summary = result.relationship_summary or {}
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_RELATIONSHIPS, status="OK",
            detail=(
                f"{summary.get('candidates_persisted', 0)} candidate(s) persisted as PENDING, "
                f"{summary.get('candidates_discarded_low_confidence', 0)} discarded (low confidence), "
                f"{summary.get('candidates_skipped_existing', 0)} already covered"
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        ))
    except Exception as exc:
        logger.exception("lifecycle: relationship discovery failed for source_id=%s", source_id)
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_RELATIONSHIPS, status="FAILED", detail=str(exc),
        ))

    # --- REFRESH_KNOWLEDGE_GRAPH — legitimate no-op ---------------------------
    result.steps.append(StepResult(
        step=WorkflowStep.REFRESH_KNOWLEDGE_GRAPH, status="SKIPPED_NOOP",
        detail="Knowledge graph is computed live on read; no persisted artifact to refresh",
    ))

    # --- DETECT_GOVERNANCE_IMPACT ------------------------------------------------
    t0 = time.monotonic()
    impact_items: list[ImpactItem] = []
    impact_items.extend(detect_new_pii(before_pii, after_pii))
    impact_items.extend(detect_reclassification("domain.assignment", before_domain, after_domain))
    impact_items.extend(detect_reclassification("entity.assignment", before_entity, after_entity))
    impact_items.extend(detect_schema_drift(change_set))
    result.steps.append(StepResult(
        step=WorkflowStep.DETECT_GOVERNANCE_IMPACT, status="OK",
        detail=f"{len(impact_items)} impact item(s) detected",
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))

    # --- CREATE_REVIEW_TASKS ---------------------------------------------------
    t0 = time.monotonic()
    created = 0
    if impact_items:
        from data.review_task_service import create_review_task
        for item in impact_items:
            task_id = create_review_task(
                source_id=source_id,
                object_type=item.object_type,
                table_fqn=item.table_fqn,
                column_name=item.column_name,
                reasoning=item.reasoning,
                suggested_domain=item.suggested_domain,
                suggested_entity=item.suggested_entity,
                suggested_business_name=item.suggested_business_name,
                suggested_description=item.suggested_description,
                confidence=item.confidence,
            )
            if task_id is not None:
                created += 1
                _log_governance_flag(item, source_id)
    result.review_tasks_created = created
    result.steps.append(StepResult(
        step=WorkflowStep.CREATE_REVIEW_TASKS, status="OK",
        detail=f"{created} new review task(s) created ({len(impact_items) - created} already pending)",
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))

    # --- NOTIFY -------------------------------------------------------------------
    notified = 0
    try:
        from data.notification_service import create_notification
        title = "Metadata lifecycle refresh completed"
        message = (
            f"Autonomous metadata lifecycle for source {source_id}: "
            f"{len(change_set.added_tables)} table(s) added, "
            f"{len(change_set.removed_tables)} removed, "
            f"{len(change_set.modified_tables)} modified, "
            f"{created} review task(s) created."
        )
        create_notification(
            user_id=user_id, title=title, message=message,
            type="metadata_lifecycle", status="success",
        )
        notified = 1
    except Exception:
        logger.warning("lifecycle: notification failed for source_id=%s", source_id, exc_info=True)
    result.notifications_sent = notified
    result.steps.append(StepResult(
        step=WorkflowStep.NOTIFY, status="OK" if notified else "FAILED",
        detail=f"{notified} notification(s) sent",
    ))

    # --- UPDATE_DASHBOARD — legitimate no-op ---------------------------------------
    result.steps.append(StepResult(
        step=WorkflowStep.UPDATE_DASHBOARD, status="SKIPPED_NOOP",
        detail="All dashboard/read endpoints query tables live; no cache to invalidate",
    ))

    result.status = "COMPLETE"
    result.completed_at = _now()
    record_lifecycle_run_complete(run_id, result)
    _audit(result, user_id)
    return result


def _log_governance_flag(item: ImpactItem, source_id: int) -> None:
    try:
        from data.governance_service import GovernanceState, log_governance_event
        object_id = f"{source_id}:{item.table_fqn}"
        if item.column_name:
            object_id += f":{item.column_name}"
        log_governance_event(
            object_type_id=item.object_type,
            object_id=object_id,
            event_type="LIFECYCLE_FLAGGED",
            from_state=GovernanceState.GENERATED,
            to_state=GovernanceState.NEEDS_REVIEW,
            actor_id="lifecycle_engine",
            source_service="core.lifecycle.runner",
        )
    except Exception:
        logger.warning("lifecycle: governance event logging failed", exc_info=True)


def _audit(result: LifecycleRunResult, user_id: str) -> None:
    try:
        from data.audit import log_audit_event
        log_audit_event(
            {
                "task_type": "metadata_lifecycle.workflow_run",
                "original_input": json.dumps({
                    "source_id": result.source_id,
                    "trigger": result.trigger.value,
                    "run_id": result.run_id,
                }),
                "status": "success" if result.status == "COMPLETE" else "failed",
            },
            user_id=user_id,
        )
    except Exception:
        logger.warning("lifecycle: audit logging failed", exc_info=True)
