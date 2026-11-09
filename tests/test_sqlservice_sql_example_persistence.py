"""
Focused tests — SQL rebuild Phase 5, Steps 9/9A/9B: sql_examples /
sql_example_embeddings persistence (core/sqlservice/sql_example_store.py,
core/sqlservice/sql_example_embedding_store.py,
core/sqlservice/sql_example_candidate.py).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_example_persistence.py -v
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step9-persistence-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-step9-persistence-salt-long-enough-1234567")

import data.models as models_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
from core.sqlservice.sql_example_candidate import (
    ExampleCandidateRejected,
    TrustedSqlExampleCandidate,
    build_trusted_example_candidate,
)
from core.sqlservice.sql_example_embedding_store import (
    STATUS_OK as EMBED_STATUS_OK,
    STATUS_UNOWNED as EMBED_STATUS_UNOWNED,
    find_missing_example_search_document_hashes,
    get_example_embeddings_for_source,
    insert_example_embeddings,
)
from core.sqlservice.sql_example_model import (
    PROVENANCE_DEVELOPER_VERIFICATION_SET,
    PROVENANCE_OBSERVED_UNVERIFIED,
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
    RESULT_SHAPE_COUNT,
    STATUS_ACTIVE,
    STATUS_STALE,
    TRUST_LEVEL_TRUSTED,
    TRUST_LEVEL_UNTRUSTED,
    build_example_search_document,
    compute_content_hash,
)
from core.sqlservice.sql_example_store import (
    OUTCOME_CREATED,
    OUTCOME_REUSED,
    OUTCOME_UNOWNED,
    get_example,
    insert_trusted_example,
    insert_untrusted_observation,
    list_examples_for_source,
    update_example_status,
)
from core.sqlservice.sql_validation import STATUS_OK as VALIDATION_STATUS_OK
from core.sqlservice.sql_validation import SqlValidationResult, ValidatedColumnReference


# ── Fixture plumbing ────────────────────────────────────────────────────────

def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    import shutil
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-12T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


# ── Trusted-candidate helper (goes through the REAL gate — Step 9B: there
#    is no other way to build a TrustedSqlExampleCandidate) ────────────────

def _validation_result(
    source_id=1, question="How many :entity are there", sql="SELECT COUNT(*) FROM dbo.T", objects=("dbo.T",),
    validated_column_references=(),
):
    return SqlValidationResult(
        status=VALIDATION_STATUS_OK, sql=sql, canonical_referenced_object_fqns=objects,
        statement_type="Select", build_id=1, schema_fingerprint="fp1", warnings=(), error=None,
        validated_column_references=validated_column_references,
    )


def _trusted_candidate(
    source_id=1, question="How many :entity are there", sql="SELECT COUNT(*) FROM dbo.T", objects=("dbo.T",),
    validated_column_references=(),
) -> TrustedSqlExampleCandidate:
    return build_trusted_example_candidate(
        source_id=source_id, provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET,
        validation_result=_validation_result(
            source_id=source_id, question=question, sql=sql, objects=objects,
            validated_column_references=validated_column_references,
        ),
        question_pattern=question, result_shape=RESULT_SHAPE_COUNT,
    )


def _observation_kwargs(source_id=1, question="an unverified observation", sql="SELECT COUNT(*) FROM dbo.T", objects=("dbo.T",)):
    search_doc = build_example_search_document(
        question_pattern=question, canonical_object_fqns=objects, join_path=(), result_shape=RESULT_SHAPE_COUNT,
    )
    content_hash = compute_content_hash(
        source_id=source_id, question_pattern=question, sql_template=sql, canonical_object_fqns=objects,
    )
    return dict(
        source_id=source_id, schema_fingerprint="fp1", question_pattern=question, sql_template=sql,
        canonical_object_fqns=objects, result_shape=RESULT_SHAPE_COUNT,
        search_document_text=search_doc, search_document_hash=hashlib.sha256(search_doc.encode()).hexdigest(),
        content_hash=content_hash,
    )


# ── Storage / idempotency / ownership — trusted path ────────────────────────

def test_insert_creates_new_example(db_path):
    _insert_source(db_path, 1)
    result = insert_trusted_example("alice", _trusted_candidate())
    assert result.outcome == OUTCOME_CREATED
    assert result.example is not None
    assert result.example.status == STATUS_ACTIVE
    assert result.example.trust_level == TRUST_LEVEL_TRUSTED
    assert result.example.provenance == PROVENANCE_DEVELOPER_VERIFICATION_SET


def test_persisted_canonical_columns_preserve_table_ownership(db_path):
    """Step 10A: the sql_examples.canonical_columns_json round-trip (no
    schema change -- still a plain JSON array of strings) must still
    distinguish dbo.Student.id from dbo.Class.id after a real
    insert_trusted_example()/get_example() persistence round-trip, not
    just in the in-memory candidate."""
    _insert_source(db_path, 1)
    candidate = _trusted_candidate(
        objects=("dbo.Student", "dbo.Class"),
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="id"),
            ValidatedColumnReference(object_fqn="dbo.Class", column_name="id"),
        ),
    )
    created = insert_trusted_example("alice", candidate).example
    reloaded = get_example(created.id, 1, "alice")

    assert created.canonical_columns == ("dbo.Class.id", "dbo.Student.id")
    assert reloaded.canonical_columns == ("dbo.Class.id", "dbo.Student.id")
    assert "dbo.Student.id" in reloaded.canonical_columns
    assert "dbo.Class.id" in reloaded.canonical_columns


def test_insert_is_idempotent_by_content_hash(db_path):
    _insert_source(db_path, 1)
    first = insert_trusted_example("alice", _trusted_candidate())
    second = insert_trusted_example("alice", _trusted_candidate())
    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_REUSED
    assert first.example.id == second.example.id
    examples = list_examples_for_source(1, "alice", status=None)
    assert len(examples) == 1


def test_insert_rejected_for_unowned_source(db_path):
    _insert_source(db_path, 1, user_id="alice")
    result = insert_trusted_example("mallory", _trusted_candidate())
    assert result.outcome == OUTCOME_UNOWNED
    assert result.example is None


def test_get_example_returns_none_for_unowned(db_path):
    _insert_source(db_path, 1, user_id="alice")
    created = insert_trusted_example("alice", _trusted_candidate()).example
    assert get_example(created.id, 1, "mallory") is None
    assert get_example(created.id, 1, "alice") is not None


def test_list_examples_filters_by_status_and_trust(db_path):
    _insert_source(db_path, 1)
    active_trusted = insert_trusted_example("alice", _trusted_candidate(question="q1")).example
    untrusted = insert_untrusted_observation("alice", **_observation_kwargs(question="q2")).example

    trusted_only = list_examples_for_source(1, "alice", trust_level=TRUST_LEVEL_TRUSTED)
    assert {e.id for e in trusted_only} == {active_trusted.id}

    all_active = list_examples_for_source(1, "alice")
    assert {e.id for e in all_active} == {active_trusted.id, untrusted.id}
    assert untrusted.trust_level == TRUST_LEVEL_UNTRUSTED
    assert untrusted.provenance == PROVENANCE_OBSERVED_UNVERIFIED


# ── Storage — untrusted-observation path ────────────────────────────────────

def test_insert_untrusted_observation_creates_untrusted_row(db_path):
    _insert_source(db_path, 1)
    result = insert_untrusted_observation("alice", **_observation_kwargs())
    assert result.outcome == OUTCOME_CREATED
    assert result.example.trust_level == TRUST_LEVEL_UNTRUSTED
    assert result.example.provenance == PROVENANCE_OBSERVED_UNVERIFIED


def test_insert_untrusted_observation_has_no_trust_or_provenance_parameter():
    """Step 9B requirement, checked structurally: this function's own
    signature has no way to pass a trust_level or provenance at all."""
    import inspect
    params = inspect.signature(insert_untrusted_observation).parameters
    assert "trust_level" not in params
    assert "provenance" not in params


# ── Trust enforcement: raw/forged inputs are rejected ──────────────────────

def test_raw_trusted_insert_rejected(db_path):
    """A hand-built dict of 'trusted-looking' fields is never accepted in
    place of a real TrustedSqlExampleCandidate."""
    _insert_source(db_path, 1)
    with pytest.raises(ValueError):
        insert_trusted_example("alice", _observation_kwargs())  # a plain dict, not a candidate


def test_forged_trust_flag_rejected_via_direct_candidate_construction(db_path):
    """Even a hand-built TrustedSqlExampleCandidate (bypassing
    build_trusted_example_candidate entirely) is rejected. Omitting the
    private construction-guard field entirely is a plain Python TypeError
    (a required constructor argument is missing); supplying a fake one
    reaches __post_init__'s own explicit check, which raises
    ExampleCandidateRejected -- either way, direct construction never
    succeeds."""
    _insert_source(db_path, 1)
    with pytest.raises(TypeError):
        TrustedSqlExampleCandidate(
            source_id=1, schema_fingerprint="fp1", question_pattern="q", sql_template="SELECT 1",
            canonical_object_fqns=(), canonical_columns=(), join_path=(), result_shape=RESULT_SHAPE_COUNT,
            provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET, trust_level=TRUST_LEVEL_TRUSTED,
            search_document_text="x", search_document_hash="0" * 64, content_hash="0" * 64,
        )
    with pytest.raises(ExampleCandidateRejected):
        TrustedSqlExampleCandidate(
            source_id=1, schema_fingerprint="fp1", question_pattern="q", sql_template="SELECT 1",
            canonical_object_fqns=(), canonical_columns=(), join_path=(), result_shape=RESULT_SHAPE_COUNT,
            provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET, trust_level=TRUST_LEVEL_TRUSTED,
            search_document_text="x", search_document_hash="0" * 64, content_hash="0" * 64,
            _gate_guard=object(),
        )


def test_forged_objects_or_sql_rejected_via_failed_validation(db_path):
    """A caller cannot smuggle in an unvalidated SQL string or an
    unverified object list -- build_trusted_example_candidate requires a
    real, OK SqlValidationResult; a rejected one refuses to produce a
    candidate at all, so there is nothing to insert."""
    _insert_source(db_path, 1)
    bad_validation_result = SqlValidationResult(
        status="REJECTED", sql="SELECT * FROM dbo.Ghost", canonical_referenced_object_fqns=(),
        statement_type=None, build_id=1, schema_fingerprint="fp1", warnings=(),
        error="table not present in schema context",
    )
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE, validation_result=bad_validation_result,
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_untrusted_record_cannot_be_activated_via_status_update(db_path):
    """update_example_status has no trust_level parameter at all -- a
    STALE<->ACTIVE flip on an untrusted observation can never promote it
    to trusted, and it never leaves the untrusted-only index view."""
    _insert_source(db_path, 1)
    observed = insert_untrusted_observation("alice", **_observation_kwargs()).example
    assert update_example_status(observed.id, 1, "alice", status=STATUS_STALE) is True
    assert update_example_status(observed.id, 1, "alice", status=STATUS_ACTIVE) is True

    reloaded = get_example(observed.id, 1, "alice")
    assert reloaded.trust_level == TRUST_LEVEL_UNTRUSTED
    assert reloaded.status == STATUS_ACTIVE

    trusted_only = list_examples_for_source(1, "alice", trust_level=TRUST_LEVEL_TRUSTED)
    assert observed.id not in {e.id for e in trusted_only}


# ── Immutability ─────────────────────────────────────────────────────────

def test_update_status_only_changes_status_never_content(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    ok = update_example_status(created.id, 1, "alice", status=STATUS_STALE)
    assert ok is True
    updated = get_example(created.id, 1, "alice")
    assert updated.status == STATUS_STALE
    assert updated.question_pattern == created.question_pattern
    assert updated.sql_template == created.sql_template
    assert updated.content_hash == created.content_hash
    assert updated.trust_level == created.trust_level
    assert updated.provenance == created.provenance


def test_update_status_rejects_invalid_value(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    with pytest.raises(ValueError):
        update_example_status(created.id, 1, "alice", status="BOGUS")


def test_update_status_false_for_unowned(db_path):
    _insert_source(db_path, 1, user_id="alice")
    created = insert_trusted_example("alice", _trusted_candidate()).example
    assert update_example_status(created.id, 1, "mallory", status=STATUS_STALE) is False


# ── Cascade behavior ────────────────────────────────────────────────────

def test_deleting_source_cascades_to_examples(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    conn = _make_connection(db_path)
    conn.execute("DELETE FROM data_source_connections WHERE id = ?", (1,))
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM sql_examples WHERE id = ?", (created.id,)).fetchone()[0]
    conn.close()
    assert remaining == 0


# ── No credentials/result rows stored ──────────────────────────────────────

def test_schema_never_has_credential_or_result_row_columns(db_path):
    conn = _make_connection(db_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sql_examples)").fetchall()}
    conn.close()
    forbidden_substrings = ("password", "secret", "credential", "api_key", "result_row", "raw_prompt", "error_text")
    for col in cols:
        for bad in forbidden_substrings:
            assert bad not in col.lower(), f"unexpected column {col!r} on sql_examples"


# ── Embedding store: idempotency, ownership, hash validation ──────────────

def test_insert_example_embeddings_and_retrieve(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    result = insert_example_embeddings(1, "alice", "openai", "text-embedding-3-small", 4, {
        created.search_document_hash: [1.0, 0.0, 0.0, 0.0],
    })
    assert result.status == EMBED_STATUS_OK
    assert result.inserted_hashes == (created.search_document_hash,)

    stored = get_example_embeddings_for_source(1, "alice", "openai", "text-embedding-3-small", 4)
    assert len(stored) == 1
    assert stored[0].example_id == created.id


def test_reinsert_identical_vector_is_reused_not_duplicated(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    vec = {created.search_document_hash: [1.0, 0.0, 0.0, 0.0]}
    insert_example_embeddings(1, "alice", "openai", "m", 4, vec)
    second = insert_example_embeddings(1, "alice", "openai", "m", 4, vec)
    assert second.reused_hashes == (created.search_document_hash,)
    assert second.inserted_hashes == ()


def test_conflicting_vector_raises(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    insert_example_embeddings(1, "alice", "openai", "m", 4, {created.search_document_hash: [1.0, 0.0, 0.0, 0.0]})
    with pytest.raises(ValueError):
        insert_example_embeddings(1, "alice", "openai", "m", 4, {created.search_document_hash: [0.0, 1.0, 0.0, 0.0]})


def test_unknown_hash_raises(db_path):
    _insert_source(db_path, 1)
    insert_trusted_example("alice", _trusted_candidate())
    with pytest.raises(ValueError):
        insert_example_embeddings(1, "alice", "openai", "m", 4, {"0" * 64: [1.0, 0.0, 0.0, 0.0]})


def test_embedding_insert_unowned(db_path):
    _insert_source(db_path, 1, user_id="alice")
    created = insert_trusted_example("alice", _trusted_candidate()).example
    result = insert_example_embeddings(1, "mallory", "openai", "m", 4, {created.search_document_hash: [1.0, 0.0, 0.0, 0.0]})
    assert result.status == EMBED_STATUS_UNOWNED


def test_find_missing_hashes(db_path):
    _insert_source(db_path, 1)
    created = insert_trusted_example("alice", _trusted_candidate()).example
    missing = find_missing_example_search_document_hashes(1, "alice", "openai", "m", 4)
    assert missing == (created.search_document_hash,)
    insert_example_embeddings(1, "alice", "openai", "m", 4, {created.search_document_hash: [1.0, 0.0, 0.0, 0.0]})
    missing_after = find_missing_example_search_document_hashes(1, "alice", "openai", "m", 4)
    assert missing_after == ()
