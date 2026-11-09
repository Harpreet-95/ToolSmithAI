import json
import logging
import time
from datetime import datetime, timezone

from core.lifecycle.diff import diff_profiling_classification, diff_snapshots, merge_change_sets
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

    Fixed 13-step sequence: detect changes -> refresh dictionary -> classify
    PII -> refresh domains/entities -> mature governance -> refresh vocabulary
    and relationships -> knowledge graph (no-op, already current) -> detect
    governance impact -> create review tasks -> notify -> update dashboard
    (no-op, all reads are live).
    Never raises — every failure is recorded on the returned
    LifecycleRunResult instead.

    Change detection is schema diff OR profiling/classification diff,
    merged into one ChangeSet (core.lifecycle.diff.merge_change_sets) — a
    profiling/classification change with no accompanying schema change
    (e.g. a re-run of full/batch profiling that reclassifies a table, with
    no column added/removed/retyped) must not no-op here just because the
    schema half of the diff is empty. See diff_profiling_classification's
    own docstring for why this stays cheap on a genuinely no-op re-profile.
    """
    from data.lifecycle_service import (
        get_domain_values, get_entity_values, get_latest_two_snapshots,
        get_pii_flags, get_table_classifications, record_lifecycle_run_complete,
        record_lifecycle_run_failed, record_lifecycle_run_start,
    )
    from data.profiling_snapshot_resolver import get_latest_two_complete_profiling_snapshots

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

    # Profiling/classification side of change detection — independent of
    # the schema diff below. get_latest_two_complete_profiling_snapshots
    # returns (None, None) for a source with no COMPLETE profiling
    # evidence yet (e.g. these tests' own fixtures, which never set
    # status='COMPLETE'), so this is a guaranteed no-op wherever profiling
    # evidence doesn't exist or hasn't been touched — never a new
    # requirement for callers that only care about schema changes.
    prof_prev, prof_latest = get_latest_two_complete_profiling_snapshots(source_id)
    prof_old_snapshot_id = prof_prev.id if prof_prev is not None else None
    prof_new_snapshot_id = prof_latest.id if prof_latest is not None else None

    # Idempotency guard (Part 16 fix) — any caller that invokes this function
    # twice for a state (schema pair AND profiling pair together) already
    # fully processed gets a cheap no-op instead of a duplicate full run.
    # Keying on the schema pair alone is not fine-grained enough: two runs
    # can share an unchanged schema pair while differing in profiling
    # evidence (a re-profile with no schema change) — see
    # data.lifecycle_service.find_duplicate_lifecycle_run's docstring.
    from data.lifecycle_service import find_duplicate_lifecycle_run
    duplicate = find_duplicate_lifecycle_run(
        source_id, trigger.value, old_snapshot_id, new_snapshot_id,
        prof_old_snapshot_id, prof_new_snapshot_id,
    )
    if duplicate is not None:
        result.run_id = duplicate["id"]
        result.status = "SKIPPED_DUPLICATE"
        result.error_message = (
            f"No-op: run #{duplicate['id']} already completed this exact "
            f"(trigger={trigger.value}, schema {old_snapshot_id}->{new_snapshot_id}, "
            f"profiling {prof_old_snapshot_id}->{prof_new_snapshot_id}) transition."
        )
        result.completed_at = _now()
        return result

    t0 = time.monotonic()
    schema_change_set = diff_snapshots(old_snapshot, new_snapshot)

    if prof_latest is not None:
        old_classifications = get_table_classifications(
            source_id, prof_prev.id if prof_prev is not None else None,
        )
        new_classifications = get_table_classifications(source_id, prof_latest.id)
        profiling_change_set = diff_profiling_classification(
            old_classifications if prof_prev is not None else None, new_classifications,
        )
    else:
        profiling_change_set = diff_profiling_classification({}, {})

    change_set = merge_change_sets(schema_change_set, profiling_change_set)
    result.change_set = change_set
    result.steps.append(StepResult(
        step=WorkflowStep.CHANGE_DETECTION, status="OK",
        detail=(
            f"{len(change_set.added_tables)} added, {len(change_set.removed_tables)} removed, "
            f"{len(change_set.modified_tables)} modified "
            f"(schema: {len(schema_change_set.added_tables)} added/"
            f"{len(schema_change_set.modified_tables)} modified; "
            f"profiling: {len(profiling_change_set.added_tables)} added/"
            f"{len(profiling_change_set.modified_tables)} modified)"
        ),
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))

    run_id = record_lifecycle_run_start(
        source_id, user_id, job_id, trigger.value, old_snapshot_id, new_snapshot_id,
        prof_old_snapshot_id, prof_new_snapshot_id,
    )
    result.run_id = run_id

    if not change_set.has_changes:
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_DICTIONARY, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.CLASSIFY_PII, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_DOMAINS, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_ENTITIES, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.GOVERNANCE_AUTO_MATURATION, status="SKIPPED_NO_CHANGES"))
        result.steps.append(StepResult(step=WorkflowStep.REFRESH_VOCABULARY, status="SKIPPED_NO_CHANGES"))
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
            detail="No notification sent — no schema or profiling/classification change since last run",
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

    # --- CLASSIFY_PII ----------------------------------------------------------
    # Autonomous PII classification (Parts 2-4) — must run before
    # GOVERNANCE_AUTO_MATURATION below, since dictionary/domain/entity
    # auto-approval eligibility reads pii_confirmed. Never blocks the rest of
    # the lifecycle: a classification failure is recorded and skipped, same
    # best-effort convention as vocabulary/relationships below.
    if affected:
        t0 = time.monotonic()
        try:
            from data.pii_classification_service import run_pii_classification
            pii_result = run_pii_classification(source_id, table_fqns=affected)
            result.pii_summary = {
                "columns_evaluated": pii_result.columns_evaluated,
                "confirmed_pii": pii_result.confirmed_pii,
                "confirmed_non_pii": pii_result.confirmed_non_pii,
                "uncertain": pii_result.uncertain,
                "manual_override_skipped": pii_result.manual_override_skipped,
            }
            result.steps.append(StepResult(
                step=WorkflowStep.CLASSIFY_PII, status="OK",
                detail=(
                    f"{pii_result.columns_evaluated} column(s) evaluated: "
                    f"{pii_result.confirmed_pii} confirmed PII, "
                    f"{pii_result.confirmed_non_pii} confirmed non-PII, "
                    f"{pii_result.uncertain} uncertain, "
                    f"{pii_result.manual_override_skipped} manual override(s) preserved"
                ),
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
        except Exception as exc:
            logger.exception("lifecycle: PII classification failed for source_id=%s", source_id)
            result.steps.append(StepResult(
                step=WorkflowStep.CLASSIFY_PII, status="FAILED", detail=str(exc),
            ))
    else:
        result.steps.append(StepResult(
            step=WorkflowStep.CLASSIFY_PII, status="SKIPPED_NO_CHANGES",
            detail="No added/modified tables to classify",
        ))

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

    # --- GOVERNANCE_AUTO_MATURATION ---------------------------------------------
    # Wires the previously-unreachable data.dictionary_curation_service /
    # data.semantic_governance_rollout_service (Parts 7-9): evidence-based
    # auto-approval of dictionary table/column rows plus auto-maturation of
    # domain/entity assignments, using their own existing eligibility gates
    # (governance policy incl. pii_confirmed, review-group classification,
    # ambiguous-sibling check) — no new approval logic invented here. Runs
    # source-wide (not table_fqns-scoped — the underlying service evaluates
    # every not-yet-approved/matured row each call, which is inexpensive and
    # already idempotent by construction: an already-approved/matured row is
    # excluded by its own query). dry_run=False — this is the one step that
    # actually flips governance state autonomously; still bounded entirely by
    # each row's own evidence, and never touches a pii_manual_override'd or
    # human-approved row.
    t0 = time.monotonic()
    try:
        from data.semantic_governance_rollout_service import run_semantic_governance_rollout
        rollout = run_semantic_governance_rollout(source_id, user_id, dry_run=False)
        result.curation_summary = rollout
        dict_r = rollout.get("dictionary", {})
        asn_r = rollout.get("assignments", {})
        result.steps.append(StepResult(
            step=WorkflowStep.GOVERNANCE_AUTO_MATURATION, status="OK",
            detail=(
                f"dictionary: {len(dict_r.get('auto_approved', []))} auto-approved, "
                f"{len(dict_r.get('blocked', []))} blocked; "
                f"assignments: {len(asn_r.get('auto_approved', []))} auto-approved, "
                f"{len(asn_r.get('blocked', []))} blocked"
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        ))
    except Exception as exc:
        logger.exception("lifecycle: governance auto-maturation failed for source_id=%s", source_id)
        result.steps.append(StepResult(
            step=WorkflowStep.GOVERNANCE_AUTO_MATURATION, status="FAILED", detail=str(exc),
        ))

    # --- REFRESH_VOCABULARY -------------------------------------------------------
    # generated_business_vocabulary derives from schema shape PLUS dictionary/
    # domain/entity evidence (data.vocabulary_bootstrap_service's own
    # _dictionary_and_assignment_evidence) — placed after those three so a
    # fresh classification-driven dictionary/domain/entity change is already
    # reflected in the vocabulary this call regenerates. bootstrap_vocabulary
    # is whole-source (no table_fqns scoping — it always fully replaces the
    # source's prior generation in one transaction) and already documents
    # itself as safe to call repeatedly; still gated on `affected` here, same
    # as the three steps above, so a change confined to unrelated tables
    # never pays this cost. Best-effort like REFRESH_RELATIONSHIPS below —
    # bootstrap_vocabulary already catches its own exceptions and never
    # raises, so this failure path is defensive, not expected to fire.
    if affected:
        t0 = time.monotonic()
        try:
            from data.vocabulary_bootstrap_service import bootstrap_vocabulary
            vocab_result = bootstrap_vocabulary(source_id, user_id)
            result.vocabulary_summary = {
                "term_count": vocab_result.term_count,
                "tables_generated": len(vocab_result.generated),
                "tables_skipped": len(vocab_result.skipped),
                "failed": vocab_result.failed,
            }
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_VOCABULARY,
                status="OK" if not vocab_result.failed else "FAILED",
                detail=(
                    vocab_result.failed
                    or f"{vocab_result.term_count} term(s) across "
                       f"{len(vocab_result.generated)} table(s)"
                ),
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
        except Exception as exc:
            logger.exception("lifecycle: vocabulary refresh failed for source_id=%s", source_id)
            result.steps.append(StepResult(
                step=WorkflowStep.REFRESH_VOCABULARY, status="FAILED", detail=str(exc),
            ))
    else:
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_VOCABULARY, status="SKIPPED_NO_CHANGES",
            detail="No added/modified tables to refresh",
        ))

    # --- REFRESH_RELATIONSHIPS ---------------------------------------------------
    # Declared FK relationships are already refreshed automatically by
    # schema_service.run_discovery(). This step covers the remaining piece: inferred
    # (non-FK) relationship candidates, persisted as PENDING and never auto-trusted.
    t0 = time.monotonic()
    try:
        from data.relationship_service import (
            discover_relationship_candidates, discover_semantic_key_candidates,
        )
        summary = discover_relationship_candidates(source_id, user_id) or {}
        # Additive third pass — differently-named key candidates (e.g.
        # child.SessionID -> parent.ID) that name/domain-anchored discovery
        # above cannot reach. Reported as a nested key rather than merged
        # into the top-level counts, so result.relationship_summary's
        # existing top-level shape (and every caller/test already reading
        # it) is unchanged; each pass's own PENDING rows are independently
        # gated by the same auto-trust evidence rules on read regardless of
        # which pass proposed them.
        semantic_summary = discover_semantic_key_candidates(source_id, user_id) or {}
        summary["semantic_key_candidates"] = semantic_summary
        result.relationship_summary = summary
        result.steps.append(StepResult(
            step=WorkflowStep.REFRESH_RELATIONSHIPS, status="OK",
            detail=(
                f"{summary.get('candidates_persisted', 0)} candidate(s) persisted as PENDING, "
                f"{summary.get('candidates_discarded_low_confidence', 0)} discarded (low confidence), "
                f"{summary.get('candidates_skipped_existing', 0)} already covered; "
                f"semantic key-role pass: {semantic_summary.get('candidates_persisted', 0)} persisted, "
                f"{semantic_summary.get('candidates_live_validated', 0)} live-validated"
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
