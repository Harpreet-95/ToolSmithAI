from dataclasses import dataclass, field
from enum import Enum


# ── Enums ──────────────────────────────────────────────────────────────────────

class ProfilingMode(str, Enum):
    FULL            = 'full'
    INCREMENTAL     = 'incremental'
    SAMPLED         = 'sampled'
    STRUCTURAL_ONLY = 'structural_only'


class TableClass(str, Enum):
    REFERENCE     = 'Reference'
    MASTER        = 'Master'
    TRANSACTIONAL = 'Transactional'
    AUDIT         = 'Audit'
    STAGING       = 'Staging'
    REPORTING     = 'Reporting'
    UNKNOWN       = 'Unknown'


class SemanticType(str, Enum):
    EMAIL   = 'EMAIL'
    PHONE   = 'PHONE'
    SSN     = 'SSN'
    ID      = 'ID'
    AMOUNT  = 'AMOUNT'
    COUNT   = 'COUNT'
    DATE    = 'DATE'
    STATUS  = 'STATUS'
    CODE    = 'CODE'
    FLAG    = 'FLAG'
    NAME    = 'NAME'
    TEXT    = 'TEXT'
    BINARY  = 'BINARY'
    UNKNOWN = 'UNKNOWN'


class RowCountTier(str, Enum):
    EMPTY      = 'EMPTY'        # 0 rows
    TINY       = 'TINY'         # 1 – 999
    SMALL      = 'SMALL'        # 1 000 – 99 999
    MEDIUM     = 'MEDIUM'       # 100 000 – 999 999
    LARGE      = 'LARGE'        # 1 000 000 – 9 999 999
    VERY_LARGE = 'VERY_LARGE'   # 10 000 000+


class CardinalityTier(str, Enum):
    UNIQUE   = 'UNIQUE'     # distinct_count == populated_count
    HIGH     = 'HIGH'       # distinct % > 95
    MEDIUM   = 'MEDIUM'     # 10 < distinct % ≤ 95
    LOW      = 'LOW'        # 2 < distinct % ≤ 10
    CONSTANT = 'CONSTANT'   # 1 distinct value across all rows
    BINARY   = 'BINARY'     # exactly 2 distinct values


class DataCurrency(str, Enum):
    HISTORICAL = 'HISTORICAL'   # latest record > 180 days ago
    RECENT     = 'RECENT'       # latest record 30 – 180 days ago
    ACTIVE     = 'ACTIVE'       # latest record < 30 days ago
    UNKNOWN    = 'UNKNOWN'      # no date column detected or empty table


class ProfilingDepth(str, Enum):
    FULL            = 'FULL'            # all metrics incl. value distributions
    SAMPLED         = 'SAMPLED'         # statistical on % sample
    STATISTICAL     = 'STATISTICAL'     # full COUNT queries, no value sampling
    STRUCTURAL_ONLY = 'STRUCTURAL_ONLY' # schema only; zero queries against source
    SKIPPED         = 'SKIPPED'         # excluded; no profiling at any level


class ProfilingStatus(str, Enum):
    PENDING   = 'PENDING'
    RUNNING   = 'RUNNING'
    COMPLETE  = 'COMPLETE'
    PARTIAL   = 'PARTIAL'
    TIMED_OUT = 'TIMED_OUT'
    FAILED    = 'FAILED'
    SKIPPED   = 'SKIPPED'


# ── ProfilingConfig ────────────────────────────────────────────────────────────

@dataclass
class ProfilingConfig:
    mode:                ProfilingMode = ProfilingMode.FULL
    sample_rate:         float         = 1.0           # 1.0 = full scan
    max_top_values:      int           = 20
    max_sample_values:   int           = 10
    row_limit_for_full:  int           = 1_000_000     # tables above this → SAMPLED
    timeout_per_table_s: int           = 60
    timeout_per_col_s:   int           = 30
    max_column_count:    int           = 300            # above this → STRUCTURAL_ONLY
    max_tables:          int           = 0              # 0 = unlimited; N > 0 = only first N eligible tables receive live statistical profiling
    excluded_schemas:    list[str]     = field(default_factory=list)
    excluded_prefixes:   list[str]     = field(default_factory=list)
    excluded_table_fqns: list[str]     = field(default_factory=list)
    priority_tables:     list[str]     = field(default_factory=list)


# ── ProfilingBatchState ────────────────────────────────────────────────────────

@dataclass
class ProfilingBatchState:
    """Tracks progress of a multi-batch profiling run across API calls."""
    profiling_snapshot_id:       int | None      = None
    next_table_index:            int             = 0
    total_tables:                int             = 0
    completed_tables:            int             = 0
    statistical_tables_completed: int            = 0
    structural_tables_completed: int             = 0
    status:                      ProfilingStatus = ProfilingStatus.PENDING


# ── ConfidenceScore ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConfidenceScore:
    classification: str
    confidence:     float               # 0.0–1.0
    evidence:       tuple[str, ...]     # ordered highest to lowest contribution
    rule_version:   str                 # semver of classification rules applied
    competing:      tuple[dict, ...]    # [{classification, confidence}] runner-ups


# ── ColumnProfile ──────────────────────────────────────────────────────────────

@dataclass
class ColumnProfile:
    # Identity — always required
    source_id:             int
    profiling_snapshot_id: int
    table_fqn:             str
    column_name:           str

    # From schema snapshot — always populated (zero query cost)
    data_type:             str
    raw_type:              str
    is_nullable:           bool
    is_primary_key:        bool
    is_identity:           bool
    ordinal_position:      int

    # Existence metrics — STATISTICAL depth and above
    null_count:            int | None   = None
    null_percentage:       float | None = None
    populated_count:       int | None   = None
    populated_percentage:  float | None = None
    empty_string_count:    int | None   = None  # TEXT: '' is distinct from NULL
    zero_count:            int | None   = None  # NUMERIC: 0 is distinct from NULL

    # Cardinality — STATISTICAL depth and above
    distinct_count:        int | None              = None
    distinct_percentage:   float | None            = None
    uniqueness_score:      float | None            = None  # distinct / populated
    cardinality_tier:      CardinalityTier | None  = None

    # Value range — STATISTICAL depth and above
    min_value:             str | None   = None   # stored as string, type-aware at display
    max_value:             str | None   = None
    min_length:            int | None   = None   # TEXT columns
    max_length_observed:   int | None   = None
    avg_length:            float | None = None
    mean_value:            float | None = None   # NUMERIC only
    std_deviation:         float | None = None   # NUMERIC only
    p5_value:              str | None   = None
    p25_value:             str | None   = None
    p50_value:             str | None   = None   # median
    p75_value:             str | None   = None
    p95_value:             str | None   = None

    # Derived numeric stats — computed in-memory; blank_percentage is stored, variance is not
    blank_percentage:      float | None = None   # TEXT: empty_string_count / total_rows * 100
    variance:              float | None = None   # NUMERIC: std_deviation ** 2; not persisted

    # Pattern detection — FULL depth only
    dominant_pattern:      str | None   = None
    pattern_coverage:      float | None = None
    email_match_rate:      float | None = None
    phone_match_rate:      float | None = None
    guid_match_rate:       float | None = None
    date_string_rate:      float | None = None  # TEXT columns storing dates as strings
    numeric_string_rate:   float | None = None  # TEXT columns storing numbers
    masked_value_rate:     float | None = None  # % of rows with masked values (****)

    # Semantic classification — from ClassificationEngine
    semantic_type:         SemanticType | None = None
    semantic_confidence:   float | None        = None
    semantic_evidence:     list[str]           = field(default_factory=list)

    # PII
    pii_name_heuristic:    bool       = False  # from existing pii_detector.detect_pii()
    pii_confirmed:         bool       = False  # confirmed by profiling pattern rates
    pii_signals:           list[str]  = field(default_factory=list)

    # Value samples — always empty for PII columns
    top_values:           list[dict]  = field(default_factory=list)  # [{value, count, percentage}]
    sample_values:        list[str]   = field(default_factory=list)
    top_values_coverage:  float | None = None

    # Execution metadata
    profiling_depth:       ProfilingDepth  = ProfilingDepth.STRUCTURAL_ONLY
    profiling_duration_ms: int | None      = None
    profiling_status:      ProfilingStatus = ProfilingStatus.PENDING


# ── TableProfile ───────────────────────────────────────────────────────────────

@dataclass
class TableProfile:
    # Identity — always required
    source_id:             int
    profiling_snapshot_id: int
    table_fqn:             str
    table_name:            str
    schema_name:           str
    table_type:            str  # TABLE | VIEW

    # Row counts — STATISTICAL depth and above
    exact_row_count:       int | None         = None
    estimated_row_count:   int | None         = None  # from sys.partitions / pg_class
    row_count_tier:        RowCountTier | None = None

    # Temporal signals — populated when a date column is detected
    has_date_column:       bool         = False
    date_column_name:      str | None   = None
    earliest_record:       str | None   = None   # ISO-8601
    latest_record:         str | None   = None   # ISO-8601
    data_span_days:        int | None   = None
    data_currency:         DataCurrency = DataCurrency.UNKNOWN

    # Structural metrics — from schema snapshot; always present, zero cost
    column_count:          int  = 0
    pk_column_count:       int  = 0
    fk_count:              int  = 0   # outbound: this table → others
    referenced_by_count:   int  = 0   # inbound: other tables → this
    is_junction_table:     bool = False
    is_root_table:         bool = False
    is_leaf_table:         bool = False
    has_identity_column:   bool = False

    # Aggregate quality — derived from column_profiles after they complete
    avg_null_percentage:   float | None = None
    completeness_score:    float | None = None  # 0.0–1.0

    # Classification — set by ClassificationEngine
    classification:        ConfidenceScore | None = None

    # PII summary — derived from column_profiles
    pii_column_count:      int = 0
    confirmed_pii_count:   int = 0

    # Column profiles — populated as profiling executes
    column_profiles:       list[ColumnProfile] = field(default_factory=list)

    # Execution metadata
    profiling_depth:       ProfilingDepth  = ProfilingDepth.STRUCTURAL_ONLY
    profiling_duration_ms: int | None      = None
    profiling_status:      ProfilingStatus = ProfilingStatus.PENDING
    skip_reason:           str | None      = None
    profiled_at:           str | None      = None  # ISO-8601


# ── ProfilingSnapshot ──────────────────────────────────────────────────────────

@dataclass
class ProfilingSnapshot:
    # Identity — required at construction
    source_id:               int
    schema_snapshot_id:      int
    snapshot_version:        int
    mode:                    ProfilingMode
    sample_rate:             float
    profiling_rules_version: str

    # Progress counters — updated as profiling executes
    status:                   ProfilingStatus = ProfilingStatus.PENDING
    tables_total:             int             = 0
    tables_profiled:          int             = 0
    tables_skipped:           int             = 0
    tables_failed:            int             = 0
    tables_timed_out:         int             = 0
    columns_total:            int             = 0
    columns_profiled:         int             = 0
    columns_skipped:          int             = 0
    total_rows_profiled:      int             = 0
    pii_columns_found:        int             = 0
    classifications_complete: int             = 0

    # Timing
    started_at:            str | None = None   # ISO-8601
    completed_at:          str | None = None   # ISO-8601
    duration_seconds:      int | None = None

    # Checkpoint for resuming interrupted runs — serialised as JSON string
    resumable_state_json:  str | None = None

    # Assigned after DB insert
    id:                    int | None = None
