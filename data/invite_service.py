import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from data.db import get_connection

_INVITE_EXPIRY_HOURS = 72


def create_invite(email: str, created_by: str) -> dict:
    """Create an admin invite. Stores only the SHA-256 hash; returns the raw token."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_INVITE_EXPIRY_HOURS)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    email_norm = email.lower().strip()

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO admin_invites"
            " (email, invite_token_hash, role, used, expires_at, created_by, created_at)"
            " VALUES (?, ?, 'admin', 0, ?, ?, ?)",
            (email_norm, token_hash, expires_at, created_by, now),
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "email": email_norm,
            "role": "admin",
            "expires_at": expires_at,
            "invite_token": raw_token,
        }
    finally:
        conn.close()


def consume_invite(email: str, raw_token: str) -> None:
    """
    Validate and atomically mark an invite as used.
    Raises ValueError with a user-facing message on any validation failure:
      - Invalid token
      - Email mismatch
      - Already used
      - Expired
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    email_norm = email.lower().strip()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, email, used, expires_at"
            " FROM admin_invites WHERE invite_token_hash = ?",
            (token_hash,),
        ).fetchone()

        if row is None:
            raise ValueError("Invalid invite token")

        if row["email"] != email_norm:
            raise ValueError("Invite email does not match")

        if row["used"]:
            raise ValueError("Invite has already been used")

        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            raise ValueError("Invite has expired")

        conn.execute(
            "UPDATE admin_invites SET used = 1, used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
