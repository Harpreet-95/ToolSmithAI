"""
Focused tests — Phase 7, Step 6H, Part 4/10: the durable (SQLite-persisted)
fast-path plan store (core/sqlservice/durable_plan_store.py).

Uses a temporary on-disk SQLite database (via the real data.models.init_db(),
monkeypatched to a temp file) — never the real data/toolsmith.db, never SQL
Server, never OpenAI.

Run:
    venv/Scripts/pytest tests/test_sqlservice_durable_plan_store.py -v
"""
import os
import shutil
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import core.sqlservice.durable_plan_store as store_module
from core.sqlservice.durable_plan_store import (
    DURABLE_PLAN_FORMAT_VERSION,
    DurablePlan,
    build_durable_plan_key,
    durable_plan_count,
    evict_durable_plan,
    get_durable_plan,
    normalize_durable_question,
    purge_expired_durable_plans,
    store_durable_plan,
)
from core.sqlservice.fast_path_generation_service import FastPathIntent, FastPathObjectReason


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
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(path))
    return path


_INTENT = FastPathIntent(
    operation="COUNT", entities=("students",), measures=(), filters=(),
    date_constraints=(), groupings=(), requested_attributes=(),
)
_OBJECT_REASONS = (FastPathObjectReason(object_fqn="dbo.EDU_Student", supporting_columns=("StudentID",)),)


def _key(**overrides):
    defaults = dict(
        question="How many students are there?",
        datasource_identity_key="v1:abc123",
        user_id="user-1",
        schema_fingerprint="fp-1",
        retrieval_build_id=7,
        business_knowledge_package_id=1,
        business_knowledge_package_version=1,
        fast_path_model="gpt-6-sol",
        fast_path_reasoning_effort="medium",
        fast_path_prompt_version="v3",
        view_profile_state=(10, "2026-01-01"),
        secret_policy_version=2,
    )
    defaults.update(overrides)
    return build_durable_plan_key(**defaults)


def _plan(**overrides):
    defaults = dict(
        intent=_INTENT, selected_object_fqns=("dbo.EDU_Student",), object_reasons=_OBJECT_REASONS,
        sql="SELECT COUNT(*) FROM dbo.EDU_Student", decision_evidence=("matched entity: students",),
        confidence=0.95, model="gpt-6-sol", follow_up_resolution_type="NONE",
    )
    defaults.update(overrides)
    return DurablePlan(**defaults)


def test_normalize_durable_question_matches_in_memory_normalization():
    # Must byte-for-byte match fast_path_plan_cache.normalize_fast_path_question
    # -- a question hitting one tier must hit the other under the same identity.
    from core.sqlservice.fast_path_plan_cache import normalize_fast_path_question
    raw = "  How Many   Students Are There?  "
    assert normalize_durable_question(raw) == normalize_fast_path_question(raw)


def test_store_then_get_roundtrips_exactly(db_path):
    key = _key()
    plan = _plan()
    store_durable_plan(key, plan)
    got = get_durable_plan(key)
    assert got is not None
    assert got == plan


def test_miss_when_never_stored(db_path):
    assert get_durable_plan(_key()) is None


@pytest.mark.parametrize("field,other_value", [
    ("datasource_identity_key", "v1:different"),
    ("user_id", "user-2"),
    ("schema_fingerprint", "fp-2"),
    ("retrieval_build_id", 999),
    ("business_knowledge_package_id", 2),
    ("business_knowledge_package_version", 2),
    ("fast_path_model", "gpt-7"),
    ("fast_path_reasoning_effort", "high"),
    ("fast_path_prompt_version", "v4"),
    ("secret_policy_version", 3),
])
def test_any_identity_field_change_misses(db_path, field, other_value):
    store_durable_plan(_key(), _plan())
    assert get_durable_plan(_key(**{field: other_value})) is None


def test_different_question_misses(db_path):
    store_durable_plan(_key(), _plan())
    assert get_durable_plan(_key(question="Show me all instructors.")) is None


def test_view_profile_state_change_misses(db_path):
    store_durable_plan(_key(), _plan())
    assert get_durable_plan(_key(view_profile_state=(11, "2026-01-02"))) is None


def test_ttl_expiry_is_a_safe_miss_and_self_evicts(db_path):
    key = _key()
    store_durable_plan(key, _plan(), ttl_seconds=0.01)
    time.sleep(0.05)
    assert get_durable_plan(key, ttl_seconds=0.01) is None
    # Row must actually be gone, not just skipped -- a repeat lookup with a
    # generous TTL must still miss (never resurrected).
    assert get_durable_plan(key, ttl_seconds=10_000) is None


def test_zero_ttl_means_no_expiry(db_path):
    key = _key()
    store_durable_plan(key, _plan(), ttl_seconds=0)
    assert get_durable_plan(key, ttl_seconds=0) is not None


def test_version_mismatch_is_a_safe_miss(db_path, monkeypatch):
    key = _key()
    store_durable_plan(key, _plan())
    monkeypatch.setattr(store_module, "DURABLE_PLAN_FORMAT_VERSION", DURABLE_PLAN_FORMAT_VERSION + 1)
    assert get_durable_plan(key) is None


def test_corrupted_payload_is_a_safe_miss_and_is_deleted(db_path):
    key = _key()
    store_durable_plan(key, _plan())
    conn = store_module.get_connection()
    try:
        conn.execute(
            "UPDATE fast_path_durable_plans SET plan_payload_json = ? WHERE cache_key_hash = ?",
            ("{not valid json", key.hash()),
        )
        conn.commit()
    finally:
        conn.close()
    assert get_durable_plan(key) is None
    assert durable_plan_count() == 0  # the corrupted row was cleaned up, not left behind


def test_storage_error_is_a_safe_miss_not_an_exception(db_path, monkeypatch):
    monkeypatch.setattr(store_module, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert get_durable_plan(_key()) is None  # never raises


def test_store_is_best_effort_on_connection_failure(db_path, monkeypatch):
    monkeypatch.setattr(store_module, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    store_durable_plan(_key(), _plan())  # never raises


def test_evict_removes_entry(db_path):
    key = _key()
    store_durable_plan(key, _plan())
    assert get_durable_plan(key) is not None
    evict_durable_plan(key)
    assert get_durable_plan(key) is None


def test_hit_increments_use_count_and_last_used(db_path):
    key = _key()
    store_durable_plan(key, _plan())
    get_durable_plan(key)
    get_durable_plan(key)
    conn = store_module.get_connection()
    try:
        row = conn.execute(
            "SELECT use_count FROM fast_path_durable_plans WHERE cache_key_hash = ?", (key.hash(),)
        ).fetchone()
    finally:
        conn.close()
    assert row["use_count"] == 2


def test_purge_expired_removes_only_expired_rows(db_path):
    fresh_key = _key(question="fresh question")
    stale_key = _key(question="stale question")
    store_durable_plan(fresh_key, _plan(), ttl_seconds=10_000)
    store_durable_plan(stale_key, _plan(), ttl_seconds=0.01)
    time.sleep(0.05)
    removed = purge_expired_durable_plans()
    assert removed == 1
    assert durable_plan_count() == 1


def test_max_rows_per_identity_evicts_least_recently_used(db_path):
    identity = "v1:bounded"
    for i in range(5):
        key = _key(datasource_identity_key=identity, question=f"question {i}")
        store_durable_plan(key, _plan(), max_rows_per_identity=3)
    assert durable_plan_count(identity) == 3
    # The three most-recently-stored should survive; the earliest two evicted.
    assert get_durable_plan(_key(datasource_identity_key=identity, question="question 0")) is None
    assert get_durable_plan(_key(datasource_identity_key=identity, question="question 4")) is not None


def test_store_is_idempotent_upsert_on_identical_key(db_path):
    key = _key()
    store_durable_plan(key, _plan(sql="SELECT 1"))
    store_durable_plan(key, _plan(sql="SELECT 2"))
    assert durable_plan_count() == 1
    got = get_durable_plan(key)
    assert got.sql == "SELECT 2"
