"""
Phase 7, Step 6H1, Part 8 — controlled datasource-identity key rotation.

Supports a small, explicit key-ring (core.secrets.datasource_identity's
_load_keyring) instead of a single hardcoded secret, so an operator can
introduce a NEW key id while still RECOGNIZING records produced by an
earlier, still-approved key id, then migrate those records to the new key
id via rekey_datasource_identities below -- a deliberate, auditable,
transactional operation, never an implicit side effect of rotating a key.

Without this module, rotating DATASOURCE_IDENTITY_HMAC_KEY (or its
fallback derivation from ENCRYPTION_KEY) orphans every existing identity
key: the safe no-match/new-source fallback in
data.datasource_service.reconnect_data_source prevents any cross-source
leakage (a reconnect that no longer matches simply allocates a new row,
exactly like today), but every disconnected source's existing row becomes
permanently unreachable by identity. This module is what lets an operator
recover from that instead of just accepting the orphaning.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.secrets.datasource_identity import compute_datasource_identity_key, parse_identity_key
from core.secrets.manager import get_secret_manager
from data.db import get_connection

logger = logging.getLogger(__name__)

__all__ = ["RekeyReport", "rekey_datasource_identities"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RekeyReport:
    from_key_id: str
    to_key_id: str
    dry_run: bool
    candidate_count: int = 0
    rekeyed_count: int = 0
    skipped_decrypt_failures: list = field(default_factory=list)
    collisions: list = field(default_factory=list)
    committed: bool = False
    error: str | None = None


def _write_rekey_audit(conn, report: RekeyReport, *, committed: bool) -> None:
    # source_id=0 / user_id='system' is this table's sentinel for a
    # system-level operation that is not scoped to one source row --
    # datasource_lifecycle_audit otherwise only ever carries real, owned
    # source_ids (disconnect/reconnect/credentials-removed/permanent-delete).
    reason = (
        f"from_key_id={report.from_key_id} to_key_id={report.to_key_id} "
        f"candidates={report.candidate_count} rekeyed={report.rekeyed_count} "
        f"decrypt_failures={len(report.skipped_decrypt_failures)} "
        f"collisions={len(report.collisions)} dry_run={report.dry_run} committed={committed}"
    )
    try:
        conn.execute(
            "INSERT INTO datasource_lifecycle_audit (source_id, user_id, action, reason, created_at) "
            "VALUES (0, 'system', 'identity_rekey', ?, ?)",
            (reason, _now()),
        )
    except Exception:  # noqa: BLE001
        logger.warning("rekey_datasource_identities: failed to write audit record", exc_info=True)


def rekey_datasource_identities(from_key_id: str, to_key_id: str, *, dry_run: bool = True) -> RekeyReport:
    """Recomputes datasource_identity_key for every row whose CURRENT
    identity key was produced by `from_key_id`, re-deriving it under
    `to_key_id` instead. Which key_id a row used is read directly out of
    its own already-stored identity string (parse_identity_key) -- this
    never brute-forces every historical key against every row, and never
    touches a row outside the requested from_key_id.

    Transactional: either every qualifying row is updated and one audit
    record is written in the SAME commit, or nothing is (collision
    detected, decrypt failure mid-write, or any other exception) -- never
    a partial rekey.

    Collision-checked BEFORE any write: if two different source rows would
    resolve to the same new identity key -- against each other, or against
    an existing, untouched row's current key -- the whole operation aborts
    and reports every colliding pair (source ids and a short key suffix
    only, never the locator or credential that produced it) rather than
    silently merging them. The abort itself is still audited (a collision
    is exactly the kind of event an operator needs a durable record of).

    dry_run=True (the default) computes and reports everything above
    WITHOUT writing anything. Applying a rekey requires an explicit
    dry_run=False call.

    Never exposes a decrypted locator or credential value anywhere in the
    returned report, in logs, or in the audit record this writes -- only
    key ids, counts, and (for a collision) the last 12 characters of the
    new identity key string, which is itself a one-way hash output.
    """
    if from_key_id == to_key_id:
        raise ValueError("from_key_id and to_key_id must differ.")

    report = RekeyReport(from_key_id=from_key_id, to_key_id=to_key_id, dry_run=dry_run)
    secret_manager = get_secret_manager()

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, user_id, source_type, encrypted_config_json, datasource_identity_key "
            "FROM data_source_connections WHERE datasource_identity_key IS NOT NULL"
        ).fetchall()

        all_current_keys = {row["id"]: row["datasource_identity_key"] for row in rows}

        planned: dict[int, str] = {}
        for row in rows:
            parsed = parse_identity_key(row["datasource_identity_key"])
            if parsed is None or parsed[0] != from_key_id:
                continue
            report.candidate_count += 1
            try:
                params = json.loads(secret_manager.decrypt_secret(row["encrypted_config_json"]))
            except Exception:  # noqa: BLE001
                report.skipped_decrypt_failures.append(row["id"])
                continue
            planned[row["id"]] = compute_datasource_identity_key(
                row["user_id"], row["source_type"], params, key_id=to_key_id,
            )

        seen: dict[str, int] = {}
        for source_id, new_key in planned.items():
            if new_key in seen:
                report.collisions.append({"new_key_suffix": new_key[-12:], "source_ids": sorted((seen[new_key], source_id))})
                continue
            seen[new_key] = source_id
        for source_id, existing_key in all_current_keys.items():
            if source_id in planned:
                continue
            if existing_key in seen:
                report.collisions.append({
                    "new_key_suffix": existing_key[-12:],
                    "source_ids": sorted((source_id, seen[existing_key])),
                })

        if report.collisions:
            report.error = f"{len(report.collisions)} identity collision(s) detected; aborted, nothing rekeyed."
            _write_rekey_audit(conn, report, committed=False)
            conn.commit()  # persist the audit record of the aborted attempt only
            return report

        if dry_run:
            report.rekeyed_count = len(planned)
            return report

        try:
            for source_id, new_key in planned.items():
                conn.execute(
                    "UPDATE data_source_connections SET datasource_identity_key = ? WHERE id = ?",
                    (new_key, source_id),
                )
            report.rekeyed_count = len(planned)
            _write_rekey_audit(conn, report, committed=True)
            conn.commit()
            report.committed = True
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            report.rekeyed_count = 0
            report.committed = False
            report.error = f"rekey failed and was rolled back: {type(exc).__name__}"
            logger.exception("rekey_datasource_identities: transaction failed, rolled back")
        return report
    finally:
        conn.close()
