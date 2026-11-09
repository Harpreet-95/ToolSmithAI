"""
Phase 7, Step 6H, Part 2 — stable datasource identity.

Computes a versioned, opaque HMAC identity key for a datasource connection
that survives credential rotation and a brand-new local `source_id`, without
ever storing or logging a reversible server/database locator.

Design constraints (from the Step 6H0 audit + the Step 6H spec):
  - never plaintext credentials, never username/password/token in the input;
  - never reversible -- HMAC, not encryption, so the key cannot be decoded
    back into a host/database name;
  - never the numeric source_id, and never schema_fingerprint alone (two
    unrelated databases can share a schema; schema_fingerprint is a separate,
    secondary COMPATIBILITY check elsewhere, never identity);
  - stable across credential rotation -- only locator fields (host/port/
    database) enter the HMAC input, never auth material;
  - tenant-bound -- user_id is part of the input, so two different owners
    connecting the same physical server/database never collide.

Key rotation: an operator may set DATASOURCE_IDENTITY_HMAC_KEY independently
of ENCRYPTION_KEY. If unset, the key is derived deterministically from
ENCRYPTION_KEY so existing installs need zero new configuration. Rotating
either key changes every identity key computed afterward in that
installation; on the next reconnect, no existing row will match by identity,
so the system safely falls back to creating a new row (the existing, already-
correct behavior) -- never a crash, never silent data loss, never a false
match. This is why a reconnect flow must always treat "no identity match
found" as "allocate a new row", not as an error.

Phase 7, Step 6H1, Part 8 -- an operator who wants to rotate the key WITHOUT
orphaning every existing row may instead configure a small key-ring via
DATASOURCE_IDENTITY_HMAC_KEYRING (a JSON object: {"current_key_id": "k2",
"keys": {"k1": "<old secret>", "k2": "<new secret>"}}). Every row's stored
identity_key string carries the key_id that produced it (see
parse_identity_key), so:
  - new computations always use current_key_id's key;
  - a row produced by an OLDER, still-listed key_id is still RECOGNIZABLE
    (verify_identity_key can confirm it without yet rewriting it);
  - core.secrets.datasource_identity_rekey.rekey_datasource_identities can
    then explicitly, transactionally migrate those rows to current_key_id.
A key_id removed from the ring entirely is simply no longer recognizable --
exactly like rotating the single legacy key, those rows fall back to "no
match, allocate new row" until/unless rekeyed while the old key_id was
still listed. Never configuring a keyring (the default) keeps the exact
single-key, two-part "v1:<hex>" format this module always produced --
zero format change for an installation that never opts in.

Cross-installation note: because the key is a local secret, the SAME physical
database connected from two different ToolSmithAI installations computes
DIFFERENT identity keys. This is intentional, not a bug -- an identity key is
never meant to travel (see Part 8 / data.portable_knowledge_service, which
matches across installations via schema_fingerprint plus explicit human
confirmation instead).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from core.config import ENCRYPTION_KEY

IDENTITY_KEY_VERSION = "v1"

# Which params keys are the physical "locator" (never credentials) for a
# given source_type. Deliberately excludes username/password/token/auth_type
# so rotating credentials never changes identity. Unknown source_types fall
# back to the generic (host, port, database) tuple, which already covers
# every connector currently registered (mssql/postgresql/mysql).
_CONNECTOR_LOCATOR_FIELDS: dict[str, tuple[str, ...]] = {
    "mssql": ("host", "port", "database"),
    "postgresql": ("host", "port", "database"),
    "mysql": ("host", "port", "database"),
    # Test-only connector (tests/test_fixture_*.py) — its "physical database"
    # is a local sqlite file path, not a host/port/database triple. Included
    # here (not just defaulted) so the test suite's own reconnect/isolation
    # coverage exercises a real, non-empty locator rather than silently
    # colliding every fixture source under one tenant onto the same key.
    "fixture_relational": ("business_db_path",),
}
_DEFAULT_LOCATOR_FIELDS: tuple[str, ...] = ("host", "port", "database")

# Fields that must NEVER be read into the identity input, defense-in-depth
# against a future connector adding a locator field inadvertently named like
# a credential, or vice versa -- the allow-list above is the real guard, this
# is a second, independent check on the SAME normalized dict.
_FORBIDDEN_LOCATOR_FIELDS = frozenset({
    "password", "pwd", "secret", "token", "username", "user", "uid",
    "api_key", "apikey", "auth_type", "access_key", "connection_string",
})


# Phase 7, Step 6H1, Part 8 -- the key_id used when no keyring is configured
# (the original, single-key behavior). Never written into the identity
# string itself in this mode -- see compute_datasource_identity_key.
DEFAULT_KEY_ID = "default"


def _default_derived_key() -> bytes:
    env_key = os.getenv("DATASOURCE_IDENTITY_HMAC_KEY")
    if env_key:
        return env_key.encode("utf-8")
    return hashlib.sha256(
        b"toolsmith-datasource-identity-v1:" + ENCRYPTION_KEY.encode("utf-8")
    ).digest()


def _derive_identity_hmac_key() -> bytes:
    """Preserved for any existing caller/test that imports this name
    directly -- identical to resolving DEFAULT_KEY_ID through the keyring."""
    return _default_derived_key()


def _load_keyring() -> tuple[dict[str, bytes], str]:
    """Returns (keys_by_id, current_key_id). Reads DATASOURCE_IDENTITY_
    HMAC_KEYRING (JSON: {"current_key_id": "...", "keys": {"id": "secret"}})
    if set; any parse/shape error is treated exactly like "not configured"
    (fails closed to the single-key default, never a crash, never a
    partially-applied ring). When not configured, returns a ring of exactly
    one entry (DEFAULT_KEY_ID -> the legacy derived/env key)."""
    raw = os.getenv("DATASOURCE_IDENTITY_HMAC_KEYRING")
    if not raw:
        return {DEFAULT_KEY_ID: _default_derived_key()}, DEFAULT_KEY_ID
    try:
        parsed = json.loads(raw)
        keys_raw = parsed["keys"]
        current_key_id = parsed["current_key_id"]
        if not isinstance(keys_raw, dict) or current_key_id not in keys_raw:
            raise ValueError("DATASOURCE_IDENTITY_HMAC_KEYRING: current_key_id must be a key in 'keys'.")
        keys = {str(key_id): str(secret).encode("utf-8") for key_id, secret in keys_raw.items()}
        return keys, str(current_key_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "DATASOURCE_IDENTITY_HMAC_KEYRING is set but malformed; falling back to the single-key default. "
            "Identity keys computed right now may not match what a correctly configured ring would produce."
        )
        return {DEFAULT_KEY_ID: _default_derived_key()}, DEFAULT_KEY_ID


def _normalize_locator(source_type: str, params: dict) -> dict:
    fields = _CONNECTOR_LOCATOR_FIELDS.get(source_type, _DEFAULT_LOCATOR_FIELDS)
    normalized: dict = {}
    for field in fields:
        if field in _FORBIDDEN_LOCATOR_FIELDS:
            continue  # unreachable given the allow-lists above; defense-in-depth only
        value = (params or {}).get(field)
        if value is None:
            continue
        if field == "host":
            normalized[field] = str(value).strip().lower()
        elif field == "port":
            try:
                normalized[field] = int(value)
            except (TypeError, ValueError):
                normalized[field] = str(value).strip()
        else:
            normalized[field] = str(value).strip()
    return normalized


def _hmac_digest(key_bytes: bytes, user_id: str, source_type: str, params: dict) -> str:
    locator = _normalize_locator(source_type, params)
    payload = {
        "tenant": str(user_id),
        "source_type": str(source_type),
        "locator": locator,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key_bytes, canonical, hashlib.sha256).hexdigest()


def compute_datasource_identity_key(
    user_id: str, source_type: str, params: dict, *, key_id: str | None = None,
) -> str:
    """Returns "v1:<64-hex>" (no keyring configured, or key_id explicitly
    DEFAULT_KEY_ID -- the original, unchanged format) or "v1:<key_id>:
    <64-hex>" (a keyring is configured and key_id is anything else).
    Deterministic for the same (user_id, source_type, locator, key_id)
    quadruple; never raises (a missing locator field simply narrows the
    input, never fails the computation).

    `key_id` defaults to the ring's current_key_id (or DEFAULT_KEY_ID when
    no ring is configured) -- a caller only ever passes it explicitly to
    compute what a SPECIFIC key_id would produce, e.g.
    core.secrets.datasource_identity_rekey verifying/migrating old records."""
    keys, current_key_id = _load_keyring()
    resolved_key_id = key_id if key_id is not None else current_key_id
    key_bytes = keys.get(resolved_key_id)
    if key_bytes is None:
        raise ValueError(f"Unknown datasource identity key_id: {resolved_key_id!r}")

    digest = _hmac_digest(key_bytes, user_id, source_type, params)
    if resolved_key_id == DEFAULT_KEY_ID:
        return f"{IDENTITY_KEY_VERSION}:{digest}"
    return f"{IDENTITY_KEY_VERSION}:{resolved_key_id}:{digest}"


def parse_identity_key(identity_key: str) -> tuple[str, str] | None:
    """Returns (key_id, hex_digest) for a well-formed identity key string,
    or None for anything malformed -- never raises. Handles both the
    legacy two-part "v1:<hex>" form (key_id implicitly DEFAULT_KEY_ID) and
    the keyring-aware three-part "v1:<key_id>:<hex>" form."""
    if not identity_key:
        return None
    parts = identity_key.split(":")
    if len(parts) == 2 and parts[0] == IDENTITY_KEY_VERSION:
        return DEFAULT_KEY_ID, parts[1]
    if len(parts) == 3 and parts[0] == IDENTITY_KEY_VERSION:
        return parts[1], parts[2]
    return None


def verify_identity_key(identity_key: str, user_id: str, source_type: str, params: dict) -> bool:
    """Recomputes the identity key using the SAME key_id already embedded
    in `identity_key` (never the ring's current key_id) and compares in
    constant time. Used to RECOGNIZE a record produced by an older,
    still-listed key_id without rewriting it -- see
    core.secrets.datasource_identity_rekey for the explicit, transactional
    migration step. Returns False (never raises) for a malformed key, an
    unknown/removed key_id, or an actual mismatch."""
    parsed = parse_identity_key(identity_key)
    if parsed is None:
        return False
    key_id, digest_hex = parsed
    keys, _ = _load_keyring()
    key_bytes = keys.get(key_id)
    if key_bytes is None:
        return False
    expected = _hmac_digest(key_bytes, user_id, source_type, params)
    return hmac.compare_digest(expected, digest_hex)
