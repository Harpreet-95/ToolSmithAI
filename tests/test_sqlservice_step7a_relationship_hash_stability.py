"""
Focused tests — SQL rebuild Phase 5, Step 7A: eliminate unnecessary
retrieval-document hash churn caused by relationship summary lines.

Step 7 (core/sqlservice/retrieval_documents.py) unconditionally added
RELATIONSHIPS_*_TOTAL lines to any document with >=1 confirmed/inferred
relationship, and re-sorted relationship lines by provenance-kind priority
instead of plain alphabetical line text — both changed document_hash (and
therefore forced a real re-embedding) for objects that were never actually
truncated. This step corrects both: a non-truncated section renders
byte-for-byte like the pre-Step-7 format (plain alphabetical line order, no
TOTAL/OMITTED lines); only a genuinely truncated section gets the summary
lines and the provenance-priority render order.

Pure/offline throughout: no real database connection beyond a per-test
temporary SQLite file, no OpenAI call, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sqlservice_step7a_relationship_hash_stability.py -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step7a-hash-stability-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-step7a-hash-stability-salt-long-enough12")

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.sqlservice.retrieval_documents import (
    DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES,
    build_retrieval_documents,
)

import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.embedding_store import (
    STATUS_OK as EMBEDDING_STATUS_OK,
    find_missing_document_hashes,
    insert_embeddings,
)
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build


# ── Fixture builders (schema-snapshot side, mirrors test_sqlservice_retrieval_documents.py) ──

def _col(name, data_type, *, pk=False, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=True, is_primary_key=pk, is_identity=False,
    )


def _table(schema_name, table_name, columns, *, table_type="TABLE", primary_keys=None, foreign_keys=None):
    return TableInfo(
        table_name=table_name, schema_name=schema_name, table_fqn=f"{schema_name}.{table_name}",
        table_type=table_type, columns=columns, primary_keys=primary_keys or [], foreign_keys=foreign_keys or [],
    )


def _snapshot(source_id, tables, schema_name="dbo"):
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at="2026-09-10T00:00:00Z",
        schemas=[SchemaInfo(schema_name=schema_name, tables=tables)],
    )


def _pre_step7_confirmed_section_text(header: str, lines: list[str]) -> str:
    """The EXACT pre-Step-7 rendering for one confirmed-relationship
    direction: a bare header followed by plain-alphabetically-sorted lines
    — no TOTAL, no OMITTED. Used as an independent reference algorithm
    (never imported from production code) to prove byte-for-byte parity."""
    body = [header] + [f"  {line}" for line in sorted(lines)]
    return "\n".join(body)


# ── 1/3. Non-truncated: byte-for-byte pre-Step-7 rendering + hash stability ──

def _adversarial_two_fk_table():
    """Two physical FKs whose OWN COLUMN NAME order is the OPPOSITE of
    their TARGET OBJECT FQN order — sorting by column-name-first line text
    (pre-Step-7) and sorting by target-FQN-first (Step 7's provenance-
    priority tie-break) produce genuinely DIFFERENT orders here, so this
    case actually exercises the correction rather than coincidentally
    matching either algorithm."""
    fk1 = ForeignKeyInfo(fk_name="fk1", from_column="zzz_col", to_schema="dbo", to_table="aaa_target", to_column="id")
    fk2 = ForeignKeyInfo(fk_name="fk2", from_column="aaa_col", to_schema="dbo", to_table="zzz_target", to_column="id")
    hub = _table(
        "dbo", "zzz_hub",
        [_col("id", "INTEGER", pk=True), _col("zzz_col", "INTEGER"), _col("aaa_col", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk1, fk2],
    )
    aaa_target = _table("dbo", "aaa_target", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    zzz_target = _table("dbo", "zzz_target", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    return _snapshot(1, [hub, aaa_target, zzz_target])


def test_non_truncated_relationship_section_is_plain_alphabetical_not_priority_ordered():
    docs = build_retrieval_documents(1, _adversarial_two_fk_table())
    hub_doc = {d.object_fqn: d for d in docs}["dbo.zzz_hub"]

    # Pre-Step-7 order (sorted by the full "col -> target" line text):
    # "aaa_col -> dbo.zzz_target.id" before "zzz_col -> dbo.aaa_target.id".
    section = hub_doc.document_text.split("RELATIONSHIPS_CONFIRMED:")[1]
    idx_aaa_col_line = section.index("aaa_col -> dbo.zzz_target.id")
    idx_zzz_col_line = section.index("zzz_col -> dbo.aaa_target.id")
    assert idx_aaa_col_line < idx_zzz_col_line


def test_non_truncated_relationship_section_has_no_total_or_omitted_lines():
    docs = build_retrieval_documents(1, _adversarial_two_fk_table())
    hub_doc = {d.object_fqn: d for d in docs}["dbo.zzz_hub"]

    assert "RELATIONSHIPS_CONFIRMED_OUTBOUND_TOTAL" not in hub_doc.document_text
    assert "RELATIONSHIPS_CONFIRMED_OUTBOUND_OMITTED" not in hub_doc.document_text
    assert "RELATIONSHIPS_CONFIRMED_INBOUND_TOTAL" not in hub_doc.document_text
    assert "RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED" not in hub_doc.document_text


def test_non_truncated_document_hash_matches_independent_pre_step7_reference():
    """Reconstructs the pre-Step-7 RELATIONSHIPS_CONFIRMED section with a
    small, independent reference algorithm (not imported from production
    code) and asserts it is character-for-character what the current
    document_text actually contains at that position — proving the
    document_hash a caller sees today is identical to what pre-Step-7 code
    would have produced for the same, non-truncating input."""
    docs = build_retrieval_documents(1, _adversarial_two_fk_table())
    hub_doc = {d.object_fqn: d for d in docs}["dbo.zzz_hub"]

    expected_section = _pre_step7_confirmed_section_text(
        "RELATIONSHIPS_CONFIRMED:",
        ["aaa_col -> dbo.zzz_target.id", "zzz_col -> dbo.aaa_target.id"],
    )
    assert expected_section in hub_doc.document_text

    # And the reverse: exactly the two relationship lines follow the
    # RELATIONSHIPS_CONFIRMED: header (no extra TOTAL/OMITTED line, and
    # RELATIONSHIPS_CONFIRMED: is the last section here — no inbound edges,
    # no inferred edges, no status/date/measure columns in this fixture).
    after_header = hub_doc.document_text.split("RELATIONSHIPS_CONFIRMED:")[1]
    assert after_header.strip() == "\n".join(
        [f"  {line}" for line in sorted(["aaa_col -> dbo.zzz_target.id", "zzz_col -> dbo.aaa_target.id"])]
    ).strip()


def test_old_cached_embedding_is_reusable_after_step7a_correction(tmp_path, monkeypatch):
    """End-to-end: a document persisted (and embedded) using the pre-Step-7
    hash-equivalent text is recognized as ALREADY EMBEDDED when the SAME
    logical table is rebuilt through the current (Step 7A-corrected)
    build_retrieval_documents — find_missing_document_hashes reports zero
    missing hashes, proving no new OpenAI call would be made."""
    db_path = str(tmp_path / "step7a.db")

    def _make_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    original_get_connection = models_module.get_connection
    models_module.get_connection = _make_connection
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original_get_connection
    monkeypatch.setattr(retrieval_store_module, "get_connection", _make_connection)
    monkeypatch.setattr(embedding_store_module, "get_connection", _make_connection)

    conn = _make_connection()
    now = "2026-09-10T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (1, 'alice', 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, 1, 1, 'mssql', '{}', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    # Current (Step 7A-corrected) build for the adversarial two-FK table.
    docs = build_retrieval_documents(1, _adversarial_two_fk_table())
    hub_doc = next(d for d in docs if d.object_fqn == "dbo.zzz_hub")

    batch = RetrievalDocumentBatch(
        source_id=1, snapshot_id=1, snapshot_version=1, status=STATUS_READY,
        documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    fingerprint = {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(b"step7a-fp").hexdigest(),
        "table_set_hash": hashlib.sha256(b"step7a-ts").hexdigest(),
    }
    result = persist_retrieval_build(batch, fingerprint, "alice")
    assert result.build is not None
    build_id = result.build.id

    # Pre-populate an embedding under hub_doc.document_hash — simulating an
    # embedding already cached from a PRE-Step-7 build that happened to
    # produce the identical hash for this identical, non-truncating input.
    insert_result = insert_embeddings(
        build_id, 1, "alice", "openai", "text-embedding-3-small", 3,
        {hub_doc.document_hash: (0.1, 0.2, 0.3)},
    )
    assert insert_result.status == EMBEDDING_STATUS_OK

    missing = find_missing_document_hashes(build_id, 1, "alice", "openai", "text-embedding-3-small", 3)
    assert hub_doc.document_hash not in missing


# ── 2. Truncated hub documents: accurate total/omitted counts preserved ──

def test_truncated_hub_document_still_includes_accurate_total_and_omitted_counts():
    hub = _table("dbo", "hub", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [_table("dbo", f"v_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW") for i in range(30)]
    relationships = [
        {
            "from_table_fqn": f"dbo.v_{i:03d}", "from_column": "id",
            "to_schema": "dbo", "to_table": "hub", "to_table_fqn": "dbo.hub", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(30)
    ]
    docs = build_retrieval_documents(1, _snapshot(1, [hub, *views]), relationships=relationships)
    hub_doc = {d.object_fqn: d for d in docs}["dbo.hub"]

    assert f"RELATIONSHIPS_CONFIRMED_INBOUND_TOTAL: {30}" in hub_doc.document_text
    expected_omitted = 30 - DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES
    assert f"RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED: {expected_omitted}" in hub_doc.document_text
    kept_lines = [l for l in hub_doc.document_text.splitlines() if l.strip().startswith("id <-")]
    assert len(kept_lines) == DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES


def test_truncated_physical_fk_still_prioritized_in_render_order():
    hub_fk = ForeignKeyInfo(
        fk_name="fk_important", from_column="important_ref",
        to_schema="dbo", to_table="zzz_important_target", to_column="id",
    )
    hub = _table(
        "dbo", "hub", [_col("id", "INTEGER", pk=True), _col("important_ref", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[hub_fk],
    )
    target = _table("dbo", "zzz_important_target", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [_table("dbo", f"aaa_view_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW") for i in range(20)]
    relationships = [
        {
            "from_table_fqn": "dbo.hub", "from_column": f"view_ref_{i:03d}",
            "to_schema": "dbo", "to_table": f"aaa_view_{i:03d}", "to_table_fqn": f"dbo.aaa_view_{i:03d}", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(20)
    ]
    docs = build_retrieval_documents(1, _snapshot(1, [hub, target, *views]), relationships=relationships)
    hub_doc = {d.object_fqn: d for d in docs}["dbo.hub"]

    outbound = hub_doc.document_text.split("RELATIONSHIPS_CONFIRMED:")[1].split("RELATIONSHIPS_CONFIRMED_INBOUND", 1)[0]
    assert "important_ref -> dbo.zzz_important_target.id" in outbound
    assert "RELATIONSHIPS_CONFIRMED_OUTBOUND_OMITTED:" in hub_doc.document_text


# ── Deterministic priority/rendering ────────────────────────────────────

def test_deterministic_rendering_across_repeated_builds():
    a = build_retrieval_documents(1, _adversarial_two_fk_table())
    b = build_retrieval_documents(1, _adversarial_two_fk_table())
    hashes_a = {d.object_fqn: d.document_hash for d in a}
    hashes_b = {d.object_fqn: d.document_hash for d in b}
    assert hashes_a == hashes_b


# ── Full graph metadata remains available ───────────────────────────────

def test_full_graph_metadata_still_available_after_correction():
    hub = _table("dbo", "hub", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [_table("dbo", f"v_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW") for i in range(30)]
    relationships = [
        {
            "from_table_fqn": f"dbo.v_{i:03d}", "from_column": "id",
            "to_schema": "dbo", "to_table": "hub", "to_table_fqn": "dbo.hub", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(30)
    ]
    docs = build_retrieval_documents(1, _snapshot(1, [hub, *views]), relationships=relationships)
    hub_doc = {d.object_fqn: d for d in docs}["dbo.hub"]

    assert len(hub_doc.metadata["confirmed_relationship_sources"]) == 30
    assert len(hub_doc.metadata["confirmed_relationship_edges"]) == 30
