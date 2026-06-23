from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnInfo:
    column_name: str
    ordinal_position: int
    data_type: str            # normalized: TEXT | INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY | JSON | OTHER
    raw_type: str             # dialect-native: nvarchar(255), int, datetime2, etc.
    is_nullable: bool
    is_primary_key: bool      # denormalized copy — set during table assembly for quick downstream access
    is_identity: bool         # IDENTITY / AUTO_INCREMENT / SERIAL
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    default_value: str | None = None


@dataclass(frozen=True)
class PrimaryKeyInfo:
    column_name: str
    key_ordinal: int


@dataclass(frozen=True)
class ForeignKeyInfo:
    fk_name: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str


@dataclass
class TableInfo:
    table_name: str
    schema_name: str
    table_fqn: str            # "schema_name.table_name" — stable identifier for join path resolution
    table_type: str           # TABLE | VIEW
    row_count_estimate: int | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_keys: list[PrimaryKeyInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)


@dataclass
class SchemaInfo:
    schema_name: str
    tables: list[TableInfo] = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    source_id: int
    source_type: str
    discovered_at: str                   # ISO-8601
    schemas: list[SchemaInfo] = field(default_factory=list)
    database_name: str | None = None
    server_name: str | None = None
    connector_version: str | None = None
    discovery_duration_ms: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def table_count(self) -> int:
        return sum(1 for s in self.schemas for t in s.tables if t.table_type == 'TABLE')

    @property
    def view_count(self) -> int:
        return sum(1 for s in self.schemas for t in s.tables if t.table_type == 'VIEW')

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for s in self.schemas for t in s.tables)


_TYPE_MAP: dict[str, str] = {
    # INTEGER
    'int': 'INTEGER', 'bigint': 'INTEGER', 'smallint': 'INTEGER',
    'tinyint': 'INTEGER', 'integer': 'INTEGER',
    # DECIMAL
    'decimal': 'DECIMAL', 'numeric': 'DECIMAL', 'float': 'DECIMAL',
    'real': 'DECIMAL', 'money': 'DECIMAL', 'smallmoney': 'DECIMAL',
    'double precision': 'DECIMAL', 'double': 'DECIMAL',
    # TEXT
    'char': 'TEXT', 'varchar': 'TEXT', 'nchar': 'TEXT', 'nvarchar': 'TEXT',
    'text': 'TEXT', 'ntext': 'TEXT', 'sysname': 'TEXT',
    'uniqueidentifier': 'TEXT', 'character varying': 'TEXT', 'character': 'TEXT',
    'clob': 'TEXT', 'tinytext': 'TEXT', 'mediumtext': 'TEXT', 'longtext': 'TEXT',
    # DATETIME
    'date': 'DATETIME', 'datetime': 'DATETIME', 'datetime2': 'DATETIME',
    'datetimeoffset': 'DATETIME', 'smalldatetime': 'DATETIME', 'time': 'DATETIME',
    'timestamp': 'DATETIME', 'timestamp without time zone': 'DATETIME',
    'timestamp with time zone': 'DATETIME', 'interval': 'DATETIME',
    # BOOLEAN
    'bit': 'BOOLEAN', 'bool': 'BOOLEAN', 'boolean': 'BOOLEAN',
    # BINARY
    'binary': 'BINARY', 'varbinary': 'BINARY', 'image': 'BINARY',
    'bytea': 'BINARY', 'blob': 'BINARY', 'tinyblob': 'BINARY',
    'mediumblob': 'BINARY', 'longblob': 'BINARY',
    # JSON
    'xml': 'JSON', 'json': 'JSON', 'jsonb': 'JSON',
}


def normalize_data_type(raw_type: str) -> str:
    if not raw_type:
        return 'OTHER'
    base = raw_type.strip().lower()
    paren = base.find('(')
    if paren != -1:
        base = base[:paren].rstrip()
    return _TYPE_MAP.get(base, 'OTHER')
