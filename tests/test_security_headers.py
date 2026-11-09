"""
Tests for SecurityHeadersMiddleware.

Run from the project root:
    venv/Scripts/pytest tests/test_security_headers.py -v
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware.security_headers import SecurityHeadersMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    @app.post("/probe")
    def probe_post():
        return {"ok": True}

    return app


@pytest.fixture(scope="module")
def client():
    return TestClient(_make_app())


def test_x_content_type_options(client):
    assert client.get("/probe").headers["x-content-type-options"] == "nosniff"


def test_x_frame_options(client):
    assert client.get("/probe").headers["x-frame-options"] == "DENY"


def test_referrer_policy(client):
    assert client.get("/probe").headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_permissions_policy_restricts_camera(client):
    assert "camera=()" in client.get("/probe").headers["permissions-policy"]


def test_permissions_policy_restricts_microphone(client):
    assert "microphone=()" in client.get("/probe").headers["permissions-policy"]


def test_permissions_policy_restricts_geolocation(client):
    assert "geolocation=()" in client.get("/probe").headers["permissions-policy"]


def test_cache_control(client):
    assert client.get("/probe").headers["cache-control"] == "no-store"


def test_headers_present_on_post(client):
    resp = client.post("/probe")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"


def test_headers_present_on_404(client):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
