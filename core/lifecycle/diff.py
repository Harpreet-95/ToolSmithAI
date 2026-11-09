from core.connectors.schema import SchemaSnapshot, TableInfo
from core.lifecycle.models import ChangeSet, ColumnChange


def _table_map(snapshot: SchemaSnapshot) -> dict[str, TableInfo]:
    return {
        table.table_fqn: table
        for schema in snapshot.schemas
        for table in schema.tables
    }


def _column_type_map(table: TableInfo) -> dict[str, str]:
    return {c.column_name: c.data_type for c in table.columns}


def diff_snapshots(old: SchemaSnapshot | None, new: SchemaSnapshot) -> ChangeSet:
    """Compare two schema snapshots and return the set of added/removed/modified
    tables and columns. Pure function — no DB access.

    old=None means there is no prior snapshot to diff against (first-ever scan):
    every table in `new` is reported as added, nothing is removed or modified.
    """
    new_tables = _table_map(new)

    if old is None:
        return ChangeSet(
            added_tables=list(new_tables.keys()),
            removed_tables=[],
            modified_tables=[],
            column_changes=[],
            is_first_scan=True,
        )

    old_tables = _table_map(old)

    old_fqns = set(old_tables.keys())
    new_fqns = set(new_tables.keys())

    added_tables = [fqn for fqn in new_tables if fqn not in old_fqns]
    removed_tables = [fqn for fqn in old_tables if fqn not in new_fqns]

    modified_tables: list[str] = []
    column_changes: list[ColumnChange] = []

    for fqn in new_tables:
        if fqn not in old_fqns:
            continue  # already counted as added

        old_cols = _column_type_map(old_tables[fqn])
        new_cols = _column_type_map(new_tables[fqn])
        old_col_names = set(old_cols.keys())
        new_col_names = set(new_cols.keys())

        table_changed = False

        for col_name in new_cols:
            if col_name not in old_col_names:
                column_changes.append(ColumnChange(
                    table_fqn=fqn, column_name=col_name, change_type="added",
                    old_data_type=None, new_data_type=new_cols[col_name],
                ))
                table_changed = True

        for col_name in old_cols:
            if col_name not in new_col_names:
                column_changes.append(ColumnChange(
                    table_fqn=fqn, column_name=col_name, change_type="removed",
                    old_data_type=old_cols[col_name], new_data_type=None,
                ))
                table_changed = True

        for col_name in new_cols:
            if col_name in old_col_names and old_cols[col_name] != new_cols[col_name]:
                column_changes.append(ColumnChange(
                    table_fqn=fqn, column_name=col_name, change_type="type_changed",
                    old_data_type=old_cols[col_name], new_data_type=new_cols[col_name],
                ))
                table_changed = True

        if table_changed:
            modified_tables.append(fqn)

    return ChangeSet(
        added_tables=added_tables,
        removed_tables=removed_tables,
        modified_tables=modified_tables,
        column_changes=column_changes,
        is_first_scan=False,
    )


def diff_profiling_classification(
    old: dict[str, str | None] | None, new: dict[str, str | None],
) -> ChangeSet:
    """Compare table_class across two profiling snapshots and return a
    ChangeSet — the profiling/classification counterpart of diff_snapshots,
    same shape and same consumer contract (added/modified feed
    ChangeSet.affected_table_fqns for the lifecycle's dictionary/domain/
    entity refresh gating), so the rest of run_autonomous_lifecycle needs no
    profiling-specific branching: this and diff_snapshots' outputs are just
    merged before use.

    old/new are {table_fqn: table_class} from
    data.lifecycle_service.get_table_classifications. Pure — no DB access.

    old=None means there is no prior COMPLETE profiling snapshot to diff
    against (this source's first-ever completed profiling run): every
    profiled table is reported as added, mirroring diff_snapshots' own
    old=None contract.

    A table whose table_class is unchanged (including staying None ->
    None, e.g. a table that failed classification in both runs) is not
    reported — this is what keeps a genuinely no-op re-profiling run cheap
    (ChangeSet.has_changes stays False, so run_autonomous_lifecycle takes
    its existing SKIPPED_NO_CHANGES short-circuit) instead of bumping every
    downstream artifact just because a new profiling_snapshot row exists.
    column_changes is always empty here — profiling has no column-shape
    concept of its own; that is schema_snapshots' contract, untouched.
    """
    if old is None:
        return ChangeSet(
            added_tables=list(new.keys()),
            removed_tables=[],
            modified_tables=[],
            column_changes=[],
            is_first_scan=True,
        )

    old_fqns = set(old.keys())
    new_fqns = set(new.keys())

    added_tables = [fqn for fqn in new if fqn not in old_fqns]
    removed_tables = [fqn for fqn in old if fqn not in new_fqns]
    modified_tables = [
        fqn for fqn in new
        if fqn in old_fqns and old[fqn] != new[fqn]
    ]

    return ChangeSet(
        added_tables=added_tables,
        removed_tables=removed_tables,
        modified_tables=modified_tables,
        column_changes=[],
        is_first_scan=False,
    )


def merge_change_sets(schema_change: ChangeSet, profiling_change: ChangeSet) -> ChangeSet:
    """Union two ChangeSets (schema-diff + profiling-classification-diff)
    into one combined ChangeSet, so the rest of run_autonomous_lifecycle
    (affected_table_fqns/has_changes gating, governance-impact detection)
    runs completely unchanged regardless of which side(s) actually changed.

    removed_tables is schema-only: detect_schema_drift's "table removed
    from source schema" wording is specifically a schema-removal claim, and
    a table dropping out of profiling scope while still existing in the
    schema is not that — keeping it schema-only avoids a mislabeled
    governance review task for a shape this module doesn't otherwise
    produce (profiling scope only ever grows to match the schema; see
    profiling_snapshot_resolver's own coverage-never-regresses note).
    is_first_scan carries schema_change's own value only, for the same
    reason: it names a schema-scan event, not a profiling one.
    """
    def _dedup(*lists: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for lst in lists:
            for fqn in lst:
                if fqn not in seen:
                    seen.add(fqn)
                    out.append(fqn)
        return out

    return ChangeSet(
        added_tables=_dedup(schema_change.added_tables, profiling_change.added_tables),
        removed_tables=list(schema_change.removed_tables),
        modified_tables=_dedup(schema_change.modified_tables, profiling_change.modified_tables),
        column_changes=list(schema_change.column_changes),
        is_first_scan=schema_change.is_first_scan,
    )
