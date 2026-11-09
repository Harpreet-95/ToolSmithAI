"""
Focused tests -- SQL Architecture Correction, Phase 6, Step 6E: the
VIEW_ENRICHED preparation coordinator
(core/sqlservice/view_semantic_enrichment_preparation.py).

Mirrors tests/test_sqlservice_semantic_enrichment_object_core_preparation.py
exactly (same fixture plumbing, same fake-provider pattern), adapted for
VIEW-typed documents and the VIEW_ENRICHED contract shape (see
tests/test_sqlservice_view_semantic_enrichment.py's own `_view_item`/
`_make_view_input` helpers, reused here directly).

Uses a temporary on-disk SQLite database only -- never the real
data/toolsmith.db. Every provider call is a FakeSemanticEnrichmentProvider
-- never OpenAISemanticEnrichmentProvider against a real network.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step6e-view-preparation-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-step6e-view-preparation-salt-long-enough-1234")

import data.models as models_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.semantic_enrichment_store as store_module
import core.sqlservice.view_semantic_enrichment_preparation as view_prep_module
from core.sqlservice.join_planner import clear_join_planner_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_linker import clear_schema_linker_cache
from core.sqlservice.semantic_enrichment_store import get_view_enrichment_by_cache_key
from core.sqlservice.view_semantic_enrichment_preparation import (
    STATUS_NOT_PREPARED,
    STATUS_UNOWNED,
    STATUS_VIEW_ENRICHED_PARTIAL,
    STATUS_VIEW_ENRICHED_READY,
    prepare_source_view_enrichment,
    summarize_view_validation_diagnostics,
)
from tests.test_sqlservice_view_semantic_enrichment import (
    FakeSemanticEnrichmentProvider,
    _view_item,
)


# ── Fixture plumbing (mirrors test_sqlservice_semantic_enrichment_object_core_preparation.py) ─


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _patch_connections(monkeypatch, db_path):
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(view_prep_module, "get_connection", lambda: _make_connection(db_path))


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("template") / "template.db")
    original_get_connection = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original_get_connection
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    _patch_connections(monkeypatch, path)
    clear_schema_linker_cache()
    clear_join_planner_cache()
    yield path
    clear_schema_linker_cache()
    clear_join_planner_cache()


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-17T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, snapshot_id, source_id, version=1):
    conn = _make_connection(db_path)
    now = "2026-09-17T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _view_doc(object_fqn, *, source_id, columns=(("StudentId", "INTEGER", True, False),), tier=1):
    schema_name, object_name = object_fqn.split(".", 1)
    column_descriptors = tuple(
        {"column_name": name, "normalized_sql_type": sql_type, "is_primary_key": pk, "is_foreign_key": fk}
        for name, sql_type, pk, fk in columns
    )
    metadata = {
        "column_count": len(columns), "column_names": tuple(c[0] for c in columns),
        "column_descriptors": column_descriptors, "has_primary_key": any(c[2] for c in columns),
        "primary_key_columns": tuple(c[0] for c in columns if c[2]),
        "schema_name": schema_name, "table_name": object_name,
        "business_name": None, "description": None, "synonyms": (),
    }
    text = f"OBJECT: {object_fqn}\nTYPE: VIEW\nTIER: {tier}\nCOLUMNS:\n" + "\n".join(f"  {c[0]}" for c in columns)
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type="VIEW",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _fingerprint(*, seed="a"):
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(f"fingerprint-{seed}".encode()).hexdigest(),
        "table_set_hash": hashlib.sha256(f"table-set-{seed}".encode()).hexdigest(),
    }


def _make_ready_build(db_path, docs, *, source_id=1, snapshot_id=1, user_id="alice", seed="a"):
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=1,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(seed=seed), user_id)
    assert result.build is not None
    return result.build


class FakeViewProvider(FakeSemanticEnrichmentProvider):
    """Auto-responds using `_view_item`, matching every "VIEW v\\d+:" token
    the real prompt renderer produces -- never a real network call."""

    def __init__(self, responder=None, *, provider_id="fake", model="fake-model-1"):
        super().__init__(responder or _auto_respond, provider_id=provider_id, model=model)

    def enrich_batch(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append((system_prompt, user_prompt))
        responder = self._queue.pop(0) if self._queue is not None else self._single
        raw = responder(user_prompt) if callable(responder) else responder
        if isinstance(raw, Exception):
            raise raw
        from core.sqlservice.semantic_enrichment_providers import ProviderSemanticEnrichmentResponse
        return ProviderSemanticEnrichmentResponse(raw=raw, model=self._model)


def _auto_respond(user_prompt: str) -> dict:
    ids = re.findall(r"VIEW (v\d+):", user_prompt)
    return {"views": [_view_item(vid) for vid in ids]}


# ── First run / repeat run / readiness ──────────────────────────────────


def test_first_run_generates_all_and_reports_ready(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, 1)
    doc_a = _view_doc("dbo.ActiveStudents", source_id=1)
    doc_b = _view_doc("dbo.CourseSummary", source_id=1, columns=(("CourseId", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b])

    provider = FakeViewProvider()
    result = prepare_source_view_enrichment(1, "alice", lambda: provider)

    assert result.status == STATUS_VIEW_ENRICHED_READY
    assert result.eligible_view_count == 2
    assert result.generated_count == 2
    assert result.reused_count == 0
    assert result.missing_view_fqns == ()
    assert result.contract_version == 1


def test_repeat_run_makes_zero_provider_calls_and_stays_ready(db_path):
    _insert_source(db_path, 2)
    _insert_snapshot(db_path, 2, 2)
    doc = _view_doc("dbo.ActiveStudents", source_id=2)
    _make_ready_build(db_path, [doc], source_id=2, snapshot_id=2)

    provider = FakeViewProvider()
    first = prepare_source_view_enrichment(2, "alice", lambda: provider)
    assert first.status == STATUS_VIEW_ENRICHED_READY
    assert first.generated_count == 1

    provider2 = FakeViewProvider()
    second = prepare_source_view_enrichment(2, "alice", lambda: provider2)
    assert second.status == STATUS_VIEW_ENRICHED_READY
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert provider2.calls == []  # zero NEW provider calls


def test_no_ready_build_reports_not_prepared(db_path):
    _insert_source(db_path, 3)
    provider = FakeViewProvider()
    result = prepare_source_view_enrichment(3, "alice", lambda: provider)
    assert result.status == STATUS_NOT_PREPARED
    assert provider.calls == []


# ── Partial batch failure / bounded resume ──────────────────────────────


def test_partial_batch_failure_reports_partial_and_persists_the_rest(db_path):
    _insert_source(db_path, 4)
    _insert_snapshot(db_path, 4, 4)
    doc_a = _view_doc("dbo.Alpha", source_id=4)
    doc_b = _view_doc("dbo.Beta", source_id=4, columns=(("CourseId", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b], source_id=4, snapshot_id=4)

    def responder(user_prompt):
        if "dbo.Alpha" in user_prompt:
            return {"views": []}  # missing id -- fails this batch alone
        ids = re.findall(r"VIEW (v\d+):", user_prompt)
        return {"views": [_view_item(vid) for vid in ids]}

    provider = FakeViewProvider(responder)
    result = prepare_source_view_enrichment(4, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_VIEW_ENRICHED_PARTIAL
    assert result.batch_validation_failed_count == 1
    assert result.generated_count == 1
    assert result.missing_view_fqns == ("dbo.Alpha",)
    assert result.resumable is True
    # Structured diagnostic surfaced, grouped by (code, field_path) -- never a raw message.
    assert any(d.code == "MISSING_VIEW_IDS" for d in result.validation_diagnostics)
    assert result.batch_rejected_count == 1


def test_bounded_resume_after_partial_failure_completes_and_becomes_ready(db_path):
    _insert_source(db_path, 5)
    _insert_snapshot(db_path, 5, 5)
    doc = _view_doc("dbo.ActiveStudents", source_id=5)
    _make_ready_build(db_path, [doc], source_id=5, snapshot_id=5)

    provider = FakeViewProvider(lambda user_prompt: {"views": []})
    first = prepare_source_view_enrichment(5, "alice", lambda: provider)
    assert first.status == STATUS_VIEW_ENRICHED_PARTIAL
    assert first.generated_count == 0
    assert first.missing_view_fqns == ("dbo.ActiveStudents",)

    # "At most one bounded resume" -- exactly one more call, never a loop.
    provider2 = FakeViewProvider()
    second = prepare_source_view_enrichment(5, "alice", lambda: provider2)
    assert second.status == STATUS_VIEW_ENRICHED_READY
    assert second.generated_count == 1
    assert second.missing_view_fqns == ()


# ── PERCENTAGE -> RATE canonicalization, end to end through the coordinator ──


def test_percentage_measure_role_persists_as_rate_end_to_end(db_path):
    _insert_source(db_path, 6)
    _insert_snapshot(db_path, 6, 6)
    doc = _view_doc(
        "dbo.CompletionRates", source_id=6,
        columns=(("StudentId", "INTEGER", True, False), ("CompletionPct", "DECIMAL", False, False)),
    )
    _make_ready_build(db_path, [doc], source_id=6, snapshot_id=6)

    def responder(user_prompt):
        ids = re.findall(r"VIEW (v\d+):", user_prompt)
        return {"views": [
            _view_item(vid, columns=[{
                "column": "CompletionPct", "label": "Completion percentage", "semantic_role": "MEASURE",
                "date_role": None, "measure_role": "PERCENTAGE", "dimension_role": None, "identifier_role": None,
                "confidence": 0.7,
            }])
            for vid in ids
        ]}

    provider = FakeViewProvider(responder)
    result = prepare_source_view_enrichment(6, "alice", lambda: provider)

    assert result.status == STATUS_VIEW_ENRICHED_READY
    stored = get_view_enrichment_by_cache_key(
        6, "alice", object_fqn="dbo.CompletionRates",
        semantic_input_hash=[k for k in _stored_hashes(db_path, 6)][0],
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert stored is not None
    pct_col = next(c for c in stored.columns if c.column_name == "CompletionPct")
    assert pct_col.measure_role == "RATE"


def _stored_hashes(db_path, source_id):
    conn = _make_connection(db_path)
    rows = conn.execute(
        "SELECT semantic_input_hash FROM sql_view_semantic_enrichments WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    return [r["semantic_input_hash"] for r in rows]


# ── Diagnostics summarization ────────────────────────────────────────────


def test_summarize_view_validation_diagnostics_groups_by_code_and_path(db_path):
    _insert_source(db_path, 7)
    _insert_snapshot(db_path, 7, 7)
    doc_a = _view_doc("dbo.Alpha", source_id=7)
    doc_b = _view_doc("dbo.Beta", source_id=7, columns=(("CourseId", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b], source_id=7, snapshot_id=7)

    provider = FakeViewProvider(lambda user_prompt: {"views": []})  # every batch fails MISSING_VIEW_IDS
    result = prepare_source_view_enrichment(7, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_VIEW_ENRICHED_PARTIAL
    # The coordinator's own already-computed, grouped diagnostics field:
    assert all(d.code == "MISSING_VIEW_IDS" for d in result.validation_diagnostics)
    assert sum(d.count for d in result.validation_diagnostics) == 2

    # summarize_view_validation_diagnostics itself is pure and reusable
    # against any ViewEnrichmentResult.batch_outcomes tuple directly.
    from core.sqlservice.semantic_enrichment_service import generate_view_semantic_enrichment
    from tests.test_sqlservice_view_semantic_enrichment import _make_view_input

    raw_result = generate_view_semantic_enrichment(
        [_make_view_input("dbo.Solo")], lambda: FakeViewProvider(lambda p: {"views": []}),
    )
    direct_summary = summarize_view_validation_diagnostics(raw_result.diagnostics)
    assert len(direct_summary) == 1
    assert direct_summary[0].code == "MISSING_VIEW_IDS"
    assert direct_summary[0].count == 1


# ── Oversized view skip is reported, never retried/split ────────────────


def test_oversized_view_reported_as_skipped_not_missing(db_path):
    _insert_source(db_path, 8)
    _insert_snapshot(db_path, 8, 8)
    doc = _view_doc("dbo.HugeView", source_id=8)
    _make_ready_build(db_path, [doc], source_id=8, snapshot_id=8)

    provider = FakeViewProvider()
    # An impossibly tiny output-token budget forces this single view to be
    # individually skipped BEFORE dispatch, never retried or split.
    result = prepare_source_view_enrichment(
        8, "alice", lambda: provider, max_estimated_output_tokens=1,
    )
    assert "dbo.HugeView" in result.oversized_view_fqns
    assert provider.calls == []  # skipped before dispatch -- never even called


# ── View-subset override -- the exact mechanism a pilot resume uses ──────


def test_view_inputs_override_scopes_run_to_named_views(db_path):
    _insert_source(db_path, 9)
    _insert_snapshot(db_path, 9, 9)
    doc_a = _view_doc("dbo.Alpha", source_id=9)
    doc_b = _view_doc("dbo.Beta", source_id=9, columns=(("CourseId", "INTEGER", True, False),))
    doc_c = _view_doc("dbo.Gamma", source_id=9, columns=(("OrderId", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b, doc_c], source_id=9, snapshot_id=9)

    from core.sqlservice.view_semantic_enrichment_input import build_view_semantic_input
    from core.sqlservice.view_semantic_enrichment_selection import ViewEnrichmentCandidate

    override_inputs = [
        build_view_semantic_input(
            ViewEnrichmentCandidate(object_fqn="dbo.Alpha", schema_name="dbo", view_name="Alpha", retrieval_tier=1),
            columns=(), primary_key_columns=(), dictionary_business_name=None, dictionary_description=None,
            dictionary_synonyms=(), raw_definition_sql=None, dependency_edges=(), confirmed_relationship_summary=(),
        ).view_input,
    ]

    provider = FakeViewProvider()
    result = prepare_source_view_enrichment(
        9, "alice", lambda: provider, view_inputs_override=tuple(override_inputs),
    )
    assert result.eligible_view_count == 1
    assert result.status == STATUS_VIEW_ENRICHED_READY
    assert result.generated_count == 1
    # dbo.Beta/dbo.Gamma were never part of THIS call's scope at all.
    stored_alpha = get_view_enrichment_by_cache_key(
        9, "alice", object_fqn="dbo.Alpha",
        semantic_input_hash=_stored_hashes(db_path, 9)[0],
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert stored_alpha is not None
    conn = _make_connection(db_path)
    total = conn.execute("SELECT COUNT(*) c FROM sql_view_semantic_enrichments WHERE source_id=9").fetchone()["c"]
    conn.close()
    assert total == 1


# ── Safe logging: no raw provider response or secret leakage ────────────


def test_no_raw_provider_response_or_secret_leakage_in_logs(db_path, caplog):
    _insert_source(db_path, 10)
    _insert_snapshot(db_path, 10, 10)
    doc = _view_doc("dbo.ActiveStudents", source_id=10)
    _make_ready_build(db_path, [doc], source_id=10, snapshot_id=10)

    secret_marker = "TOTALLY_SECRET_VIEW_PAYLOAD_MARKER_789"

    def responder(user_prompt):
        return {"views": [{
            "id": "v1", "business_domain": "Enrollment", "business_name": "X",
            "description": secret_marker, "source_role": "CURRENT_STATE_VIEW",
            "entity_concepts": [], "status_concepts": [], "aliases": [], "operations": [],
            # missing "confidence" and "columns" -- forces a field-set validation failure
        }]}

    provider = FakeViewProvider(responder)
    with caplog.at_level(logging.DEBUG):
        prepare_source_view_enrichment(10, "alice", lambda: provider)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_marker not in combined


def test_no_ready_build_and_unowned_never_call_provider(db_path):
    provider = FakeViewProvider()
    result = prepare_source_view_enrichment(999, "mallory", lambda: provider)
    assert result.status == STATUS_NOT_PREPARED
    assert provider.calls == []


# ── Connection lifecycle: soft disconnect/reconnect vs. genuine delete ───


def test_cache_survives_soft_disconnect_reconnect_when_source_id_unchanged(db_path):
    """A 'soft' disconnect (marking the connection inactive, e.g. across a
    backend restart) never touches sql_view_semantic_enrichments -- only a
    genuine row DELETE does (see the next test). Schema identity (source_id,
    build, hashes) is unchanged throughout, so a resumed run after
    'reconnecting' must reuse every row with zero new provider calls --
    exactly the same guarantee test_repeat_run_makes_zero_provider_calls_
    and_stays_ready already proves for two back-to-back calls, demonstrated
    here across an explicit is_active toggle simulating the connection
    lifecycle."""
    _insert_source(db_path, 11)
    _insert_snapshot(db_path, 11, 11)
    doc = _view_doc("dbo.ActiveStudents", source_id=11)
    _make_ready_build(db_path, [doc], source_id=11, snapshot_id=11)

    provider = FakeViewProvider()
    first = prepare_source_view_enrichment(11, "alice", lambda: provider)
    assert first.status == STATUS_VIEW_ENRICHED_READY
    assert first.generated_count == 1

    # Simulate "disconnect": mark the connection inactive (no row deleted,
    # no cascade -- the cache table is never touched by this).
    conn = _make_connection(db_path)
    conn.execute("UPDATE data_source_connections SET is_active = 0, source_status = 'INACTIVE' WHERE id = ?", (11,))
    conn.commit()
    conn.close()

    before = _stored_hashes(db_path, 11)

    # Simulate "reconnect": mark active again -- schema identity (source_id,
    # build, hashes) never changed, so a fresh call must be a pure reuse.
    conn = _make_connection(db_path)
    conn.execute("UPDATE data_source_connections SET is_active = 1, source_status = 'ACTIVE' WHERE id = ?", (11,))
    conn.commit()
    conn.close()

    provider2 = FakeViewProvider()
    second = prepare_source_view_enrichment(11, "alice", lambda: provider2)
    assert second.status == STATUS_VIEW_ENRICHED_READY
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert provider2.calls == []  # zero NEW provider calls across the reconnect
    assert _stored_hashes(db_path, 11) == before  # byte-identical cache identity


def test_genuine_delete_cascades_and_reconnect_allocates_new_source_id(db_path):
    """The OTHER half of Part 4.7: a genuine delete_data_source() call (not
    a soft disconnect) cascade-deletes sql_view_semantic_enrichments too --
    the SAME ON DELETE CASCADE FK every other source-specific table in this
    schema already uses (see tests/test_delete_reconnect_and_cross_database_
    isolation.py for the identical pattern at the dictionary/entity/
    relationship layer). There is no "reuse" across a genuine delete because
    a reconnect allocates a brand-new source_id by design -- schema identity
    is NOT unchanged in that case, so this is consistent with, not a
    violation of, Part 4.7's "when the schema identity is unchanged"
    qualifier."""
    _insert_source(db_path, 12)
    _insert_snapshot(db_path, 12, 12)
    doc = _view_doc("dbo.ActiveStudents", source_id=12)
    _make_ready_build(db_path, [doc], source_id=12, snapshot_id=12)

    provider = FakeViewProvider()
    first = prepare_source_view_enrichment(12, "alice", lambda: provider)
    assert first.generated_count == 1
    assert len(_stored_hashes(db_path, 12)) == 1

    import data.datasource_service as datasource_service_module
    from data.datasource_service import delete_data_source

    original_get_connection = datasource_service_module.get_connection
    datasource_service_module.get_connection = lambda: _make_connection(db_path)
    try:
        deleted = delete_data_source(12, "alice")
    finally:
        datasource_service_module.get_connection = original_get_connection
    assert deleted is not None

    assert _stored_hashes(db_path, 12) == []  # cascade-deleted with the source row


# ── Tier boundary: a Tier 3/4 view is never selected, never enriched ─────


def test_tier_3_4_views_are_never_enriched(db_path):
    _insert_source(db_path, 13)
    _insert_snapshot(db_path, 13, 13)
    eligible = _view_doc("dbo.ActiveStudents", source_id=13, tier=1)
    tier3 = _view_doc("dbo.zz_archive_students_2019", source_id=13, tier=3)
    tier4 = _view_doc("dbo.tmp_students_staging", source_id=13, tier=4)
    _make_ready_build(db_path, [eligible, tier3, tier4], source_id=13, snapshot_id=13)

    provider = FakeViewProvider()
    result = prepare_source_view_enrichment(13, "alice", lambda: provider)

    assert result.status == STATUS_VIEW_ENRICHED_READY
    assert result.eligible_view_count == 1  # only the Tier 1 view was ever a candidate
    for _, prompt in provider.calls:
        assert "zz_archive_students_2019" not in prompt
        assert "tmp_students_staging" not in prompt
    conn = _make_connection(db_path)
    fqns = {r["object_fqn"] for r in conn.execute(
        "SELECT object_fqn FROM sql_view_semantic_enrichments WHERE source_id=13"
    ).fetchall()}
    conn.close()
    assert fqns == {"dbo.ActiveStudents"}
