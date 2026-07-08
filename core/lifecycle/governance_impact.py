from dataclasses import dataclass, field

from core.lifecycle.models import ChangeSet


@dataclass
class ImpactItem:
    object_type: str  # 'pii.new' | 'domain.assignment' | 'entity.assignment' | 'schema.drift'
    table_fqn: str
    column_name: str = ""  # '' sentinel for table-level items
    reasoning: list[dict] = field(default_factory=list)
    suggested_domain: str | None = None
    suggested_entity: str | None = None
    suggested_business_name: str | None = None
    suggested_description: str | None = None
    confidence: float | None = None


def detect_new_pii(
    before: dict[tuple[str, str], bool],
    after: dict[tuple[str, str], bool],
) -> list[ImpactItem]:
    """Flag columns whose pii_risk flipped to True.

    before/after are {(table_fqn, column_name): pii_risk} captured immediately
    before and after a dictionary refresh, scoped to the affected columns only.
    A column absent from `before` (brand new column) counts as pii_confirmed=False
    beforehand, so a new column that is PII from the start is still flagged.
    """
    items: list[ImpactItem] = []
    for key, is_pii_after in after.items():
        if not is_pii_after:
            continue
        was_pii_before = before.get(key, False)
        if was_pii_before:
            continue
        table_fqn, column_name = key
        items.append(ImpactItem(
            object_type="pii.new",
            table_fqn=table_fqn,
            column_name=column_name,
            reasoning=[{"signal": "pii_risk flagged True on dictionary refresh"}],
            suggested_description="Column flagged as potential PII — requires review.",
        ))
    return items


def detect_reclassification(
    object_type: str,
    before: dict[str, str],
    after: dict[str, str],
) -> list[ImpactItem]:
    """Flag tables whose domain/entity value changed between runs.

    Only fires for table_fqns present in BOTH before and after with a different
    value — a first-time assignment (table_fqn absent from `before`) is not a
    reclassification and does not generate a review task on its own.
    """
    items: list[ImpactItem] = []
    for table_fqn, new_value in after.items():
        old_value = before.get(table_fqn)
        if old_value is None or old_value == new_value:
            continue
        item = ImpactItem(
            object_type=object_type,
            table_fqn=table_fqn,
            reasoning=[{"signal": f"classification changed from '{old_value}' to '{new_value}'"}],
        )
        if object_type == "domain.assignment":
            item.suggested_domain = new_value
        elif object_type == "entity.assignment":
            item.suggested_entity = new_value
        items.append(item)
    return items


def detect_schema_drift(change_set: ChangeSet) -> list[ImpactItem]:
    """One ImpactItem per removed table and one per removed column.

    Added tables/columns do not generate drift tasks on their own — they get
    fresh dictionary/domain/entity rows via the refresh steps, which is enough
    signal without a separate review task.
    """
    items: list[ImpactItem] = []
    for table_fqn in change_set.removed_tables:
        items.append(ImpactItem(
            object_type="schema.drift",
            table_fqn=table_fqn,
            reasoning=[{"signal": "table removed from source schema"}],
            suggested_description=f"Table '{table_fqn}' no longer exists in the source schema.",
        ))
    for change in change_set.column_changes:
        if change.change_type != "removed":
            continue
        items.append(ImpactItem(
            object_type="schema.drift",
            table_fqn=change.table_fqn,
            column_name=change.column_name,
            reasoning=[{"signal": f"column removed (was {change.old_data_type})"}],
            suggested_description=(
                f"Column '{change.column_name}' removed from '{change.table_fqn}'."
            ),
        ))
    return items
