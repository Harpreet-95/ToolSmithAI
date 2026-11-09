"""
Phase 7, Step 6H, Part 8 — cross-computer portability proof: a signed
package exported from one ToolSmithAI "installation" (its own independent
SQLite metadata file) is imported and verified on a SECOND, completely
separate installation (a different SQLite file, never sharing a connection
or any in-process cache with the first).

This cannot literally run on two physical computers inside one test
process, but two independently initialized SQLite files with no shared
state between them (re-pointing every module's get_connection() in turn, the
SAME technique tests/test_portable_knowledge_layer.py already uses to
simulate a fresh reconnect) is the closest available proof that the
signature/fingerprint verification logic does not implicitly depend on
anything installation-local (in particular: it must never depend on
core.secrets.datasource_identity's per-installation HMAC key, and it must
never depend on source_id matching between the two sides).

Run:
    venv/Scripts/pytest tests/test_portable_knowledge_cross_install_signing.py -v
"""
import os

import data.portable_knowledge_service as pks
from tests.test_portable_knowledge_layer import (  # test harness reuse only
    _CLIENT_TABLE,
    _NOW,
    _STUDENT_TABLE,
    _db_conn,
    _insert_vocabulary_row,
    _write_snapshot,
    env,
)

_SHARED_SIGNING_KEY = "shared-out-of-band-secret-both-installations-configured"


def _install(tmp_path, monkeypatch, name: str, *, source_id: int = 1):
    install_dir = tmp_path / name
    install_dir.mkdir()
    return env(install_dir, monkeypatch, source_id=source_id)


def test_export_from_install_a_import_into_install_b_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTABLE_PACKAGE_SIGNING_KEY", _SHARED_SIGNING_KEY)

    # ---- Installation A: build real validated knowledge, export+sign ----
    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)

    exported = pks.export_signed_package(1, "u1", "cross_install_test_v1", directory=str(tmp_path / "exports"))
    assert exported is not None
    assert exported["signature_status"] == pks.SIGNATURE_STATUS_VALID
    package = exported["package"]
    assert package["signature"]
    assert pks.verify_package_signature(package) == pks.SIGNATURE_STATUS_VALID

    # Never leaks a machine-local source_id or any credential-shaped key.
    assert "source_id" not in str(package.keys())
    assert pks.audit_package_for_unsafe_content(package) == []

    # ---- Installation B: a completely separate SQLite file, same schema -
    db_b = _install(tmp_path, monkeypatch, "install_b", source_id=2)
    _write_snapshot(db_b, 2, [_STUDENT_TABLE])

    result = pks.import_signed_package(package, 2, "u1")
    assert result["accepted"] is True
    assert result["signature_status"] == pks.SIGNATURE_STATUS_VALID
    assert result["validation"]["match"] == "EXACT"
    assert result["activation"]["vocabulary"]["activated"] == 1

    import data.vocabulary_bootstrap_service as vocab_svc
    activated = vocab_svc.get_generated_vocabulary(2)
    assert "student" in activated

    conn = _db_conn(db_b)
    try:
        audit_row = conn.execute(
            "SELECT action, reason FROM datasource_lifecycle_audit WHERE source_id=2 "
            "AND action='portable_package_import'"
        ).fetchone()
    finally:
        conn.close()
    assert audit_row is not None
    assert "accepted" in audit_row["reason"]


def test_tampered_package_is_rejected_without_resigning(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTABLE_PACKAGE_SIGNING_KEY", _SHARED_SIGNING_KEY)

    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)
    exported = pks.export_signed_package(1, "u1", "tamper_test_v1")
    package = exported["package"]

    tampered = dict(package)
    tampered["vocabulary"] = [dict(row, term="tampered-term") for row in tampered["vocabulary"]]
    assert pks.verify_package_signature(tampered) == pks.SIGNATURE_STATUS_INVALID

    db_b = _install(tmp_path, monkeypatch, "install_b", source_id=2)
    _write_snapshot(db_b, 2, [_STUDENT_TABLE])
    result = pks.import_signed_package(tampered, 2, "u1")
    assert result["accepted"] is False
    assert result["signature_status"] == pks.SIGNATURE_STATUS_INVALID
    assert result["activation"] is None

    import data.vocabulary_bootstrap_service as vocab_svc
    assert vocab_svc.get_generated_vocabulary(2) == {}


def test_unsigned_package_is_refused_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PORTABLE_PACKAGE_SIGNING_KEY", raising=False)

    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)
    exported = pks.export_signed_package(1, "u1", "unsigned_test_v1")
    assert exported["signature_status"] == pks.SIGNATURE_STATUS_UNSIGNED
    assert "signature" not in exported["package"]

    db_b = _install(tmp_path, monkeypatch, "install_b", source_id=2)
    _write_snapshot(db_b, 2, [_STUDENT_TABLE])
    result = pks.import_signed_package(exported["package"], 2, "u1")
    assert result["accepted"] is False
    assert result["signature_status"] == pks.SIGNATURE_STATUS_UNSIGNED


def test_unsigned_package_can_be_explicitly_force_imported(tmp_path, monkeypatch):
    """An operator who explicitly trusts their own unsigned export (no
    shared key configured anywhere) can opt in via require_valid_signature
    =False -- an explicit, auditable choice, never a silent default."""
    monkeypatch.delenv("PORTABLE_PACKAGE_SIGNING_KEY", raising=False)

    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)
    exported = pks.export_signed_package(1, "u1", "force_test_v1")

    db_b = _install(tmp_path, monkeypatch, "install_b", source_id=2)
    _write_snapshot(db_b, 2, [_STUDENT_TABLE])
    result = pks.import_signed_package(exported["package"], 2, "u1", require_valid_signature=False)
    assert result["accepted"] is True
    assert result["activation"]["vocabulary"]["activated"] == 1


def test_incompatible_schema_is_refused_even_with_a_valid_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTABLE_PACKAGE_SIGNING_KEY", _SHARED_SIGNING_KEY)

    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)
    exported = pks.export_signed_package(1, "u1", "mismatch_test_v1")
    package = exported["package"]

    # A completely unrelated schema -- the real-world "two unrelated
    # databases" case Part 2/8 both warn about.
    db_c = _install(tmp_path, monkeypatch, "install_c", source_id=3)
    _write_snapshot(db_c, 3, [_CLIENT_TABLE])
    result = pks.import_signed_package(package, 3, "u1")
    assert result["accepted"] is False
    assert result["validation"]["match"] == "NO_MATCH"
    assert result["signature_status"] == pks.SIGNATURE_STATUS_VALID  # signature itself was fine -- schema wasn't


def test_import_refuses_when_target_source_not_owned_by_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTABLE_PACKAGE_SIGNING_KEY", _SHARED_SIGNING_KEY)

    db_a = _install(tmp_path, monkeypatch, "install_a")
    _write_snapshot(db_a, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_a, 1, "student", "dbo.Student", 0.9)
    exported = pks.export_signed_package(1, "u1", "ownership_test_v1")

    db_b = _install(tmp_path, monkeypatch, "install_b", source_id=2)
    _write_snapshot(db_b, 2, [_STUDENT_TABLE])
    result = pks.import_signed_package(exported["package"], 2, "someone-else")
    assert result["accepted"] is False
    assert "not owned" in result["reason"]
