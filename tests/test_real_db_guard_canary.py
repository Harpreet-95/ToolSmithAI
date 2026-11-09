"""
Phase 5, Step 6C — canary proving the real-DB guard in the root conftest.py
actually blocks access to data/toolsmith.db, before SQLite ever opens it,
across the path-equivalent forms a test (or a stale module alias) might use.

Every assertion here is a *negative* test: it proves the connection attempt
is rejected and that no on-disk artifact (no growth of the real file, no
new -wal/-shm sidecar) is produced by the attempt itself. None of these
tests ever successfully open the real DB.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest

import conftest as _guard_conftest
import data.db as db_module


def _real_db_snapshot() -> tuple[int, bool, bool]:
    path = _guard_conftest._REAL_DB_PATH
    size = path.stat().st_size if path.exists() else -1
    wal_exists = path.with_name(path.name + "-wal").exists()
    shm_exists = path.with_name(path.name + "-shm").exists()
    return size, wal_exists, shm_exists


def test_direct_sqlite3_connect_to_real_path_is_blocked():
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        sqlite3.connect(str(_guard_conftest._REAL_DB_PATH))
    assert _real_db_snapshot() == before


def test_get_connection_with_unpatched_real_db_path_is_blocked(monkeypatch):
    # Force data.db.DB_PATH back to its real, unpatched value in case an
    # earlier test in this session left it monkeypatched to a temp file —
    # this test specifically wants to prove the *real* path is guarded.
    monkeypatch.setattr(db_module, "DB_PATH", _guard_conftest._REAL_DB_PATH)
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        db_module.get_connection()
    assert _real_db_snapshot() == before


def test_relative_path_form_of_real_db_is_blocked(monkeypatch):
    real = _guard_conftest._REAL_DB_PATH
    monkeypatch.chdir(real.parent)
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        sqlite3.connect(real.name)  # relative — resolves against new cwd
    assert _real_db_snapshot() == before


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive path check is Windows-specific")
def test_case_different_form_of_real_db_is_blocked():
    real = _guard_conftest._REAL_DB_PATH
    swapped = str(real).swapcase()
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        sqlite3.connect(swapped)
    assert _real_db_snapshot() == before


def test_file_uri_form_of_real_db_is_blocked():
    real = _guard_conftest._REAL_DB_PATH
    uri = "file:" + str(real).replace("\\", "/") + "?mode=ro"
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        sqlite3.connect(uri, uri=True)
    assert _real_db_snapshot() == before


def test_symlink_or_junction_to_real_db_is_blocked(tmp_path):
    real = _guard_conftest._REAL_DB_PATH
    link = tmp_path / "toolsmith_link.db"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    before = _real_db_snapshot()
    with pytest.raises(RuntimeError, match="BLOCKED BY TEST GUARD"):
        sqlite3.connect(str(link))
    assert _real_db_snapshot() == before


def test_memory_and_temp_db_connections_are_not_blocked(tmp_path):
    # Control case: the guard must not be so broad it breaks legitimate
    # isolated-test usage.
    conn = sqlite3.connect(":memory:")
    conn.close()
    tmp_db = tmp_path / "isolated.db"
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()
    assert tmp_db.exists()


def test_live_real_db_marker_is_auto_skipped_without_env_opt_in():
    # Simulates pytest's collection-time skip logic without actually
    # collecting/running a live test (which would require the env var and a
    # real connection, exactly what this suite must never do implicitly).
    class _FakeConfig:
        pass

    class _FakeNode:
        def __init__(self, marked: bool) -> None:
            self._marked = marked
            self.markers_added = []

        def get_closest_marker(self, name):
            return object() if self._marked else None

        def add_marker(self, marker):
            self.markers_added.append(marker)

    marked_item = _FakeNode(marked=True)
    unmarked_item = _FakeNode(marked=False)

    saved = os.environ.pop(_guard_conftest._LIVE_DB_ENV_VAR, None)
    try:
        _guard_conftest.pytest_collection_modifyitems(_FakeConfig(), [marked_item, unmarked_item])
    finally:
        if saved is not None:
            os.environ[_guard_conftest._LIVE_DB_ENV_VAR] = saved

    assert len(marked_item.markers_added) == 1
    assert len(unmarked_item.markers_added) == 0
