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
