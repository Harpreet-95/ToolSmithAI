from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.connectors.schema import SchemaSnapshot
from core.dictionary.humanizer import humanize_column_name, humanize_table_name
from core.dictionary.rule_classifier import classify_column, classify_table


@dataclass
class TableDictEntry:
    source_id:         int
    snapshot_id:       int
    table_fqn:         str
    table_name:        str
    schema_name:       str
    table_type:        str
    business_name:     str
    description:       str
    domain:            str
    grain:             str
    generation_method: str = 'rule_based'


@dataclass
class ColumnDictEntry:
    source_id:         int
    snapshot_id:       int
    table_fqn:         str
    column_name:       str
    business_label:    str
    meaning:           str
    semantic_type:     str
    is_metric:         bool
    is_dimension:      bool
    is_date:           bool
    is_id:             bool
    pii_risk:          bool
    generation_method: str = 'rule_based'


@dataclass
class DictionaryResult:
    source_id:         int
    snapshot_id:       int
    generated_at:      str           # ISO-8601
    generation_method: str           # 'rule_based' | 'ai_enriched'
    pii_column_count:  int
    table_entries:     list[TableDictEntry]  = field(default_factory=list)
    column_entries:    list[ColumnDictEntry] = field(default_factory=list)


def _make_meaning(semantic_type: str, business_label: str, table_business_name: str) -> str:
    label = business_label.lower()
    table = table_business_name.lower()
    if semantic_type == 'id':
        return f"Unique identifier for the {table}."
    if semantic_type == 'date':
        return f"Date or timestamp indicating when {label} occurred."
    if semantic_type == 'flag':
        stripped = label.removeprefix('is ').removeprefix('has ').removeprefix('can ')
        return f"Indicates whether {stripped}."
    if semantic_type == 'metric':
        return f"Numeric measure of {label}."
    if semantic_type == 'dimension':
        return f"Categorizes {label} for the {table}."
    return f"Stores {label} data."


def generate_dictionary(snapshot: SchemaSnapshot, snapshot_id: int) -> DictionaryResult:
    """Generate a complete rule-based data dictionary from a SchemaSnapshot.

    No LLM, no database access, no data sampling.
    PII columns receive a fixed placeholder meaning; all others receive
    a basic meaning derived from their semantic type and humanized label.
    """
    source_id = snapshot.source_id
    generated_at = datetime.now(timezone.utc).isoformat()
    table_entries: list[TableDictEntry] = []
    column_entries: list[ColumnDictEntry] = []
    pii_count = 0

    for schema in snapshot.schemas:
        for table in schema.tables:
            table_meta   = classify_table(table, schema.schema_name)
            business_name = humanize_table_name(table.table_name, schema.schema_name)

            if table.table_type == 'VIEW':
                description = f"View of {table_meta['domain'].lower()} data."
            else:
                description = (
                    f"Contains {table_meta['domain'].lower()} data. "
                    f"{table_meta['grain']}."
                )

            table_entries.append(TableDictEntry(
                source_id=source_id,
                snapshot_id=snapshot_id,
                table_fqn=table.table_fqn,
                table_name=table.table_name,
                schema_name=schema.schema_name,
                table_type=table.table_type,
                business_name=business_name,
                description=description,
                domain=table_meta['domain'],
                grain=table_meta['grain'],
            ))

            for column in table.columns:
                col_meta      = classify_column(column, table)
                business_label = humanize_column_name(column.column_name)

                if col_meta['pii_risk']:
                    meaning = "[PII — manual review required]"
                    pii_count += 1
                else:
                    meaning = _make_meaning(
                        col_meta['semantic_type'], business_label, business_name
                    )

                column_entries.append(ColumnDictEntry(
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    table_fqn=table.table_fqn,
                    column_name=column.column_name,
                    business_label=business_label,
                    meaning=meaning,
                    semantic_type=col_meta['semantic_type'],
                    is_metric=col_meta['is_metric'],
                    is_dimension=col_meta['is_dimension'],
                    is_date=col_meta['is_date'],
                    is_id=col_meta['is_id'],
                    pii_risk=col_meta['pii_risk'],
                ))

    return DictionaryResult(
        source_id=source_id,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        generation_method='rule_based',
        pii_column_count=pii_count,
        table_entries=table_entries,
        column_entries=column_entries,
    )
