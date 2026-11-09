"""
Phase 5, Step 6C — real metadata-database isolation guard.

This is the ONLY place in the test suite allowed to know about the real
data/toolsmith.db path. It patches sqlite3.connect() itself (not
data.db.get_connection specifically) so that no code path — get_connection(),
a direct sqlite3.connect() call, a stale module-level alias of DB_PATH
(e.g. api/app.py, data/migrate_data_dir.py), or a background thread that
outlives the test that spawned it — can open the real on-disk metadata DB
during an ordinary pytest run, regardless of which module or how many
indirections it went through to get there.

The one escape hatch: a test marked @pytest.mark.live_real_db AND run with
TOOLSMITH_ALLOW_REAL_DB_TESTS=1 in the environment. Both are required.
Without the env var, such tests are auto-skipped at collection time — they
never run, so they can never trip (or rely on) the guard by accident.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from urllib.parse import unquote, urlparse

import pytest

# A fresh clone intentionally has no private .env file. Supply deterministic,
# test-only values before importing application configuration so collection is
# reproducible without weakening the production startup checks.
os.environ.setdefault("ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("USER_ID_SALT", "test-user-id-salt-000000000000000000000000000000000000000000000000")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-000000000000000000000000000000000000000000000000")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-api-key-000000000000000000000000")
os.environ.setdefault("USER_API_KEY", "test-user-api-key-0000000000000000000000000")
# Keep optional background/AI/cache capabilities deterministic in tests without
# making production configuration inspect whether pytest is imported.
os.environ["AUTONOMOUS_METADATA_ON_CONNECT_ENABLED"] = "false"
os.environ["AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED"] = "false"
os.environ["AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED"] = "false"
os.environ["BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED"] = "false"

import data.db as _db_module

# Captured once, at collection time, before any test has a chance to
# monkeypatch data.db.DB_PATH. This is the actual production path
# (TOOLSMITH_DATA_DIR-aware, via data.db's own resolution) — not a
# hardcoded guess — so the guard tracks wherever the real DB really lives.
_REAL_DB_PATH = pathlib.Path(_db_module.DB_PATH).resolve()

_LIVE_DB_MARKER = "live_real_db"
_LIVE_DB_ENV_VAR = "TOOLSMITH_ALLOW_REAL_DB_TESTS"

_live_test_active = False  # set True only while a live_real_db-marked test body runs


def _normalize_target(database: object) -> pathlib.Path | None:
    """Best-effort resolution of whatever sqlite3.connect() was given into an
    absolute, resolved filesystem path — or None for :memory:/temp/unresolvable
    targets, which can never be the real DB."""
    if database is None:
        return None
    if isinstance(database, (int,)):
        return None  # sqlite3 file descriptor form — never the real DB by path
    s = os.fspath(database) if hasattr(database, "__fspath__") else str(database)
    if s == ":memory:" or s == "":
        return None
    if s.startswith("file:"):
        parsed = urlparse(s)
        s = unquote(parsed.path)
        if os.name == "nt" and s.startswith("/") and len(s) > 2 and s[2] == ":":
            s = s[1:]
        if not s:
            return None
    try:
        p = pathlib.Path(s)
        if not p.is_absolute():
            p = pathlib.Path.cwd() / p
        return p.resolve()
    except (OSError, ValueError):
        return None


def _is_real_db_target(database: object) -> bool:
    resolved = _normalize_target(database)
    if resolved is None:
        return False
    # Windows paths are case-insensitive; str comparison after resolve()
    # also collapses relative/absolute and symlink forms to the same value.
    return str(resolved).lower() == str(_REAL_DB_PATH).lower()


_original_sqlite_connect = sqlite3.connect


def _guarded_sqlite_connect(database, *args, **kwargs):
    if _is_real_db_target(database):
        allowed = _live_test_active and os.environ.get(_LIVE_DB_ENV_VAR) == "1"
        if not allowed:
            raise RuntimeError(
                "BLOCKED BY TEST GUARD: an ordinary pytest run attempted to open "
                f"the real metadata database at {_REAL_DB_PATH}. Tests must use an "
                "isolated temporary SQLite database (tmp_path + monkeypatch the "
                "relevant DB_PATH/get_connection), never the real one. If this is "
                "a deliberately authorized live-integration test, mark it "
                f"@pytest.mark.{_LIVE_DB_MARKER} and run with "
                f"{_LIVE_DB_ENV_VAR}=1 set."
            )
    return _original_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{_LIVE_DB_MARKER}: explicitly authorized test that may touch the real "
        f"metadata DB. Skipped unless {_LIVE_DB_ENV_VAR}=1 is set in the "
        "environment.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    if os.environ.get(_LIVE_DB_ENV_VAR) == "1":
        return
    skip_marker = pytest.mark.skip(
        reason=(
            f"live_real_db test skipped by default — set {_LIVE_DB_ENV_VAR}=1 "
            "to opt in explicitly."
        )
    )
    for item in items:
        if item.get_closest_marker(_LIVE_DB_MARKER) is not None:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _live_real_db_guard_toggle(request: pytest.FixtureRequest):
    """Flips the guard's escape hatch on only for the duration of a test body
    that both carries @pytest.mark.live_real_db and ran with the env opt-in
    (collection already skipped it otherwise, so reaching here means both
    conditions held)."""
    global _live_test_active
    is_live = request.node.get_closest_marker(_LIVE_DB_MARKER) is not None
    if is_live:
        _live_test_active = True
    try:
        yield
    finally:
        _live_test_active = False
