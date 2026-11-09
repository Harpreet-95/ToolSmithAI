from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.connectors.schema import SchemaSnapshot
from core.dictionary.humanizer import humanize_column_name, humanize_table_name
from core.dictionary.rule_classifier import classify_column, classify_table

# Maps profiling SemanticType values (uppercase) to rule-classifier semantic_type strings.
_PROFILING_TO_DICT_SEMANTIC: dict[str, str] = {
    'ID':      'id',
    'DATE':    'date',
    'FLAG':    'flag',
    'AMOUNT':  'metric',
    'COUNT':   'metric',
    'STATUS':  'dimension',
    'CODE':    'dimension',
    'NAME':    'dimension',
    'TEXT':    'dimension',
    'EMAIL':   'other',
    'PHONE':   'other',
    'SSN':     'other',
    'BINARY':  'other',
    'UNKNOWN': 'other',
}


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


def _make_meaning_with_profiling(
    semantic_type: str,
    business_label: str,
    table_business_name: str,
    prof: dict,
) -> str:
    """Generate a meaning string enhanced with deep profiling signals.

    Falls back to the same templates as _make_meaning, then appends caveats
    derived from null rate, quality score, and cardinality tier.
    """
    label = business_label.lower()
    table = table_business_name.lower()

    uniqueness      = prof.get('uniqueness_score')
    cardinality_tier = (prof.get('cardinality_tier') or '').upper()
    null_pct        = prof.get('null_percentage') or 0.0
    blank_pct       = prof.get('blank_percentage') or 0.0
    quality_score   = prof.get('quality_score')

    if semantic_type == 'id':
        if uniqueness is not None and uniqueness >= 0.95:
            base = f"Unique identifier for each {table} record."
        else:
            base = f"Unique identifier for the {table}."
    elif semantic_type == 'date':
        base = f"Date or timestamp indicating when {label} occurred."
    elif semantic_type == 'flag':
        stripped = label.removeprefix('is ').removeprefix('has ').removeprefix('can ')
        base = f"Indicates whether {stripped}."
    elif semantic_type == 'metric':
        base = f"Numeric measure of {label}."
    elif semantic_type == 'dimension':
        if cardinality_tier in ('BINARY', 'LOW', 'CONSTANT'):
            base = f"Categorical or status indicator for {label}."
        else:
            base = f"Categorizes {label} for the {table}."
    else:
        base = f"Stores {label} data."

    caveats: list[str] = []
    if null_pct > 30 or blank_pct > 30:
        caveats.append("often missing or optional in practice")
    if quality_score is not None and quality_score < 60:
        caveats.append("requires review due to data quality")

    if not caveats:
        return base
    return base.rstrip('.') + ' — ' + '; '.join(caveats) + '.'


def generate_dictionary(
    snapshot: SchemaSnapshot,
    snapshot_id: int,
    profiling_context: dict[tuple[str, str], dict] | None = None,
) -> DictionaryResult:
    """Generate a complete rule-based data dictionary from a SchemaSnapshot.

    If profiling_context is provided (a dict keyed by (table_fqn, column_name)),
    column semantic types are improved when profiling confidence >= 0.70, and
    meanings are enriched with null-rate, quality, and cardinality signals.
    Falls back to pure rule-based behaviour when no profiling data is available.
    No LLM, no live data sampling.
    """
    source_id = snapshot.source_id
    generated_at = datetime.now(timezone.utc).isoformat()
    table_entries: list[TableDictEntry] = []
    column_entries: list[ColumnDictEntry] = []
    pii_count = 0
    profiling_used = False

    for schema in snapshot.schemas:
        for table in schema.tables:
            table_meta    = classify_table(table, schema.schema_name)
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
                col_meta       = classify_column(column, table)
                business_label = humanize_column_name(column.column_name)

                # Resolve profiling data for this column (None if no snapshot exists)
                prof: dict | None = None
                if profiling_context is not None:
                    prof = profiling_context.get((table.table_fqn, column.column_name))

                # Start from rule-based classification
                semantic_type = col_meta['semantic_type']
                pii_risk      = col_meta['pii_risk']

                if prof is not None:
                    # PII: profiling confirmation extends rule-classifier detection
                    if not pii_risk and (prof.get('pii_confirmed') or prof.get('pii_name_heuristic')):
                        pii_risk = True

                    # Semantic type: override only when profiling confidence >= 0.70
                    prof_sem  = (prof.get('semantic_type') or '').upper()
                    prof_conf = prof.get('semantic_confidence')
                    if prof_sem and prof_conf is not None and prof_conf >= 0.70:
                        mapped = _PROFILING_TO_DICT_SEMANTIC.get(prof_sem)
                        if mapped:
                            semantic_type = mapped

                # Generate meaning
                if pii_risk:
                    meaning = "[PII — manual review required]"
                    pii_count += 1
                elif prof is not None:
                    meaning = _make_meaning_with_profiling(
                        semantic_type, business_label, business_name, prof
                    )
                else:
                    meaning = _make_meaning(semantic_type, business_label, business_name)

                col_method = 'rule_based+profiling' if prof is not None else 'rule_based'
                if prof is not None:
                    profiling_used = True

                column_entries.append(ColumnDictEntry(
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    table_fqn=table.table_fqn,
                    column_name=column.column_name,
                    business_label=business_label,
                    meaning=meaning,
                    semantic_type=semantic_type,
                    is_metric=col_meta['is_metric'] or (semantic_type == 'metric'),
                    is_dimension=col_meta['is_dimension'] or (semantic_type == 'dimension'),
                    is_date=col_meta['is_date'] or (semantic_type == 'date'),
                    is_id=col_meta['is_id'] or (semantic_type == 'id'),
                    pii_risk=pii_risk,
                    generation_method=col_method,
                ))

    overall_method = 'rule_based+profiling' if profiling_used else 'rule_based'

    return DictionaryResult(
        source_id=source_id,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        generation_method=overall_method,
        pii_column_count=pii_count,
        table_entries=table_entries,
        column_entries=column_entries,
    )
