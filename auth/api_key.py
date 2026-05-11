import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from core.config import KEY_ROLE_MAP, TENANT_ID_MAP, USER_ID_SALT
from data.audit import log_audit_event
from data.tenant_service import get_tenant_by_id


@dataclass
class AuthenticatedUser:
    role: str
    user_id: str
    tenant_id: str


def _derive_user_id(api_key: str) -> str:
    return hmac.new(
        USER_ID_SALT.encode(),
        api_key.encode(),
        hashlib.sha256,
    ).hexdigest()


def _check_tenant(tenant_id: str) -> None:
    """Reject suspended tenants. Allows request if tenant row is absent or DB is unavailable."""
    try:
        tenant = get_tenant_by_id(tenant_id)
    except Exception:
        return
    if tenant is not None and tenant["status"] != "active":
        raise HTTPException(status_code=403, detail="Tenant is not active")


def require_api_key(x_api_key: str = Header(...)) -> AuthenticatedUser:
    if not KEY_ROLE_MAP:
        raise HTTPException(status_code=500, detail="No API keys configured on server")
    for key, role in KEY_ROLE_MAP.items():
        if secrets.compare_digest(x_api_key, key):
            tenant_id = TENANT_ID_MAP.get(key, "default")
            _check_tenant(tenant_id)
            return AuthenticatedUser(
                role=role,
                user_id=_derive_user_id(key),
                tenant_id=tenant_id,
            )
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_role(role: str):
    def dependency(user: AuthenticatedUser = Depends(require_api_key)) -> AuthenticatedUser:
        if user.role != role:
            log_audit_event({"task_type": "auth_failure", "original_input": "role_violation", "status": "forbidden"})
            raise HTTPException(status_code=403, detail=f"{role.capitalize()} access required")
        return user
    return dependency
