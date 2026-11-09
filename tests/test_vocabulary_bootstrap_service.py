"""
Tests for data.vocabulary_bootstrap_service — Enterprise Phase 4 autonomous
vocabulary bootstrap. Verifies deterministic term derivation from schema
metadata (no LLM), source scoping, idempotent/replaceable regeneration, and
that generation never raises even in degraded conditions — this module
backs an inline best-effort step in data.datasource_service.create_data_source
(see tests/test_zero_catalog_bootstrap.py for that connection-flow contract)
and must never be able to break it.

Run from the project root:
    python -m pytest tests/test_vocabulary_bootstrap_service.py -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-vocab-bootstrap-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-vocab-bootstrap-salt-long-enough-0987654321")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo

import data.models as models
from data.vocabulary_bootstrap_service import (
    VocabularyBootstrapResult,
    _derive_table_terms,
    bootstrap_vocabulary,
    get_generated_vocabulary,
)

_NOW = "2026-07-25T00:00:00+00:00"
_USER = "user-1"
_OTHER_USER = "user-2"

_PATCHED_MODULES = (
    "data.vocabulary_bootstrap_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _table(table_name: str, schema_name: str = "dbo", columns: list[str] | None = None) -> TableInfo:
    cols = [
        ColumnInfo(
            column_name=name, ordinal_position=i + 1, data_type="TEXT", raw_type="varchar(50)",
            is_nullable=True, is_primary_key=False, is_identity=False,
        )
        for i, name in enumerate(columns or [])
    ]
    return TableInfo(
        table_name=table_name, schema_name=schema_name,
        table_fqn=f"{schema_name}.{table_name}", table_type="TABLE", columns=cols,
    )


def _snapshot_json(source_id: int, tables: list[TableInfo]) -> str:
    snapshot = SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at=_NOW,
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )
    return json.dumps(dataclasses.asdict(snapshot))


def env(tmp_path, monkeypatch):
    """One mssql source (id=1, owned by _USER), no schema_snapshots row yet
    — individual tests insert whichever snapshot(s) they need."""
    db_path = str(tmp_path / "vocabbootstrap.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,?,'Prod SQL Server','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_USER, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_snapshot(db_path, source_id: int, tables: list[TableInfo], version: int = 1) -> int:
    conn = _db_conn(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO schema_snapshots "
            "(source_id, snapshot_version, source_type, table_count, snapshot_json, "
            " discovered_at, created_at) VALUES (?,?,'mssql',?,?,?,?)",
            (source_id, version, len(tables), _snapshot_json(source_id, tables), _NOW, _NOW),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _vocab_rows(db_path, source_id: int, term: str | None = None):
    conn = _db_conn(db_path)
    try:
        if term is None:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM generated_business_vocabulary WHERE source_id = ?", (source_id,),
            ).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM generated_business_vocabulary WHERE source_id = ? AND term = ?",
            (source_id, term),
        ).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure derivation logic (_derive_table_terms) — no DB involved.
# ---------------------------------------------------------------------------

def test_derives_high_confidence_term_from_clean_table_name():
    terms = _derive_table_terms("dbo.Candidates", "Candidates", [])
    high = [t for t in terms if t["tier"] == "HIGH"]
    assert any(t["term"] == "candidate" for t in high)


def test_singular_and_plural_forms_resolve_to_same_normalized_term():
    # normalize_term singularizes at write time; a query for either "client"
    # or "clients" must land on the exact same stored term.
    from data.vocabulary_service import normalize_term
    terms = _derive_table_terms("dbo.Clients", "Clients", [])
    stored_term = next(t["term"] for t in terms if t["tier"] == "HIGH")
    assert normalize_term("client") == stored_term
    assert normalize_term("clients") == stored_term


def test_strips_vendor_prefix_and_dated_suffix():
    plain = _derive_table_terms("dbo.Candidates", "Candidates", [])
    prefixed = _derive_table_terms(
        "dbo.EDU_Candidates_17Feb2021", "EDU_Candidates_17Feb2021", [],
    )
    plain_term = next(t["term"] for t in plain if t["tier"] == "HIGH")
    prefixed_term = next(t["term"] for t in prefixed if t["tier"] == "HIGH")
    assert plain_term == prefixed_term == "candidate"


def test_column_name_evidence_adds_medium_candidate():
    terms = _derive_table_terms("dbo.T1", "T1", ["PlacementDate"])
    medium_from_column = [
        t for t in terms
        if t["tier"] == "MEDIUM" and t["column_name"] == "PlacementDate"
    ]
    assert any(t["term"] == "placement date" for t in medium_from_column)


def test_generic_noise_word_is_low_tier_not_medium():
    terms = _derive_table_terms("dbo.Export", "Export", [])
    assert terms and all(t["tier"] == "LOW" for t in terms if t["term"] == "export")


def test_compound_table_name_surfaces_each_meaningful_token():
    # Real the reference database shape: dbo.ERP_StudentsPlacements — "placements" must
    # resolve even though it's only the tail of a compound legacy name.
    terms = _derive_table_terms(
        "dbo.ERP_StudentsPlacements", "ERP_StudentsPlacements", [],
    )
    medium_terms = {t["term"] for t in terms if t["tier"] == "MEDIUM"}
    assert "placement" in medium_terms
    assert "student" in medium_terms


def test_compound_column_name_surfaces_each_meaningful_token():
    # Real the reference database shape: dbo.EDU_Track has no lexical relationship to "program"
    # in its own table name, but carries a "ProgramType" column — a business
    # user asking about "program" never types "program type" verbatim, so
    # the bare, per-token term must surface too, not just the whole
    # normalized column name.
    terms = _derive_table_terms("dbo.EDU_Track", "EDU_Track", ["ProgramType"])
    medium_from_column = [
        t for t in terms
        if t["tier"] == "MEDIUM" and t["column_name"] == "ProgramType"
    ]
    per_token_terms = {t["term"] for t in medium_from_column}
    assert "program" in per_token_terms
    assert "program type" in per_token_terms  # whole-column term still present, unchanged
    # Weaker evidence than the whole-column term (more inferential).
    program_row = next(t for t in medium_from_column if t["term"] == "program")
    whole_row = next(t for t in medium_from_column if t["term"] == "program type")
    assert program_row["score"] < whole_row["score"]


def test_single_token_column_name_unaffected_by_per_token_derivation():
    # A single-word column name already IS its own whole-column term
    # (produced by the existing branch) — the per-token loop must not
    # duplicate it or emit anything extra for it.
    terms = _derive_table_terms("dbo.T1", "T1", ["Active"])
    active_rows = [t for t in terms if t["column_name"] == "Active"]
    assert len(active_rows) == 1
    assert active_rows[0]["term"] == "active"


def test_generic_noise_token_excluded_from_per_token_column_derivation():
    # "type" alone must never surface as a candidate term from "ProgramType"
    # — it's exactly the kind of structurally common word
    # _GENERIC_NOISE_TOKENS excludes everywhere else in this module.
    terms = _derive_table_terms("dbo.EDU_Track", "EDU_Track", ["ProgramType"])
    noise_terms = {t["term"] for t in terms if t["column_name"] == "ProgramType"}
    assert "type" not in noise_terms


# ---------------------------------------------------------------------------
# bootstrap_vocabulary() / get_generated_vocabulary() against a real DB.
# ---------------------------------------------------------------------------

def test_bootstrap_populates_generated_vocabulary_table(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])

    result = bootstrap_vocabulary(1, _USER)

    assert isinstance(result, VocabularyBootstrapResult)
    assert "dbo.Candidates" in result.generated
    assert result.failed is None
    rows = _vocab_rows(db_path, 1, "candidate")
    assert any(r["table_fqn"] == "dbo.Candidates" and r["confidence_tier"] == "HIGH" for r in rows)


def test_bootstrap_stores_multiple_competing_tables_for_ambiguous_term(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    # Both tables reduce to the exact same HIGH term "client" after prefix
    # stripping — ambiguity must never be silently collapsed to one winner.
    _insert_snapshot(db_path, 1, [_table("Clients"), _table("EDU_StaffingClients")])

    bootstrap_vocabulary(1, _USER)

    rows = _vocab_rows(db_path, 1, "client")
    table_fqns = {r["table_fqn"] for r in rows}
    assert {"dbo.Clients", "dbo.EDU_StaffingClients"} <= table_fqns


def test_bootstrap_is_idempotent_on_rerun(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])

    first = bootstrap_vocabulary(1, _USER)
    rows_after_first = _vocab_rows(db_path, 1)
    second = bootstrap_vocabulary(1, _USER)
    rows_after_second = _vocab_rows(db_path, 1)

    assert first.term_count == second.term_count
    assert len(rows_after_first) == len(rows_after_second)


def test_bootstrap_replaces_stale_rows_on_new_snapshot(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])
    bootstrap_vocabulary(1, _USER)
    assert _vocab_rows(db_path, 1, "candidate")

    # Rediscovery replaces the table set entirely (e.g. a renamed/dropped table).
    _insert_snapshot(db_path, 1, [_table("Placements")], version=2)
    bootstrap_vocabulary(1, _USER)

    assert _vocab_rows(db_path, 1, "candidate") == []
    assert _vocab_rows(db_path, 1, "placement")


def test_bootstrap_never_raises_with_no_schema_snapshot(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)  # no schema_snapshots row inserted

    result = bootstrap_vocabulary(1, _USER)

    assert result.failed is not None
    assert result.generated == []


def test_bootstrap_returns_failed_for_unowned_source(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])

    result = bootstrap_vocabulary(1, _OTHER_USER)

    assert result.failed is not None
    assert _vocab_rows(db_path, 1) == []


def test_bootstrap_best_effort_enrichment_absent_dictionary_rows_still_succeeds(tmp_path, monkeypatch):
    # No data_dictionary_tables/domain_assignments/entity_assignments rows
    # exist at all (the real state immediately post-connect, before any
    # dictionary/domain/entity generation has run) — bootstrap must still
    # succeed purely from schema_snapshots.
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])

    result = bootstrap_vocabulary(1, _USER)

    assert result.failed is None
    assert _vocab_rows(db_path, 1, "candidate")


def test_generated_vocabulary_is_source_scoped_no_leakage(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,?,'Other Source','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_USER, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    _insert_snapshot(db_path, 1, [_table("Candidates")])
    _insert_snapshot(db_path, 2, [_table("Invoices")])

    bootstrap_vocabulary(1, _USER)
    bootstrap_vocabulary(2, _USER)

    assert get_generated_vocabulary(1).get("invoice") is None
    assert get_generated_vocabulary(2).get("candidate") is None
    assert get_generated_vocabulary(1).get("candidate")
    assert get_generated_vocabulary(2).get("invoice")


def test_get_generated_vocabulary_grouped_by_term_ordered_by_confidence(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Candidates")])

    bootstrap_vocabulary(1, _USER)
    grouped = get_generated_vocabulary(1)

    assert isinstance(grouped, dict)
    rows = grouped["candidate"]
    assert isinstance(rows, list)
    scores = [r["confidence_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_low_tier_rows_are_never_returned_to_retrieval_candidates(tmp_path, monkeypatch):
    # Excluded at the retrieval/query_planning integration layer (see
    # tests/test_semantic_retrieval_service.py), but the underlying stored
    # rows must still carry LOW so that layer has something to filter.
    db_path = env(tmp_path, monkeypatch)
    _insert_snapshot(db_path, 1, [_table("Export")])

    bootstrap_vocabulary(1, _USER)

    rows = _vocab_rows(db_path, 1, "export")
    assert rows and all(r["confidence_tier"] == "LOW" for r in rows)
