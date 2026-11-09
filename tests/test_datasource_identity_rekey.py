"""
Focused tests — Phase 7, Step 6H1, Part 8: the identity key-ring
(core/secrets/datasource_identity.py's parse/verify/key_id support) and the
controlled, transactional rekey operation
(core/secrets/datasource_identity_rekey.py).

Run:
    venv/Scripts/pytest tests/test_datasource_identity_rekey.py -v
"""
import json
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "x" * 40)
os.environ.setdefault("USER_ID_SALT", "y" * 20)

import data.models as models_module
import core.secrets.datasource_identity as identity_module
import core.secrets.datasource_identity_rekey as rekey_module
from core.secrets.datasource_identity import (
    DEFAULT_KEY_ID,
    compute_datasource_identity_key,
    parse_identity_key,
    verify_identity_key,
)
from core.secrets.datasource_identity_rekey import rekey_datasource_identities
from core.secrets.manager import get_secret_manager


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    original = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original
    monkeypatch.setattr(rekey_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id, params, identity_key):
    secret = get_secret_manager().encrypt_secret(json.dumps(params))
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " config_schema_version, capabilities_json, metadata_json, source_status, is_active, "
        " datasource_identity_key, connection_state, created_at, updated_at) "
        "VALUES (?, ?, 'Test', 'mssql', 'RELATIONAL', ?, 1, '[]', '{}', 'ACTIVE', 1, ?, 'ACTIVE', 'now', 'now')",
        (source_id, user_id, secret, identity_key),
    )
    conn.commit()
    conn.close()


# ── Key-ring: no configuration -> unchanged legacy behavior ────────────────

def test_no_keyring_configured_produces_legacy_two_part_format(monkeypatch):
    monkeypatch.delenv("DATASOURCE_IDENTITY_HMAC_KEYRING", raising=False)
    key = compute_datasource_identity_key("u1", "mssql", {"host": "h", "port": 1433, "database": "d"})
    assert key.count(":") == 1
    assert parse_identity_key(key) == (DEFAULT_KEY_ID, key.split(":")[1])


# ── Key-ring: configured -> three-part format, explicit key_id ─────────────

def _ring_env(monkeypatch, current, keys):
    monkeypatch.setenv("DATASOURCE_IDENTITY_HMAC_KEYRING", json.dumps({"current_key_id": current, "keys": keys}))


def test_keyring_configured_produces_three_part_format_with_current_key_id(monkeypatch):
    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    key = compute_datasource_identity_key("u1", "mssql", {"host": "h", "port": 1433, "database": "d"})
    key_id, digest = parse_identity_key(key)
    assert key_id == "k2"
    assert len(digest) == 64


def test_keyring_different_key_ids_produce_different_keys(monkeypatch):
    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    params = {"host": "h", "port": 1433, "database": "d"}
    k_old = compute_datasource_identity_key("u1", "mssql", params, key_id="k1")
    k_new = compute_datasource_identity_key("u1", "mssql", params, key_id="k2")
    assert k_old != k_new


def test_verify_identity_key_recognizes_a_record_under_an_older_still_listed_key(monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    params = {"host": "h", "port": 1433, "database": "d"}
    old_key = compute_datasource_identity_key("u1", "mssql", params)

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    assert verify_identity_key(old_key, "u1", "mssql", params) is True


def test_verify_identity_key_fails_once_the_old_key_id_is_removed_from_the_ring(monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    params = {"host": "h", "port": 1433, "database": "d"}
    old_key = compute_datasource_identity_key("u1", "mssql", params)

    _ring_env(monkeypatch, "k2", {"k2": "secret-two"})  # k1 fully removed
    assert verify_identity_key(old_key, "u1", "mssql", params) is False


def test_malformed_keyring_env_falls_back_to_default_single_key(monkeypatch):
    monkeypatch.setenv("DATASOURCE_IDENTITY_HMAC_KEYRING", "{not valid json")
    key = compute_datasource_identity_key("u1", "mssql", {"host": "h", "port": 1433, "database": "d"})
    assert parse_identity_key(key)[0] == DEFAULT_KEY_ID


def test_parse_identity_key_returns_none_for_garbage():
    assert parse_identity_key("not-a-key") is None
    assert parse_identity_key("") is None
    assert parse_identity_key("v2:abc") is None  # unknown version


# ── Rekey operation ─────────────────────────────────────────────────────────

_PARAMS_A = {"host": "host-a", "port": 1433, "database": "db-a"}
_PARAMS_B = {"host": "host-b", "port": 1433, "database": "db-b"}


def test_rekey_dry_run_never_writes(db_path, monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    old_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A)
    _insert_source(db_path, 1, "u1", _PARAMS_A, old_key)

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    report = rekey_datasource_identities("k1", "k2", dry_run=True)

    assert report.candidate_count == 1
    assert report.rekeyed_count == 1
    assert report.committed is False

    conn = _make_connection(db_path)
    row = conn.execute("SELECT datasource_identity_key FROM data_source_connections WHERE id=1").fetchone()
    conn.close()
    assert row["datasource_identity_key"] == old_key  # unchanged


def test_rekey_applies_and_audits_when_dry_run_false(db_path, monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    old_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A)
    _insert_source(db_path, 1, "u1", _PARAMS_A, old_key)

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    expected_new_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A, key_id="k2")

    report = rekey_datasource_identities("k1", "k2", dry_run=False)
    assert report.committed is True
    assert report.rekeyed_count == 1

    conn = _make_connection(db_path)
    row = conn.execute("SELECT datasource_identity_key FROM data_source_connections WHERE id=1").fetchone()
    audit = conn.execute(
        "SELECT action, reason FROM datasource_lifecycle_audit WHERE action='identity_rekey'"
    ).fetchone()
    conn.close()
    assert row["datasource_identity_key"] == expected_new_key
    assert audit is not None
    assert "committed=True" in audit["reason"]
    # Never leaks the locator/credential values into the audit reason.
    assert "host-a" not in audit["reason"]
    assert "db-a" not in audit["reason"]


def test_rekey_only_touches_rows_with_the_matching_from_key_id(db_path, monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    key_a_under_k1 = compute_datasource_identity_key("u1", "mssql", _PARAMS_A)
    _insert_source(db_path, 1, "u1", _PARAMS_A, key_a_under_k1)

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    key_b_under_k2 = compute_datasource_identity_key("u2", "mssql", _PARAMS_B)
    _insert_source(db_path, 2, "u2", _PARAMS_B, key_b_under_k2)

    report = rekey_datasource_identities("k1", "k2", dry_run=False)
    assert report.candidate_count == 1  # only source 1 (key_id=k1)
    assert report.rekeyed_count == 1

    conn = _make_connection(db_path)
    row_b = conn.execute("SELECT datasource_identity_key FROM data_source_connections WHERE id=2").fetchone()
    conn.close()
    assert row_b["datasource_identity_key"] == key_b_under_k2  # source 2 untouched


def test_rekey_detects_collision_and_aborts_without_writing(db_path, monkeypatch):
    # Two DIFFERENT physical databases that happen to produce the SAME new
    # key under to_key_id -- simulated directly by inserting a row whose
    # CURRENT key already equals what source 1 would become.
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    old_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A)
    _insert_source(db_path, 1, "u1", _PARAMS_A, old_key)

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    collision_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A, key_id="k2")
    _insert_source(db_path, 2, "u1", _PARAMS_B, collision_key)  # pre-existing row already holds the WOULD-BE new key

    report = rekey_datasource_identities("k1", "k2", dry_run=False)
    assert report.committed is False
    assert len(report.collisions) == 1
    assert sorted(report.collisions[0]["source_ids"]) == [1, 2]
    assert report.error is not None

    conn = _make_connection(db_path)
    row1 = conn.execute("SELECT datasource_identity_key FROM data_source_connections WHERE id=1").fetchone()
    audit = conn.execute("SELECT reason FROM datasource_lifecycle_audit WHERE action='identity_rekey'").fetchone()
    conn.close()
    assert row1["datasource_identity_key"] == old_key  # nothing written
    assert audit is not None  # the aborted attempt is still audited
    assert "collisions=1" in audit["reason"]


def test_rekey_skips_rows_with_undecryptable_config_but_still_processes_others(db_path, monkeypatch):
    _ring_env(monkeypatch, "k1", {"k1": "secret-one"})
    old_key = compute_datasource_identity_key("u1", "mssql", _PARAMS_A)
    _insert_source(db_path, 1, "u1", _PARAMS_A, old_key)

    # Row 2: stored encrypted_config_json is garbage -- must not crash the batch.
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " config_schema_version, capabilities_json, metadata_json, source_status, is_active, "
        " datasource_identity_key, connection_state, created_at, updated_at) "
        "VALUES (2, 'u2', 'Broken', 'mssql', 'RELATIONAL', 'not-valid-fernet-token', 1, '[]', '{}', "
        "'ACTIVE', 1, ?, 'ACTIVE', 'now', 'now')",
        (compute_datasource_identity_key("u2", "mssql", _PARAMS_B),),
    )
    conn.commit()
    conn.close()

    _ring_env(monkeypatch, "k2", {"k1": "secret-one", "k2": "secret-two"})
    report = rekey_datasource_identities("k1", "k2", dry_run=False)
    # Row 2 was ALSO stored under key_id=k1 (both rows inserted while the
    # ring's current_key_id was still "k1"), so it's a genuine candidate
    # that fails only at decryption -- proving one bad row never blocks
    # the rest of the batch.
    assert report.candidate_count == 2
    assert report.skipped_decrypt_failures == [2]
    assert report.committed is True
    assert report.rekeyed_count == 1


def test_rekey_requires_different_from_and_to_key_ids(db_path):
    with pytest.raises(ValueError):
        rekey_datasource_identities("k1", "k1")
