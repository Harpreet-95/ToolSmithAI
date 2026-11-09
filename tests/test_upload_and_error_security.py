from __future__ import annotations

import asyncio
import io
import json

from fastapi import Request, UploadFile

from api.v1 import routes
from core.errors.exception_handler import global_exception_handler
from core.errors.error_response import build_error_response


def test_dataset_upload_accepts_file_at_limit(monkeypatch):
    monkeypatch.setattr(routes, "DATASET_UPLOAD_MAX_BYTES", 4)
    upload = UploadFile(filename="small.csv", file=io.BytesIO(b"1234"))

    assert asyncio.run(routes._read_dataset_upload(upload)) == b"1234"


def test_dataset_upload_rejects_file_over_limit(monkeypatch):
    monkeypatch.setattr(routes, "DATASET_UPLOAD_MAX_BYTES", 4)
    upload = UploadFile(filename="large.csv", file=io.BytesIO(b"12345"))

    try:
        asyncio.run(routes._read_dataset_upload(upload))
    except ValueError as exc:
        assert str(exc) == "dataset_upload_too_large"
    else:
        raise AssertionError("oversized upload was not rejected")


def test_global_error_response_does_not_leak_exception_text():
    request = Request({"type": "http", "method": "GET", "path": "/test", "headers": []})
    secret_text = "database password was hunter2"

    response = asyncio.run(global_exception_handler(request, RuntimeError(secret_text)))
    payload = json.loads(response.body)

    assert response.status_code == 500
    assert payload["message"] == "Internal server error"
    assert payload["details"] is None
    assert secret_text not in response.body.decode()


def test_internal_error_builder_suppresses_legacy_exception_details():
    payload = build_error_response("Internal server error", "server=private;password=secret")

    assert payload == {"status": "error", "message": "Internal server error", "details": None}
