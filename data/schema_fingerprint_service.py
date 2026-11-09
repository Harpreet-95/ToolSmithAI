"""
Portable Business Learning Layer milestone, Part 3 — deterministic schema
fingerprint.

Builds a structural identity for a connected database from data ALREADY
persisted by schema discovery (data.schema_service.get_latest_snapshot) —
zero additional live reads, zero new profiling. Used to recognize a database
across connect/delete/reconnect cycles and across machines (Part 11/12)
without ever depending on source_id, connection name, hostname, or database
name — none of those survive a delete/reconnect or a fresh clone, and
hostname/database name can legitimately differ across environments for the
same logical schema.

Two granularities:
  - fingerprint_hash: exact structural identity (every table/column/type/
    nullability/PK/FK, canonically ordered). Byte-identical only when
    nothing structural changed.
  - table_set_hash: coarser identity over table names only. Used together
    with table_overlap_ratio to tell "same database family, some drift"
    apart from "unrelated database" (Part 10).

Never includes credentials, row data, or PII values — the input
(core.connectors.schema.SchemaSnapshot, via schema_snapshots.snapshot_json)
structurally cannot carry any of those; it is discovery-time catalog
metadata only (table/column names, normalized types, nullability, PK/FK
structure).
"""
from __future__ import annotations

import hashlib
import json

from data.schema_service import get_latest_snapshot

FINGERPRINT_VERSION = 1

# Part 10 — below this fraction of the package's own tables still present in
# the live schema, treat the live database as unrelated rather than "the
# same database, drifted". Chosen conservatively: a handful of renamed/
# dropped tables should not disqualify an otherwise-recognized database, but
# a database that shares only a minority of table names is not safely
# assumed to be the same one.
_COMPATIBLE_TABLE_OVERLAP_MIN = 0.5


def _canonical_table_shape(table: dict) -> dict:
    columns = sorted(
        (
            {
                "column_name": c["column_name"],
                "data_type": c["data_type"],
                "is_nullable": bool(c["is_nullable"]),
                "is_primary_key": bool(c["is_primary_key"]),
            }
            for c in table.get("columns", [])
        ),
        key=lambda c: c["column_name"],
    )
    primary_keys = sorted(pk["column_name"] for pk in table.get("primary_keys", []))
    foreign_keys = sorted(
        (
            {
                "from_column": fk["from_column"],
                "to_table": f"{fk['to_schema']}.{fk['to_table']}",
                "to_column": fk["to_column"],
            }
            for fk in table.get("foreign_keys", [])
        ),
        key=lambda fk: (fk["from_column"], fk["to_table"], fk["to_column"]),
    )
    return {"columns": columns, "primary_keys": primary_keys, "foreign_keys": foreign_keys}


def _canonical_structural_summary(snapshot: dict) -> dict:
    """table_fqn -> canonical shape, over TABLE and VIEW objects alike — a
    view reclassified as a table, or vice versa, is still the same object
    for fingerprinting purposes, so table_type is deliberately excluded from
    identity to tolerate that harmless reclassification."""
    tables: dict = {}
    for schema in snapshot.get("schemas", []):
        for table in schema.get("tables", []):
            tables[table["table_fqn"]] = _canonical_table_shape(table)
    return tables


def _hash_json(obj) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fingerprint(source_id: int, user_id: str) -> dict | None:
    """Returns None if the source has no schema_snapshots row yet (discovery
    hasn't run). Otherwise a dict with fingerprint_version, fingerprint_hash,
    table_set_hash, table_fqns (sorted), and structural_summary — the
    canonical per-table shape, safe to serialize into a portable package
    since it is derived entirely from core.connectors.schema dataclasses,
    which never carry credentials, row data, or PII values."""
    snapshot = get_latest_snapshot(source_id, user_id)
    if snapshot is None:
        return None

    structural_summary = _canonical_structural_summary(snapshot)
    table_fqns = sorted(structural_summary.keys())

    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "fingerprint_hash": _hash_json(structural_summary),
        "table_set_hash": _hash_json(table_fqns),
        "table_fqns": table_fqns,
        "structural_summary": structural_summary,
    }


def compare_fingerprints(package_fingerprint: dict, live_fingerprint: dict) -> dict:
    """Part 3/10 — classify a portable package's stored fingerprint against
    a live, freshly-computed one.

    Returns {"match": "EXACT"|"COMPATIBLE_DRIFTED"|"NO_MATCH",
    "table_overlap_ratio": float, "tables_added": [...],
    "tables_removed": [...], "tables_changed": [...]}.

    Never raises: a missing/malformed fingerprint on either side is treated
    as NO_MATCH — fail closed, never load physical mappings from another
    database without positive structural evidence."""
    try:
        if package_fingerprint.get("fingerprint_version") != live_fingerprint.get("fingerprint_version"):
            return {
                "match": "NO_MATCH", "reason": "fingerprint_version_mismatch",
                "table_overlap_ratio": 0.0,
                "tables_added": [], "tables_removed": [], "tables_changed": [],
            }

        if package_fingerprint["fingerprint_hash"] == live_fingerprint["fingerprint_hash"]:
            return {
                "match": "EXACT", "table_overlap_ratio": 1.0,
                "tables_added": [], "tables_removed": [], "tables_changed": [],
            }

        pkg_tables = set(package_fingerprint.get("table_fqns") or [])
        live_tables = set(live_fingerprint.get("table_fqns") or [])
        if not pkg_tables:
            return {
                "match": "NO_MATCH", "reason": "empty_package_fingerprint",
                "table_overlap_ratio": 0.0,
                "tables_added": [], "tables_removed": [], "tables_changed": [],
            }

        overlap = pkg_tables & live_tables
        overlap_ratio = len(overlap) / len(pkg_tables)

        pkg_summary = package_fingerprint.get("structural_summary") or {}
        live_summary = live_fingerprint.get("structural_summary") or {}
        tables_changed = sorted(
            fqn for fqn in overlap if pkg_summary.get(fqn) != live_summary.get(fqn)
        )

        match = "COMPATIBLE_DRIFTED" if overlap_ratio >= _COMPATIBLE_TABLE_OVERLAP_MIN else "NO_MATCH"

        return {
            "match": match,
            "table_overlap_ratio": round(overlap_ratio, 4),
            "tables_added": sorted(live_tables - pkg_tables),
            "tables_removed": sorted(pkg_tables - live_tables),
            "tables_changed": tables_changed,
        }
    except Exception:
        return {
            "match": "NO_MATCH", "reason": "comparison_error",
            "table_overlap_ratio": 0.0,
            "tables_added": [], "tables_removed": [], "tables_changed": [],
        }
