from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row) -> dict:
    return {
        "id":         row["id"],
        "name":       row["name"],
        "plan":       row["plan"],
        "status":     row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_tenant(
    tenant_id: str,
    name: str,
    plan: str = "free",
    status: str = "active",
) -> dict:
    """Insert a new tenant record and return it as a dict.

    If a tenant with the given tenant_id already exists, returns the
    existing record without modifying it.
    """
    existing = get_tenant_by_id(tenant_id)
    if existing is not None:
        return existing
    now = _now()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO tenants (id, name, plan, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, name, plan, status, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, name, plan, status, created_at, updated_at FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_tenant_by_id(tenant_id: str) -> dict | None:
    """Return a tenant dict by primary key, or None if not found."""
    conn = get_connection()
    row = conn.execute(
        "SELECT id, name, plan, status, created_at, updated_at FROM tenants WHERE id = ? LIMIT 1",
        (tenant_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_dict(row)


def list_tenants(limit: int = 100) -> list[dict]:
    """Return up to limit tenant records ordered by creation date descending."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, plan, status, created_at, updated_at FROM tenants ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(row) for row in rows]


def update_tenant_status(tenant_id: str, status: str) -> dict | None:
    """Update a tenant's status. Returns the updated tenant dict, or None if not found."""
    conn = get_connection()
    conn.execute(
        "UPDATE tenants SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), tenant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, name, plan, status, created_at, updated_at FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_dict(row)


def update_tenant_plan(tenant_id: str, plan: str) -> dict | None:
    """Update a tenant's plan tier. Returns the updated tenant dict, or None if not found."""
    conn = get_connection()
    conn.execute(
        "UPDATE tenants SET plan = ?, updated_at = ? WHERE id = ?",
        (plan, _now(), tenant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, name, plan, status, created_at, updated_at FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_dict(row)
