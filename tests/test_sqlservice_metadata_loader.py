"""
Focused unit tests — SQL rebuild Phase 1, Step 2: read-only bulk metadata
loader (core/sqlservice/metadata_loader.py).

Uses a temporary on-disk SQLite fixture DB with only the minimum required
tables — never the real ToolSmithAI database, never SQL Server, never
OpenAI. `get_connection` is monkeypatched per-test to point at the fixture.

Run:
    venv/Scripts/pytest tests/test_sqlservice_metadata_loader.py -v
"""

import ast
import dataclasses
import inspect
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config (via
# core.sqlservice.metadata_loader -> data.db -> core.config). Matches the
# existing convention in tests/test_phase2_schema_discovery.py.
import os
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import core.sqlservice.metadata_loader as metadata_loader_module
from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.sqlservice.metadata_loader import (
    RetrievalDocumentBatch,
    STATUS_DISCOVERY_FAILED,
    STATUS_INVALID_SNAPSHOT,
    STATUS_NO_SNAPSHOT,
    STATUS_READY,
    STATUS_UNOWNED,
    _reconstruct_schema_snapshot,
    load_retrieval_document_batch,
)


# ── Fixture DB: minimum required tables only ─────────────────────────────

_DDL = """
CREATE TABLE data_source_connections (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    encrypted_config_json TEXT
);
CREATE TABLE schema_snapshots (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE data_dictionary_tables (
    source_id INTEGER NOT NULL,
    table_fqn TEXT NOT NULL,
    business_name TEXT,
    description TEXT,
    domain TEXT
);
CREATE TABLE data_dictionary_columns (
    source_id INTEGER NOT NULL,
    table_fqn TEXT NOT NULL,
    column_name TEXT NOT NULL,
    business_label TEXT,
    meaning TEXT,
    semantic_type TEXT,
    is_metric INTEGER,
    is_date INTEGER
);
CREATE TABLE generated_business_vocabulary (
    source_id INTEGER NOT NULL,
    term TEXT NOT NULL,
    table_fqn TEXT NOT NULL,
    column_name TEXT,
    confidence_tier TEXT,
    confidence_score REAL
);
CREATE TABLE table_relationships (
    source_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    from_schema TEXT,
    from_table TEXT,
    from_table_fqn TEXT,
    from_column TEXT,
    to_schema TEXT,
    to_table TEXT,
    to_table_fqn TEXT,
    to_column TEXT,
    relationship_name TEXT,
    relationship_type TEXT,
    confidence REAL,
    relationship_status TEXT,
    inference_method TEXT
);
"""


def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_DDL)
        conn.commit()
    finally:
        conn.close()


def _insert_source(db_path, source_id=1, user_id="alice", encrypted_config_json=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO data_source_connections (id, user_id, encrypted_config_json) VALUES (?, ?, ?)",
            (source_id, user_id, encrypted_config_json),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_snapshot(db_path, *, snapshot_id, source_id, version, snapshot_json_text):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO schema_snapshots (id, source_id, snapshot_version, snapshot_json) "
            "VALUES (?, ?, ?, ?)",
            (snapshot_id, source_id, version, snapshot_json_text),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_dictionary_table(db_path, source_id, table_fqn, business_name=None, description=None, domain=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO data_dictionary_tables (source_id, table_fqn, business_name, description, domain) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, table_fqn, business_name, description, domain),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_dictionary_column(db_path, source_id, table_fqn, column_name, **kwargs):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, table_fqn, column_name, business_label, meaning, semantic_type, is_metric, is_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id, table_fqn, column_name,
                kwargs.get("business_label"), kwargs.get("meaning"), kwargs.get("semantic_type"),
                kwargs.get("is_metric", 0), kwargs.get("is_date", 0),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_vocabulary(db_path, source_id, term, table_fqn, column_name="", confidence_tier="HIGH", confidence_score=0.9):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO generated_business_vocabulary "
            "(source_id, term, table_fqn, column_name, confidence_tier, confidence_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, term, table_fqn, column_name, confidence_tier, confidence_score),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_relationship(db_path, source_id, snapshot_id, **kwargs):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
            " to_schema, to_table, to_table_fqn, to_column, relationship_name, "
            " relationship_type, confidence, relationship_status, inference_method) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id, snapshot_id,
                kwargs.get("from_schema", "dbo"), kwargs.get("from_table"),
                kwargs.get("from_table_fqn"), kwargs.get("from_column"),
                kwargs.get("to_schema", "dbo"), kwargs.get("to_table"),
                kwargs.get("to_table_fqn"), kwargs.get("to_column"),
                kwargs.get("relationship_name"), kwargs.get("relationship_type"),
                kwargs.get("confidence"), kwargs.get("relationship_status"),
                kwargs.get("inference_method"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _col(name, data_type, *, pk=False, identity=False, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=True, is_primary_key=pk, is_identity=identity,
    )


def _table(schema_name, table_name, columns, *, table_type="TABLE", primary_keys=None, foreign_keys=None):
    return TableInfo(
        table_name=table_name, schema_name=schema_name, table_fqn=f"{schema_name}.{table_name}",
        table_type=table_type, columns=columns,
        primary_keys=primary_keys or [], foreign_keys=foreign_keys or [],
    )


def _snapshot_json(tables, schema_name="dbo", source_id=1):
    snapshot = SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at="2026-09-04T00:00:00Z",
        schemas=[SchemaInfo(schema_name=schema_name, tables=tables)],
    )
    return json.dumps(dataclasses.asdict(snapshot))


class _TrackingConnection:
    """Wraps a real sqlite3.Connection to count execute() calls and record close()."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.execute_count = 0
        self.closed = False

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._real.execute(*args, **kwargs)

    def close(self):
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _patch_get_connection(monkeypatch, db_path, created: list):
    def _fake_get_connection():
        real = sqlite3.connect(db_path)
        real.row_factory = sqlite3.Row
        wrapper = _TrackingConnection(real)
        created.append(wrapper)
        return wrapper

    monkeypatch.setattr(metadata_loader_module, "get_connection", _fake_get_connection)
    return created


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "fixture.db")
    _init_db(path)
    return path


# ── 1-3. Status behavior: READY / UNOWNED / NO_SNAPSHOT ──────────────────

def test_owned_source_with_valid_snapshot_returns_ready(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_READY
    assert batch.snapshot_id == 1
    assert batch.snapshot_version == 1
    assert len(batch.documents) == 1
    assert batch.documents[0].object_fqn == "dbo.learners"


def test_unowned_source_returns_unowned(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _patch_get_connection(monkeypatch, db_path, [])

    # Wrong user.
    batch_wrong_user = load_retrieval_document_batch(1, "mallory")
    assert batch_wrong_user.status == STATUS_UNOWNED
    assert batch_wrong_user.documents == ()
    assert batch_wrong_user.snapshot_id is None

    # Nonexistent source.
    batch_missing = load_retrieval_document_batch(999, "alice")
    assert batch_missing.status == STATUS_UNOWNED


def test_owned_source_without_snapshot_returns_no_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_NO_SNAPSHOT
    assert batch.documents == ()
    assert batch.snapshot_id is None


# ── 4-5. Invalid snapshot handling ────────────────────────────────────────

def test_malformed_json_returns_invalid_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text="{not valid json")
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT
    assert batch.documents == ()
    assert batch.snapshot_id == 1
    assert len(batch.warnings) == 1


@pytest.mark.parametrize(
    "bad_payload",
    [
        json.dumps({"schemas": "not-a-list"}),
        json.dumps({"schemas": [{"schema_name": "dbo", "tables": "not-a-list"}]}),
        json.dumps({"schemas": [{"schema_name": "dbo", "tables": [{"table_name": "x"}]}]}),  # missing table_fqn
        json.dumps({"schemas": [{"tables": []}]}),  # missing schema_name
        json.dumps([1, 2, 3]),  # top-level not an object
        json.dumps({"schemas": [{"schema_name": "dbo", "tables": [
            {"table_name": "x", "table_fqn": "dbo.x", "table_type": "TABLE",
             "columns": [{"column_name": "id"}]},  # missing required ColumnInfo fields
        ]}]}),
    ],
)
def test_structurally_malformed_valid_json_returns_invalid_snapshot(db_path, monkeypatch, bad_payload):
    _insert_source(db_path, source_id=1, user_id="alice")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=bad_payload)
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT, bad_payload
    assert batch.documents == ()
    assert len(batch.warnings) == 1


# ── 6. Tables and views reconstruct correctly ────────────────────────────

def test_tables_and_views_reconstruct_correctly(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    view = _table("dbo", "active_learners", [_col("id", "INTEGER", pk=True)], table_type="VIEW", primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table, view]))
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    by_fqn = {d.object_fqn: d for d in batch.documents}
    assert by_fqn["dbo.learners"].object_type == "TABLE"
    assert by_fqn["dbo.active_learners"].object_type == "VIEW"
    assert batch.counts["tables"] == 1
    assert batch.counts["views"] == 1
    assert batch.counts["documents"] == 2


# ── 7. Dictionary enrichment ──────────────────────────────────────────────

def test_dictionary_tables_and_columns_enrich_documents(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table(
        "dbo", "learners",
        [_col("id", "INTEGER", pk=True), _col("email", "TEXT")],
        primary_keys=[PrimaryKeyInfo("id", 1)],
    )
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _insert_dictionary_table(db_path, 1, "dbo.learners", business_name="Learner", description="Enrolled individual.")
    _insert_dictionary_column(db_path, 1, "dbo.learners", "email", business_label="Email Address", meaning="Contact email.")
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    doc = batch.documents[0]
    assert "BUSINESS_NAME: Learner" in doc.document_text
    assert "DESCRIPTION: Enrolled individual." in doc.document_text
    assert "Email Address" in doc.document_text
    assert batch.counts["dictionary_tables"] == 1
    assert batch.counts["dictionary_columns"] == 1


# ── 8. Vocabulary grouping ─────────────────────────────────────────────────

def test_vocabulary_is_grouped_into_builders_expected_shape(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _insert_vocabulary(db_path, 1, "trainee", "dbo.learners")
    _insert_vocabulary(db_path, 1, "attendee", "dbo.learners")
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    doc = batch.documents[0]
    assert "trainee" in doc.document_text
    assert "attendee" in doc.document_text
    assert doc.metadata["synonym_count"] == 2
    assert batch.counts["vocabulary_rows"] == 2


# ── 9. Declared vs. inferred relationship classification ────────────────

def test_declared_and_inferred_relationships_retain_correct_classification(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    source_table = _table(
        "dbo", "enrollments",
        [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER"), _col("mentor_ref", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)],
    )
    target_table = _table("dbo", "tracks", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    staff_table = _table("dbo", "staff", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(
        db_path, snapshot_id=1, source_id=1, version=1,
        snapshot_json_text=_snapshot_json([source_table, target_table, staff_table]),
    )
    _insert_relationship(
        db_path, 1, 1,
        from_table="enrollments", from_table_fqn="dbo.enrollments", from_column="track_id",
        to_table="tracks", to_table_fqn="dbo.tracks", to_column="id",
        relationship_name="fk_track", relationship_type="FOREIGN_KEY", confidence=1.0,
    )
    _insert_relationship(
        db_path, 1, 1,
        from_table="enrollments", from_table_fqn="dbo.enrollments", from_column="mentor_ref",
        to_table="staff", to_table_fqn="dbo.staff", to_column="id",
        relationship_name="inferred_name_match", relationship_type="INFERRED_NAME_MATCH", confidence=0.7,
    )
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    source_doc = next(d for d in batch.documents if d.object_fqn == "dbo.enrollments")
    assert "dbo.tracks.id" in source_doc.metadata["confirmed_relationship_targets"]
    assert "dbo.staff.id" in source_doc.metadata["inferred_relationship_targets"]
    assert "dbo.staff.id" not in source_doc.metadata["confirmed_relationship_targets"]
    assert batch.counts["confirmed_relationship_edges"] == 1
    assert batch.counts["inferred_relationship_edges"] == 1


# ── 10. Relationships restricted to the selected snapshot ───────────────

def test_relationships_restricted_to_selected_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    source_table = _table(
        "dbo", "enrollments", [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)],
    )
    target_table = _table("dbo", "tracks", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot_text = _snapshot_json([source_table, target_table])

    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=snapshot_text)
    _insert_snapshot(db_path, snapshot_id=2, source_id=1, version=2, snapshot_json_text=snapshot_text)

    # Old-snapshot relationship — must NOT appear in a batch built from the latest snapshot.
    _insert_relationship(
        db_path, 1, 1,
        from_table="enrollments", from_table_fqn="dbo.enrollments", from_column="track_id",
        to_table="tracks", to_table_fqn="dbo.tracks", to_column="id",
        relationship_type="FOREIGN_KEY", confidence=1.0,
    )
    # Latest-snapshot relationship — must appear.
    _insert_relationship(
        db_path, 1, 2,
        from_table="enrollments", from_table_fqn="dbo.enrollments", from_column="track_id",
        to_table="tracks", to_table_fqn="dbo.tracks", to_column="id",
        relationship_type="INFERRED_VALUE_OVERLAP", confidence=0.8,
    )
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.snapshot_id == 2
    source_doc = next(d for d in batch.documents if d.object_fqn == "dbo.enrollments")
    # Only the latest-snapshot row is present, and it is INFERRED_VALUE_OVERLAP
    # (not FOREIGN_KEY), so it must show up as inferred, not confirmed.
    assert "dbo.tracks.id" in source_doc.metadata["inferred_relationship_targets"]
    assert "dbo.tracks.id" not in source_doc.metadata["confirmed_relationship_targets"]


# ── 11. Sparse optional metadata still produces documents ───────────────

def test_sparse_optional_metadata_still_produces_documents(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    # No dictionary/vocabulary/relationship rows inserted at all.
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_READY
    assert len(batch.documents) == 1
    assert batch.counts["dictionary_tables"] == 0
    assert batch.counts["vocabulary_rows"] == 0
    assert batch.counts["confirmed_relationship_edges"] == 0


# ── 12. Immutability ──────────────────────────────────────────────────────

def test_returned_documents_and_counts_are_immutable(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert isinstance(batch.documents, tuple)
    assert isinstance(batch.warnings, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        batch.status = "READY_HACKED"
    with pytest.raises(TypeError):
        batch.counts["tables"] = 999
    with pytest.raises(AttributeError):
        batch.documents.append("not allowed")


# ── 13-14. Connection/query-count discipline ─────────────────────────────

def test_loader_uses_exactly_one_sqlite_connection(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    created = _patch_get_connection(monkeypatch, db_path, [])

    load_retrieval_document_batch(1, "alice")

    assert len(created) == 1


def test_query_count_is_constant_as_object_count_grows(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")

    small_tables = [
        _table("dbo", f"t{i}", [_col("id", "INTEGER", pk=True), _col("val", "TEXT")], primary_keys=[PrimaryKeyInfo("id", 1)])
        for i in range(3)
    ]
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json(small_tables))
    created_small = _patch_get_connection(monkeypatch, db_path, [])
    load_retrieval_document_batch(1, "alice")
    small_count = created_small[0].execute_count

    # A second, much larger source sharing the same fixture DB.
    _insert_source(db_path, source_id=2, user_id="alice")
    large_tables = [
        _table("dbo", f"big_t{i}", [_col("id", "INTEGER", pk=True), _col("val", "TEXT"), _col("amount", "DECIMAL")], primary_keys=[PrimaryKeyInfo("id", 1)])
        for i in range(60)
    ]
    _insert_snapshot(db_path, snapshot_id=2, source_id=2, version=1, snapshot_json_text=_snapshot_json(large_tables, source_id=2))
    created_large = _patch_get_connection(monkeypatch, db_path, [])
    load_retrieval_document_batch(2, "alice")
    large_count = created_large[0].execute_count

    assert small_count == large_count
    assert small_count == 6  # ownership + snapshot + dict tables + dict columns + vocabulary + relationships


# ── 15. No live SQL connector / OpenAI client ────────────────────────────

def test_no_live_sql_connector_or_openai_client_referenced():
    # Inspect actual import statements via ast, not raw text search — the
    # module's own docstring legitimately says "never calls OpenAI" in
    # prose, which a naive substring check would misfire on.
    tree = ast.parse(inspect.getsource(metadata_loader_module))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "openai", "pyodbc", "psycopg2", "pymysql",
        "core.connectors.relational", "core.connectors.base",
    )
    for module_name in imported_modules:
        for forbidden in forbidden_prefixes:
            assert not module_name.startswith(forbidden), (module_name, forbidden)


# ── 16-17. Connection lifecycle ──────────────────────────────────────────

def test_connection_closes_on_success(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    created = _patch_get_connection(monkeypatch, db_path, [])

    load_retrieval_document_batch(1, "alice")

    assert created[0].closed is True


def test_connection_closes_after_malformed_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text="{not valid json")
    created = _patch_get_connection(monkeypatch, db_path, [])

    load_retrieval_document_batch(1, "alice")

    assert created[0].closed is True


def test_connection_closes_after_query_failure(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    # Drop a table the loader queries later, so the relationships SELECT fails mid-way.
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE table_relationships")
    conn.commit()
    conn.close()
    created = _patch_get_connection(monkeypatch, db_path, [])

    with pytest.raises(sqlite3.OperationalError):
        load_retrieval_document_batch(1, "alice")

    assert created[0].closed is True


# ── 18. No credentials/secrets/sample values leak into output ───────────

def test_no_credentials_or_secrets_enter_documents_or_warnings(db_path, monkeypatch):
    secret = "SECRET_ENCRYPTED_CONNECTION_STRING_DO_NOT_LEAK"
    _insert_source(db_path, source_id=1, user_id="alice", encrypted_config_json=secret)
    table = _table(
        "dbo", "learners",
        [_col("id", "INTEGER", pk=True), _col("email", "TEXT")],
        primary_keys=[PrimaryKeyInfo("id", 1)],
    )
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=_snapshot_json([table]))
    _insert_dictionary_column(db_path, 1, "dbo.learners", "email", business_label="Email Address", meaning="Contact email.")
    _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    for doc in batch.documents:
        assert secret not in doc.document_text
    assert all(secret not in w for w in batch.warnings)


# ── Step 2 follow-up: discovery_succeeded / warnings / object identity ──
#
# `_reconstruct_schema_snapshot` is exercised directly for the precise
# validation-rule proofs below (fast, no DB round trip needed), and
# `load_retrieval_document_batch` end-to-end for the user-visible status
# and connection-lifecycle proofs.

_UNSET = object()


def _minimal_snapshot_payload(
    *,
    schema_name="dbo",
    table_name="learners",
    table_fqn=None,
    table_schema_name=_UNSET,
    discovery_succeeded=_UNSET,
    warnings=_UNSET,
):
    """Build a raw (dict, not dataclass-derived) snapshot payload with full
    control over the fields under test — including presence/absence of
    keys, which dataclasses.asdict() round-tripping can't express."""
    if table_fqn is None:
        table_fqn = f"{schema_name}.{table_name}"

    table = {
        "table_name": table_name,
        "table_fqn": table_fqn,
        "table_type": "TABLE",
        "row_count_estimate": None,
        "columns": [
            {
                "column_name": "id", "ordinal_position": 1, "data_type": "INTEGER",
                "raw_type": "int", "is_nullable": False, "is_primary_key": True,
                "is_identity": True, "max_length": None, "precision": None,
                "scale": None, "default_value": None,
            }
        ],
        "primary_keys": [{"column_name": "id", "key_ordinal": 1}],
        "foreign_keys": [],
    }
    if table_schema_name is not _UNSET:
        table["schema_name"] = table_schema_name

    payload = {
        "source_type": "mssql",
        "discovered_at": "2026-09-04T00:00:00Z",
        "schemas": [{"schema_name": schema_name, "tables": [table]}],
        "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None,
    }
    if discovery_succeeded is not _UNSET:
        payload["discovery_succeeded"] = discovery_succeeded
    if warnings is not _UNSET:
        payload["warnings"] = warnings
    return payload


# -- discovery_succeeded: unit-level proofs on _reconstruct_schema_snapshot --

def test_missing_discovery_succeeded_defaults_true():
    payload = _minimal_snapshot_payload()
    assert "discovery_succeeded" not in payload
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    assert snapshot.discovery_succeeded is True


def test_explicit_discovery_succeeded_true_is_accepted():
    payload = _minimal_snapshot_payload(discovery_succeeded=True)
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    assert snapshot.discovery_succeeded is True


def test_explicit_discovery_succeeded_false_is_accepted():
    payload = _minimal_snapshot_payload(discovery_succeeded=False)
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    assert snapshot.discovery_succeeded is False


@pytest.mark.parametrize("bad_value", ["false", 0, 1, None])
def test_non_boolean_discovery_succeeded_is_rejected(bad_value):
    payload = _minimal_snapshot_payload(discovery_succeeded=bad_value)
    with pytest.raises(ValueError):
        _reconstruct_schema_snapshot(payload, source_id=1)


# -- warnings: unit-level proofs --

def test_warnings_list_of_strings_reconstructs_correctly():
    payload = _minimal_snapshot_payload(warnings=["disk was slow", "one table skipped"])
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    assert snapshot.warnings == ["disk was slow", "one table skipped"]


def test_missing_or_null_warnings_becomes_empty_list():
    snapshot_missing = _reconstruct_schema_snapshot(_minimal_snapshot_payload(), source_id=1)
    assert snapshot_missing.warnings == []
    snapshot_null = _reconstruct_schema_snapshot(_minimal_snapshot_payload(warnings=None), source_id=1)
    assert snapshot_null.warnings == []


def test_string_warnings_value_is_rejected_not_exploded_into_characters():
    payload = _minimal_snapshot_payload(warnings="oops")
    with pytest.raises(ValueError):
        _reconstruct_schema_snapshot(payload, source_id=1)


def test_mixed_type_warnings_list_is_rejected():
    payload = _minimal_snapshot_payload(warnings=["a real warning", 42, None])
    with pytest.raises(ValueError):
        _reconstruct_schema_snapshot(payload, source_id=1)


# -- object identity (schema_name / table_fqn): unit-level proofs --

def test_matching_fqn_is_accepted():
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="learners", table_fqn="dbo.learners")
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    table = snapshot.schemas[0].tables[0]
    assert table.table_fqn == "dbo.learners"
    assert table.schema_name == "dbo"


def test_case_only_fqn_difference_is_normalized_and_accepted():
    # documented rule: _ci_equal — case-only differences are normalized
    # away, and the ORIGINAL stored spelling is preserved verbatim.
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="Learners", table_fqn="DBO.Learners")
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    table = snapshot.schemas[0].tables[0]
    assert table.table_fqn == "DBO.Learners"  # preserved exactly as stored, not rewritten


def test_substantive_fqn_mismatch_is_rejected():
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="learners", table_fqn="dbo.completely_different_table")
    with pytest.raises(ValueError):
        _reconstruct_schema_snapshot(payload, source_id=1)


def test_table_schema_name_conflicting_with_containing_schema_is_rejected():
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="learners", table_schema_name="sales")
    with pytest.raises(ValueError):
        _reconstruct_schema_snapshot(payload, source_id=1)


def test_table_schema_name_case_only_difference_is_accepted():
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="learners", table_schema_name="DBO")
    snapshot = _reconstruct_schema_snapshot(payload, source_id=1)
    table = snapshot.schemas[0].tables[0]
    # The table's own (case-different) spelling is preserved verbatim.
    assert table.schema_name == "DBO"


# -- end-to-end: status mapping and connection lifecycle --

def test_discovery_failed_snapshot_returns_discovery_failed_status(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(discovery_succeeded=False)
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_DISCOVERY_FAILED
    assert batch.documents == ()
    assert batch.counts == {}
    with pytest.raises(TypeError):
        batch.counts["tables"] = 1
    assert len(batch.warnings) == 1
    assert created[0].closed is True


def test_non_boolean_discovery_succeeded_returns_invalid_snapshot_end_to_end(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(discovery_succeeded="false")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT
    assert created[0].closed is True


def test_case_only_fqn_difference_still_reaches_ready_end_to_end(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(schema_name="dbo", table_name="Learners", table_fqn="DBO.Learners")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_READY
    assert len(batch.documents) == 1
    assert created[0].closed is True


def test_substantive_fqn_mismatch_returns_invalid_snapshot_end_to_end(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(table_fqn="dbo.completely_different_table")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT
    assert created[0].closed is True


def test_table_schema_name_conflict_returns_invalid_snapshot_end_to_end(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(table_schema_name="sales")
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT
    assert created[0].closed is True


def test_mixed_type_warnings_returns_invalid_snapshot_end_to_end(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    payload = _minimal_snapshot_payload(warnings=["real warning", 42])
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1, snapshot_json_text=json.dumps(payload))
    created = _patch_get_connection(monkeypatch, db_path, [])

    batch = load_retrieval_document_batch(1, "alice")

    assert batch.status == STATUS_INVALID_SNAPSHOT
    assert created[0].closed is True
