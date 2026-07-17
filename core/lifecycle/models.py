from dataclasses import dataclass, field
from enum import Enum


class LifecycleTrigger(str, Enum):
    """System events that can start an autonomous metadata lifecycle run.

    SCHEDULED_NIGHTLY / SCHEDULED_HOURLY are reserved for a future scheduler
    (no cron is implemented in this phase) — run_autonomous_lifecycle() takes
    a trigger value as a plain argument so wiring a scheduler later requires
    no changes to this module.
    """
    SCAN_COMPLETE     = "scan_complete"
    MANUAL            = "manual"
    SCHEDULED_NIGHTLY = "scheduled_nightly"
    SCHEDULED_HOURLY  = "scheduled_hourly"


class WorkflowStep(str, Enum):
    CHANGE_DETECTION         = "change_detection"
    REFRESH_DICTIONARY       = "refresh_dictionary"
    REFRESH_DOMAINS          = "refresh_domains"
    REFRESH_ENTITIES         = "refresh_entities"
    REFRESH_RELATIONSHIPS    = "refresh_relationships"
    REFRESH_KNOWLEDGE_GRAPH  = "refresh_knowledge_graph"
    DETECT_GOVERNANCE_IMPACT = "detect_governance_impact"
    CREATE_REVIEW_TASKS      = "create_review_tasks"
    NOTIFY                   = "notify"
    UPDATE_DASHBOARD         = "update_dashboard"


@dataclass(frozen=True)
class ColumnChange:
    table_fqn: str
    column_name: str
    change_type: str  # 'added' | 'removed' | 'type_changed'
    old_data_type: str | None = None
    new_data_type: str | None = None


@dataclass
class ChangeSet:
    added_tables: list[str] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)
    modified_tables: list[str] = field(default_factory=list)
    column_changes: list[ColumnChange] = field(default_factory=list)
    is_first_scan: bool = False

    @property
    def affected_table_fqns(self) -> list[str]:
        """Tables that need dictionary/domain/entity regeneration: added + modified,
        de-duplicated, order-stable. Removed tables are excluded — there is nothing
        to regenerate for a table that no longer exists."""
        seen: set[str] = set()
        result: list[str] = []
        for fqn in [*self.added_tables, *self.modified_tables]:
            if fqn not in seen:
                seen.add(fqn)
                result.append(fqn)
        return result

    @property
    def has_changes(self) -> bool:
        return bool(self.added_tables or self.removed_tables or self.modified_tables)


@dataclass
class StepResult:
    step: WorkflowStep
    status: str  # 'OK' | 'SKIPPED_NOOP' | 'SKIPPED_NO_CHANGES' | 'FAILED'
    detail: str | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict:
        return {
            "step": self.step.value,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class LifecycleRunResult:
    run_id: int | None
    source_id: int
    trigger: LifecycleTrigger
    status: str  # 'RUNNING' | 'COMPLETE' | 'FAILED'
    steps: list[StepResult] = field(default_factory=list)
    change_set: ChangeSet | None = None
    dictionary_summary: dict | None = None
    domain_summary: dict | None = None
    entity_summary: dict | None = None
    relationship_summary: dict | None = None
    review_tasks_created: int = 0
    notifications_sent: int = 0
    error_message: str | None = None
    started_at: str = ""
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "trigger": self.trigger.value,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "change_set": (
                {
                    "added_tables": self.change_set.added_tables,
                    "removed_tables": self.change_set.removed_tables,
                    "modified_tables": self.change_set.modified_tables,
                    "column_changes": [
                        {
                            "table_fqn": c.table_fqn,
                            "column_name": c.column_name,
                            "change_type": c.change_type,
                            "old_data_type": c.old_data_type,
                            "new_data_type": c.new_data_type,
                        }
                        for c in self.change_set.column_changes
                    ],
                    "is_first_scan": self.change_set.is_first_scan,
                }
                if self.change_set is not None
                else None
            ),
            "dictionary_summary": self.dictionary_summary,
            "domain_summary": self.domain_summary,
            "entity_summary": self.entity_summary,
            "relationship_summary": self.relationship_summary,
            "review_tasks_created": self.review_tasks_created,
            "notifications_sent": self.notifications_sent,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
