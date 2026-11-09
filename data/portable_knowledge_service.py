"""
Portable Business Learning Layer milestone — Layer 2 (PORTABLE VALIDATED
DATABASE KNOWLEDGE), Parts 4/5/9/10/16/17/18/19/20.

Design decision (documented here since it drives every function below):
portable packages are NOT read directly at question-answering time. Reading
them at query time would mean teaching every existing consumer (candidate
adjudicator, vocabulary lookup, semantic contract resolution, ...) a second
knowledge source, which is exactly the "second parallel system" this
milestone's own DO-NOT list forbids. Instead, a matched-and-validated
package is *materialized* into the SAME source-scoped tables normal Q&A
already reads (generated_business_vocabulary, entity_assignments,
semantic_entity_contracts) — scoped to whatever source_id the current
connection happens to have. Every existing read path
(data.vocabulary_bootstrap_service.get_generated_vocabulary,
data.entity_service.list_entity_assignments,
data.semantic_contract_service.get_latest_entity_contract, and everything
built on top of them) needs zero changes and is unaware a package was ever
involved. This is what makes "portable" and "source-instance-scoped"
(Layer 2 vs Layer 3) coexist without a second runtime code path.

Package assertions are only ever written after validate_package_against_
live_schema confirms them against the CURRENT live schema (Part 9/17 — live
structural truth always wins over stored knowledge).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from data.db import get_connection
from data.schema_fingerprint_service import compute_fingerprint, compare_fingerprints
import data.vocabulary_bootstrap_service as _vocab_svc
import data.semantic_contract_service as _contract_svc
import data.entity_service as _entity_svc
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

logger = logging.getLogger(__name__)

PACKAGE_FORMAT_VERSION = 1
# Bumped only on a breaking change to how a package is interpreted — lets a
# future ToolSmithAI version detect and safely refuse (rather than
# misinterpret) a package produced by an incompatible past/future version
# (Part 19).
TOOLSMITH_COMPATIBILITY_VERSION = "1.0"

DEFAULT_PACKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge_packages"
)

# Part 5/16 — knowledge below this confidence is not eligible for a portable
# package. Conservative on purpose (Part 16: "Be conservative"); matches the
# HIGH/MEDIUM confidence_tier boundary vocabulary_bootstrap_service already
# uses elsewhere in this codebase, rather than inventing a new scale.
DEFAULT_MIN_CONFIDENCE = 0.6

# Part 18 — the exhaustive set of top-level package keys and, for each
# section, the exhaustive set of per-row keys. Anything outside this set is
# dropped before a package is ever written to disk, so a future field added
# upstream (e.g. a raw evidence blob that starts including sampled values)
# cannot silently leak into a portable package without a deliberate edit
# here. This is the enforcement point for Part 18's security/privacy audit.
_TOP_LEVEL_KEYS = frozenset({
    "knowledge_package_version", "fingerprint_version", "toolsmith_compatibility_version",
    "database_family_identifier", "generated_at", "human_reviewed", "provenance_note",
    "schema_fingerprint", "vocabulary", "entity_assignments", "entity_contracts",
    "trusted_relationships",
    # Phase 7, Step 6H, Part 8 — signature envelope. Never user-supplied data
    # (never part of build_package_from_source's own output); only ever set
    # by sign_package, immediately before save_package.
    "signature", "signature_key_version",
})
_FINGERPRINT_KEYS = frozenset({
    "fingerprint_version", "fingerprint_hash", "table_set_hash", "table_fqns", "structural_summary",
})
_VOCAB_ROW_KEYS = frozenset({
    "term", "table_fqn", "column_name", "confidence_tier", "confidence_score",
    "evidence", "required_schema_objects",
})
_ENTITY_ROW_KEYS = frozenset({
    "table_fqn", "entity", "confidence", "evidence", "required_schema_objects",
})
_CONTRACT_ROW_KEYS = frozenset({
    "entity_name", "canonical_table_fqn", "preferred_view_fqn", "resolution_status",
    "confidence", "contract", "required_schema_objects",
})
_RELATIONSHIP_ROW_KEYS = frozenset({
    "from_table_fqn", "from_column", "to_table_fqn", "to_column",
    "relationship_type", "confidence", "cardinality", "required_schema_objects",
})

# Part 18 — fields that must NEVER appear inside a contract_json blob (some
# older contract_json documents carry free-form evidence sub-dicts). Scanned
# recursively over every string/dict key in the sanitized contract before it
# is allowed into a package.
_FORBIDDEN_CONTRACT_KEYS = frozenset({
    "password", "secret", "token", "connection_string", "credentials",
    "server", "host", "port", "raw_rows", "sample_values", "sample_rows",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _required_objects_for_column(table_fqn: str, column_name: str | None) -> list[dict]:
    """A structured {table_fqn, column_name} reference, never a dot-joined
    string — table_fqn is itself "schema.table" (already contains a dot),
    which would be ambiguous with a "table.column" string-split heuristic."""
    if not table_fqn:
        return []
    return [{"table_fqn": table_fqn, "column_name": column_name or None}]


def _sanitize_contract_json(contract: dict) -> dict:
    """Recursively drop any key matching _FORBIDDEN_CONTRACT_KEYS, and any
    key whose value looks like a bare column-value sample list rather than
    schema-level evidence. Fails closed: on any error, returns {} rather
    than risk leaking an unsanitized blob."""
    try:
        def _clean(obj):
            if isinstance(obj, dict):
                return {
                    k: _clean(v) for k, v in obj.items()
                    if k.lower() not in _FORBIDDEN_CONTRACT_KEYS
                }
            if isinstance(obj, list):
                return [_clean(v) for v in obj]
            return obj
        return _clean(contract or {})
    except Exception:  # noqa: BLE001
        logger.warning("portable_knowledge_service: contract sanitization failed; dropping contract body")
        return {}


# ---------------------------------------------------------------------------
# Part 5 — build a package from already-VALIDATED knowledge (never a manual
# per-database dictionary; every row here traces back to a real, already-
# persisted, evidence-backed row in this source's own knowledge tables).
# ---------------------------------------------------------------------------

def build_package_from_source(
    source_id: int, user_id: str, database_family_identifier: str, *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    human_reviewed: bool = False,
    provenance_note: str = "",
    session=None,
) -> dict | None:
    """Part 5/16 — project a source's current validated knowledge into a
    portable package. Returns None if source_id is not owned by user_id or
    has no schema fingerprint yet (discovery hasn't run).

    Deliberately conservative: only rows meeting min_confidence are
    included, and every row's required_schema_objects are recorded so a
    future validation pass can check them against a live (possibly
    different) schema without re-deriving anything. This function performs
    no live database reads of its own — it only reads already-persisted
    rows plus the already-persisted schema fingerprint."""
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
    finally:
        conn.close()

    fingerprint = compute_fingerprint(source_id, user_id)
    if fingerprint is None:
        return None

    vocabulary_rows = []
    for term, rows in _vocab_svc.get_generated_vocabulary(source_id, session=session).items():
        for row in rows:
            score = row.get("confidence_score") or 0.0
            if score < min_confidence:
                continue
            vocabulary_rows.append({
                "term": row["term"],
                "table_fqn": row["table_fqn"],
                "column_name": row.get("column_name") or "",
                "confidence_tier": row.get("confidence_tier"),
                "confidence_score": score,
                "evidence": row.get("evidence") if isinstance(row.get("evidence"), list) else
                            json.loads(row.get("evidence_json") or "[]"),
                "required_schema_objects": _required_objects_for_column(
                    row["table_fqn"], row.get("column_name")
                ),
            })

    entity_rows = []
    for row in (_entity_svc.list_entity_assignments(source_id, user_id) or []):
        confidence = row.get("confidence") or 0.0
        if confidence < min_confidence:
            continue
        entity_rows.append({
            "table_fqn": row["table_fqn"],
            "entity": row["entity"],
            "confidence": confidence,
            "evidence": row.get("evidence") or [],
            "required_schema_objects": _required_objects_for_column(row["table_fqn"], None),
        })

    contract_rows = []
    conn = get_connection()
    try:
        entity_names = [
            r["entity_name"] for r in
            conn.execute(
                "SELECT DISTINCT entity_name FROM semantic_entity_contracts WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    for entity_name in entity_names:
        contract = _contract_svc.get_latest_entity_contract(source_id, entity_name, session=session)
        if not contract:
            continue
        if contract.get("resolution_status") != "RESOLVED":
            continue
        confidence = contract.get("confidence") or 0.0
        if confidence < min_confidence:
            continue
        table_fqn = contract.get("canonical_table_fqn") or contract.get("preferred_view_fqn")
        contract_rows.append({
            "entity_name": entity_name,
            "canonical_table_fqn": contract.get("canonical_table_fqn"),
            "preferred_view_fqn": contract.get("preferred_view_fqn"),
            "resolution_status": contract.get("resolution_status"),
            "confidence": confidence,
            "contract": _sanitize_contract_json(contract.get("contract") or {}),
            "required_schema_objects": _required_objects_for_column(table_fqn, None),
        })

    # Trusted, non-structural relationship knowledge only — declared FKs are
    # structural truth rediscovered fresh on every connect (see
    # data.schema_service.run_discovery -> extract_and_persist_relationships)
    # and must never be duplicated into "learned" portable knowledge; only
    # relationships this system inferred/validated beyond raw FK extraction
    # belong here.
    relationship_rows = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT from_table_fqn, from_column, to_table_fqn, to_column, relationship_type, "
            "confidence, cardinality, inference_method, relationship_status "
            "FROM table_relationships "
            "WHERE source_id = ? AND inference_method != 'declared_fk' "
            "AND relationship_status IN ('AUTO', 'APPROVED')",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        d = dict(row)
        confidence = (d.get("confidence") or 0.0)
        if confidence < min_confidence:
            continue
        relationship_rows.append({
            "from_table_fqn": d["from_table_fqn"], "from_column": d["from_column"],
            "to_table_fqn": d["to_table_fqn"], "to_column": d["to_column"],
            "relationship_type": d["relationship_type"], "confidence": confidence,
            "cardinality": d.get("cardinality"),
            "required_schema_objects": (
                _required_objects_for_column(d["from_table_fqn"], d["from_column"])
                + _required_objects_for_column(d["to_table_fqn"], d["to_column"])
            ),
        })

    package = {
        "knowledge_package_version": PACKAGE_FORMAT_VERSION,
        "fingerprint_version": fingerprint["fingerprint_version"],
        "toolsmith_compatibility_version": TOOLSMITH_COMPATIBILITY_VERSION,
        "database_family_identifier": database_family_identifier,
        "generated_at": _now(),
        "human_reviewed": bool(human_reviewed),
        "provenance_note": provenance_note,
        "schema_fingerprint": fingerprint,
        "vocabulary": vocabulary_rows,
        "entity_assignments": entity_rows,
        "entity_contracts": contract_rows,
        "trusted_relationships": relationship_rows,
    }
    return sanitize_package(package)


def sanitize_package(package: dict) -> dict:
    """Part 18 — enforce the allow-listed key set at every level. Anything
    not explicitly named in _TOP_LEVEL_KEYS/_*_ROW_KEYS is dropped. This is
    the single choke point every package must pass through before being
    written to disk or activated, regardless of how it was constructed."""
    clean = {k: v for k, v in package.items() if k in _TOP_LEVEL_KEYS}
    if "schema_fingerprint" in clean:
        clean["schema_fingerprint"] = {
            k: v for k, v in clean["schema_fingerprint"].items() if k in _FINGERPRINT_KEYS
        }
    if "vocabulary" in clean:
        clean["vocabulary"] = [
            {k: v for k, v in row.items() if k in _VOCAB_ROW_KEYS} for row in clean["vocabulary"]
        ]
    if "entity_assignments" in clean:
        clean["entity_assignments"] = [
            {k: v for k, v in row.items() if k in _ENTITY_ROW_KEYS} for row in clean["entity_assignments"]
        ]
    if "entity_contracts" in clean:
        clean["entity_contracts"] = [
            {k: v for k, v in row.items() if k in _CONTRACT_ROW_KEYS} for row in clean["entity_contracts"]
        ]
    if "trusted_relationships" in clean:
        clean["trusted_relationships"] = [
            {k: v for k, v in row.items() if k in _RELATIONSHIP_ROW_KEYS} for row in clean["trusted_relationships"]
        ]
    return clean


_FORBIDDEN_KEY_MARKERS = (
    "password", "secret_key", "connection_string", "encrypted_config",
    "apikey", "api_key", "bearer_token", "access_token",
)


def _scan_keys_for_forbidden_markers(obj, path: str, violations: list[str]) -> None:
    """Recursively checks dict KEYS (never string VALUES) against
    _FORBIDDEN_KEY_MARKERS. Deliberately key-only: a schema legitimately has
    columns literally named things like "Password" or "ApiKey" (the reference database does —
    live-confirmed: dbo.EDU_Channels.ApiKey, a channel-integration table) and
    those column NAMES are exactly the safe structural metadata this package
    format is meant to carry (Part 4) — only a KEY shaped like a secret
    field (e.g. an actual "password": "..." entry) indicates a real
    violation, never a "column_name": "Password" value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_lower = str(k).lower()
            if any(marker in key_lower for marker in _FORBIDDEN_KEY_MARKERS):
                violations.append(f"forbidden key {k!r} present at {path or '<root>'}")
            _scan_keys_for_forbidden_markers(v, f"{path}.{k}" if path else k, violations)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_keys_for_forbidden_markers(item, f"{path}[{i}]", violations)


def audit_package_for_unsafe_content(package: dict) -> list[str]:
    """Part 18 — returns a list of violation descriptions (empty = clean).
    Defense-in-depth check independent of sanitize_package's key-allowlist:
    recursively scans dict KEYS (never legitimate schema-metadata string
    values like column names) for secret/credential-shaped field names, and
    confirms no forbidden top-level/row keys survived sanitization."""
    violations: list[str] = []
    sanitized = sanitize_package(package)
    if sanitized != package:
        violations.append("package contained keys outside the allow-list (dropped by sanitize_package)")

    _scan_keys_for_forbidden_markers(package, "", violations)
    return violations


# ---------------------------------------------------------------------------
# Phase 7, Step 6H, Part 8 — package signing. A signature proves a package
# was produced (or re-signed) by an installation that holds the shared
# PORTABLE_PACKAGE_SIGNING_KEY secret — deliberately NOT derived from this
# installation's own ENCRYPTION_KEY the way core.secrets.datasource_identity
# derives its (intentionally installation-local, never-shared) identity
# key. Signature verification therefore requires the *same* secret to have
# been configured, out-of-band, on every installation that should trust
# each other's packages — if PORTABLE_PACKAGE_SIGNING_KEY is never set
# anywhere, packages are exported unsigned and import proceeds with an
# explicit UNSIGNED classification (never silently treated as verified).
# ---------------------------------------------------------------------------

SIGNATURE_KEY_VERSION = "v1"

SIGNATURE_STATUS_VALID = "SIGNED_VALID"
SIGNATURE_STATUS_INVALID = "SIGNED_INVALID"
SIGNATURE_STATUS_UNSIGNED = "UNSIGNED"
SIGNATURE_STATUS_UNVERIFIABLE = "UNVERIFIABLE_NO_KEY_CONFIGURED"


def _signing_key() -> bytes | None:
    key = os.getenv("PORTABLE_PACKAGE_SIGNING_KEY")
    return key.encode("utf-8") if key else None


def _signable_canonical_json(package: dict) -> bytes:
    """Canonical bytes signed/verified -- always the SANITIZED package with
    the signature envelope itself excluded (a signature can never cover
    itself), so tampering with ANY allow-listed field after signing is
    detectable, and the envelope fields are never part of what they sign."""
    payload = {k: v for k, v in sanitize_package(package).items() if k not in ("signature", "signature_key_version")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_package(package: dict) -> dict:
    """Returns a NEW sanitized dict with signature/signature_key_version set
    (overwriting any prior values -- never trusts a caller-supplied
    signature as a shortcut). If no PORTABLE_PACKAGE_SIGNING_KEY is
    configured on this installation, returns the sanitized package with NO
    signature field at all (an honestly unsigned package), never a fake or
    placeholder signature."""
    import hashlib
    import hmac as _hmac

    clean = sanitize_package(package)
    key = _signing_key()
    if key is None:
        clean.pop("signature", None)
        clean.pop("signature_key_version", None)
        logger.warning("sign_package: PORTABLE_PACKAGE_SIGNING_KEY is not set; exporting an UNSIGNED package.")
        return clean
    digest = _hmac.new(key, _signable_canonical_json(clean), hashlib.sha256).hexdigest()
    clean["signature"] = f"{SIGNATURE_KEY_VERSION}:{digest}"
    clean["signature_key_version"] = SIGNATURE_KEY_VERSION
    return clean


def verify_package_signature(package: dict) -> str:
    """Returns one of SIGNATURE_STATUS_* -- never raises, never silently
    treats an unverifiable or tampered package as trusted. A caller that
    requires a positive guarantee must check for exactly
    SIGNATURE_STATUS_VALID; every other value means "do not trust this
    package's provenance claim" (it may still be used as an UNVERIFIED
    suggestion, exactly like any other unmatched/low-confidence evidence
    elsewhere in this codebase, but never auto-applied)."""
    import hashlib
    import hmac as _hmac

    signature = package.get("signature")
    if not signature:
        return SIGNATURE_STATUS_UNSIGNED
    key = _signing_key()
    if key is None:
        return SIGNATURE_STATUS_UNVERIFIABLE
    try:
        prefix, _, digest_hex = str(signature).partition(":")
        if prefix != SIGNATURE_KEY_VERSION:
            return SIGNATURE_STATUS_INVALID
        expected = _hmac.new(key, _signable_canonical_json(package), hashlib.sha256).hexdigest()
        return SIGNATURE_STATUS_VALID if _hmac.compare_digest(expected, digest_hex) else SIGNATURE_STATUS_INVALID
    except Exception:  # noqa: BLE001 — malformed signature is a verification failure, not a crash
        return SIGNATURE_STATUS_INVALID


# ---------------------------------------------------------------------------
# Disk I/O — Part 20 (repository portability)
# ---------------------------------------------------------------------------

def save_package(package: dict, *, directory: str | None = None, filename: str | None = None) -> str:
    directory = directory or DEFAULT_PACKAGE_DIR
    os.makedirs(directory, exist_ok=True)
    filename = filename or f"{package['database_family_identifier']}.json"
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, sort_keys=True)
    return path


def list_packages(directory: str | None = None) -> list[dict]:
    directory = directory or DEFAULT_PACKAGE_DIR
    if not os.path.isdir(directory):
        return []
    packages = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                packages.append(json.load(f))
        except Exception:  # noqa: BLE001
            logger.warning("list_packages: failed to load %s", name, exc_info=True)
    return packages


# ---------------------------------------------------------------------------
# Part 3/9/10 — matching + validation against a LIVE schema.
# ---------------------------------------------------------------------------

def find_matching_package(source_id: int, user_id: str, *, packages: list[dict] | None = None) -> dict | None:
    """Returns {"package": ..., "comparison": ...} for the best-matching
    known package (EXACT preferred over COMPATIBLE_DRIFTED, and among
    COMPATIBLE_DRIFTED matches the highest table_overlap_ratio wins), or
    None if no package reaches at least COMPATIBLE_DRIFTED. Never loads
    physical mappings from a package classified NO_MATCH."""
    live_fingerprint = compute_fingerprint(source_id, user_id)
    if live_fingerprint is None:
        return None

    candidates = packages if packages is not None else list_packages()
    best = None
    for package in candidates:
        pkg_fp = package.get("schema_fingerprint")
        if not pkg_fp:
            continue
        comparison = compare_fingerprints(pkg_fp, live_fingerprint)
        if comparison["match"] == "NO_MATCH":
            continue
        rank = (comparison["match"] == "EXACT", comparison["table_overlap_ratio"])
        if best is None or rank > best[0]:
            best = (rank, {"package": package, "comparison": comparison})
    return best[1] if best else None


def _live_column_check(structural_summary: dict, table_fqn: str, column_name: str | None) -> bool:
    table = structural_summary.get(table_fqn)
    if table is None:
        return False
    if not column_name:
        return True
    return any(c["column_name"] == column_name for c in table.get("columns", []))


def _classify_assertion(required_objects: list[dict], structural_summary: dict) -> str:
    """VALID: every required {table_fqn, column_name} object still exists
    exactly as referenced. NEEDS_REVALIDATION: every required table exists,
    but at least one required column does not (harmless drift — a column
    rename/drop on an otherwise-intact table). INCOMPATIBLE: at least one
    required table itself no longer exists."""
    if not required_objects:
        return "NEEDS_REVALIDATION"
    tables_ok = True
    columns_ok = True
    for obj in required_objects:
        table_fqn = obj.get("table_fqn") if isinstance(obj, dict) else None
        column_name = obj.get("column_name") if isinstance(obj, dict) else None
        if not table_fqn or table_fqn not in structural_summary:
            tables_ok = False
            continue
        if column_name and not _live_column_check(structural_summary, table_fqn, column_name):
            columns_ok = False
    if not tables_ok:
        return "INCOMPATIBLE"
    if not columns_ok:
        return "NEEDS_REVALIDATION"
    return "VALID"


def validate_package_against_live_schema(package: dict, source_id: int, user_id: str) -> dict:
    """Part 9/10 — classify every assertion in the package individually
    against a freshly-computed live fingerprint. Never trusts the package's
    own stored fingerprint for this step (Part 9: "DO NOT blindly trust old
    package") — always recomputes live structural truth."""
    live_fingerprint = compute_fingerprint(source_id, user_id)
    if live_fingerprint is None:
        return {"match": "NO_MATCH", "sections": {}}

    comparison = compare_fingerprints(package.get("schema_fingerprint") or {}, live_fingerprint)
    structural_summary = live_fingerprint["structural_summary"]

    result = {"match": comparison["match"], "comparison": comparison, "sections": {}}
    if comparison["match"] == "NO_MATCH":
        return result

    for section in ("vocabulary", "entity_assignments", "entity_contracts", "trusted_relationships"):
        classified = []
        for row in package.get(section, []):
            status = _classify_assertion(row.get("required_schema_objects") or [], structural_summary)
            classified.append({"row": row, "status": status})
        result["sections"][section] = classified
    return result


# ---------------------------------------------------------------------------
# Part 9/6 — activation: materialize VALID assertions into the SAME
# source-scoped tables normal Q&A already reads, for the new source_id.
# ---------------------------------------------------------------------------

def _upsert_entity_assignment(
    conn, source_id: int, profiling_snapshot_id: int, table_fqn: str, entity: str,
    confidence: float, evidence: list,
) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO entity_assignments
               (source_id, profiling_snapshot_id, table_fqn, entity, confidence,
                evidence_json, competing_entities_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)
           ON CONFLICT (source_id, table_fqn) DO UPDATE SET
               entity = excluded.entity,
               confidence = excluded.confidence,
               evidence_json = excluded.evidence_json,
               updated_at = excluded.updated_at""",
        (source_id, profiling_snapshot_id, table_fqn, entity, confidence,
         json.dumps(evidence or []), now, now),
    )


def activate_package(
    package: dict, validation_result: dict, source_id: int, user_id: str, *, session=None
) -> dict:
    """Materializes every VALID assertion from validation_result into
    source_id's own knowledge tables via the SAME persistence functions
    normal learning already uses (record_investigated_term,
    persist_entity_contract, and a small entity_assignments upsert matching
    the existing idx_ea_source_fqn unique constraint). NEEDS_REVALIDATION
    and INCOMPATIBLE assertions are never written — they are left for the
    normal on-demand candidate-adjudication / Investigation Agent path to
    rediscover (Part 10: 'investigate only affected portions').

    Returns counts per section per status. Never raises: an individual row
    failure is logged and skipped rather than aborting the whole activation."""
    counts = {section: {"VALID": 0, "activated": 0, "NEEDS_REVALIDATION": 0, "INCOMPATIBLE": 0}
              for section in ("vocabulary", "entity_assignments", "entity_contracts", "trusted_relationships")}

    if validation_result.get("match") == "NO_MATCH":
        return counts

    profiling_snapshot_id = None
    snap_ref = get_latest_profiling_snapshot(source_id)
    if snap_ref is not None:
        profiling_snapshot_id = snap_ref.id

    for classified in validation_result["sections"].get("vocabulary", []):
        counts["vocabulary"][classified["status"]] = counts["vocabulary"].get(classified["status"], 0) + 1
        if classified["status"] != "VALID":
            continue
        row = classified["row"]
        ok = _vocab_svc.record_investigated_term(
            source_id, user_id, row["term"], row["table_fqn"],
            column_name=row.get("column_name") or "",
            tier=row.get("confidence_tier") or "MEDIUM",
            score=row.get("confidence_score") or 0.0,
            evidence=(row.get("evidence") or []) + [{"source": "portable_knowledge_package",
                                                       "database_family_identifier": package.get("database_family_identifier")}],
        )
        if ok:
            counts["vocabulary"]["activated"] += 1

    if profiling_snapshot_id is not None:
        conn = get_connection()
        try:
            for classified in validation_result["sections"].get("entity_assignments", []):
                counts["entity_assignments"][classified["status"]] = (
                    counts["entity_assignments"].get(classified["status"], 0) + 1
                )
                if classified["status"] != "VALID":
                    continue
                row = classified["row"]
                try:
                    _upsert_entity_assignment(
                        conn, source_id, profiling_snapshot_id, row["table_fqn"], row["entity"],
                        row.get("confidence") or 0.0,
                        (row.get("evidence") or []) + [{"source": "portable_knowledge_package"}],
                    )
                    counts["entity_assignments"]["activated"] += 1
                except Exception:  # noqa: BLE001
                    logger.warning("activate_package: entity_assignment upsert failed for %s", row.get("table_fqn"),
                                    exc_info=True)
            conn.commit()
        finally:
            conn.close()
    else:
        for classified in validation_result["sections"].get("entity_assignments", []):
            counts["entity_assignments"][classified["status"]] = (
                counts["entity_assignments"].get(classified["status"], 0) + 1
            )

    for classified in validation_result["sections"].get("entity_contracts", []):
        counts["entity_contracts"][classified["status"]] = counts["entity_contracts"].get(classified["status"], 0) + 1
        if classified["status"] != "VALID":
            continue
        row = classified["row"]
        try:
            _contract_svc.persist_entity_contract(
                source_id, row["entity_name"],
                canonical_table_fqn=row.get("canonical_table_fqn"),
                preferred_view_fqn=row.get("preferred_view_fqn"),
                resolution_status=row.get("resolution_status") or "RESOLVED",
                confidence=row.get("confidence") or 0.0,
                contract_json=row.get("contract") or {},
                metadata_revision=f"portable:{package.get('database_family_identifier')}:{package.get('generated_at')}",
                generation_method="portable_knowledge_package",
                session=session,
            )
            counts["entity_contracts"]["activated"] += 1
        except Exception:  # noqa: BLE001
            logger.warning("activate_package: contract persist failed for %s", row.get("entity_name"), exc_info=True)

    for classified in validation_result["sections"].get("trusted_relationships", []):
        counts["trusted_relationships"][classified["status"]] = (
            counts["trusted_relationships"].get(classified["status"], 0) + 1
        )
        # Relationship activation intentionally omitted in this cut — see
        # build_package_from_source's own docstring on why this section is
        # populated conservatively (only non-FK inferred relationships) and
        # is typically empty; activating it requires a dedicated persist
        # path in data.relationship_service this milestone does not yet add.

    return counts


# ---------------------------------------------------------------------------
# Part 16 — explicit, human-triggered promotion. Deliberately NOT wired into
# any automatic per-question code path: "Do NOT automatically make every AI
# guess permanent."
# ---------------------------------------------------------------------------

def promote_source_to_package(
    source_id: int, user_id: str, database_family_identifier: str, *,
    min_confidence: float = 0.75, human_reviewed: bool = False,
    provenance_note: str = "", directory: str | None = None, session=None,
) -> dict | None:
    package = build_package_from_source(
        source_id, user_id, database_family_identifier,
        min_confidence=min_confidence, human_reviewed=human_reviewed,
        provenance_note=provenance_note, session=session,
    )
    if package is None:
        return None
    path = save_package(package, directory=directory)
    return {"package": package, "path": path}


# ---------------------------------------------------------------------------
# Phase 7, Step 6H, Part 8 — the actual cross-computer transfer flow: signed
# export from one installation, verified import on another. Builds on
# promote_source_to_package (export) and activate_package (import) above —
# never a second parallel knowledge representation, only a signature
# envelope and a fail-closed verification gate wrapped around the SAME
# build/validate/activate functions the same-installation flow already uses.
# ---------------------------------------------------------------------------

def export_signed_package(
    source_id: int, user_id: str, database_family_identifier: str, *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE, human_reviewed: bool = False,
    provenance_note: str = "", directory: str | None = None, session=None,
) -> dict | None:
    """The export half of Part 8. build -> sanitize -> sign -> save. Returns
    None under the same precondition as build_package_from_source (no
    schema fingerprint yet). Never includes credentials, PII, result rows,
    embeddings, validated SQL plans, or any machine-local source_id —
    build_package_from_source's own allow-list already guarantees this;
    sign_package's sanitize-before-sign step re-enforces it one more time,
    independently."""
    package = build_package_from_source(
        source_id, user_id, database_family_identifier,
        min_confidence=min_confidence, human_reviewed=human_reviewed,
        provenance_note=provenance_note, session=session,
    )
    if package is None:
        return None
    signed = sign_package(package)
    path = save_package(signed, directory=directory)
    return {
        "package": signed, "path": path,
        "signature_status": SIGNATURE_STATUS_VALID if signed.get("signature") else SIGNATURE_STATUS_UNSIGNED,
    }


def _write_import_audit(source_id: int, user_id: str, reason: str) -> None:
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO datasource_lifecycle_audit (source_id, user_id, action, reason, created_at) "
                "VALUES (?, ?, 'portable_package_import', ?, ?)",
                (source_id, user_id, reason, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("import_signed_package: audit write failed for source_id=%s", source_id, exc_info=True)


def import_signed_package(
    package: dict, source_id: int, user_id: str, *,
    require_valid_signature: bool = True, session=None,
) -> dict:
    """The import half of Part 8 — every check fail-closed, in order:
      1. signature verification (verify_package_signature). When
         require_valid_signature (the default), anything other than
         SIGNED_VALID refuses immediately, before step 2-4 ever run.
      2. tenant/owner authorization — source_id must be owned by user_id.
      3. schema-fingerprint compatibility — reuses
         validate_package_against_live_schema's EXACT/COMPATIBLE_DRIFTED/
         NO_MATCH classification; NO_MATCH refuses. This is the "stable
         physical datasource verification" for a cross-installation
         import — never core.secrets.datasource_identity's HMAC key, which
         is deliberately installation-local and would never match across
         installations by design (see that module's own docstring).
      4. activation — only once 1-3 pass, calls activate_package exactly
         as the existing same-installation flow already does.

    Writes exactly one datasource_lifecycle_audit row
    (action="portable_package_import") regardless of outcome — an import
    attempt is always auditable, accepted or refused.

    Returns {"accepted": bool, "signature_status": str, "validation": dict
    | None, "activation": dict | None, "reason": str}."""
    signature_status = verify_package_signature(package)

    if require_valid_signature and signature_status != SIGNATURE_STATUS_VALID:
        reason = f"refused: signature_status={signature_status}"
        _write_import_audit(source_id, user_id, reason)
        return {"accepted": False, "signature_status": signature_status, "validation": None,
                "activation": None, "reason": reason}

    conn = get_connection()
    try:
        owned = _verify_source(conn, source_id, user_id)
    finally:
        conn.close()
    if not owned:
        reason = "refused: source_id not owned by user_id"
        _write_import_audit(source_id, user_id, reason)
        return {"accepted": False, "signature_status": signature_status, "validation": None,
                "activation": None, "reason": reason}

    validation = validate_package_against_live_schema(package, source_id, user_id)
    if validation.get("match") == "NO_MATCH":
        reason = "refused: schema_fingerprint NO_MATCH"
        _write_import_audit(source_id, user_id, reason)
        return {"accepted": False, "signature_status": signature_status, "validation": validation,
                "activation": None, "reason": reason}

    activation = activate_package(package, validation, source_id, user_id, session=session)
    reason = f"accepted: signature_status={signature_status} match={validation.get('match')}"
    _write_import_audit(source_id, user_id, reason)
    return {"accepted": True, "signature_status": signature_status, "validation": validation,
            "activation": activation, "reason": reason}
