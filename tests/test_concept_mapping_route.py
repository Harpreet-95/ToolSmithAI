"""
Phase 2, Step 7b — POST /v1/sources/{source_id}/concept-mappings.

Calls promote_concept_mapping_route directly as a plain function (same
convention as tests/test_execute_query_route_autonomous_preparation.py),
bypassing FastAPI's HTTP layer.

Run from the project root:
    python -m pytest tests/test_concept_mapping_route.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-concept-mapping-route-secret-long-enough")
os.environ.setdefault("USER_ID_SALT", "test-concept-mapping-route-salt-long-enough")

from starlette.responses import JSONResponse

import data.models as models
from api.v1.routes import PromoteConceptMappingRequest, promote_concept_mapping_route
from auth.api_key import AuthenticatedUser

_NOW = "2026-07-24T00:00:00+00:00"
_USER = AuthenticatedUser(role="user", user_id="u1")


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "concept_mapping_route.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in ("data.concept_mapping_service", "data.governance_service"):
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def test_promotes_and_returns_mapping_id(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(concept_term="clients", table_fqn="dbo.EDU_Clients"), _USER,
    )
    assert result["status"] == "success"
    assert result["data"]["created"] is True
    assert isinstance(result["data"]["mapping_id"], int)


def test_rejects_empty_concept_term(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(concept_term="  ", table_fqn="dbo.EDU_Clients"), _USER,
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 400


def test_rejects_unowned_source(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    other_user = AuthenticatedUser(role="user", user_id="someone_else")
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(concept_term="clients", table_fqn="dbo.EDU_Clients"), other_user,
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404


# ---------------------------------------------------------------------------
# Phase 3, Step 3 — remembered-term conflict confirmation
# ---------------------------------------------------------------------------

def test_request_confirm_replace_defaults_to_false():
    req = PromoteConceptMappingRequest(concept_term="clients", table_fqn="dbo.EDU_Clients")
    assert req.confirm_replace is False
    assert req.canonical_term is None


def test_no_canonical_term_leaves_synonym_path_untouched(tmp_path, monkeypatch):
    # Byte-for-byte pre-Step-3 behavior when canonical_term is omitted.
    env(tmp_path, monkeypatch)
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(concept_term="clients", table_fqn="dbo.EDU_Clients"), _USER,
    )
    assert result["status"] == "success"
    assert "synonym" not in result["data"]


def test_canonical_term_creates_a_synonym(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    assert result["status"] == "success"
    assert result["data"]["synonym"]["created"] is True
    assert result["data"]["synonym"]["canonical_term"] == "customer"

    from data.concept_mapping_service import get_synonym_canonical
    assert get_synonym_canonical(1, "clients") == "customer"


def test_conflicting_canonical_term_returns_structured_409(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Organizations", canonical_term="organization",
        ), _USER,
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(result.body)
    assert body["conflict"] is True
    assert body["reason"] == "remembered_term_conflict"
    assert body["source_id"] == 1
    assert body["synonym_term"] == "client"
    # No internal SQL, secrets, or unrelated mappings — just the two terms.
    assert set(body.keys()) == {
        "status", "reason", "conflict", "source_id",
        "synonym_term", "existing_canonical_term", "requested_canonical_term",
    }


def test_conflict_response_contains_existing_and_requested_terms(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Organizations", canonical_term="organization",
        ), _USER,
    )
    body = json.loads(result.body)
    assert body["existing_canonical_term"] == "customer"
    assert body["requested_canonical_term"] == "organization"

    # Nothing was overwritten by the conflicting attempt.
    from data.concept_mapping_service import get_synonym_canonical
    assert get_synonym_canonical(1, "clients") == "customer"


def test_confirm_replace_true_replaces_the_mapping(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Organizations",
            canonical_term="organization", confirm_replace=True,
        ), _USER,
    )
    assert result["status"] == "success"
    assert result["data"]["synonym"]["replaced"] is True

    from data.concept_mapping_service import get_synonym_canonical
    assert get_synonym_canonical(1, "clients") == "organization"


def test_replacement_does_not_create_duplicate_synonym_rows(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Organizations",
            canonical_term="organization", confirm_replace=True,
        ), _USER,
    )
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM concept_term_synonyms WHERE source_id = 1 AND synonym_term = 'client'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_source_isolation_for_synonym_conflict(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Test2','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    # Source 2 has never been taught anything — teaching "clients" a
    # different meaning there must succeed, not conflict with source 1.
    result = promote_concept_mapping_route(
        2, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.ADF2_Organizations", canonical_term="organization",
        ), _USER,
    )
    assert result["status"] == "success"
    assert result["data"]["synonym"]["created"] is True


def test_re_teaching_same_canonical_term_remains_idempotent_success(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), _USER,
    )
    assert result["status"] == "success"
    assert result["data"]["synonym"]["unchanged"] is True


def test_unowned_source_remains_rejected_with_canonical_term(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    other_user = AuthenticatedUser(role="user", user_id="someone_else")
    result = promote_concept_mapping_route(
        1, PromoteConceptMappingRequest(
            concept_term="clients", table_fqn="dbo.EDU_Clients", canonical_term="customer",
        ), other_user,
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
