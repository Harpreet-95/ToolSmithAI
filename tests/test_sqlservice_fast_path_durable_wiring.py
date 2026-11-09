"""
Pipeline-level tests — Phase 7, Step 6H, Part 5/6/7: the durable plan store
wired into core.sqlservice.ai_first_fast_path_service's runtime hierarchy
(in-memory cache -> durable store -> embedding+GPT-6 Sol only if both miss).

Reuses the exact same SQLite fixture harness as
tests/test_sqlservice_fast_path_plan_cache.py. `get_datasource_identity_key`
is monkeypatched to a fixed value for most tests -- the real identity-key
computation and its wiring into data_source_connections is already covered
end-to-end by tests/test_datasource_lifecycle_identity.py; this file's job
is proving the fast path's OWN runtime hierarchy (what gets skipped on a
durable hit, what always re-runs, restart/isolation semantics), not
re-deriving the identity key itself.

No real OpenAI call, no real SQL Server, anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_fast_path_durable_wiring.py -v
"""
import sqlite3

import pytest

import core.sqlservice.ai_first_fast_path_service as fast_path_module
import core.sqlservice.durable_plan_store as durable_plan_store_module
from core.sqlservice.ai_first_fast_path_service import answer_question_ai_first_fast_path
from core.sqlservice.ai_first_result import STATUS_OK
from core.sqlservice.durable_plan_store import durable_plan_count
from core.sqlservice.fast_path_plan_cache import clear_fast_path_plan_cache

from tests.test_sqlservice_ai_first_fast_path_service import _fast_path_provider
from tests.sqlservice_fixtures import (
    USER_ID,
    FakeConnection,
    FakeCursor,
    FakeEmbeddingProvider,
    _connection_factory_for,
    _prepare_source,
    _wire_execution_lookups,
    template_db_path,  # noqa: F401 -- re-exported fixture
)
from tests.test_sqlservice_fast_path_plan_cache import db_path  # noqa: F401 -- re-exported fixture

_IDENTITY_KEY = "v1:" + "a" * 64


@pytest.fixture(autouse=True)
def _patch_durable_store_connection(db_path, monkeypatch):
    """The shared db_path fixture (test_sqlservice_fast_path_plan_cache.py)
    patches get_connection on every OTHER module that holds its own
    `from data.db import get_connection` reference -- durable_plan_store is
    new, so it needs the same treatment here, or every call would fall
    through to the real data/toolsmith.db and trip conftest's test guard."""
    def _make_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    monkeypatch.setattr(durable_plan_store_module, "get_connection", _make_connection)


def _patch_identity(monkeypatch, value=_IDENTITY_KEY):
    monkeypatch.setattr(fast_path_module, "get_datasource_identity_key", lambda source_id, user_id: value)


def _run(
    db_path, monkeypatch, build, *,
    question="How many widgets are there?", connection_rows=None,
    plan_cache_enabled=False, durable_plan_cache_enabled=True,
):
    _wire_execution_lookups(monkeypatch, build)
    embedding_provider = FakeEmbeddingProvider()
    fast_path_provider = _fast_path_provider()
    rows = connection_rows if connection_rows is not None else [(2,)]
    cursor = FakeCursor(("Count",), rows)
    connection = FakeConnection(cursor)
    connection_factory = _connection_factory_for(connection)
    result = answer_question_ai_first_fast_path(
        question, build.source_id, USER_ID, embedding_provider, fast_path_provider, None,
        connection_factory=connection_factory,
        plan_cache_enabled=plan_cache_enabled, durable_plan_cache_enabled=durable_plan_cache_enabled,
    )
    return result, embedding_provider, fast_path_provider, connection_factory


def test_first_request_makes_one_embedding_and_one_model_call_and_persists(db_path, monkeypatch):
    _patch_identity(monkeypatch)
    build = _prepare_source(db_path)
    result, embedding_provider, fast_path_provider, _ = _run(db_path, monkeypatch, build)

    assert result.status == STATUS_OK
    assert result.cache_hit is False
    assert len(embedding_provider.calls) == 1
    assert len(fast_path_provider.calls) == 1
    assert durable_plan_count() == 1


def test_simulated_backend_restart_still_hits_durable_store(db_path, monkeypatch):
    """Phase 7, Step 6H, Part 6 -- the in-memory cache is explicitly cleared
    between the two calls (simulating a fresh process with an empty
    OrderedDict, exactly what a real restart produces) while the durable
    store is backed by the same on-disk SQLite file throughout (exactly
    what a real restart preserves) -- proving the durable tier, not the
    in-memory tier, is what survives."""
    _patch_identity(monkeypatch)
    build = _prepare_source(db_path)

    first, _, _, first_conn = _run(db_path, monkeypatch, build)
    assert first.status == STATUS_OK
    assert durable_plan_count() == 1

    clear_fast_path_plan_cache()  # the only in-process state a real restart would also drop

    second, embedding_provider_2, fast_path_provider_2, second_conn = _run(db_path, monkeypatch, build)

    assert second.status == STATUS_OK
    assert second.cache_hit is True
    assert len(embedding_provider_2.calls) == 0, "a durable hit must make zero embedding calls"
    assert len(fast_path_provider_2.calls) == 0, "a durable hit must make zero GPT-6 Sol calls"
    # Validation + live execution still ran, against a fresh connection --
    # never a shortcut around either.
    assert len(second_conn.calls) == 1
    assert first_conn is not second_conn


def test_durable_hit_reexecutes_live_so_changed_data_is_reflected(db_path, monkeypatch):
    """Phase 7, Step 6H, Part 6, steps 8/9 -- same reused plan, different
    underlying row data between the two calls -> the returned value must
    change, proving a durable hit never replays a stale result."""
    _patch_identity(monkeypatch)
    build = _prepare_source(db_path)

    first, _, _, _ = _run(db_path, monkeypatch, build, connection_rows=[(2,)])
    assert first.status == STATUS_OK
    assert first.rows[0][0] == 2

    second, _, _, _ = _run(db_path, monkeypatch, build, connection_rows=[(999,)])
    assert second.status == STATUS_OK
    assert second.cache_hit is True
    assert second.rows[0][0] == 999, (
        "a durable hit must reflect freshly executed data, never a cached result value"
    )


def test_durable_tier_disabled_never_hits(db_path, monkeypatch):
    _patch_identity(monkeypatch)
    build = _prepare_source(db_path)
    _run(db_path, monkeypatch, build, durable_plan_cache_enabled=False)
    clear_fast_path_plan_cache()
    second, embedding_provider_2, fast_path_provider_2, _ = _run(
        db_path, monkeypatch, build, durable_plan_cache_enabled=False,
    )
    assert second.status == STATUS_OK
    assert second.cache_hit is False
    assert len(embedding_provider_2.calls) == 1
    assert len(fast_path_provider_2.calls) == 1


def test_missing_identity_key_disables_durable_tier_safely(db_path, monkeypatch):
    """No identity key (e.g. a pre-migration row never backfilled) must be
    a safe no-op for this tier, never an error, and never silently falls
    back to source_id."""
    monkeypatch.setattr(fast_path_module, "get_datasource_identity_key", lambda source_id, user_id: None)
    build = _prepare_source(db_path)
    first, _, _, _ = _run(db_path, monkeypatch, build)
    assert first.status == STATUS_OK
    assert durable_plan_count() == 0

    clear_fast_path_plan_cache()
    second, embedding_provider_2, fast_path_provider_2, _ = _run(db_path, monkeypatch, build)
    assert second.status == STATUS_OK
    assert len(embedding_provider_2.calls) == 1, "with no identity key, every request must be a fresh miss"
    assert len(fast_path_provider_2.calls) == 1


def test_different_identity_key_never_inherits_anothers_durable_plan(db_path, monkeypatch):
    """Phase 7, Step 6H, Part 7, step 10 -- two different physical
    databases (different stable identity, e.g. after a permanent delete and
    a brand-new connection) must never share a durable plan even when every
    OTHER identity field (user, schema_fingerprint, build, model...) is
    identical in this fixture harness."""
    build = _prepare_source(db_path)

    _patch_identity(monkeypatch, value="v1:" + "a" * 64)
    first, _, _, _ = _run(db_path, monkeypatch, build)
    assert first.status == STATUS_OK
    assert durable_plan_count() == 1

    clear_fast_path_plan_cache()
    _patch_identity(monkeypatch, value="v1:" + "b" * 64)
    second, embedding_provider_2, fast_path_provider_2, _ = _run(db_path, monkeypatch, build)
    assert second.status == STATUS_OK
    assert second.cache_hit is False
    assert len(embedding_provider_2.calls) == 1, "a different identity key must never reuse the first database's plan"
    assert len(fast_path_provider_2.calls) == 1
    assert durable_plan_count() == 2, "both plans persist, scoped to their own identity"


def test_in_memory_tier_is_checked_before_durable_tier(db_path, monkeypatch):
    """When both tiers are enabled, an in-memory hit must short-circuit
    before the durable store is ever consulted (Part 5's explicit ordering:
    in-memory -> durable -> AI)."""
    _patch_identity(monkeypatch)
    build = _prepare_source(db_path)
    first, _, _, _ = _run(db_path, monkeypatch, build, plan_cache_enabled=True, durable_plan_cache_enabled=True)
    assert first.status == STATUS_OK
    assert durable_plan_count() == 1  # stored into the durable tier too

    # Do NOT clear the in-memory cache this time -- it should serve the hit
    # before durable_plan_store.get_durable_plan is ever called.
    original_get_durable = fast_path_module.get_durable_plan
    calls = []

    def _tracking_get_durable(*args, **kwargs):
        calls.append(1)
        return original_get_durable(*args, **kwargs)

    monkeypatch.setattr(fast_path_module, "get_durable_plan", _tracking_get_durable)

    second, embedding_provider_2, fast_path_provider_2, _ = _run(
        db_path, monkeypatch, build, plan_cache_enabled=True, durable_plan_cache_enabled=True,
    )
    assert second.status == STATUS_OK
    assert len(embedding_provider_2.calls) == 0
    assert len(fast_path_provider_2.calls) == 0
    assert calls == [], "the durable store must never be consulted once the in-memory tier already hit"
