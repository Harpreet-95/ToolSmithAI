"""
View Semantic Discovery milestone.

Unit tests for data.semantic_retrieval_service.get_curated_view_candidates
-- the additive-only, provenance-tagged candidate source fed by
data.view_semantics_service's connect-time-persisted view_semantic_evidence
table. Mirrors tests/test_business_learning_candidate_discovery.py's own
unit-level structure for get_preliminary_candidates (same floor-ladder
contract, one rung up).

Run:
    python -m pytest tests/test_curated_view_evidence.py -v
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-curated-view-evidence-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-curated-view-evidence-salt-long-enough-12")

import data.db as db_module
import data.models as models
from data.db import get_connection

_USER = "curated-view-evidence-test-user"


@pytest.fixture(scope="module", autouse=True)
def _isolated_metadata_db(tmp_path_factory):
    """This file used to call models.init_db() at import time against the
    real, unpatched DB_PATH -- every collection of this file permanently
    wrote rows into data/toolsmith.db. Redirect DB_PATH to a module-scoped
    temp file before initializing schema (Phase 5 Step 6C)."""
    db_path = tmp_path_factory.mktemp("curated_view_evidence") / "test.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    yield
    monkeypatch.undo()


def _make_source() -> int:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO data_source_connections "
            "(user_id, display_name, source_type, source_category, encrypted_config_json, "
            " config_schema_version, capabilities_json, metadata_json, source_status, "
            " is_active, created_at, updated_at) "
            "VALUES (?, 'cve-test', 'fixture_relational', 'relational_db', '{}', 1, "
            " '[]', '{}', 'ACTIVE', 1, ?, ?)",
            (_USER, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _make_snapshot(source_id: int) -> int:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO schema_snapshots "
            "(source_id, snapshot_version, source_type, table_count, view_count, "
            " column_count, snapshot_json, discovered_at, created_at) "
            "VALUES (?, 1, 'fixture_relational', 1, 1, 1, '{}', ?, ?)",
            (source_id, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_view_row(
    source_id: int, table_fqn: str, *, referenced_tables: list[str], is_curated: bool,
) -> None:
    snapshot_id = _make_snapshot(source_id)
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO view_semantic_evidence "
            "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, "
            " referenced_tables_json, restriction_json, naming_penalty_score, is_curated, "
            " discovered_at) "
            "VALUES (?, ?, ?, 'SELECT 1', 'chk', ?, NULL, 0.0, ?, ?)",
            (source_id, snapshot_id, table_fqn, json.dumps(referenced_tables), 1 if is_curated else 0, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_curated_view_candidates_requires_real_name_overlap():
    from data.semantic_retrieval_service import get_curated_view_candidates

    source_id = _make_source()
    _insert_view_row(source_id, "main.vw_OpenTickets", referenced_tables=["main.Tickets"], is_curated=True)

    result = get_curated_view_candidates(source_id, _USER, "how many widgets", ["widgets"])
    assert result == []


def test_get_curated_view_candidates_matches_view_own_name():
    from data.semantic_retrieval_service import (
        get_curated_view_candidates, CURATED_VIEW_EVIDENCE_PROVENANCE, _CURATED_VIEW_EVIDENCE_FLOOR,
    )

    source_id = _make_source()
    _insert_view_row(source_id, "main.vw_OpenTickets", referenced_tables=["main.Tickets"], is_curated=True)

    result = get_curated_view_candidates(source_id, _USER, "open tickets", ["open", "tickets"])
    fqns = {r["qualified_name"] for r in result}
    assert "main.vw_OpenTickets" in fqns
    entry = next(r for r in result if r["qualified_name"] == "main.vw_OpenTickets")
    assert entry["provenance"] == CURATED_VIEW_EVIDENCE_PROVENANCE
    assert entry["relevance_score"] >= _CURATED_VIEW_EVIDENCE_FLOOR


def test_get_curated_view_candidates_matches_referenced_table_tokens():
    """A view's dependency (referenced_tables) can qualify it even when the
    view's OWN name shares no token with the question -- mirrors
    get_preliminary_candidates' measure-column-token behavior."""
    from data.semantic_retrieval_service import get_curated_view_candidates

    source_id = _make_source()
    _insert_view_row(source_id, "main.vw_Summary", referenced_tables=["main.Tickets"], is_curated=True)

    result = get_curated_view_candidates(source_id, _USER, "show tickets", ["tickets"])
    fqns = {r["qualified_name"] for r in result}
    assert "main.vw_Summary" in fqns


def test_get_curated_view_candidates_excludes_uncurated_rows():
    from data.semantic_retrieval_service import get_curated_view_candidates

    source_id = _make_source()
    _insert_view_row(source_id, "main.vw_TicketsBackup", referenced_tables=["main.Tickets"], is_curated=False)

    result = get_curated_view_candidates(source_id, _USER, "tickets backup", ["tickets", "backup"])
    assert result == []


def test_get_curated_view_candidates_no_terms_returns_empty():
    from data.semantic_retrieval_service import get_curated_view_candidates

    source_id = _make_source()
    _insert_view_row(source_id, "main.vw_OpenTickets", referenced_tables=["main.Tickets"], is_curated=True)
    assert get_curated_view_candidates(source_id, _USER, "", []) == []


def test_curated_view_floor_sits_between_preliminary_and_generated_medium():
    from data.semantic_retrieval_service import (
        _CURATED_VIEW_EVIDENCE_FLOOR, _PRELIMINARY_BUSINESS_LEARNING_FLOOR, _GENERATED_VOCAB_MEDIUM_FLOOR,
    )
    assert _PRELIMINARY_BUSINESS_LEARNING_FLOOR < _CURATED_VIEW_EVIDENCE_FLOOR < _GENERATED_VOCAB_MEDIUM_FLOOR


def test_curated_view_floor_never_outranks_generated_vocab():
    from data.semantic_retrieval_service import _merge_best, _GENERATED_VOCAB_MEDIUM_FLOOR, _CURATED_VIEW_EVIDENCE_FLOOR

    merged: dict = {}
    _merge_best(merged, [{"qualified_name": "main.Tickets", "relevance_score": _GENERATED_VOCAB_MEDIUM_FLOOR + 0.5}])
    _merge_best(merged, [{
        "qualified_name": "main.Tickets", "relevance_score": _CURATED_VIEW_EVIDENCE_FLOOR,
        "provenance": "CURATED_VIEW_EVIDENCE",
    }])
    assert merged["main.Tickets"]["relevance_score"] == _GENERATED_VOCAB_MEDIUM_FLOOR + 0.5
    assert merged["main.Tickets"].get("provenance") is None  # validated evidence's dict wins outright


def test_curated_view_floor_outranks_preliminary_business_learning():
    from data.semantic_retrieval_service import _merge_best, _PRELIMINARY_BUSINESS_LEARNING_FLOOR, _CURATED_VIEW_EVIDENCE_FLOOR

    merged: dict = {}
    _merge_best(merged, [{
        "qualified_name": "main.Tickets", "relevance_score": _PRELIMINARY_BUSINESS_LEARNING_FLOOR,
        "provenance": "PRELIMINARY_BUSINESS_LEARNING",
    }])
    _merge_best(merged, [{
        "qualified_name": "main.Tickets", "relevance_score": _CURATED_VIEW_EVIDENCE_FLOOR,
        "provenance": "CURATED_VIEW_EVIDENCE",
    }])
    assert merged["main.Tickets"]["provenance"] == "CURATED_VIEW_EVIDENCE"
