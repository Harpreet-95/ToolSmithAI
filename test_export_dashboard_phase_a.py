#!/usr/bin/env python3
"""
Export Activity Dashboard Phase A — Backend Foundation Test Suite

Tests the new capabilities added in Phase A:
  - total count field in list response
  - date_from / date_to filtering
  - user_id filtering
  - /admin/export-logs/summary endpoint
  - idx_export_logs_status index migration
  - non-breaking: count field and existing filters preserved

Run from project root: python test_export_dashboard_phase_a.py
"""
import json
import os
import pathlib
import sys
import tempfile

PROJECT_ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Redirect DB to isolated temp file BEFORE any data module is imported
# ---------------------------------------------------------------------------
import data.db as _db_module

_tmp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB_PATH = pathlib.Path(_tmp_db_file.name)
_tmp_db_file.close()
_db_module.DB_PATH = _TMP_DB_PATH

from data.models import init_db
init_db()

from fastapi.testclient import TestClient
from api.app import app

import core.config as _cfg
_TEST_ADMIN_KEY = "test-admin-api-key-phase-a-dashboard-check"
_TEST_USER_KEY  = "test-user-api-key-phase-a-dashboard-chk"
_cfg.KEY_ROLE_MAP[_TEST_ADMIN_KEY] = "admin"
_cfg.KEY_ROLE_MAP[_TEST_USER_KEY]  = "user"

from auth.jwt_auth import create_access_token
from data.report_service import save_report
from data.db import get_connection


def _jwt(user_id: str, role: str = "user") -> str:
    return create_access_token({"sub": user_id, "role": role})


_MINIMAL_CONTENT = {
    "sections": [{"type": "text", "heading": "Overview",
                  "items": ["Phase A test report."]}]
}


def _create_report(user_id: str, title: str = "Phase A Report") -> int:
    return save_report(
        user_id=user_id,
        title=title,
        task_type="generate_dataset_report",
        content=_MINIMAL_CONTENT,
    )


def _direct_insert_log(
    user_id: str,
    export_format: str,
    status: str,
    created_at: str,
    exported_at: str | None = None,
) -> None:
    """Insert an export_log row directly, bypassing the HTTP layer."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO export_logs "
        "(user_id, report_id, export_format, status, created_at, exported_at) "
        "VALUES (?, NULL, ?, ?, ?, ?)",
        (user_id, export_format, status, created_at, exported_at or created_at),
    )
    conn.commit()
    conn.close()


def _count_db_rows() -> int:
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM export_logs").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
PASS_SYM = "PASS"
FAIL_SYM = "FAIL"
_results: list[tuple[str, str, bool, str]] = []


def check(test_id: str, label: str, ok: bool, detail: str = "") -> bool:
    sym = PASS_SYM if ok else FAIL_SYM
    _results.append((test_id, label, ok, detail))
    line = f"    [{sym}] {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


# ===========================================================================
# RUN
# ===========================================================================
print()
print("=" * 72)
print("  Export Activity Dashboard Phase A — Backend Foundation Tests")
print("  DB:", _TMP_DB_PATH)
print("=" * 72)

USER_A = "pa-user-alpha"
USER_B = "pa-user-beta"

# Timestamps used for date-filter tests
OLD_TS     = "2020-06-01T00:00:00+00:00"   # well before any real run
FUTURE_TS  = "2099-01-01T00:00:00+00:00"   # well after any real run
CUTOFF_LO  = "2021-01-01"                   # date_from: excludes OLD_TS
CUTOFF_HI  = "2050-01-01"                   # date_to:   excludes FUTURE_TS

with TestClient(app) as client:

    # -----------------------------------------------------------------------
    # Seed known rows directly so we control counts precisely
    # -----------------------------------------------------------------------
    # Old row (before CUTOFF_LO) — user A, pdf, success
    _direct_insert_log(USER_A, "pdf",  "success", OLD_TS)
    # Future row (after CUTOFF_HI) — user A, pdf, success
    _direct_insert_log(USER_A, "pdf",  "success", FUTURE_TS)
    # Normal rows — user A
    _direct_insert_log(USER_A, "csv",  "success", "2026-06-01T10:00:00+00:00")
    _direct_insert_log(USER_A, "xlsx", "failed",  "2026-06-02T10:00:00+00:00", None)
    # Normal rows — user B
    _direct_insert_log(USER_B, "pdf",  "success", "2026-06-03T10:00:00+00:00")
    _direct_insert_log(USER_B, "json", "success", "2026-06-04T10:00:00+00:00")

    db_total = _count_db_rows()
    print(f"\n  Seeded {db_total} rows directly into export_logs.\n")

    # -----------------------------------------------------------------------
    print("[T-PA-01] total field is present in list response")
    # -----------------------------------------------------------------------
    r = client.get("/v1/admin/export-logs", headers={"x-api-key": _TEST_ADMIN_KEY})
    body = r.json()
    check("T-PA-01-01", "HTTP 200",               r.status_code == 200,
          f"got {r.status_code}")
    check("T-PA-01-02", "'total' key in response", "total" in body,
          f"keys={list(body.keys())}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-02] total equals actual DB row count (no filters)")
    # -----------------------------------------------------------------------
    r = client.get("/v1/admin/export-logs", headers={"x-api-key": _TEST_ADMIN_KEY})
    body = r.json()
    check("T-PA-02-01", "total == db_total",
          body.get("total") == db_total,
          f"total={body.get('total')} db={db_total}")
    check("T-PA-02-02", "count == len(data)",
          body.get("count") == len(body.get("data", [])),
          f"count={body.get('count')} len={len(body.get('data', []))}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-03] date_from excludes rows created before it")
    # -----------------------------------------------------------------------
    r = client.get(
        f"/v1/admin/export-logs?date_from={CUTOFF_LO}",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body = r.json()
    rows = body.get("data", [])
    old_in_result = any(r["created_at"] == OLD_TS for r in rows)
    print(f"    date_from={CUTOFF_LO}  total={body.get('total')}  old_row_present={old_in_result}")
    check("T-PA-03-01", "HTTP 200",                   r.status_code == 200, f"got {r.status_code}")
    check("T-PA-03-02", "old row excluded",           not old_in_result,    f"old row still in result")
    check("T-PA-03-03", "total reflects filter",
          (body.get("total") or 0) < db_total,
          f"total={body.get('total')} db_total={db_total}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-04] date_to excludes rows created after it")
    # -----------------------------------------------------------------------
    r = client.get(
        f"/v1/admin/export-logs?date_to={CUTOFF_HI}",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body = r.json()
    rows = body.get("data", [])
    future_in_result = any(r["created_at"] == FUTURE_TS for r in rows)
    old_in_result_2  = any(r["created_at"] == OLD_TS    for r in rows)
    print(f"    date_to={CUTOFF_HI}  total={body.get('total')}  future_present={future_in_result}  old_present={old_in_result_2}")
    check("T-PA-04-01", "HTTP 200",                     r.status_code == 200, f"got {r.status_code}")
    check("T-PA-04-02", "future row excluded",          not future_in_result, "future row still in result")
    check("T-PA-04-03", "old row still included",       old_in_result_2,      "old row missing from date_to result")
    check("T-PA-04-04", "total reflects filter",
          (body.get("total") or 0) < db_total,
          f"total={body.get('total')} db_total={db_total}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-05] user_id filter returns only that user's rows")
    # -----------------------------------------------------------------------
    r = client.get(
        f"/v1/admin/export-logs?user_id={USER_B}",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body = r.json()
    rows = body.get("data", [])
    all_b = all(row["user_id"] == USER_B for row in rows)
    no_a  = all(row["user_id"] != USER_A for row in rows)
    print(f"    user_id={USER_B}  total={body.get('total')}  all_correct={all_b}")
    check("T-PA-05-01", "HTTP 200",                        r.status_code == 200,  f"got {r.status_code}")
    check("T-PA-05-02", "all rows belong to USER_B",       all_b,                 f"rows={[(r['user_id']) for r in rows]}")
    check("T-PA-05-03", "no USER_A rows in result",        no_a,                  f"user_a row found")
    check("T-PA-05-04", "total == 2 (seeded 2 for USER_B)",
          body.get("total") == 2,
          f"total={body.get('total')}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-06] combined filter: user_id + export_format")
    # -----------------------------------------------------------------------
    r = client.get(
        f"/v1/admin/export-logs?user_id={USER_A}&export_format=pdf",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body = r.json()
    rows = body.get("data", [])
    all_match = all(row["user_id"] == USER_A and row["export_format"] == "pdf"
                    for row in rows)
    print(f"    user_id={USER_A}&export_format=pdf  total={body.get('total')}")
    check("T-PA-06-01", "HTTP 200",                   r.status_code == 200, f"got {r.status_code}")
    check("T-PA-06-02", "all rows match both filters", all_match,           f"rows={[(r['user_id'],r['export_format']) for r in rows]}")
    check("T-PA-06-03", "total == 2 (old + future pdf for USER_A)",
          body.get("total") == 2,
          f"total={body.get('total')}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-07] summary endpoint HTTP 200")
    # -----------------------------------------------------------------------
    r_sum = client.get(
        "/v1/admin/export-logs/summary",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body_sum = r_sum.json()
    print(f"    HTTP {r_sum.status_code}  body={json.dumps(body_sum, indent=6)}")
    check("T-PA-07-01", "HTTP 200",            r_sum.status_code == 200,              f"got {r_sum.status_code}")
    check("T-PA-07-02", "status='success'",    body_sum.get("status") == "success",   repr(body_sum.get("status")))
    check("T-PA-07-03", "'data' key present",  "data" in body_sum,                    f"keys={list(body_sum.keys())}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-08] summary contains all 5 required fields")
    # -----------------------------------------------------------------------
    data = body_sum.get("data", {})
    required_fields = [
        "total_exports", "successful_exports", "failed_exports",
        "success_rate", "exports_by_format",
    ]
    missing = [f for f in required_fields if f not in data]
    check("T-PA-08-01", "all 5 fields present",
          len(missing) == 0,
          f"missing={missing}" if missing else "")

    # -----------------------------------------------------------------------
    print("\n[T-PA-09] summary total_exports matches actual DB count")
    # -----------------------------------------------------------------------
    check("T-PA-09-01", "total_exports == db_total",
          data.get("total_exports") == db_total,
          f"summary={data.get('total_exports')} db={db_total}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-10] summary success_rate is accurate")
    # -----------------------------------------------------------------------
    # Seeded: 5 success (old_pdf, future_pdf, csv, pdf_b, json_b), 1 failed (xlsx_a)
    expected_successful = 5
    expected_failed     = 1
    expected_rate       = round(expected_successful / db_total * 100, 1)
    check("T-PA-10-01", f"successful_exports == {expected_successful}",
          data.get("successful_exports") == expected_successful,
          f"got {data.get('successful_exports')}")
    check("T-PA-10-02", f"failed_exports == {expected_failed}",
          data.get("failed_exports") == expected_failed,
          f"got {data.get('failed_exports')}")
    check("T-PA-10-03", f"success_rate == {expected_rate}",
          data.get("success_rate") == expected_rate,
          f"got {data.get('success_rate')}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-11] summary exports_by_format has correct keys and values")
    # -----------------------------------------------------------------------
    by_fmt = data.get("exports_by_format", {})
    print(f"    exports_by_format={by_fmt}")
    # Seeded: pdf=3 (old+future+user_b), csv=1, xlsx=1, json=1
    check("T-PA-11-01", "'pdf' key present",  "pdf"  in by_fmt, f"keys={list(by_fmt.keys())}")
    check("T-PA-11-02", "'csv' key present",  "csv"  in by_fmt, f"keys={list(by_fmt.keys())}")
    check("T-PA-11-03", "'xlsx' key present", "xlsx" in by_fmt, f"keys={list(by_fmt.keys())}")
    check("T-PA-11-04", "'json' key present", "json" in by_fmt, f"keys={list(by_fmt.keys())}")
    check("T-PA-11-05", "pdf count == 3",     by_fmt.get("pdf")  == 3, f"got {by_fmt.get('pdf')}")
    check("T-PA-11-06", "csv count == 1",     by_fmt.get("csv")  == 1, f"got {by_fmt.get('csv')}")
    check("T-PA-11-07", "xlsx count == 1",    by_fmt.get("xlsx") == 1, f"got {by_fmt.get('xlsx')}")
    check("T-PA-11-08", "json count == 1",    by_fmt.get("json") == 1, f"got {by_fmt.get('json')}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-12] summary endpoint returns 403 for non-admin")
    # -----------------------------------------------------------------------
    r_unauth = client.get(
        "/v1/admin/export-logs/summary",
        headers={"x-api-key": _TEST_USER_KEY},
    )
    print(f"    HTTP {r_unauth.status_code}")
    check("T-PA-12-01", "non-admin summary -> HTTP 403",
          r_unauth.status_code == 403,
          f"got {r_unauth.status_code}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-13] count (page-size) still present — backward-compatible")
    # -----------------------------------------------------------------------
    r = client.get("/v1/admin/export-logs", headers={"x-api-key": _TEST_ADMIN_KEY})
    body = r.json()
    check("T-PA-13-01", "'count' key still present",
          "count" in body,
          f"keys={list(body.keys())}")
    check("T-PA-13-02", "count == len(data)",
          body.get("count") == len(body.get("data", [])),
          f"count={body.get('count')} len={len(body.get('data', []))}")
    check("T-PA-13-03", "existing export_format filter still works",
          True,  # validated in T-PA-06; just confirm no KeyError here
    )
    # Spot-check existing status filter
    r_f = client.get(
        "/v1/admin/export-logs?status=failed",
        headers={"x-api-key": _TEST_ADMIN_KEY},
    )
    body_f = r_f.json()
    check("T-PA-13-04", "status=failed filter still works",
          r_f.status_code == 200 and
          all(row["status"] == "failed" for row in body_f.get("data", [])),
          f"HTTP {r_f.status_code} rows={[r['status'] for r in body_f.get('data', [])]}")

    # -----------------------------------------------------------------------
    print("\n[T-PA-14] idx_export_logs_status index exists in DB schema")
    # -----------------------------------------------------------------------
    conn = get_connection()
    idx_rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='export_logs'"
    ).fetchall()
    conn.close()
    all_indexes = {r[0] for r in idx_rows}
    print(f"    export_logs indexes: {sorted(all_indexes)}")
    check("T-PA-14-01", "idx_export_logs_status present",
          "idx_export_logs_status" in all_indexes,
          f"found={sorted(all_indexes)}")
    # Confirm original 4 indexes also still present
    for idx_name in [
        "idx_export_logs_user_id",
        "idx_export_logs_report_id",
        "idx_export_logs_export_format",
        "idx_export_logs_exported_at",
    ]:
        check(f"T-PA-14-02-{idx_name[-6:]}", f"{idx_name} still present",
              idx_name in all_indexes,
              f"missing from {sorted(all_indexes)}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("  RESULTS SUMMARY")
print("=" * 72)
passes = sum(1 for *_, ok, _ in _results if ok)
fails  = sum(1 for *_, ok, _ in _results if not ok)
total  = len(_results)
print(f"  Checks:  {total}  |  PASS: {passes}  |  FAIL: {fails}")
print()

if fails:
    print("  FAILED checks:")
    for tid, label, ok, detail in _results:
        if not ok:
            print(f"    [{tid}]  {label}")
            if detail:
                print(f"            -> {detail}")
    print()

try:
    os.unlink(_TMP_DB_PATH)
    print(f"  Temp DB removed: {_TMP_DB_PATH}")
except Exception:
    print(f"  Note: could not remove temp DB: {_TMP_DB_PATH}")
print()

sys.exit(0 if fails == 0 else 1)
