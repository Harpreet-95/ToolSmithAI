"""
API-layer tests for M-3 Part 4 (Safe Bulk Review) — mounts the real
api.v1.routes router (not a hand-rolled stub) behind FastAPI's TestClient,
so these tests exercise the actual require_role("admin") dependency,
confirmed-flag gate, and BulkFilter validation exactly as a real request
would hit them.

Run from the project root:
    python -m pytest tests/test_governance_bulk_api.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-governance-bulk-api-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-governance-bulk-api-salt-long-enough-1")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import data.models as models
from auth.jwt_auth import create_access_token
from api.v1.routes import router

_NOW = "2026-07-12T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bulk_api.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    return TestClient(app)


def _token(role: str, user_id: str = "u1") -> str:
    return create_access_token({"sub": user_id, "role": role})


def _auth(role: str, user_id: str = "u1") -> dict:
    return {"Authorization": f"Bearer {_token(role, user_id)}"}


# ---------------------------------------------------------------------------
# Admin RBAC gating — non-admin callers get 403
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("post", "/v1/governance/bulk/dry-run", {"object_type": "domain.rule", "source_id": 1}),
    ("post", "/v1/governance/bulk/approve", {"object_type": "domain.rule", "source_id": 1, "confirmed": True}),
    ("post", "/v1/governance/bulk/reject", {"object_type": "domain.rule", "source_id": 1, "confirmed": True}),
    ("get", "/v1/governance/policies", None),
    ("post", "/v1/governance/policies", {"policy_name": "x", "action": "REQUIRE_HUMAN"}),
    ("get", "/v1/governance/review-segmentation?source_id=1", None),
])
def test_non_admin_gets_403(client, method, path, body):
    kwargs = {"json": body} if method == "post" else {}
    resp = getattr(client, method)(path, headers=_auth("user"), **kwargs)
    assert resp.status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("post", "/v1/governance/bulk/dry-run", {"object_type": "domain.rule", "source_id": 1}),
    ("get", "/v1/governance/policies", None),
])
def test_admin_is_not_blocked_by_role_gate(client, method, path, body):
    kwargs = {"json": body} if method == "post" else {}
    resp = getattr(client, method)(path, headers=_auth("admin"), **kwargs)
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Explicit second confirmation required to commit approve/reject
# ---------------------------------------------------------------------------

def test_bulk_approve_without_confirmed_flag_is_rejected(client):
    resp = client.post(
        "/v1/governance/bulk/approve",
        headers=_auth("admin"),
        json={"object_type": "domain.rule", "source_id": 1, "confirmed": False},
    )
    assert resp.status_code == 400


def test_bulk_reject_without_confirmed_flag_is_rejected(client):
    resp = client.post(
        "/v1/governance/bulk/reject",
        headers=_auth("admin"),
        json={"object_type": "domain.rule", "source_id": 1, "confirmed": False},
    )
    assert resp.status_code == 400


def test_bulk_approve_with_confirmed_flag_and_no_candidates_succeeds(client):
    resp = client.post(
        "/v1/governance/bulk/approve",
        headers=_auth("admin"),
        json={"object_type": "domain.rule", "source_id": 1, "confirmed": True},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total_candidates"] == 0


# ---------------------------------------------------------------------------
# source_id is a required field on the bulk request body
# ---------------------------------------------------------------------------

def test_bulk_dry_run_missing_source_id_is_rejected(client):
    resp = client.post(
        "/v1/governance/bulk/dry-run",
        headers=_auth("admin"),
        json={"object_type": "domain.rule"},
    )
    assert resp.status_code == 422  # pydantic: source_id is a required int field


# ---------------------------------------------------------------------------
# relationship.suggestion is now an accepted bulk object_type
# ---------------------------------------------------------------------------

def test_relationship_suggestion_is_supported_bulk_type(client):
    resp = client.post(
        "/v1/governance/bulk/dry-run",
        headers=_auth("admin"),
        json={"object_type": "relationship.suggestion", "source_id": 1},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Reject dry-run action is supported
# ---------------------------------------------------------------------------

def test_bulk_dry_run_reject_action(client):
    resp = client.post(
        "/v1/governance/bulk/dry-run?action=reject",
        headers=_auth("admin"),
        json={"object_type": "domain.rule", "source_id": 1},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["action"] == "reject"
    assert resp.json()["data"]["dry_run"] is True


# ---------------------------------------------------------------------------
# review-segmentation route
# ---------------------------------------------------------------------------

def test_review_segmentation_route_returns_groups(client):
    resp = client.get("/v1/governance/review-segmentation?source_id=1", headers=_auth("admin"))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source_id"] == 1
    assert set(data["groups"].keys()) == {"A", "B", "C", "D", "E", "F", "G"}


def test_review_segmentation_route_404_for_unowned_source(client):
    resp = client.get(
        "/v1/governance/review-segmentation?source_id=1",
        headers=_auth("admin", user_id="someone-else"),
    )
    assert resp.status_code == 404
